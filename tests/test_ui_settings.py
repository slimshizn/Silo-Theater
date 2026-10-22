"""Settings window behaviour with Kodi stubbed, plus checks on SiloSettings.xml."""

import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default"


class FakeWindow:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        instance.properties = {}
        instance.controls = {}
        instance.focus = 0
        instance.closed = False
        return instance

    def getControl(self, control_id):
        if control_id not in self.controls:
            self.controls[control_id] = MagicMock(name="control{}".format(control_id))
            self.controls[control_id].getSelectedPosition.return_value = 0
        return self.controls[control_id]

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setFocusId(self, control_id):
        self.focus = control_id

    def getFocusId(self):
        return self.focus

    def doModal(self):
        self.onInit()

    def close(self):
        self.closed = True


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=MagicMock),
}
with patch.dict(sys.modules, KODI_MODULES):
    sys.modules.pop("resources.lib.silo.ui_settings", None)
    ui_settings = importlib.import_module("resources.lib.silo.ui_settings")


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


UP, DOWN, BACK = 3, 4, 10
PLAYBACK, AUDIO, DEVICE = 0, 1, 2


def full_profile(**extra):
    profile = {"id": 7, "name": "Example Profile", "auto_skip_intro": False, "auto_skip_credits": True,
               "auto_skip_recap": False, "quality_preference": "auto", "language": None,
               "subtitle_language": "en", "subtitle_mode": "auto", "show_forced_subtitles": True}
    profile.update(extra)
    return profile


class FakeAddon:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def getSetting(self, key):
        return self.values.get(key, "")

    def setSetting(self, key, value):
        self.values[key] = value

    def getAddonInfo(self, key):
        return str(ROOT) if key == "path" else ""


def make_application(profile=None, fail=False, addon=None):
    application = MagicMock()
    application.profile = full_profile() if profile is None else profile
    application.profile_name = "Example Profile"
    application.addon = addon or FakeAddon()
    application.dialog.select.return_value = -1

    def guard(callback):
        try:
            return callback()
        except RuntimeError:
            return None

    def update_profile(changes):
        if fail:
            raise RuntimeError("save failed")
        application.profile = dict(application.profile, **changes)
        return application.profile

    application.guard.side_effect = guard
    application.update_profile.side_effect = update_profile
    return application


def open_window(application=None):
    window = ui_settings.SettingsWindow("SiloSettings.xml", str(ROOT), "Default", "1080i")
    window.application = application or make_application()
    window.onInit()
    return window


def button(index):
    return ui_settings.slot_ids(index)[1]


def value_label(window, index):
    return window.getControl(ui_settings.slot_ids(index)[2]).setLabel.call_args[0][0]


def row_index(window, key):
    return next(index for index, item in enumerate(window.rows()) if item["key"] == key)


def select_category(window, index):
    window.setFocusId(ui_settings.RAIL)
    window.getControl(ui_settings.RAIL).getSelectedPosition.return_value = index
    window.onAction(Action(DOWN))


def click(window, key):
    index = row_index(window, key)
    window.setFocusId(button(index))
    window.onClick(button(index))
    return index


class LayoutTests(unittest.TestCase):
    def test_opens_on_playback_with_profile_name_and_three_categories(self):
        window = open_window()
        self.assertEqual(window.properties["silo.subtitle"], "Settings for Example Profile")
        items = window.getControl(ui_settings.RAIL).addItems.call_args[0][0]
        self.assertEqual(len(items), 3)
        self.assertEqual(window.focus, ui_settings.RAIL)
        labels = [window.getControl(button(index)).setLabel.call_args[0][0] for index in range(4)]
        self.assertEqual(labels, ["Skip intros automatically", "Skip credits automatically",
                                  "Skip recaps automatically", "Quality preference"])
        self.assertEqual([value_label(window, index) for index in range(4)], ["Off", "On", "Off", "Automatic"])
        for index in range(4, ui_settings.SLOTS):
            window.getControl(ui_settings.slot_ids(index)[0]).setVisible.assert_called_with(False)

    def test_moving_on_the_rail_swaps_the_rows(self):
        window = open_window()
        select_category(window, AUDIO)
        self.assertEqual(window.category, AUDIO)
        self.assertEqual([value_label(window, index) for index in range(4)],
                         ["Default", "English", "Automatic", "On"])
        select_category(window, DEVICE)
        self.assertEqual(window.getControl(button(3)).setLabel.call_args[0][0], "More device settings…")

    def test_up_and_down_move_within_rows_and_the_hint_opens_under_the_focused_row(self):
        window = open_window()
        window.setFocusId(button(0))
        window.onFocus(button(0))
        window.getControl(ui_settings.HINT).setVisible.assert_called_with(False)
        for _ in range(5):
            window.onAction(Action(DOWN))
        self.assertEqual(window.focus, button(3))
        hint = window.getControl(ui_settings.HINT)
        hint.setVisible.assert_called_with(True)
        self.assertIn("plays the original file", hint.setLabel.call_args[0][0])
        hint.setPosition.assert_called_with(ui_settings.ROWS_LEFT + ui_settings.HINT_INDENT,
                                            ui_settings.ROWS_TOP + 3 * ui_settings.ROW_PITCH
                                            + ui_settings.ROW_HEIGHT + 4)
        window.onAction(Action(UP))
        self.assertEqual(window.focus, button(2))
        hint.setVisible.assert_called_with(False)

    def test_temporary_session_shows_only_this_device(self):
        window = open_window(make_application(profile={"id": 7}))
        items = window.getControl(ui_settings.RAIL).addItems.call_args[0][0]
        self.assertEqual(len(items), 1)
        self.assertEqual([item["key"] for item in window.rows()],
                         ["auto_next", "skip_prompts", "player_menu_key", None])

    def test_back_closes_without_result(self):
        window = open_window()
        window.onAction(Action(BACK))
        self.assertTrue(window.closed)
        self.assertIsNone(window.result)

    def test_open_settings_returns_window_result(self):
        application = make_application()
        with patch.object(ui_settings.SettingsWindow, "doModal", lambda self: (self.onInit(), self.onAction(
                Action(BACK)))):
            self.assertIsNone(ui_settings.open_settings(application))

        def choose_more(window):
            window.onInit()
            select_category(window, DEVICE)
            click(window, None)

        with patch.object(ui_settings.SettingsWindow, "doModal", choose_more):
            self.assertEqual(ui_settings.open_settings(application), "device")


class ProfileSaveTests(unittest.TestCase):
    def test_each_toggle_saves_the_flipped_value(self):
        for category, key, expected in ((PLAYBACK, "auto_skip_intro", True), (PLAYBACK, "auto_skip_credits", False),
                                        (PLAYBACK, "auto_skip_recap", True),
                                        (AUDIO, "show_forced_subtitles", False)):
            with self.subTest(key=key):
                application = make_application()
                window = open_window(application)
                select_category(window, category)
                index = click(window, key)
                application.update_profile.assert_called_once_with({key: expected})
                self.assertEqual(value_label(window, index), "On" if expected else "Off")

    def test_pickers_preselect_the_current_value_and_save_the_choice(self):
        cases = ((PLAYBACK, "quality_preference", 0, 2, "2160p", "4K (2160p)"),
                 (AUDIO, "language", 0, 4, "de", "German"),
                 (AUDIO, "subtitle_language", 1, 3, "fr", "French"),
                 (AUDIO, "subtitle_mode", 0, 2, "forced_only", "Forced only"))
        for category, key, current, choice, value, label in cases:
            with self.subTest(key=key):
                application = make_application()
                application.dialog.select.return_value = choice
                window = open_window(application)
                select_category(window, category)
                index = click(window, key)
                self.assertEqual(application.dialog.select.call_args[1]["preselect"], current)
                application.update_profile.assert_called_once_with({key: value})
                self.assertEqual(value_label(window, index), label)

    def test_default_language_is_sent_as_none(self):
        application = make_application(profile=full_profile(subtitle_language="ja"))
        application.dialog.select.return_value = 0
        window = open_window(application)
        select_category(window, AUDIO)
        index = click(window, "subtitle_language")
        options = application.dialog.select.call_args[0][1]
        self.assertEqual(options[0], "Default")
        self.assertIn("Romanian", options)
        application.update_profile.assert_called_once_with({"subtitle_language": None})
        self.assertEqual(value_label(window, index), "Default")

    def test_cancelled_or_unchanged_picker_does_not_save(self):
        application = make_application()
        window = open_window(application)
        click(window, "quality_preference")
        application.dialog.select.return_value = 0
        click(window, "quality_preference")
        application.update_profile.assert_not_called()

    def test_failed_save_keeps_the_old_value(self):
        application = make_application(fail=True)
        application.dialog.select.return_value = 1
        window = open_window(application)
        index = click(window, "auto_skip_intro")
        self.assertEqual(value_label(window, index), "Off")
        index = click(window, "quality_preference")
        self.assertEqual(value_label(window, index), "Automatic")
        self.assertEqual(application.profile["quality_preference"], "auto")
        self.assertEqual(application.guard.call_count, 2)


class DeviceSettingTests(unittest.TestCase):
    def test_device_toggles_write_settings(self):
        addon = FakeAddon({"auto_next": "true", "skip_prompts": "false"})
        application = make_application(addon=addon)
        window = open_window(application)
        select_category(window, DEVICE)
        self.assertEqual([value_label(window, index) for index in range(3)], ["On", "Off", "On"])
        index = click(window, "auto_next")
        self.assertEqual(addon.values["auto_next"], "false")
        self.assertEqual(value_label(window, index), "Off")
        click(window, "skip_prompts")
        self.assertEqual(addon.values["skip_prompts"], "true")
        application.sync_keymap.assert_not_called()
        application.update_profile.assert_not_called()

    def test_player_menu_key_syncs_the_keymap(self):
        addon = FakeAddon({"player_menu_key": "true"})
        application = make_application(addon=addon)
        window = open_window(application)
        select_category(window, DEVICE)
        index = click(window, "player_menu_key")
        self.assertEqual(addon.values["player_menu_key"], "false")
        self.assertEqual(value_label(window, index), "Off")
        application.sync_keymap.assert_called_once_with()

    def test_player_menu_key_without_sync_keymap(self):
        addon = FakeAddon()
        application = make_application(addon=addon)
        del application.sync_keymap
        window = open_window(application)
        select_category(window, DEVICE)
        click(window, "player_menu_key")
        self.assertEqual(addon.values["player_menu_key"], "false")

    def test_more_device_settings_returns_device(self):
        window = open_window()
        select_category(window, DEVICE)
        click(window, None)
        self.assertTrue(window.closed)
        self.assertEqual(window.result, "device")


class SkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ET.parse(SKIN / "1080i/SiloSettings.xml")
        cls.ids = [control.get("id") for control in cls.tree.iter("control") if control.get("id")]

    def test_ids_are_unique(self):
        self.assertEqual(len(self.ids), len(set(self.ids)))

    def test_navigation_targets_exist(self):
        ids = set(self.ids)
        for tag in ("onup", "ondown", "onleft", "onright"):
            for element in self.tree.iter(tag):
                self.assertIn(element.text, ids, "{} -> {}".format(tag, element.text))

    def test_textures_exist(self):
        media = SKIN / "media"
        for element in self.tree.iter():
            if not element.tag.startswith("texture"):
                continue
            for name in (element.text, element.get("diffuse")):
                if name and "$INFO" not in name:
                    self.assertTrue((media / name).is_file(), name)

    def test_python_control_ids_exist(self):
        ids = set(self.ids)
        wanted = [ui_settings.RAIL, ui_settings.HINT]
        wanted += [control for index in range(ui_settings.SLOTS) for control in ui_settings.slot_ids(index)]
        for control in wanted:
            self.assertIn(str(control), ids)

    def test_every_category_fits_the_slots(self):
        for _title, rows in (ui_settings.PLAYBACK, ui_settings.AUDIO_SUBTITLES, ui_settings.THIS_DEVICE):
            self.assertLessEqual(len(rows), ui_settings.SLOTS)


if __name__ == "__main__":
    unittest.main()
