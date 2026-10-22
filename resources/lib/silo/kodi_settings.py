"""Kodi's own player settings that Founders Theater offers in its settings.

Frame-rate matching is a Kodi system setting, not an add-on one, so these
read and write it through JSON-RPC (Settings.GetSettingValue/SetSettingValue)
instead of keeping a copy that could disagree with Kodi.
"""

import json

# Kodi's AdjustRefreshRate values.
FRAME_RATE = "videoplayer.adjustrefreshrate"
FRAME_RATE_CHOICES = ((0, "Off"), (2, "On start and stop"), (3, "On start"), (1, "Always"))
# Tenths of a second Kodi pauses after switching, so the TV can settle.
FRAME_RATE_PAUSE = "videoplayer.pauseafterrefreshchange"
FRAME_RATE_PAUSE_CHOICES = ((0, "Off"), (10, "1 second"), (20, "2 seconds"), (30, "3 seconds"), (50, "5 seconds"))


def _rpc(method, params, execute=None):
    try:
        if execute is None:
            import xbmc

            execute = xbmc.executeJSONRPC
        answer = json.loads(execute(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})))
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        return None  # No JSON-RPC here: treat the setting as unavailable.
    return answer if isinstance(answer, dict) and "error" not in answer else None


def get(setting, execute=None):
    """The setting's current value, or None when Kodi does not have it."""
    answer = _rpc("Settings.GetSettingValue", {"setting": setting}, execute)
    value = ((answer or {}).get("result") or {}).get("value")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def set_value(setting, value, execute=None):
    """Change a Kodi setting; True when Kodi accepted it."""
    answer = _rpc("Settings.SetSettingValue", {"setting": setting, "value": value}, execute)
    return bool(answer and answer.get("result") is True)


def label(choices, value):
    return next((text for number, text in choices if number == value), "Off" if value is None else str(value))
