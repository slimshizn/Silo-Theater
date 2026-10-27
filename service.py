"""Kodi service entry point: opens Founders Theater when Kodi starts, if this device asked for it."""

from resources.lib.silo.startup import main


if __name__ == "__main__":
    main()
