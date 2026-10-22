"""Media requests: discover, search, request, follow, and the profile's own requests.

Routes are Silo /api/v2: GET /requests/status, GET /requests/discover,
GET /requests/search, GET /requests/detail/{media_type}/{tmdb_id}, POST /requests,
GET /requests/mine, POST /requests/{id}/cancel and PUT/DELETE
/requests/follows/{media_type}/{tmdb_id}. Titles are keyed by their TMDB id
(an external integer, not a Silo id). The state wording follows the Silo
Android TV app. This module has no Kodi dependency so it can be tested directly.
"""

from urllib.parse import quote, urlsplit

MEDIA_TYPES = ("movie", "series")
TMDB_IMAGES = "https://image.tmdb.org/t/p/"
MINE_PAGE_LIMIT = 50
MINE_MAX_PAGES = 4

# Status dot colours, one per display state (muted, so they read as labels, not accents).
TINTS = {"pending": "FFE8B04B", "on_the_way": "FF6CB4E8", "available": "FF58C48E", "declined": "FFE86A82",
         "failed": "FFE86A82", "unavailable": "9EEDEDED"}

REASONS = {
    "already_requested": "Already requested",
    "already_available": "Already in your library",
    "quota_exceeded": "Request limit reached",
    "limit_reached": "Request limit reached",
    "requests_disabled": "Requests are turned off",
    "blocked": "You can't request media right now",
    "requesting_blocked": "You can't request media right now",
    "validation_failed": "That request couldn't be submitted",
    "invalid_state": "This request can no longer be changed",
    "not_found": "This title is no longer available",
}


# ---------------------------------------------------------------- small helpers

def _text(value):
    return str(value).strip() if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _int(value):
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number


def _dict(value):
    return value if isinstance(value, dict) else {}


def _list(value):
    return [entry for entry in value if isinstance(entry, dict)] if isinstance(value, list) else []


def media_type(media):
    value = _text(_dict(media).get("media_type")).lower()
    return value if value in MEDIA_TYPES else ""


def tmdb_id(media):
    value = _int(_dict(media).get("tmdb_id"))
    return value if value is not None and value > 0 else None


def is_supported(media):
    """Movies and series with a usable TMDB id; other kinds stay off the Kodi screens."""
    return bool(media_type(media)) and tmdb_id(media) is not None


def _media_path(prefix, media):
    kind, number = media_type(media), tmdb_id(media)
    if not kind or number is None:
        raise ValueError("This title cannot be requested.")
    return "{}/{}/{}".format(prefix, kind, number)


def image_url(path, size="w500"):
    """A TMDB image path from the server as a full URL; absolute http(s) URLs pass through."""
    value = _text(path)
    if not value or any(char in value for char in ("|", "\r", "\n", "\t", "\\", " ")):
        return ""
    if value.startswith("/") and not value.startswith("//"):
        return TMDB_IMAGES + size + quote(value, safe="/._-")
    parts = urlsplit(value)
    if parts.scheme in ("http", "https") and parts.hostname and not (parts.username or parts.password):
        return value
    return ""


def poster_url(media):
    return image_url(_dict(media).get("poster_path"), "w500")


def backdrop_url(media):
    return image_url(_dict(media).get("backdrop_path"), "w780")


def reason_text(token):
    """A server reason token as a phrase; a readable sentence shows as written, an unknown code not at all."""
    value = _text(token)
    if not value:
        return ""
    if value in REASONS:
        return REASONS[value]
    if all(char.islower() or char.isdigit() or char == "_" for char in value):
        return ""
    return value[0].upper() + value[1:]


# ---------------------------------------------------------------- server calls

def status(client):
    """The request feature document for this profile (GET /requests/status)."""
    return _dict(client.request("GET", "/requests/status"))


def enabled(client):
    """Whether this profile can use requests now; any failure reads as "no"."""
    try:
        document = status(client)
    except Exception:  # The Requests entry is optional: never break Home over it.
        return False
    return (document.get("requests_enabled") is True and document.get("allowed") is True
            and document.get("state") == "available")


def discover(client):
    """Discover rows in server order: {"key","title","results"}; empty rows are dropped."""
    response = _dict(client.request("GET", "/requests/discover"))
    sections = []
    for section in _list(response.get("items")):
        results = [result for result in _list(section.get("results")) if is_supported(result)]
        if results:
            key = _text(section.get("key"))
            title = _text(section.get("title")) or key.replace("_", " ").capitalize() or "Discover"
            sections.append({"key": key, "title": title, "results": results})
    return sections


def search(client, query, kind="all", page=1):
    """One provider page of search results: {"results","page","total_pages"}."""
    query = _text(query)
    if not query:
        return {"results": [], "page": 1, "total_pages": 0}
    params = {"q": query, "media_type": kind if kind in MEDIA_TYPES else "all", "page": max(1, _int(page) or 1)}
    response = _dict(client.request("GET", "/requests/search", params=params))
    return {"results": [result for result in _list(response.get("results")) if is_supported(result)],
            "page": _int(response.get("page")) or params["page"],
            "total_pages": _int(response.get("total_pages")) or 0}


def detail(client, media):
    """The title's detail document, with availability, request state and seasons."""
    return _dict(client.request("GET", _media_path("/requests/detail", media)))


def create_body(media, seasons=None):
    """The POST /requests body for a title as seen on a card or detail page."""
    kind, number = media_type(media), tmdb_id(media)
    title = _text(media.get("title")) if isinstance(media, dict) else ""
    if not kind or number is None or not title:
        raise ValueError("This title cannot be requested.")
    body = {"media_type": kind, "tmdb_id": number, "title": title}
    tvdb = _int(media.get("tvdb_id"))
    if tvdb is not None and tvdb > 0:
        body["tvdb_id"] = tvdb
    year = _int(media.get("year"))
    if year is not None and year > 0:
        body["year"] = year
    for key in ("imdb_id", "overview", "poster_path", "backdrop_path"):
        value = _text(media.get(key))
        if value:
            body[key] = value
    if kind == "series" and seasons:
        numbers = sorted({number for number in (_int(season) for season in seasons) if number and number > 0})
        if numbers:
            body["seasons"] = numbers
    return body


def create(client, media, seasons=None):
    """Request a title; ``seasons`` (series only) limits it to those season numbers."""
    return _dict(client.request("POST", "/requests", body=create_body(media, seasons)))


def mine(client, limit=MINE_PAGE_LIMIT, max_pages=MINE_MAX_PAGES):
    """The account's requests, newest first, movies and series only."""
    records, cursor = [], None
    for _page in range(max_pages):
        params = {"limit": limit}
        if cursor:
            params["cursor"] = cursor
        response = _dict(client.request("GET", "/requests/mine", params=params))
        records.extend(record for record in _list(response.get("items")) if is_supported(record))
        page = _dict(response.get("page"))
        cursor = _text(page.get("next_cursor")) if page.get("has_more") is True else ""
        if not cursor:
            break
    return records


def cancel(client, request_id):
    request_id = _text(request_id)
    if not request_id:
        raise ValueError("This request cannot be cancelled.")
    return _dict(client.request("POST", "/requests/{}/cancel".format(quote(request_id, safe="")), body={}))


def follow(client, media):
    """Get notified when a title someone else requested becomes available."""
    return _dict(client.request("PUT", _media_path("/requests/follows", media)))


def unfollow(client, media):
    client.request("DELETE", _media_path("/requests/follows", media))


def set_follow(client, media, on):
    return follow(client, media) if on else unfollow(client, media)


# ---------------------------------------------------------------- presentation

def _state(key, short, long, reason=""):
    return {"key": key, "short": short, "long": long, "tint": TINTS[key], "reason": reason}


def _from_user_state(state, status_value, reason=""):
    if state == "pending":
        return _state("pending", "Pending", "Waiting for approval")
    if state == "partially_available":
        return _state("on_the_way", "Partly in library", "Partly in your library")
    if state in ("approved", "processing"):
        return _in_flight(status_value)
    if state == "available":
        return _state("available", "In library", "In your library")
    if state == "declined":
        return _state("declined", "Declined", "Declined", reason)
    if state == "failed":
        return _state("failed", "Failed", "Request failed", reason)
    if state == "cancelled":
        return _state("unavailable", "Cancelled", "Cancelled", reason)
    return None


def _in_flight(status_value):
    if status_value == "downloading":
        return _state("on_the_way", "Downloading", "Downloading")
    if status_value == "completed":
        return _state("on_the_way", "Adding to library", "Adding to your library")
    if status_value in ("approved", "queued"):
        return _state("on_the_way", "Queued", "Queued for download")
    return _state("on_the_way", "On the way", "On the way")


def _from_status(status_value, outcome, reason=""):
    if outcome == "declined":
        return _state("declined", "Declined", "Declined", reason)
    if outcome == "failed" or status_value == "failed":
        return _state("failed", "Failed", "Request failed", reason)
    if outcome == "cancelled":
        return _state("unavailable", "Cancelled", "Cancelled", reason)
    if status_value == "pending":
        return _state("pending", "Pending", "Waiting for approval")
    if status_value == "completed":
        return _state("available", "In library", "In your library")
    return _in_flight(status_value)  # An unknown stage reads as in flight, not broken.


def record_state(record):
    """Display state of one of the profile's own requests."""
    record = _dict(record)
    status_value, outcome = _text(record.get("status")), _text(record.get("outcome"))
    reason = reason_text(record.get("outcome_reason")) or reason_text(record.get("last_error"))
    return (_from_user_state(_text(record.get("state")), status_value, reason)
            or _from_status(status_value, outcome or "active", reason))


def result_state(media):
    """Display state of a discover, search or detail title; None when it can simply be requested."""
    media = _dict(media)
    request = _dict(media.get("request"))
    status_value = _text(request.get("status"))
    found = _from_user_state(_text(request.get("state")), status_value)
    if found:
        return found
    if media.get("availability") == "available":
        return _state("available", "In library", "In your library")
    if status_value:
        return _from_status(status_value, "active")
    if request.get("requestable") is True:
        return None
    reason = reason_text(request.get("reason"))
    return _state("unavailable", "Unavailable", reason or "Unavailable", reason)


def library_item(media, record=False):
    """The catalog content id to open instead of the request, for a title that reads "In library"."""
    state = record_state(media) if record else result_state(media)
    content_id = _text(_dict(media).get("library_content_id"))
    return content_id if state and state["key"] == "available" and content_id else ""


def can_follow(media, feature=None):
    """Someone else's open request: offer "Notify me" instead of a duplicate request."""
    if _dict(feature).get("follow_supported") is False:
        return False
    request = _dict(_dict(media).get("request"))
    if (request.get("requestable") is True or request.get("requested_by_viewer") is True
            or request.get("reason") != "already_requested"):
        return False
    state = result_state(media)
    return bool(state) and state["key"] not in ("available", "unavailable")


def follow_label(media):
    following = _dict(_dict(media).get("request")).get("following") is True
    return "Stop notifying me" if following else "Notify me when available"


def cancellable_request(media):
    """The viewer's own pending request id on a detail document, or ""."""
    request = _dict(_dict(media).get("request"))
    state = result_state(media)
    if request.get("requested_by_viewer") is True and state and state["key"] == "pending":
        return _text(request.get("request_id"))
    return ""


def requestable_seasons(media, feature=None):
    """Series seasons the viewer can still ask for; [] when the server takes whole-series requests only."""
    if media_type(media) != "series" or _dict(feature).get("season_requests_supported") is not True:
        return []
    seasons = []
    for season in _list(_dict(media).get("seasons")):
        number = _int(season.get("season_number"))
        if number and number > 0 and season.get("availability") != "available" and season.get("requested") is not True:
            seasons.append(season)
    return sorted(seasons, key=lambda season: _int(season.get("season_number")))


def season_text(season):
    number = _int(season.get("season_number"))
    parts = [_text(season.get("name")) or "Season {}".format(number)]
    count = _int(season.get("episode_count"))
    if count and count > 0:
        parts.append("1 episode" if count == 1 else "{} episodes".format(count))
    if season.get("availability") == "partial":
        parts.append("partly in library")
    return " · ".join(parts)


def type_label(media):
    return {"movie": "Movie", "series": "Series"}.get(media_type(media), "")


def meta_text(media, record=False):
    """Year · Movie/Series · TMDB rating (titles) or the request day (own requests)."""
    media = _dict(media)
    parts = []
    year = _int(media.get("year"))
    if year and year > 0:
        parts.append(str(year))
    if type_label(media):
        parts.append(type_label(media))
    if record:
        day = _text(media.get("created_at"))[:10]
        if len(day) == 10:
            parts.append("Requested " + day)
    else:
        try:
            rating = float(media.get("vote_average"))
        except (TypeError, ValueError):
            rating = 0.0
        if 0 < rating <= 10:
            parts.append("TMDB {:.1f}".format(rating))
    return " · ".join(parts)
