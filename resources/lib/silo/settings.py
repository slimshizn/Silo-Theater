"""Validated preferences shared by the Kodi screens and player.

Reading never imports Kodi, so callers can supply an add-on object or an
offline test double. Read again after the settings dialog closes to pick up
changes without restarting the add-on.
"""


DEFAULTS = {
    "server_url": "",
    "sign_in_method": "password",
    "resume_behavior": "resume",
    "file_selection": "ask",
    "dv_mode": "auto",
    "max_resolution": "auto",
    "subtitle_mode": "server",
    "progress_interval": 10,
    "page_size": 50,
    "show_plot": True,
}

CHOICES = {
    "sign_in_method": ("code", "password"),
    "resume_behavior": ("resume", "ask", "start"),
    "file_selection": ("ask", "last_played", "first"),
    "dv_mode": ("auto", "hdr10", "dv", "p7_fel"),
    "max_resolution": ("auto", "480p", "720p", "1080p", "2160p"),
    "subtitle_mode": ("server", "off"),
}

INTEGER_CHOICES = {"progress_interval": (5, 10), "page_size": (25, 50, 100)}


def _read(addon, name):
    try:
        value = addon.getSetting(name)
    except (AttributeError, KeyError, RuntimeError, TypeError, ValueError):
        return ""
    return str(value).strip() if value is not None else ""


def read_options(addon):
    """Return safe, typed preferences; unknown or missing values use defaults."""
    options = dict(DEFAULTS)
    options["server_url"] = _read(addon, "server_url")
    for name, choices in CHOICES.items():
        value = _read(addon, name).lower()
        if value in choices:
            options[name] = value
    for name, choices in INTEGER_CHOICES.items():
        try:
            value = int(_read(addon, name))
        except ValueError:
            continue
        if value in choices:
            options[name] = value
    value = _read(addon, "show_plot").lower()
    if value in ("true", "false"):
        options["show_plot"] = value == "true"
    return options
