"""Settings → Account → Test connection speed, as in the Silo apps.

Runs the shared speed test (bandwidth_probe.py) against this server, reports
the result as a manual test, and offers to clear a limit the server learned
after buffering on this connection.
"""

import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener

from . import bandwidth_probe as probe
from .api import ApiError, _NoRedirect


def _opener(client):
    """open_url for BandwidthProbe: Silo's sign-in only when asked, no redirects."""
    opener = build_opener(_NoRedirect())

    def origin(value):
        parts = urlsplit(value)
        return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)

    def open_url(url, authenticated):
        # Never send the sign-in to another host, whatever the server advertises.
        authenticated = authenticated and origin(url) == origin(client.server_url)
        headers = client.headers() if authenticated else {"User-Agent": getattr(client, "user_agent", "Silo")}
        headers.update({"Accept": "application/octet-stream", "Cache-Control": "no-store"})
        try:
            response = opener.open(Request(url, headers=headers, method="GET"), timeout=30)
            return SimpleNamespace(status=response.status, headers=response.headers,
                                   read=response.read, close=response.close)
        except HTTPError as error:
            return SimpleNamespace(status=error.code, headers=error.headers, read=error.read, close=error.close)
    return open_url


def _mbps(kbps):
    return "{:.0f} Mbps".format(kbps / 1000.0) if kbps >= 10000 else "{:.1f} Mbps".format(kbps / 1000.0)


def test_connection_speed(application):
    """Measure the connection to Silo and tell the viewer; never raises for network trouble."""
    import xbmcgui

    client, dialog = application.client, application.dialog
    try:
        capabilities = client.request("GET", "/playback/capabilities")
    except (ApiError, OSError):
        dialog.ok("Test connection speed", "Silo could not be reached.")
        return
    target = probe.ProbeTarget.from_capabilities(capabilities or {}, client.server_url) \
        if probe.CONNECTION_SPEED_FEATURE in (capabilities or {}).get("features", []) else None
    if target is None:
        dialog.ok("Test connection speed", "This Silo server does not offer a speed test.")
        return
    progress = xbmcgui.DialogProgress()
    progress.create("Test connection speed", "Measuring the connection to your Silo server…")
    started = time.monotonic()

    def cancelled():
        progress.update(min(99, int((time.monotonic() - started) * 100 / (probe.ACCURATE.max_ms / 1000.0))))
        return progress.iscanceled()

    try:
        outcome = probe.BandwidthProbe().measure(
            probe.ACCURATE, target.target_kbps, _opener(client),
            probe.probe_download_url(target.url, probe.ACCURATE), cancelled=cancelled)
    except (URLError, OSError):
        outcome = None
    finally:
        progress.close()
    if outcome is None or outcome.kind != probe.MEASURED:
        if outcome is not None and outcome.kind == probe.RATE_LIMITED:
            dialog.ok("Test connection speed", "Silo is busy. Try again in {} seconds.".format(
                int(outcome.retry_after_seconds or probe.DEFAULT_RETRY_AFTER_SECONDS)))
        elif not progress.iscanceled():
            dialog.ok("Test connection speed", "The speed test did not finish. Try again.")
        return
    result = outcome.result
    try:
        client.request("POST", probe.RESULTS_PATH, probe.report_body(result, "other", manual=True))
    except (ApiError, OSError):
        pass  # The measurement still stands for this answer.
    message = "Your connection to Silo: {}{}.".format("at least " if result.lower_bound else "", _mbps(result.kbps))
    try:
        connection = client.request("GET", probe.CONNECTION_PATH)
    except (ApiError, OSError):
        connection = None
    limit = (connection or {}).get("learned_limit") if isinstance(connection, dict) else None
    if isinstance(limit, dict) and isinstance(limit.get("kbps"), (int, float)):
        if dialog.yesno("Test connection speed", message + "\n\nSilo limited quality to {} after buffering on "
                        "this connection. Clear that limit?".format(_mbps(limit["kbps"])),
                        nolabel="Keep", yeslabel="Clear"):
            try:
                client.request("DELETE", probe.LEARNED_LIMIT_PATH)
            except (ApiError, OSError):
                dialog.ok("Test connection speed", "Silo could not clear the limit. Try again later.")
        return
    dialog.ok("Test connection speed", message)
