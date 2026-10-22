"""The Silo settings screen: profile playback preferences and this device's player options."""

import xbmcgui

from .settings import DEFAULTS
from .ui_common import ACTION_DOWN, ACTION_LEFT, ACTION_UP, BACK_ACTIONS, set_properties

# Control ids in SiloSettings.xml. Categories are buttons (not a list) so Kodi reports
# every move between them through onFocus. Row slot k: group SLOT_BASE + 10k, button +1,
# value +2, value shown while the row has focus +3.
RAIL_BUTTONS, RAIL_FILLS, HINT = (101, 102, 103), (111, 112, 113), 400
RAIL = RAIL_BUTTONS[0]
SLOTS, SLOT_BASE = 6, 1000
ROWS_LEFT, ROWS_TOP, ROW_PITCH, ROW_HEIGHT = 630, 312, 84, 76
HINT_INDENT, HINT_SPACE = 24, 44

TOGGLE, CHOICE, DEVICE, MORE = "toggle", "choice", "device", "more"

QUALITIES = (("auto", "Automatic"), ("original", "Original"), ("2160p", "4K (2160p)"),
             ("1080p", "1080p"), ("720p", "720p"))
SUBTITLE_MODES = (("auto", "Automatic"), ("always", "Always"), ("forced_only", "Forced only"), ("off", "Off"))
# ISO 639-1 codes; None is the profile default and is sent as null.
LANGUAGES = ((None, "Default"), ("en", "English"), ("es", "Spanish"), ("fr", "French"), ("de", "German"),
             ("it", "Italian"), ("pt", "Portuguese"), ("nl", "Dutch"), ("sv", "Swedish"), ("no", "Norwegian"),
             ("da", "Danish"), ("fi", "Finnish"), ("pl", "Polish"), ("ru", "Russian"), ("ja", "Japanese"),
             ("ko", "Korean"), ("zh", "Chinese"), ("ar", "Arabic"), ("hi", "Hindi"), ("tr", "Turkish"),
             ("el", "Greek"), ("he", "Hebrew"), ("cs", "Czech"), ("hu", "Hungarian"), ("th", "Thai"),
             ("vi", "Vietnamese"), ("id", "Indonesian"), ("uk", "Ukrainian"), ("ro", "Romanian"))


def row(kind, label, key=None, choices=None, default=None, hint=""):
    return {"kind": kind, "label": label, "key": key, "choices": choices, "default": default, "hint": hint}


PLAYBACK = ("Playback", (
    row(TOGGLE, "Skip intros automatically", "auto_skip_intro"),
    row(TOGGLE, "Skip credits automatically", "auto_skip_credits"),
    row(TOGGLE, "Skip recaps automatically", "auto_skip_recap"),
    row(CHOICE, "Quality preference", "quality_preference", QUALITIES, "auto",
        "Used by Silo apps that can convert video. Silo Theater plays the original file."),
))
AUDIO_SUBTITLES = ("Audio and subtitles", (
    row(CHOICE, "Audio language", "language", LANGUAGES),
    row(CHOICE, "Subtitle language", "subtitle_language", LANGUAGES),
    row(CHOICE, "Subtitle mode", "subtitle_mode", SUBTITLE_MODES, "auto"),
    row(TOGGLE, "Show forced subtitles", "show_forced_subtitles"),
))
THIS_DEVICE = ("This device", (
    row(DEVICE, "Play next episode automatically", "auto_next"),
    row(DEVICE, "Show skip buttons", "skip_prompts",
        hint="Offers Skip intro, Skip recap and Skip credits during playback."),
    row(DEVICE, "Silo player menu on the Menu key", "player_menu_key",
        hint="The Menu key (C on a keyboard) opens the Silo player menu during playback."),
    row(MORE, "More device settings…", hint="Display, HDR and Dolby Vision, resume and browsing."),
))
PROFILE_FIELDS = frozenset(item["key"] for _title, rows in (PLAYBACK, AUDIO_SUBTITLES) for item in rows)


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3


def categories(profile):
    """A temporary session has no profile preferences, so only this device's settings show."""
    profile = profile if isinstance(profile, dict) else {}
    if any(field in profile for field in PROFILE_FIELDS):
        return [PLAYBACK, AUDIO_SUBTITLES, THIS_DEVICE]
    return [THIS_DEVICE]


def normalized(value):
    if isinstance(value, str):
        return value.strip().lower() or None
    return value


def choice_index(item, value):
    values = [code for code, _label in item["choices"]]
    value = normalized(value)
    if value is None and item["default"] is not None:
        value = item["default"]
    return values.index(value) if value in values else -1


class SettingsWindow(xbmcgui.WindowXML):
    """Silo's settings window; ``result`` is "device" (open the add-on settings) or None."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.result = None
        self.initialized = False
        self.categories = []
        self.category = 0
        self.row = 0

    def onInit(self):
        if self.initialized:
            # Kodi re-runs onInit when a dialog over this window closes.
            return
        self.initialized = True
        application = self.application
        self.categories = categories(getattr(application, "profile", None))
        name = str(getattr(application, "profile_name", "") or "")
        set_properties(self, {"silo.subtitle": "Settings for " + name if name else "Settings for this device"})
        for index, (button, fill) in enumerate(zip(RAIL_BUTTONS, RAIL_FILLS)):
            shown = index < len(self.categories)
            if shown:
                self.getControl(button).setLabel(self.categories[index][0])
            self.getControl(button).setVisible(shown)
            self.getControl(fill).setVisible(shown)
        self.show_category(0)
        self.setFocusId(RAIL)

    def focus_id(self):
        try:
            return self.getFocusId()
        except RuntimeError:
            return 0

    def rows(self):
        return self.categories[self.category][1]

    # ------------------------------------------------------------ values

    def profile(self):
        profile = getattr(self.application, "profile", None)
        return profile if isinstance(profile, dict) else {}

    def device_on(self, key):
        value = str(self.application.addon.getSetting(key) or "").strip().lower()
        if value in ("true", "false"):
            return value == "true"
        return bool(DEFAULTS.get(key, False))

    def value_text(self, item):
        kind = item["kind"]
        if kind == TOGGLE:
            return "On" if self.profile().get(item["key"]) is True else "Off"
        if kind == DEVICE:
            return "On" if self.device_on(item["key"]) else "Off"
        if kind == CHOICE:
            value = self.profile().get(item["key"])
            index = choice_index(item, value)
            if index >= 0:
                return item["choices"][index][1]
            return str(value)
        return ""

    # ------------------------------------------------------------ layout

    def show_category(self, index):
        self.category = index
        self.row = 0
        self.setProperty("silo.category", str(index))
        self.render_rows()

    def render_rows(self):
        rows = self.rows()
        for index in range(SLOTS):
            group, button, value, focused_value = slot_ids(index)
            shown = index < len(rows)
            self.getControl(group).setVisible(shown)
            if not shown:
                continue
            text = self.value_text(rows[index])
            self.getControl(button).setLabel(rows[index]["label"])
            self.getControl(value).setLabel(text)
            self.getControl(focused_value).setLabel(text)
        self.layout_rows()

    def row_of(self, control_id):
        offset = control_id - SLOT_BASE
        if offset < 0 or offset % 10 != 1:
            return None
        index = offset // 10
        return index if index < len(self.rows()) else None

    def layout_rows(self, focused=None):
        """Stack the rows; the focused row's hint opens a gap directly under it."""
        rows = self.rows()
        if focused is None:
            focused = self.row_of(self.focus_id())
        hint = ""
        top = ROWS_TOP
        for index in range(len(rows)):
            self.getControl(slot_ids(index)[0]).setPosition(ROWS_LEFT, top)
            top += ROW_PITCH
            if index == focused and rows[index]["hint"]:
                hint = rows[index]["hint"]
                self.getControl(HINT).setPosition(ROWS_LEFT + HINT_INDENT, top - ROW_PITCH + ROW_HEIGHT + 4)
                top += HINT_SPACE
        self.getControl(HINT).setLabel(hint)
        self.getControl(HINT).setVisible(bool(hint))

    def focus_row(self, index):
        self.row = index
        self.setFocusId(slot_ids(index)[1])
        self.layout_rows(index)

    # ------------------------------------------------------------ input

    def onFocus(self, control_id):
        if control_id in RAIL_BUTTONS:
            category = RAIL_BUTTONS.index(control_id)
            if category < len(self.categories) and category != self.category:
                self.show_category(category)
        index = self.row_of(control_id)
        if index is not None:
            self.row = index
        # Leaving the rows hides the hint; -1 matches no row.
        self.layout_rows(-1 if index is None else index)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.result = None
            self.close()
            return
        focus = self.focus_id()
        if focus in RAIL_BUTTONS:
            # Kodi has already moved between the category buttons; onFocus normally
            # follows, and this catches a missed focus callback.
            self.onFocus(focus)
            return
        index = self.row_of(focus)
        if index is None:
            return
        if action_id == ACTION_LEFT:
            self.setFocusId(RAIL_BUTTONS[self.category])
        elif action_id == ACTION_UP and index > 0:
            self.focus_row(index - 1)
        elif action_id == ACTION_DOWN and index + 1 < len(self.rows()):
            self.focus_row(index + 1)

    def onClick(self, control_id):
        if control_id in RAIL_BUTTONS:
            self.onFocus(control_id)
            if self.rows():
                self.focus_row(0)
            return
        index = self.row_of(control_id)
        if index is not None:
            self.activate(self.rows()[index])

    def activate(self, item):
        kind = item["kind"]
        if kind == MORE:
            self.result = "device"
            self.close()
            return
        if kind == TOGGLE:
            self.save(item["key"], self.profile().get(item["key"]) is not True)
        elif kind == CHOICE:
            current = choice_index(item, self.profile().get(item["key"]))
            choice = self.application.dialog.select(item["label"], [label for _code, label in item["choices"]],
                                                    preselect=current)
            if choice < 0 or choice == current:
                return
            self.save(item["key"], item["choices"][choice][0])
        elif kind == DEVICE:
            self.toggle_device(item["key"])
        self.render_rows()

    def save(self, field, value):
        """Save one profile preference; on failure guard shows the error and the old value stays."""
        application = self.application
        application.guard(lambda: application.update_profile({field: value}))

    def toggle_device(self, key):
        application = self.application
        application.addon.setSetting(key, "false" if self.device_on(key) else "true")
        if key == "player_menu_key":
            sync_keymap = getattr(application, "sync_keymap", None)
            if callable(sync_keymap):
                application.guard(sync_keymap)


def open_settings(application):
    """Show the settings screen; returns "device" to open the add-on settings, else None."""
    window = SettingsWindow("SiloSettings.xml", application.addon.getAddonInfo("path"), "Default", "1080i")
    window.application = application
    window.doModal()
    result = window.result
    del window
    return result
