"""Exercise user journeys with Kodi adapters stubbed; no server or Kodi needed."""

import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


class FakeWindow:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        instance.properties = {}
        instance.controls = {}
        instance.closed = False
        return instance

    def getControl(self, control_id):
        if control_id not in self.controls:
            self.controls[control_id] = MagicMock()
            self.controls[control_id].getSelectedPosition.return_value = 0
        return self.controls[control_id]

    def setProperty(self, key, value):
        self.properties[key] = value

    def setFocusId(self, control_id):
        self.focus = control_id

    def close(self):
        self.closed = True


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcaddon": types.SimpleNamespace(Addon=MagicMock),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=MagicMock,
                                     DialogProgress=MagicMock, INPUT_ALPHANUM=0, ALPHANUM_HIDE_INPUT=1),
    "xbmcvfs": types.SimpleNamespace(translatePath=lambda path: path),
}
with patch.dict(sys.modules, KODI_MODULES):
    application_module = importlib.import_module("resources.lib.silo.application")
    ui = importlib.import_module("resources.lib.silo.ui")

Application = application_module.Application
ApiError = application_module.ApiError


class Store(dict):
    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = Application.__new__(Application)
        self.app.addon = MagicMock()
        self.app.addon.getSetting.return_value = "code"
        self.app.dialog = MagicMock()
        self.app.monitor = MagicMock()
        self.app.monitor.abortRequested.return_value = False
        self.app.store = Store()
        self.app.client = MagicMock()
        self.app.browse = None

    def test_pin_refusal_attempts_once_and_never_selects_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.side_effect = ApiError(403, "Incorrect PIN")
        self.assertFalse(self.app.choose_profile())
        self.app.client.verify_pin.assert_called_once_with("locked", "1234")
        self.app.dialog.input.assert_called_once()
        self.app.client.select_profile.assert_not_called()

    def test_pin_success_attaches_returned_proof_to_selected_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.return_value = {"valid": True, "profile_token": "proof"}
        self.assertTrue(self.app.choose_profile())
        self.app.client.select_profile.assert_called_once_with("locked", "proof")
        self.assertEqual(self.app.profile_name, "Adult")

    def test_invalid_pin_success_response_cannot_select_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.return_value = {"valid": False}
        self.assertFalse(self.app.choose_profile())
        self.app.client.select_profile.assert_not_called()

    def test_temporary_session_reuses_bound_profile_without_pin_or_profile_picker(self):
        self.app.store.update(temporary_session=True, bound_profile_id="bound", profile_token="proof")
        self.assertTrue(self.app.choose_profile())
        self.app.client.select_profile.assert_called_once_with("bound", "proof")
        self.app.client.profiles.assert_not_called()
        self.app.client.verify_pin.assert_not_called()
        self.app.dialog.input.assert_not_called()
        self.app.dialog.select.assert_not_called()

    def test_saved_code_method_is_used_after_forced_sign_in(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(return_value=True)
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.device_sign_in.assert_called_once()
        self.app.dialog.select.assert_not_called()
        self.app.dialog.input.assert_not_called()

    def test_back_from_code_sign_in_does_not_open_password_prompt(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(side_effect=application_module.DeviceAuthCancelled())
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertFalse(self.app.sign_in())
        self.app.dialog.input.assert_not_called()

    def test_password_only_retry_does_not_offer_unsupported_code(self):
        self.app.password_sign_in = MagicMock(side_effect=[ApiError(401, "Incorrect password"), True])
        self.app.dialog.select.return_value = 0
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["password"]
            self.assertTrue(self.app.sign_in())
        self.assertEqual(self.app.password_sign_in.call_count, 2)
        choices = self.app.dialog.select.call_args.args[1]
        self.assertFalse(any("code" in value.lower() for value in choices))

    def test_code_failure_can_fall_back_to_password(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(side_effect=ApiError(404, "Unavailable"))
        self.app.password_sign_in = MagicMock(return_value=True)
        self.app.dialog.select.return_value = 1
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.password_sign_in.assert_called_once()

    def test_library_navigation_preserves_filters_and_uses_library_scope(self):
        self.app.browse = {"params": {"library_id": "old", "type": "movie", "sort": "-added_at"}}
        self.app.client.items.return_value = {"items": [], "page": {"has_more": False}, "total": 0}
        window = MagicMock()
        window.sections = [{"id": "new", "title": "Cinema", "library": {"id": "new"}}]
        self.app.show_section(window, window.sections[0])
        self.app.client.items.assert_called_once_with(library_id="new", type="movie", sort="-added_at", limit=50)
        self.assertNotIn("source", self.app.browse["params"])

    def test_catalog_cursor_is_carried_to_next_page(self):
        self.app.browse = {"heading": "Movies", "sections": [], "params": {"library_id": "movies", "type": "video"}}
        self.app.client.items.side_effect = [
            {"items": [{"content_id": "m:1"}], "page": {"has_more": True, "next_cursor": "opaque"}},
            {"items": [{"content_id": "m:2"}], "page": {"has_more": False}},
        ]
        window = MagicMock()
        self.app._browse_page(window)
        self.app.next_page(window)
        self.assertEqual(self.app.client.items.call_args.kwargs["cursor"], "opaque")
        self.assertIsNone(self.app.browse["next_cursor"])
        self.assertEqual(window.display.call_args.args[2], [{"content_id": "m:2"}])

    def test_series_detail_loads_seasons_without_starting_playback(self):
        item = {"content_id": "series:1", "type": "series", "title": "Show"}
        season = {"content_id": "season:1", "type": "season", "title": "Season 1"}
        self.app.client.item.return_value = item
        self.app.client.children.return_value = {"items": [season]}
        with patch.object(application_module, "DetailsWindow") as window:
            self.app.open_item(item)
        self.app.client.children.assert_called_once_with(item)
        self.assertEqual(window.return_value.children, [season])
        window.return_value.doModal.assert_called_once()

    def test_resume_uses_server_position_and_start_explicitly_passes_zero(self):
        item = {"content_id": "movie:1", "type": "movie"}
        with patch.object(application_module, "PlaybackController") as player:
            self.app.play(item, resume=True)
            player.return_value.play.assert_called_with(item, start_position=None)
            self.app.play(item, resume=False)
            player.return_value.play.assert_called_with(item, start_position=0)

    def test_session_storage_failure_is_shown_inside_startup_error_handler(self):
        self.app.store = None
        with patch.object(application_module, "SessionStore", side_effect=application_module.StorageError("Cannot read session")):
            self.app.run()
        self.app.dialog.ok.assert_called_once_with("Silo", "Cannot read session")


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.app = MagicMock()
        self.app.guard.side_effect = lambda callback: callback()
        self.app.artwork.return_value = {}
        self.app.store = Store()

    def test_item_selection_opens_corresponding_detail(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        first, second = {"content_id": "movie:1"}, {"content_id": "movie:2"}
        window.items = [first, second]
        window.getControl(500).getSelectedPosition.return_value = 1
        window.onClick(500)
        self.app.open_item.assert_called_once_with(second)

    def test_empty_media_selection_is_inert(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        window.onClick(500)
        self.app.open_item.assert_not_called()

    def test_back_closes_window(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        action = MagicMock()
        action.getId.return_value = 92
        window.onAction(action)
        self.assertTrue(window.closed)

    def test_profile_button_returns_action_for_application_loop(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        window.onClick(103)
        self.assertTrue(window.closed)
        self.assertEqual(window.result, "profile")

    def test_detail_refreshes_resume_after_playback(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application = self.app
        window.item = {"content_id": "movie:1", "type": "movie", "title": "Example"}
        self.app.is_playable.return_value = True
        self.app.resume_seconds.return_value = 75
        updated = dict(window.item, user_data={"position_seconds": 75})
        self.app.client.item.return_value = updated
        window.onClick(100)
        self.app.play.assert_called_once_with({"content_id": "movie:1", "type": "movie", "title": "Example"}, resume=False)
        self.assertEqual(window.properties["silo.resume_label"], "Resume from 1:15")
        self.assertIs(window.item, updated)

    def test_skin_control_ids_match_controllers(self):
        root = Path(__file__).resolve().parents[1] / "resources/skins/Default/1080i"
        for name, required in (("SiloHome.xml", {100, 101, 102, 103, 104, 105, 106, 300, 500}),
                               ("SiloDetails.xml", {100, 101, 102, 500})):
            tree = ET.parse(root / name)
            ids = [int(control.attrib["id"]) for control in tree.iter("control") if "id" in control.attrib]
            self.assertEqual(len(ids), len(set(ids)), name)
            self.assertTrue(required.issubset(set(ids)), name)
            for direction in ("onleft", "onright", "onup", "ondown"):
                for node in tree.iter(direction):
                    self.assertIn(int(node.text), ids, name)


if __name__ == "__main__":
    unittest.main()
