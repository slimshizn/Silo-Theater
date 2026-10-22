"""Browse screens with Kodi stubbed, the svc_browse readers, and checks on the generated XML."""

import datetime
import importlib
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default"
WINDOWS = ("SiloBrowseRows.xml", "SiloBrowseGrid.xml", "SiloPerson.xml")


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


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2, self.art, self.properties = label, label2, {}, {}

    def setLabel(self, value):
        self.label = value

    def setLabel2(self, value):
        self.label2 = value

    def setArt(self, art):
        self.art = art

    def setProperty(self, key, value):
        self.properties[key] = value

    def getVideoInfoTag(self):
        return MagicMock()


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=FakeListItem),
}
with patch.dict(sys.modules, KODI_MODULES):
    for name in ("ui_common", "ui_browse"):
        sys.modules.pop("resources.lib.silo." + name, None)
    ui_browse = importlib.import_module("resources.lib.silo.ui_browse")
    svc_browse = importlib.import_module("resources.lib.silo.svc_browse")
    for name in ("ui_common", "ui_browse"):
        # Other test modules import these with their own stubs.
        sys.modules.pop("resources.lib.silo." + name, None)


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


UP, DOWN, LEFT, RIGHT, BACK, MENU = 3, 4, 1, 2, 10, 117


def movie(number, **extra):
    item = {"content_id": "movie:{}".format(number), "type": "movie", "title": "Example Movie {}".format(number),
            "year": 2020 + number % 5, "overview": "An example synopsis.",
            "poster_url": "https://example.invalid/p{}.jpg".format(number)}
    item.update(extra)
    return item


def episode(number, watched=False, **extra):
    item = {"content_id": "episode:{}".format(number), "type": "episode", "title": "Example Episode Title",
            "series_title": "Example Show", "season_number": 1, "episode_number": number,
            "overview": "What happens next.", "poster_url": "https://example.invalid/still.jpg",
            "poster_is_episode_still": True, "user_state": {"played": watched}}
    item.update(extra)
    return item


class FakeClient:
    """Answers GET/POST by path; records every call."""

    def __init__(self, routes=None):
        self.routes = routes or {}
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, params, body))
        answer = self.routes.get((method, path), self.routes.get(path))
        if isinstance(answer, Exception):
            raise answer
        return answer(params) if callable(answer) else answer

    def paths(self, method="GET"):
        return [call[1] for call in self.calls if call[0] == method]


def make_application(client, options=None):
    application = MagicMock()
    application.client = client
    application.guard.side_effect = guard
    application.image.side_effect = lambda item, kind: item.get(
        {"poster": "poster_url", "backdrop": "backdrop_url", "photo": "photo_url"}.get(kind, ""), "") or ""
    application.options.return_value = dict({"show_plot": True, "page_size": 50, "hide_spoilers": False},
                                            **(options or {}))
    application.addon.getAddonInfo.return_value = str(ROOT)
    application.store = {}
    return application


def guard(callback):
    try:
        return callback()
    except Exception:  # The real guard shows a dialog and returns None.
        return None


def run(function, *args, **kwargs):
    shown = []

    def do_modal(window):
        if hasattr(window, "hero_debouncer"):
            window.hero_debouncer = MagicMock()
        window.onInit()
        shown.append(window)

    with patch.object(FakeWindow, "doModal", do_modal, create=True):
        function(*args, **kwargs)
    return shown


def items_added(window, control_id):
    return [item for call in window.getControl(control_id).addItems.call_args_list for item in call.args[0]]


def page(items, cursor=None, total=None):
    return {"items": items, "page": {"has_more": bool(cursor), "next_cursor": cursor}, "total": total}


# ---------------------------------------------------------------- For You

class ForYouTests(unittest.TestCase):
    def discover(self):
        return {"items": [
            {"type": "cluster", "key": "0", "title": "Because you enjoy Example", "items": [movie(1), movie(2)]},
            {"type": "popular", "title": "Popular", "items": [{"content_id": "book:1", "type": "audiobook"}]},
            {"type": "top_rated", "title": "Top Rated", "items": [movie(3), "junk", {"type": "movie"}]},
        ]}

    def test_rows_come_from_discover_and_skip_non_video(self):
        client = FakeClient({"/recommendations/discover": self.discover()})
        window = run(ui_browse.open_for_you, make_application(client))[0]
        self.assertEqual(client.paths(), ["/recommendations/discover"])
        self.assertEqual([row["title"] for row in window.rows], ["Because you enjoy Example", "Top Rated"])
        group, header, _icon, _wide, poster = ui_browse.slot_ids(0)
        window.getControl(header).setLabel.assert_called_with("Because you enjoy Example")
        self.assertEqual([item.label for item in items_added(window, poster)], ["Example Movie 1", "Example Movie 2"])
        self.assertEqual(window.focus, poster)
        self.assertEqual(window.properties["silo.title"], "For You")
        self.assertEqual(window.properties["silo.hero"], "true")
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 1")
        window.getControl(ui_browse.slot_ids(2)[0]).setVisible.assert_called_with(False)

    def test_hero_follows_focus_and_rows_scroll(self):
        client = FakeClient({"/recommendations/discover": self.discover()})
        window = run(ui_browse.open_for_you, make_application(client))[0]
        window.onAction(Action(DOWN))
        second = ui_browse.slot_ids(1)[4]
        self.assertEqual(window.focus, second)
        window.getControl(ui_browse.slot_ids(1)[0]).setPosition.assert_called_with(0, ui_browse.HERO_ROWS_TOP)
        window.getControl(ui_browse.slot_ids(0)[0]).setVisible.assert_called_with(False)
        window.update_hero()
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 3")
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_browse.slot_ids(0)[4])
        window.getControl(ui_browse.slot_ids(0)[4]).getSelectedPosition.return_value = 1
        window.update_hero()
        self.assertEqual(window.properties["silo.hero.title"], "Example Movie 2")

    def test_card_click_opens_item_and_keeps_focus(self):
        client = FakeClient({"/recommendations/discover": self.discover()})
        application = make_application(client)
        window = run(ui_browse.open_for_you, application)[0]
        poster = ui_browse.slot_ids(0)[4]
        window.getControl(poster).getSelectedPosition.return_value = 1
        window.onClick(poster)
        application.open_item.assert_called_once_with(movie(2))
        self.assertEqual(window.focus, poster)
        self.assertEqual(window.last_focus, poster)

    def test_empty_and_error_states(self):
        window = run(ui_browse.open_for_you, make_application(FakeClient({"/recommendations/discover": {}})))[0]
        self.assertIn("No recommendations yet", window.properties["silo.empty"])
        failing = FakeClient({"/recommendations/discover": RuntimeError("offline")})
        window = run(ui_browse.open_for_you, make_application(failing))[0]
        self.assertEqual(window.properties["silo.empty"], "Recommendations could not be loaded.")

    def test_back_closes(self):
        window = run(ui_browse.open_for_you, make_application(FakeClient({"/recommendations/discover": {}})))[0]
        window.onAction(Action(BACK))
        self.assertTrue(window.closed)


# ---------------------------------------------------------------- calendar

class CalendarTests(unittest.TestCase):
    TODAY = datetime.date(2026, 10, 22)  # A Thursday.

    def response(self, params):
        return {"events": [
            {"date": "2026-10-23", "items": [
                {"content_id": "episode:9", "type": "episode", "title": "Example Show", "episode_title": "Secret",
                 "season_number": 2, "episode_number": 5, "air_time": "21:00", "watched": False, "badges": [],
                 "series_id": "series:1", "poster_url": "https://example.invalid/series.jpg"}]},
            {"date": "2026-10-22", "items": [
                {"content_id": "movie:7", "type": "movie", "title": "Example Movie", "badges": []},
                {"title": "missing id"}]},
            {"date": "2026-10-24", "items": []},
        ]}

    def open(self, client=None, application=None):
        client = client or FakeClient({"/calendar": self.response})
        application = application or make_application(client)
        with patch.object(ui_browse, "today", return_value=self.TODAY), \
                patch.object(ui_browse, "local_timezone", return_value="Europe/Example"):
            window = run(ui_browse.open_calendar, application)[0]
        return window, client, application

    def test_fetches_the_current_week_with_filter_and_zone(self):
        window, client, _application = self.open()
        self.assertEqual(client.calls[0][:3], ("GET", "/calendar", {
            "start": "2026-10-19", "end": "2026-10-25", "filter": "following", "timezone": "Europe/Example"}))
        self.assertEqual([row["title"] for row in window.rows], ["Today", "Tomorrow"])
        today, tomorrow = (items_added(window, ui_browse.slot_ids(index)[4]) for index in range(2))
        self.assertEqual((today[0].label, today[0].label2), ("Example Movie", "Movie"))
        self.assertEqual((tomorrow[0].label, tomorrow[0].label2), ("Example Show", "S2:E5 · 21:00"))
        window.getControl(ui_browse.slot_ids(0)[0]).setPosition.assert_called_with(0, ui_browse.CALENDAR_ROWS_TOP)
        self.assertEqual(window.properties["silo.header"], "true")
        self.assertEqual(window.properties["silo.hero"], "")
        window.getControl(ui_browse.WEEK_BUTTON).setLabel.assert_called_with("‹   Oct 19 – 25   ›")
        window.getControl(ui_browse.FILTER_CHIP).setLabel.assert_called_with("Show: Following")
        self.assertTrue(window.properties["silo.week_hint"].startswith("This week"))

    def test_left_and_right_on_the_week_control_change_weeks(self):
        window, client, _application = self.open()
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_browse.WEEK_BUTTON)
        window.onAction(Action(RIGHT))
        self.assertEqual(client.calls[-1][2]["start"], "2026-10-26")
        self.assertEqual(client.calls[-1][2]["end"], "2026-11-01")
        self.assertEqual(window.focus, ui_browse.WEEK_BUTTON)
        window.getControl(ui_browse.WEEK_BUTTON).setLabel.assert_called_with("‹   Oct 26 – Nov 1   ›")
        window.onAction(Action(LEFT))
        window.onAction(Action(LEFT))
        self.assertEqual(client.calls[-1][2]["start"], "2026-10-12")
        window.onClick(ui_browse.WEEK_BUTTON)  # Select jumps back to this week.
        self.assertEqual(client.calls[-1][2]["start"], "2026-10-19")
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, ui_browse.slot_ids(0)[4])

    def test_filter_choice_reloads(self):
        window, client, application = self.open()
        window.focus = ui_browse.WEEK_BUTTON
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_browse.FILTER_CHIP)
        application.dialog.select.return_value = 2
        window.onClick(ui_browse.FILTER_CHIP)
        self.assertEqual(client.calls[-1][2]["filter"], "all")
        window.getControl(ui_browse.FILTER_CHIP).setLabel.assert_called_with("Show: All")
        calls = len(client.calls)
        application.dialog.select.return_value = -1
        window.onClick(ui_browse.FILTER_CHIP)
        self.assertEqual(len(client.calls), calls)

    def test_empty_week_and_error(self):
        window, _client, _application = self.open(FakeClient({"/calendar": {"events": []}}))
        self.assertEqual(window.properties["silo.empty"], "Nothing scheduled this week")
        self.assertEqual(window.focus, ui_browse.WEEK_BUTTON)
        window, _client, _application = self.open(FakeClient({"/calendar": RuntimeError("offline")}))
        self.assertEqual(window.properties["silo.empty"], "The calendar could not be loaded.")

    def test_click_opens_event(self):
        window, _client, application = self.open()
        window.onClick(ui_browse.slot_ids(1)[4])
        self.assertEqual(application.open_item.call_args.args[0]["content_id"], "episode:9")

    def test_day_headings(self):
        today = self.TODAY
        self.assertEqual(ui_browse.day_heading("2026-10-21", today), "Yesterday")
        self.assertEqual(ui_browse.day_heading("2026-10-25", today), "Sunday, Oct 25")
        self.assertEqual(ui_browse.day_heading("bad", today), "bad")

    def test_air_time_prefers_the_instant(self):
        self.assertEqual(ui_browse.air_time({"air_time": "21:00:00"}), "21:00")
        self.assertEqual(ui_browse.air_time({"air_at": "not a time", "air_time": "20:30"}), "20:30")
        expected = datetime.datetime(2026, 10, 23, 1, 0, tzinfo=datetime.timezone.utc).astimezone().strftime("%H:%M")
        self.assertEqual(ui_browse.air_time({"air_at": "2026-10-23T01:00:00.000Z", "air_time": "21:00"}), expected)


# ---------------------------------------------------------------- collections

class CollectionTests(unittest.TestCase):
    def personal(self):
        return {"items": [{"id": "c1", "name": "Example Rainy Days", "description": "Cozy picks.", "item_count": 2,
                           "poster_url": "https://example.invalid/c1.jpg"}, {"name": "no id"}], "groups": []}

    def test_personal_collections_grid_and_detail(self):
        client = FakeClient({"/collections": self.personal(),
                             "/catalog": lambda params: page([movie(1), {"content_id": "book:1", "type": "ebook"}],
                                                             total=2)})
        application = make_application(client)
        windows = []
        with patch.object(ui_browse, "open_collection",
                          side_effect=lambda app, collection, library=None: windows.append(collection)):
            window = run(ui_browse.open_collections, application)[0]
            self.assertEqual(window.kind, "collection")
            added = items_added(window, ui_browse.GRID)
            self.assertEqual((added[0].label, added[0].label2), ("Example Rainy Days", "2 titles"))
            self.assertEqual(added[0].art["poster"], "https://example.invalid/c1.jpg")
            window.onClick(ui_browse.GRID)
        self.assertEqual(windows[0]["id"], "c1")
        self.assertEqual(windows[0]["kind"], "personal")
        detail = run(ui_browse.open_collection, application, windows[0])[0]
        self.assertEqual(client.calls[-1][1:3], ("/catalog", {"limit": 50, "source": "user_collection",
                                                              "collection_id": "c1"}))
        self.assertEqual(detail.properties["silo.heading"], "Example Rainy Days")
        self.assertEqual(detail.properties["silo.meta"], "2 titles")
        self.assertEqual(detail.properties["silo.overview"], "Cozy picks.")
        detail.getControl(ui_browse.GRID).setPosition.assert_called_with(ui_browse.GRID_LEFT,
                                                                           ui_browse.GRID_TOP_OVERVIEW)
        self.assertEqual([item["content_id"] for item in detail.items], ["movie:1"])
        detail.onClick(ui_browse.GRID)
        application.open_item.assert_called_once_with(movie(1))

    def library_tab(self):
        card = {"id": "k1", "title": "Example Franchise", "poster_url": "", "item_count": 3}
        return {"library_id": "3", "collections": [dict(card, description="Every film.", library_id="3",
                                                        backdrop_url="https://example.invalid/b.jpg")],
                "groups": [{"id": "g", "name": "Franchises", "kind": "admin", "collections": [card]},
                           {"id": "u", "name": "Mine", "kind": "user_collections",
                            "collections": [{"id": "c9", "title": "Example Mine", "item_count": 1,
                                             "creator_profile_id": "p1"}]}],
                "ungrouped": {"sort_order": 9999, "collections": [{"id": "k2", "title": "Example Loose"}]}}

    def test_library_collections_show_groups_as_rows(self):
        client = FakeClient({"/library/3/collections": self.library_tab(),
                             "/library/3/collections/k1/items": lambda params: page([movie(1)], "next"),
                             "/catalog": lambda params: page([movie(2)])})
        application = make_application(client)
        library = {"id": 3, "name": "Example Films"}
        window = run(ui_browse.open_collections, application, library)[0]
        self.assertEqual([row["title"] for row in window.rows], ["Franchises", "Mine", "More collections"])
        poster = ui_browse.slot_ids(0)[4]
        self.assertEqual(items_added(window, poster)[0].label2, "3 titles")
        detail = run(window.open_collection, window.rows[0]["items"][0])
        self.assertEqual(client.calls[-1][1:3], ("/library/3/collections/k1/items", {"limit": 50}))
        self.assertEqual(detail[0].properties["silo.overview"], "Every film.")
        self.assertEqual(detail[0].properties["silo.backdrop"], "https://example.invalid/b.jpg")
        self.assertEqual(detail[0].cursor, "next")
        run(window.open_collection, window.rows[1]["items"][0])
        self.assertEqual(client.calls[-1][1:3], ("/catalog", {"limit": 50, "source": "user_collection",
                                                              "collection_id": "c9", "library_id": 3}))

    def test_raw_collection_without_kind_is_accepted(self):
        client = FakeClient({"/library/3/collections/k1/items": lambda params: page([movie(1)])})
        detail = run(ui_browse.open_collection, make_application(client), {"id": "k1", "title": "Example"},
                     {"id": 3})[0]
        self.assertEqual(client.paths(), ["/library/3/collections/k1/items"])
        self.assertEqual(detail.properties["silo.heading"], "Example")

    def test_empty_and_error(self):
        window = run(ui_browse.open_collections, make_application(FakeClient({"/collections": {"items": []}})))[0]
        self.assertEqual(window.properties["silo.empty"], "No collections yet")
        failing = FakeClient({"/collections": RuntimeError("offline")})
        window = run(ui_browse.open_collections, make_application(failing))[0]
        self.assertEqual(window.properties["silo.empty"], "Collections could not be loaded.")


# ---------------------------------------------------------------- library sections

class LibrarySectionTests(unittest.TestCase):
    def test_sections_become_rows_and_lazy_sections_are_fetched(self):
        client = FakeClient({
            "/library/3/sections": {"sections": [
                {"id": "continue", "section_type": "continue_watching", "title": "Continue Watching",
                 "items": [movie(1, position_seconds=60, duration_seconds=600)], "total_count": 1},
                {"id": "recent", "section_type": "recently_added", "title": "Recently Added", "items": [],
                 "total_count": 4},
                {"id": "none", "section_type": "genre", "title": "Empty", "items": [], "total_count": 0},
            ]},
            "/library/3/sections/recent/items": {"id": "recent", "items": [movie(2), movie(3)]},
        })
        window = run(ui_browse.open_library_sections, make_application(client), {"id": 3, "name": "Example Films"})[0]
        self.assertEqual(client.paths(), ["/library/3/sections", "/library/3/sections/recent/items"])
        self.assertEqual([row["title"] for row in window.rows], ["Continue Watching", "Recently Added"])
        self.assertTrue(window.rows[0]["wide"])
        self.assertEqual(window.focus, ui_browse.slot_ids(0)[3])
        self.assertEqual(window.properties["silo.title"], "Example Films")
        window.getControl(ui_browse.TITLE_PILL).setWidth.assert_called_with(ui_browse.pill_width("Example Films"))
        self.assertEqual(len(items_added(window, ui_browse.slot_ids(1)[4])), 2)

    def test_hide_spoilers_strips_unwatched_episodes(self):
        client = FakeClient({"/library/3/sections": {"sections": [
            {"id": "eps", "section_type": "recently_added", "title": "New Episodes",
             "items": [episode(1), episode(2, watched=True)], "total_count": 2}]}})
        application = make_application(client, {"hide_spoilers": True})
        window = run(ui_browse.open_library_sections, application, {"id": 3})[0]
        hidden, shown = window.rows[0]["items"]
        self.assertEqual((hidden["title"], hidden["overview"]), ("", ""))
        self.assertNotIn("poster_url", hidden)
        self.assertEqual(shown["title"], "Example Episode Title")
        self.assertEqual(window.properties["silo.hero.synopsis"], "")
        self.assertEqual(window.properties["silo.hero.title"], "Example Show")


# ---------------------------------------------------------------- personal lists

class PersonalTests(unittest.TestCase):
    def test_watchlist_grid_pages_and_removes(self):
        pages = {None: page([movie(index) for index in range(14)], "c2", total=15), "c2": page([movie(14)])}
        client = FakeClient({"/watchlist": lambda params: pages[params.get("cursor")],
                             ("DELETE", "/watchlist/movie%3A3"): None})
        application = make_application(client, {"page_size": 14})
        window = run(ui_browse.open_personal, application, "watchlist")[0]
        self.assertEqual(client.calls[0][1:3], ("/watchlist", {"limit": 14}))
        self.assertEqual(window.properties["silo.title"], "Watchlist")
        self.assertEqual(window.properties["silo.meta"], "")
        window.getControl(ui_browse.GRID).setPosition.assert_called_with(ui_browse.GRID_LEFT,
                                                                           ui_browse.GRID_TOP_PLAIN)
        window.focus = ui_browse.GRID
        window.onAction(Action(DOWN))  # Position 0 is far from the end.
        self.assertEqual(len(client.calls), 1)
        window.getControl(ui_browse.GRID).getSelectedPosition.return_value = 5
        window.onAction(Action(DOWN))
        self.assertEqual(client.calls[-1][2], {"limit": 14, "cursor": "c2"})
        self.assertEqual(len(window.items), 15)
        window.onAction(Action(DOWN))  # No cursor left.
        self.assertEqual(len(client.calls), 2)
        window.getControl(ui_browse.GRID).getSelectedPosition.return_value = 3
        application.dialog.contextmenu.return_value = 0
        window.onAction(Action(MENU))
        application.dialog.contextmenu.assert_called_with(["Remove from Watchlist"])
        self.assertEqual(client.calls[-1][:2], ("DELETE", "/watchlist/movie%3A3"))
        self.assertNotIn("movie:3", [item["content_id"] for item in window.items])
        window.getControl(ui_browse.GRID).selectItem.assert_called_with(3)

    def test_cancelled_context_menu_keeps_the_title(self):
        client = FakeClient({"/favorites": page([movie(1)])})
        application = make_application(client)
        window = run(ui_browse.open_personal, application, "favorites")[0]
        window.focus = ui_browse.GRID
        application.dialog.contextmenu.return_value = -1
        window.onAction(Action(MENU))
        self.assertEqual(client.paths("DELETE"), [])
        self.assertEqual(len(window.items), 1)

    def test_favorites_removal_uses_delete(self):
        client = FakeClient({"/favorites": page([movie(1)]), ("DELETE", "/favorites/movie%3A1"): None})
        application = make_application(client)
        window = run(ui_browse.open_personal, application, "favorites")[0]
        window.focus = ui_browse.GRID
        application.dialog.contextmenu.return_value = 0
        window.onAction(Action(MENU))
        self.assertEqual(client.paths("DELETE"), ["/favorites/movie%3A1"])
        self.assertEqual(window.properties["silo.empty"], "No favorites yet")

    def test_history_shows_watched_date_and_removes_with_post(self):
        card = movie(1, watch={"media_item_id": "movie:1", "watched_at": "2025-03-04T05:06:07.000Z",
                               "duration_seconds": 0, "completed": True})
        client = FakeClient({"/history": page([card]), ("POST", "/history/remove"): None})
        application = make_application(client)
        window = run(ui_browse.open_personal, application, "history")[0]
        self.assertEqual(items_added(window, ui_browse.GRID)[0].label2, "Watched Mar 4, 2025")
        window.focus = ui_browse.GRID
        application.dialog.contextmenu.return_value = 0
        application.dialog.yesno.return_value = True
        window.onAction(Action(MENU))
        self.assertEqual(client.calls[-1], ("POST", "/history/remove", None,
                                            {"targets": [{"content_id": "movie:1"}]}))
        self.assertEqual(window.properties["silo.empty"], "Nothing watched yet")

    def test_history_removal_needs_confirmation(self):
        client = FakeClient({"/history": page([movie(1)])})
        application = make_application(client)
        window = run(ui_browse.open_personal, application, "history")[0]
        window.focus = ui_browse.GRID
        application.dialog.contextmenu.return_value = 0
        application.dialog.yesno.return_value = False
        window.onAction(Action(MENU))
        self.assertEqual(client.paths("POST"), [])

    def test_open_item_then_reload_watchlist(self):
        client = FakeClient({"/watchlist": page([movie(1), movie(2)])})
        application = make_application(client)
        window = run(ui_browse.open_personal, application, "watchlist")[0]
        window.getControl(ui_browse.GRID).getSelectedPosition.return_value = 1
        window.onClick(ui_browse.GRID)
        application.open_item.assert_called_once_with(movie(2))
        self.assertEqual(client.paths(), ["/watchlist", "/watchlist"])
        self.assertEqual(window.focus, ui_browse.GRID)

    def test_empty_error_and_unknown_kind(self):
        window = run(ui_browse.open_personal, make_application(FakeClient({"/watchlist": {}})), "watchlist")[0]
        self.assertEqual(window.properties["silo.empty"], "Your watchlist is empty")
        failing = FakeClient({"/history": RuntimeError("offline")})
        window = run(ui_browse.open_personal, make_application(failing), "history")[0]
        self.assertEqual(window.properties["silo.empty"], "History could not be loaded.")
        with self.assertRaises(ValueError):
            ui_browse.open_personal(make_application(FakeClient()), "downloads")


# ---------------------------------------------------------------- people

class PersonTests(unittest.TestCase):
    def client(self, person=None):
        def catalog(params):
            if params["type"] == "movie":
                return page([movie(1), movie(2)], "more")
            return page([])

        return FakeClient({"/catalog/people/7": person if person is not None else {
            "id": "7", "name": "Example Person", "photo_url": "https://example.invalid/face.jpg",
            "bio": "An example biography. " * 20, "birth_date": "1950-04-25", "birthplace": "Example City",
            "death_date": ""}, "/catalog": catalog})

    def test_header_and_filmography(self):
        client = self.client()
        window = run(ui_browse.open_person, make_application(client), "7", name="Example")[0]
        self.assertEqual(client.calls[0][1], "/catalog/people/7")
        self.assertEqual(client.calls[1][2], {"source": "person", "person_id": "7", "type": "movie",
                                              "sort": "-year", "limit": 50})
        self.assertEqual(client.calls[2][2]["type"], "series")
        self.assertEqual(window.properties["silo.name"], "Example Person")
        self.assertEqual(window.properties["silo.initials"], "EP")
        self.assertEqual(window.properties["silo.photo"], "https://example.invalid/face.jpg")
        self.assertEqual(window.properties["silo.facts"], "Born April 25, 1950 in Example City")
        self.assertEqual(window.properties["silo.bio_more"], "true")
        self.assertEqual([row["title"] for row in window.rows], ["Movies"])
        self.assertEqual(window.rows[0]["cursor"], "more")

    def test_read_more_and_navigation(self):
        application = make_application(self.client())
        window = run(ui_browse.open_person, application, "7")[0]
        window.onAction(Action(UP))
        self.assertEqual(window.focus, ui_browse.READ_MORE)
        window.onClick(ui_browse.READ_MORE)
        application.dialog.textviewer.assert_called_once_with("Example Person", window.bio)
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, ui_browse.slot_ids(0)[4])
        window.onClick(ui_browse.slot_ids(0)[4])
        application.open_item.assert_called_once_with(movie(1))

    def test_row_pages_load_near_the_end(self):
        client = self.client()
        window = run(ui_browse.open_person, make_application(client), "7")[0]
        client.routes["/catalog"] = lambda params: page([movie(3)])
        window.onAction(Action(RIGHT))
        self.assertEqual(client.calls[-1][2]["cursor"], "more")
        self.assertEqual(len(window.rows[0]["items"]), 3)
        self.assertIsNone(window.rows[0]["cursor"])

    def test_missing_person_keeps_the_given_name(self):
        client = FakeClient({"/catalog/people/7": RuntimeError("gone"), "/catalog": page([])})
        window = run(ui_browse.open_person, make_application(client), "7", name="Example Name")[0]
        self.assertEqual(window.properties["silo.name"], "Example Name")
        self.assertEqual(window.properties["silo.photo"], "")
        self.assertEqual(window.properties["silo.empty"], "No titles with Example Name in your library")
        window.onAction(Action(UP))  # No Read more button: focus stays put.
        self.assertEqual(window.focus, 0)


# ---------------------------------------------------------------- svc_browse readers

class ServiceTests(unittest.TestCase):
    def test_readers_tolerate_wrong_types(self):
        client = FakeClient({"/recommendations/discover": {"items": "nope"}, "/calendar": {"events": [None, 3]},
                             "/collections": [], "/library/1/sections": {"sections": [{"items": None}]},
                             "/catalog/people/2": None, "/library/1/collections": {"groups": "x"}})
        self.assertEqual(svc_browse.discover(client), [])
        self.assertEqual(svc_browse.calendar(client, "2026-01-05", "2026-01-11"), [])
        self.assertEqual(svc_browse.collections(client), [])
        self.assertEqual(svc_browse.library_sections(client, 1), [])
        self.assertEqual(svc_browse.library_collections(client, 1), [])
        self.assertEqual(svc_browse.person(client, 2)["id"], "2")

    def test_page_cursor_only_when_more(self):
        client = FakeClient({"/catalog": {"items": [movie(1)], "page": {"has_more": False, "next_cursor": "x"},
                                          "total": "3"}})
        self.assertEqual(svc_browse.person_credits(client, 1, "movie"),
                         {"items": [movie(1)], "next_cursor": None, "total": 3})

    def test_calendar_event_shapes(self):
        event = svc_browse.calendar_event({"content_id": "season:1", "type": "season_premiere", "title": "Example",
                                           "season_number": 3, "watched": "yes"})
        self.assertEqual((event["type"], event["season_number"], event["user_state"]), ("series", 3,
                                                                                         {"played": False}))
        self.assertEqual(ui_browse.calendar_caption(event), ("Example", "Season 3"))
        self.assertIsNone(svc_browse.calendar_event({"type": "episode"}))

    def test_ids_are_quoted(self):
        client = FakeClient()
        svc_browse.person(client, "a/b")
        svc_browse.library_sections(client, "x y")
        self.assertEqual(client.paths(), ["/catalog/people/a%2Fb", "/library/x%20y/sections"])


# ---------------------------------------------------------------- skin

class SkinTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trees = {name: ET.parse(SKIN / "1080i" / name) for name in WINDOWS}

    def ids(self, name):
        return [control.get("id") for control in self.trees[name].iter("control") if control.get("id")]

    def test_ids_are_unique(self):
        for name in WINDOWS:
            ids = self.ids(name)
            self.assertEqual(len(ids), len(set(ids)), name)

    def test_navigation_targets_exist(self):
        for name in WINDOWS:
            ids = set(self.ids(name))
            for tag in ("onup", "ondown", "onleft", "onright"):
                for element in self.trees[name].iter(tag):
                    self.assertIn(element.text, ids, "{}: {} -> {}".format(name, tag, element.text))

    def test_textures_exist(self):
        media = SKIN / "media"
        for name in WINDOWS:
            for element in self.trees[name].iter():
                if not element.tag.startswith("texture") and element.tag not in (
                        "midtexture", "lefttexture", "righttexture", "overlaytexture"):
                    continue
                for value in (element.text, element.get("diffuse")):
                    if value and "$INFO" not in value:
                        self.assertTrue((media / value).is_file(), "{}: {}".format(name, value))

    def test_fonts_are_estuary_names(self):
        fonts = {"font_MainMenu", "font52_title", "font36_title", "font30_title", "font25_title", "font27",
                 "font12", "font_flag", "font10"}
        for name in WINDOWS:
            for element in self.trees[name].iter("font"):
                self.assertIn(element.text, fonts, name)

    def test_python_control_ids_exist(self):
        rows = [ui_browse.TITLE_PILL, ui_browse.EMPTY, ui_browse.HERO_GROUP, ui_browse.HERO_CONTENT,
                ui_browse.HERO_META, ui_browse.CALENDAR_HEADER, ui_browse.FILTER_CHIP, ui_browse.WEEK_BUTTON]
        rows += list(ui_browse.BADGE_IMAGES + ui_browse.BADGE_LABELS)
        slots = [control for index in range(ui_browse.SLOTS) for control in ui_browse.slot_ids(index)]
        wanted = {"SiloBrowseRows.xml": rows + slots,
                  "SiloBrowseGrid.xml": [ui_browse.TITLE_PILL, ui_browse.EMPTY, ui_browse.GRID, ui_browse.HEADING,
                                         ui_browse.GRID_META, ui_browse.OVERVIEW],
                  "SiloPerson.xml": [ui_browse.TITLE_PILL, ui_browse.EMPTY, ui_browse.READ_MORE] + slots}
        for name, controls in wanted.items():
            ids = set(self.ids(name))
            for control in controls:
                self.assertIn(str(control), ids, name)

    def test_generator_output_is_current(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            skin_browse = importlib.import_module("skin_browse")
        finally:
            sys.path.pop(0)
        for name, build in skin_browse.WINDOWS.items():
            self.assertEqual((SKIN / "1080i" / name).read_text(encoding="utf-8"), build(), name)


if __name__ == "__main__":
    unittest.main()
