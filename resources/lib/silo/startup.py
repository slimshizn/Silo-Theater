"""Open Founders Theater when Kodi starts, when this device asked for it.

Kodi runs service.py once it has started, and again whenever this add-on is
installed, updated or re-enabled. A property on Kodi's Home window lasts until
Kodi closes, so only the first run in a Kodi session can open the add-on.

The service waits out the optional delay and Kodi's startup screen, then
opens the add-on once and exits. It never opens over something that is
already playing.
"""

import time

from .settings import read_options

HOME_WINDOW = 10000
STARTED_PROPERTY = "script.silo.startup_handled"
WAIT_SECONDS = 60  # how long Kodi gets to finish starting up
POLL_SECONDS = 0.5
OPEN = "RunScript(script.silo)"


def run(xbmc, xbmcgui, addon, clock=time.monotonic):
    """Open the add-on if this device asked for it; return what happened."""
    home = xbmcgui.Window(HOME_WINDOW)
    if home.getProperty(STARTED_PROPERTY):
        return "already_handled"
    home.setProperty(STARTED_PROPERTY, "1")
    options = read_options(addon)
    if not options["open_on_start"]:
        return "off"
    monitor = xbmc.Monitor()
    if options["open_delay"] and monitor.waitForAbort(options["open_delay"]):
        return "aborted"
    deadline = clock() + WAIT_SECONDS
    while True:
        if xbmc.getCondVisibility("Player.HasMedia"):
            return "playing"
        ready = not (xbmc.getCondVisibility("Window.IsActive(startup)")
                     or xbmc.getCondVisibility("System.HasActiveModalDialog"))
        if ready and (options["open_on_any_screen"] or xbmc.getCondVisibility("Window.IsActive(home)")):
            xbmc.executebuiltin(OPEN)
            return "opened"
        if clock() >= deadline:
            return "not_home" if ready else "not_ready"
        if monitor.waitForAbort(POLL_SECONDS):
            return "aborted"


def main():
    import xbmc
    import xbmcaddon
    import xbmcgui

    xbmc.log("[Silo] startup: " + run(xbmc, xbmcgui, xbmcaddon.Addon()), xbmc.LOGINFO)
