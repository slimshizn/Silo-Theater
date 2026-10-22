"""Kodi-only presentation adapters for Silo's original fullscreen interface."""

import xbmcgui

from .media import available_versions, default_version, format_time, is_multipart, version_label, version_name


BACK_ACTIONS = {9, 10, 92, 216, 247, 257, 61448}


def item_title(item):
    return str(item.get("title") or item.get("name") or "Untitled")


def item_subtitle(item):
    parts = []
    if item.get("year"):
        parts.append(str(item["year"]))
    if item.get("content_rating"):
        parts.append(str(item["content_rating"]))
    if item.get("runtime"):
        parts.append("{} min".format(item["runtime"]))
    if item.get("season_number") is not None and item.get("episode_number") is not None:
        parts.append("S{:02d} · E{:02d}".format(
            int(item["season_number"]), int(item["episode_number"])))
    media_type = item.get("type") or item.get("media_type")
    if media_type:
        parts.append(str(media_type).replace("_", " ").title())
    return "  ·  ".join(parts)


def make_list_item(item, application):
    result = xbmcgui.ListItem(label=item_title(item), label2=item_subtitle(item))
    result.setProperty("silo.plot", str(item.get("overview") or item.get("description") or ""))
    art = application.artwork(item)
    if art:
        result.setArt(art)
    return result


class HomeWindow(xbmcgui.WindowXML):
    """Home hubs and browse/search results share one remote-friendly screen."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.sections = []
        self.items = []
        self.result = None
        self.initialized = False

    def onInit(self):
        if self.initialized:
            return
        self.initialized = True
        self.setProperty("silo.profile", self.application.profile_name)
        self.setProperty("silo.profile_locked", "true" if self.application.store.get("temporary_session") else "")
        self.setProperty("silo.server", self.application.server_name)
        self.application.guard(self.load_home)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        if control_id == 100:
            self.load_home()
        elif control_id == 101:
            self.application.search(self)
        elif control_id == 102:
            self.application.libraries(self)
        elif control_id in (103, 104, 105):
            if control_id == 103 and self.application.store.get("temporary_session"):
                return
            self.result = {103: "profile", 104: "settings", 105: "logout"}[control_id]
            self.close()
        elif control_id == 300 and self.sections:
            position = self.getControl(300).getSelectedPosition()
            if 0 <= position < len(self.sections):
                section = self.sections[position]
                self.application.show_section(self, section)
        elif control_id == 500 and self.items:
            position = self.getControl(500).getSelectedPosition()
            if 0 <= position < len(self.items):
                self.application.open_item(self.items[position])
        elif control_id == 106:
            self.application.next_page(self)

    def load_home(self):
        self.application.home(self)

    def display(self, heading, sections, items, subtitle="", has_more=False):
        self.setProperty("silo.heading", str(heading))
        self.setProperty("silo.subtitle", str(subtitle))
        self.setProperty("silo.empty", "" if items else "No titles to display.")
        self.setProperty("silo.more", "true" if has_more else "")
        self.sections = sections
        section_control = self.getControl(300)
        section_control.reset()
        section_control.addItems([
            xbmcgui.ListItem(label=str(section["title"])) for section in sections
        ])
        self.items = items
        control = self.getControl(500)
        control.reset()
        control.addItems([make_list_item(item, self.application) for item in items])
        self.setFocusId(500 if items else 300 if sections else 100)


class DetailsWindow(xbmcgui.WindowXML):
    """A title's metadata and actions, with season/episode navigation."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.item = {}
        self.children = []
        self.initialized = False
        self.selected_file_id = None
        self.file_confirmed = False

    def onInit(self):
        if self.initialized:
            return
        self.initialized = True
        self.setProperty("silo.title", item_title(self.item))
        self.setProperty("silo.meta", item_subtitle(self.item))
        options = self.application.options()
        show_plot = options["show_plot"]
        blocks = []
        overview = self.item.get("overview") or self.item.get("description")
        if show_plot and overview:
            blocks.append(str(overview))
        cast = self.item.get("cast", [])
        if cast:
            blocks.append("Cast: " + ", ".join(str(credit["name"]) for credit in cast[:6] if credit.get("name")))
        ratings = self.item.get("ratings", [])
        if ratings:
            blocks.append("  ·  ".join("{} {}".format(rating.get("name", ""), rating.get("display", ""))
                                       for rating in ratings))
        self.setProperty("silo.plot", "\n\n".join(blocks))
        self.setProperty("silo.has_summary", "true" if blocks else "")
        art = self.application.artwork(self.item)
        self.setProperty("silo.poster", art.get("poster", ""))
        self.setProperty("silo.fanart", art.get("fanart", ""))
        playable = self.application.is_playable(self.item)
        self.setProperty("silo.playable", "true" if playable else "")
        self.setProperty("silo.continue", "true" if self.item.get("type") in ("series", "season")
                         and self.item.get("play_content_id") else "")
        self.update_file_selection()
        self.update_resume()
        self.setProperty("silo.children", "true" if self.children else "")
        self.setProperty("silo.children_label", "Seasons" if self.item.get("type") == "series" else "Episodes")
        self.getControl(500).addItems([
            make_list_item(item, self.application) for item in self.children
        ])
        # Play applies the saved preference; Resume/Start are explicit alternatives.
        self.setFocusId(100 if playable and self.selected_file_id else 500 if self.children else 102)

    def update_file_selection(self):
        version = default_version(self.item, self.application.options()["file_selection"], self.selected_file_id)
        self.selected_file_id = version["file_id"] if version else None
        self.setProperty("silo.has_file", "true" if version else "")
        if version:
            label, name = version_label(version), version_name(version)
            self.setProperty("silo.file_label", "File: " + label + (" — " + name if name != label else ""))
        else:
            self.setProperty("silo.file_label", "File: No media files available")

    def update_resume(self):
        resume = self.application.resume_seconds(self.item)
        playable = self.application.is_playable(self.item)
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        # A shared item offset is not a valid seek point inside a multipart file.
        self.setProperty("silo.resume", "true" if playable and resume > 0 and not is_multipart(version) else "")
        self.setProperty("silo.resume_label", "Resume from {}".format(format_time(resume)))
        return resume

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        if control_id == 102:
            self.close()
        elif control_id == 103:
            file_id = self.application.choose_file(self.item, self.selected_file_id)
            if file_id is not None:
                self.refresh(file_id)
        elif control_id == 105 and self.item.get("play_content_id"):
            self.application.open_item({"content_id": self.item["play_content_id"]})
        elif control_id in (100, 101, 104):
            resume = {100: None, 101: True, 104: False}[control_id]
            file_id = self.application.play(self.item, resume=resume,
                                            file_id=self.selected_file_id if self.file_confirmed else None)
            if file_id is not None:
                self.refresh(file_id)
        elif control_id == 500:
            position = self.getControl(500).getSelectedPosition()
            if 0 <= position < len(self.children):
                self.application.open_item(self.children[position])

    def refresh(self, file_id):
        item = self.application.refresh_item(self.item, file_id)
        self.item = item
        self.selected_file_id = file_id
        self.file_confirmed = any(version["file_id"] == file_id for version in available_versions(item))
        self.update_file_selection()
        self.update_resume()
