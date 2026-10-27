"""Native Silo playback core and a lazily imported Kodi player adapter."""

import math
import os
import threading
import time
import uuid
from urllib.parse import quote, urlencode, urljoin, urlsplit

from .api import ApiError
from . import buffering_quality as bq
from .capabilities import playback_capabilities
from .player_extras import STREAK
from .recovery import forget_session, remember_session, remember_stop
from .storage import StorageError


# How long fullscreen video must be gone before playback stops (see the poll loop).
LEFT_FULLSCREEN_SECONDS = 1.0
# How long Kodi has to reach a saved position; converted streams start converting there on demand.
RESUME_TIMEOUT, RESUME_TIMEOUT_CONVERTED = 10, 30


def player_near(player, target):
    """Whether Kodi already started at ``target`` (StartOffset), so no seek is needed."""
    try:
        return abs(float(player.getTime()) - target) <= max(1.0, min(3.0, target / 10))
    except (RuntimeError, TypeError, ValueError):
        return False


class PlaybackError(Exception):
    """A plan cannot be executed by this build."""


# Terminal reasons a 201 carries when the original file exceeds what this
# device declared (HDR range or resolution) and the server cannot convert it
# for this device either.
CAPABILITY_TERMINALS = frozenset({
    "client_hls_unsupported", "adaptation_unavailable", "hdr_transcode_unsupported",
    "video_conversion_unsupported", "dv_conversion_unsupported",
    "transcoding_disabled", "local_transcode_disabled",
})
CAPABILITY_GUIDANCE = (
    "This title needs a format this device isn't set to play directly, such as HDR, "
    "Dolby Vision or 4K. If your TV supports it, change Settings → Playback → "
    "HDR and Dolby Vision capability or Maximum video resolution, then play it again."
)
# Server deliveries this build executes. Converted streams arrive as HLS;
# progressive remux is declared disabled in capabilities.py.
HLS_DELIVERIES = ("server_remux_hls", "server_transcode_hls")
SUPPORTED_DELIVERIES = ("original_http",) + HLS_DELIVERIES
REPLAN_OPERATIONS = ("failure_recovery", "seek_reanchor", "track_change", "quality_change")
INTENT_OPERATIONS = ("track_change", "quality_change")
MAX_RECOVERY_ATTEMPTS = 3
SUBTITLE_LIMIT = 8 * 1024 * 1024
SUBTITLE_EXTENSIONS = ("srt", "vtt", "ass", "ssa")


def subtitle_extension(artifact):
    """Pick the file extension Kodi uses to recognize a sidecar's format."""
    value = str(artifact.get("format") or "").lower().lstrip(".")
    value = {"webvtt": "vtt", "subrip": "srt"}.get(value, value)
    if value in SUBTITLE_EXTENSIONS:
        return value
    mime = str(artifact.get("mime_type") or "").lower()
    if "vtt" in mime:
        return "vtt"
    if "ssa" in mime or "ass" in mime:
        return "ass"
    suffix = urlsplit(str(artifact.get("url") or "")).path.rsplit(".", 1)[-1].lower()
    return suffix if suffix in SUBTITLE_EXTENSIONS else "srt"


def embedded_subtitle_index(subtitle):
    """Kodi's subtitle-stream index for a plan that renders an embedded track.

    The plan's embedded.stream_index is FFmpeg's absolute stream number, not
    Kodi's subtitle ordinal. The inventory lists every embedded track in
    container order (bitmap tracks keep their place), so the selected track's
    position among the embedded entries is the ordinal Kodi uses.
    """
    if subtitle.get("mode") != "render" or not isinstance(subtitle.get("embedded"), dict):
        return None
    track_id = subtitle.get("track_id")
    if not track_id:
        return None
    embedded = sorted(
        (entry for entry in subtitle.get("inventory") or []
         if isinstance(entry, dict) and entry.get("source") == "embedded"
         and isinstance(entry.get("combined_index"), int) and not isinstance(entry.get("combined_index"), bool)),
        key=lambda entry: entry["combined_index"])
    return next((index for index, entry in enumerate(embedded) if entry.get("track_id") == track_id), None)


def _number(value, signed=False, field=None):
    """A server timeline value: finite, and not negative unless ``signed``."""
    value = float(value)
    if not math.isfinite(value) or (not signed and value < 0):
        raise PlaybackError("The server returned an invalid playback time{}.".format(
            " ({})".format(field) if field else ""))
    return value


def _position(value):
    """A playback position from Kodi or derived from it.

    Kodi briefly reports slightly negative times around seeks and while an HLS
    stream starts; that is the start of the video, not an error. Only a value
    that is not a number at all stops playback.
    """
    value = float(value)
    if not math.isfinite(value):
        raise PlaybackError("Kodi reported an invalid playback position.")
    return max(0.0, value)


# Media routes the Silo API serves itself. They need the account's sign-in
# token on every request, so they play through the loopback relay (relay.py).
SERVER_MEDIA_PATHS = ("/api/v2/stream/", "/api/v1/stream/", "/api/v2/playback/transcode/",
                      "/api/v1/playback/transcode/")


def served_by_server(server_url, url):
    """True when ``url`` is a media route on the Silo server itself, not a proxy node."""
    parts, server = urlsplit(url), urlsplit(server_url)
    origin = lambda p: (p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80))
    base = server.path.rstrip("/")
    return origin(parts) == origin(server) and parts.path.startswith(tuple(base + path for path in SERVER_MEDIA_PATHS))


def kodi_media_url(server_url, stream, relay=None):
    """Encode only server-issued headers, without adding account credentials.

    Kodi cannot swap bearer headers on an already open media item, so this
    client deliberately does not negotiate header_authenticated_media_v1.
    ``relay(url, headers)`` returns a loopback URL for media the server serves
    itself; signed proxy-node URLs always go to Kodi directly.
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
    if relay is not None and served_by_server(server_url, url):
        return relay(url, headers)
    origin = lambda p: (p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80))
    if headers and origin(parts) != origin(server):
        # Cross-origin authenticated media requires explicit origin negotiation,
        # which this build does not advertise. Signed proxy URLs need no headers.
        raise PlaybackError("Authenticated media on another origin is not supported by this build.")
    return url + ("|" + urlencode(headers, quote_via=quote) if headers else "")


def is_converted(plan):
    """True when the server repackages or re-encodes the file (HLS)."""
    return (plan or {}).get("delivery") in HLS_DELIVERIES


def server_anchored(plan):
    """True when seeks must go back through the server (protocol v3 §5).

    A growing HLS playlist (every copy remux, and a transcode of unknown
    length) cannot seek locally past what FFmpeg has produced; the plan says
    so with ``can_seek_anywhere: false``.
    """
    return (plan or {}).get("timeline", {}).get("can_seek_anywhere") is False


def _bandwidth_cap(options):
    value = options.get("max_bitrate")
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return max(100, min(1000000, value))


class PlaybackSession:
    """Synchronous session core, independent of Kodi and exercised offline."""

    def __init__(self, client, mode="auto", version="0.7.5", options=None):
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
        # Replan identity: the start request's attempt, the client-minted id of
        # the plan now playing, and the loop guard for failure recovery.
        self.start_body = None
        self.plan_attempt_id = None
        self.attempted_keys = []
        self.attempt_count = 1
        self.quality_preference = str(self.options.get("quality_preference") or "auto")
        # Buffering quality (buffering_quality_v1): the server's features gate
        # the request fields; the kept step-down estimate rides later replans.
        self.server_features = []
        self.kept_estimate = bq.KeptEstimate()
        self.buffering_mode = bq.parse_mode(self.options.get("buffering_quality_mode"))

    def _negotiate(self):
        capability = self.client.request("GET", "/playback/capabilities", None)
        if not isinstance(capability, dict) or capability.get("state") != "available" or capability.get("allowed") is not True:
            raise PlaybackError("Playback is not available for this profile on the server.")
        if 3 not in capability.get("protocol_versions", []):
            raise PlaybackError("The server does not offer playback protocol 3.")
        if not any(delivery in capability.get("deliveries", []) for delivery in SUPPORTED_DELIVERIES):
            raise PlaybackError("The server does not offer a playback route this device can use.")
        identity = capability.get("installation_id")
        if not isinstance(identity, str) or not identity:
            raise PlaybackError("The server did not provide its playback installation identity.")
        self.installation_id = identity
        self.server_capabilities = capability
        self.server_features = [str(value) for value in capability.get("features") or []]
        # This diagnostic key cannot be confused with old client-minted ids.
        self.client.store.set("playback_installation_id", identity)
        return [feature for feature in ("subrip_sidecar_v1",) if feature in capability.get("features", [])]

    def _capabilities(self):
        return playback_capabilities(self.mode, self.version, self.options.get("max_resolution", "auto"),
                                     self.options.get("client_audio_selection", True))

    def start(self, file_id, start_position=None):
        profile_id = self.client.store.get("profile_id")
        if not profile_id:
            raise PlaybackError("Choose a profile before playing a title.")
        features = self._negotiate()
        codecs, context = self._capabilities()
        body = {
            "installation_id": self.installation_id,
            "protocol_version": 3, "client_features": features,
            "file_id": str(file_id), "profile_id": str(profile_id),
            "playback_attempt_id": str(uuid.uuid4()), "quality_preference": self.quality_preference,
            "subtitle_fidelity_preference": "compatible",
            "progress_persistence": "server",
            "allow_alternate_versions": False,
            "client_capabilities": codecs, "client_playback_context": context,
        }
        cap = _bandwidth_cap(self.options)
        if cap is not None:
            # The server converts anything above this ceiling (decimal kbps).
            body["bandwidth_cap_kbps"] = cap
        if start_position is not None:
            body["start_position"] = _position(start_position)
        audio = self.options.get("audio_track_index")
        if isinstance(audio, int) and not isinstance(audio, bool) and audio >= 0:
            # The profile's audio language, chosen like the Silo apps do (track_prefs).
            body["audio_track_index"] = audio
        # Without a subtitle field Silo plans subtitles off.
        track = self.options.get("subtitle_track_index")
        if (isinstance(track, int) and not isinstance(track, bool) and track >= 0
                and self.options.get("subtitle_mode", "server") != "off"):
            body["subtitle_track_index"] = track
        self._gate_start(body)
        try:
            decision = self.client.request("POST", "/playback/start", body)
        except ApiError as error:
            if error.status != 409 or error.code != "installation_changed":
                raise
            # This exact refusal happens before the server creates an attempt.
            # Refresh discovery once; do not retry unrelated conflicts or loops.
            body["client_features"] = self._negotiate()
            body["installation_id"] = self.installation_id
            self._gate_start(body)
            decision = self.client.request("POST", "/playback/start", body)
        self._merge_features(decision)
        if decision.get("protocol_version") != 3:
            raise PlaybackError("The server did not return playback protocol 3.")
        if decision.get("outcome") != "playable":
            terminal = decision.get("terminal") or {}
            if terminal.get("reason") in CAPABILITY_TERMINALS:
                raise PlaybackError(CAPABILITY_GUIDANCE)
            raise PlaybackError(terminal.get("message") or terminal.get("reason") or "No playable route is available.")
        plan = decision.get("playback_plan") or {}
        self.session_id = decision.get("session_id") or plan.get("session_id")
        if not self.session_id:
            raise PlaybackError("The playback response is missing its session identifier.")
        # Keep the id before validation so the caller can release a refused plan.
        self.plan = plan
        self.start_body = body
        self.plan_attempt_id = str(uuid.uuid4())
        try:
            self.recovery_key = remember_session(self.client, self.installation_id, self.session_id)
        except StorageError as error:
            # Media must not open without a durable cleanup record. The live
            # session id is retained so finally can still release this attempt.
            raise PlaybackError(str(error)) from error
        self._validate(plan)
        self.position = _number(plan["timeline"]["source_start_seconds"], field="timeline.source_start_seconds")
        return plan

    def _gate_start(self, body):
        learned = bq.learned_limit_for_mode(self.buffering_mode)
        if learned is not None:
            body["learned_limit"] = learned
        bq.gate_start_request(body, self.server_features)

    def _merge_features(self, decision):
        for value in (decision or {}).get("server_features") or [] if isinstance(decision, dict) else []:
            if value not in self.server_features:
                self.server_features.append(str(value))

    def _validate(self, plan):
        if plan.get("protocol_version") != 3 or plan.get("delivery") not in SUPPORTED_DELIVERIES:
            raise PlaybackError("This device cannot play the playback route the server chose.")
        if any(t.get("executor") == "client" for t in plan.get("transformations", [])):
            raise PlaybackError("This plan needs a client video transformation that is not available.")
        timeline = plan.get("timeline") or {}
        try:
            for key in ("source_start_seconds", "player_start_seconds", "timeline_offset_seconds"):
                _number(timeline[key], signed=key == "timeline_offset_seconds", field="timeline." + key)
        except KeyError as error:
            raise PlaybackError("The server returned an incomplete playback timeline ({}).".format(error.args[0])) from error
        except (TypeError, ValueError) as error:
            raise PlaybackError("The server returned an invalid playback timeline.") from error
        kodi_media_url(self.client.server_url, plan.get("stream") or {})

    def can_replan(self):
        plan = self.plan or {}
        return bool(self.session_id and self.start_body and isinstance(plan.get("plan_id"), str)
                    and isinstance(plan.get("plan_attempt_key"), str) and plan["plan_attempt_key"])

    def replan(self, operation, position=None, quality=None, audio_index=None, classification=None,
               client=None, buffering=None):
        """Ask the server for another plan for this session (protocol v3 §6).

        ``position`` is a source position. Intent operations (track and
        quality changes) keep the current route eligible; failure recovery
        sends every key already tried so the server cannot hand one back.
        Returns the new, validated plan; a terminal answer raises
        PlaybackError and leaves the current plan in place. Network and HTTP
        errors propagate as ApiError/OSError.
        """
        if operation not in REPLAN_OPERATIONS:
            raise PlaybackError("Unknown playback change.")
        if not self.can_replan():
            raise PlaybackError("This playback cannot be changed while it plays.")
        plan, intent = self.plan, operation in INTENT_OPERATIONS
        # Only failure recovery excludes routes already tried; a reanchor is a
        # timeline operation and may legitimately return the same route.
        guarded = operation == "failure_recovery"
        codecs, context = self._capabilities()
        if quality is not None:
            quality = str(quality).strip() or "auto"
        body = {
            "installation_id": self.installation_id, "protocol_version": 3,
            "client_features": list(self.start_body.get("client_features") or []),
            "operation": operation,
            "playback_attempt_id": self.start_body["playback_attempt_id"],
            "replan_request_id": str(uuid.uuid4()),
            "failed_plan_id": plan["plan_id"], "plan_attempt_id": self.plan_attempt_id,
            "plan_attempt_key": plan["plan_attempt_key"],
            "attempt_count": 1 if intent else min(8, self.attempt_count),
            "quality_preference": quality if quality is not None else self.quality_preference,
            "client_capabilities": codecs, "client_playback_context": context,
        }
        if "bandwidth_cap_kbps" in self.start_body:
            body["bandwidth_cap_kbps"] = self.start_body["bandwidth_cap_kbps"]
        attempted = []
        if guarded:
            attempted = list(dict.fromkeys(self.attempted_keys + [plan["plan_attempt_key"]]))[-16:]
            body["attempted_plan_keys"] = attempted
        if operation == "failure_recovery":
            body["failure"] = {"classification": str(classification or "playback_failed")[:64]}
        if position is not None:
            body["position_seconds"] = min(31536000.0, _position(position))
        if operation == "track_change":
            # Only a track change reads an omitted subtitle as "off", so the
            # current server subtitle is restated with the new audio track.
            tracks = {}
            if isinstance(audio_index, int) and not isinstance(audio_index, bool) and audio_index >= 0:
                tracks["audio"] = {"index": audio_index}
            subtitle = plan.get("subtitle") or {}
            if subtitle.get("mode") not in (None, "off") and subtitle.get("track_id"):
                tracks["subtitle"] = {"id": str(subtitle["track_id"])}
            body["selected_tracks"] = tracks
        if buffering is not None:
            # The buffering-quality controller's own quality_change (replan_fields).
            body.update(bq.replan_fields(buffering))
        self.kept_estimate.apply(body, buffering)
        learned = bq.learned_limit_for_mode(self.buffering_mode)
        if learned is not None and "learned_limit" not in body:
            body["learned_limit"] = learned
        bq.gate_replan_request(body, self.server_features)
        decision = (client or self.client).request("POST", self.path + "/replan", body)
        self._merge_features(decision)
        if not isinstance(decision, dict) or decision.get("protocol_version") != 3:
            raise PlaybackError("The server did not return playback protocol 3.")
        if decision.get("outcome") != "playable":
            terminal = decision.get("terminal") or {}
            raise PlaybackError(terminal.get("message") or terminal.get("reason") or "No other route is available.")
        candidate = decision.get("playback_plan") or {}
        session_id = decision.get("session_id") or candidate.get("session_id")
        if session_id and session_id != self.session_id:
            raise PlaybackError("The server moved playback to another session.")
        self._validate(candidate)
        key = candidate.get("plan_attempt_key")
        if guarded and key in attempted:
            raise PlaybackError("The server offered a playback route that already failed on this device.")
        self.plan = candidate
        self.plan_attempt_id = str(uuid.uuid4())
        if quality is not None:
            self.quality_preference = quality
        if intent:
            self.attempted_keys, self.attempt_count = [], 1
        elif guarded:
            self.attempted_keys, self.attempt_count = attempted, self.attempt_count + 1
        return candidate

    @property
    def path(self):
        return "/playback/" + quote(str(self.session_id), safe="")

    def record_position(self, player_position, paused=False, offset=None):
        """Keep the latest confirmed local position even between heartbeats.

        ``offset`` is the timeline offset of the plan the sample was taken
        on; it defaults to the current plan's, and is passed explicitly so a
        sample queued just before a stream switch keeps its own timeline.
        """
        if not self.session_id or not self.plan:
            return
        if offset is None:
            offset = self.plan["timeline"]["timeline_offset_seconds"]
        self.position = _position(float(player_position) + float(offset))
        self.paused = bool(paused)
        self.has_sample = True

    def _send_progress(self):
        receipt = self.client.request("POST", self.path + "/progress", self.pending_progress)
        if receipt.get("outcome") not in ("applied", "replayed"):
            raise PlaybackError("The server did not accept the playback progress sample.")
        self.pending_progress = None

    def progress(self, player_position, paused=False, offset=None):
        if not self.session_id or not self.plan:
            return
        self.record_position(player_position, paused, offset)
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
    """Do not persist pre-seek zeroes while Kodi restores a saved position.

    Converted streams can take a while to reach a far position, because the
    server starts converting there only when asked. The guard asks for the
    seek again (``retry_due``) and, if Kodi never gets there, gives up quietly
    so playback continues from wherever it is (``gave_up``) instead of
    stopping with an error.
    """

    RETRY_SECONDS = (4.0, 8.0)

    def __init__(self, target, clock=None, timeout=10):
        self.target = _position(target)
        self.clock = clock or time.monotonic
        self.started = self.clock()
        self.timeout = timeout
        self.confirmed = self.target == 0
        self.gave_up = False
        self.retries = 0

    def accept(self, position):
        position = _position(position)
        if self.confirmed:
            return True
        elapsed = self.clock() - self.started
        tolerance = min(1.0, self.target / 10)
        if self.target - tolerance <= position <= self.target + elapsed + 3:
            self.confirmed = True
            return True
        if elapsed >= self.timeout:
            self.confirmed = self.gave_up = True
            return True
        return False

    def retry_due(self):
        """True once per retry point while the saved position is still not reached."""
        if self.confirmed or self.retries >= len(self.RETRY_SECONDS):
            return False
        if self.clock() - self.started < self.RETRY_SECONDS[self.retries]:
            return False
        self.retries += 1
        return True


class StallRecovery:
    """Nudge a decoder that stopped advancing, a common fix after audio changes.

    Kodi's hardware decoder can freeze with no player callback firing while the
    stream keeps arriving. When the position has not moved for
    ``stall_seconds`` (not paused, not buffering), ``observe`` returns a target a
    few seconds ahead; a small forward seek restarts the decoder. Nudges are
    spaced by ``settle_seconds``, capped at ``max_nudges``, and the cap resets
    after ``healthy_reset_seconds`` of normal playback.
    """

    def __init__(self, clock=None, stall_seconds=3.0, nudge_seconds=3.0, max_nudges=3,
                 settle_seconds=5.0, healthy_reset_seconds=30.0):
        self.clock = clock or time.monotonic
        self.stall_seconds = stall_seconds
        self.nudge_seconds = nudge_seconds
        self.max_nudges = max_nudges
        self.settle_seconds = settle_seconds
        self.healthy_reset_seconds = healthy_reset_seconds
        self.nudges = 0
        self.exhausted_reported = False
        self._position = None
        self._changed_at = None
        self._last_nudge_at = None
        self._settle_until = 0.0

    def observe(self, position, paused=False, caching=False, total=None):
        """Return a seek target when playback is stuck, otherwise None."""
        now = self.clock()
        moved = (self._position is None or position > self._position + 0.1
                 or position < self._position - 0.5)
        if paused or caching or moved:
            # A pause, buffering or real movement (including a user seek back)
            # restarts the stall clock.
            if moved and self.nudges and self._last_nudge_at is not None \
                    and now - self._last_nudge_at >= self.healthy_reset_seconds:
                self.nudges = 0
                self.exhausted_reported = False
            self._position, self._changed_at = position, now
            return None
        if now < self._settle_until or now - self._changed_at < self.stall_seconds:
            return None
        if self.nudges >= self.max_nudges:
            return None
        target = position + self.nudge_seconds
        if total and total > 0 and target >= total - 5:
            return None  # Never jump into the last seconds of the title.
        self.nudges += 1
        self._last_nudge_at = now
        self._settle_until = now + self.settle_seconds
        self._changed_at = now
        return target


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
        self._held = 0
        self._done = threading.Event()
        self._thread = threading.Thread(target=self._run, name="SiloPlaybackReports", daemon=True)
        self._thread.start()

    def submit(self, position, paused=False, offset=None):
        """Queue a player-time sample; ``offset`` pins the plan timeline it belongs to."""
        sample = (_position(position), bool(paused))
        if offset is not None:
            sample += (_number(offset, signed=True),)
        with self._condition:
            if not self._closing:
                self._latest = sample
                self._updated_at = self.clock()
                self._condition.notify()

    def hold(self):
        """Suspend the owner watchdog while the controller blocks on purpose.

        A stream switch or a modal dialog keeps the polling path busy for
        longer than ``owner_timeout``; heartbeats of the last sample continue.
        """
        with self._condition:
            self._held += 1

    def release(self):
        with self._condition:
            self._held = max(0, self._held - 1)
            if not self._held:
                # A new stream may still be seeking to its start: give the
                # owner the startup window again until its next sample.
                self._created_at, self._updated_at = self.clock(), None
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
                        if now >= deadline and not self._held:
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


SEEK_JUMP_SECONDS = 10.0
ADAPTIVE_ADDON = "inputstream.adaptive"


def rewound_start(start_position, rewind):
    """Back a saved resume point up by ``rewind`` seconds, never below zero."""
    if start_position is None:
        return None
    start = _position(start_position)
    if start <= 0 or isinstance(rewind, bool) or not isinstance(rewind, (int, float)) or rewind <= 0:
        return start
    return max(0.0, start - float(rewind))


def effective_file_id(plan, file_id=None):
    """The file the server actually streams (v2 ``effective_media_file_id``)."""
    plan = plan or {}
    value = plan.get("effective_media_file_id") or plan.get("effective_file_id") or file_id
    return str(value) if value not in (None, "") else None


def plan_duration(plan):
    """Player-time length from the plan, never from a growing HLS playlist."""
    try:
        duration = _number((plan.get("source") or {})["duration_seconds"])
        offset = _number(plan["timeline"]["timeline_offset_seconds"], signed=True)
    except (KeyError, TypeError, ValueError, PlaybackError):
        return None
    return duration - offset if duration > offset else None


class PlaybackController:
    """Poll Kodi locally while a separate worker reports server progress."""

    # Consecutive Up next auto-advances. application.play makes one controller
    # per episode, so the count lives on the class and survives the hand-off.
    streak = STREAK

    def __init__(self, client, addon):
        self.client, self.addon = client, addon
        self.last_result = None
        self.last_reporter = None
        # Set by the application: find the episode after ``item``, and resolve art.
        self.next_lookup = None
        self.image = None
        # Set by the application: open_screen(name, **kwargs) for the subtitle
        # tools, and a callable telling whether AI subtitle translation is on.
        self.open_screen = None
        self.subtitle_ai_enabled = None
        # Set by the application: remember a subtitle picked in the player menu.
        self.remember_subtitle = None
        # Why playback stopped for something else: next episode, version, sleep.
        self.next_action = None
        self.loaded_subtitle = None
        # Set by the application: remember an audio track picked in the player menu.
        self.remember_audio = None
        # Builds the loopback relay for media the server serves itself.
        self.relay_factory = None

    def _extras(self, player, item, plan, options, file_id, log_event, seek=None, ai_enabled=None):
        """Overlays for this playback, or None where Kodi cannot draw them."""
        try:
            import xbmcgui
            from .media import available_versions, default_version
            from .player_extras import PlaybackExtras
            from .ui_overlay import SkipButton, UpNextCard
            from .ui_player import PlayerMenu
            xbmcgui.WindowXMLDialog, xbmcgui.Window  # Missing outside a real Kodi.
        except Exception:
            log_event("extras_unavailable")
            return None
        path = self.addon.getAddonInfo("path")
        effective = effective_file_id(plan, file_id)
        version = default_version(item, preferred_file_id=effective)

        def notify(message):
            xbmcgui.Dialog().notification("Silo", message, time=2500, sound=False)

        return PlaybackExtras(
            player, item, version, plan, options, skip_button=SkipButton(path), up_next=UpNextCard(path),
            menu_factory=lambda: PlayerMenu(path), home=xbmcgui.Window(10000), notify=notify,
            next_lookup=self.next_lookup, image=self.image, versions=available_versions(item),
            file_id=effective, save_setting=self.addon.setSetting, remember_subtitle=self.remember_subtitle,
            remember_audio=self.remember_audio,
            log=lambda event, position=None: log_event(event, position),
            streak=self.streak, seek=seek, ai_enabled=ai_enabled)

    def _save_subtitle(self, artifact, log_event):
        """Download a sidecar with the add-on's own headers; return a local path.

        A missing subtitle never blocks playback: failures are logged by code
        and the title plays without it. Older sidecars are removed, except the
        one Kodi currently shows.
        """
        import xbmcvfs

        try:
            data = self.client.download(artifact.get("url"), SUBTITLE_LIMIT)
            folder = os.path.join(xbmcvfs.translatePath(self.addon.getAddonInfo("profile")), "subtitles")
            os.makedirs(folder, mode=0o700, exist_ok=True)
            keep = os.path.abspath(self.loaded_subtitle) if self.loaded_subtitle else None
            for name in os.listdir(folder):
                if name.startswith("silo-subtitle-") and os.path.abspath(os.path.join(folder, name)) != keep:
                    try:
                        os.unlink(os.path.join(folder, name))
                    except OSError:
                        pass
            path = os.path.join(folder, "silo-subtitle-{}.{}".format(uuid.uuid4().hex, subtitle_extension(artifact)))
            with open(path, "wb") as handle:
                handle.write(data)
            return path
        except (ApiError, OSError) as error:
            log_event("subtitle_unavailable", error=error if isinstance(error, ApiError) else None)
            return None

    def play(self, item, start_position=None, file_id=None, options=None, exact_start=False):
        """Play ``item``; ``start_position`` is source seconds (None = server resume).

        A saved resume point is backed up by ``options["resume_rewind"]``
        seconds unless ``exact_start`` says the position is a deliberate one
        (a chapter or the place a version switch left off). The rewind is
        applied in Kodi only: the server gets the saved position, so a start
        that fails cannot move the viewer's resume point.
        """
        import xbmc
        import xbmcgui
        from .settings import read_options

        options = dict(read_options(self.addon) if options is None else options)
        self.last_result = None
        self.last_reporter = None
        self.loaded_subtitle = None
        condition = getattr(xbmc, "getCondVisibility", None)
        rewind = 0.0
        if not exact_start and start_position is not None:
            rewind = _position(start_position) - rewound_start(start_position, options.get("resume_rewind", 0))

        class Player(xbmc.Player):
            # Kodi dispatches these asynchronously. Callbacks only change Python
            # data; all native player calls remain on the polling path.
            started = False
            finished = False
            failed = False
            ended = False
            paused = False
            state_revision = 0
            # While a new stream replaces the current one, and briefly after
            # it starts, the old item's stop/end callbacks are not the viewer's.
            switching = False
            quiet_until = 0.0

            def _stale(self):
                return self.switching or time.monotonic() < self.quiet_until

            def onAVStarted(self):
                if self.switching:
                    self.switching = False
                    self.quiet_until = time.monotonic() + 3
                self.started = True

            def onPlayBackStopped(self):
                if not self._stale():
                    self.finished = True

            def onPlayBackEnded(self):
                if not self._stale():
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

        def notify(message):
            try:
                xbmcgui.Dialog().notification("Silo", message, time=3000, sound=False)
            except (AttributeError, RuntimeError):
                log_event("notification_unavailable")

        def has_condition(name):
            try:
                return bool(condition and condition(name))
            except (RuntimeError, TypeError):
                return False

        session = PlaybackSession(self.client, options.get("dv_mode", "auto"),
                                  self.addon.getAddonInfo("version"), options=options)
        player, monitor = Player(), xbmc.Monitor()
        opened, stop_requested = False, False
        reporter, guard, latest_sample, extras = None, None, None, None
        stage, last_notice = "planning", 0
        used_adaptive = False
        pending_reanchor = []
        self.next_action = None
        self.last_progress_error = None

        def start_reporter():
            clone = getattr(self.client, "reporting_client", None)
            session.client = clone(timeout=1.5) if clone is not None else self.client
            # The session's own client is now the short-timeout reporting
            # clone; replans and subtitle downloads keep using self.client.
            worker = ProgressReporter(session, 5 if options.get("progress_interval") == 5 else 10)
            self.last_reporter = worker
            return worker

        def stop_local():
            nonlocal stop_requested
            if opened and not player.finished and not stop_requested:
                stop_requested = True
                player.switching, player.quiet_until = False, 0.0
                try:
                    player.stop()
                except RuntimeError:
                    log_event("native_stop_unavailable")

        def source_position():
            if latest_sample is not None:
                return _position(latest_sample[0] + latest_sample[2])
            return session.position

        media_relay = None

        def relay(url, headers):
            nonlocal media_relay
            if media_relay is None:
                factory = self.relay_factory
                if factory is None:
                    from .relay import MediaRelay as factory
                media_relay = factory(self.client)
                log_event("media_relay")
            return media_relay.register(url, headers)

        def mount(plan, switching):
            """Hand one plan's stream to Kodi; "started", "failed", "finished" or "aborted"."""
            nonlocal opened, stage, guard, used_adaptive
            converted = is_converted(plan)
            path = kodi_media_url(self.client.server_url, plan["stream"], relay=relay)
            # Subtitle setup, including any credential refresh, happens before
            # media opens. Sidecars are saved locally so Kodi never receives a
            # URL carrying the sign-in or profile token. An HLS transport has no
            # subtitle stream, so only original files select embedded tracks.
            subtitle = plan.get("subtitle") or {}
            subtitle_path, embedded_index = None, None
            if options.get("subtitle_mode", "server") != "off":
                if subtitle.get("artifact"):
                    subtitle_path = self._save_subtitle(subtitle["artifact"], log_event)
                elif not converted:
                    embedded_index = embedded_subtitle_index(subtitle)
            used_adaptive = bool(converted and not native_only
                                 and has_condition("System.HasAddon({})".format(ADAPTIVE_ADDON))
                                 and has_condition("System.AddonIsEnabled({})".format(ADAPTIVE_ADDON)))
            headers = ""
            if used_adaptive:
                path, _, headers = path.partition("|")
            listitem = xbmcgui.ListItem(label=item.get("title", "Silo"), path=path)
            planned_start = _number(plan["timeline"]["player_start_seconds"], field="timeline.player_start_seconds")
            if not switching:
                # Resume rewind, locally; a switch continues where the viewer is.
                planned_start = max(0.0, planned_start - rewind)
            if planned_start > 0:
                # Kodi opens the stream at the saved position instead of playing
                # from 0:00 and seeking afterwards, which a converted stream may
                # not finish before the resume check gives up.
                listitem.setProperty("StartOffset", "{:.3f}".format(planned_start))
            tag = listitem.getVideoInfoTag()
            tag.setTitle(item.get("title", "Silo"))
            tag.setPlot(item.get("overview", ""))
            listitem.setProperty("IsPlayable", "true")
            # Silo supplies subtitles itself; never let Kodi probe the stream's
            # "folder" for subtitle files (Kodi does this for LAN servers).
            listitem.setProperty("no-ext-subs-scan", "true")
            if used_adaptive:
                listitem.setProperty("inputstream", ADAPTIVE_ADDON)
                # A growing EVENT playlist must start at its first segment, not
                # at the live edge; the plan's player_start is applied by seek.
                listitem.setProperty("inputstream.adaptive.play_timeshift_buffer", "true")
                if headers:
                    listitem.setProperty("inputstream.adaptive.manifest_headers", headers)
                    listitem.setProperty("inputstream.adaptive.stream_headers", headers)
            if plan["stream"].get("mime_type"):
                listitem.setMimeType(plan["stream"]["mime_type"])
            listitem.setContentLookup(False)
            stage = "opening"
            player.started = player.failed = player.ended = player.finished = False
            player.switching = switching
            player.play(path, listitem)
            opened = True
            limit = 60 if converted else 30
            deadline = time.monotonic() + limit
            while not player.started and not player.finished:
                if monitor.waitForAbort(0.2):
                    player.switching = False
                    stop_local()
                    return "aborted"
                if time.monotonic() >= deadline:
                    player.switching = False
                    raise PlaybackError("Kodi did not start playback within {} seconds.".format(limit))
            player.switching = False
            if player.failed:
                return "failed"
            if player.finished:
                return "finished"
            log_event("av_started")
            start = planned_start
            guard = ResumeGuard(start, timeout=RESUME_TIMEOUT_CONVERTED if converted else RESUME_TIMEOUT)
            stage = "resume_seek"
            if start and not player_near(player, start):
                player.seekTime(start)
            stage = "track_setup"
            audio_index = (plan.get("selected_tracks", {}).get("audio") or {}).get("index")
            if not converted and not player.finished and audio_index is not None:
                # An HLS transport carries only the planned audio track.
                streams = player.getAvailableAudioStreams()
                if not player.finished and 0 <= audio_index < len(streams):
                    player.setAudioStream(audio_index)
            if not player.finished:
                if (options.get("subtitle_mode", "server") == "off" or options.get("subtitles_off")
                        or subtitle.get("mode") in (None, "off")):
                    # Off means off: Kodi's own subtitle language settings must not turn one on.
                    player.showSubtitles(False)
                elif subtitle_path:
                    player.setSubtitles(subtitle_path)
                    self.loaded_subtitle = subtitle_path
                elif embedded_index is not None:
                    streams = player.getAvailableSubtitleStreams()
                    if not player.finished and 0 <= embedded_index < len(streams):
                        player.setSubtitleStream(embedded_index)
                        player.showSubtitles(True)
                    elif not player.finished:
                        player.showSubtitles(False)
                elif subtitle.get("mode") != "burn_in":
                    # Nothing Kodi should show (a burned-in subtitle is part of the picture).
                    player.showSubtitles(False)
            return "started"

        native_only = False

        def open_plan(plan, switching=False):
            """Mount ``plan``; if Kodi cannot open it, fall back natively, then replan."""
            nonlocal native_only
            native_only = False
            while True:
                outcome = mount(plan, switching)
                if outcome != "failed":
                    return outcome
                if used_adaptive:
                    log_event("adaptive_open_failed")
                    native_only = True
                    continue
                if not session.can_replan() or session.attempt_count >= MAX_RECOVERY_ATTEMPTS:
                    raise PlaybackError("Kodi could not open the original media file." if not is_converted(plan)
                                        else "Kodi could not open the converted stream.")
                # The route itself failed on this device: let the server pick
                # another one, excluding every key already tried.
                log_event("failure_recovery", session.position)
                plan = session.replan("failure_recovery", position=session.position,
                                      classification="player_open_failed", client=self.client)
                native_only = False

        # Lower quality when playback keeps buffering (buffering_quality.py).
        adoptions = {}
        buffering_requests, buffering_prompts = [], []
        caching_since = {"seek": 0.0, "reload": 0.0, "was": False}

        def post_plan_summary(summary):
            if bq.PLAN_SUMMARY_FEATURE not in session.server_features or not session.start_body:
                return
            ids = adoptions.get(summary.plan_id) or {}
            body = {"installation_id": session.installation_id, "event_id": str(uuid.uuid4()),
                    "protocol_version": 3, "playback_attempt_id": session.start_body["playback_attempt_id"],
                    "event": "plan_summary", "diagnostics": summary.diagnostics()}
            body.update({key: value for key, value in ids.items() if value})
            try:
                self.client.request("POST", "/playback/route-events", body)
            except (ApiError, OSError):
                log_event("plan_summary_failed")

        def buffering_notice(key):
            log_event("buffering_quality_" + key)
            notify(bq.COPY.get(key, ""))

        def buffering_pill(pill):
            if pill is None:
                return
            if pill.kind == bq.PROMPT:
                buffering_prompts.append(pill)
            else:
                # Kodi has no button overlay here; Quality in the Silo player menu restores it.
                log_event("buffering_quality_lowered")
                notify(bq.COPY["undo_caption"])

        buffering = bq.BufferingQualityController(
            on_replan=buffering_requests.append, on_notice=buffering_notice,
            on_plan_summary=post_plan_summary, on_pill=buffering_pill,
            clock=lambda: int(time.monotonic() * 1000))
        buffering.set_mode(session.buffering_mode)

        def adopt(plan, controller_initiated, switched=True):
            adoption_id = "{}#{}".format(plan.get("plan_id") or "plan", len(adoptions) + 1)
            adoptions[adoption_id] = {"session_id": session.session_id, "plan_id": plan.get("plan_id"),
                                      "plan_attempt_id": session.plan_attempt_id,
                                      "plan_attempt_key": plan.get("plan_attempt_key")}
            buffering.on_plan_adopted(bq.plan_from_wire(plan, session.quality_preference, adoption_id),
                                      controller_initiated=controller_initiated)
            if switched:  # Kodi refills its buffer after a stream switch; the first plan has the start-up grace.
                caching_since["reload"] = time.monotonic() + 15
            if not player.finished:
                buffering.on_playing()  # Kodi started this plan's first frame (onAVStarted).

        def ask_buffering(pill):
            """Ask first mode: Lower or Keep, closing by itself like the apps' prompt."""
            try:
                lower = xbmcgui.Dialog().yesno(bq.COPY["prompt_title"], "Lower the quality to stop buffering?",
                                               nolabel=bq.COPY["prompt_keep"], yeslabel=bq.COPY["prompt_lower"],
                                               autoclose=min(10, int(pill.seconds)) * 1000)
            except (AttributeError, RuntimeError, TypeError):
                return
            if lower:
                buffering.select_lower()
            elif buffering.pill is not None:
                buffering.select_keep()

        def switch(operation, target=None, buffering_replan=None, **changes):
            """Replan this session and move Kodi to the new stream."""
            nonlocal stage, recovery, previous
            position = target if target is not None else source_position()
            reporter.hold()
            try:
                stage = "replan"
                if buffering_replan is not None:
                    changes["quality"] = buffering_replan.quality_preference
                try:
                    plan = session.replan(operation, position=position, client=self.client,
                                          buffering=buffering_replan, **changes)
                except PlaybackError as error:
                    log_event("replan_refused_" + operation)
                    if buffering_replan is not None:
                        buffering.on_replan_failed()
                    else:
                        notify(str(error))
                    return None
                except (ApiError, OSError) as error:
                    log_event("replan_failed_" + operation, error=error if isinstance(error, ApiError) else None)
                    if buffering_replan is not None:
                        buffering.on_replan_failed()
                    if isinstance(error, ApiError) and error.status in (404, 410):
                        raise PlaybackError("The playback session ended. Open the title again.")
                    if buffering_replan is None:
                        notify("Silo could not change playback. Try again.")
                    return None
                log_event("replan_" + operation, position)
                outcome = open_plan(plan, switching=True)
                if outcome == "started":
                    recovery, previous = StallRecovery(), None
                    if extras is not None:
                        extras.update_plan(plan, session.quality_preference)
                    if operation == "quality_change" and buffering_replan is None:
                        buffering.on_viewer_quality_change()
                    adopt(plan, controller_initiated=buffering_replan is not None)
                    stage = "polling"
                elif buffering_replan is not None:
                    buffering.on_replan_failed()
                return outcome
            finally:
                reporter.release()

        def run_screen(command):
            """Open a subtitle tool from the player menu, paused while it is open."""
            name = command.get("screen")
            hook = self.open_screen
            if not callable(hook):
                notify("This option is not available here.")
                return
            kwargs = {"item": item, "file_id": effective_file_id(session.plan, file_id), "plan": session.plan}
            if name == "subtitle_translate":
                kwargs["position"] = source_position()
            if name == "subtitle_sync":
                kwargs["subtitle"] = command.get("subtitle")
            was_paused = player.paused
            if not was_paused:
                player.pause()
            if latest_sample is not None:
                reporter.submit(latest_sample[0], True, latest_sample[2])
            reporter.hold()
            try:
                result = hook(name, **kwargs)
            except Exception:  # A tool must never end playback.
                log_event("screen_failed_" + "".join(char for char in str(name) if char.isalnum() or char == "_"))
                result = None
            finally:
                reporter.release()
            if player.finished:
                return
            if isinstance(result, str) and result:
                player.setSubtitles(result)
                player.showSubtitles(True)
                self.loaded_subtitle = result
            if not was_paused:
                player.pause()

        def ai_enabled():
            hook = self.subtitle_ai_enabled
            if not callable(hook):
                return False
            reporter.hold()
            try:
                return bool(hook())
            except Exception:
                return False
            finally:
                reporter.release()

        def extras_seek(target):
            # On a server-anchored timeline a deliberate jump is a reanchor.
            if server_anchored(session.plan):
                pending_reanchor.append(_position(target) + session.plan["timeline"]["timeline_offset_seconds"])
            else:
                player.seekTime(target)

        recovery, previous = StallRecovery(), None
        try:
            version = "".join(char for char in self.addon.getAddonInfo("version") if char in "0123456789.")[:24]
            log_event("begin_v" + (version or "unknown"))
            plan = session.start(str(file_id) if file_id is not None else select_file(item), start_position)
            log_event("plan_ready", session.position)
            reporter = start_reporter()
            reporter.hold()  # Kodi may take up to a minute to open a converted stream.
            try:
                outcome = open_plan(plan)
            finally:
                reporter.release()
            if outcome != "started":
                return
            stage = "polling"
            adopt(session.plan, controller_initiated=False, switched=False)
            if not player.finished:
                extras = self._extras(player, item, session.plan, options, file_id, log_event,
                                      seek=extras_seek, ai_enabled=ai_enabled)
            extras_total, extras_total_at = None, 0.0
            revision = player.state_revision
            unavailable_since = None
            # Back during fullscreen video returns Kodi to its menus while the
            # video keeps playing, and this add-on's screens stay frozen until
            # it ends. Like the Silo apps, leaving fullscreen video stops it.
            stop_on_back = options.get("stop_on_back", True)
            resume_notice_shown = False
            fullscreen_seen, away_since = False, None
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
                    if guard.retry_due() and not player.finished:
                        log_event("resume_seek_retry", current)
                        caching_since["seek"] = time.monotonic() + 10
                        player.seekTime(guard.target)
                    continue
                if guard.gave_up and not resume_notice_shown:
                    resume_notice_shown = True
                    log_event("resume_seek_gave_up", current)
                    xbmcgui.Dialog().notification("Silo", "Could not jump to your saved position; playing from here.",
                                                  time=4000, sound=False)
                offset = session.plan["timeline"]["timeline_offset_seconds"]
                latest_sample = (_position(current), bool(player.paused), _number(offset, signed=True))
                reporter.submit(*latest_sample)
                now = time.monotonic()
                if previous is not None and not server_anchored(session.plan):
                    expected = previous[0] + (0.0 if latest_sample[1] else now - previous[1])
                    if abs(latest_sample[0] - expected) > SEEK_JUMP_SECONDS:
                        # A viewer seek Kodi made locally: its buffering is not a stall.
                        buffering.on_seek()
                        caching_since["seek"] = now + 10
                if server_anchored(session.plan) and previous is not None:
                    expected = previous[0] + (0.0 if latest_sample[1] else now - previous[1])
                    if abs(latest_sample[0] - expected) > SEEK_JUMP_SECONDS:
                        # Kodi seeked inside a growing playlist; the server has
                        # to re-cut the stream at the new position (§6).
                        log_event("seek_reanchor", latest_sample[0])
                        buffering.on_seek()
                        previous = None
                        if extras is not None:
                            extras.interaction()
                        if switch("seek_reanchor", target=source_position()) not in (None, "started"):
                            break
                        continue
                previous = (latest_sample[0], now)
                if stop_on_back and not player._stale():
                    # Kodi's own dialogs and Silo's overlays sit above fullscreen
                    # video, so they keep it visible; only leaving it counts.
                    if has_condition("Window.IsVisible(fullscreenvideo)"):
                        fullscreen_seen, away_since = True, None
                    elif fullscreen_seen:
                        away_since = away_since or now
                        if now - away_since >= LEFT_FULLSCREEN_SECONDS:
                            log_event("left_fullscreen_stop", latest_sample[0])
                            stop_local()
                            break
                caching = has_condition("Player.Caching")
                if caching and not caching_since["was"]:
                    cause = (bq.SEEK if now < caching_since["seek"] else
                             bq.RELOAD if now < caching_since["reload"] else bq.NETWORK)
                    buffering.on_waiting(cause)
                elif caching_since["was"] and not caching and not latest_sample[1]:
                    buffering.on_playing()
                caching_since["was"] = caching
                buffering.tick()
                if recovery.nudges >= recovery.max_nudges:
                    if not recovery.exhausted_reported:
                        recovery.exhausted_reported = True
                        log_event("stall_recovery_exhausted", current)
                total = None
                target = recovery.observe(latest_sample[0], latest_sample[1], caching)
                if target is not None and not player.finished:
                    try:
                        total = player.getTotalTime()
                    except (AttributeError, RuntimeError):
                        total = None
                    if total and total > 0 and target >= total - 5:
                        target = None
                    if target is not None:
                        log_event("stall_recovery_seek", current)
                        caching_since["seek"] = time.monotonic() + 10
                        player.seekTime(target)
                command = None
                if extras is not None and not player.finished:
                    if total is not None or time.monotonic() - extras_total_at >= 5:
                        extras_total_at = time.monotonic()
                        if total is None and plan_duration(session.plan) is None:
                            try:
                                total = player.getTotalTime()
                            except (AttributeError, RuntimeError):
                                total = None
                        extras_total = plan_duration(session.plan) or total or extras_total
                    command = extras.tick(latest_sample[0], latest_sample[1], extras_total)
                if command and command.get("native"):
                    xbmc.executebuiltin("ActivateWindow({})".format(command["native"]))
                elif command and command.get("stop"):
                    self.next_action = command
                    log_event("stop_for_" + command["stop"], current)
                    stop_local()
                    break
                elif command and command.get("screen"):
                    run_screen(command)
                elif command and command.get("replan") == "quality":
                    if switch("quality_change", quality=command.get("quality")) not in (None, "started"):
                        break
                elif command and command.get("replan") == "track":
                    if switch("track_change", audio_index=command.get("audio_index")) not in (None, "started"):
                        break
                if buffering_prompts and not player.finished:
                    ask_buffering(buffering_prompts.pop())
                    del buffering_prompts[:]
                if buffering_requests and not player.finished:
                    replan = buffering_requests.pop()
                    del buffering_requests[:]
                    log_event("buffering_quality_replan")
                    if switch("quality_change", buffering_replan=replan) not in (None, "started"):
                        break
                if pending_reanchor and not player.finished:
                    target = pending_reanchor.pop()
                    del pending_reanchor[:]
                    if switch("seek_reanchor", target=target) not in (None, "started"):
                        break
                if revision != player.state_revision:
                    revision = player.state_revision
                    log_event("paused" if player.paused else "resumed", current)
                    if player.paused:
                        buffering.on_pause()
                    elif not caching:
                        buffering.on_playing()
                    if extras is not None:
                        extras.interaction()
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
            if extras is not None:
                extras.close()
            stop_local()
            try:
                buffering.on_playback_stopped()  # Sends the playing plan's summary.
            except Exception:  # Diagnostics must never break cleanup.
                log_event("plan_summary_failed")
            if media_relay is not None:
                media_relay.close()
            source = session.position
            if latest_sample is not None and session.plan:
                source = source_position()
            self.last_result = {"has_sample": latest_sample is not None,
                                "position_seconds": source, "ended": bool(player.ended)}
            if not (isinstance(self.next_action, dict) and self.next_action.get("auto")):
                # Only an unattended Up next countdown keeps the streak going.
                self.streak.reset()
            if reporter is None and session.session_id is not None:
                reporter = start_reporter()
            if reporter is not None:
                if latest_sample is not None:
                    reporter.submit(*latest_sample)
                reporter.close()
                log_event("cleanup_queued", source if latest_sample else None)
