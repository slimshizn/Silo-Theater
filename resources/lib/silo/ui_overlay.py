"""Small windows drawn over playing video: the skip button and Up next."""

import xbmc
import xbmcgui

from .ui_common import BACK_ACTIONS

# Transport keys keep reaching the player while an overlay has focus.
ACTION_PAUSE, ACTION_STOP, ACTION_PLAYER_PLAY = 12, 13, 79


def forward_transport(action_id):
    """Send play/pause/stop to the player; return True when handled."""
    try:
        if action_id in (ACTION_PAUSE, ACTION_PLAYER_PLAY):
            xbmc.Player().pause()
        elif action_id == ACTION_STOP:
            xbmc.Player().stop()
        else:
            return False
    except RuntimeError:
        pass
    return True


class _OverlayWindow(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.owner = None

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.owner._choose("dismiss")
        else:
            forward_transport(action_id)

    def onClick(self, control_id):
        self.owner._choose({100: "skip", 200: "play", 201: "dismiss"}.get(control_id, "dismiss"))


class _Overlay:
    """A modeless dialog; the playback loop polls ``take_choice``."""

    XML = ""

    def __init__(self, addon_path):
        self.addon_path = addon_path
        self.window = None
        self.choice = None

    def _choose(self, choice):
        self.choice = choice
        self.hide()

    def _show(self, properties, focus):
        if self.window is None:
            self.window = _OverlayWindow(self.XML, self.addon_path, "Default", "1080i")
            self.window.owner = self
        for key, value in properties.items():
            self.window.setProperty(key, "" if value is None else str(value))
        self.choice = None
        self.window.show()
        try:
            self.window.setFocusId(focus)
        except RuntimeError:
            pass

    def take_choice(self):
        choice, self.choice = self.choice, None
        return choice

    def visible(self):
        return self.window is not None

    def hide(self):
        window, self.window = self.window, None
        if window is not None:
            try:
                window.close()
            except RuntimeError:
                pass


class SkipButton(_Overlay):
    """The white Skip Intro / Skip Credits capsule, bottom right."""

    XML = "SiloSkip.xml"

    def show(self, label):
        self._show({"silo.skip_label": label}, 100)


class UpNextCard(_Overlay):
    """Up next with the following episode and an optional countdown."""

    XML = "SiloUpNext.xml"

    def show(self, episode, seconds=None):
        self._show({"silo.next_eyebrow": episode.get("eyebrow", ""),
                    "silo.next_title": episode.get("title", ""),
                    "silo.next_thumb": episode.get("thumb", ""),
                    "silo.next_countdown": self.countdown_text(seconds)}, 200)

    def update(self, seconds):
        if self.window is not None:
            self.window.setProperty("silo.next_countdown", self.countdown_text(seconds))

    @staticmethod
    def countdown_text(seconds):
        if seconds is None:
            return ""
        return "Playing in {}".format(max(0, int(seconds)))
