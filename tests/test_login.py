"""Native sign-in form tests with Kodi controls replaced at the boundary."""

from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resources/lib"))

from silo.login import (CANCEL_CONTROL, CODE_CONTROL, PASSWORD_CONTROL, SERVER_CONTROL,
                        SIGN_IN_CONTROL, USERNAME_CONTROL, LoginValidationError,
                        login_submission, prompt_login)


class Edit:
    def __init__(self):
        self.text = ""
        self.input_type = None
        self.heading = ""

    def setType(self, kind, heading):
        self.input_type, self.heading = kind, heading

    def setText(self, text):
        self.text = text

    def getText(self):
        return self.text


class LoginTests(unittest.TestCase):
    def window(self, interact, **kwargs):
        windows = []

        class Window:
            def __new__(cls, *args):
                window = object.__new__(cls)
                window.controls = {key: Edit() for key in (SERVER_CONTROL, USERNAME_CONTROL, PASSWORD_CONTROL)}
                window.properties, window.focus, window.closed = {}, None, False
                window.arguments = args
                windows.append(window)
                return window

            def getControl(self, key):
                return self.controls[key]

            def setProperty(self, key, value):
                self.properties[key] = value

            def setFocusId(self, key):
                self.focus = key

            def close(self):
                self.closed = True

            def doModal(self):
                self.onInit()
                interact(self)

        gui = SimpleNamespace(WindowXMLDialog=Window, INPUT_TYPE_TEXT=0, INPUT_TYPE_PASSWORD=6)
        addon = SimpleNamespace(getAddonInfo=lambda key: str(ROOT))
        with patch.dict(sys.modules, {"xbmcgui": gui}):
            result = prompt_login(addon, **kwargs)
        return result, windows[0]

    def test_confirmed_password_uses_one_form_and_masked_original_input(self):
        def interact(window):
            self.assertEqual(window.controls[SERVER_CONTROL].text, "https://silo.invalid")
            self.assertEqual(window.controls[USERNAME_CONTROL].text, "alice")
            self.assertEqual(window.controls[PASSWORD_CONTROL].text, "")
            self.assertEqual(window.controls[PASSWORD_CONTROL].input_type, 6)
            self.assertEqual(window.focus, PASSWORD_CONTROL)
            window.controls[PASSWORD_CONTROL].setText(" secret with spaces ")
            window.onClick(SIGN_IN_CONTROL)

        result, window = self.window(interact, server_url="https://silo.invalid", username="alice")
        self.assertEqual(result, {"action": "password", "server_url": "https://silo.invalid",
                                  "username": "alice", "password": " secret with spaces "})
        self.assertTrue(window.closed)
        self.assertEqual(window.controls[PASSWORD_CONTROL].text, "")
        self.assertIsNone(window.result)
        self.assertEqual(window.arguments[0], "SiloLogin.xml")

    def test_missing_fields_show_inline_error_without_closing_or_extra_dialogs(self):
        def interact(window):
            window.onClick(SIGN_IN_CONTROL)
            self.assertFalse(window.closed)
            self.assertEqual(window.focus, SERVER_CONTROL)
            window.controls[SERVER_CONTROL].setText("https://silo.invalid")
            window.onClick(SIGN_IN_CONTROL)
            self.assertEqual(window.focus, USERNAME_CONTROL)
            window.controls[USERNAME_CONTROL].setText("alice")
            window.onClick(SIGN_IN_CONTROL)
            self.assertEqual(window.focus, PASSWORD_CONTROL)
            self.assertIn("password", window.properties["silo.login_message"])
            window.onClick(CANCEL_CONTROL)

        result, _ = self.window(interact)
        self.assertIsNone(result)

    def test_code_action_never_returns_typed_password(self):
        def interact(window):
            window.controls[PASSWORD_CONTROL].setText("must not escape")
            window.onClick(CODE_CONTROL)

        result, window = self.window(interact, server_url="https://silo.invalid")
        self.assertEqual(result["action"], "code")
        self.assertNotIn("password", result)
        self.assertEqual(window.controls[PASSWORD_CONTROL].text, "")

    def test_disabled_code_action_stays_in_form_and_is_hidden(self):
        def interact(window):
            self.assertEqual(window.properties["silo.allow_code"], "")
            window.onClick(CODE_CONTROL)
            self.assertFalse(window.closed)
            self.assertIn("not available", window.properties["silo.login_message"])
            window.onClick(CANCEL_CONTROL)

        result, _ = self.window(interact, server_url="https://silo.invalid", allow_code=False)
        self.assertIsNone(result)

    def test_disabled_native_login_can_only_check_the_server_without_sending_a_password(self):
        def interact(window):
            self.assertEqual(window.properties["silo.allow_password"], "")
            self.assertEqual(window.properties["silo.sign_in_label"], "Check server")
            self.assertEqual(window.focus, CODE_CONTROL)
            window.controls[PASSWORD_CONTROL].setText("must not escape")
            window.onClick(SIGN_IN_CONTROL)

        result, _ = self.window(interact, server_url="https://silo.invalid", allow_password=False)
        self.assertEqual(result, {"action": "discover", "server_url": "https://silo.invalid", "username": ""})
        with self.assertRaises(LoginValidationError):
            login_submission("password", "https://silo.invalid", "alice", "secret", allow_password=False)

    def test_server_remains_editable_when_no_native_sign_in_method_is_available(self):
        def interact(window):
            self.assertEqual(window.focus, SERVER_CONTROL)
            window.controls[SERVER_CONTROL].setText("https://other-silo.invalid")
            window.onClick(SIGN_IN_CONTROL)

        result, _ = self.window(interact, server_url="https://silo.invalid", allow_password=False, allow_code=False)
        self.assertEqual(result["action"], "discover")
        self.assertEqual(result["server_url"], "https://other-silo.invalid")
        self.assertNotIn("password", result)

    def test_back_cancels_and_clears_password(self):
        def interact(window):
            window.controls[PASSWORD_CONTROL].setText("must not persist")
            window.onAction(SimpleNamespace(getId=lambda: 92))

        result, window = self.window(interact, server_url="https://silo.invalid")
        self.assertIsNone(result)
        self.assertTrue(window.closed)
        self.assertEqual(window.controls[PASSWORD_CONTROL].text, "")

    def test_retry_message_preserves_server_and_username_without_a_password(self):
        def interact(window):
            self.assertEqual(window.properties["silo.login_message"], "Check your username or password.")
            self.assertEqual(window.controls[PASSWORD_CONTROL].text, "")
            window.onClick(CANCEL_CONTROL)

        self.window(interact, server_url="https://silo.invalid", username="alice",
                    message="Check your username or password.")

    def test_submission_rejects_embedded_credentials_and_preserves_proxy_prefix(self):
        with self.assertRaises(LoginValidationError) as error:
            login_submission("password", "https://user:secret@silo.invalid", "alice", "password")
        self.assertNotIn("secret", str(error.exception))
        self.assertEqual(login_submission("password", " https://silo.invalid/prefix/ ", " alice ", "pw")["server_url"],
                         "https://silo.invalid/prefix")

    def test_skin_has_all_controller_fields_and_action_buttons(self):
        root = ET.parse(ROOT / "resources/skins/Default/1080i/SiloLogin.xml").getroot()
        controls = {int(control.get("id")): control for control in root.findall(".//control[@id]")}
        for key in (SERVER_CONTROL, USERNAME_CONTROL, PASSWORD_CONTROL):
            self.assertEqual(controls[key].get("type"), "edit")
        for key in (SIGN_IN_CONTROL, CODE_CONTROL, CANCEL_CONTROL):
            self.assertEqual(controls[key].get("type"), "button")
        self.assertIn("silo.allow_code", controls[CODE_CONTROL].findtext("visible"))


if __name__ == "__main__":
    unittest.main()
