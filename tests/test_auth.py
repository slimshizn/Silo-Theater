"""Device pairing state-machine checks with controllable monotonic time."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources" / "lib"))

from silo.api import ApiError
from silo.auth import AuthController, DeviceAuthCancelled, DeviceAuthExpired


class Clock:
    def __init__(self):
        self.now = 0
        self.waits = []

    def __call__(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds
        return False


class Client:
    def __init__(self, *polls):
        self.polls = list(polls)
        self.canceled = []
        self.polled = []
        self.start = {"device_code": "secret-code", "user_code": "1234-5678",
                      "verification_uri": "https://silo.example/activate", "expires_in": 30, "interval": 5}
        self.capability = {"state": "available"}
        self.providers = {"password_login": True}

    def device_start(self, *args):
        return self.start

    def device_poll(self, code):
        self.polled.append(code)
        if not self.polls:
            raise AssertionError("Unexpected poll")
        value = self.polls.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def device_cancel(self, code):
        self.canceled.append(code)

    def device_capability(self):
        if isinstance(self.capability, Exception):
            raise self.capability
        return self.capability

    def auth_providers(self):
        return self.providers


class DeviceAuthTests(unittest.TestCase):
    def setup_controller(self, *polls):
        client, clock = Client(*polls), Clock()
        return client, clock, AuthController(client, clock=clock, wall_clock=lambda: 0)

    def test_pending_then_approval_keeps_secret_out_of_display(self):
        client, clock, controller = self.setup_controller({"status": "pending", "poll_after": 7}, {"status": "approved"})
        shown = []
        result = controller.sign_in_device(shown.append, clock.wait, lambda: False)
        self.assertEqual(result["status"], "approved")
        self.assertNotIn("device_code", shown[0])
        self.assertEqual(shown[0]["user_code"], "1234-5678")
        self.assertEqual(clock.waits, [5, 7])
        self.assertEqual(client.polled, ["secret-code", "secret-code"])
        self.assertEqual(client.canceled, [])

    def test_rate_limit_respects_retry_after_and_then_approves(self):
        client, clock, controller = self.setup_controller(ApiError(429, "slow", headers={"Retry-After": "12"}), {"status": "approved"})
        controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(clock.waits, [5, 12])

    def test_rate_limit_without_header_backs_off(self):
        client, clock, controller = self.setup_controller(ApiError(429, "slow"), {"status": "approved"})
        controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(clock.waits, [5, 10])

    def test_local_cancel_withdraws_request_without_polling(self):
        client, clock, controller = self.setup_controller()
        with self.assertRaises(DeviceAuthCancelled):
            controller.sign_in_device(lambda data: None, clock.wait, lambda: True)
        self.assertEqual(client.canceled, ["secret-code"])
        self.assertEqual(client.polled, [])

    def test_wait_abort_withdraws_request(self):
        client, clock, controller = self.setup_controller()
        with self.assertRaises(DeviceAuthCancelled):
            controller.sign_in_device(lambda data: None, lambda seconds: True, lambda: False)
        self.assertEqual(client.canceled, ["secret-code"])

    def test_display_failure_also_withdraws_pairing_request(self):
        client, clock, controller = self.setup_controller()

        def failed_display(data):
            raise RuntimeError("Window was closed")

        with self.assertRaises(RuntimeError):
            controller.sign_in_device(failed_display, clock.wait, lambda: False)
        self.assertEqual(client.canceled, ["secret-code"])

    def test_expiry_never_polls_after_local_deadline_or_restarts_code(self):
        client, clock, controller = self.setup_controller({"status": "pending"})
        client.start["expires_in"] = 8
        with self.assertRaises(DeviceAuthExpired):
            controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(clock.waits, [5, 3])
        self.assertEqual(len(client.polled), 1)
        self.assertEqual(client.canceled, ["secret-code"])

    def test_approver_extends_pending_deadline(self):
        client, clock, controller = self.setup_controller(
            {"status": "pending", "opened": True, "expires_at": "1970-01-01T00:01:00Z", "poll_after": 10},
            {"status": "approved"})
        client.start["expires_in"] = 8
        controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(clock.waits, [5, 10])

    def test_terminal_states_do_not_poll_again(self):
        for state, error_type in [("denied", ApiError), ("consumed", ApiError), ("canceled", DeviceAuthCancelled), ("expired", DeviceAuthExpired)]:
            with self.subTest(state=state):
                client, clock, controller = self.setup_controller({"status": state})
                with self.assertRaises(error_type):
                    controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
                self.assertEqual(len(client.polled), 1)

    def test_404_is_actionable_error_and_is_not_an_automatic_new_code(self):
        client, clock, controller = self.setup_controller(ApiError(404, "Unavailable"))
        with self.assertRaises(ApiError) as error:
            controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(len(client.polled), 1)

    def test_unknown_status_is_not_silent_infinite_poll(self):
        client, clock, controller = self.setup_controller({"status": "surprise"})
        with self.assertRaises(ApiError) as error:
            controller.sign_in_device(lambda data: None, clock.wait, lambda: False)
        self.assertEqual(error.exception.code, "invalid_response")
        self.assertEqual(len(client.polled), 1)

    def test_advertised_auth_methods(self):
        client, clock, controller = self.setup_controller()
        self.assertEqual(controller.available_methods(), ["device", "password"])
        client.providers["password_login"] = False
        self.assertEqual(controller.available_methods(), ["device"])
        client.capability = {"state": "disabled"}
        self.assertEqual(controller.available_methods(), [])

    def test_old_server_device_capability_404_allows_password(self):
        client, clock, controller = self.setup_controller()
        client.capability = ApiError(404, "Missing")
        self.assertEqual(controller.available_methods(), ["password"])

    def test_discovery_connection_failures_are_not_hidden_as_password_only(self):
        client, clock, controller = self.setup_controller()
        client.capability = ApiError(0, "Offline")
        with self.assertRaises(ApiError):
            controller.available_methods()


if __name__ == "__main__":
    unittest.main()
