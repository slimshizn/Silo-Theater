"""Native Kodi sign-in form with transient credentials and no storage access."""

from .api import normalize_server_url


BACK_ACTIONS = frozenset({9, 10, 92, 216, 247, 257, 61448})
SERVER_CONTROL = 101
USERNAME_CONTROL = 102
PASSWORD_CONTROL = 103
SIGN_IN_CONTROL = 201
CODE_CONTROL = 202
CANCEL_CONTROL = 203


class LoginValidationError(ValueError):
    def __init__(self, field, message):
        super().__init__(message)
        self.field = field


def login_submission(action, server_url, username="", password="", allow_code=True):
    """Validate a submission without networking, logging or saving credentials."""
    try:
        server_url = normalize_server_url(str(server_url or "").strip())
    except (TypeError, ValueError) as error:
        raise LoginValidationError(
            "server_url", "Enter your Silo server's full http:// or https:// address."
        ) from error
    username = str(username or "").strip()
    if action == "code":
        if not allow_code:
            raise LoginValidationError("action", "Code sign-in is not available.")
        return {"action": "code", "server_url": server_url, "username": username}
    if action != "password":
        raise LoginValidationError("action", "Choose a sign-in method.")
    if not username:
        raise LoginValidationError("username", "Enter your Silo username.")
    if not isinstance(password, str) or not password:
        raise LoginValidationError("password", "Enter your Silo password.")
    return {"action": "password", "server_url": server_url,
            "username": username, "password": password}


def prompt_login(addon, server_url="", username="", allow_code=True, message=""):
    """Show server, username and masked password together; return data or None.

    Passwords only leave this function in a confirmed password submission.
    The caller owns authentication and may reopen the form with an inline
    message, remembered server and username. Passwords are never prefilled.
    """
    import xbmcgui

    class LoginWindow(xbmcgui.WindowXMLDialog):
        def __init__(self, *args, **kwargs):
            self.result = None
            self.initialized = False

        def onInit(self):
            if self.initialized:
                return
            self.initialized = True
            self.setProperty("silo.allow_code", "true" if allow_code else "")
            self.setProperty("silo.login_message", str(message or ""))
            server = self.getControl(SERVER_CONTROL)
            user = self.getControl(USERNAME_CONTROL)
            password = self.getControl(PASSWORD_CONTROL)
            server.setType(xbmcgui.INPUT_TYPE_TEXT, "Silo server address")
            user.setType(xbmcgui.INPUT_TYPE_TEXT, "Silo username")
            # INPUT_TYPE_PASSWORD masks input and returns the original string.
            # INPUT_TYPE_PASSWORD_MD5 would hash it and break server sign-in.
            password.setType(xbmcgui.INPUT_TYPE_PASSWORD, "Silo password")
            server.setText(str(server_url or ""))
            user.setText(str(username or ""))
            password.setText("")
            self.setFocusId(SERVER_CONTROL if not server_url else
                            USERNAME_CONTROL if not username else PASSWORD_CONTROL)

        def onAction(self, action):
            if action.getId() in BACK_ACTIONS:
                self.cancel()

        def onClick(self, control_id):
            if control_id == CANCEL_CONTROL:
                self.cancel()
            elif control_id in (SIGN_IN_CONTROL, CODE_CONTROL):
                action = "password" if control_id == SIGN_IN_CONTROL else "code"
                try:
                    self.result = login_submission(
                        action, self.getControl(SERVER_CONTROL).getText(),
                        self.getControl(USERNAME_CONTROL).getText(),
                        self.getControl(PASSWORD_CONTROL).getText() if action == "password" else "",
                        allow_code=allow_code,
                    )
                except LoginValidationError as error:
                    self.setProperty("silo.login_message", str(error))
                    self.setFocusId({"server_url": SERVER_CONTROL,
                                     "username": USERNAME_CONTROL,
                                     "password": PASSWORD_CONTROL}.get(error.field, SIGN_IN_CONTROL))
                    return
                self.getControl(PASSWORD_CONTROL).setText("")
                self.close()

        def cancel(self):
            self.result = None
            self.getControl(PASSWORD_CONTROL).setText("")
            self.close()

    window = LoginWindow("SiloLogin.xml", addon.getAddonInfo("path"), "Default", "1080i")
    try:
        window.doModal()
        result = window.result
        window.result = None
        return result
    finally:
        del window
