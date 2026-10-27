"""Settings → Test connection speed reports a manual test and can clear a learned limit."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo import bandwidth_probe as probe
from silo import svc_connection

CAPABILITIES = {"features": ["connection_speed_v1"],
                "bandwidth_probe_url": "/api/v2/playback/bandwidth-probe?media_path=true"}


class Client:
    server_url = "https://silo.invalid"
    user_agent = "FoundersTheater"

    def __init__(self, capabilities=CAPABILITIES, connection=None):
        self.calls = []
        self.capabilities, self.connection = capabilities, connection or {"learned_limit": None}

    def headers(self):
        return {"Authorization": "Bearer token"}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path == "/playback/capabilities":
            return self.capabilities
        if path == probe.CONNECTION_PATH and method == "GET":
            return self.connection
        return None


class SpeedTestTests(unittest.TestCase):
    def setUp(self):
        self.progress = MagicMock()
        self.progress.iscanceled.return_value = False
        modules = patch.dict(sys.modules, {"xbmcgui": SimpleNamespace(DialogProgress=lambda: self.progress)})
        modules.start()
        self.addCleanup(modules.stop)

    def run_test(self, client, outcome, yes=False):
        application = SimpleNamespace(client=client, dialog=MagicMock())
        application.dialog.yesno.return_value = yes
        with patch.object(svc_connection.probe, "BandwidthProbe") as meter:
            meter.return_value.measure.return_value = outcome
            svc_connection.test_connection_speed(application)
        return application.dialog, meter

    def measured(self, kbps=48000, lower_bound=False):
        return probe.ProbeOutcome(probe.MEASURED, probe.SpeedTestResult(kbps, lower_bound, 60000000, 10000),
                                  None, None, None)

    def test_a_measurement_is_reported_as_manual_and_shown(self):
        client = Client()
        dialog, meter = self.run_test(client, self.measured())
        mode, _target, _open, url = meter.return_value.measure.call_args.args[:4]
        self.assertEqual(mode, probe.ACCURATE)
        self.assertIn("size=1000000000", url)
        posted = [body for method, path, body in client.calls if path == probe.RESULTS_PATH]
        self.assertEqual(posted[0]["manual"], True)
        self.assertEqual(posted[0]["transport"], "other")
        dialog.ok.assert_called_with("Test connection speed", "Your connection to Silo: 48 Mbps.")

    def test_a_learned_limit_can_be_cleared(self):
        client = Client(connection={"learned_limit": {"kbps": 12000, "expires_at": "2026-10-28T00:00:00Z"}})
        dialog, _meter = self.run_test(client, self.measured(), yes=True)
        self.assertIn("limited quality to 12 Mbps", dialog.yesno.call_args.args[1])
        self.assertIn(("DELETE", probe.LEARNED_LIMIT_PATH, None), client.calls)

    def test_no_speed_test_or_a_busy_server_is_explained(self):
        dialog, meter = self.run_test(Client(capabilities={"features": []}), self.measured())
        meter.return_value.measure.assert_not_called()
        dialog.ok.assert_called_with("Test connection speed", "This Silo server does not offer a speed test.")
        busy = probe.ProbeOutcome(probe.RATE_LIMITED, None, 45, 429, None)
        dialog, _meter = self.run_test(Client(), busy)
        dialog.ok.assert_called_with("Test connection speed", "Silo is busy. Try again in 45 seconds.")


if __name__ == "__main__":
    unittest.main()
