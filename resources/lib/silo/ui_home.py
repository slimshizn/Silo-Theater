"""The home screen: top bar, hero and rows, library grids and search."""

import xbmcgui

from . import present
from .ui_common import (ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer,
                        card_list_item, set_properties)

# Control ids; scripts/skin_home.py writes the matching SiloHome.xml.
TOP_BAR, TAB_CLUSTER, SEARCH_BUTTON, AVATAR = 200, 202, 201, 230
# (tab, button, group, width); the cluster is centred with a search-sized spacer after the last tab.
TABS = (("home", 210, 215, 124), ("movies", 211, 216, 148), ("series", 212, 217, 140))
TAB_KINDS = {"movies": "movie", "series": "series"}
SEARCH_SIZE, SEARCH_GAP, TAB_GAP, SCREEN_WIDTH, SCREEN_HEIGHT = 56, 24, 8, 1920, 1080
BAR_CONTROLS = frozenset([SEARCH_BUTTON, AVATAR] + [tab[1] for tab in TABS])

HOME_GROUP, HERO_GROUP, HERO_META = 300, 400, 430
BADGE_IMAGES, BADGE_LABELS = (411, 412, 413, 414), (421, 422, 423, 424)
HERO_LEFT, BADGE_TOP, BADGE_GAP, NO_SYNOPSIS_SHIFT = 88, 346, 12, 84

# Row slot k: group SLOT_BASE + 10k, header +1, play icon +2, wide list +3, poster list +4.
SLOTS, SLOT_BASE = 12, 3000
ROWS_TOP, WIDE_PITCH, POSTER_PITCH = 530, 410, 470

BROWSE_GROUP, LIBRARY_GROUP, SEARCH_GROUP, GRID = 700, 710, 800, 500
LIBRARY_CHIP, SORT_CHIP, SEARCH_FIELD = 510, 511, 520
CHEVRONS = {LIBRARY_CHIP: (512, 513), SORT_CHIP: (514, 515)}
CHIP_TOP, CHIP_GAP = 188, 16
GRID_LEFT, LIBRARY_GRID_TOP, SEARCH_GRID_TOP = 40, 270, 370
SEARCH_CHIPS = {530: "video", 531: "movie", 532: "series"}
LOAD_AHEAD = 12
# (sort value, dialog label, chip label)
SORTS = (("title", "Title A–Z", "Title"), ("-added_at", "Recently added", "Recently added"),
         ("-release_date", "Release date", "Release date"), ("-rating", "Rating", "Rating"))

MENU, MENU_PANEL = 600, 601
MENU_ITEMS = {610: "profile", 611: "settings", 612: "logout"}
MENU_ROW = 72


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2, base + 3, base + 4


def chip_width(text):
    """Estimated chip width: text at the 25px bold tab font plus padding and chevron."""
    return 28 + 14 * len(text) + 12 + 24 + 28


def total_text(total):
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return ""
    total = int(total)
    return "{:,} title{}".format(total, "" if total == 1 else "s")


class HomeWindow(xbmcgui.WindowXML):
    """Silo's home window; ``result`` is "profile", "settings", "logout" or None."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.result = None
        self.initialized = False
        self.tab = "home"
        self.sections = []
        self.row = 0
        self.show_plot = True
        self.libraries = {"movie": [], "series": []}
        self.menu_open = False
        self.last_focus = None
        self.kind = "movie"
        self.library = None
        self.sort = "title"
        self.query = ""
        self.search_type = "video"
        self.params = None
        self.grid_items = []
        self.cursor = None
        self.generation = 0
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
        self.show_plot = bool((application.guard(application.options) or {}).get("show_plot", True))
        set_properties(self, {
            "silo.initials": present.initials(application.profile_name),
            "silo.avatar": present.avatar_image(application.profile_avatar),
            "silo.profile_locked": "true" if self.locked() else "",
            "silo.menu": "",
            "silo.search_type": self.search_type,
        })
        self.getControl(MENU).setVisible(False)
        for kind in self.libraries:
            self.libraries[kind] = application.guard(lambda kind=kind: application.video_libraries(kind)) or []
        self.layout_tabs()
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

    def layout_tabs(self):
        """Hide tabs without libraries and keep the tab group centred."""
        chain, left = [SEARCH_BUTTON], SEARCH_SIZE + SEARCH_GAP
        for tab, button, group, width in TABS:
            shown = tab == "home" or bool(self.libraries[TAB_KINDS[tab]])
            self.getControl(group).setVisible(shown)
            self.getControl(button).setVisible(shown)
            if shown:
                self.getControl(group).setPosition(left, 2)
                left += width + TAB_GAP
                chain.append(button)
        chain.append(AVATAR)
        width = left - TAB_GAP + SEARCH_GAP + SEARCH_SIZE
        self.getControl(TAB_CLUSTER).setPosition((SCREEN_WIDTH - width) // 2, 0)
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

    def focus_tab(self):
        buttons = {tab[0]: tab[1] for tab in TABS}
        self.setFocusId(buttons.get(self.tab, SEARCH_BUTTON))

    def focus_content(self):
        if self.tab == "home":
            if self.sections:
                self.focus_row(self.row)
        elif self.tab == "search":
            self.setFocusId(SEARCH_FIELD)
        else:
            self.setFocusId(LIBRARY_CHIP)

    def open_menu(self):
        locked = self.locked()
        self.setProperty("silo.profile_locked", "true" if locked else "")
        self.getControl(610).setVisible(not locked)
        shown = [control for control in sorted(MENU_ITEMS) if not (locked and control == 610)]
        for index, control in enumerate(shown):
            self.getControl(control).setPosition(16, 16 + MENU_ROW * index)
        self.getControl(MENU_PANEL).setHeight(32 + MENU_ROW * len(shown) - 8)
        self.menu_open = True
        self.setProperty("silo.menu", "true")
        self.getControl(MENU).setVisible(True)
        self.setFocusId(shown[0])

    def close_menu(self):
        self.menu_open = False
        self.setProperty("silo.menu", "")
        self.getControl(MENU).setVisible(False)
        self.setFocusId(AVATAR)

    def choose_menu(self, control_id):
        if control_id == 610 and self.locked():
            return
        self.result = MENU_ITEMS[control_id]
        self.close()

    # ------------------------------------------------------------ home rows

    def show_home(self):
        self.set_tab("home")
        if self.sections:
            self.focus_row(self.row)
        else:
            self.setFocusId(TABS[0][1])

    def load_home(self):
        sections = self.application.home_sections() or []
        self.sections = [section for section in sections if section.get("items")][:SLOTS]
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
        control.addItems([card_list_item(item, self.application.image, wide) for item in section["items"]])
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
        # Without a synopsis the title block drops so the hero stays bottom-aligned.
        self.getControl(HERO_GROUP).setPosition(0, 0 if hero["synopsis"] else NO_SYNOPSIS_SHIFT)

    # ------------------------------------------------------------ library grid

    def show_library(self, kind):
        self.kind = kind
        self.set_tab("movies" if kind == "movie" else "series")
        self.library = None
        self.sort = "title"
        self.update_chips()
        grid = self.getControl(GRID)
        grid.setPosition(GRID_LEFT, LIBRARY_GRID_TOP)
        grid.controlUp(self.getControl(LIBRARY_CHIP))
        self.load_grid(self.library_params())

    def library_params(self):
        params = {"type": self.kind, "sort": self.sort}
        if self.library is not None:
            params["library_id"] = self.library["id"]
        return params

    def update_chips(self):
        name = str(self.library.get("name") or "Library") if self.library else "All"
        sort = next(chip for value, _label, chip in SORTS if value == self.sort)
        left = HERO_LEFT
        for control_id, text in ((LIBRARY_CHIP, "Library: " + name), (SORT_CHIP, "Sort: " + sort)):
            width = chip_width(text)
            chip = self.getControl(control_id)
            chip.setLabel(text)
            chip.setPosition(left, CHIP_TOP)
            chip.setWidth(width)
            for chevron in CHEVRONS[control_id]:
                self.getControl(chevron).setPosition(left + width - 52, CHIP_TOP + 16)
            left += width + CHIP_GAP

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
        values = [value for value, _label, _chip in SORTS]
        current = values.index(self.sort)
        choice = self.application.dialog.select("Sort by", [label for _value, label, _chip in SORTS],
                                                preselect=current)
        if choice < 0 or choice == current:
            return
        self.sort = values[choice]
        self.update_chips()
        self.load_grid(self.library_params())

    def clear_grid(self, empty=""):
        self.generation += 1
        self.params, self.grid_items, self.cursor = None, [], None
        self.getControl(GRID).reset()
        self.setProperty("silo.total", "")
        self.setProperty("silo.empty", empty)

    def load_grid(self, params):
        self.clear_grid()
        generation = self.generation
        self.params = params
        response = self.application.guard(lambda: self.application.browse(dict(params)))
        if generation != self.generation:
            return
        if response is None:
            self.setProperty("silo.empty", "Titles could not be loaded.")
            return
        self.append_page(response)

    def append_page(self, response):
        items = present.video_items(response.get("items"))
        self.grid_items.extend(items)
        self.getControl(GRID).addItems([card_list_item(item, self.application.image) for item in items])
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

    # ------------------------------------------------------------ search

    def show_search(self):
        self.set_tab("search")
        grid = self.getControl(GRID)
        grid.setPosition(GRID_LEFT, SEARCH_GRID_TOP)
        self.select_search_type(self.search_type)
        self.setFocusId(SEARCH_FIELD)

    def select_search_type(self, search_type):
        self.search_type = search_type
        self.setProperty("silo.search_type", search_type)
        chip = next(control for control, value in SEARCH_CHIPS.items() if value == search_type)
        self.getControl(GRID).controlUp(self.getControl(chip))
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
            self.clear_grid("Search your library")
            return
        self.load_grid({"q": self.query, "type": self.search_type})

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
        row = self.row_of(focus)
        if action_id == ACTION_UP:
            if row is not None:
                if row > 0:
                    self.focus_row(row - 1)
                else:
                    self.focus_tab()
            elif focus in (LIBRARY_CHIP, SORT_CHIP, SEARCH_FIELD):
                self.focus_tab()
        elif action_id == ACTION_DOWN:
            if row is not None:
                if row + 1 < len(self.sections):
                    self.focus_row(row + 1)
            elif focus in BAR_CONTROLS:
                self.focus_content()
        elif action_id in (ACTION_LEFT, ACTION_RIGHT) and row is not None:
            self.hero_debouncer.trigger()
        if focus == GRID and action_id in (ACTION_UP, ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT):
            self.application.guard(self.load_more)

    def back(self):
        if self.menu_open:
            self.close_menu()
        elif self.tab != "home":
            self.show_home()
        else:
            self.result = None
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
        elif control_id == AVATAR:
            self.open_menu()
        elif control_id == TABS[0][1]:
            self.set_tab("home")
        elif control_id in (TABS[1][1], TABS[2][1]):
            self.show_library("movie" if control_id == TABS[1][1] else "series")
        elif control_id == SEARCH_BUTTON:
            self.show_search()
        elif control_id == LIBRARY_CHIP:
            self.choose_library()
        elif control_id == SORT_CHIP:
            self.choose_sort()
        elif control_id == SEARCH_FIELD:
            self.edit_query()
        elif control_id in SEARCH_CHIPS and SEARCH_CHIPS[control_id] != self.search_type:
            self.select_search_type(SEARCH_CHIPS[control_id])

    def open(self, items, control_id):
        position = self.getControl(control_id).getSelectedPosition()
        if not 0 <= position < len(items):
            return
        self.last_focus = control_id
        try:
            self.application.open_item(items[position])
        finally:
            self.setFocusId(control_id)
