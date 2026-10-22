"""Kodi script entry point for Silo."""

import sys

from resources.lib.silo.application import Application


if __name__ == "__main__":
    Application().run(sys.argv)
