"""Frame-rate matching reads and writes Kodi's own settings over JSON-RPC."""

import json
import unittest
from unittest.mock import MagicMock, patch

from resources.lib.silo import kodi_settings

try:
    from .test_ui import Application, Store
except ImportError:
    from test_ui import Application, Store


class FakeRpc:
    def __init__(self, values, accept=True):
        self.values, self.accept, self.calls = dict(values), accept, []

    def __call__(self, raw):
        request = json.loads(raw)
        self.calls.append(request)
        params = request["params"]
        if request["method"] == "Settings.GetSettingValue":
            if params["setting"] not in self.values:
                return json.dumps({"error": {"code": -32602}})
            return json.dumps({"result": {"value": self.values[params["setting"]]}})
        if not self.accept:
            return json.dumps({"error": {"code": -32602}})
        self.values[params["setting"]] = params["value"]
        return json.dumps({"result": True})


def using(rpc):
    """Route kodi_settings through ``rpc`` instead of xbmc.executeJSONRPC."""
    get, set_value = kodi_settings.get, kodi_settings.set_value
    return patch.multiple(kodi_settings, get=lambda setting, execute=None: get(setting, rpc),
                          set_value=lambda setting, value, execute=None: set_value(setting, value, rpc))


class KodiSettingsTests(unittest.TestCase):
    def test_get_and_set_use_settings_methods(self):
        rpc = FakeRpc({kodi_settings.FRAME_RATE: 0})
        self.assertEqual(kodi_settings.get(kodi_settings.FRAME_RATE, rpc), 0)
        self.assertTrue(kodi_settings.set_value(kodi_settings.FRAME_RATE, 2, rpc))
        self.assertEqual(rpc.values[kodi_settings.FRAME_RATE], 2)
        self.assertEqual(rpc.calls[-1]["method"], "Settings.SetSettingValue")

    def test_missing_settings_errors_and_bad_answers_are_none(self):
        self.assertIsNone(kodi_settings.get("videoplayer.unknown", FakeRpc({})))
        self.assertFalse(kodi_settings.set_value(kodi_settings.FRAME_RATE, 2, FakeRpc({}, accept=False)))
        self.assertIsNone(kodi_settings.get(kodi_settings.FRAME_RATE, lambda raw: "not json"))
        self.assertIsNone(kodi_settings.get(kodi_settings.FRAME_RATE,
                                            lambda raw: json.dumps({"result": {"value": True}})))

    def test_labels(self):
        self.assertEqual(kodi_settings.label(kodi_settings.FRAME_RATE_CHOICES, 2), "On start and stop")
        self.assertEqual(kodi_settings.label(kodi_settings.FRAME_RATE_CHOICES, None), "Off")


class FrameRateSetupTests(unittest.TestCase):
    def setUp(self):
        self.app = Application.__new__(Application)
        self.settings = {}
        self.app.addon = MagicMock()
        self.app.addon.getSetting.side_effect = lambda key: self.settings.get(key, "")
        self.app.addon.setSetting.side_effect = self.settings.__setitem__
        self.app.dialog = MagicMock()
        self.app.store = Store()

    def test_asks_once_when_off_and_turns_matching_on_with_a_settle_pause(self):
        rpc = FakeRpc({kodi_settings.FRAME_RATE: 0, kodi_settings.FRAME_RATE_PAUSE: 0})
        self.app.dialog.yesno.return_value = True
        with using(rpc):
            self.app.ensure_frame_rate_setup()
            self.assertEqual(rpc.values, {kodi_settings.FRAME_RATE: 2, kodi_settings.FRAME_RATE_PAUSE: 20})
            self.assertEqual(self.settings["framerate_asked"], "true")
            self.app.dialog.yesno.reset_mock()
            self.app.ensure_frame_rate_setup()
        self.app.dialog.yesno.assert_not_called()

    def test_already_on_or_unavailable_does_not_ask(self):
        with using(FakeRpc({kodi_settings.FRAME_RATE: 1})):
            self.app.ensure_frame_rate_setup()
        self.app.dialog.yesno.assert_not_called()
        self.assertEqual(self.settings["framerate_asked"], "true")
        self.settings.clear()
        with using(FakeRpc({})):
            self.app.ensure_frame_rate_setup()
        self.app.dialog.yesno.assert_not_called()
        self.assertNotIn("framerate_asked", self.settings)

    def test_declining_leaves_kodi_unchanged(self):
        rpc = FakeRpc({kodi_settings.FRAME_RATE: 0, kodi_settings.FRAME_RATE_PAUSE: 0})
        self.app.dialog.yesno.return_value = False
        with using(rpc):
            self.app.ensure_frame_rate_setup()
        self.assertEqual(rpc.values[kodi_settings.FRAME_RATE], 0)


if __name__ == "__main__":
    unittest.main()
