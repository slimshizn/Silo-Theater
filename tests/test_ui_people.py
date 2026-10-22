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

DISCLAIMER = "Founders Theater is a community add-on for Silo, not an official Silo app."


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
        window.controls = {300: FakeList(), 310: FakeProgress(), 500: FakeList()}
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


GUI = SimpleNamespace(WindowXMLDialog=FakeDialog, WindowXML=FakeDialog, ListItem=FakeListItem,
                      INPUT_ALPHANUM=0, ALPHANUM_HIDE_INPUT=2)
with patch.dict(sys.modules, {"xbmcgui": GUI}):
    for name in ("silo.ui_people", "silo.ui_common"):
        sys.modules.pop(name, None)
    ui_people = importlib.import_module("silo.ui_people")
# The modules ui_people imported (patch.dict dropped them from sys.modules on exit).
svc = ui_people.svc
ApiError = ui_people.ApiError

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


class ScriptedDialog:
    """xbmcgui.Dialog with scripted answers; records what it was asked."""

    def __init__(self, inputs=(), selects=(), yesno=True, multiselect=None):
        self.inputs, self.selects = list(inputs), list(selects)
        self.answer, self.chosen = yesno, multiselect
        self.calls, self.oks = [], []

    def input(self, heading, defaultt="", type=0, option=0):
        self.calls.append(("input", heading, option))
        return self.inputs.pop(0) if self.inputs else ""

    def select(self, heading, options, preselect=-1, useDetails=False):
        self.calls.append(("select", heading, list(options), preselect, useDetails))
        return self.selects.pop(0) if self.selects else -1

    def multiselect(self, heading, options, preselect=None):
        self.calls.append(("multiselect", heading, list(options), preselect))
        return self.chosen

    def yesno(self, heading, message):
        self.calls.append(("yesno", heading, message))
        return self.answer

    def ok(self, heading, message):
        self.oks.append(message)


HOUSEHOLD = [
    {"id": "1", "name": "Example Parent", "is_primary": True, "has_pin": True, "avatar": "", "is_child": False},
    {"id": "2", "name": "Example Kid", "is_primary": False, "has_pin": False, "is_child": True,
     "max_content_rating": "PG", "library_restrictions_enabled": True, "allowed_library_ids": ["3"]},
    {"id": "3", "name": "Example Guest", "is_primary": False, "has_pin": True},
]
FLAGS = {"max_advisory_age_supported": True, "require_advisory_age_supported": True}


class FakeClient:
    server_url = "https://silo.example.invalid"

    def __init__(self, active_id=None, token=None):
        self.store = {"access_token": "access", "profile_id": active_id, "profile_token": token}
        self.calls = []
        self.household = [dict(profile) for profile in HOUSEHOLD]
        self.failure = None
        self.pin = {"valid": True, "profile_token": "primary-proof"}

    def request(self, method, path, body=None, **kwargs):
        headers = {} if kwargs.get("profile") is False else {
            "X-Profile-Id": self.store.get("profile_id"), "X-Profile-Token": self.store.get("profile_token")}
        self.calls.append((method, path, body, headers))
        if self.failure and method in ("POST", "PATCH", "DELETE"):
            raise self.failure
        if (method, path) == ("GET", "/profiles"):
            return dict(FLAGS, items=[dict(profile) for profile in self.household])
        if (method, path) == ("GET", "/user/libraries"):
            return {"items": [{"id": "3", "name": "Example Movies"}, {"id": "4", "name": "Example Shows"}]}
        if (method, path) == ("GET", "/auth/signup"):
            return {"enabled": True}
        if method == "PATCH":
            profile = next(item for item in self.household if "/profiles/" + item["id"] == path)
            profile.update(body)
            return dict(profile)
        return {}

    def writes(self):
        return [call for call in self.calls if call[0] in ("POST", "PATCH", "DELETE")]

    def profiles(self):
        return self.request("GET", "/profiles", profile=False)

    def verify_pin(self, profile_id, pin):
        self.calls.append(("verify", profile_id, pin, {}))
        return dict(self.pin)

    def select_profile(self, profile_id, token=None):
        self.store.update(profile_id=str(profile_id), profile_token=token)


def make_application(active_id="1", dialog=None, temporary=False):
    client = FakeClient(active_id, "active-proof" if active_id == "1" else None)
    active = next((dict(profile) for profile in HOUSEHOLD if profile["id"] == active_id), {})
    application = SimpleNamespace(client=client, dialog=dialog or ScriptedDialog(), profile=active,
                                  profile_name=active.get("name", ""), profile_avatar="",
                                  store={"temporary_session": temporary},
                                  addon=SimpleNamespace(getAddonInfo=lambda key: str(ROOT)),
                                  guard=lambda callback: callback(), opened=[])
    application.open_screen = lambda name, **kwargs: application.opened.append(name)
    return application


def started(application):
    manager = ui_people.ProfileManager(application)
    manager.start()
    return manager


def keys(form):
    return [key for key, _label, _value in form.rows()]


class PermissionTests(unittest.TestCase):
    def test_primary_manages_every_field_and_every_profile(self):
        manager = started(make_application("1"))
        self.assertTrue(manager.manager)
        self.assertTrue(all(manager.can_edit(profile) for profile in manager.profiles))
        form = manager.form(manager.profiles[1])
        self.assertEqual(keys(form), ["name", "avatar", "pin", "is_child", "max_content_rating", "max_advisory_age",
                                      "library_restrictions_enabled", "allowed_library_ids"])
        self.assertEqual(dict((key, value) for key, _label, value in form.rows())["allowed_library_ids"], "1 selected")

    def test_other_profiles_edit_only_themselves_without_access_fields(self):
        application = make_application("3")
        manager = started(application)
        self.assertFalse(manager.manager)
        self.assertEqual([manager.can_edit(profile) for profile in manager.profiles], [False, False, True])
        self.assertEqual(keys(manager.form(manager.profiles[2])), ["name", "avatar", "pin"])
        self.assertIn("Only Example Parent can add, change or delete other profiles", manager.note())
        self.assertEqual(manager.unlock_label(), "Manage as Example Parent")
        self.assertIn("Only Example Parent", manager.save(None, manager.form(None)))
        self.assertIn("Only Example Parent", manager.delete(manager.profiles[1]))
        self.assertEqual(application.client.writes(), [])

    def test_self_service_body_never_carries_access_limits(self):
        profile = HOUSEHOLD[1]
        form = svc.form_state(profile)
        form.update(name="Example Renamed", is_child=False, max_content_rating=None, library_restrictions_enabled=False)
        self.assertEqual(svc.update_body(profile, form, manager=False), {"name": "Example Renamed"})
        body = svc.update_body(profile, form, manager=True, flags=FLAGS)
        self.assertEqual(body, {"name": "Example Renamed", "is_child": False, "library_restrictions_enabled": False,
                                "max_content_rating": None})

    def test_advisory_rows_follow_the_server_flags(self):
        manager = started(make_application("1"))
        manager.flags = {}
        self.assertNotIn("max_advisory_age", keys(manager.form(manager.profiles[1])))
        manager.flags = FLAGS
        form = manager.form(manager.profiles[1])
        form.dialog = ScriptedDialog(selects=[3])
        form.activate("max_advisory_age")
        self.assertEqual(form.state["max_advisory_age"], 13)
        self.assertIn("require_advisory_age", keys(form))
        self.assertEqual(svc.update_body(manager.profiles[1], form.state, True, FLAGS), {"max_advisory_age": 13})
        self.assertEqual(svc.update_body(manager.profiles[1], form.state, True, {}), {})

    def test_unlock_acts_as_the_primary_without_changing_the_selected_profile(self):
        dialog = ScriptedDialog(inputs=["2468"])
        application = make_application("3", dialog)
        manager = started(application)
        self.assertTrue(manager.unlock())
        self.assertIn(("verify", "1", "2468", {}), application.client.calls)
        self.assertEqual(dialog.calls[0][2], GUI.ALPHANUM_HIDE_INPUT)
        manager.save(manager.profiles[1], manager.form(manager.profiles[1]))  # Unchanged: nothing sent.
        form = manager.form(manager.profiles[1])
        form.state["name"] = "Example Kid Two"
        self.assertEqual(manager.save(manager.profiles[1], form), "")
        method, path, body, headers = application.client.writes()[-1]
        self.assertEqual((method, path, body), ("PATCH", "/profiles/2", {"name": "Example Kid Two"}))
        self.assertEqual(headers, {"X-Profile-Id": "1", "X-Profile-Token": "primary-proof"})
        self.assertEqual(application.client.store["profile_id"], "3")
        self.assertIsNone(application.client.store["profile_token"])

    def test_wrong_or_cancelled_primary_pin_does_not_unlock(self):
        for inputs, valid in (([], True), (["0000"], False)):
            with self.subTest(inputs=inputs):
                application = make_application("3", ScriptedDialog(inputs=inputs))
                application.client.pin = {"valid": valid, "profile_token": "x" if valid else None}
                manager = started(application)
                self.assertFalse(manager.unlock())
                self.assertFalse(manager.manager)

    def test_refused_household_proof_drops_manager_mode(self):
        application = make_application("3", ScriptedDialog(inputs=["2468"]))
        application.client.failure = ApiError(403, "Verify the profile.", "profile_verification_required")
        manager = started(application)
        manager.unlock()
        form = manager.form(manager.profiles[1])
        form.state["name"] = "Example Changed"
        self.assertEqual(manager.save(manager.profiles[1], form),
                         "Enter the PIN for Example Parent again to manage profiles.")
        self.assertFalse(manager.manager)

    def test_server_conflicts_are_shown_as_sent(self):
        application = make_application("1")
        manager = started(application)
        application.client.failure = ApiError(409, "A profile with this name already exists", "conflict")
        form = manager.form(manager.profiles[2])
        form.state["name"] = "Example Other"
        self.assertEqual(manager.save(manager.profiles[2], form), "A profile with this name already exists")


class PinTests(unittest.TestCase):
    def edit(self, profile, dialog):
        application = make_application("1", dialog)
        manager = started(application)
        profile = next(item for item in manager.profiles if item["id"] == profile)
        form = manager.form(profile)
        form.activate("pin")
        return application, manager, profile, form

    def test_set_pin_asks_twice_hidden_and_sends_it(self):
        application, manager, profile, form = self.edit("2", ScriptedDialog(inputs=["1234", "1234"]))
        self.assertEqual(form.pin_text(), "New PIN")
        self.assertEqual(manager.save(profile, form), "")
        self.assertEqual(application.client.writes()[-1][:3], ("PATCH", "/profiles/2", {"pin": "1234"}))

    def test_remove_pin_sends_null(self):
        application, manager, profile, form = self.edit("3", ScriptedDialog(selects=[1]))
        self.assertEqual(form.pin_text(), "Off")
        manager.save(profile, form)
        self.assertEqual(application.client.writes()[-1][:3], ("PATCH", "/profiles/3", {"pin": None}))

    def test_change_pin_offers_change_and_remove(self):
        dialog = ScriptedDialog(selects=[0], inputs=["9876", "9876"])
        _application, _manager, _profile, form = self.edit("3", dialog)
        self.assertEqual(dialog.calls[0][2], ["Change PIN", "Remove PIN"])
        self.assertEqual(form.state["pin"], "9876")

    def test_mismatched_or_malformed_pins_change_nothing(self):
        for inputs, message in ((["1234", "4321"], "did not match"), (["12a4"], "must be 4 digits")):
            with self.subTest(inputs=inputs):
                dialog = ScriptedDialog(inputs=inputs)
                _application, _manager, _profile, form = self.edit("2", dialog)
                self.assertIsNone(form.state["pin"])
                self.assertIn(message, dialog.oks[-1])

    def test_changing_the_active_profiles_pin_proves_the_new_one(self):
        application, manager, profile, form = self.edit("1", ScriptedDialog(selects=[0], inputs=["1357", "1357"]))
        application.client.pin = {"valid": True, "profile_token": "new-proof"}
        self.assertEqual(manager.save(profile, form), "")
        self.assertIn(("verify", "1", "1357", {}), application.client.calls)
        self.assertEqual(application.client.store["profile_token"], "new-proof")
        self.assertTrue(application.profile.get("has_pin") is not None)


class CreateDeleteTests(unittest.TestCase):
    def test_new_profile_posts_only_set_members(self):
        dialog = ScriptedDialog(inputs=["Example New"], selects=[2])
        application = make_application("1", dialog)
        manager = started(application)
        form = manager.form(None)
        form.activate("name")
        form.activate("is_child")
        form.activate("max_content_rating")
        self.assertEqual(manager.save(None, form), "")
        self.assertEqual(application.client.writes()[-1][:3],
                         ("POST", "/profiles", {"name": "Example New", "is_child": True, "max_content_rating": "PG"}))
        self.assertNotIn(None, application.client.writes()[-1][2].values())

    def test_kid_profile_defaults_to_pg_and_library_limits_need_a_library(self):
        dialog = ScriptedDialog(inputs=["Example Small"], multiselect=[])
        application = make_application("1", dialog)
        manager = started(application)
        form = manager.form(None)
        form.activate("name")
        form.activate("is_child")
        self.assertEqual(form.state["max_content_rating"], "PG")
        form.activate("library_restrictions_enabled")
        self.assertEqual(dialog.calls[-1][2], ["Example Movies", "Example Shows"])
        self.assertEqual(manager.save(None, form), svc.CHOOSE_A_LIBRARY)
        dialog.chosen = [1]
        form.activate("allowed_library_ids")
        manager.save(None, form)
        body = application.client.writes()[-1][2]
        self.assertEqual((body["library_restrictions_enabled"], body["allowed_library_ids"]), (True, ["4"]))

    def test_duplicate_or_empty_names_are_refused_before_sending(self):
        application = make_application("1")
        manager = started(application)
        form = manager.form(None)
        self.assertEqual(manager.save(None, form), "Enter a name for this profile.")
        form.state["name"] = " example kid "
        self.assertEqual(manager.save(None, form), "A profile with this name already exists.")
        self.assertEqual(application.client.writes(), [])

    def test_delete_guards_the_primary_and_deletes_others(self):
        application = make_application("1")
        manager = started(application)
        self.assertEqual(manager.delete(manager.profiles[0]), "The primary profile cannot be deleted.")
        self.assertEqual(application.client.writes(), [])
        self.assertEqual(manager.delete(manager.profiles[2]), "")
        self.assertEqual(application.client.writes()[-1][:2], ("DELETE", "/profiles/3"))
        application.client.failure = ApiError(409, "The primary profile cannot be deleted", "conflict")
        self.assertEqual(manager.delete(manager.profiles[1]), "The primary profile cannot be deleted")
        application.client.failure = ApiError(404, "Not found", "not_found")
        self.assertEqual(manager.delete(manager.profiles[1]), "")


class AvatarTests(unittest.TestCase):
    def test_presets_are_png_and_carry_preset_refs(self):
        presets = svc.preset_batch("fun-emoji")
        self.assertEqual(len(presets), svc.PRESETS_PER_BATCH)
        ref, image = presets[0]
        self.assertTrue(ref.startswith("preset:dicebear:fun-emoji:"))
        self.assertIn("/9.x/fun-emoji/png?", image)
        self.assertNotEqual(presets, svc.preset_batch("fun-emoji", 1))
        self.assertEqual(svc.preset_batch("initials", name="Example Kid!")[0][0], "preset:dicebear:initials:Example-Kid")

    def test_picker_shows_images_and_more_choices(self):
        dialog = ScriptedDialog(selects=[1, 0, 3])
        application = make_application("1", dialog)
        manager = started(application)
        form = manager.form(manager.profiles[1])
        form.activate("avatar")
        grid = [call for call in dialog.calls if call[0] == "select" and call[1] == "Choose an avatar"]
        self.assertEqual(len(grid), 2)  # "More choices" showed a second page.
        items = grid[1][2]
        self.assertTrue(grid[1][4])
        self.assertEqual(items[0].label, "More choices")
        self.assertTrue(items[3].art["thumb"].startswith("https://api.dicebear.com/9.x/bottts-neutral/png?"))
        self.assertEqual(form.state["avatar"], svc.preset_batch("bottts-neutral", 1)[2][0])
        self.assertEqual(form.avatar_text(), "Bottts")
        manager.save(manager.profiles[1], form)
        self.assertEqual(application.client.writes()[-1][2], {"avatar": form.state["avatar"]})

    def test_remove_avatar_sends_null(self):
        profile = dict(HOUSEHOLD[2], avatar="preset:dicebear:identicon:example")
        form = ui_people.ProfileForm(profile, True, FLAGS, ScriptedDialog(selects=[5]))
        form.activate("avatar")
        self.assertEqual(svc.update_body(profile, form.state, True, FLAGS), {"avatar": None})


class ManageScreenTests(unittest.TestCase):
    def setUp(self):
        FakeDialog.created = []

    def test_manage_mode_lists_profiles_and_add_for_the_primary(self):
        def interact(window):
            items = window.controls[300].items
            self.assertEqual([item.label for item in items], ["Example Parent", "Example Kid", "Example Guest",
                                                              "Add profile"])
            self.assertEqual(items[-1].getProperty("silo.initials"), "+")
            self.assertEqual(items[0].getProperty("silo.caption"), "Edit")
            self.assertEqual(window.properties["silo.heading"], "Manage profiles")
            self.assertEqual(window.properties["silo.manage_label"], "Done")
            self.assertEqual(window.properties["silo.unlock_label"], "")
            window.onClick(ui_people.MANAGE_BUTTON)

        FakeDialog.interact = interact
        self.assertIsNone(ui_people.manage_profiles(make_application("1")))
        self.assertEqual(FakeDialog.created[-1].closed, 1)

    def test_editing_from_the_row_saves_and_refreshes(self):
        application = make_application("1", ScriptedDialog(inputs=["Example Teen"]))

        def interact(window):
            if window.arguments[0] == ui_people.EDIT_XML:
                rows = window.controls[500].items
                self.assertEqual(rows[0].label, "Name")
                self.assertEqual(window.properties["silo.can_delete"], "true")
                window.controls[500].selected = 0
                window.onClick(ui_people.EDIT_LIST)
                self.assertEqual(window.controls[500].items[0].label2, "Example Teen")
                window.onClick(ui_people.EDIT_SAVE)
                return
            window.controls[300].selected = 1
            window.onClick(ui_people.PROFILE_LIST)
            self.assertEqual(window.controls[300].items[1].label, "Example Teen")

        FakeDialog.interact = interact
        ui_people.manage_profiles(application)
        self.assertEqual(application.client.writes()[-1][:3], ("PATCH", "/profiles/2", {"name": "Example Teen"}))

    def test_editor_shows_inline_errors_and_confirms_delete(self):
        application = make_application("1", ScriptedDialog(yesno=False))
        manager = started(application)

        def interact(window):
            window.form.state["name"] = ""
            window.onClick(ui_people.EDIT_SAVE)
            self.assertEqual(window.properties["silo.message"], "Enter a name for this profile.")
            self.assertEqual(window.closed, 0)
            window.onClick(ui_people.EDIT_DELETE)
            self.assertEqual(window.closed, 0)
            application.dialog.answer = True
            window.onClick(ui_people.EDIT_DELETE)

        FakeDialog.interact = interact
        self.assertEqual(ui_people.edit_profile(manager, manager.profiles[2]), "deleted")
        self.assertEqual(application.client.writes(), [("DELETE", "/profiles/3", None,
                                                         {"X-Profile-Id": "1", "X-Profile-Token": "active-proof"})])

    def test_primary_cannot_be_deleted_from_the_editor(self):
        manager = started(make_application("1"))
        FakeDialog.interact = lambda window: window.onClick(ui_people.EDIT_CANCEL)
        self.assertIsNone(ui_people.edit_profile(manager, manager.profiles[0]))
        self.assertEqual(FakeDialog.created[-1].properties["silo.can_delete"], "")

    def test_non_manager_sees_a_note_and_cannot_open_others(self):
        application = make_application("3")

        def interact(window):
            self.assertIn("Only Example Parent", window.properties["silo.note"])
            self.assertEqual(window.properties["silo.unlock_label"], "Manage as Example Parent")
            self.assertEqual(len(window.controls[300].items), 3)  # No "Add profile".
            window.controls[300].selected = 1
            window.onClick(ui_people.PROFILE_LIST)
            window.onAction(back())

        FakeDialog.interact = interact
        ui_people.manage_profiles(application)
        self.assertIn("Only Example Parent can change Example Kid", application.dialog.oks[-1])
        self.assertEqual(len(FakeDialog.created), 1)

    def test_temporary_session_and_cancelled_unlock_open_nothing(self):
        application = make_application("1", temporary=True)
        ui_people.manage_profiles(application)
        self.assertIn("temporary sign-in", application.dialog.oks[-1])
        application = make_application(None)  # From the picker, before any profile is chosen.
        ui_people.manage_profiles(application)
        self.assertEqual(FakeDialog.created, [])

    def test_picker_manage_button_refreshes_the_callers_list(self):
        application = make_application("1")
        profiles = [dict(profile) for profile in HOUSEHOLD]

        def manage(name):
            application.opened.append(name)
            application.client.household = [HOUSEHOLD[0], dict(HOUSEHOLD[2], name="Example Visitor")]

        application.open_screen = manage

        def interact(window):
            self.assertEqual(window.properties["silo.can_manage"], "true")
            window.controls[300].selected = 2
            window.onClick(ui_people.MANAGE_BUTTON)
            self.assertEqual([item.label for item in window.controls[300].items], ["Example Parent", "Example Visitor"])
            self.assertEqual(window.controls[300].selected, 1)
            window.onClick(ui_people.PROFILE_LIST)

        FakeDialog.interact = interact
        index = ui_people.pick_profile(str(ROOT), profiles, application=application)
        self.assertEqual(application.opened, ["manage_profiles"])
        self.assertEqual(profiles[index]["name"], "Example Visitor")

    def test_picker_without_application_hides_manage(self):
        def interact(window):
            self.assertEqual(window.properties["silo.can_manage"], "")
            window.onClick(ui_people.MANAGE_BUTTON)
            window.onAction(back())

        FakeDialog.interact = interact
        self.assertEqual(ui_people.pick_profile(str(ROOT), PROFILES), -1)


class SignupTests(unittest.TestCase):
    def test_signup_is_offered_only_when_the_server_enables_it(self):
        client = FakeClient()
        self.assertTrue(ui_people.signup_available(client))
        self.assertEqual(client.calls[-1][:2], ("GET", "/auth/signup"))
        client.request = lambda *args, **kwargs: {"enabled": False}
        self.assertFalse(ui_people.signup_available(client))

        def missing(*args, **kwargs):
            raise ApiError(404, "Not found")

        client.request = missing
        self.assertFalse(ui_people.signup_available(client))
        self.assertFalse(ui_people.signup_available(None))

    def test_create_account_posts_signup_and_returns_a_password_submission(self):
        client = FakeClient()
        dialog = ScriptedDialog(inputs=["example", "user@example.invalid", "long-password", "long-password",
                                        "EXAMPLE-INVITE"])
        result = ui_people.create_account(client, dialog)
        self.assertEqual(result, {"action": "password", "server_url": "https://silo.example.invalid",
                                  "username": "example", "password": "long-password"})
        method, path, body, headers = client.calls[-1]
        self.assertEqual((method, path, headers), ("POST", "/auth/signup", {}))
        self.assertEqual(body, {"username": "example", "email": "user@example.invalid", "password": "long-password",
                                "invite_code": "EXAMPLE-INVITE", "create_default_profile": True,
                                "default_profile_name": "example"})
        self.assertEqual([call[2] for call in dialog.calls if "password" in call[1].lower()],
                         [GUI.ALPHANUM_HIDE_INPUT, GUI.ALPHANUM_HIDE_INPUT])

    def test_create_account_stops_on_cancel_mismatch_short_password_or_refusal(self):
        cases = ((["example", ""], None),
                 (["example", "user@example.invalid", "long-password", "other-password"], "did not match"),
                 (["example", "user@example.invalid", "short", "short", "INVITE"], "at least 8"),
                 (["example", "not-an-email", "long-password", "long-password", "INVITE"], "valid email"))
        for inputs, message in cases:
            with self.subTest(inputs=inputs):
                client, dialog = FakeClient(), ScriptedDialog(inputs=inputs)
                self.assertIsNone(ui_people.create_account(client, dialog))
                self.assertEqual(client.writes(), [])
                if message:
                    self.assertIn(message, dialog.oks[-1])
        client = FakeClient()
        client.failure = ApiError(409, "conflict", "conflict")
        dialog = ScriptedDialog(inputs=["example", "user@example.invalid", "long-password", "long-password", "X"])
        self.assertIsNone(ui_people.create_account(client, dialog))
        self.assertEqual(dialog.oks[-1], "That username or email is already in use.")


def controls_by_id(tree):
    return {int(node.attrib["id"]): node for node in tree.iter("control") if "id" in node.attrib}


class SkinTests(unittest.TestCase):
    NAMES = ("SiloProfiles.xml", "SiloCode.xml", "SiloLogin.xml", "SiloProfileEdit.xml")

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
            self.assertIn("Founders Theater", labels, name)
            # Only the add-on's name, never the Silo wordmark (it must not look official).
            self.assertNotIn("silo_wordmark.png", (SKIN / name).read_text(encoding="utf-8"), name)

    def test_profile_and_code_controls_match_controllers(self):
        profiles = controls_by_id(ET.parse(SKIN / "SiloProfiles.xml"))
        self.assertEqual(profiles[ui_people.PROFILE_LIST].get("type"), "list")
        self.assertEqual(profiles[ui_people.PROFILE_LIST].findtext("top"), str(ui_people.PROFILE_LIST_TOP))
        self.assertEqual(profiles[ui_people.PROFILE_LIST].findtext("orientation"), "horizontal")
        for button in (ui_people.MANAGE_BUTTON, ui_people.UNLOCK_BUTTON):
            self.assertEqual(profiles[button].get("type"), "button")
        edit = controls_by_id(ET.parse(SKIN / ui_people.EDIT_XML))
        self.assertEqual(edit[ui_people.EDIT_LIST].get("type"), "list")
        for button in (ui_people.EDIT_SAVE, ui_people.EDIT_CANCEL, ui_people.EDIT_DELETE):
            self.assertEqual(edit[button].get("type"), "button")
        self.assertIn("silo.can_delete", edit[ui_people.EDIT_DELETE].findtext("visible"))
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
        self.assertEqual(focusable, {101, 102, 103, 201, 202, 203, ui_people.SIGNUP_CONTROL})

        def holds(condition, properties):
            for term in condition.split(" + "):
                match = re.fullmatch(r"(!?)String\.IsEmpty\(Window\.Property\((silo\.\w+)\)\)", term.strip())
                self.assertIsNotNone(match, term)
                negated, key = match.groups()
                if (not properties.get(key)) == bool(negated):
                    return False
            return True

        modes = [(code, password, signup) for code in ("true", "") for password in ("true", "")
                 for signup in ("true", "") if password or not signup]  # Sign-up needs account sign-in.
        for allow_code, allow_password, allow_signup in modes:
            properties = {"silo.allow_code": allow_code, "silo.allow_password": allow_password,
                          "silo.allow_signup": allow_signup}
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
