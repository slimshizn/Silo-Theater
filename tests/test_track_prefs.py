"""Subtitle and audio defaults follow Silo's profile preferences like the Silo apps."""

import unittest
from unittest.mock import MagicMock, patch

from resources.lib.silo import track_prefs
from resources.lib.silo.track_prefs import OFF

try:
    from .test_ui import Application, Store, application_module, movie_item
except ImportError:
    from test_ui import Application, Store, application_module, movie_item

VERSION = {
    "file_id": "file-a",
    "audio_tracks": [{"language": "jpn", "channels": 6, "default": True},
                     {"language": "eng", "channels": 2, "title": "Commentary"},
                     {"language": "eng", "channels": 6}],
    # Playback ordinals: the external file is 0, embedded tracks follow (1, 2, 3).
    "subtitle_tracks": [{"language": "eng", "codec": "subrip"},
                        {"language": "eng", "codec": "subrip", "forced": True},
                        {"language": "eng", "codec": "subrip", "title": "English SDH"},
                        {"language": "spa", "codec": "srt", "external": True}],
}


def profile(**values):
    base = {"subtitle_mode": "auto", "subtitle_language": "en", "show_forced_subtitles": True, "language": "en"}
    base.update(values)
    return base


class SubtitleRuleTests(unittest.TestCase):
    def test_ordinals_put_external_files_first(self):
        self.assertEqual([(t["index"], t["language"], t["external"]) for t in track_prefs.subtitle_tracks(VERSION)],
                         [(0, "es", True), (1, "en", False), (2, "en", False), (3, "en", False)])

    def test_auto_shows_full_subtitles_for_foreign_audio_and_only_forced_for_your_language(self):
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(), audio_language="jpn"), 1)
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(), audio_language="eng"), 2)
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(show_forced_subtitles=False), "eng"), OFF)

    def test_always_off_and_empty_language(self):
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(subtitle_mode="always"), "eng"), 1)
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(subtitle_mode="off"), "jpn"), OFF)
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(subtitle_language=""), "jpn"), OFF)
        self.assertIsNone(track_prefs.auto_subtitle(VERSION, profile(subtitle_language=None), "jpn"))
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(subtitle_language=None, subtitle_mode="always"),
                                                   "jpn"), 1)

    def test_missing_language_falls_back_to_a_forced_track(self):
        self.assertEqual(track_prefs.auto_subtitle(VERSION, profile(subtitle_language="fr"), "jpn"), 2)
        self.assertIsNone(track_prefs.auto_subtitle(VERSION, profile(subtitle_language="fr",
                                                                     show_forced_subtitles=False), "jpn"))

    def test_sdh_and_external_lose_ties(self):
        version = {"subtitle_tracks": [{"language": "en", "title": "SDH"}, {"language": "en", "external": True},
                                       {"language": "en"}]}
        self.assertEqual(track_prefs.auto_subtitle(version, profile(subtitle_mode="always"), "en"), 2)

    def test_language_codes_compare_across_iso_forms(self):
        self.assertEqual(track_prefs.language("ENG"), "en")
        self.assertEqual(track_prefs.language("pt-BR"), "pt")
        self.assertEqual(track_prefs.language("und"), "")


class RememberedChoiceTests(unittest.TestCase):
    def test_choice_follows_language_and_kind_into_another_file(self):
        choice = track_prefs.remembered_choice(track_prefs.track_at(VERSION, 3))
        self.assertEqual(choice, {"kind": "track", "language": "en", "forced": False, "hi": True, "external": False})
        other = {"subtitle_tracks": [{"language": "en"}, {"language": "en", "title": "SDH"}]}
        self.assertEqual(track_prefs.match_choice(other, choice), 1)
        self.assertIsNone(track_prefs.match_choice({"subtitle_tracks": [{"language": "fr"}]}, choice))
        self.assertEqual(track_prefs.match_choice(other, track_prefs.remembered_choice(None)), OFF)

    def test_episodes_remember_per_show(self):
        self.assertEqual(track_prefs.choice_key({"type": "episode", "series_id": "s:1", "content_id": "e:1"}),
                         "series:s:1")
        self.assertEqual(track_prefs.choice_key({"type": "movie", "content_id": "m:1"}), "item:m:1")


class AudioTests(unittest.TestCase):
    def test_profile_language_then_english_and_main_mix_before_commentary(self):
        self.assertEqual(track_prefs.audio_default(VERSION, profile()), (2, "en"))
        self.assertEqual(track_prefs.audio_default(VERSION, profile(language="ja")), (0, "ja"))
        self.assertEqual(track_prefs.audio_default(VERSION, profile(language="de")), (2, "en"))
        self.assertEqual(track_prefs.audio_default({}, profile()), (None, ""))


class ApplicationTrackTests(unittest.TestCase):
    def setUp(self):
        self.app = Application.__new__(Application)
        self.app.addon = MagicMock()
        self.app.addon.getSetting.return_value = ""
        self.app.dialog = MagicMock()
        self.app.store = Store(profile_id="p")
        self.app.client = MagicMock(server_url="https://silo.example.invalid")
        self.app.profile = dict(profile(), id="p")
        self.episode = {"content_id": "episode:1", "type": "episode", "series_id": "series:1", "versions": [VERSION]}

    def test_default_comes_from_profile_then_a_remembered_pick_wins(self):
        # English audio, so Auto shows only the forced English track.
        self.assertEqual(self.app.subtitle_default(self.episode, VERSION), (2, "auto"))
        self.app.remember_subtitle(self.episode, VERSION, 0)
        self.assertEqual(self.app.subtitle_default(self.episode, VERSION), (0, "remembered"))
        next_episode = dict(self.episode, content_id="episode:2")
        self.assertEqual(self.app.subtitle_default(next_episode, VERSION), (0, "remembered"))
        self.app.remember_subtitle(self.episode, VERSION, None)
        self.assertEqual(self.app.subtitle_default(next_episode, VERSION), (None, "remembered"))

    def test_picking_the_automatic_track_forgets_the_override(self):
        self.app.profile["language"] = "ja"  # Japanese audio, so Auto picks English subtitles (ordinal 1).
        self.app.remember_subtitle(self.episode, VERSION, 0)
        self.app.remember_subtitle(self.episode, VERSION, 1)
        self.assertEqual(self.app.subtitle_default(self.episode, VERSION), (1, "auto"))

    def test_player_menu_picks_are_remembered_by_language(self):
        self.app.remember_subtitle_language(self.episode, {"off": False, "language": "spa"})
        self.assertEqual(self.app.subtitle_default(self.episode, VERSION), (0, "remembered"))
        self.app.remember_subtitle_language(self.episode, {"off": True})
        self.assertEqual(self.app.subtitle_default(self.episode, VERSION), (None, "remembered"))

    def test_play_requests_the_resolved_audio_and_subtitle(self):
        item = movie_item(0)
        item["versions"][0].update(VERSION, file_id="file-a")
        self.app.client.item.return_value = item
        self.app.profile["language"] = "ja"
        with patch.object(application_module, "PlaybackController") as controller:
            controller.return_value.next_action = None
            self.app.play(item, resume=False)
            options = controller.return_value.play.call_args.kwargs["options"]
            self.assertEqual((options["audio_track_index"], options["subtitle_track_index"]), (0, 1))
            self.app.play(item, resume=False, subtitle_index=None)
            options = controller.return_value.play.call_args.kwargs["options"]
            self.assertTrue(options["subtitles_off"])
            self.assertNotIn("subtitle_track_index", options)


if __name__ == "__main__":
    unittest.main()
