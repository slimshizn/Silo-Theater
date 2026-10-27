"""The connection speed test every Silo client runs, without the HTTP.

A port of the Silo Android ``SpeedTestMeter``, ``BandwidthProbe`` and the
decisions of ``BandwidthProbeScheduler``; the contract is *Connection speed
test* in the server repo's ``docs/design/2026-10-17-buffering-quality.md``,
and the tests replay its ``probe_cases``. No Kodi dependency.

Flow, for a server whose ``/api/v2/playback/capabilities`` ``features`` carry
``connection_speed_v1`` and name a ``bandwidth_probe_url``::

    target = ProbeTarget.from_capabilities(capabilities, server_url)
    readings = Readings.from_connection(GET /api/v2/playback/connection, now_epoch_ms)
    reason = automatic_test_blocked(target, readings, now_epoch_ms, ...)   # None: test
    outcome = BandwidthProbe(clock).measure(QUICK, target.target_kbps, open_url,
                                            probe_download_url(target.url, QUICK))
    if outcome.kind == MEASURED:
        POST /api/v2/playback/bandwidth-probe/results  report_body(outcome.result, transport, manual)
    elif outcome.kind == RATE_LIMITED:
        rate_limits.hold(server_id, outcome.retry_after_seconds)

``open_url(url, authenticated)`` is injected: it sends a GET with
``Accept: application/octet-stream`` and ``Cache-Control: no-store``, with
Silo's credentials only when ``authenticated`` (the 307 to a proxy carries its
own token), follows no redirect, and returns an object with ``status``,
``headers`` (a mapping with ``get``), ``read(n)`` (bytes; ``b""`` at the end;
``socket.timeout`` when nothing arrived for a while) and ``close()``. Closing
early ends the transfer; the server stops at its next write.
"""

import socket
import time
from collections import namedtuple
from datetime import datetime, timezone

from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

CONNECTION_SPEED_FEATURE = "connection_speed_v1"
BANDWIDTH_PROBE_FEATURE = "bandwidth_probe_v1"

PROBE_PATH = "/playback/bandwidth-probe"
RESULTS_PATH = "/playback/bandwidth-probe/results"
CONNECTION_PATH = "/playback/connection"
LEARNED_LIMIT_PATH = "/playback/connection/learned-limit"

PROBE_WINDOW_MS = 500
PROBE_FIRST_BYTE_DEADLINE_MS = 10000
MIN_PROBE_KBPS = 100
MAX_PROBE_KBPS = 1000000
READ_BUFFER_BYTES = 64 * 1024
DEFAULT_RETRY_AFTER_SECONDS = 30
REDIRECT_STATUSES = (301, 302, 303, 307, 308)

# Let the first screen settle before spending bandwidth on a probe.
PROBE_START_DELAY_MS = 5000
# How long a reading stands when capabilities name no max age.
DEFAULT_READING_LIFETIME_MS = 7 * 24 * 60 * 60 * 1000
# Automatic tests hold off this long after a playback start.
PLAYBACK_START_QUIET_MS = 60000

TRANSPORTS = ("wifi", "ethernet", "cellular", "other")


class SpeedTestMode(namedtuple("SpeedTestMode", "name request_bytes max_ms settle_windows")):
    """One of the two tests: the ``size`` it asks for, how long it reads, its settle span."""


# Automatic: stops once a window proves the server's target.
QUICK = SpeedTestMode("quick", 100000000, 3000, 2)
# Test speed only: the rate held over the final seconds.
ACCURATE = SpeedTestMode("accurate", 1000000000, 10000, 6)


class SpeedTestResult(namedtuple("SpeedTestResult", "kbps lower_bound bytes duration_ms")):
    """``kbps`` from ``bytes`` over ``duration_ms``; ``lower_bound``: the link is at least that."""


def _rate_kbps(byte_count, windows):
    # Bits per millisecond is kbit/s.
    return byte_count * 8 // (windows * PROBE_WINDOW_MS)


class SpeedTestMeter(object):
    """The speed test's arithmetic: times are ms after the first body byte.

    Bytes count in the 500 ms window they arrive in; window 0 (TCP ramp-up)
    never counts; a window with no bytes still closes, at 0, when the clock
    passes it (``advance_to``). Once ``stop_at_ms`` is set the test stops
    reading and later bytes are ignored. ``target_kbps`` applies to the quick
    test only.
    """

    def __init__(self, mode, target_kbps=None):
        self.mode = mode
        self.target = target_kbps if mode == QUICK else None
        self._closed = []
        self._open_bytes = 0
        self.total_bytes = 0
        self._end_ms = None
        self._target_window_bytes = None
        self.stop_at_ms = None

    @property
    def stopped(self):
        return self.stop_at_ms is not None

    def advance_to(self, now_ms):
        """Close every window that ended by ``now_ms``."""
        while not self.stopped and (len(self._closed) + 1) * PROBE_WINDOW_MS <= now_ms:
            window = self._open_bytes
            self._closed.append(window)
            self._open_bytes = 0
            window_end = len(self._closed) * PROBE_WINDOW_MS
            if len(self._closed) > 1 and self.target is not None and _rate_kbps(window, 1) >= self.target:
                self._target_window_bytes = window
                self.stop_at_ms = window_end
            elif window_end >= self.mode.max_ms:
                self.stop_at_ms = self.mode.max_ms

    def on_bytes(self, at_ms, count):
        """``count`` bytes arrived at ``at_ms``."""
        self.advance_to(at_ms)
        if self.stopped:
            return
        self._open_bytes += count
        self.total_bytes += count

    def on_end(self, at_ms):
        """The body ended cleanly at ``at_ms``; a window it cut short does not count."""
        self.advance_to(at_ms)
        if self.stopped:
            return
        self.stop_at_ms = at_ms
        self._end_ms = at_ms

    def result(self):
        """The SpeedTestResult, or None when the test measured nothing."""
        if self._target_window_bytes is not None:
            if self.target < MIN_PROBE_KBPS:
                return None
            return SpeedTestResult(min(self.target, MAX_PROBE_KBPS), True, self._target_window_bytes,
                                   PROBE_WINDOW_MS)
        counted = self._closed[1:][-self.mode.settle_windows:]
        if not counted:
            return self._whole_transfer()
        total = sum(counted)
        kbps = _rate_kbps(total, len(counted))
        duration = len(counted) * PROBE_WINDOW_MS
        if kbps < MIN_PROBE_KBPS:
            return None
        if kbps > MAX_PROBE_KBPS:
            return SpeedTestResult(MAX_PROBE_KBPS, True, total, duration)
        return SpeedTestResult(kbps, False, total, duration)

    def _whole_transfer(self):
        # Every requested byte arrived before window 1 completed: the average
        # from first to last byte, ramp included, so always a lower bound.
        if self._end_ms is None or self.total_bytes < self.mode.request_bytes:
            return None
        average = self.total_bytes * 8 // max(1, self._end_ms)
        kbps = self.target if self.target is not None and average >= self.target else average
        kbps = min(kbps, MAX_PROBE_KBPS)
        if kbps < MIN_PROBE_KBPS:
            return None
        return SpeedTestResult(kbps, True, self.total_bytes, self._end_ms)


# ---- One download --------------------------------------------------------------

MEASURED, RATE_LIMITED, UNAVAILABLE, TOO_LITTLE_DATA, NETWORK_ERROR = (
    "measured", "rate_limited", "unavailable", "too_little_data", "network_error")


class ProbeOutcome(namedtuple("ProbeOutcome", "kind result retry_after_seconds status error")):
    """What one test came to; only the field for ``kind`` is set."""


def _outcome(kind, result=None, retry_after_seconds=None, status=None, error=None):
    return ProbeOutcome(kind, result, retry_after_seconds, status, error)


class ProbeFirstByteTimeout(Exception):
    """No probe byte arrived within PROBE_FIRST_BYTE_DEADLINE_MS of the request."""


class ProbeShortBody(Exception):
    """The body ended short of its Content-Length."""


def parse_retry_after(value):
    """Seconds from a 429's Retry-After: missing or unreadable is 30, under 1 is 1."""
    try:
        seconds = int(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_RETRY_AFTER_SECONDS
    return max(1, seconds)


def resolve_probe_url(server_url, advertised):
    """``bandwidth_probe_url``, resolved against the server URL when relative."""
    return urljoin(server_url, advertised)


def probe_download_url(probe_url, mode):
    """``probe_url`` with ``size`` set to the bytes ``mode`` asks for."""
    parts = urlsplit(probe_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "size"]
    query.append(("size", str(mode.request_bytes)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _timed_out(error):
    return isinstance(error, (socket.timeout, TimeoutError))


class BandwidthProbe(object):
    """Times one speed test download. ``clock`` returns monotonic milliseconds."""

    def __init__(self, clock=None):
        self.clock = clock or (lambda: int(time.monotonic() * 1000))

    def measure(self, mode, target_kbps, open_url, url, cancelled=None):
        """Run one test of ``mode`` against ``url`` (see probe_download_url).

        ``cancelled`` is an optional callable checked between reads; when it
        returns True the test stops and measured nothing (NETWORK_ERROR with
        error "cancelled"), as when media starts playing.
        """
        cancelled = cancelled or (lambda: False)
        requested_at = self.clock()
        try:
            response = open_url(url, True)
        except Exception as error:  # noqa: BLE001 - any failure is a network error
            return _outcome(NETWORK_ERROR, error=error)
        try:
            status = int(getattr(response, "status", 0) or 0)
            if status in REDIRECT_STATUSES:
                location = (response.headers.get("Location") or "").strip()
                response.close()
                if not location:
                    return _outcome(UNAVAILABLE, status=status)
                # Followed once, without Silo's credentials: it carries its own token.
                response = open_url(urljoin(url, location), False)
                status = int(getattr(response, "status", 0) or 0)
                if status in REDIRECT_STATUSES:
                    return _outcome(UNAVAILABLE, status=status)
            if status == 429:
                return _outcome(RATE_LIMITED, retry_after_seconds=parse_retry_after(
                    response.headers.get("Retry-After")))
            if status != 200:
                return _outcome(UNAVAILABLE, status=status)
            meter = SpeedTestMeter(mode, target_kbps)
            error = self._time(response, meter, requested_at, cancelled)
            if error is not None:
                return _outcome(NETWORK_ERROR, error=error)
            result = meter.result()
            return _outcome(MEASURED, result=result) if result is not None else _outcome(TOO_LITTLE_DATA)
        except Exception as error:  # noqa: BLE001
            return _outcome(NETWORK_ERROR, error=error)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    def _time(self, response, meter, requested_at, cancelled):
        length = response.headers.get("Content-Length")
        try:
            length = int(length) if length is not None else None
        except ValueError:
            length = None
        received = 0
        # The clock starts at the first body byte, so request latency does not count.
        while True:
            if cancelled():
                return "cancelled"
            try:
                chunk = response.read(READ_BUFFER_BYTES)
            except Exception as error:  # noqa: BLE001
                if not _timed_out(error):
                    return error
                chunk = None
            if chunk:
                break
            if chunk is not None:
                return None  # Ended before any byte: measured nothing.
            if self.clock() - requested_at >= PROBE_FIRST_BYTE_DEADLINE_MS:
                return ProbeFirstByteTimeout()
        origin = self.clock()
        received += len(chunk)
        meter.on_bytes(0, len(chunk))
        while not meter.stopped:
            if cancelled():
                return "cancelled"
            try:
                chunk = response.read(READ_BUFFER_BYTES)
            except Exception as error:  # noqa: BLE001
                if not _timed_out(error):
                    return error
                meter.advance_to(self.clock() - origin)
                continue
            at = self.clock() - origin
            if not chunk:
                if length is not None and received < length:
                    return ProbeShortBody()
                meter.on_end(at)
            else:
                received += len(chunk)
                meter.on_bytes(at, len(chunk))
        return None


def report_body(result, transport="other", manual=False):
    """The ``POST /api/v2/playback/bandwidth-probe/results`` body for ``result``."""
    body = {"kbps": result.kbps, "bytes": result.bytes, "duration_ms": result.duration_ms,
            "transport": transport if transport in TRANSPORTS else "other"}
    if result.lower_bound:
        body["lower_bound"] = True
    if manual:
        body["manual"] = True
    return body


# ---- Readings and when to test ---------------------------------------------------

def parse_rfc3339_ms(value):
    """Epoch milliseconds from an RFC 3339 time, or None."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().replace("z", "Z")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    head, sep, tail = text.partition(".")
    if sep:
        digits = ""
        while tail and tail[0].isdigit():
            digits, tail = digits + tail[0], tail[1:]
        text = head + "." + (digits + "000000")[:6] + tail
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


class ProbeTarget(namedtuple("ProbeTarget", "url target_kbps automatic lifetime_ms")):
    """The probe endpoint and schedule from the capability response."""

    @classmethod
    def from_capabilities(cls, capabilities, server_url):
        """None unless the server advertises connection_speed_v1 with a probe URL."""
        capabilities = capabilities or {}
        advertised = capabilities.get("bandwidth_probe_url")
        if (CONNECTION_SPEED_FEATURE not in (capabilities.get("features") or [])
                or not isinstance(advertised, str) or not advertised.strip()):
            return None
        target = capabilities.get("bandwidth_probe_target_kbps")
        target = target if isinstance(target, int) and not isinstance(target, bool) and target > 0 else None
        age = capabilities.get("bandwidth_probe_max_age_seconds")
        lifetime = age * 1000 if isinstance(age, int) and not isinstance(age, bool) and age > 0 \
            else DEFAULT_READING_LIFETIME_MS
        automatic = capabilities.get("bandwidth_probe_automatic")
        return cls(resolve_probe_url(server_url, advertised.strip()), target, automatic is not False, lifetime)


class Reading(namedtuple("Reading", "kbps measured_at_ms lower_bound")):
    """A probe result or playback sample, dated in epoch ms."""


def _reading(entry, now_ms, lower_bound=None):
    if not isinstance(entry, dict):
        return None
    kbps = entry.get("kbps")
    measured = parse_rfc3339_ms(entry.get("measured_at"))
    if (measured is None or isinstance(kbps, bool) or not isinstance(kbps, int)
            or not MIN_PROBE_KBPS <= kbps <= MAX_PROBE_KBPS):
        return None
    # A time the server dated ahead of this device's clock counts as now.
    bound = bool(entry.get("lower_bound")) if lower_bound is None else lower_bound
    return Reading(kbps, min(measured, now_ms), bound)


class Readings(namedtuple("Readings", "probe sample learned_limit auto_budget_kbps connection")):
    """What ``GET /api/v2/playback/connection`` holds for this device and connection."""

    @classmethod
    def from_connection(cls, response, now_ms):
        response = response or {}
        limit = response.get("learned_limit") if isinstance(response.get("learned_limit"), dict) else None
        budget = response.get("auto_budget_kbps")
        return cls(_reading(response.get("probe"), now_ms),
                   _reading(response.get("playback_sample"), now_ms, lower_bound=True),
                   limit, budget if isinstance(budget, int) and not isinstance(budget, bool) else None,
                   response.get("connection"))

    def with_probe(self, reading, keep_sample=True):
        """These readings after this device stored a result (Test speed drops the sample)."""
        probe = self.probe if self.probe is not None and self.probe.measured_at_ms > reading.measured_at_ms \
            else reading
        return self._replace(probe=probe, sample=self.sample if keep_sample else None)


def is_current(reading, lifetime_ms, now_ms):
    """Younger than the lifetime; a reading from the future counts."""
    return reading is not None and now_ms - reading.measured_at_ms < lifetime_ms


def fresh_estimate_kbps(readings, lifetime_ms, now_ms):
    """The start's ``bandwidth_estimate_kbps`` (source ``probe``): the highest current reading, or None."""
    if readings is None:
        return None
    values = [r.kbps for r in (readings.probe, readings.sample) if is_current(r, lifetime_ms, now_ms)]
    return max(values) if values else None


def proves_the_link(readings, target_kbps, lifetime_ms, now_ms):
    """A settled current probe, or a current lower bound at least the target; any reading without one."""
    if readings is None:
        return False
    probe = readings.probe if is_current(readings.probe, lifetime_ms, now_ms) else None
    sample = readings.sample if is_current(readings.sample, lifetime_ms, now_ms) else None
    if probe is not None and (not probe.lower_bound or target_kbps is None or probe.kbps >= target_kbps):
        return True
    return sample is not None and (target_kbps is None or sample.kbps >= target_kbps)


class RateLimits(object):
    """After a 429: per server, no automatic test before Retry-After passes (monotonic ms)."""

    def __init__(self, clock=None):
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self._not_before = {}

    def hold(self, server_id, retry_after_seconds):
        now = self.clock()
        self._not_before = {k: v for k, v in self._not_before.items() if v > now}
        self._not_before[server_id] = now + max(1, int(retry_after_seconds)) * 1000

    def wait_ms(self, server_id):
        """Milliseconds until the hold lifts, 0 when none."""
        return max(0, self._not_before.get(server_id, 0) - self.clock())


# Reasons a test does not run, or None.
NOT_SUPPORTED, OFFLINE, METERED, PLAYBACK_ACTIVE, NOT_TESTED = (
    "not_supported", "offline", "metered", "playback_active", "not_tested")


def probe_blocked(manual, connected=True, metered=False, media_active=False,
                 last_playback_ms=None, now_monotonic_ms=None):
    """Why a test may not run now (OFFLINE, METERED, PLAYBACK_ACTIVE), or None.

    The quiet period after a playback start holds automatic tests only.
    """
    if not connected:
        return OFFLINE
    if metered:
        return METERED
    if media_active:
        return PLAYBACK_ACTIVE
    if (not manual and last_playback_ms is not None and now_monotonic_ms is not None
            and now_monotonic_ms - last_playback_ms < PLAYBACK_START_QUIET_MS):
        return PLAYBACK_ACTIVE
    return None


def automatic_test_blocked(target, readings, now_epoch_ms, rate_limit_wait_ms=0):
    """Why the automatic quick test is not due (after the connection read), or None.

    Check ``probe_blocked(False, ...)`` too, before and after PROBE_START_DELAY_MS.
    """
    if target is None:
        return NOT_SUPPORTED
    if proves_the_link(readings, target.target_kbps, target.lifetime_ms, now_epoch_ms):
        return NOT_TESTED
    if not target.automatic or rate_limit_wait_ms > 0:
        return NOT_TESTED
    return None
