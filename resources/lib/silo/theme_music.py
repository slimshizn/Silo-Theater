"""Theme music on detail pages, like the Silo apps.

Opt-in through the server setting ``ui.theme_music_enabled`` (with
``ui.theme_music_loop``) and only when the server reports the theme capability.
A page's theme starts when its details open, continues onto a page with the
same theme set, comes back when an inner page closes, stops when the last page
closes and pauses while a video plays. It never starts over other audio.

Theme grants are short-lived signed URLs; Kodi plays them through the loopback
relay (relay.py), so the URL never reaches Kodi's log. The sign-in is only
attached when the theme comes from the Silo server itself.

Optionally (device setting "Online TV themes", off by default), a show the
server has no theme for plays its theme from Plex's public TV theme library
by TVDB id. A show found missing there is asked about again after a week.
"""

import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import Request, build_opener

from .api import ApiError

ENABLED, LOOP = "ui.theme_music_enabled", "ui.theme_music_loop"
# What Kodi's audio player decodes; anything else the server converts to AAC.
ACCEPTED_FORMATS = [{"container": container, "audio_codec": codec} for container, codec in (
    ("mp3", "mp3"), ("mp4", "aac"), ("m4a", "aac"), ("m4a", "alac"), ("aac", "aac"), ("flac", "flac"),
    ("ogg", "vorbis"), ("ogg", "opus"), ("opus", "opus"), ("wav", "pcm"))]
WATCH_SECONDS = 1.0
# Kodi's Player.stop() is asynchronous; wait this long for our theme to end.
STOP_WAIT_SECONDS = 2.0
ONLINE_THEME_URL = "https://tvthemes.plexapp.com/{}.mp3"
ONLINE_RECHECK_SECONDS = 7 * 24 * 3600
ONLINE_CHECK_TIMEOUT = 5
MAX_REMEMBERED_MISSES = 5000
SERIES_TVDB = re.compile(r"^series-tvdb-(\d+)$")


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


def _digits(value):
    text = str(value or "").strip()
    return text if text.isdigit() else None


def _online_theme_exists(url):
    """True/False when Plex answers whether the theme exists; None when it cannot be reached."""
    try:
        with build_opener().open(Request(url, method="HEAD", headers={"User-Agent": "FoundersTheater"}),
                                 timeout=ONLINE_CHECK_TIMEOUT) as response:
            return 200 <= response.status < 300
    except HTTPError as error:
        return False if error.code in (403, 404, 410) else None
    except (URLError, OSError, ValueError):
        return None


class ThemeMusic:
    """One theme player for the whole add-on, driven by the detail-page stack."""

    def __init__(self, client, setting, player=None, relay_factory=None, monitor=None, online=None,
                 misses_path=None, check_online=None, clock=time.time):
        self.client = client
        self.setting = setting  # setting(key) -> value of a server setting
        self.online = online or (lambda: False)  # Device setting: online TV themes
        self.misses_path = misses_path
        self.check_online = check_online or _online_theme_exists
        self.clock = clock
        self._player = player
        self.relay_factory = relay_factory
        self.monitor = monitor
        self.stack = []
        self.playing = None  # Key of the theme currently started by us
        self.suspended = False
        self.capability = None
        self.relay = None
        self.loop = False
        self.failures = 0
        self._series_tvdb = {}
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
    def _server_themes(self):
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

    def _wanted(self, item):
        """(key, title, make_url) for a page's theme: the server's, else an online TV theme."""
        if self.setting(ENABLED) is not True:
            return None
        found = theme_set(item)
        if found is not None and self._server_themes():
            owner, theme = found
            return (owner, theme["id"]), str(theme.get("title") or "Theme"), lambda: self._grant(owner, theme)
        if not self.online():
            return None
        tvdb = self._tvdb_for(item)
        if not tvdb or not self._online_available(tvdb):
            return None
        url = ONLINE_THEME_URL.format(tvdb)
        return ("tvdb", tvdb), "Theme", lambda: url

    def _sync(self):
        """Make what plays match the top page."""
        wanted = self._wanted(self.stack[-1]) if self.stack and not self.suspended else None
        if wanted is None:
            self._stop()
            return
        key, title, make_url = wanted
        if self.playing == key and self._ours_playing():
            return
        self._stop()  # Waits for our previous theme to end, so the check below sees other audio only.
        player = self.player()
        if player.isPlaying():  # Never start over other audio or video.
            return
        self._start(key, title, make_url)

    def _start(self, key, title, make_url):
        url = make_url()
        if not url:
            return
        import xbmcgui
        listitem = xbmcgui.ListItem(label=title, path=url)
        listitem.setProperty("IsPlayable", "true")
        try:
            listitem.getMusicInfoTag().setTitle(title)
        except AttributeError:
            pass
        with self._lock:
            self.playing = key
        self.loop = self.setting(LOOP) is True  # Read here, never from the watcher thread.
        self.player().play(url, listitem)
        self._watch(key, title, make_url)

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

    # ---------------------------------------------------- online TV themes
    def _tvdb_for(self, item):
        """The show's TVDB id for a series, season or episode page; None for anything else."""
        kind = item.get("type")
        if kind == "series":
            return _digits(item.get("tvdb_id"))
        if kind not in ("season", "episode"):
            return None
        series_id = str(item.get("series_id") or "")
        match = SERIES_TVDB.match(series_id)
        if match:
            return match.group(1)
        if not series_id:
            return None
        if series_id not in self._series_tvdb:
            try:
                series = self.client.request("GET", "/catalog/items/{}".format(quote(series_id, safe="")))
            except (ApiError, OSError):
                return None
            self._series_tvdb[series_id] = _digits(series.get("tvdb_id")) if isinstance(series, dict) else None
        return self._series_tvdb[series_id]

    def _misses(self):
        if not self.misses_path:
            return {}
        try:
            with open(self.misses_path) as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_misses(self, misses):
        if not self.misses_path:
            return
        if len(misses) > MAX_REMEMBERED_MISSES:  # Oldest checks go first.
            misses = dict(sorted(misses.items(), key=lambda entry: entry[1])[-MAX_REMEMBERED_MISSES:])
        try:
            temporary = self.misses_path + ".tmp"
            with open(temporary, "w") as handle:
                json.dump(misses, handle)
            os.replace(temporary, self.misses_path)
        except OSError:
            pass

    def _online_available(self, tvdb):
        """Whether Plex has this show's theme; a miss is asked about again after a week."""
        misses = self._misses()
        checked = misses.get(tvdb)
        now = self.clock()
        if isinstance(checked, (int, float)) and now - checked < ONLINE_RECHECK_SECONDS:
            return False
        exists = self.check_online(ONLINE_THEME_URL.format(tvdb))
        if exists is None:  # Could not tell (offline): try again next time.
            return False
        if exists:
            if tvdb in misses:
                del misses[tvdb]
                self._save_misses(misses)
            return True
        misses[tvdb] = now
        self._save_misses(misses)
        return False

    # -------------------------------------------------------------- state
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

    def _watch(self, key, title, make_url):
        """Loop the theme while its page stays open: a fresh URL (grant) each time."""
        self._stop_watch = stop = threading.Event()

        def run():
            started = False
            while not stop.wait(WATCH_SECONDS):
                if self.monitor is not None and self.monitor.abortRequested():
                    return
                if self._ours_playing():
                    started = True
                    continue
                if not started:
                    continue
                with self._lock:
                    current = self.playing
                if current != key or stop.is_set():
                    return
                if not self.loop:
                    with self._lock:
                        self.playing = None
                    return
                try:
                    url = make_url()
                    import xbmcgui
                    with self._lock:  # suspend() between the check and play() must win.
                        if not url or stop.is_set() or self.playing != key:
                            return
                        self.player().play(url, xbmcgui.ListItem(label=title, path=url))
                except Exception:
                    self.failures += 1
                    return
                started = False

        self._watcher = threading.Thread(target=run, name="silo-theme-music", daemon=True)
        self._watcher.start()
