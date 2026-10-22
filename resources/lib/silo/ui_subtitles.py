"""Subtitle search, AI translation and audio sync screens.

Opened through ``application.open_screen("subtitle_search" | "subtitle_translate"
| "subtitle_sync", ...)``. Each flow returns a local subtitle file path Kodi can
load (``xbmc.Player().setSubtitles(path)``) or None.

The screens use Kodi's own select and progress dialogs, which are remote
friendly and draw over fullscreen video. Server calls live in
``svc_subtitles``. A stored subtitle's bytes come through the live playback
session (see svc_subtitles), so callers that are playing pass ``plan`` (the
playback plan) or ``session_id``/``stream_token``. Without a session the
subtitle is still added on the server and offered the next time the title
plays.
"""

import time

import xbmcgui

from . import svc_subtitles as svc
from .api import ApiError
from .media import default_version

HEADING_SEARCH = "Search subtitles"
HEADING_AI = "AI subtitles"
HEADING_SYNC = "Sync subtitles"
POLL_SECONDS = 2.0
# A job that never reaches a terminal state should not hold the dialog forever.
POLL_LIMIT_SECONDS = 3 * 60 * 60

# (BCP 47 tag, name) offered after the profile's own languages.
LANGUAGES = (
    ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"), ("it", "Italian"),
    ("pt", "Portuguese"), ("pt-BR", "Portuguese (Brazil)"), ("nl", "Dutch"), ("sv", "Swedish"),
    ("da", "Danish"), ("no", "Norwegian"), ("fi", "Finnish"), ("pl", "Polish"), ("cs", "Czech"),
    ("hu", "Hungarian"), ("ro", "Romanian"), ("el", "Greek"), ("tr", "Turkish"), ("ru", "Russian"),
    ("uk", "Ukrainian"), ("ar", "Arabic"), ("he", "Hebrew"), ("hi", "Hindi"), ("th", "Thai"),
    ("vi", "Vietnamese"), ("id", "Indonesian"), ("ms", "Malay"), ("ja", "Japanese"), ("ko", "Korean"),
    ("zh-Hans", "Chinese (Simplified)"), ("zh-Hant", "Chinese (Traditional)"),
)
LANGUAGE_NAMES = {code.lower(): name for code, name in LANGUAGES}

SYNC_PHASES = {"queued": "Waiting for the server…", "analyzing": "Listening to the audio…",
               "matching": "Matching subtitle lines…"}
SYNC_FAILURES = {
    "subtitle_changed": "The subtitle or its timing changed while it was being synced. Try again.",
    "no_audio": "The server cannot read audio from this file.",
    "unavailable": "No server can analyze the audio right now. Try again later.",
}


# ------------------------------------------------------------------ small helpers

def language_name(code):
    code = str(code or "")
    return LANGUAGE_NAMES.get(code.lower()) or code.upper() or "Unknown"


def language_choices(application):
    """Profile subtitle and audio languages first, then common languages."""
    profile = getattr(application, "profile", None) or {}
    preferred = [profile.get("subtitle_language"), profile.get("language")]
    result, seen = [], set()
    for code in [value for value in preferred if isinstance(value, str) and value] + [code for code, _ in LANGUAGES]:
        if code.lower() not in seen:
            seen.add(code.lower())
            result.append((code, language_name(code)))
    return result


def _notify(application, message):
    try:
        application.dialog.notification("Silo", message, time=3500, sound=False)
    except (AttributeError, RuntimeError, TypeError):
        pass


def _session(plan, session_id, stream_token):
    if plan and not session_id:
        session_id, token = svc.stream_reference(plan)
        stream_token = stream_token or token
    return str(session_id or ""), str(stream_token or "")


def _inventory(plan):
    subtitle = (plan or {}).get("subtitle") if isinstance(plan, dict) else None
    inventory = subtitle.get("inventory") if isinstance(subtitle, dict) else None
    return inventory if isinstance(inventory, list) else None


def _wait(application, seconds):
    """Sleep between polls; True when Kodi is shutting down."""
    monitor = getattr(application, "monitor", None)
    if monitor is not None:
        return bool(monitor.waitForAbort(seconds))
    time.sleep(seconds)
    return False


def _select(application, heading, labels, preselect=0):
    if not labels:
        return -1
    return application.dialog.select(heading, labels, preselect=preselect)


def _percent(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return max(0, min(100, int(round(value * 100 if value <= 1 else value))))


def _version(item, file_id):
    return default_version(item or {}, preferred_file_id=file_id) or {}


def _stored_path(application, file_id, stored_id, fmt, session_id, stream_token):
    """Fetch a stored subtitle through the session and save it; path or None."""
    url = svc.stored_subtitle_url(session_id, file_id, stored_id, fmt, stream_token)
    data = svc.fetch(application.client, url)
    return svc.save(application.addon, data, svc.saved_extension(fmt))


def _deliver(application, file_id, stored_id, fmt, session_id, stream_token, ready_message):
    if not session_id:
        _notify(application, "Subtitles added. They appear in the subtitle list the next time this plays.")
        return None
    try:
        path = _stored_path(application, file_id, stored_id, fmt, session_id, stream_token)
    except (ApiError, OSError):
        application.dialog.ok("Silo", "The subtitles were added on the server but could not be loaded here. "
                                      "They appear in the subtitle list the next time this plays.")
        return None
    _notify(application, ready_message)
    return path


# ------------------------------------------------------------------ search

def result_item(result):
    """A details ListItem for one search result."""
    title = str(result.get("release_name") or "").strip() or "Untitled subtitle"
    facts = [str(result.get("provider") or "").strip() or "Provider", language_name(result.get("language"))]
    downloads = result.get("downloads")
    if isinstance(downloads, int) and not isinstance(downloads, bool) and downloads > 0:
        facts.append("{:,} downloads".format(downloads))
    score = result.get("score")
    if isinstance(score, (int, float)) and not isinstance(score, bool) and score > 0:
        facts.append("Score {:g}".format(round(score, 1)))
    if result.get("hearing_impaired") is True:
        facts.append("SDH")
    if result.get("format"):
        facts.append(str(result["format"]).upper())
    item = xbmcgui.ListItem(label=title, label2=" · ".join(facts))
    item.setProperty("silo.provider", str(result.get("provider") or ""))
    item.setProperty("silo.language", str(result.get("language") or ""))
    item.setProperty("silo.hearing_impaired", "true" if result.get("hearing_impaired") is True else "")
    return item


def search_subtitles(application, item, file_id, plan=None, session_id=None, stream_token=None):
    """Search providers, store the chosen result and return its local path."""
    return application.guard(lambda: _search(application, item, file_id, plan, session_id, stream_token))


def _search(application, item, file_id, plan, session_id, stream_token):
    client = application.client
    if not file_id:
        application.dialog.ok(HEADING_SEARCH, "Choose a version of this title first.")
        return None
    if not svc.provider_status(client)["enabled"]:
        application.dialog.ok(HEADING_SEARCH, "No subtitle providers are set up on this Silo server.")
        return None
    languages = language_choices(application)
    while True:
        picked = _select(application, "Subtitle language", [name for _, name in languages])
        if picked < 0:
            return None
        code = languages[picked][0]
        progress = xbmcgui.DialogProgress()
        progress.create(HEADING_SEARCH, "Searching {} subtitles…".format(language_name(code)))
        try:
            found = svc.search(client, file_id, [code])
        finally:
            progress.close()
        if found["warnings"]:
            _notify(application, found["warnings"][0])
        results = found["results"]
        if not results:
            application.dialog.ok(HEADING_SEARCH, "No {} subtitles were found.".format(language_name(code)))
            continue
        chosen = application.dialog.select("{} subtitles".format(language_name(code)),
                                           [result_item(result) for result in results], useDetails=True)
        if chosen < 0:
            continue
        result = results[chosen]
        progress = xbmcgui.DialogProgress()
        progress.create(HEADING_SEARCH, "Downloading subtitles…")
        try:
            # Non-retryable on the server: sent exactly once, errors shown as is.
            subtitle = svc.download(client, file_id, result)
        finally:
            progress.close()
        stored_id = str(subtitle.get("id") or "")
        if not stored_id:
            application.dialog.ok(HEADING_SEARCH, "The server did not return the downloaded subtitle.")
            return None
        session_id, stream_token = _session(plan, session_id, stream_token)
        return _deliver(application, file_id, stored_id, subtitle.get("format") or result.get("format"),
                        session_id, stream_token, "Subtitles added")


# ------------------------------------------------------------------ AI translation

def translate_subtitles(application, item, file_id, plan=None, session_id=None, stream_token=None,
                        position=0.0):
    """Translate a subtitle track or transcribe audio; return the result's path."""
    return application.guard(lambda: _translate(application, item, file_id, plan, session_id, stream_token,
                                                position))


def _translate(application, item, file_id, plan, session_id, stream_token, position):
    client = application.client
    if not file_id:
        application.dialog.ok(HEADING_AI, "Choose a version of this title first.")
        return None
    status = svc.ai_status(client)
    if not status["enabled"] and not status["transcribe_enabled"]:
        application.dialog.ok(HEADING_AI, "AI subtitles are not turned on for this Silo server.")
        return None
    version = _version(item, file_id)
    sources = []
    if status["enabled"]:
        inventory = _inventory(plan)
        rows = []
        if inventory is None:
            try:
                rows = svc.stored(client, file_id)
            except ApiError:
                rows = []  # Stored tracks are optional sources; sidecars still count.
        sources = svc.subtitle_sources(version, rows, inventory)
    modes = []
    if sources:
        modes.append(("translate", "Translate a subtitle track"))
    if status["transcribe_enabled"]:
        modes.append(("audio", "Create subtitles from audio"))
    if not modes:
        application.dialog.ok(HEADING_AI, "This title has no text subtitle track to translate.")
        return None
    if len(modes) > 1:
        picked = _select(application, HEADING_AI, [label for _, label in modes])
        if picked < 0:
            return None
        mode = modes[picked][0]
    else:
        mode = modes[0][0]
    languages = language_choices(application)

    if mode == "translate":
        if len(sources) > 1:
            picked = _select(application, "Translate from",
                             ["{} ({})".format(row["label"], row["kind"]) for row in sources])
            if picked < 0:
                return None
            source = sources[picked]
        else:
            source = sources[0]
        targets = [entry for entry in languages if entry[0].lower() != source["language"].lower()]
        picked = _select(application, "Translate to", [name for _, name in targets])
        if picked < 0:
            return None
        request = ("translate", source["index"], source["language"], targets[picked][0])
    else:
        quota = svc.ai_quota(client)
        if quota["limited"] and quota["remaining"] <= 0:
            period = " this {}".format(quota["period"]) if quota["period"] else ""
            application.dialog.ok(HEADING_AI, "You have used all {} audio transcriptions{}.".format(
                quota["limit"], period))
            return None
        audio = svc.audio_sources(version)
        index = audio[0][0]
        if len(audio) > 2:
            picked = _select(application, "Audio track", [label for _, label in audio])
            if picked < 0:
                return None
            index = audio[picked][0]
        if status["enabled"]:
            targets = [("", "Spoken language (no translation)")] + languages
            picked = _select(application, "Subtitle language", [name for _, name in targets])
            if picked < 0:
                return None
            target = targets[picked][0]
        else:
            target = ""
        request = ("transcribe_translate" if target else "transcribe", index, "", target)

    kind, source_index, source_language, target_language = request
    try:
        # Non-retryable on the server: sent exactly once.
        job = svc.create_ai_job(client, file_id, kind, source_index, source_language, target_language,
                                position)
    except ApiError as error:
        if error.status == 429 or error.code == "rate_limited":
            application.dialog.ok(HEADING_AI, error.message or "The AI subtitle limit has been reached.")
            return None
        raise
    job = _follow_ai_job(application, job)
    if job is None:
        return None
    state = job.get("status")
    if state == "completed" and job.get("result_subtitle_id"):
        stored_id = str(job["result_subtitle_id"])
        fmt = "srt"
        try:
            row = next((row for row in svc.stored(client, file_id) if str(row.get("id")) == stored_id), None)
            fmt = (row or {}).get("format") or fmt
        except ApiError:
            pass
        session_id, stream_token = _session(plan, session_id, stream_token)
        return _deliver(application, file_id, stored_id, fmt, session_id, stream_token, "AI subtitles ready")
    if state == "cancelled":
        _notify(application, "AI subtitles cancelled")
        return None
    application.dialog.ok(HEADING_AI, job.get("error_message") or "Subtitle processing failed.")
    return None


def _follow_ai_job(application, job):
    """Show a job's progress until it ends; None when the viewer cancelled."""
    client = application.client
    job_id = job.get("id")
    if not job_id:
        application.dialog.ok(HEADING_AI, "The server did not return the new job.")
        return None
    progress = xbmcgui.DialogProgress()
    progress.create(HEADING_AI, "Starting…")
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    try:
        while job.get("status") not in svc.AI_TERMINAL:
            progress.update(_percent(job.get("progress")), str(job.get("progress_message") or "Working…"))
            if progress.iscanceled():
                svc.cancel_ai_job(client, job_id)
                # Completion can win the race; read the real outcome once.
                latest = svc.ai_job(client, job_id)
                if latest.get("status") == "completed":
                    return latest
                _notify(application, "AI subtitles cancelled")
                return None
            if _wait(application, POLL_SECONDS) or time.monotonic() > deadline:
                return None
            job = svc.ai_job(client, job_id) or job
        return job
    finally:
        progress.close()


# ------------------------------------------------------------------ sync

def sync_subtitles(application, item, file_id, subtitle, plan=None, session_id=None, stream_token=None):
    """Align ``subtitle`` to the audio on the server; return the retimed file's path."""
    return application.guard(lambda: _sync(application, item, file_id, subtitle, plan, session_id,
                                           stream_token))


def _sync(application, item, file_id, subtitle, plan, session_id, stream_token):
    client = application.client
    subtitle = subtitle if isinstance(subtitle, dict) else {}
    if not file_id:
        application.dialog.ok(HEADING_SYNC, "Choose a version of this title first.")
        return None
    status = svc.sync_status(client)
    if not status["available"]:
        application.dialog.ok(HEADING_SYNC, "Subtitle sync is not available on this Silo server.")
        return None
    key = svc.sync_key(subtitle)
    if not key:
        application.dialog.ok(HEADING_SYNC, "Only SRT, WebVTT, ASS and SSA subtitles added to Silo or "
                                            "stored next to the video can be synced.")
        return None
    if key.startswith("external-") and not status["external"]:
        application.dialog.ok(HEADING_SYNC, "This server cannot sync subtitle files stored next to the video.")
        return None
    state = svc.start_sync(client, file_id, key)
    job = state.get("sync") if isinstance(state.get("sync"), dict) else {}
    progress = xbmcgui.DialogProgress()
    progress.create(HEADING_SYNC, "Starting…")
    deadline = time.monotonic() + POLL_LIMIT_SECONDS
    try:
        while job.get("status") in svc.SYNC_ACTIVE or not job:
            progress.update(_percent(job.get("progress")), SYNC_PHASES.get(job.get("phase"), "Syncing…"))
            if progress.iscanceled():
                # There is no cancel operation; the job finishes on the server
                # and its timing applies whenever the subtitle loads again.
                _notify(application, "Sync continues on the server")
                return None
            if _wait(application, POLL_SECONDS) or time.monotonic() > deadline:
                return None
            state = svc.sync_state(client, file_id, key) or state
            job = state.get("sync") if isinstance(state.get("sync"), dict) else {}
    finally:
        progress.close()
    outcome = job.get("status")
    if outcome == "already_synced":
        _notify(application, "These subtitles already match the audio")
        return None
    if outcome == "no_match":
        application.dialog.ok(HEADING_SYNC, "No timing lines these subtitles up with the audio. They probably "
                                            "belong to another release.")
        return None
    if outcome != "synced":
        application.dialog.ok(HEADING_SYNC, SYNC_FAILURES.get(job.get("failure"), "The subtitles could not be synced."))
        return None
    message = "Subtitles synced" + _offset_text(job.get("result"))
    fmt = state.get("format") or subtitle.get("format") or subtitle.get("codec")
    session_id, stream_token = _session(plan, session_id, stream_token)
    url = _sidecar_url(subtitle, plan, key)
    try:
        if url:
            data = svc.fetch(client, url)
            return _saved(application, data, fmt, message)
        stored_id = svc.stored_id_of(state) or svc.stored_id_of(subtitle)
        if stored_id and session_id:
            return _saved(application, None, fmt, message, file_id=file_id, stored_id=stored_id,
                          session_id=session_id, stream_token=stream_token)
    except (ApiError, OSError):
        pass
    application.dialog.ok(HEADING_SYNC, "The subtitles were synced on the server. The new timing applies the next "
                                        "time they load.")
    return None


def _saved(application, data, fmt, message, file_id=None, stored_id=None, session_id="", stream_token=""):
    if data is None:
        path = _stored_path(application, file_id, stored_id, fmt, session_id, stream_token)
    else:
        path = svc.save(application.addon, data, svc.saved_extension(fmt))
    _notify(application, message)
    return path


def _sidecar_url(subtitle, plan, key):
    """The session sidecar URL for the synced track, when one is known."""
    url = subtitle.get("url")
    if isinstance(url, str) and url:
        return url
    for track in _inventory(plan) or []:
        if isinstance(track, dict) and track.get("sync_key") == key and isinstance(track.get("url"), str):
            return track["url"] or None
    return None


def _offset_text(result):
    offset = (result or {}).get("offset_ms") if isinstance(result, dict) else None
    if isinstance(offset, bool) or not isinstance(offset, (int, float)) or not offset:
        return ""
    return " ({:+.1f} s)".format(offset / 1000.0)
