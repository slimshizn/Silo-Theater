"""Silo subtitle services: provider search, AI translation and audio sync.

Plain functions over a ``SiloClient``; no Kodi imports except in ``save``.
Request and response shapes follow Silo's native /api/v2 subtitle contract
(docs/subtitles-api.md). Every reader is defensive about missing fields and
never logs URLs or credentials.

Stored subtitles have no session-free byte route for viewers: their bytes are
served through the active playback session's sidecar route, pinned to the
stored row with ``downloaded_subtitle_id``. The timing correction a sync found
is applied on that route too.
"""

import os
import uuid
from urllib.parse import parse_qs, quote, urlsplit

SUBTITLE_LIMIT = 8 * 1024 * 1024
SAVE_EXTENSIONS = ("srt", "vtt", "ass", "ssa")

AI_ACTIVE = ("pending", "running")
AI_TERMINAL = ("completed", "failed", "cancelled")
SYNC_ACTIVE = ("pending", "running")

# Formats the server can translate from (mirrors the Silo apps): text sidecars
# must already be SRT or WebVTT; embedded text tracks are extracted to text.
TRANSLATABLE_TEXT = frozenset({"srt", "subrip", "vtt", "webvtt"})
BITMAP_CODECS = frozenset({"pgs", "hdmv_pgs_subtitle", "dvd_subtitle", "dvdsub", "dvb_subtitle", "dvbsub"})
RETIMABLE = frozenset({"srt", "subrip", "vtt", "webvtt", "ass", "ssa"})


def _path_id(value):
    return quote(str(value), safe="")


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


# ------------------------------------------------------------------ capability probes

def provider_status(client):
    """``{"enabled": bool, "providers": [names]}``; disabled on a bad answer."""
    data = _dict(client.request("GET", "/subtitles/providers/status"))
    providers = [str(name) for name in data.get("providers") or [] if isinstance(name, str) and name]
    return {"enabled": data.get("enabled") is True, "providers": providers}


def ai_status(client):
    data = _dict(client.request("GET", "/subtitles/ai/status"))
    return {"enabled": data.get("enabled") is True, "transcribe_enabled": data.get("transcribe_enabled") is True}


def ai_quota(client):
    data = _dict(client.request("GET", "/subtitles/ai/quota"))
    number = lambda key: data.get(key) if isinstance(data.get(key), int) and not isinstance(data.get(key), bool) else 0
    return {"limited": data.get("limited") is True, "limit": number("limit"), "used": number("used"),
            "remaining": number("remaining"), "period": str(data.get("period") or "")}


def sync_status(client):
    data = _dict(client.request("GET", "/subtitles/sync/status"))
    return {"available": data.get("state") == "available", "auto_sync": data.get("auto_sync") is True,
            "external": data.get("external") is True}


# ------------------------------------------------------------------ stored subtitles and search

def stored(client, file_id):
    """Stored (downloaded or generated) subtitles of a file, oldest first."""
    return _list(_dict(client.request("GET", "/subtitles/" + _path_id(file_id))).get("subtitles"))


def search(client, file_id, languages):
    data = _dict(client.request("POST", "/subtitles/search", {
        "media_file_id": str(file_id), "languages": [str(code) for code in languages if code]}))
    warnings = [str(text) for text in data.get("warnings") or [] if isinstance(text, str) and text]
    results = [row for row in _list(data.get("results")) if row.get("id") and row.get("provider")]
    return {"results": results, "warnings": warnings}


def download(client, file_id, result):
    """Store one provider result on the server (send once; never retried)."""
    score = result.get("score")
    body = {
        "media_file_id": str(file_id),
        "provider": str(result.get("provider") or ""),
        "subtitle_id": str(result.get("id") or ""),
        "language": str(result.get("language") or ""),
        "release_name": str(result.get("release_name") or ""),
        "score": float(score) if isinstance(score, (int, float)) and not isinstance(score, bool) else 0.0,
        "hearing_impaired": result.get("hearing_impaired") is True,
    }
    return _dict(_dict(client.request("POST", "/subtitles/download", body)).get("subtitle"))


# ------------------------------------------------------------------ AI jobs

def create_ai_job(client, file_id, kind, source_index, source_language="", target_language="",
                  start_position=0.0):
    body = {"media_file_id": str(file_id), "kind": kind, "source_index": int(source_index),
            "source_language": str(source_language or ""), "target_language": str(target_language or ""),
            "start_position": max(0.0, float(start_position or 0))}
    return _dict(_dict(client.request("POST", "/subtitles/ai/translate", body)).get("job"))


def ai_job(client, job_id):
    return _dict(_dict(client.request("GET", "/subtitles/ai/jobs/" + _path_id(job_id))).get("job"))


def cancel_ai_job(client, job_id):
    client.request("POST", "/subtitles/ai/jobs/{}/cancel".format(_path_id(job_id)))


# ------------------------------------------------------------------ sync

def sync_key(subtitle):
    """The server's sync key for a subtitle dict, or "" when it cannot be synced.

    Accepts a playback inventory row (``sync_key``), a sync state (``key``), a
    stored subtitle (``id``/``stored_subtitle_id`` with a retimable format) or
    a sidecar URL pinned with ``downloaded_subtitle_id``.
    """
    subtitle = _dict(subtitle)
    for name in ("sync_key", "key"):
        value = subtitle.get(name)
        if isinstance(value, str) and (value.startswith("stored-") or value.startswith("external-")):
            return value
    stored_id = stored_id_of(subtitle)
    fmt = str(subtitle.get("format") or subtitle.get("codec") or "").lower()
    if stored_id and (not fmt or fmt in RETIMABLE):
        return "stored-" + stored_id
    return ""


def stored_id_of(subtitle):
    """A stored subtitle's id from a stored row, sync state or pinned URL."""
    subtitle = _dict(subtitle)
    value = subtitle.get("stored_subtitle_id")
    if value is None and str(subtitle.get("source") or "downloaded") == "downloaded" and "provider" in subtitle:
        value = subtitle.get("id")
    if value is None and subtitle.get("url"):
        values = parse_qs(urlsplit(str(subtitle["url"])).query).get("downloaded_subtitle_id") or []
        value = values[0] if len(values) == 1 else None
    text = str(value) if value is not None else ""
    return text if text.isdigit() and text != "0" else ""


def sync_state(client, file_id, key):
    path = "/subtitles/{}/sync/{}".format(_path_id(file_id), _path_id(key))
    return _dict(_dict(client.request("GET", path)).get("subtitle"))


def start_sync(client, file_id, key):
    path = "/subtitles/{}/sync/{}".format(_path_id(file_id), _path_id(key))
    return _dict(_dict(client.request("POST", path)).get("subtitle"))


# ------------------------------------------------------------------ bytes

def stream_reference(plan):
    """``(session_id, stream_token)`` from a playback plan; "" when absent."""
    plan = _dict(plan)
    session_id = str(plan.get("session_id") or "")
    url = str(_dict(plan.get("stream")).get("url") or "")
    values = parse_qs(urlsplit(url).query).get("st") or []
    return session_id, values[0] if len(values) == 1 else ""


def extension(fmt):
    value = str(fmt or "").lower().lstrip(".")
    value = {"subrip": "srt", "webvtt": "vtt"}.get(value, value)
    return value if value in SAVE_EXTENSIONS else "vtt"


def stored_subtitle_url(session_id, file_id, stored_id, fmt, stream_token=""):
    """The session sidecar route for one stored subtitle (relative to the server).

    SRT is asked for as original bytes, ASS/SSA raw, anything else as WebVTT.
    The path ordinal is ignored by the server once the row is pinned. The URL
    has no leading slash so a reverse-proxy prefix in the server URL survives.
    """
    ext = extension(fmt)
    route = ".srt" if ext == "srt" else ".ass" if ext in ("ass", "ssa") else ".vtt"
    url = "api/v2/stream/{}/subtitles/0{}?file_id={}&downloaded_subtitle_id={}".format(
        _path_id(session_id), route, quote(str(file_id), safe=""), quote(str(stored_id), safe=""))
    if route == ".srt":
        url += "&original=1"
    if stream_token:
        url += "&st=" + quote(str(stream_token), safe="")
    return url


def saved_extension(fmt):
    ext = extension(fmt)
    return "ass" if ext == "ssa" else ext


def fetch(client, url):
    return client.download(url, SUBTITLE_LIMIT)


def save(addon, data, ext):
    """Write subtitle bytes under the add-on profile and return the path.

    Mirrors the playback sidecar handling: one folder, older Silo subtitle
    files removed first, a random file name with the right extension.
    """
    import xbmcvfs

    folder = os.path.join(xbmcvfs.translatePath(addon.getAddonInfo("profile")), "subtitles")
    os.makedirs(folder, mode=0o700, exist_ok=True)
    for name in os.listdir(folder):
        if name.startswith("silo-subtitle-"):
            try:
                os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    path = os.path.join(folder, "silo-subtitle-{}.{}".format(uuid.uuid4().hex, ext if ext in SAVE_EXTENSIONS else "srt"))
    with open(path, "wb") as handle:
        handle.write(data)
    return path


# ------------------------------------------------------------------ sources for translation

def translatable(source, codec):
    codec = str(codec or "").lower()
    if source == "embedded":
        return codec not in BITMAP_CODECS
    return codec in TRANSLATABLE_TEXT


def subtitle_sources(version, stored_rows=(), inventory=None):
    """Translatable subtitle sources as ``{"index","label","language","source"}``.

    ``index`` is Silo's combined ordinal: sidecar files, then embedded tracks,
    then stored subtitles in creation order. A playback inventory, when given,
    is the authority; otherwise it is rebuilt from the catalog version and the
    stored list.
    """
    rows = []
    if inventory:
        for track in _list(inventory):
            index = track.get("combined_index")
            if not isinstance(index, int) or isinstance(index, bool) or index < 0:
                continue
            source = str(track.get("source") or "")
            rows.append((index, source, track.get("codec"), track.get("language"), track.get("label")))
    else:
        tracks = _list(_dict(version).get("subtitle_tracks"))
        external = [track for track in tracks if track.get("external")]
        embedded = [track for track in tracks if not track.get("external")]
        for index, track in enumerate(external + embedded):
            rows.append((index, "external" if track.get("external") else "embedded", track.get("codec"),
                         track.get("language"), track.get("title") or track.get("embedded_title")
                         or track.get("file_name")))
        base = len(tracks)
        for offset, row in enumerate(_list(list(stored_rows))):
            rows.append((base + offset, "downloaded", row.get("format"), row.get("language"),
                         row.get("release_name")))
    result = []
    for index, source, codec, language, label in rows:
        if not translatable(source, codec):
            continue
        language = str(language or "")
        name = (language.upper() or "Unknown") + (" · " + str(label) if label else "")
        kind = {"external": "File", "embedded": "Embedded", "downloaded": "Downloaded"}.get(source, source.title())
        result.append({"index": index, "label": name, "language": language, "source": source,
                       "kind": kind, "codec": str(codec or "").lower()})
    return result


def audio_sources(version):
    """``(ordinal, label)`` per audio track; -1 asks for the default track."""
    result = [(-1, "Default audio track")]
    for index, track in enumerate(_list(_dict(version).get("audio_tracks"))):
        label = str(track.get("language") or "Unknown").upper()
        title = track.get("title") or track.get("embedded_title")
        if title:
            label += " · " + str(title)
        if track.get("codec"):
            label += " ({})".format(str(track["codec"]).upper())
        result.append((index, label))
    return result
