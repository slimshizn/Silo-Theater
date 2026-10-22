"""Browse data: For You, calendar, collections, library rows, people and history.

Routes are Silo /api/v2: GET /recommendations/discover, GET /calendar,
GET /collections, GET /library/{id}/collections, GET /library/{id}/collections/{cid}/items,
GET /library/{id}/sections (+ /sections/{sid}/items), GET /catalog/people/{id},
GET /catalog (source=person | user_collection) and POST /history/remove.
Every reader tolerates missing fields and wrong types and returns plain dicts.
"""

from urllib.parse import quote

from . import present


def _id(value):
    return quote(str(value), safe="")


def _list(value):
    return value if isinstance(value, list) else []


def _dict(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    if value is None or isinstance(value, (dict, list, bool)):
        return ""
    return str(value).strip()


def _int(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _get(client, path, **params):
    return _dict(client.request("GET", path, params=params or None))


def _page(response):
    page = _dict(response.get("page"))
    cursor = _text(page.get("next_cursor"))
    return {"items": present.video_items(_list(response.get("items"))),
            "next_cursor": cursor if page.get("has_more") and cursor else None,
            "total": _int(response.get("total"))}


def _row(row_id, title, section_type, items):
    return {"id": _text(row_id), "title": _text(title), "section_type": _text(section_type),
            "items": present.video_items(_list(items))}


# ----------------------------------------------------------------- For You

def discover(client):
    """The For You rows, as the Android TV app shows them: non-empty video rows."""
    rows = []
    for index, row in enumerate(_list(_get(client, "/recommendations/discover").get("items"))):
        row = _dict(row)
        entry = _row(row.get("key") or index, row.get("title"), row.get("type"), row.get("items"))
        if entry["items"]:
            rows.append(entry)
    return rows


# ----------------------------------------------------------------- calendar

def calendar_event(event):
    """A calendar event as a card item; ``title`` is the episode title for episodes."""
    event = _dict(event)
    content_id = _text(event.get("content_id"))
    if not content_id:
        return None
    season, episode = _int(event.get("season_number")), _int(event.get("episode_number"))
    kind = _text(event.get("type"))
    if kind != "movie":
        kind = "episode" if season is not None and episode is not None else "series"
    item = {"content_id": content_id, "type": kind, "poster_url": _text(event.get("poster_url")),
            "air_date": _text(event.get("air_date")), "local_air_date": _text(event.get("local_air_date")),
            "air_time": _text(event.get("air_time")), "air_at": _text(event.get("air_at")),
            "badges": [_text(badge) for badge in _list(event.get("badges")) if _text(badge)],
            "user_state": {"played": event.get("watched") is True}}
    if kind == "episode":
        item.update(title=_text(event.get("episode_title")), series_title=_text(event.get("title")),
                    series_id=_text(event.get("series_id")), season_number=season, episode_number=episode)
    else:
        item["title"] = _text(event.get("title"))
        if kind == "series" and season is not None:
            item["season_number"] = season  # A season premiere: "Season N" on the caption.
    return item


def calendar(client, start, end, filter=None, timezone=None, library_id=None):  # noqa: A002 - API name
    """Days from ``start`` to ``end`` (YYYY-MM-DD) with events: [{"date", "items"}]."""
    params = {"start": start, "end": end}
    if filter:
        params["filter"] = filter
    if timezone:
        params["timezone"] = timezone
    if library_id is not None:
        params["library_id"] = library_id
    days = []
    for day in _list(_get(client, "/calendar", **params).get("events")):
        day = _dict(day)
        date = _text(day.get("date"))[:10]
        items = [item for item in map(calendar_event, _list(day.get("items"))) if item]
        if date and items:
            days.append({"date": date, "items": items})
    days.sort(key=lambda entry: entry["date"])
    return days


# ----------------------------------------------------------------- collections

def collection_card(collection, kind="personal", library_id=None):
    """A collection as {"id", "title", "overview", "poster_url", "backdrop_url", "count", "kind", "library_id"}."""
    collection = _dict(collection)
    collection_id = _text(collection.get("id"))
    if not collection_id:
        return None
    if collection.get("creator_profile_id"):
        kind = "personal"
    return {"id": collection_id, "title": _text(collection.get("title") or collection.get("name")) or "Collection",
            "overview": _text(collection.get("description") or collection.get("overview")),
            "poster_url": _text(collection.get("poster_url")), "backdrop_url": _text(collection.get("backdrop_url")),
            "count": _int(collection.get("item_count")), "kind": kind,
            "library_id": _text(library_id if library_id is not None else collection.get("library_id")) or None}


def collections(client):
    """The profile's own and shared collections: [{"title", "collections"}] (one group)."""
    cards = [card for card in (collection_card(entry) for entry in _list(_get(client, "/collections").get("items")))
             if card]
    return [{"title": "Collections", "collections": cards}] if cards else []


def library_collections(client, library_id):
    """A library's Collections tab as groups in display order: [{"title", "collections"}]."""
    response = _get(client, "/library/{}/collections".format(_id(library_id)))
    full = {}
    for entry in _list(response.get("collections")):
        card = collection_card(entry, "library", library_id)
        if card:
            full[card["id"]] = card

    def cards(entries, kind):
        result = []
        for entry in _list(entries):
            card = collection_card(entry, kind, library_id)
            if card:
                known = full.get(card["id"]) if card["kind"] == "library" else None
                result.append(dict(known, **{key: value for key, value in card.items() if value})
                              if known else card)
        return result

    groups, seen = [], set()
    for group in _list(response.get("groups")):
        group = _dict(group)
        kind = "personal" if group.get("kind") == "user_collections" else "library"
        entries = cards(group.get("collections"), kind)
        if entries:
            groups.append({"title": _text(group.get("name")) or "Collections", "collections": entries})
            seen.update(card["id"] for card in entries)
    ungrouped = cards(_dict(response.get("ungrouped")).get("collections"), "library")
    if not groups and not ungrouped:
        ungrouped = list(full.values())
    ungrouped = [card for card in ungrouped if card["id"] not in seen]
    if ungrouped:
        groups.append({"title": "More collections" if groups else "Collections", "collections": ungrouped})
    return groups


def collection_items(client, collection, cursor=None, limit=50, library_id=None):
    """One page of a collection's titles: {"items", "next_cursor", "total"}."""
    collection = _dict(collection)
    library_id = library_id if library_id is not None else collection.get("library_id")
    params = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    if collection.get("kind") == "library" and library_id not in (None, ""):
        path = "/library/{}/collections/{}/items".format(_id(library_id), _id(collection["id"]))
        return _page(_get(client, path, **params))
    params.update(source="user_collection", collection_id=collection["id"])
    if library_id not in (None, ""):
        params["library_id"] = library_id
    return _page(_get(client, "/catalog", **params))


# ----------------------------------------------------------------- library rows

def library_sections(client, library_id):
    """A library's Recommended rows: [{"id", "title", "section_type", "items"}], non-empty only."""
    base = "/library/{}/sections".format(_id(library_id))
    rows = []
    for section in _list(_get(client, base).get("sections")):
        section = _dict(section)
        section_id = _text(section.get("id"))
        items = _list(section.get("items"))
        if not items and section_id and (_int(section.get("total_count")) or 0) > 0:
            items = _list(_get(client, "{}/{}/items".format(base, _id(section_id))).get("items"))
        row = _row(section_id, section.get("title"), section.get("section_type"), items)
        if row["items"]:
            rows.append(row)
    return rows


# ----------------------------------------------------------------- people

def person(client, person_id):
    """A person: {"id", "name", "photo_url", "bio", "birth_date", "birthplace", "death_date", "known_for"}."""
    data = _get(client, "/catalog/people/{}".format(_id(person_id)))
    return {"id": _text(data.get("id")) or _text(person_id), "name": _text(data.get("name")),
            "photo_url": _text(data.get("photo_url")), "bio": _text(data.get("bio") or data.get("biography")),
            "birth_date": _text(data.get("birth_date"))[:10], "birthplace": _text(data.get("birthplace")),
            "death_date": _text(data.get("death_date"))[:10],
            "known_for": _text(data.get("known_for_department") or data.get("known_for"))}


def person_credits(client, person_id, kind, cursor=None, limit=50):
    """One page of a person's movies or series, newest first."""
    params = {"source": "person", "person_id": person_id, "type": kind, "sort": "-year", "limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _page(_get(client, "/catalog", **params))


# ----------------------------------------------------------------- history

def remove_history(client, content_id):
    """Hide a title's watches from the profile's history (a series expands to its episodes)."""
    return client.request("POST", "/history/remove", body={"targets": [{"content_id": str(content_id)}]})
