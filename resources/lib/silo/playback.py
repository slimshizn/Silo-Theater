"""Native Silo playback core and a lazily imported Kodi player adapter."""

import math
import time
import uuid
from urllib.parse import quote, urlencode, urljoin, urlsplit

from .api import ApiError
from .capabilities import playback_capabilities


class PlaybackError(Exception):
    """A plan cannot be executed by this build."""


def _number(value, signed=False):
    value = float(value)
    if not math.isfinite(value) or (not signed and value < 0):
        raise PlaybackError("The server returned an invalid playback time.")
    return value


def kodi_media_url(server_url, stream):
    """Encode only server-issued headers, without adding account credentials.

    Kodi cannot swap bearer headers on an already open media item, so this
    client deliberately does not negotiate header_authenticated_media_v1.
    """
    source_url = stream.get("url", "")
    if not isinstance(source_url, str) or any(char in source_url for char in ("|", "\\", "\r", "\n", "\t", "\x00")):
        raise PlaybackError("The server returned an invalid media URL.")
    try:
        url = urljoin(server_url.rstrip("/") + "/", source_url)
        parts = urlsplit(url)
        server = urlsplit(server_url)
        # Evaluate ports even on credential-free routes, before Kodi sees them.
        parts.port
        server.port
    except ValueError as error:
        raise PlaybackError("The server returned an invalid media URL.") from error
    if (not stream.get("url") or parts.scheme not in ("http", "https") or
            not parts.hostname or parts.username or parts.password or parts.fragment or
            any(char in url for char in ("|", "\r", "\n"))):
        raise PlaybackError("The server returned an invalid media URL.")
    if stream.get("header_refresh", "none") != "none":
        raise PlaybackError("This playback route requires media header refresh, which this build cannot execute.")
    headers = stream.get("headers") or {}
    for key, value in headers.items():
        if any(char in str(key) + str(value) for char in ("\r", "\n")):
            raise PlaybackError("The server returned invalid media headers.")
    origin = lambda p: (p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80))
    if headers and origin(parts) != origin(server):
        # Cross-origin authenticated media requires explicit origin negotiation,
        # which this build does not advertise. Signed proxy URLs need no headers.
        raise PlaybackError("Authenticated media on another origin is not supported by this build.")
    return url + ("|" + urlencode(headers, quote_via=quote) if headers else "")


class PlaybackSession:
    """Synchronous session core, independent of Kodi and exercised offline."""

    def __init__(self, client, mode="auto", version="0.1.1"):
        self.client, self.mode, self.version = client, mode, version
        self.installation_id = client.store.get("installation_id")
        if not self.installation_id:
            self.installation_id = str(uuid.uuid4())
            client.store.set("installation_id", self.installation_id)
        self.session_id = None
        self.plan = None
        self.sequence = 0
        self.pending_progress = None
        self.stop_body = None
        self.position = 0.0
        self.paused = False
        self.has_sample = False

    def start(self, file_id, start_position=None):
        profile_id = self.client.store.get("profile_id")
        if not profile_id:
            raise PlaybackError("Choose a profile before playing a title.")
        codecs, context = playback_capabilities(self.mode, self.version)
        body = {
            "installation_id": self.installation_id,
            "protocol_version": 3, "client_features": ["subrip_sidecar_v1"],
            "file_id": str(file_id), "profile_id": str(profile_id),
            "playback_attempt_id": str(uuid.uuid4()), "quality_preference": "auto",
            "subtitle_fidelity_preference": "compatible",
            "allow_alternate_versions": False,
            "client_capabilities": codecs, "client_playback_context": context,
        }
        if start_position is not None:
            body["start_position"] = _number(start_position)
        decision = self.client.request("POST", "/playback/start", body)
        if decision.get("protocol_version") != 3:
            raise PlaybackError("The server did not return playback protocol 3.")
        if decision.get("outcome") != "playable":
            terminal = decision.get("terminal") or {}
            raise PlaybackError(terminal.get("message") or terminal.get("reason") or "No playable route is available.")
        plan = decision.get("playback_plan") or {}
        self.session_id = decision.get("session_id") or plan.get("session_id")
        if not self.session_id:
            raise PlaybackError("The playback response is missing its session identifier.")
        # Keep the id before validation so the caller can release a refused plan.
        self.plan = plan
        if plan.get("protocol_version") != 3 or plan.get("delivery") != "original_http":
            raise PlaybackError("This build currently supports original-file direct play only.")
        if any(t.get("executor") == "client" for t in plan.get("transformations", [])):
            raise PlaybackError("This plan needs a client video transformation that is not available.")
        timeline = plan.get("timeline") or {}
        try:
            for key in ("source_start_seconds", "player_start_seconds", "timeline_offset_seconds"):
                _number(timeline[key], signed=key == "timeline_offset_seconds")
        except (KeyError, TypeError, ValueError) as error:
            raise PlaybackError("The server returned an invalid playback timeline.") from error
        kodi_media_url(self.client.server_url, plan.get("stream") or {})
        self.position = _number(timeline["source_start_seconds"])
        return plan

    @property
    def path(self):
        return "/playback/" + quote(str(self.session_id), safe="")

    def progress(self, player_position, paused=False):
        if not self.session_id or not self.plan:
            return
        offset = self.plan["timeline"]["timeline_offset_seconds"]
        self.position = _number(float(player_position) + float(offset))
        self.paused = bool(paused)
        self.has_sample = True
        if self.pending_progress is None:
            self.sequence += 1
            self.pending_progress = {"installation_id": self.installation_id,
                                     "sequence": self.sequence, "position": self.position,
                                     "is_paused": self.paused}
        receipt = self.client.request("POST", self.path + "/progress", self.pending_progress)
        if receipt.get("outcome") not in ("applied", "replayed"):
            raise PlaybackError("The server did not accept the playback progress sample.")
        self.pending_progress = None

    def stop(self):
        if not self.session_id:
            return
        if self.stop_body is None:
            self.sequence += 1
            self.stop_body = {"installation_id": self.installation_id,
                              "stop_id": str(uuid.uuid4())}
            if self.has_sample:
                self.stop_body.update(sequence=self.sequence, position=self.position,
                                      is_paused=self.paused)
        self.client.request("DELETE", self.path, self.stop_body)
        self.session_id = None


def select_file(item):
    """Choose one readable source from a title's available files."""
    if item.get("file_id"):
        return str(item["file_id"])
    for version in item.get("versions", []) + item.get("files", []):
        if version.get("file_id") and not version.get("unreadable"):
            return str(version["file_id"])
    raise PlaybackError("This title has no playable video file.")


class PlaybackController:
    """Own Kodi playback and heartbeat until the user stops or playback ends."""

    def __init__(self, client, addon):
        self.client, self.addon = client, addon

    def play(self, item, start_position=None):
        import xbmc
        import xbmcgui

        class Player(xbmc.Player):
            started = False
            finished = False
            failed = False
            paused = False

            def onAVStarted(self):
                self.started = True

            def onPlayBackStopped(self):
                self.finished = True

            def onPlayBackEnded(self):
                self.finished = True

            def onPlayBackError(self):
                self.failed = True
                self.finished = True

            def onPlayBackPaused(self):
                self.paused = True

            def onPlayBackResumed(self):
                self.paused = False

        session = PlaybackSession(self.client, self.addon.getSetting("dv_mode"),
                                  self.addon.getAddonInfo("version"))
        player, monitor = Player(), xbmc.Monitor()
        opened = False
        self.last_progress_error = None
        try:
            plan = session.start(select_file(item), start_position)
            path = kodi_media_url(self.client.server_url, plan["stream"])
            listitem = xbmcgui.ListItem(label=item.get("title", "Silo"), path=path)
            tag = listitem.getVideoInfoTag()
            tag.setTitle(item.get("title", "Silo"))
            tag.setPlot(item.get("overview", ""))
            listitem.setProperty("IsPlayable", "true")
            if plan["stream"].get("mime_type"):
                listitem.setMimeType(plan["stream"]["mime_type"])
            listitem.setContentLookup(False)
            player.play(path, listitem)
            opened = True
            deadline = time.monotonic() + 30
            while not player.started and not player.finished:
                if monitor.waitForAbort(0.2):
                    player.stop()
                    return
                if time.monotonic() >= deadline:
                    player.stop()
                    raise PlaybackError("Kodi did not start playback within 30 seconds.")
            if player.failed:
                raise PlaybackError("Kodi could not open the original media file.")
            if player.finished:
                return
            start = _number(plan["timeline"]["player_start_seconds"])
            if start:
                player.seekTime(start)
            audio_index = (plan.get("selected_tracks", {}).get("audio") or {}).get("index")
            if audio_index is not None and 0 <= audio_index < len(player.getAvailableAudioStreams()):
                player.setAudioStream(audio_index)
            subtitle = plan.get("subtitle") or {}
            if subtitle.get("mode") == "off":
                player.showSubtitles(False)
            elif subtitle.get("artifact"):
                artifact = subtitle["artifact"]
                player.setSubtitles(kodi_media_url(self.client.server_url,
                                                  {"url": artifact["url"],
                                                   "headers": self.client.headers()}))
            try:
                interval = min(10, max(1, int(self.addon.getSetting("progress_interval") or 10)))
            except ValueError:
                interval = 10
            next_report = 0.0
            while not player.finished:
                if monitor.waitForAbort(0.25):
                    player.stop()
                    break
                try:
                    current = player.getTime()
                except RuntimeError:
                    if not player.isPlaying():
                        break
                    continue
                session.position = _number(current + plan["timeline"]["timeline_offset_seconds"])
                session.paused = player.paused
                session.has_sample = True
                if time.monotonic() >= next_report:
                    try:
                        session.progress(current, player.paused)
                        self.last_progress_error = None
                    except (ApiError, PlaybackError) as error:
                        self.last_progress_error = error
                        xbmc.log("Silo: playback progress was not accepted", xbmc.LOGWARNING)
                        if isinstance(error, ApiError) and error.status in (401, 403, 404):
                            player.stop()
                            raise PlaybackError("The playback session ended or needs sign-in. Open the title again.")
                    next_report = time.monotonic() + interval
            if player.failed:
                raise PlaybackError("Kodi reported a playback error.")
        except (ApiError, PlaybackError) as error:
            if opened and player.isPlaying():
                player.stop()
            xbmcgui.Dialog().ok("Silo — playback", str(error))
        finally:
            if opened and player.isPlaying():
                player.stop()
            try:
                session.stop()
            except (ApiError, PlaybackError):
                xbmc.log("Silo: unable to close playback session; server expiry will release it", xbmc.LOGWARNING)
