"""Intro, recap and credits skipping from Silo's per-file markers.

Catalog versions carry ``intro`` and ``credits`` as ``{"start", "end"}`` in
source seconds, and ``chapters`` whose titles can name a recap. The player
runs on its own timeline: player seconds = source seconds - the plan's
``timeline_offset_seconds``. No Kodi dependency.
"""

import math
import re

LABELS = {"intro": "Skip Intro", "recap": "Skip Recap", "credits": "Skip Credits"}
DONE_LABELS = {"intro": "Skipped intro", "recap": "Skipped recap", "credits": "Skipped credits"}
MIN_LENGTH = 3.0
RECAP_TITLE = re.compile(r"^\s*(recap|previously)\b", re.IGNORECASE)


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def _span(start, end, offset):
    start, end = _number(start), _number(end)
    if start is None or end is None:
        return None
    start, end = max(0.0, start - offset), end - offset
    return (start, end) if end - start >= MIN_LENGTH else None


def segments(version, offset=0.0):
    """Skippable spans for one file, in player seconds, sorted by start."""
    if not isinstance(version, dict):
        return []
    offset = _number(offset) or 0.0
    result = []
    for kind in ("intro", "credits"):
        marker = version.get(kind)
        if isinstance(marker, dict):
            span = _span(marker.get("start"), marker.get("end"), offset)
            if span:
                result.append({"kind": kind, "start": span[0], "end": span[1]})
    chapters = version.get("chapters") if isinstance(version.get("chapters"), list) else []
    for chapter in chapters:
        if isinstance(chapter, dict) and RECAP_TITLE.match(str(chapter.get("title") or "")):
            span = _span(chapter.get("start_seconds"), chapter.get("end_seconds"), offset)
            if span and not any(s["start"] < span[1] and span[0] < s["end"] for s in result):
                result.append({"kind": "recap", "start": span[0], "end": span[1]})
            break
    return sorted(result, key=lambda segment: segment["start"])


class SkipTracker:
    """Turn player positions into show/hide/auto-skip decisions.

    ``observe`` returns None, ("show", span), ("hide", span) or ("skip", span).
    Each span is offered once per pass; seeking back before its start offers
    it again. The button hides after ``show_seconds`` even inside a long span.
    """

    def __init__(self, spans, auto=None, show_seconds=10.0):
        self.spans = list(spans)
        self.auto = dict(auto or {})
        self.show_seconds = show_seconds
        self.active = None
        self.shown_at = None
        self.handled = set()

    def _current(self, position):
        for index, span in enumerate(self.spans):
            if span["start"] <= position < span["end"] - 1.0:
                return index
        return None

    def observe(self, position, now):
        position = _number(position)
        if position is None:
            return None
        for index, span in enumerate(self.spans):
            if position < span["start"] - 1.0:
                self.handled.discard(index)
        current = self._current(position)
        if self.active is not None and (current != self.active or now - self.shown_at >= self.show_seconds):
            span, self.active = self.spans[self.active], None
            return ("hide", span)
        if current is None or current in self.handled or self.active is not None:
            return None
        self.handled.add(current)
        span = self.spans[current]
        if self.auto.get(span["kind"]):
            return ("skip", span)
        self.active, self.shown_at = current, now
        return ("show", span)

    def dismiss(self):
        """The viewer pressed Back or took the skip; hide until the next pass."""
        span = self.spans[self.active] if self.active is not None else None
        self.active = None
        return span


def skip_target(span, total=None):
    """Where to seek: the marker end, kept a little before the file's end."""
    target = span["end"]
    total = _number(total)
    if total and total > 0:
        target = min(target, total - 0.5)
    return max(0.0, target)
