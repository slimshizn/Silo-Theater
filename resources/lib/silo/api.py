"""Original stdlib client for the Silo native /api/v2 contract.

Endpoint and field names were checked against Silo-Server/silo-server's public
contracts/api/v2/openapi.json on 2026-10-22. Responses retain their native shapes.
An injected transport is callable (or has a request method) with
``(method, url, headers, body) -> (http_status, response_headers, decoded_json)``.
The default transport uses urllib, TLS verification, a timeout and no redirects.
"""

import copy
import json
import math
import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class ApiError(Exception):
    def __init__(self, status, message, code="", headers=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code
        self.headers = dict(headers or {})


def normalize_server_url(value):
    """Keep a reverse proxy prefix while rejecting embedded credentials."""
    parts = urlsplit(str(value).strip())
    if (parts.scheme not in ("http", "https") or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment or "\\" in parts.netloc):
        raise ValueError("Enter an HTTP or HTTPS server address without credentials or a query.")
    # Evaluating port also rejects invalid/out-of-range ports.
    parts.port
    return urlunsplit((parts.scheme.lower(), parts.netloc, parts.path.rstrip("/"), "", ""))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward passwords or bearer/profile tokens to a redirect target.
        return None


class UrllibTransport:
    def __init__(self, timeout=20):
        self.timeout = timeout
        self.opener = build_opener(_NoRedirect())

    def request(self, method, url, headers, body=None):
        payload = None if body is None else json.dumps(body).encode("utf-8")
        req = Request(url, data=payload, headers=headers, method=method)
        try:
            response = self.opener.open(req, timeout=self.timeout)
        except HTTPError as exc:
            response = exc
        except (URLError, OSError, TimeoutError) as exc:
            raise ApiError(0, "Could not connect to the Silo server.", "connection_failed") from exc
        try:
            with response:
                data = response.read(16 * 1024 * 1024 + 1)
                status = response.code
                response_headers = dict(response.headers)
        except (OSError, TimeoutError) as exc:
            raise ApiError(0, "The Silo server response was interrupted.", "connection_failed") from exc
        if len(data) > 16 * 1024 * 1024:
            raise ApiError(status, "The server response is too large.", "invalid_response")
        if not data:
            return status, response_headers, None
        try:
            decoded = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeError) as exc:
            if not 200 <= status < 300:
                return status, response_headers, None
            raise ApiError(status, "The server returned an invalid JSON response.", "invalid_response") from exc
        return status, response_headers, decoded


class SiloClient:
    AUTH_KEYS = ("access_token", "refresh_token", "expires_at", "user",
                 "profile_id", "profile_token", "profile_token_expires_at",
                 "temporary_session", "bound_profile_id", "session_expires_at", "auth_session_id")

    def __init__(self, server_url, store, transport=None, clock=None):
        self.server_url = normalize_server_url(server_url)
        self.store = store
        self.transport = transport or UrllibTransport()
        self._clock = clock or time.time
        # Keep auth rotation shared across new clients for the same store,
        # without holding the storage/read lock during an HTTP refresh.
        guard = getattr(store, "_lock", None) or threading.RLock()
        with guard:
            self._lock = getattr(store, "_silo_auth_lock", None)
            if self._lock is None:
                self._lock = threading.RLock()
                try:
                    store._silo_auth_lock = self._lock
                except AttributeError:
                    pass  # Plain dict test stores still share their clone lock.
        self._reporting_scope = None
        previous = self.store.get("server_url")
        if previous != self.server_url:
            self.clear_credentials()
            self._update({"server_url": self.server_url})

    def _update(self, values):
        with self._lock:
            self.store.update(values)

    def _delete(self, key):
        if hasattr(self.store, "delete"):
            self.store.delete(key)
        else:
            self.store.pop(key, None)

    def clear_credentials(self):
        # Preserve the chosen sign-in method for a future forced login.
        self._update({key: None for key in self.AUTH_KEYS})

    def reporting_client(self, timeout=1.5):
        """A short-timeout client bound to this exact sign-in/profile context.

        The clone shares the token-rotation lock and store. Refreshes in the same
        login remain valid, while late playback reports cannot use credentials
        from a new login, selected profile, PIN proof, or configured server.
        """
        with self._lock:
            if not self.store.get("auth_session_id"):
                self._update({"auth_session_id": str(uuid.uuid4())})
            clone = copy.copy(self)
            if isinstance(self.transport, UrllibTransport):
                clone.transport = UrllibTransport(timeout=timeout)
            clone._reporting_scope = self._scope()
            return clone

    def _scope(self):
        with self._lock:
            return tuple(self.store.get(key) for key in ("server_url", "auth_session_id", "profile_id", "profile_token"))

    def _check_scope(self):
        if self._reporting_scope is not None and self._scope() != self._reporting_scope:
            raise ApiError(403, "The playback sign-in or selected profile changed.", "playback_scope_changed")

    def _url(self, path, params=None):
        if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
            raise ValueError("API paths must be relative to this server.")
        if "#" in path or "\\" in path:
            raise ValueError("Invalid API path.")
        path = path if path.startswith("/api/v2/") else "/api/v2" + path
        url = self.server_url + path
        if params:
            pairs = []
            for key, value in params.items():
                if value is not None:
                    if isinstance(value, bool):
                        value = str(value).lower()
                    pairs.append((key, value))
            query = urlencode(pairs, doseq=True)
            if query:
                url += ("&" if "?" in url else "?") + query
        return url

    def current_token(self):
        """Read after refresh, including when creating a new media request."""
        with self._lock:
            token = self.store.get("access_token")
            expiry = self.store.get("expires_at")
            if self.store.get("refresh_token") and (
                    not token or isinstance(expiry, (int, float)) and expiry <= self._clock() + 30):
                self.refresh()
            return self.store.get("access_token") or ""

    def headers(self, authenticated=True, profile=True):
        headers = {"Accept": "application/json", "User-Agent": "Silo-Kodi/0.1"}
        if authenticated:
            token = self.current_token()
            if not token:
                raise ApiError(401, "Sign in to Silo to continue.", "authentication_required")
            headers["Authorization"] = "Bearer " + token
            profile_id = self.store.get("profile_id")
            if profile and profile_id:
                headers["X-Profile-Id"] = str(profile_id)
                profile_token = self.store.get("profile_token")
                if profile_token:
                    headers["X-Profile-Token"] = profile_token
        return headers

    def request(self, method, path, body=None, authenticated=True, profile=True,
                params=None, retry_auth=True):
        """Make one request, refreshing once on 401; never retry network errors.

        PIN verification opts out of the 401 replay as well as profile headers.
        This method does not open prompts or turn 403 responses into PIN retries.
        """
        self._check_scope()
        method = method.upper()
        url = self._url(path, params)
        headers = self.headers(authenticated, profile)
        self._check_scope()
        if body is not None:
            headers["Content-Type"] = "application/json"
        send = getattr(self.transport, "request", self.transport)
        status, response_headers, data = send(method, url, headers, body)
        self._check_scope()
        if status == 401 and authenticated and retry_auth and self.store.get("refresh_token"):
            # Serialize rotations. A concurrent caller may have already refreshed.
            with self._lock:
                if headers.get("Authorization") == "Bearer " + (self.store.get("access_token") or ""):
                    self.refresh()
            return self.request(method, path, body, authenticated, profile, params, retry_auth=False)
        if not 200 <= status < 300:
            problem = data if isinstance(data, dict) else {}
            code = str(problem.get("type", "")).rstrip("/").rsplit("/", 1)[-1]
            message = problem.get("detail") or problem.get("title") or "Silo returned HTTP {}.".format(status)
            raise ApiError(status, str(message), code, response_headers)
        return data

    def _save_tokens(self, tokens, method=None, reset_profile=False, session=None):
        if not isinstance(tokens, dict) or not all(isinstance(tokens.get(k), str) and tokens[k]
                                                 for k in ("access_token", "refresh_token")):
            raise ApiError(200, "Silo did not return a valid token pair.", "invalid_response")
        lifetime = tokens.get("expires_in")
        if isinstance(lifetime, bool) or not isinstance(lifetime, (int, float)) or not math.isfinite(lifetime) or lifetime <= 0:
            raise ApiError(200, "Silo did not return a valid token lifetime.", "invalid_response")
        values = {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                  "expires_at": self._clock() + lifetime}
        if "user" in tokens:
            values["user"] = tokens["user"]
        if reset_profile:
            values.update(profile_id=None, profile_token=None, profile_token_expires_at=None,
                          temporary_session=False, bound_profile_id=None, session_expires_at=None,
                          auth_session_id=str(uuid.uuid4()))
        if session:
            values.update(session)
        if method:
            values["auth_method"] = method
        self._update(values)

    def server_info(self):
        return self.request("GET", "/system/info", authenticated=False, profile=False)

    def auth_providers(self):
        return self.request("GET", "/auth/providers", authenticated=False, profile=False)

    def device_capability(self):
        return self.request("GET", "/auth/device/capability", authenticated=False, profile=False)

    def device_start(self, device_name="Kodi", device_platform="kodi"):
        return self.request("POST", "/auth/device/start", {"device_name": device_name,
                            "device_platform": device_platform, "client_purpose": "device_login",
                            "temporary": False}, authenticated=False, profile=False)

    def device_poll(self, device_code):
        result = self.request("POST", "/auth/device/poll", {"device_code": device_code},
                              authenticated=False, profile=False)
        if isinstance(result, dict) and result.get("status") == "approved":
            temporary = bool(result.get("temporary"))
            profile_id = result.get("profile_id") or None
            if temporary and not profile_id:
                raise ApiError(200, "Silo returned a temporary session without its bound profile.", "invalid_response")
            session = {"profile_id": profile_id, "profile_token": result.get("profile_token") or None,
                       "temporary_session": temporary, "bound_profile_id": profile_id if temporary else None,
                       "session_expires_at": result.get("session_expires_at")}
            self._save_tokens(result.get("tokens"), method="device", reset_profile=True, session=session)
        return result

    def device_cancel(self, device_code):
        return self.request("POST", "/auth/device/cancel", {"device_code": device_code},
                            authenticated=False, profile=False)

    def login(self, username, password, provider=None):
        body = {"username": username, "password": password}
        if provider is not None:
            body["provider"] = provider
        result = self.request("POST", "/auth/login", body, authenticated=False, profile=False)
        self._save_tokens(result, method="password", reset_profile=True)
        return result

    def refresh(self):
        with self._lock:
            scope_before = self._scope()
            token = self.store.get("refresh_token")
            if not token:
                raise ApiError(401, "Sign in to Silo again.", "authentication_required")
            try:
                result = self.request("POST", "/auth/refresh", {"refresh_token": token},
                                      authenticated=False, profile=False)
            except ApiError as exc:
                if exc.status in (401, 403) and self._scope() == scope_before:
                    self.clear_credentials()
                raise
            if self._scope() != scope_before:
                raise ApiError(403, "The playback sign-in or selected profile changed.", "playback_scope_changed")
            self._save_tokens(result)
            return result

    def logout(self):
        try:
            if self.store.get("access_token") or self.store.get("refresh_token"):
                return self.request("POST", "/auth/logout", profile=False)
        finally:
            self.clear_credentials()

    def me(self):
        return self.request("GET", "/account/me", profile=False)

    def profiles(self):
        return self.request("GET", "/profiles", profile=False)

    def verify_pin(self, profile_id, pin):
        return self.request("POST", "/profiles/{}/verify-pin".format(quote(str(profile_id), safe="")),
                            {"pin": pin}, profile=False, retry_auth=False)

    def select_profile(self, profile_id, token=None):
        bound = self.store.get("bound_profile_id")
        if self.store.get("temporary_session") and bound:
            if str(profile_id) != str(bound):
                raise ApiError(403, "This temporary sign-in is bound to another profile.", "profile_scope_mismatch")
            token = token or self.store.get("profile_token")
        self._update({"profile_id": str(profile_id), "profile_token": token,
                      "profile_token_expires_at": None})

    def hubs(self, **params):
        return self.request("GET", "/home/sections", params=params)

    def libraries(self):
        return self.request("GET", "/user/libraries")

    def playback_capability(self):
        """Read the server-owned installation identity before a new attempt."""
        return self.request("GET", "/playback/capabilities")

    def items(self, library_id=None, parent_id=None, **filters):
        if parent_id is not None:
            return self.children(parent_id, **filters)
        if library_id is not None:
            filters["library_id"] = library_id
        return self.request("GET", "/catalog", params=filters)

    def item(self, item_id, **params):
        return self.request("GET", "/catalog/items/" + quote(str(item_id), safe=""), params=params)

    def search(self, query, **filters):
        filters["q"] = query
        filters.setdefault("type", "video")
        return self.items(**filters)

    def seasons(self, series_id, **params):
        return self.request("GET", "/catalog/series/{}/seasons".format(quote(str(series_id), safe="")), params=params)

    def episodes(self, season_id, **params):
        return self.request("GET", "/catalog/items/{}/episodes".format(quote(str(season_id), safe="")), params=params)

    def children(self, parent, **params):
        if not isinstance(parent, dict):
            parent = self.item(parent)
        kind = parent.get("type")
        content_id = parent.get("content_id")
        if kind == "series":
            result, child_type = self.seasons(content_id, **params), "season"
        elif kind == "season":
            result, child_type = self.episodes(content_id, **params), "episode"
        else:
            raise ValueError("Only series and seasons have browsable children.")
        return dict(result, items=[dict(child, type=child_type) for child in result.get("items", [])])

    def image_url(self, item, kind="poster"):
        # These URLs are presigned by Silo; never append bearer credentials.
        value = item.get(kind + "_url") or (item.get("still_url") if kind == "poster" else "")
        if not value:
            return ""
        parts = urlsplit(value)
        if parts.scheme and parts.scheme not in ("http", "https"):
            return ""
        if (parts.username or parts.password or parts.fragment or value.startswith("//")
                or any(char in value for char in ("|", "\r", "\n"))):
            return ""
        return urljoin(self.server_url + "/", value)
