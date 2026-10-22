"""Native Silo playback core and a lazily imported Kodi player adapter."""

import math
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urljoin, urlsplit

from .api import ApiError
from .capabilities import playback_capabilities
from .recovery import forget_session, remember_session, remember_stop
from .storage import StorageError


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

    def __init__(self, client, mode="auto", version="0.2.1", options=None):
        self.client, self.mode, self.version = client, mode, version
        self.options = dict(options or {})
        # /api/v2 calls this installation_id, but it identifies the SERVER.
        # A locally minted UUID is always refused with installation_changed.
        self.installation_id = None
        self.server_capabilities = None
        self.session_id = None
        self.recovery_key = None
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
        try:
            self.recovery_key = remember_session(self.client, self.installation_id, self.session_id)
        except StorageError as error:
            # Media must not open without a durable cleanup record. The live
            # session id is retained so finally can still release this attempt.
            raise PlaybackError(str(error)) from error
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
        # If journaling itself refused a just-created attempt (capacity/I/O),
        # still try to retire that unopened attempt with its scoped client.
        recorded = remember_stop(self.client, self.recovery_key, self.stop_body) if self.recovery_key else self.stop_body
        if recorded is None:
            raise ApiError(403, "The playback sign-in or selected profile changed.", "playback_scope_changed")
        self.stop_body = recorded
        try:
            receipt = self.client.request("DELETE", self.path, self.stop_body)
        except ApiError as error:
            if error.status not in (404, 410):
                raise
            if self.recovery_key:
                forget_session(self.client, self.recovery_key)
            self.session_id = None
            return
        if (not isinstance(receipt, dict) or receipt.get("outcome") not in ("stopped", "replayed")
                or receipt.get("outcome") == "stopped"
                and receipt.get("stop_id", self.stop_body["stop_id"]) != self.stop_body["stop_id"]):
            raise PlaybackError("The server did not confirm that playback stopped.")
        if self.recovery_key:
            forget_session(self.client, self.recovery_key)
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


class ProgressReporter:
    """Serialize HTTP reports away from Kodi's callback and polling paths.

    Only immutable position/pause samples cross the thread boundary. The worker
    never receives a Kodi Player, Monitor, window, logger, or callback. Its last
    sample replaces queued samples, so a slow server cannot build a backlog.
    ``close`` requests a final stop and returns immediately; tests can use wait.
    """

    def __init__(self, session, interval=10, clock=None, owner_timeout=5, startup_timeout=30):
        self.session = session
        self.interval = interval
        self.clock = clock or time.monotonic
        self.owner_timeout = owner_timeout
        self.startup_timeout = startup_timeout
        self._created_at = self.clock()
        self._updated_at = None
        self._condition = threading.Condition()
        self._latest = None
        self._closing = False
        self._error = None
        self._fatal = False
        self._owner_stalled = False
        self._notice = None
        self._serial = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="SiloPlaybackReports", daemon=True)
        self._thread.start()

    def submit(self, position, paused=False):
        sample = (_number(position), bool(paused))
        with self._condition:
            if not self._closing:
                self._latest = sample
                self._updated_at = self.clock()
                self._condition.notify()

    def close(self):
        with self._condition:
            self._closing = True
            self._condition.notify()

    def wait(self, timeout=None):
        """Optional synchronization for tests; the Kodi controller never waits."""
        return self._done.wait(timeout)

    def status(self):
        with self._condition:
            return self._serial, self._notice, self._fatal, self._error

    def _publish(self, notice, error=None):
        with self._condition:
            self._serial += 1
            self._notice = "controller_stalled" if self._owner_stalled else notice
            self._error = PlaybackError("Kodi stopped reporting its playback position.") if self._owner_stalled else error
            self._fatal = self._owner_stalled or bool(isinstance(error, ApiError) and (
                error.status in (401, 403, 404, 410) or error.code == "installation_changed"))

    def _finish(self, sample):
        if sample is not None:
            self.session.record_position(*sample)
        for attempt in range(2):
            try:
                self.session.stop()
                self._publish("stop_confirmed")
                return
            except (ApiError, OSError, PlaybackError) as error:
                transient = isinstance(error, OSError) or isinstance(error, ApiError) and (
                    error.status in (0, 408, 429) or error.status >= 500)
                if attempt == 0 and transient:
                    continue
                self._publish("stop_failed", error)
                return

    def _run(self):
        next_report, reported_pause = 0.0, None
        try:
            while True:
                with self._condition:
                    while not self._closing:
                        now = self.clock()
                        deadline = (self._updated_at + self.owner_timeout if self._updated_at is not None
                                    else self._created_at + self.startup_timeout)
                        if now >= deadline:
                            # Repeating a stale last position would otherwise
                            # keep Silo alive while Kodi's polling path hangs.
                            self._owner_stalled = True
                            self._closing = True
                            self._publish("controller_stalled")
                            break
                        if (self._latest is not None and not self._fatal
                                and (now >= next_report or self._latest[1] != reported_pause)):
                            break
                        delay = max(0.01, min(0.25, next_report - now)) if self._latest else 0.25
                        self._condition.wait(delay)
                    closing, sample = self._closing, self._latest
                if closing:
                    self._finish(sample)
                    return
                try:
                    self.session.progress(*sample)
                    self._publish("progress_accepted")
                except (ApiError, OSError, PlaybackError) as error:
                    self._publish("progress_failed", error)
                reported_pause = sample[1]
                next_report = self.clock() + self.interval
        except Exception:
            # Keep diagnostics deterministic; arbitrary exception strings can
            # contain signed media addresses or credentials.
            self._publish("reporter_failed", PlaybackError("Playback reporting failed."))
        finally:
            self._done.set()


class PlaybackController:
    """Poll Kodi locally while a separate worker reports server progress."""

    def __init__(self, client, addon):
        self.client, self.addon = client, addon
        self.last_result = None
        self.last_reporter = None

    def play(self, item, start_position=None, file_id=None, options=None):
        import xbmc
        import xbmcgui
        from .settings import read_options

        options = dict(read_options(self.addon) if options is None else options)
        self.last_result = None
        self.last_reporter = None

        class Player(xbmc.Player):
            # Kodi dispatches these asynchronously. Callbacks only change Python
            # data; all native player calls remain on the polling path.
            started = False
            finished = False
            failed = False
            ended = False
            paused = False
            state_revision = 0

            def onAVStarted(self):
                self.started = True

            def onPlayBackStopped(self):
                self.finished = True

            def onPlayBackEnded(self):
                self.ended = True
                self.finished = True

            def onPlayBackError(self):
                self.failed = True
                self.finished = True

            def onPlayBackPaused(self):
                self.paused = True
                self.state_revision += 1

            def onPlayBackResumed(self):
                self.paused = False
                self.state_revision += 1

        def log_event(event, position=None, error=None):
            message = "[Silo] playback event=" + event
            if position is not None:
                message += " position={:.3f}".format(position)
            if isinstance(error, ApiError):
                code = "".join(char for char in str(error.code) if char.isalnum() or char == "_")[:64]
                message += " status={} code={}".format(int(error.status), code or "unspecified")
            xbmc.log(message, xbmc.LOGWARNING if error is not None else xbmc.LOGINFO)

        session = PlaybackSession(self.client, options.get("dv_mode", "auto"),
                                  self.addon.getAddonInfo("version"), options=options)
        player, monitor = Player(), xbmc.Monitor()
        opened, stop_requested = False, False
        reporter, guard, latest_sample = None, None, None
        stage, last_notice = "planning", 0
        self.last_progress_error = None

        def start_reporter():
            clone = getattr(self.client, "reporting_client", None)
            session.client = clone(timeout=1.5) if clone is not None else self.client
            worker = ProgressReporter(session, 5 if options.get("progress_interval") == 5 else 10)
            self.last_reporter = worker
            return worker

        def stop_local():
            nonlocal stop_requested
            if opened and not player.finished and not stop_requested:
                stop_requested = True
                try:
                    player.stop()
                except RuntimeError:
                    log_event("native_stop_unavailable")

        try:
            version = "".join(char for char in self.addon.getAddonInfo("version") if char in "0123456789.")[:24]
            log_event("begin_v" + (version or "unknown"))
            plan = session.start(str(file_id) if file_id is not None else select_file(item), start_position)
            log_event("plan_ready", session.position)
            path = kodi_media_url(self.client.server_url, plan["stream"])
            # Credential refresh for subtitle setup happens before media opens.
            subtitle = plan.get("subtitle") or {}
            subtitle_path = None
            if options.get("subtitle_mode", "server") != "off" and subtitle.get("artifact"):
                subtitle_path = kodi_media_url(self.client.server_url,
                    {"url": subtitle["artifact"]["url"], "headers": self.client.headers()})
            reporter = start_reporter()
            listitem = xbmcgui.ListItem(label=item.get("title", "Silo"), path=path)
            tag = listitem.getVideoInfoTag()
            tag.setTitle(item.get("title", "Silo"))
            tag.setPlot(item.get("overview", ""))
            listitem.setProperty("IsPlayable", "true")
            if plan["stream"].get("mime_type"):
                listitem.setMimeType(plan["stream"]["mime_type"])
            listitem.setContentLookup(False)
            stage = "opening"
            player.play(path, listitem)
            opened = True
            deadline = time.monotonic() + 30
            while not player.started and not player.finished:
                if monitor.waitForAbort(0.2):
                    stop_local()
                    return
                if time.monotonic() >= deadline:
                    raise PlaybackError("Kodi did not start playback within 30 seconds.")
            if player.failed:
                raise PlaybackError("Kodi could not open the original media file.")
            if player.finished:
                return
            log_event("av_started")
            start = _number(plan["timeline"]["player_start_seconds"])
            guard = ResumeGuard(start)
            stage = "resume_seek"
            if start:
                player.seekTime(start)
            stage = "track_setup"
            audio_index = (plan.get("selected_tracks", {}).get("audio") or {}).get("index")
            if not player.finished and audio_index is not None:
                streams = player.getAvailableAudioStreams()
                if not player.finished and 0 <= audio_index < len(streams):
                    player.setAudioStream(audio_index)
            if not player.finished:
                if options.get("subtitle_mode", "server") == "off" or subtitle.get("mode") == "off":
                    player.showSubtitles(False)
                elif subtitle_path:
                    player.setSubtitles(subtitle_path)
            stage = "polling"
            revision = player.state_revision
            unavailable_since = None
            while not player.finished:
                if monitor.waitForAbort(0.25):
                    stop_local()
                    break
                # Monitor dispatch can deliver stop/end; never query a player
                # after that notification has arrived.
                if player.finished:
                    break
                serial, notice, fatal, error = reporter.status()
                if serial != last_notice:
                    last_notice = serial
                    self.last_progress_error = error
                    log_event(notice, error=error)
                if fatal:
                    if notice == "controller_stalled":
                        raise PlaybackError("Playback stopped after Kodi stopped reporting its position. Open the title again.")
                    raise PlaybackError("The playback session ended or needs sign-in. Open the title again.")
                try:
                    current = player.getTime()
                except RuntimeError:
                    # Native teardown can race this poll. No nested Kodi calls
                    # are made to probe a player whose query just failed.
                    if player.finished:
                        break
                    if unavailable_since is None:
                        unavailable_since = time.monotonic()
                        log_event("native_time_unavailable")
                    elif time.monotonic() - unavailable_since >= 2:
                        log_event("native_time_lost")
                        # The native player is no longer queryable. Mark it
                        # finished so cleanup does not probe or stop it again.
                        player.finished = True
                        break
                    continue
                unavailable_since = None
                if player.finished:
                    break
                if not guard.accept(current):
                    continue
                latest_sample = (_number(current), bool(player.paused))
                reporter.submit(*latest_sample)
                if revision != player.state_revision:
                    revision = player.state_revision
                    log_event("paused" if player.paused else "resumed", current)
            if player.failed:
                log_event("native_error")
                raise PlaybackError("Kodi reported a playback error.")
            log_event("native_ended" if player.ended else "native_stopped",
                      latest_sample[0] if latest_sample else None)
        except (ApiError, PlaybackError) as error:
            log_event("failure_" + stage, error=error)
            stop_local()
            xbmcgui.Dialog().ok("Silo — playback", str(error))
        except RuntimeError:
            # A user stop can race AV/track setup. Keep exception strings out of
            # diagnostics, and do not call native stop after its callback.
            log_event("native_runtime_error_" + stage)
            already_finished = player.finished
            stop_local()
            if not already_finished:
                xbmcgui.Dialog().ok("Silo — playback", "Kodi could not finish setting up this playback.")
        finally:
            stop_local()
            source_position = session.position
            if latest_sample is not None and session.plan:
                source_position = _number(latest_sample[0] + session.plan["timeline"]["timeline_offset_seconds"])
            self.last_result = {"has_sample": latest_sample is not None,
                                "position_seconds": source_position, "ended": bool(player.ended)}
            if reporter is None and session.session_id is not None:
                reporter = start_reporter()
            if reporter is not None:
                if latest_sample is not None:
                    reporter.submit(*latest_sample)
                reporter.close()
                log_event("cleanup_queued", source_position if latest_sample else None)
