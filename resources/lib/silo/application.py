"""Kodi application flow; networking and state live in the portable client."""

import os
import time
from urllib.parse import urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from .api import ApiError, SiloClient
from .auth import AuthController, DeviceAuthCancelled, DeviceAuthExpired
from .playback import PlaybackController
from .storage import SessionStore, StorageError
from .ui import DetailsWindow, HomeWindow


class Application:
    def __init__(self):
        self.addon = xbmcaddon.Addon()
        self.dialog = xbmcgui.Dialog()
        self.monitor = xbmc.Monitor()
        self.store = None
        self.client = None
        self.profile_name = ""
        self.server_name = ""
        self.browse = None

    def run(self):
        try:
            self._run()
        except ApiError as exc:
            self.show_error(exc)
        except (OSError, ValueError, StorageError) as exc:
            self.dialog.ok("Silo", str(exc))
        except Exception:
            # Do not log exception text: networking exceptions can contain signed URLs.
            xbmc.log("[Silo] Unexpected application error", xbmc.LOGERROR)
            self.dialog.ok("Silo", "Silo could not continue. Check the Kodi log for diagnostics.")

    def _run(self):
        if self.store is None:
            self.store = SessionStore(os.path.join(
                xbmcvfs.translatePath(self.addon.getAddonInfo("profile")), "session.json"))
        server_url = self.addon.getSetting("server_url").strip()
        if not server_url:
            self.dialog.ok("Welcome to Silo", "Enter your Silo server address in the add-on settings to begin.")
            self.addon.openSettings()
            server_url = self.addon.getSetting("server_url").strip()
        if not server_url:
            return
        self.client = SiloClient(server_url, self.store)
        self.server_name = urlsplit(server_url).hostname or "Silo"
        self.client.server_info()
        signed_in = False
        if self.store.get("access_token"):
            try:
                self.client.me()
                signed_in = True
            except ApiError as exc:
                if exc.status != 401:
                    raise
        if not signed_in and not self.sign_in():
            return
        if not self.choose_profile():
            return

        while not self.monitor.abortRequested():
            window = HomeWindow("SiloHome.xml", self.addon.getAddonInfo("path"),
                                "Default", "1080i")
            window.application = self
            window.doModal()
            action = window.result
            del window
            if action == "profile":
                if not self.choose_profile():
                    return
            elif action == "settings":
                self.addon.openSettings()
                if self.addon.getSetting("server_url").strip() != server_url:
                    self._run()
                    return
            elif action == "logout":
                try:
                    self.client.logout()
                except ApiError:
                    # The client has already cleared local credentials in finally.
                    pass
                if not self.sign_in() or not self.choose_profile():
                    return
            else:
                return

    def sign_in(self):
        auth = AuthController(self.client)
        methods = auth.available_methods()
        if not methods:
            self.dialog.ok("Silo sign-in", "This server currently offers no supported TV sign-in method.")
            return False
        saved = self.store.get("auth_method")
        preferred = saved or ("device" if self.addon.getSetting("sign_in_method") == "code" else "password")
        if saved in methods:
            method = saved
        elif len(methods) == 1:
            method = methods[0]
        else:
            selected = self.dialog.select("Sign in to Silo", [
                "Sign in with a code" if value == "device" else "Username and password" for value in methods
            ], preselect=methods.index(preferred) if preferred in methods else 0)
            if selected < 0:
                return False
            method = methods[selected]
        while not self.monitor.abortRequested():
            try:
                if method == "password":
                    return self.password_sign_in()
                if self.device_sign_in(auth):
                    self.addon.setSetting("sign_in_method", "code")
                    return True
                return False
            except DeviceAuthCancelled:
                return False
            except (DeviceAuthExpired, ApiError) as exc:
                message = "The sign-in code expired." if isinstance(exc, DeviceAuthExpired) else exc.message
                self.dialog.ok("Silo sign-in", message)
                retry_methods = [method] + [value for value in methods if value != method]
                choices = ["Try code sign-in again" if value == "device"
                           else "Use username and password" for value in retry_methods] + ["Cancel"]
                selected = self.dialog.select("Sign in to Silo", choices)
                if selected < 0 or selected == len(choices) - 1:
                    return False
                method = retry_methods[selected]
        return False

    def password_sign_in(self):
        username = self.dialog.input("Silo username", type=xbmcgui.INPUT_ALPHANUM)
        if not username:
            return False
        password = self.dialog.input("Silo password", type=xbmcgui.INPUT_ALPHANUM,
                                     option=xbmcgui.ALPHANUM_HIDE_INPUT)
        if not password:
            return False
        self.client.login(username, password)
        self.store.set("auth_method", "password")
        self.addon.setSetting("sign_in_method", "password")
        return True

    def device_sign_in(self, auth):
        progress = xbmcgui.DialogProgress()
        started = [time.monotonic(), 0, ""]

        def show_code(response):
            expires = max(1, int(response.get("expires_in", 600)))
            started[:] = [time.monotonic(), expires,
                          "Open {} on another device.\n\nEnter code: {}".format(
                              response.get("verification_uri") or response.get("verification_url")
                              or self.client.server_url + "/activate", response["user_code"])]
            progress.create("Sign in to Silo", started[2])

        def cancelled():
            return self.monitor.abortRequested() or progress.iscanceled()

        def wait(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if cancelled() or self.monitor.waitForAbort(min(0.2, max(0, deadline - time.monotonic()))):
                    return True
                if started[1]:
                    elapsed = time.monotonic() - started[0]
                    progress.update(min(99, int(100 * elapsed / started[1])), started[2])
            return False

        try:
            auth.sign_in_device(show_code, wait, cancelled)
            self.store.set("auth_method", "device")
            return True
        finally:
            progress.close()

    def choose_profile(self):
        if self.store.get("temporary_session"):
            profile_id = self.store.get("bound_profile_id")
            if not profile_id:
                raise ApiError(401, "This temporary session has no bound profile.")
            self.client.select_profile(profile_id, self.store.get("profile_token"))
            self.profile_name = "Temporary session"
            return True
        profiles = self.client.profiles().get("items", [])
        if not profiles:
            self.dialog.ok("Silo profiles", "No profiles are available for this account.")
            return False
        saved = str(self.store.get("profile_id") or "")
        preselect = next((i for i, profile in enumerate(profiles) if str(profile["id"]) == saved), 0)
        selected = 0 if len(profiles) == 1 else self.dialog.select(
            "Who's watching?", [str(profile["name"]) + ("  ·  PIN" if profile.get("has_pin") else "")
                               for profile in profiles], preselect=preselect)
        if selected < 0:
            return False
        profile = profiles[selected]
        token = None
        if profile.get("has_pin"):
            pin = self.dialog.input("PIN for {}".format(profile["name"]), type=xbmcgui.INPUT_ALPHANUM,
                                    option=xbmcgui.ALPHANUM_HIDE_INPUT)
            if not pin:
                return False
            try:
                verification = self.client.verify_pin(profile["id"], pin)
            except ApiError as exc:
                self.show_error(exc)
                return False
            if not verification.get("valid") or not verification.get("profile_token"):
                self.dialog.ok("Silo profile", "The PIN was not accepted. Reopen Silo when you are ready to try again.")
                return False
            token = verification["profile_token"]
        self.client.select_profile(profile["id"], token)
        self.profile_name = str(profile["name"])
        return True

    def guard(self, callback):
        try:
            return callback()
        except ApiError as exc:
            self.show_error(exc)
        except (OSError, ValueError, StorageError) as exc:
            self.dialog.ok("Silo", str(exc))
        except Exception:
            xbmc.log("[Silo] Unexpected window action error", xbmc.LOGERROR)
            self.dialog.ok("Silo", "This action could not be completed.")
        return None

    def show_error(self, exc):
        if exc.status == 401:
            self.dialog.ok("Silo sign-in", "Your session has expired. Close and reopen Silo to sign in again.")
        elif exc.status == 403:
            self.dialog.ok("Silo access", exc.message or "This profile cannot access that title.")
        else:
            self.dialog.ok("Silo", exc.message or "The server could not complete the request.")

    def home(self, window):
        response = self.client.hubs()
        sections = response.get("sections", [])
        self.browse = None
        first = next((section for section in sections if section.get("items")), None)
        window.display("Home", sections, first.get("items", []) if first else [],
                       first["title"] if first else "Your Silo library")

    def show_section(self, window, section):
        if "library" in section:
            params = dict(self.browse["params"] if self.browse else {})
            params["library_id"] = section["id"]
            self.browse = {"heading": section["title"], "sections": window.sections, "params": params}
            self._browse_page(window)
            return
        self.browse = {"heading": section["title"], "sections": window.sections,
                       "params": {"source": "section", "scope": "home", "section_id": section["id"]}}
        self._browse_page(window)

    def search(self, window):
        query = self.dialog.input("Search Silo", type=xbmcgui.INPUT_ALPHANUM)
        if not query.strip():
            return
        selected = self.dialog.select("Search in", ["Movies and TV series", "Movies", "TV series", "Episodes"])
        if selected < 0:
            return
        media_type = ("video", "movie", "series", "episode")[selected]
        self.browse = {"heading": "Search: {}".format(query.strip()), "sections": [],
                       "params": {"q": query.strip(), "type": media_type}}
        self._browse_page(window)

    def libraries(self, window):
        libraries = [library for library in self.client.libraries().get("items", [])
                     if library.get("type") in ("movies", "series", "mixed")]
        if not libraries:
            self.dialog.ok("Silo libraries", "No video libraries are available for this profile.")
            return
        selected = self.dialog.select("Video libraries", [str(library["name"]) for library in libraries])
        if selected < 0:
            return
        library = libraries[selected]
        selected = self.dialog.select("Show", ["Movies and TV series", "Movies", "TV series", "Episodes"])
        if selected < 0:
            return
        media_type = ("video", "movie", "series", "episode")[selected]
        selected = self.dialog.select("Browse order", ["Title A–Z", "Recently added", "Newest releases", "Highest rating"])
        if selected < 0:
            return
        sections = [{"id": entry["id"], "title": entry["name"], "library": entry} for entry in libraries]
        self.browse = {"heading": library["name"], "sections": sections,
                       "params": {"library_id": library["id"], "type": media_type,
                                  "sort": ("title", "-added_at", "-release_date", "-rating")[selected]}}
        self._browse_page(window)

    def _browse_page(self, window, cursor=None):
        state = self.browse
        params = dict(state["params"], limit=50)
        if cursor:
            params["cursor"] = cursor
        response = self.client.items(**params)
        page = response.get("page") or {}
        state["next_cursor"] = page.get("next_cursor") if page.get("has_more") else None
        count = response.get("total")
        subtitle = "{} titles".format(count) if count is not None else ""
        window.display(state["heading"], state["sections"], response.get("items", []),
                       subtitle, bool(state["next_cursor"]))

    def next_page(self, window):
        if self.browse and self.browse.get("next_cursor"):
            self._browse_page(window, self.browse["next_cursor"])

    def open_item(self, card):
        item = self.client.item(card["content_id"])
        children = self.client.children(item) if item.get("type") in ("series", "season") else []
        if isinstance(children, dict):
            children = children.get("items", [])
        window = DetailsWindow("SiloDetails.xml", self.addon.getAddonInfo("path"), "Default", "1080i")
        window.application, window.item, window.children = self, item, children
        window.doModal()
        del window

    def play(self, item, resume=True):
        if not self.is_playable(item):
            return
        PlaybackController(self.client, self.addon).play(item, start_position=None if resume else 0)

    @staticmethod
    def is_playable(item):
        return item.get("type") in ("movie", "episode")

    @staticmethod
    def resume_seconds(item):
        value = (item.get("user_data") or {}).get("position_seconds", item.get("position_seconds", 0))
        return max(0.0, float(value or 0))

    def artwork(self, item):
        result = {}
        for key, kind in (("poster", "poster"), ("fanart", "backdrop")):
            url = self.client.image_url(item, kind)
            if url:
                result[key] = url
        return result
