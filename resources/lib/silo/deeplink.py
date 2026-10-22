"""Deep links into the add-on (``RunScript(script.silo,play,<id>)``); no Kodi imports."""

from urllib.parse import quote, unquote

ADDON_ID = "script.silo"
DEEP_LINK_ACTIONS = ("play", "open")


def valid_id(value):
    value = str(value or "")
    return value if 0 < len(value) <= 512 and value.isprintable() else ""


def run_script_builtin(action, content_id):
    """``RunScript(script.silo,<action>,<id>)``.

    Ordinary ids such as ``movie:example-2020`` pass unchanged; anything else
    (a comma, quote, parenthesis, space or ``%``) is percent-encoded so it
    cannot break Kodi's builtin parameter parsing. ``deep_link`` decodes it.
    """
    return "RunScript({},{},{})".format(ADDON_ID, action, quote(content_id, safe=":-_."))


def deep_link(args):
    """Parse RunScript arguments (``sys.argv[1:]``) for default.py.

    Returns ``("play", content_id)``, ``("open", content_id)`` or None.
    """
    if not isinstance(args, (list, tuple)) or len(args) < 2:
        return None
    action, content_id = str(args[0]).strip().lower(), valid_id(unquote(str(args[1])))
    if action not in DEEP_LINK_ACTIONS or not content_id:
        return None
    return action, content_id
