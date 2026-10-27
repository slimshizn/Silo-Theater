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
from . import present, track_prefs
from .present import avatar_image, video_items
from .settings import read_options
from .storage import SessionStore, StorageError
from .ui import DetailsWindow, HomeWindow


# play(subtitle_index=AUTO): use the remembered choice or the profile's preferences.
AUTO = object()


class Application:
    def __init__(self):
        self.addon = xbmcaddon.Addon()
        self.dialog = xbmcgui.Dialog()
        self.monitor = xbmc.Monitor()
        self.store = None
        self.client = None
        self.profile = {}
        self.profile_name = ""
        self.profile_avatar = ""
        self.server_name = ""
        self._libraries = None
        self._server_settings = None
        self._theme = None

    def run(self, argv=None):
        """Start the add-on; ``play <id>`` / ``open <id>`` arguments deep-link
        straight to a title (used by the Kodi home-screen widgets)."""
        from .deeplink import deep_link

        self.deep_link = deep_link([str(arg) for arg in (argv or [])[1:]])
        home = xbmcgui.Window(10000) if hasattr(xbmcgui, "Window") else None
        if home is not None and self.deep_link and home.getProperty("silo.running"):
            # A widget click while Founders Theater is open: never start a second copy.
            self.dialog.notification("Founders Theater", "Founders Theater is already open.", time=3000, sound=False)
            return
        if home is not None:
            home.setProperty("silo.running", "1")
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
        finally:
            if home is not None:
                home.clearProperty("silo.running")
            if getattr(self, "_theme", None) is not None:
                self._theme.close()

    def _profile_dir(self):
        return xbmcvfs.translatePath(self.addon.getAddonInfo("profile"))

    def device_id(self):
        """A stable id for this Kodi installation, created on first use."""
        import uuid
        from .storage import SessionStore

        folder = str(self._profile_dir())
        if not os.path.isabs(folder):
            return None
        try:
            store = SessionStore(os.path.join(folder, "device.json"))
            value = store.get("device_id")
            if not isinstance(value, str) or not value:
                value = "kodi-" + uuid.uuid4().hex
                store.set("device_id", value)
            return value
        except (StorageError, OSError, AttributeError):
            return None

    # Server settings this add-on shows, with the server manifest's defaults.
    SERVER_SETTINGS = {"ui.theme_music_enabled": False, "ui.theme_music_loop": False,
                       "playback.buffering_quality_mode": "always"}

    def server_setting(self, key):
        """A server setting as it applies to this profile on this device (cached per profile)."""
        if getattr(self, "_server_settings", None) is None:
            try:
                self._server_settings = self.client.effective_settings(self.SERVER_SETTINGS)
            except (ApiError, OSError, AttributeError):
                # Defaults for this profile until it changes; never one request per screen row.
                self._server_settings = {}
        value = self._server_settings.get(key)
        return self.SERVER_SETTINGS.get(key) if value is None else value

    def set_server_setting(self, key, value):
        """Save a server setting for this profile on this device (see api.put_setting)."""
        self.client.put_setting(key, value)
        if getattr(self, "_server_settings", None) is not None:
            self._server_settings[key] = value

    def theme(self):
        """The add-on's single theme music player (theme_music.py)."""
        if getattr(self, "_theme", None) is None:
            from .theme_music import ThemeMusic
            self._theme = ThemeMusic(self.client, self.server_setting, monitor=self.monitor)
        return self._theme

    def _session_path(self, server_url):
        """Each server keeps its own sign-in, so switching servers keeps both."""
        from .api import normalize_server_url
        from .storage import session_path

        try:
            server_url = normalize_server_url(server_url) if server_url else ""
        except ValueError:
            pass  # Sign-in reports the bad address; keep the raw value here.
        return session_path(self._profile_dir(), server_url)

    def _servers_path(self):
        """servers.json in the add-on profile, or None outside a real Kodi profile folder."""
        folder = str(self._profile_dir())
        return os.path.join(folder, "servers.json") if os.path.isabs(folder) else None

    def known_servers(self):
        from .storage import SessionStore as Store

        path = self._servers_path()
        servers = Store(path).get("servers", []) if path else []
        return [str(url) for url in servers if isinstance(url, str) and url]

    def remember_server(self, server_url, remove=False):
        from .storage import SessionStore as Store

        path = self._servers_path()
        if not path:
            return
        store = Store(path)
        servers = [url for url in store.get("servers", []) if url != server_url]
        if not remove:
            servers.insert(0, server_url)
        store.set("servers", servers[:10])

    def switch_server(self):
        """Pick a remembered server or add one; return True when the server changed."""
        current = self.client.server_url if self.client is not None else ""
        servers = [url for url in self.known_servers() if url != current]
        labels = [urlsplit(url).hostname or url for url in servers] + ["Add a server…"]
        selected = self.dialog.select("Switch server", labels)
        if not isinstance(selected, int) or selected < 0:
            return False
        if selected < len(servers):
            target = servers[selected]
        else:
            target = (self.dialog.input("Silo server address", "https://", type=xbmcgui.INPUT_ALPHANUM) or "").strip()
            if not target or target == "https://":
                return False
        self.addon.setSetting("server_url", target)
        self.store = None  # The next run opens the session saved for that server.
        return True

    PROFILE_PROMPT_SECONDS = {"1h": 3600, "12h": 12 * 3600}

    def profile_prompt_due(self):
        """Whether "Ask who's watching" wants the picker on this launch."""
        mode = self.options().get("profile_select", "auto")
        if mode == "always":
            return True
        limit = self.PROFILE_PROMPT_SECONDS.get(mode)
        if limit is None:
            return False
        last = self.store.get("last_active")
        return not isinstance(last, (int, float)) or time.time() - last > limit

    def mark_active(self):
        try:
            self.store.set("last_active", int(time.time()))
        except (StorageError, AttributeError):
            pass

    def _run(self):
        server_url = self.addon.getSetting("server_url").strip()
        if self.store is None:
            self.store = SessionStore(self._session_path(server_url))
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
        try:
            self.remember_server(server_url)
        except (StorageError, OSError):
            pass
        if not self.choose_profile(force=self.profile_prompt_due()):
            return
        self.mark_active()
        self._recover_playback()
        self.configure_artwork()
        self.ensure_display_setup()
        self.ensure_frame_rate_setup()
        self.sync_keymap()
        if getattr(self, "deep_link", None):
            action, content_id = self.deep_link
            self.deep_link = None
            if action == "play":
                self.guard(lambda: self.play(self.client.item(content_id)))
            else:
                self.guard(lambda: self.open_item({"content_id": content_id}))
            self.mark_active()
            return

        while not self.monitor.abortRequested():
            window = HomeWindow("SiloHome.xml", self.addon.getAddonInfo("path"),
                                "Default", "1080i")
            window.application = self
            window.doModal()
            action = window.result
            del window
            self.mark_active()
            if action == "settings":
                from .ui_settings import open_settings

                # Silo Settings can hand back an account action or ask for the
                # Kodi add-on settings ("device").
                choice = self.guard(lambda: open_settings(self))
                if choice in ("server", "logout"):
                    action = choice
                elif choice == "device":
                    self.addon.openSettings()
                    if self.addon.getSetting("server_url").strip() != server_url:
                        self.store = None
                        self._run()
                        return
                    continue
                else:
                    continue
            if action == "server":
                if self.switch_server():
                    self._run()
                    return
            elif action == "profile":
                if not self.choose_profile(force=True):
                    return
                self._recover_playback()
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
                                 user_agent="FoundersTheater/" + version if version else "FoundersTheater",
                                 device_id=self.device_id())
        self._server_settings = None
        if getattr(self, "_theme", None) is not None:
            self._theme.close()
        self._theme = None
        self.server_name = urlsplit(self.client.server_url).hostname or "Silo"

    # The default display setting declares SDR and 1080p, so the server converts
    # HDR and 4K titles until the device is described. Ask once, on the first
    # launch that reaches the library.
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

    # Width each artwork slot is drawn at on a 1920-wide skin; scaled to the screen.
    ARTWORK_SLOTS_1080 = {"poster": 400, "logo": 900, "still": 720, "photo": 440}

    def configure_artwork(self):
        """Request artwork sharp enough for this screen.

        The server's list defaults are small. When it advertises per-slot
        widths (GET /images/capabilities), backdrops come at the full screen
        width and the other slots at the size they are drawn; otherwise the
        "large" preset is used.
        """
        try:
            width = int(float(xbmc.getInfoLabel("System.ScreenWidth") or 1920))
        except (AttributeError, TypeError, ValueError):
            width = 1920
        width = max(1280, min(7680, width))
        params = {"image_size": "large"}
        try:
            capabilities = self.client.request("GET", "/images/capabilities") or {}
        except (ApiError, OSError, ValueError):
            capabilities = {}
        slots = capabilities.get("slot_width_params") if capabilities.get("state") == "available" else None
        if isinstance(slots, dict):
            wanted = dict({slot: px * width / 1920.0 for slot, px in self.ARTWORK_SLOTS_1080.items()},
                          backdrop=width)
            params.update({str(slots[slot]): int(round(px)) for slot, px in wanted.items()
                           if isinstance(slots.get(slot), str) and slots[slot]})
        try:
            self.client.configure_artwork(params)
        except AttributeError:
            pass

    def ensure_frame_rate_setup(self):
        """Offer Kodi's frame-rate matching once, when it is off on this device."""
        from . import kodi_settings

        if self.addon.getSetting("framerate_asked") == "true":
            return
        current = kodi_settings.get(kodi_settings.FRAME_RATE)
        if current is None:
            return  # Not offered on this Kodi; ask again on a later launch.
        self.addon.setSetting("framerate_asked", "true")
        if current != 0:
            return
        if self.dialog.yesno("Match frame rate",
                             "Switch the TV to each video's frame rate, so films play without judder?\n\n"
                             "You can change this later in Settings → This device."):
            if kodi_settings.set_value(kodi_settings.FRAME_RATE, 2) and not kodi_settings.get(
                    kodi_settings.FRAME_RATE_PAUSE):
                kodi_settings.set_value(kodi_settings.FRAME_RATE_PAUSE, 20)

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
                                       allow_password=methods is None or "password" in methods, message=message,
                                       allow_signup=self._signup_allowed(methods),
                                       signup=lambda _server: self._create_account())
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

    def _signup_allowed(self, methods):
        if self.client is None or methods is None or "password" not in methods:
            return False
        try:
            from .ui_people import signup_available

            return bool(signup_available(self.client))
        except Exception:
            return False

    def _create_account(self):
        from .ui_people import create_account

        return create_account(self.client, self.dialog)

    def device_sign_in(self, auth):
        from .ui_people import CodeScreen

        screen = CodeScreen(self.addon.getAddonInfo("path"))
        started = [time.monotonic(), 0]

        def show_code(response):
            expires = max(1, int(response.get("expires_in", 600)))
            started[:] = [time.monotonic(), expires]
            screen.open(response.get("verification_uri") or response.get("verification_url")
                        or self.client.server_url + "/activate", response["user_code"], expires)

        def cancelled():
            return self.monitor.abortRequested() or screen.cancelled()

        def wait(seconds):
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if cancelled() or self.monitor.waitForAbort(min(0.2, max(0, deadline - time.monotonic()))):
                    return True
                if started[1]:
                    screen.update((time.monotonic() - started[0]) / started[1])
            return False

        try:
            auth.sign_in_device(show_code, wait, cancelled)
            self.store.set("auth_method", "device")
            return True
        finally:
            screen.close()

    def choose_profile(self, force=False):
        if self.store.get("temporary_session"):
            profile_id = self.store.get("bound_profile_id")
            if not profile_id:
                raise ApiError(401, "This temporary session has no bound profile.")
            self.client.select_profile(profile_id, self.store.get("profile_token"))
            self.profile = {"id": profile_id}
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
        elif len(profiles) == 1:
            selected = 0
        else:
            from .ui_people import pick_profile

            account = self.store.get("login_username", "") if self.store.get("login_username_server") == (
                self.client.server_url if self.client is not None else None) else ""
            selected = pick_profile(self.addon.getAddonInfo("path"), profiles, preselect, account,
                                    application=self)
        if not isinstance(selected, int) or not 0 <= selected < len(profiles):
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
        self.profile = dict(profile)
        self.profile_name = str(profile["name"])
        self.profile_avatar = avatar_image(profile.get("avatar_url"))
        self._libraries = None  # Library access differs per profile.
        self._server_settings = None  # So do server settings and theme access.
        if getattr(self, "_theme", None) is not None:
            self._theme.close()
        self._theme = None
        self._requests_enabled = None
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

    def home_sections(self):
        """Home rows in server order, with only video cards and no empty rows."""
        result = []
        for section in self.client.hubs().get("sections", []):
            items = video_items(section.get("items"))
            if items:
                result.append(dict(section, items=items))
        return result

    LIBRARY_KINDS = {"movie": ("movies", "mixed"), "series": ("series", "mixed")}

    def video_libraries(self, kind):
        if self._libraries is None:
            self._libraries = [library for library in self.client.libraries().get("items", [])
                               if isinstance(library, dict) and library.get("id") is not None]
        return [library for library in self._libraries if library.get("type") in self.LIBRARY_KINDS.get(kind, ())]

    def browse(self, params, cursor=None):
        """One /catalog page: items, the next cursor when there is more, and the total."""
        query = dict(params, limit=self.options()["page_size"])
        if cursor:
            query["cursor"] = cursor
        response = self.client.items(**query)
        page = response.get("page") or {}
        return {"items": video_items(response.get("items")),
                "next_cursor": page.get("next_cursor") if page.get("has_more") else None,
                "total": response.get("total")}

    def episodes(self, season):
        response = self.client.episodes(season["content_id"])
        return [dict(episode, type="episode") for episode in response.get("items", []) if isinstance(episode, dict)]

    def similar(self, item):
        """Related titles; the row is optional, so any failure just hides it."""
        try:
            return video_items(self.client.similar(item["content_id"]).get("items"))
        except (ApiError, OSError, ValueError, KeyError, AttributeError):
            return []

    def play_next(self, item):
        """Play a series' Next Up episode straight from the series page."""
        content_id = item.get("play_content_id")
        if not content_id:
            return None
        return self.play(self.client.item(content_id))

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
        theme = self.theme()
        theme.push(item)
        try:
            window.doModal()
        finally:
            theme.pop()
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

    def choose_subtitle(self, choices, current=None):
        """Pick a subtitle before playback: a playback index, None for Off,
        or False when the dialog is cancelled (keep the current choice)."""
        labels = ["Off"] + [label for _, label in choices]
        indexes = [None] + [index for index, _ in choices]
        preselect = indexes.index(current) if current in indexes else 0
        selected = self.dialog.select("Subtitles", labels, preselect=preselect)
        if not isinstance(selected, int) or not 0 <= selected < len(indexes):
            return False
        return indexes[selected]

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

    def play(self, item, resume=None, file_id=None, subtitle_index=AUTO, start_at=None, auto_next=True):
        """Generic Play follows preferences; explicit Resume/Start keeps its intent.

        Return the chosen file so the detail screen can retain it after refresh.
        Resume positions are source seconds, exactly as the catalog reports them.
        ``subtitle_index`` is a playback subtitle ordinal, None for Off, or AUTO
        to use the remembered choice or the profile's subtitle preferences.
        """
        if not self.is_playable(item):
            return None
        self._after_playback_item = None
        options = dict(self.options(), auto_skip=self.auto_skip(),
                       quality_preference=(self.profile or {}).get("quality_preference") or "auto",
                       buffering_quality_mode=self.server_setting("playback.buffering_quality_mode"))
        if not auto_next:
            # Shuffle and extras pick what plays next themselves.
            options["auto_next"] = False
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
        options = self._track_options(options, selected_item, selected_version, subtitle_index)
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
        if start_at is not None:
            start_position = start_at
        controller = PlaybackController(self.client, self.addon)
        controller.next_lookup, controller.image = self.next_episode, self.image
        controller.open_screen = self.open_screen
        # A subtitle picked in the player menu is remembered like one picked on the details screen.
        controller.remember_subtitle = lambda choice: self.remember_subtitle_language(
            selected_item, selected_version, choice)
        controller.remember_audio = lambda choice: self.remember_audio_language(selected_item, selected_version, choice)
        controller.subtitle_ai_enabled = self.subtitle_ai_enabled
        theme = getattr(self, "_theme", None)
        if theme is not None:
            theme.suspend()
        try:
            controller.play(selected_item, start_position=start_position, file_id=file_id, options=options,
                            exact_start=start_at is not None)
        finally:
            self._cache_playback_item(selected_item, file_id, getattr(controller, "last_result", None))
            if theme is not None:
                theme.resume()
        try:
            from .widgets import clear_cache

            clear_cache(self.addon)  # Widgets show the new resume point and watched state.
        except Exception:
            pass
        self.last_playback_result = getattr(controller, "last_result", None)
        action = getattr(controller, "next_action", None)
        if isinstance(action, dict) and action.get("stop") == "version":
            # The player menu switched files: continue on the new one where playback was.
            self.play(selected_item, resume=True, file_id=action["file_id"], subtitle_index=subtitle_index,
                      start_at=action.get("position"), auto_next=auto_next)
        elif isinstance(action, dict) and action.get("stop") == "next" and action.get("item") and auto_next:
            # Up next: open the episode fresh so its files, markers and progress are current.
            self.play(self.client.item(action["item"]["content_id"]), resume=True)
        return file_id

    # ---------------------------------------------------------- tracks

    def _track_scope(self):
        server = self.client.server_url if self.client is not None else ""
        return "{}|{}".format(server, (self.profile or {}).get("id") or self.store.get("profile_id") or "")

    def _track_choices(self):
        choices = self.store.get("track_choices") or {}
        return choices if isinstance(choices, dict) else {}

    def _remembered(self, item, key=None):
        return (self._track_choices().get(self._track_scope()) or {}).get(key or track_prefs.choice_key(item))

    def _store_choice(self, item, choice, key=None):
        choices = self._track_choices()
        scope = dict(choices.get(self._track_scope()) or {})
        key = key or track_prefs.choice_key(item)
        if choice is None:
            scope.pop(key, None)
        else:
            scope.pop(key, None)
            scope[key] = choice
            while len(scope) > 500:  # Oldest choices go first.
                scope.pop(next(iter(scope)))
        choices[self._track_scope()] = scope
        try:
            self.store.set("track_choices", choices)
        except (StorageError, AttributeError):
            pass

    def _save_server_choice(self, kind, item, body):
        """Save (body) or forget (None) a choice on the server; False when it cannot.

        Silo applies the saved choice on every device and reports it back in
        the item's effective_* fields. The local store is only the fallback.
        """
        key = track_prefs.pref_key(item)
        client = self.client
        if not key or client is None or not hasattr(client, "save_track_pref"):
            return False
        try:
            if body is None:
                client.forget_track_pref(kind, key)
            else:
                client.save_track_pref(kind, key, body)
        except ApiError:
            return False
        return True

    def audio_default(self, item, version):
        """(audio ordinal, its language): what the server will play, else the local rules."""
        tracks = (version or {}).get("audio_tracks") or []
        effective = (version or {}).get("effective_audio_track_index")
        if (isinstance(effective, int) and not isinstance(effective, bool) and 0 <= effective < len(tracks)
                and isinstance(tracks[effective], dict)):
            return effective, track_prefs.language(tracks[effective].get("language"))
        ordinal = track_prefs.match_audio(version, self._remembered(item, track_prefs.audio_choice_key(item)))
        if ordinal is None:
            return track_prefs.audio_default(version, self.profile)
        return ordinal, track_prefs.language(version["audio_tracks"][ordinal].get("language"))

    def remember_audio(self, item, version, ordinal):
        """Keep an audio pick for this show (or movie) on the server; the profile's own pick resets it."""
        automatic, _language = track_prefs.audio_default(version, self.profile)
        body = None if ordinal == automatic else track_prefs.audio_pref_body(version, ordinal)
        if ordinal != automatic and body is None:
            return
        key = track_prefs.audio_choice_key(item)
        if self._save_server_choice("audio", item, body):
            self._store_choice(item, None, key)
            return
        track = ((version or {}).get("audio_tracks") or [])[ordinal] if body is not None else None
        self._store_choice(item, None if body is None else {
            "language": track_prefs.language(track.get("language")),
            "commentary": track_prefs.is_commentary(track)}, key)

    def remember_audio_language(self, item, version, choice):
        """An audio pick from the player menu, where Kodi reports the stream's language."""
        if not isinstance(choice, dict):
            return
        language = track_prefs.language(choice.get("language"))
        if len(language) != 2:  # Only a real language code can find the track again.
            return
        commentary = track_prefs.is_commentary({"title": choice.get("title")})
        ordinal = track_prefs.match_audio(version, {"language": language, "commentary": commentary})
        if ordinal is not None:
            self.remember_audio(item, version, ordinal)

    def _profile_subtitle(self, item, version):
        """The profile's automatic subtitle, ignoring any choice saved for the series."""
        _audio, audio_language = self.audio_default(item, version)
        automatic = track_prefs.auto_subtitle(version, self.profile, audio_language)
        return None if automatic in (None, track_prefs.OFF) else automatic

    def subtitle_default(self, item, version):
        """(ordinal or None for Off, "remembered" | "auto"): what plays unless the viewer picks."""
        _audio, audio_language = self.audio_default(item, version)
        server = track_prefs.server_subtitle_prefs(item)
        if server is not None:
            value = track_prefs.match_subtitle_signature(version, item.get("effective_subtitle_track_signature"))
            source = "remembered"
            if value is None or server["subtitle_mode"] == "off":
                value = track_prefs.auto_subtitle(version, server, audio_language)
                # Shown as (Auto) unless a series choice changed the profile's mode or language.
                profile = self.profile or {}
                source = "auto" if (
                    str(server["subtitle_mode"]).lower() == str(profile.get("subtitle_mode") or "auto").lower()
                    and track_prefs.language(server["subtitle_language"])
                    == track_prefs.language(profile.get("subtitle_language"))) else "remembered"
        else:
            value = track_prefs.match_choice(version, self._remembered(item))
            source = "remembered"
            if value is None:
                value, source = track_prefs.auto_subtitle(version, self.profile, audio_language), "auto"
        return (None if value in (None, track_prefs.OFF) else value), source

    def remember_subtitle(self, item, version, ordinal):
        """Keep a subtitle pick for this show (or movie); the profile's own pick resets it to automatic."""
        if ordinal == self._profile_subtitle(item, version):
            body = None
        else:
            body = track_prefs.subtitle_pref_body(version, ordinal)
            if body is None:
                return
        if self._save_server_choice("subtitle", item, body):
            self._store_choice(item, None)
            return
        self._store_choice(item, None if body is None else track_prefs.remembered_choice(
            track_prefs.track_at(version, ordinal) if ordinal is not None else None))

    def remember_subtitle_language(self, item, version, choice):
        """A pick from the player menu, where Kodi only reports the stream language."""
        if not isinstance(choice, dict):
            return
        if choice.get("off"):
            self.remember_subtitle(item, version, None)
            return
        language = track_prefs.language(choice.get("language"))
        if not language:
            return
        ordinal = track_prefs.match_choice(version, {"kind": "track", "language": language, "forced": False,
                                                     "hi": False, "external": False})
        if ordinal is not None and ordinal != track_prefs.OFF:
            self.remember_subtitle(item, version, ordinal)
        else:
            self._store_choice(item, {"kind": "track", "language": language, "forced": False, "hi": False,
                                      "external": False})

    def _track_options(self, options, item, version, subtitle_index):
        """Audio (remembered, else the profile's language) and the subtitle to request (see track_prefs)."""
        options = dict(options)
        audio, _language = self.audio_default(item, version)
        if audio is not None:
            options["audio_track_index"] = audio
        if subtitle_index is AUTO:
            subtitle_index, _source = self.subtitle_default(item, version)
        if isinstance(subtitle_index, int) and not isinstance(subtitle_index, bool) and subtitle_index >= 0:
            options["subtitle_track_index"] = subtitle_index
        else:
            options["subtitles_off"] = True
        return options

    def next_episode(self, item):
        """The episode after ``item`` in its series, crossing into the next season."""
        series_id = item.get("series_id")
        try:
            season, number = int(item.get("season_number")), int(item.get("episode_number"))
        except (TypeError, ValueError):
            return None
        if not series_id:
            return None
        seasons = [entry for entry in present.ordered_seasons(self.client.seasons(series_id).get("items", []))
                   if entry.get("season_number") not in (0, "0")]
        for index, entry in enumerate(seasons):
            try:
                entry_number = int(entry.get("season_number"))
            except (TypeError, ValueError):
                continue
            if entry_number < season:
                continue
            episodes = sorted(self.episodes(entry), key=lambda episode: int(episode.get("episode_number") or 0))
            for episode in episodes:
                if entry_number > season or int(episode.get("episode_number") or 0) > number:
                    return dict(episode, series_id=series_id, series_title=item.get("series_title"),
                                season_number=episode.get("season_number", entry_number))
        return None

    def sync_keymap(self):
        """Add or remove the Menu-key keymap to match the device setting."""
        from .keymap import sync

        folder = xbmcvfs.translatePath("special://userdata/keymaps/")
        if not os.path.isabs(str(folder)):
            return  # Not resolved by a real Kodi.
        try:
            if sync(folder, self.options().get("player_menu_key", True), self.addon.getAddonInfo("path")):
                xbmc.executebuiltin("Action(reloadkeymaps)")
        except OSError:
            xbmc.log("[Silo] Could not update the player menu keymap", xbmc.LOGWARNING)

    @staticmethod
    def is_playable(item):
        return item.get("type") in ("movie", "episode", "extra")

    @staticmethod
    def resume_seconds(item):
        return resume_position(item)

    def update_profile(self, changes):
        """Save the active profile's own playback preferences on the server."""
        profile_id = (getattr(self, "profile", None) or {}).get("id")
        if profile_id is None:
            raise ApiError(403, "Settings are unavailable for this sign-in.")
        updated = self.client.update_profile(profile_id, changes)
        self.profile = dict(self.profile, **(updated if isinstance(updated, dict) else changes))
        return self.profile

    # Screens open each other through this table, so no window imports another
    # window's module. Each entry is (module in this package, function); the
    # function is called as function(application, **kwargs).
    SCREENS = {
        "for_you": ("ui_browse", "open_for_you"),
        "calendar": ("ui_browse", "open_calendar"),
        "collections": ("ui_browse", "open_collections"),
        "collection": ("ui_browse", "open_collection"),
        "library_sections": ("ui_browse", "open_library_sections"),
        "personal": ("ui_browse", "open_personal"),
        "person": ("ui_browse", "open_person"),
        "requests": ("ui_requests", "open_requests"),
        "manage_profiles": ("ui_people", "manage_profiles"),
        "subtitle_search": ("ui_subtitles", "search_subtitles"),
        "subtitle_translate": ("ui_subtitles", "translate_subtitles"),
        "subtitle_sync": ("ui_subtitles", "sync_subtitles"),
    }

    def open_screen(self, name, **kwargs):
        """Open another screen by name; returns whatever that screen returns."""
        import importlib

        module_name, function_name = self.SCREENS[name]
        module = importlib.import_module("." + module_name, __package__)
        return getattr(module, function_name)(self, **kwargs)

    def subtitle_ai_enabled(self):
        """Whether the server offers AI subtitle translation (asked once per session)."""
        if getattr(self, "_subtitle_ai", None) is None:
            try:
                from .svc_subtitles import ai_status

                self._subtitle_ai = ai_status(self.client)["enabled"]
            except Exception:
                self._subtitle_ai = False
        return self._subtitle_ai

    def requests_enabled(self):
        """Whether this server offers media requests (cached per profile)."""
        if getattr(self, "_requests_enabled", None) is None:
            try:
                from .svc_requests import enabled

                self._requests_enabled = bool(enabled(self.client))
            except Exception:
                self._requests_enabled = False
        return self._requests_enabled

    def auto_skip(self):
        """The profile's auto-skip switches, as the player reads them."""
        profile = getattr(self, "profile", None) or {}
        return {kind: profile.get("auto_skip_" + kind) is True for kind in ("intro", "credits", "recap")}

    def image(self, item, kind):
        return self.client.image_url(item, kind) if isinstance(item, dict) else ""

    def artwork(self, item):
        result = {}
        for key, kind in (("poster", "poster"), ("fanart", "backdrop")):
            url = self.client.image_url(item, kind)
            if url:
                result[key] = url
        return result
