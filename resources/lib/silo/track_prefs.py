"""Choose audio and subtitle tracks the way the Silo apps do.

Silo plays without subtitles unless the client names a track, so the client
applies the profile's preferences itself. These rules follow the Silo Android
app (AutoSubtitleResolver, selectBestCompatibleAudioTrackOrdinal), plus the
viewer's remembered choice for a title or a show. No Kodi dependency.

Subtitle results are a playback ordinal (externals first, then embedded, as
media.subtitle_choices numbers them), OFF, or None when nothing fits.
"""

OFF = "off"

# ISO 639-2 (bibliographic and terminology) to ISO 639-1, for the languages
# catalogs commonly carry.
_ISO = {
    "eng": "en", "spa": "es", "fre": "fr", "fra": "fr", "ger": "de", "deu": "de", "dut": "nl", "nld": "nl",
    "ita": "it", "por": "pt", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh", "rus": "ru", "ara": "ar",
    "hin": "hi", "tur": "tr", "pol": "pl", "swe": "sv", "nor": "no", "nob": "no", "nno": "no", "dan": "da",
    "fin": "fi", "gre": "el", "ell": "el", "heb": "he", "cze": "cs", "ces": "cs", "hun": "hu", "tha": "th",
    "vie": "vi", "ind": "id", "ukr": "uk", "rum": "ro", "ron": "ro", "bul": "bg", "hrv": "hr", "srp": "sr",
    "slo": "sk", "slk": "sk", "slv": "sl", "est": "et", "lav": "lv", "lit": "lt", "ice": "is", "isl": "is",
    "per": "fa", "fas": "fa", "may": "ms", "msa": "ms", "fil": "tl", "tgl": "tl", "cat": "ca", "baq": "eu",
    "eus": "eu", "glg": "gl", "wel": "cy", "cym": "cy", "gle": "ga", "tam": "ta", "tel": "te", "ben": "bn",
    "urd": "ur", "mal": "ml", "kan": "kn", "mar": "mr", "afr": "af", "alb": "sq", "sqi": "sq", "arm": "hy",
    "hye": "hy", "geo": "ka", "kat": "ka", "mac": "mk", "mkd": "mk", "bos": "bs", "kaz": "kk", "uzb": "uz",
}
# English language names, as Kodi reports some streams, for the same languages.
_NAMES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de", "dutch": "nl", "italian": "it",
    "portuguese": "pt", "japanese": "ja", "korean": "ko", "chinese": "zh", "mandarin": "zh", "cantonese": "zh",
    "russian": "ru", "arabic": "ar", "hindi": "hi", "turkish": "tr", "polish": "pl", "swedish": "sv",
    "norwegian": "no", "danish": "da", "finnish": "fi", "greek": "el", "hebrew": "he", "czech": "cs",
    "hungarian": "hu", "thai": "th", "vietnamese": "vi", "indonesian": "id", "ukrainian": "uk",
    "romanian": "ro", "bulgarian": "bg", "croatian": "hr", "serbian": "sr", "slovak": "sk", "slovenian": "sl",
    "persian": "fa", "malay": "ms", "filipino": "tl", "tagalog": "tl", "catalan": "ca", "tamil": "ta",
    "telugu": "te", "bengali": "bn", "urdu": "ur", "malayalam": "ml",
}
BITMAP = ("pgs", "hdmv_pgs_subtitle", "vobsub", "dvd_subtitle", "dvb_subtitle", "dvbsub", "sup", "sub", "idx")


def language(value):
    """Canonical ISO 639-1 code, or "" for unknown/undetermined."""
    text = str(value or "").strip().lower().replace("_", "-").split("-")[0]
    if not text or text in ("und", "unknown", "zxx", "mul"):
        return ""
    name = text.split(" (")[0].split(" ")[0]
    return _ISO.get(text) or _NAMES.get(name) or text


def _is_hi(track):
    title = str(track.get("title") or track.get("embedded_title") or "").lower()
    return bool(track.get("hearing_impaired")) or any(word in title for word in ("sdh", "hearing impaired", " cc"))


def subtitle_tracks(version):
    """Tracks with their playback ordinal, externals first like the server numbers them."""
    tracks = [track for track in (version or {}).get("subtitle_tracks") or [] if isinstance(track, dict)]
    ordered = [track for track in tracks if track.get("external")] + [t for t in tracks if not t.get("external")]
    result = []
    for ordinal, track in enumerate(ordered):
        codec = str(track.get("codec") or track.get("format") or "").lower()
        external = bool(track.get("external"))
        result.append({"index": ordinal, "language": language(track.get("language")),
                       "raw_language": str(track.get("language") or "").strip(),
                       "codec": codec, "label": str(track.get("title") or "").strip(),
                       "forced": bool(track.get("forced")), "hi": _is_hi(track), "external": external,
                       # Embedded bitmap subtitles can be shown by the player; external ones need burning in.
                       "burn": codec in BITMAP and external})
    return result


def _best(candidates):
    """Android's tie-break: no burn-in, not forced, not SDH, embedded, then catalog order."""
    return min(candidates, key=lambda track: (track["burn"], track["forced"], track["hi"], track["external"],
                                              track["index"]))["index"] if candidates else None


def auto_subtitle(version, profile, audio_language=None):
    """The profile's automatic subtitle: an ordinal, OFF, or None when nothing fits (plays without)."""
    tracks = subtitle_tracks(version)
    if not tracks:
        return None
    profile = profile or {}
    mode = str(profile.get("subtitle_mode") or "auto").strip().lower()
    if mode == "off":
        return OFF
    raw = profile.get("subtitle_language")
    if raw is not None and not str(raw).strip():
        return OFF  # An empty language means "no subtitles".
    preferred = language(raw)
    show_forced = profile.get("show_forced_subtitles") is not False
    if not preferred:
        return _best(tracks) if mode == "always" else None
    in_language = [track for track in tracks if track["language"] == preferred]
    if mode == "auto" and language(audio_language) == preferred:
        # The audio is already in the viewer's language: only forced subtitles (signs, foreign lines).
        return _best([track for track in in_language if track["forced"]]) if show_forced else OFF
    choice = _best([track for track in in_language if not track["forced"]] or in_language)
    if choice is None and show_forced:
        choice = _best([track for track in tracks if track["forced"]])
    return choice


def remembered_choice(track):
    """What to store when the viewer picks a track: enough to find it again in another file."""
    if track is None:
        return {"kind": "off"}
    return {"kind": "track", "language": track["language"], "forced": track["forced"], "hi": track["hi"],
            "external": track["external"]}


def match_choice(version, choice):
    """Apply a remembered choice to a (possibly different) file: an ordinal, OFF, or None if no match.

    The language must match; then the closest kind (forced, SDH, external)
    wins, and equal candidates keep catalog order.
    """
    if not isinstance(choice, dict):
        return None
    if choice.get("kind") == "off":
        return OFF
    tracks = [track for track in subtitle_tracks(version) if track["language"] == choice.get("language")]
    if not tracks:
        return None
    return min(tracks, key=lambda track: (track["forced"] != bool(choice.get("forced")),
                                          track["hi"] != bool(choice.get("hi")),
                                          track["external"] != bool(choice.get("external")),
                                          track["burn"], track["index"]))["index"]


def track_at(version, ordinal):
    return next((track for track in subtitle_tracks(version) if track["index"] == ordinal), None)


def _audio_tracks(version):
    return [track for track in (version or {}).get("audio_tracks") or [] if isinstance(track, dict)]


def is_commentary(track):
    return "comment" in str((track or {}).get("title") or "").lower()


def _audio_rank(tracks, ordinal):
    """Main mix before commentary, the file's default, then more channels."""
    track = tracks[ordinal]
    channels = track.get("channels") if isinstance(track.get("channels"), int) else 0
    return (is_commentary(track), not track.get("default"), -channels, ordinal)


def match_audio(version, choice):
    """Apply a remembered audio choice ({language, commentary}) to a file: an ordinal, or None."""
    if not isinstance(choice, dict) or not choice.get("language"):
        return None
    tracks = _audio_tracks(version)
    matches = [ordinal for ordinal, track in enumerate(tracks) if language(track.get("language")) == choice["language"]]
    if not matches:
        return None
    commentary = bool(choice.get("commentary"))
    return min(matches, key=lambda ordinal: (is_commentary(tracks[ordinal]) != commentary,) + _audio_rank(tracks, ordinal))


def audio_default(version, profile):
    """(audio ordinal, its language) for the profile's language, then English; main mix before commentary."""
    tracks = _audio_tracks(version)
    if not tracks:
        return None, ""
    preferred = language((profile or {}).get("language"))

    def rank(ordinal):
        return _audio_rank(tracks, ordinal)
    for wanted in [code for code in (preferred, "en") if code]:
        matches = [ordinal for ordinal, track in enumerate(tracks) if language(track.get("language")) == wanted]
        if matches:
            return min(matches, key=rank), wanted
    best = min(range(len(tracks)), key=rank)
    return best, language(tracks[best].get("language"))


def audio_choice_key(item):
    """Audio picks are kept beside subtitle picks, per show or per title."""
    return "audio:" + choice_key(item)


def pref_key(item):
    """The server's key for a remembered track choice: the series, or the movie itself."""
    return str(item.get("series_id") or item.get("content_id") or "") if item.get("type") in (
        "episode", "movie") else ""


def server_subtitle_prefs(item):
    """The subtitle preferences the server resolved for this item, as a profile-like dict, or None.

    The detail's effective_subtitle_* fields already include a choice saved
    for the series on any device, so they replace the profile's own values.
    """
    if not isinstance(item, dict) or "effective_subtitle_mode" not in item:
        return None
    return {"subtitle_mode": item.get("effective_subtitle_mode") or "auto",
            "subtitle_language": item.get("effective_subtitle_language") or "",
            "show_forced_subtitles": item.get("effective_show_forced_subtitles") is not False}


def _norm(value):
    return str(value or "").strip().lower()


def subtitle_signature(track):
    """A subtitle_tracks() entry as the server's SubtitleTrackSignature (Android parity)."""
    signature = {"source": "external" if track["external"] else "embedded", "forced": track["forced"],
                 "hearing_impaired": track["hi"]}
    for key, value in (("language", track["raw_language"]), ("codec", track["codec"]), ("label", track["label"])):
        if value:
            signature[key] = value
    return signature


def match_subtitle_signature(version, signature):
    """The ordinal of the track a stored signature describes, or None.

    Exact match first (source, language, codec, label when stored, forced,
    SDH); otherwise the closest track in the same language.
    """
    if not isinstance(signature, dict):
        return None
    wanted = language(signature.get("language"))
    candidates = [track for track in subtitle_tracks(version) if track["language"] == wanted]
    source = _norm(signature.get("source"))
    for track in candidates:
        if (source == ("external" if track["external"] else "embedded")
                and _norm(signature.get("codec")) == _norm(track["codec"])
                and (not _norm(signature.get("label")) or _norm(signature.get("label")) == _norm(track["label"]))
                and bool(signature.get("forced")) == track["forced"]
                and bool(signature.get("hearing_impaired")) == track["hi"]):
            return track["index"]

    def score(track):
        return ((4 if source == ("external" if track["external"] else "embedded") else 0)
                + (2 if bool(signature.get("forced")) == track["forced"] else 0)
                + (2 if bool(signature.get("hearing_impaired")) == track["hi"] else 0)
                + (1 if _norm(signature.get("codec")) == _norm(track["codec"]) else 0)
                + (1 if _norm(signature.get("label")) == _norm(track["label"]) else 0))
    return max(candidates, key=lambda track: (score(track), -track["index"]))["index"] if candidates else None


def subtitle_pref_body(version, ordinal):
    """PUT /subtitle-prefs body: a track (mode always) or Off (index -1)."""
    if ordinal is None:
        return {"subtitle_language": "", "subtitle_track_index": -1, "subtitle_mode": "off"}
    track = track_at(version, ordinal)
    if track is None:
        return None
    return {"subtitle_language": "" if track["language"] in ("", "und") else track["raw_language"],
            "subtitle_track_index": ordinal, "subtitle_mode": "always",
            "track_signature": subtitle_signature(track)}


def audio_pref_body(version, ordinal):
    """PUT /audio-prefs body for the track at ``ordinal`` (Android SeriesAudioChoice parity)."""
    tracks = _audio_tracks(version)
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or not 0 <= ordinal < len(tracks):
        return None
    track = tracks[ordinal]
    body = {"audio_track_index": ordinal}
    if language(track.get("language")):
        body["audio_language"] = str(track["language"]).strip()
    signature = {}
    for key in ("language", "title", "embedded_title", "codec", "layout"):
        value = str(track.get(key) or "").strip()
        if value:
            signature[key] = value
    if isinstance(track.get("channels"), int) and not isinstance(track.get("channels"), bool) and track["channels"] > 0:
        signature["channels"] = track["channels"]
    if signature:
        body["track_signature"] = signature
    return body


def choice_key(item):
    """Remembered choices: per show for episodes (so the next episode follows), else per title."""
    if item.get("type") == "episode" and item.get("series_id"):
        return "series:" + str(item["series_id"])
    return "item:" + str(item.get("content_id") or "")
