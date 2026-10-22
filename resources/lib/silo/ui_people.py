"""People screens: the profile picker, profile management, the device-code
sign-in screen and account sign-up.

They follow the Silo Android TV "marquee" screens. The sign-in form itself
lives in ``login.py``; this module only adds the windows around it.
"""

import xbmcgui

from . import present
from . import svc_profiles as svc
from .api import ApiError
from .ui_common import BACK_ACTIONS, set_properties

PROFILES_XML = "SiloProfiles.xml"
CODE_XML = "SiloCode.xml"
EDIT_XML = "SiloProfileEdit.xml"

# SiloProfiles.xml (the picker, and the same row in manage mode)
PROFILE_LIST = 300
MANAGE_BUTTON = 320  # "Manage profiles" on the picker, "Done" in manage mode
UNLOCK_BUTTON = 321  # manage mode without the primary profile: "Manage as <primary>"
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

def profile_list_item(profile, caption=None):
    """A ListItem for one profile: label, avatar art and fallback initials.

    Properties: silo.initials (shown under the avatar while it loads or when
    there is none), silo.pin ("true" when the profile asks for a PIN) and
    silo.caption (the line under the name: "PIN" on the picker, "Edit" when
    managing).
    """
    name = str(profile.get("name") or "")
    item = xbmcgui.ListItem(label=name)
    avatar = present.avatar_image(profile.get("avatar_url"))
    if avatar:
        item.setArt({"thumb": avatar})
    item.setProperty("silo.initials", present.initials(name))
    item.setProperty("silo.pin", "true" if profile.get("has_pin") else "")
    if caption is None:
        caption = "PIN" if profile.get("has_pin") else ""
    item.setProperty("silo.caption", caption)
    return item


def add_profile_item():
    item = xbmcgui.ListItem(label="Add profile")
    item.setProperty("silo.initials", "+")
    item.setProperty("silo.pin", "")
    item.setProperty("silo.caption", "")
    item.setProperty("silo.add", "true")
    return item


def fill_profile_row(row, items, selected=0):
    row.reset()
    row.addItems(items)
    left, width = profile_row_geometry(len(items))
    row.setWidth(width)
    row.setPosition(left, PROFILE_LIST_TOP)
    row.selectItem(selected if 0 <= selected < len(items) else 0)


def profile_row_geometry(count):
    """Left edge and width that centre ``count`` profile columns on screen."""
    columns = max(1, min(MAX_VISIBLE_PROFILES, count))
    width = columns * PROFILE_COLUMN
    return (SCREEN_WIDTH - width) // 2, width


class ProfilePicker(xbmcgui.WindowXMLDialog):
    """Modal "Who's watching?" row; ``result`` is the chosen index or -1."""

    def __init__(self, *args, **kwargs):
        self.profiles = []
        self.source = None
        self.preselect = 0
        self.account_label = ""
        self.application = None
        self.result = -1
        self.initialized = False

    def setup(self, profiles, preselect=0, account_label="", application=None):
        self.source = profiles if isinstance(profiles, list) else None
        self.profiles = list(profiles or [])
        self.preselect = preselect if 0 <= preselect < len(self.profiles) else 0
        self.account_label = str(account_label or "")
        self.application = application

    def onInit(self):
        if not self.initialized:
            self.initialized = True
            self.setProperty("silo.heading", "Who's watching?")
            self.setProperty("silo.account_label", self.account_label)
            self.setProperty("silo.manage_label", "Manage profiles")
            self.setProperty("silo.can_manage", "true" if self.application is not None else "")
            fill_profile_row(self.getControl(PROFILE_LIST),
                             [profile_list_item(profile) for profile in self.profiles], self.preselect)
        self.setFocusId(PROFILE_LIST)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.result = -1
            self.close()

    def onClick(self, control_id):
        if control_id == MANAGE_BUTTON and self.application is not None:
            self.manage()
            return
        if control_id != PROFILE_LIST:
            return
        position = self.getControl(PROFILE_LIST).getSelectedPosition()
        if 0 <= position < len(self.profiles):
            self.result = position
            self.close()

    def manage(self):
        """Open Manage profiles, then show the household as it is now.

        The caller's list is updated in place, so the returned index always
        points into the list it holds.
        """
        application = self.application
        position = self.getControl(PROFILE_LIST).getSelectedPosition()
        current = self.profiles[position].get("id") if 0 <= position < len(self.profiles) else None
        application.guard(lambda: application.open_screen("manage_profiles"))
        data = application.guard(lambda: application.client.profiles())
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list) and items:
            self.profiles = list(items)
            if self.source is not None:
                self.source[:] = items
            selected = next((index for index, profile in enumerate(self.profiles)
                             if current is not None and str(profile.get("id")) == str(current)), 0)
            fill_profile_row(self.getControl(PROFILE_LIST),
                             [profile_list_item(profile) for profile in self.profiles], selected)
        self.setFocusId(PROFILE_LIST)


def pick_profile(addon_path, profiles, preselect=0, account_label="", application=None):
    """Show the profile picker; return the chosen index, or -1 on Back.

    With ``application`` the picker offers "Manage profiles"; ``profiles`` (a
    list) is then refreshed in place after managing, and the index refers to it.
    """
    if not profiles:
        return -1
    window = ProfilePicker(PROFILES_XML, *_window_args(addon_path))
    try:
        window.setup(profiles, preselect, account_label, application)
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


# ---------------------------------------------------------- manage profiles

# SiloProfileEdit.xml
EDIT_LIST = 500
EDIT_SAVE, EDIT_CANCEL, EDIT_DELETE = 601, 602, 603


def _on(value):
    return "On" if value else "Off"


def _hidden_input(dialog, heading):
    return dialog.input(heading, type=xbmcgui.INPUT_ALPHANUM, option=xbmcgui.ALPHANUM_HIDE_INPUT) or ""


class ProfileForm:
    """The editable rows of one profile and the dialogs that change them.

    ``manager`` shows the household-only rows (kid profile, ratings, library
    access); without it only name, avatar and PIN appear, which the server
    lets a profile change on itself.
    """

    def __init__(self, profile, manager, flags, dialog, libraries=None):
        self.profile = profile
        self.manager = bool(manager)
        self.flags = flags if isinstance(flags, dict) else {}
        self.dialog = dialog
        self.load_libraries = libraries or (lambda: [])
        self.state = svc.form_state(profile)
        self.rating_touched = False
        self.avatar_batch = 0
        self._libraries = None

    def libraries(self):
        if self._libraries is None:
            self._libraries = list(self.load_libraries() or [])
        return self._libraries

    # ------------------------------------------------------------ rows

    def avatar_text(self):
        preset = svc.parse_preset(self.state["avatar"])
        if preset:
            return dict(svc.AVATAR_STYLES).get(preset[0], "Preset")
        if self.state["avatar"]:
            return "Custom"
        return "None"

    def pin_text(self):
        pin = self.state["pin"]
        if pin:
            return "New PIN"
        if pin == "" or not self.state["has_pin"]:
            return "Off"
        return "On"

    def rows(self):
        state = self.state
        rows = [("name", "Name", state["name"] or "Not set"),
                ("avatar", "Avatar", self.avatar_text()),
                ("pin", "PIN", self.pin_text())]
        if not self.manager:
            return rows
        rows += [("is_child", "Kid profile", _on(state["is_child"])),
                 ("max_content_rating", "Content rating limit", state["max_content_rating"] or "Any rating")]
        if self.flags.get("max_advisory_age_supported"):
            age = state["max_advisory_age"]
            rows.append(("max_advisory_age", "Advisory age limit", "Up to age {}".format(age) if age else "No limit"))
            if age and self.flags.get("require_advisory_age_supported"):
                rows.append(("require_advisory_age", "Hide titles without an advisory age",
                             _on(state["require_advisory_age"])))
        rows.append(("library_restrictions_enabled", "Limit libraries", _on(state["library_restrictions_enabled"])))
        if state["library_restrictions_enabled"]:
            count = len(state["allowed_library_ids"])
            rows.append(("allowed_library_ids", "Allowed libraries",
                         "{} selected".format(count) if count else "None selected"))
        return rows

    def validate(self, profiles=(), profile_id=None):
        assignable = None
        if self.manager and self.state["library_restrictions_enabled"] and self._libraries is not None:
            assignable = [str(library["id"]) for library in self._libraries]
        return svc.validate_form(self.state, profiles, profile_id, self.manager, assignable)

    # ------------------------------------------------------------ edits

    def activate(self, key):
        handler = getattr(self, "edit_" + key, None)
        if handler is not None:
            handler()

    def edit_name(self):
        name = self.dialog.input("Profile name", defaultt=self.state["name"], type=xbmcgui.INPUT_ALPHANUM)
        if name and name.strip():
            self.state["name"] = name.strip()[:svc.NAME_LIMIT]

    def edit_avatar(self):
        styles = list(svc.AVATAR_STYLES)
        labels = [label for _style, label in styles]
        if self.state["avatar"]:
            labels.append("Remove avatar")
        current = svc.parse_preset(self.state["avatar"])
        preselect = next((index for index, (style, _label) in enumerate(styles)
                          if current and current[0] == style), 0)
        choice = self.dialog.select("Avatar style", labels, preselect=preselect)
        if choice < 0:
            return
        if choice >= len(styles):
            self.state["avatar"], self.state["avatar_url"] = None, ""
            return
        style = styles[choice][0]
        while True:
            presets = svc.preset_batch(style, self.avatar_batch, self.state["name"])
            if len(presets) == 1:
                picked = presets[0]
                break
            items = [xbmcgui.ListItem(label="More choices")]
            for index, (_ref, image) in enumerate(presets):
                item = xbmcgui.ListItem(label="{} {}".format(styles[choice][1], index + 1))
                item.setArt({"thumb": image, "icon": image})
                items.append(item)
            picked = self.dialog.select("Choose an avatar", items, useDetails=True)
            if picked < 0:
                return
            if picked == 0:
                self.avatar_batch += 1
                continue
            picked = presets[picked - 1]
            break
        self.state["avatar"], self.state["avatar_url"] = picked

    def edit_pin(self):
        state = self.state
        if state["pin"] or (state["has_pin"] and state["pin"] is None):
            choice = self.dialog.select("PIN", ["Change PIN", "Remove PIN"])
            if choice < 0:
                return
            if choice == 1:
                state["pin"] = "" if state["has_pin"] else None
                return
        pin = _hidden_input(self.dialog, "New {}-digit PIN".format(svc.PIN_LENGTH))
        if not pin:
            return
        if len(pin) != svc.PIN_LENGTH or not pin.isdigit():
            self.dialog.ok("PIN", "The PIN must be {} digits.".format(svc.PIN_LENGTH))
            return
        if _hidden_input(self.dialog, "Enter the new PIN again") != pin:
            self.dialog.ok("PIN", "The PINs did not match. The PIN was not changed.")
            return
        state["pin"] = pin

    def edit_is_child(self):
        state = self.state
        state["is_child"] = not state["is_child"]
        # As on Android: a kid profile starts at PG unless a limit was chosen here.
        if state["is_child"] and not state["max_content_rating"] and not self.rating_touched:
            state["max_content_rating"] = svc.DEFAULT_CHILD_RATING

    def edit_max_content_rating(self):
        current = self.state["max_content_rating"]
        ratings = list(svc.CONTENT_RATINGS)
        if current and current not in ratings:
            ratings.insert(0, current)  # Keep a value another app stored.
        options = ["Any rating"] + ratings
        choice = self.dialog.select("Content rating limit", options,
                                    preselect=options.index(current) if current in options else 0)
        if choice >= 0:
            self.state["max_content_rating"] = None if choice == 0 else options[choice]
            self.rating_touched = True

    def edit_max_advisory_age(self):
        ages = list(svc.ADVISORY_AGES)
        current = self.state["max_advisory_age"]
        if current and current not in ages:
            ages = sorted(ages + [current])
        options = ["No limit"] + ["Up to age {}".format(age) for age in ages]
        choice = self.dialog.select("Advisory age limit", options,
                                    preselect=ages.index(current) + 1 if current in ages else 0)
        if choice >= 0:
            self.state["max_advisory_age"] = None if choice == 0 else ages[choice - 1]

    def edit_require_advisory_age(self):
        self.state["require_advisory_age"] = not self.state["require_advisory_age"]

    def edit_library_restrictions_enabled(self):
        state = self.state
        state["library_restrictions_enabled"] = not state["library_restrictions_enabled"]
        if state["library_restrictions_enabled"] and not state["allowed_library_ids"]:
            self.edit_allowed_library_ids()

    def edit_allowed_library_ids(self):
        libraries = self.libraries()
        if not libraries:
            self.dialog.ok("Allowed libraries", "This account has no libraries to choose from.")
            return
        ids = [str(library["id"]) for library in libraries]
        names = [str(library.get("name") or library["id"]) for library in libraries]
        chosen = self.dialog.multiselect("Allowed libraries", names,
                                         preselect=[index for index, value in enumerate(ids)
                                                    if value in self.state["allowed_library_ids"]])
        if chosen is not None:
            self.state["allowed_library_ids"] = sorted(ids[index] for index in chosen)


class ProfileManager:
    """Who may change what, and the server calls behind Manage profiles.

    The active profile manages the household when it is the primary profile.
    Anyone else can "Manage as" the primary profile after entering its PIN
    (as on Android); the requests then carry the primary profile's headers
    without changing the profile this device has selected.
    """

    def __init__(self, application):
        self.application = application
        self.client = application.client
        self.dialog = application.dialog
        self.profiles = []
        self.flags = {}
        self.writer = None  # The client that acts as the household manager.
        self._libraries = None

    @property
    def manager(self):
        return self.writer is not None

    def start(self):
        self.load()
        active, primary = self.active(), self.primary()
        if active is not None and primary is not None and str(active.get("id")) == str(primary.get("id")):
            self.writer = self.client
        return True

    def load(self):
        data = svc.list_profiles(self.client)
        self.flags = data
        self.profiles = [profile for profile in data.get("items") or [] if isinstance(profile, dict)]
        return self.profiles

    def active(self):
        profile = getattr(self.application, "profile", None) or {}
        return svc.find_profile(self.profiles, profile.get("id"))

    def primary(self):
        return svc.primary_profile(self.profiles)

    def primary_name(self):
        primary = self.primary()
        return str(primary.get("name") or "") if primary else ""

    def can_edit(self, profile):
        active = self.active()
        return self.manager or (active is not None and str(active.get("id")) == str(profile.get("id")))

    def note(self):
        if self.manager:
            return "Select a profile to change it, or add a new one."
        who = self.primary_name() or "The primary profile"
        return ("Only {} can add, change or delete other profiles. You can change your own name, avatar "
                "and PIN here; playback preferences are in Settings.".format(who))

    def unlock_label(self):
        primary = self.primary()
        if self.manager or primary is None:
            return ""
        return "Manage as {}".format(primary.get("name") or "the primary profile")

    def unlock(self):
        """Act as the primary profile, asking for its PIN when it has one."""
        primary = self.primary()
        if primary is None:
            self.dialog.ok("Manage profiles", "This household has no primary profile to manage it.")
            return False
        active = self.active()
        if active is not None and str(active.get("id")) == str(primary.get("id")):
            self.writer = self.client
            return True
        token = None
        if primary.get("has_pin"):
            pin = _hidden_input(self.dialog, "PIN for {}".format(primary.get("name") or "the primary profile"))
            if not pin:
                return False
            try:
                verification = self.client.verify_pin(primary["id"], pin)
            except ApiError as exc:
                self.dialog.ok("Manage profiles", exc.message)
                return False
            if not verification.get("valid") or not verification.get("profile_token"):
                self.dialog.ok("Manage profiles", "The PIN was not accepted.")
                return False
            token = verification["profile_token"]
        self.writer = svc.acting_client(self.client, primary["id"], token)
        return True

    def libraries(self):
        if self._libraries is None:
            self._libraries = svc.assignable_libraries(self.client)
        return self._libraries

    def form(self, profile):
        return ProfileForm(profile, self.manager, self.flags, self.dialog, self.libraries)

    def save(self, profile, form):
        """Create or update; returns "" on success or the message to show."""
        if profile is None and not self.manager:
            return "Only {} can add profiles.".format(self.primary_name() or "the primary profile")
        message = form.validate(self.profiles, profile.get("id") if profile else None)
        if message:
            return message
        writer = self.writer or self.client
        try:
            if profile is None:
                svc.create_profile(writer, svc.create_body(form.state, self.flags))
                return ""
            body = svc.update_body(profile, form.state, self.manager, self.flags)
            if not body:
                return ""
            updated = svc.update_profile(writer, profile["id"], body)
        except ApiError as exc:
            if exc.code == "profile_verification_required" and self.writer is not self.client:
                self.writer = None
            return svc.error_message(exc, self.primary_name())
        self.after_update(profile, body, updated)
        return ""

    def after_update(self, profile, body, updated):
        """Keep this device's selected profile working after its own edit."""
        application = self.application
        current = getattr(application, "profile", None) or {}
        if str(current.get("id")) != str(profile.get("id")):
            return
        if "pin" in body:
            # A new PIN ends the old profile token; prove the new one right away.
            token = None
            if body["pin"]:
                try:
                    token = (self.client.verify_pin(profile["id"], body["pin"]) or {}).get("profile_token")
                except ApiError:
                    token = None
            self.client.select_profile(profile["id"], token)
        if isinstance(updated, dict):
            application.profile = dict(current, **updated)
            application.profile_name = str(updated.get("name") or application.profile_name)
            application.profile_avatar = present.avatar_image(updated.get("avatar_url"))

    def delete(self, profile):
        if profile.get("is_primary"):
            return "The primary profile cannot be deleted."
        if not self.manager:
            return "Only {} can delete profiles.".format(self.primary_name() or "the primary profile")
        try:
            svc.delete_profile(self.writer, profile["id"])
        except ApiError as exc:
            if exc.status == 404:
                return ""  # Already gone.
            return svc.error_message(exc, self.primary_name())
        return ""


class ProfileEditWindow(xbmcgui.WindowXMLDialog):
    """Edit or add one profile; ``result`` is "saved", "deleted" or None."""

    def __init__(self, *args, **kwargs):
        self.manager = None
        self.profile = None
        self.form = None
        self.result = None
        self.initialized = False

    def onInit(self):
        if not self.initialized:
            self.initialized = True
            profile, manager = self.profile, self.manager
            deletable = bool(profile) and manager.manager and not profile.get("is_primary")
            set_properties(self, {
                "silo.heading": "Edit profile" if profile else "Add profile",
                "silo.note": "" if manager.manager else
                ("Kid profile, ratings and library access can only be changed by {}."
                 .format(manager.primary_name() or "the primary profile")),
                "silo.message": "",
                "silo.can_delete": "true" if deletable else "",
            })
            self.render(0)
        self.setFocusId(EDIT_LIST)

    def render(self, selected=None):
        state = self.form.state
        set_properties(self, {"silo.name": state["name"] or "New profile", "silo.avatar": state["avatar_url"] or "",
                              "silo.initials": present.initials(state["name"])})
        row = self.getControl(EDIT_LIST)
        if selected is None:
            selected = row.getSelectedPosition()
        items = []
        for key, label, value in self.form.rows():
            item = xbmcgui.ListItem(label=label, label2=value)
            item.setProperty("silo.key", key)
            items.append(item)
        row.reset()
        row.addItems(items)
        row.selectItem(min(max(0, selected), len(items) - 1))

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        guard = self.manager.application.guard
        if control_id == EDIT_LIST:
            rows = self.form.rows()
            position = self.getControl(EDIT_LIST).getSelectedPosition()
            if 0 <= position < len(rows):
                guard(lambda: self.form.activate(rows[position][0]))
                self.render(position)
        elif control_id == EDIT_SAVE:
            message = guard(lambda: self.manager.save(self.profile, self.form))
            if message == "":
                self.result = "saved"
                self.close()
            elif message:
                self.setProperty("silo.message", message)
        elif control_id == EDIT_DELETE and self.profile:
            name = self.profile.get("name") or "this profile"
            if not self.manager.dialog.yesno("Delete profile", "Delete {}? Its watch history, lists and settings "
                                                               "are removed from this server.".format(name)):
                return
            message = guard(lambda: self.manager.delete(self.profile))
            if message == "":
                self.result = "deleted"
                self.close()
            elif message:
                self.setProperty("silo.message", message)
        elif control_id == EDIT_CANCEL:
            self.close()


def edit_profile(manager, profile=None):
    """Open the editor for ``profile`` (None adds one); returns its result."""
    window = ProfileEditWindow(EDIT_XML, *_window_args(manager.application.addon.getAddonInfo("path")))
    try:
        window.manager, window.profile, window.form = manager, profile, manager.form(profile)
        window.doModal()
        return window.result
    finally:
        del window


class ManageWindow(xbmcgui.WindowXMLDialog):
    """The profile row in manage mode, with "Add profile" at the end."""

    def __init__(self, *args, **kwargs):
        self.manager = None
        self.initialized = False

    def onInit(self):
        if not self.initialized:
            self.initialized = True
            set_properties(self, {"silo.heading": "Manage profiles", "silo.manage": "true",
                                  "silo.manage_label": "Done", "silo.can_manage": "true",
                                  "silo.account_label": ""})
            self.refresh()
        self.setFocusId(PROFILE_LIST)

    def refresh(self, selected=0):
        manager = self.manager
        set_properties(self, {"silo.note": manager.note(), "silo.unlock_label": manager.unlock_label()})
        items = [profile_list_item(profile, "Edit" if manager.can_edit(profile) else
                                   ("PIN" if profile.get("has_pin") else ""))
                 for profile in manager.profiles]
        if manager.manager:
            items.append(add_profile_item())
        fill_profile_row(self.getControl(PROFILE_LIST), items, selected)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        manager = self.manager
        guard = manager.application.guard
        if control_id == MANAGE_BUTTON:
            self.close()
        elif control_id == UNLOCK_BUTTON:
            if guard(manager.unlock):
                self.refresh(self.getControl(PROFILE_LIST).getSelectedPosition())
                self.setFocusId(PROFILE_LIST)
        elif control_id == PROFILE_LIST:
            position = self.getControl(PROFILE_LIST).getSelectedPosition()
            if position == len(manager.profiles) and manager.manager:
                changed = edit_profile(manager, None)
            elif 0 <= position < len(manager.profiles):
                profile = manager.profiles[position]
                if not manager.can_edit(profile):
                    manager.dialog.ok("Manage profiles", "Only {} can change {}. Choose “{}” first.".format(
                        manager.primary_name() or "the primary profile", profile.get("name") or "this profile",
                        manager.unlock_label() or "Manage as the primary profile"))
                    return
                changed = edit_profile(manager, profile)
            else:
                return
            if changed:
                guard(manager.load)
                self.refresh(position if changed == "saved" else max(0, position - 1))


def manage_profiles(application):
    """Manage profiles (``open_screen("manage_profiles")``); returns None."""
    store = getattr(application, "store", None)
    if store is not None and store.get("temporary_session"):
        application.dialog.ok("Manage profiles", "Profiles cannot be managed with a temporary sign-in.")
        return None
    manager = ProfileManager(application)
    if not application.guard(manager.start):
        return None
    if not manager.manager and manager.active() is None:
        # Opened from the picker before a profile is chosen: act as the primary profile.
        if not application.guard(manager.unlock):
            return None
    window = ManageWindow(PROFILES_XML, *_window_args(application.addon.getAddonInfo("path")))
    try:
        window.manager = manager
        window.doModal()
    finally:
        del window
    return None


# ------------------------------------------------------------------ sign-up

# "Create an account" on SiloLogin.xml, shown while silo.allow_signup is set.
SIGNUP_CONTROL = 204


def signup_available(client):
    """Whether to show "Create account" on the sign-in form for this server."""
    return client is not None and svc.signup_enabled(client)


def create_account(client, dialog):
    """Collect sign-up details with Kodi dialogs and create the account.

    Returns ``{"action": "password", "server_url", "username", "password"}``
    so the sign-in form hands the new credentials to the normal password
    sign-in, or None when cancelled or refused (after saying why).
    """
    heading = "Create a Silo account"
    username = (dialog.input("Choose a username", type=xbmcgui.INPUT_ALPHANUM) or "").strip()
    if not username:
        return None
    email = (dialog.input("Email address", type=xbmcgui.INPUT_ALPHANUM) or "").strip()
    if not email:
        return None
    password = _hidden_input(dialog, "Choose a password (at least 8 characters)")
    if not password:
        return None
    if _hidden_input(dialog, "Enter the password again") != password:
        dialog.ok(heading, "The passwords did not match.")
        return None
    invite_code = (dialog.input("Invite code", type=xbmcgui.INPUT_ALPHANUM) or "").strip()
    if not invite_code:
        return None
    message = svc.validate_signup(username, email, password, invite_code)
    if message:
        dialog.ok(heading, message)
        return None
    try:
        svc.sign_up(client, username, email, password, invite_code)
    except ApiError as exc:
        dialog.ok(heading, svc.signup_error(exc))
        return None
    return {"action": "password", "server_url": client.server_url, "username": username, "password": password}
