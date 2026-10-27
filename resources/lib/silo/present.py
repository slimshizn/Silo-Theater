"""Turn Silo catalog items into the text and art the skin shows.

Mirrors what the Silo Android TV app puts on its cards, hero and details
page. Field names follow Silo's /api/v2 catalog responses. This module has
no Kodi dependency so it can be tested directly.
"""

import math
from urllib.parse import urlsplit, urlunsplit

VIDEO_TYPES = frozenset({"movie", "series", "season", "episode"})
WIDE_SECTION_TYPES = frozenset({"continue_watching", "next_up"})


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _int(value):
    number = _number(value)
    return int(number) if number is not None else None


def _text(value):
    return str(value).strip() if value not in (None, "") else ""


def is_video(item):
    return isinstance(item, dict) and (item.get("type") or item.get("media_type")) in VIDEO_TYPES


def video_items(items):
    return [item for item in items or [] if is_video(item) and item.get("content_id")]


def is_wide_section(section):
    return section.get("section_type") in WIDE_SECTION_TYPES


def episode_code(item, separator=" · "):
    """``S2 · E8`` for an episode, or "" when either number is missing."""
    season, episode = _int(item.get("season_number")), _int(item.get("episode_number"))
    if season is None or episode is None:
        return ""
    return "S{}{}E{}".format(season, separator, episode)


def duration_text(minutes):
    minutes = _int(minutes)
    if not minutes or minutes <= 0:
        return ""
    if minutes < 60:
        return "{} min".format(minutes)
    hours, rest = divmod(minutes, 60)
    return "{}h {}m".format(hours, rest) if rest else "{}h".format(hours)


def remaining_text(position, duration):
    position, duration = _number(position), _number(duration)
    if not position or not duration or position <= 0 or position >= duration:
        return ""
    left = int(round((duration - position) / 60.0))
    if left <= 0:
        return ""
    return "{}m left".format(left) if left < 60 else "{}h {}m left".format(*divmod(left, 60))


def progress_percent(item):
    """Whole percent watched for a card's progress bar; 0 hides the bar."""
    state = item.get("user_data") if isinstance(item.get("user_data"), dict) else {}
    position = _number(item.get("position_seconds"))
    if position is None:
        position = _number(state.get("position_seconds"))
    duration = _number(item.get("duration_seconds"))
    if duration is None:
        duration = _number(state.get("duration_seconds"))
    if duration is None:
        runtime = _number(item.get("runtime"))
        duration = runtime * 60 if runtime else None
    if not position or not duration or position <= 0 or duration <= 0:
        return 0
    return max(1, min(99, int(position * 100 / duration)))


def is_watched(item):
    for key in ("user_state", "user_data"):
        state = item.get(key)
        if isinstance(state, dict) and state.get("played") is True:
            return True
    return False


def card_title(item):
    if item.get("type") == "episode" and item.get("series_title"):
        return _text(item["series_title"])
    return _text(item.get("title") or item.get("name")) or "Untitled"


def card_subtitle(item, wide=False):
    """The second caption line: year, or the episode for episode cards."""
    if item.get("type") == "episode":
        title = _text(item.get("title"))
        if wide:
            return title
        return " · ".join(part for part in (episode_code(item, ":"), title) if part)
    if item.get("type") == "season":
        return _text(item.get("title"))
    return _text(item.get("year"))


def resolution_badge(value):
    """8K, 4K, FHD (1080), HD (720) or SD (576 and below) from Silo's 480p…4320p labels."""
    value = _text(value).lower()
    if value in ("4k", "uhd"):
        return "4K"
    if value == "sd":
        return "SD"
    digits = ""
    for character in value:
        if not character.isdigit():
            break
        digits += character
    if not digits:
        return ""
    height = int(digits)
    if height >= 4320:
        return "8K"
    if height >= 2160:
        return "4K"
    if height >= 1080:
        return "FHD"
    if height >= 720:
        return "HD"
    return "SD" if height > 0 else ""


def hdr_badge(value):
    value = _text(value).upper()
    if not value or value == "SDR":
        return ""
    if "DV" in value.split() or "DOLBY" in value:
        return "DOLBY VISION"
    return "HDR"


def badges(item, version=None):
    """Quality and rating badges, in the order Android shows them."""
    summary = item.get("overlay_summary") if isinstance(item.get("overlay_summary"), dict) else {}
    if version:
        summary = {"resolution": version.get("resolution"),
                   "hdr": version.get("hdr_format") or ("HDR" if version.get("hdr") else ""),
                   "audio": version.get("codec_audio")}
    result = [resolution_badge(summary.get("resolution")), hdr_badge(summary.get("hdr"))]
    audio = _text(summary.get("audio")).upper()
    if audio:
        result.append(audio)
    rating = _text(item.get("content_rating"))
    if rating:
        result.append(rating)
    return [badge for badge in result if badge]


def badge_width(text):
    """Estimated width of a badge at the skin's 18px bold badge font."""
    wide = sum(1 for char in text if char in "MWDOQGH")
    return 24 + 12 * len(text) + 3 * wide


def ratings_text(item):
    imdb, tmdb = _number(item.get("rating_imdb")), _number(item.get("rating_tmdb"))
    if imdb:
        return ["IMDb {:.1f}".format(imdb)]
    if tmdb:
        return ["TMDb {:.1f}".format(tmdb)]
    return []


def genres(item, limit=1):
    values = item.get("genres") if isinstance(item.get("genres"), list) else []
    return [_text(value) for value in values if _text(value)][:limit]


def hero_meta(item):
    """Home hero line after the badges."""
    if item.get("type") == "episode":
        parts = [episode_code(item, " "), _text(item.get("title")), duration_text(item.get("runtime")),
                 remaining_text(item.get("position_seconds"), item.get("duration_seconds"))]
    else:
        parts = [_text(item.get("year")), duration_text(item.get("runtime"))]
        parts += ratings_text(item) + genres(item)
    return " · ".join(part for part in parts if part)


def hero(item, image, show_plot=True):
    """Home hero fields; ``image(item, kind)`` resolves artwork URLs."""
    synopsis = _text(item.get("overview") or item.get("description")) if show_plot else ""
    return {
        "backdrop": image(item, "backdrop"),
        "logo": image(item, "logo"),
        "title": card_title(item),
        "badges": badges(item),
        "meta": hero_meta(item),
        "synopsis": synopsis,
    }


def card(item, image, wide=False):
    """Caption, art and overlay state for a row or grid card."""
    poster = image(item, "poster")
    backdrop = image(item, "backdrop")
    art = {"poster": poster or backdrop, "fanart": backdrop}
    if wide:
        art["thumb"] = backdrop or poster
    progress = progress_percent(item)
    return {
        "label": card_title(item),
        "label2": card_subtitle(item, wide),
        "art": art,
        "badge": episode_code(item) if wide else "",
        "progress": progress,
        "watched": is_watched(item) and not progress,
    }


# ----------------------------------------------------------------- details

def detail_meta(item):
    """Facts after the rating chip on the details page."""
    kind = item.get("type")
    if kind == "episode":
        parts = [episode_code(item), _text(item.get("air_date"))[:10], duration_text(item.get("runtime"))]
    else:
        parts = [_text(item.get("year"))]
        count = _int(item.get("season_count"))
        if kind == "series" and count:
            parts.append("{} Season{}".format(count, "" if count == 1 else "s"))
        elif kind != "series":
            parts.append(duration_text(item.get("runtime")))
    parts += ratings_text(item) + genres(item, 3)
    return " · ".join(part for part in parts if part)


def credit_line(item):
    crew = item.get("crew") if isinstance(item.get("crew"), list) else []
    for job, label in (("Director", "Directed by"), ("Creator", "Created by")):
        names = [_text(person.get("name")) for person in crew
                 if isinstance(person, dict) and person.get("job") == job and _text(person.get("name"))]
        if names:
            return "{} {}".format(label, ", ".join(names[:2]))
    cast = item.get("cast") if isinstance(item.get("cast"), list) else []
    names = [_text(person.get("name")) for person in cast if isinstance(person, dict) and _text(person.get("name"))]
    return "Starring " + ", ".join(names[:3]) if names else ""


def _is_specials(season):
    return bool(season.get("is_specials")) or _int(season.get("season_number")) == 0


def season_label(season):
    if _is_specials(season):
        return "Specials"
    number = _int(season.get("season_number"))
    return _text(season.get("title")) or ("Season {}".format(number) if number is not None else "Season")


def ordered_seasons(seasons):
    """Numbered seasons in order, with Specials last."""
    seasons = [season for season in seasons or [] if isinstance(season, dict) and season.get("content_id")]
    regular = sorted((season for season in seasons if not _is_specials(season)),
                     key=lambda season: _int(season.get("season_number")) or 0)
    return regular + [season for season in seasons if _is_specials(season)]


def initial_season_index(item, seasons):
    wanted = _int(item.get("play_season_number"))
    if wanted is None:
        wanted = _int(item.get("season_number"))
    for index, season in enumerate(seasons):
        if wanted is not None and _int(season.get("season_number")) == wanted:
            return index
    return 0


def episode_caption(episode):
    code = "S{:02d}E{:02d}".format(_int(episode.get("season_number")) or 0, _int(episode.get("episode_number")) or 0)
    title = _text(episode.get("title"))
    second = " · ".join(part for part in (_text(episode.get("air_date"))[:10], duration_text(episode.get("runtime")))
                        if part)
    return ("{} · {}".format(code, title) if title else code), second


def initials(name):
    words = [word for word in _text(name).replace("_", " ").split() if word]
    if not words:
        return "?"
    if len(words) == 1:
        return words[0][:1].upper()
    return (words[0][:1] + words[-1][:1]).upper()


def avatar_image(url):
    """Kodi cannot draw SVG; ask DiceBear-style avatar services for PNG."""
    url = _text(url)
    if not url:
        return ""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or parts.username or parts.password:
        return ""
    path = parts.path
    if path.endswith("/svg"):
        path = path[:-4] + "/png"
    elif path.lower().endswith(".svg"):
        return ""
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))
