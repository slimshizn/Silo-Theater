"""Kodi plugin entry point: Silo rows as widgets on Kodi's own home screen."""

import sys

from resources.lib.silo.widgets import run


if __name__ == "__main__":
    run(sys.argv)
