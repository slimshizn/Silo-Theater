"""The details screen, styled after the Silo Android TV app.

A hero (backdrop, title or logo, facts, synopsis) sits above a row of
actions. Series add season capsules and an episode rail; every title can
show its cast and related titles further down. The page scrolls in zones:
Python moves focus up and down between zones and slides the page so the
focused zone is in view, while left and right stay native inside a zone.
The layout constants here match scripts/skin_details.py.
"""

from urllib.parse import urlsplit

import xbmcgui

from . import present
from .media import (available_versions, default_version, format_time, is_multipart, resume_position,
                    subtitle_choices, version_label, version_name)
from .ui_common import (ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer,
                        card_list_item, set_properties)

PLAY, RESUME, VERSION, START_OVER, NEXT_UP, SUBTITLES = 100, 101, 103, 104, 105, 106
SEASONS, EPISODES, CAST, SIMILAR = 510, 520, 530, 540
PAGE, CAST_GROUP, SIMILAR_GROUP = 1000, 3000, 4000
SUPERTITLE, TITLE, TITLE2 = 1101, 1102, 1103
CHIP, CHIP_LABEL, BADGE, BADGE_LABEL, META = 1201, 1202, 1211, 1221, 1230
# A button's group id is 1300 + its id; the order is the on-screen order.
BUTTON_ORDER = (NEXT_UP, PLAY, RESUME, START_OVER, VERSION, SUBTITLES)
CAPSULES = (NEXT_UP, PLAY)
ZONES = ("buttons", "seasons", "episodes", "cast", "similar")
ZONE_CONTROLS = {"seasons": SEASONS, "episodes": EPISODES, "cast": CAST, "similar": SIMILAR}

LEFT = 100
TEXT_RIGHT = LEFT + 1080
TITLE_BOTTOM = 276
META_TOP = 294
BUTTON_TOP = 556
BUTTON_GAP = 18
CIRCLE = 76
MAX_BADGES = 5
# Where the cast section starts below the action row, or below the episode rail.
SECTIONS_TOP = 700
SECTIONS_TOP_SERIES = 1100
CAST_HEIGHT = 400
# A scrolled-to section's heading sits this far from the top of the screen.
SECTION_SCROLL_TOP = 140
SEASON_SETTLE_SECONDS = 0.3
MAX_CAST = 30


def _text(value):
    return str(value).strip() if value not in (None, "") else ""


def split_title(title):
    """Android puts a subtitle after ``:`` or a dash on its own, smaller line."""
    for separator in (": ", " — ", " – "):
        head, found, tail = title.partition(separator)
        if found and head.strip() and tail.strip():
            return head.strip(), tail.strip()
    return title, ""


def capsule_width(text):
    # Icon and padding, plus roughly 15 px per character of font25_title.
    return max(170, 74 + 15 * len(text) + 34)


def chip_width(text):
    return 24 + 13 * len(text)


class DetailsWindow(xbmcgui.WindowXML):
    """A title's metadata and actions, with season/episode navigation."""

    def __init__(self, *args, **kwargs):
        self.application = None
        self.item = {}
        self.children = []
        self.initialized = False
        self.selected_file_id = None
        self.file_confirmed = False
        # Playback ordinal of the chosen subtitle; None plays without subtitles.
        self.subtitle_index = None
        self.can_resume = False
        self.show_plot = True
        self.resume_label = ""
        self.subtitle_label = ""
        self.play_label = "Play"
        self.continue_label = "Play"
        self.seasons = []
        self.season_index = 0
        self.episodes = []
        self.episode_cache = {}
        self.cast = []
        self.similar = []
        self.visible_buttons = []
        self.focusable_buttons = []
        self.last_button = None
        self.focus_id = None
        self.hero_episode = None
        self.scroll_offset = 0
        self.cast_top = SECTIONS_TOP
        self.similar_top = SECTIONS_TOP
        self.season_settle = Debouncer(self.season_settled, SEASON_SETTLE_SECONDS)

    # ------------------------------------------------------------ lifecycle
    def onInit(self):
        if self.initialized:
            return
        self.initialized = True
        self.show_plot = self.application.options()["show_plot"]
        self.update_file_selection()
        self.update_subtitle_selection()
        self.load_children()
        self.load_cast()
        self.update_actions()
        self.show_hero()
        self.layout_sections()
        # Play applies the saved preference; Resume/Start are explicit alternatives.
        zones = self.available_zones()
        if PLAY in self.focusable_buttons:
            self.focus_control(PLAY)
        elif zones:
            self.focus_zone(zones[0])
        self.load_similar()

    def onAction(self, action):
        action_id = action.getId()
        if action_id in BACK_ACTIONS:
            self.season_settle.cancel()
            self.close()
        elif action_id in (ACTION_UP, ACTION_DOWN):
            self.move_zone(-1 if action_id == ACTION_UP else 1)
        elif action_id in (ACTION_LEFT, ACTION_RIGHT):
            focused = self.focused()
            if focused == SEASONS:
                self.season_settle.trigger()
            elif focused == EPISODES:
                self.show_focused_episode()

    def onFocus(self, control_id):
        self.focus_id = control_id
        zone = self.zone_of(control_id)
        if zone == "buttons":
            self.last_button = control_id
        if zone == "episodes":
            self.show_focused_episode()
        elif self.hero_episode is not None:
            self.hero_episode = None
            self.show_hero()
        self.setProperty("silo.hint", self.hint(control_id))
        if zone:
            self.scroll_to(zone)

    def onClick(self, control_id):
        self.application.guard(lambda: self._click(control_id))

    def _click(self, control_id):
        if control_id == VERSION:
            file_id = self.application.choose_file(self.item, self.selected_file_id)
            if file_id is not None:
                self.refresh(file_id)
        elif control_id == SUBTITLES:
            choices = self.subtitle_options()
            chosen = self.application.choose_subtitle(choices, self.subtitle_index)
            if chosen is not False:
                self.subtitle_index = chosen
                self.update_subtitle_selection()
                self.setProperty("silo.hint", self.hint(SUBTITLES))
        elif control_id == NEXT_UP and self.item.get("play_content_id"):
            if self.application.play_next(self.item) is not None:
                self.reload_episodes()
        elif control_id in (PLAY, RESUME, START_OVER):
            resume = {PLAY: None, RESUME: True, START_OVER: False}[control_id]
            # Play the version shown on this screen; Version is how to change it.
            file_id = self.application.play(self.item, resume=resume, file_id=self.selected_file_id,
                                            subtitle_index=self.subtitle_index)
            if file_id is not None:
                self.refresh(file_id, after_playback=True)
        elif control_id == SEASONS:
            self.season_settle.cancel()
            self.season_settled()
        elif control_id == EPISODES:
            episode = self.selected(EPISODES, self.episodes)
            if episode is not None:
                self.application.open_item(episode)
        elif control_id == SIMILAR:
            item = self.selected(SIMILAR, self.similar)
            if item is not None:
                self.application.open_item(item)

    def selected(self, control_id, items):
        position = self.getControl(control_id).getSelectedPosition()
        return items[position] if 0 <= position < len(items) else None

    # ------------------------------------------------------------- versions
    def update_file_selection(self):
        version = default_version(self.item, self.application.options()["file_selection"], self.selected_file_id)
        self.selected_file_id = version["file_id"] if version else None
        self.setProperty("silo.has_file", "true" if version else "")
        if version:
            label, name = version_label(version), version_name(version)
            self.setProperty("silo.file_label", "File: " + label + (" — " + name if name != label else ""))
        else:
            self.setProperty("silo.file_label", "File: No media files available")

    def subtitle_options(self):
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        return subtitle_choices(version)

    def update_subtitle_selection(self):
        choices = dict(self.subtitle_options())
        if self.subtitle_index not in choices:
            self.subtitle_index = None
        self.setProperty("silo.has_subtitles", "true" if choices else "")
        self.subtitle_label = "Subtitles: " + choices.get(self.subtitle_index, "Off")
        self.setProperty("silo.subtitle_label", self.subtitle_label)

    def update_resume(self):
        resume = self.application.resume_seconds(self.item)
        playable = self.application.is_playable(self.item)
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        # A shared item offset is not a valid seek point inside a multipart file.
        self.can_resume = bool(playable and resume > 0 and not is_multipart(version))
        self.resume_label = "Resume from {}".format(format_time(resume))
        self.setProperty("silo.resume", "true" if self.can_resume else "")
        self.setProperty("silo.resume_label", self.resume_label)
        return resume

    def refresh(self, file_id, after_playback=False):
        item = (self.application.item_after_playback(self.item, file_id) if after_playback
                else self.application.refresh_item(self.item, file_id))
        if file_id != self.selected_file_id:
            # Another version has its own subtitle tracks.
            self.subtitle_index = None
        self.item = item
        self.selected_file_id = file_id
        self.file_confirmed = any(version["file_id"] == file_id for version in available_versions(item))
        self.update_file_selection()
        self.update_subtitle_selection()
        self.update_actions()
        if self.hero_episode is None:
            self.show_hero()

    # -------------------------------------------------------------- actions
    def update_actions(self):
        """Work out which buttons show, their labels, and lay the row out."""
        resume = self.update_resume()
        playable = bool(self.application.is_playable(self.item))
        start_first = self.application.options()["resume_behavior"] == "start"
        has_file = self.selected_file_id is not None
        self.play_label = "Resume " + format_time(resume) if self.can_resume and not start_first else "Play"
        shown = {
            NEXT_UP: bool(self.item.get("type") in ("series", "season") and self.item.get("play_content_id")),
            PLAY: playable,
            # With "start", Play starts over, so Resume needs its own button.
            RESUME: self.can_resume and start_first,
            START_OVER: self.can_resume,
            VERSION: playable and len(available_versions(self.item)) > 1,
            SUBTITLES: playable and bool(self.subtitle_options()),
        }
        set_properties(self, {
            "silo.playable": "true" if playable else "",
            "silo.play_label": self.play_label,
            "silo.resume_button": "true" if shown[RESUME] else "",
            "silo.multi_version": "true" if shown[VERSION] else "",
            "silo.continue": "true" if shown[NEXT_UP] else "",
        })
        self.update_next_up_label()
        needs_file = (PLAY, RESUME, START_OVER, VERSION)
        self.visible_buttons = [control_id for control_id in BUTTON_ORDER if shown[control_id]]
        self.focusable_buttons = [control_id for control_id in self.visible_buttons
                                  if has_file or control_id not in needs_file]
        self.layout_buttons()

    def next_up_episode(self):
        content_id = self.item.get("play_content_id")
        if not content_id:
            return None
        for episodes in [self.episodes] + list(self.episode_cache.values()):
            for episode in episodes:
                if episode.get("content_id") == content_id:
                    return episode
        return None

    def update_next_up_label(self):
        verb = "Resume" if resume_position(self.item) > 0 else "Play"
        episode = self.next_up_episode() or {}
        season, number = episode.get("season_number"), episode.get("episode_number")
        if self.item.get("type") == "series" and season is not None and number is not None:
            label = "{} S{}:E{}".format(verb, season, number)
        elif number is not None:
            label = "{} E{}".format(verb, number)
        else:
            label = verb
        self.continue_label = label
        self.setProperty("silo.continue_label", label)

    def layout_buttons(self):
        x = LEFT
        for control_id in self.visible_buttons:
            if control_id in CAPSULES:
                width = capsule_width(self.continue_label if control_id == NEXT_UP else self.play_label)
                self.getControl(control_id).setWidth(width)
            else:
                width = CIRCLE
            self.getControl(1300 + control_id).setPosition(x, BUTTON_TOP)
            x += width + BUTTON_GAP

    def hint(self, control_id):
        """Android widens a focused circle to name it; Kodi shows a line below."""
        if control_id == RESUME:
            return self.resume_label
        if control_id == START_OVER:
            return "Start over"
        if control_id == VERSION:
            version = default_version(self.item, preferred_file_id=self.selected_file_id)
            return "Version: " + version_label(version) if version else ""
        if control_id == SUBTITLES:
            return self.subtitle_label
        return ""

    # ----------------------------------------------------------------- hero
    def show_hero(self, episode=None):
        """Fill the hero from the page item, or from a focused episode."""
        item = episode or self.item
        image = self.application.image
        is_episode = episode is not None or item.get("type") == "episode"
        if episode is not None:
            supertitle = _text(self.item.get("title") or self.item.get("name"))
        else:
            supertitle = _text(item.get("series_title")) if is_episode else ""
        title, title2 = split_title(_text(item.get("title") or item.get("name")) or "Untitled")
        version = None if episode is not None else default_version(item, preferred_file_id=self.selected_file_id)
        rating = _text(item.get("content_rating") or (self.item.get("content_rating") if episode else ""))
        overview = _text(item.get("overview") or item.get("description")) if self.show_plot else ""
        values = {
            "silo.supertitle": supertitle,
            "silo.title": title.upper(),
            "silo.title2": title2.upper(),
            "silo.logo": "" if is_episode else image(item, "logo"),
            "silo.rating": rating,
            "silo.meta": present.detail_meta(item),
            "silo.plot": overview,
            # The credit stays the page's own so the rows below never jump.
            "silo.credit": present.credit_line(self.item),
        }
        if episode is None:
            values["silo.backdrop"] = image(item, "backdrop")
        set_properties(self, values)
        self.layout_title(bool(supertitle), bool(title2))
        self.layout_meta(rating, [badge for badge in present.badges(item, version) if badge != rating])

    def layout_title(self, has_supertitle, has_second_line):
        bottom = TITLE_BOTTOM
        if has_second_line:
            bottom -= 50
            self.getControl(TITLE2).setPosition(LEFT, bottom)
        bottom -= 80
        self.getControl(TITLE).setPosition(LEFT, bottom)
        if has_supertitle:
            self.getControl(SUPERTITLE).setPosition(LEFT, bottom - 40)

    def layout_meta(self, rating, badges):
        """Rating chip, then quality badges, then the facts text on one line."""
        x = LEFT
        if rating:
            width = chip_width(rating)
            for control_id in (CHIP, CHIP_LABEL):
                self.getControl(control_id).setPosition(x, META_TOP)
                self.getControl(control_id).setWidth(width)
            x += width + 14
        badges = badges[:MAX_BADGES]
        for slot in range(MAX_BADGES):
            text = badges[slot] if slot < len(badges) else ""
            self.setProperty("silo.badge.{}".format(slot), text)
            if text:
                width = present.badge_width(text)
                for control_id in (BADGE + slot, BADGE_LABEL + slot):
                    self.getControl(control_id).setPosition(x, META_TOP)
                    self.getControl(control_id).setWidth(width)
                x += width + 10
        if x > LEFT:
            x += 4
        meta = self.getControl(META)
        meta.setPosition(x, META_TOP)
        meta.setWidth(max(1, TEXT_RIGHT - x))

    def show_focused_episode(self):
        episode = self.selected(EPISODES, self.episodes)
        if episode is not None and episode is not self.hero_episode:
            self.hero_episode = episode
            self.show_hero(episode)

    # ------------------------------------------------------ seasons/episodes
    def load_children(self):
        kind = self.item.get("type")
        if kind == "series":
            self.seasons = present.ordered_seasons(self.children)
            self.setProperty("silo.has_seasons", "true" if self.seasons else "")
            if self.seasons:
                self.season_index = present.initial_season_index(self.item, self.seasons)
                control = self.getControl(SEASONS)
                control.reset()
                control.addItems([xbmcgui.ListItem(label=present.season_label(season)) for season in self.seasons])
                control.selectItem(self.season_index)
                self.show_season(self.season_index)
                return
        elif kind == "season":
            self.setProperty("silo.has_seasons", "")
            self.set_episodes([dict(child, type="episode") for child in self.children if isinstance(child, dict)])
            return
        self.setProperty("silo.has_seasons", "")
        self.set_episodes([])

    def season_settled(self):
        """Load the season the capsule row settled on."""
        if not self.seasons:
            return
        position = self.getControl(SEASONS).getSelectedPosition()
        if 0 <= position < len(self.seasons) and position != self.season_index:
            self.season_index = position
            self.show_season(position)

    def show_season(self, index):
        season = self.seasons[index]
        key = season.get("content_id")
        episodes = self.episode_cache.get(key)
        if episodes is None:
            episodes = self.application.guard(lambda: self.application.episodes(season))
            if not isinstance(episodes, list):
                episodes = []
            else:
                self.episode_cache[key] = episodes
        if index == self.season_index:
            self.set_episodes(episodes)

    def reload_episodes(self):
        """Progress changed after playback; fetch the shown season again."""
        if self.seasons:
            self.episode_cache.pop(self.seasons[self.season_index].get("content_id"), None)
            self.show_season(self.season_index)

    def set_episodes(self, episodes):
        self.episodes = [episode for episode in episodes if isinstance(episode, dict)]
        control = self.getControl(EPISODES)
        control.reset()
        entries = []
        for episode in self.episodes:
            entry = card_list_item(episode, self.application.image, wide=True)
            first, second = present.episode_caption(episode)
            entry.setLabel(first)
            entry.setLabel2(second)
            entries.append(entry)
        control.addItems(entries)
        play_id = self.item.get("play_content_id")
        position = next((index for index, episode in enumerate(self.episodes)
                         if play_id and episode.get("content_id") == play_id), 0)
        if self.episodes:
            control.selectItem(position)
        self.setProperty("silo.has_episodes", "true" if self.episodes else "")
        if self.hero_episode is not None:
            self.hero_episode = None
            if self.focused() == EPISODES:
                self.show_focused_episode()
            else:
                self.show_hero()
        if self.initialized:
            self.update_next_up_label()
            self.layout_buttons()
            self.layout_sections()

    # ------------------------------------------------------- cast & similar
    def photo(self, url):
        url = _text(url)
        if not url:
            return ""
        if urlsplit(url).scheme == "https":
            return url
        return self.application.image({"poster_url": url}, "poster")

    def load_cast(self):
        credits = self.item.get("cast") if isinstance(self.item.get("cast"), list) else []
        self.cast = [person for person in credits if isinstance(person, dict) and _text(person.get("name"))][:MAX_CAST]
        entries = []
        for person in self.cast:
            name = _text(person["name"])
            entry = xbmcgui.ListItem(label=name, label2=_text(person.get("character")))
            photo = self.photo(person.get("photo_url"))
            if photo:
                entry.setArt({"thumb": photo})
            entry.setProperty("silo.initials", present.initials(name))
            entries.append(entry)
        control = self.getControl(CAST)
        control.reset()
        control.addItems(entries)
        self.setProperty("silo.has_cast", "true" if self.cast else "")

    def load_similar(self):
        try:
            items = self.application.similar(self.item)
        except Exception:  # The row is optional; never let it break the page.
            items = []
        self.similar = present.video_items(items) if isinstance(items, list) else []
        control = self.getControl(SIMILAR)
        control.reset()
        control.addItems([card_list_item(item, self.application.image) for item in self.similar])
        self.setProperty("silo.has_similar", "true" if self.similar else "")
        self.layout_sections()

    def layout_sections(self):
        has_rail = bool(self.seasons or self.episodes)
        self.cast_top = SECTIONS_TOP_SERIES if has_rail else SECTIONS_TOP
        self.similar_top = self.cast_top + (CAST_HEIGHT if self.cast else 0)
        self.getControl(CAST_GROUP).setPosition(0, self.cast_top)
        self.getControl(SIMILAR_GROUP).setPosition(0, self.similar_top)

    # ----------------------------------------------------------- navigation
    def focused(self):
        try:
            return self.getFocusId()
        except (AttributeError, RuntimeError):
            return self.focus_id

    def focus_control(self, control_id):
        self.focus_id = control_id
        self.setFocusId(control_id)

    def zone_of(self, control_id):
        if control_id in BUTTON_ORDER:
            return "buttons"
        return next((zone for zone, zone_control in ZONE_CONTROLS.items() if zone_control == control_id), None)

    def available_zones(self):
        present_zones = {
            "buttons": bool(self.focusable_buttons),
            "seasons": bool(self.seasons),
            "episodes": bool(self.episodes),
            "cast": bool(self.cast),
            "similar": bool(self.similar),
        }
        return [zone for zone in ZONES if present_zones[zone]]

    def move_zone(self, step):
        zones = self.available_zones()
        current = self.zone_of(self.focused())
        if not zones:
            return
        if current not in zones:
            target = zones[0]
        else:
            index = zones.index(current) + step
            if not 0 <= index < len(zones):
                return
            target = zones[index]
        self.focus_zone(target)

    def focus_zone(self, zone):
        if zone == "buttons":
            if self.last_button in self.focusable_buttons:
                control_id = self.last_button
            elif PLAY in self.focusable_buttons:
                control_id = PLAY
            else:
                control_id = self.focusable_buttons[0]
        else:
            control_id = ZONE_CONTROLS[zone]
        self.focus_control(control_id)
        self.scroll_to(zone)

    def scroll_to(self, zone):
        if zone == "cast":
            offset = max(0, self.cast_top - SECTION_SCROLL_TOP)
        elif zone == "similar":
            offset = max(0, self.similar_top - SECTION_SCROLL_TOP)
        else:
            offset = 0
        if offset != self.scroll_offset:
            self.scroll_offset = offset
            self.getControl(PAGE).setPosition(0, -offset)
