"""Presentation of Silo FileVersion and WatchRollup catalog responses.

Field names and units follow internal/apiv2/catalog_items.go in Silo Server:
file_id is opaque, file_size is bytes, bitrate is kbps, progress is seconds.
This module deliberately has no Kodi dependency.
"""

import math


def available_versions(item):
    result, seen = [], set()
    for version in item.get("versions") or []:
        if not isinstance(version, dict):
            continue
        file_id = version.get("file_id")
        if not isinstance(file_id, str) or not file_id or file_id in seen:
            continue
        seen.add(file_id)
        result.append(version)
    return result


def default_version(item, mode="ask", preferred_file_id=None):
    """Keep an explicit selection; otherwise honor the default-file policy."""
    versions = available_versions(item)
    if not versions:
        return None
    if preferred_file_id is not None:
        selected = next((version for version in versions if version["file_id"] == preferred_file_id), None)
        if selected:
            return selected
    if mode != "first":
        last_file_id = (item.get("user_data") or {}).get("last_file_id")
        selected = next((version for version in versions if version["file_id"] == last_file_id), None)
        if selected:
            return selected
    return versions[0]


def version_name(version):
    filename = version.get("file_name") or version.get("file_path")
    if filename:
        return str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    return "File {}".format(version["file_id"])


def _positive_number(value):
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value)
    except (ValueError, TypeError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def version_label(version):
    parts = []
    for key in ("resolution", "edition_raw", "codec_video", "codec_audio", "container"):
        value = version.get(key)
        if value:
            parts.append(str(value).upper() if key.startswith("codec_") or key == "container" else str(value))
    if not version.get("edition_raw") and version.get("edition_key"):
        parts.insert(1 if parts else 0, str(version["edition_key"]).replace("_", " "))
    if "hdr" in version:
        parts.insert(1 if parts else 0, "HDR" if version["hdr"] else "SDR")
    size = _positive_number(version.get("file_size"))
    if size:
        parts.append("{:.1f} GiB".format(size / (1024 ** 3)))
    bitrate = _positive_number(version.get("bitrate"))
    if bitrate:
        parts.append("{:.1f} Mbps".format(bitrate / 1000))
    part_index, part_total = version.get("presentation_part_index"), version.get("presentation_part_total")
    if isinstance(part_index, int) and isinstance(part_total, int) and part_total > 1:
        parts.append("Part {} of {}".format(part_index, part_total))
    return " · ".join(parts) or version_name(version)


def is_multipart(version):
    if not version:
        return False
    return (version.get("presentation_kind") in ("multipart", "multipart_movie", "split_episode")
            or _positive_number(version.get("presentation_part_index")) > 0
            or _positive_number(version.get("presentation_part_total")) > 1)


def resume_position(item):
    progress = item.get("user_data") or {}
    if progress.get("played") and not progress.get("is_in_progress"):
        return 0.0
    position = _positive_number(progress.get("position_seconds", item.get("position_seconds")))
    duration = _positive_number(progress.get("duration_seconds", item.get("duration_seconds")))
    return 0.0 if duration and position >= duration else position


def format_time(seconds):
    seconds = int(_positive_number(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return "{}:{:02d}:{:02d}".format(hours, minutes, seconds) if hours else "{}:{:02d}".format(minutes, seconds)
