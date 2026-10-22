"""Kodi home-screen widget listings with Kodi stubbed: items, art, info tags, links, cache."""

import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qsl, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from resources.lib.silo.api import ApiError, SiloClient  # noqa: E402
from resources.lib.silo.storage import SessionStore  # noqa: E402

ACCESS, REFRESH, PROFILE_TOKEN = "access-secret-1", "refresh-secret-2", "profile-secret-3"
SERVER = "https://silo.example.invalid"


class FakeInfoTag:
    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        if not name.startswith("set"):
            raise AttributeError(name)

        def record(*args):
            self.calls[name] = args[0] if len(args) == 1 else args
        return record


class FakeListItem:
    def __init__(self, label="", label2="", path="", offscreen=False):
        self.label, self.label2 = label, label2
        self.art, self.properties = {}, {}
        self.tag = FakeInfoTag()

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value

    def getVideoInfoTag(self):
        return self.tag


class FakePlugin(types.SimpleNamespace):
    def reset(self):
        self.items, self.content, self.sorts, self.ended = [], None, [], None

    def addDirectoryItems(self, handle, items, total=0):
        self.items.extend(items)
        return True

    def setContent(self, handle, content):
        self.content = content

    def addSortMethod(self, handle, method):
        self.sorts.append(method)

    def endOfDirectory(self, handle, succeeded=True, updateListing=False, cacheToDisc=True):
        self.ended = succeeded


plugin = FakePlugin(SORT_METHOD_UNSORTED=0, SORT_METHOD_LABEL_IGNORE_THE=2, SORT_METHOD_VIDEO_YEAR=18,
                    SORT_METHOD_EPISODE=24)
plugin.reset()
kodi = types.SimpleNamespace(executebuiltin=MagicMock(), log=MagicMock(), LOGWARNING=2, LOGERROR=4)
KODI_MODULES = {
    "xbmc": kodi,
    "xbmcgui": types.SimpleNamespace(ListItem=FakeListItem),
    "xbmcplugin": plugin,
    "xbmcaddon": types.SimpleNamespace(Addon=MagicMock),
    "xbmcvfs": types.SimpleNamespace(translatePath=lambda path: path),
}
with patch.dict(sys.modules, KODI_MODULES):
    sys.modules.pop("resources.lib.silo.widgets", None)
    widgets = importlib.import_module("resources.lib.silo.widgets")


class FakeAddon:
    def __init__(self, folder, settings=None):
        self.folder = folder
        self.settings = dict({"server_url": SERVER}, **(settings or {}))

    def getSetting(self, key):
        return self.settings.get(key, "")

    def getAddonInfo(self, key):
        return {"profile": self.folder, "version": "0.5.0", "id": "script.silo"}.get(key, "")


def movie(number, **extra):
    item = {"content_id": "movie:example-{}".format(number), "type": "movie",
            "title": "Example Movie {}".format(number), "year": 2020, "runtime": 100,
            "overview": "An example synopsis.", "genres": ["Drama"], "content_rating": "PG",
            "poster_url": "https://img.example.invalid/p{}.jpg".format(number),
            "backdrop_url": "https://img.example.invalid/b{}.jpg".format(number),
            "logo_url": "https://img.example.invalid/l{}.png".format(number)}
    item.update(extra)
    return item


def episode(number, **extra):
    item = {"content_id": "episode:example-s01e{:02d}".format(number), "type": "episode",
            "title": "Example Episode {}".format(number), "series_title": "Example Show",
            "series_id": "series:example", "season_number": 1, "episode_number": number,
            "overview": "Something surprising happens.", "duration_seconds": 3000.0,
            "poster_url": "https://img.example.invalid/still{}.jpg".format(number),
            "poster_is_episode_still": True,
            "backdrop_url": "https://img.example.invalid/show.jpg"}
    item.update(extra)
    return item


class FakeTransport:
    """Answers API paths; records every URL and header set it was sent."""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, dict(headers)))
        path = urlsplit(url).path[len("/api/v2"):]
        answer = self.routes.get(path)
        if isinstance(answer, ApiError):
            raise answer
        if isinstance(answer, int):
            return answer, {}, {"detail": "nope"}
        if answer is None:
            return 404, {}, {"detail": "not found"}
        return 200, {}, answer


SECTIONS = {"sections": [
    {"id": "continue_watching", "title": "Continue Watching", "section_type": "continue_watching",
     "items": [episode(3)]},
    {"id": "books", "title": "Example Books", "items": [{"content_id": "book:1", "type": "ebook"}]},
    {"id": "recently_added", "title": "Recently Added", "items": [movie(1), movie(2)]},
]}


class WidgetTestCase(unittest.TestCase):
    def setUp(self):
        plugin.reset()
        kodi.executebuiltin.reset_mock()
        kodi.log.reset_mock()
        self.folder = tempfile.mkdtemp()
        self.addon = FakeAddon(self.folder)
        self.now = [1000.0]
        self.transport = FakeTransport({
            "/home/sections": SECTIONS,
            "/home/sections/recently_added/items": {"id": "recently_added", "items": [movie(1), movie(2)]},
            "/home/sections/continue_watching/items": {"items": [
                episode(3, position_seconds=600.0, item_source="in_progress"),
                dict(movie(4), type="series", content_id="series:example-4", play_content_id="episode:x-1",
                     item_source="next_up")]},
            "/watchlist": {"items": [movie(5)], "page": {"has_more": False}},
            "/favorites": {"items": [dict(movie(6), user_state={"played": True, "is_favorite": True})],
                           "page": {"has_more": False}},
        })

    def tearDown(self):
        for name in os.listdir(self.folder):
            path = os.path.join(self.folder, name)
            if os.path.isdir(path):
                for child in os.listdir(path):
                    os.unlink(os.path.join(path, child))
                os.rmdir(path)
            else:
                os.unlink(path)
        os.rmdir(self.folder)

    def sign_in(self, **extra):
        values = {"server_url": SERVER, "access_token": ACCESS, "refresh_token": REFRESH,
                  "expires_at": 10 ** 12, "profile_id": "7", "profile_token": PROFILE_TOKEN}
        values.update(extra)
        SessionStore(os.path.join(self.folder, "session.json")).update(values)

    def factory(self, server_url, store, user_agent="SiloTheater"):
        return SiloClient(server_url, store, transport=self.transport, user_agent=user_agent)

    def run_plugin(self, query=""):
        plugin.reset()
        widgets.Plugin(5, addon=self.addon, client_factory=self.factory,
                       clock=lambda: self.now[0]).dispatch(widgets.parse_query(query))
        return plugin.items

    def paths(self):
        return [urlsplit(url).path for _, url, _ in self.transport.calls]


class RootListingTest(WidgetTestCase):
    def test_root_lists_video_sections_then_watchlist_and_favorites(self):
        self.sign_in()
        items = self.run_plugin()
        self.assertEqual([entry.label for _, entry, _ in items],
                         ["Continue Watching", "Recently Added", "Watchlist", "Favorites"])
        self.assertTrue(all(folder for _, _, folder in items))
        self.assertEqual(items[1][0], "plugin://script.silo/?action=section&id=recently_added")
        self.assertEqual(items[2][0], "plugin://script.silo/?action=watchlist")
        self.assertEqual(items[1][1].art["fanart"], "https://img.example.invalid/b1.jpg")
        self.assertTrue(plugin.ended)
        self.assertEqual(self.paths(), ["/api/v2/home/sections"])

    def test_requests_carry_profile_headers(self):
        self.sign_in()
        self.run_plugin()
        headers = self.transport.calls[0][2]
        self.assertEqual(headers["Authorization"], "Bearer " + ACCESS)
        self.assertEqual(headers["X-Profile-Id"], "7")
        self.assertEqual(headers["X-Profile-Token"], PROFILE_TOKEN)


class ItemListingTest(WidgetTestCase):
    def test_movie_items_have_art_info_and_play_links(self):
        self.sign_in()
        items = self.run_plugin("?action=section&id=recently_added")
        self.assertEqual(plugin.content, "movies")
        self.assertIn(plugin.SORT_METHOD_UNSORTED, plugin.sorts)
        self.assertIn(plugin.SORT_METHOD_VIDEO_YEAR, plugin.sorts)
        url, entry, folder = items[0]
        self.assertFalse(folder)
        self.assertEqual(url, "plugin://script.silo/?action=play&id=movie%3Aexample-1")
        self.assertEqual(entry.properties["IsPlayable"], "false")
        self.assertEqual(entry.art["poster"], "https://img.example.invalid/p1.jpg")
        self.assertEqual(entry.art["fanart"], "https://img.example.invalid/b1.jpg")
        self.assertEqual(entry.art["landscape"], "https://img.example.invalid/b1.jpg")
        self.assertEqual(entry.art["clearlogo"], "https://img.example.invalid/l1.png")
        tag = entry.tag.calls
        self.assertEqual(tag["setMediaType"], "movie")
        self.assertEqual(tag["setTitle"], "Example Movie 1")
        self.assertEqual(tag["setYear"], 2020)
        self.assertEqual(tag["setDuration"], 6000)
        self.assertEqual(tag["setPlot"], "An example synopsis.")
        self.assertEqual(tag["setGenres"], ["Drama"])
        self.assertEqual(tag["setMpaa"], "PG")
        self.assertEqual(tag["setPlaycount"], 0)
        self.assertNotIn("setResumePoint", tag)

    def test_episode_resume_and_series_next_up(self):
        self.sign_in()
        items = self.run_plugin("?action=section&id=continue_watching")
        self.assertEqual(plugin.content, "videos")  # mixed episode and series cards
        url, entry, _ = items[0]
        tag = entry.tag.calls
        self.assertEqual(tag["setMediaType"], "episode")
        self.assertEqual(tag["setTvShowTitle"], "Example Show")
        self.assertEqual((tag["setSeason"], tag["setEpisode"]), (1, 3))
        self.assertEqual(tag["setResumePoint"], (600.0, 3000.0))
        self.assertEqual(entry.art["thumb"], "https://img.example.invalid/still3.jpg")
        self.assertEqual(entry.art["fanart"], "https://img.example.invalid/show.jpg")
        self.assertIn("action=play&id=episode%3Aexample-s01e03", url)
        # A next-up series card plays its next episode directly.
        self.assertIn("action=play&id=episode%3Ax-1", items[1][0])
        self.assertEqual(items[1][1].tag.calls["setMediaType"], "tvshow")

    def test_series_opens_details(self):
        self.assertEqual(widgets.item_target({"content_id": "series:s", "type": "series"}), ("open", "series:s"))
        self.assertEqual(widgets.item_target({"content_id": "season:s1", "type": "season"}), ("open", "season:s1"))

    def test_hide_spoilers_blanks_unwatched_episode_plot_only(self):
        self.sign_in()
        self.addon.settings["hide_spoilers"] = "true"
        items = self.run_plugin("?action=section&id=continue_watching")
        self.assertEqual(items[0][1].tag.calls["setPlot"], "")
        watched = widgets.list_item(episode(1, user_state={"played": True}), self.factory(SERVER, {}),
                                    {"hide_spoilers": True})[1]
        self.assertEqual(watched.tag.calls["setPlot"], "Something surprising happens.")
        film = widgets.list_item(movie(1), self.factory(SERVER, {}), {"hide_spoilers": True})[1]
        self.assertEqual(film.tag.calls["setPlot"], "An example synopsis.")

    def test_watched_items_have_playcount_and_no_resume(self):
        self.sign_in()
        items = self.run_plugin("?action=favorites")
        tag = items[0][1].tag.calls
        self.assertEqual(tag["setPlaycount"], 1)
        self.assertNotIn("setResumePoint", tag)
        self.assertIn("/api/v2/favorites", self.paths())

    def test_watchlist_listing(self):
        self.sign_in()
        items = self.run_plugin("?action=watchlist")
        self.assertEqual([entry.label for _, entry, _ in items], ["Example Movie 5"])
        self.assertEqual(urlsplit(self.transport.calls[0][1]).query, "limit=50")

    def test_episode_listing_content_and_sorts(self):
        self.assertEqual(widgets.content_type([episode(1), episode(2)]), "episodes")
        self.assertEqual(widgets.content_type([{"type": "series"}]), "tvshows")
        self.assertIn(plugin.SORT_METHOD_EPISODE, widgets.Plugin.sort_methods("episodes"))


class SignedOutTest(WidgetTestCase):
    def assert_sign_in_item(self, items):
        self.assertEqual(len(items), 1)
        url, entry, folder = items[0]
        self.assertEqual(entry.label, "Open Silo Theater to sign in")
        self.assertEqual(url, "plugin://script.silo/?action=signin")
        self.assertFalse(folder)
        self.assertTrue(plugin.ended)

    def test_no_session_shows_sign_in_item_without_requests(self):
        self.assert_sign_in_item(self.run_plugin())
        self.assertEqual(self.transport.calls, [])

    def test_no_profile_shows_sign_in_item(self):
        self.sign_in(profile_id=None)
        self.assert_sign_in_item(self.run_plugin())

    def test_session_for_another_server_is_left_untouched(self):
        self.sign_in(server_url="https://other.example.invalid")
        self.assert_sign_in_item(self.run_plugin())
        stored = SessionStore(os.path.join(self.folder, "session.json"))
        self.assertEqual(stored.get("access_token"), ACCESS)

    def test_rejected_session_shows_sign_in_item(self):
        self.sign_in(refresh_token=None)
        self.transport.routes["/home/sections"] = 401
        self.assert_sign_in_item(self.run_plugin())

    def test_sign_in_item_runs_the_addon(self):
        self.run_plugin("?action=signin")
        kodi.executebuiltin.assert_called_once_with("RunAddon(script.silo)")

    def test_network_failure_ends_directory_unsuccessfully(self):
        self.sign_in()
        self.transport.routes["/home/sections"] = 500
        self.assertEqual(self.run_plugin(), [])
        self.assertFalse(plugin.ended)
        logged = " ".join(str(call) for call in kodi.log.call_args_list)
        self.assertNotIn(ACCESS, logged)


class DeepLinkTest(WidgetTestCase):
    def test_play_hands_off_to_the_addon(self):
        self.run_plugin("?action=play&id=movie%3Aexample-1")
        kodi.executebuiltin.assert_called_once_with("RunScript(script.silo,play,movie:example-1)")
        self.assertEqual(self.transport.calls, [])

    def test_open_hands_off_to_the_addon(self):
        self.run_plugin("?action=open&id=series%3Aexample")
        kodi.executebuiltin.assert_called_once_with("RunScript(script.silo,open,series:example)")

    def test_unsafe_ids_are_encoded_and_round_trip(self):
        content_id = "movie:a,b (c)%"
        builtin = widgets.run_script_builtin("play", content_id)
        arguments = builtin[len("RunScript("):-1].split(",")
        self.assertEqual(len(arguments), 3)
        self.assertNotIn("(", arguments[2])
        self.assertEqual(widgets.deep_link(arguments[1:]), ("play", content_id))

    def test_deep_link_parsing(self):
        self.assertEqual(widgets.deep_link(["play", "movie:example-1"]), ("play", "movie:example-1"))
        self.assertEqual(widgets.deep_link(["OPEN", "series%3Aexample"]), ("open", "series:example"))
        self.assertIsNone(widgets.deep_link(["delete", "movie:1"]))
        self.assertIsNone(widgets.deep_link(["play", ""]))
        self.assertIsNone(widgets.deep_link(["play"]))
        self.assertIsNone(widgets.deep_link([]))

    def test_run_uses_kodi_plugin_argv(self):
        with patch.object(widgets, "Plugin") as factory:
            widgets.run(["plugin://script.silo/", "12", "?action=section&id=recently_added"])
        factory.assert_called_once_with(12)
        factory.return_value.dispatch.assert_called_once_with({"action": "section", "id": "recently_added"})


class CacheTest(WidgetTestCase):
    def test_second_listing_within_a_minute_is_served_from_cache(self):
        self.sign_in()
        first = [entry.label for _, entry, _ in self.run_plugin("?action=section&id=recently_added")]
        self.now[0] += 30
        second = [entry.label for _, entry, _ in self.run_plugin("?action=section&id=recently_added")]
        self.assertEqual(first, second)
        self.assertEqual(len(self.transport.calls), 1)

    def test_cache_expires(self):
        self.sign_in()
        self.run_plugin("?action=watchlist")
        self.now[0] += widgets.CACHE_SECONDS + 1
        self.run_plugin("?action=watchlist")
        self.assertEqual(len(self.transport.calls), 2)

    def test_cache_is_per_profile(self):
        self.sign_in()
        self.run_plugin("?action=watchlist")
        self.sign_in(profile_id="8")
        self.run_plugin("?action=watchlist")
        self.assertEqual(len(self.transport.calls), 2)

    def test_recent_copy_is_shown_when_the_server_is_unreachable(self):
        self.sign_in()
        self.run_plugin("?action=watchlist")
        self.now[0] += widgets.CACHE_SECONDS + 1
        self.transport.routes["/watchlist"] = 503
        items = self.run_plugin("?action=watchlist")
        self.assertEqual([entry.label for _, entry, _ in items], ["Example Movie 5"])

    def test_cache_files_hold_no_credentials(self):
        self.sign_in()
        self.run_plugin()
        self.run_plugin("?action=section&id=recently_added")
        folder = os.path.join(self.folder, widgets.CACHE_FOLDER)
        names = os.listdir(folder)
        self.assertEqual(len(names), 2)
        for name in names:
            self.assertRegex(name, r"^[0-9a-f]{32}\.json$")
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                text = handle.read()
            json.loads(text)
            for secret in (ACCESS, REFRESH, PROFILE_TOKEN):
                self.assertNotIn(secret, text)

    def test_clear_cache(self):
        self.sign_in()
        self.run_plugin("?action=watchlist")
        widgets.clear_cache(self.addon)
        self.run_plugin("?action=watchlist")
        self.assertEqual(len(self.transport.calls), 2)


class NoTokensTest(WidgetTestCase):
    def test_no_token_in_any_url_label_or_property(self):
        self.sign_in()
        listings = []
        for query in ("", "?action=section&id=recently_added", "?action=section&id=continue_watching",
                      "?action=watchlist", "?action=favorites"):
            listings.extend(self.run_plugin(query))
        self.assertTrue(listings)
        for url, entry, _ in listings:
            text = " ".join([url, entry.label, entry.label2, json.dumps(entry.art), json.dumps(entry.properties),
                             repr(entry.tag.calls)])
            for secret in (ACCESS, REFRESH, PROFILE_TOKEN):
                self.assertNotIn(secret, text)
            self.assertTrue(url.startswith("plugin://script.silo/?action="))
            self.assertEqual(set(dict(parse_qsl(urlsplit(url).query))) - {"action", "id"}, set())


if __name__ == "__main__":
    unittest.main()
