"""The optional Kodi keymap that opens the Silo player menu during playback.

Kodi does not pass key presses to an add-on while video plays, so the menu is
reached through a keymap: in fullscreen video only, the remote's Menu or
Title key (C on a keyboard) runs ``resources/player_menu.py`` by path. That
small script only flags the request; the menu opens only when Silo itself is
playing, and the running Silo session is never restarted.
"""

import os
from xml.sax.saxutils import escape

FILE_NAME = "silo_theater.xml"
SCRIPT = "resources/player_menu.py"
MARKER = "<!-- Written by Founders Theater; turn off in its settings to remove. -->"
# Files written before the add-on was renamed are still this add-on's to update or remove.
LEGACY_MARKERS = ("<!-- Written by Silo Theater; turn off in its settings to remove. -->",)


def keymap_xml(addon_path):
    command = escape("RunScript({})".format(str(addon_path).rstrip("/\\") + "/" + SCRIPT))
    return "\n".join((
        '<?xml version="1.0" encoding="UTF-8"?>',
        MARKER,
        "<keymap>",
        "  <FullscreenVideo>",
        "    <keyboard>",
        "      <c>{}</c>".format(command),
        "      <menu>{}</menu>".format(command),
        "    </keyboard>",
        "    <remote>",
        "      <menu>{}</menu>".format(command),
        "      <title>{}</title>".format(command),
        "    </remote>",
        "  </FullscreenVideo>",
        "</keymap>",
        "",
    ))


def sync(folder, enabled, addon_path):
    """Write or remove the keymap; return True when Kodi should reload keymaps.

    A file with the same name that this add-on did not write is left alone.
    """
    path = os.path.join(folder, FILE_NAME)
    existing = None
    try:
        with open(path, encoding="utf-8") as handle:
            existing = handle.read()
    except OSError:
        pass
    if existing is not None and not any(marker in existing for marker in (MARKER,) + LEGACY_MARKERS):
        return False
    if enabled:
        content = keymap_xml(addon_path)
        if existing == content:
            return False
        os.makedirs(folder, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return True
    if existing is None:
        return False
    os.unlink(path)
    return True
