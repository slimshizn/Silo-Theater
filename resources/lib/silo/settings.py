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
    # Device-local player behaviour; profile-wide choices live on the server.
    "auto_next": True,
    "skip_prompts": True,
    "player_menu_key": True,
    "hide_spoilers": False,
    "stop_on_back": True,
    "online_tv_themes": False,
    "profile_select": "auto",
    "resume_rewind": 7,
    "still_watching": 3,
    "next_up_lead": 30,
    "max_bitrate": 0,
    # Opening Founders Theater when Kodi starts (startup.py).
    "open_on_start": False,
    "open_on_any_screen": True,
    "open_delay": 0,
}

BOOLEANS = ("show_plot", "auto_next", "skip_prompts", "player_menu_key", "hide_spoilers", "stop_on_back",
            "online_tv_themes", "open_on_start", "open_on_any_screen")

CHOICES = {
    "sign_in_method": ("code", "password"),
    "resume_behavior": ("resume", "ask", "start"),
    "file_selection": ("ask", "last_played", "first"),
    "dv_mode": ("auto", "hdr10", "dv", "p7_fel"),
    "max_resolution": ("auto", "480p", "720p", "1080p", "2160p"),
    "subtitle_mode": ("server", "off"),
    # When to ask "Who's watching?": automatic (saved profile), every launch, or after 1 h / 12 h away.
    "profile_select": ("auto", "always", "1h", "12h"),
}

INTEGER_CHOICES = {
    "progress_interval": (5, 10),
    "page_size": (25, 50, 100),
    "resume_rewind": (0, 3, 5, 7, 10, 15, 30),  # seconds to back up when resuming
    "still_watching": (0, 2, 3, 4, 5),  # auto-played episodes before asking; 0 never asks
    "next_up_lead": (15, 30, 45, 60),  # seconds before the end when a file has no credits marker
    "max_bitrate": (0, 4000, 8000, 12000, 20000, 40000),  # kbps for converted streams; 0 = no limit
    "open_delay": (0, 5, 10, 20, 30),  # seconds to wait after Kodi starts before opening
}


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
    for name in BOOLEANS:
        value = _read(addon, name).lower()
        if value in ("true", "false"):
            options[name] = value == "true"
    return options
