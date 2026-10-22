"""Browse screens: For You, calendar, collections, library rows, personal lists and people.

Each ``open_*`` function opens a modal full-screen window styled like Home and
returns when it closes. Rows screens use SiloBrowseRows.xml (and SiloPerson.xml
for a person); grids use SiloBrowseGrid.xml. scripts/skin_browse.py writes the
XML; control ids must match.
"""

import datetime
import os

import xbmcgui

from . import present, svc_browse, svc_personal
from .ui_common import (ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer,
                        card_list_item, set_properties)

ACTION_CONTEXT_MENU = 117
SCREEN_WIDTH = 1920

# Shared ids.
TITLE_PILL, EMPTY = 102, 550
HERO_GROUP, HERO_CONTENT, HERO_META = 400, 401, 430
BADGE_IMAGES, BADGE_LABELS = (411, 412, 413, 414), (421, 422, 423, 424)
HERO_LEFT, BADGE_TOP, BADGE_GAP, NO_SYNOPSIS_SHIFT = 88, 346, 12, 84

# Row slot k: group SLOT_BASE + 10k, header +1, play icon +2, wide list +3, poster list +4.
SLOTS, SLOT_BASE = 12, 3000
HERO_ROWS_TOP, CALENDAR_ROWS_TOP, WIDE_PITCH, POSTER_PITCH = 530, 350, 410, 470
SCREEN_HEIGHT = 1080
LOAD_AHEAD = 12

# Calendar header.
CALENDAR_HEADER, FILTER_CHIP, WEEK_BUTTON = 500, 510, 520
CALENDAR_FILTERS = (("following", "Following"), ("trending", "Trending"), ("all", "All"))
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August", "September",
               "October", "November", "December")

# Grid window.
GRID, HEADING, GRID_META, OVERVIEW = 500, 410, 420, 430
GRID_LEFT, GRID_TOP_PLAIN, GRID_TOP_HEADING, GRID_TOP_OVERVIEW = 40, 148, 270, 400

# Person window.
READ_MORE, BIO_PREVIEW = 600, 280

PERSONAL = {
    "watchlist": ("Watchlist", "Your watchlist is empty", "Remove from Watchlist"),
    "favorites": ("Favorites", "No favorites yet", "Remove from Favorites"),
    "history": ("History", "Nothing watched yet", "Remove from History"),
}


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3, base + 4


def pill_width(text):
    """Estimated width of the top-bar title pill at the 25px bold tab font."""
    return 56 + 14 * len(text)


def count_text(count):
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return ""
    return "{:,} title{}".format(count, "" if count == 1 else "s")


# ------------------------------------------------------------------ data helpers

def spoiler_safe(item):
    """An unwatched episode without its title, synopsis or still (series art is kept)."""
    if item.get("type") != "episode" or present.is_watched(item):
        return item
    item = dict(item, overview="", description="")
    number = item.get("episode_number")
    item["title"] = "" if item.get("series_title") else ("Episode {}".format(number) if number else "Episode")
    for kind in ("poster", "backdrop"):
        if item.get(kind + "_is_episode_still") is not False:
            item.pop(kind + "_url", None)
    item.pop("still_url", None)
    return item


def prepare(application, items):
    """Video cards only, with spoilers hidden when the device setting asks for it."""
    items = present.video_items(items)
    if (application.guard(application.options) or {}).get("hide_spoilers"):
        items = [spoiler_safe(item) for item in items]
    return items


def page_size(application):
    size = (application.guard(application.options) or {}).get("page_size")
    return size if isinstance(size, int) and not isinstance(size, bool) and size > 0 else 50


def today():
    return datetime.date.today()


def parse_date(text):
    try:
        return datetime.date.fromisoformat(str(text or "")[:10])
    except ValueError:
        return None


def day_heading(date, today):
    """"Today", "Tomorrow", "Yesterday" or "Wednesday, Oct 22"."""
    day = parse_date(date)
    if day is None:
        return str(date or "")
    offset = (day - today).days
    if offset == 0:
        return "Today"
    if offset == 1:
        return "Tomorrow"
    if offset == -1:
        return "Yesterday"
    return "{}, {} {}".format(WEEKDAYS[day.weekday()], MONTHS[day.month - 1], day.day)


def week_start(day):
    return day - datetime.timedelta(days=day.weekday())


def week_label(start):
    end = start + datetime.timedelta(days=6)
    if start.month == end.month:
        return "{} {} – {}".format(MONTHS[start.month - 1], start.day, end.day)
    return "{} {} – {} {}".format(MONTHS[start.month - 1], start.day, MONTHS[end.month - 1], end.day)


def relative_week(start, today):
    weeks = (start - week_start(today)).days // 7
    return {0: "This week", 1: "Next week", -1: "Last week"}.get(weeks, "")


def air_time(item):
    """Local HH:MM when the airing instant is known, else the network's wall-clock time."""
    instant = str(item.get("air_at") or "")
    if instant:
        try:
            moment = datetime.datetime.fromisoformat(instant.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                return moment.astimezone().strftime("%H:%M")
        except (ValueError, OverflowError, OSError):
            pass
    return str(item.get("air_time") or "")[:5]


def calendar_caption(item):
    """Card captions for a calendar event: series title, then episode code and air time."""
    if item.get("type") == "episode":
        code = present.episode_code(item, ":")
    elif item.get("type") == "series" and item.get("season_number") is not None:
        code = "Season {}".format(item["season_number"])
    else:
        code = "Movie" if item.get("type") == "movie" else ""
    second = " · ".join(part for part in (code, air_time(item)) if part)
    title = item.get("series_title") if item.get("type") == "episode" else item.get("title")
    return str(title or item.get("title") or "Untitled"), second


def local_timezone():
    """The device's IANA zone name, or "" when it cannot be told (the server then uses UTC)."""
    zone = os.environ.get("TZ", "").lstrip(":")
    if "/" in zone and not zone.startswith("/"):
        return zone
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return ""
    marker = "/zoneinfo/"
    return target.split(marker, 1)[1] if marker in target else ""


def watched_caption(item):
    """"Watched Oct 3" (with the year when it is not this year) from a history card."""
    watch = item.get("watch") if isinstance(item.get("watch"), dict) else {}
    day = parse_date(watch.get("watched_at"))
    if day is None:
        return ""
    text = "Watched {} {}".format(MONTHS[day.month - 1], day.day)
    return text if day.year == today().year else "{}, {}".format(text, day.year)


def long_date(text):
    day = parse_date(text)
    return "{} {}, {}".format(MONTH_NAMES[day.month - 1], day.day, day.year) if day else ""


def person_facts(person):
    parts = []
    if person.get("known_for"):
        parts.append("Known for " + person["known_for"])
    born = long_date(person.get("birth_date"))
    if born or person.get("birthplace"):
        text = "Born " + born if born else "Born"
        parts.append(text + (" in " + person["birthplace"] if person.get("birthplace") else ""))
    died = long_date(person.get("death_date"))
    if died:
        parts.append("Died " + died)
    return " · ".join(parts)


def collection_list_item(collection, image):
    result = xbmcgui.ListItem(label=collection.get("title") or "Collection",
                              label2=count_text(collection.get("count")))
    poster, backdrop = image(collection, "poster"), image(collection, "backdrop")
    result.setArt({key: value for key, value in {"poster": poster or backdrop, "fanart": backdrop,
                                                 "thumb": backdrop or poster}.items() if value})
    result.setProperty("silo.plot", collection.get("overview") or "")
    return result


def open_window(application, window_class, xml, **attributes):
    window = window_class(xml, application.addon.getAddonInfo("path"), "Default", "1080i")
    window.application = application
    for key, value in attributes.items():
        setattr(window, key, value)
    window.doModal()
    del window


# ------------------------------------------------------------------ windows

class BrowseWindow(xbmcgui.WindowXML):
    """Shared lifecycle: top-bar title, guarded clicks, focus kept across child windows."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.title = ""
        self.initialized = False
        self.last_focus = None

    def onInit(self):
        if self.initialized:
            # Kodi re-runs onInit when a child window closes.
            if self.last_focus:
                self.setFocusId(self.last_focus)
            return
        self.initialized = True
        self.set_title(self.title)
        self.setProperty("silo.empty", "")
        self.start()

    def start(self):
        raise NotImplementedError

    def set_title(self, title):
        self.setProperty("silo.title", title)
        if title:
            width = pill_width(title)
            pill = self.getControl(TITLE_PILL)
            pill.setWidth(width)
            pill.setPosition((SCREEN_WIDTH - width) // 2, 2)

    def focus_id(self):
        try:
            return self.getFocusId()
        except RuntimeError:
            return 0

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        pass

    def open_card(self, control_id, item, opener):
        self.last_focus = control_id
        try:
            opener(item)
        finally:
            self.setFocusId(control_id)


class RowsWindow(BrowseWindow):
    """Rows of cards with an optional hero; ``loader()`` returns the rows.

    A row is {"title", "items", "wide", "cursor", "fetch", "caption", "kind"}; ``fetch(cursor)``
    returns the next page, ``caption(item)`` overrides the two caption lines and ``kind``
    "collection" makes cards collections rather than titles.
    """

    rows_top = HERO_ROWS_TOP

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.loader = None
        self.hero = False
        self.empty_text = "Nothing to show"
        self.error_text = "This screen could not be loaded."
        self.open_collection = None
        self.rows = []
        self.row = 0
        self.show_plot = True
        self.hero_debouncer = Debouncer(self.update_hero)

    def close(self):
        self.hero_debouncer.cancel()
        super().close()

    def start(self):
        application = self.application
        self.show_plot = bool((application.guard(application.options) or {}).get("show_plot", True))
        self.setProperty("silo.hero", "true" if self.hero else "")
        self.prepare_header()
        self.load_rows()

    def prepare_header(self):
        pass

    def load_rows(self):
        rows = self.application.guard(self.loader)
        self.set_rows(rows, self.error_text if rows is None else self.empty_text)

    def set_rows(self, rows, empty_text):
        self.rows = [row for row in rows or [] if row.get("items")][:SLOTS]
        for index in range(SLOTS):
            self.fill_slot(index, self.rows[index] if index < len(self.rows) else None)
        self.row = 0
        self.layout_rows()
        self.setProperty("silo.empty", "" if self.rows else empty_text)
        if self.rows:
            self.focus_row(0)
            if self.hero:
                self.show_hero(self.rows[0]["items"][0])
        else:
            self.focus_header()

    def list_items(self, row, items):
        if row.get("kind") == "collection":
            return [collection_list_item(item, self.application.image) for item in items]
        result = []
        for item in items:
            entry = card_list_item(item, self.application.image, row.get("wide", False))
            if row.get("caption"):
                first, second = row["caption"](item)
                entry.setLabel(first)
                entry.setLabel2(second)
            result.append(entry)
        return result

    def fill_slot(self, index, row):
        group, header, icon, wide_list, poster_list = slot_ids(index)
        if row is None:
            self.getControl(group).setVisible(False)
            self.getControl(wide_list).reset()
            self.getControl(poster_list).reset()
            return
        wide = bool(row.get("wide"))
        shown, hidden = (wide_list, poster_list) if wide else (poster_list, wide_list)
        self.getControl(hidden).reset()
        self.getControl(hidden).setVisible(False)
        control = self.getControl(shown)
        control.reset()
        control.addItems(self.list_items(row, row["items"]))
        control.setVisible(True)
        self.getControl(header).setLabel(str(row.get("title") or ""))
        self.getControl(header).setPosition(148 if wide else 88, 0)
        self.getControl(icon).setVisible(wide)

    def row_list(self, index):
        return slot_ids(index)[3 if self.rows[index].get("wide") else 4]

    def row_of(self, control_id):
        offset = control_id - SLOT_BASE
        if offset < 0 or offset % 10 not in (3, 4):
            return None
        index = offset // 10
        return index if index < len(self.rows) else None

    def layout_rows(self):
        """Put the focused row at the top of the rows area; rows above it are hidden."""
        top = self.rows_top
        for index in range(SLOTS):
            group = self.getControl(slot_ids(index)[0])
            if index >= len(self.rows) or index < self.row or top >= SCREEN_HEIGHT:
                group.setVisible(False)
                continue
            group.setPosition(0, top)
            group.setVisible(True)
            top += WIDE_PITCH if self.rows[index].get("wide") else POSTER_PITCH

    def focus_row(self, index):
        self.row = index
        self.layout_rows()
        self.setFocusId(self.row_list(index))
        if self.hero:
            self.hero_debouncer.trigger()

    def focus_header(self):
        """Up from the first row; screens with header controls override this."""
        return False

    def selected(self, control_id, items):
        position = self.getControl(control_id).getSelectedPosition()
        return items[position] if 0 <= position < len(items) else None

    def update_hero(self):
        """Debounced: show the focused card in the hero; keep the last one otherwise."""
        index = self.row_of(self.focus_id())
        if index is None:
            return
        item = self.selected(self.row_list(index), self.rows[index]["items"])
        if item is not None:
            self.application.guard(lambda: self.show_hero(item))

    def show_hero(self, item):
        hero = present.hero(item, self.application.image, self.show_plot)
        set_properties(self, {
            "silo.hero.backdrop": hero["backdrop"],
            "silo.hero.logo": hero["logo"],
            "silo.hero.title": hero["title"],
            "silo.hero.meta": hero["meta"],
            "silo.hero.synopsis": hero["synopsis"],
        })
        left = HERO_LEFT
        for slot, (image_id, label_id) in enumerate(zip(BADGE_IMAGES, BADGE_LABELS)):
            text = hero["badges"][slot] if slot < len(hero["badges"]) else ""
            self.setProperty("silo.hero.badge{}".format(slot + 1), text)
            if text:
                width = present.badge_width(text)
                for control_id in (image_id, label_id):
                    self.getControl(control_id).setPosition(left, BADGE_TOP)
                    self.getControl(control_id).setWidth(width)
                left += width + BADGE_GAP
        self.getControl(HERO_META).setPosition(left, BADGE_TOP)
        self.getControl(HERO_CONTENT).setPosition(0, 0 if hero["synopsis"] else NO_SYNOPSIS_SHIFT)

    def load_more(self, index):
        """Fetch a row's next page when the selection nears its end."""
        row = self.rows[index]
        if not row.get("cursor") or not row.get("fetch"):
            return
        control = self.getControl(self.row_list(index))
        if control.getSelectedPosition() < len(row["items"]) - LOAD_AHEAD:
            return
        cursor, row["cursor"] = row["cursor"], None  # One request at a time; a failure is not retried per key.
        page = row["fetch"](cursor)
        if not page:
            return
        items = prepare(self.application, page.get("items"))
        row["items"].extend(items)
        row["cursor"] = page.get("next_cursor") or None
        control.addItems(self.list_items(row, items))

    # ------------------------------------------------------------ input

    def onFocus(self, control_id):
        if self.hero and self.row_of(control_id) is not None:
            self.hero_debouncer.trigger()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.close()
            return
        focus = self.focus_id()
        row = self.row_of(focus)
        if row is None:
            self.header_action(focus, action_id)
            return
        if action_id == ACTION_UP:
            if row > 0:
                self.focus_row(row - 1)
            else:
                self.focus_header()
        elif action_id == ACTION_DOWN:
            if row + 1 < len(self.rows):
                self.focus_row(row + 1)
        elif action_id in (ACTION_LEFT, ACTION_RIGHT):
            if self.hero:
                self.hero_debouncer.trigger()
            self.application.guard(lambda: self.load_more(row))

    def header_action(self, focus, action_id):
        pass

    def _click(self, control_id):
        index = self.row_of(control_id)
        if index is None:
            self.header_click(control_id)
            return
        row = self.rows[index]
        item = self.selected(control_id, row["items"])
        if item is None:
            return
        if row.get("kind") == "collection":
            self.open_card(control_id, item, self.open_collection)
        else:
            self.open_card(control_id, item, self.application.open_item)

    def header_click(self, control_id):
        pass


class CalendarWindow(RowsWindow):
    """Upcoming episodes and movies by day for one week; Left/Right on the week control change it."""

    rows_top = CALENDAR_ROWS_TOP

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.today = today()
        self.week = week_start(self.today)
        self.filter = "following"
        self.timezone = ""

    def prepare_header(self):
        self.setProperty("silo.header", "true")
        saved = self.store_get("calendar_filter")
        if saved in dict(CALENDAR_FILTERS):
            self.filter = saved
        self.timezone = local_timezone()
        self.loader = self.load_week

    def store_get(self, key):
        try:
            return self.application.store.get(key)
        except (AttributeError, TypeError):
            return None

    def update_header(self):
        name = dict(CALENDAR_FILTERS)[self.filter]
        self.getControl(FILTER_CHIP).setLabel("Show: " + name)
        self.getControl(WEEK_BUTTON).setLabel("‹   {}   ›".format(week_label(self.week)))
        hint = relative_week(self.week, self.today)
        self.setProperty("silo.week_hint", " · ".join(part for part in (hint, "Left and right change the week")
                                                      if part))

    def load_week(self):
        self.update_header()
        end = self.week + datetime.timedelta(days=6)
        days = svc_browse.calendar(self.application.client, self.week.isoformat(), end.isoformat(),
                                   filter=self.filter, timezone=self.timezone or None)
        rows = []
        for day in days:
            items = prepare(self.application, day["items"])
            if items:
                rows.append({"title": day_heading(day["date"], self.today), "items": items,
                             "caption": calendar_caption})
        return rows

    def reload(self):
        rows = self.application.guard(self.load_week)
        self.set_rows(rows, "The calendar could not be loaded." if rows is None else "Nothing scheduled this week")
        if self.rows:
            self.setFocusId(WEEK_BUTTON)

    def load_rows(self):
        self.empty_text = "Nothing scheduled this week"
        self.error_text = "The calendar could not be loaded."
        super().load_rows()

    def focus_header(self):
        self.setFocusId(WEEK_BUTTON)
        return True

    def move_week(self, weeks):
        self.week += datetime.timedelta(days=7 * weeks)
        self.reload()

    def header_action(self, focus, action_id):
        if focus == WEEK_BUTTON:
            if action_id == ACTION_LEFT:
                self.move_week(-1)
            elif action_id == ACTION_RIGHT:
                self.move_week(1)
            elif action_id == ACTION_UP:
                self.setFocusId(FILTER_CHIP)
            elif action_id == ACTION_DOWN and self.rows:
                self.focus_row(0)
        elif focus == FILTER_CHIP and action_id == ACTION_DOWN:
            self.setFocusId(WEEK_BUTTON)

    def header_click(self, control_id):
        if control_id == FILTER_CHIP:
            values = [value for value, _name in CALENDAR_FILTERS]
            current = values.index(self.filter)
            choice = self.application.dialog.select("Show", [name for _value, name in CALENDAR_FILTERS],
                                                    preselect=current)
            if choice < 0 or choice == current:
                return
            self.filter = values[choice]
            try:
                self.application.store.set("calendar_filter", self.filter)
            except (AttributeError, TypeError):
                pass
            self.reload()
            self.setFocusId(FILTER_CHIP)
        elif control_id == WEEK_BUTTON and self.week != week_start(self.today):
            self.week = week_start(self.today)
            self.reload()


class PersonWindow(RowsWindow):
    """A person's photo, facts and biography over their movies and series."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.person_id = None
        self.name = ""
        self.bio = ""

    def load_rows(self):
        application = self.application
        client = application.client
        person = application.guard(lambda: svc_browse.person(client, self.person_id))
        data = person or {}
        self.name = data.get("name") or self.name
        self.bio = data.get("bio") or ""
        set_properties(self, {
            "silo.name": self.name,
            "silo.initials": present.initials(self.name),
            "silo.photo": application.image({"photo_url": data.get("photo_url")}, "photo") if data else "",
            "silo.facts": person_facts(data),
            "silo.bio": self.bio,
            "silo.bio_more": "true" if len(self.bio) > BIO_PREVIEW else "",
        })
        self.loader = self.credits
        self.empty_text = "No titles with {} in your library".format(self.name) if self.name else "No titles"
        self.error_text = "This person's titles could not be loaded."
        super().load_rows()

    def credits(self):
        client, size = self.application.client, page_size(self.application)
        rows = []
        for kind, title in (("movie", "Movies"), ("series", "Series")):
            def fetch(cursor, kind=kind):
                return svc_browse.person_credits(client, self.person_id, kind, cursor, size)

            page = fetch(None)
            rows.append({"title": title, "items": prepare(self.application, page["items"]),
                         "cursor": page["next_cursor"], "fetch": fetch})
        return rows

    def focus_header(self):
        if len(self.bio) > BIO_PREVIEW:
            self.setFocusId(READ_MORE)
            return True
        return False

    def header_action(self, focus, action_id):
        if focus == READ_MORE and action_id == ACTION_DOWN and self.rows:
            self.focus_row(0)

    def header_click(self, control_id):
        if control_id == READ_MORE:
            self.application.dialog.textviewer(self.name or "Biography", self.bio)


class GridWindow(BrowseWindow):
    """A poster grid with an optional heading, overview and backdrop.

    ``fetch(cursor)`` returns {"items", "next_cursor", "total"}; ``kind`` "collection" shows
    collections; ``caption(item)`` overrides the second caption line; ``remove(item)``
    enables the context menu's ``remove_label`` entry.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.heading = ""
        self.meta = ""
        self.overview = ""
        self.backdrop = ""
        self.fetch = None
        self.kind = "item"
        self.caption = None
        self.remove = None
        self.remove_label = "Remove"
        self.confirm_remove = False
        self.refresh_on_return = False
        self.empty_text = "No titles to show"
        self.error_text = "Titles could not be loaded."
        self.open_collection = None
        self.items = []
        self.cursor = None

    def start(self):
        set_properties(self, {"silo.heading": self.heading, "silo.meta": self.meta,
                              "silo.overview": self.overview, "silo.backdrop": self.backdrop})
        top = GRID_TOP_OVERVIEW if self.overview else GRID_TOP_HEADING if self.heading else GRID_TOP_PLAIN
        grid = self.getControl(GRID)
        grid.setPosition(GRID_LEFT, top)
        grid.setHeight(SCREEN_HEIGHT - top)
        self.load()
        self.setFocusId(GRID)

    def list_items(self, items):
        if self.kind == "collection":
            return [collection_list_item(item, self.application.image) for item in items]
        result = []
        for item in items:
            entry = card_list_item(item, self.application.image)
            if self.caption:
                entry.setLabel2(self.caption(item))
            result.append(entry)
        return result

    def load(self, position=0):
        self.items, self.cursor = [], None
        self.getControl(GRID).reset()
        page = self.application.guard(lambda: self.fetch(None))
        if page is None:
            self.setProperty("silo.empty", self.error_text)
            return
        self.append(page)
        if self.items:
            self.getControl(GRID).selectItem(max(0, min(position, len(self.items) - 1)))

    def append(self, page):
        items = page.get("items") or []
        items = items if self.kind == "collection" else prepare(self.application, items)
        self.items.extend(items)
        self.getControl(GRID).addItems(self.list_items(items))
        self.cursor = page.get("next_cursor") or None
        if self.heading and self.kind != "collection" and not self.meta and isinstance(page.get("total"), int):
            self.setProperty("silo.meta", count_text(page["total"]))
        self.setProperty("silo.empty", "" if self.items else self.empty_text)

    def load_more(self):
        if not self.cursor:
            return
        if self.getControl(GRID).getSelectedPosition() < len(self.items) - LOAD_AHEAD:
            return
        cursor, self.cursor = self.cursor, None
        page = self.fetch(cursor)
        if page:
            self.append(page)

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.close()
        elif action_id == ACTION_CONTEXT_MENU and self.focus_id() == GRID:
            self.application.guard(self.context_menu)
        elif self.focus_id() == GRID and action_id in (ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT):
            self.application.guard(self.load_more)

    def context_menu(self):
        if self.remove is None:
            return
        position = self.getControl(GRID).getSelectedPosition()
        if not 0 <= position < len(self.items):
            return
        item = self.items[position]
        if self.application.dialog.contextmenu([self.remove_label]) != 0:
            return
        if self.confirm_remove and not self.application.dialog.yesno(
                self.remove_label, "Remove {} from your history?".format(present.card_title(item))):
            return
        self.remove(item)
        del self.items[position]
        grid = self.getControl(GRID)
        grid.reset()
        grid.addItems(self.list_items(self.items))
        if self.items:
            grid.selectItem(min(position, len(self.items) - 1))
        self.setProperty("silo.empty", "" if self.items or self.cursor else self.empty_text)

    def _click(self, control_id):
        if control_id != GRID:
            return
        position = self.getControl(GRID).getSelectedPosition()
        if not 0 <= position < len(self.items):
            return
        if self.kind == "collection":
            self.open_card(GRID, self.items[position], self.open_collection)
            return
        self.open_card(GRID, self.items[position], self.application.open_item)
        if self.refresh_on_return:
            # The details page can change the list (unwatch, unfavorite); show it as it is now.
            self.load(position)


# ------------------------------------------------------------------ screens

def open_for_you(application):
    """For You: the profile's recommendation rows with a hero that follows focus."""
    client = application.client
    open_window(application, RowsWindow, "SiloBrowseRows.xml", title="For You", hero=True,
                loader=lambda: [dict(row, items=prepare(application, row["items"]), wide=False)
                                for row in svc_browse.discover(client)],
                empty_text="No recommendations yet. Watch a few titles and check back.",
                error_text="Recommendations could not be loaded.")


def open_calendar(application):
    """Calendar: this week's episodes and movies by day."""
    open_window(application, CalendarWindow, "SiloBrowseRows.xml", title="Calendar")


def collection_opener(application, library):
    return lambda collection: open_collection(application, collection, library)


def open_collections(application, library=None):
    """Collections: the profile's own, or a library's Collections tab with its groups as rows."""
    client = application.client
    library_id = library.get("id") if isinstance(library, dict) else None
    title = "Collections"
    if library_id is not None:
        groups = application.guard(lambda: svc_browse.library_collections(client, library_id))
    else:
        groups = application.guard(lambda: svc_browse.collections(client))
    opener = collection_opener(application, library)
    empty = "No collections yet" if groups is not None else "Collections could not be loaded."
    if groups and len(groups) > 1:
        rows = [{"title": group["title"], "items": group["collections"], "kind": "collection"} for group in groups]
        open_window(application, RowsWindow, "SiloBrowseRows.xml", title=title, loader=lambda: rows,
                    open_collection=opener, empty_text=empty)
        return
    collections = groups[0]["collections"] if groups else []
    heading = str(library.get("name") or "") if library_id is not None else ""
    open_window(application, GridWindow, "SiloBrowseGrid.xml", title=title, heading=heading, kind="collection",
                fetch=lambda cursor: None if groups is None else {"items": collections},
                open_collection=opener, empty_text=empty, error_text=empty)


def open_collection(application, collection, library=None):
    """One collection: its title, description and art over a poster grid of its titles."""
    library_id = library.get("id") if isinstance(library, dict) else None
    card = collection if isinstance(collection, dict) and collection.get("kind") else None
    if card is None:
        guess = "library" if library_id is not None or (collection or {}).get("library_id") else "personal"
        card = svc_browse.collection_card(collection, guess, library_id)
    if card is None:
        return
    client, size = application.client, page_size(application)
    open_window(application, GridWindow, "SiloBrowseGrid.xml", title="Collection", heading=card["title"],
                meta=count_text(card.get("count")), overview=card.get("overview") or "",
                backdrop=application.image(card, "backdrop"),
                fetch=lambda cursor: svc_browse.collection_items(client, card, cursor, size, library_id),
                empty_text="This collection is empty", error_text="This collection could not be loaded.")


def open_library_sections(application, library):
    """A library's Recommended rows (its section layout) with a hero that follows focus."""
    client = application.client
    open_window(application, RowsWindow, "SiloBrowseRows.xml", title=str(library.get("name") or "Recommended"),
                hero=True,
                loader=lambda: [dict(row, items=prepare(application, row["items"]),
                                     wide=present.is_wide_section(row))
                                for row in svc_browse.library_sections(client, library["id"])],
                empty_text="Nothing to recommend in this library yet",
                error_text="This library could not be loaded.")


def open_personal(application, kind):
    """Watchlist, Favorites or History as a poster grid; the context menu removes a title."""
    if kind not in PERSONAL:
        raise ValueError("Unknown personal list: {}".format(kind))
    title, empty, remove_label = PERSONAL[kind]
    client, size = application.client, page_size(application)
    reader = getattr(svc_personal, kind)
    removers = {"watchlist": lambda item: svc_personal.set_watchlist(client, item["content_id"], False),
                "favorites": lambda item: svc_personal.set_favorite(client, item["content_id"], False),
                "history": lambda item: svc_browse.remove_history(client, item["content_id"])}
    open_window(application, GridWindow, "SiloBrowseGrid.xml", title=title,
                fetch=lambda cursor: reader(client, cursor, size),
                caption=watched_caption if kind == "history" else None,
                remove=removers[kind], remove_label=remove_label, confirm_remove=kind == "history",
                refresh_on_return=kind != "history", empty_text=empty,
                error_text="{} could not be loaded.".format(title))


def open_person(application, person_id, name=""):
    """A person: photo, facts, biography and their movies and series in this server's libraries."""
    open_window(application, PersonWindow, "SiloPerson.xml", person_id=person_id, name=str(name or ""))
