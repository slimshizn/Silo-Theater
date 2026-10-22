"""The profile's own lists and marks: watchlist, favorites, watched, history.

Routes are Silo /api/v2: PUT/DELETE /watchlist/{id}, PUT/DELETE /favorites/{id},
POST/DELETE /watched/{id} (a season or series expands to its episodes),
PUT /home/dismissals/{surface}/{id}, and GET /watchlist, /favorites, /history.
"""

from urllib.parse import quote

DISMISS_SURFACES = ("continue_watching", "next_up")


def _id(item_id):
    return quote(str(item_id), safe="")


def set_watchlist(client, item_id, on):
    return client.request("PUT" if on else "DELETE", "/watchlist/" + _id(item_id))


def set_favorite(client, item_id, on):
    return client.request("PUT" if on else "DELETE", "/favorites/" + _id(item_id))


def set_watched(client, item_id, on):
    return client.request("POST" if on else "DELETE", "/watched/" + _id(item_id))


def dismiss(client, surface, item_id, item=None):
    """Hide a card from Continue Watching or Next Up on every device.

    The server anchors a dismissal: Next Up needs the card's series_id, and
    Continue Watching its progress_updated_at (the card returns once the title
    is played again). Both come from the home card ``item``.
    """
    if surface not in DISMISS_SURFACES:
        raise ValueError("Unknown home surface: {}".format(surface))
    item = item or {}
    if surface == "next_up":
        body = {"series_id": item.get("series_id")}
    else:
        body = {"progress_updated_at": item.get("progress_updated_at")}
    if not all(body.values()):
        raise ValueError("This card cannot be removed from {}.".format(surface.replace("_", " ")))
    return client.request("PUT", "/home/dismissals/{}/{}".format(surface, _id(item_id)), body)


def dismiss_surface(item):
    """The surface a home card belongs to, from the card's item_source."""
    return {"in_progress": "continue_watching", "next_up": "next_up"}.get(item.get("item_source"))


def _page(client, path, cursor=None, limit=50, **params):
    params = dict(params, limit=limit)
    if cursor:
        params["cursor"] = cursor
    response = client.request("GET", path, params=params) or {}
    page = response.get("page") or {}
    return {"items": [item for item in response.get("items", []) if isinstance(item, dict)],
            "next_cursor": page.get("next_cursor") if page.get("has_more") else None,
            "total": response.get("total")}


def watchlist(client, cursor=None, limit=50):
    return _page(client, "/watchlist", cursor, limit)


def favorites(client, cursor=None, limit=50):
    return _page(client, "/favorites", cursor, limit)


def history(client, cursor=None, limit=50):
    return _page(client, "/history", cursor, limit)


def state(item):
    """The card's current marks, from user_state (catalog) or user_data (details)."""
    marks = {}
    for key in ("user_state", "user_data"):
        if isinstance(item.get(key), dict):
            marks.update(item[key])
    return {"watchlist": marks.get("in_watchlist") is True, "favorite": marks.get("is_favorite") is True,
            "watched": marks.get("played") is True}
