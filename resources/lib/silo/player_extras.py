"""Everything shown over Silo playback besides Kodi's own controls.

Skip Intro/Recap/Credits, Up next, the sleep timer and the Silo player menu.
The playback loop calls ``tick`` about four times a second; ``tick`` returns a
command when playback has to stop for something else (the next episode,
another version, the sleep timer) or a native Kodi dialog should open. Kodi
objects are injected so the logic can be tested without Kodi.
"""

import math
import time

from . import present
from .media import version_label
from .skip import DONE_LABELS, LABELS, SkipTracker, segments, skip_target

UP_NEXT_LEAD_SECONDS = 30.0
COUNTDOWN_SECONDS = 10.0
MENU_REQUEST = "silo.player_menu"
PLAYING = "silo.playing"
NATIVE_WINDOWS = ("osdaudiosettings", "osdsubtitlesettings")


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


def chapters_for(version, offset=0.0):
    """Chapter list in player seconds for the menu."""
    result = []
    offset = _number(offset) or 0.0
    chapters = (version or {}).get("chapters")
    for chapter in chapters if isinstance(chapters, list) else []:
        if not isinstance(chapter, dict):
            continue
        start, end = _number(chapter.get("start_seconds")), _number(chapter.get("end_seconds"))
        if start is None:
            continue
        result.append({"title": str(chapter.get("title") or "Chapter {}".format(len(result) + 1)),
                       "start": max(0.0, start - offset),
                       "end": max(0.0, end - offset) if end is not None else None})
    return result


def menu_context(item, version, plan, versions, file_id, options, sleep_minutes):
    offset = _number((plan.get("timeline") or {}).get("timeline_offset_seconds")) or 0.0
    requested = plan.get("requested_file_id") or file_id
    effective = plan.get("effective_file_id") or file_id
    episode = item.get("type") == "episode"
    eyebrow = " · ".join(part for part in (present.card_title(item), present.episode_code(item, ":")) if part)
    return {
        "title": str(item.get("title") or "") if episode else present.card_title(item),
        "eyebrow": eyebrow if episode else str(item.get("year") or ""),
        "runtime": present.duration_text(item.get("runtime")),
        "badges": present.badges(item, version),
        "route": "Direct play · original file",
        "source": "Alternate version" if requested and effective and requested != effective else "",
        "versions": [{"file_id": entry["file_id"], "label": version_label(entry)} for entry in versions],
        "file_id": file_id,
        "chapters": chapters_for(version, offset),
        "is_episode": episode,
        "auto_next": bool(options.get("auto_next", True)),
        "skip_prompts": bool(options.get("skip_prompts", True)),
        "sleep_minutes": sleep_minutes,
    }


class PlaybackExtras:
    def __init__(self, player, item, version, plan, options, skip_button=None, up_next=None,
                 menu_factory=None, home=None, notify=None, next_lookup=None, image=None,
                 versions=(), file_id=None, save_setting=None, log=None, clock=time.monotonic):
        self.player = player
        self.item, self.version, self.plan = item, version or {}, plan or {}
        self.options = dict(options or {})
        self.skip_button, self.up_next = skip_button, up_next
        self.menu_factory, self.menu = menu_factory, None
        self.home = home
        self.notify = notify or (lambda message: None)
        self.next_lookup, self.image = next_lookup, image
        self.versions, self.file_id = list(versions), file_id
        self.save_setting = save_setting or (lambda key, value: None)
        self.log = log or (lambda event, position=None: None)
        self.clock = clock
        self.offset = _number((self.plan.get("timeline") or {}).get("timeline_offset_seconds")) or 0.0
        self.tracker = self._tracker()
        self.next_item = None
        self.next_state = "pending" if item.get("type") == "episode" else "none"
        self.countdown_until = None
        self.sleep_deadline = None
        self.last_menu_refresh = 0.0
        if self.home is not None:
            self.home.setProperty(PLAYING, "1")
            self.home.clearProperty(MENU_REQUEST)

    def _tracker(self):
        auto = dict(self.options.get("auto_skip") or {})
        spans = segments(self.version, self.offset)
        if not self.options.get("skip_prompts", True):
            spans = [span for span in spans if auto.get(span["kind"])]
        return SkipTracker(spans, auto)

    # ------------------------------------------------------------ skipping

    def _seek(self, span, total):
        target = skip_target(span, total)
        self.log("skip_" + span["kind"], target)
        self.player.seekTime(target)

    def _skip_tick(self, position, total, now):
        if self.skip_button is not None:
            choice = self.skip_button.take_choice()
            if choice == "skip":
                span = self.tracker.dismiss()
                if span:
                    self._seek(span, total)
                return
            if choice == "dismiss":
                self.tracker.dismiss()
        decision = self.tracker.observe(position, now)
        if decision is None:
            return
        kind, span = decision
        if kind == "show" and self.skip_button is not None:
            self.skip_button.show(LABELS[span["kind"]])
        elif kind == "hide" and self.skip_button is not None:
            self.skip_button.hide()
        elif kind == "skip":
            self._seek(span, total)
            self.notify(DONE_LABELS[span["kind"]])

    # ------------------------------------------------------------- up next

    def _up_next_start(self, total):
        credits = next((span for span in segments(self.version, self.offset) if span["kind"] == "credits"), None)
        if credits:
            return credits["start"]
        total = _number(total)
        return total - UP_NEXT_LEAD_SECONDS if total and total > UP_NEXT_LEAD_SECONDS * 2 else None

    def _next_card(self, episode):
        thumb = self.image(episode, "backdrop") if self.image else ""
        return {"eyebrow": present.episode_code(episode, ":"), "title": str(episode.get("title") or ""),
                "thumb": thumb}

    def _up_next_tick(self, position, total, now):
        if self.next_state in ("none", "done") or self.up_next is None:
            return None
        start = self._up_next_start(total)
        if start is None:
            return None
        if self.next_state == "pending":
            if position < start - 60 or self.next_lookup is None:
                return None
            try:
                self.next_item = self.next_lookup(self.item)
            except Exception:  # An optional card must never end playback.
                self.next_item = None
            self.next_state = "ready" if self.next_item else "none"
            return None
        if self.next_state == "ready" and position >= start:
            auto = self.options.get("auto_next", True)
            self.countdown_until = now + COUNTDOWN_SECONDS if auto else None
            self.up_next.show(self._next_card(self.next_item), COUNTDOWN_SECONDS if auto else None)
            self.next_state = "shown"
            return None
        if self.next_state == "shown":
            choice = self.up_next.take_choice()
            if choice == "play" or (self.countdown_until is not None and now >= self.countdown_until):
                self.up_next.hide()
                self.next_state = "done"
                self.log("up_next_play", position)
                return {"stop": "next", "item": self.next_item}
            if choice == "dismiss":
                self.next_state = "done"
            elif position < start - 1:
                self.up_next.hide()
                self.next_state = "ready"  # Seeked back before the credits.
            elif self.countdown_until is not None:
                self.up_next.update(self.countdown_until - now)
        return None

    # --------------------------------------------------------- player menu

    def _menu_tick(self, position, now):
        if self.home is not None and self.home.getProperty(MENU_REQUEST):
            self.home.clearProperty(MENU_REQUEST)
            if self.menu_factory is not None and (self.menu is None or not self.menu.is_open()):
                remaining = int(math.ceil((self.sleep_deadline - now) / 60)) if self.sleep_deadline else 0
                if self.menu is None:
                    # One menu per playback, so choices such as speed are remembered.
                    self.menu = self.menu_factory()
                self.menu.open(menu_context(self.item, self.version, self.plan, self.versions,
                                            self.file_id, self.options, max(0, remaining)))
                self.last_menu_refresh = now
        if self.menu is None:
            return None
        if self.menu.is_open() and now - self.last_menu_refresh >= 1.0:
            self.last_menu_refresh = now
            self.menu.refresh()
        command = None
        for intent in self.menu.take_result() or []:
            action = intent.get("action")
            if action == "version" and intent.get("file_id") and intent["file_id"] != self.file_id:
                command = {"stop": "version", "file_id": intent["file_id"], "position": position + self.offset}
            elif action == "sleep":
                minutes = max(0, int(intent.get("minutes") or 0))
                self.sleep_deadline = now + minutes * 60 if minutes else None
                self.notify("Sleep timer: {} min".format(minutes) if minutes else "Sleep timer off")
            elif action in ("skip_prompts", "auto_next"):
                self.options[action] = bool(intent.get("value"))
                self.save_setting(action, "true" if self.options[action] else "false")
                if action == "skip_prompts":
                    self.tracker = self._tracker()
            elif action == "native" and intent.get("window") in NATIVE_WINDOWS and command is None:
                command = {"native": intent["window"]}
        return command

    # ---------------------------------------------------------------- loop

    def tick(self, position, paused=False, total=None, now=None):
        now = self.clock() if now is None else now
        position = _number(position)
        if position is None:
            return None
        command = self._menu_tick(position, now)
        if command is not None and "stop" in command:
            return command
        if self.sleep_deadline is not None and now >= self.sleep_deadline:
            self.sleep_deadline = None
            self.log("sleep_timer", position)
            return {"stop": "sleep"}
        if not paused:
            self._skip_tick(position, total, now)
            stop = self._up_next_tick(position, total, now)
            if stop is not None:
                return stop
        return command

    def close(self):
        for overlay in (self.skip_button, self.up_next):
            if overlay is not None:
                try:
                    overlay.hide()
                except Exception:
                    pass
        if self.menu is not None:
            try:
                self.menu.close()
            except Exception:
                pass
            self.menu = None
        if self.home is not None:
            self.home.clearProperty(PLAYING)
            self.home.clearProperty(MENU_REQUEST)
