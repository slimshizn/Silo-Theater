"""Household profiles, avatar presets, account sign-up and diagnostics reports.

No Kodi imports: the screens in ``ui_people.py`` and ``ui_settings.py`` call
these with a ``SiloClient`` and plain dicts, so the rules are testable offline.

Server rules (``/api/v2/profiles``): the verified primary profile manages the
household (create, any field of any profile, delete). Any other profile may
change only itself, and never its access limits (``ACCESS_FIELDS``). The
primary profile cannot be deleted.
"""

import copy
import gzip
import hashlib
import io
import json
import re
import tarfile
import time
import uuid
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import present
from .api import ApiError

CONTENT_RATINGS = ("G", "PG", "PG-13", "R", "NC-17")
DEFAULT_CHILD_RATING = "PG"
ADVISORY_AGES = (7, 10, 13, 16, 18)
PIN_LENGTH = 4
NAME_LIMIT = 64
# Members only the household manager may send (the server's self-service check).
ACCESS_FIELDS = ("is_child", "max_content_rating", "max_advisory_age", "require_advisory_age",
                 "library_restrictions_enabled", "allowed_library_ids", "max_playback_quality")
CHOOSE_A_LIBRARY = "Select at least one library for this profile."

# DiceBear presets, as the Silo apps offer them: preset:dicebear:<style>:<seed>.
AVATAR_STYLES = (("fun-emoji", "Fun Emoji"), ("bottts-neutral", "Bottts"), ("pixel-art-neutral", "Pixel Art"),
                 ("identicon", "Identicon"), ("initials", "Initials"))
PRESETS_PER_BATCH = 18
_ADJECTIVES = ("cosmic", "jelly", "starlight", "mango", "bubble", "neon", "marble", "comet", "snappy", "pepper",
               "glimmer", "ripple", "pocket", "candy", "ember", "mochi", "sprout", "twinkle", "whiz", "plasma",
               "mint", "velvet", "crunchy", "sunbeam", "toffee", "splash", "boomer", "lunar", "fizzy", "biscuit",
               "rocket", "cobalt")
_NOUNS = ("otter", "rocket", "fox", "cookie", "gecko", "bunny", "penguin", "puffin", "tiger", "panda", "saturn",
          "wizard", "skater", "nebula", "robot", "pirate", "meteor", "sprite", "pebble", "nova", "dragon", "donut",
          "parrot", "panther", "goblin", "muffin", "laser", "pickle", "falcon", "sunset", "toaster", "bandit")
_SEED = re.compile(r"[^A-Za-z0-9-]+")


# ------------------------------------------------------------------ avatars

def preset_ref(style, seed):
    return "preset:dicebear:{}:{}".format(style, seed)


def parse_preset(ref):
    """(style, seed) for a DiceBear preset reference, else None."""
    parts = str(ref or "").strip().split(":", 3)
    if len(parts) != 4 or parts[0].lower() != "preset" or parts[1].lower() != "dicebear":
        return None
    style, seed = parts[2].strip(), parts[3].strip()
    return (style, seed) if style and seed else None


def preset_image(style, seed):
    """PNG preview of a preset; the server serves SVG, which Kodi cannot draw."""
    query = urlencode((("seed", seed), ("size", "128"), ("radius", "24"), ("backgroundType", "gradientLinear")))
    return present.avatar_image("https://api.dicebear.com/9.x/{}/svg?{}".format(quote(style, safe=""), query))


def initials_seed(name):
    seed = _SEED.sub("-", str(name or "").strip()).strip("-")[:64]
    return seed or "You"


def preset_batch(style, batch=0, name=""):
    """``[(ref, image_url)]`` for one page of a style's presets (Android's seeds)."""
    if style == "initials":
        seed = initials_seed(name)
        return [(preset_ref(style, seed), preset_image(style, seed))]
    if style not in dict(AVATAR_STYLES):
        style = AVATAR_STYLES[0][0]
    presets = []
    for index in range(PRESETS_PER_BATCH):
        adjective = _ADJECTIVES[(batch * 7 + index * 3 + len(style)) % len(_ADJECTIVES)]
        noun = _NOUNS[(batch * 11 + index * 5 + len(style) * 2) % len(_NOUNS)]
        seed = "{}-{}".format(adjective, noun)
        presets.append((preset_ref(style, seed), preset_image(style, seed)))
    return presets


# --------------------------------------------------------------- household

def primary_profile(profiles):
    return next((profile for profile in profiles or [] if profile.get("is_primary")), None)


def find_profile(profiles, profile_id):
    if profile_id is None:
        return None
    return next((profile for profile in profiles or [] if str(profile.get("id")) == str(profile_id)), None)


class _ScopedStore:
    """The session store with this request's profile headers swapped in.

    Writes (token refreshes) still reach the real store; only the profile the
    requests act as differs, so the device's selected profile is untouched.
    """

    def __init__(self, store, profile_id, token):
        self._store = store
        self._profile = {"profile_id": str(profile_id), "profile_token": token}

    def get(self, key, default=None):
        if key in self._profile:
            return self._profile[key]
        return self._store.get(key, default)

    def __getattr__(self, name):
        return getattr(self._store, name)


def acting_client(client, profile_id, token=None):
    """A client whose requests act as ``profile_id`` (with its PIN proof)."""
    scoped = copy.copy(client)
    scoped.store = _ScopedStore(client.store, profile_id, token)
    return scoped


def _profile_path(profile_id):
    return "/profiles/{}".format(quote(str(profile_id), safe=""))


def list_profiles(client):
    """The household with the server's feature flags (``max_advisory_age_supported`` …)."""
    data = client.request("GET", "/profiles", profile=False)
    return data if isinstance(data, dict) else {"items": []}


def assignable_libraries(client):
    """The account's own libraries, read without profile headers (as Android does)."""
    data = client.request("GET", "/user/libraries", profile=False)
    items = data.get("items") if isinstance(data, dict) else None
    return [library for library in items or [] if isinstance(library, dict) and library.get("id") is not None]


def create_profile(client, body):
    return client.request("POST", "/profiles", dict(body), retry_auth=False)


def update_profile(client, profile_id, changes):
    return client.request("PATCH", _profile_path(profile_id), dict(changes), retry_auth=False)


def delete_profile(client, profile_id):
    return client.request("DELETE", _profile_path(profile_id), retry_auth=False)


def form_state(profile=None):
    """Editable values of a profile (or the defaults of a new one)."""
    profile = profile or {}
    age = profile.get("max_advisory_age")
    return {
        "name": str(profile.get("name") or ""),
        "avatar": str(profile.get("avatar") or "") or None,
        "avatar_url": present.avatar_image(profile.get("avatar_url")),
        "has_pin": bool(profile.get("has_pin")),
        "pin": None,  # None unchanged, a string sets it, "" removes it.
        "is_child": bool(profile.get("is_child")),
        "max_content_rating": str(profile.get("max_content_rating") or "") or None,
        "max_advisory_age": age if isinstance(age, int) and not isinstance(age, bool) and age > 0 else None,
        "require_advisory_age": bool(profile.get("require_advisory_age")),
        "library_restrictions_enabled": bool(profile.get("library_restrictions_enabled")),
        "allowed_library_ids": sorted(str(value) for value in profile.get("allowed_library_ids") or []),
    }


def validate_form(form, profiles=(), profile_id=None, manager=True, assignable=None):
    """An error message for the form, or "" when it can be saved."""
    name = form["name"].strip()
    if not name:
        return "Enter a name for this profile."
    if len(name) > NAME_LIMIT:
        return "Profile names can be at most {} characters.".format(NAME_LIMIT)
    for other in profiles or ():
        if str(other.get("id")) != str(profile_id) and str(other.get("name") or "").strip().lower() == name.lower():
            return "A profile with this name already exists."
    pin = form["pin"]
    if pin and (len(pin) != PIN_LENGTH or not pin.isdigit()):
        return "The PIN must be {} digits.".format(PIN_LENGTH)
    if manager and form["library_restrictions_enabled"]:
        allowed = [value for value in form["allowed_library_ids"]
                   if assignable is None or value in {str(item) for item in assignable}]
        if not allowed:
            return CHOOSE_A_LIBRARY
    return ""


def update_body(original, form, manager, flags=None):
    """PATCH members that changed. Null clears; access limits only for a manager."""
    flags = flags or {}
    before = form_state(original)
    body = {}
    name = form["name"].strip()
    if name != before["name"]:
        body["name"] = name
    if form["avatar"] != before["avatar"]:
        body["avatar"] = form["avatar"]  # None removes it.
    if form["pin"]:
        body["pin"] = form["pin"]
    elif form["pin"] == "" and before["has_pin"]:
        body["pin"] = None
    if manager:
        for key in ("is_child", "library_restrictions_enabled"):
            if form[key] != before[key]:
                body[key] = form[key]
        if form["max_content_rating"] != before["max_content_rating"]:
            body["max_content_rating"] = form["max_content_rating"]
        if flags.get("max_advisory_age_supported") and form["max_advisory_age"] != before["max_advisory_age"]:
            body["max_advisory_age"] = form["max_advisory_age"]
        if flags.get("require_advisory_age_supported") and form["require_advisory_age"] != before["require_advisory_age"]:
            body["require_advisory_age"] = form["require_advisory_age"]
        if form["library_restrictions_enabled"] and (
                form["allowed_library_ids"] != before["allowed_library_ids"] or not before["library_restrictions_enabled"]):
            body["allowed_library_ids"] = list(form["allowed_library_ids"])
    return body


def create_body(form, flags=None):
    """POST members; create admits no nulls, so unset members are left out."""
    flags = flags or {}
    body = {"name": form["name"].strip()}
    if form["avatar"]:
        body["avatar"] = form["avatar"]
    if form["pin"]:
        body["pin"] = form["pin"]
    if form["is_child"]:
        body["is_child"] = True
    if form["max_content_rating"]:
        body["max_content_rating"] = form["max_content_rating"]
    if flags.get("max_advisory_age_supported") and form["max_advisory_age"]:
        body["max_advisory_age"] = form["max_advisory_age"]
        if flags.get("require_advisory_age_supported") and form["require_advisory_age"]:
            body["require_advisory_age"] = True
    if form["library_restrictions_enabled"]:
        body["library_restrictions_enabled"] = True
        body["allowed_library_ids"] = list(form["allowed_library_ids"])
    return body


def error_message(exc, primary_name=""):
    """Plain wording for a profile write the server refused."""
    who = primary_name or "the primary profile"
    if exc.code == "profile_verification_required":
        return "Enter the PIN for {} again to manage profiles.".format(who)
    if exc.status == 403:
        return "Only {} can change this. {}".format(who, exc.message).strip()
    if exc.status == 409:
        return exc.message or "This profile could not be saved."
    if exc.status in (400, 422) and "see errors" in (exc.message or ""):
        return "Silo did not accept these profile details. Check them and try again."
    return exc.message or "This profile could not be saved."


# ------------------------------------------------------------------ sign-up

def signup_enabled(client):
    """Whether this server accepts new accounts with an invite code."""
    try:
        status = client.request("GET", "/auth/signup", authenticated=False, profile=False)
    except ApiError:
        return False
    return bool(isinstance(status, dict) and status.get("enabled") is True)


def validate_signup(username, email, password, invite_code):
    if not username.strip():
        return "Enter a username."
    if not email.strip() or "@" not in email:
        return "Enter a valid email address."
    if len(password) < 8:
        return "The password must be at least 8 characters."
    if len(password.encode("utf-8")) > 72:
        return "The password can be at most 72 bytes."
    if not invite_code.strip():
        return "Enter your invite code."
    return ""


def sign_up(client, username, email, password, invite_code):
    """Create the account and its first profile; the tokens are not kept.

    The caller signs in with the same credentials through the normal password
    flow, which stores the session the add-on's way.
    """
    client.request("POST", "/auth/signup", {
        "username": username.strip(), "email": email.strip(), "password": password,
        "invite_code": invite_code.strip(), "create_default_profile": True,
        "default_profile_name": username.strip()[:128]},
        authenticated=False, profile=False, retry_auth=False)


def signup_error(exc):
    if exc.status == 409:
        return "That username or email is already in use."
    if exc.status == 403:
        return "This server is not accepting new accounts."
    if exc.status == 422 or exc.status == 400:
        return "Check the invite code and the details you entered."
    return exc.message or "The account could not be created."


# -------------------------------------------------------------- diagnostics

DIAGNOSTICS_PLATFORM = "android-tv"  # The only server platform value Kodi can honestly claim (on Android).
SECRET_KEYS = re.compile(r"token|password|secret|pin|url|path|host|server|cookie|auth|key", re.IGNORECASE)


def diagnostics_capabilities(client):
    data = client.request("GET", "/diagnostics/capabilities", profile=False)
    return data if isinstance(data, dict) else {}


def _instant(now):
    return datetime.fromtimestamp(now, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def safe_state(values):
    """Keep only plain values whose names cannot hold credentials or locations."""
    state = {}
    for key, value in (values or {}).items():
        if SECRET_KEYS.search(str(key)):
            continue
        if isinstance(value, (bool, int, float)) or value is None:
            state[str(key)[:128]] = value
        elif isinstance(value, str) and "://" not in value and "/" not in value and "\\" not in value:
            state[str(key)[:128]] = value[:128]
    return state


def build_report(capabilities, info, now=None, session_id=None):
    """(manifest bytes, bundle bytes) for a manual report, per the v1 schema.

    ``info``: app_version, kodi_version, os, model, state (dict) and optional
    profile_id. Only ``safe_state`` values reach the bundle.
    """
    now = time.time() if now is None else now
    stamp = _instant(now)
    device = {
        "captured_at": stamp, "provenance": "pre_failure",
        "identity": {"client": "Founders Theater (Kodi add-on)", "app_version": info.get("app_version") or "unknown",
                     "kodi_version": info.get("kodi_version") or "unknown"},
        "display": "not_collected", "audio": "not_collected", "video_codecs": "not_collected",
        "network": "not_collected",
    }
    state = safe_state(info.get("state"))
    line = {"ts": stamp, "run": session_id or "kodi", "lvl": "I", "cat": "other", "tag": "settings",
            "msg": "Settings: " + json.dumps(state, sort_keys=True)[:2000]}
    logs = (json.dumps(line, separators=(",", ":")) + "\n").encode("utf-8")
    manifest = {
        "schema_version": 1,
        "report": {"type": "manual", "captured_at": stamp, "capture_session_id": session_id or str(uuid.uuid4()),
                   "app_version": ("Founders Theater " + str(info.get("app_version") or "")).strip()[:64],
                   "app_build": str(info.get("kodi_version") or "unknown")[:64],
                   "platform": DIAGNOSTICS_PLATFORM, "os_version": str(info.get("os") or "unknown")[:128]},
        "destination": {"server_instance_id": str(capabilities.get("server_instance_id") or "")},
        "consent": {"mode": "manual", "notice_version": int(capabilities.get("consent_notice_version") or 1)},
        "device_summary": {"manufacturer": "unknown", "model": str(info.get("model") or "unknown")[:128],
                           "os": str(info.get("os") or "unknown")[:128], "form_factor": "tv"},
        "playback_session_ids": [],
        "log_summary": {"lines": 1, "bytes_gz": len(gzip.compress(logs, mtime=0)), "dropped_lines": 0,
                        "categories": ["other"], "debug_logging": False},
    }
    if info.get("profile_id"):
        manifest["report"]["profile_id"] = str(info["profile_id"])
    embedded = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    entries = (("manifest.json", embedded), ("device.json", json.dumps(device).encode("utf-8")),
               ("logs.jsonl", logs))
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, data in entries:
            member = tarfile.TarInfo(name)
            member.size, member.mtime, member.mode = len(data), int(now), 0o644
            archive.addfile(member, io.BytesIO(data))
    tar_bytes = raw.getvalue()
    bundle = gzip.compress(tar_bytes, mtime=0)
    manifest["archive"] = {"entries": [name for name, _data in entries], "bytes": len(bundle),
                           "uncompressed_bytes": len(tar_bytes), "sha256": hashlib.sha256(bundle).hexdigest()}
    return json.dumps(manifest, separators=(",", ":")).encode("utf-8"), bundle


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the bearer token to another address.


def _urllib_upload(method, url, headers, data, timeout=60):
    try:
        response = build_opener(_NoRedirect()).open(Request(url, data=data, headers=headers, method=method),
                                                    timeout=timeout)
    except HTTPError as exc:
        response = exc
    except (URLError, OSError) as exc:
        raise ApiError(0, "Could not connect to the Silo server.", "connection_failed") from exc
    with response:
        body = response.read(1024 * 1024)
        status, response_headers = response.code, dict(response.headers)
    try:
        decoded = json.loads(body.decode("utf-8")) if body else None
    except (ValueError, UnicodeError):
        decoded = None
    return status, response_headers, decoded


def send_report(client, manifest, bundle, profile_id=None):
    """POST the two-part report; returns ``{"report_id", "short_id"}``. Never retried."""
    boundary = "silo-" + uuid.uuid4().hex
    parts = []
    for name, filename, kind, data in (("manifest", "manifest.json", "application/json", manifest),
                                       ("bundle", "bundle.tar.gz", "application/gzip", bundle)):
        parts.append("--{}\r\nContent-Disposition: form-data; name=\"{}\"; filename=\"{}\"\r\n"
                     "Content-Type: {}\r\n\r\n".format(boundary, name, filename, kind).encode("ascii"))
        parts.extend((data, b"\r\n"))
    parts.append("--{}--\r\n".format(boundary).encode("ascii"))
    headers = client.headers(profile=False)
    headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    if profile_id:
        headers["X-Profile-Id"] = str(profile_id)
    upload = getattr(client.transport, "upload", None) or _urllib_upload
    status, response_headers, data = upload("POST", client._url("/diagnostics/reports"), headers, b"".join(parts))
    if not 200 <= status < 300:
        problem = data if isinstance(data, dict) else {}
        code = str(problem.get("type", "")).rstrip("/").rsplit("/", 1)[-1]
        raise ApiError(status, str(problem.get("detail") or problem.get("title")
                                   or "Silo returned HTTP {}.".format(status)), code, response_headers)
    return data if isinstance(data, dict) else {}
