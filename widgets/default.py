"""Silo Theater Widgets: Kodi plugin entry that reuses Silo Theater's code.

The widgets live in their own add-on so Silo Theater itself stays a single
program in Kodi's add-on lists. The code, sign-in and settings all belong to
Silo Theater (script.silo), which this add-on requires.
"""

import sys

import xbmcaddon

SILO = xbmcaddon.Addon("script.silo")
sys.path.insert(0, SILO.getAddonInfo("path"))

from resources.lib.silo.widgets import run  # noqa: E402

if __name__ == "__main__":
    run(sys.argv, addon=SILO)
