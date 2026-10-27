"""Theme music follows the detail-page stack and never starts over other audio."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo.theme_music import ThemeMusic, theme_set


def item(content_id, theme_id="t1", owner=None):
    return {"content_id": content_id, "themes": {"owner_id": owner or content_id,
                                                  "items": [{"id": theme_id, "title": "Main theme"}]}}


class FakePlayer:
    def __init__(self):
        self.audio, self.other, self.played, self.stops = False, False, [], 0

    def isPlaying(self):
        return self.audio or self.other

    def isPlayingAudio(self):
        return self.audio

    def play(self, url, listitem=None):
        self.played.append(url)
        self.audio = True

    def stop(self):
        self.stops += 1
        self.audio = False


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
        theme = ThemeMusic(self.client, self.settings.get, player=self.player, relay_factory=FakeRelay)
        theme._watch = lambda owner, entry: None  # No loop thread in unit tests.
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
        self.player.other = True
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
