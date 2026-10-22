"""Profile picker, code sign-in screen and sign-in skin, with Kodi stubbed."""

import importlib
from pathlib import Path
import re
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default/1080i"
MEDIA = ROOT / "resources/skins/Default/media"
sys.path.insert(0, str(ROOT / "resources/lib"))

DISCLAIMER = "Silo Theater is a community add-on for Silo, not an official Silo app."


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2 = label, label2
        self.art, self.properties = {}, {}

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")


class FakeList:
    def __init__(self):
        self.items, self.selected, self.width, self.position = [], 0, None, None

    def reset(self):
        self.items = []

    def addItems(self, items):
        self.items.extend(items)

    def setWidth(self, width):
        self.width = width

    def setPosition(self, left, top):
        self.position = (left, top)

    def selectItem(self, index):
        self.selected = index

    def getSelectedPosition(self):
        return self.selected


class FakeProgress:
    def __init__(self):
        self.percent = None

    def setPercent(self, percent):
        self.percent = percent


class FakeDialog:
    created = []
    interact = None

    def __new__(cls, *args, **kwargs):
        window = object.__new__(cls)
        window.arguments = args
        window.properties, window.focus = {}, None
        window.closed = window.shown = 0
        window.initialized_controls = False
        window.controls = {300: FakeList(), 310: FakeProgress()}
        FakeDialog.created.append(window)
        return window

    def getControl(self, key):
        if not self.initialized_controls:
            raise RuntimeError("Non-Existent Control {}".format(key))
        return self.controls[key]

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")

    def setFocusId(self, key):
        self.focus = key

    def close(self):
        self.closed += 1

    def show(self):
        self.shown += 1
        self.initialized_controls = True
        self.onInit()

    def doModal(self):
        self.initialized_controls = True
        self.onInit()
        FakeDialog.interact(self)


GUI = SimpleNamespace(WindowXMLDialog=FakeDialog, WindowXML=FakeDialog, ListItem=FakeListItem)
with patch.dict(sys.modules, {"xbmcgui": GUI}):
    for name in ("silo.ui_people", "silo.ui_common"):
        sys.modules.pop(name, None)
    ui_people = importlib.import_module("silo.ui_people")

PROFILES = [
    {"id": "a", "name": "Example Parent", "avatar_url": "https://avatars.example.invalid/9.x/thumbs/svg?seed=a"},
    {"id": "b", "name": "kid", "has_pin": True},
    {"id": "c", "name": "", "avatar_url": "https://avatars.example.invalid/c.svg"},
]


def back():
    return SimpleNamespace(getId=lambda: 92)


class ProfilePickerTests(unittest.TestCase):
    def setUp(self):
        FakeDialog.created = []

    def pick(self, interact, profiles=PROFILES, **kwargs):
        FakeDialog.interact = interact
        result = ui_people.pick_profile(str(ROOT), profiles, **kwargs)
        return result, FakeDialog.created[-1]

    def test_click_returns_selected_index(self):
        def interact(window):
            window.controls[300].selected = 1
            window.onClick(300)

        result, window = self.pick(interact)
        self.assertEqual(result, 1)
        self.assertEqual(window.closed, 1)
        self.assertEqual(window.arguments, ("SiloProfiles.xml", str(ROOT), "Default", "1080i"))

    def test_back_returns_minus_one(self):
        result, window = self.pick(lambda window: window.onAction(back()))
        self.assertEqual(result, -1)
        self.assertEqual(window.closed, 1)

    def test_no_profiles_returns_minus_one_without_a_window(self):
        self.assertEqual(ui_people.pick_profile(str(ROOT), []), -1)
        self.assertEqual(FakeDialog.created, [])

    def test_preselect_focuses_the_right_item_and_labels_account(self):
        def interact(window):
            self.assertEqual(window.controls[300].selected, 2)
            self.assertEqual(window.focus, 300)
            self.assertEqual(window.properties["silo.account_label"], "user@example.invalid")
            window.onClick(300)

        result, _ = self.pick(interact, preselect=2, account_label="user@example.invalid")
        self.assertEqual(result, 2)

    def test_out_of_range_preselect_falls_back_to_first(self):
        def interact(window):
            self.assertEqual(window.controls[300].selected, 0)
            window.onAction(back())

        self.pick(interact, preselect=7)

    def test_items_carry_png_avatar_initials_and_pin(self):
        def interact(window):
            items = window.controls[300].items
            self.assertEqual([item.label for item in items], ["Example Parent", "kid", ""])
            self.assertEqual(items[0].art, {"thumb": "https://avatars.example.invalid/9.x/thumbs/png?seed=a"})
            self.assertEqual(items[1].art, {})
            self.assertEqual(items[2].art, {})  # Kodi cannot draw SVG files.
            self.assertEqual([item.getProperty("silo.initials") for item in items], ["EP", "K", "?"])
            self.assertEqual([item.getProperty("silo.pin") for item in items], ["", "true", ""])
            window.onAction(back())

        self.pick(interact)

    def test_row_is_centred_for_few_profiles_and_capped_for_many(self):
        def interact(window):
            window.onAction(back())

        _, window = self.pick(interact)
        self.assertEqual(window.controls[300].width, 3 * 284)
        self.assertEqual(window.controls[300].position, ((1920 - 3 * 284) // 2, ui_people.PROFILE_LIST_TOP))
        many = [{"id": str(index), "name": "Example {}".format(index)} for index in range(9)]
        _, window = self.pick(interact, profiles=many)
        self.assertEqual(window.controls[300].width, 6 * 284)
        self.assertEqual(len(window.controls[300].items), 9)


class CodeScreenTests(unittest.TestCase):
    def setUp(self):
        FakeDialog.created = []
        self.screen = ui_people.CodeScreen(str(ROOT))

    def test_open_shows_modeless_window_with_url_and_code_tiles(self):
        self.screen.open("https://silo.example.invalid/activate", "ABCD-EFGH", 600)
        window = FakeDialog.created[-1]
        self.assertEqual(window.arguments[0], "SiloCode.xml")
        self.assertEqual(window.shown, 1)
        self.assertEqual(window.focus, 400)
        props = window.properties
        self.assertEqual(props["silo.code_url"], "https://silo.example.invalid/activate")
        self.assertEqual(props["silo.code"], "ABCD-EFGH")
        self.assertEqual(props["silo.code_tiles"], "true")
        self.assertEqual([props["silo.code.{}".format(i)] for i in range(9)], list("ABCDEFGH") + [""])
        self.assertEqual(props["silo.code.3.gap"], "true")
        self.assertEqual(props["silo.code.2.gap"], "")
        self.assertEqual(props["silo.code_expiry"], "Expires in 10:00")
        self.assertEqual(window.controls[310].percent, 100.0)
        self.assertFalse(self.screen.cancelled())

    def test_long_codes_fall_back_to_plain_text(self):
        self.assertEqual(len(ui_people.code_slots("ABCDEFGHJ")), 9)
        self.assertEqual(ui_people.code_slots("ABCDEFGHJK"), [])
        self.assertEqual(ui_people.code_slots("ABCDE-FGHJ"), [])
        self.assertEqual(ui_people.code_slots("-AB--CD-"), [("A", False), ("B", True), ("C", False), ("D", False)])
        props = ui_people.code_properties("https://silo.example.invalid", "ABCDEFGHJK")
        self.assertEqual(props["silo.code_tiles"], "")
        self.assertEqual(props["silo.code"], "ABCDEFGHJK")

    def test_update_clamps_fraction(self):
        self.screen.open("https://silo.example.invalid", "ABCD", 120)
        window = FakeDialog.created[-1]
        self.assertEqual(self.screen.update(-3), 0.0)
        self.assertEqual(window.controls[310].percent, 100.0)
        self.assertEqual(self.screen.update(0.5), 0.5)
        self.assertEqual(window.controls[310].percent, 50.0)
        self.assertEqual(window.properties["silo.code_expiry"], "Expires in 1:00")
        self.assertEqual(self.screen.update(4), 1.0)
        self.assertEqual(window.controls[310].percent, 0.0)
        self.assertEqual(window.properties["silo.code_expiry"], "Code expired")
        self.assertEqual(self.screen.update(float("nan")), 0.0)
        self.assertEqual(self.screen.update(None), 0.0)

    def test_back_sets_cancelled(self):
        self.screen.open("https://silo.example.invalid", "ABCD", 60)
        FakeDialog.created[-1].onAction(back())
        self.assertTrue(self.screen.cancelled())

    def test_cancel_button_sets_cancelled_and_survives_close(self):
        self.screen.open("https://silo.example.invalid", "ABCD", 60)
        window = FakeDialog.created[-1]
        window.onClick(400)
        self.assertTrue(self.screen.cancelled())
        self.screen.close()
        self.assertTrue(self.screen.cancelled())

    def test_other_actions_do_not_cancel(self):
        self.screen.open("https://silo.example.invalid", "ABCD", 60)
        window = FakeDialog.created[-1]
        window.onAction(SimpleNamespace(getId=lambda: 1))
        window.onClick(999)
        self.assertFalse(self.screen.cancelled())

    def test_close_is_idempotent_and_safe_before_open(self):
        self.screen.close()
        self.screen.open("https://silo.example.invalid", "ABCD", 60)
        window = FakeDialog.created[-1]
        self.screen.close()
        self.screen.close()
        self.assertEqual(window.closed, 1)
        self.screen.update(0.5)  # No window left; must not raise.

    def test_reopen_with_new_code_reuses_window_and_clears_old_tiles(self):
        self.screen.open("https://silo.example.invalid", "ABCDEFGH", 60)
        self.screen.open("https://silo.example.invalid", "WXYZ", 60)
        self.assertEqual(len(FakeDialog.created), 1)
        window = FakeDialog.created[-1]
        self.assertEqual(window.shown, 1)
        self.assertEqual(window.properties["silo.code.4"], "")
        self.assertEqual(window.properties["silo.code.0"], "W")


def controls_by_id(tree):
    return {int(node.attrib["id"]): node for node in tree.iter("control") if "id" in node.attrib}


class SkinTests(unittest.TestCase):
    NAMES = ("SiloProfiles.xml", "SiloCode.xml", "SiloLogin.xml")

    def test_skins_are_well_formed_with_unique_ids_and_valid_nav(self):
        for name in self.NAMES:
            tree = ET.parse(SKIN / name)
            ids = [int(node.attrib["id"]) for node in tree.iter("control") if "id" in node.attrib]
            self.assertEqual(len(ids), len(set(ids)), name)
            for direction in ("onleft", "onright", "onup", "ondown"):
                for node in tree.iter(direction):
                    self.assertIn(int(node.text), ids, name)
            default = tree.getroot().findtext("defaultcontrol")
            self.assertIn(int(default), ids, name)

    def test_referenced_textures_exist(self):
        for name in self.NAMES:
            tree = ET.parse(SKIN / name)
            for node in tree.iter():
                if node.tag.startswith("texture") or node.tag in ("midtexture", "lefttexture", "righttexture"):
                    names = [node.get("diffuse")]
                    if node.text and "$" not in node.text:
                        names.append(node.text.strip())
                    for texture in filter(None, names):
                        self.assertTrue((MEDIA / texture).is_file(), (name, texture))

    def test_every_screen_shows_the_lockup_and_the_community_line(self):
        for name in self.NAMES:
            tree = ET.parse(SKIN / name)
            labels = [node.findtext("label") for node in tree.iter("control")]
            self.assertIn(DISCLAIMER, labels, name)
            self.assertIn("Theater", labels, name)
            wordmark = [node for node in tree.iter("control") if node.findtext("texture") == "silo_wordmark.png"]
            self.assertEqual(len(wordmark), 1, name)
            self.assertIsNone(wordmark[0].find("texture").get("colordiffuse"), name)
            self.assertEqual((wordmark[0].findtext("width"), wordmark[0].findtext("height")), ("100", "52"), name)

    def test_profile_and_code_controls_match_controllers(self):
        profiles = controls_by_id(ET.parse(SKIN / "SiloProfiles.xml"))
        self.assertEqual(profiles[ui_people.PROFILE_LIST].get("type"), "list")
        self.assertEqual(profiles[ui_people.PROFILE_LIST].findtext("top"), str(ui_people.PROFILE_LIST_TOP))
        self.assertEqual(profiles[ui_people.PROFILE_LIST].findtext("orientation"), "horizontal")
        code_tree = ET.parse(SKIN / "SiloCode.xml")
        code = controls_by_id(code_tree)
        self.assertEqual(code[ui_people.CODE_PROGRESS].get("type"), "progress")
        self.assertEqual(code[ui_people.CODE_CANCEL].get("type"), "button")
        text = ET.tostring(code_tree.getroot(), encoding="unicode")
        for index in range(ui_people.CODE_MAX_CHARACTERS):
            self.assertIn("Window.Property(silo.code.{})".format(index), text)
        for index in range(ui_people.CODE_MAX_CHARACTERS - 1):
            self.assertIn("Window.Property(silo.code.{}.gap)".format(index), text)

    def test_login_navigation_targets_visible_enabled_controls_in_every_mode(self):
        controls = controls_by_id(ET.parse(SKIN / "SiloLogin.xml"))
        focusable = {key for key, node in controls.items() if node.get("type") in ("edit", "button")}
        self.assertEqual(focusable, {101, 102, 103, 201, 202, 203})

        def holds(condition, properties):
            for term in condition.split(" + "):
                match = re.fullmatch(r"(!?)String\.IsEmpty\(Window\.Property\((silo\.\w+)\)\)", term.strip())
                self.assertIsNotNone(match, term)
                negated, key = match.groups()
                if (not properties.get(key)) == bool(negated):
                    return False
            return True

        for allow_code in ("true", ""):
            for allow_password in ("true", ""):
                properties = {"silo.allow_code": allow_code, "silo.allow_password": allow_password}
                usable = {key for key in focusable
                          if all(holds(controls[key].findtext(tag) or "!String.IsEmpty(Window.Property(silo.always))",
                                       dict(properties, **{"silo.always": "true"}))
                                 for tag in ("visible", "enable"))}
                self.assertTrue({101, 201, 203} <= usable)
                for current in usable:
                    for direction in ("onleft", "onright", "onup", "ondown"):
                        choices = controls[current].findall(direction)
                        if not choices:  # Kodi keeps focus where it is (edit fields' left/right).
                            continue
                        target = next((int(node.text) for node in choices
                                       if not node.get("condition") or holds(node.get("condition"), properties)), None)
                        self.assertIn(target, usable, (current, direction, properties))


if __name__ == "__main__":
    unittest.main()
