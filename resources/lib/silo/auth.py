"""Cancelable, UI-independent Silo device sign-in orchestration."""

import datetime
import time
from email.utils import parsedate_to_datetime

from .api import ApiError


class DeviceAuthCancelled(ApiError):
    def __init__(self):
        super().__init__(0, "Code sign-in was canceled.", "device_login_canceled")


class DeviceAuthExpired(ApiError):
    def __init__(self):
        super().__init__(410, "The sign-in code expired. Start code sign-in again.", "device_login_expired")


def _seconds(value, default):
    try:
        value = float(value)
        return value if 0 < value < float("inf") else default
    except (TypeError, ValueError):
        return default


def _instant(value):
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, AttributeError, OverflowError):
        return None


class AuthController:
    def __init__(self, client, clock=None, wall_clock=None):
        self.client = client
        self.clock = clock or time.monotonic
        self.wall_clock = wall_clock or time.time

    def available_methods(self):
        """Advertised support; old servers may offer password login only."""
        methods = []
        try:
            capability = self.client.device_capability()
            if capability.get("state") == "available" and capability.get("allowed", True):
                methods.append("device")
        except ApiError as exc:
            if exc.status not in (404, 405, 501):
                raise
        providers = self.client.auth_providers()
        if providers.get("password_login"):
            methods.append("password")
        return methods

    def _deadline(self, expires_at, fallback):
        instant = _instant(expires_at)
        return self.clock() + max(0, instant - self.wall_clock()) if instant is not None else fallback

    def _cancel(self, code):
        try:
            self.client.device_cancel(code)
        except ApiError:
            # Cancellation is best effort when the device is offline.
            pass

    def _retry_after(self, headers, default):
        value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
        try:
            instant = parsedate_to_datetime(value).timestamp()
        except (ValueError, TypeError, AttributeError):
            return _seconds(value, default)
        return max(1, instant - self.wall_clock())

    def sign_in_device(self, show_code, wait, cancelled, device_name="Kodi", device_platform="kodi"):
        """Show only the public code fields and poll with cancellation support.

        ``show_code(public_fields)`` displays the code and verification URI;
        ``wait(seconds)`` must return true when aborted and remain responsive;
        ``cancelled()`` reports a back action. A new code is an explicit UI action.
        The secret device_code is never passed to display callbacks or persisted.
        """
        start = self.client.device_start(device_name, device_platform)
        if not isinstance(start, dict) or not all(start.get(k) for k in ("device_code", "user_code", "verification_uri")):
            raise ApiError(200, "Silo returned an incomplete sign-in code.", "invalid_response")
        code = start["device_code"]
        interval = _seconds(start.get("interval"), 5)
        deadline = self._deadline(start.get("expires_at"), self.clock() + _seconds(start.get("expires_in"), 900))
        try:
            show_code({key: start[key] for key in ("user_code", "verification_uri", "verification_uri_complete", "expires_in", "expires_at", "interval") if key in start})
            while True:
                if cancelled():
                    raise DeviceAuthCancelled()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise DeviceAuthExpired()
                if wait(min(interval, remaining)) or cancelled():
                    raise DeviceAuthCancelled()
                if self.clock() >= deadline:
                    raise DeviceAuthExpired()
                try:
                    result = self.client.device_poll(code)
                except ApiError as exc:
                    if exc.status == 429:
                        interval = max(interval + 5, self._retry_after(exc.headers, interval + 5))
                        continue
                    if exc.status == 410:
                        raise DeviceAuthExpired() from exc
                    raise
                if not isinstance(result, dict):
                    raise ApiError(200, "Silo returned an invalid pairing response.", "invalid_response")
                status = result.get("status")
                if status == "approved":
                    return result
                if status == "pending":
                    interval = _seconds(result.get("poll_after"), interval)
                    deadline = self._deadline(result.get("expires_at"), deadline)
                    continue
                if status == "expired":
                    raise DeviceAuthExpired()
                if status in ("canceled", "cancelled"):
                    raise DeviceAuthCancelled()
                if status in ("denied", "consumed"):
                    raise ApiError(403, "Code sign-in was {}. Start sign-in again.".format(status), "device_login_" + status)
                raise ApiError(200, "Silo returned an unknown pairing status.", "invalid_response")
        except BaseException:
            self._cancel(code)
            raise
