"""Skip markers, Up next, the sleep timer, the player menu bridge and the keymap."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from resources.lib.silo import keymap
from resources.lib.silo.player_extras import MENU_REQUEST, PLAYING, PlaybackExtras, chapters_for
from resources.lib.silo.skip import SkipTracker, segments, skip_target

VERSION = {
    "file_id": "file-a", "resolution": "1080p",
    "intro": {"start": 327.3, "end": 401.6},
    "credits": {"start": 3133.9, "end": 3202},
    "chapters": [{"title": "Studio Logo", "start_seconds": 0, "end_seconds": 8.8},
                 {"title": "Recap", "start_seconds": 8.8, "end_seconds": 73.0},
                 {"title": "Scene 01", "start_seconds": 73.0, "end_seconds": 401.6}],
}
EPISODE = {"content_id": "episode-1", "type": "episode", "title": "Pilot", "series_id": "series-1",
           "series_title": "Example Show", "season_number": 1, "episode_number": 1, "versions": [VERSION]}


class SegmentTests(unittest.TestCase):
    def test_markers_and_recap_chapter_become_player_spans(self):
        spans = segments(VERSION, offset=8.8)
        self.assertEqual([span["kind"] for span in spans], ["recap", "intro", "credits"])
        self.assertAlmostEqual(spans[0]["start"], 0.0)
        self.assertAlmostEqual(spans[1]["start"], 318.5)

    def test_bad_or_tiny_markers_are_ignored(self):
        version = {"intro": {"start": 10, "end": 11}, "credits": {"start": "x", "end": 20},
                   "chapters": [{"title": "Previously on", "start_seconds": 0, "end_seconds": 30}]}
        self.assertEqual([span["kind"] for span in segments(version)], ["recap"])
        self.assertEqual(segments(None), [])

    def test_skip_target_stays_inside_the_file(self):
        self.assertEqual(skip_target({"end": 3202.0}, total=3200), 3199.5)
        self.assertEqual(skip_target({"end": 401.6}), 401.6)


class TrackerTests(unittest.TestCase):
    def test_button_shows_once_hides_on_exit_and_rearms_after_seeking_back(self):
        tracker = SkipTracker(segments(VERSION))
        self.assertIsNone(tracker.observe(200, 0))
        self.assertEqual(tracker.observe(330, 1)[0], "show")
        self.assertIsNone(tracker.observe(331, 2))
        self.assertEqual(tracker.observe(402, 3)[0], "hide")
        self.assertIsNone(tracker.observe(340, 4))  # already offered this pass
        self.assertIsNone(tracker.observe(100, 5))
        self.assertEqual(tracker.observe(330, 6)[0], "show")

    def test_button_times_out_inside_a_long_segment(self):
        tracker = SkipTracker(segments(VERSION), show_seconds=10)
        tracker.observe(330, 0)
        self.assertIsNone(tracker.observe(335, 5))
        self.assertEqual(tracker.observe(341, 11)[0], "hide")

    def test_auto_skip_uses_profile_switch_per_kind(self):
        tracker = SkipTracker(segments(VERSION), auto={"intro": True})
        self.assertEqual(tracker.observe(330, 0), ("skip", tracker.spans[1]))
        self.assertEqual(tracker.observe(3140, 1)[0], "show")


class Overlay:
    def __init__(self):
        self.shown, self.choice, self.updates = None, None, []

    def show(self, *args):
        self.shown = args

    def hide(self):
        self.shown = None

    def update(self, seconds):
        self.updates.append(seconds)

    def take_choice(self):
        choice, self.choice = self.choice, None
        return choice


class Home(dict):
    def setProperty(self, key, value):
        self[key] = value

    def getProperty(self, key):
        return self.get(key, "")

    def clearProperty(self, key):
        self.pop(key, None)


class Menu:
    def __init__(self):
        self.context, self.open_state, self.intents, self.refreshed = None, False, [], 0

    def open(self, context, tab=None):
        self.context, self.open_state = context, True

    def refresh(self):
        self.refreshed += 1

    def is_open(self):
        return self.open_state

    def take_result(self):
        intents, self.intents = self.intents, []
        return intents

    def close(self):
        self.open_state = False


class ExtrasTests(unittest.TestCase):
    def extras(self, item=EPISODE, **options):
        self.player, self.skip, self.next_card, self.home, self.menu = MagicMock(), Overlay(), Overlay(), Home(), Menu()
        self.saved, self.notices = {}, []
        plan = {"timeline": {"timeline_offset_seconds": 0}, "effective_file_id": "file-a"}
        settings = {"auto_next": True, "skip_prompts": True}
        settings.update(options)
        return PlaybackExtras(
            self.player, item, VERSION, plan, settings, skip_button=self.skip, up_next=self.next_card,
            menu_factory=lambda: self.menu, home=self.home, notify=self.notices.append,
            next_lookup=lambda current: {"content_id": "episode-2", "title": "Second", "season_number": 1,
                                         "episode_number": 2},
            image=lambda item, kind: "", versions=[VERSION, dict(VERSION, file_id="file-b")], file_id="file-a",
            save_setting=self.saved.__setitem__)

    def test_skip_button_click_seeks_to_marker_end(self):
        extras = self.extras()
        self.assertEqual(self.home[PLAYING], "1")
        extras.tick(330, now=0)
        self.assertEqual(self.skip.shown, ("Skip Intro",))
        self.skip.choice = "skip"
        extras.tick(331, now=1)
        self.player.seekTime.assert_called_once_with(401.6)

    def test_prompts_off_keeps_only_auto_skips(self):
        extras = self.extras(skip_prompts=False, auto_skip={"credits": True})
        extras.tick(330, now=0)
        self.assertIsNone(self.skip.shown)
        extras.tick(3140, total=3210, now=1)
        self.player.seekTime.assert_called_once_with(3202.0)
        self.assertEqual(self.notices, ["Skipped credits"])

    def test_paused_playback_shows_nothing(self):
        extras = self.extras()
        extras.tick(330, paused=True, now=0)
        self.assertIsNone(self.skip.shown)

    def test_up_next_counts_down_then_stops_for_the_next_episode(self):
        extras = self.extras()
        self.assertIsNone(extras.tick(3080, total=3210, now=0))  # lookup a minute early
        self.assertIsNone(extras.tick(3134, total=3210, now=1))
        self.assertEqual(self.next_card.shown[0]["eyebrow"], "S1:E2")
        self.assertIsNone(extras.tick(3135, total=3210, now=5))
        command = extras.tick(3140, total=3210, now=11.5)
        self.assertEqual(command["stop"], "next")
        self.assertEqual(command["item"]["content_id"], "episode-2")

    def test_up_next_without_auto_play_waits_for_a_choice(self):
        extras = self.extras(auto_next=False)
        extras.tick(3080, total=3210, now=0)
        extras.tick(3134, total=3210, now=1)
        self.assertIsNone(self.next_card.shown[1])
        self.assertIsNone(extras.tick(3150, total=3210, now=60))
        self.next_card.choice = "play"
        self.assertEqual(extras.tick(3151, total=3210, now=61)["stop"], "next")

    def test_movies_never_offer_up_next(self):
        extras = self.extras(item={"content_id": "movie-1", "type": "movie", "title": "Example"})
        extras.tick(3140, total=3210, now=0)
        self.assertIsNone(self.next_card.shown)

    def test_menu_request_opens_menu_and_applies_intents(self):
        extras = self.extras()
        self.home[MENU_REQUEST] = "1"
        extras.tick(100, now=0)
        self.assertNotIn(MENU_REQUEST, self.home)
        self.assertEqual(self.menu.context["eyebrow"], "Example Show · S1:E1")
        self.assertEqual(len(self.menu.context["versions"]), 2)
        self.menu.intents = [{"action": "skip_prompts", "value": False}, {"action": "sleep", "minutes": 15},
                             {"action": "native", "window": "osdaudiosettings"}]
        self.assertEqual(extras.tick(101, now=1.5), {"native": "osdaudiosettings"})
        self.assertEqual(self.saved, {"skip_prompts": "false"})
        self.assertEqual(self.menu.refreshed, 1)
        self.assertEqual(extras.tick(102, now=15 * 60 + 2), {"stop": "sleep"})

    def test_version_intent_stops_with_source_position(self):
        extras = self.extras()
        self.home[MENU_REQUEST] = "1"
        extras.tick(100, now=0)
        self.menu.intents = [{"action": "version", "file_id": "file-b"}]
        self.assertEqual(extras.tick(120, now=1), {"stop": "version", "file_id": "file-b", "position": 120})

    def test_close_clears_flags(self):
        extras = self.extras()
        extras.close()
        self.assertNotIn(PLAYING, self.home)

    def test_chapters_convert_to_player_time(self):
        self.assertAlmostEqual(chapters_for(VERSION, 8.8)[2]["start"], 64.2)


class KeymapTests(unittest.TestCase):
    def test_write_update_and_remove_only_our_file(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertTrue(keymap.sync(folder, True, "/addons/script.silo/"))
            path = os.path.join(folder, keymap.FILE_NAME)
            with open(path, encoding="utf-8") as handle:
                content = handle.read()
            self.assertIn("RunScript(/addons/script.silo/resources/player_menu.py)", content)
            self.assertIn("<FullscreenVideo>", content)
            self.assertFalse(keymap.sync(folder, True, "/addons/script.silo"))
            self.assertTrue(keymap.sync(folder, False, "/addons/script.silo"))
            self.assertFalse(os.path.exists(path))
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("<keymap/>")
            self.assertFalse(keymap.sync(folder, False, "/addons/script.silo"))
            self.assertTrue(os.path.exists(path))

    def test_paths_are_escaped(self):
        self.assertIn("A&amp;B", keymap.keymap_xml("C:\\A&B\\script.silo"))


if __name__ == "__main__":
    unittest.main()
