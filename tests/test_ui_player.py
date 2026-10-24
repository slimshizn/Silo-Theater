"""The in-playback Silo player menu, with xbmc and xbmcgui stubbed."""

import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
SKIN = ROOT / "resources/skins/Default/1080i/SiloPlayerMenu.xml"
MEDIA = ROOT / "resources/skins/Default/media"
sys.path.insert(0, str(ROOT / "resources/lib"))


class Kodi:
    """Mutable player state the stubs read from; reset per test."""

    def reset(self):
        self.info = {}
        self.conditions = {}
        self.rpc = {}
        self.rpc_calls = []
        self.builtins = []
        self.player = FakePlayer()
        self.choices = []
        self.selects = []


class FakePlayer:
    def __init__(self):
        self.audio = ["English · AC3 · 5.1", "Commentary"]
        self.subtitles = ["English", "German (forced)"]
        self.time, self.total = 0.0, 3600.0
        self.calls = []
        self.gone = False

    def _call(self, name, *args):
        if self.gone:
            raise RuntimeError("Kodi is not playing any media file")
        self.calls.append((name,) + args)

    def getAvailableAudioStreams(self):
        self._call("getAvailableAudioStreams")
        return list(self.audio)

    def getAvailableSubtitleStreams(self):
        self._call("getAvailableSubtitleStreams")
        return list(self.subtitles)

    def getTime(self):
        self._call("getTime")
        return self.time

    def getTotalTime(self):
        self._call("getTotalTime")
        return self.total

    def __getattr__(self, name):
        if name in ("setAudioStream", "setSubtitleStream", "showSubtitles", "seekTime", "pause", "stop"):
            return lambda *args: self._call(name, *args)
        raise AttributeError(name)

    def actions(self):
        return [call for call in self.calls if not call[0].startswith("get")]


KODI = Kodi()


def execute_jsonrpc(raw):
    request = json.loads(raw)
    KODI.rpc_calls.append((request["method"], request.get("params")))
    answer = KODI.rpc.get(request["method"], "OK")
    if answer == "error":
        return json.dumps({"id": 1, "jsonrpc": "2.0", "error": {"code": -32601, "message": "Method not found."}})
    return json.dumps({"id": 1, "jsonrpc": "2.0", "result": answer})


XBMC = SimpleNamespace(
    getInfoLabel=lambda name: KODI.info.get(name, ""),
    getCondVisibility=lambda name: KODI.conditions.get(name, False),
    executeJSONRPC=execute_jsonrpc,
    executebuiltin=lambda command: KODI.builtins.append(command),
    Player=lambda: KODI.player,
)


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2, self.properties = label, label2, {}

    def getLabel(self):
        return self.label

    def setLabel2(self, value):
        self.label2 = value

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")


class FakeControl:
    def __init__(self):
        self.items, self.selected = [], 0
        self.position = self.width = self.height = self.up = None
        self.resets = 0

    def setPosition(self, left, top):
        self.position = (left, top)

    def setWidth(self, width):
        self.width = width

    def setHeight(self, height):
        self.height = height

    def reset(self):
        self.items, self.resets = [], self.resets + 1

    def addItems(self, items):
        self.items.extend(items)

    def size(self):
        return len(self.items)

    def getListItem(self, index):
        return self.items[index]

    def selectItem(self, index):
        self.selected = index

    def getSelectedPosition(self):
        return self.selected

    def controlUp(self, other):
        self.up = other


class FakeWindow:
    created = []

    def __new__(cls, *args, **kwargs):
        window = object.__new__(cls)
        window.arguments = args
        window.properties, window.controls = {}, {}
        window.focus, window.closes, window.shown = None, 0, 0
        FakeWindow.created.append(window)
        return window

    def getControl(self, key):
        if self.closes:
            raise RuntimeError("Non-Existent Control {}".format(key))
        return self.controls.setdefault(key, FakeControl())

    def setProperty(self, key, value):
        self.properties[key] = value

    def setFocusId(self, key):
        self.focus = key
        self.onFocus(key)

    def getFocusId(self):
        return self.focus

    def close(self):
        self.closes += 1

    def show(self):
        self.shown += 1
        self.onInit()


class FakeDialog:
    def select(self, heading, labels, preselect=-1):
        KODI.selects.append((heading, labels, preselect))
        return KODI.choices.pop(0) if KODI.choices else -1


GUI = SimpleNamespace(WindowXMLDialog=FakeWindow, ListItem=FakeListItem, Dialog=FakeDialog)
with patch.dict(sys.modules, {"xbmc": XBMC, "xbmcgui": GUI}):
    for name in ("silo.ui_player", "silo.ui_common"):
        sys.modules.pop(name, None)
    ui_player = importlib.import_module("silo.ui_player")
    sys.modules.pop("skin_player", None)
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        skin_player = importlib.import_module("skin_player")
    finally:
        sys.path.remove(str(ROOT / "scripts"))

STATS_INFO = {
    "Player.Process(videodecoder)": "hevc",
    "Player.Process(pixformat)": "yuv420p10le",
    "Player.Process(videowidth)": "3840",
    "Player.Process(videoheight)": "2160",
    "Player.Process(videofps)": "23.976025",
    "Player.Process(videodar)": "1.78",
    "Player.Process(deintmethod)": "",
    "VideoPlayer.HdrType": "hdr10",
    "Player.Process(audiodecoder)": "eac3",
    "Player.Process(audiochannels)": "FL,FR,FC,LFE,BL,BR",
    "Player.Process(audiosamplerate)": "48000",
    "Player.Process(audiobitspersample)": "24",
    "Player.CacheLevel": "87",
    "System.CPUUsage": "",
}

CHAPTERS = [{"title": "Opening", "start": 0, "end": 300},
            {"title": "The Middle", "start": 300, "end": 1800},
            {"title": "", "start": 1800}]


def context(**extra):
    base = {"title": "Example Show", "eyebrow": "S1:E2", "runtime": 47, "badges": ["4K", "HDR10", "EAC3"],
            "route": "Direct play", "source": "", "versions": [], "file_id": "f1", "chapters": CHAPTERS,
            "is_episode": True, "auto_next": True, "skip_prompts": False, "sleep_minutes": 0}
    base.update(extra)
    return base


def action(action_id):
    return SimpleNamespace(getId=lambda: action_id)


BACK, LEFT, RIGHT, UP, DOWN = 92, 1, 2, 3, 4


class MenuTestCase(unittest.TestCase):
    def setUp(self):
        KODI.reset()
        KODI.info.update(STATS_INFO)
        FakeWindow.created = []
        self.menu = ui_player.PlayerMenu(str(ROOT))

    def open(self, tab=None, **extra):
        self.menu.open(context(**extra), tab=tab)
        return FakeWindow.created[-1]

    def rows(self, window):
        return [(item.label, item.label2) for item in window.controls[ui_player.ROWS].items]

    def facts(self, window, control_id):
        return [(item.label, item.label2) for item in window.controls[control_id].items]

    def click_row(self, window, label):
        position = next(index for index, row in enumerate(window.rows) if row["label"] == label)
        window.controls[ui_player.ROWS].selected = position
        window.onClick(ui_player.ROWS)


class OpenAndTabsTests(MenuTestCase):
    def test_open_shows_modeless_window_on_info_with_focus_on_the_rail(self):
        window = self.open()
        self.assertEqual(window.arguments, ("SiloPlayerMenu.xml", str(ROOT), "Default", "1080i"))
        self.assertEqual(window.shown, 1)
        self.assertTrue(self.menu.is_open())
        self.assertEqual(window.properties["silo.tab"], "info")
        self.assertEqual(window.focus, ui_player.TAB_BUTTON["info"])
        self.assertEqual(window.tabs, list(ui_player.TAB_NAMES))

    def test_open_on_a_named_tab(self):
        window = self.open(tab="subtitles")
        self.assertEqual(window.properties["silo.tab"], "subtitles")
        self.assertEqual(window.properties["silo.rows"], "true")
        self.assertEqual(window.focus, ui_player.TAB_BUTTON["subtitles"])

    def test_no_audio_streams_hides_audio(self):
        KODI.player.audio = []
        window = self.open(tab="audio")
        self.assertNotIn("audio", window.tabs)
        self.assertEqual(window.properties["silo.tab.audio"], "")
        self.assertEqual(window.properties["silo.tab"], "info")

    def test_no_chapters_hides_chapters(self):
        window = self.open(chapters=[])
        self.assertNotIn("chapters", window.tabs)
        self.assertEqual(window.properties["silo.tab.chapters"], "")
        self.assertEqual(window.properties["silo.tab.video"], "true")

    def test_all_empty_stats_hide_stats(self):
        KODI.info.clear()
        window = self.open()
        self.assertNotIn("stats", window.tabs)
        self.assertEqual(window.tabs, ["info", "video", "audio", "subtitles", "chapters"])

    def test_player_gone_hides_audio_and_does_not_raise(self):
        KODI.player.gone = True
        window = self.open()
        self.assertEqual(window.tabs, ["info", "stats", "video", "subtitles", "chapters"])
        self.menu.refresh()

    def test_rail_is_centred_on_the_visible_tabs(self):
        KODI.player.audio = []
        window = self.open()
        rail = window.controls[ui_player.RAIL]
        widths = [ui_player.TAB_WIDTH[name] for name in window.tabs]
        width = sum(widths) + 4 * (len(widths) - 1) + 16
        self.assertEqual(rail.width, width)
        self.assertEqual(rail.position, ((1920 - width) // 2, ui_player.RAIL_TOP))
        self.assertEqual(window.controls[ui_player.TAB_GROUP["info"]].position,
                         ((1920 - width) // 2 + 8, ui_player.TAB_TOP))

    def test_reopen_while_open_updates_the_same_window(self):
        window = self.open()
        self.menu.open(context(title="Another"), tab="video")
        self.assertEqual(len(FakeWindow.created), 1)
        self.assertEqual(window.properties["silo.tab"], "video")


class NavigationTests(MenuTestCase):
    def test_left_right_switch_tabs_and_skip_hidden_ones(self):
        KODI.player.audio = []
        window = self.open(tab="video")
        window.onAction(action(RIGHT))
        self.assertEqual(window.tab, "subtitles")
        self.assertEqual(window.focus, ui_player.TAB_BUTTON["subtitles"])
        window.onAction(action(LEFT))
        window.onAction(action(LEFT))
        window.onAction(action(LEFT))
        self.assertEqual(window.tab, "info")
        window.onAction(action(LEFT))  # No wrap at the first tab.
        self.assertEqual(window.tab, "info")

    def test_up_from_rows_returns_to_the_selected_tab(self):
        window = self.open(tab="audio")
        rows = window.controls[ui_player.ROWS]
        self.assertIs(rows.up, window.controls[ui_player.TAB_BUTTON["audio"]])
        window.onAction(action(RIGHT))
        self.assertIs(rows.up, window.controls[ui_player.TAB_BUTTON["subtitles"]])

    def test_left_right_inside_rows_do_not_switch_tabs(self):
        window = self.open(tab="audio")
        window.focus = ui_player.ROWS
        window.onAction(action(RIGHT))
        self.assertEqual(window.tab, "audio")

    def test_select_on_a_row_tab_enters_rows(self):
        window = self.open(tab="video")
        window.onClick(ui_player.TAB_BUTTON["video"])
        self.assertEqual(window.focus, ui_player.ROWS)
        window.focus = ui_player.TAB_BUTTON["info"]
        window.onClick(ui_player.TAB_BUTTON["info"])
        self.assertEqual(window.focus, ui_player.TAB_BUTTON["info"])

    def test_back_closes_a_picker_first_then_the_menu(self):
        window = self.open(tab="video")
        KODI.choices = [-1]  # Back inside the picker.
        self.click_row(window, "Sleep timer")
        self.assertTrue(self.menu.is_open())
        self.assertEqual(window.closes, 0)
        window.onAction(action(BACK))
        self.assertFalse(self.menu.is_open())
        self.assertEqual(window.closes, 1)
        self.assertEqual(self.menu.take_result(), [])

    def test_close_is_idempotent_and_safe_before_open(self):
        self.menu.close()
        window = self.open()
        self.menu.close()
        self.menu.close()
        self.assertEqual(window.closes, 1)
        self.assertFalse(self.menu.is_open())
        self.menu.refresh()

    def test_close_after_back_does_not_close_twice_and_reopen_makes_a_new_window(self):
        window = self.open()
        window.onAction(action(BACK))
        self.menu.close()
        self.assertEqual(window.closes, 1)
        self.open()
        self.assertEqual(len(FakeWindow.created), 2)
        self.assertTrue(self.menu.is_open())

    def test_player_keys_reach_the_player(self):
        window = self.open()
        window.onAction(action(12))
        window.onAction(action(79))  # Play while playing does nothing.
        KODI.conditions["Player.Paused"] = True
        window.onAction(action(79))
        window.onAction(action(14))
        self.assertEqual(KODI.player.actions(), [("pause",), ("pause",)])
        self.assertEqual(KODI.builtins, ["PlayerControl(Next)"])
        self.assertTrue(self.menu.is_open())
        window.onAction(action(13))
        self.assertEqual(KODI.player.actions()[-1], ("stop",))
        self.assertFalse(self.menu.is_open())


class InfoAndStatsTests(MenuTestCase):
    def test_info_header_and_rows(self):
        KODI.info.update({"VideoPlayer.VideoCodec": "hevc", "VideoPlayer.VideoResolution": "2160",
                          "VideoPlayer.AudioCodec": "eac3", "VideoPlayer.AudioChannels": "6"})
        KODI.player.time = 420
        window = self.open()
        self.assertEqual(window.properties["silo.eyebrow"], "S1:E2")
        self.assertEqual(window.properties["silo.title"], "Example Show")
        self.assertEqual(window.properties["silo.runtime"], "47 min")
        self.assertEqual([window.properties["silo.badge.{}".format(i)] for i in range(5)],
                         ["4K", "HDR10", "EAC3", "", ""])
        self.assertEqual(self.facts(window, ui_player.INFO_ROWS), [
            ("Route", "Direct play"), ("Video", "HEVC · 2160p"), ("Audio", "EAC3 · 5.1"),
            ("Subtitles", "Off"), ("Chapter", "The Middle")])

    def test_info_shows_source_and_current_subtitle(self):
        KODI.rpc["Player.GetProperties"] = {"subtitleenabled": True,
                                            "currentsubtitle": {"index": 1, "name": "", "language": "ger"},
                                            "currentaudiostream": {"index": 0}}
        window = self.open(source="Original file")
        facts = dict(self.facts(window, ui_player.INFO_ROWS))
        self.assertEqual(facts["Source"], "Original file")
        self.assertEqual(facts["Subtitles"], "ger")

    def test_stats_rows_from_infolabels(self):
        KODI.conditions["Player.Process(videohwdecoder)"] = True
        window = self.open(tab="stats")
        self.assertEqual(self.facts(window, ui_player.STATS_LEFT), [
            ("Video decoder", "hevc (hardware)"), ("Pixel format", "yuv420p10le"), ("Resolution", "3840x2160"),
            ("Frame rate", "23.976 fps"), ("Aspect", "1.78"), ("HDR", "HDR10")])
        self.assertEqual(self.facts(window, ui_player.STATS_RIGHT), [
            ("Audio decoder", "eac3"), ("Channels", "5.1"), ("Sample rate", "48000 Hz"), ("Bits", "24"),
            ("Cache", "87 %")])

    def test_stats_without_hardware_and_with_zero_sizes(self):
        KODI.info.update({"Player.Process(videowidth)": "0", "Player.Process(audiochannels)": "FL,FR",
                          "System.CPUUsage": "12%"})
        left, right = ui_player.stat_rows()
        self.assertEqual(dict(left)["Video decoder"], "hevc")
        self.assertNotIn("Resolution", dict(left))
        self.assertEqual(dict(right)["Channels"], "Stereo")
        self.assertEqual(dict(right)["CPU"], "12%")

    def test_refresh_updates_stats_in_place(self):
        window = self.open(tab="stats")
        control = window.controls[ui_player.STATS_RIGHT]
        resets = control.resets
        KODI.info["Player.CacheLevel"] = "42"
        self.menu.refresh()
        self.assertEqual(control.resets, resets)
        self.assertEqual(dict(self.facts(window, ui_player.STATS_RIGHT))["Cache"], "42 %")

    def test_refresh_hides_stats_when_values_vanish(self):
        window = self.open(tab="stats")
        KODI.info.clear()
        self.menu.refresh()
        self.assertNotIn("stats", window.tabs)
        self.assertEqual(window.tab, "info")
        self.assertEqual(window.focus, ui_player.TAB_BUTTON["info"])

    def test_panel_height_stays_within_bounds(self):
        window = self.open(tab="stats")
        height = window.controls[ui_player.PANEL].height
        self.assertTrue(ui_player.PANEL_MIN <= height <= ui_player.PANEL_MAX)


class VideoTabTests(MenuTestCase):
    VERSIONS = [{"file_id": "f1", "label": "4K HDR"}, {"file_id": "f2", "label": "1080p"}]

    def test_rows_for_an_episode_with_versions(self):
        KODI.rpc["Player.GetViewMode"] = {"viewmode": "zoom"}
        window = self.open(tab="video", versions=self.VERSIONS, sleep_minutes=25)
        self.assertEqual(self.rows(window), [
            ("Version", "4K HDR"), ("Speed", "Normal"), ("Aspect", "Zoom"), ("Sleep timer", "25 min left"),
            ("Skip buttons", "Off"), ("Auto-play next", "On")])

    def test_one_version_and_movies_hide_rows(self):
        window = self.open(tab="video", versions=self.VERSIONS[:1], is_episode=False)
        labels = [label for label, _ in self.rows(window)]
        self.assertNotIn("Version", labels)
        self.assertNotIn("Auto-play next", labels)

    def test_speed_row_hidden_when_jsonrpc_returns_an_error(self):
        KODI.rpc["JSONRPC.Introspect"] = "error"
        window = self.open(tab="video")
        self.assertNotIn("Speed", [label for label, _ in self.rows(window)])

    def test_speed_sets_tempo_and_an_error_hides_the_row(self):
        window = self.open(tab="video")
        KODI.choices = [3]
        self.click_row(window, "Speed")
        self.assertIn(("Player.SetTempo", {"playerid": 1, "tempo": 1.5}), KODI.rpc_calls)
        self.assertEqual(dict(self.rows(window))["Speed"], "1.5×")
        self.assertEqual(KODI.selects[-1][2], 1)
        KODI.rpc["Player.SetTempo"] = "error"
        KODI.choices = [0]
        self.click_row(window, "Speed")
        self.assertNotIn("Speed", dict(self.rows(window)))
        self.assertTrue(self.menu.is_open())

    def test_speed_from_context(self):
        window = self.open(tab="video", speed=1.25)
        self.assertEqual(dict(self.rows(window))["Speed"], "1.25×")

    def test_aspect_sets_view_mode(self):
        window = self.open(tab="video")
        KODI.choices = [2]
        self.click_row(window, "Aspect")
        self.assertIn(("Player.SetViewMode", {"viewmode": "stretch16x9"}), KODI.rpc_calls)

    def test_version_intent_closes_the_menu(self):
        window = self.open(tab="video", versions=self.VERSIONS)
        KODI.choices = [1]
        self.click_row(window, "Version")
        self.assertEqual(KODI.selects[-1], ("Version", ["4K HDR", "1080p"], 0))
        self.assertFalse(self.menu.is_open())
        self.assertEqual(self.menu.take_result(), [{"action": "version", "file_id": "f2"}])
        self.assertEqual(self.menu.take_result(), [])

    def test_picking_the_current_version_does_nothing(self):
        window = self.open(tab="video", versions=self.VERSIONS)
        KODI.choices = [0]
        self.click_row(window, "Version")
        self.assertTrue(self.menu.is_open())
        self.assertEqual(self.menu.take_result(), [])

    def test_sleep_intent_and_cancel(self):
        window = self.open(tab="video")
        KODI.choices = [2]
        self.click_row(window, "Sleep timer")
        self.assertEqual(KODI.selects[-1][1], ["Off", "15 min", "30 min", "45 min", "1h", "1h 30m"])
        self.assertEqual(dict(self.rows(window))["Sleep timer"], "30 min left")
        KODI.choices = [0]
        self.click_row(window, "Sleep timer")
        self.assertEqual(KODI.selects[-1][1][0], "Cancel timer")
        self.assertEqual(self.menu.take_result(),
                         [{"action": "sleep", "minutes": 30}, {"action": "sleep", "minutes": 0}])
        self.assertTrue(self.menu.is_open())

    def test_off_when_no_timer_queues_nothing(self):
        window = self.open(tab="video")
        KODI.choices = [0]
        self.click_row(window, "Sleep timer")
        self.assertEqual(self.menu.take_result(), [])

    def test_skip_prompts_and_auto_next_toggle(self):
        window = self.open(tab="video")
        self.click_row(window, "Skip buttons")
        self.click_row(window, "Auto-play next")
        self.click_row(window, "Skip buttons")
        self.assertEqual(self.menu.take_result(), [
            {"action": "skip_prompts", "value": True}, {"action": "auto_next", "value": False},
            {"action": "skip_prompts", "value": False}])
        self.assertEqual(dict(self.rows(window))["Auto-play next"], "Off")
        self.assertEqual(window.controls[ui_player.ROWS].selected, 3)  # The cursor stays on "Skip buttons".
        self.assertTrue(self.menu.is_open())


class TrackTabTests(MenuTestCase):
    def test_audio_rows_mark_current_and_switch_locally(self):
        KODI.rpc["Player.GetProperties"] = {"currentaudiostream": {"index": 1}, "currentsubtitle": {},
                                            "subtitleenabled": False}
        window = self.open(tab="audio")
        self.assertEqual([row["label"] for row in window.rows],
                         ["English · AC3 · 5.1", "Commentary", "Audio settings (delay, passthrough)…"])
        self.assertEqual([item.getProperty("silo.checked") for item in window.controls[ui_player.ROWS].items],
                         ["", "true", ""])
        self.assertEqual(window.controls[ui_player.ROWS].selected, 1)
        self.click_row(window, "English · AC3 · 5.1")
        self.assertEqual(KODI.player.actions(), [("setAudioStream", 0)])
        self.assertTrue(self.menu.is_open())
        # The pick is passed on to be remembered for this title or show.
        self.assertEqual(self.menu.take_result(), [{"action": "audio_choice", "language": "English · AC3 · 5.1",
                                                    "title": "English · AC3 · 5.1"}])

    def test_audio_picks_carry_kodis_stream_language(self):
        KODI.rpc["Player.GetProperties"] = {"currentaudiostream": {"index": 0}, "currentsubtitle": {},
                                            "subtitleenabled": False,
                                            "audiostreams": [{"index": 0, "language": "eng", "name": "Main"},
                                                             {"index": 1, "language": "jpn", "name": ""}]}
        window = self.open(tab="audio")
        self.click_row(window, "Commentary")
        self.assertEqual(self.menu.take_result(), [{"action": "audio_choice", "language": "jpn",
                                                    "title": "Commentary"}])

    def test_audio_settings_intent(self):
        window = self.open(tab="audio")
        self.click_row(window, "Audio settings (delay, passthrough)…")
        self.assertEqual(self.menu.take_result(), [{"action": "native", "window": "osdaudiosettings"}])
        self.assertFalse(self.menu.is_open())

    def test_subtitle_on_and_off(self):
        window = self.open(tab="subtitles")
        self.assertEqual([row["label"] for row in window.rows],
                         ["Off", "English", "German (forced)", "Subtitle style and timing…"])
        self.assertEqual(window.rows[0]["checked"], True)
        self.click_row(window, "English")
        self.assertEqual(KODI.player.actions(), [("setSubtitleStream", 0), ("showSubtitles", True)])
        self.click_row(window, "Off")
        self.assertEqual(KODI.player.actions()[-1], ("showSubtitles", False))
        self.assertTrue(self.menu.is_open())

    def test_subtitle_settings_intent(self):
        window = self.open(tab="subtitles")
        self.click_row(window, "Subtitle style and timing…")
        self.assertEqual(self.menu.take_result(), [{"action": "native", "window": "osdsubtitlesettings"}])
        self.assertFalse(self.menu.is_open())

    def test_chapters_table_and_seek(self):
        KODI.player.time = 1900
        window = self.open(tab="chapters")
        items = window.controls[ui_player.ROWS].items
        self.assertEqual([(item.label, item.getProperty("silo.number"), item.getProperty("silo.start"),
                           item.getProperty("silo.length"), item.getProperty("silo.checked")) for item in items], [
            ("Opening", "1", "0:00", "5:00", ""),
            ("The Middle", "2", "5:00", "25:00", ""),
            ("Chapter 3", "3", "30:00", "30:00", "true")])
        self.click_row(window, "The Middle")
        self.assertEqual(KODI.player.actions(), [("seekTime", 300.0)])
        self.assertFalse(self.menu.is_open())

    def test_player_gone_during_a_click_does_not_raise(self):
        window = self.open(tab="subtitles")
        KODI.player.gone = True
        self.click_row(window, "English")
        self.assertTrue(self.menu.is_open())


class ConvertedMenuRowTests(MenuTestCase):
    QUALITIES = [{"value": "original", "label": "Original"}, {"value": "1080p-medium", "label": "1080p Medium"}]
    TRACKS = [{"index": 0, "label": "ENG · TRUEHD · 7.1", "language": "eng"},
              {"index": 1, "label": "Commentary · AAC · Stereo", "language": "eng", "title": "Commentary"}]

    def test_quality_row_shows_the_current_choice_and_queues_a_replan(self):
        window = self.open(tab="video", qualities=self.QUALITIES, quality="auto")
        self.assertEqual(self.rows(window)[0], ("Quality", "Auto"))
        KODI.choices = [2]
        self.click_row(window, "Quality")
        self.assertEqual(KODI.selects[-1], ("Quality", ["Auto", "Original", "1080p Medium"], 0))
        self.assertEqual(self.menu.take_result(), [{"action": "quality", "value": "1080p-medium"}])
        self.assertFalse(self.menu.is_open())

    def test_picking_the_current_quality_or_backing_out_queues_nothing(self):
        window = self.open(tab="video", qualities=self.QUALITIES, quality="original")
        self.assertEqual(dict(self.rows(window))["Quality"], "Original")
        KODI.choices = [1]
        self.click_row(window, "Quality")
        KODI.choices = [-1]
        self.click_row(window, "Quality")
        self.assertEqual(self.menu.take_result(), [])
        self.assertTrue(self.menu.is_open())

    def test_no_server_choices_hide_the_quality_row(self):
        window = self.open(tab="video", qualities=[])
        self.assertNotIn("Quality", dict(self.rows(window)))

    def test_converted_audio_lists_the_files_tracks_and_asks_for_a_new_stream(self):
        KODI.player.audio = ["aac"]
        window = self.open(tab="audio", converted=True, audio_tracks=self.TRACKS, audio_index=1)
        self.assertEqual([row["label"] for row in window.rows][:2], [track["label"] for track in self.TRACKS])
        self.assertEqual([row.get("checked") for row in window.rows][:2], [False, True])
        self.click_row(window, "Commentary · AAC · Stereo")  # already playing
        self.assertEqual(self.menu.take_result(), [])
        self.click_row(window, "ENG · TRUEHD · 7.1")
        self.assertEqual(self.menu.take_result(), [{"action": "audio_choice", "language": "eng", "title": ""},
                                                   {"action": "audio_track", "index": 0}])
        self.assertNotIn(("setAudioStream", 0), KODI.player.actions())
        self.assertFalse(self.menu.is_open())

    def test_converted_audio_keeps_the_tab_even_when_kodi_sees_no_streams(self):
        KODI.player.audio = []
        window = self.open(converted=True, audio_tracks=self.TRACKS, audio_index=0)
        self.assertIn("audio", window.tabs)

    def test_direct_play_audio_still_switches_locally(self):
        window = self.open(tab="audio", converted=False, audio_tracks=self.TRACKS, audio_index=0)
        self.click_row(window, "Commentary")
        self.assertIn(("setAudioStream", 1), KODI.player.actions())
        self.assertEqual(self.menu.take_result(), [{"action": "audio_choice", "language": "Commentary",
                                                    "title": "Commentary"}])

    def test_subtitle_tools_queue_intents_and_close(self):
        window = self.open(tab="subtitles", subtitle_tools=True, ai_translate=True, subtitle_sync=True)
        labels = [row["label"] for row in window.rows]
        self.assertEqual(labels[-4:], ["Search online…", "Translate with AI…", "Sync to audio…",
                                       "Subtitle style and timing…"])
        self.click_row(window, "Sync to audio…")
        self.assertEqual(self.menu.take_result(), [{"action": "subtitle_tool", "screen": "subtitle_sync"}])
        self.assertFalse(self.menu.is_open())

    def test_ai_and_sync_rows_need_server_support(self):
        window = self.open(tab="subtitles", subtitle_tools=True, ai_translate=False, subtitle_sync=False)
        labels = [row["label"] for row in window.rows]
        self.assertIn("Search online…", labels)
        self.assertNotIn("Translate with AI…", labels)
        self.assertNotIn("Sync to audio…", labels)
        window = self.open(tab="subtitles")
        self.assertNotIn("Search online…", [row["label"] for row in window.rows])


class HelperTests(unittest.TestCase):
    def test_clock(self):
        self.assertEqual(ui_player.clock(5), "0:05")
        self.assertEqual(ui_player.clock(754.9), "12:34")
        self.assertEqual(ui_player.clock(3723), "1:02:03")
        self.assertEqual(ui_player.clock(None), "")
        self.assertEqual(ui_player.clock(-1), "")

    def test_channels(self):
        self.assertEqual(ui_player.channels_text("1"), "Mono")
        self.assertEqual(ui_player.channels_text("2"), "Stereo")
        self.assertEqual(ui_player.channels_text("8"), "7.1")
        self.assertEqual(ui_player.channels_text("FL,FR,FC,LFE,BL,BR,SL,SR"), "7.1")
        self.assertEqual(ui_player.channels_text(""), "")

    def test_chapter_list_sorts_and_skips_bad_entries(self):
        chapters = ui_player.chapter_list([{"start": 60, "title": "B"}, {"start": "x"}, {"start": 0, "title": "A"}],
                                          total=None)
        self.assertEqual([(c["title"], c["start"], c["length"]) for c in chapters], [("A", 0, 60), ("B", 60, None)])
        self.assertEqual(ui_player.current_chapter(chapters, 59), 0)
        self.assertEqual(ui_player.current_chapter(chapters, None), -1)


def controls_by_id(tree):
    return {int(node.attrib["id"]): node for node in tree.iter("control") if "id" in node.attrib}


class SkinTests(unittest.TestCase):
    def test_committed_xml_matches_the_generator(self):
        self.assertEqual(SKIN.read_text(encoding="utf-8"), skin_player.build())

    def test_well_formed_with_unique_ids_and_valid_nav(self):
        tree = ET.parse(SKIN)
        ids = [int(node.attrib["id"]) for node in tree.iter("control") if "id" in node.attrib]
        self.assertEqual(len(ids), len(set(ids)))
        for direction in ("onleft", "onright", "onup", "ondown"):
            for node in tree.iter(direction):
                self.assertIn(int(node.text), ids)
        self.assertIn(int(tree.getroot().findtext("defaultcontrol")), ids)
        self.assertEqual(tree.getroot().get("type"), "dialog")

    def test_referenced_textures_exist(self):
        tree = ET.parse(SKIN)
        for node in tree.iter():
            if node.tag.startswith("texture") or node.tag in ("midtexture", "lefttexture", "righttexture"):
                names = [node.get("diffuse")]
                if node.text and "$" not in node.text:
                    names.append(node.text.strip())
                for texture in filter(None, names):
                    self.assertTrue((MEDIA / texture).is_file(), texture)

    def test_controls_match_the_controller(self):
        controls = controls_by_id(ET.parse(SKIN))
        for name in ui_player.TAB_NAMES:
            button = controls[ui_player.TAB_BUTTON[name]]
            self.assertEqual(button.get("type"), "button")
            self.assertEqual(controls[ui_player.TAB_GROUP[name]].get("type"), "group")
            self.assertEqual(button.findtext("width"), str(ui_player.TAB_WIDTH[name]))
            self.assertEqual(button.findtext("ondown"),
                             str(ui_player.ROWS if name in ui_player.ROW_TABS else ui_player.TAB_BUTTON[name]))
        for control_id in (ui_player.ROWS, ui_player.INFO_ROWS, ui_player.STATS_LEFT, ui_player.STATS_RIGHT):
            self.assertEqual(controls[control_id].get("type"), "list")
        self.assertEqual(controls[ui_player.PANEL].get("type"), "image")
        self.assertEqual(controls[ui_player.RAIL].get("type"), "image")
        for slot in range(ui_player.MAX_BADGES):
            self.assertIn(ui_player.BADGE + slot, controls)
            self.assertIn(ui_player.BADGE_LABEL + slot, controls)

    def test_no_full_screen_dim_and_smoke_panel(self):
        tree = ET.parse(SKIN)
        for node in tree.iter("control"):
            if node.get("type") == "image":
                self.assertFalse(node.findtext("width") == "1920" and node.findtext("height") == "1080")
        panel = controls_by_id(tree)[ui_player.PANEL].find("texture")
        self.assertEqual((panel.text, panel.get("colordiffuse")), ("round28.png", "F2131417"))


if __name__ == "__main__":
    unittest.main()
