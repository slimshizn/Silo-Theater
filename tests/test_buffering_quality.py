"""The buffering-quality controller replays the server's shared conformance vectors.

The fixture is ``docs/design/schemas/buffering-quality/v1/vectors.json`` from
the Silo server repo, byte for byte (web, Android and Apple replay the same
file). A failing case is a divergence from the other clients: fix the
controller, never the fixture.
"""

import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo import buffering_quality as bq

VECTORS = json.loads((Path(__file__).resolve().parent / "fixtures" / "buffering_quality_vectors.json")
                     .read_text(encoding="utf-8"))
CLIENT_REPLAN_OPERATIONS = {"track_change", "seek_reanchor", "seek_failure_recovery", "failure_recovery",
                            "output_change"}
REPLAN_BODY_KEYS = ("operation", "quality_preference", "bandwidth_estimate_kbps", "bandwidth_estimate_source",
                    "learned_limit")


class FakeClock(object):
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class ClientReplans(object):
    """The replans one playback sends, passed through the kept-estimate rule."""

    def __init__(self):
        self.attempt = 1
        self.kept = bq.KeptEstimate()
        self.adopted_quality = bq.AUTO

    def _body(self, operation, quality):
        return {"operation": operation, "playback_attempt_id": "attempt-%d" % self.attempt,
                "quality_preference": quality}

    def for_controller(self, replan):
        body = self._body("quality_change", replan.quality_preference)
        body.update(bq.replan_fields(replan))
        return self.kept.apply(body, replan)

    def for_client(self, operation):
        return self.kept.apply(self._body(operation, self.adopted_quality), None)


def plan_of(plan):
    warnings = plan.get("degradation_warnings") or []
    return bq.BufferingPlan(
        plan_id=plan["plan_id"], delivered_kbps=plan.get("delivered_kbps"), quality=plan["quality"],
        available_qualities=[bq.Rung(r["label"], r.get("bitrate_kbps") or 0)
                             for r in plan.get("available_qualities") or []],
        quality_reduction_unavailable=bq.QUALITY_REDUCTION_UNAVAILABLE in warnings,
        active_quality=plan.get("active_quality"))


def replay(case, default_mode):
    clock = FakeClock()
    outputs, summaries = [], []
    replans = ClientReplans()

    def replan_output(body):
        return {"t": clock.now, "type": "replan",
                "body": {k: body[k] for k in REPLAN_BODY_KEYS if body.get(k) is not None}}

    previous = {"kind": None}

    def on_pill(pill):
        kind = pill.kind if pill else None
        if kind != previous["kind"] and kind is not None:
            outputs.append({"t": clock.now, "type": kind + "_shown"})
        previous["kind"] = kind

    controller = bq.BufferingQualityController(
        on_replan=lambda replan: outputs.append(replan_output(replans.for_controller(replan))),
        on_notice=lambda key: outputs.append({"t": clock.now, "type": "notice", "key": key}),
        on_plan_summary=summaries.append, on_pill=on_pill,
        # The vectors carry no throughput: every plan's media count as arriving
        # below its delivered bitrate, the evidence a start's wait needs.
        plan_throughput_kbps=lambda: 1, clock=clock)
    mode = case.get("mode", default_mode)
    if bq.parse_mode(mode) is None:
        raise AssertionError("unknown mode %s" % mode)
    controller.set_mode(mode)

    def advance_to(t):
        if t < clock.now:
            raise AssertionError("event at %d is before the clock (%d)" % (t, clock.now))
        while True:
            due = controller.next_timer_at()
            if due is None or due > t:
                break
            clock.now = max(clock.now, due)
            controller.tick()
        clock.now = t
        controller.tick()

    for event in case["events"]:
        advance_to(event["t"])
        kind = event["type"]
        if kind == "plan_adopted":
            plan = plan_of(event["plan"])
            replans.adopted_quality = plan.quality
            controller.on_plan_adopted(plan, controller_initiated=event.get("controllerInitiated", True))
        elif kind == "client_replan":
            if event["operation"] not in CLIENT_REPLAN_OPERATIONS:
                raise AssertionError("unknown client_replan operation %s" % event["operation"])
            outputs.append(replan_output(replans.for_client(event["operation"])))
        elif kind == "quality_changed":
            controller.on_viewer_quality_change()
            replans.for_client("quality_change")
        elif kind == "new_attempt":
            controller.on_new_attempt()
            replans.attempt += 1
        elif kind == "waiting":
            cause = event.get("cause", bq.NETWORK)
            if cause not in (bq.NETWORK, bq.SEEK, bq.PAUSE, bq.RELOAD, bq.RENDERER):
                raise AssertionError("unknown waiting cause %s" % cause)
            controller.on_waiting(cause)
        else:
            handler = {
                "pick_sent": controller.on_quality_pick_sent,
                "pick_refused": controller.on_quality_pick_refused,
                "playing": controller.on_playing, "pause": controller.on_pause, "seek": controller.on_seek,
                "transport_error_start": controller.on_transport_error_start,
                "transport_error_end": controller.on_transport_error_end,
                "replan_failed": controller.on_replan_failed,
                "select_lower": controller.select_lower, "select_keep": controller.select_keep,
                "select_restore": controller.select_restore, "back": controller.back,
                "pill_obscured": lambda: controller.set_other_pill_visible(True),
                "pill_unobscured": lambda: controller.set_other_pill_visible(False),
            }.get(kind)
            if handler is None:
                raise AssertionError("unknown event type %s" % kind)
            handler()
    advance_to(case["until"])

    if outputs != case["expect"]:
        raise AssertionError("outputs\n  expected: %s\n  actual:   %s" % (case["expect"], outputs))
    actions = case.get("buffering_action")
    if actions:
        controller.on_playback_stopped()
        for plan_id, action in actions.items():
            matching = [s for s in summaries if s.plan_id == plan_id]
            if not matching:
                raise AssertionError("no plan_summary for %s (got %s)" % (plan_id, [s.plan_id for s in summaries]))
            if matching[-1].action != action:
                raise AssertionError("buffering_action for %s: expected %s, got %s"
                                     % (plan_id, action, matching[-1].action))


class VectorsTest(unittest.TestCase):
    def test_constants_match_the_vectors(self):
        constants = VECTORS["constants"]
        for name, value in constants.items():
            self.assertEqual(getattr(bq, name), value, name)
        self.assertEqual(bq.DEFAULT_MODE, VECTORS["defaults"]["mode"])

    def test_copy_matches_the_vectors(self):
        self.assertEqual(bq.COPY, VECTORS["copy"])

    def test_every_case_replays(self):
        cases = VECTORS["cases"]
        self.assertTrue(cases)
        for case in cases:
            with self.subTest(case=case["name"]):
                try:
                    replay(case, VECTORS["defaults"]["mode"])
                except AssertionError as error:
                    raise AssertionError("%s: %s" % (case["name"], error))


class GatingTest(unittest.TestCase):
    def body(self):
        return {"client_features": ["subrip_sidecar_v1"], "operation": "quality_change",
                "bandwidth_estimate_kbps": 44100, "bandwidth_estimate_source": "observed_rebuffer",
                "observed_throughput_kbps": 30000, "learned_limit": "clear"}

    def test_advertised_feature_keeps_the_fields_and_joins_client_features(self):
        body = bq.gate_replan_request(self.body(), ["buffering_quality_v1"])
        self.assertEqual(body["client_features"], ["subrip_sidecar_v1", "buffering_quality_v1"])
        self.assertEqual(body["observed_throughput_kbps"], 30000)
        self.assertEqual(body["learned_limit"], "clear")
        # Idempotent.
        self.assertEqual(bq.gate_replan_request(dict(body), ["buffering_quality_v1"]), body)

    def test_older_server_gets_only_the_estimate(self):
        body = bq.gate_replan_request(self.body(), ["subrip_sidecar_v1"])
        self.assertEqual(body, {"client_features": ["subrip_sidecar_v1"], "operation": "quality_change",
                                "bandwidth_estimate_kbps": 44100})

    def test_source_without_estimate_and_throughput_without_lowering_are_dropped(self):
        body = {"client_features": [], "bandwidth_estimate_source": "probe", "observed_throughput_kbps": 1}
        self.assertEqual(bq.gate_replan_request(body, ["buffering_quality_v1"]),
                         {"client_features": ["buffering_quality_v1"]})
        start = bq.gate_start_request({"client_features": ["buffering_quality_v1"], "learned_limit": "ignore"}, [])
        self.assertEqual(start, {"client_features": []})

    def test_plan_from_wire(self):
        plan = bq.plan_from_wire({
            "plan_id": "srv", "source": {"bitrate_kbps": 63000}, "effective_recipe": {"bitrate_kbps": 0},
            "available_qualities": [{"label": "1080p-high", "bitrate_kbps": 10000}],
            "degradation_warnings": [{"code": "quality_reduction_unavailable"}],
            "active_quality": "1080p-high"}, " Original ", "adoption-1")
        self.assertEqual((plan.plan_id, plan.server_plan_id, plan.delivered_kbps, plan.quality),
                         ("adoption-1", "srv", 63000, "auto"))
        self.assertTrue(plan.quality_reduction_unavailable)
        self.assertEqual(plan.active_quality, "1080p-high")

    def test_lowering_fields_carry_observed_throughput(self):
        replan = bq.BufferingReplan("auto", 44100, "observed_rebuffer", None)
        self.assertEqual(bq.replan_fields(replan, 30000)["observed_throughput_kbps"], 30000)
        self.assertNotIn("observed_throughput_kbps", bq.replan_fields(replan, None))
        restore = bq.BufferingReplan("auto", None, None, "clear")
        self.assertEqual(bq.replan_fields(restore, 30000),
                         {"operation": "quality_change", "quality_preference": "auto", "learned_limit": "clear"})


if __name__ == "__main__":
    unittest.main()
