"""Skip markers, Up next, the sleep timer, the player menu bridge and the keymap."""

import os
import tempfile
import unittest
from unittest.mock import MagicMock

from resources.lib.silo import keymap
from resources.lib.silo.player_extras import (MENU_REQUEST, PLAYING, PlaybackExtras, WatchStreak, chapters_for,
                                             quality_options, route_label)
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

    def show(self, *args, **kwargs):
        self.shown, self.kwargs = args, kwargs

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

    def test_player_menu_subtitle_picks_are_passed_on_to_be_remembered(self):
        extras = self.extras()
        extras.remember_subtitle = MagicMock()
        self.home[MENU_REQUEST] = "1"
        extras.tick(100, now=0)
        self.menu.intents = [{"action": "subtitle_choice", "off": False, "language": "spa"}]
        extras.tick(101, now=1)
        extras.remember_subtitle.assert_called_once_with({"off": False, "language": "spa"})

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


class StillWatchingAndLeadTests(unittest.TestCase):
    def extras(self, streak=None, item=EPISODE, plan=None, **options):
        self.player, self.skip, self.next_card, self.home, self.menu = MagicMock(), Overlay(), Overlay(), Home(), Menu()
        self.streak = streak if streak is not None else WatchStreak()
        self.seeks = []
        settings = {"auto_next": True, "skip_prompts": True}
        settings.update(options)
        return PlaybackExtras(
            self.player, item, VERSION, plan or {"timeline": {"timeline_offset_seconds": 0}}, settings,
            skip_button=self.skip, up_next=self.next_card, menu_factory=lambda: self.menu, home=self.home,
            next_lookup=lambda current: {"content_id": "episode-2", "title": "Second", "season_number": 1,
                                         "episode_number": 2},
            image=lambda item, kind: "", versions=[VERSION], file_id="file-a", streak=self.streak,
            seek=self.seeks.append, ai_enabled=lambda: True)

    def reach_up_next(self, extras, now=0):
        extras.tick(3080, total=3210, now=now)
        extras.tick(3134, total=3210, now=now + 1)

    def test_countdown_expiry_counts_an_unattended_episode(self):
        extras = self.extras(still_watching=3)
        self.reach_up_next(extras)
        command = extras.tick(3140, total=3210, now=12)
        self.assertEqual(command, {"stop": "next", "item": extras.next_item, "auto": True})
        self.assertEqual(self.streak.count, 1)

    def test_play_now_is_an_interaction_and_resets_the_count(self):
        streak = WatchStreak()
        streak.count = 2
        extras = self.extras(streak, still_watching=3)
        self.reach_up_next(extras)
        self.next_card.choice = "play"
        self.assertEqual(extras.tick(3135, total=3210, now=3)["auto"], False)
        self.assertEqual(streak.count, 0)

    def test_threshold_shows_still_watching_instead_of_a_countdown(self):
        streak = WatchStreak()
        streak.count = 3
        extras = self.extras(streak, still_watching=3)
        self.reach_up_next(extras)
        self.assertEqual(self.next_card.kwargs, {"still": True})
        self.assertIsNone(self.next_card.shown[1])  # no countdown
        # Nobody answers: playback is never advanced by itself.
        self.assertIsNone(extras.tick(3200, total=3210, now=100))
        self.next_card.choice = "play"
        command = extras.tick(3201, total=3210, now=101)
        self.assertEqual((command["stop"], command["auto"]), ("next", False))
        self.assertEqual(streak.count, 0)

    def test_still_watching_stop_ends_playback(self):
        streak = WatchStreak()
        streak.count = 5
        extras = self.extras(streak, still_watching=2)
        self.reach_up_next(extras)
        self.next_card.choice = "stop"
        self.assertEqual(extras.tick(3140, total=3210, now=5), {"stop": "still_watching"})
        self.assertEqual(streak.count, 0)

    def test_zero_never_asks(self):
        streak = WatchStreak()
        streak.count = 50
        extras = self.extras(streak, still_watching=0)
        self.reach_up_next(extras)
        self.assertEqual(self.next_card.kwargs, {})
        self.assertEqual(self.next_card.shown[1], 10.0)

    def test_menu_and_skip_button_reset_the_count(self):
        streak = WatchStreak()
        streak.count = 2
        extras = self.extras(streak)
        self.home[MENU_REQUEST] = "1"
        extras.tick(100, now=0)
        self.assertEqual(streak.count, 0)
        streak.count = 2
        extras.tick(330, now=1)
        self.skip.choice = "dismiss"
        extras.tick(331, now=2)
        self.assertEqual(streak.count, 0)

    def test_up_next_lead_replaces_the_fixed_thirty_seconds_without_credits(self):
        version = {key: value for key, value in VERSION.items() if key != "credits"}
        item = dict(EPISODE, versions=[version])
        for lead, start in ((60, 3150), (15, 3195), (None, 3180)):
            with self.subTest(lead=lead):
                extras = self.extras(item=item, **({"next_up_lead": lead} if lead else {}))
                extras.version = version
                extras.tick(start - 61, total=3210, now=0)  # lookup
                extras.tick(start - 61, total=3210, now=0)
                extras.tick(start - 0.5, total=3210, now=1)
                self.assertIsNone(self.next_card.shown)
                extras.tick(start, total=3210, now=2)
                self.assertIsNotNone(self.next_card.shown)

    def test_credits_marker_still_wins_over_the_lead(self):
        extras = self.extras(next_up_lead=60)
        extras.tick(3080, total=3210, now=0)
        extras.tick(3130, total=3210, now=1)
        self.assertIsNone(self.next_card.shown)
        extras.tick(3134, total=3210, now=2)
        self.assertIsNotNone(self.next_card.shown)


class ConvertedMenuTests(unittest.TestCase):
    PLAN = {
        "delivery": "server_transcode_hls", "plan_id": "plan:x", "plan_attempt_key": "v3:x",
        "timeline": {"timeline_offset_seconds": 0},
        "effective_recipe": {"video_codec": "h264", "height": 1080, "bitrate_kbps": 6000},
        "selected_tracks": {"audio": {"index": 1}},
        "available_qualities": [{"label": "original", "display_name": "Original", "preserves_source": True},
                                {"label": "1080p-medium", "display_name": "1080p Medium"}],
        "subtitle": {"mode": "render", "track_id": "file:42:subtitle:0",
                     "inventory": [{"track_id": "file:42:subtitle:0", "sync_key": "external-abc", "url": "/s.vtt"}]},
    }
    TRACKS = [{"language": "eng", "codec": "truehd", "channels": 8}, {"title": "Commentary", "codec": "aac",
                                                                      "channels": 2}]

    def extras(self, plan=None, **options):
        self.player, self.home, self.menu = MagicMock(), Home(), Menu()
        self.seeks = []
        version = dict(VERSION, audio_tracks=self.TRACKS)
        return PlaybackExtras(self.player, EPISODE, version, plan or self.PLAN, dict(options),
                              skip_button=Overlay(), up_next=Overlay(), menu_factory=lambda: self.menu,
                              home=self.home, versions=[version], file_id="42", seek=self.seeks.append,
                              ai_enabled=lambda: True)

    def open_menu(self, extras):
        self.home[MENU_REQUEST] = "1"
        extras.tick(100, now=0)
        return self.menu.context

    def test_context_describes_the_converted_route_and_choices(self):
        context = self.open_menu(self.extras(quality_preference="1080p-medium"))
        self.assertEqual(context["route"], "Transcode · 1080p H.264 · 6 Mbps")
        self.assertTrue(context["converted"])
        self.assertEqual(context["audio_index"], 1)
        self.assertEqual([track["label"] for track in context["audio_tracks"]],
                         ["ENG · TRUEHD · 7.1", "Commentary · AAC · Stereo"])
        self.assertEqual(context["qualities"], [{"value": "original", "label": "Original"},
                                                {"value": "1080p-medium", "label": "1080p Medium"}])
        self.assertEqual(context["quality"], "1080p-medium")
        self.assertTrue(context["ai_translate"])
        self.assertTrue(context["subtitle_sync"])

    def test_direct_play_and_single_rung_offer_no_quality_choice(self):
        plan = dict(self.PLAN, delivery="original_http", available_qualities=[{"label": "original"}])
        context = self.open_menu(self.extras(plan))
        self.assertEqual(context["qualities"], [])
        self.assertFalse(context["converted"])
        self.assertEqual(context["route"], "Direct play · original file")
        self.assertEqual(route_label({"delivery": "server_remux_hls"}), "Direct stream · remuxed by the server")
        self.assertEqual(quality_options({}), [])

    def test_quality_audio_and_tool_intents_become_controller_commands(self):
        extras = self.extras()
        self.open_menu(extras)
        self.menu.intents = [{"action": "quality", "value": "1080p-medium"}]
        self.assertEqual(extras.tick(101, now=1), {"replan": "quality", "quality": "1080p-medium"})
        self.menu.intents = [{"action": "quality", "value": "1080p-medium"}]
        self.assertIsNone(extras.tick(102, now=2))  # already the current choice
        self.menu.intents = [{"action": "audio_track", "index": 0}]
        self.assertEqual(extras.tick(103, now=3), {"replan": "track", "audio_index": 0})
        self.menu.intents = [{"action": "audio_track", "index": True}, {"action": "audio_track", "index": -1}]
        self.assertIsNone(extras.tick(104, now=4))
        self.menu.intents = [{"action": "subtitle_tool", "screen": "subtitle_sync"}]
        command = extras.tick(105, now=5)
        self.assertEqual(command["screen"], "subtitle_sync")
        self.assertEqual(command["subtitle"]["sync_key"], "external-abc")
        self.menu.intents = [{"action": "subtitle_tool", "screen": "subtitle_search"}]
        self.assertEqual(extras.tick(106, now=6), {"screen": "subtitle_search"})
        self.menu.intents = [{"action": "subtitle_tool", "screen": "elsewhere"}]
        self.assertIsNone(extras.tick(107, now=7))

    def test_ai_status_is_asked_once_and_failures_hide_the_row(self):
        calls = []

        def broken():
            calls.append(1)
            raise OSError("offline")

        extras = self.extras()
        extras.ai_enabled = broken
        self.assertFalse(self.open_menu(extras)["ai_translate"])
        self.menu.open_state = False
        self.assertFalse(self.open_menu(extras)["ai_translate"])
        self.assertEqual(len(calls), 1)

    def test_skip_seeks_go_through_the_injected_seek(self):
        extras = self.extras(plan=dict(self.PLAN, subtitle={}))
        extras.tick(330, now=0)
        extras.skip_button.choice = "skip"
        extras.tick(331, now=1)
        self.assertEqual(self.seeks, [401.6])
        self.player.seekTime.assert_not_called()

    def test_update_plan_moves_markers_to_the_new_timeline(self):
        extras = self.extras()
        extras.update_plan(dict(self.PLAN, timeline={"timeline_offset_seconds": 300}), "720p-low")
        self.assertEqual(extras.quality, "720p-low")
        intro = next(span for span in extras.tracker.spans if span["kind"] == "intro")
        self.assertAlmostEqual(intro["start"], 27.3)


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
