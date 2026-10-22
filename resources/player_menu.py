"""Run by the Founders Theater keymap: ask the playing Silo session for its menu.

This is a separate script, not the add-on entry point, so pressing the key
never starts or stops the running Silo session. It only sets a flag the
Silo playback loop watches, and only while Silo is playing.
"""

import xbmcgui

home = xbmcgui.Window(10000)
if home.getProperty("silo.playing"):
    home.setProperty("silo.player_menu", "1")
