"""Loopback relay for media the Silo server serves itself.

Silo serves a stream from its own API (no proxy node) only to requests that
carry the account's sign-in token. Kodi records and logs the path it plays,
headers included, so the token must never be part of it. Kodi instead plays
``http://127.0.0.1:<port>/<id>/...`` and this relay forwards each request to
the server with the add-on's current headers, renewing the token on a 401.

Only one stream's directory is reachable per id, so relative HLS segment URLs
resolve through the relay too. It listens on loopback only, logs nothing and
lives for one playback.
"""

import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from .api import ApiError, _NoRedirect

CHUNK = 256 * 1024
UPSTREAM_TIMEOUT = 120
# Request headers a player needs for seeking and caching; nothing else passes.
FORWARD_REQUEST = ("Range", "If-Range", "If-None-Match", "If-Modified-Since")
FORWARD_RESPONSE = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "ETag",
                    "Last-Modified", "Cache-Control")


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def handle_error(self, request, client_address):
        # Tracebacks could carry upstream URLs; a dropped connection is normal.
        pass


class _Handler(BaseHTTPRequestHandler):
    server_version = "FoundersTheater"
    sys_version = ""

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self._relay("GET")

    def do_HEAD(self):
        self._relay("HEAD")

    def _relay(self, method):
        relay = self.server.relay
        target = relay.resolve(self.path)
        if target is None:
            self.send_error(404)
            return
        url, extra = target
        try:
            response = relay.open(method, url, extra, {key: self.headers[key] for key in FORWARD_REQUEST
                                                       if self.headers.get(key)})
        except (ApiError, URLError, OSError):
            self.send_error(502)
            return
        with response:
            self.send_response(response.code)
            for key in FORWARD_RESPONSE:
                value = response.headers.get(key)
                if value:
                    self.send_header(key, value)
            self.send_header("Connection", "close")
            self.end_headers()
            if method == "HEAD":
                return
            read = getattr(response, "read1", None) or response.read
            try:
                while not relay.closed:
                    data = read(CHUNK)
                    if not data:
                        break
                    self.wfile.write(data)
            except (OSError, ValueError):
                # Kodi closes the connection to seek or stop; the upstream follows.
                pass


class MediaRelay:
    """Serve server-issued media URLs to Kodi without exposing credentials."""

    def __init__(self, client, opener=None):
        self.client = client
        self.opener = opener or build_opener(_NoRedirect())
        self.closed = False
        self._routes = {}
        self._lock = threading.Lock()
        self._server = _Server(("127.0.0.1", 0), _Handler)
        self._server.relay = self
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.25},
                                        name="silo-media-relay", daemon=True)
        self._thread.start()

    @property
    def port(self):
        return self._server.server_address[1]

    def register(self, url, headers=None):
        """A loopback URL for ``url``; its directory stays reachable (HLS segments)."""
        parts = urlsplit(url)
        directory, _, name = parts.path.rpartition("/")
        route = secrets.token_urlsafe(18)
        with self._lock:
            self._routes[route] = ("{}://{}{}/".format(parts.scheme, parts.netloc, directory), dict(headers or {}))
        return "http://127.0.0.1:{}/{}/{}{}".format(self.port, route, name, "?" + parts.query if parts.query else "")

    def resolve(self, path):
        """(upstream URL, extra headers) for a relay path, or None."""
        parts = urlsplit(path)
        route, _, rest = parts.path.lstrip("/").partition("/")
        with self._lock:
            target = self._routes.get(route)
        lowered = rest.lower()
        if (target is None or not rest or "\\" in rest or "%2f" in lowered or "%2e" in lowered or "%5c" in lowered
                or any(segment in ("", ".", "..") for segment in rest.split("/"))):
            return None
        base, extra = target
        return base + rest + ("?" + parts.query if parts.query else ""), extra

    def open(self, method, url, extra, forwarded):
        """Open ``url`` with fresh sign-in headers; one renewal on 401.

        Returns the upstream response (an HTTPError is one too), so the
        player sees the server's real status.
        """
        for attempt in (0, 1):
            headers = self.client.headers()
            headers["Accept"] = "*/*"
            headers.update(extra)
            headers.update(forwarded)
            try:
                return self.opener.open(Request(url, headers=headers, method=method), timeout=UPSTREAM_TIMEOUT)
            except HTTPError as error:
                if error.code == 401 and attempt == 0 and self.client.store.get("refresh_token"):
                    error.close()
                    self.client.refresh_if_current(headers)
                    continue
                return error
        raise ApiError(401, "Sign in to Silo again.", "authentication_required")

    def close(self):
        if self.closed:
            return
        self.closed = True
        self._server.shutdown()
        self._server.server_close()
        with self._lock:
            self._routes.clear()
