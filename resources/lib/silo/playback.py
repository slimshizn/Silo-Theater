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

    def __init__(self, client, mode="auto", version="0.2.0", options=None):
        self.client, self.mode, self.version = client, mode, version
        self.options = dict(options or {})
        # /api/v2 calls this installation_id, but it identifies the SERVER.
        # A locally minted UUID is always refused with installation_changed.
        self.installation_id = None
        self.server_capabilities = None
        self.session_id = None
        self.plan = None
        self.sequence = 0
        self.pending_progress = None
        self.stop_body = None
        self.position = 0.0
        self.paused = False
        self.has_sample = False

    def _negotiate(self):
        capability = self.client.request("GET", "/playback/capabilities", None)
        if not isinstance(capability, dict) or capability.get("state") != "available" or capability.get("allowed") is not True:
            raise PlaybackError("Playback is not available for this profile on the server.")
        if 3 not in capability.get("protocol_versions", []):
            raise PlaybackError("The server does not offer playback protocol 3.")
        if "original_http" not in capability.get("deliveries", []):
            raise PlaybackError("The server does not offer original-file playback.")
        identity = capability.get("installation_id")
        if not isinstance(identity, str) or not identity:
            raise PlaybackError("The server did not provide its playback installation identity.")
        self.installation_id = identity
        self.server_capabilities = capability
        # This diagnostic key cannot be confused with old client-minted ids.
        self.client.store.set("playback_installation_id", identity)
        return [feature for feature in ("subrip_sidecar_v1",) if feature in capability.get("features", [])]

    def start(self, file_id, start_position=None):
        profile_id = self.client.store.get("profile_id")
        if not profile_id:
            raise PlaybackError("Choose a profile before playing a title.")
        features = self._negotiate()
        codecs, context = playback_capabilities(self.mode, self.version, self.options.get("max_resolution", "auto"))
        body = {
            "installation_id": self.installation_id,
            "protocol_version": 3, "client_features": features,
            "file_id": str(file_id), "profile_id": str(profile_id),
            "playback_attempt_id": str(uuid.uuid4()), "quality_preference": "auto",
            "subtitle_fidelity_preference": "compatible",
            "progress_persistence": "server",
            "allow_alternate_versions": False,
            "client_capabilities": codecs, "client_playback_context": context,
        }
        if start_position is not None:
            body["start_position"] = _number(start_position)
        try:
            decision = self.client.request("POST", "/playback/start", body)
        except ApiError as error:
            if error.status != 409 or error.code != "installation_changed":
                raise
            # This exact refusal happens before the server creates an attempt.
            # Refresh discovery once; do not retry unrelated conflicts or loops.
            body["client_features"] = self._negotiate()
            body["installation_id"] = self.installation_id
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

    def record_position(self, player_position, paused=False):
        """Keep the latest confirmed local position even between heartbeats."""
        if not self.session_id or not self.plan:
            return
        offset = self.plan["timeline"]["timeline_offset_seconds"]
        self.position = _number(float(player_position) + float(offset))
        self.paused = bool(paused)
        self.has_sample = True

    def _send_progress(self):
        receipt = self.client.request("POST", self.path + "/progress", self.pending_progress)
        if receipt.get("outcome") not in ("applied", "replayed"):
            raise PlaybackError("The server did not accept the playback progress sample.")
        self.pending_progress = None

    def progress(self, player_position, paused=False):
        if not self.session_id or not self.plan:
            return
        self.record_position(player_position, paused)
        if self.pending_progress is not None:
            previous = self.pending_progress
            self._send_progress()  # Identical sequence/body after a lost response.
            if previous["position"] == self.position and previous["is_paused"] == self.paused:
                return
        self.sequence += 1
        self.pending_progress = {"installation_id": self.installation_id,
                                 "sequence": self.sequence, "position": self.position,
                                 "is_paused": self.paused}
        self._send_progress()

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
        receipt = self.client.request("DELETE", self.path, self.stop_body)
        if (not isinstance(receipt, dict) or receipt.get("outcome") not in ("stopped", "replayed")
                or receipt.get("stop_id", self.stop_body["stop_id"]) != self.stop_body["stop_id"]):
            raise PlaybackError("The server did not confirm that playback stopped.")
        self.session_id = None


def select_file(item):
    """Choose one readable source from a title's available files."""
    if item.get("file_id"):
        return str(item["file_id"])
    for version in item.get("versions", []) + item.get("files", []):
        if version.get("file_id") and not version.get("unreadable"):
            return str(version["file_id"])
    raise PlaybackError("This title has no playable video file.")


class ResumeGuard:
    """Do not persist pre-seek zeroes while Kodi restores a saved position."""

    def __init__(self, target, clock=None, timeout=10):
        self.target = _number(target)
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.timeout = timeout
        self.confirmed = self.target == 0

    def accept(self, position):
        position = _number(position)
        if self.confirmed:
            return True
        elapsed = self.clock() - self.started
        tolerance = min(1.0, self.target / 10)
        if self.target - tolerance <= position <= self.target + elapsed + 3:
            self.confirmed = True
            return True
        if elapsed >= self.timeout:
            raise PlaybackError("Kodi could not restore the saved playback position. Your saved progress was kept.")
        return False


class PlaybackController:
    """Own Kodi playback and heartbeat until the user stops or playback ends."""

    def __init__(self, client, addon):
        self.client, self.addon = client, addon

    def play(self, item, start_position=None, file_id=None, options=None):
        import xbmc
        import xbmcgui
        from .settings import read_options

        options = dict(read_options(self.addon) if options is None else options)

        class Player(xbmc.Player):
            started = False
            finished = False
            failed = False
            paused = False
            state_revision = 0
            latest_time = None

            def capture_time(self, closing=False):
                try:
                    value = _number(self.getTime())
                    # Kodi may return zero after it has torn down playback.
                    if closing and value == 0 and self.latest_time is not None and self.latest_time > 0:
                        return
                    self.latest_time = value
                except (RuntimeError, TypeError, ValueError, PlaybackError):
                    pass

            def onAVStarted(self):
                self.started = True

            def onPlayBackStopped(self):
                self.capture_time(closing=True)
                self.finished = True

            def onPlayBackEnded(self):
                self.capture_time(closing=True)
                self.finished = True

            def onPlayBackError(self):
                self.capture_time(closing=True)
                self.failed = True
                self.finished = True

            def onPlayBackPaused(self):
                self.paused = True
                self.capture_time()
                self.state_revision += 1

            def onPlayBackResumed(self):
                self.paused = False
                self.capture_time()
                self.state_revision += 1

        session = PlaybackSession(self.client, options.get("dv_mode", "auto"),
                                  self.addon.getAddonInfo("version"), options=options)
        player, monitor = Player(), xbmc.Monitor()
        opened = False
        guard = None
        self.last_progress_error = None
        try:
            plan = session.start(str(file_id) if file_id is not None else select_file(item), start_position)
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
            guard = ResumeGuard(start)
            if start:
                player.seekTime(start)
            audio_index = (plan.get("selected_tracks", {}).get("audio") or {}).get("index")
            if audio_index is not None and 0 <= audio_index < len(player.getAvailableAudioStreams()):
                player.setAudioStream(audio_index)
            subtitle = plan.get("subtitle") or {}
            if options.get("subtitle_mode", "server") == "off" or subtitle.get("mode") == "off":
                player.showSubtitles(False)
            elif subtitle.get("artifact"):
                artifact = subtitle["artifact"]
                player.setSubtitles(kodi_media_url(self.client.server_url,
                                                  {"url": artifact["url"],
                                                   "headers": self.client.headers()}))
            interval = 5 if options.get("progress_interval") == 5 else 10
            next_report = 0.0
            reported_revision = -1
            while not player.finished:
                if monitor.waitForAbort(0.25):
                    player.capture_time()
                    if player.latest_time is not None and guard.accept(player.latest_time):
                        session.record_position(player.latest_time, player.paused)
                    player.stop()
                    break
                try:
                    current = player.getTime()
                except RuntimeError:
                    if not player.isPlaying():
                        break
                    continue
                if player.finished:
                    break
                if not guard.accept(current):
                    continue
                player.latest_time = current
                session.record_position(current, player.paused)
                revision = player.state_revision
                if time.monotonic() >= next_report or revision != reported_revision:
                    try:
                        session.progress(current, player.paused)
                        self.last_progress_error = None
                    except (ApiError, PlaybackError) as error:
                        self.last_progress_error = error
                        xbmc.log("Silo: playback progress was not accepted", xbmc.LOGWARNING)
                        if isinstance(error, ApiError) and (error.status in (401, 403, 404, 410)
                                                           or error.code == "installation_changed"):
                            player.stop()
                            raise PlaybackError("The playback session ended or needs sign-in. Open the title again.")
                    reported_revision = revision
                    next_report = time.monotonic() + interval
            if player.failed:
                raise PlaybackError("Kodi reported a playback error.")
        except (ApiError, PlaybackError) as error:
            if opened and player.isPlaying():
                player.stop()
            xbmcgui.Dialog().ok("Silo — playback", str(error))
        finally:
            # Capture while the player still exists; never ask a stopped player
            # for a final zero and overwrite the latest valid sample with it.
            if opened and player.isPlaying():
                player.capture_time()
            if guard is not None and player.latest_time is not None:
                try:
                    if guard.accept(player.latest_time):
                        session.record_position(player.latest_time, player.paused)
                except PlaybackError:
                    pass
            if opened and player.isPlaying():
                player.stop()
            for attempt in range(2):
                try:
                    session.stop()
                    break
                except ApiError as error:
                    transient = error.status in (0, 408, 429) or error.status >= 500
                    if attempt == 0 and transient and not monitor.waitForAbort(0.5):
                        continue
                    xbmc.log("Silo: unable to close playback session; server expiry will release it", xbmc.LOGWARNING)
                    break
                except PlaybackError:
                    xbmc.log("Silo: unable to close playback session; server expiry will release it", xbmc.LOGWARNING)
                    break
