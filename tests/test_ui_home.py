"""Home window behaviour with Kodi stubbed, plus checks on the generated SiloHome.xml."""

import importlib
import json
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


class FakeStore(dict):
    """The session store's get/set interface over a dict."""

    def set(self, key, value):
        self[key] = value


SERVER = "https://example.invalid"


def make_application(sections=None, libraries=None, store=None, options=None, requests=False):
    application = MagicMock()
    application.guard.side_effect = lambda callback: callback()
    application.image.side_effect = lambda item, kind: "https://example.invalid/{}/{}".format(
        item.get("content_id", "person"), kind)
    application.options.return_value = options if options is not None else {"show_plot": True}
    application.requests_enabled.return_value = requests
    application.client.server_url = SERVER
    application.profile = {"id": "p1"}
    application.home_sections.return_value = sections if sections is not None else [
        section("Continue Watching", "continue_watching"), section("Recently Added"), section("Example Picks")]
    libraries = libraries if libraries is not None else {"movie": [{"id": 3, "name": "Films", "type": "movie"}],
                                                        "series": [{"id": 4, "name": "Shows", "type": "series"}]}
    application.video_libraries.side_effect = lambda kind: libraries.get(kind, [])
    application.profile_name = "Example Profile"
    application.profile_avatar = ""
    application.store = FakeStore(store or {})
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
        # Home, Movies, For You and Calendar; Requests is off on this server.
        width = 56 + 24 + 124 + 8 + 148 + 8 + 156 + 8 + 172 + 24 + 56
        window.getControl(ui_home.TAB_CLUSTER).setPosition.assert_called_with((1920 - width) // 2, 0)
        window.getControl(211).controlRight.assert_called_with(window.getControl(203))
        window.getControl(204).controlRight.assert_called_with(window.getControl(ui_home.AVATAR))

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
        window.getControl(613).setPosition.assert_called_with(16, 16)
        self.assertEqual(window.focus, 613)
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

    def test_reverse_order_flips_the_sort_direction(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.onClick(211)
        reverse = len(ui_home.SORTS)
        window.application.dialog.select.return_value = reverse
        window.onClick(ui_home.SORT_CHIP)
        self.assertEqual(window.application.dialog.select.call_args.args[1][-1], "Reverse order: Off")
        window.application.browse.assert_called_with({"type": "movie", "sort": "-title"})
        window.getControl(ui_home.SORT_CHIP).setLabel.assert_called_with("Sort: Title (reversed)")
        self.assertEqual(window.properties["silo.rail"], "")  # The A–Z rail only jumps through A→Z.
        # A new sort starts in its natural direction; reversing it again flips that.
        window.application.dialog.select.return_value = 1
        window.onClick(ui_home.SORT_CHIP)
        window.application.browse.assert_called_with({"type": "movie", "sort": "-added_at"})
        window.getControl(ui_home.SORT_CHIP).setLabel.assert_called_with("Sort: Recently added")
        window.application.dialog.select.return_value = reverse
        window.onClick(ui_home.SORT_CHIP)
        window.application.browse.assert_called_with({"type": "movie", "sort": "added_at"})
        window.getControl(ui_home.SORT_CHIP).setLabel.assert_called_with("Sort: Recently added (reversed)")
        self.assertEqual(window.application.dialog.select.call_args.args[1][-1], "Reverse order: Off")
        window.onClick(ui_home.SORT_CHIP)
        self.assertEqual(window.application.dialog.select.call_args.args[1][-1], "Reverse order: On")
        window.application.browse.assert_called_with({"type": "movie", "sort": "-added_at"})

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


CONTEXT = 117
FILTERS = {"genres": ["Comedy", "Drama", "Example Genre"], "studios": [], "networks": ["Example Network"],
           "countries": [], "content_ratings": ["PG", "R"], "technical": {"resolutions": ["2160p", "1080p"]}}


def episode(number, watched=False, **extra):
    item = {"content_id": "episode:{}".format(number), "type": "episode", "title": "Example Episode",
            "series_id": "series:1", "series_title": "Example Show", "season_number": 1,
            "episode_number": number, "overview": "Something happens.",
            "poster_url": "https://example.invalid/still.jpg", "backdrop_url": "https://example.invalid/still.jpg",
            "user_state": {"played": watched}}
    item.update(extra)
    return item


def routes(handlers):
    """client.request stub answering by (method, path); unknown routes return {}."""
    calls = []

    def request(method, path, body=None, params=None):
        calls.append((method, path, body, params))
        answer = handlers.get((method, path), {})
        return answer(body, params) if callable(answer) else answer
    return request, calls


def rail_items(window):
    items = {}
    window.getControl(ui_home.RAIL).getListItem.side_effect = lambda index: items.setdefault(index, MagicMock())
    return items


class TabTests(unittest.TestCase):
    def test_requests_tab_shows_when_the_server_offers_requests(self):
        window = open_window(make_application(requests=True))
        window.getControl(208).setVisible.assert_called_with(True)
        width = 56 + 24 + 124 + 148 + 140 + 156 + 172 + 172 + 5 * 8 + 24 + 56
        # With every tab shown, centring would run into the add-on's name; the row starts clear of it.
        window.getControl(ui_home.TAB_CLUSTER).setPosition.assert_called_with(
            max((1920 - width) // 2, ui_home.TABS_MIN_LEFT), 0)
        self.assertGreaterEqual(window.cluster_left, ui_home.TABS_MIN_LEFT)
        self.assertLessEqual(window.cluster_left + width, 1776 - 24)  # still clear of the avatar
        window.getControl(212).controlRight.assert_called_with(window.getControl(203))
        window.getControl(205).controlRight.assert_called_with(window.getControl(ui_home.AVATAR))

    def test_requests_tab_hidden_by_default(self):
        window = open_window()
        window.getControl(208).setVisible.assert_called_with(False)
        window.getControl(205).setVisible.assert_called_with(False)

    def test_screen_tabs_open_modally_and_home_stays_selected(self):
        for control_id, name in ((203, "for_you"), (204, "calendar"), (205, "requests")):
            window = open_window(make_application(requests=True))
            window.application.browse.return_value = page(1)
            window.onClick(211)
            window.application.home_sections.reset_mock()
            window.onClick(control_id)
            window.application.open_screen.assert_called_once_with(name)
            self.assertEqual(window.properties["silo.tab"], "home")
            self.assertEqual(window.focus, control_id)
            window.application.home_sections.assert_called()  # Rows refresh on return.


class LibraryPillTests(unittest.TestCase):
    def libraries(self, count):
        return {"movie": [{"id": 3 + index, "name": "Films {}".format(index), "type": "movies"}
                          for index in range(count)], "series": [{"id": 9, "name": "Shows", "type": "series"}]}

    def test_down_on_a_library_tab_opens_the_dropdown_under_it(self):
        window = open_window()
        window.focus = 211
        window.onAction(Action(DOWN))
        self.assertEqual(window.pill_open, "movie")
        self.assertEqual(window.focus, 642)
        self.assertEqual(window.properties["silo.pill"], "movie")
        window.getControl(ui_home.PILL).setVisible.assert_called_with(True)
        left = window.cluster_left + window.tab_lefts["movies"]
        window.getControl(ui_home.PILL).setPosition.assert_called_with(left, ui_home.PILL_TOP)

    def test_browse_shows_the_grid(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.focus = 212
        window.onAction(Action(DOWN))
        window.onClick(643)
        self.assertEqual(window.properties["silo.tab"], "series")
        self.assertEqual(window.focus, ui_home.LIBRARY_CHIP)
        self.assertIsNone(window.pill_open)
        window.getControl(ui_home.PILL).setVisible.assert_called_with(False)

    def test_recommended_opens_library_sections_for_the_only_library(self):
        window = open_window()
        window.focus = 211
        window.onAction(Action(DOWN))
        window.onClick(642)
        window.application.open_screen.assert_called_once_with(
            "library_sections", library={"id": 3, "name": "Films", "type": "movie"})
        window.application.dialog.select.assert_not_called()
        self.assertEqual(window.focus, 211)
        self.assertEqual(window.properties["silo.tab"], "home")

    def test_recommended_asks_which_library_when_there_are_several(self):
        window = open_window(make_application(libraries=self.libraries(2)))
        window.application.dialog.select.return_value = 1
        window.focus = 211
        window.onAction(Action(DOWN))
        window.onClick(642)
        self.assertEqual(window.application.dialog.select.call_args.args[1], ["Films 0", "Films 1"])
        window.application.open_screen.assert_called_once_with("library_sections",
                                                               library=self.libraries(2)["movie"][1])

    def test_collections_can_cover_all_libraries(self):
        window = open_window(make_application(libraries=self.libraries(2)))
        window.application.dialog.select.return_value = 0
        window.focus = 211
        window.onAction(Action(DOWN))
        window.onClick(644)
        self.assertEqual(window.application.dialog.select.call_args.args[1], ["All libraries", "Films 0", "Films 1"])
        window.application.open_screen.assert_called_once_with("collections", library=None)

    def test_cancelled_library_choice_opens_nothing(self):
        window = open_window(make_application(libraries=self.libraries(2)))
        window.application.dialog.select.return_value = -1
        window.focus = 211
        window.onAction(Action(DOWN))
        window.onClick(644)
        window.application.open_screen.assert_not_called()

    def test_back_left_and_up_close_the_dropdown(self):
        for action in (BACK, LEFT, UP):
            window = open_window()
            window.focus = 211
            window.onAction(Action(DOWN))
            window.onAction(Action(action))
            self.assertIsNone(window.pill_open)
            self.assertEqual(window.focus, 211)
            self.assertFalse(window.closed)


class ProfileMenuTests(unittest.TestCase):
    def test_menu_rows_follow_android_order(self):
        window = open_window()
        window.onClick(ui_home.AVATAR)
        for index, control in enumerate((610, 613, 614, 615, 611, 616, 617, 612)):
            window.getControl(control).setPosition.assert_called_with(16, 16 + 72 * index)
        window.getControl(ui_home.MENU_PANEL).setHeight.assert_called_with(32 + 72 * 8 - 8)

    def test_switch_server_result(self):
        window = open_window()
        window.onClick(ui_home.AVATAR)
        window.onClick(617)
        self.assertEqual(window.result, "server")
        self.assertTrue(window.closed)

    def test_personal_lists_open_their_screen(self):
        for control_id, kind in ((613, "watchlist"), (614, "favorites"), (615, "history")):
            window = open_window()
            window.onClick(ui_home.AVATAR)
            window.onClick(control_id)
            window.application.open_screen.assert_called_once_with("personal", kind=kind)
            self.assertFalse(window.menu_open)
            self.assertFalse(window.closed)
            self.assertEqual(window.focus, ui_home.AVATAR)


class HomeRowEditorTests(unittest.TestCase):
    def test_move_and_hide_are_saved_per_server_and_profile(self):
        window = open_window()
        # Recently Added -> Move up; Example Picks -> Hide (its actions are Move up, Hide); close.
        window.application.dialog.select.side_effect = [1, 0, 2, 1, -1]
        window.onClick(ui_home.AVATAR)
        window.onClick(616)
        saved = window.application.store["home_rows"]["https://example.invalid|p1"]
        self.assertEqual(saved, {"order": ["recently added", "continue watching", "example picks"],
                                 "hidden": ["example picks"]})
        self.assertEqual([section["title"] for section in window.sections], ["Recently Added", "Continue Watching"])
        self.assertEqual(window.focus, ui_home.slot_ids(0)[4])
        labels = window.application.dialog.select.call_args_list[2].args[1]
        self.assertEqual(labels[-1], "Reset to default")

    def test_saved_choices_apply_when_rows_load(self):
        store = {"home_rows": {"https://example.invalid|p1": {"order": ["example picks"], "hidden": ["recently added"]},
                               "https://example.invalid|p2": {"hidden": ["example picks"]}}}
        window = open_window(make_application(store=store))
        self.assertEqual([section["title"] for section in window.sections], ["Example Picks", "Continue Watching"])

    def test_reset_restores_server_order(self):
        saved = {"order": ["example picks"], "hidden": ["recently added"]}
        window = open_window(make_application(store={"home_rows": {"https://example.invalid|p1": saved}}))
        window.application.dialog.select.side_effect = [3, -1]
        window.edit_home_rows()
        self.assertEqual(window.application.store["home_rows"]["https://example.invalid|p1"],
                         {"order": [], "hidden": []})
        self.assertEqual(len(window.sections), 3)

    def test_cancel_saves_nothing(self):
        window = open_window()
        window.application.dialog.select.return_value = -1
        window.edit_home_rows()
        self.assertNotIn("home_rows", window.application.store)

    def test_new_server_rows_follow_saved_ones(self):
        sections = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertEqual([section["id"] for section in ui_home.apply_row_prefs(sections, {"order": ["c", "a"]})],
                         ["c", "a", "b"])


class FilterTests(unittest.TestCase):
    def open_movies(self, filters=None):
        window = open_window()
        request, calls = routes({("GET", "/catalog/filters"): filters or FILTERS})
        window.application.client.request.side_effect = request
        window.application.browse.return_value = page(3)
        window.onClick(211)
        return window, calls

    def test_facets_lower_to_rule_groups_and_reload(self):
        window, calls = self.open_movies()
        dialog = window.application.dialog
        # Genre, Decade, Watch status -> Unwatched, then close.
        dialog.select.side_effect = [0, 1, 2, 1, -1]
        dialog.multiselect.side_effect = [[0, 2], [1]]
        window.onClick(ui_home.FILTER_CHIP)
        params = window.application.browse.call_args.args[0]
        self.assertEqual(json.loads(params["groups"]), [
            {"match": "any", "rules": [{"field": "genre", "op": "contains", "value": "Comedy"},
                                       {"field": "genre", "op": "contains", "value": "Example Genre"}]},
            {"match": "any", "rules": [{"field": "decade", "op": "is", "value": 2010}]},
            {"match": "all", "rules": [{"field": "watched", "op": "is", "value": False}]}])
        self.assertEqual(window.properties["silo.filter_count"], "3")
        window.getControl(ui_home.CLEAR_CHIP).setVisible.assert_called_with(True)
        window.getControl(ui_home.FILTER_BADGE).setPosition.assert_called()
        self.assertEqual([call for call in calls if call[1] == "/catalog/filters"],
                         [("GET", "/catalog/filters", None, {"type": "movie"})])
        labels = dialog.select.call_args_list[4].args[1]
        self.assertEqual(labels[:3], ["Genre (2)", "Decade (1)", "Watch status (1)"])
        self.assertEqual(labels[-1], "Clear filters")

    def test_facets_without_values_are_not_offered(self):
        window, _calls = self.open_movies()
        window.application.dialog.select.return_value = -1
        window.onClick(ui_home.FILTER_CHIP)
        self.assertEqual(window.application.dialog.select.call_args.args[1], [
            "Genre", "Decade", "Watch status", "Content rating", "Resolution", "Dynamic range", "Network"])
        self.assertEqual(window.application.browse.call_count, 1)

    def test_clear_filters_chip(self):
        window, _calls = self.open_movies()
        window.filters = {"genre": ["Drama"]}
        window.onClick(ui_home.CLEAR_CHIP)
        window.application.browse.assert_called_with({"type": "movie", "sort": "title"})
        self.assertEqual(window.properties["silo.filter_count"], "")
        window.getControl(ui_home.CLEAR_CHIP).setVisible.assert_called_with(False)
        self.assertEqual(window.focus, ui_home.FILTER_CHIP)

    def test_other_facets_lower_like_android(self):
        groups = ui_home.filter_groups({"resolution": ["2160p"], "dynamic_range": ["dolby_vision", "hdr"],
                                        "content_rating": ["R", "PG"], "watch_status": ["watchlist"]})
        self.assertEqual(groups, [
            {"match": "any", "rules": [{"field": "content_rating", "op": "is", "value": "PG"},
                                       {"field": "content_rating", "op": "is", "value": "R"}]},
            {"match": "any", "rules": [{"field": "resolution", "op": "is", "value": "2160p"}]},
            {"match": "any", "rules": [{"field": "hdr", "op": "is", "value": True},
                                       {"field": "dolby_vision", "op": "is", "value": True}]},
            {"match": "all", "rules": [{"field": "in_watchlist", "op": "is", "value": True}]}])

    def test_long_lists_search_the_facet_first(self):
        studios = ["Example Studio {}".format(index) for index in range(70)]
        window, calls = self.open_movies(dict(FILTERS, studios=studios))
        request, calls = routes({("GET", "/catalog/filters"): dict(FILTERS, studios=studios),
                                 ("GET", "/catalog/filters/search"): {
                                     "matches": [], "values": [{"value": "Example Studio 7", "count": 3}]}})
        window.application.client.request.side_effect = request
        window.filter_options_cache = {}
        dialog = window.application.dialog
        dialog.select.side_effect = [6, -1]  # Studio is listed seventh.
        dialog.input.return_value = "seven"
        dialog.multiselect.return_value = [0]
        window.onClick(ui_home.FILTER_CHIP)
        search = next(call for call in calls if call[1] == "/catalog/filters/search")
        self.assertEqual(search[3], {"type": "movie", "facet": "studio", "q": "seven", "limit": 100})
        self.assertEqual(dialog.multiselect.call_args.args[1], ["Example Studio 7"])
        self.assertEqual(window.filters, {"studio": ["Example Studio 7"]})

    def test_library_scope_reaches_the_filters_request(self):
        window, calls = self.open_movies()
        window.library = {"id": 3, "name": "Films"}
        window.application.dialog.select.return_value = -1
        window.onClick(ui_home.FILTER_CHIP)
        self.assertIn(("GET", "/catalog/filters", None, {"type": "movie", "library_id": 3}), calls)


class SortAndShuffleTests(unittest.TestCase):
    def test_sort_offers_server_fields(self):
        window = open_window()
        window.application.browse.return_value = page(2)
        window.onClick(211)
        window.application.dialog.select.return_value = 4
        window.onClick(ui_home.SORT_CHIP)
        window.application.browse.assert_called_with({"type": "movie", "sort": "-rating_imdb"})
        self.assertEqual(window.application.dialog.select.call_args.args[1], [
            "Title A–Z", "Recently added", "Release date", "Year", "Rating", "Runtime", "Recently watched",
            "Most played", "Reverse order: Off"])
        # The A–Z rail only makes sense in title order.
        self.assertEqual(window.properties["silo.rail"], "")
        window.getControl(ui_home.RAIL_GROUP).setVisible.assert_called_with(False)

    def test_shuffle_chip_shuffles_the_library(self):
        stub = types.ModuleType("svc_shuffle")
        stub.play_shuffle = MagicMock()
        with patch.dict(sys.modules, {"resources.lib.silo.svc_shuffle": stub}):
            window = open_window()
            window.application.browse.return_value = page(2)
            window.onClick(211)
            window.onClick(ui_home.SHUFFLE_CHIP)
            stub.play_shuffle.assert_called_once_with(window.application, type="movie", library_id=None)
            window.library = {"id": 3, "name": "Films"}
            window.onClick(ui_home.SHUFFLE_CHIP)
            stub.play_shuffle.assert_called_with(window.application, type="movie", library_id=3)
        self.assertEqual(window.focus, ui_home.SHUFFLE_CHIP)


class AlphabetRailTests(unittest.TestCase):
    def open_movies(self, letters=None):
        window = open_window()
        items = rail_items(window)
        request, calls = routes({("POST", "/catalog/letters"): letters if letters is not None else {
            "total": 60, "letters": [{"letter": "#", "count": 2, "position": 0},
                                     {"letter": "B", "count": 58, "position": 2}], "runs": []}})
        window.application.client.request.side_effect = request
        window.application.browse.return_value = page(30, cursor="next")
        window.onClick(211)
        return window, items, calls

    def test_rail_shows_in_title_order_and_dims_empty_letters(self):
        window, items, calls = self.open_movies()
        self.assertEqual(window.properties["silo.rail"], "true")
        self.assertIn(("POST", "/catalog/letters", {"type": "movie", "order": "asc"}, None), calls)
        letters = ui_home.RAIL_LETTERS
        items[letters.index("B")].setProperty.assert_called_with("silo.has", "true")
        items[letters.index("C")].setProperty.assert_called_with("silo.has", "")
        items[letters.index("All")].setProperty.assert_called_with("silo.has", "true")

    def test_letter_jump_seeks_and_focuses_the_grid(self):
        window, _items, _calls = self.open_movies()
        window.getControl(ui_home.RAIL).getSelectedPosition.return_value = ui_home.RAIL_LETTERS.index("B")
        window.onClick(ui_home.RAIL)
        window.application.browse.assert_called_with({"type": "movie", "sort": "title", "seek": 2})
        self.assertEqual(window.focus, ui_home.GRID)
        window.getControl(ui_home.GRID).selectItem.assert_called_with(0)
        # Later pages continue from the cursor without seeking again.
        window.getControl(ui_home.GRID).getSelectedPosition.return_value = 25
        window.onAction(Action(DOWN))
        window.application.browse.assert_called_with({"type": "movie", "sort": "title"}, "next")

    def test_letters_without_titles_do_nothing_and_all_returns_to_the_start(self):
        window, _items, _calls = self.open_movies()
        window.getControl(ui_home.RAIL).getSelectedPosition.return_value = ui_home.RAIL_LETTERS.index("Q")
        window.onClick(ui_home.RAIL)
        self.assertEqual(window.application.browse.call_count, 1)
        window.getControl(ui_home.RAIL).getSelectedPosition.return_value = 0
        window.onClick(ui_home.RAIL)
        window.application.browse.assert_called_with({"type": "movie", "sort": "title"})

    def test_letters_follow_library_and_filters(self):
        window, _items, calls = self.open_movies()
        window.library = {"id": 3, "name": "Films"}
        window.filters = {"genre": ["Drama"]}
        window.reload_filtered()
        body = [call[2] for call in calls if call[1] == "/catalog/letters"][-1]
        self.assertEqual(body, {"type": "movie", "order": "asc", "library_id": "3", "groups": [
            {"match": "any", "rules": [{"field": "genre", "op": "contains", "value": "Drama"}]}]})


class PeopleRowTests(unittest.TestCase):
    PERSON = {"id": "7", "name": "Example Person", "photo_url": "https://example.invalid/person.jpg"}

    def search(self, people):
        window = open_window()
        request, calls = routes({("GET", "/catalog/people"): people})
        window.application.client.request.side_effect = request
        window.application.browse.return_value = page(2)
        window.onClick(ui_home.SEARCH_BUTTON)
        window.application.dialog.input.return_value = "example"
        window.onClick(ui_home.SEARCH_FIELD)
        return window, calls

    def test_people_row_sits_above_titles(self):
        window, calls = self.search({"items": [self.PERSON]})
        self.assertIn(("GET", "/catalog/people", None, {"q": "example", "limit": 20, "media_scope": "video"}), calls)
        self.assertEqual(window.properties["silo.people"], "true")
        window.getControl(ui_home.PEOPLE_GROUP).setVisible.assert_called_with(True)
        top = ui_home.SEARCH_GRID_TOP + ui_home.PEOPLE_HEIGHT
        window.getControl(ui_home.GRID).setPosition.assert_called_with(ui_home.GRID_LEFT, top)
        window.getControl(530).controlDown.assert_called_with(window.getControl(ui_home.PEOPLE_LIST))
        window.getControl(ui_home.GRID).controlUp.assert_called_with(window.getControl(ui_home.PEOPLE_LIST))
        self.assertEqual(len(window.getControl(ui_home.PEOPLE_LIST).addItems.call_args.args[0]), 1)

    def test_selecting_a_person_opens_their_page(self):
        window, _calls = self.search({"items": [self.PERSON]})
        window.getControl(ui_home.PEOPLE_LIST).getSelectedPosition.return_value = 0
        window.onClick(ui_home.PEOPLE_LIST)
        window.application.open_screen.assert_called_once_with("person", person_id="7", name="Example Person")
        self.assertEqual(window.focus, ui_home.PEOPLE_LIST)

    def test_no_people_keeps_the_grid_under_the_chips(self):
        window, _calls = self.search({"items": []})
        self.assertEqual(window.properties["silo.people"], "")
        window.getControl(ui_home.GRID).setPosition.assert_called_with(ui_home.GRID_LEFT, ui_home.SEARCH_GRID_TOP)
        window.getControl(ui_home.GRID).controlUp.assert_called_with(window.getControl(530))

    def test_people_lookup_failure_hides_the_row(self):
        window = open_window()
        window.application.client.request.side_effect = OSError("offline")
        window.application.browse.return_value = page(2)
        window.onClick(ui_home.SEARCH_BUTTON)
        window.application.dialog.input.return_value = "example"
        window.onClick(ui_home.SEARCH_FIELD)
        self.assertEqual(window.properties["silo.people"], "")
        self.assertEqual(len(window.grid_items), 2)


class ContextMenuTests(unittest.TestCase):
    def test_continue_watching_card_can_be_removed(self):
        sections = [section("Continue Watching", "continue_watching"), section("Recently Added")]
        sections[0]["items"][0].update(item_source="in_progress", progress_updated_at="2026-01-02T03:04:05Z")
        window = open_window(make_application(sections))
        request, calls = routes({})
        window.application.client.request.side_effect = request
        window.application.dialog.contextmenu.return_value = 3
        window.application.home_sections.reset_mock()
        window.onAction(Action(CONTEXT))
        self.assertEqual(window.application.dialog.contextmenu.call_args.args[0], [
            "Mark as watched", "Add to watchlist", "Add to favorites", "Remove from Continue Watching"])
        self.assertEqual(calls, [("PUT", "/home/dismissals/continue_watching/movie%3A0",
                                  {"progress_updated_at": "2026-01-02T03:04:05Z"}, None)])
        window.application.home_sections.assert_called_once()

    def test_next_up_dismissal_names_the_series(self):
        sections = [section("Continue Watching", "continue_watching")]
        sections[0]["items"][0] = episode(4, item_source="next_up")
        window = open_window(make_application(sections))
        request, calls = routes({})
        window.application.client.request.side_effect = request
        window.application.dialog.contextmenu.return_value = 3
        window.onAction(Action(CONTEXT))
        labels = window.application.dialog.contextmenu.call_args.args[0]
        self.assertEqual(labels[3:], ["Remove from Next Up", "Go to series"])
        self.assertEqual(calls, [("PUT", "/home/dismissals/next_up/episode%3A4", {"series_id": "series:1"}, None)])

    def test_marks_toggle_from_the_current_state(self):
        sections = [section("Recently Added")]
        sections[0]["items"][0]["user_state"] = {"played": True, "in_watchlist": True, "is_favorite": False}
        window = open_window(make_application(sections))
        request, calls = routes({})
        window.application.client.request.side_effect = request
        labels = None
        for choice, expected in ((0, ("DELETE", "/watched/movie%3A0")), (1, ("DELETE", "/watchlist/movie%3A0")),
                                 (2, ("PUT", "/favorites/movie%3A0"))):
            sections[0]["items"][0]["user_state"] = {"played": True, "in_watchlist": True, "is_favorite": False}
            window.application.dialog.contextmenu.return_value = choice
            window.focus = ui_home.slot_ids(0)[4]
            window.onAction(Action(CONTEXT))
            labels = window.application.dialog.contextmenu.call_args.args[0]
            self.assertEqual(calls[-1][:2], expected)
        self.assertEqual(labels, ["Mark as unwatched", "Remove from watchlist", "Add to favorites"])
        self.assertEqual(window.focus, ui_home.slot_ids(0)[4])

    def test_poster_rows_do_not_offer_removal(self):
        sections = [section("Recently Added")]
        sections[0]["items"][0]["item_source"] = "in_progress"
        window = open_window(make_application(sections))
        window.application.dialog.contextmenu.return_value = -1
        window.onAction(Action(CONTEXT))
        self.assertEqual(len(window.application.dialog.contextmenu.call_args.args[0]), 3)

    def test_go_to_series_opens_the_series(self):
        sections = [section("Recently Added")]
        sections[0]["items"][0] = episode(2)
        window = open_window(make_application(sections))
        window.application.dialog.contextmenu.return_value = 3
        window.onAction(Action(CONTEXT))
        window.application.open_item.assert_called_once_with({"content_id": "series:1", "type": "series"})

    def test_grid_cards_update_in_place(self):
        window = open_window()
        window.application.browse.return_value = page(3)
        window.onClick(211)
        request, calls = routes({})
        window.application.client.request.side_effect = request
        window.focus = ui_home.GRID
        window.getControl(ui_home.GRID).getSelectedPosition.return_value = 1
        window.application.dialog.contextmenu.return_value = 0
        window.onAction(Action(CONTEXT))
        self.assertEqual(calls[-1][:2], ("POST", "/watched/movie%3A1"))
        self.assertTrue(window.grid_items[1]["user_state"]["played"])
        window.getControl(ui_home.GRID).getListItem.return_value.setProperty.assert_any_call("silo.watched", "true")

    def test_menu_key_off_a_card_does_nothing(self):
        window = open_window()
        window.focus = 211
        window.onAction(Action(CONTEXT))
        window.application.dialog.contextmenu.assert_not_called()


class RefreshTests(unittest.TestCase):
    def test_rows_reload_after_details_and_focus_follows_the_card(self):
        application = make_application()
        window = open_window(application)
        poster = ui_home.slot_ids(1)[4]
        window.getControl(poster).getSelectedPosition.return_value = 1
        # The card moved to the front of its row while the details page was open.
        application.home_sections.return_value = [section("Continue Watching", "continue_watching"),
                                                  section("Recently Added", count=3, start=1)]
        window.onClick(poster)
        self.assertEqual(window.focus, poster)
        window.getControl(poster).selectItem.assert_called_with(0)
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 1")

    def test_removed_row_falls_back_to_a_nearby_row(self):
        application = make_application()
        window = open_window(application)
        poster = ui_home.slot_ids(2)[4]
        application.home_sections.return_value = [section("Continue Watching", "continue_watching")]
        window.onClick(poster)
        self.assertEqual(window.focus, ui_home.slot_ids(0)[3])

    def test_away_from_home_rows_reload_when_home_returns(self):
        window = open_window()
        window.application.browse.return_value = page(3)
        window.onClick(211)
        window.application.home_sections.reset_mock()
        window.onClick(ui_home.GRID)
        window.application.home_sections.assert_not_called()
        self.assertEqual(window.focus, ui_home.GRID)
        window.onAction(Action(BACK))
        window.application.home_sections.assert_called_once()


class SpoilerTests(unittest.TestCase):
    def window(self, hide=True, item=None):
        sections = [{"id": "next", "title": "Continue Watching", "section_type": "continue_watching",
                     "items": [item or episode(3)]}]
        application = make_application(sections, options={"show_plot": True, "hide_spoilers": hide})
        application.client.item.return_value = {"content_id": "series:1", "type": "series"}
        return open_window(application)

    def test_unwatched_episode_hides_synopsis_and_uses_series_art(self):
        window = self.window()
        self.assertEqual(window.properties["silo.hero.synopsis"], "")
        self.assertEqual(window.properties["silo.hero.backdrop"], "https://example.invalid/series:1/backdrop")
        window.getControl(ui_home.HERO_GROUP).setPosition.assert_called_with(0, ui_home.NO_SYNOPSIS_SHIFT)
        art = window.card_image(episode(3), "poster")
        self.assertEqual(art, "https://example.invalid/series:1/poster")
        window.application.client.item.assert_called_once_with("series:1")  # Cached per series.

    def test_watched_episodes_and_disabled_setting_show_everything(self):
        for window in (self.window(item=episode(3, watched=True)), self.window(hide=False)):
            self.assertEqual(window.properties["silo.hero.synopsis"], "Something happens.")
            self.assertEqual(window.properties["silo.hero.backdrop"], "https://example.invalid/episode:3/backdrop")
            window.application.client.item.assert_not_called()

    def test_art_marked_as_series_art_is_kept(self):
        window = self.window(item=episode(3, backdrop_is_episode_still=False))
        self.assertEqual(window.properties["silo.hero.backdrop"], "https://example.invalid/episode:3/backdrop")
        window.application.client.item.assert_not_called()

    def test_movies_are_never_spoilers(self):
        window = self.window()
        self.assertEqual(window.card_image(movie(1), "backdrop"), "https://example.invalid/movie:1/backdrop")


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
        wanted += list(ui_home.CHIPS) + [ui_home.FILTER_BADGE, 526, ui_home.RAIL, ui_home.RAIL_GROUP,
                                         ui_home.PEOPLE_GROUP, ui_home.PEOPLE_LIST, ui_home.PILL, ui_home.PILL_PANEL]
        wanted += list(ui_home.PILL_ITEMS)
        wanted += [control for index in range(ui_home.SLOTS) for control in ui_home.slot_ids(index)]
        for control in wanted:
            self.assertIn(str(control), ids)

    def test_fonts_are_estuary_names(self):
        allowed = {"font_MainMenu", "font52_title", "font36_title", "font30_title", "font25_title", "font27",
                   "font12", "font_flag", "font10"}
        for element in self.tree.iter("font"):
            self.assertIn(element.text, allowed)

    def test_menu_rows_are_in_android_order(self):
        menu = next(control for control in self.tree.iter("control") if control.get("id") == "600")
        labels = [button.findtext("label") for button in menu.iter("control") if button.get("type") == "button"]
        self.assertEqual(labels, ["Switch profile", "Watchlist", "Favorites", "History", "Settings",
                                  "Edit home rows", "Switch server", "Sign out"])

    def control(self, control_id):
        return next(control for control in self.tree.iter("control") if control.get("id") == str(control_id))

    def test_tabs_are_in_android_order_and_draw_their_name_once(self):
        names = []
        for _key, button_id, group_id, _width in ui_home.TABS:
            group = self.control(group_id)
            # The button draws no text: a Kodi button pads and scrolls its label, and a second
            # overlay label for the selected tab showed through behind it.
            self.assertEqual(self.control(button_id).findtext("label") or "", "", button_id)
            captions = [node for node in group.iter("control") if node.get("type") == "label"]
            self.assertEqual(len(captions), 3, group_id)  # Idle, selected and focused.
            self.assertEqual(len({node.findtext("label") for node in captions}), 1, group_id)
            for caption in captions:
                self.assertNotEqual(caption.findtext("scroll"), "true")
            names.append(captions[0].findtext("label"))
            visible = [node.findtext("visible") for node in captions]
            self.assertIn("Control.HasFocus({})".format(button_id), visible)
        self.assertEqual(names, ["Home", "Movies", "Series", "For You", "Calendar", "Requests"])

    def test_every_top_bar_shows_the_clock_top_right(self):
        for name in ("SiloHome.xml", "SiloBrowseRows.xml", "SiloBrowseGrid.xml", "SiloPerson.xml",
                     "SiloRequests.xml"):
            tree = ET.parse(SKIN / "1080i" / name)
            clocks = [node for node in tree.iter("control")
                      if node.get("type") == "label" and node.findtext("label") == "$INFO[System.Time]"]
            self.assertEqual(len(clocks), 1, name)
            self.assertEqual(clocks[0].findtext("align"), "right", name)
            right = int(clocks[0].findtext("left")) + int(clocks[0].findtext("width"))
            self.assertGreaterEqual(right, 1700, name)

    def test_focused_avatar_gets_a_white_disc_like_the_tabs(self):
        avatar = next(group for group in self.tree.iter("control")
                      if any(child.get("id") == str(ui_home.AVATAR) for child in group))
        focused = "Control.HasFocus({})".format(ui_home.AVATAR)
        discs = [node for node in avatar if node.get("type") == "image" and node.findtext("texture") == "circle.png"
                 and node.findtext("visible") == focused]
        self.assertEqual(len(discs), 1)
        self.assertEqual(discs[0].find("texture").get("colordiffuse"), "FFEDEDED")
        initials = [node for node in avatar if node.get("type") == "label"]
        self.assertEqual({node.findtext("textcolor") for node in initials
                          if node.findtext("visible") == focused}, {"FF000000"})

    def test_generator_output_is_current(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            skin_home = importlib.import_module("skin_home")
        finally:
            sys.path.pop(0)
        self.assertEqual((SKIN / "1080i/SiloHome.xml").read_text(encoding="utf-8"), skin_home.window())


if __name__ == "__main__":
    unittest.main()
