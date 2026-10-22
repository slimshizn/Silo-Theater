"""People screens: the profile picker and the device-code sign-in screen.

Both follow the Silo Android TV "marquee" screens. The sign-in form itself
lives in ``login.py``; this module only adds the windows around it.
"""

import xbmcgui

from . import present
from .ui_common import BACK_ACTIONS, set_properties

PROFILES_XML = "SiloProfiles.xml"
CODE_XML = "SiloCode.xml"

# SiloProfiles.xml
PROFILE_LIST = 300
PROFILE_COLUMN = 284  # 256 px profile column plus a 28 px gap
PROFILE_LIST_TOP = 400
MAX_VISIBLE_PROFILES = 6
SCREEN_WIDTH = 1920

# SiloCode.xml
CODE_PROGRESS = 310
CODE_CANCEL = 400
CODE_MAX_CHARACTERS = 9
CODE_TILE_WIDTH, CODE_TILE_GAP, CODE_SEPARATOR_WIDTH, CODE_ROW_WIDTH = 56, 4, 12, 536
CODE_SEPARATORS = frozenset("- ")
WAITING_TEXT = "Waiting for approval…"


def _window_args(addon_path):
    return (str(addon_path), "Default", "1080i")


# ----------------------------------------------------------------- profiles

def profile_list_item(profile):
    """A ListItem for one profile: label, avatar art and fallback initials.

    Properties: silo.initials (shown under the avatar while it loads or when
    there is none) and silo.pin ("true" when the profile asks for a PIN).
    """
    name = str(profile.get("name") or "")
    item = xbmcgui.ListItem(label=name)
    avatar = present.avatar_image(profile.get("avatar_url"))
    if avatar:
        item.setArt({"thumb": avatar})
    item.setProperty("silo.initials", present.initials(name))
    item.setProperty("silo.pin", "true" if profile.get("has_pin") else "")
    return item


def profile_row_geometry(count):
    """Left edge and width that centre ``count`` profile columns on screen."""
    columns = max(1, min(MAX_VISIBLE_PROFILES, count))
    width = columns * PROFILE_COLUMN
    return (SCREEN_WIDTH - width) // 2, width


class ProfilePicker(xbmcgui.WindowXMLDialog):
    """Modal "Who's watching?" row; ``result`` is the chosen index or -1."""

    def __init__(self, *args, **kwargs):
        self.profiles = []
        self.preselect = 0
        self.account_label = ""
        self.result = -1
        self.initialized = False

    def setup(self, profiles, preselect=0, account_label=""):
        self.profiles = list(profiles or [])
        self.preselect = preselect if 0 <= preselect < len(self.profiles) else 0
        self.account_label = str(account_label or "")

    def onInit(self):
        if not self.initialized:
            self.initialized = True
            self.setProperty("silo.account_label", self.account_label)
            row = self.getControl(PROFILE_LIST)
            row.reset()
            row.addItems([profile_list_item(profile) for profile in self.profiles])
            left, width = profile_row_geometry(len(self.profiles))
            row.setWidth(width)
            row.setPosition(left, PROFILE_LIST_TOP)
            row.selectItem(self.preselect)
        self.setFocusId(PROFILE_LIST)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.result = -1
            self.close()

    def onClick(self, control_id):
        if control_id != PROFILE_LIST:
            return
        position = self.getControl(PROFILE_LIST).getSelectedPosition()
        if 0 <= position < len(self.profiles):
            self.result = position
            self.close()


def pick_profile(addon_path, profiles, preselect=0, account_label=""):
    """Show the profile picker; return the chosen index, or -1 on Back."""
    if not profiles:
        return -1
    window = ProfilePicker(PROFILES_XML, *_window_args(addon_path))
    try:
        window.setup(profiles, preselect, account_label)
        window.doModal()
        return window.result
    finally:
        del window


# --------------------------------------------------------------- code sign-in

def code_slots(code):
    """Split a sign-in code into tiles.

    Returns ``[(character, gap_after), ...]`` where dashes and spaces become
    gaps, or [] when the code does not fit the tile row (the screen then shows
    it as plain text).
    """
    slots = []
    for character in str(code or "").strip():
        if character in CODE_SEPARATORS:
            if slots:
                slots[-1] = (slots[-1][0], True)
        else:
            slots.append((character, False))
    if slots:
        slots[-1] = (slots[-1][0], False)
    if not slots or len(slots) > CODE_MAX_CHARACTERS:
        return []
    gaps = sum(1 for _, gap in slots if gap)
    width = (len(slots) * CODE_TILE_WIDTH + gaps * CODE_SEPARATOR_WIDTH
             + (len(slots) + gaps - 1) * CODE_TILE_GAP)
    return slots if width <= CODE_ROW_WIDTH else []


def code_properties(verification_url, user_code):
    """Window properties for the URL and the code tiles."""
    slots = code_slots(user_code)
    values = {"silo.code_url": str(verification_url or ""),
              "silo.code": str(user_code or "").strip(),
              "silo.code_tiles": "true" if slots else "",
              "silo.code_status": WAITING_TEXT}
    for index in range(CODE_MAX_CHARACTERS):
        character, gap = slots[index] if index < len(slots) else ("", False)
        values["silo.code.{}".format(index)] = character
        values["silo.code.{}.gap".format(index)] = "true" if gap else ""
    return values


def expiry_text(seconds_left):
    seconds = int(max(0, seconds_left))
    if seconds <= 0:
        return "Code expired"
    return "Expires in {}:{:02d}".format(*divmod(seconds, 60))


class CodeWindow(xbmcgui.WindowXMLDialog):
    """Modeless window; callbacks only record that the user wants out."""

    def __init__(self, *args, **kwargs):
        self.cancelled = False
        self.percent = 100.0

    def onInit(self):
        self.show_progress()
        self.setFocusId(CODE_CANCEL)

    def show_progress(self):
        try:
            self.getControl(CODE_PROGRESS).setPercent(self.percent)
        except (RuntimeError, TypeError):  # Controls do not exist before the window initialises.
            pass

    def cancel(self):
        self.cancelled = True
        self.close()

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.cancel()

    def onClick(self, control_id):
        if control_id == CODE_CANCEL:
            self.cancel()


class CodeScreen:
    """Device-code sign-in screen shown while the caller polls for approval.

    ``open`` shows the window without blocking, ``update`` moves the time-left
    bar, ``cancelled`` reports Back or Cancel, and ``close`` is safe to call
    more than once.
    """

    def __init__(self, addon_path):
        self.addon_path = addon_path
        self.window = None
        self.expires_in = 0
        self._cancelled = False

    def open(self, verification_url, user_code, expires_in):
        try:
            self.expires_in = max(0, float(expires_in or 0))
        except (TypeError, ValueError):
            self.expires_in = 0
        new = self.window is None
        if new:
            self.window = CodeWindow(CODE_XML, *_window_args(self.addon_path))
        set_properties(self.window, code_properties(verification_url, user_code))
        self.update(0)
        if new:
            self.window.show()
            self.window.show_progress()

    def update(self, fraction_elapsed):
        """Show how much time is left; returns the clamped fraction (0..1)."""
        try:
            fraction = min(1.0, max(0.0, float(fraction_elapsed)))
        except (TypeError, ValueError):
            fraction = 0.0
        if fraction != fraction:  # NaN
            fraction = 0.0
        if self.window is not None:
            self.window.percent = 100.0 * (1.0 - fraction)
            self.window.setProperty("silo.code_expiry",
                                    expiry_text(self.expires_in * (1.0 - fraction)) if self.expires_in else "")
            self.window.show_progress()
        return fraction

    def cancelled(self):
        return self._cancelled or bool(self.window is not None and self.window.cancelled)

    def close(self):
        window, self.window = self.window, None
        if window is None:
            return
        self._cancelled = self._cancelled or window.cancelled
        try:
            window.close()
        finally:
            del window
