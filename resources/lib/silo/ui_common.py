"""Kodi helpers shared by the Silo windows: actions, debouncing, card items."""

import threading

import xbmcgui

from . import present

BACK_ACTIONS = frozenset({9, 10, 92, 216, 247, 257, 61448})
ACTION_LEFT, ACTION_RIGHT, ACTION_UP, ACTION_DOWN = 1, 2, 3, 4
ACTION_SELECT = 7
# Android waits about 150 ms after focus settles before swapping the hero.
SETTLE_SECONDS = 0.15


class Debouncer:
    """Run ``callback`` once, ``delay`` seconds after the last ``trigger``."""

    def __init__(self, callback, delay=SETTLE_SECONDS):
        self.callback = callback
        self.delay = delay
        self._timer = None
        self._lock = threading.Lock()

    def trigger(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay, self._fire)
            self._timer.daemon = True
            self._timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
        try:
            self.callback()
        except Exception:  # A late timer must never crash Kodi's script thread.
            pass

    def cancel(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


def set_properties(window, values):
    for key, value in values.items():
        window.setProperty(key, "" if value is None else str(value))


def card_list_item(item, image, wide=False):
    """A ListItem for a row or grid card.

    Properties: silo.badge (``S2 · E8`` on wide cards), silo.progress
    (whole percent, "" when none), silo.watched ("true"/""), silo.plot.
    ``ListItem.PercentPlayed`` also reports the progress for progress bars.
    """
    data = present.card(item, image, wide)
    result = xbmcgui.ListItem(label=data["label"], label2=data["label2"])
    result.setArt({key: value for key, value in data["art"].items() if value})
    result.setProperty("silo.badge", data["badge"])
    result.setProperty("silo.progress", str(data["progress"]) if data["progress"] else "")
    result.setProperty("silo.watched", "true" if data["watched"] else "")
    result.setProperty("silo.plot", str(item.get("overview") or ""))
    if data["progress"]:
        try:
            result.getVideoInfoTag().setResumePoint(float(data["progress"]), 100.0)
        except (AttributeError, TypeError):
            pass
    return result
