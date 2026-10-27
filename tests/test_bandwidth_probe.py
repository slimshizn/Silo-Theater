"""The connection speed test replays the server's probe vectors and follows the probe contract.

``probe_cases`` live in the shared buffering-quality vectors
(``tests/fixtures/buffering_quality_vectors.json``, vendored byte for byte
from the Silo server repo). ``connection_budget_cases`` are server-only.
"""

import json
from pathlib import Path
import socket
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo import bandwidth_probe as bp

VECTORS = json.loads((Path(__file__).resolve().parent / "fixtures" / "buffering_quality_vectors.json")
                     .read_text(encoding="utf-8"))
MODES = {"quick": bp.QUICK, "accurate": bp.ACCURATE}
ORIGIN = 1000  # When the first byte arrives on the fake clock.


class FakeClock(object):
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


class Sized(object):
    """A chunk of ``n`` bytes without allocating them (vectors move up to 1 GB)."""

    def __init__(self, n):
        self.n = n

    def __len__(self):
        return self.n

    def __bool__(self):
        return self.n > 0


class FakeResponse(object):
    def __init__(self, clock, status=200, headers=None, chunks=(), end=None, first_byte_at=ORIGIN):
        self.clock, self.status, self.headers = clock, status, dict(headers or {})
        self.chunks, self.end, self.first_byte_at = list(chunks), end, first_byte_at
        self.closed = False
        self.reads = 0

    def read(self, size):
        self.reads += 1
        if self.chunks:
            at, count = self.chunks.pop(0)
            self.clock.now = max(self.clock.now, self.first_byte_at + at)
            return Sized(count)
        if self.end is not None:
            self.clock.now = max(self.clock.now, self.first_byte_at + self.end)
            return b""
        self.clock.now += 250
        raise socket.timeout("timed out")

    def close(self):
        self.closed = True


def expected_result(expect):
    if expect is None:
        return None
    return bp.SpeedTestResult(expect["kbps"], expect["lower_bound"], expect["bytes"], expect["duration_ms"])


class ProbeVectorsTest(unittest.TestCase):
    def test_constants_match_the_vectors(self):
        constants = VECTORS["probe_constants"]
        self.assertEqual(bp.PROBE_WINDOW_MS, constants["PROBE_WINDOW_MS"])
        self.assertEqual(bp.QUICK.request_bytes, constants["QUICK_PROBE_BYTES"])
        self.assertEqual(bp.QUICK.max_ms, constants["QUICK_PROBE_MAX_MS"])
        self.assertEqual(bp.QUICK.settle_windows, constants["QUICK_SETTLE_WINDOWS"])
        self.assertEqual(bp.ACCURATE.request_bytes, constants["ACCURATE_PROBE_BYTES"])
        self.assertEqual(bp.ACCURATE.max_ms, constants["ACCURATE_PROBE_MAX_MS"])
        self.assertEqual(bp.ACCURATE.settle_windows, constants["ACCURATE_SETTLE_WINDOWS"])
        self.assertEqual(bp.PROBE_FIRST_BYTE_DEADLINE_MS, constants["PROBE_FIRST_BYTE_DEADLINE_MS"])
        self.assertEqual(bp.MIN_PROBE_KBPS, constants["MIN_PROBE_KBPS"])
        self.assertEqual(bp.MAX_PROBE_KBPS, constants["MAX_PROBE_KBPS"])

    def test_every_probe_case_replays_on_the_meter(self):
        cases = VECTORS["probe_cases"]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case["name"]):
                mode = MODES[case["mode"]]
                meter = bp.SpeedTestMeter(mode, case.get("target_kbps"))
                for at, count in case["chunks"]:
                    meter.on_bytes(at, count)
                if case.get("end") is not None:
                    meter.on_end(case["end"])
                else:
                    meter.advance_to(mode.max_ms)
                expect = case["expect"]
                self.assertEqual(meter.result(), expected_result(expect), case["name"])
                if expect is not None:
                    self.assertEqual(meter.stop_at_ms, expect["stop"], case["name"] + ": stop")

    def test_every_probe_case_replays_through_a_download(self):
        for case in VECTORS["probe_cases"]:
            with self.subTest(case=case["name"]):
                clock = FakeClock(0)
                response = FakeResponse(clock, chunks=case["chunks"], end=case.get("end"))
                outcome = bp.BandwidthProbe(clock).measure(
                    MODES[case["mode"]], case.get("target_kbps"), lambda url, auth: response, "http://s/p")
                expect = expected_result(case["expect"])
                if expect is None:
                    self.assertEqual(outcome.kind, bp.TOO_LITTLE_DATA, case["name"])
                else:
                    self.assertEqual((outcome.kind, outcome.result), (bp.MEASURED, expect), case["name"])
                    # It stops reading once the test stops: chunks due after the
                    # first one past the stop are never read.
                    later = [at for at, _ in case["chunks"] if at > case["expect"]["stop"]]
                    self.assertEqual(len(response.chunks), max(0, len(later) - 1), case["name"])
                self.assertTrue(response.closed, case["name"])


class DownloadContractTest(unittest.TestCase):
    def measure(self, responses, mode=bp.QUICK, target=None, clock=None):
        clock = clock or FakeClock(0)
        calls = []

        def open_url(url, authenticated):
            calls.append((url, authenticated))
            return responses.pop(0)
        outcome = bp.BandwidthProbe(clock).measure(mode, target, open_url,
                                                   bp.probe_download_url("http://s/api/v2/playback/bandwidth-probe"
                                                                         "?media_path=true", mode))
        return outcome, calls

    def test_download_url_sets_the_size(self):
        self.assertEqual(bp.probe_download_url("https://s/api/v2/playback/bandwidth-probe?media_path=true&size=5",
                                               bp.ACCURATE),
                         "https://s/api/v2/playback/bandwidth-probe?media_path=true&size=1000000000")
        self.assertEqual(bp.resolve_probe_url("https://s:8096/", "/api/v2/playback/bandwidth-probe?media_path=true"),
                         "https://s:8096/api/v2/playback/bandwidth-probe?media_path=true")

    def test_rate_limited_reads_retry_after(self):
        clock = FakeClock()
        for header, seconds in (("12", 12), ("0", 1), (None, 30), ("Wed, 21 Oct 2026 07:28:00 GMT", 30)):
            headers = {"Retry-After": header} if header is not None else {}
            outcome, _ = self.measure([FakeResponse(clock, status=429, headers=headers)], clock=clock)
            self.assertEqual((outcome.kind, outcome.retry_after_seconds), (bp.RATE_LIMITED, seconds))

    def test_redirect_is_followed_once_without_credentials(self):
        clock = FakeClock()
        redirect = FakeResponse(clock, status=307, headers={"Location": "https://proxy/stream/bandwidth-probe/t?size=9"})
        body = FakeResponse(clock, chunks=[(0, 5000000), (750, 5000000), (1100, 1000000)], end=1300)
        outcome, calls = self.measure([redirect, body], clock=clock)
        self.assertEqual(calls[1], ("https://proxy/stream/bandwidth-probe/t?size=9", False))
        self.assertTrue(calls[0][1])
        self.assertEqual(outcome.result, bp.SpeedTestResult(80000, False, 5000000, 500))
        self.assertTrue(redirect.closed and body.closed)

    def test_refusals_are_unavailable(self):
        clock = FakeClock()
        outcome, _ = self.measure([FakeResponse(clock, status=503)], clock=clock)
        self.assertEqual((outcome.kind, outcome.status), (bp.UNAVAILABLE, 503))
        outcome, _ = self.measure([FakeResponse(clock, status=307)], clock=clock)
        self.assertEqual(outcome.kind, bp.UNAVAILABLE)

    def test_no_first_byte_within_ten_seconds_is_a_network_error(self):
        clock = FakeClock()
        outcome, _ = self.measure([FakeResponse(clock)], clock=clock)
        self.assertEqual(outcome.kind, bp.NETWORK_ERROR)
        self.assertIsInstance(outcome.error, bp.ProbeFirstByteTimeout)
        self.assertGreaterEqual(clock.now, bp.PROBE_FIRST_BYTE_DEADLINE_MS)

    def test_short_body_measured_nothing(self):
        clock = FakeClock()
        body = FakeResponse(clock, headers={"Content-Length": "100000000"},
                            chunks=[(0, 5000000), (750, 5000000), (1100, 1000000)], end=1300)
        outcome, _ = self.measure([body], clock=clock)
        self.assertEqual(outcome.kind, bp.NETWORK_ERROR)

    def test_connection_failure_is_a_network_error(self):
        def refuse(url, authenticated):
            raise OSError("refused")
        outcome = bp.BandwidthProbe(FakeClock()).measure(bp.QUICK, None, refuse, "http://s/p")
        self.assertEqual(outcome.kind, bp.NETWORK_ERROR)

    def test_report_body(self):
        result = bp.SpeedTestResult(115000, True, 7500000, 500)
        self.assertEqual(bp.report_body(result, "ethernet"),
                         {"kbps": 115000, "bytes": 7500000, "duration_ms": 500, "transport": "ethernet",
                          "lower_bound": True})
        self.assertEqual(bp.report_body(bp.SpeedTestResult(48000, False, 6000000, 1000), "lan", manual=True),
                         {"kbps": 48000, "bytes": 6000000, "duration_ms": 1000, "transport": "other",
                          "manual": True})


class ScheduleTest(unittest.TestCase):
    NOW = bp.parse_rfc3339_ms("2026-10-18T20:14:03Z")

    def capabilities(self, **extra):
        caps = {"features": ["connection_speed_v1"],
                "bandwidth_probe_url": "/api/v2/playback/bandwidth-probe?media_path=true",
                "bandwidth_probe_target_kbps": 115000, "bandwidth_probe_automatic": True,
                "bandwidth_probe_max_age_seconds": 604800}
        caps.update(extra)
        return caps

    def test_target_needs_the_feature_and_a_url(self):
        target = bp.ProbeTarget.from_capabilities(self.capabilities(), "https://s")
        self.assertEqual(target, bp.ProbeTarget("https://s/api/v2/playback/bandwidth-probe?media_path=true",
                                                115000, True, 604800000))
        self.assertIsNone(bp.ProbeTarget.from_capabilities(self.capabilities(features=[]), "https://s"))
        bare = bp.ProbeTarget.from_capabilities({"features": ["connection_speed_v1"],
                                                 "bandwidth_probe_url": "https://s/p"}, "https://s")
        self.assertEqual((bare.target_kbps, bare.automatic, bare.lifetime_ms),
                         (None, True, bp.DEFAULT_READING_LIFETIME_MS))

    def readings(self, probe=None, sample=None):
        return bp.Readings.from_connection({"connection": "remote", "probe": probe, "playback_sample": sample},
                                           self.NOW)

    def test_readings_parse_and_estimate(self):
        readings = self.readings(
            {"kbps": 48000, "lower_bound": False, "measured_at": "2026-10-17T19:02:11.123456789Z", "transport": "wifi"},
            {"kbps": 51200, "measured_at": "2026-10-18T20:14:03Z"})
        self.assertEqual(readings.probe.kbps, 48000)
        self.assertTrue(readings.sample.lower_bound)
        self.assertEqual(bp.fresh_estimate_kbps(readings, 604800000, self.NOW), 51200)
        # Past the lifetime nothing is current.
        self.assertIsNone(bp.fresh_estimate_kbps(readings, 1000, self.NOW + 2000))
        # Out of bounds and future-dated readings.
        odd = self.readings({"kbps": 50, "measured_at": "2026-10-17T19:02:11Z"},
                            {"kbps": 2000, "measured_at": "2030-01-01T00:00:00+00:00"})
        self.assertIsNone(odd.probe)
        self.assertEqual(odd.sample.measured_at_ms, self.NOW)

    def test_proving_the_link(self):
        settled = self.readings({"kbps": 20000, "measured_at": "2026-10-18T20:00:00Z"})
        low_bound = self.readings({"kbps": 20000, "lower_bound": True, "measured_at": "2026-10-18T20:00:00Z"})
        sample = self.readings(sample={"kbps": 120000, "measured_at": "2026-10-18T20:00:00Z"})
        life = 604800000
        self.assertTrue(bp.proves_the_link(settled, 115000, life, self.NOW))
        self.assertFalse(bp.proves_the_link(low_bound, 115000, life, self.NOW))
        self.assertTrue(bp.proves_the_link(low_bound, None, life, self.NOW))
        self.assertTrue(bp.proves_the_link(sample, 115000, life, self.NOW))
        self.assertFalse(bp.proves_the_link(self.readings(), 115000, life, self.NOW))
        target = bp.ProbeTarget.from_capabilities(self.capabilities(), "https://s")
        self.assertIsNone(bp.automatic_test_blocked(target, low_bound, self.NOW))
        self.assertEqual(bp.automatic_test_blocked(target, settled, self.NOW), bp.NOT_TESTED)
        self.assertEqual(bp.automatic_test_blocked(target, low_bound, self.NOW, rate_limit_wait_ms=5), bp.NOT_TESTED)
        off = bp.ProbeTarget.from_capabilities(self.capabilities(bandwidth_probe_automatic=False), "https://s")
        self.assertEqual(bp.automatic_test_blocked(off, low_bound, self.NOW), bp.NOT_TESTED)

    def test_gates(self):
        self.assertEqual(bp.probe_blocked(False, connected=False), bp.OFFLINE)
        self.assertEqual(bp.probe_blocked(True, metered=True), bp.METERED)
        self.assertEqual(bp.probe_blocked(True, media_active=True), bp.PLAYBACK_ACTIVE)
        self.assertEqual(bp.probe_blocked(False, last_playback_ms=0, now_monotonic_ms=59999), bp.PLAYBACK_ACTIVE)
        self.assertIsNone(bp.probe_blocked(True, last_playback_ms=0, now_monotonic_ms=59999))
        self.assertIsNone(bp.probe_blocked(False, last_playback_ms=0, now_monotonic_ms=60000))

    def test_rate_limits_hold_per_server(self):
        clock = FakeClock(0)
        limits = bp.RateLimits(clock)
        limits.hold("a", 0)
        self.assertEqual(limits.wait_ms("a"), 1000)
        self.assertEqual(limits.wait_ms("b"), 0)
        clock.now = 1000
        self.assertEqual(limits.wait_ms("a"), 0)


if __name__ == "__main__":
    unittest.main()
