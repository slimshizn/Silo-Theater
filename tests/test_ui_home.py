"""Home window behaviour with Kodi stubbed, plus checks on the generated SiloHome.xml."""

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

    def close(self):
        self.closed = True


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=MagicMock),
}
with patch.dict(sys.modules, KODI_MODULES):
    sys.modules.pop("resources.lib.silo.ui_home", None)
    ui_home = importlib.import_module("resources.lib.silo.ui_home")


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


UP, DOWN, LEFT, RIGHT, BACK = 3, 4, 1, 2, 10


def movie(number, **extra):
    item = {"content_id": "movie:{}".format(number), "type": "movie", "title": "Example Movie {}".format(number),
            "year": 2020 + number % 5, "overview": "An example synopsis."}
    item.update(extra)
    return item


def section(title, section_type="recently_added", count=3, start=0):
    return {"id": title.lower(), "title": title, "section_type": section_type,
            "items": [movie(start + index) for index in range(count)]}


def make_application(sections=None, libraries=None, store=None):
    application = MagicMock()
    application.guard.side_effect = lambda callback: callback()
    application.image.side_effect = lambda item, kind: "https://example.invalid/{}/{}".format(
        item["content_id"], kind)
    application.options.return_value = {"show_plot": True}
    application.home_sections.return_value = sections if sections is not None else [
        section("Continue Watching", "continue_watching"), section("Recently Added"), section("Example Picks")]
    libraries = libraries if libraries is not None else {"movie": [{"id": 3, "name": "Films", "type": "movie"}],
                                                        "series": [{"id": 4, "name": "Shows", "type": "series"}]}
    application.video_libraries.side_effect = lambda kind: libraries.get(kind, [])
    application.profile_name = "Example Profile"
    application.profile_avatar = ""
    application.store = store if store is not None else {}
    return application


def open_window(application=None):
    window = ui_home.HomeWindow("SiloHome.xml", str(ROOT), "Default", "1080i")
    window.application = application or make_application()
    window.hero_debouncer = MagicMock()
    window.onInit()
    return window


def page(count, cursor=None, start=0, total=None):
    return {"items": [movie(start + index) for index in range(count)], "next_cursor": cursor, "total": total}


class HomeRowTests(unittest.TestCase):
    def test_rows_fill_slots_in_order_and_hide_extras(self):
        window = open_window()
        for index, title in enumerate(("Continue Watching", "Recently Added", "Example Picks")):
            group, header = ui_home.slot_ids(index)[:2]
            window.getControl(header).setLabel.assert_called_with(title)
            # Rows that start below the screen stay hidden until focus reaches them.
            window.getControl(group).setVisible.assert_called_with(index < 2)
        for index in range(3, ui_home.SLOTS):
            window.getControl(ui_home.slot_ids(index)[0]).setVisible.assert_called_with(False)
        self.assertEqual(window.focus, ui_home.slot_ids(0)[3])
        self.assertEqual(window.properties["silo.tab"], "home")

    def test_wide_sections_use_the_wide_list_and_play_icon(self):
        window = open_window()
        _group, header, icon, wide, poster = ui_home.slot_ids(0)
        window.getControl(wide).setVisible.assert_called_with(True)
        window.getControl(poster).setVisible.assert_called_with(False)
        window.getControl(icon).setVisible.assert_called_with(True)
        window.getControl(header).setPosition.assert_called_with(148, 0)
        _group, header, icon, wide, poster = ui_home.slot_ids(1)
        window.getControl(poster).setVisible.assert_called_with(True)
        window.getControl(wide).setVisible.assert_called_with(False)
        window.getControl(icon).setVisible.assert_called_with(False)
        self.assertEqual(len(window.getControl(poster).addItems.call_args.args[0]), 3)

    def test_sections_without_items_are_skipped(self):
        window = open_window(make_application([section("Empty", count=0), section("Recently Added")]))
        self.assertEqual([entry["title"] for entry in window.sections], ["Recently Added"])

    def test_hero_follows_the_focused_card(self):
        window = open_window()
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 0")
        poster = ui_home.slot_ids(1)[4]
        window.focus = poster
        window.getControl(poster).getSelectedPosition.return_value = 2
        window.update_hero()
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 2")
        self.assertEqual(window.properties["silo.hero.backdrop"], "https://example.invalid/movie:2/backdrop")
        self.assertEqual(window.properties["silo.hero.synopsis"], "An example synopsis.")

    def test_hero_keeps_last_item_when_focus_is_in_the_top_bar(self):
        window = open_window()
        window.focus = ui_home.SEARCH_BUTTON
        window.properties["silo.hero.title"] = "kept"
        window.update_hero()
        self.assertEqual(window.properties["silo.hero.title"], "kept")

    def test_hero_badges_are_laid_out_and_meta_follows_them(self):
        window = open_window()
        window.show_hero(movie(1, overlay_summary={"resolution": "2160p", "hdr": "HDR10"}, content_rating="PG"))
        self.assertEqual([window.properties["silo.hero.badge{}".format(slot)] for slot in range(1, 5)],
                         ["4K", "HDR", "PG", ""])
        widths = [ui_home.present.badge_width(text) for text in ("4K", "HDR", "PG")]
        window.getControl(412).setPosition.assert_called_with(88 + widths[0] + 12, ui_home.BADGE_TOP)
        window.getControl(ui_home.HERO_META).setPosition.assert_called_with(
            88 + sum(widths) + 36, ui_home.BADGE_TOP)

    def test_hidden_plot_moves_the_hero_down(self):
        application = make_application()
        application.options.return_value = {"show_plot": False}
        window = open_window(application)
        self.assertEqual(window.properties["silo.hero.synopsis"], "")
        window.getControl(ui_home.HERO_GROUP).setPosition.assert_called_with(0, ui_home.NO_SYNOPSIS_SHIFT)

    def test_up_and_down_move_between_rows_and_up_from_first_row_reaches_the_tab(self):
        window = open_window()
        first, second = ui_home.slot_ids(0)[3], ui_home.slot_ids(1)[4]
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, second)
        self.assertEqual(window.row, 1)
        window.getControl(ui_home.slot_ids(0)[0]).setVisible.assert_called_with(False)
        window.getControl(ui_home.slot_ids(1)[0]).setPosition.assert_called_with(0, ui_home.ROWS_TOP)
        window.hero_debouncer.trigger.assert_called()
        window.onAction(Action(UP))
        self.assertEqual(window.focus, first)
        window.onAction(Action(UP))
        self.assertEqual(window.focus, 210)
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, first)

    def test_down_from_last_row_stays(self):
        window = open_window(make_application([section("Recently Added")]))
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, ui_home.slot_ids(0)[4])

    def test_rows_below_the_focused_one_are_stacked(self):
        window = open_window()
        window.getControl(ui_home.slot_ids(1)[0]).setPosition.assert_called_with(
            0, ui_home.ROWS_TOP + ui_home.WIDE_PITCH)

    def test_card_click_opens_item_and_keeps_focus(self):
        window = open_window()
        poster = ui_home.slot_ids(1)[4]
        window.getControl(poster).getSelectedPosition.return_value = 1
        window.application.open_item.side_effect = lambda item: window.setFocusId(999)
        window.onClick(poster)
        window.application.open_item.assert_called_once_with(window.sections[1]["items"][1])
        self.assertEqual(window.focus, poster)

    def test_failed_home_load_leaves_tabs_usable(self):
        application = make_application()
        application.home_sections.side_effect = OSError("offline")
        application.guard.side_effect = lambda callback: None if callback == application.home_sections else (
            _swallow(callback))
        window = open_window(application)
        self.assertEqual(window.sections, [])
        self.assertEqual(window.focus, 210)


def _swallow(callback):
    try:
        return callback()
    except OSError:
        return None


class TopBarTests(unittest.TestCase):
    def test_tabs_without_libraries_are_hidden_and_cluster_recentred(self):
        window = open_window(make_application(libraries={"movie": [{"id": 3, "name": "Films"}], "series": []}))
        window.getControl(217).setVisible.assert_called_with(False)
        window.getControl(212).setVisible.assert_called_with(False)
        width = 56 + 24 + 124 + 8 + 148 + 24 + 56
        window.getControl(ui_home.TAB_CLUSTER).setPosition.assert_called_with((1920 - width) // 2, 0)
        window.getControl(211).controlRight.assert_called_with(window.getControl(ui_home.AVATAR))

    def test_avatar_menu_results(self):
        for control_id, expected in ((610, "profile"), (611, "settings"), (612, "logout")):
            window = open_window()
            window.onClick(ui_home.AVATAR)
            self.assertTrue(window.menu_open)
            self.assertEqual(window.focus, 610)
            window.onClick(control_id)
            self.assertEqual(window.result, expected)
            self.assertTrue(window.closed)
            window.hero_debouncer.cancel.assert_called()

    def test_temporary_session_hides_switch_profile(self):
        window = open_window(make_application(store={"temporary_session": True}))
        window.onClick(ui_home.AVATAR)
        window.getControl(610).setVisible.assert_called_with(False)
        window.getControl(611).setPosition.assert_called_with(16, 16)
        self.assertEqual(window.focus, 611)
        window.onClick(610)
        self.assertIsNone(window.result)
        self.assertFalse(window.closed)

    def test_back_and_sideways_close_the_menu(self):
        for action in (BACK, LEFT, RIGHT):
            window = open_window()
            window.onClick(ui_home.AVATAR)
            window.onAction(Action(action))
            self.assertFalse(window.menu_open)
            self.assertEqual(window.focus, ui_home.AVATAR)
            self.assertFalse(window.closed)


class LibraryTests(unittest.TestCase):
    def test_movies_tab_browses_movies_by_title(self):
        window = open_window()
        window.application.browse.return_value = page(3, total=1234)
        window.onClick(211)
        window.application.browse.assert_called_once_with({"type": "movie", "sort": "title"})
        self.assertEqual(window.properties["silo.tab"], "movies")
        self.assertEqual(window.properties["silo.total"], "1,234 titles")
        self.assertEqual(len(window.grid_items), 3)
        window.getControl(ui_home.HOME_GROUP).setVisible.assert_called_with(False)
        window.getControl(ui_home.LIBRARY_GROUP).setVisible.assert_called_with(True)

    def test_library_and_sort_choices_reload(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.onClick(212)
        window.application.dialog.select.return_value = 1
        window.onClick(ui_home.LIBRARY_CHIP)
        window.application.browse.assert_called_with({"type": "series", "sort": "title", "library_id": 4})
        window.getControl(ui_home.LIBRARY_CHIP).setLabel.assert_called_with("Library: Shows")
        window.application.dialog.select.return_value = 1
        window.onClick(ui_home.SORT_CHIP)
        window.application.browse.assert_called_with({"type": "series", "sort": "-added_at", "library_id": 4})
        window.getControl(ui_home.SORT_CHIP).setLabel.assert_called_with("Sort: Recently added")

    def test_cancelled_choice_does_not_reload(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.onClick(211)
        window.application.dialog.select.return_value = -1
        window.onClick(ui_home.SORT_CHIP)
        self.assertEqual(window.application.browse.call_count, 1)

    def test_infinite_scroll_requests_next_cursor_and_appends(self):
        window = open_window()
        window.application.browse.return_value = page(30, cursor="next")
        window.onClick(211)
        grid = window.getControl(ui_home.GRID)
        window.focus = ui_home.GRID
        grid.getSelectedPosition.return_value = 10
        window.onAction(Action(DOWN))
        self.assertEqual(window.application.browse.call_count, 1)
        window.application.browse.return_value = page(5, start=30)
        grid.getSelectedPosition.return_value = 18
        window.onAction(Action(DOWN))
        window.application.browse.assert_called_with({"type": "movie", "sort": "title"}, "next")
        self.assertEqual(len(window.grid_items), 35)
        self.assertEqual(len(grid.addItems.call_args.args[0]), 5)
        window.onAction(Action(DOWN))
        self.assertEqual(window.application.browse.call_count, 2)

    def test_stale_page_is_ignored(self):
        window = open_window()
        window.application.browse.return_value = page(20, cursor="next")
        window.onClick(211)
        grid = window.getControl(ui_home.GRID)
        window.focus = ui_home.GRID
        grid.getSelectedPosition.return_value = 19

        def switch_tab(params, cursor=None):
            if cursor:
                window.set_tab("series")  # The user moved on while the page loaded.
                return page(5, start=20)
            return page(1)
        window.application.browse.side_effect = switch_tab
        window.onAction(Action(RIGHT))
        self.assertEqual(len(window.grid_items), 20)

    def test_stale_first_page_is_ignored(self):
        window = open_window()

        def slow(params, cursor=None):
            window.set_tab("home")
            return page(4)
        window.application.browse.side_effect = slow
        window.onClick(211)
        self.assertEqual(window.grid_items, [])

    def test_up_from_chips_reaches_selected_tab_and_down_from_tab_reaches_chips(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.focus = 212
        window.onClick(212)
        self.assertEqual(window.focus, 212)
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, ui_home.LIBRARY_CHIP)
        window.onAction(Action(UP))
        self.assertEqual(window.focus, 212)

    def test_grid_click_opens_item(self):
        window = open_window()
        window.application.browse.return_value = page(3)
        window.onClick(211)
        window.getControl(ui_home.GRID).getSelectedPosition.return_value = 2
        window.onClick(ui_home.GRID)
        window.application.open_item.assert_called_once_with(window.grid_items[2])
        self.assertEqual(window.focus, ui_home.GRID)


class SearchTests(unittest.TestCase):
    def test_search_without_query_shows_prompt(self):
        window = open_window()
        window.onClick(ui_home.SEARCH_BUTTON)
        self.assertEqual(window.properties["silo.tab"], "search")
        self.assertEqual(window.properties["silo.empty"], "Search your library")
        self.assertEqual(window.focus, ui_home.SEARCH_FIELD)
        window.application.browse.assert_not_called()

    def test_cancelled_input_does_nothing(self):
        window = open_window()
        window.onClick(ui_home.SEARCH_BUTTON)
        window.application.dialog.input.return_value = ""
        window.onClick(ui_home.SEARCH_FIELD)
        window.application.browse.assert_not_called()
        self.assertEqual(window.query, "")

    def test_query_and_type_chips(self):
        window = open_window()
        window.onClick(ui_home.SEARCH_BUTTON)
        window.application.dialog.input.return_value = " example "
        window.application.browse.return_value = page(0)
        window.onClick(ui_home.SEARCH_FIELD)
        window.application.browse.assert_called_with({"q": "example", "type": "video"})
        self.assertEqual(window.properties["silo.empty"], "No results")
        self.assertEqual(window.properties["silo.query"], "example")
        window.application.browse.return_value = page(2)
        window.onClick(532)
        window.application.browse.assert_called_with({"q": "example", "type": "series"})
        self.assertEqual(window.properties["silo.search_type"], "series")
        self.assertEqual(window.properties["silo.empty"], "")
        window.getControl(ui_home.GRID).setPosition.assert_called_with(ui_home.GRID_LEFT, ui_home.SEARCH_GRID_TOP)

    def test_up_from_field_reaches_search_button(self):
        window = open_window()
        window.onClick(ui_home.SEARCH_BUTTON)
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_home.SEARCH_BUTTON)


class BackTests(unittest.TestCase):
    def test_back_from_a_tab_returns_home(self):
        window = open_window()
        window.application.browse.return_value = page(1)
        window.onClick(211)
        window.onAction(Action(BACK))
        self.assertEqual(window.properties["silo.tab"], "home")
        self.assertFalse(window.closed)
        self.assertEqual(window.focus, ui_home.slot_ids(0)[3])

    def test_back_from_home_closes_without_result(self):
        window = open_window()
        window.onAction(Action(BACK))
        self.assertTrue(window.closed)
        self.assertIsNone(window.result)


class SkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tree = ET.parse(SKIN / "1080i/SiloHome.xml")
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
            if not element.tag.startswith("texture") and element.tag not in (
                    "midtexture", "lefttexture", "righttexture", "overlaytexture"):
                continue
            for name in (element.text, element.get("diffuse")):
                if name and "$INFO" not in name:
                    self.assertTrue((media / name).is_file(), name)

    def test_python_control_ids_exist(self):
        ids = set(self.ids)
        wanted = [ui_home.TOP_BAR, ui_home.TAB_CLUSTER, ui_home.SEARCH_BUTTON, ui_home.AVATAR,
                  ui_home.HOME_GROUP, ui_home.HERO_GROUP, ui_home.HERO_META, ui_home.BROWSE_GROUP,
                  ui_home.LIBRARY_GROUP, ui_home.SEARCH_GROUP, ui_home.GRID, ui_home.LIBRARY_CHIP,
                  ui_home.SORT_CHIP, ui_home.SEARCH_FIELD, ui_home.MENU, ui_home.MENU_PANEL]
        wanted += [control for tab in ui_home.TABS for control in tab[1:3]]
        wanted += list(ui_home.BADGE_IMAGES + ui_home.BADGE_LABELS) + list(ui_home.SEARCH_CHIPS)
        wanted += list(ui_home.MENU_ITEMS) + [chevron for pair in ui_home.CHEVRONS.values() for chevron in pair]
        wanted += [control for index in range(ui_home.SLOTS) for control in ui_home.slot_ids(index)]
        for control in wanted:
            self.assertIn(str(control), ids)

    def test_generator_output_is_current(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            skin_home = importlib.import_module("skin_home")
        finally:
            sys.path.pop(0)
        self.assertEqual((SKIN / "1080i/SiloHome.xml").read_text(encoding="utf-8"), skin_home.window())


if __name__ == "__main__":
    unittest.main()
