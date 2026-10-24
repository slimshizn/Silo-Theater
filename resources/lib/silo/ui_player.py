"""The Silo player menu: a modeless card drawn over fullscreen video.

Follows the Silo Android TV player HUD: a capsule tab rail at the top centre
and a smoked panel under it with label/value rows. The playback controller
owns the polling loop; this module only reads Kodi's player state, applies
local changes (audio and subtitle tracks, chapters, speed, aspect) and queues
intents the controller acts on (``take_result``).

Every callback returns quickly. Anything that talks to ``xbmc.Player``,
``getInfoLabel`` or JSON-RPC tolerates a player that has already gone.
"""

import json
import threading

import xbmc
import xbmcgui

from . import present
from .ui_common import ACTION_LEFT, ACTION_RIGHT, BACK_ACTIONS, set_properties

MENU_XML = "SiloPlayerMenu.xml"

# Tabs in rail order: (name, label, width). Widths fit the font25_title labels.
TABS = (
    ("info", "Info", 120),
    ("stats", "Stats", 136),
    ("video", "Video", 136),
    ("audio", "Audio", 136),
    ("subtitles", "Subtitles", 188),
    ("chapters", "Chapters", 180),
)
TAB_NAMES = tuple(name for name, _, _ in TABS)
TAB_BUTTON = {name: 101 + index for index, name in enumerate(TAB_NAMES)}
TAB_GROUP = {name: 111 + index for index, name in enumerate(TAB_NAMES)}
TAB_WIDTH = {name: width for name, _, width in TABS}
ROW_TABS = frozenset({"video", "audio", "subtitles", "chapters"})

# SiloPlayerMenu.xml control ids
RAIL = 90
ROWS = 200
INFO_ROWS = 220
STATS_LEFT, STATS_RIGHT = 230, 231
PANEL = 300
BADGE, BADGE_LABEL = 411, 421
MAX_BADGES = 5

# Geometry (1080p; Android dp x 2)
SCREEN_WIDTH = 1920
RAIL_TOP, RAIL_HEIGHT, RAIL_PADDING, TAB_GAP = 40, 80, 8, 4
TAB_TOP, TAB_HEIGHT = RAIL_TOP + RAIL_PADDING, 64
PANEL_LEFT, PANEL_TOP, PANEL_WIDTH = 180, 140, 1560
PANEL_MIN, PANEL_MAX = 312, 672
PAD_X, PAD_TOP, PAD_BOTTOM = 44, 40, 36
CONTENT_LEFT, CONTENT_TOP = PANEL_LEFT + PAD_X, PANEL_TOP + PAD_TOP
CONTENT_WIDTH = PANEL_WIDTH - 2 * PAD_X
ROW_HEIGHT = 68
FACT_HEIGHT = 52  # Info and Stats rows (read only)
CHAPTER_HEADER = 44
INFO_HEADER = 236  # eyebrow, title, runtime and badges in the left column
BADGE_TOP = CONTENT_TOP + 176
BADGE_GAP = 10
BADGE_HEIGHT = 36

# Actions forwarded to the player while the menu has focus.
ACTION_PAUSE, ACTION_STOP, ACTION_NEXT_ITEM, ACTION_PREV_ITEM = 12, 13, 14, 15
ACTION_PLAYER_PLAY = 79
ACTION_CONTEXT_MENU = 117  # The Menu key toggles the menu closed.

SPEEDS = (0.75, 1.0, 1.25, 1.5, 2.0)
VIEW_MODES = (("normal", "Fit"), ("zoom", "Zoom"), ("stretch16x9", "Stretch"))
SLEEP_CHOICES = (15, 30, 45, 60, 90)
HDR_NAMES = {"hdr10": "HDR10", "hdr10plus": "HDR10+", "dolbyvision": "Dolby Vision", "hlg": "HLG"}
VIDEO_PLAYER_ID = 1  # Kodi's video player is always player 1.


# ------------------------------------------------------------------ helpers

def clock(seconds):
    """``1:02:03`` or ``2:03``; "" for missing or negative times."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError, OverflowError):
        return ""
    if total < 0:
        return ""
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "{}:{:02d}:{:02d}".format(hours, minutes, secs)
    return "{}:{:02d}".format(minutes, secs)


def _text(value):
    return str(value).strip() if value not in (None, "") else ""


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number == number else None


def info_label(name):
    try:
        return _text(xbmc.getInfoLabel(name))
    except (RuntimeError, TypeError):
        return ""


def condition(name):
    try:
        return bool(xbmc.getCondVisibility(name))
    except (RuntimeError, TypeError):
        return False


def jsonrpc(method, params=None):
    """``(ok, result)`` for one JSON-RPC call; ``ok`` is False on any error."""
    request = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        request["params"] = params
    try:
        answer = json.loads(xbmc.executeJSONRPC(json.dumps(request)))
    except (RuntimeError, TypeError, ValueError):
        return False, None
    if not isinstance(answer, dict) or "error" in answer or "result" not in answer:
        return False, None
    return True, answer["result"]


def player_call(name, *args, default=None):
    """Call an ``xbmc.Player`` method; ``default`` when the player is gone."""
    try:
        return getattr(xbmc.Player(), name)(*args)
    except (RuntimeError, AttributeError, TypeError):
        return default


def channels_text(value):
    """Channel count or Kodi's channel layout as ``Stereo`` / ``5.1``."""
    value = _text(value)
    if not value:
        return ""
    parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    if len(parts) > 1:
        count = len(parts)
        lfe = any(part.upper() == "LFE" for part in parts)
    else:
        number = _number(value)
        if number is None:
            return value
        count, lfe = int(number), int(number) in (6, 8)
    if count <= 0:
        return ""
    if count == 1:
        return "Mono"
    if count == 2 and not lfe:
        return "Stereo"
    return "{}.1".format(count - 1) if lfe else "{}.0".format(count)


def resolution_text(value):
    value = _text(value)
    return value + "p" if value.isdigit() else value


def joined(*values):
    return " · ".join(value for value in values if value)


def speed_label(speed):
    if abs(speed - 1.0) < 0.01:
        return "Normal"
    return "{:g}×".format(speed)


def sleep_label(minutes):
    return present.duration_text(minutes) or "Off"


def stat_rows():
    """``(left, right)`` lists of (label, value) from Kodi's player info.

    Rows with no value are left out, so both lists can be empty.
    """
    def process(name):
        return info_label("Player.Process({})".format(name))

    def nonzero(value):
        number = _number(value)
        return "" if number is not None and number <= 0 else value

    decoder = process("videodecoder")
    if decoder and condition("Player.Process(videohwdecoder)"):
        decoder += " (hardware)"
    width, height = nonzero(process("videowidth")), nonzero(process("videoheight"))
    fps = _number(process("videofps"))
    rate = nonzero(process("audiosamplerate"))
    cache = info_label("Player.CacheLevel")
    hdr = info_label("VideoPlayer.HdrType")
    left = [
        ("Video decoder", decoder),
        ("Pixel format", process("pixformat")),
        ("Resolution", "{}x{}".format(width, height) if width and height else ""),
        ("Frame rate", "{:.3f}".format(fps).rstrip("0").rstrip(".") + " fps" if fps and fps > 0 else ""),
        ("Aspect", nonzero(process("videodar"))),
        ("Deinterlace", process("deintmethod")),
        ("HDR", HDR_NAMES.get(hdr.lower(), hdr)),
    ]
    right = [
        ("Audio decoder", process("audiodecoder")),
        ("Channels", channels_text(process("audiochannels"))),
        ("Sample rate", rate + " Hz" if rate else ""),
        ("Bits", nonzero(process("audiobitspersample"))),
        ("Cache", cache + " %" if cache else ""),
        ("CPU", info_label("System.CPUUsage")),
    ]
    return ([row for row in left if row[1]], [row for row in right if row[1]])


def chapter_list(chapters, total=None):
    """Chapters with a number, a start and a length, in playback order."""
    valid = []
    for chapter in chapters or []:
        if not isinstance(chapter, dict):
            continue
        start = _number(chapter.get("start"))
        if start is None or start < 0:
            continue
        valid.append((start, chapter))
    valid.sort(key=lambda pair: pair[0])
    result = []
    for index, (start, chapter) in enumerate(valid):
        end = _number(chapter.get("end"))
        if end is None or end <= start:
            end = valid[index + 1][0] if index + 1 < len(valid) else _number(total)
        result.append({"number": index + 1, "title": _text(chapter.get("title")) or "Chapter {}".format(index + 1),
                       "start": start, "length": end - start if end and end > start else None})
    return result


def current_chapter(chapters, position):
    """Index into ``chapter_list`` output for ``position`` seconds, or -1."""
    position = _number(position)
    if position is None:
        return -1
    current = -1
    for index, chapter in enumerate(chapters):
        if chapter["start"] <= position + 0.5:
            current = index
    return current


def visible_tabs(audio_streams, chapters, stats):
    left, right = stats
    hidden = set()
    if not audio_streams:
        hidden.add("audio")
    if not chapters:
        hidden.add("chapters")
    if not left and not right:
        hidden.add("stats")
    return [name for name in TAB_NAMES if name not in hidden]


def rail_layout(tabs):
    """Rail ``(left, width)`` and each visible tab's left edge, centred."""
    width = sum(TAB_WIDTH[name] for name in tabs) + TAB_GAP * max(0, len(tabs) - 1) + 2 * RAIL_PADDING
    left = (SCREEN_WIDTH - width) // 2
    positions, x = {}, left + RAIL_PADDING
    for name in tabs:
        positions[name] = x
        x += TAB_WIDTH[name] + TAB_GAP
    return left, width, positions


def panel_height(content):
    return max(PANEL_MIN, min(PANEL_MAX, PAD_TOP + content + PAD_BOTTOM))


def rows_area():
    return PANEL_MAX - PAD_TOP - PAD_BOTTOM


def list_item(row):
    item = xbmcgui.ListItem(label=row.get("label", ""), label2=row.get("value", ""))
    for key in ("checked", "number", "start", "length"):
        value = row.get(key)
        item.setProperty("silo." + key, "true" if value is True else ("" if value in (None, False) else str(value)))
    return item


def fill_facts(control, rows):
    """Show (label, value) rows; values change in place so the list never blinks."""
    try:
        same = control.size() == len(rows) and all(
            control.getListItem(index).getLabel() == label for index, (label, _) in enumerate(rows))
    except (RuntimeError, TypeError, AttributeError):
        same = False
    if same:
        for index, (_, value) in enumerate(rows):
            control.getListItem(index).setLabel2(value)
    else:
        control.reset()
        control.addItems([xbmcgui.ListItem(label=label, label2=value) for label, value in rows])
    control.setHeight(max(FACT_HEIGHT, len(rows) * FACT_HEIGHT))


# ------------------------------------------------------------------ window

class PlayerMenuWindow(xbmcgui.WindowXMLDialog):
    """The menu window. ``menu`` is the owning ``PlayerMenu``."""

    def __init__(self, *args, **kwargs):
        self.menu = None
        self.context = {}
        self.tab = "info"
        self.tabs = list(TAB_NAMES)
        self.rows = []
        self.closed = False
        self.ready = False
        self.busy = False  # True while a picker is open

    # --------------------------------------------------------------- state
    def setup(self, menu, context, tab=None):
        self.menu = menu
        self.context = dict(context or {})
        speed = _number(self.context.get("speed"))
        if speed and speed > 0:  # Optional: the controller may remember the tempo itself.
            menu.speed = speed
        if tab in TAB_NAMES:
            self.tab = tab
        if self.ready:
            self.build(focus=True)

    def onInit(self):
        self.ready = True
        self.build(focus=True)

    def converted_audio(self):
        """The file's audio tracks when the stream itself carries only one."""
        if not self.context.get("converted"):
            return []
        return [track for track in self.context.get("audio_tracks") or []
                if isinstance(track, dict) and isinstance(track.get("index"), int)]

    def audio_streams(self):
        converted = self.converted_audio()
        if converted:
            return [_text(track.get("label")) for track in converted]
        streams = player_call("getAvailableAudioStreams", default=None)
        return [_text(name) for name in streams] if isinstance(streams, (list, tuple)) else []

    def audio_languages(self):
        """{stream index: (language, title)} for the file's audio streams, as Kodi reports them."""
        ok, result = jsonrpc("Player.GetProperties", {"playerid": VIDEO_PLAYER_ID, "properties": ["audiostreams"]})
        streams = result.get("audiostreams") if ok and isinstance(result, dict) else None
        return {stream["index"]: (_text(stream.get("language")), _text(stream.get("name")))
                for stream in streams or [] if isinstance(stream, dict) and isinstance(stream.get("index"), int)}

    def subtitle_streams(self):
        streams = player_call("getAvailableSubtitleStreams", default=None)
        return [_text(name) for name in streams] if isinstance(streams, (list, tuple)) else []

    def chapters(self):
        return chapter_list(self.context.get("chapters"), player_call("getTotalTime", default=None))

    def stream_state(self):
        """Current audio index, subtitle index and whether subtitles show."""
        ok, result = jsonrpc("Player.GetProperties", {
            "playerid": VIDEO_PLAYER_ID,
            "properties": ["currentaudiostream", "currentsubtitle", "subtitleenabled"]})
        if not ok or not isinstance(result, dict):
            return {"audio": -1, "subtitle": -1, "enabled": False, "subtitle_name": ""}
        audio = result.get("currentaudiostream") if isinstance(result.get("currentaudiostream"), dict) else {}
        subtitle = result.get("currentsubtitle") if isinstance(result.get("currentsubtitle"), dict) else {}
        return {"audio": audio.get("index", -1) if isinstance(audio.get("index"), int) else -1,
                "subtitle": subtitle.get("index", -1) if isinstance(subtitle.get("index"), int) else -1,
                "enabled": bool(result.get("subtitleenabled")),
                "subtitle_name": _text(subtitle.get("name")) or _text(subtitle.get("language"))}

    # --------------------------------------------------------------- build
    def build(self, focus=False):
        """Recompute visible tabs and draw the selected tab."""
        if self.closed or not self.ready:
            return
        stats = stat_rows()
        self.tabs = visible_tabs(self.audio_streams(), self.context.get("chapters"), stats)
        if self.tab not in self.tabs:
            self.tab = "info"
        self.layout_rail()
        self.draw(stats)
        if focus:
            self.setFocusId(TAB_BUTTON[self.tab])

    def layout_rail(self):
        left, width, positions = rail_layout(self.tabs)
        values = {"silo.tab": self.tab}
        for name in TAB_NAMES:
            values["silo.tab." + name] = "true" if name in self.tabs else ""
        set_properties(self, values)
        try:
            rail = self.getControl(RAIL)
            rail.setPosition(left, RAIL_TOP)
            rail.setWidth(width)
            for name, x in positions.items():
                self.getControl(TAB_GROUP[name]).setPosition(x, TAB_TOP)
            # Up from the first row returns to the selected tab, not the nearest one.
            self.getControl(ROWS).controlUp(self.getControl(TAB_BUTTON[self.tab]))
        except (RuntimeError, TypeError):
            pass

    def draw(self, stats=None):
        self.setProperty("silo.tab", self.tab)
        self.setProperty("silo.rows", "true" if self.tab in ROW_TABS else "")
        try:
            if self.tab == "info":
                self.draw_info()
            elif self.tab == "stats":
                self.draw_stats(stats or stat_rows())
            else:
                self.draw_rows()
        except (RuntimeError, TypeError):  # Controls are gone once the window closes.
            pass

    def set_panel(self, content):
        self.getControl(PANEL).setHeight(panel_height(content))

    def draw_info(self):
        context = self.context
        runtime = context.get("runtime")
        if isinstance(runtime, (int, float)) and not isinstance(runtime, bool):
            runtime = present.duration_text(runtime)
        set_properties(self, {"silo.eyebrow": _text(context.get("eyebrow")),
                              "silo.title": _text(context.get("title")),
                              "silo.runtime": _text(runtime)})
        badges = [_text(badge) for badge in context.get("badges") or [] if _text(badge)][:MAX_BADGES]
        x = CONTENT_LEFT
        for slot in range(MAX_BADGES):
            text = badges[slot] if slot < len(badges) else ""
            self.setProperty("silo.badge.{}".format(slot), text)
            if text:
                width = present.badge_width(text)
                for control_id in (BADGE + slot, BADGE_LABEL + slot):
                    self.getControl(control_id).setPosition(x, BADGE_TOP)
                    self.getControl(control_id).setWidth(width)
                x += width + BADGE_GAP
        rows = self.info_rows()
        fill_facts(self.getControl(INFO_ROWS), rows)
        self.set_panel(max(INFO_HEADER, len(rows) * FACT_HEIGHT))

    def info_rows(self):
        context = self.context
        state = self.stream_state()
        if state["enabled"]:
            names = self.subtitle_streams()
            index = state["subtitle"]
            subtitle = state["subtitle_name"] or (names[index] if 0 <= index < len(names) else "") or "On"
        else:
            subtitle = "Off"
        chapters = self.chapters()
        current = current_chapter(chapters, player_call("getTime", default=None))
        rows = [
            ("Route", _text(context.get("route"))),
            ("Source", _text(context.get("source"))),
            ("Video", joined(info_label("VideoPlayer.VideoCodec").upper(),
                             resolution_text(info_label("VideoPlayer.VideoResolution")))),
            ("Audio", joined(info_label("VideoPlayer.AudioCodec").upper(),
                             channels_text(info_label("VideoPlayer.AudioChannels")))),
            ("Subtitles", subtitle),
            ("Chapter", chapters[current]["title"] if current >= 0 else ""),
        ]
        return [row for row in rows if row[1]]

    def draw_stats(self, stats):
        left, right = stats
        for control_id, rows in ((STATS_LEFT, left), (STATS_RIGHT, right)):
            fill_facts(self.getControl(control_id), rows)
        self.set_panel(max(len(left), len(right)) * FACT_HEIGHT)

    def draw_rows(self):
        self.rows = getattr(self, "rows_" + self.tab)()
        control = self.getControl(ROWS)
        control.reset()
        control.addItems([list_item(row) for row in self.rows])
        header = CHAPTER_HEADER if self.tab == "chapters" else 0
        visible = min(len(self.rows) * ROW_HEIGHT, (rows_area() - header) // ROW_HEIGHT * ROW_HEIGHT)
        control.setPosition(CONTENT_LEFT, CONTENT_TOP + header)
        control.setHeight(max(ROW_HEIGHT, visible))
        selected = next((index for index, row in enumerate(self.rows) if row.get("checked")), 0)
        control.selectItem(selected)
        self.set_panel(header + visible)

    # ---------------------------------------------------------------- rows
    def rows_video(self):
        context, rows = self.context, []
        versions = [version for version in context.get("versions") or [] if isinstance(version, dict)]
        if len(versions) > 1:
            current = next((version for version in versions if version.get("file_id") == context.get("file_id")), None)
            rows.append({"key": "version", "label": "Version",
                         "value": _text(current.get("label")) if current else ""})
        qualities = self.qualities()
        if qualities:
            current = next((label for value, label in qualities if value == self.context.get("quality")), "Auto")
            rows.append({"key": "quality", "label": "Quality", "value": current})
        if self.menu.speed_supported():
            rows.append({"key": "speed", "label": "Speed", "value": speed_label(self.menu.speed)})
        ok, result = jsonrpc("Player.GetViewMode")
        mode = result.get("viewmode") if ok and isinstance(result, dict) else None
        rows.append({"key": "aspect", "label": "Aspect", "value": dict(VIEW_MODES).get(mode, "")})
        minutes = self.sleep_minutes()
        rows.append({"key": "sleep", "label": "Sleep timer",
                     "value": "{} left".format(sleep_label(minutes)) if minutes else "Off"})
        rows.append({"key": "skip_prompts", "label": "Skip buttons",
                     "value": "On" if context.get("skip_prompts") else "Off"})
        if context.get("is_episode"):
            rows.append({"key": "auto_next", "label": "Auto-play next",
                         "value": "On" if context.get("auto_next") else "Off"})
        return rows

    def qualities(self):
        """("auto", "Auto") plus the server's menu; empty when it offers no choice."""
        entries = [(_text(entry.get("value")), _text(entry.get("label")) or _text(entry.get("value")))
                   for entry in self.context.get("qualities") or [] if isinstance(entry, dict) and entry.get("value")]
        return [("auto", "Auto")] + entries if entries else []

    def rows_audio(self):
        converted = self.converted_audio()
        if converted:
            # Another track needs a new stream from the server (track_change).
            rows = [{"key": "audio_track", "index": track["index"],
                     "label": _text(track.get("label")) or "Track {}".format(track["index"] + 1),
                     "language": _text(track.get("language")), "title": _text(track.get("title")),
                     "checked": track["index"] == self.context.get("audio_index")} for track in converted]
            rows.append({"key": "native", "window": "osdaudiosettings", "label": "Audio settings (delay, passthrough)…"})
            return rows
        current = self.stream_state()["audio"]
        languages = self.audio_languages()
        rows = [{"key": "audio", "index": index, "label": name or "Track {}".format(index + 1),
                 "language": languages.get(index, ("", ""))[0] or name,
                 "title": languages.get(index, ("", ""))[1] or name,
                 "checked": index == current} for index, name in enumerate(self.audio_streams())]
        rows.append({"key": "native", "window": "osdaudiosettings", "label": "Audio settings (delay, passthrough)…"})
        return rows

    def rows_subtitles(self):
        state = self.stream_state()
        rows = [{"key": "subtitle", "index": -1, "label": "Off", "checked": not state["enabled"]}]
        rows.extend({"key": "subtitle", "index": index, "label": name or "Track {}".format(index + 1),
                     "checked": state["enabled"] and index == state["subtitle"]}
                    for index, name in enumerate(self.subtitle_streams()))
        if self.context.get("subtitle_tools"):
            rows.append({"key": "tool", "screen": "subtitle_search", "label": "Search online…"})
            if self.context.get("ai_translate"):
                rows.append({"key": "tool", "screen": "subtitle_translate", "label": "Translate with AI…"})
            if self.context.get("subtitle_sync"):
                rows.append({"key": "tool", "screen": "subtitle_sync", "label": "Sync to audio…"})
        rows.append({"key": "native", "window": "osdsubtitlesettings", "label": "Subtitle style and timing…"})
        return rows

    def rows_chapters(self):
        chapters = self.chapters()
        current = current_chapter(chapters, player_call("getTime", default=None))
        return [{"key": "chapter", "label": chapter["title"], "seconds": chapter["start"],
                 "number": chapter["number"], "start": clock(chapter["start"]),
                 "length": clock(chapter["length"]) if chapter["length"] else "–",
                 "checked": index == current}
                for index, chapter in enumerate(chapters)]

    def sleep_minutes(self):
        try:
            return max(0, int(self.context.get("sleep_minutes") or 0))
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------ callbacks
    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS or action_id == ACTION_CONTEXT_MENU:
            self.dismiss()
        elif action_id == ACTION_PAUSE:
            player_call("pause")
        elif action_id == ACTION_PLAYER_PLAY:
            if condition("Player.Paused"):
                player_call("pause")
        elif action_id == ACTION_STOP:
            player_call("stop")
            self.dismiss()
        elif action_id in (ACTION_NEXT_ITEM, ACTION_PREV_ITEM):
            try:
                xbmc.executebuiltin("PlayerControl({})".format("Next" if action_id == ACTION_NEXT_ITEM else "Previous"))
            except RuntimeError:
                pass
        elif action_id in (ACTION_LEFT, ACTION_RIGHT):
            self.move_tab(-1 if action_id == ACTION_LEFT else 1)

    def focused(self):
        try:
            return self.getFocusId()
        except (RuntimeError, TypeError, AttributeError):
            return 0

    def move_tab(self, step):
        """Left/Right on the rail: the tab buttons point at themselves in XML."""
        focused = self.focused()
        current = next((name for name, control_id in TAB_BUTTON.items() if control_id == focused), None)
        if current is None or current not in self.tabs:
            return
        index = self.tabs.index(current) + step
        if 0 <= index < len(self.tabs):
            self.select_tab(self.tabs[index], focus=True)

    def select_tab(self, name, focus=False):
        if name not in self.tabs:
            return
        changed = name != self.tab
        self.tab = name
        if focus:
            self.setFocusId(TAB_BUTTON[name])
        if changed:
            self.layout_rail()
            self.draw()

    def onFocus(self, control_id):
        for name, button in TAB_BUTTON.items():
            if button == control_id:
                self.select_tab(name)

    def onClick(self, control_id):
        if self.busy or self.closed:
            return
        for name, button in TAB_BUTTON.items():
            if button == control_id:
                if name in ROW_TABS and self.rows:
                    self.setFocusId(ROWS)
                return
        if control_id != ROWS:
            return
        try:
            position = self.getControl(ROWS).getSelectedPosition()
        except (RuntimeError, TypeError):
            return
        if 0 <= position < len(self.rows):
            self.busy = True
            try:
                self.activate(self.rows[position])
            finally:
                self.busy = False

    def activate(self, row):
        key = row["key"]
        if key == "audio":
            player_call("setAudioStream", row["index"])
            # Remember the language for this title or show, like a subtitle pick.
            self.menu.queue({"action": "audio_choice", "language": row.get("language"), "title": row.get("title")})
            self.redraw()
        elif key == "subtitle":
            if row["index"] < 0:
                player_call("showSubtitles", False)
            else:
                player_call("setSubtitleStream", row["index"])
                player_call("showSubtitles", True)
            # Kodi names embedded streams by language; remember the pick for this title or show.
            self.menu.queue({"action": "subtitle_choice", "off": row["index"] < 0,
                             "language": row.get("language") or row.get("label")})
            self.redraw()
        elif key == "chapter":
            player_call("seekTime", float(row["seconds"]))
            self.dismiss()
        elif key == "audio_track":
            if row["index"] != self.context.get("audio_index"):
                self.menu.queue({"action": "audio_choice", "language": row.get("language"), "title": row.get("title")})
                self.menu.queue({"action": "audio_track", "index": row["index"]})
                self.dismiss()
        elif key == "native":
            self.menu.queue({"action": "native", "window": row["window"]})
            self.dismiss()
        elif key == "tool":
            # The controller pauses playback and opens the tool's dialog.
            self.menu.queue({"action": "subtitle_tool", "screen": row["screen"]})
            self.dismiss()
        elif key == "skip_prompts" or key == "auto_next":
            value = not self.context.get(key)
            self.context[key] = value
            self.menu.queue({"action": key, "value": value})
            self.redraw()
        else:
            getattr(self, "pick_" + key)()

    def redraw(self):
        """Redraw the row tab in place, keeping the cursor on the same row."""
        try:
            position = self.getControl(ROWS).getSelectedPosition()
        except (RuntimeError, TypeError):
            position = 0
        self.draw()
        if self.tab in ROW_TABS and 0 <= position < len(self.rows):
            try:
                self.getControl(ROWS).selectItem(position)
            except (RuntimeError, TypeError):
                pass

    def pick(self, heading, labels, preselect=0):
        """A short modal picker; -1 on Back or when the menu closed meanwhile."""
        try:
            choice = xbmcgui.Dialog().select(heading, list(labels), preselect=preselect)
        except (RuntimeError, TypeError):
            return -1
        return -1 if self.closed else choice

    def pick_version(self):
        versions = [version for version in self.context.get("versions") or [] if isinstance(version, dict)]
        labels = [_text(version.get("label")) or "Version {}".format(index + 1)
                  for index, version in enumerate(versions)]
        current = next((index for index, version in enumerate(versions)
                        if version.get("file_id") == self.context.get("file_id")), 0)
        choice = self.pick("Version", labels, current)
        if choice >= 0 and choice != current:
            self.menu.queue({"action": "version", "file_id": versions[choice].get("file_id")})
            self.dismiss()

    def pick_quality(self):
        qualities = self.qualities()
        values = [value for value, _ in qualities]
        current = values.index(self.context.get("quality")) if self.context.get("quality") in values else 0
        choice = self.pick("Quality", [label for _, label in qualities], current)
        if choice >= 0 and choice != current:
            self.menu.queue({"action": "quality", "value": values[choice]})
            self.dismiss()

    def pick_speed(self):
        current = min(range(len(SPEEDS)), key=lambda index: abs(SPEEDS[index] - self.menu.speed))
        choice = self.pick("Speed", [speed_label(speed) for speed in SPEEDS], current)
        if choice < 0:
            return
        ok, _ = jsonrpc("Player.SetTempo", {"playerid": VIDEO_PLAYER_ID, "tempo": SPEEDS[choice]})
        if ok:
            self.menu.speed = SPEEDS[choice]
        else:
            self.menu.speed_ok = False
        self.redraw()

    def pick_aspect(self):
        ok, result = jsonrpc("Player.GetViewMode")
        mode = result.get("viewmode") if ok and isinstance(result, dict) else None
        modes = [mode_id for mode_id, _ in VIEW_MODES]
        current = modes.index(mode) if mode in modes else 0
        choice = self.pick("Aspect", [label for _, label in VIEW_MODES], current)
        if choice < 0:
            return
        jsonrpc("Player.SetViewMode", {"viewmode": modes[choice]})
        self.redraw()

    def pick_sleep(self):
        active = self.sleep_minutes() > 0
        choices = [0] + list(SLEEP_CHOICES)
        labels = ["Cancel timer" if active else "Off"] + [sleep_label(minutes) for minutes in SLEEP_CHOICES]
        choice = self.pick("Sleep timer", labels, 0)
        if choice < 0 or (choice == 0 and not active):
            return
        self.context["sleep_minutes"] = choices[choice]
        self.menu.queue({"action": "sleep", "minutes": choices[choice]})
        self.redraw()

    def dismiss(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.close()
        except RuntimeError:
            pass


# -------------------------------------------------------------- controller

class PlayerMenu:
    """The controller's handle on the menu.

    ``open`` shows the window without blocking, ``refresh`` redraws the live
    values, ``is_open`` turns False after Back, ``take_result`` returns and
    clears the queued intents, and ``close`` is safe to call more than once.
    Playback speed is remembered across opens of the same handle.
    """

    def __init__(self, addon_path):
        self.addon_path = addon_path
        self.window = None
        self.speed = 1.0
        self.speed_ok = None  # Unknown until the first Video tab draw.
        self._results = []
        self._lock = threading.Lock()

    def open(self, context, tab=None):
        if self.window is not None and not self.window.closed:
            self.window.setup(self, context, tab)
            return
        self.close()
        self.window = PlayerMenuWindow(MENU_XML, str(self.addon_path), "Default", "1080i")
        self.window.setup(self, context, tab)
        self.window.show()

    def refresh(self):
        """Redraw live values (Info, Stats and tab visibility)."""
        window = self.window
        if window is None or window.closed or not window.ready or window.busy:
            return
        try:
            stats = stat_rows()
            tabs = visible_tabs(window.audio_streams(), window.context.get("chapters"), stats)
            if tabs != window.tabs:
                focus = window.focused() in TAB_BUTTON.values() or window.tab not in tabs
                window.build(focus=focus)
            elif window.tab in ("info", "stats"):
                window.draw(stats)
        except RuntimeError:
            pass

    def is_open(self):
        return self.window is not None and not self.window.closed

    def take_result(self):
        with self._lock:
            results, self._results = self._results, []
        return results

    def queue(self, intent):
        with self._lock:
            self._results.append(intent)

    def speed_supported(self):
        if self.speed_ok is None:
            ok, _ = jsonrpc("JSONRPC.Introspect", {"getdescriptions": False, "getreferences": False,
                                                   "filter": {"id": "Player.SetTempo", "type": "method"}})
            self.speed_ok = ok
        return self.speed_ok

    def close(self):
        window, self.window = self.window, None
        if window is None:
            return
        try:
            window.dismiss()
        finally:
            del window
