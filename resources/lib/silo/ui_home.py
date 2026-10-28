"""The home screen: top bar, hero and rows, library grids and search."""

import json
from urllib.parse import quote

import xbmcgui

from . import present, svc_personal
from .ui_common import (ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer,
                        card_list_item, set_properties)

# Control ids; scripts/skin_home.py writes the matching SiloHome.xml.
TOP_BAR, TAB_CLUSTER, SEARCH_BUTTON, AVATAR = 200, 202, 201, 230
# (tab, button, group, width) in Android order; the cluster is centred with a search-sized spacer
# after the last shown tab. Library tabs need a library, Requests needs the server feature.
TABS = (("home", 210, 215, 124), ("movies", 211, 216, 148), ("series", 212, 217, 140),
        ("for_you", 203, 206, 156), ("calendar", 204, 207, 172), ("requests", 205, 208, 172))
TAB_KINDS = {"movies": "movie", "series": "series"}
TAB_BUTTONS = {tab: button for tab, button, _group, _width in TABS}
LIBRARY_TABS = {TAB_BUTTONS["movies"]: "movie", TAB_BUTTONS["series"]: "series"}
# Tabs that open another screen modally; Home stays the selected tab.
SCREEN_TABS = {TAB_BUTTONS["for_you"]: "for_you", TAB_BUTTONS["calendar"]: "calendar",
               TAB_BUTTONS["requests"]: "requests"}
SEARCH_SIZE, SEARCH_GAP, TAB_GAP, SCREEN_WIDTH, SCREEN_HEIGHT = 56, 24, 8, 1920, 1080
# The add-on's name sits at x 88 in font36_title (about 380 px wide); the tab group
# stays centred but never starts closer to the name than this.
TABS_MIN_LEFT = 88 + 380 + 40
BAR_CONTROLS = frozenset([SEARCH_BUTTON, AVATAR] + [tab[1] for tab in TABS])

HOME_GROUP, HERO_GROUP, HERO_META = 300, 400, 430
BADGE_IMAGES, BADGE_LABELS = (411, 412, 413, 414), (421, 422, 423, 424)
HERO_LEFT, BADGE_TOP, BADGE_GAP, NO_SYNOPSIS_SHIFT = 88, 346, 12, 84

# Row slot k: group SLOT_BASE + 10k, header +1, play icon +2, wide list +3, poster list +4.
SLOTS, SLOT_BASE = 12, 3000
ROWS_TOP, WIDE_PITCH, POSTER_PITCH = 530, 410, 470

BROWSE_GROUP, LIBRARY_GROUP, SEARCH_GROUP, GRID = 700, 710, 800, 500
LIBRARY_CHIP, SORT_CHIP, SEARCH_FIELD = 510, 511, 520
FILTER_CHIP, CLEAR_CHIP, SHUFFLE_CHIP = 516, 517, 518
FILTER_BADGE = 525  # group with the count label 526
CHIPS = (LIBRARY_CHIP, SORT_CHIP, FILTER_CHIP, CLEAR_CHIP, SHUFFLE_CHIP)
CHEVRONS = {LIBRARY_CHIP: (512, 513), SORT_CHIP: (514, 515)}
CHIP_TOP, CHIP_GAP, BADGE_SIZE = 188, 16, 32
GRID_LEFT, LIBRARY_GRID_TOP, SEARCH_GRID_TOP = 40, 270, 370
SEARCH_CHIPS = {530: "video", 531: "movie", 532: "series"}
LOAD_AHEAD = 12
# (sort value, dialog label, chip label); values are the server's catalog sort fields.
SORTS = (("title", "Title A–Z", "Title"), ("-added_at", "Recently added", "Recently added"),
         ("-release_date", "Release date", "Release date"), ("-year", "Year", "Year"),
         ("-rating_imdb", "Rating", "Rating"), ("-runtime", "Runtime", "Runtime"),
         ("-date_viewed", "Recently watched", "Recently watched"), ("-plays", "Most played", "Most played"))

# A–Z jump rail beside the grid while it is sorted by title (Android TvAlphabetRail).
RAIL, RAIL_GROUP = 560, 565
RAIL_LETTERS = ("All", "#") + tuple(chr(code) for code in range(ord("A"), ord("Z") + 1))

# Search: a People row above the title grid.
PEOPLE_GROUP, PEOPLE_LIST = 575, 570
PEOPLE_HEIGHT, PEOPLE_LIMIT = 290, 20

# Facet filters in Android TvCatalogFacet order. Decade, dynamic range and watch status are fixed
# vocabularies; the others come from GET /catalog/filters.
FACETS = (("genre", "Genre"), ("decade", "Decade"), ("watch_status", "Watch status"),
          ("content_rating", "Content rating"), ("resolution", "Resolution"),
          ("dynamic_range", "Dynamic range"), ("studio", "Studio"), ("network", "Network"),
          ("country", "Country"))
FACET_SOURCES = {"genre": "genres", "content_rating": "content_ratings", "studio": "studios",
                 "network": "networks", "country": "countries"}
# (facet, rule field, operator) for facets that lower to one "any" group of value rules.
VALUE_RULES = (("genre", "genre", "contains"), ("studio", "studio", "is"), ("network", "network", "is"),
               ("country", "country", "is"), ("content_rating", "content_rating", "is"),
               ("resolution", "resolution", "is"))
DECADES = (2020, 2010, 2000, 1990, 1980, 1970, 1960)
DYNAMIC_RANGES = (("hdr", "HDR"), ("dolby_vision", "Dolby Vision"))
# (value, label, (rule field, rule value)); watch status is single-select.
WATCH_STATUSES = (("unwatched", "Unwatched", ("watched", False)), ("in_progress", "In progress", ("in_progress", True)),
                  ("watched", "Watched", ("watched", True)), ("favorited", "Favorited", ("favorited", True)),
                  ("watchlist", "Watchlist", ("in_watchlist", True)))
# Long server lists offer a search first (GET /catalog/filters/search).
SEARCHABLE_FACETS = frozenset(["genre", "content_rating", "studio", "network", "country"])
FACET_SEARCH_MIN, FACET_SEARCH_LIMIT = 60, 100

# Library tab dropdown (Android TvLibraryPill): Recommended / Browse / Collections.
PILL, PILL_PANEL = 640, 641
PILL_ITEMS = {642: "recommended", 643: "browse", 644: "collections"}
PILL_TOP, PILL_WIDTH = 128, 320

MENU, MENU_PANEL = 600, 601
# Avatar menu in Android order.
MENU_ORDER = (610, 613, 614, 615, 611, 616, 617, 612)
MENU_ITEMS = {610: "profile", 613: "watchlist", 614: "favorites", 615: "history", 611: "settings",
              616: "home_rows", 617: "server", 612: "logout"}
MENU_RESULTS = {610: "profile", 611: "settings", 617: "server", 612: "logout"}
MENU_PERSONAL = {613: "watchlist", 614: "favorites", 615: "history"}
MENU_ROW = 72

# Kodi actions that open a card's context menu: ContextMenu (the Menu key; the default keymap
# also sends it for a long press of OK), a right click and a long mouse click.
CONTEXT_ACTIONS = frozenset([117, 101, 108])
HOME_ROWS_KEY = "home_rows"


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3, base + 4


def chip_width(text, chevron=True):
    """Estimated chip width: text at the 25px bold tab font plus padding and chevron."""
    return 28 + 14 * len(text) + (12 + 24 if chevron else 0) + 28


def total_text(total):
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return ""
    total = int(total)
    return "{:,} title{}".format(total, "" if total == 1 else "s")


def filter_groups(filters):
    """Lower facet selections to /catalog rule groups, as Android's TvCatalogFacetSelection does."""
    groups = []
    for facet, field, op in VALUE_RULES:
        values = sorted(filters.get(facet) or ())
        if values:
            groups.append({"match": "any", "rules": [{"field": field, "op": op, "value": value}
                                                     for value in values]})
    decades = sorted(int(value) for value in filters.get("decade") or ())
    if decades:
        groups.append({"match": "any", "rules": [{"field": "decade", "op": "is", "value": decade}
                                                 for decade in decades]})
    ranges = [value for value, _label in DYNAMIC_RANGES if value in (filters.get("dynamic_range") or ())]
    if ranges:
        groups.append({"match": "any", "rules": [{"field": value, "op": "is", "value": True} for value in ranges]})
    status = next((rule for value, _label, rule in WATCH_STATUSES
                   if value in (filters.get("watch_status") or ())), None)
    if status:
        groups.append({"match": "all", "rules": [{"field": status[0], "op": "is", "value": status[1]}]})
    return groups


def active_facets(filters):
    return sum(1 for facet, _label in FACETS if filters.get(facet))


def apply_row_prefs(sections, prefs):
    """Order and hide home sections by the saved editor choices; new rows keep server order after them."""
    prefs = prefs if isinstance(prefs, dict) else {}
    hidden = set(str(value) for value in prefs.get("hidden") or ())
    rank = {str(section_id): index for index, section_id in enumerate(prefs.get("order") or ())}
    known = sorted((section for section in sections if str(section.get("id")) in rank),
                   key=lambda section: rank[str(section.get("id"))])
    rest = [section for section in sections if str(section.get("id")) not in rank]
    return [section for section in known + rest if str(section.get("id")) not in hidden]


class HomeWindow(xbmcgui.WindowXML):
    """Silo's home window; ``result`` is "profile", "settings", "server", "logout" or None."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.result = None
        self.initialized = False
        self.tab = "home"
        self.sections = []
        self.all_sections = []
        self.row = 0
        self.show_plot = True
        self.hide_spoilers = False
        self.libraries = {"movie": [], "series": []}
        self.requests = False
        self.tab_lefts = {}
        self.cluster_left = 0
        self.menu_open = False
        self.pill_open = None
        self.last_focus = None
        self.home_stale = False
        self.kind = "movie"
        self.library = None
        self.sort = "title"
        self.reversed = False
        self.filters = {}
        self.filter_options_cache = {}
        self.letters = None
        self.query = ""
        self.search_type = "video"
        self.people = []
        self.params = None
        self.grid_items = []
        self.cursor = None
        self.generation = 0
        self.series_art_cache = {}
        self.hero_debouncer = Debouncer(self.update_hero)

    # ------------------------------------------------------------ lifecycle

    def onInit(self):
        if self.initialized:
            # Kodi re-runs onInit when a child window closes.
            if self.last_focus:
                self.setFocusId(self.last_focus)
            return
        self.initialized = True
        application = self.application
        options = application.guard(application.options) or {}
        self.show_plot = bool(options.get("show_plot", True))
        self.hide_spoilers = bool(options.get("hide_spoilers", False))
        set_properties(self, {
            "silo.initials": present.initials(application.profile_name),
            "silo.avatar": present.avatar_image(application.profile_avatar),
            "silo.profile_locked": "true" if self.locked() else "",
            "silo.menu": "",
            "silo.pill": "",
            "silo.rail": "",
            "silo.people": "",
            "silo.filter_count": "",
            "silo.search_type": self.search_type,
        })
        self.getControl(MENU).setVisible(False)
        self.getControl(PILL).setVisible(False)
        for kind in self.libraries:
            self.libraries[kind] = application.guard(lambda kind=kind: application.video_libraries(kind)) or []
        self.requests = application.guard(application.requests_enabled) is True
        self.layout_tabs()
        self.fill_rail()
        application.guard(self.load_home)
        self.show_home()

    def close(self):
        self.hero_debouncer.cancel()
        super().close()

    def locked(self):
        return bool(self.application.store.get("temporary_session"))

    def focus_id(self):
        try:
            return self.getFocusId()
        except RuntimeError:
            return 0

    # ------------------------------------------------------------ top bar

    def tab_shown(self, tab):
        if tab in TAB_KINDS:
            return bool(self.libraries[TAB_KINDS[tab]])
        if tab == "requests":
            return self.requests
        return True

    def layout_tabs(self):
        """Hide tabs without content and keep the tab group centred."""
        chain, left = [SEARCH_BUTTON], SEARCH_SIZE + SEARCH_GAP
        self.tab_lefts = {}
        for tab, button, group, width in TABS:
            shown = self.tab_shown(tab)
            self.getControl(group).setVisible(shown)
            self.getControl(button).setVisible(shown)
            if shown:
                self.getControl(group).setPosition(left, 2)
                self.tab_lefts[tab] = left
                left += width + TAB_GAP
                chain.append(button)
        chain.append(AVATAR)
        width = left - TAB_GAP + SEARCH_GAP + SEARCH_SIZE
        self.cluster_left = max((SCREEN_WIDTH - width) // 2, TABS_MIN_LEFT)
        self.getControl(TAB_CLUSTER).setPosition(self.cluster_left, 0)
        for before, after in zip(chain, chain[1:]):
            self.getControl(before).controlRight(self.getControl(after))
            self.getControl(after).controlLeft(self.getControl(before))

    def set_tab(self, tab):
        self.tab = tab
        self.generation += 1  # Pages still loading for the previous tab are dropped.
        self.setProperty("silo.tab", tab)
        # Python visibility applies at once, so focus can move in the same call.
        self.getControl(HOME_GROUP).setVisible(tab == "home")
        self.getControl(BROWSE_GROUP).setVisible(tab != "home")
        self.getControl(LIBRARY_GROUP).setVisible(tab in TAB_KINDS)
        self.getControl(SEARCH_GROUP).setVisible(tab == "search")
        self.update_rail()
        if tab != "search":
            self.show_people([])

    def select_home_tab(self):
        self.set_tab("home")
        if self.home_stale:
            self.home_stale = False
            self.application.guard(self.load_home)

    def focus_tab(self):
        self.setFocusId(TAB_BUTTONS.get(self.tab, SEARCH_BUTTON))

    def focus_content(self):
        if self.tab == "home":
            if self.sections:
                self.focus_row(self.row)
        elif self.tab == "search":
            self.setFocusId(SEARCH_FIELD)
        else:
            self.setFocusId(LIBRARY_CHIP)

    def open_screen_tab(self, control_id):
        """For You, Calendar and Requests open their own screens; Home is the selected tab afterwards."""
        name = SCREEN_TABS[control_id]
        if self.tab != "home":
            self.select_home_tab()
        self.run_modal(lambda: self.application.open_screen(name), control_id)

    # ------------------------------------------------------------ library dropdown

    def open_pill(self, kind):
        tab = "movies" if kind == "movie" else "series"
        self.pill_open = kind
        left = self.cluster_left + self.tab_lefts.get(tab, 0)
        self.getControl(PILL).setPosition(min(left, SCREEN_WIDTH - PILL_WIDTH - 40), PILL_TOP)
        self.setProperty("silo.pill", kind)
        self.getControl(PILL).setVisible(True)
        self.setFocusId(min(PILL_ITEMS))

    def close_pill(self, focus=True):
        kind, self.pill_open = self.pill_open, None
        self.setProperty("silo.pill", "")
        self.getControl(PILL).setVisible(False)
        if focus and kind:
            self.setFocusId(TAB_BUTTONS["movies" if kind == "movie" else "series"])

    def choose_pill(self, control_id):
        kind = self.pill_open
        if kind is None:
            return
        choice = PILL_ITEMS[control_id]
        self.close_pill()
        tab_button = TAB_BUTTONS["movies" if kind == "movie" else "series"]
        if choice == "browse":
            self.show_library(kind)
            self.setFocusId(LIBRARY_CHIP)
            return
        libraries = self.libraries.get(kind) or []
        if choice == "recommended":
            library = self.pick_library(kind, libraries, allow_all=False)
            if library is not None:
                self.run_modal(lambda: self.application.open_screen("library_sections", library=library),
                               tab_button)
        else:
            library = self.pick_library(kind, libraries, allow_all=True)
            if library is not None:
                chosen = library or None
                self.run_modal(lambda: self.application.open_screen("collections", library=chosen), tab_button)

    def pick_library(self, kind, libraries, allow_all):
        """One library of the kind, asking when there are several; None when cancelled, {} for all."""
        if not libraries:
            return None
        if len(libraries) == 1:
            return libraries[0]
        names = [str(library.get("name") or "Library") for library in libraries]
        if allow_all:
            names = ["All libraries"] + names
        choice = self.application.dialog.select("Movies" if kind == "movie" else "Series", names)
        if not isinstance(choice, int) or not 0 <= choice < len(names):
            return None
        if allow_all:
            return libraries[choice - 1] if choice else {}
        return libraries[choice]

    # ------------------------------------------------------------ avatar menu

    def open_menu(self):
        locked = self.locked()
        self.setProperty("silo.profile_locked", "true" if locked else "")
        self.getControl(610).setVisible(not locked)
        shown = [control for control in MENU_ORDER if not (locked and control == 610)]
        for index, control in enumerate(shown):
            self.getControl(control).setPosition(16, 16 + MENU_ROW * index)
        self.getControl(shown[0]).controlUp(self.getControl(shown[0]))
        self.getControl(MENU_PANEL).setHeight(32 + MENU_ROW * len(shown) - 8)
        self.menu_open = True
        self.setProperty("silo.menu", "true")
        self.getControl(MENU).setVisible(True)
        self.setFocusId(shown[0])

    def close_menu(self, focus=True):
        self.menu_open = False
        self.setProperty("silo.menu", "")
        self.getControl(MENU).setVisible(False)
        if focus:
            self.setFocusId(AVATAR)

    def choose_menu(self, control_id):
        if control_id == 610 and self.locked():
            return
        if control_id in MENU_RESULTS:
            self.result = MENU_RESULTS[control_id]
            self.close()
        elif control_id in MENU_PERSONAL:
            self.close_menu()
            kind = MENU_PERSONAL[control_id]
            self.run_modal(lambda: self.application.open_screen("personal", kind=kind), AVATAR)
        elif control_id == 616:
            self.close_menu()
            self.edit_home_rows()

    # ------------------------------------------------------------ home rows

    def show_home(self):
        self.select_home_tab()
        if self.sections:
            self.focus_row(self.row)
        else:
            self.setFocusId(TABS[0][1])

    def rows_key(self):
        """"<server>|<profile id>", the key home row choices are saved under."""
        application = self.application
        profile = getattr(application, "profile", None)
        profile_id = profile.get("id") if isinstance(profile, dict) else None
        if profile_id in (None, ""):
            profile_id = application.store.get("profile_id")
        server = getattr(getattr(application, "client", None), "server_url", "")
        return "{}|{}".format(server if isinstance(server, str) else "", profile_id or "")

    def row_prefs(self):
        saved = self.application.store.get(HOME_ROWS_KEY)
        prefs = saved.get(self.rows_key()) if isinstance(saved, dict) else None
        return prefs if isinstance(prefs, dict) else {}

    def save_row_prefs(self, order, hidden):
        saved = self.application.store.get(HOME_ROWS_KEY)
        saved = dict(saved) if isinstance(saved, dict) else {}
        saved[self.rows_key()] = {"order": list(order), "hidden": list(hidden)}
        self.application.store.set(HOME_ROWS_KEY, saved)

    def load_home(self):
        sections = self.application.home_sections() or []
        self.all_sections = [section for section in sections if section.get("items")]
        self.apply_sections()

    def apply_sections(self):
        self.sections = apply_row_prefs(self.all_sections, self.row_prefs())[:SLOTS]
        for index in range(SLOTS):
            self.fill_slot(index, self.sections[index] if index < len(self.sections) else None)
        self.row = 0
        self.layout_rows()
        if self.sections:
            self.show_hero(self.sections[0]["items"][0])

    def fill_slot(self, index, section):
        group, header, icon, wide_list, poster_list = slot_ids(index)
        if section is None:
            self.getControl(group).setVisible(False)
            self.getControl(wide_list).reset()
            self.getControl(poster_list).reset()
            return
        wide = present.is_wide_section(section)
        shown, hidden = (wide_list, poster_list) if wide else (poster_list, wide_list)
        self.getControl(hidden).reset()
        self.getControl(hidden).setVisible(False)
        control = self.getControl(shown)
        control.reset()
        control.addItems([card_list_item(item, self.card_image, wide) for item in section["items"]])
        control.setVisible(True)
        self.getControl(header).setLabel(str(section.get("title") or ""))
        self.getControl(header).setPosition(148 if wide else 88, 0)
        self.getControl(icon).setVisible(wide)

    def is_wide(self, index):
        return present.is_wide_section(self.sections[index])

    def row_list(self, index):
        return slot_ids(index)[3 if self.is_wide(index) else 4]

    def row_of(self, control_id):
        offset = control_id - SLOT_BASE
        if offset < 0 or offset % 10 not in (3, 4):
            return None
        index = offset // 10
        return index if index < len(self.sections) else None

    def layout_rows(self):
        """Put the focused row at the top of the rows area; rows above it are hidden."""
        top = ROWS_TOP
        for index in range(SLOTS):
            group = self.getControl(slot_ids(index)[0])
            if index >= len(self.sections) or index < self.row or top >= SCREEN_HEIGHT:
                group.setVisible(False)
                continue
            group.setPosition(0, top)
            group.setVisible(True)
            top += WIDE_PITCH if self.is_wide(index) else POSTER_PITCH

    def focus_row(self, index):
        self.row = index
        self.layout_rows()
        self.setFocusId(self.row_list(index))
        self.hero_debouncer.trigger()

    def card_at(self, control_id):
        """(section id, content id, row, position) of the selected card in a home row, or None."""
        row = self.row_of(control_id)
        if row is None:
            return None
        position = self.getControl(control_id).getSelectedPosition()
        items = self.sections[row]["items"]
        item = items[position] if 0 <= position < len(items) else {}
        return self.sections[row].get("id"), item.get("content_id"), row, position

    def refresh_home(self, card=None):
        """Reload the rows (Android refreshes on resume); refocus the same card when it still exists."""
        sections = self.application.guard(self.application.home_sections)
        if sections is None:
            return False
        self.all_sections = [section for section in sections if section.get("items")]
        self.apply_sections()
        if card is None or not self.sections:
            return False
        section_id, content_id, old_row, old_position = card
        row = next((index for index, section in enumerate(self.sections) if section.get("id") == section_id), None)
        if row is None:
            row, position = min(old_row, len(self.sections) - 1), 0
        else:
            items = self.sections[row]["items"]
            position = next((index for index, item in enumerate(items) if item.get("content_id") == content_id),
                            min(old_position, len(items) - 1))
        self.focus_row(row)
        self.last_focus = self.row_list(row)  # Kodi's onInit after a child window refocuses this.
        self.getControl(self.row_list(row)).selectItem(position)
        item = self.sections[row]["items"][position]
        self.application.guard(lambda: self.show_hero(item))
        return True

    def update_hero(self):
        """Debounced: show the focused card in the hero; keep the last one otherwise."""
        index = self.row_of(self.focus_id())
        if index is None:
            return
        position = self.getControl(self.row_list(index)).getSelectedPosition()
        items = self.sections[index]["items"]
        if 0 <= position < len(items):
            self.application.guard(lambda: self.show_hero(items[position]))

    def show_hero(self, item):
        hero = present.hero(item, self.card_image, self.show_plot and not self.is_spoiler(item))
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
        # Without a synopsis the title block drops so the hero stays bottom-aligned.
        self.getControl(HERO_GROUP).setPosition(0, 0 if hero["synopsis"] else NO_SYNOPSIS_SHIFT)

    # ------------------------------------------------------------ spoilers

    def is_spoiler(self, item):
        return (self.hide_spoilers and isinstance(item, dict) and item.get("type") == "episode"
                and not svc_personal.state(item)["watched"])

    def card_image(self, item, kind):
        """Artwork for cards and the hero; unwatched episodes show series art when spoilers are hidden."""
        if kind not in ("poster", "backdrop") or not self.is_spoiler(item):
            return self.application.image(item, kind)
        # The card's own art is safe only when the server says it is not an episode still.
        for own in (kind, "backdrop" if kind == "poster" else "poster"):
            if item.get(own + "_is_episode_still") is False and item.get(own + "_url"):
                return self.application.image(item, own)
        series = self.series_art(item.get("series_id"))
        return self.application.image(series, kind) if series else ""

    def series_art(self, series_id):
        if not series_id:
            return {}
        if series_id not in self.series_art_cache:
            try:
                series = self.application.client.item(series_id)
            except Exception:  # Art is optional; a failed lookup leaves the card on its placeholder.
                series = None
            self.series_art_cache[series_id] = series if isinstance(series, dict) else {}
        return self.series_art_cache[series_id]

    # ------------------------------------------------------------ home rows editor

    def edit_home_rows(self):
        """Reorder or hide home rows with dialogs; saved per server and profile."""
        dialog = self.application.dialog
        prefs = self.row_prefs()
        hidden = set(str(value) for value in prefs.get("hidden") or ())
        default = [str(section.get("id")) for section in self.all_sections]
        order = [str(section.get("id")) for section in apply_row_prefs(self.all_sections,
                                                                       {"order": prefs.get("order")})]
        titles = {str(section.get("id")): str(section.get("title") or "Row") for section in self.all_sections}
        changed, selected = False, 0
        while order:
            labels = [titles[section_id] + (" (hidden)" if section_id in hidden else "") for section_id in order]
            choice = dialog.select("Edit home rows", labels + ["Reset to default"], preselect=selected)
            if not isinstance(choice, int) or not 0 <= choice <= len(order):
                break
            if choice == len(order):
                order, hidden, changed, selected = list(default), set(), True, 0
                continue
            section_id = order[choice]
            actions = ([("up", "Move up")] if choice > 0 else []) + (
                [("down", "Move down")] if choice + 1 < len(order) else []) + [
                ("toggle", "Show" if section_id in hidden else "Hide")]
            picked = dialog.select(titles[section_id], [label for _key, label in actions])
            selected = choice
            if not isinstance(picked, int) or not 0 <= picked < len(actions):
                continue
            action = actions[picked][0]
            if action == "toggle":
                hidden ^= {section_id}
            else:
                target = choice - 1 if action == "up" else choice + 1
                order[choice], order[target] = order[target], order[choice]
                selected = target
            changed = True
        if not changed:
            self.setFocusId(AVATAR)
            return
        # An unchanged order is not saved, so the server's order keeps applying.
        self.save_row_prefs([] if order == default else order,
                            [section_id for section_id in order if section_id in hidden])
        self.apply_sections()
        if self.tab == "home" and self.sections:
            self.focus_row(0)
        else:
            self.setFocusId(AVATAR)

    # ------------------------------------------------------------ library grid

    def show_library(self, kind):
        if kind != self.kind:
            self.filters = {}
        self.kind = kind
        self.set_tab("movies" if kind == "movie" else "series")
        self.library = None
        self.sort = "title"
        self.reversed = False
        self.update_chips()
        grid = self.getControl(GRID)
        grid.setPosition(GRID_LEFT, LIBRARY_GRID_TOP)
        grid.setHeight(SCREEN_HEIGHT - LIBRARY_GRID_TOP)
        grid.controlUp(self.getControl(LIBRARY_CHIP))
        self.load_grid(self.library_params())

    def sort_value(self):
        """The catalog sort term: a leading "-" sorts descending, so reversing flips the sign."""
        if not self.reversed:
            return self.sort
        return self.sort[1:] if self.sort.startswith("-") else "-" + self.sort

    def library_params(self):
        params = {"type": self.kind, "sort": self.sort_value()}
        if self.library is not None:
            params["library_id"] = self.library["id"]
        groups = filter_groups(self.filters)
        if groups:
            params["groups"] = json.dumps(groups, separators=(",", ":"))
        return params

    def update_chips(self):
        name = str(self.library.get("name") or "Library") if self.library else "All"
        sort = next(chip for value, _label, chip in SORTS if value == self.sort)
        if self.reversed:
            sort += " (reversed)"
        count = active_facets(self.filters)
        chips = [(LIBRARY_CHIP, "Library: " + name, True), (SORT_CHIP, "Sort: " + sort, True),
                 (FILTER_CHIP, "Filters", False)]
        if count:
            chips.append((CLEAR_CHIP, "Clear filters", False))
        chips.append((SHUFFLE_CHIP, "Shuffle", False))
        self.getControl(CLEAR_CHIP).setVisible(bool(count))
        self.setProperty("silo.filter_count", str(count) if count else "")
        left = HERO_LEFT
        for control_id, text, chevron in chips:
            width = chip_width(text, chevron)
            if control_id == FILTER_CHIP and count:
                width += BADGE_SIZE + 8
                self.getControl(FILTER_BADGE).setPosition(left + width - 20 - BADGE_SIZE, CHIP_TOP + 12)
            chip = self.getControl(control_id)
            chip.setLabel(text)
            chip.setPosition(left, CHIP_TOP)
            chip.setWidth(width)
            for chevron_id in CHEVRONS.get(control_id, ()):
                self.getControl(chevron_id).setPosition(left + width - 52, CHIP_TOP + 16)
            left += width + CHIP_GAP
        shown = [control_id for control_id, _text, _chevron in chips]
        for before, after in zip(shown, shown[1:]):
            self.getControl(before).controlRight(self.getControl(after))
            self.getControl(after).controlLeft(self.getControl(before))

    def choose_library(self):
        libraries = self.libraries.get(self.kind) or []
        names = ["All libraries"] + [str(library.get("name") or "Library") for library in libraries]
        current = libraries.index(self.library) + 1 if self.library in libraries else 0
        choice = self.application.dialog.select("Library", names, preselect=current)
        if choice < 0 or choice == current:
            return
        self.library = libraries[choice - 1] if choice else None
        self.update_chips()
        self.load_grid(self.library_params())

    def choose_sort(self):
        """Pick a sort, or the last row to flip the current one's direction."""
        values = [value for value, _label, _chip in SORTS]
        current = values.index(self.sort)
        labels = [label for _value, label, _chip in SORTS]
        labels.append("Reverse order: " + ("On" if self.reversed else "Off"))
        choice = self.application.dialog.select("Sort by", labels, preselect=current)
        if choice < 0 or choice == current:
            return
        if choice == len(values):
            self.reversed = not self.reversed
        else:
            # A new sort starts in its natural direction.
            self.sort, self.reversed = values[choice], False
        self.update_chips()
        self.load_grid(self.library_params())

    def shuffle(self):
        from .svc_shuffle import play_shuffle

        library_id = self.library["id"] if self.library is not None else None
        kind = self.kind
        self.run_modal(lambda: play_shuffle(self.application, type=kind, library_id=library_id), SHUFFLE_CHIP)

    # ------------------------------------------------------------ filters

    def scope_params(self):
        params = {"type": self.kind}
        if self.library is not None:
            params["library_id"] = self.library["id"]
        return params

    def filter_options(self):
        """GET /catalog/filters for the current kind and library, cached for the window."""
        key = (self.kind, self.library["id"] if self.library is not None else None)
        if key not in self.filter_options_cache:
            response = self.application.client.request("GET", "/catalog/filters", params=self.scope_params())
            self.filter_options_cache[key] = response if isinstance(response, dict) else {}
        return self.filter_options_cache[key]

    @staticmethod
    def facet_values(facet, options):
        """(value, label) pairs the facet offers; facets without any are not listed."""
        if facet == "decade":
            return [(str(decade), "{}s".format(decade)) for decade in DECADES]
        if facet == "dynamic_range":
            return list(DYNAMIC_RANGES)
        if facet == "watch_status":
            return [(value, label) for value, label, _rule in WATCH_STATUSES]
        if facet == "resolution":
            technical = options.get("technical") if isinstance(options.get("technical"), dict) else {}
            values = technical.get("resolutions")
        else:
            values = options.get(FACET_SOURCES[facet])
        return [(str(value), str(value)) for value in values or () if value not in (None, "")]

    def choose_filters(self):
        options = self.filter_options()
        dialog = self.application.dialog
        changed = False
        while True:
            facets = [(facet, label) for facet, label in FACETS if self.facet_values(facet, options)]
            labels = []
            for facet, label in facets:
                count = len(self.filters.get(facet) or ())
                labels.append("{} ({})".format(label, count) if count else label)
            if active_facets(self.filters):
                labels.append("Clear filters")
            choice = dialog.select("Filters", labels)
            if not isinstance(choice, int) or not 0 <= choice < len(labels):
                break
            if choice == len(facets):
                self.filters, changed = {}, True
                continue
            facet, label = facets[choice]
            if self.edit_facet(facet, label, self.facet_values(facet, options)):
                changed = True
        if changed:
            self.reload_filtered()

    def edit_facet(self, facet, label, values):
        """Pick the facet's values; True when the selection changed."""
        dialog = self.application.dialog
        current = set(self.filters.get(facet) or ())
        if facet == "watch_status":
            names = ["Any"] + [name for _value, name in values]
            preselect = next((index + 1 for index, (value, _name) in enumerate(values) if value in current), 0)
            choice = dialog.select(label, names, preselect=preselect)
            if not isinstance(choice, int) or not 0 <= choice < len(names):
                return False
            chosen = {values[choice - 1][0]} if choice else set()
        else:
            if facet in SEARCHABLE_FACETS and len(values) > FACET_SEARCH_MIN:
                text = (dialog.input("Search {} (leave empty for all)".format(label.lower())) or "").strip()
                if text:
                    values = self.search_facet(facet, text, current)
            if not values:
                return False
            selected = [index for index, (value, _name) in enumerate(values) if value in current]
            picked = dialog.multiselect(label, [name for _value, name in values], preselect=selected)
            if picked is None:
                return False
            listed = set(value for value, _name in values)
            chosen = (current - listed) | set(values[index][0] for index in picked if 0 <= index < len(values))
        if chosen == current:
            return False
        if chosen:
            self.filters[facet] = sorted(chosen)
        else:
            self.filters.pop(facet, None)
        return True

    def search_facet(self, facet, text, current):
        """GET /catalog/filters/search; current selections stay listed first."""
        params = dict(self.scope_params(), facet=facet, q=text, limit=FACET_SEARCH_LIMIT)
        response = self.application.client.request("GET", "/catalog/filters/search", params=params) or {}
        found = [entry.get("value") for entry in response.get("values") or () if isinstance(entry, dict)]
        if not found:
            found = list(response.get("matches") or ())
        values = sorted(current) + [str(value) for value in found if value and str(value) not in current]
        return [(value, value) for value in values]

    def clear_filters(self):
        if not self.filters:
            return
        self.filters = {}
        self.reload_filtered()
        self.setFocusId(FILTER_CHIP)

    def reload_filtered(self):
        self.update_chips()
        self.load_grid(self.library_params())

    # ------------------------------------------------------------ grid loading

    def clear_grid(self, empty=""):
        self.generation += 1
        self.params, self.grid_items, self.cursor = None, [], None
        self.getControl(GRID).reset()
        self.setProperty("silo.total", "")
        self.setProperty("silo.empty", empty)

    def load_grid(self, params, seek=None):
        """Load the first page; ``seek`` starts it at a list position (the A–Z rail)."""
        self.clear_grid()
        generation = self.generation
        self.params = params
        query = dict(params, seek=seek) if seek else dict(params)
        response = self.application.guard(lambda: self.application.browse(query))
        if generation != self.generation:
            return
        if response is None:
            self.setProperty("silo.empty", "Titles could not be loaded.")
            return
        self.append_page(response)
        if seek is None:
            self.load_letters()

    def append_page(self, response):
        items = present.video_items(response.get("items"))
        self.grid_items.extend(items)
        self.getControl(GRID).addItems([card_list_item(item, self.card_image) for item in items])
        self.cursor = response.get("next_cursor") or None
        if self.tab in TAB_KINDS:
            self.setProperty("silo.total", total_text(response.get("total")))
        if not self.grid_items:
            self.setProperty("silo.empty", "No results" if self.tab == "search" else "No titles to show")
        else:
            self.setProperty("silo.empty", "")

    def load_more(self):
        """Fetch the next page when the selection nears the end of the grid."""
        if not self.cursor or self.params is None:
            return
        if self.getControl(GRID).getSelectedPosition() < len(self.grid_items) - LOAD_AHEAD:
            return
        generation, cursor = self.generation, self.cursor
        self.cursor = None  # One request at a time; a failed page is not retried on every key press.
        response = self.application.browse(dict(self.params), cursor)
        if generation == self.generation and response is not None:
            self.append_page(response)

    # ------------------------------------------------------------ A–Z rail

    def rail_active(self):
        return self.tab in TAB_KINDS and self.sort == "title" and not self.reversed

    def update_rail(self):
        active = self.rail_active()
        self.setProperty("silo.rail", "true" if active else "")
        self.getControl(RAIL_GROUP).setVisible(active)
        if not active:
            self.letters = None

    def fill_rail(self):
        rail = self.getControl(RAIL)
        rail.reset()
        items = []
        for letter in RAIL_LETTERS:
            item = xbmcgui.ListItem(label=letter)
            item.setProperty("silo.has", "true")
            items.append(item)
        rail.addItems(items)

    def load_letters(self):
        """POST /catalog/letters for the grid's scope and filters; letters without titles are dimmed."""
        self.update_rail()
        if not self.rail_active() or self.params is None:
            return
        body = {"type": self.kind, "order": "asc"}
        if self.library is not None:
            body["library_id"] = str(self.library["id"])
        groups = filter_groups(self.filters)
        if groups:
            body["groups"] = groups
        try:
            response = self.application.client.request("POST", "/catalog/letters", body=body)
        except Exception:  # Without the index the rail still offers All and every letter.
            response = None
        letters = {}
        entries = response.get("letters") if isinstance(response, dict) else None
        for entry in entries or ():
            if isinstance(entry, dict) and isinstance(entry.get("position"), int):
                letters[str(entry.get("letter"))] = entry["position"]
        self.letters = letters if isinstance(response, dict) else None
        rail = self.getControl(RAIL)
        for index, letter in enumerate(RAIL_LETTERS):
            has = letter == "All" or self.letters is None or letter in self.letters
            try:
                rail.getListItem(index).setProperty("silo.has", "true" if has else "")
            except (RuntimeError, AttributeError):
                pass

    def jump_to_letter(self):
        """Load the grid from the chosen letter's first title and focus it."""
        position = self.getControl(RAIL).getSelectedPosition()
        if not 0 <= position < len(RAIL_LETTERS) or self.params is None:
            return
        letter = RAIL_LETTERS[position]
        if letter == "All":
            seek = 0
        elif self.letters is None or letter not in self.letters:
            return
        else:
            seek = self.letters[letter]
        params, letters = self.params, self.letters
        self.load_grid(params, seek=seek)
        self.letters = letters
        if self.grid_items:
            self.setFocusId(GRID)
            self.getControl(GRID).selectItem(0)

    # ------------------------------------------------------------ search

    def show_search(self):
        self.set_tab("search")
        grid = self.getControl(GRID)
        grid.setPosition(GRID_LEFT, SEARCH_GRID_TOP)
        grid.setHeight(SCREEN_HEIGHT - SEARCH_GRID_TOP)
        self.select_search_type(self.search_type)
        self.setFocusId(SEARCH_FIELD)

    def select_search_type(self, search_type):
        self.search_type = search_type
        self.setProperty("silo.search_type", search_type)
        self.run_search()

    def edit_query(self):
        text = (self.application.dialog.input("Search", self.query) or "").strip()
        if not text:
            return
        self.query = text
        self.setProperty("silo.query", text)
        self.run_search()

    def run_search(self):
        if not self.query:
            self.show_people([])
            self.clear_grid("Search your library")
            return
        self.show_people(self.search_people())
        self.load_grid({"q": self.query, "type": self.search_type})

    def search_people(self):
        """GET /catalog/people; the row is optional, so a failure just hides it."""
        params = {"q": self.query, "limit": PEOPLE_LIMIT, "media_scope": self.search_type}
        try:
            response = self.application.client.request("GET", "/catalog/people", params=params)
        except Exception:
            return []
        items = response.get("items") if isinstance(response, dict) else None
        return [person for person in items or () if isinstance(person, dict) and person.get("id") is not None]

    def show_people(self, people):
        """Fill the People row and move the grid below it."""
        self.people = people
        control = self.getControl(PEOPLE_LIST)
        control.reset()
        if people:
            control.addItems([self.person_item(person) for person in people])
        self.setProperty("silo.people", "true" if people else "")
        self.getControl(PEOPLE_GROUP).setVisible(bool(people))
        if self.tab != "search":
            return
        grid = self.getControl(GRID)
        chip = self.getControl(next(control_id for control_id, value in SEARCH_CHIPS.items()
                                    if value == self.search_type))
        top = SEARCH_GRID_TOP + (PEOPLE_HEIGHT if people else 0)
        grid.setPosition(GRID_LEFT, top)
        grid.setHeight(SCREEN_HEIGHT - top)
        below = control if people else grid
        for chip_id in SEARCH_CHIPS:
            self.getControl(chip_id).controlDown(below)
        if people:
            control.controlUp(chip)
            control.controlDown(grid)
        grid.controlUp(below if people else chip)

    def person_item(self, person):
        name = str(person.get("name") or "")
        item = xbmcgui.ListItem(label=name)
        # application.image validates the presigned URL like any other artwork URL.
        photo = self.application.image({"poster_url": person["photo_url"]}, "poster") if person.get(
            "photo_url") else ""
        if photo:
            item.setArt({"thumb": photo})
        item.setProperty("silo.initials", present.initials(name))
        return item

    def open_person(self):
        position = self.getControl(PEOPLE_LIST).getSelectedPosition()
        if not 0 <= position < len(self.people):
            return
        person = self.people[position]
        self.run_modal(lambda: self.application.open_screen(
            "person", person_id=person["id"], name=str(person.get("name") or "")), PEOPLE_LIST)

    # ------------------------------------------------------------ card context menu

    def context_card(self, control_id):
        """(item, row or None) for the selected card of a home row or the grid."""
        row = self.row_of(control_id)
        if row is not None:
            items = self.sections[row]["items"]
        elif control_id == GRID:
            items = self.grid_items
        else:
            return None, None
        position = self.getControl(control_id).getSelectedPosition()
        if not 0 <= position < len(items):
            return None, None
        return items[position], row

    def card_menu(self, control_id):
        item, row = self.context_card(control_id)
        if item is None:
            return
        marks = svc_personal.state(item)
        entries = [("watched", "Mark as unwatched" if marks["watched"] else "Mark as watched"),
                   ("watchlist", "Remove from watchlist" if marks["watchlist"] else "Add to watchlist"),
                   ("favorite", "Remove from favorites" if marks["favorite"] else "Add to favorites")]
        surface = svc_personal.dismiss_surface(item) if row is not None and self.is_wide(row) else None
        if surface:
            entries.append(("dismiss", "Remove from Continue Watching" if surface == "continue_watching"
                            else "Remove from Next Up"))
        if item.get("type") == "episode" and item.get("series_id"):
            entries.append(("series", "Go to series"))
        choice = self.application.dialog.contextmenu([label for _key, label in entries])
        if not isinstance(choice, int) or not 0 <= choice < len(entries):
            return
        action = entries[choice][0]
        if action == "series":
            card = {"content_id": item["series_id"], "type": "series"}
            self.run_modal(lambda: self.application.open_item(card), control_id)
            return
        if self.application.guard(lambda: self.apply_mark(action, item, marks, surface)) is None:
            return
        if row is not None:
            self.refresh_after(control_id, self.card_at(control_id))
        else:
            self.update_grid_card(control_id, item)

    def apply_mark(self, action, item, marks, surface):
        client, content_id = self.application.client, item["content_id"]
        if action == "watched":
            svc_personal.set_watched(client, content_id, not marks["watched"])
            self.set_user_state(item, "played", not marks["watched"])
        elif action == "watchlist":
            svc_personal.set_watchlist(client, content_id, not marks["watchlist"])
            self.set_user_state(item, "in_watchlist", not marks["watchlist"])
        elif action == "favorite":
            svc_personal.set_favorite(client, content_id, not marks["favorite"])
            self.set_user_state(item, "is_favorite", not marks["favorite"])
        elif action == "dismiss":
            self.dismiss(surface, item)
        return True

    def dismiss(self, surface, item):
        """PUT /home/dismissals/{surface}/{id} with the anchor the server requires for the surface."""
        if surface not in svc_personal.DISMISS_SURFACES:
            raise ValueError("Unknown home surface: {}".format(surface))
        if surface == "next_up":
            anchor = {"series_id": item.get("series_id") or (
                item["content_id"] if item.get("type") == "series" else None)}
        else:
            anchor = {"progress_updated_at": item.get("progress_updated_at")}
        path = "/home/dismissals/{}/{}".format(surface, quote(str(item["content_id"]), safe=""))
        return self.application.client.request(
            "PUT", path, body={key: value for key, value in anchor.items() if value})

    @staticmethod
    def set_user_state(item, key, value):
        state = item.get("user_state") if isinstance(item.get("user_state"), dict) else {}
        item["user_state"] = dict(state, **{key: value})
        if isinstance(item.get("user_data"), dict):
            item["user_data"] = dict(item["user_data"], **{key: value})

    def update_grid_card(self, control_id, item):
        """Grid cards are not reloaded; their watched and progress overlays update in place."""
        position = self.getControl(control_id).getSelectedPosition()
        data = present.card(item, self.card_image)
        try:
            list_item = self.getControl(control_id).getListItem(position)
            list_item.setProperty("silo.watched", "true" if data["watched"] else "")
            list_item.setProperty("silo.progress", str(data["progress"]) if data["progress"] else "")
        except (RuntimeError, AttributeError):
            pass

    # ------------------------------------------------------------ modal screens and refresh

    def run_modal(self, callback, focus):
        """Run a modal screen, then refresh home rows and return focus where it was."""
        card = self.card_at(focus) if self.tab == "home" else None
        self.last_focus = focus
        try:
            callback()
        finally:
            self.refresh_after(focus, card)

    def refresh_after(self, focus, card=None):
        if self.tab == "home":
            if self.refresh_home(card):
                return
        else:
            self.home_stale = True  # Home reloads when it is shown again.
        self.setFocusId(focus)

    # ------------------------------------------------------------ input

    def onFocus(self, control_id):
        if self.row_of(control_id) is not None:
            self.hero_debouncer.trigger()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.back()
            return
        if self.menu_open:
            if action_id in (ACTION_LEFT, ACTION_RIGHT):
                self.close_menu()
            return
        focus = self.focus_id()
        if self.pill_open is not None:
            # The first entry's onup points at itself, so Up there closes the dropdown.
            if action_id in (ACTION_LEFT, ACTION_RIGHT) or (action_id == ACTION_UP and focus == min(PILL_ITEMS)):
                self.close_pill()
            return
        row = self.row_of(focus)
        if action_id in CONTEXT_ACTIONS:
            if row is not None or focus == GRID:
                self.application.guard(lambda: self.card_menu(focus))
            return
        if action_id == ACTION_UP:
            if row is not None:
                if row > 0:
                    self.focus_row(row - 1)
                else:
                    self.focus_tab()
            elif focus in CHIPS or focus == SEARCH_FIELD:
                self.focus_tab()
        elif action_id == ACTION_DOWN:
            if row is not None:
                if row + 1 < len(self.sections):
                    self.focus_row(row + 1)
            elif focus in LIBRARY_TABS and TAB_KINDS.get(self.tab) != LIBRARY_TABS[focus]:
                # Android's library pill: Down on Movies or Series opens Recommended/Browse/Collections.
                self.open_pill(LIBRARY_TABS[focus])
            elif focus in BAR_CONTROLS:
                self.focus_content()
        elif action_id in (ACTION_LEFT, ACTION_RIGHT) and row is not None:
            self.hero_debouncer.trigger()
        if focus == GRID and action_id in (ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT):
            self.application.guard(self.load_more)

    def back(self):
        if self.menu_open:
            self.close_menu()
        elif self.pill_open is not None:
            self.close_pill()
        elif self.tab != "home":
            self.show_home()
        elif self.row_of(self.focus_id()) is not None:
            # Back from the rows comes up to the Home tab first.
            self.focus_tab()
        elif self.application.options().get("confirm_exit", True):
            self.confirm_exit()
        else:
            self.result = None
            self.close()

    def confirm_exit(self):
        """Ask before leaving; focus starts on Cancel, and Back in the dialog also stays."""
        answer = self.application.dialog.yesnocustom(
            "Exit Founders Theater", "Are you ready to exit Founders Theater?",
            customlabel="Quit Kodi", nolabel="Cancel", yeslabel="Exit",
            defaultbutton=getattr(xbmcgui, "DLG_YESNO_NO_BTN", 10))
        if answer in (1, 2):
            self.result = "quit_kodi" if answer == 2 else None
            self.close()

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        row = self.row_of(control_id)
        if row is not None:
            self.open(self.sections[row]["items"], control_id)
        elif control_id == GRID:
            self.open(self.grid_items, control_id)
        elif control_id in MENU_ITEMS:
            self.choose_menu(control_id)
        elif control_id in PILL_ITEMS:
            self.choose_pill(control_id)
        elif control_id == AVATAR:
            self.open_menu()
        elif control_id == TABS[0][1]:
            self.select_home_tab()
        elif control_id in LIBRARY_TABS:
            self.show_library(LIBRARY_TABS[control_id])
        elif control_id in SCREEN_TABS:
            self.open_screen_tab(control_id)
        elif control_id == SEARCH_BUTTON:
            self.show_search()
        elif control_id == LIBRARY_CHIP:
            self.choose_library()
        elif control_id == SORT_CHIP:
            self.choose_sort()
        elif control_id == FILTER_CHIP:
            self.choose_filters()
        elif control_id == CLEAR_CHIP:
            self.clear_filters()
        elif control_id == SHUFFLE_CHIP:
            self.shuffle()
        elif control_id == RAIL:
            self.jump_to_letter()
        elif control_id == PEOPLE_LIST:
            self.open_person()
        elif control_id == SEARCH_FIELD:
            self.edit_query()
        elif control_id in SEARCH_CHIPS and SEARCH_CHIPS[control_id] != self.search_type:
            self.select_search_type(SEARCH_CHIPS[control_id])

    def open(self, items, control_id):
        position = self.getControl(control_id).getSelectedPosition()
        if not 0 <= position < len(items):
            return
        item = items[position]
        self.run_modal(lambda: self.application.open_item(item), control_id)
