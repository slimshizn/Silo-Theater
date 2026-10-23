"""The details screen, styled after the Silo Android TV app.

A hero (backdrop, title or logo, facts, synopsis) sits above a row of
actions. Series add season capsules and an episode rail; every title can
show its cast, trailers and extras, related titles and a facts table
further down. Round Watchlist and Favorite toggles and a More menu (shuffle,
mark watched, go to series or season, media info, translation) follow the
play actions. The page scrolls in zones:
Python moves focus up and down between zones and slides the page so the
focused zone is in view, while left and right stay native inside a zone.
The layout constants here match scripts/skin_details.py.
"""

import re
from urllib.parse import quote, urlsplit

import xbmc
import xbmcgui

from . import present, svc_personal, svc_shuffle
from .media import (available_versions, default_version, format_time, is_multipart, resume_position,
                    subtitle_choices, version_label, version_name)
from .ui_common import (ACTION_DOWN, ACTION_LEFT, ACTION_RIGHT, ACTION_UP, BACK_ACTIONS, Debouncer,
                        card_list_item, set_properties)

PLAY, RESUME, VERSION, START_OVER, NEXT_UP, SUBTITLES = 100, 101, 103, 104, 105, 106
WATCHLIST, FAVORITE, MORE = 107, 108, 109
SEASONS, EPISODES, CAST, SIMILAR, EXTRAS, FACTS = 510, 520, 530, 540, 560, 570
PAGE, CAST_GROUP, SIMILAR_GROUP, EXTRAS_GROUP, FACTS_GROUP = 1000, 3000, 4000, 5000, 6000
SUPERTITLE, TITLE, TITLE2 = 1101, 1102, 1103
CHIP, CHIP_LABEL, BADGE, BADGE_LABEL, META = 1201, 1202, 1211, 1221, 1230
# A button's group id is 1300 + its id; the order is the on-screen order.
BUTTON_ORDER = (NEXT_UP, PLAY, RESUME, START_OVER, VERSION, SUBTITLES, WATCHLIST, FAVORITE, MORE)
CAPSULES = (NEXT_UP, PLAY)
ZONES = ("buttons", "seasons", "episodes", "cast", "extras", "similar", "facts")
ZONE_CONTROLS = {"seasons": SEASONS, "episodes": EPISODES, "cast": CAST, "extras": EXTRAS,
                 "similar": SIMILAR, "facts": FACTS}

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
EXTRAS_HEIGHT = 400
SIMILAR_HEIGHT = 460
MAX_FACTS = 12
# A scrolled-to section's heading sits this far from the top of the screen.
SECTION_SCROLL_TOP = 140
SEASON_SETTLE_SECONDS = 0.3
MAX_CAST = 30

YOUTUBE_ADDON = "plugin.video.youtube"
YOUTUBE_PLAY = "plugin://plugin.video.youtube/play/?video_id={}"
YOUTUBE_THUMB = "https://i.ytimg.com/vi/{}/hqdefault.jpg"
_YOUTUBE_KEY = re.compile(r"[A-Za-z0-9_-]{6,32}")
EXTRA_KINDS = {"trailer": "Trailer", "teaser": "Teaser", "featurette": "Featurette", "clip": "Clip",
               "behind_the_scenes": "Behind the Scenes", "deleted_scene": "Deleted Scene",
               "interview": "Interview", "short": "Short", "scene": "Scene", "bloopers": "Bloopers"}
LANGUAGES = {"en": "English", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "ja": "Japanese",
             "ko": "Korean", "zh": "Chinese", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic", "hi": "Hindi",
             "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
             "tr": "Turkish", "cs": "Czech", "el": "Greek", "he": "Hebrew", "hu": "Hungarian", "th": "Thai"}
LANGUAGE_CODES = {"eng": "en", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "spa": "es", "ita": "it",
                  "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "por": "pt", "rus": "ru", "ara": "ar",
                  "hin": "hi", "dut": "nl", "nld": "nl", "swe": "sv", "dan": "da", "nor": "no", "fin": "fi",
                  "pol": "pl", "tur": "tr", "cze": "cs", "ces": "cs", "gre": "el", "ell": "el", "heb": "he",
                  "hun": "hu", "tha": "th"}
MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
          "November", "December")


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


# ------------------------------------------------------------- pure helpers
def is_unwatched(item):
    """An episode nobody on this profile has started (Android's spoiler rule)."""
    data = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
    state = item.get("user_state") if isinstance(item.get("user_state"), dict) else {}
    if data.get("played") is True or state.get("played") is True or data.get("is_in_progress") is True:
        return False
    position = data.get("position_seconds", item.get("position_seconds"))
    try:
        return float(position or 0) <= 0
    except (TypeError, ValueError):
        return True


def language_name(code):
    code = _text(code).lower()
    if not code or code == "und":
        return ""
    code = LANGUAGE_CODES.get(code, code)
    return LANGUAGES.get(code, code.upper())


def long_date(value):
    """``2024-03-09`` -> ``March 9, 2024``; other text is returned as is."""
    value = _text(value)
    parts = value[:10].split("-")
    try:
        year, month, day = int(parts[0]), int(parts[1]), int(parts[2])
        return "{} {}, {}".format(MONTHS[month - 1], day, year)
    except (ValueError, IndexError):
        return value


def _names(values, limit=3):
    names = [_text(value) for value in values if _text(value)] if isinstance(values, list) else []
    return ", ".join(names[:limit])


def _crew(item, tokens):
    crew = item.get("crew") if isinstance(item.get("crew"), list) else []
    names = []
    for person in crew:
        if not isinstance(person, dict):
            continue
        job, name = _text(person.get("job")).lower(), _text(person.get("name"))
        if name and name not in names and any(token in job for token in tokens):
            names.append(name)
    return ", ".join(names[:3]) + (", …" if len(names) > 3 else "")


def ratings_line(item):
    """Every external rating the server shows, as "IMDb 8.5 · TMDB 7.9"."""
    ratings = item.get("ratings") if isinstance(item.get("ratings"), list) else []
    parts = ["{} {}".format(_text(rating.get("name")), _text(rating.get("display"))).strip()
             for rating in ratings if isinstance(rating, dict) and _text(rating.get("display"))]
    if parts:
        return " · ".join(parts)
    fallback = []
    for key, name in (("rating_imdb", "IMDb"), ("rating_tmdb", "TMDB")):
        if isinstance(item.get(key), (int, float)) and not isinstance(item.get(key), bool) and item[key]:
            fallback.append("{} {:.1f}".format(name, item[key]))
    for key, name in (("rating_rt_critic", "Rotten Tomatoes"), ("rating_rt_audience", "RT Audience")):
        if isinstance(item.get(key), int) and not isinstance(item.get(key), bool):
            fallback.append("{} {}%".format(name, item[key]))
    return " · ".join(fallback)


def facts(item, version=None):
    """The Details table: (label, value) rows in the Android facts order."""
    rows = [("Director", _crew(item, ("director",))),
            ("Written by", _crew(item, ("writer", "screenplay", "story", "teleplay"))),
            ("Created by", _crew(item, ("creator",))),
            ("Studio", _names(item.get("studios"))),
            ("Network", _names(item.get("networks"))),
            ("Country", _names(item.get("countries")))]
    title, original = _text(item.get("title")), _text(item.get("original_title"))
    rows.append(("Original title", original if original and original != title else ""))
    rows.append(("Original language", language_name(item.get("original_language"))))
    if version:
        audio = []
        for track in version.get("audio_tracks") or []:
            name = language_name(track.get("language")) if isinstance(track, dict) else ""
            if name and name not in audio:
                audio.append(name)
        rows.append(("Audio", ", ".join(audio)))
    rows.append(("Genres", _names(item.get("genres"), limit=10)))
    if item.get("type") == "episode":
        rows.append(("Aired", long_date(item.get("air_date") or item.get("release_date"))))
    else:
        rows += [("Released", long_date(item.get("release_date"))),
                 ("First aired", long_date(item.get("first_air_date"))),
                 ("Last aired", long_date(item.get("last_air_date")))]
    rows.append(("Ratings", ratings_line(item)))
    return [(label, value) for label, value in rows if value][:MAX_FACTS]


def _codec(value):
    value = _text(value)
    return {"hevc": "HEVC", "h265": "HEVC", "h264": "H.264", "avc": "H.264", "subrip": "SRT",
            "hdmv_pgs_subtitle": "PGS", "webvtt": "VTT", "dca": "DTS"}.get(value.lower(), value.upper())


def _channels(count):
    return {1: "Mono", 2: "Stereo", 6: "5.1", 8: "7.1"}.get(count, "{}ch".format(count)) if count else ""


def _size(value):
    if not isinstance(value, (int, float)) or value <= 0:
        return ""
    gigabytes = value / 1024.0 ** 3
    return "{:.2f} GB".format(gigabytes) if gigabytes >= 1 else "{:.0f} MB".format(value / 1024.0 ** 2)


def _join(*parts):
    return " · ".join(_text(part) for part in parts if _text(part))


def media_info_text(version):
    """Container, video, audio and subtitle tracks of one file version."""
    if not version:
        return "No media details are available."
    lines = [version_name(version)]
    duration = version.get("duration")
    bitrate = version.get("bitrate")
    lines.append(_join(_text(version.get("container")).upper(), _text(version.get("resolution")),
                       _size(version.get("file_size")),
                       "{} kbps".format(bitrate) if isinstance(bitrate, int) and bitrate > 0 else "",
                       format_time(duration) if isinstance(duration, (int, float)) and duration > 0 else ""))
    video = [track for track in version.get("video_tracks") or [] if isinstance(track, dict)]
    lines += ["", "[B]Video[/B]"]
    if video:
        for track in video:
            size = "{}x{}".format(track["width"], track["height"]) if track.get("width") and track.get("height") else ""
            dynamic = _text(track.get("video_range_type") or track.get("video_range"))
            if track.get("dolby_vision") or track.get("dv_profile"):
                dynamic = _join(dynamic, "Dolby Vision" + (" P{}".format(track["dv_profile"])
                                                           if track.get("dv_profile") else ""))
            lines.append("• " + _join(_codec(track.get("codec")), size, dynamic,
                                      "{} fps".format(track["frame_rate"]) if track.get("frame_rate") else "",
                                      "{}-bit".format(track["bit_depth"]) if track.get("bit_depth") else ""))
    else:
        lines.append("• " + _join(_codec(version.get("codec_video")), version.get("resolution"),
                                  "HDR" if version.get("hdr") else ""))
    audio = [track for track in version.get("audio_tracks") or [] if isinstance(track, dict)]
    lines += ["", "[B]Audio[/B]"]
    if audio:
        for track in audio:
            lines.append("• " + _join(language_name(track.get("language")), track.get("title"),
                                      _codec(track.get("codec")), _channels(track.get("channels")),
                                      "Default" if track.get("default") else ""))
    else:
        lines.append("• " + (_codec(version.get("codec_audio")) or "Unknown"))
    subtitles = [track for track in version.get("subtitle_tracks") or [] if isinstance(track, dict)]
    if subtitles:
        lines += ["", "[B]Subtitles[/B]"]
        for track in subtitles:
            lines.append("• " + _join(language_name(track.get("language")) or "Unknown", track.get("title"),
                                      _codec(track.get("codec")), "Forced" if track.get("forced") else "",
                                      "SDH" if track.get("hearing_impaired") else "",
                                      "External" if track.get("external") else ""))
    return "\n".join(line for line in lines if line is not None)


def extra_kind_label(kind):
    kind = _text(kind).lower().replace("-", "_")
    return EXTRA_KINDS.get(kind) or (" ".join(word.capitalize() for word in kind.split("_") if word) or "Extra")


def duration_label(seconds):
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        return ""
    return format_time(seconds)


def trailer_entries(item, youtube_available):
    """Local extras first (they play in Silo), then YouTube videos when they can play."""
    entries, seen = [], set()
    for extra in item.get("extras") or []:
        if not isinstance(extra, dict) or not _text(extra.get("content_id")):
            continue
        key = "local:" + _text(extra["content_id"])
        if key in seen:
            continue
        seen.add(key)
        kind = extra_kind_label(extra.get("kind"))
        entries.append({"source": "local", "extra": extra, "title": _text(extra.get("title")) or kind,
                        "label2": _join(kind, duration_label(extra.get("duration_seconds"))),
                        "still": _text(extra.get("still_url"))})
    if youtube_available:
        for video in item.get("videos") or []:
            if not isinstance(video, dict) or _text(video.get("site")).lower() != "youtube":
                continue
            key = _text(video.get("site_key"))
            if not _YOUTUBE_KEY.fullmatch(key) or "remote:" + key in seen:
                continue
            seen.add("remote:" + key)
            kind = extra_kind_label(video.get("kind"))
            entries.append({"source": "youtube", "key": key, "title": _text(video.get("name")) or kind,
                            "label2": _join(kind, "YouTube"), "thumb": YOUTUBE_THUMB.format(key)})
    return entries


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
        # It starts from the remembered choice or the profile's preferences.
        self.subtitle_index = None
        self.subtitle_source = "auto"
        self.can_resume = False
        self.show_plot = True
        self.hide_spoilers = False
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
        self.extras = []
        self.facts = []
        self.visible_buttons = []
        self.focusable_buttons = []
        self.last_button = None
        self.focus_id = None
        self.hero_episode = None
        self.scroll_offset = 0
        self.cast_top = SECTIONS_TOP
        self.similar_top = SECTIONS_TOP
        self.extras_top = SECTIONS_TOP
        self.facts_top = SECTIONS_TOP
        # Server capabilities, read once per page when the More menu needs them.
        self.shuffle_kinds = None
        self.metadata_ai = None
        self.season_settle = Debouncer(self.season_settled, SEASON_SETTLE_SECONDS)

    # ------------------------------------------------------------ lifecycle
    def onInit(self):
        if self.initialized:
            return
        self.initialized = True
        options = self.application.options()
        self.show_plot = options["show_plot"]
        self.hide_spoilers = bool(options.get("hide_spoilers"))
        self.update_file_selection()
        self.default_subtitle()
        self.update_subtitle_selection()
        self.load_children()
        self.load_cast()
        self.load_extras()
        self.load_facts()
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
                self.subtitle_index, self.subtitle_source = chosen, "picked"
                version = default_version(self.item, preferred_file_id=self.selected_file_id)
                remember = getattr(self.application, "remember_subtitle", None)
                if callable(remember):
                    self.application.guard(lambda: remember(self.item, version, chosen))
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
        elif control_id in (WATCHLIST, FAVORITE):
            self.toggle_mark("watchlist" if control_id == WATCHLIST else "favorite")
        elif control_id == MORE:
            self.open_more()
        elif control_id == CAST:
            person = self.selected(CAST, self.cast)
            if person is not None and _text(person.get("person_id")):
                self.application.open_screen("person", person_id=_text(person["person_id"]),
                                             name=_text(person.get("name")))
        elif control_id == EXTRAS:
            entry = self.selected(EXTRAS, self.extras)
            if entry is not None:
                self.play_extra(entry)

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

    def default_subtitle(self):
        """Start from the remembered choice or the profile's subtitle preferences."""
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        try:
            index, source = self.application.subtitle_default(self.item, version)
        except (AttributeError, TypeError, ValueError):
            index, source = None, "auto"
        self.subtitle_index = index if isinstance(index, int) and not isinstance(index, bool) else None
        self.subtitle_source = source if source in ("auto", "remembered") else "auto"

    def update_subtitle_selection(self):
        choices = dict(self.subtitle_options())
        if self.subtitle_index not in choices:
            self.subtitle_index = None
        self.setProperty("silo.has_subtitles", "true" if choices else "")
        label = choices.get(self.subtitle_index, "Off")
        if self.subtitle_index is not None and self.subtitle_source == "auto":
            label += " (Auto)"  # Chosen from the profile's subtitle preferences.
        self.subtitle_label = "Subtitles: " + label
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
        changed_file = file_id != self.selected_file_id
        self.item = item
        self.selected_file_id = file_id
        if changed_file or after_playback:
            # Another version has its own tracks, and a pick made in the player
            # menu during playback is now the remembered choice.
            self.default_subtitle()
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
            WATCHLIST: bool(self.item.get("content_id")),
            FAVORITE: bool(self.item.get("content_id")),
            MORE: bool(self.item.get("content_id")),
        }
        marks = svc_personal.state(self.item)
        set_properties(self, {
            "silo.playable": "true" if playable else "",
            "silo.play_label": self.play_label,
            "silo.resume_button": "true" if shown[RESUME] else "",
            "silo.multi_version": "true" if shown[VERSION] else "",
            "silo.continue": "true" if shown[NEXT_UP] else "",
            "silo.marks": "true" if shown[MORE] else "",
            "silo.in_watchlist": "true" if marks["watchlist"] else "",
            "silo.favorite": "true" if marks["favorite"] else "",
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
        marks = svc_personal.state(self.item)
        if control_id == WATCHLIST:
            return "Remove from watchlist" if marks["watchlist"] else "Add to watchlist"
        if control_id == FAVORITE:
            return "Remove from favorites" if marks["favorite"] else "Add to favorites"
        if control_id == MORE:
            return "More"
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
        if episode is not None and self.spoiler_hidden(episode):
            overview = ""
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

    def spoiler_hidden(self, episode):
        return self.hide_spoilers and is_unwatched(episode)

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
            if self.spoiler_hidden(episode):
                # Spoiler protection: no still and no synopsis until the episode is started.
                entry.setArt({"thumb": "", "poster": "", "fanart": ""})
                entry.setProperty("silo.plot", "")
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
        """Stack the sections below the rail: cast, trailers, related, details."""
        has_rail = bool(self.seasons or self.episodes)
        top = SECTIONS_TOP_SERIES if has_rail else SECTIONS_TOP
        self.cast_top = top
        top += CAST_HEIGHT if self.cast else 0
        self.extras_top = top
        top += EXTRAS_HEIGHT if self.extras else 0
        self.similar_top = top
        top += SIMILAR_HEIGHT if self.similar else 0
        self.facts_top = top
        self.getControl(CAST_GROUP).setPosition(0, self.cast_top)
        self.getControl(EXTRAS_GROUP).setPosition(0, self.extras_top)
        self.getControl(SIMILAR_GROUP).setPosition(0, self.similar_top)
        self.getControl(FACTS_GROUP).setPosition(0, self.facts_top)

    # ----------------------------------------------------- trailers & extras
    def youtube_available(self):
        try:
            return bool(xbmc.getCondVisibility("System.HasAddon({})".format(YOUTUBE_ADDON)))
        except (AttributeError, RuntimeError):
            return False

    def load_extras(self):
        self.extras = trailer_entries(self.item, self.youtube_available())
        entries = []
        for extra in self.extras:
            entry = xbmcgui.ListItem(label=extra["title"], label2=extra["label2"])
            thumb = extra.get("thumb") or self.photo(extra.get("still"))
            if thumb:
                entry.setArt({"thumb": thumb})
            entries.append(entry)
        control = self.getControl(EXTRAS)
        control.reset()
        control.addItems(entries)
        self.setProperty("silo.has_extras", "true" if self.extras else "")

    def play_extra(self, entry):
        if entry["source"] == "youtube":
            # Trailers hand off to Kodi's YouTube add-on; the row hides them without it.
            xbmc.Player().play(YOUTUBE_PLAY.format(quote(entry["key"], safe="")))
            return
        # A local extra is a catalog item of type "extra" with its own versions.
        item = self.application.client.item(entry["extra"]["content_id"])
        if not isinstance(item, dict):
            return
        if not self.application.is_playable(item) and item.get("type") == "extra":
            item = dict(item, type="movie")  # Plays as a single title: no Up Next, no series context.
        self.application.play(item)

    # ---------------------------------------------------------------- facts
    def load_facts(self):
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        self.facts = facts(self.item, version)
        control = self.getControl(FACTS)
        control.reset()
        control.addItems([xbmcgui.ListItem(label=label.upper(), label2=value) for label, value in self.facts])
        self.setProperty("silo.has_facts", "true" if self.facts else "")

    # ------------------------------------------------------- personal marks
    def set_marks(self, **changes):
        """Apply watchlist/favorite/played changes to the page item locally."""
        keys = {"watchlist": "in_watchlist", "favorite": "is_favorite", "watched": "played"}
        item = dict(self.item)
        for key in ("user_state", "user_data"):
            state = dict(item.get(key) or {}) if isinstance(item.get(key), dict) else {}
            if key == "user_state" or state:
                for name, value in changes.items():
                    if key == "user_state" or keys[name] in state:
                        state[keys[name]] = value
                item[key] = state
        self.item = item

    def toggle_mark(self, kind):
        """Watchlist and Favorite answer 204, so the page flips its own state."""
        on = not svc_personal.state(self.item)[kind]
        setter = svc_personal.set_watchlist if kind == "watchlist" else svc_personal.set_favorite
        setter(self.application.client, self.item["content_id"], on)
        self.set_marks(**{kind: on})
        self.update_actions()
        self.setProperty("silo.hint", self.hint(WATCHLIST if kind == "watchlist" else FAVORITE))

    def toggle_watched(self):
        on = not svc_personal.state(self.item)["watched"]
        svc_personal.set_watched(self.application.client, self.item["content_id"], on)
        # Marking resets progress on the server; read the page and its episodes again.
        for season in self.seasons:
            season["user_data"] = dict(season.get("user_data") or {}, played=on)
        self.refresh(self.selected_file_id)
        self.reload_children()

    def set_season_watched(self, season, on):
        svc_personal.set_watched(self.application.client, season["content_id"], on)
        season["user_data"] = dict(season.get("user_data") or {}, played=on)
        self.refresh(self.selected_file_id)
        self.episode_cache.pop(season.get("content_id"), None)
        if self.seasons and self.seasons[self.season_index] is season:
            self.show_season(self.season_index)

    def reload_children(self):
        """Episodes changed on the server: drop the cache and show them again."""
        if self.item.get("type") == "season":
            episodes = self.application.guard(lambda: self.application.episodes(self.item))
            if isinstance(episodes, list):
                self.set_episodes(episodes)
        elif self.seasons:
            self.episode_cache.clear()
            self.show_season(self.season_index)

    # ------------------------------------------------------------ More menu
    def current_season(self):
        if self.item.get("type") == "series" and self.seasons and 0 <= self.season_index < len(self.seasons):
            return self.seasons[self.season_index]
        return None

    def can_shuffle(self, kind):
        if self.shuffle_kinds is None:
            try:
                document = svc_shuffle.capability(self.application.client)
            except Exception:  # Without the capability the menu just leaves shuffle out.
                document = {}
            kinds = document.get("scope_kinds") if document.get("state") == "available" else None
            self.shuffle_kinds = [kind for kind in kinds or [] if isinstance(kind, str)]
        return kind in self.shuffle_kinds

    def can_translate(self):
        if not _text(self.item.get("pending_translation_language")):
            return False
        if self.metadata_ai is None:
            try:
                document = self.application.client.request("GET", "/capabilities/metadata-ai")
            except Exception:  # Translation is optional; hide it when the server says nothing.
                document = None
            self.metadata_ai = document if isinstance(document, dict) else {}
        return self.metadata_ai.get("state") == "available" and self.metadata_ai.get("on_view", "off") != "off"

    def more_actions(self):
        """The More menu as (label, action) pairs, in the Android order."""
        item, actions = self.item, []
        kind, content_id = item.get("type"), item.get("content_id")
        season = self.current_season()
        if kind == "series" and self.can_shuffle("series"):
            actions.append(("Shuffle series", lambda: self.shuffle(series_id=content_id)))
        if season is not None and self.can_shuffle("season"):
            actions.append(("Shuffle " + present.season_label(season),
                            lambda: self.shuffle(season_id=season["content_id"])))
        if kind == "season" and self.can_shuffle("season"):
            actions.append(("Shuffle season", lambda: self.shuffle(season_id=content_id)))
        watched = svc_personal.state(item)["watched"]
        actions.append(("Mark as unwatched" if watched else "Mark as watched", self.toggle_watched))
        if season is not None:
            played = (season.get("user_data") or {}).get("played") is True
            actions.append(("Mark {} as {}".format(present.season_label(season), "unwatched" if played else "watched"),
                            lambda: self.set_season_watched(season, not played)))
        series_id = _text(item.get("series_id"))
        if kind == "episode" and series_id:
            number = item.get("season_number")
            if isinstance(number, int) and not isinstance(number, bool) and number > 0:
                actions.append(("Go to season {}".format(number), self.go_to_season))
        if kind in ("episode", "season") and series_id:
            actions.append(("Go to series", self.go_to_series))
        if default_version(item, preferred_file_id=self.selected_file_id):
            actions.append(("Media info", self.show_media_info))
        if self.can_translate():
            actions.append(("Translate description", self.translate_description))
        return actions

    def open_more(self):
        actions = self.more_actions()
        chosen = self.application.dialog.contextmenu([label for label, _ in actions])
        if isinstance(chosen, int) and 0 <= chosen < len(actions):
            actions[chosen][1]()

    def shuffle(self, **scope):
        played = svc_shuffle.play_shuffle(self.application, **scope)
        if played:
            self.refresh(self.selected_file_id)
            self.reload_children()

    def go_to_series(self):
        self.application.open_item({"content_id": self.item["series_id"]})

    def go_to_season(self):
        series_id, number = self.item["series_id"], self.item.get("season_number")
        response = self.application.client.seasons(series_id) or {}
        season = next((entry for entry in response.get("items", [])
                       if isinstance(entry, dict) and entry.get("season_number") == number
                       and entry.get("content_id")), None)
        self.application.open_item(dict(season, type="season") if season else {"content_id": series_id})

    def show_media_info(self):
        version = default_version(self.item, preferred_file_id=self.selected_file_id)
        self.application.dialog.textviewer("Media info", media_info_text(version))

    def translate_description(self):
        language = _text(self.item.get("pending_translation_language"))
        self.application.client.request(
            "POST", "/catalog/items/{}/translate-description".format(quote(str(self.item["content_id"]), safe="")),
            body={"target_language": language})
        # The server translates in the background; read the page once more.
        self.refresh(self.selected_file_id)
        if _text(self.item.get("pending_translation_language")):
            self.application.dialog.notification("Silo", "The translated description will appear when it is ready.")

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
            "extras": bool(self.extras),
            "similar": bool(self.similar),
            "facts": bool(self.facts),
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
        tops = {"cast": self.cast_top, "extras": self.extras_top, "similar": self.similar_top,
                "facts": self.facts_top}
        offset = max(0, tops[zone] - SECTION_SCROLL_TOP) if zone in tops else 0
        if offset != self.scroll_offset:
            self.scroll_offset = offset
            self.getControl(PAGE).setPosition(0, -offset)
