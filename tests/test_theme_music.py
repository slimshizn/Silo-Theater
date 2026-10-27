"""Theme music follows the detail-page stack and never starts over other audio."""

from pathlib import Path
import os
import sys
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo.theme_music import ThemeMusic, theme_set


def item(content_id, theme_id="t1", owner=None):
    return {"content_id": content_id, "themes": {"owner_id": owner or content_id,
                                                  "items": [{"id": theme_id, "title": "Main theme"}]}}


class FakePlayer:
    """Kodi's player: one file at a time; ``other`` is something the viewer plays."""

    def __init__(self):
        self.file, self.other, self.played, self.stops = None, None, [], 0

    @property
    def audio(self):
        return self.file is not None

    def isPlaying(self):
        return self.file is not None or self.other is not None

    def isPlayingAudio(self):
        return self.isPlaying()

    def getPlayingFile(self):
        return self.other if self.other is not None else (self.file or "")

    def play(self, url, listitem=None, windowed=False):
        if not windowed:
            raise AssertionError("A theme must play windowed, or Kodi opens its fullscreen music player")
        self.played.append(url)
        self.file, self.other = url, None

    def stop(self):
        self.stops += 1
        self.file = self.other = None


class FakeVolume:
    """Kodi's application volume; ``levels`` records every change."""

    def __init__(self, volume=80, muted=False):
        self.value, self.muted, self.levels = volume, muted, []

    def get(self):
        return self.value, self.muted

    def set(self, volume):
        self.value = int(round(volume))
        self.levels.append(self.value)


def run_now(function):
    function()
    return None


class FakeRelay:
    def __init__(self, client):
        self.routes, self.closed = [], False

    def register(self, url, authenticated=True):
        self.routes.append((url, authenticated))
        return "http://127.0.0.1:9/relay/{}".format(len(self.routes))

    def close(self):
        self.closed = True


class FakeClient:
    server_url = "https://silo.invalid"

    def __init__(self, capability="available", grant_url="/api/v2/catalog/items/x/themes/t1/audio?grant=opaque"):
        self.calls = []
        self.capability = capability
        self.grant_url = grant_url

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        if path == "/catalog/themes/capabilities":
            return {"state": self.capability, "allowed": True}
        return {"url": self.grant_url, "content_type": "audio/mpeg", "delivery": "original"}


class ThemeMusicTests(unittest.TestCase):
    def setUp(self):
        self.settings = {"ui.theme_music_enabled": True, "ui.theme_music_loop": False}
        self.player = FakePlayer()
        self.client = FakeClient()
        self.xbmcgui = SimpleNamespace(ListItem=lambda **kwargs: SimpleNamespace(
            setProperty=lambda *a: None, getMusicInfoTag=lambda: SimpleNamespace(setTitle=lambda t: None)))
        patcher = patch.dict(sys.modules, {"xbmcgui": self.xbmcgui})
        patcher.start()
        self.addCleanup(patcher.stop)

    def music(self):
        self.volume = getattr(self, "volume", None) or FakeVolume()
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay,
                           volume=self.volume, sleep=lambda seconds: False, spawn=run_now)
        theme._watch = lambda *args: None  # No loop thread in unit tests.
        self.addCleanup(theme.close)
        return theme

    def test_a_page_plays_its_theme_through_the_relay(self):
        theme = self.music()
        theme.push(item("movie:1"))
        self.assertEqual(self.player.played, ["http://127.0.0.1:9/relay/1"])
        self.assertEqual(theme.relay.routes, [("https://silo.invalid/api/v2/catalog/items/x/themes/t1/audio"
                                               "?grant=opaque", True)])
        self.assertIn(("POST", "/catalog/items/movie%3A1/themes/t1/playback"), self.client.calls)
        theme.pop()
        self.assertEqual(self.player.stops, 1)

    def test_a_proxy_node_theme_gets_no_sign_in(self):
        self.client.grant_url = "https://node.invalid/theme/opaque.mp3"
        theme = self.music()
        theme.push(item("movie:1"))
        self.assertEqual(theme.relay.routes, [("https://node.invalid/theme/opaque.mp3", False)])

    def test_off_unavailable_or_other_audio_plays_nothing(self):
        self.settings["ui.theme_music_enabled"] = False
        self.music().push(item("movie:1"))
        self.settings["ui.theme_music_enabled"] = True
        self.client.capability = "disabled"
        self.music().push(item("movie:1"))
        self.client.capability = "available"
        self.player.other = "/music/viewer-song.flac"
        self.music().push(item("movie:1"))
        self.assertEqual(self.player.played, [])

    def test_same_set_continues_and_an_inner_page_restores_the_outer_theme(self):
        theme = self.music()
        theme.push(item("series:1", owner="series:1"))
        theme.push(item("season:1", owner="series:1"))  # A season inheriting its series' set.
        self.assertEqual(len(self.player.played), 1)
        theme.push(item("movie:2", theme_id="t2"))
        self.assertEqual(len(self.player.played), 2)
        theme.pop()
        self.assertEqual(len(self.player.played), 3)
        self.assertEqual(theme.playing, ("series:1", "t1"))

    def test_playback_suspends_the_theme_and_resume_restarts_it(self):
        theme = self.music()
        theme.push(item("movie:1"))
        theme.suspend()
        self.assertFalse(self.player.audio)
        theme.resume()
        self.assertTrue(self.player.audio)
        self.assertEqual(len(self.player.played), 2)

    def test_theme_set_needs_an_owner_and_a_theme(self):
        self.assertIsNone(theme_set({"content_id": "x"}))
        self.assertIsNone(theme_set({"content_id": "x", "themes": {"items": []}}))
        self.assertEqual(theme_set(item("x"))[0], "x")


if __name__ == "__main__":
    unittest.main()


class ThemeMusicSafetyTests(ThemeMusicTests):
    def test_player_errors_never_reach_navigation(self):
        theme = self.music()

        def broken(*args):
            raise RuntimeError("player gone")
        self.player.play = broken
        theme.push(item("movie:1"))  # Must not raise.
        theme.suspend()
        theme.resume()
        theme.pop()
        self.assertGreaterEqual(theme.failures, 1)

    def test_a_theme_from_this_server_on_its_default_port_still_gets_the_sign_in(self):
        self.client.grant_url = "https://silo.invalid:443/api/v2/catalog/items/x/themes/t1/audio?grant=opaque"
        theme = self.music()
        theme.push(item("movie:1"))
        self.assertTrue(theme.relay.routes[0][1])


class OnlineTvThemeTests(unittest.TestCase):
    """Online TV themes: Plex's public library by TVDB id when the server has none."""

    def setUp(self):
        self.settings = {"ui.theme_music_enabled": True, "ui.theme_music_loop": False}
        self.player = FakePlayer()
        self.client = FakeClient()
        self.now = 1000000.0
        self.answers = {}  # url -> True / False / None
        self.checked = []
        self.misses = str(Path(self.tmp()) / "theme_misses.json")
        modules = patch.dict(sys.modules, {"xbmcgui": SimpleNamespace(ListItem=lambda **kwargs: SimpleNamespace(
            setProperty=lambda *a: None, getMusicInfoTag=lambda: SimpleNamespace(setTitle=lambda t: None)))})
        modules.start()
        self.addCleanup(modules.stop)

    def tmp(self):
        import tempfile
        folder = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        return folder

    def check(self, url):
        self.checked.append(url)
        return self.answers.get(url, True)

    def music(self, online=True):
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay,
                           online=lambda: online, misses_path=self.misses, check_online=self.check,
                           clock=lambda: self.now, volume=FakeVolume(), sleep=lambda seconds: False,
                           spawn=run_now)
        theme._watch = lambda *args: None
        self.addCleanup(theme.close)
        return theme

    def series(self, tvdb="81189"):
        return {"content_id": "series-tvdb-" + tvdb, "type": "series", "tvdb_id": tvdb}

    def test_a_show_without_a_server_theme_plays_plexs_theme(self):
        self.music().push(self.series())
        self.assertEqual(self.player.played, ["https://tvthemes.plexapp.com/81189.mp3"])

    def test_the_servers_own_theme_wins(self):
        self.music().push(dict(self.series(), themes={"owner_id": "series-tvdb-81189",
                                                       "items": [{"id": "t1", "title": "Local"}]}))
        self.assertEqual(self.player.played, ["http://127.0.0.1:9/relay/1"])
        self.assertEqual(self.checked, [])

    def test_episode_and_season_pages_use_the_shows_tvdb_id(self):
        self.music().push({"content_id": "episode:1", "type": "episode", "series_id": "series-tvdb-121361",
                           "tvdb_id": "999"})
        self.assertEqual(self.player.played, ["https://tvthemes.plexapp.com/121361.mp3"])

    def test_a_miss_waits_a_week_before_asking_again(self):
        url = "https://tvthemes.plexapp.com/81189.mp3"
        self.answers[url] = False
        self.music().push(self.series())
        self.now += 6 * 24 * 3600
        self.music().push(self.series())
        self.assertEqual(self.checked, [url])  # Not asked again within the week, even after a restart.
        self.answers[url] = True
        self.now += 2 * 24 * 3600
        self.music().push(self.series())
        self.assertEqual(self.checked, [url, url])
        self.assertEqual(self.player.played, [url])

    def test_offline_is_not_remembered_as_a_miss(self):
        url = "https://tvthemes.plexapp.com/81189.mp3"
        self.answers[url] = None
        self.music().push(self.series())
        self.answers[url] = True
        self.music().push(self.series())
        self.assertEqual(self.player.played, [url])

    def test_off_movies_and_theme_music_off_never_ask_plex(self):
        self.music(online=False).push(self.series())
        self.music().push({"content_id": "movie-tmdb-1", "type": "movie", "tvdb_id": "5"})
        self.settings["ui.theme_music_enabled"] = False
        self.music().push(self.series())
        self.assertEqual((self.checked, self.player.played), ([], []))


class StopTests(ThemeMusicTests):
    """The theme stops for anything else that plays, and never loops over it."""

    def test_playback_stops_the_theme_and_waits_for_it(self):
        theme = self.music()
        theme.push(item("movie:1"))
        theme.suspend()  # application.play does this before Silo's player starts.
        self.assertEqual((self.player.stops, self.player.file), (1, None))

    def test_a_trailer_stops_the_theme_until_the_next_page(self):
        theme = self.music()
        theme.push(item("movie:1"))
        theme.yield_to()
        self.player.other = "plugin://plugin.video.youtube/play/?video_id=x"
        self.assertEqual(self.player.stops, 1)
        theme.push(item("movie:2", theme_id="t2"))  # The trailer still plays: never start over it.
        self.assertEqual(len(self.player.played), 1)
        self.player.other = None
        theme.pop()
        self.assertEqual(len(self.player.played), 2)

    def test_the_viewers_own_playback_is_never_stopped(self):
        theme = self.music()
        self.player.other = "/music/viewer-song.flac"
        theme.push(item("movie:1"))
        theme.suspend()
        theme.close()
        self.assertEqual((self.player.stops, self.player.other), (0, "/music/viewer-song.flac"))

    def test_the_loop_never_restarts_over_other_playback(self):
        import silo.theme_music as module
        self.settings["ui.theme_music_loop"] = True
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay,
                           volume=FakeVolume(), sleep=lambda seconds: False, spawn=run_now)
        self.addCleanup(theme.close)
        with patch.object(module, "WATCH_SECONDS", 0.01):
            theme.push(item("movie:1"))
            for _ in range(50):  # Let the watcher see the theme playing.
                time.sleep(0.005)
            self.player.other = "https://silo.invalid/video.mkv"  # A video replaced the theme.
            for _ in range(50):
                time.sleep(0.005)
        self.assertEqual(len(self.player.played), 1)
        self.assertIsNone(theme.playing)

    def test_the_loop_restarts_the_theme_when_nothing_else_plays(self):
        import silo.theme_music as module
        self.settings["ui.theme_music_loop"] = True
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay,
                           volume=FakeVolume(), sleep=lambda seconds: False, spawn=run_now)
        self.addCleanup(theme.close)
        with patch.object(module, "WATCH_SECONDS", 0.01):
            theme.push(item("movie:1"))
            for _ in range(50):
                time.sleep(0.005)
            self.player.file = None  # The theme reached its end.
            for _ in range(100):
                time.sleep(0.005)
                if len(self.player.played) > 1:
                    break
        self.assertEqual(len(self.player.played), 2)


class SmoothThemeTests(unittest.TestCase):
    """Download first (no spinner), fade in from low, fade out and restore the viewer's volume."""

    def setUp(self):
        import tempfile, shutil
        self.cache = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.cache, True)
        self.settings = {"ui.theme_music_enabled": True, "ui.theme_music_loop": False}
        self.player, self.client, self.volume = FakePlayer(), FakeClient(), FakeVolume(80)
        self.fetched = []
        modules = patch.dict(sys.modules, {"xbmcgui": SimpleNamespace(ListItem=lambda **kwargs: SimpleNamespace(
            setProperty=lambda *a: None, getMusicInfoTag=lambda: SimpleNamespace(setTitle=lambda t: None)))})
        modules.start()
        self.addCleanup(modules.stop)

    def fetch(self, url, path):
        self.fetched.append(url)
        if "broken" in url:
            return False
        with open(path, "wb") as handle:
            handle.write(b"ID3" + b"\0" * 1000)
        return True

    def music(self):
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay,
                           cache_dir=self.cache, fetch=self.fetch, volume=self.volume,
                           sleep=lambda seconds: False, spawn=run_now)
        theme._watch = lambda *args: None
        self.addCleanup(theme.close)
        return theme

    def test_kodi_plays_a_downloaded_file_and_a_cached_theme_is_reused(self):
        theme = self.music()
        theme.push(item("movie:1"))
        played = self.player.played[0]
        self.assertTrue(played.startswith(self.cache) and played.endswith(".theme"))
        self.assertEqual(self.fetched, ["http://127.0.0.1:9/relay/1"])
        theme.pop()
        theme.push(item("movie:1"))
        self.assertEqual(len(self.fetched), 1)  # Second visit: no download, no grant.
        self.assertEqual(self.player.played[1], played)

    def test_it_fades_in_from_low_to_the_viewers_volume(self):
        self.music().push(item("movie:1"))
        levels = self.volume.levels
        self.assertEqual(levels[0], 12)  # 15% of 80
        self.assertEqual(levels, sorted(levels))
        self.assertEqual(levels[-1], 80)

    def test_a_video_waits_for_the_fade_out_and_gets_the_volume_back(self):
        theme = self.music()
        theme.push(item("movie:1"))
        self.volume.levels.clear()
        self.volume.value = 60  # The viewer turned it down while the theme played.
        theme.suspend()
        levels = self.volume.levels
        self.assertEqual(levels[-1], 60)
        self.assertEqual(levels[:-1], sorted(levels[:-1], reverse=True))
        self.assertEqual((self.player.file, self.player.stops), (None, 1))

    def test_a_muted_kodi_is_never_touched(self):
        self.volume.muted = True
        theme = self.music()
        theme.push(item("movie:1"))
        theme.suspend()
        self.assertEqual(self.volume.levels, [])
        self.assertEqual(len(self.player.played), 1)

    def test_a_failed_download_plays_nothing(self):
        self.client.grant_url = "/api/v2/catalog/items/x/themes/t1/broken"
        theme = self.music()
        theme.relay_factory = lambda client: SimpleNamespace(register=lambda url, authenticated=True: url,
                                                            close=lambda: None)
        theme.push(item("movie:1"))
        self.assertEqual((self.player.played, self.volume.levels), ([], []))

    def test_the_cache_stays_under_its_limit(self):
        import silo.theme_music as module
        with patch.object(module, "CACHE_LIMIT_BYTES", 2500):
            theme = self.music()
            for index in range(5):
                theme.push(item("movie:{}".format(index), theme_id="t{}".format(index)))
                theme.pop()
        files = [name for name in os.listdir(self.cache) if name.endswith(".theme")]
        self.assertLessEqual(len(files), 2)


class KodiVolumeTests(unittest.TestCase):
    def test_setting_the_volume_never_shows_kodis_volume_bar(self):
        from silo.theme_music import KodiVolume
        calls = []
        xbmc = SimpleNamespace(executebuiltin=calls.append,
                               executeJSONRPC=lambda request: '{"result": {"volume": 72, "muted": false}}')
        with patch.dict(sys.modules, {"xbmc": xbmc}):
            volume = KodiVolume()
            self.assertEqual(volume.get(), (72, False))
            volume.set(12.4)
            volume.set(140)
        self.assertEqual(calls, ["SetVolume(12)", "SetVolume(100)"])  # No ",showvolumebar".
