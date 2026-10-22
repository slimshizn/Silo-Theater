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


XBMC = types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4,
                             getInfoLabel=lambda label: {"System.BuildVersion": "21.1 Git:example",
                                                         "System.OSVersionInfo": "Android 12"}.get(label, ""),
                             getCondVisibility=lambda condition: condition == "System.Platform.Android")
KODI_MODULES = {
    "xbmc": XBMC,
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


LEFT, UP, DOWN, BACK = 1, 3, 4, 10
PLAYBACK, AUDIO, DEVICE, ACCOUNT = 0, 1, 2, 3


def full_profile(**extra):
    profile = {"id": 7, "name": "Example Profile", "auto_skip_intro": False, "auto_skip_credits": True,
               "auto_skip_recap": False, "quality_preference": "auto", "language": None,
               "subtitle_language": "en", "subtitle_mode": "auto", "show_forced_subtitles": True,
               "preferred_metadata_language": ""}
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
    application.store = {}
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
    # Kodi moves focus to the category's button and reports it through onFocus.
    window.setFocusId(ui_settings.RAIL_BUTTONS[index])
    window.onFocus(ui_settings.RAIL_BUTTONS[index])


def click(window, key):
    index = row_index(window, key)
    window.setFocusId(button(index))
    window.onClick(button(index))
    return index


class LayoutTests(unittest.TestCase):
    def test_opens_on_playback_with_profile_name_and_four_categories(self):
        window = open_window()
        self.assertEqual(window.properties["silo.subtitle"], "Settings for Example Profile")
        self.assertEqual([window.getControl(cid).setLabel.call_args[0][0] for cid in ui_settings.RAIL_BUTTONS],
                         ["Playback", "Audio and subtitles", "This device", "Account"])
        self.assertEqual(window.focus, ui_settings.RAIL)
        self.assertEqual(window.properties["silo.category"], "0")
        labels = [window.getControl(button(index)).setLabel.call_args[0][0] for index in range(8)]
        self.assertEqual(labels, ["Skip intros automatically", "Skip credits automatically",
                                  "Skip recaps automatically", "Quality preference", "Rewind when resuming",
                                  "Ask if you're still watching", "Show Up next",
                                  "Maximum bitrate for converted video"])
        self.assertEqual([value_label(window, index) for index in range(8)],
                         ["Off", "On", "Off", "Automatic", "7 seconds", "After 3 episodes",
                          "30 seconds before the end", "No limit"])
        select_category(window, ACCOUNT)
        for index in range(4, ui_settings.SLOTS):
            window.getControl(ui_settings.slot_ids(index)[0]).setVisible.assert_called_with(False)

    def test_moving_on_the_rail_swaps_the_rows(self):
        window = open_window()
        select_category(window, AUDIO)
        self.assertEqual(window.category, AUDIO)
        self.assertEqual([value_label(window, index) for index in range(5)],
                         ["Default", "English", "Automatic", "On", "Default"])
        self.assertEqual(window.getControl(button(4)).setLabel.call_args[0][0], "Metadata language")
        select_category(window, DEVICE)
        self.assertEqual(window.getControl(button(7)).setLabel.call_args[0][0], "More device settings…")
        # Back stops playback is on by default; frame-rate matching reads Kodi (off without JSON-RPC).
        self.assertEqual([value_label(window, index) for index in (2, 3, 5, 6)],
                         ["On", "Off", "Only when no profile is saved", "Off"])

    def test_up_and_down_move_within_rows_and_the_hint_opens_under_the_focused_row(self):
        window = open_window()
        window.setFocusId(button(0))
        window.onFocus(button(0))
        window.getControl(ui_settings.HINT).setVisible.assert_called_with(False)
        for _ in range(3):
            window.onAction(Action(DOWN))
        self.assertEqual(window.focus, button(3))
        hint = window.getControl(ui_settings.HINT)
        hint.setVisible.assert_called_with(True)
        self.assertIn("converts", hint.setLabel.call_args[0][0])
        hint.setPosition.assert_called_with(ui_settings.ROWS_LEFT + ui_settings.HINT_INDENT,
                                            ui_settings.ROWS_TOP + 3 * ui_settings.ROW_PITCH
                                            + ui_settings.ROW_HEIGHT + 4)
        window.onAction(Action(UP))
        self.assertEqual(window.focus, button(2))
        hint.setVisible.assert_called_with(False)

    def test_temporary_session_shows_only_device_settings_and_account_actions(self):
        window = open_window(make_application(profile={"id": 7}))
        self.assertEqual([window.getControl(cid).setLabel.call_args[0][0] for cid in ui_settings.RAIL_BUTTONS[:3]],
                         ["Playback", "This device", "Account"])
        window.getControl(ui_settings.RAIL_BUTTONS[3]).setVisible.assert_called_with(False)
        self.assertEqual([item["key"] for item in window.rows()],
                         ["resume_rewind", "still_watching", "next_up_lead", "max_bitrate"])
        select_category(window, 2)
        self.assertEqual([item["key"] for item in window.rows()], ["diagnostics", "server", "logout"])

    def test_every_category_shows_its_rows_when_focused_or_clicked(self):
        window = open_window()
        select_category(window, 1)
        self.assertEqual(window.properties["silo.category"], "1")
        self.assertEqual(window.getControl(button(0)).setLabel.call_args[0][0], "Audio language")
        window.onClick(ui_settings.RAIL_BUTTONS[2])
        self.assertEqual(window.getControl(button(0)).setLabel.call_args[0][0], "Play next episode automatically")
        self.assertEqual(window.focus, button(0))
        # A missed focus callback is caught on the next key press.
        window.setFocusId(ui_settings.RAIL_BUTTONS[1])
        window.onAction(Action(UP))
        self.assertEqual(window.category, 1)

    def test_left_from_a_row_returns_to_the_open_category(self):
        window = open_window()
        select_category(window, 1)
        window.setFocusId(button(2))
        window.onAction(Action(LEFT))
        self.assertEqual(window.focus, ui_settings.RAIL_BUTTONS[1])

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


class NewDeviceSettingTests(unittest.TestCase):
    def test_device_choices_write_the_integer_and_choice_values(self):
        cases = ((PLAYBACK, "resume_rewind", 0, "0", "Off"),
                 (PLAYBACK, "still_watching", 3, "4", "After 4 episodes"),
                 (PLAYBACK, "next_up_lead", 3, "60", "60 seconds before the end"),
                 (PLAYBACK, "max_bitrate", 2, "8000", "8 Mbps"),
                 (DEVICE, "profile_select", 3, "12h", "After 12 hours away"))
        for category, key, choice, stored, label in cases:
            with self.subTest(key=key):
                addon = FakeAddon()
                application = make_application(addon=addon)
                application.dialog.select.return_value = choice
                window = open_window(application)
                select_category(window, category)
                index = click(window, key)
                self.assertEqual(addon.values[key], stored)
                self.assertEqual(value_label(window, index), label)
                application.update_profile.assert_not_called()

    def test_device_choice_preselects_the_stored_value_and_ignores_cancel(self):
        addon = FakeAddon({"max_bitrate": "20000"})
        application = make_application(addon=addon)
        window = open_window(application)
        index = click(window, "max_bitrate")
        options = application.dialog.select.call_args[0][1]
        self.assertEqual(options, ["No limit", "4 Mbps", "8 Mbps", "12 Mbps", "20 Mbps", "40 Mbps"])
        self.assertEqual(application.dialog.select.call_args[1]["preselect"], 4)
        self.assertEqual(addon.values, {"max_bitrate": "20000"})
        self.assertEqual(value_label(window, index), "20 Mbps")

    def test_bitrate_choices_match_the_settings_module(self):
        from resources.lib.silo.settings import INTEGER_CHOICES

        for item in ui_settings.PLAYBACK[1]:
            if item["kind"] == ui_settings.DEVICE_CHOICE:
                self.assertEqual([code for code, _label in item["choices"]], list(INTEGER_CHOICES[item["key"]]))

    def test_hide_spoilers_toggles_the_device_setting(self):
        addon = FakeAddon()
        window = open_window(make_application(addon=addon))
        select_category(window, DEVICE)
        index = click(window, "hide_spoilers")
        self.assertEqual(addon.values["hide_spoilers"], "true")
        self.assertEqual(value_label(window, index), "On")

    def test_metadata_language_saves_on_the_profile_with_default_as_none(self):
        application = make_application(profile=full_profile(preferred_metadata_language="de"))
        application.dialog.select.return_value = 2
        window = open_window(application)
        select_category(window, AUDIO)
        self.assertEqual(value_label(window, 4), "German")
        index = click(window, "preferred_metadata_language")
        application.update_profile.assert_called_once_with({"preferred_metadata_language": "es"})
        self.assertEqual(value_label(window, index), "Spanish")
        application.dialog.select.return_value = 0
        click(window, "preferred_metadata_language")
        application.update_profile.assert_called_with({"preferred_metadata_language": None})


class AccountTests(unittest.TestCase):
    def test_manage_profiles_opens_the_screen_and_refreshes_the_subtitle(self):
        application = make_application()

        def manage(name):
            self.assertEqual(name, "manage_profiles")
            application.profile_name = "Example Renamed"

        application.open_screen.side_effect = manage
        window = open_window(application)
        select_category(window, ACCOUNT)
        click(window, "manage_profiles")
        self.assertFalse(window.closed)
        self.assertEqual(window.properties["silo.subtitle"], "Settings for Example Renamed")

    def test_switch_server_and_sign_out_confirm_then_return_their_result(self):
        for key, result in (("server", "server"), ("logout", "logout")):
            with self.subTest(key=key):
                application = make_application()
                application.dialog.yesno.return_value = False
                window = open_window(application)
                select_category(window, ACCOUNT)
                click(window, key)
                self.assertFalse(window.closed)
                application.dialog.yesno.return_value = True
                click(window, key)
                self.assertTrue(window.closed)
                self.assertEqual(window.result, result)

    def test_open_settings_passes_account_results_through(self):
        application = make_application()
        application.dialog.yesno.return_value = True

        def sign_out(window):
            window.onInit()
            select_category(window, ACCOUNT)
            click(window, "logout")

        with patch.object(ui_settings.SettingsWindow, "doModal", sign_out):
            self.assertEqual(ui_settings.open_settings(application), "logout")


class DiagnosticsTests(unittest.TestCase):
    CAPABILITIES = {"state": "available", "server_instance_id": "example-instance", "consent_notice_version": 2,
                    "max_bundle_bytes": 1048576}

    def setUp(self):
        # ui_settings imports xbmc when it collects versions, after the module stubs are gone.
        patcher = patch.dict(sys.modules, {"xbmc": XBMC})
        patcher.start()
        self.addCleanup(patcher.stop)

    def make(self, addon_values=None):
        addon = FakeAddon(dict({"server_url": "https://silo.example.invalid", "dv_mode": "dv"},
                               **(addon_values or {})))
        addon.getAddonInfo = lambda key: "0.5.0" if key == "version" else str(ROOT)
        application = make_application(addon=addon)
        application.store = {"access_token": "secret-access", "refresh_token": "secret-refresh"}
        sent = {}

        def request(method, path, body=None, **kwargs):
            self.assertEqual((method, path), ("GET", "/diagnostics/capabilities"))
            return dict(self.CAPABILITIES)

        def upload(method, url, headers, data):
            sent.update(method=method, url=url, headers=headers, data=data)
            return 201, {}, {"report_id": "r-1", "short_id": "AB12CD"}

        client = MagicMock()
        client.request.side_effect = request
        client.headers.return_value = {"Authorization": "Bearer secret-access"}
        client._url.side_effect = lambda path: "https://silo.example.invalid/api/v2" + path
        client.transport.upload.side_effect = upload
        application.client = client
        application.dialog.yesno.return_value = True
        return application, sent

    def test_report_is_confirmed_sent_and_its_id_shown(self):
        application, sent = self.make()
        self.assertEqual(ui_settings.send_diagnostics(application), "AB12CD")
        self.assertIn("never contains passwords", application.dialog.yesno.call_args[0][1])
        self.assertIn("AB12CD", application.dialog.ok.call_args[0][1])
        self.assertEqual(sent["url"], "https://silo.example.invalid/api/v2/diagnostics/reports")
        self.assertIn("multipart/form-data; boundary=", sent["headers"]["Content-Type"])
        self.assertEqual(sent["headers"]["X-Profile-Id"], "7")

    def test_report_holds_versions_and_settings_but_no_secrets(self):
        import gzip
        import io
        import json
        import tarfile

        application, sent = self.make()
        ui_settings.send_diagnostics(application)
        body = sent["data"]
        for secret in (b"secret-access", b"secret-refresh", b"silo.example.invalid", str(ROOT).encode()):
            self.assertNotIn(secret, body)
        boundary = sent["headers"]["Content-Type"].split("boundary=")[1].encode()
        parts = [part for part in body.split(b"--" + boundary) if part.strip(b"-\r\n")]
        manifest = json.loads(parts[0].split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
        self.assertEqual(manifest["report"]["type"], "manual")
        self.assertEqual(manifest["report"]["app_version"], "Founders Theater 0.5.0")
        self.assertEqual(manifest["report"]["app_build"], "21.1")
        self.assertEqual(manifest["report"]["os_version"], "Android 12")
        self.assertEqual(manifest["destination"]["server_instance_id"], "example-instance")
        self.assertEqual(manifest["consent"], {"mode": "manual", "notice_version": 2})
        bundle = parts[1].split(b"\r\n\r\n", 1)[1][:-2]
        self.assertEqual(manifest["archive"]["bytes"], len(bundle))
        with tarfile.open(fileobj=io.BytesIO(gzip.decompress(bundle))) as archive:
            names = archive.getnames()
            embedded = json.loads(archive.extractfile("manifest.json").read())
            logs = archive.extractfile("logs.jsonl").read().decode()
        self.assertEqual(names, manifest["archive"]["entries"])
        self.assertEqual(embedded, {key: value for key, value in manifest.items() if key != "archive"})
        self.assertIn('\\"dv_mode\\": \\"dv\\"', logs)
        self.assertNotIn("server_url", logs)

    def test_declined_confirmation_sends_nothing(self):
        application, sent = self.make()
        application.dialog.yesno.return_value = False
        self.assertIsNone(ui_settings.send_diagnostics(application))
        self.assertEqual(sent, {})

    def test_unavailable_server_or_platform_explains_and_sends_nothing(self):
        application, sent = self.make()
        self.CAPABILITIES = dict(DiagnosticsTests.CAPABILITIES, state="disabled")
        self.assertIsNone(ui_settings.send_diagnostics(application))
        self.assertIn("not accepting", application.dialog.ok.call_args[0][1])
        del self.CAPABILITIES
        with patch.object(XBMC, "getCondVisibility", lambda condition: False):
            self.assertIsNone(ui_settings.send_diagnostics(application))
        self.assertIn("cannot send one yet", application.dialog.ok.call_args[0][1])
        application.dialog.yesno.assert_not_called()
        self.assertEqual(sent, {})

    def test_kid_profile_is_not_attributed(self):
        application, sent = self.make()
        application.profile = full_profile(is_child=True)
        ui_settings.send_diagnostics(application)
        self.assertNotIn("X-Profile-Id", sent["headers"])
        self.assertNotIn(b'"profile_id"', sent["data"])

    def test_settings_row_runs_diagnostics_through_guard(self):
        application, sent = self.make()
        window = open_window(application)
        select_category(window, ACCOUNT)
        click(window, "diagnostics")
        self.assertEqual(sent["method"], "POST")
        self.assertFalse(window.closed)


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
        wanted = [*ui_settings.RAIL_BUTTONS, *ui_settings.RAIL_FILLS, ui_settings.HINT]
        wanted += [control for index in range(ui_settings.SLOTS) for control in ui_settings.slot_ids(index)]
        for control in wanted:
            self.assertIn(str(control), ids)

    def test_every_category_fits_the_slots(self):
        for _title, rows in (ui_settings.PLAYBACK, ui_settings.AUDIO_SUBTITLES, ui_settings.THIS_DEVICE,
                             ui_settings.ACCOUNT):
            self.assertLessEqual(len(rows), ui_settings.SLOTS)
        self.assertEqual(len(ui_settings.RAIL_BUTTONS), 4)

    def test_lowest_row_and_its_hint_stay_on_screen(self):
        bottom = (ui_settings.ROWS_TOP + (ui_settings.SLOTS - 1) * ui_settings.ROW_PITCH
                  + ui_settings.ROW_HEIGHT + ui_settings.HINT_SPACE)
        self.assertLessEqual(bottom, 1080 - 40)


if __name__ == "__main__":
    unittest.main()
