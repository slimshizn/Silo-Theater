"""Theme music on detail pages, like the Silo apps.

Opt-in through the server setting ``ui.theme_music_enabled`` (with
``ui.theme_music_loop``) and only when the server reports the theme capability.
A page's theme starts when its details open, continues onto a page with the
same theme set, comes back when an inner page closes, stops when the last page
closes and pauses while a video plays. It never starts over other audio.

Theme grants are short-lived signed URLs; Kodi plays them through the loopback
relay (relay.py), so the URL never reaches Kodi's log. The sign-in is only
attached when the theme comes from the Silo server itself.
"""

import threading
import time
from urllib.parse import quote, urljoin, urlsplit

from .api import ApiError

ENABLED, LOOP = "ui.theme_music_enabled", "ui.theme_music_loop"
# What Kodi's audio player decodes; anything else the server converts to AAC.
ACCEPTED_FORMATS = [{"container": container, "audio_codec": codec} for container, codec in (
    ("mp3", "mp3"), ("mp4", "aac"), ("m4a", "aac"), ("m4a", "alac"), ("aac", "aac"), ("flac", "flac"),
    ("ogg", "vorbis"), ("ogg", "opus"), ("opus", "opus"), ("wav", "pcm"))]
WATCH_SECONDS = 1.0
# Kodi's Player.stop() is asynchronous; wait this long for our theme to end.
STOP_WAIT_SECONDS = 2.0


def _origin(url):
    parts = urlsplit(url)
    return parts.scheme.lower(), parts.hostname, parts.port or (443 if parts.scheme == "https" else 80)


def _quiet(method):
    """Theme music is a nicety: it must never raise into navigation or playback."""
    def wrapper(self, *args):
        try:
            return method(self, *args)
        except Exception:  # Kodi/player errors included; never log details (URLs).
            self.failures += 1
            return None
    wrapper.__name__ = method.__name__
    wrapper.__doc__ = method.__doc__
    return wrapper


def theme_set(item):
    """(owner content id, first theme) for an item's details, or None."""
    themes = (item or {}).get("themes")
    if not isinstance(themes, dict):
        return None
    items = [entry for entry in themes.get("items") or [] if isinstance(entry, dict) and entry.get("id")]
    owner = themes.get("owner_id") or (item or {}).get("content_id")
    return (str(owner), items[0]) if items and owner else None


class ThemeMusic:
    """One theme player for the whole add-on, driven by the detail-page stack."""

    def __init__(self, client, setting, player=None, relay_factory=None, monitor=None):
        self.client = client
        self.setting = setting  # setting(key) -> value of a server setting
        self._player = player
        self.relay_factory = relay_factory
        self.monitor = monitor
        self.stack = []
        self.playing = None  # (owner, theme id) currently started by us
        self.suspended = False
        self.capability = None
        self.relay = None
        self.loop = False
        self.failures = 0
        self._lock = threading.Lock()
        self._watcher = None
        self._stop_watch = threading.Event()

    # -------------------------------------------------------------- pages
    @_quiet
    def push(self, item):
        self.stack.append(item)
        self._sync()

    @_quiet
    def pop(self):
        if self.stack:
            self.stack.pop()
        self._sync()

    @_quiet
    def suspend(self):
        """Playback is starting: stop the theme, and wait until Kodi has, so its
        stop event cannot reach the video player that starts next."""
        self.suspended = True
        self._stop()

    @_quiet
    def resume(self):
        self.suspended = False
        self._sync()

    @_quiet
    def close(self):
        self.stack = []
        self._stop()
        with self._lock:
            relay, self.relay = self.relay, None
        if relay is not None:
            relay.close()

    # ------------------------------------------------------------ helpers
    def _enabled(self):
        if self.setting(ENABLED) is not True:
            return False
        if self.capability is None:
            try:
                answer = self.client.request("GET", "/catalog/themes/capabilities")
            except (ApiError, OSError):
                return False
            self.capability = isinstance(answer, dict) and answer.get("state") == "available" \
                and answer.get("allowed") is True
        return self.capability

    def player(self):
        if self._player is None:
            import xbmc
            self._player = xbmc.Player()
        return self._player

    def _sync(self):
        """Make what plays match the top page."""
        wanted = theme_set(self.stack[-1]) if self.stack and not self.suspended else None
        if wanted is None or not self._enabled():
            self._stop()
            return
        owner, theme = wanted
        if self.playing == (owner, theme["id"]) and self._ours_playing():
            return
        self._stop()  # Waits for our previous theme to end, so the check below sees other audio only.
        player = self.player()
        if player.isPlaying():  # Never start over other audio or video.
            return
        self._start(owner, theme)

    def _start(self, owner, theme):
        url = self._grant(owner, theme)
        if not url:
            return
        import xbmcgui
        listitem = xbmcgui.ListItem(label=str(theme.get("title") or "Theme"), path=url)
        listitem.setProperty("IsPlayable", "true")
        try:
            listitem.getMusicInfoTag().setTitle(str(theme.get("title") or "Theme"))
        except AttributeError:
            pass
        with self._lock:
            self.playing = (owner, theme["id"])
        self.loop = self.setting(LOOP) is True  # Read here, never from the watcher thread.
        self.player().play(url, listitem)
        self._watch(owner, theme)

    def _grant(self, owner, theme):
        path = "/catalog/items/{}/themes/{}/playback".format(quote(owner, safe=""), quote(str(theme["id"]), safe=""))
        try:
            grant = self.client.request("POST", path, {"accepted_formats": ACCEPTED_FORMATS})
        except (ApiError, OSError):
            return None
        source = grant.get("url") if isinstance(grant, dict) else None
        if not isinstance(source, str) or not source:
            return None
        url = urljoin(self.client.server_url.rstrip("/") + "/", source)
        with self._lock:
            if self.relay is None:
                factory = self.relay_factory
                if factory is None:
                    from .relay import MediaRelay as factory
                self.relay = factory(self.client)
            relay = self.relay
        return relay.register(url, authenticated=_origin(url) == _origin(self.client.server_url))

    def _ours_playing(self):
        try:
            return self.player().isPlayingAudio()
        except RuntimeError:
            return False

    def _stop(self):
        self._stop_watch.set()
        with self._lock:
            was_playing, self.playing = self.playing, None
        if was_playing and self._ours_playing():
            try:
                self.player().stop()
            except RuntimeError:
                return
            deadline = time.monotonic() + STOP_WAIT_SECONDS
            while self._ours_playing() and time.monotonic() < deadline:
                if self.monitor is not None:
                    if self.monitor.waitForAbort(0.05):
                        return
                else:
                    time.sleep(0.05)

    def _watch(self, owner, theme):
        """Loop the theme while its page stays open: a fresh grant each time."""
        self._stop_watch = stop = threading.Event()

        def run():
            started = False
            while not stop.wait(WATCH_SECONDS):
                if self.monitor is not None and self.monitor.abortRequested():
                    return
                playing = self._ours_playing()
                if playing:
                    started = True
                    continue
                if not started:
                    continue
                with self._lock:
                    current = self.playing
                if current != (owner, theme["id"]) or stop.is_set():
                    return
                if not self.loop:
                    with self._lock:
                        self.playing = None
                    return
                try:
                    url = self._grant(owner, theme)
                    import xbmcgui
                    with self._lock:  # suspend() between the check and play() must win.
                        if not url or stop.is_set() or self.playing != (owner, theme["id"]):
                            return
                        self.player().play(url, xbmcgui.ListItem(label=str(theme.get("title") or "Theme"),
                                                                 path=url))
                except Exception:
                    self.failures += 1
                    return
                started = False

        self._watcher = threading.Thread(target=run, name="silo-theme-music", daemon=True)
        self._watcher.start()
