"""Settings validation and the shared preferences/XML boundary."""

from pathlib import Path
import sys
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resources/lib"))

from silo.settings import CHOICES, DEFAULTS, INTEGER_CHOICES, read_options


class Addon:
    def __init__(self, **values):
        self.values = values

    def getSetting(self, key):
        return self.values.get(key, "")


class SettingsTests(unittest.TestCase):
    def test_missing_settings_preserve_safe_existing_behavior(self):
        options = read_options(Addon())
        self.assertEqual(options, DEFAULTS)
        self.assertEqual(options["sign_in_method"], "password")
        self.assertEqual(options["dv_mode"], "auto")
        self.assertEqual(options["max_resolution"], "auto")
        self.assertEqual(options["resume_behavior"], "resume")
        self.assertEqual(options["subtitle_mode"], "server")
        self.assertEqual(options["progress_interval"], 10)

    def test_invalid_or_corrupt_values_never_become_player_or_query_options(self):
        options = read_options(Addon(
            sign_in_method="unknown", resume_behavior="always", file_selection="guess",
            dv_mode="CoreELEC", max_resolution="unlimited", subtitle_mode="burn_in",
            progress_interval="999", page_size="-1", show_plot="sometimes",
        ))
        self.assertEqual(options, DEFAULTS)

    def test_preference_values_are_typed_and_trimmed(self):
        options = read_options(Addon(
            server_url="  https://silo.invalid/prefix  ", sign_in_method=" PASSWORD ",
            resume_behavior="ASK", file_selection="last_played", dv_mode="P7_FEL",
            max_resolution="2160p", subtitle_mode="OFF", progress_interval="5",
            page_size="100", show_plot="false",
        ))
        self.assertEqual(options["server_url"], "https://silo.invalid/prefix")
        self.assertEqual(options["sign_in_method"], "password")
        self.assertEqual(options["resume_behavior"], "ask")
        self.assertEqual(options["file_selection"], "last_played")
        self.assertEqual(options["dv_mode"], "p7_fel")
        self.assertEqual(options["max_resolution"], "2160p")
        self.assertEqual(options["subtitle_mode"], "off")
        self.assertEqual(options["progress_interval"], 5)
        self.assertEqual(options["page_size"], 100)
        self.assertIs(options["show_plot"], False)

    def test_unavailable_settings_api_and_non_string_values_fall_back_safely(self):
        self.assertEqual(read_options(object()), DEFAULTS)
        self.assertEqual(read_options(Addon(page_size=None, progress_interval="5.0")), DEFAULTS)
        self.assertIs(read_options(Addon(show_plot=False))["show_plot"], False)

    def test_settings_changes_are_read_again_without_shared_mutable_state(self):
        addon = Addon(page_size="25")
        first = read_options(addon)
        first["page_size"] = 100
        addon.values["page_size"] = "50"
        self.assertEqual(read_options(addon)["page_size"], 50)
        self.assertEqual(DEFAULTS["page_size"], 50)

    def test_xml_defaults_and_selectable_values_match_the_shared_reader(self):
        settings = {item.get("id"): item for item in ET.parse(ROOT / "resources/settings.xml").findall(".//setting")}
        for name, default in DEFAULTS.items():
            with self.subTest(name=name):
                setting = settings[name]
                text = setting.findtext("default") or ""
                actual = read_options(Addon(**{name: text}))[name]
                self.assertEqual(actual, default)
                options = setting.findall("./constraints/options/option")
                if name in CHOICES:
                    self.assertEqual(tuple(item.text for item in options), CHOICES[name])
                if name == "page_size":
                    self.assertEqual(tuple(int(item.text) for item in options), INTEGER_CHOICES[name])
        progress = settings["progress_interval"]
        self.assertEqual(progress.findtext("./constraints/minimum"), "5")
        self.assertEqual(progress.findtext("./constraints/maximum"), "10")
        self.assertEqual(progress.findtext("./constraints/step"), "5")


if __name__ == "__main__":
    unittest.main()
