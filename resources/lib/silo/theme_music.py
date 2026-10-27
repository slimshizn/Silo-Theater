"""Theme music on detail pages, like the Silo apps.

Opt-in through the server setting ``ui.theme_music_enabled`` (with
``ui.theme_music_loop``) and only when the server reports the theme capability.
A page's theme starts when its details open, continues onto a page with the
same theme set, comes back when an inner page closes, stops when the last page
closes and pauses while a video plays. It never starts over other audio.

Theme grants are short-lived signed URLs; they are fetched through the loopback
relay (relay.py), so the URL never reaches Kodi's log. The sign-in is only
attached when the theme comes from the Silo server itself.

Optionally (device setting "Online TV themes", off by default), a show the
server has no theme for plays its theme from Plex's public TV theme library
by TVDB id. A show found missing there is asked about again after a week.

To start without Kodi's buffering spinner, a theme is downloaded in the
background into a small cache and played from the local file. It fades in from
a low volume and fades out before a video starts or the page closes. Kodi has
no per-stream volume, so the fade moves Kodi's own volume and always puts it
back to where the viewer had it.
"""

import hashlib
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

# Downloaded themes: one file per theme, refreshed after a month, 100 MB in all.
CACHE_LIMIT_BYTES = 100 * 1024 * 1024
CACHE_MAX_AGE_SECONDS = 30 * 24 * 3600
THEME_LIMIT_BYTES = 25 * 1024 * 1024
DOWNLOAD_TIMEOUT = 20

FADE_IN_SECONDS, FADE_OUT_SECONDS, FADE_STEP_SECONDS = 2.0, 1.0, 0.1
FADE_FLOOR = 0.15  # Start and end at this share of the viewer's volume.


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


def _fetch(url, path, limit=THEME_LIMIT_BYTES):
    """Download ``url`` to ``path`` (atomically); False on any failure or an oversized file."""
    temporary = path + ".part"
    try:
        with build_opener().open(Request(url, headers={"User-Agent": "FoundersTheater"}),
                                 timeout=DOWNLOAD_TIMEOUT) as response, open(temporary, "wb") as handle:
            total = 0
            while True:
                chunk = response.read(64 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError("theme too large")
                handle.write(chunk)
        if total == 0:
            raise ValueError("empty theme")
        os.replace(temporary, path)
        return True
    except (HTTPError, URLError, OSError, ValueError):
        try:
            os.unlink(temporary)
        except OSError:
            pass
        return False


class KodiVolume:
    """Kodi's application volume through JSON-RPC: the only volume an add-on can move."""

    def get(self):
        """(volume 0-100, muted) or None when Kodi does not answer."""
        import xbmc
        try:
            answer = json.loads(xbmc.executeJSONRPC(json.dumps({
                "jsonrpc": "2.0", "id": 1, "method": "Application.GetProperties",
                "params": {"properties": ["volume", "muted"]}})))
            result = answer["result"]
            return int(result["volume"]), bool(result["muted"])
        except (ValueError, KeyError, TypeError):
            return None

    def set(self, volume):
        import xbmc
        xbmc.executeJSONRPC(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "Application.SetVolume",
                                        "params": {"volume": max(0, min(100, int(round(volume))))}}))


def _spawn(function):
    thread = threading.Thread(target=function, name="silo-theme-music", daemon=True)
    thread.start()
    return thread


class ThemeMusic:
    """One theme player for the whole add-on, driven by the detail-page stack."""

    def __init__(self, client, setting, player=None, relay_factory=None, monitor=None, online=None,
                 misses_path=None, check_online=None, clock=time.time, cache_dir=None, fetch=None,
                 volume=None, sleep=None, spawn=None):
        self.client = client
        self.setting = setting  # setting(key) -> value of a server setting
        self.online = online or (lambda: False)  # Device setting: online TV themes
        self.misses_path = misses_path
        self.check_online = check_online or _online_theme_exists
        self.clock = clock
        self.cache_dir = cache_dir
        self.fetch = fetch or _fetch
        self.volume = volume if volume is not None else KodiVolume()
        self.sleep = sleep or self._sleep
        self.spawn = spawn or _spawn
        self._player = player
        self.relay_factory = relay_factory
        self.monitor = monitor
        self.stack = []
        self.playing = None  # Key of the theme currently started by us
        self.url = None  # The exact file Kodi was given for it: how we recognize our own theme
        self.yielded = False  # Something else plays (a trailer): stay quiet until the next page
        self.suspended = False
        self.capability = None
        self.relay = None
        self.loop = False
        self.failures = 0
        self._series_tvdb = {}
        self._lock = threading.Lock()
        self._generation = 0  # Bumped by every stop: running fades and starts give up.
        self._starting = None  # Key of a theme being downloaded or started in the background
        self._base_volume = None  # The viewer's volume while a fade-in has Kodi's volume lowered
        self._fade_out = None  # A background fade-out still running
        self._watcher = None
        self._stop_watch = threading.Event()

    # -------------------------------------------------------------- pages
    @_quiet
    def push(self, item):
        self.stack.append(item)
        self.yielded = False
        self._sync()

    @_quiet
    def pop(self):
        if self.stack:
            self.stack.pop()
        self.yielded = False
        self._sync()

    @_quiet
    def yield_to(self):
        """Other playback starts outside Silo's player (a YouTube trailer): stop the theme
        and keep quiet until the viewer moves to another page."""
        self.yielded = True
        self._stop(wait=True)

    @_quiet
    def suspend(self):
        """Playback is starting: fade the theme out and stop it, waiting until Kodi has
        and the viewer's volume is back, so the video starts at its normal volume."""
        self.suspended = True
        self._stop(wait=True)

    @_quiet
    def resume(self):
        self.suspended = False
        self._sync()

    @_quiet
    def close(self):
        self.stack = []
        self._stop(wait=True)
        with self._lock:
            relay, self.relay = self.relay, None
        if relay is not None:
            relay.close()

    # ------------------------------------------------------------ helpers
    def _sleep(self, seconds):
        """True when Kodi is shutting down."""
        if self.monitor is not None:
            return self.monitor.waitForAbort(seconds)
        time.sleep(seconds)
        return False

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
        wanted = self._wanted(self.stack[-1]) if self.stack and not (self.suspended or self.yielded) else None
        if wanted is None:
            self._stop(wait=False)
            return
        key, title, make_url = wanted
        if self.playing == key and self._ours_playing():
            return
        if self._starting == key:
            return
        self._stop(wait=False)  # Fades the previous theme out in the background.
        with self._lock:
            self._starting = key
            generation = self._generation
        self.spawn(lambda: self._start(key, title, make_url, generation))

    def _start(self, key, title, make_url, generation):
        """Background: download (or reuse) the theme, then start it quietly and fade in."""
        try:
            fade_out = self._fade_out
            if fade_out is not None and fade_out is not threading.current_thread():
                fade_out.join(FADE_OUT_SECONDS + STOP_WAIT_SECONDS + 1)
            path = self._cached(key)
            if path is None:
                url = make_url()
                path = self._download(key, url) if url else None
            with self._lock:
                if generation != self._generation or path is None:
                    return
            if self._other_playing():  # Never start over other audio or video.
                return
            with self._lock:
                if generation != self._generation:
                    return
                self.playing, self.url = key, path
            self.loop = self.setting(LOOP) is True
            self._play_with_fade_in(path, title, generation)
            self._watch(key, title, path)
        except Exception:
            self.failures += 1
        finally:
            with self._lock:
                if self._starting == key and generation == self._generation:
                    self._starting = None

    def _play_with_fade_in(self, path, title, generation):
        import xbmcgui
        listitem = xbmcgui.ListItem(label=title, path=path)
        listitem.setProperty("IsPlayable", "true")
        try:
            listitem.getMusicInfoTag().setTitle(title)
        except AttributeError:
            pass
        reading = self.volume.get()
        if reading is None or reading[1] or reading[0] <= 0:
            # Muted or unknown: just play, at the volume the viewer has.
            self.player().play(path, listitem, True)  # Windowed: never Kodi's fullscreen music player.
            return
        base = reading[0]
        with self._lock:
            self._base_volume = base
        self.volume.set(max(1, base * FADE_FLOOR))
        self.player().play(path, listitem, True)
        steps = int(FADE_IN_SECONDS / FADE_STEP_SECONDS)
        for step in range(1, steps + 1):
            if self.sleep(FADE_STEP_SECONDS):
                break
            with self._lock:
                if generation != self._generation:
                    return  # The stop that cancelled us puts the viewer's volume back.
                self.volume.set(base * (FADE_FLOOR + (1 - FADE_FLOOR) * step / steps))
        with self._lock:
            if generation == self._generation:
                self._base_volume = None
                self.volume.set(base)

    # ------------------------------------------------------------- cache
    def _cache_path(self, key):
        if not self.cache_dir:
            return None
        name = hashlib.sha256(json.dumps(list(key)).encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.cache_dir, name + ".theme")

    def _cached(self, key):
        path = self._cache_path(key)
        try:
            if path and self.clock() - os.path.getmtime(path) < CACHE_MAX_AGE_SECONDS and os.path.getsize(path) > 0:
                return path
        except OSError:
            pass
        return None

    def _download(self, key, url):
        """Fetch the theme into the cache and return the local path; the URL itself without a cache."""
        path = self._cache_path(key)
        if path is None:
            return url
        try:
            os.makedirs(self.cache_dir, exist_ok=True)
        except OSError:
            return url
        if not self.fetch(url, path):
            return None
        self._prune(keep=path)
        return path

    def _prune(self, keep=None):
        try:
            entries = [os.path.join(self.cache_dir, name) for name in os.listdir(self.cache_dir)
                       if name.endswith(".theme")]
            entries.sort(key=os.path.getmtime)
            total = sum(os.path.getsize(entry) for entry in entries)
            for oldest in entries:
                if total <= CACHE_LIMIT_BYTES:
                    break
                if oldest in (keep, self.url):
                    continue
                total -= os.path.getsize(oldest)
                os.unlink(oldest)
        except OSError:
            pass

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
        if self._cached(("tvdb", tvdb)):
            return True
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
        """Only the exact file we started counts: never the viewer's own music or video."""
        url = self.url
        if not url:
            return False
        try:
            player = self.player()
            return bool(player.isPlaying()) and player.getPlayingFile() == url
        except RuntimeError:
            return False

    def _other_playing(self):
        try:
            player = self.player()
            return bool(player.isPlaying()) and player.getPlayingFile() != self.url
        except RuntimeError:
            return False

    def _stop(self, wait):
        """Fade our theme out and stop it; the viewer's volume always comes back.

        ``wait`` blocks until it is done (a video is about to start); otherwise
        the fade runs in the background and the next theme waits for it.
        """
        self._stop_watch.set()
        with self._lock:
            self._generation += 1
            self.playing, self._starting = None, None
            base, self._base_volume = self._base_volume, None
        if not self._ours_playing():
            if base is not None:
                self.volume.set(base)
            return
        previous = self._fade_out
        if previous is not None and previous is not threading.current_thread():
            previous.join(FADE_OUT_SECONDS + STOP_WAIT_SECONDS + 1)
        if wait:
            self._fade_and_stop(base)
        else:
            self._fade_out = self.spawn(lambda: self._fade_and_stop(base))

    def _fade_and_stop(self, base):
        """``base`` is the viewer's volume when a fade-in was cut short; else Kodi's current one."""
        reading = self.volume.get()
        faded = reading is not None and not reading[1]
        if base is None and faded:
            base = reading[0]  # The viewer may have changed it while the theme played.
        try:
            if faded and base:
                current = reading[0]
                steps = int(FADE_OUT_SECONDS / FADE_STEP_SECONDS)
                for step in range(1, steps + 1):
                    self.volume.set(current - (current - base * FADE_FLOOR) * step / steps)
                    if self.sleep(FADE_STEP_SECONDS):
                        break
            try:
                self.player().stop()
            except RuntimeError:
                return
            deadline = time.monotonic() + STOP_WAIT_SECONDS
            while self._ours_playing() and time.monotonic() < deadline:
                if self.sleep(0.05):
                    return
        finally:
            if faded and base:
                self.volume.set(base)

    def _watch(self, key, title, path):
        """Loop the theme from its cached file while its page stays open."""
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
                if self._other_playing():
                    # Something else replaced the theme (a video, a trailer, music): never
                    # loop over it. The theme comes back with the next page.
                    with self._lock:
                        if self.playing == key:
                            self.playing = None
                    return
                if not self.loop:
                    with self._lock:
                        self.playing = None
                    return
                try:
                    import xbmcgui
                    with self._lock:  # suspend() between the check and play() must win.
                        if stop.is_set() or self.playing != key or self._other_playing():
                            return
                        self.player().play(path, xbmcgui.ListItem(label=title, path=path), True)
                except Exception:
                    self.failures += 1
                    return
                started = False

        self._watcher = threading.Thread(target=run, name="silo-theme-loop", daemon=True)
        self._watcher.start()
