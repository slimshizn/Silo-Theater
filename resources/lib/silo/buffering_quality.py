"""Lower quality when playback keeps buffering, as every Silo client does.

A port of the Silo Android ``BufferingQualityController`` (with its mode,
plan mapping, kept step-down estimate and request gating). The contract is
the server repo's ``docs/design/2026-10-17-buffering-quality.md``; the tests
replay its conformance vectors (``tests/fixtures/buffering_quality_vectors.json``,
vendored byte for byte). Fix the code, never the vectors. No Kodi dependency.

Times are integer milliseconds from an injectable monotonic clock. The
controller never sleeps: its timers fire from ``tick()``, which the playback
loop calls on every poll (``next_timer_at()`` says when one is due).

Inputs (call them as things happen to the playing plan)::

    c = BufferingQualityController(on_replan, on_notice=None, on_plan_summary=None,
                                   on_pill=None, plan_throughput_kbps=None, clock=None)
    c.set_mode("never" | "ask" | "always")     # playback.buffering_quality_mode
    c.on_plan_adopted(plan_from_wire(plan, quality, adoption_id), controller_initiated=True)
    c.on_playing()                              # first frame, or resumed
    c.on_waiting(cause)                         # "network" (Kodi Player.Caching), "seek",
                                                # "pause", "reload", "renderer"
    c.on_pause(); c.on_seek()                   # viewer pause / seek
    c.on_transport_error_start(); c.on_transport_error_end()   # media requests failing
    c.on_replan_failed()                        # our replan failed or will never be adopted
    c.on_quality_pick_sent(); c.on_quality_pick_refused()
    c.on_viewer_quality_change()                # a pick took effect (before its adoption)
    c.on_start_deadline_missed(failure_path)    # -> True when it lowered instead
    c.select_lower(); c.select_keep(); c.select_restore(); c.select_primary(); c.back()
    c.set_other_pill_visible(bool); c.set_view_hidden(bool); c.set_suspended(bool)
    c.on_playback_stopped(); c.on_new_attempt(); c.reset()     # reset = other content
    c.tick()                                    # run due timers

Outputs (callbacks, run inline from the call or ``tick()`` that caused them)::

    on_replan(BufferingReplan)    send a quality_change replan; build its fields with
                                  replan_fields(), then KeptEstimate.apply() and
                                  gate_replan_request(), then call on_plan_adopted
                                  (controller_initiated=True) or on_replan_failed()
    on_notice(key)                "lowered" or "cant_lower": display-only, 5 s (COPY)
    on_pill(Pill or None)         the actionable pill: Pill("prompt"|"undo", seconds)
    on_plan_summary(PlanSummary)  a plan stopped playing: send a plan_summary route
                                  event with summary.diagnostics()

``c.pill`` is the pill showing now. ``c.running_summary()`` reads the playing
plan's summary without ending it.

Request gating: ``gate_start_request`` and ``gate_replan_request`` add
``buffering_quality_v1`` to ``client_features`` and keep
``bandwidth_estimate_source``, ``observed_throughput_kbps`` and
``learned_limit`` only when the server advertises ``buffering_quality_v1``;
otherwise they strip them (the server rejects unknown fields).
``bandwidth_estimate_kbps`` is an older field and always stays.
"""

import time
from collections import namedtuple

FEATURE = "buffering_quality_v1"
PLAN_SUMMARY_FEATURE = "playback_plan_summary_v1"
CONNECTION_SPEED_FEATURE = "connection_speed_v1"
ACTIVE_QUALITY_FEATURE = "plan_active_quality_v1"

AUTO = "auto"
SOURCE_PROBE = "probe"
SOURCE_OBSERVED_REBUFFER = "observed_rebuffer"
LEARNED_LIMIT_IGNORE = "ignore"
LEARNED_LIMIT_CLEAR = "clear"
QUALITY_REDUCTION_UNAVAILABLE = "quality_reduction_unavailable"

NEVER, ASK, ALWAYS = "never", "ask", "always"
MODES = (NEVER, ASK, ALWAYS)
DEFAULT_MODE = ALWAYS
MODE_LABELS = {NEVER: "Never", ASK: "Ask first", ALWAYS: "Automatically"}
SETTING_KEY = "playback.buffering_quality_mode"

NETWORK, SEEK, PAUSE, RELOAD, RENDERER = "network", "seek", "pause", "reload", "renderer"

PROMPT, UNDO = "prompt", "undo"
NOTICE_LOWERED, NOTICE_CANT_LOWER = "lowered", "cant_lower"
NOTICE_MS = 5000

# buffering_action values for plan_summary.
NONE, IGNORED, LOWERED, RESTORED, ACCEPTED, KEPT, TIMED_OUT, CANT_LOWER = (
    "none", "ignored", "lowered", "restored", "accepted", "kept", "timed_out", "cant_lower")

COPY = {
    "prompt_title": "Playback keeps buffering",
    "prompt_lower": "Lower quality",
    "prompt_keep": "Keep quality",
    "undo_caption": "Quality lowered to stop buffering",
    "undo_action": "Restore quality",
    NOTICE_LOWERED: "Quality lowered to stop buffering",
    NOTICE_CANT_LOWER: "Can't lower quality for this video",
}

STARTUP_GRACE_MS = 10000
STALL_MIN_MS = 1000
STALLS_BEFORE_TRIGGER = 2
STALL_WINDOW_MS = 120000
LONG_STALL_MS = 6000
PROMPT_SECONDS = 15
REOFFER_MIN_MS = 120000
NO_LOWER_COOLDOWN_MS = 300000
REARM_AFTER_UNADOPTED_MS = 60000
RENDERER_WAIT_BUFFER_MS = 1000
RENDERER_WAIT_MAX_MS = 3000
OUTAGE_NO_DATA_MS = 4000
OUTAGE_NO_DATA_SEGMENTED_MS = 15000
# Documentation only: the estimate is the exact integer delivered * 7 // 10.
STEP_DOWN_RATIO = 0.7
MIN_ESTIMATE_KBPS = 100
MAX_ESTIMATE_KBPS = 1000000

_SOURCE_ALIASES = ("original", "source", "max")


def parse_mode(value):
    """The stored or wire mode, or None for anything unrecognized."""
    return value if value in MODES else None


def learned_limit_for_mode(mode):
    """``ignore`` on every request in ``never`` mode, else nothing."""
    return LEARNED_LIMIT_IGNORE if mode == NEVER else None


def quality_preference(value):
    """Normalize a quality preference: blank is None, source aliases are Auto."""
    text = str(value or "").strip().lower()
    if not text:
        return None
    return AUTO if text in _SOURCE_ALIASES else text


def _positive(value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


class Rung(namedtuple("Rung", "label bitrate_kbps")):
    """One quality menu entry, as far as the controller needs it."""


class BufferingPlan(object):
    """What the controller needs from a plan that just became the playing plan.

    ``plan_id`` is the local adoption identity (unique per adoption; the
    server ``plan_id`` can repeat and is ``server_plan_id``).
    ``delivered_kbps`` is effective_recipe.bitrate_kbps, else
    source.bitrate_kbps, or None. ``active_quality`` is the menu label the
    plan names with plan_active_quality_v1, else None.
    """

    def __init__(self, plan_id, delivered_kbps, quality, available_qualities=(),
                 quality_reduction_unavailable=False, server_plan_id=None, active_quality=None):
        self.plan_id = plan_id
        self.delivered_kbps = delivered_kbps
        self.quality = quality
        self.available_qualities = [Rung(*r) if not isinstance(r, Rung) else r for r in available_qualities]
        self.quality_reduction_unavailable = bool(quality_reduction_unavailable)
        self.server_plan_id = server_plan_id if server_plan_id is not None else plan_id
        self.active_quality = active_quality


def plan_from_wire(plan, quality, adoption_id):
    """A BufferingPlan from a v3 ``playback_plan`` dict.

    ``quality`` is the preference the plan was requested with; ``adoption_id``
    must be unique per adoption (a counter or uuid will do).
    """
    plan = plan or {}
    recipe = plan.get("effective_recipe") or {}
    source = plan.get("source") or {}
    delivered = _positive(recipe.get("bitrate_kbps")) or _positive(source.get("bitrate_kbps"))
    rungs = []
    for entry in plan.get("available_qualities") or []:
        if isinstance(entry, dict) and entry.get("label"):
            rungs.append(Rung(str(entry["label"]), _positive(entry.get("bitrate_kbps")) or 0))
    unavailable = False
    for warning in plan.get("degradation_warnings") or []:
        code = warning.get("code") if isinstance(warning, dict) else warning
        if code == QUALITY_REDUCTION_UNAVAILABLE:
            unavailable = True
    active = plan.get("active_quality")
    return BufferingPlan(
        plan_id=adoption_id, delivered_kbps=delivered,
        quality=quality_preference(quality) or AUTO, available_qualities=rungs,
        quality_reduction_unavailable=unavailable,
        server_plan_id=plan.get("plan_id") or adoption_id,
        active_quality=active if isinstance(active, str) and active else None)


class BufferingReplan(namedtuple("BufferingReplan",
                                 "quality_preference bandwidth_estimate_kbps bandwidth_estimate_source learned_limit")):
    """The quality_change replan the controller wants sent."""


def replan_fields(replan, throughput_kbps=None):
    """The wire fields of a controller replan, before gating.

    A lowering (``observed_rebuffer``) also carries ``observed_throughput_kbps``,
    the throughput the plan that stalled received so far, when measured.
    """
    fields = {"operation": "quality_change", "quality_preference": replan.quality_preference}
    if replan.bandwidth_estimate_kbps is not None:
        fields["bandwidth_estimate_kbps"] = replan.bandwidth_estimate_kbps
    if replan.bandwidth_estimate_source is not None:
        fields["bandwidth_estimate_source"] = replan.bandwidth_estimate_source
        throughput = _positive(throughput_kbps)
        if replan.bandwidth_estimate_source == SOURCE_OBSERVED_REBUFFER and throughput is not None:
            fields["observed_throughput_kbps"] = max(MIN_ESTIMATE_KBPS, min(MAX_ESTIMATE_KBPS, throughput))
    if replan.learned_limit is not None:
        fields["learned_limit"] = replan.learned_limit
    return fields


class KeptEstimate(object):
    """The step-down estimate a playback attempt's Auto was last lowered with.

    Every later Auto replan the client sends for its own reasons (track
    change, seek reanchor or recovery, failure recovery, output change)
    repeats it as ``bandwidth_estimate_kbps`` without a source, so the
    lowering holds where no learned limit applies. Memory only.
    """

    def __init__(self):
        self.attempt_id = None
        self.kbps = None

    def apply(self, body, replan=None):
        """Apply the rule to a replan ``body`` dict (mutated and returned).

        ``replan`` is the controller's BufferingReplan when it asked for this
        request, else None. Call once per request, right before sending.
        """
        attempt = body.get("playback_attempt_id")
        observed = replan.bandwidth_estimate_kbps if replan is not None else None
        discards = ((replan is not None and replan.learned_limit == LEARNED_LIMIT_CLEAR)
                    or (replan is None and body.get("operation") == "quality_change"))
        if discards:
            if self.attempt_id == attempt:
                self.attempt_id = self.kbps = None
            return body
        if observed is not None:
            self.attempt_id, self.kbps = attempt, observed
            return body
        if (self.kbps is not None and self.attempt_id == attempt
                and body.get("bandwidth_estimate_kbps") is None
                and quality_preference(body.get("quality_preference")) == AUTO):
            body["bandwidth_estimate_kbps"] = self.kbps
            body.pop("bandwidth_estimate_source", None)
            body.pop("observed_throughput_kbps", None)
        return body

    def clear(self):
        self.attempt_id = self.kbps = None


def _gate(body, server_features, replan):
    features = list(body.get("client_features") or [])
    if FEATURE in (server_features or ()):
        if FEATURE not in features:
            features.append(FEATURE)
        body["client_features"] = features
        # A source without an estimate is a 400.
        if body.get("bandwidth_estimate_kbps") is None:
            body.pop("bandwidth_estimate_source", None)
        # So is a measured throughput on anything but a lowering (422).
        if replan and body.get("bandwidth_estimate_source") != SOURCE_OBSERVED_REBUFFER:
            body.pop("observed_throughput_kbps", None)
    else:
        body["client_features"] = [f for f in features if f != FEATURE]
        for key in ("bandwidth_estimate_source", "learned_limit"):
            body.pop(key, None)
        if replan:
            body.pop("observed_throughput_kbps", None)
    return body


def gate_start_request(body, server_features):
    """The start ``body`` (mutated and returned) for a server advertising ``server_features``."""
    return _gate(body, server_features, replan=False)


def gate_replan_request(body, server_features):
    """The replan ``body`` (mutated and returned) for a server advertising ``server_features``."""
    return _gate(body, server_features, replan=True)


class Pill(namedtuple("Pill", "kind seconds")):
    """The buffering pill on screen: ``kind`` is "prompt" or "undo"."""


class PlanSummary(namedtuple("PlanSummary", "plan_id played_ms stall_count stall_total_ms stall_longest_ms "
                                            "delivered_kbps mode action adoption_id")):
    """One plan's plan_summary facts; ``plan_id`` is the server plan_id."""

    def diagnostics(self, bandwidth_estimate_kbps=None, throughput_kbps=None):
        """The plan_summary route-event ``diagnostics`` (a string map)."""
        out = {
            "played_ms": str(self.played_ms), "stall_count": str(self.stall_count),
            "stall_total_ms": str(self.stall_total_ms), "stall_longest_ms": str(self.stall_longest_ms),
            "buffering_mode": self.mode, "buffering_action": self.action,
        }
        if _positive(self.delivered_kbps):
            out["delivered_kbps"] = str(self.delivered_kbps)
        if bandwidth_estimate_kbps is not None:
            out["bandwidth_estimate_kbps"] = str(bandwidth_estimate_kbps)
        throughput = _positive(throughput_kbps)
        if throughput is not None and throughput >= MIN_ESTIMATE_KBPS:
            out["throughput_kbps"] = str(min(MAX_ESTIMATE_KBPS, throughput))
        return out


class _Timer(object):
    __slots__ = ("due", "seq", "fn", "cancelled", "done")

    def __init__(self, due, seq, fn):
        self.due, self.seq, self.fn = due, seq, fn
        self.cancelled = self.done = False

    @property
    def active(self):
        return not (self.cancelled or self.done)

    def cancel(self):
        self.cancelled = True


def _active(timer):
    return timer is not None and timer.active


def _cancel(timer):
    if timer is not None:
        timer.cancel()


class _Pending(object):
    AUTOMATIC, ACCEPTED, RESTORE = "automatic", "accepted", "restore"

    def __init__(self, kind, from_kbps, from_state=None, action_before=NONE):
        self.kind, self.from_kbps, self.from_state, self.action_before = kind, from_kbps, from_state, action_before


class _StallTracker(object):
    """One plan's stall history: the web player's createStallTracker."""

    def __init__(self):
        self.first_playing_at = None
        self.first_waiting_at = None
        self.stall_started_at = None
        self.stalled = False
        self.stall_ends = []
        self.long_stall_ended_at = None

    @property
    def starting(self):
        return self.first_playing_at is None

    def note_waiting(self, now):
        if self.first_playing_at is None and self.first_waiting_at is None:
            self.first_waiting_at = now

    def grace_ends_at(self, now):
        anchor = self.first_waiting_at
        if anchor is None:
            anchor = self.first_playing_at if self.first_playing_at is not None else now
        return anchor + STARTUP_GRACE_MS

    def should_trigger(self, now):
        if self.long_stall_ended_at is not None and now - self.long_stall_ended_at < STALL_WINDOW_MS:
            return True
        if self.stall_started_at is not None and now - self.stall_started_at >= LONG_STALL_MS:
            return True
        self.stall_ends = [end for end in self.stall_ends if now - end < STALL_WINDOW_MS]
        return len(self.stall_ends) >= STALLS_BEFORE_TRIGGER


class _PlanState(object):
    def __init__(self, plan, replacement):
        self.plan = plan
        self.replacement = replacement
        self.tracker = _StallTracker()
        self.played = False
        self.action = NONE
        self.cooldown_until = None
        self.deadline_failure_path = None
        self.playing_since = None
        self.played_ms = 0
        self.stats_stall_started_at = None
        self.stall_count = 0
        self.stall_total_ms = 0
        self.stall_longest_ms = 0

    @property
    def attempt_started(self):
        return self.replacement or self.played


class BufferingQualityController(object):
    """The buffering-quality state machine (see the module docstring).

    ``plan_throughput_kbps`` returns the playing plan's measured throughput
    so far, or None: it is the evidence a lowering needs before the attempt
    first plays. ``clock`` returns monotonic milliseconds. Not thread-safe.
    """

    def __init__(self, on_replan, on_notice=None, on_plan_summary=None, on_pill=None,
                 plan_throughput_kbps=None, clock=None):
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self._on_replan = on_replan
        self._on_notice = on_notice or (lambda key: None)
        self._on_plan_summary = on_plan_summary or (lambda summary: None)
        self._on_pill = on_pill or (lambda pill: None)
        self._throughput = plan_throughput_kbps or (lambda: None)
        self._timers = []
        self._seq = 0
        self.pill = None
        self.mode = DEFAULT_MODE
        self._enabled = True
        self._suspended = False
        self._other_pill_visible = False
        self._view_hidden = False
        self._paused = False
        self._transport_trouble = False
        self._transport_trouble_ended_at = None
        self._wait_cause = None
        self._pick_pending = False
        self._current = None
        self._answered_quality = None
        self._cant_lower_shown = False
        self._reoffer_not_before = None
        self._pending = None
        self._active_pill = None
        self._deferred_pill = None
        self._deferred_resumes = False
        self._prompt_shown_at = 0
        self._pill_remaining_ms = 0
        self._pill_step_started_at = 0
        self._pill_job = None
        self._long_stall_job = None
        self._reevaluate_job = None
        self._rearm_job = None

    # ---- Timers -----------------------------------------------------------

    def _schedule(self, delay_ms, fn):
        timer = _Timer(self.clock() + delay_ms, self._seq, fn)
        self._seq += 1
        if delay_ms <= 0:
            timer.done = True
            fn()
        else:
            self._timers.append(timer)
        return timer

    def next_timer_at(self):
        """When the next timer is due (clock ms), or None."""
        live = [t for t in self._timers if t.active]
        self._timers = live
        return min(t.due for t in live) if live else None

    def tick(self):
        """Run every timer due by now, earliest first."""
        while True:
            now = self.clock()
            due = [t for t in self._timers if t.active and t.due <= now]
            if not due:
                return
            timer = min(due, key=lambda t: (t.due, t.seq))
            timer.done = True
            self._timers.remove(timer)
            timer.fn()

    # ---- Settings and player state ---------------------------------------

    def set_mode(self, mode):
        """The effective playback.buffering_quality_mode (never, ask, always)."""
        self.mode = parse_mode(mode) or DEFAULT_MODE
        if self.mode == NEVER:
            self._hide_pill()

    def set_enabled(self, enabled):
        """False when playback has no server plan: the pill hides, events are ignored."""
        if enabled == self._enabled:
            return
        if not enabled:
            self._drop_plan(self.clock())
        self._enabled = enabled

    def set_suspended(self, suspended):
        """True inside a Watch Party room: track and summarize, but act on nothing."""
        if suspended == self._suspended:
            return
        self._suspended = suspended
        if not self._enabled:
            return
        self._pending = None
        _cancel(self._rearm_job)
        self._rearm_job = None
        _cancel(self._reevaluate_job)
        self._reevaluate_job = None
        if suspended:
            self._hide_pill()
        elif self._current is not None:
            self._reset_tracker(self._current, self.clock())

    def set_other_pill_visible(self, visible):
        """A higher-priority pill (skip, Next Up) shows: ours waits or hides with its timer frozen."""
        self._other_pill_visible = visible
        if not self._enabled:
            return
        if visible:
            self._obscure_pill()
            return
        kind = self._deferred_pill
        if kind is None:
            return
        if self._view_hidden and kind == PROMPT:
            return
        self._present(kind, self._deferred_resumes)

    def set_view_hidden(self, hidden):
        """The player cannot show pills (an overlay or dialog covers it), or can again."""
        if hidden == self._view_hidden:
            return
        self._view_hidden = hidden
        if not self._enabled:
            return
        if hidden and self._active_pill == PROMPT:
            self._obscure_pill()
            return
        self._publish_pill()
        if not hidden and not self._other_pill_visible and self._deferred_pill is not None:
            self._present(self._deferred_pill, self._deferred_resumes)

    @property
    def has_plan(self):
        return self._current is not None

    @property
    def loading_replacement(self):
        """The playing plan replaced one after the attempt played and has not played yet."""
        state = self._current
        return state is not None and state.replacement and state.tracker.starting

    def running_summary(self):
        """The playing plan's summary so far, without ending it; None without a plan."""
        return self._summary_of(self._current, self.clock()) if self._current is not None else None

    # ---- Events -------------------------------------------------------------

    def on_plan_adopted(self, plan, controller_initiated=True, pick_still_pending=False):
        """A new adoption became the playing plan (a repeat of the current one is ignored).

        ``controller_initiated`` False: it answers a replan the controller did
        not send (track change, recovery); a pending lowering is dropped.
        """
        if not self._enabled:
            return
        if self._current is not None and self._current.plan.plan_id == plan.plan_id:
            return
        now = self.clock()
        self._transport_trouble = False
        settled = self._pending
        previous = self._current
        self._drop_plan(now)
        reload = previous is not None and previous.attempt_started
        state = _PlanState(plan, replacement=reload)
        if previous is not None and not reload:
            state.tracker.first_waiting_at = previous.tracker.first_waiting_at
        self._current = state
        if pick_still_pending:
            self._pick_pending = True
        elif not controller_initiated:
            self._pick_pending = False

        if settled is None:
            return
        if not controller_initiated or settled.kind == _Pending.RESTORE:
            return
        delivered = _positive(plan.delivered_kbps)
        no_lower = plan.quality_reduction_unavailable or (
            delivered is not None and settled.from_kbps is not None and delivered >= settled.from_kbps)
        quiet = self._pick_pending
        if no_lower:
            state.action = CANT_LOWER
            state.cooldown_until = now + NO_LOWER_COOLDOWN_MS
            if not quiet:
                self._cant_lower_shown = True
                self._on_notice(NOTICE_CANT_LOWER)
        elif quiet:
            pass
        elif settled.kind == _Pending.AUTOMATIC:
            self._show_pill(UNDO)
        else:
            self._on_notice(NOTICE_LOWERED)

    def on_playing(self):
        """Playback is running: the first frame, or a resume from waiting or pause."""
        self._paused = False
        self._wait_cause = None
        if not self._enabled or self._current is None:
            return
        state = self._current
        now = self.clock()
        self._thaw_pill()
        start_wait = not state.attempt_started
        state.played = True
        if state.playing_since is None:
            state.playing_since = now
        tracker = state.tracker
        if tracker.first_playing_at is None:
            tracker.first_playing_at = now
        tracker.stalled = False
        if state.stats_stall_started_at is not None:
            self._record_stall_stat(state, now - state.stats_stall_started_at)
        state.stats_stall_started_at = None
        started_at = tracker.stall_started_at
        if started_at is not None:
            self._clear_stall_timing(state)
            duration = now - started_at
            if start_wait and not self._slow_link_evidence(state.plan):
                pass
            elif duration >= LONG_STALL_MS:
                tracker.long_stall_ended_at = now
            elif duration >= STALL_MIN_MS:
                tracker.stall_ends.append(now)
        self._evaluate(now)

    def on_waiting(self, cause=NETWORK):
        """The player is waiting for ``cause``; only a network wait can be a stall."""
        if cause != PAUSE:
            self._paused = False
        self._wait_cause = cause
        if not self._enabled or self._current is None:
            return
        state = self._current
        now = self.clock()
        if not self._paused:
            self._thaw_pill()
        self._stop_played_clock(state, now)
        tracker = state.tracker
        if state.replacement and tracker.starting:
            self._interrupt(state, now)
            return
        start_wait = tracker.starting and cause in (RELOAD, SEEK)
        if cause != NETWORK and not start_wait:
            self._interrupt(state, now)
            return
        tracker.stalled = True
        tracker.note_waiting(now)
        if self._transport_trouble:
            return
        self._start_stall_timing(state, now)

    def on_pause(self):
        """The viewer paused."""
        self._paused = True
        self._wait_cause = None
        if not self._enabled:
            return
        self._freeze_pill()
        state = self._current
        if state is None:
            return
        now = self.clock()
        self._stop_played_clock(state, now)
        self._interrupt(state, now)

    def on_seek(self):
        """The viewer seeked. A seek before the plan first plays leaves the start's wait running."""
        if not self._enabled or self._current is None:
            return
        state = self._current
        if state.tracker.starting:
            return
        now = self.clock()
        self._stop_played_clock(state, now)
        self._interrupt(state, now)

    def on_transport_error_start(self):
        """Media requests started failing, or the device went offline: an outage."""
        self._transport_trouble = True
        if not self._enabled or self._current is None:
            return
        state = self._current
        if state.tracker.stall_started_at is not None:
            self._clear_stall_timing(state)
            state.stats_stall_started_at = None

    def on_transport_error_end(self):
        """Media data arrives again."""
        self._transport_trouble = False
        self._transport_trouble_ended_at = self.clock()
        if not self._enabled or self._current is None:
            return
        state = self._current
        if state.tracker.stalled and state.tracker.stall_started_at is None:
            self._start_stall_timing(state, self.clock())

    def on_replan_failed(self):
        """The controller's last replan failed or will never be adopted."""
        if not self._enabled:
            return
        failed = self._pending
        if failed is not None and failed.kind == _Pending.AUTOMATIC and failed.from_state is not None:
            failed.from_state.action = failed.action_before
        self._pending = None
        self._run_deadline_failure_path()

    def on_quality_pick_sent(self):
        """The viewer's quality pick is on its way: any pill hides, nothing acts until it settles."""
        if not self._enabled:
            return
        self._pick_pending = True
        self._hide_pill()

    def on_quality_pick_refused(self):
        """The server refused the pick: a long stall in progress is evaluated at once."""
        if not self._pick_pending:
            return
        self._pick_pending = False
        self._evaluate(self.clock())

    def on_viewer_quality_change(self):
        """A quality change took effect; call it before adopting that plan."""
        self._pick_pending = False
        if not self._enabled:
            return
        self._hide_pill()
        self._answered_quality = None
        self._reoffer_not_before = None
        self._pending = None
        _cancel(self._rearm_job)
        self._rearm_job = None
        if self._current is not None:
            self._reset_tracker(self._current, self.clock())

    def on_playback_stopped(self):
        """The playing plan stopped (ended, closed or failed) without a successor."""
        if not self._enabled:
            return
        self._drop_plan(self.clock())

    def on_new_attempt(self):
        """A same-title attempt the viewer did not choose: the title's state stays."""
        self.on_playback_stopped()

    def on_start_deadline_missed(self, failure_path):
        """The playing plan missed the start deadline waiting for media.

        Returns True when it lowered as a long stall would; ``failure_path``
        then runs later if that lowering fails or is never adopted while this
        plan still has not played. False: run the failure path now.
        """
        if (not self._enabled or self._suspended or self._pick_pending or self._pending is not None
                or _active(self._rearm_job)):
            return False
        now = self.clock()
        recent_outage = (self._transport_trouble_ended_at is not None
                         and now - self._transport_trouble_ended_at < LONG_STALL_MS)
        if self._transport_trouble or recent_outage or self._wait_cause == RENDERER:
            return False
        state = self._current
        if state is None or not state.tracker.starting:
            return False
        if state.cooldown_until is not None and now < state.cooldown_until:
            return False
        plan = state.plan
        if plan.quality != AUTO or self.mode != ALWAYS:
            return False
        if self._answered_quality == plan.quality or not self._slow_link_evidence(plan):
            return False
        lowering = self._lowering_for(plan)
        if lowering is None:
            return False
        self._hide_pill()
        state.deadline_failure_path = failure_path
        self._send_lowering(state, lowering, _Pending.AUTOMATIC)
        return True

    def select_lower(self):
        """"Lower quality" on the prompt. True when a prompt was answered."""
        if self._active_pill != PROMPT or self._current is None:
            return False
        state = self._current
        replan = self._lowering_for(state.plan)
        if replan is None:
            return False
        self._hide_pill()
        self._send_lowering(state, replan, _Pending.ACCEPTED)
        return True

    def select_keep(self):
        """"Keep quality" on the prompt. True when a prompt was answered."""
        if self._active_pill != PROMPT or self._current is None:
            return False
        state = self._current
        self._hide_pill()
        self._answered_quality = state.plan.quality
        state.action = KEPT
        return True

    def select_restore(self):
        """"Restore quality" on the undo pill. True when an undo was answered."""
        if self._active_pill != UNDO or self._current is None:
            return False
        state = self._current
        self._hide_pill()
        state.action = RESTORED
        self._answered_quality = AUTO
        self._pending = _Pending(_Pending.RESTORE, None)
        self._start_rearm()
        self._on_replan(BufferingReplan(AUTO, None, None, LEARNED_LIMIT_CLEAR))
        return True

    def select_primary(self):
        """Select/OK at the player root: the visible pill's primary action."""
        if self._active_pill == PROMPT:
            return self.select_lower()
        if self._active_pill == UNDO:
            return self.select_restore()
        return False

    def back(self):
        """Back while a pill shows. True when a pill consumed it."""
        if self._active_pill == PROMPT:
            return self.select_keep()
        if self._active_pill == UNDO:
            self._hide_pill()
            self._undo_ended()
            return True
        return False

    def reset(self):
        """Clear all per-title state, for different content."""
        self._pick_pending = False
        self._drop_plan(self.clock())
        self._answered_quality = None
        self._cant_lower_shown = False
        self._reoffer_not_before = None
        self._prompt_shown_at = 0

    # ---- Trigger -------------------------------------------------------------

    def _evaluate(self, now):
        if not self._enabled or self._suspended or self._transport_trouble or self._pick_pending:
            return
        state = self._current
        if state is None or not state.tracker.should_trigger(now):
            return
        if self._is_busy():
            return
        if state.cooldown_until is not None and now < state.cooldown_until:
            self._suppress_until(state, state.cooldown_until, now)
            return
        plan = state.plan
        if self._answered_quality == plan.quality:
            return
        if not state.attempt_started and not self._slow_link_evidence(plan):
            return
        if self.mode == NEVER:
            state.action = IGNORED
            self._reset_tracker(state, now)
            return
        lowering = self._lowering_for(plan)
        if lowering is None:
            self._show_cant_lower(state)
            self._reset_tracker(state, now)
            return
        if plan.quality == AUTO and self.mode == ALWAYS:
            self._send_lowering(state, lowering, _Pending.AUTOMATIC)
            return
        if self._reoffer_not_before is not None and now < self._reoffer_not_before:
            self._suppress_until(state, self._reoffer_not_before, now)
            return
        self._show_pill(PROMPT)

    def _is_busy(self):
        return (self._pending is not None or _active(self._rearm_job)
                or self._active_pill is not None or self._deferred_pill is not None)

    def _slow_link_evidence(self, plan):
        delivered = _positive(plan.delivered_kbps)
        if delivered is None:
            return False
        observed = self._throughput()
        return observed is not None and observed < delivered

    def _suppress_until(self, state, lift_at, now):
        if state.tracker.stall_started_at is None:
            return
        _cancel(self._reevaluate_job)

        def fire():
            self._reevaluate_job = None
            if self._current is state and state.tracker.stall_started_at is not None:
                self._evaluate(self.clock())
        self._reevaluate_job = self._schedule(lift_at - now, fire)

    def _lowering_for(self, plan):
        rungs = sorted((r for r in plan.available_qualities if r.bitrate_kbps > 0),
                       key=lambda r: r.bitrate_kbps, reverse=True)
        delivered = _positive(plan.delivered_kbps)
        if plan.quality == AUTO:
            if delivered is None or not any(r.bitrate_kbps < delivered for r in rungs):
                return None
            estimate = delivered * 7 // 10
            if estimate < MIN_ESTIMATE_KBPS:
                return None
            return BufferingReplan(AUTO, estimate, SOURCE_OBSERVED_REBUFFER, None)
        named = None
        if plan.active_quality is not None:
            for rung in rungs:
                if rung.label == plan.active_quality:
                    named = rung.bitrate_kbps
                    break
        bounds = [b for b in (named, delivered) if b is not None]
        if not bounds:
            return None
        ceiling = min(bounds)
        for rung in rungs:
            if rung.bitrate_kbps < ceiling:
                return BufferingReplan(rung.label, None, None, None)
        return None

    def _send_lowering(self, state, replan, kind):
        self._pending = _Pending(kind, _positive(state.plan.delivered_kbps), state, state.action)
        state.action = LOWERED if kind == _Pending.AUTOMATIC else ACCEPTED
        self._start_rearm()
        self._on_replan(replan)

    def _show_cant_lower(self, state):
        state.action = CANT_LOWER
        if self._cant_lower_shown:
            return
        self._cant_lower_shown = True
        self._on_notice(NOTICE_CANT_LOWER)

    def _start_rearm(self):
        _cancel(self._rearm_job)

        def fire():
            self._rearm_job = None
            self._pending = None
            self._run_deadline_failure_path()
            if self._current is not None:
                self._reset_tracker(self._current, self.clock())
        self._rearm_job = self._schedule(REARM_AFTER_UNADOPTED_MS, fire)

    def _run_deadline_failure_path(self):
        state = self._current
        if state is None or state.deadline_failure_path is None:
            return
        failure_path = state.deadline_failure_path
        state.deadline_failure_path = None
        if state.tracker.starting:
            failure_path()

    # ---- Stall tracker -------------------------------------------------------

    def _start_stall_timing(self, state, now):
        tracker = state.tracker
        started_at = tracker.stall_started_at
        if started_at is None:
            started_at = max(now, tracker.grace_ends_at(now))
        tracker.stall_started_at = started_at
        if state.stats_stall_started_at is None:
            state.stats_stall_started_at = started_at
        if _active(self._long_stall_job):
            return

        def fire():
            self._long_stall_job = None
            if self._current is state and state.tracker.stall_started_at == started_at:
                self._evaluate(self.clock())
        self._long_stall_job = self._schedule(started_at + LONG_STALL_MS - now, fire)

    def _clear_stall_timing(self, state):
        state.tracker.stall_started_at = None
        _cancel(self._long_stall_job)
        self._long_stall_job = None
        _cancel(self._reevaluate_job)
        self._reevaluate_job = None

    def _interrupt(self, state, now):
        state.tracker.stalled = False
        self._clear_stall_timing(state)
        if state.stats_stall_started_at is not None:
            self._record_stall_stat(state, now - state.stats_stall_started_at, count=False)
        state.stats_stall_started_at = None

    def _reset_tracker(self, state, now):
        tracker = state.tracker
        tracker.first_playing_at = now - STARTUP_GRACE_MS
        tracker.first_waiting_at = None
        tracker.stall_ends = []
        tracker.long_stall_ended_at = None
        self._clear_stall_timing(state)
        if tracker.stalled and not self._transport_trouble:
            self._start_stall_timing(state, now)

    # ---- Summary -------------------------------------------------------------

    def _stop_played_clock(self, state, now):
        if state.playing_since is not None:
            state.played_ms += max(0, now - state.playing_since)
        state.playing_since = None

    def _record_stall_stat(self, state, duration_ms, count=True):
        if duration_ms <= 0:
            return
        if count and duration_ms >= STALL_MIN_MS:
            state.stall_count += 1
        state.stall_total_ms += duration_ms
        state.stall_longest_ms = max(state.stall_longest_ms, duration_ms)

    def _summary_of(self, state, now):
        playing = max(0, now - state.playing_since) if state.playing_since is not None else 0
        stall = 0
        if state.stats_stall_started_at is not None and now - state.stats_stall_started_at > 0:
            stall = now - state.stats_stall_started_at
        return PlanSummary(
            plan_id=state.plan.server_plan_id,
            played_ms=state.played_ms + playing,
            stall_count=state.stall_count + (1 if stall >= STALL_MIN_MS else 0),
            stall_total_ms=state.stall_total_ms + stall,
            stall_longest_ms=max(state.stall_longest_ms, stall),
            delivered_kbps=state.plan.delivered_kbps,
            mode=self.mode, action=state.action, adoption_id=state.plan.plan_id)

    def _drop_plan(self, now):
        if self._current is not None:
            self._on_plan_summary(self._summary_of(self._current, now))
        self._current = None
        self._pending = None
        self._hide_pill()
        for name in ("_long_stall_job", "_reevaluate_job", "_rearm_job"):
            _cancel(getattr(self, name))
            setattr(self, name, None)

    # ---- Pill ----------------------------------------------------------------

    def _show_pill(self, kind):
        if self._other_pill_visible or (self._view_hidden and kind == PROMPT):
            self._deferred_pill = kind
            self._deferred_resumes = False
            return
        self._present(kind, resume=False)

    def _present(self, kind, resume):
        self._clear_deferred()
        self._active_pill = kind
        if not resume:
            self._pill_remaining_ms = PROMPT_SECONDS * 1000
            if kind == PROMPT:
                self._prompt_shown_at = self.clock()
        self._publish_pill()
        if not self._paused:
            self._start_pill_timer()

    def _obscure_pill(self):
        kind = self._active_pill
        if kind is None:
            return
        self._stop_pill_timer()
        self._active_pill = None
        self._deferred_pill = kind
        self._deferred_resumes = True
        self._set_pill(None)

    def _clear_deferred(self):
        self._deferred_pill = None
        self._deferred_resumes = False

    def _start_pill_timer(self):
        _cancel(self._pill_job)
        self._pill_job = None
        if self._pill_remaining_ms <= 0:
            self._pill_timed_out()
            return
        self._pill_step()

    def _pill_step(self):
        # One whole-second step of the countdown, as the Android loop runs it.
        self._publish_pill()
        step = self._pill_remaining_ms - (self._seconds_for(self._pill_remaining_ms) - 1) * 1000
        self._pill_step_started_at = self.clock()

        def fire():
            elapsed = self.clock() - self._pill_step_started_at
            self._pill_remaining_ms -= max(0, min(elapsed, self._pill_remaining_ms))
            if self._pill_remaining_ms > 0:
                self._pill_step()
                return
            self._pill_job = None
            self._pill_timed_out()
        self._pill_job = self._schedule(step, fire)

    def _freeze_pill(self):
        if not _active(self._pill_job):
            return
        self._stop_pill_timer()
        self._publish_pill()

    def _stop_pill_timer(self):
        if _active(self._pill_job):
            elapsed = self.clock() - self._pill_step_started_at
            self._pill_remaining_ms -= max(0, min(elapsed, self._pill_remaining_ms))
            self._pill_job.cancel()
        self._pill_job = None

    def _thaw_pill(self):
        if self._active_pill is None or self._pill_job is not None:
            return
        self._start_pill_timer()

    def _pill_timed_out(self):
        kind = self._active_pill
        if kind is None:
            return
        self._hide_pill()
        if kind == UNDO:
            self._undo_ended()
            return
        state = self._current
        if state is None:
            return
        state.action = TIMED_OUT
        self._reoffer_not_before = self._prompt_shown_at + REOFFER_MIN_MS
        self._reset_tracker(state, self.clock())

    def _undo_ended(self):
        state = self._current
        if state is None or state.tracker.stall_started_at is None:
            return
        now = self.clock()
        if now - state.tracker.stall_started_at >= LONG_STALL_MS:
            self._evaluate(now)

    def _publish_pill(self):
        kind = None if self._view_hidden else self._active_pill
        self._set_pill(Pill(kind, self._seconds_for(self._pill_remaining_ms)) if kind else None)

    def _set_pill(self, pill):
        if pill != self.pill:
            self.pill = pill
            self._on_pill(pill)

    def _hide_pill(self):
        _cancel(self._pill_job)
        self._pill_job = None
        self._active_pill = None
        self._clear_deferred()
        self._pill_remaining_ms = 0
        self._set_pill(None)

    @staticmethod
    def _seconds_for(ms):
        return (ms + 999) // 1000
