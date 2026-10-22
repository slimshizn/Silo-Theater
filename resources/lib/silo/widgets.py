"""Kodi plugin listings so skins can show Silo rows as home-screen widgets.

Kodi calls the plugin entry with ``argv = [base_url, handle, query]``. The root
lists the profile's home sections plus Watchlist and Favorites as folders; each
folder lists video items with full info tags and artwork. Selecting an item
never resolves a stream here: it hands the content id to the main add-on with
``RunScript(script.silo,play,<id>)`` (or ``open``), so playback keeps Silo's
progress reporting, resume and skip markers.

The saved session (``session.json`` in the add-on profile folder, the
``server_url`` setting and the saved profile) is reused read-mostly. Tokens are
only ever sent as request headers by ``SiloClient``; they never appear in item
URLs, labels, cache file names or log lines. Kodi refreshes widgets often, so
each listing is cached in the profile folder for a short time.

Routes: GET /home/sections (root), GET /home/sections/{id}/items (one row),
GET /watchlist and GET /favorites (personal rows, via svc_personal).
"""

import hashlib
import json
import math
import os
import tempfile
import time
from urllib.parse import parse_qsl, quote, unquote, urlencode

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

from . import present, svc_personal
from .api import ApiError, SiloClient, normalize_server_url
from .deeplink import ADDON_ID, DEEP_LINK_ACTIONS, deep_link, run_script_builtin, valid_id  # noqa: F401
from .settings import read_options
from .storage import SessionStore, StorageError, session_path

# The widgets add-on (plugin.video.silotheater) runs this code with Founders Theater's settings and session.
BASE_URL = "plugin://plugin.video.silotheater/"
CACHE_SECONDS = 60
# A failed refresh may show an older copy; presigned art outlives this.
STALE_SECONDS = 15 * 60
CACHE_FOLDER = "widget_cache"
PERSONAL_LIMIT = 50
PERSONAL = (("watchlist", "Watchlist", svc_personal.watchlist),
            ("favorites", "Favorites", svc_personal.favorites))
MEDIA_TYPES = {"movie": "movie", "episode": "episode", "series": "tvshow", "season": "season"}
CONTENT_TYPES = {"movie": "movies", "episode": "episodes", "series": "tvshows", "season": "seasons"}
SIGN_IN_LABEL = "Open Founders Theater to sign in"


class SignedOut(Exception):
    """No usable saved session or profile; the add-on must be opened first."""


# ------------------------------------------------------------------ URLs

def plugin_url(action, **params):
    """A plugin:// URL for this add-on. Only ids and actions go in it."""
    pairs = [("action", action)] + sorted((key, str(value)) for key, value in params.items() if value is not None)
    return BASE_URL + "?" + urlencode(pairs)


def parse_query(query):
    query = str(query or "")
    return dict(parse_qsl(query[1:] if query.startswith("?") else query))











def item_target(item):
    """(action, content_id) for a card: play movies/episodes, open the rest.

    A Continue Watching or Next Up card on a series plays its next episode,
    like the system row on Android TV.
    """
    if item.get("play_content_id") and item.get("item_source") in ("in_progress", "next_up"):
        return "play", str(item["play_content_id"])
    if item.get("type") in ("movie", "episode"):
        return "play", str(item["content_id"])
    return "open", str(item["content_id"])


# --------------------------------------------------------------- session

def connect(addon, client_factory=SiloClient):
    """A client on the saved session and profile, or raise SignedOut.

    Never signs in or chooses a profile, and never builds a client for a
    store saved for another server (that would clear its credentials).
    """
    server_url = str(addon.getSetting("server_url") or "").strip()
    if not server_url:
        raise SignedOut()
    try:
        server_url = normalize_server_url(server_url)
        store = SessionStore(session_path(profile_folder(addon), server_url, adopt_legacy=False))
    except (ValueError, StorageError):
        raise SignedOut()
    if store.get("server_url") != server_url:
        raise SignedOut()
    if not (store.get("access_token") or store.get("refresh_token")) or not store.get("profile_id"):
        raise SignedOut()
    version = "".join(char for char in str(addon.getAddonInfo("version")) if char in "0123456789.")[:24]
    return client_factory(server_url, store, user_agent="FoundersTheater/" + version if version else "FoundersTheater")


def profile_folder(addon):
    return xbmcvfs.translatePath(addon.getAddonInfo("profile"))


def cache_scope(client):
    store = client.store
    return "{}|{}".format(client.server_url, store.get("profile_id") or "")


# ----------------------------------------------------------------- cache

class ListingCache:
    """Small JSON files, one per listing and profile, replaced atomically.

    File names are hashes, so neither the server nor the profile id appears
    on disk; responses hold presigned art URLs only, never credentials.
    """

    def __init__(self, folder, ttl=CACHE_SECONDS, clock=time.time):
        self.folder = folder
        self.ttl = ttl
        self.clock = clock

    def _path(self, key, scope):
        digest = hashlib.sha256("{}\n{}".format(scope, key).encode("utf-8")).hexdigest()[:32]
        return os.path.join(self.folder, digest + ".json")

    def get(self, key, scope, max_age=None):
        max_age = self.ttl if max_age is None else max_age
        try:
            with open(self._path(key, scope), "r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or entry.get("scope") != scope or entry.get("key") != key:
            return None
        saved = entry.get("at")
        if isinstance(saved, bool) or not isinstance(saved, (int, float)) or not math.isfinite(saved):
            return None
        age = self.clock() - saved
        if age < 0 or age > max_age:
            return None
        return entry.get("data")

    def put(self, key, scope, data):
        tmp = None
        try:
            os.makedirs(self.folder, mode=0o700, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=".widget-", dir=self.folder)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"key": key, "scope": scope, "at": self.clock(), "data": data}, handle,
                          ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._path(key, scope))
            tmp = None
        except (OSError, ValueError, TypeError):
            pass  # A cache that cannot be written only costs a request.
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass

    def clear(self):
        try:
            names = os.listdir(self.folder)
        except OSError:
            return
        for name in names:
            if name.endswith(".json"):
                try:
                    os.unlink(os.path.join(self.folder, name))
                except OSError:
                    pass


def cached(cache, key, scope, fetch):
    """Fresh cache, else fetch and save; on a network error, a recent copy."""
    data = cache.get(key, scope)
    if data is not None:
        return data
    try:
        data = fetch()
    except ApiError as exc:
        if exc.status in (401, 403):
            raise
        stale = cache.get(key, scope, max_age=STALE_SECONDS)
        if stale is not None:
            return stale
        raise
    cache.put(key, scope, data)
    return data


def clear_cache(addon=None):
    """Drop every cached listing, e.g. after playback changed progress."""
    addon = addon or xbmcaddon.Addon()
    ListingCache(os.path.join(profile_folder(addon), CACHE_FOLDER)).clear()


# --------------------------------------------------------------- fetches

def fetch_sections(client):
    response = client.request("GET", "/home/sections") or {}
    return [{"id": str(section.get("id")), "title": str(section.get("title") or ""),
             "items": present.video_items(section.get("items"))[:1]}
            for section in response.get("sections", [])
            if isinstance(section, dict) and section.get("id") and present.video_items(section.get("items"))]


def fetch_section_items(client, section_id):
    response = client.request("GET", "/home/sections/{}/items".format(quote(section_id, safe=""))) or {}
    return present.video_items(response.get("items"))


def fetch_personal(function):
    def fetch(client):
        return present.video_items(function(client, limit=PERSONAL_LIMIT).get("items"))
    return fetch


# -------------------------------------------------------------- list items

def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _int(value):
    number = _number(value)
    return int(number) if number is not None else None


def duration_seconds(item):
    state = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
    for value in (item.get("duration_seconds"), state.get("duration_seconds")):
        number = _number(value)
        if number and number > 0:
            return number
    runtime = _number(item.get("runtime"))
    return runtime * 60 if runtime and runtime > 0 else 0.0


def resume_seconds(item):
    if present.is_watched(item):
        return 0.0
    state = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
    position = _number(item.get("position_seconds"))
    if position is None:
        position = _number(state.get("position_seconds"))
    duration = duration_seconds(item)
    if not position or position <= 0 or (duration and position >= duration):
        return 0.0
    return position


def artwork(item, client):
    image = lambda kind: client.image_url(item, kind)
    poster, backdrop, logo = image("poster"), image("backdrop"), image("logo")
    still = poster if item.get("type") == "episode" and item.get("poster_is_episode_still") is not False else ""
    art = {"poster": poster or backdrop, "fanart": backdrop, "landscape": backdrop or still,
           "thumb": still or backdrop or poster, "clearlogo": logo, "icon": poster or backdrop}
    if item.get("type") == "episode" and not item.get("poster_is_episode_still"):
        # A series poster on an episode card is the show's, not the episode's.
        art["tvshow.poster"] = poster
    return {key: value for key, value in art.items() if value}


def item_label(item):
    if item.get("type") == "episode":
        title = str(item.get("title") or "").strip()
        number = _int(item.get("episode_number"))
        return title or ("Episode {}".format(number) if number is not None else "Episode")
    return str(item.get("title") or item.get("name") or "").strip() or "Untitled"


def list_item(item, client, options):
    kind = item.get("type")
    label = item_label(item)
    result = xbmcgui.ListItem(label=label, label2=present.card_subtitle(item), offscreen=True)
    result.setArt(artwork(item, client))
    result.setProperty("IsPlayable", "false")
    tag = result.getVideoInfoTag()
    tag.setMediaType(MEDIA_TYPES.get(kind, "video"))
    tag.setTitle(label)
    plot = str(item.get("overview") or "")
    if options.get("hide_spoilers") and kind == "episode" and not present.is_watched(item):
        plot = ""
    tag.setPlot(plot)
    year = _int(item.get("year"))
    if year:
        tag.setYear(year)
    genres = [str(genre) for genre in item.get("genres") or [] if genre]
    if genres:
        tag.setGenres(genres)
    if item.get("content_rating"):
        tag.setMpaa(str(item["content_rating"]))
    if item.get("release_date"):
        tag.setPremiered(str(item["release_date"])[:10])
    rating = _number(item.get("rating_imdb"))
    if rating:
        tag.setRating(rating, 0, "imdb", True)
    duration = duration_seconds(item)
    if duration:
        tag.setDuration(int(duration))
    if kind in ("episode", "season"):
        if item.get("series_title"):
            tag.setTvShowTitle(str(item["series_title"]))
        season = _int(item.get("season_number"))
        if season is not None:
            tag.setSeason(season)
    if kind == "episode":
        episode = _int(item.get("episode_number"))
        if episode is not None:
            tag.setEpisode(episode)
    if kind == "series":
        tag.setTvShowTitle(label)
    tag.setPlaycount(1 if present.is_watched(item) else 0)
    position = resume_seconds(item)
    if position:
        tag.setResumePoint(position, duration)
    tag.setUniqueIDs({"silo": str(item["content_id"])}, "silo")
    action, content_id = item_target(item)
    return plugin_url(action, id=content_id), result, False


def folder_item(label, url, art=None):
    result = xbmcgui.ListItem(label=label, offscreen=True)
    if art:
        result.setArt(art)
    return url, result, True


def content_type(items):
    kinds = {item.get("type") for item in items}
    return CONTENT_TYPES.get(kinds.pop(), "videos") if len(kinds) == 1 else "videos"


# ----------------------------------------------------------------- plugin

class Plugin:
    def __init__(self, handle, addon=None, client_factory=SiloClient, clock=time.time):
        self.handle = handle
        self.addon = addon or xbmcaddon.Addon()
        self.client_factory = client_factory
        self.clock = clock
        self.client = None
        self.cache = None

    def dispatch(self, params):
        action = params.get("action", "")
        if action in DEEP_LINK_ACTIONS:
            content_id = valid_id(params.get("id"))
            if content_id:
                xbmc.executebuiltin(run_script_builtin(action, content_id))
            return
        if action == "signin":
            xbmc.executebuiltin("RunScript({})".format(ADDON_ID))
            return
        try:
            self.client = connect(self.addon, self.client_factory)
            self.cache = ListingCache(os.path.join(profile_folder(self.addon), CACHE_FOLDER), clock=self.clock)
            if action == "section" and valid_id(params.get("id")):
                self.items_listing("section:" + valid_id(params.get("id")),
                                   lambda client: fetch_section_items(client, valid_id(params.get("id"))))
            elif action in [name for name, _, _ in PERSONAL]:
                function = next(function for name, _, function in PERSONAL if name == action)
                self.items_listing(action, fetch_personal(function))
            else:
                self.root_listing()
        except SignedOut:
            self.signed_out()
        except ApiError as exc:
            if exc.status in (401, 403):
                self.signed_out()
            else:
                # Never log exc.message: it may echo server text.
                xbmc.log("[Silo] Widget listing failed (HTTP {})".format(exc.status), xbmc.LOGWARNING)
                xbmcplugin.endOfDirectory(self.handle, succeeded=False, cacheToDisc=False)
        except Exception:
            xbmc.log("[Silo] Unexpected widget error", xbmc.LOGERROR)
            xbmcplugin.endOfDirectory(self.handle, succeeded=False, cacheToDisc=False)

    def _fetch(self, key, fetch):
        return cached(self.cache, key, cache_scope(self.client), lambda: fetch(self.client))

    def root_listing(self):
        entries = []
        for section in self._fetch("sections", fetch_sections):
            if not section.get("title"):
                continue
            sample = (section.get("items") or [{}])[0]
            backdrop, poster = self.client.image_url(sample, "backdrop"), self.client.image_url(sample, "poster")
            art = {key: value for key, value in (("fanart", backdrop), ("thumb", backdrop or poster)) if value}
            entries.append(folder_item(section["title"], plugin_url("section", id=section["id"]), art))
        for name, label, _ in PERSONAL:
            entries.append(folder_item(label, plugin_url(name)))
        xbmcplugin.setContent(self.handle, "videos")
        xbmcplugin.addSortMethod(self.handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addDirectoryItems(self.handle, entries, len(entries))
        xbmcplugin.endOfDirectory(self.handle, succeeded=True, cacheToDisc=False)

    def items_listing(self, key, fetch):
        items = [item for item in self._fetch(key, fetch) or [] if isinstance(item, dict)]
        options = read_options(self.addon)
        entries = [list_item(item, self.client, options) for item in items]
        kind = content_type(items)
        xbmcplugin.setContent(self.handle, kind)
        for method in self.sort_methods(kind):
            xbmcplugin.addSortMethod(self.handle, method)
        xbmcplugin.addDirectoryItems(self.handle, entries, len(entries))
        xbmcplugin.endOfDirectory(self.handle, succeeded=True, cacheToDisc=False)

    @staticmethod
    def sort_methods(kind):
        methods = [xbmcplugin.SORT_METHOD_UNSORTED, xbmcplugin.SORT_METHOD_LABEL_IGNORE_THE]
        if kind == "episodes":
            methods.append(xbmcplugin.SORT_METHOD_EPISODE)
        else:
            methods.append(xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        return methods

    def signed_out(self):
        result = xbmcgui.ListItem(label=SIGN_IN_LABEL, offscreen=True)
        result.setArt({"icon": "special://home/addons/{}/resources/icon.png".format(ADDON_ID)})
        result.setProperty("IsPlayable", "false")
        xbmcplugin.addDirectoryItems(self.handle, [(plugin_url("signin"), result, False)], 1)
        xbmcplugin.endOfDirectory(self.handle, succeeded=True, cacheToDisc=False)


def run(argv, addon=None):
    """Kodi plugin entry: ``argv = [base_url, handle, query]``; ``addon`` is Founders Theater's."""
    try:
        handle = int(argv[1])
    except (IndexError, TypeError, ValueError):
        handle = -1
    params = parse_query(argv[2] if len(argv) > 2 else "")
    Plugin(handle, addon=addon).dispatch(params)
