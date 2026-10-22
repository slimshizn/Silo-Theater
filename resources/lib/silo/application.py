"""Kodi application flow; networking and state live in the portable client."""

from copy import deepcopy
import math
import os
import time
from urllib.parse import urlsplit

import xbmc
import xbmcaddon
import xbmcgui
import xbmcvfs

from .api import ApiError, SiloClient
from .auth import AuthController, DeviceAuthCancelled, DeviceAuthExpired, NATIVE_PROVIDER_ID
from .media import available_versions, default_version, format_time, is_multipart, resume_position, version_label, version_name
from .playback import PlaybackController
from .settings import read_options
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
        self.client = None
        self._login_message = ""
        if server_url:
            try:
                self._connect(server_url)
            except ValueError as exc:
                self._login_message = str(exc)
        signed_in = False
        if self.client is not None and (self.store.get("access_token") or self.store.get("refresh_token")):
            try:
                self.client.me()
                signed_in = True
            except ApiError as exc:
                revoked = exc.status == 403 and not (
                    self.store.get("access_token") or self.store.get("refresh_token"))
                if exc.status != 401 and not revoked:
                    raise
        if not signed_in and not self.sign_in():
            return
        server_url = self.client.server_url
        if not self.choose_profile():
            return
        self._recover_playback()
        self.ensure_display_setup()

        while not self.monitor.abortRequested():
            window = HomeWindow("SiloHome.xml", self.addon.getAddonInfo("path"),
                                "Default", "1080i")
            window.application = self
            window.doModal()
            action = window.result
            del window
            if action == "profile":
                if not self.choose_profile(force=True):
                    return
                self._recover_playback()
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
                server_url = self.client.server_url
                self._recover_playback()
            else:
                return

    def _recover_playback(self):
        from .recovery import start_recovery

        self.recovery_worker = start_recovery(self.client)

    def _connect(self, server_url):
        version = "".join(char for char in str(self.addon.getAddonInfo("version")) if char in "0123456789.")[:24]
        self.client = SiloClient(server_url, self.store,
                                 user_agent="SiloTheater/" + version if version else "SiloTheater")
        self.server_name = urlsplit(self.client.server_url).hostname or "Silo"

    # The default display setting declares SDR and 1080p, and this build plays
    # original files only, so HDR and 4K titles are refused until the device is
    # described. Ask once, on the first launch that reaches the library.
    DISPLAY_CHOICES = (
        ("HD TV or monitor (SDR, up to 1080p)", "auto"),
        ("4K TV with HDR10", "hdr10"),
        ("4K TV with Dolby Vision", "dv"),
        ("Dolby Vision P7 FEL player (for example CoreELEC on a Dolby Vision Amlogic box)", "p7_fel"),
        ("Decide later in Settings", None),
    )

    def ensure_display_setup(self):
        if self.addon.getSetting("display_setup_done") == "true":
            return
        if self.addon.getSetting("dv_mode") not in ("", "auto") or self.addon.getSetting("max_resolution") not in ("", "auto"):
            # Someone already described this device in Settings.
            self.addon.setSetting("display_setup_done", "true")
            return
        selected = self.dialog.select("What can this TV show?", [label for label, _ in self.DISPLAY_CHOICES])
        if not isinstance(selected, int) or not 0 <= selected < len(self.DISPLAY_CHOICES):
            return  # Backed out: ask again next launch.
        mode = self.DISPLAY_CHOICES[selected][1]
        if mode is not None:
            self.addon.setSetting("dv_mode", mode)
        self.addon.setSetting("display_setup_done", "true")

    def sign_in(self):
        if self.client is None:
            return self.password_sign_in(message=getattr(self, "_login_message", ""))
        auth = AuthController(self.client)
        try:
            methods = auth.available_methods()
        except ApiError as exc:
            # Keep the address editable when an old server address is offline.
            return self.password_sign_in(message=exc.message)
        if not methods:
            self.dialog.ok("Silo sign-in", "This server currently offers no supported TV sign-in method.")
            return False
        saved = self.store.get("auth_method")
        configured = self.addon.getSetting("sign_in_method")
        preferred = "device" if configured == "code" else "password" if configured == "password" else saved or "password"
        method = preferred if preferred in methods else "password" if "password" in methods else methods[0]
        while not self.monitor.abortRequested():
            try:
                if method == "password":
                    return self.password_sign_in(methods=methods)
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
                           else "Use your Silo account" for value in retry_methods] + ["Cancel"]
                selected = self.dialog.select("Sign in to Silo", choices)
                if selected < 0 or selected == len(choices) - 1:
                    return False
                method = retry_methods[selected]
        return False

    def password_sign_in(self, methods=None, message=""):
        from .login import prompt_login

        server_url = self.client.server_url if self.client is not None else self.addon.getSetting("server_url").strip()
        username = self.store.get("login_username", "") if self.store.get("login_username_server") == server_url else ""
        known_server = self.client.server_url if self.client is not None and methods is not None else None
        while not self.monitor.abortRequested():
            credentials = prompt_login(self.addon, server_url=server_url, username=username,
                                       allow_code=methods is None or "device" in methods,
                                       allow_password=methods is None or "password" in methods, message=message)
            if credentials is None:
                return False
            server_url = credentials["server_url"]
            username = credentials.get("username", "")
            password = credentials.pop("password", "")
            try:
                if self.client is None or self.client.server_url != server_url:
                    self._connect(server_url)
                server_url = self.client.server_url
                auth = AuthController(self.client)
                if known_server != server_url or credentials["action"] == "discover":
                    methods = auth.available_methods()
                    known_server = server_url
                if credentials["action"] == "discover":
                    message = "" if "password" in methods else "Silo account sign-in is unavailable on this server."
                    if "password" not in methods and "device" in methods:
                        message += " Use a sign-in code."
                    continue
                if credentials["action"] == "code":
                    if "device" not in methods:
                        message = "Code sign-in is unavailable on this server."
                        if "password" in methods:
                            message += " Use your Silo account."
                        continue
                    if not self.device_sign_in(auth):
                        message = "Code sign-in was canceled."
                        if "password" in methods:
                            message += " You can use your Silo account instead."
                        continue
                    self.addon.setSetting("sign_in_method", "code")
                else:
                    if "password" not in methods:
                        message = "Silo account sign-in is unavailable on this server."
                        message += " Use a sign-in code." if "device" in methods else " Check the server address or contact its administrator."
                        continue
                    self.client.login(username, password, provider=NATIVE_PROVIDER_ID)
                    self.store.update({"auth_method": "password", "login_username": username,
                                       "login_username_server": server_url})
                    self.addon.setSetting("sign_in_method", "password")
                self.addon.setSetting("server_url", server_url)
                return True
            except DeviceAuthCancelled:
                message = "Code sign-in was canceled."
                if "password" in (methods or []):
                    message += " You can use your Silo account instead."
            except (ApiError, ValueError) as exc:
                message = exc.message if isinstance(exc, ApiError) else str(exc)
            finally:
                password = ""
                credentials.clear()
        return False

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

    def choose_profile(self, force=False):
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
        saved_exists = any(str(profile["id"]) == saved for profile in profiles)
        if saved_exists and not force:
            selected = preselect
        else:
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
        params = dict(state["params"], limit=self.options()["page_size"])
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
        content_id = card["content_id"]
        if card.get("play_content_id") and card.get("item_source") in ("in_progress", "next_up"):
            content_id = card["play_content_id"]
        item = self.client.item(content_id)
        children = self.client.children(item) if item.get("type") in ("series", "season") else []
        if isinstance(children, dict):
            children = children.get("items", [])
        window = DetailsWindow("SiloDetails.xml", self.addon.getAddonInfo("path"), "Default", "1080i")
        window.application, window.item, window.children = self, item, children
        window.doModal()
        del window

    def options(self):
        return read_options(self.addon)

    def choose_file(self, item, preferred_file_id=None):
        versions = available_versions(item)
        if not versions:
            self.dialog.ok("Silo", "No media files are available for this title.")
            return None
        default = default_version(item, self.options()["file_selection"], preferred_file_id)
        preselect = next(index for index, version in enumerate(versions) if version["file_id"] == default["file_id"])
        entries = [xbmcgui.ListItem(label=version_label(version), label2=version_name(version)) for version in versions]
        selected = self.dialog.select("Choose file", entries, preselect=preselect, useDetails=True)
        return versions[selected]["file_id"] if 0 <= selected < len(versions) else None

    def refresh_item(self, item, file_id=None):
        params = {"file_id": file_id} if file_id is not None else {}
        return self.client.item(item["content_id"], **params)

    def _playback_item_key(self, item, file_id):
        return (self.client.server_url, self.store.get("profile_id"), item.get("content_id"), file_id)

    def item_after_playback(self, item, file_id):
        """Update the details screen immediately, without a post-stop HTTP call."""
        cached = getattr(self, "_after_playback_item", None)
        if cached and cached[0] == self._playback_item_key(item, file_id):
            return deepcopy(cached[1])
        # Missing controller results and stale caches must never trigger networking.
        return deepcopy(item)

    def _cache_playback_item(self, item, file_id, result):
        detail = deepcopy(item)
        detail["file_id"] = file_id
        version = default_version(item, preferred_file_id=file_id)
        if isinstance(result, dict) and result.get("has_sample") is True and not is_multipart(version):
            position = result.get("position_seconds")
            if (isinstance(position, (int, float)) and not isinstance(position, bool)
                    and math.isfinite(position) and position >= 0):
                progress = dict(detail.get("user_data") or {})
                progress.update(position_seconds=position, last_file_id=file_id,
                                is_in_progress=position > 0 and result.get("ended") is not True)
                if result.get("ended") is True:
                    progress["played"] = True
                detail["user_data"] = progress
        self._after_playback_item = (self._playback_item_key(item, file_id), detail)

    def play(self, item, resume=None, file_id=None):
        """Generic Play follows preferences; explicit Resume/Start keeps its intent.

        Return the chosen file so the detail screen can retain it after refresh.
        Resume positions are source seconds, exactly as the catalog reports them.
        """
        if not self.is_playable(item):
            return None
        self._after_playback_item = None
        options = self.options()
        version = default_version(item, options["file_selection"], file_id)
        if not version:
            self.dialog.ok("Silo", "No media files are available for this title.")
            return None
        if file_id is not None and version["file_id"] != file_id:
            self.dialog.ok("Silo", "The selected file is no longer available. Choose another file.")
            return None
        if file_id is None and options["file_selection"] == "ask" and len(available_versions(item)) > 1:
            file_id = self.choose_file(item, version["file_id"])
            if file_id is None:
                return None
        else:
            file_id = version["file_id"]
        # Resolve effective markers and the latest progress for this exact file.
        selected_item = self.refresh_item(item, file_id)
        position = self.resume_seconds(selected_item)
        selected_version = default_version(selected_item, preferred_file_id=file_id)
        multipart = is_multipart(selected_version)
        if resume is None and multipart:
            # Silo intentionally avoids applying an item-global offset to a part.
            resume = options["resume_behavior"] != "start"
        elif resume is None and position:
            behavior = options["resume_behavior"]
            if behavior == "ask":
                selected = self.dialog.select("Play {}".format(selected_item.get("title", "title")),
                                              ["Resume from {}".format(format_time(position)), "Play from start"])
                if selected < 0:
                    return None
                resume = selected == 0
            else:
                resume = behavior == "resume"
        start_position = None if resume and multipart else position if resume else 0.0
        controller = PlaybackController(self.client, self.addon)
        try:
            controller.play(selected_item, start_position=start_position, file_id=file_id, options=options)
        finally:
            self._cache_playback_item(selected_item, file_id, getattr(controller, "last_result", None))
        return file_id

    @staticmethod
    def is_playable(item):
        return item.get("type") in ("movie", "episode")

    @staticmethod
    def resume_seconds(item):
        return resume_position(item)

    def artwork(self, item):
        result = {}
        for key, kind in (("poster", "poster"), ("fanart", "backdrop")):
            url = self.client.image_url(item, kind)
            if url:
                result[key] = url
        return result
