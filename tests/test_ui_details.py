"""The details window with Kodi stubbed: actions, seasons, episodes, zones, skin."""

import importlib
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default/1080i/SiloDetails.xml"
MEDIA = ROOT / "resources/skins/Default/media"


class FakeWindow:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        instance.properties = {}
        instance.controls = {}
        instance.closed = False
        return instance

    def getControl(self, control_id):
        if control_id not in self.controls:
            self.controls[control_id] = MagicMock()
            self.controls[control_id].getSelectedPosition.return_value = 0
        return self.controls[control_id]

    def setProperty(self, key, value):
        self.properties[key] = value

    def setFocusId(self, control_id):
        self.focus = control_id

    def close(self):
        self.closed = True


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2, self.art, self.properties = label, label2, {}, {}

    def setLabel(self, label):
        self.label = label

    def setLabel2(self, label):
        self.label2 = label

    def setArt(self, art):
        self.art.update(art)

    def setProperty(self, key, value):
        self.properties[key] = value

    def getVideoInfoTag(self):
        return MagicMock()


XBMCGUI = types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=FakeListItem)
with patch.dict(sys.modules, {"xbmc": types.SimpleNamespace(log=MagicMock(), LOGERROR=4), "xbmcgui": XBMCGUI}):
    ui_common = importlib.import_module("resources.lib.silo.ui_common")
    ui_details = importlib.import_module("resources.lib.silo.ui_details")
    media = importlib.import_module("resources.lib.silo.media")

DetailsWindow = ui_details.DetailsWindow
UP, DOWN, LEFT, RIGHT = ui_common.ACTION_UP, ui_common.ACTION_DOWN, ui_common.ACTION_LEFT, ui_common.ACTION_RIGHT


def movie_item(position=75, multiple=False):
    versions = [{"file_id": "file-a", "resolution": "2160p", "hdr": True, "file_name": "Example.UHD.mkv"}]
    if multiple:
        versions.append({"file_id": "file-b", "resolution": "1080p", "hdr": False, "file_name": "Example.HD.mkv"})
    return {"content_id": "movie:1", "type": "movie", "title": "Example", "versions": versions,
            "user_data": {"position_seconds": position, "duration_seconds": 7200, "last_file_id": "file-a"}}


def series_item(**extra):
    item = {"content_id": "series:1", "type": "series", "title": "Example Show", "content_rating": "TV-14",
            "overview": "A show.", "play_content_id": "episode:1-2", "play_season_number": 1}
    item.update(extra)
    return item


SEASONS = [
    {"content_id": "season:0", "type": "season", "season_number": 0, "is_specials": True},
    {"content_id": "season:2", "type": "season", "season_number": 2},
    {"content_id": "season:1", "type": "season", "season_number": 1},
]


def episodes_for(season):
    number = season["season_number"]
    return [{"content_id": "episode:{}-{}".format(number, index), "type": "episode", "season_number": number,
             "episode_number": index, "title": "Chapter {}".format(index), "overview": "Episode {}".format(index),
             "air_date": "2024-01-0{}".format(index), "runtime": 42} for index in (1, 2, 3)]


class Action:
    def __init__(self, action_id):
        self.action_id = action_id

    def getId(self):
        return self.action_id


class DetailsWindowTests(unittest.TestCase):
    def setUp(self):
        for module in (ui_common, ui_details):
            patcher = patch.object(module, "xbmcgui", XBMCGUI)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = MagicMock()
        self.app.guard.side_effect = lambda callback: callback()
        self.app.image.side_effect = lambda item, kind: ("https://img.example/{}/{}".format(
            item.get("content_id") or item.get("poster_url"), kind))
        self.app.options.return_value = {"file_selection": "ask", "show_plot": True, "resume_behavior": "resume"}
        self.app.resume_seconds.side_effect = media.resume_position
        self.app.is_playable.side_effect = lambda item: item.get("type") in ("movie", "episode")
        self.app.similar.return_value = []
        self.app.episodes.side_effect = episodes_for

    def window(self, item, children=()):
        window = DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item, window.children = self.app, item, list(children)
        return window

    def series_window(self, **extra):
        window = self.window(series_item(**extra), SEASONS)
        window.onInit()
        return window

    # ---------------------------------------------------- ported behaviour
    def test_detail_refreshes_resume_after_playback(self):
        original = movie_item(0)
        window = self.window(original)
        updated = movie_item(75)
        self.app.play.return_value = "file-a"
        self.app.item_after_playback.return_value = updated
        window.onClick(100)
        self.app.play.assert_called_once_with(original, resume=None, file_id=None, subtitle_index=None)
        self.assertEqual(window.properties["silo.resume_label"], "Resume from 1:15")
        self.assertEqual(window.properties["silo.resume"], "true")
        self.assertEqual(window.properties["silo.play_label"], "Resume 1:15")
        self.assertIs(window.item, updated)
        self.app.refresh_item.assert_not_called()
        self.app.item_after_playback.assert_called_once_with(original, "file-a")

    def test_manual_file_choice_is_visible_and_preserved_after_playback_refresh(self):
        window = self.window(movie_item(multiple=True))
        self.app.choose_file.return_value = "file-b"
        refreshed = movie_item(120, multiple=True)
        self.app.refresh_item.return_value = refreshed
        self.app.item_after_playback.return_value = refreshed
        window.onClick(103)
        self.assertEqual(window.selected_file_id, "file-b")
        self.assertIn("Example.HD.mkv", window.properties["silo.file_label"])
        self.assertIn("1080p", window.properties["silo.file_label"])
        self.assertTrue(window.hint(103).startswith("Version: 1080p"))
        self.assertEqual(window.properties["silo.badge.0"], "HD")
        self.app.play.return_value = "file-b"
        window.onClick(101)
        self.app.play.assert_called_once_with(refreshed, resume=True, file_id="file-b", subtitle_index=None)
        self.assertEqual(window.selected_file_id, "file-b")
        self.assertTrue(window.file_confirmed)

    def test_file_picker_cancel_leaves_selection_untouched(self):
        window = self.window(movie_item(multiple=True))
        window.selected_file_id = "file-a"
        self.app.choose_file.return_value = None
        window.onClick(103)
        self.assertEqual(window.selected_file_id, "file-a")
        self.app.refresh_item.assert_not_called()

    def test_play_start_and_resume_controls_keep_distinct_intents(self):
        window = self.window(movie_item())
        self.app.play.return_value = None
        for control_id, intent in ((100, None), (101, True), (104, False)):
            window.onClick(control_id)
            self.assertIs(self.app.play.call_args.kwargs["resume"], intent)

    def test_primary_focus_is_generic_play_even_when_resume_exists(self):
        window = self.window(movie_item())
        self.app.options.return_value["resume_behavior"] = "start"
        window.onInit()
        self.assertEqual(window.focus, 100)
        self.assertEqual(window.properties["silo.resume"], "true")
        # Play starts over with this setting, so Resume gets its own circle.
        self.assertEqual(window.properties["silo.play_label"], "Play")
        self.assertEqual(window.properties["silo.resume_button"], "true")
        self.assertEqual(window.visible_buttons, [100, 101, 104])

    def test_resume_preference_puts_the_position_on_play_and_lays_out_the_row(self):
        window = self.window(movie_item(4000, multiple=True))
        window.onInit()
        self.assertEqual(window.properties["silo.play_label"], "Resume 1:06:40")
        self.assertEqual(window.properties["silo.resume_button"], "")
        self.assertEqual(window.visible_buttons, [100, 104, 103])
        play_width = window.getControl(100).setWidth.call_args.args[0]
        window.getControl(1400).setPosition.assert_called_with(100, 556)
        window.getControl(1404).setPosition.assert_called_with(100 + play_width + 18, 556)
        window.getControl(1403).setPosition.assert_called_with(100 + play_width + 18 + 76 + 18, 556)
        window.onFocus(104)
        self.assertEqual(window.properties["silo.hint"], "Start over")
        window.onFocus(100)
        self.assertEqual(window.properties["silo.hint"], "")

    def subtitled_item(self, multiple=True):
        item = movie_item(multiple=multiple)
        item["versions"][0]["subtitle_tracks"] = [
            {"index": 3, "language": "eng", "title": "Full", "external": False},
            {"index": 4, "language": "fre", "forced": True, "external": False},
            {"index": 5, "language": "eng", "external": True, "file_name": "Example.en.srt"},
        ]
        return item

    def test_play_uses_the_displayed_version_without_asking_again(self):
        window = self.window(movie_item(multiple=True))
        window.onInit()
        self.assertEqual(window.selected_file_id, "file-a")
        self.app.play.return_value = "file-a"
        window.onClick(100)
        self.assertEqual(self.app.play.call_args.kwargs["file_id"], "file-a")
        self.app.choose_file.assert_not_called()

    def test_subtitle_button_lists_tracks_and_play_sends_the_choice(self):
        window = self.window(self.subtitled_item())
        window.onInit()
        self.assertEqual(window.properties["silo.has_subtitles"], "true")
        self.assertEqual(window.properties["silo.subtitle_label"], "Subtitles: Off")
        self.assertIn(106, window.visible_buttons)
        offered = None

        def choose(choices, current):
            nonlocal offered
            offered = choices
            return 1  # first embedded track, after the one external file

        self.app.choose_subtitle.side_effect = choose
        window.onFocus(106)
        window.onClick(106)
        self.assertEqual([index for index, _ in offered], [0, 1, 2])
        self.assertIn("External file", offered[0][1])
        self.assertEqual(window.subtitle_index, 1)
        self.assertEqual(window.properties["silo.subtitle_label"], "Subtitles: ENG · Full")
        self.assertEqual(window.properties["silo.hint"], "Subtitles: ENG · Full")
        self.app.play.return_value = "file-a"
        window.onClick(100)
        self.assertEqual(self.app.play.call_args.kwargs["subtitle_index"], 1)

    def test_subtitle_cancel_keeps_choice_and_new_version_resets_it(self):
        item = self.subtitled_item()
        window = self.window(item)
        window.onInit()
        self.app.choose_subtitle.return_value = 2
        window.onClick(106)
        self.app.choose_subtitle.return_value = False
        window.onClick(106)
        self.assertEqual(window.subtitle_index, 2)
        self.app.choose_file.return_value = "file-b"
        self.app.refresh_item.return_value = item
        window.onClick(103)
        self.assertIsNone(window.subtitle_index)
        self.assertEqual(window.properties["silo.has_subtitles"], "")
        self.assertNotIn(106, window.visible_buttons)

    def test_completed_progress_hides_resume_and_spoiler_setting_keeps_credit(self):
        item = movie_item()
        item.update(overview="A spoiler", cast=[{"name": "Actor"}], rating_imdb=8.5)
        item["user_data"]["played"] = True
        window = self.window(item)
        self.app.options.return_value["show_plot"] = False
        window.onInit()
        self.assertEqual(window.properties["silo.resume"], "")
        self.assertEqual(window.properties["silo.play_label"], "Play")
        self.assertEqual(window.visible_buttons, [100])
        self.assertEqual(window.properties["silo.plot"], "")
        self.assertEqual(window.properties["silo.credit"], "Starring Actor")
        self.assertIn("IMDb 8.5", window.properties["silo.meta"])

    def test_series_play_button_plays_next_up_and_names_the_episode(self):
        window = self.series_window()
        self.assertEqual(window.visible_buttons, [105])
        self.assertEqual(window.properties["silo.playable"], "")
        self.assertEqual(window.properties["silo.continue_label"], "Play S1:E2")
        self.assertEqual(window.focus, 105)
        self.app.play_next.return_value = "file-x"
        window.onClick(105)
        self.app.play_next.assert_called_once_with(window.item)
        self.app.play.assert_not_called()
        self.app.open_item.assert_not_called()

    def test_series_progress_reads_resume_and_series_without_next_up_has_no_play(self):
        window = self.series_window(user_data={"position_seconds": 300, "is_in_progress": True})
        self.assertEqual(window.properties["silo.continue_label"], "Resume S1:E2")
        window = self.window(series_item(play_content_id=None), SEASONS)
        window.onInit()
        self.assertEqual(window.visible_buttons, [])
        self.assertEqual(window.focus, 510)

    # ---------------------------------------------------- seasons/episodes
    def test_seasons_are_ordered_with_specials_last_and_open_on_next_up_season(self):
        window = self.series_window()
        labels = [entry.label for entry in window.getControl(510).addItems.call_args.args[0]]
        self.assertEqual(labels, ["Season 1", "Season 2", "Specials"])
        self.assertEqual(window.season_index, 0)
        window.getControl(510).selectItem.assert_called_with(0)
        self.assertEqual(window.properties["silo.has_seasons"], "true")
        window = self.series_window(play_season_number=2)
        window.getControl(510).selectItem.assert_called_with(1)
        self.assertEqual(self.app.episodes.call_args.args[0]["content_id"], "season:2")

    def test_episode_rail_captions_and_next_up_selection(self):
        window = self.series_window()
        entries = window.getControl(520).addItems.call_args.args[0]
        self.assertEqual(entries[1].label, "S01E02 · Chapter 2")
        self.assertEqual(entries[1].label2, "2024-01-02 · 42 min")
        window.getControl(520).selectItem.assert_called_with(1)
        self.assertEqual(window.properties["silo.has_episodes"], "true")

    def test_episodes_load_once_per_season(self):
        window = self.series_window()
        rail = window.getControl(520)
        for position in (1, 0, 1):
            window.getControl(510).getSelectedPosition.return_value = position
            window.season_settled()
        self.assertEqual([call.args[0]["content_id"] for call in self.app.episodes.call_args_list],
                         ["season:1", "season:2"])
        self.assertEqual(rail.addItems.call_args.args[0][0].label, "S02E01 · Chapter 1")

    def test_season_moves_wait_for_focus_to_settle_and_click_applies_now(self):
        window = self.series_window()
        window.season_settle = MagicMock()
        window.onFocus(510)
        window.onAction(Action(RIGHT))
        window.season_settle.trigger.assert_called_once_with()
        self.assertEqual(self.app.episodes.call_count, 1)
        window.getControl(510).getSelectedPosition.return_value = 2
        window.onClick(510)
        window.season_settle.cancel.assert_called_once_with()
        self.assertEqual(self.app.episodes.call_args.args[0]["content_id"], "season:0")

    def test_failed_season_load_is_not_cached(self):
        window = self.series_window()
        self.app.guard.side_effect = lambda callback: None
        window.getControl(510).getSelectedPosition.return_value = 1
        window.season_settled()
        self.assertEqual(window.episodes, [])
        self.assertEqual(window.properties["silo.has_episodes"], "")
        self.assertNotIn("season:2", window.episode_cache)

    def test_focusing_an_episode_swaps_the_hero_and_leaving_restores_it(self):
        window = self.series_window()
        self.assertEqual(window.properties["silo.title"], "EXAMPLE SHOW")
        self.assertEqual(window.properties["silo.logo"], "https://img.example/series:1/logo")
        window.getControl(520).getSelectedPosition.return_value = 2
        window.onFocus(520)
        self.assertEqual(window.properties["silo.supertitle"], "Example Show")
        self.assertEqual(window.properties["silo.title"], "CHAPTER 3")
        self.assertEqual(window.properties["silo.logo"], "")
        self.assertEqual(window.properties["silo.plot"], "Episode 3")
        self.assertIn("S1 · E3", window.properties["silo.meta"])
        self.assertEqual(window.properties["silo.rating"], "TV-14")
        self.assertEqual(window.properties["silo.backdrop"], "https://img.example/series:1/backdrop")
        window.getControl(520).getSelectedPosition.return_value = 0
        window.onAction(Action(LEFT))
        self.assertEqual(window.properties["silo.title"], "CHAPTER 1")
        window.onFocus(105)
        self.assertEqual(window.properties["silo.title"], "EXAMPLE SHOW")
        self.assertEqual(window.properties["silo.supertitle"], "")
        self.assertEqual(window.properties["silo.plot"], "A show.")
        window.getControl(520).getSelectedPosition.return_value = 1
        window.onClick(520)
        self.app.open_item.assert_called_once_with(window.episodes[1])

    def test_season_page_lists_its_episodes_without_capsules(self):
        season = {"content_id": "season:1", "type": "season", "season_number": 1, "title": "Season 1",
                  "play_content_id": "episode:1-3"}
        window = self.window(season, episodes_for({"season_number": 1}))
        window.onInit()
        self.assertEqual(window.properties["silo.has_seasons"], "")
        self.assertEqual(window.properties["silo.has_episodes"], "true")
        self.assertEqual(window.properties["silo.continue_label"], "Play E3")
        self.app.episodes.assert_not_called()

    def test_episode_page_shows_series_title_above_episode_title(self):
        episode = dict(episodes_for({"season_number": 1})[0], series_title="Example Show",
                       versions=movie_item()["versions"], title="Pilot: Part One")
        window = self.window(episode)
        window.onInit()
        self.assertEqual(window.properties["silo.supertitle"], "Example Show")
        self.assertEqual(window.properties["silo.title"], "PILOT")
        self.assertEqual(window.properties["silo.title2"], "PART ONE")
        self.assertEqual(window.properties["silo.logo"], "")
        window.getControl(1103).setPosition.assert_called_with(100, 226)
        window.getControl(1102).setPosition.assert_called_with(100, 146)
        window.getControl(1101).setPosition.assert_called_with(100, 106)

    # ------------------------------------------------- zones and sections
    def test_up_and_down_move_between_zones_and_skip_hidden_ones(self):
        self.app.similar.return_value = [{"content_id": "movie:9", "type": "movie", "title": "Other"}]
        window = self.series_window(cast=[{"name": "Actor One"}])
        self.assertEqual(window.available_zones(), ["buttons", "seasons", "episodes", "cast", "similar"])
        steps = []
        for _ in range(5):
            window.onAction(Action(DOWN))
            steps.append((window.focus, window.scroll_offset))
        self.assertEqual(steps, [(510, 0), (520, 0), (530, 960), (540, 1360), (540, 1360)])
        window.getControl(1000).setPosition.assert_called_with(0, -1360)
        window.onAction(Action(UP))
        self.assertEqual((window.focus, window.scroll_offset), (530, 960))

        movie = movie_item()
        movie["cast"] = [{"name": "Actor One"}]
        self.app.similar.return_value = []
        window = self.window(movie)
        window.onInit()
        window.onFocus(104)
        self.assertEqual(window.available_zones(), ["buttons", "cast"])
        window.onAction(Action(DOWN))
        self.assertEqual((window.focus, window.scroll_offset), (530, 700 - 140))
        window.onAction(Action(DOWN))
        self.assertEqual(window.focus, 530)
        window.onAction(Action(UP))
        # Up returns to the button that was focused last, and the page scrolls back.
        self.assertEqual((window.focus, window.scroll_offset), (104, 0))

    def test_similar_failures_leave_the_section_hidden(self):
        for failure in (ValueError("offline"), None):
            if failure:
                self.app.similar.side_effect = failure
            else:
                self.app.similar.side_effect = None
                self.app.similar.return_value = []
            window = self.window(movie_item())
            window.onInit()
            self.assertEqual(window.properties["silo.has_similar"], "")
            self.assertNotIn("similar", window.available_zones())

    def test_similar_titles_open_their_details(self):
        related = [{"content_id": "movie:9", "type": "movie", "title": "Other", "year": 2020},
                   {"content_id": "person:1", "type": "person"}]
        self.app.similar.return_value = related
        window = self.window(movie_item())
        window.onInit()
        self.assertEqual(window.properties["silo.has_similar"], "true")
        entries = window.getControl(540).addItems.call_args.args[0]
        self.assertEqual([entry.label for entry in entries], ["Other"])
        window.onClick(540)
        self.app.open_item.assert_called_once_with(related[0])

    def test_cast_photos_resolve_and_fall_back_to_initials(self):
        item = movie_item()
        item["cast"] = [{"name": "Actor One", "character": "Hero", "photo_url": "https://cdn.example/a.jpg"},
                        {"name": "Actor Two", "photo_url": "people/b.jpg"},
                        {"name": "Actor Three"}, {"character": "Nameless"}]
        window = self.window(item)
        window.onInit()
        entries = window.getControl(530).addItems.call_args.args[0]
        self.assertEqual([entry.label for entry in entries], ["Actor One", "Actor Two", "Actor Three"])
        self.assertEqual(entries[0].label2, "Hero")
        self.assertEqual(entries[0].art, {"thumb": "https://cdn.example/a.jpg"})
        self.assertEqual(entries[1].art, {"thumb": "https://img.example/people/b.jpg/poster"})
        self.assertEqual(entries[2].art, {})
        self.assertEqual(entries[2].properties["silo.initials"], "AT")
        self.assertEqual(window.properties["silo.has_cast"], "true")

    def test_meta_row_places_chip_badges_then_facts(self):
        item = movie_item()
        item.update(content_rating="PG-13", year=2021, runtime=125)
        window = self.window(item)
        window.onInit()
        self.assertEqual(window.properties["silo.rating"], "PG-13")
        self.assertEqual(window.properties["silo.badge.0"], "4K")
        self.assertEqual(window.properties["silo.badge.1"], "HDR")
        self.assertEqual(window.properties["silo.badge.2"], "")
        self.assertEqual(window.properties["silo.meta"], "2021 · 2h 5m")
        window.getControl(1201).setPosition.assert_called_with(100, 294)
        meta_x = window.getControl(1230).setPosition.call_args.args[0]
        self.assertGreater(meta_x, window.getControl(1212).setPosition.call_args.args[0])

    def test_back_closes_window(self):
        window = self.window(movie_item())
        window.onAction(Action(92))
        self.assertTrue(window.closed)


class DetailsSkinTests(unittest.TestCase):
    def setUp(self):
        self.tree = ET.parse(SKIN)
        self.controls = {int(node.attrib["id"]): node for node in self.tree.iter("control") if "id" in node.attrib}

    def test_generated_skin_is_current(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        try:
            generator = importlib.import_module("skin_details")
        finally:
            sys.path.pop(0)
        self.assertEqual(SKIN.read_text(encoding="utf-8"), generator.build())

    def test_ids_are_unique_and_cover_the_controller(self):
        ids = [int(node.attrib["id"]) for node in self.tree.iter("control") if "id" in node.attrib]
        self.assertEqual(len(ids), len(set(ids)))
        required = {100, 101, 103, 104, 105, 106, 510, 520, 530, 540, 1000, 3000, 4000, 1101, 1102, 1103,
                    1201, 1202, 1230} | {1300 + control_id for control_id in ui_details.BUTTON_ORDER}
        required |= {ui_details.BADGE + slot for slot in range(ui_details.MAX_BADGES)}
        self.assertTrue(required.issubset(ids), required - set(ids))
        for direction in ("onleft", "onright", "onup", "ondown"):
            for node in self.tree.iter(direction):
                self.assertIn(int(node.text), ids)

    def test_every_texture_exists(self):
        names = set()
        for node in self.tree.iter():
            if node.tag.startswith("texture") or node.tag.endswith("texture"):
                if node.text and not node.text.startswith("$"):
                    names.add(node.text)
                if node.get("diffuse"):
                    names.add(node.get("diffuse"))
        self.assertIn("mask_details.png", names)
        for name in names:
            self.assertTrue((MEDIA / name).is_file(), name)

    def test_zone_controls_leave_up_and_down_to_python(self):
        for control_id in ui_details.BUTTON_ORDER + (510, 520, 530, 540):
            for direction in ("onup", "ondown"):
                self.assertEqual([int(node.text) for node in self.controls[control_id].findall(direction)],
                                 [control_id], (control_id, direction))

    def test_button_row_left_right_only_reaches_visible_enabled_buttons(self):
        def condition_matches(condition, visible, enabled):
            for term in condition.split(" + "):
                match = re.fullmatch(r"(!?)Control.Is(Visible|Enabled)\((\d+)\)", term)
                self.assertIsNotNone(match, term)
                negated, kind, target = match.groups()
                value = int(target) in (visible if kind == "Visible" else enabled)
                if value == bool(negated):
                    return False
            return True

        # Movie with resume (both preferences), plain movie, subtitles, series, and a movie without files.
        scenarios = (({100, 104, 103, 106}, set()), ({100, 101, 104}, set()), ({100}, set()),
                     ({100, 106}, set()), ({105}, set()), ({100, 106}, {100}))
        for visible, disabled in scenarios:
            enabled = visible - disabled
            for current in enabled:
                for direction in ("onleft", "onright"):
                    choices = self.controls[current].findall(direction)
                    target = next(int(node.text) for node in choices if not node.get("condition")
                                  or condition_matches(node.attrib["condition"], visible, enabled))
                    self.assertIn(target, enabled, (current, direction, visible))
            order = [control_id for control_id in ui_details.BUTTON_ORDER if control_id in enabled]
            for left, right in zip(order, order[1:]):
                choices = self.controls[left].findall("onright")
                target = next(int(node.text) for node in choices if not node.get("condition")
                              or condition_matches(node.attrib["condition"], visible, enabled))
                self.assertEqual(target, right)


if __name__ == "__main__":
    unittest.main()
