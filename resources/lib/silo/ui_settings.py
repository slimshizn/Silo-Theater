"""The Silo settings screen: profile preferences, this device's player options and the account."""

import xbmcgui

from . import svc_profiles as svc
from .api import ApiError
from . import kodi_settings
from .settings import DEFAULTS, INTEGER_CHOICES, read_options
from .ui_common import ACTION_DOWN, ACTION_LEFT, ACTION_UP, BACK_ACTIONS, set_properties

# Control ids in SiloSettings.xml. Categories are buttons (not a list) so Kodi reports
# every move between them through onFocus. Row slot k: group SLOT_BASE + 10k, button +1,
# value +2, value shown while the row has focus +3.
RAIL_BUTTONS, RAIL_FILLS, HINT = (101, 102, 103, 104), (111, 112, 113, 114), 400
RAIL = RAIL_BUTTONS[0]
SLOTS, SLOT_BASE = 8, 1000
ROWS_LEFT, ROWS_TOP, ROW_PITCH, ROW_HEIGHT = 630, 312, 84, 76
HINT_INDENT, HINT_SPACE = 24, 44

# Row kinds: profile preferences (TOGGLE, CHOICE) are saved on the server; device
# settings (DEVICE, DEVICE_CHOICE) with addon.setSetting; ACTION rows run an account action.
TOGGLE, CHOICE, DEVICE, DEVICE_CHOICE, MORE, ACTION = "toggle", "choice", "device", "device_choice", "more", "action"
# KODI rows show and change one of Kodi's own settings (frame-rate matching) through JSON-RPC.
KODI = "kodi"

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


# Device choices: (value, label); values are settings.INTEGER_CHOICES / CHOICES members.
REWIND_CHOICES = tuple((value, "Off" if value == 0 else "{} seconds".format(value))
                       for value in INTEGER_CHOICES["resume_rewind"])
STILL_WATCHING_CHOICES = tuple((value, "Never" if value == 0 else "After {} episodes".format(value))
                               for value in INTEGER_CHOICES["still_watching"])
NEXT_UP_CHOICES = tuple((value, "{} seconds before the end".format(value)) for value in INTEGER_CHOICES["next_up_lead"])
BITRATE_CHOICES = tuple((value, "No limit" if value == 0 else "{:g} Mbps".format(value / 1000.0))
                        for value in INTEGER_CHOICES["max_bitrate"])
PROFILE_SELECT_CHOICES = (("auto", "Only when no profile is saved"), ("always", "Every time Silo opens"),
                          ("1h", "After 1 hour away"), ("12h", "After 12 hours away"))


def row(kind, label, key=None, choices=None, default=None, hint=""):
    return {"kind": kind, "label": label, "key": key, "choices": choices, "default": default, "hint": hint}


PLAYBACK = ("Playback", (
    row(TOGGLE, "Skip intros automatically", "auto_skip_intro"),
    row(TOGGLE, "Skip credits automatically", "auto_skip_credits"),
    row(TOGGLE, "Skip recaps automatically", "auto_skip_recap"),
    row(CHOICE, "Quality preference", "quality_preference", QUALITIES, "auto",
        "The quality Silo converts to when this device cannot play the original file."),
    row(DEVICE_CHOICE, "Rewind when resuming", "resume_rewind", REWIND_CHOICES,
        hint="Goes back a few seconds when you continue watching."),
    row(DEVICE_CHOICE, "Ask if you're still watching", "still_watching", STILL_WATCHING_CHOICES,
        hint="Pauses autoplay after this many episodes in a row without a key press."),
    row(DEVICE_CHOICE, "Show Up next", "next_up_lead", NEXT_UP_CHOICES,
        hint="Used when an episode has no credits marker."),
    row(DEVICE_CHOICE, "Maximum bitrate for converted video", "max_bitrate", BITRATE_CHOICES,
        hint="Limits video that Silo converts. Original files play as they are."),
))
AUDIO_SUBTITLES = ("Audio and subtitles", (
    row(CHOICE, "Audio language", "language", LANGUAGES),
    row(CHOICE, "Subtitle language", "subtitle_language", LANGUAGES),
    row(CHOICE, "Subtitle mode", "subtitle_mode", SUBTITLE_MODES, "auto"),
    row(TOGGLE, "Show forced subtitles", "show_forced_subtitles"),
    row(CHOICE, "Metadata language", "preferred_metadata_language", LANGUAGES,
        hint="Language for titles and descriptions. Default uses each library's language."),
))
THIS_DEVICE = ("This device", (
    row(DEVICE, "Play next episode automatically", "auto_next"),
    row(DEVICE, "Show skip buttons", "skip_prompts",
        hint="Offers Skip intro, Skip recap and Skip credits during playback."),
    row(DEVICE, "Back stops playback", "stop_on_back",
        hint="Back during a Silo video stops it. Turn off to keep it playing behind Kodi's menus."),
    row(KODI, "Match frame rate", kodi_settings.FRAME_RATE, kodi_settings.FRAME_RATE_CHOICES,
        hint="Switches the TV to each video's frame rate (24p films play without judder). "
             "Kodi pauses 2 seconds while the TV switches."),
    row(DEVICE, "Silo player menu on the Menu key", "player_menu_key",
        hint="The Menu key (C on a keyboard) opens the Silo player menu during playback."),
    row(DEVICE_CHOICE, "Ask who's watching", "profile_select", PROFILE_SELECT_CHOICES,
        hint="When Silo shows the profile picker on this device."),
    row(DEVICE, "Hide spoilers", "hide_spoilers",
        hint="Hides descriptions and images of episodes you have not watched."),
    row(MORE, "More device settings…", hint="Display, HDR and Dolby Vision, resume and browsing."),
))
ACCOUNT = ("Account", (
    row(ACTION, "Manage profiles", "manage_profiles", hint="Add, change or delete the profiles on this account."),
    row(ACTION, "Send diagnostics report", "diagnostics",
        hint="Sends this device's versions and Founders Theater settings to the server's administrator."),
    row(ACTION, "Switch server", "server", hint="Signs out and connects to another Silo server."),
    row(ACTION, "Sign out", "logout"),
))
PROFILE_KINDS = (TOGGLE, CHOICE)
PROFILE_FIELDS = frozenset(item["key"] for _title, rows in (PLAYBACK, AUDIO_SUBTITLES) for item in rows
                           if item["kind"] in PROFILE_KINDS)
# Account actions that need a full profile (not a temporary session).
PROFILE_ACTIONS = frozenset({"manage_profiles"})


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3


def categories(profile):
    """Categories with the rows this sign-in can use.

    A temporary session has no profile preferences and cannot manage profiles,
    so only device settings and the other account actions show.
    """
    profile = profile if isinstance(profile, dict) else {}
    full = any(field in profile for field in PROFILE_FIELDS)
    result = []
    for title, rows in (PLAYBACK, AUDIO_SUBTITLES, THIS_DEVICE, ACCOUNT):
        if not full:
            rows = tuple(item for item in rows if item["kind"] not in PROFILE_KINDS
                         and item["key"] not in PROFILE_ACTIONS)
        if rows:
            result.append((title, rows))
    return result


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
    """Silo's settings window; ``result`` is "device", "server", "logout" or None (see open_settings)."""

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
        self.show_subtitle()
        for index, (button, fill) in enumerate(zip(RAIL_BUTTONS, RAIL_FILLS)):
            shown = index < len(self.categories)
            if shown:
                self.getControl(button).setLabel(self.categories[index][0])
            self.getControl(button).setVisible(shown)
            self.getControl(fill).setVisible(shown)
        self.show_category(0)
        self.setFocusId(RAIL)

    def show_subtitle(self):
        name = str(getattr(self.application, "profile_name", "") or "")
        set_properties(self, {"silo.subtitle": "Settings for " + name if name else "Settings for this device"})

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

    def device_value(self, key):
        return read_options(self.application.addon).get(key, DEFAULTS.get(key))

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
        if kind == DEVICE_CHOICE:
            value = self.device_value(item["key"])
            return next((label for code, label in item["choices"] if code == value), str(value))
        if kind == KODI:
            return kodi_settings.label(item["choices"], kodi_settings.get(item["key"]))
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
        if kind == ACTION:
            self.run_action(item["key"])
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
        elif kind == DEVICE_CHOICE:
            self.choose_device(item)
        elif kind == KODI:
            self.choose_kodi(item)
        self.render_rows()

    def choose_kodi(self, item):
        values = [code for code, _label in item["choices"]]
        current = kodi_settings.get(item["key"])
        preselect = values.index(current) if current in values else -1
        choice = self.application.dialog.select(item["label"], [label for _code, label in item["choices"]],
                                                preselect=preselect)
        if not 0 <= choice < len(values) or choice == preselect:
            return
        if not kodi_settings.set_value(item["key"], values[choice]):
            self.application.dialog.ok("Silo", "Kodi did not accept this change on this device.")
            return
        if item["key"] == kodi_settings.FRAME_RATE and values[choice] and not kodi_settings.get(
                kodi_settings.FRAME_RATE_PAUSE):
            # Most TVs blank for a moment while switching; give them time.
            kodi_settings.set_value(kodi_settings.FRAME_RATE_PAUSE, 20)

    def choose_device(self, item):
        values = [code for code, _label in item["choices"]]
        current = self.device_value(item["key"])
        preselect = values.index(current) if current in values else -1
        choice = self.application.dialog.select(item["label"], [label for _code, label in item["choices"]],
                                                preselect=preselect)
        if 0 <= choice < len(values) and choice != preselect:
            self.application.addon.setSetting(item["key"], str(values[choice]))

    def run_action(self, key):
        application = self.application
        dialog = application.dialog
        if key == "manage_profiles":
            application.guard(lambda: application.open_screen("manage_profiles"))
            # The active profile's name or preferences may have changed.
            self.show_subtitle()
            self.render_rows()
        elif key == "diagnostics":
            application.guard(lambda: send_diagnostics(application))
        elif key == "server":
            if dialog.yesno("Switch server", "Sign out of this Silo server and connect to another one?"):
                self.result = "server"
                self.close()
        elif key == "logout":
            if dialog.yesno("Sign out", "Sign out of Silo on this device?"):
                self.result = "logout"
                self.close()

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


def diagnostics_info(application):
    """Versions, platform and this device's settings; never tokens, addresses or paths."""
    info = {"app_version": str(application.addon.getAddonInfo("version") or ""), "kodi_version": "",
            "os": "", "android": False, "model": "Kodi"}
    try:
        import xbmc

        info["kodi_version"] = str(xbmc.getInfoLabel("System.BuildVersion") or "").split(" ")[0]
        info["os"] = str(xbmc.getInfoLabel("System.OSVersionInfo") or "")
        info["android"] = bool(xbmc.getCondVisibility("System.Platform.Android"))
    except (ImportError, AttributeError, RuntimeError):
        pass
    state = svc.safe_state(read_options(application.addon))
    profile = getattr(application, "profile", None) or {}
    temporary = bool(getattr(application, "store", None) and application.store.get("temporary_session"))
    state["profile_type"] = "kid" if profile.get("is_child") else "standard"
    state["temporary_session"] = temporary
    info["state"] = state
    # Attribution is optional and the server refuses it for kid profiles.
    if profile.get("id") is not None and not profile.get("is_child") and not temporary:
        info["profile_id"] = str(profile["id"])
    return info


DIAGNOSTICS_NOTICE = ("Send a report to the administrator of this Silo server? It contains the Founders Theater "
                      "and Kodi versions, the operating system and this device's Founders Theater settings. It never "
                      "contains passwords, sign-in tokens, server addresses or file paths.")


def send_diagnostics(application):
    """Confirm, then upload a manual diagnostics report and show its id."""
    dialog = application.dialog
    heading = "Diagnostics report"
    try:
        capabilities = svc.diagnostics_capabilities(application.client)
    except ApiError as exc:
        if exc.status in (404, 405, 501):
            dialog.ok(heading, "This Silo server does not accept diagnostics reports.")
            return None
        raise
    if capabilities.get("state") != "available":
        dialog.ok(heading, "This Silo server is not accepting diagnostics reports right now.")
        return None
    info = diagnostics_info(application)
    if not info["android"]:
        dialog.ok(heading, "Silo servers accept diagnostics reports from Android and Apple devices. Kodi on this "
                           "system cannot send one yet.")
        return None
    if not dialog.yesno(heading, DIAGNOSTICS_NOTICE):
        return None
    manifest, bundle = svc.build_report(capabilities, info)
    receipt = svc.send_report(application.client, manifest, bundle, info.get("profile_id"))
    report = receipt.get("short_id") or receipt.get("report_id") or ""
    dialog.ok(heading, "Report sent. Report ID: {}. Give this ID to your server's administrator.".format(report)
              if report else "Report sent.")
    return report


def open_settings(application):
    """Show the settings screen and return what the app loop should do next.

    "device": open the add-on's own settings dialog; "server": sign out and
    choose another server; "logout": sign out; None: back to Home.
    """
    window = SettingsWindow("SiloSettings.xml", application.addon.getAddonInfo("path"), "Default", "1080i")
    window.application = application
    window.doModal()
    result = window.result
    del window
    return result
