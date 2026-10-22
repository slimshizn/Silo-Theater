"""Shuffle play: the server picks random movies and episodes from a scope.

Routes are Silo /api/v2: GET /shuffles/capabilities ({"state", "scope_kinds"}),
POST /shuffles ({"scope": {"kind", "id"}} -> {"id", "scope", "current", "next"}),
POST /shuffles/{id}/advance ({"from_content_id"} -> the moved shuffle), and
DELETE /shuffles/{id}. ``current`` and ``next`` are catalog cards; ``next``
equals ``current`` only when the scope holds one playable item.
"""

from urllib.parse import quote

SCOPE_KINDS = ("library", "series", "season", "library_collection", "user_collection")
# Scope keyword -> server scope kind, most specific first.
SCOPE_KEYS = (("season_id", "season"), ("series_id", "series"), ("collection_id", "library_collection"),
              ("user_collection_id", "user_collection"), ("library_id", "library"))


def _id(value):
    return quote(str(value), safe="")


def capability(client):
    response = client.request("GET", "/shuffles/capabilities")
    return response if isinstance(response, dict) else {}


def available(client, kind=None):
    """Whether this server shuffles, optionally from one scope kind; False on any failure."""
    try:
        document = capability(client)
    except Exception:  # A missing capability only hides the action.
        return False
    if document.get("state") != "available":
        return False
    kinds = document.get("scope_kinds")
    return kind is None or (isinstance(kinds, list) and kind in kinds)


def scope_of(**scope):
    """The request scope for keyword arguments such as series_id=…; None when none is given."""
    for key, kind in SCOPE_KEYS:
        value = scope.get(key)
        if value not in (None, ""):
            return {"kind": kind, "id": str(value)}
    return None


def start(client, kind, scope_id):
    if kind not in SCOPE_KINDS:
        raise ValueError("Unknown shuffle scope: {}".format(kind))
    return client.request("POST", "/shuffles", body={"scope": {"kind": kind, "id": str(scope_id)}})


def advance(client, shuffle_id, from_content_id):
    return client.request("POST", "/shuffles/{}/advance".format(_id(shuffle_id)),
                          body={"from_content_id": str(from_content_id)})


def stop(client, shuffle_id):
    return client.request("DELETE", "/shuffles/" + _id(shuffle_id))


def _content_id(card):
    return card.get("content_id") if isinstance(card, dict) else None


def _choose_library(application, kind):
    """A type-only scope (for example type="movie") shuffles one library of that type."""
    libraries = application.video_libraries(kind) if kind else []
    if not libraries:
        application.dialog.ok("Silo", "There is no library to shuffle.")
        return None
    if len(libraries) == 1:
        return libraries[0].get("id")
    selected = application.dialog.select("Shuffle which library?",
                                         [str(library.get("name") or "Library") for library in libraries])
    if not isinstance(selected, int) or not 0 <= selected < len(libraries):
        return None
    return libraries[selected].get("id")


def _ended(application, item, file_id):
    """Whether the last playback ran to its end rather than being stopped.

    Prefers the application's record of the last playback; otherwise reads the
    progress the application cached after playback.
    """
    result = getattr(application, "last_playback_result", None)
    if isinstance(result, dict):
        return result.get("ended") is True
    after = application.item_after_playback(item, file_id)
    after = after.get("user_data") if isinstance(after, dict) else None
    before = item.get("user_data")
    if not isinstance(after, dict) or after == before:
        return False  # Nothing was recorded, so nothing finished.
    return after.get("played") is True and after.get("is_in_progress") is not True


def _aborted(application):
    monitor = getattr(application, "monitor", None)
    try:
        return bool(monitor is not None and monitor.abortRequested() is True)
    except Exception:
        return False


def play_shuffle(application, **scope):
    """Start a shuffle and play its picks one after another.

    Scope keywords: season_id, series_id, library_id (also collection_id and
    user_collection_id), or type alone ("movie"/"series") to pick a library of
    that type. Each pick plays from the start; when one ends normally the
    shuffle advances and the next plays. Stopping playback ends the shuffle.
    Returns how many picks started, or None when the shuffle never began.
    """
    client = application.client
    request = scope_of(**scope)
    if request is None:
        library_id = _choose_library(application, scope.get("type"))
        if library_id in (None, ""):
            return None
        request = {"kind": "library", "id": str(library_id)}
    if not available(client, request["kind"]):
        application.dialog.ok("Silo", "This server cannot shuffle here.")
        return None
    shuffle = start(client, request["kind"], request["id"])
    if not isinstance(shuffle, dict) or not shuffle.get("id"):
        return None
    shuffle_id = shuffle["id"]
    played = 0
    try:
        while not _aborted(application):
            current_id = _content_id(shuffle.get("current"))
            if not current_id:
                break
            item = client.item(current_id)
            if not application.is_playable(item):
                break
            # Picks play from the beginning, as on the other Silo clients.
            file_id = application.play(item, resume=False, auto_next=False)
            if file_id is None:
                break
            played += 1
            if not _ended(application, item, file_id) or _aborted(application):
                break
            next_id = _content_id(shuffle.get("next"))
            if not next_id or next_id == current_id:
                break  # The scope holds one playable item.
            shuffle = advance(client, shuffle_id, current_id)
            if not isinstance(shuffle, dict):
                break
    finally:
        try:
            stop(client, shuffle_id)
        except Exception:  # The server deletes abandoned shuffles itself.
            pass
    return played
