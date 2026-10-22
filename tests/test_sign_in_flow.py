"""Sign-in journeys: one form, editable retries and remembered sessions."""

import sys
import types
import unittest
from unittest.mock import MagicMock, patch

try:
    from .test_ui import Application, ApiError, Store, application_module
except ImportError:
    from test_ui import Application, ApiError, Store, application_module


BASE = "https://silo.example/prefix"


class SignInFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = Application.__new__(Application)
        self.app.addon = MagicMock()
        self.settings = {"server_url": BASE, "sign_in_method": "password"}
        self.app.addon.getSetting.side_effect = lambda key: self.settings.get(key, "")
        self.app.addon.getAddonInfo.return_value = "."
        self.app.dialog = MagicMock()
        self.app.monitor = MagicMock()
        self.app.monitor.abortRequested.return_value = False
        self.app.store = Store()
        self.app.client = MagicMock(server_url=BASE)
        self.app.client.device_capability.return_value = {"state": "available"}
        self.app.client.auth_providers.return_value = {"password_login": True, "items": [
            {"id": "local", "display_name": "Silo account", "mode": "credentials", "default": True},
        ]}
        self.app.client.profiles.return_value = {"items": [
            {"id": "first", "name": "First", "has_pin": False},
            {"id": "saved", "name": "Saved", "has_pin": False},
        ]}
        self.prompt = MagicMock()
        self.login_module = types.SimpleNamespace(prompt_login=self.prompt)

    def form(self, action="password", server_url=BASE, username="alice", password="typed-only"):
        value = {"action": action, "server_url": server_url, "username": username}
        if action == "password":
            value["password"] = password
        return value

    def login(self, **kwargs):
        with patch.dict(sys.modules, {application_module.__package__ + ".login": self.login_module}):
            return self.app.password_sign_in(**kwargs)

    def test_first_run_opens_login_without_welcome_or_settings_detour(self):
        self.settings["server_url"] = ""
        self.app.sign_in = MagicMock()
        self.app.choose_profile = MagicMock(return_value=True)

        def signed_in():
            self.app.client = MagicMock(server_url=BASE)
            return True

        self.app.sign_in.side_effect = signed_in
        with patch.object(application_module, "HomeWindow") as home:
            home.return_value.result = None
            self.app._run()
        self.app.sign_in.assert_called_once_with()
        self.app.addon.openSettings.assert_not_called()
        self.app.dialog.ok.assert_not_called()

    def test_available_password_login_opens_form_without_method_menu(self):
        self.app.password_sign_in = MagicMock(return_value=True)
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.password_sign_in.assert_called_once_with(methods=["device", "password"])
        self.app.dialog.select.assert_not_called()

    def test_form_configures_new_server_and_remembers_username_without_password(self):
        self.settings["server_url"] = ""
        self.app.client = None
        new_client = MagicMock(server_url=BASE)
        self.prompt.return_value = self.form(password="private-password")
        with patch.object(application_module, "SiloClient", return_value=new_client), \
                patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["password"]
            self.assertTrue(self.login())
        new_client.login.assert_called_once_with("alice", "private-password", provider="local")
        self.assertEqual(self.app.store["login_username"], "alice")
        self.assertEqual(self.app.store["login_username_server"], BASE)
        self.assertNotIn("private-password", repr(self.app.store))
        self.app.addon.setSetting.assert_any_call("server_url", BASE)
        self.app.addon.setSetting.assert_any_call("sign_in_method", "password")
        self.app.dialog.input.assert_not_called()

    def test_changing_sign_in_preference_overrides_previous_code_login(self):
        self.app.store["auth_method"] = "device"
        self.app.password_sign_in = MagicMock(return_value=True)
        self.app.device_sign_in = MagicMock()
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.password_sign_in.assert_called_once()
        self.app.device_sign_in.assert_not_called()

    def test_wrong_password_returns_to_form_with_inline_error_and_prefilled_username(self):
        self.app.client.login.side_effect = [ApiError(401, "Incorrect username or password."), None]
        self.prompt.side_effect = [self.form(password="wrong"), self.form(password="correct")]
        self.assertTrue(self.login(methods=["password"]))
        self.assertEqual(self.prompt.call_count, 2)
        self.assertEqual(self.prompt.call_args.kwargs["username"], "alice")
        self.assertEqual(self.prompt.call_args.kwargs["server_url"], BASE)
        self.assertEqual(self.prompt.call_args.kwargs["message"], "Incorrect username or password.")
        self.app.dialog.ok.assert_not_called()
        self.app.dialog.select.assert_not_called()
        self.assertNotIn("correct", repr(self.app.store))
        self.assertTrue(all(call.kwargs.get("provider") == "local" for call in self.app.client.login.call_args_list))

    def test_canceling_login_does_not_send_credentials(self):
        self.prompt.return_value = None
        self.assertFalse(self.login(methods=["password"]))
        self.app.client.login.assert_not_called()
        self.assertEqual(self.app.store, {})

    def test_code_cancel_returns_to_password_form_without_another_method_menu(self):
        self.prompt.side_effect = [self.form(action="code"), self.form()]
        self.app.device_sign_in = MagicMock(side_effect=application_module.DeviceAuthCancelled())
        self.assertTrue(self.login(methods=["device", "password"]))
        self.assertIn("canceled", self.prompt.call_args.kwargs["message"])
        self.app.client.login.assert_called_once_with("alice", "typed-only", provider="local")
        self.app.dialog.select.assert_not_called()

    def test_native_login_names_local_provider_even_when_external_credentials_are_default(self):
        self.app.client.auth_providers.return_value["items"].insert(0, {
            "id": "plugin-directory", "display_name": "Directory", "mode": "credentials", "default": True,
        })
        self.app.client.auth_providers.return_value["items"][1]["default"] = False
        self.prompt.return_value = self.form()
        self.assertTrue(self.login())
        self.app.client.login.assert_called_once_with("alice", "typed-only", provider="local")

    def test_external_only_credentials_are_never_used_as_password_fallback(self):
        self.app.client.auth_providers.return_value = {"password_login": True, "items": [
            {"id": "plugin-directory", "display_name": "Directory", "mode": "credentials", "default": True},
        ]}
        self.prompt.side_effect = [self.form(), None]
        self.assertFalse(self.login())
        self.app.client.login.assert_not_called()
        self.assertFalse(self.prompt.call_args.kwargs["allow_password"])
        self.assertTrue(self.prompt.call_args.kwargs["allow_code"])
        self.assertIn("Silo account sign-in is unavailable", self.prompt.call_args.kwargs["message"])

    def test_native_password_failure_does_not_try_another_provider(self):
        self.app.client.auth_providers.return_value["items"].append({
            "id": "plugin-directory", "mode": "credentials", "default": False,
        })
        self.app.client.login.side_effect = ApiError(401, "Incorrect Silo account password.")
        self.prompt.side_effect = [self.form(password="wrong"), None]
        self.assertFalse(self.login())
        self.app.client.login.assert_called_once_with("alice", "wrong", provider="local")
        self.assertEqual(self.prompt.call_args.kwargs["message"], "Incorrect Silo account password.")

    def test_check_server_can_move_from_disabled_native_login_to_another_server(self):
        new_url = "https://new-silo.example"
        new_client = MagicMock(server_url=new_url)
        new_client.device_capability.return_value = {"state": "available"}
        new_client.auth_providers.return_value = {"password_login": True, "items": [
            {"id": "local", "display_name": "Silo account", "mode": "credentials", "default": True},
        ]}
        self.prompt.side_effect = [self.form(action="discover", server_url=new_url), self.form(server_url=new_url)]
        with patch.object(application_module, "SiloClient", return_value=new_client):
            self.assertTrue(self.login(methods=["device"]))
        self.assertFalse(self.prompt.call_args_list[0].kwargs["allow_password"])
        self.assertTrue(self.prompt.call_args_list[1].kwargs["allow_password"])
        new_client.login.assert_called_once_with("alice", "typed-only", provider="local")

    def test_remembered_username_is_bound_to_its_server(self):
        self.app.store.update(login_username="previous-user", login_username_server="https://other.example")
        self.prompt.return_value = None
        self.assertFalse(self.login(methods=["password"]))
        self.assertEqual(self.prompt.call_args.kwargs["username"], "")
        self.app.store["login_username_server"] = BASE
        self.assertFalse(self.login(methods=["password"]))
        self.assertEqual(self.prompt.call_args.kwargs["username"], "previous-user")

    def test_saved_unlocked_profile_skips_picker_but_manual_switch_still_opens_it(self):
        self.app.store["profile_id"] = "saved"
        self.assertTrue(self.app.choose_profile())
        self.app.dialog.select.assert_not_called()
        self.app.client.select_profile.assert_called_with("saved", None)
        self.app.dialog.select.return_value = 0
        self.assertTrue(self.app.choose_profile(force=True))
        self.app.dialog.select.assert_called_once()
        self.app.client.select_profile.assert_called_with("first", None)

    def test_saved_locked_profile_still_requires_pin(self):
        self.app.store["profile_id"] = "saved"
        self.app.client.profiles.return_value["items"][1]["has_pin"] = True
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.return_value = {"valid": True, "profile_token": "new-proof"}
        self.assertTrue(self.app.choose_profile())
        self.app.dialog.select.assert_not_called()
        self.app.dialog.input.assert_called_once()
        self.app.client.verify_pin.assert_called_once_with("saved", "1234")
        self.app.client.select_profile.assert_called_once_with("saved", "new-proof")

    def test_valid_session_and_refresh_only_session_both_skip_login(self):
        for access_token in ("access", None):
            with self.subTest(access_token=access_token):
                self.app.store.update(access_token=access_token, refresh_token="refresh", profile_id="saved")
                client = MagicMock(server_url=BASE)
                client.profiles.return_value = {"items": [{"id": "saved", "name": "Saved", "has_pin": False}]}
                self.app.sign_in = MagicMock()
                with patch.object(application_module, "SiloClient", return_value=client), \
                        patch.object(application_module, "HomeWindow") as home:
                    home.return_value.result = None
                    self.app._run()
                client.me.assert_called_once()
                self.app.sign_in.assert_not_called()
                self.app.dialog.select.assert_not_called()

    def test_revoked_refresh_reopens_login_during_the_same_launch(self):
        self.app.store.update(access_token="expired", refresh_token="revoked", profile_id="saved")
        client = self.app.client

        def revoked():
            self.app.store.update(access_token=None, refresh_token=None)
            raise ApiError(403, "Session revoked.")

        client.me.side_effect = revoked
        self.app.sign_in = MagicMock(return_value=True)
        with patch.object(application_module, "SiloClient", return_value=client), \
                patch.object(application_module, "HomeWindow") as home:
            home.return_value.result = None
            self.app._run()
        self.app.sign_in.assert_called_once_with()
        self.app.dialog.ok.assert_not_called()

    def test_account_access_failure_is_not_retried_as_login(self):
        self.app.store.update(access_token="valid", refresh_token="valid-refresh")
        client = self.app.client
        client.me.side_effect = ApiError(403, "Account cannot access this server.")
        self.app.sign_in = MagicMock()
        with patch.object(application_module, "SiloClient", return_value=client):
            with self.assertRaises(ApiError):
                self.app._run()
        self.app.sign_in.assert_not_called()


if __name__ == "__main__":
    unittest.main()
