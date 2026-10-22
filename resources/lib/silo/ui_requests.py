"""The Requests screen: Discover rows, the profile's own requests, search, and a title page.

Mirrors the Silo Android TV Requests tab: a preview of the focused card over
rows ("Your requests", then the server's discover rows), and a detail page
with Request (seasons for series), Notify me / Stop notifying, and Cancel.
A card that reads "In library" opens the catalog item instead.
"""

import xbmcgui

from . import present
from . import svc_requests as svc
from .ui_common import ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer, set_properties

ACTION_CONTEXT_MENU = 117

# SiloRequests.xml; scripts/skin_requests.py writes the matching skin.
SEARCH_BUTTON, PREVIEW_GROUP, EMPTY_LABEL, SEARCH_NOTE = 110, 400, 150, 151
# Row slot k: group SLOT_BASE + 10k, header +1, poster list +2.
SLOTS, SLOT_BASE = 10, 3000
ROWS_TOP, ROW_PITCH, SCREEN_HEIGHT = 560, 470, 1080

# SiloRequestsDetail.xml
PRIMARY, FOLLOW, CANCEL, MORE_SEASONS = 300, 301, 302, 303
BUTTONS = (PRIMARY, MORE_SEASONS, FOLLOW, CANCEL)
RECOMMENDATIONS, RECOMMENDATIONS_HEADER = 500, 501
BUTTON_LEFT, BUTTON_TOP, BUTTON_GAP = 100, 556, 16


def slot_ids(index):
    base = SLOT_BASE + 10 * index
    return base, base + 1, base + 2


def button_width(text):
    """Estimated pill width for a label at the 25px bold button font."""
    return max(200, 56 + 15 * len(text))


def list_item(media, record=False):
    """A poster card: title, year or status caption, TMDB art and the status dot."""
    state = svc.record_state(media) if record else svc.result_state(media)
    year = svc._int(media.get("year"))
    second = state["short"] if state else (str(year) if year and year > 0 else svc.type_label(media))
    item = xbmcgui.ListItem(label=svc._text(media.get("title")) or "Untitled", label2=second)
    art = {"poster": svc.poster_url(media), "fanart": svc.backdrop_url(media)}
    item.setArt({key: value for key, value in art.items() if value})
    item.setProperty("silo.status", state["short"] if state else "")
    item.setProperty("silo.status_key", state["key"] if state else "")
    item.setProperty("silo.plot", svc._text(media.get("overview")))
    return item


def open_requests(application):
    window = RequestsWindow("SiloRequests.xml", application.addon.getAddonInfo("path"), "Default", "1080i")
    window.application = application
    window.doModal()
    del window


def open_request_detail(application, media, feature=None):
    """The title page; returns True when something there changed (a request, follow or cancel)."""
    window = RequestDetailWindow("SiloRequestsDetail.xml", application.addon.getAddonInfo("path"), "Default",
                                 "1080i")
    window.application, window.media, window.feature = application, media, feature or {}
    window.doModal()
    changed = window.changed
    del window
    return changed


def open_media(application, media, record=False, feature=None):
    """Route a card: an item in the library opens its catalog page, anything else the request page."""
    content_id = svc.library_item(media, record)
    if content_id:
        application.open_item({"content_id": content_id})
        return False
    return open_request_detail(application, media, feature)


class RequestsWindow(xbmcgui.WindowXMLDialog):
    """Full-screen Requests hub."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.initialized = False
        self.feature = {}
        self.records = []
        self.sections = []
        self.query = ""
        self.results = None
        self.rows = []
        self.row = 0
        self.last_focus = None
        self.preview_debouncer = Debouncer(self.update_preview)

    # ------------------------------------------------------------ lifecycle

    def onInit(self):
        if self.initialized:
            if self.last_focus:
                self.setFocusId(self.last_focus)
            return
        self.initialized = True
        set_properties(self, {"silo.query": "", "silo.search_note": "", "silo.empty": ""})
        self.reload()
        self.focus_content()

    def close(self):
        self.preview_debouncer.cancel()
        super().close()

    def focus_id(self):
        try:
            return self.getFocusId()
        except RuntimeError:
            return 0

    def reload(self):
        """Read the feature status, the profile's requests and Discover; one error dialog at most."""
        loaded = self.application.guard(self.load)
        if loaded is None and not self.rows:
            self.setProperty("silo.empty", "Requests could not be loaded.")
        self.build_rows()

    def load(self):
        client = self.application.client
        try:
            self.feature = svc.status(client)
        except Exception:  # The status only tunes the detail page; the rows still load.
            self.feature = {}
        self.records = [record for record in svc.mine(client) if svc.record_state(record)["key"] != "unavailable"]
        self.sections = svc.discover(client)
        return True

    # ------------------------------------------------------------ rows

    def build_rows(self):
        rows = []
        if self.results:
            rows.append({"title": 'Results for "{}"'.format(self.query), "items": self.results, "record": False})
        if self.records:
            rows.append({"title": "Your requests", "items": self.records, "record": True})
        for section in self.sections:
            rows.append({"title": section["title"], "items": section["results"], "record": False})
        self.rows = rows[:SLOTS]
        for index in range(SLOTS):
            self.fill_slot(index, self.rows[index] if index < len(self.rows) else None)
        self.row = min(self.row, max(0, len(self.rows) - 1))
        self.layout_rows()
        if self.rows:
            self.setProperty("silo.empty", "")
            self.show_preview(self.rows[self.row]["items"][0], self.rows[self.row]["record"])
        else:
            if not self.getProperty("silo.empty"):
                self.setProperty("silo.empty", "Nothing here yet. Use Search to find a movie or series to request.")
            self.show_preview(None)

    def fill_slot(self, index, row):
        group, header, poster_list = slot_ids(index)
        control = self.getControl(poster_list)
        control.reset()
        if row is None:
            self.getControl(group).setVisible(False)
            return
        self.getControl(header).setLabel(row["title"])
        control.addItems([list_item(media, row["record"]) for media in row["items"]])

    def layout_rows(self):
        """The focused row sits at the top of the rows area; rows above it are hidden."""
        top = ROWS_TOP
        for index in range(SLOTS):
            group = self.getControl(slot_ids(index)[0])
            if index >= len(self.rows) or index < self.row or top >= SCREEN_HEIGHT:
                group.setVisible(False)
                continue
            group.setPosition(0, top)
            group.setVisible(True)
            top += ROW_PITCH

    def row_of(self, control_id):
        offset = control_id - SLOT_BASE
        if offset < 0 or offset % 10 != 2:
            return None
        index = offset // 10
        return index if index < len(self.rows) else None

    def focus_row(self, index):
        self.row = index
        self.layout_rows()
        self.setFocusId(slot_ids(index)[2])
        self.preview_debouncer.trigger()

    def focus_content(self):
        if self.rows:
            self.focus_row(min(self.row, len(self.rows) - 1))
        else:
            self.setFocusId(SEARCH_BUTTON)

    def selected(self, control_id):
        index = self.row_of(control_id)
        if index is None:
            return None, None
        row = self.rows[index]
        position = self.getControl(control_id).getSelectedPosition()
        if not 0 <= position < len(row["items"]):
            return None, row
        return row["items"][position], row

    # ------------------------------------------------------------ preview

    def update_preview(self):
        """Debounced: show the focused card above the rows."""
        media, row = self.selected(self.focus_id())
        if media is not None:
            self.show_preview(media, row["record"])

    def show_preview(self, media, record=False):
        if media is None:
            set_properties(self, {"silo.preview.title": "", "silo.preview.meta": "", "silo.preview.synopsis": "",
                                  "silo.preview.backdrop": "", "silo.preview.status": "",
                                  "silo.preview.status_key": ""})
            return
        state = svc.record_state(media) if record else svc.result_state(media)
        if state and not record and state["key"] == "unavailable":
            state = None  # Like Android: a title that can't be requested shows no status line.
        set_properties(self, {
            "silo.preview.title": svc._text(media.get("title")) or "Untitled",
            "silo.preview.meta": svc.meta_text(media, record),
            "silo.preview.synopsis": svc._text(media.get("overview")),
            "silo.preview.backdrop": svc.backdrop_url(media),
            "silo.preview.status": state["long"] if state else "",
            "silo.preview.status_key": state["key"] if state else "",
        })

    # ------------------------------------------------------------ search

    def edit_query(self):
        text = (self.application.dialog.input("Search movies and series", self.query) or "").strip()
        if not text:
            return
        response = svc.search(self.application.client, text)
        self.query = text
        self.setProperty("silo.query", text)
        self.results = response["results"]
        self.setProperty("silo.search_note", "" if self.results else 'No results for "{}"'.format(text))
        self.row = 0
        self.build_rows()
        if self.results:
            self.focus_row(0)

    # ------------------------------------------------------------ input

    def onFocus(self, control_id):
        if self.row_of(control_id) is not None:
            self.preview_debouncer.trigger()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.close()
            return
        focus = self.focus_id()
        row = self.row_of(focus)
        if action_id == ACTION_UP:
            if row is not None:
                if row > 0:
                    self.focus_row(row - 1)
                else:
                    self.setFocusId(SEARCH_BUTTON)
        elif action_id == ACTION_DOWN:
            if row is not None:
                if row + 1 < len(self.rows):
                    self.focus_row(row + 1)
            elif focus == SEARCH_BUTTON and self.rows:
                self.focus_row(0)
        elif action_id in (ACTION_LEFT, ACTION_RIGHT) and row is not None:
            self.preview_debouncer.trigger()
        elif action_id == ACTION_CONTEXT_MENU and row is not None:
            self.application.guard(lambda: self.card_menu(focus))

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        if control_id == SEARCH_BUTTON:
            self.edit_query()
            return
        media, row = self.selected(control_id)
        if media is None:
            return
        self.last_focus = control_id
        try:
            changed = open_media(self.application, media, row["record"], self.feature)
        finally:
            self.setFocusId(control_id)
        if changed:
            self.refresh()

    def card_menu(self, control_id):
        """Context menu on one of the profile's own pending requests: Cancel."""
        media, row = self.selected(control_id)
        if media is None or not row["record"] or svc.record_state(media)["key"] != "pending":
            return
        title = svc._text(media.get("title")) or "Request"
        if self.application.dialog.select(title, ["Cancel request"]) != 0:
            return
        svc.cancel(self.application.client, media.get("id"))
        self.refresh()

    def refresh(self):
        """Re-read after a change, keeping the focused row when it still exists."""
        focus = self.focus_id()
        self.reload()
        if self.row_of(focus) is not None:
            self.focus_row(self.row_of(focus))
        elif focus != SEARCH_BUTTON:
            self.focus_content()


class RequestDetailWindow(xbmcgui.WindowXMLDialog):
    """One title: status, Request, Notify me, Cancel and More like this."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.media = None
        self.feature = {}
        self.detail = None
        self.changed = False
        self.initialized = False
        self.primary = None
        self.shown = []
        self.recommendations = []

    def onInit(self):
        if self.initialized:
            self.focus_first()
            return
        self.initialized = True
        if self.application.guard(self.load) is None:
            self.close()
            return
        self.focus_first()

    def load(self):
        self.detail = svc.detail(self.application.client, self.media)
        self.show()
        return True

    # ------------------------------------------------------------ layout

    def show(self):
        detail = self.detail
        state = svc.result_state(detail)
        if state and state["key"] == "unavailable" and not state["reason"]:
            state = None
        set_properties(self, {
            "silo.title": svc._text(detail.get("title")) or svc._text(self.media.get("title")) or "Untitled",
            "silo.eyebrow": self.eyebrow(state),
            "silo.meta": self.meta(),
            "silo.plot": svc._text(detail.get("overview")),
            "silo.tagline": svc._text(detail.get("tagline")),
            "silo.credit": self.credit(),
            "silo.backdrop": svc.backdrop_url(detail) or svc.backdrop_url(self.media),
            "silo.poster": svc.poster_url(detail) or svc.poster_url(self.media),
            "silo.status": self.status_text(state),
            "silo.status_key": state["key"] if state else "",
            "silo.hint": "",
        })
        self.layout_buttons(state)
        self.recommendations = [media for media in svc._list(detail.get("recommendations")) if svc.is_supported(media)]
        recommendations = self.getControl(RECOMMENDATIONS)
        recommendations.reset()
        recommendations.addItems([list_item(media) for media in self.recommendations])
        self.setProperty("silo.has_recommendations", "true" if self.recommendations else "")
        recommendations.setVisible(bool(self.recommendations))
        self.getControl(RECOMMENDATIONS_HEADER).setVisible(bool(self.recommendations))

    @staticmethod
    def eyebrow(state):
        if state is None:
            return "Not in your library"
        return "" if state["key"] == "available" else "Requested"

    def meta(self):
        detail = self.detail
        parts = [svc.meta_text(detail)]
        parts.append(present.duration_text(detail.get("runtime")))
        seasons = svc._int(detail.get("number_of_seasons"))
        if seasons and seasons > 0:
            parts.append("1 season" if seasons == 1 else "{} seasons".format(seasons))
        rating = svc._text(detail.get("content_rating"))
        if rating:
            parts.append(rating)
        genres = [svc._text(genre) for genre in detail.get("genres") or [] if svc._text(genre)][:2]
        parts += genres
        return " · ".join(part for part in parts if part)

    def credit(self):
        detail = self.detail
        if svc._text(detail.get("director")):
            return "Directed by " + svc._text(detail["director"])
        creators = [svc._text(name) for name in detail.get("creators") or [] if svc._text(name)][:2]
        return "Created by " + ", ".join(creators) if creators else ""

    @staticmethod
    def status_text(state):
        if state is None:
            return ""
        if state["reason"] and state["key"] in ("declined", "failed", "unavailable"):
            return "{} · {}".format(state["long"], state["reason"]) if state["key"] != "unavailable" \
                else state["reason"]
        return state["long"]

    def primary_action(self, state):
        """(kind, label): open, request or status; None when nothing applies."""
        detail = self.detail
        request = svc._dict(detail.get("request"))
        if svc.library_item(detail):
            return "open", "Open in Library"
        if request.get("requestable") is True:
            ended = state is not None and state["key"] in ("declined", "failed")
            return "request", "Request Again" if ended else "Request"
        if state is not None and state["key"] != "unavailable":
            return "status", state["long"]
        return None

    def layout_buttons(self, state):
        detail = self.detail
        request = svc._dict(detail.get("request"))
        self.primary = self.primary_action(state)
        labels = {
            PRIMARY: self.primary[1] if self.primary else "",
            # A series in the library that is missing seasons can still be requested.
            MORE_SEASONS: "Request missing seasons" if (self.primary and self.primary[0] == "open"
                                                        and request.get("requestable") is True) else "",
            FOLLOW: svc.follow_label(detail) if svc.can_follow(detail, self.feature) else "",
            CANCEL: "Cancel request" if svc.cancellable_request(detail) else "",
        }
        shown, left = [], BUTTON_LEFT
        for control_id in BUTTONS:
            control = self.getControl(control_id)
            text = labels[control_id]
            control.setVisible(bool(text))
            if not text:
                continue
            width = button_width(text)
            control.setLabel(text)
            control.setPosition(left, BUTTON_TOP)
            control.setWidth(width)
            left += width + BUTTON_GAP
            shown.append(control_id)
        for before, after in zip(shown, shown[1:]):
            self.getControl(before).controlRight(self.getControl(after))
            self.getControl(after).controlLeft(self.getControl(before))
        recommendations = self.getControl(RECOMMENDATIONS)
        for control_id in shown:
            self.getControl(control_id).controlDown(recommendations)
        if shown:
            recommendations.controlUp(self.getControl(shown[0]))
        self.shown = shown

    def focus_first(self):
        if self.shown:
            self.setFocusId(self.shown[0])
        elif self.recommendations:
            self.setFocusId(RECOMMENDATIONS)

    # ------------------------------------------------------------ actions

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        if control_id == PRIMARY and self.primary:
            kind = self.primary[0]
            if kind == "open":
                self.application.open_item({"content_id": svc.library_item(self.detail)})
            elif kind == "request":
                self.request()
        elif control_id == MORE_SEASONS:
            self.request()
        elif control_id == FOLLOW:
            self.toggle_follow()
        elif control_id == CANCEL:
            self.cancel()
        elif control_id == RECOMMENDATIONS:
            position = self.getControl(RECOMMENDATIONS).getSelectedPosition()
            if 0 <= position < len(self.recommendations):
                if open_media(self.application, self.recommendations[position], feature=self.feature):
                    self.changed = True
                self.setFocusId(RECOMMENDATIONS)

    def choose_seasons(self):
        """Season numbers to request; [] for the server's default (every aired, missing season); None to stop."""
        seasons = svc.requestable_seasons(self.detail, self.feature)
        if not seasons:
            return []
        picked = self.application.dialog.multiselect("Choose seasons to request",
                                                     [svc.season_text(season) for season in seasons],
                                                     preselect=list(range(len(seasons))))
        if not picked:
            return None
        return [seasons[index]["season_number"] for index in picked]

    def request(self):
        seasons = self.choose_seasons()
        if seasons is None:
            return
        media = dict(self.media or {}, **self.detail)
        svc.create(self.application.client, media, seasons)
        self.changed = True
        self.reload("Request sent")

    def toggle_follow(self):
        following = svc._dict(self.detail.get("request")).get("following") is True
        svc.set_follow(self.application.client, self.detail, not following)
        self.changed = True
        self.reload("" if following else "You'll be notified when it's available.")

    def cancel(self):
        request_id = svc.cancellable_request(self.detail)
        if not request_id:
            return
        if not self.application.dialog.yesno("Cancel request", "Cancel your request for {}?".format(
                svc._text(self.detail.get("title")) or "this title")):
            return
        svc.cancel(self.application.client, request_id)
        self.changed = True
        self.reload("Request cancelled")

    def reload(self, hint=""):
        self.load()
        self.setProperty("silo.hint", hint)
        self.focus_first()
