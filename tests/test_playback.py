"""Offline playback protocol and Kodi boundary regression tests."""

from copy import deepcopy
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from silo.playback import (CAPABILITY_TERMINALS, PlaybackController, PlaybackError, PlaybackSession,
                           ProgressReporter, ResumeGuard, StallRecovery, embedded_subtitle_index,
                           effective_file_id, kodi_media_url, plan_duration, rewound_start, select_file,
                           server_anchored, subtitle_extension)
from silo.api import ApiError, SiloClient
from silo.storage import StorageError
from playback_contract import assert_contract


def playable_decision(offset=0, source_start=0, player_start=0):
    return {
        "protocol_version": 3, "server_features": [], "outcome": "playable",
        "session_id": "session/opaque id",
        "playback_plan": {
            "protocol_version": 3, "delivery": "original_http",
            "stream": {"url": "/api/v2/media/original?signature=opaque", "protocol": "http",
                       "headers": {}, "header_refresh": "none"},
            "timeline": {"source_start_seconds": source_start, "stream_origin_seconds": 0,
                         "player_start_seconds": player_start, "timeline_offset_seconds": offset,
                         "can_seek_anywhere": True, "seek_restoration": "client"},
            "transformations": [],
        },
    }


SESSION = "11111111-1111-4111-8111-111111111111"


def hls_decision(delivery="server_transcode_hls", source_start=0.0, origin=0.0, plan_id="plan:transcode-0001",
                 key="v3:transcode0001", qualities=None):
    """A converted decision shaped like /api/v2 projects the server's plan.

    A transcode of known length gets a complete timeline (seek locally); a
    copy remux is served from FFmpeg's growing playlist, anchored at the
    preceding keyframe ``origin`` and seeked through the server.
    """
    growing = delivery == "server_remux_hls"
    timeline = {"source_start_seconds": source_start,
                "stream_origin_seconds": origin if growing else 0,
                "player_start_seconds": source_start - origin if growing else source_start,
                "timeline_offset_seconds": origin if growing else 0,
                "can_seek_anywhere": not growing,
                "seek_restoration": "source_position" if growing else "player_position"}
    if growing:
        timeline["seek_window_start_seconds"] = origin
    return {
        "protocol_version": 3, "server_features": ["playback_plan_v3"], "outcome": "playable",
        "session_id": SESSION,
        "playback_plan": {
            "protocol_version": 3, "plan_id": plan_id, "plan_attempt_key": key, "session_id": SESSION,
            "delivery": delivery,
            "stream": {"url": "/api/v2/playback/transcode/%s/master.m3u8?st=opaque" % SESSION,
                       "protocol": "hls", "container": "hls", "mime_type": "application/vnd.apple.mpegurl",
                       "headers": {}, "header_refresh": "none"},
            "timeline": timeline,
            "selected_tracks": {"audio": {"id": "file:42:audio:1", "index": 1}},
            "effective_recipe": {"video_codec": "h264", "audio_codec": "aac", "width": 1920, "height": 1080,
                                 "bitrate_kbps": 6000, "dynamic_range": "sdr", "audio_channels": 2},
            "subtitle": {"mode": "off", "inventory": []},
            "transformations": [{"name": "video_to_h264", "executor": "server", "recipe_version": "1"},
                                {"name": "audio_to_aac", "executor": "server", "recipe_version": "2"}],
            "available_qualities": qualities if qualities is not None else [
                {"label": "original", "display_name": "Original", "height": 2160, "bitrate_kbps": 25000,
                 "preserves_source": True},
                {"label": "1080p-medium", "display_name": "1080p Medium", "height": 1080, "bitrate_kbps": 6000,
                 "preserves_source": False}],
            "degradation_warnings": [], "decision_reason": "quality_auto_source",
            "requested_media_file_id": "42", "effective_media_file_id": "42",
            "source": {"media_file_id": "42", "duration_seconds": 3000, "container": "mkv",
                       "video_codec": "hevc", "audio_codec": "dts"},
        },
    }


def server_capabilities(identity="11111111-2222-4333-8444-555555555555"):
    return {"installation_id": identity, "state": "available", "allowed": True,
            "protocol_versions": [3], "features": ["subrip_sidecar_v1", "sequenced_progress_v1"],
            "deliveries": ["original_http", "server_remux_progressive", "server_remux_hls",
                           "server_transcode_hls"], "revision": "opaque"}


class MemoryStore:
    def __init__(self, **values):
        self.values = {"profile_id": "profile/opaque-01", **values}

    def get(self, name, default=None):
        return self.values.get(name, default)

    def set(self, name, value):
        self.values[name] = value

    def update(self, values):
        self.values.update(values)


class FakeClient:
    """HTTP boundary fake; the real session builds every request body."""

    server_url = "https://silo.invalid"

    def __init__(self, decision=None, store=None):
        self.store = store or MemoryStore()
        self.decision = decision or playable_decision()
        self.calls = []
        self.fail_progress_once = False
        self.fail_stop_once = False
        self.progress_outcome = "applied"
        self.capabilities = server_capabilities()
        self.subtitle_bytes = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"
        self.download_error = None
        self.replans = []

    def download(self, url, max_bytes):
        self.calls.append(("DOWNLOAD", url, None))
        if self.download_error is not None:
            raise self.download_error
        return self.subtitle_bytes

    def request(self, method, path, body):
        self.calls.append((method, path, deepcopy(body)))
        if path == "/playback/capabilities":
            return deepcopy(self.capabilities)
        if path == "/playback/start":
            return deepcopy(self.decision)
        if path.endswith("/replan"):
            answer = self.replans.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return deepcopy(answer)
        if path.endswith("/progress"):
            if self.fail_progress_once:
                self.fail_progress_once = False
                raise OSError("The response was lost")
            return {"outcome": self.progress_outcome}
        if method == "DELETE":
            if self.fail_stop_once:
                self.fail_stop_once = False
                raise OSError("The response was lost")
            return {"outcome": "stopped", "stop_id": body["stop_id"]}
        raise AssertionError("Unexpected HTTP request")


class PlaybackSessionTests(unittest.TestCase):
    def test_start_payload_has_required_v3_fields_and_preserves_opaque_ids(self):
        client = FakeClient()
        session = PlaybackSession(client, "p7_fel")
        session.start("file/0001:opaque")
        method, path, body = client.calls[-1]
        self.assertEqual((method, path), ("POST", "/playback/start"))
        self.assertEqual(body["file_id"], "file/0001:opaque")
        self.assertEqual(body["profile_id"], "profile/opaque-01")
        self.assertEqual(body["installation_id"], client.capabilities["installation_id"])
        self.assertEqual(body["installation_id"], client.store.get("playback_installation_id"))
        self.assertTrue(body["installation_id"])
        self.assertFalse(body["allow_alternate_versions"])
        self.assertNotIn("bandwidth_estimate_kbps", body)
        assert_contract(self, body, "PlaybackStartBody")

    def test_installation_id_is_stable_while_attempt_ids_are_unique(self):
        client = FakeClient()
        first = PlaybackSession(client)
        second = PlaybackSession(client)
        first.start("file-a")
        second.start("file-b")
        self.assertEqual(first.installation_id, second.installation_id)
        attempts = [body for _, path, body in client.calls if path == "/playback/start"]
        self.assertNotEqual(attempts[0]["playback_attempt_id"], attempts[1]["playback_attempt_id"])

    def test_start_requires_a_selected_profile(self):
        client = FakeClient(store=MemoryStore(profile_id=""))
        with self.assertRaisesRegex(PlaybackError, "profile"):
            PlaybackSession(client).start("file-a")
        self.assertEqual(client.calls, [])

    def test_omitted_resume_uses_server_progress_and_explicit_zero_restarts(self):
        for resume in (None, 0, 125.5):
            with self.subTest(resume=resume):
                client = FakeClient()
                PlaybackSession(client).start("file-a", resume)
                body = client.calls[-1][2]
                if resume is None:
                    self.assertNotIn("start_position", body)
                else:
                    self.assertEqual(body["start_position"], resume)

    def test_progress_uses_source_timeline_including_a_signed_offset(self):
        for offset, source_start, player_start, player_position, expected in (
            (120, 120, 0, 7.25, 127.25),
            (-30, 0, 30, 45, 15),
        ):
            with self.subTest(offset=offset):
                client = FakeClient(playable_decision(offset, source_start, player_start))
                session = PlaybackSession(client)
                plan = session.start("file-a", source_start)
                self.assertEqual(plan["timeline"]["player_start_seconds"], player_start)
                session.progress(player_position, paused=True)
                body = client.calls[-1][2]
                self.assertEqual(body["position"], expected)
                self.assertTrue(body["is_paused"])
                self.assertEqual(client.calls[-1][1], "/playback/session%2Fopaque%20id/progress")
                assert_contract(self, body, "PlaybackProgressBody")

    def test_lost_progress_response_retries_the_exact_sample_and_sequence(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        client.fail_progress_once = True
        with self.assertRaises(OSError):
            session.progress(10, paused=False)
        first_body = client.calls[-1][2]
        client.progress_outcome = "replayed"
        session.progress(14, paused=True)
        self.assertEqual(client.calls[-2][2], first_body)
        self.assertEqual(client.calls[-1][2]["position"], 14)
        self.assertTrue(client.calls[-1][2]["is_paused"])
        session.progress(16, paused=True)
        next_body = client.calls[-1][2]
        self.assertEqual(next_body["sequence"], first_body["sequence"] + 2)
        self.assertEqual(next_body["position"], 16)

    def test_stop_retry_keeps_stop_identity_and_final_sample_idempotent(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        session.progress(23.5, paused=True)
        client.fail_stop_once = True
        with self.assertRaises(OSError):
            session.stop()
        first_body = client.calls[-1][2]
        self.assertEqual(first_body["position"], 23.5)
        self.assertTrue(first_body["is_paused"])
        assert_contract(self, first_body, "PlaybackStopBody")
        session.stop()
        self.assertEqual(client.calls[-1][2], first_body)
        count = len(client.calls)
        session.stop()
        self.assertEqual(len(client.calls), count)

    def test_stop_receipt_must_confirm_stopped_or_replayed(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        request = client.request

        def unconfirmed(method, path, body):
            if method == "DELETE":
                return {"outcome": "unknown"}
            return request(method, path, body)

        client.request = unconfirmed
        with self.assertRaisesRegex(PlaybackError, "did not confirm"):
            session.stop()
        self.assertIsNotNone(session.session_id)

    def test_playable_session_is_journaled_before_the_caller_can_open_media(self):
        client = FakeClient()

        def recorded(client_arg, installation_id, session_id):
            self.assertIs(client_arg, client)
            self.assertEqual(client.calls[-1][1], "/playback/start")
            self.assertEqual(installation_id, client.capabilities["installation_id"])
            self.assertEqual(session_id, "session/opaque id")
            return "journal-key"

        with patch("silo.playback.remember_session", side_effect=recorded) as remember:
            session = PlaybackSession(client)
            session.start("file-a")
        self.assertEqual(session.recovery_key, "journal-key")
        remember.assert_called_once()

    def test_stop_records_before_http_and_uses_first_writers_exact_body(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        session.progress(20)
        retained = {"installation_id": session.installation_id, "stop_id": "first-writer-id"}
        written = []
        request = client.request

        def stop_recorded(client_arg, key, body):
            written.append(key)
            return deepcopy(retained)

        def observed(method, path, body):
            if method == "DELETE":
                self.assertEqual(written, [session.recovery_key])
                self.assertEqual(body, retained)
            return request(method, path, body)

        client.request = observed
        with patch("silo.playback.remember_stop", side_effect=stop_recorded), patch("silo.playback.forget_session") as forget:
            session.stop()
        forget.assert_called_once_with(client, session.recovery_key)

    def test_stop_missing_or_ended_server_session_removes_only_its_journal(self):
        for status in (404, 410):
            with self.subTest(status=status):
                client = FakeClient()
                session = PlaybackSession(client)
                session.start("file-a")
                request = client.request

                def ended(method, path, body):
                    if method == "DELETE":
                        raise ApiError(status, "Already ended")
                    return request(method, path, body)

                client.request = ended
                with patch("silo.playback.forget_session") as forget:
                    session.stop()
                self.assertIsNone(session.session_id)
                forget.assert_called_once_with(client, session.recovery_key)

    def test_lost_stop_response_keeps_the_journal_for_retry_or_next_launch(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        client.fail_stop_once = True
        with patch("silo.playback.forget_session") as forget:
            with self.assertRaises(OSError):
                session.stop()
        forget.assert_not_called()
        self.assertIsNotNone(session.session_id)

    def test_journal_capacity_refusal_still_releases_the_unopened_server_attempt(self):
        client = FakeClient()
        session = PlaybackSession(client)
        with patch("silo.playback.remember_session", side_effect=StorageError("Too many unfinished sessions")):
            with self.assertRaisesRegex(PlaybackError, "unfinished sessions"):
                session.start("file-a")
        self.assertIsNotNone(session.session_id)
        session.stop()
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertNotIn("position", client.calls[-1][2])
        self.assertIsNone(session.session_id)

    def test_unopened_or_refused_plan_stop_does_not_overwrite_saved_progress(self):
        decision = playable_decision(source_start=300)
        decision["playback_plan"]["delivery"] = "hls"
        client = FakeClient(decision)
        session = PlaybackSession(client)
        with self.assertRaises(PlaybackError):
            session.start("file-a")
        session.stop()
        body = client.calls[-1][2]
        self.assertEqual(set(body), {"installation_id", "stop_id"})
        assert_contract(self, body, "PlaybackStopBody")

    def test_client_video_transformations_are_refused(self):
        decision = playable_decision()
        decision["playback_plan"]["transformations"] = [{"name": "client_dv7_to_hdr10", "executor": "client"}]
        with self.assertRaisesRegex(PlaybackError, "transformation"):
            PlaybackSession(FakeClient(decision)).start("file-a")

    def test_invalid_timeline_and_nonfinite_samples_fail_before_progress_http(self):
        for bad in (None, "not-a-number", float("nan"), float("inf")):
            with self.subTest(timeline=bad):
                decision = playable_decision()
                decision["playback_plan"]["timeline"]["source_start_seconds"] = bad
                with self.assertRaises(PlaybackError):
                    PlaybackSession(FakeClient(decision)).start("file-a")
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        for bad in (float("nan"), float("inf")):
            with self.subTest(sample=bad), self.assertRaises(PlaybackError):
                session.progress(bad)
        self.assertEqual(len(client.calls), 2)

    def test_start_requests_the_chosen_audio_and_subtitle(self):
        client = FakeClient()
        PlaybackSession(client, options={"audio_track_index": 2, "subtitle_track_index": 1}).start("file-a")
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertEqual((body["audio_track_index"], body["subtitle_track_index"]), (2, 1))
        client = FakeClient()
        PlaybackSession(client, options={"subtitles_off": True}).start("file-a")
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertNotIn("subtitle_track_index", body)

    def test_slightly_negative_kodi_times_mean_the_start_not_an_error(self):
        # Kodi reports small negative times around seeks and HLS starts; an
        # earlier build stopped playback with "invalid playback time" for them.
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        session.progress(-0.4)
        self.assertEqual(session.position, 0.0)
        decision = playable_decision()
        decision["playback_plan"]["timeline"]["player_start_seconds"] = -2
        with self.assertRaises(PlaybackError) as raised:
            PlaybackSession(FakeClient(decision)).start("file-a")
        self.assertIn("timeline.player_start_seconds", str(raised.exception))

    def test_terminal_201_decision_never_opens_the_kodi_player(self):
        terminal = {"protocol_version": 3, "server_features": [], "outcome": "terminal",
                    "terminal": {"reason": "no_route", "message": "No supported route", "retryable": False}}
        requests = []

        def transport(method, url, headers, body):
            requests.append((method, url, headers, deepcopy(body)))
            if method == "GET":
                return 200, {}, server_capabilities()
            return 201, {}, terminal

        store = MemoryStore(server_url="https://silo.invalid", access_token="account-token",
                            expires_at=10000)
        client = SiloClient("https://silo.invalid", store, transport, clock=lambda: 100)
        opened, dialogs = [], []

        class KodiPlayer:
            def play(self, *args):
                opened.append(args)

            def isPlaying(self):
                return False

        xbmc = SimpleNamespace(Player=KodiPlayer, Monitor=lambda: object(),
                               log=lambda *args: None, LOGWARNING=2, LOGINFO=1)
        xbmcgui = SimpleNamespace(Dialog=lambda: SimpleNamespace(ok=lambda *args: dialogs.append(args)))
        addon = SimpleNamespace(getSetting=lambda key: "auto", getAddonInfo=lambda key: "0.1.0")
        with patch.dict(sys.modules, {"xbmc": xbmc, "xbmcgui": xbmcgui}):
            PlaybackController(client, addon).play({"file_id": "file-a"}, options={"dv_mode": "auto"})
        self.assertEqual(opened, [])
        self.assertIn("No supported route", dialogs[0][1])
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[-1][0], "POST")
        self.assertTrue(requests[-1][1].endswith("/api/v2/playback/start"))

    def test_legacy_client_uuid_is_ignored_for_server_installation(self):
        client = FakeClient(store=MemoryStore(installation_id="legacy-random-client-id"))
        PlaybackSession(client).start("file-a")
        self.assertEqual(client.calls[0][0:2], ("GET", "/playback/capabilities"))
        self.assertEqual(client.calls[1][2]["installation_id"], client.capabilities["installation_id"])
        self.assertNotEqual(client.calls[1][2]["installation_id"], "legacy-random-client-id")
        assert_contract(self, client.capabilities, "PlaybackCapabilities")

    def test_installation_change_refreshes_once_before_repeating_same_attempt(self):
        client = FakeClient()
        original_request = client.request
        failures = [True]

        def request(method, path, body):
            if path == "/playback/start" and failures:
                failures.pop()
                client.calls.append((method, path, deepcopy(body)))
                client.capabilities["installation_id"] = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
                raise ApiError(409, "Playback installation changed; refresh capabilities", "installation_changed")
            return original_request(method, path, body)

        client.request = request
        PlaybackSession(client).start("file-a", 42)
        self.assertEqual([path for _, path, _ in client.calls], ["/playback/capabilities", "/playback/start",
                                                              "/playback/capabilities", "/playback/start"])
        first, second = client.calls[1][2], client.calls[3][2]
        self.assertNotEqual(first["installation_id"], second["installation_id"])
        self.assertEqual(first["playback_attempt_id"], second["playback_attempt_id"])
        self.assertEqual(second["start_position"], 42)

    def test_installation_change_retry_is_bounded_and_other_conflicts_are_not_retried(self):
        for code, expected_count in (("installation_changed", 4), ("idempotency_conflict", 2)):
            with self.subTest(code=code):
                client = FakeClient()
                original_request = client.request

                def request(method, path, body):
                    if path == "/playback/start":
                        client.calls.append((method, path, deepcopy(body)))
                        raise ApiError(409, "Conflict", code)
                    return original_request(method, path, body)

                client.request = request
                with self.assertRaises(ApiError):
                    PlaybackSession(client).start("file-a")
                self.assertEqual(len(client.calls), expected_count)

    def test_unsupported_or_unavailable_capabilities_never_start_playback(self):
        for fields in ({"allowed": False}, {"state": "disabled"}, {"protocol_versions": [2]},
                       {"deliveries": ["hls"]}, {"installation_id": ""}):
            with self.subTest(fields=fields):
                client = FakeClient()
                client.capabilities.update(fields)
                with self.assertRaises(PlaybackError):
                    PlaybackSession(client).start("file-a")
                self.assertEqual(len(client.calls), 1)

    def test_start_sets_server_progress_and_only_negotiated_subtitle_feature(self):
        client = FakeClient()
        client.capabilities["features"] = []
        PlaybackSession(client, options={"max_resolution": "720p", "subtitle_mode": "off"}).start("file-a")
        body = client.calls[-1][2]
        self.assertEqual(body["progress_persistence"], "server")
        self.assertEqual(body["client_features"], [])
        self.assertEqual(body["client_capabilities"]["max_resolution"], "720p")
        self.assertNotIn("subtitle_track_id", body)
        self.assertNotIn("subtitle_track_index", body)
        assert_contract(self, body, "PlaybackStartBody")


    def test_chosen_subtitle_is_requested_by_playback_index(self):
        client = FakeClient()
        PlaybackSession(client, options={"subtitle_track_index": 2}).start("file-a")
        body = client.calls[-1][2]
        self.assertEqual(body["subtitle_track_index"], 2)
        assert_contract(self, body, "PlaybackStartBody")

    def test_no_choice_or_subtitles_setting_off_requests_no_subtitle(self):
        for options in ({}, {"subtitle_track_index": None}, {"subtitle_track_index": -1},
                        {"subtitle_track_index": True}, {"subtitle_track_index": 2, "subtitle_mode": "off"}):
            with self.subTest(options=options):
                client = FakeClient()
                PlaybackSession(client, options=options).start("file-a")
                self.assertNotIn("subtitle_track_index", client.calls[-1][2])

    def test_capability_terminals_name_the_display_setting_to_change(self):
        for reason in sorted(CAPABILITY_TERMINALS):
            with self.subTest(reason=reason):
                decision = {"protocol_version": 3, "server_features": [], "outcome": "terminal",
                            "terminal": {"reason": reason, "message": "server wording", "retryable": False}}
                with self.assertRaises(PlaybackError) as error:
                    PlaybackSession(FakeClient(decision)).start("file-a")
                self.assertIn("HDR and Dolby Vision capability", str(error.exception))
                self.assertIn("Maximum video resolution", str(error.exception))

    def test_other_terminals_keep_the_server_message(self):
        decision = {"protocol_version": 3, "server_features": [], "outcome": "terminal",
                    "terminal": {"reason": "source_unreadable", "message": "The file is unreadable", "retryable": False}}
        with self.assertRaisesRegex(PlaybackError, "The file is unreadable"):
            PlaybackSession(FakeClient(decision)).start("file-a")


class ResumeGuardTests(unittest.TestCase):
    def test_startup_zero_and_early_seek_samples_are_not_accepted(self):
        now = [0.0]
        guard = ResumeGuard(180, clock=lambda: now[0])
        for value in (0, 0.2, 4, 120):
            self.assertFalse(guard.accept(value))
        now[0] = 0.5
        self.assertTrue(guard.accept(180))
        self.assertTrue(guard.accept(0))  # Later intentional seeks may start over.

    def test_seek_failure_retries_then_gives_up_quietly(self):
        now = [0.0]
        guard = ResumeGuard(180, clock=lambda: now[0], timeout=30)
        self.assertFalse(guard.retry_due())
        now[0] = 4
        self.assertTrue(guard.retry_due())
        self.assertFalse(guard.retry_due())
        now[0] = 8
        self.assertTrue(guard.retry_due())
        now[0] = 20
        self.assertFalse(guard.retry_due())
        self.assertFalse(guard.accept(0))
        now[0] = 30
        self.assertTrue(guard.accept(0))
        self.assertTrue(guard.gave_up)

    def test_restart_zero_needs_no_seek_confirmation(self):
        self.assertTrue(ResumeGuard(0).accept(0))

    def test_source_selection_skips_unreadable_files_and_preserves_id(self):
        item = {"files": [{"file_id": "bad", "unreadable": True}, {"file_id": "0001/opaque"}]}
        self.assertEqual(select_file(item), "0001/opaque")
        with self.assertRaises(PlaybackError):
            select_file({"files": [{"file_id": "bad", "unreadable": True}]})


class FakeRelay:
    """Stands in for the loopback relay; records what it was asked to serve."""

    def __init__(self, client):
        self.routes, self.closed = [], False
        FakeRelay.last = self

    def register(self, url, headers):
        self.routes.append((url, headers))
        return "http://127.0.0.1:9/relay/" + url.rsplit("/", 1)[-1]

    def close(self):
        self.closed = True


class KodiHarness(unittest.TestCase):
    def run_player(self, client, events, start=None, options=None, file_id=None):
        """Exercise the real controller against a Kodi callback/event double."""
        state = SimpleNamespace(now=0.0, player=None, dialogs=[], subtitles=[], visibility=[], seeks=[],
                                in_callback=False, logs=[], native_times=0,
                                subtitle_streams=getattr(self, "subtitle_streams", []), subtitle_selected=[],
                                audio_streams=getattr(self, "audio_streams", []), audio_selected=[],
                                listitem_props={},
                                profile_dir=getattr(self, "profile_dir", None) or tempfile.mkdtemp())
        pending = list(events)
        polling_thread = threading.get_ident()
        test = self

        class KodiPlayer:
            def __init__(self):
                state.player = self
                self.position = 0.0
                self.playing = False

            def play(self, *args):
                state.plays = getattr(state, "plays", []) + [args[0]]
                if getattr(test, "fail_opens", 0):
                    test.fail_opens -= 1
                    self.onPlayBackError()
                    return
                self.playing = True
                self.onAVStarted()

            def isPlaying(self):
                raise AssertionError("No teardown/native probe is needed")

            def getTime(self):
                if state.in_callback or self.finished or threading.get_ident() != polling_thread:
                    raise AssertionError("Native time queried from callback or after teardown")
                state.native_times += 1
                if getattr(self, "time_unavailable", False):
                    raise RuntimeError("No current native media")
                return self.position if self.playing else 0.0

            def seekTime(self, seconds):
                state.seeks.append(seconds)

            def stop(self):
                if self.finished:
                    raise AssertionError("Stopped player was queried again")
                state.in_callback = True
                try:
                    self.onPlayBackStopped()
                finally:
                    state.in_callback = False
                self.playing = False

            def showSubtitles(self, enabled):
                state.visibility.append(enabled)

            def setSubtitles(self, url):
                state.subtitles.append(url)

            def getAvailableSubtitleStreams(self):
                return list(state.subtitle_streams)

            def setSubtitleStream(self, index):
                state.subtitle_selected.append(index)

            def getAvailableAudioStreams(self):
                return list(state.audio_streams)

            def setAudioStream(self, index):
                state.audio_selected.append(index)

            def pause(self):
                state.pauses = getattr(state, "pauses", 0) + 1

            def getTotalTime(self):
                return 0.0

        class Monitor:
            def waitForAbort(self, seconds):
                # Yield to the real reporter thread; production Monitor does so.
                time.sleep(0.001)
                state.now += seconds
                if pending:
                    position, action = pending.pop(0)
                    state.player.position = position
                    state.player.time_unavailable = action == "unavailable"
                    state.in_callback = True
                    try:
                        if action == "pause":
                            state.player.onPlayBackPaused()
                        elif action == "resume":
                            state.player.onPlayBackResumed()
                        elif action == "stop":
                            state.player.onPlayBackStopped()
                            state.player.playing = False
                        elif action == "end":
                            state.player.onPlayBackEnded()
                            state.player.playing = False
                    finally:
                        state.in_callback = False
                return False

        class ListItem:
            def __init__(self, **kwargs):
                pass

            def getVideoInfoTag(self):
                return SimpleNamespace(setTitle=lambda value: None, setPlot=lambda value: None)

            def setProperty(self, key, value):
                state.listitem_props[key] = value

            def setMimeType(self, value):
                state.listitem_props["<mime>"] = value

            def setContentLookup(self, value):
                state.listitem_props["<content_lookup>"] = value

        windows = getattr(self, "platform_windows", False)
        conditions = getattr(self, "conditions", set())
        state.notices = []
        xbmc = SimpleNamespace(Player=KodiPlayer, Monitor=Monitor, executebuiltin=lambda command: None,
                               log=lambda message, level: state.logs.append(message), LOGWARNING=2, LOGINFO=1,
                               getCondVisibility=lambda name: name in conditions or (
                                   windows and name == "System.Platform.Windows"))
        xbmcgui = SimpleNamespace(ListItem=ListItem, Dialog=lambda: SimpleNamespace(
            ok=lambda *args: state.dialogs.append(args),
            notification=lambda heading, message, **kwargs: state.notices.append(message)))
        addon = SimpleNamespace(getSetting=lambda key: "", getAddonInfo=lambda key: "0.2.1")
        xbmcvfs = SimpleNamespace(translatePath=lambda path: state.profile_dir)
        with patch.dict(sys.modules, {"xbmc": xbmc, "xbmcgui": xbmcgui, "xbmcvfs": xbmcvfs}), \
                patch("silo.playback.time.monotonic", side_effect=lambda: state.now):
            controller = PlaybackController(client, addon)
            controller.relay_factory = FakeRelay
            FakeRelay.last = None
            state.controller = controller
            if getattr(self, "configure", None):
                self.configure(controller, state)
            extras = getattr(self, "fake_extras", None)
            with patch.object(PlaybackController, "_extras",
                              (lambda *args, **kwargs: extras) if extras is not None else PlaybackController._extras):
                controller.play({"file_id": "default-file", "title": "A title"},
                                start_position=start, file_id=file_id,
                                options=options or {"progress_interval": 10, "subtitle_mode": "server"},
                                exact_start=getattr(self, "exact_start", False))
            state.result = controller.last_result
            if controller.last_reporter:
                self.assertTrue(controller.last_reporter.wait(1), "Reporter did not finish")
        return state


class FullscreenFor:
    """Fullscreen video is visible for the first ``checks`` polls, then gone (Back)."""

    def __init__(self, checks):
        self.checks = checks

    def __contains__(self, name):
        if name != "Window.IsVisible(fullscreenvideo)":
            return False
        self.checks -= 1
        return self.checks >= 0


class ControllerTests(KodiHarness):
    def test_back_out_of_fullscreen_stops_playback(self):
        self.conditions = FullscreenFor(3)
        client = FakeClient()
        ticks = [(1 + index * 0.25, None) for index in range(20)]
        state = self.run_player(client, ticks, start=0)
        self.assertTrue(any("left_fullscreen_stop" in line for line in state.logs))
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertFalse(state.result["ended"])

    def test_dialogs_over_fullscreen_and_the_setting_keep_playing(self):
        self.conditions = FullscreenFor(1000)
        state = self.run_player(FakeClient(), [(1 + index * 0.25, None) for index in range(12)] + [(4.5, "stop")],
                                start=0)
        self.assertFalse(any("left_fullscreen_stop" in line for line in state.logs))
        self.conditions = FullscreenFor(3)
        state = self.run_player(FakeClient(), [(1 + index * 0.25, None) for index in range(12)] + [(4.5, "stop")],
                                start=0, options={"progress_interval": 10, "subtitle_mode": "server",
                                                  "stop_on_back": False})
        self.assertFalse(any("left_fullscreen_stop" in line for line in state.logs))

    def test_never_fullscreen_never_stops(self):
        self.conditions = FullscreenFor(0)
        state = self.run_player(FakeClient(), [(1 + index * 0.25, None) for index in range(12)] + [(4.5, "stop")],
                                start=0)
        self.assertFalse(any("left_fullscreen_stop" in line for line in state.logs))

    def test_resume_waits_for_seek_then_stop_uses_cached_sample_without_native_callback_reads(self):
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(0, None), (0.2, None), (180.1, None), (180.6, "stop")], start=180)
        self.assertEqual(state.seeks, [180])
        samples = [body for _, path, body in client.calls if path.endswith("/progress")]
        self.assertEqual(samples[0]["position"], 180.1)
        self.assertTrue(all(sample["position"] >= 180 for sample in samples))
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertEqual(client.calls[-1][2]["position"], 180.1)
        self.assertEqual(state.result, {"has_sample": True, "position_seconds": 180.1, "ended": False})
        self.assertEqual(state.dialogs, [])

    def test_stop_before_seek_lands_never_overwrites_saved_resume(self):
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        self.run_player(client, [(0, None), (0, "stop")], start=180)
        self.assertFalse(any(path.endswith("/progress") for _, path, _ in client.calls))
        self.assertNotIn("position", client.calls[-1][2])

    def test_failed_seek_retries_then_keeps_playing_from_where_kodi_is(self):
        # Kodi stays at 0:00 (a converted stream not ready at the saved spot):
        # the seek is retried twice, then playback continues with a notice
        # instead of stopping with an error, and nothing is sent before that.
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(0, None)] * 39 + [(0.3, None), (0.6, "stop")], start=180)
        self.assertEqual(state.seeks, [180, 180, 180])
        self.assertEqual(state.dialogs, [])
        self.assertTrue(any("saved position" in notice for notice in state.notices))
        self.assertTrue(any("resume_seek_gave_up" in line for line in state.logs))
        samples = [body for _, path, body in client.calls if path.endswith("/progress")]
        self.assertTrue(samples and all(sample["position"] < 1 for sample in samples))

    def test_resume_opens_the_stream_at_the_saved_position(self):
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(180.1, None), (180.6, "stop")], start=180)
        self.assertEqual(state.listitem_props.get("StartOffset"), "180.000")
        # This fake player starts at 0:00 whatever StartOffset says, so the add-on seeks once.
        self.assertEqual(state.seeks, [180])

    def test_no_seek_when_kodi_already_started_at_the_saved_position(self):
        from silo.playback import player_near

        class Player:
            def __init__(self, at):
                self.at = at

            def getTime(self):
                if self.at is None:
                    raise RuntimeError("not playing")
                return self.at

        self.assertTrue(player_near(Player(180.4), 180))
        self.assertFalse(player_near(Player(0.0), 180))
        self.assertFalse(player_near(Player(None), 180))

    def test_pause_resume_are_reported_without_waiting_for_heartbeat(self):
        client = FakeClient()
        self.run_player(client, [(5, None), (5.25, "pause"), (5.25, "resume"), (5.7, "stop")], start=0)
        samples = [body for _, path, body in client.calls if path.endswith("/progress")]
        self.assertEqual([sample["is_paused"] for sample in samples], [False, True, False])
        self.assertEqual(client.calls[-1][2]["position"], 5.25)

    def test_explicit_file_and_start_over_and_subtitle_off_options_are_executed(self):
        decision = playable_decision()
        decision["playback_plan"]["subtitle"] = {"mode": "render", "artifact": {"url": "/subtitle.srt"}}
        client = FakeClient(decision)
        state = self.run_player(client, [(1, None), (2, "stop")], start=0, file_id="chosen-file",
                                options={"subtitle_mode": "off", "max_resolution": "720p"})
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertEqual(body["file_id"], "chosen-file")
        self.assertEqual(body["start_position"], 0)
        self.assertEqual(body["client_capabilities"]["max_resolution"], "720p")
        self.assertEqual(state.visibility, [False])
        self.assertEqual(state.subtitles, [])
        self.assertEqual(state.seeks, [])

    def test_omitted_resume_uses_server_timeline_including_offset(self):
        client = FakeClient(playable_decision(offset=100, source_start=140, player_start=40))
        self.run_player(client, [(0, None), (40, None), (41, "end")], start=None)
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertNotIn("start_position", body)
        self.assertEqual(client.calls[-1][2]["position"], 140)

    def test_final_stop_transient_failure_retries_same_identity_and_sample(self):
        client = FakeClient()
        request = client.request
        failures = [True]

        def fail_once(method, path, body):
            if method == "DELETE" and failures:
                failures.pop()
                client.calls.append((method, path, deepcopy(body)))
                raise ApiError(0, "Connection response lost")
            return request(method, path, body)

        client.request = fail_once
        self.run_player(client, [(1, None), (1.7, "stop")], start=0)
        stops = [body for method, _, body in client.calls if method == "DELETE"]
        self.assertEqual(len(stops), 2)
        self.assertEqual(stops[0], stops[1])
        self.assertEqual(stops[0]["position"], 1)

    def test_network_progress_and_stop_never_run_on_the_polling_thread(self):
        client = FakeClient()
        request = client.request
        polling_thread = threading.get_ident()
        violations = []

        def checked(method, path, body):
            if (path.endswith("/progress") or method == "DELETE") and threading.get_ident() == polling_thread:
                violations.append(path)
            return request(method, path, body)

        client.request = checked
        state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        self.assertEqual(violations, [])
        self.assertTrue(state.result["has_sample"])
        self.assertTrue(any("event=begin_v0.2.1" in message for message in state.logs))

    def test_missing_native_time_without_stop_callback_exits_within_two_seconds(self):
        client = FakeClient()
        state = self.run_player(client, [(0, "unavailable")], start=0)
        self.assertLessEqual(state.now, 2.5)
        self.assertTrue(any("event=native_time_lost" in message for message in state.logs))
        self.assertFalse(state.result["has_sample"])
        self.assertNotIn("position", client.calls[-1][2])


    def subtitle_decision(self, subtitle):
        decision = playable_decision()
        decision["playback_plan"]["subtitle"] = subtitle
        return decision

    def test_sidecar_subtitle_is_saved_locally_and_kodi_never_gets_credentials(self):
        artifact_url = "/api/v2/stream/s1/subtitles/2.vtt?file_id=f&external_subtitle_key=k"
        client = FakeClient(self.subtitle_decision(
            {"mode": "render", "track_id": "file:f:subtitle:2",
             "artifact": {"url": artifact_url, "format": "vtt", "mime_type": "text/vtt"}}))
        state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        self.assertIn(("DOWNLOAD", artifact_url, None), client.calls)
        self.assertEqual(len(state.subtitles), 1)
        path = state.subtitles[0]
        self.assertTrue(path.startswith(os.path.join(state.profile_dir, "subtitles")))
        self.assertTrue(path.endswith(".vtt"))
        for secret in ("|", "Bearer", "Authorization", "X-Profile", "http"):
            self.assertNotIn(secret, path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), client.subtitle_bytes)

    def test_previous_subtitle_files_are_replaced(self):
        self.profile_dir = tempfile.mkdtemp()
        folder = os.path.join(self.profile_dir, "subtitles")
        os.makedirs(folder)
        old = os.path.join(folder, "silo-subtitle-old.srt")
        unrelated = os.path.join(folder, "keep-me.txt")
        for name in (old, unrelated):
            with open(name, "w") as handle:
                handle.write("x")
        try:
            client = FakeClient(self.subtitle_decision(
                {"mode": "render", "artifact": {"url": "/api/v2/stream/s1/subtitles/0.srt"}}))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.profile_dir
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(unrelated))
        self.assertEqual(len(state.subtitles), 1)

    def test_subtitle_download_failure_still_plays_without_subtitles(self):
        client = FakeClient(self.subtitle_decision(
            {"mode": "render", "artifact": {"url": "/api/v2/stream/s1/subtitles/2.srt"}}))
        client.download_error = ApiError(404, "Not found", "download_failed")
        state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        self.assertEqual(state.subtitles, [])
        self.assertEqual(state.dialogs, [])
        self.assertTrue(any("subtitle_unavailable" in line for line in state.logs))
        self.assertEqual(client.calls[-1][0], "DELETE")

    def test_embedded_subtitle_track_is_selected_by_its_embedded_position(self):
        self.subtitle_streams = ["eng (PGS)", "eng (SRT)", "fre"]
        try:
            client = FakeClient(self.subtitle_decision({
                "mode": "render", "track_id": "file:f:subtitle:3",
                "embedded": {"stream_index": 7, "container_track_id": "8"},
                "inventory": [
                    {"source": "external", "combined_index": 0, "track_id": "file:f:subtitle:0"},
                    {"source": "embedded", "combined_index": 1, "track_id": "file:f:subtitle:1",
                     "delivery": "burn_in_only"},
                    {"source": "embedded", "combined_index": 2, "track_id": "file:f:subtitle:2"},
                    {"source": "embedded", "combined_index": 3, "track_id": "file:f:subtitle:3"},
                    {"source": "downloaded", "combined_index": 4, "track_id": "file:f:subtitle:4"},
                ]}))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.subtitle_streams
        self.assertEqual(state.subtitle_selected, [2])
        self.assertEqual(state.visibility, [True])
        self.assertEqual(state.subtitles, [])
        self.assertFalse(any(call[0] == "DOWNLOAD" for call in client.calls))

    def test_embedded_track_beyond_kodis_list_hides_subtitles(self):
        # Leaving Kodi alone would let Kodi's own subtitle settings pick a track.
        self.subtitle_streams = ["eng"]
        try:
            client = FakeClient(self.subtitle_decision({
                "mode": "render", "track_id": "file:f:subtitle:2", "embedded": {"stream_index": 5},
                "inventory": [{"source": "embedded", "combined_index": i, "track_id": "file:f:subtitle:%d" % i}
                              for i in range(3)]}))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.subtitle_streams
        self.assertEqual(state.subtitle_selected, [])
        self.assertEqual(state.visibility, [False])


    def audio_decision(self, index):
        decision = playable_decision()
        decision["playback_plan"]["selected_tracks"] = {"audio": {"id": "file:f:audio:%d" % index, "index": index}}
        return decision

    def start_claims(self, client):
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        return body["client_playback_context"]["deliveries"]["original_http"]["validated_claims"]

    def test_other_platforms_apply_the_planned_audio_track_and_claim_it(self):
        self.audio_streams = ["eng", "fre"]
        try:
            client = FakeClient(self.audio_decision(1))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.audio_streams
        self.assertEqual(state.audio_selected, [1])
        self.assertIn("client_selected_audio_track_v1", self.start_claims(client))

    def test_windows_applies_the_planned_audio_track_like_other_platforms(self):
        self.audio_streams = ["eng", "fre"]
        self.platform_windows = True
        try:
            client = FakeClient(self.audio_decision(1))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.audio_streams, self.platform_windows
        self.assertEqual(state.audio_selected, [1])
        self.assertIn("client_selected_audio_track_v1", self.start_claims(client))


    def test_frozen_decoder_gets_a_forward_nudge_and_playback_continues(self):
        # Plays to 5 s, then the position freezes for 4 s of polling, then moves.
        events = [(1, None), (2, None), (3, None), (4, None), (5, None)]
        events += [(5, None)] * 16
        events += [(9, None), (10, None), (11, "stop")]
        client = FakeClient()
        state = self.run_player(client, events, start=0)
        self.assertEqual(state.seeks, [8.0])
        self.assertTrue(any("stall_recovery_seek" in line for line in state.logs))
        self.assertEqual(client.calls[-1][0], "DELETE")

    def test_kodi_is_told_not_to_scan_the_stream_folder_for_subtitles(self):
        state = self.run_player(FakeClient(), [(1, None), (2, "stop")], start=0)
        self.assertEqual(state.listitem_props.get("no-ext-subs-scan"), "true")
        self.assertEqual(state.listitem_props.get("IsPlayable"), "true")

    def test_other_platforms_keep_the_full_item_handoff(self):
        decision = playable_decision()
        decision["playback_plan"]["stream"]["mime_type"] = "video/x-matroska"
        state = self.run_player(FakeClient(decision), [(1, None), (2, "stop")], start=0)
        self.assertEqual(state.listitem_props.get("<mime>"), "video/x-matroska")
        self.assertIs(state.listitem_props.get("<content_lookup>"), False)
        self.assertEqual(state.listitem_props.get("IsPlayable"), "true")

    def test_windows_keeps_the_full_item_handoff(self):
        decision = playable_decision()
        decision["playback_plan"]["stream"]["mime_type"] = "video/x-matroska"
        self.platform_windows = True
        try:
            state = self.run_player(FakeClient(decision), [(1, None), (2, "stop")], start=0)
        finally:
            del self.platform_windows
        self.assertEqual(state.listitem_props.get("<mime>"), "video/x-matroska")
        self.assertIs(state.listitem_props.get("<content_lookup>"), False)
        self.assertEqual(state.listitem_props.get("no-ext-subs-scan"), "true")
        self.assertFalse(any("windows" in line for line in state.logs))

    def test_paused_playback_is_never_nudged(self):
        events = [(1, None), (2, None), (2, "pause")] + [(2, None)] * 40 + [(2, "resume"), (3, "stop")]
        state = self.run_player(FakeClient(), events, start=0)
        self.assertEqual(state.seeks, [])


class StallRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.recovery = StallRecovery(clock=lambda: self.now)

    def step(self, position, seconds=0.25, **kwargs):
        self.now += seconds
        return self.recovery.observe(position, **kwargs)

    def test_advancing_playback_is_left_alone(self):
        position = 0.0
        for _ in range(200):
            position += 0.25
            self.assertIsNone(self.step(position))

    def test_stuck_position_is_nudged_after_three_seconds(self):
        self.assertIsNone(self.step(10))
        results = [self.step(10) for _ in range(12)]
        self.assertEqual(results[:-1], [None] * 11)
        self.assertEqual(results[-1], 13.0)

    def test_nudges_wait_to_settle_and_stop_after_three(self):
        self.step(10)
        targets = [target for target in (self.step(10) for _ in range(400)) if target is not None]
        self.assertEqual(targets, [13.0, 13.0, 13.0])
        # Settle time keeps nudges at least five seconds apart.
        self.assertEqual(self.recovery.nudges, 3)

    def test_pause_and_buffering_reset_the_stall_clock(self):
        self.step(10)
        for _ in range(40):
            self.assertIsNone(self.step(10, paused=True))
        for _ in range(40):
            self.assertIsNone(self.step(10, caching=True))
        self.assertEqual(self.recovery.nudges, 0)

    def test_healthy_playback_restores_the_nudge_allowance(self):
        self.step(10)
        for _ in range(400):
            self.step(10)
        self.assertEqual(self.recovery.nudges, 3)
        position = 10.0
        for _ in range(140):  # 35 s of normal playback after the last nudge
            position += 0.25
            self.step(position)
        self.assertEqual(self.recovery.nudges, 0)

    def test_never_nudges_into_the_last_seconds(self):
        self.step(95)
        results = [self.step(95, total=100) for _ in range(40)]
        self.assertEqual([r for r in results if r is not None], [])


class SubtitleHelperTests(unittest.TestCase):
    def test_embedded_index_counts_only_embedded_entries_in_combined_order(self):
        subtitle = {"mode": "render", "track_id": "t5", "embedded": {"stream_index": 9},
                    "inventory": [{"source": "embedded", "combined_index": 5, "track_id": "t5"},
                                  {"source": "external", "combined_index": 0, "track_id": "t0"},
                                  {"source": "embedded", "combined_index": 2, "track_id": "t2"}]}
        self.assertEqual(embedded_subtitle_index(subtitle), 1)

    def test_embedded_index_is_none_unless_rendering_a_known_embedded_track(self):
        base = {"mode": "render", "track_id": "t1", "embedded": {"stream_index": 3},
                "inventory": [{"source": "embedded", "combined_index": 1, "track_id": "t1"}]}
        self.assertEqual(embedded_subtitle_index(base), 0)
        for change in ({"mode": "off"}, {"mode": "burn_in"}, {"embedded": None}, {"track_id": ""},
                       {"track_id": "missing"}, {"inventory": None}):
            with self.subTest(change=change):
                self.assertIsNone(embedded_subtitle_index(dict(base, **change)))

    def test_subtitle_extension_prefers_format_then_mime_then_url(self):
        self.assertEqual(subtitle_extension({"format": "webvtt"}), "vtt")
        self.assertEqual(subtitle_extension({"format": "ASS"}), "ass")
        self.assertEqual(subtitle_extension({"mime_type": "text/vtt"}), "vtt")
        self.assertEqual(subtitle_extension({"url": "/s/3.ssa?file_id=x"}), "ssa")
        self.assertEqual(subtitle_extension({"url": "/s/3?file_id=x"}), "srt")
        self.assertEqual(subtitle_extension({"format": "../../evil"}), "srt")


class ProgressReporterTests(unittest.TestCase):
    def test_close_does_not_wait_for_blocked_http_and_final_sample_is_coalesced(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        entered, release = threading.Event(), threading.Event()
        request = client.request
        report_threads = []
        caller_thread = threading.get_ident()

        def blocked(method, path, body):
            report_threads.append(threading.get_ident())
            if path.endswith("/progress"):
                entered.set()
                if not release.wait(2):
                    raise AssertionError("Test did not release the simulated network")
            return request(method, path, body)

        client.request = blocked
        worker = ProgressReporter(session)
        try:
            worker.submit(1)
            self.assertTrue(entered.wait(1))
            for position in range(2, 1001):
                worker.submit(position)
            started = time.perf_counter()
            worker.close()
            self.assertLess(time.perf_counter() - started, 0.05)
            self.assertFalse(worker.wait(0.01))
            release.set()
            self.assertTrue(worker.wait(1))
            reports = [body for _, path, body in client.calls if path.endswith("/progress")]
            self.assertEqual(len(reports), 1)
            self.assertEqual(client.calls[-1][0], "DELETE")
            self.assertEqual(client.calls[-1][2]["position"], 1000)
            self.assertTrue(all(identity != caller_thread for identity in report_threads))
        finally:
            release.set()
            worker.close()
            worker.wait(1)

    def test_pause_changes_report_immediately_and_in_order_before_regular_heartbeat(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        condition = threading.Condition()
        states = []
        request = client.request

        def observed(method, path, body):
            if path.endswith("/progress"):
                with condition:
                    states.append(body["is_paused"])
                    condition.notify_all()
            return request(method, path, body)

        client.request = observed
        worker = ProgressReporter(session, interval=10)
        try:
            for count, paused in enumerate((False, True, False), 1):
                worker.submit(12.5, paused)
                with condition:
                    self.assertTrue(condition.wait_for(lambda: len(states) >= count, timeout=1))
            self.assertEqual(states, [False, True, False])
        finally:
            worker.close()
            self.assertTrue(worker.wait(1))

    def test_server_first_stop_replay_accepts_the_winning_stop_identity(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        request = client.request

        def stopped_elsewhere(method, path, body):
            if method == "DELETE":
                return {"outcome": "replayed", "stop_id": "different-server-winning-stop"}
            return request(method, path, body)

        client.request = stopped_elsewhere
        session.stop()
        self.assertIsNone(session.session_id)

    def test_stalled_polling_owner_stops_silo_and_keeps_the_fatal_notice_after_receipt(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        now, accepted = [0.0], threading.Event()
        request = client.request

        def observed(method, path, body):
            result = request(method, path, body)
            if path.endswith("/progress"):
                accepted.set()
            return result

        client.request = observed
        worker = ProgressReporter(session, clock=lambda: now[0])
        try:
            worker.submit(12)
            self.assertTrue(accepted.wait(1))
            now[0] = 5.1
            self.assertTrue(worker.wait(1))
            _, notice, fatal, _ = worker.status()
            self.assertEqual(notice, "controller_stalled")
            self.assertTrue(fatal)
            self.assertEqual(client.calls[-1][0], "DELETE")
            self.assertEqual(client.calls[-1][2]["position"], 12)
            self.assertEqual(len([path for _, path, _ in client.calls if path.endswith("/progress")]), 1)
        finally:
            worker.close()
            worker.wait(1)

    def test_lost_controller_during_startup_stops_without_manufacturing_progress(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        now = [0.0]
        worker = ProgressReporter(session, clock=lambda: now[0])
        try:
            now[0] = 31
            self.assertTrue(worker.wait(1))
            self.assertTrue(worker.status()[2])
            self.assertEqual(client.calls[-1][0], "DELETE")
            self.assertNotIn("position", client.calls[-1][2])
        finally:
            worker.close()
            worker.wait(1)

    def test_fresh_paused_samples_keep_the_session_alive_at_an_unchanged_position(self):
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        now, accepted = [0.0], threading.Event()
        request = client.request

        def observed(method, path, body):
            result = request(method, path, body)
            if path.endswith("/progress"):
                accepted.set()
            return result

        client.request = observed
        worker = ProgressReporter(session, clock=lambda: now[0])
        try:
            worker.submit(12, paused=True)
            self.assertTrue(accepted.wait(1))
            for _ in range(10):
                now[0] += 4
                worker.submit(12, paused=True)
                time.sleep(0.001)
            self.assertFalse(worker.wait(0.01))
            self.assertFalse(worker.status()[2])
        finally:
            worker.close()
            self.assertTrue(worker.wait(1))
        self.assertTrue(client.calls[-1][2]["is_paused"])
        self.assertEqual(client.calls[-1][2]["position"], 12)


class ReporterHoldTests(unittest.TestCase):
    def test_a_deliberate_block_is_not_a_stalled_owner(self):
        now = [0.0]
        client = FakeClient()
        session = PlaybackSession(client)
        session.start("file-a")
        reporter = ProgressReporter(session, interval=10, clock=lambda: now[0], owner_timeout=5, startup_timeout=30)
        reporter.submit(1, False)
        reporter.hold()
        now[0] = 120.0
        time.sleep(0.4)
        self.assertFalse(reporter.status()[2])
        reporter.release()
        now[0] = 140.0  # Still inside the restarted startup window.
        time.sleep(0.4)
        self.assertFalse(reporter.status()[2])
        now[0] = 151.0
        self.assertTrue(reporter.wait(2))
        self.assertEqual(reporter.status()[1], "controller_stalled")

    def test_samples_carry_their_own_timeline_offset(self):
        client = FakeClient(hls_decision("server_remux_hls", source_start=120, origin=118))
        session = PlaybackSession(client)
        session.start("42", 120)
        session.progress(5, False, 300)
        self.assertEqual(client.calls[-1][2]["position"], 305)
        session.progress(5, False)
        self.assertEqual(client.calls[-1][2]["position"], 123)

class KodiMediaUrlTests(unittest.TestCase):
    def test_invalid_ports_and_parser_controls_are_reported_as_playback_errors(self):
        for value in ("https://silo.invalid:bad/file", "https://silo.invalid:99999/file",
                      "https://[invalid/file", "https://silo.invalid/fi\r\nle",
                      "https://silo.invalid/fi\tle", "https://silo.invalid/fi\\le"):
            with self.subTest(url=value), self.assertRaises(PlaybackError):
                kodi_media_url("https://silo.invalid", {"url": value})

    def test_only_media_the_server_serves_itself_goes_through_the_relay(self):
        relayed = []
        relay = lambda url, headers: relayed.append(url) or "http://127.0.0.1:9/r/x"
        for target in ("/api/v2/stream/s1?st=a", "https://silo.invalid/api/v2/playback/transcode/s1/master.m3u8"):
            with self.subTest(target=target):
                self.assertEqual(kodi_media_url("https://silo.invalid", {"url": target}, relay=relay),
                                 "http://127.0.0.1:9/r/x")
        for target in ("https://node.invalid/stream/transcode/signed/master.m3u8", "/media/file?signature=x",
                       "http://silo.invalid/api/v2/stream/s1", "https://silo.invalid:8443/api/v2/stream/s1"):
            with self.subTest(target=target):
                self.assertNotIn("127.0.0.1", kodi_media_url("https://silo.invalid", {"url": target}, relay=relay))
        self.assertEqual(len(relayed), 2)
        # A reverse-proxy prefix is part of the server address.
        self.assertEqual(kodi_media_url("https://silo.invalid/silo", {"url": "/silo/api/v2/stream/s1"}, relay=relay),
                         "http://127.0.0.1:9/r/x")
        self.assertNotIn("127.0.0.1", kodi_media_url("https://silo.invalid/silo", {"url": "/api/v2/stream/s1"},
                                                     relay=relay))

    def test_signed_relative_url_does_not_gain_account_credentials(self):
        stream = {"url": "/media/file?signature=route-token", "headers": {}, "header_refresh": "none"}
        self.assertEqual(kodi_media_url("https://silo.invalid", stream), "https://silo.invalid/media/file?signature=route-token")

    def test_server_issued_same_origin_headers_are_escaped_once(self):
        stream = {"url": "/media/file", "headers": {"Authorization": "Bearer route/token+?", "X-Route": "a&b=c"}}
        url, headers = kodi_media_url("https://silo.invalid", stream).split("|", 1)
        self.assertEqual(url, "https://silo.invalid/media/file")
        self.assertEqual(parse_qs(headers), {key: [value] for key, value in stream["headers"].items()})

    def test_header_credentials_never_go_to_another_origin(self):
        for target in ("https://other.invalid/file", "http://silo.invalid/file", "https://silo.invalid:444/file", "//other.invalid/file"):
            with self.subTest(target=target), self.assertRaises(PlaybackError):
                kodi_media_url("https://silo.invalid", {"url": target, "headers": {"Authorization": "Bearer route-token"}})
        self.assertEqual(kodi_media_url("https://silo.invalid", {"url": "https://cdn.invalid/file?sig=opaque"}),
                         "https://cdn.invalid/file?sig=opaque")

    def test_url_and_header_injection_and_header_refresh_routes_are_rejected(self):
        bad_streams = (
            {"url": "file:///etc/passwd"},
            {"url": "https://user:password@silo.invalid/file"},
            {"url": "/file#fragment"},
            {"url": "/file|Authorization=account-token"},
            {"url": ""},
            {"url": "/file", "headers": {"X-Route\r\nInjected": "value"}},
            {"url": "/file", "headers": {"X-Route": "value\r\nInjected: yes"}},
            {"url": "/file", "header_refresh": "session"},
        )
        for stream in bad_streams:
            with self.subTest(stream=stream), self.assertRaises(PlaybackError):
                kodi_media_url("https://silo.invalid", stream)


REPLAN_REQUIRED = ("protocol_version", "playback_attempt_id", "replan_request_id", "failed_plan_id",
                   "plan_attempt_id", "plan_attempt_key", "attempt_count", "client_capabilities",
                   "client_playback_context", "installation_id")


def assert_replan_body(case, body, operation):
    """The server's ReplanRequestV3 rules (replan-request.schema.json) plus v2's installation_id."""
    for key in REPLAN_REQUIRED:
        case.assertIn(key, body)
    case.assertEqual(body["protocol_version"], 3)
    case.assertEqual(body["operation"], operation)
    case.assertRegex(body["replan_request_id"], r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
    case.assertTrue(body["plan_attempt_key"].startswith("v3:"))
    case.assertTrue(1 <= body["attempt_count"] <= 8)
    case.assertGreaterEqual(len(body["plan_attempt_id"]), 8)
    if operation in ("failure_recovery", "seek_failure_recovery"):
        case.assertTrue(body["failure"]["classification"])
    else:
        case.assertNotIn("failure", body)
    if operation == "quality_change":
        case.assertTrue(body["quality_preference"].strip())
    assert_contract(case, body["client_capabilities"], "ClientCodecCapabilitiesV3")
    assert_contract(case, body["client_playback_context"], "ClientPlaybackContextV3")


def replans(client):
    return [body for _, path, body in client.calls if path.endswith("/replan")]


class ConvertedSessionTests(unittest.TestCase):
    def test_converted_hls_plan_is_accepted_with_honest_start_body(self):
        client = FakeClient(hls_decision())
        session = PlaybackSession(client, options={"max_bitrate": 8000, "quality_preference": "1080p"})
        plan = session.start("42", 0)
        self.assertEqual(plan["delivery"], "server_transcode_hls")
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        assert_contract(self, body, "PlaybackStartBody")
        self.assertEqual(body["bandwidth_cap_kbps"], 8000)
        self.assertEqual(body["quality_preference"], "1080p")
        hls = body["client_playback_context"]["deliveries"]["hls"]
        self.assertTrue(hls["enabled"] and hls["supported_on_device"])
        self.assertFalse(body["client_playback_context"]["deliveries"]["progressive"]["enabled"])
        self.assertTrue(session.can_replan())

    def test_no_bitrate_limit_sends_no_cap_and_defaults_to_auto_quality(self):
        for value in (0, None, True, -5, "8000"):
            with self.subTest(value=value):
                client = FakeClient()
                PlaybackSession(client, options={"max_bitrate": value}).start("f")
                body = client.calls[-1][2]
                self.assertNotIn("bandwidth_cap_kbps", body)
                self.assertEqual(body["quality_preference"], "auto")

    def test_progressive_remux_and_unknown_deliveries_are_refused(self):
        for delivery in ("server_remux_progressive", "hls", ""):
            with self.subTest(delivery=delivery):
                decision = hls_decision()
                decision["playback_plan"]["delivery"] = delivery
                with self.assertRaises(PlaybackError):
                    PlaybackSession(FakeClient(decision)).start("42")

    def test_server_offering_only_hls_is_still_usable(self):
        client = FakeClient(hls_decision())
        client.capabilities["deliveries"] = ["server_transcode_hls"]
        PlaybackSession(client).start("42")

    def test_growing_remux_timeline_reports_source_positions(self):
        client = FakeClient(hls_decision("server_remux_hls", source_start=120, origin=118.5))
        session = PlaybackSession(client)
        plan = session.start("42", 120)
        self.assertTrue(server_anchored(plan))
        self.assertEqual(plan["timeline"]["player_start_seconds"], 1.5)
        session.progress(10)
        self.assertEqual(client.calls[-1][2]["position"], 128.5)
        self.assertEqual(plan_duration(plan), 3000 - 118.5)
        self.assertFalse(server_anchored(hls_decision()["playback_plan"]))
        self.assertIsNone(plan_duration(playable_decision()["playback_plan"]))
        self.assertEqual(effective_file_id(plan, "requested"), "42")
        self.assertEqual(effective_file_id({}, "requested"), "requested")

    def test_quality_change_is_an_intent_replan_with_the_menu_label(self):
        client = FakeClient(hls_decision())
        session = PlaybackSession(client, options={"max_bitrate": 20000})
        session.start("42", 0)
        start = next(body for _, path, body in client.calls if path == "/playback/start")
        client.replans = [hls_decision(plan_id="plan:transcode-0002", key="v3:transcode0002")]
        plan = session.replan("quality_change", position=61.5, quality="1080p-medium")
        body = replans(client)[0]
        assert_replan_body(self, body, "quality_change")
        self.assertEqual(client.calls[-1][1], "/playback/" + SESSION + "/replan")
        self.assertEqual(body["quality_preference"], "1080p-medium")
        self.assertEqual(body["position_seconds"], 61.5)
        self.assertEqual(body["failed_plan_id"], "plan:transcode-0001")
        self.assertEqual(body["plan_attempt_key"], "v3:transcode0001")
        self.assertEqual(body["playback_attempt_id"], start["playback_attempt_id"])
        self.assertEqual(body["installation_id"], start["installation_id"])
        self.assertEqual(body["bandwidth_cap_kbps"], 20000)
        self.assertEqual(body["attempt_count"], 1)
        self.assertNotIn("attempted_plan_keys", body)
        self.assertNotIn("selected_tracks", body)
        self.assertEqual(plan["plan_id"], "plan:transcode-0002")
        self.assertEqual(session.quality_preference, "1080p-medium")
        # The next replan addresses the plan the server now holds.
        client.replans = [hls_decision(plan_id="plan:transcode-0003", key="v3:transcode0003")]
        session.replan("quality_change", position=70, quality="auto")
        second = replans(client)[1]
        self.assertEqual(second["failed_plan_id"], "plan:transcode-0002")
        self.assertNotEqual(second["plan_attempt_id"], body["plan_attempt_id"])

    def test_track_change_names_the_audio_index_and_keeps_the_current_subtitle(self):
        decision = hls_decision()
        decision["playback_plan"]["subtitle"] = {"mode": "render", "track_id": "file:42:subtitle:0",
                                                 "inventory": [{"track_id": "file:42:subtitle:0"}]}
        client = FakeClient(decision)
        session = PlaybackSession(client)
        session.start("42", 0)
        client.replans = [hls_decision(plan_id="plan:transcode-0002", key="v3:transcode0002")]
        session.replan("track_change", position=12, audio_index=2)
        body = replans(client)[0]
        assert_replan_body(self, body, "track_change")
        self.assertEqual(body["selected_tracks"], {"audio": {"index": 2}, "subtitle": {"id": "file:42:subtitle:0"}})
        self.assertEqual(body["quality_preference"], "auto")

    def test_track_change_without_subtitles_keeps_them_off(self):
        client = FakeClient(hls_decision())
        session = PlaybackSession(client)
        session.start("42", 0)
        client.replans = [hls_decision(plan_id="plan:transcode-0002", key="v3:transcode0002")]
        session.replan("track_change", position=12, audio_index=0)
        self.assertEqual(replans(client)[0]["selected_tracks"], {"audio": {"index": 0}})

    def test_terminal_replan_keeps_the_playing_plan(self):
        client = FakeClient(hls_decision())
        session = PlaybackSession(client)
        session.start("42", 0)
        client.replans = [{"protocol_version": 3, "outcome": "adaptation_unavailable",
                           "terminal": {"reason": "conversion_tool_unavailable", "message": "No encoder is free.",
                                        "retryable": True}}]
        with self.assertRaisesRegex(PlaybackError, "No encoder is free"):
            session.replan("quality_change", position=5, quality="720p-low")
        self.assertEqual(session.plan["plan_id"], "plan:transcode-0001")
        self.assertEqual(session.quality_preference, "auto")

    def test_failure_recovery_sends_the_loop_guard_and_refuses_a_repeat(self):
        client = FakeClient(hls_decision())
        session = PlaybackSession(client)
        session.start("42", 0)
        client.replans = [hls_decision(plan_id="plan:transcode-0002", key="v3:transcode0002")]
        session.replan("failure_recovery", position=3, classification="player_open_failed")
        body = replans(client)[0]
        assert_replan_body(self, body, "failure_recovery")
        self.assertEqual(body["attempted_plan_keys"], ["v3:transcode0001"])
        self.assertEqual(body["failure"], {"classification": "player_open_failed"})
        client.replans = [hls_decision(plan_id="plan:transcode-0001", key="v3:transcode0001")]
        with self.assertRaisesRegex(PlaybackError, "already failed"):
            session.replan("failure_recovery", position=3, classification="player_open_failed")
        second = replans(client)[1]
        self.assertEqual(second["attempted_plan_keys"], ["v3:transcode0001", "v3:transcode0002"])
        self.assertEqual(second["attempt_count"], 2)

    def test_seek_reanchor_is_not_a_failure(self):
        client = FakeClient(hls_decision("server_remux_hls"))
        session = PlaybackSession(client)
        session.start("42", 0)
        client.replans = [hls_decision("server_remux_hls", source_start=900, origin=897)]
        plan = session.replan("seek_reanchor", position=900)
        body = replans(client)[0]
        assert_replan_body(self, body, "seek_reanchor")
        self.assertNotIn("attempted_plan_keys", body)
        self.assertEqual(body["position_seconds"], 900)
        self.assertEqual(plan["timeline"]["timeline_offset_seconds"], 897)

    def test_plans_without_identity_cannot_be_changed(self):
        session = PlaybackSession(FakeClient())
        session.start("f")
        self.assertFalse(session.can_replan())
        with self.assertRaises(PlaybackError):
            session.replan("quality_change", quality="720p-low")

    def test_resume_rewind_backs_up_but_never_below_zero(self):
        self.assertEqual(rewound_start(180, 7), 173)
        self.assertEqual(rewound_start(3, 7), 0)
        self.assertEqual(rewound_start(180, 0), 180)
        self.assertEqual(rewound_start(0, 7), 0)
        self.assertIsNone(rewound_start(None, 7))
        self.assertEqual(rewound_start(180, True), 180)


class ScriptedExtras:
    """Stands in for PlaybackExtras: returns one scripted command per tick."""

    def __init__(self, commands=()):
        self.commands = list(commands)
        self.plans, self.interactions, self.closed = [], 0, False

    def tick(self, position, paused=False, total=None):
        return self.commands.pop(0) if self.commands else None

    def update_plan(self, plan, quality=None):
        self.plans.append((plan, quality))

    def interaction(self):
        self.interactions += 1

    def close(self):
        self.closed = True


def steady(start, count, step=0.25):
    """Natural playback samples after a switch, past the stale-callback window."""
    return [(start + step * index, None) for index in range(1, count + 1)]


class ConvertedControllerTests(KodiHarness):
    def tearDown(self):
        PlaybackController.streak.reset()
        for name in ("fake_extras", "configure", "conditions", "fail_opens", "exact_start"):
            self.__dict__.pop(name, None)

    def test_transcode_plays_natively_without_selecting_audio_streams(self):
        self.audio_streams = ["aac stereo"]
        try:
            client = FakeClient(hls_decision())
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.audio_streams
        self.assertEqual(state.dialogs, [])
        # The server converts it itself, so Kodi plays it through the loopback relay.
        self.assertEqual(state.plays, ["http://127.0.0.1:9/relay/master.m3u8?st=opaque"])
        self.assertEqual(FakeRelay.last.routes,
                         [("https://silo.invalid/api/v2/playback/transcode/%s/master.m3u8?st=opaque" % SESSION, {})])
        self.assertTrue(FakeRelay.last.closed)
        self.assertEqual(state.listitem_props["<mime>"], "application/vnd.apple.mpegurl")
        self.assertNotIn("inputstream", state.listitem_props)
        self.assertEqual(state.audio_selected, [])
        self.assertEqual(client.calls[-1][0], "DELETE")

    def test_inputstream_adaptive_is_used_when_installed_and_enabled(self):
        self.conditions = {"System.HasAddon(inputstream.adaptive)", "System.AddonIsEnabled(inputstream.adaptive)"}
        state = self.run_player(FakeClient(hls_decision()), [(1, None), (2, "stop")], start=0)
        self.assertEqual(state.listitem_props["inputstream"], "inputstream.adaptive")
        self.assertEqual(state.listitem_props["inputstream.adaptive.play_timeshift_buffer"], "true")
        self.assertNotIn("|", state.plays[0])

    def test_direct_play_never_uses_inputstream_adaptive(self):
        self.conditions = {"System.HasAddon(inputstream.adaptive)", "System.AddonIsEnabled(inputstream.adaptive)"}
        state = self.run_player(FakeClient(), [(1, None), (2, "stop")], start=0)
        self.assertNotIn("inputstream", state.listitem_props)

    def test_adaptive_open_failure_falls_back_to_kodis_own_hls_player(self):
        self.conditions = {"System.HasAddon(inputstream.adaptive)", "System.AddonIsEnabled(inputstream.adaptive)"}
        self.fail_opens = 1
        client = FakeClient(hls_decision())
        state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        self.assertEqual(len(state.plays), 2)
        self.assertEqual(replans(client), [])
        self.assertTrue(any("adaptive_open_failed" in line for line in state.logs))
        self.assertEqual(state.dialogs, [])

    def test_open_failure_asks_the_server_for_another_route(self):
        self.fail_opens = 1
        decision = hls_decision()
        decision["playback_plan"]["delivery"] = "original_http"
        decision["playback_plan"]["stream"] = {"url": "/api/v2/stream/%s?st=opaque" % SESSION,
                                               "protocol": "http_progressive", "headers": {},
                                               "header_refresh": "none"}
        client = FakeClient(decision)
        client.replans = [hls_decision(plan_id="plan:transcode-0002", key="v3:transcode0002")]
        state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        body = replans(client)[0]
        assert_replan_body(self, body, "failure_recovery")
        self.assertEqual(body["attempted_plan_keys"], ["v3:transcode0001"])
        self.assertEqual(len(state.plays), 2)
        self.assertIn("master.m3u8", state.plays[1])
        self.assertEqual(state.dialogs, [])

    def test_open_failure_without_plan_identity_keeps_the_original_error(self):
        self.fail_opens = 1
        state = self.run_player(FakeClient(), [(1, None)], start=0)
        self.assertIn("could not open the original media file", state.dialogs[0][1])

    def test_quality_choice_replans_and_switches_at_the_current_position(self):
        self.fake_extras = ScriptedExtras([{"replan": "quality", "quality": "1080p-medium"}])
        client = FakeClient(hls_decision())
        client.replans = [hls_decision(source_start=10, plan_id="plan:transcode-0002", key="v3:transcode0002")]
        events = [(10, None)] + steady(10, 14) + [(14, "stop")]
        state = self.run_player(client, events, start=0)
        body = replans(client)[0]
        assert_replan_body(self, body, "quality_change")
        self.assertEqual(body["quality_preference"], "1080p-medium")
        self.assertEqual(body["position_seconds"], 10)
        self.assertEqual(len(state.plays), 2)
        # The new stream opens at 10 s (StartOffset); Kodi is already there, so no extra seek.
        self.assertEqual(state.listitem_props.get("StartOffset"), "10.000")
        self.assertEqual(state.seeks, [])
        self.assertEqual(self.fake_extras.plans[-1][1], "1080p-medium")
        self.assertEqual(self.fake_extras.plans[-1][0]["plan_id"], "plan:transcode-0002")
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertEqual(state.dialogs, [])

    def test_refused_quality_change_keeps_playing_and_says_why(self):
        self.fake_extras = ScriptedExtras([{"replan": "quality", "quality": "720p-low"}])
        client = FakeClient(hls_decision())
        client.replans = [{"protocol_version": 3, "outcome": "adaptation_unavailable",
                           "terminal": {"reason": "conversion_tool_unavailable", "message": "No encoder is free."}}]
        state = self.run_player(client, [(10, None), (10.25, None), (10.5, "stop")], start=0)
        self.assertEqual(len(state.plays), 1)
        self.assertEqual(state.notices, ["No encoder is free."])
        self.assertEqual(client.calls[-1][2]["position"], 10.25)

    def test_ended_session_during_a_change_stops_with_a_message(self):
        self.fake_extras = ScriptedExtras([{"replan": "quality", "quality": "720p-low"}])
        client = FakeClient(hls_decision())
        client.replans = [ApiError(404, "Gone", "playback_session_not_found")]
        state = self.run_player(client, [(10, None), (10.25, None)], start=0)
        self.assertIn("session ended", state.dialogs[0][1])

    def test_audio_track_change_on_a_converted_stream_is_a_track_change_replan(self):
        self.fake_extras = ScriptedExtras([{"replan": "track", "audio_index": 0}])
        client = FakeClient(hls_decision())
        replacement = hls_decision(source_start=20, plan_id="plan:transcode-0002", key="v3:transcode0002")
        replacement["playback_plan"]["selected_tracks"] = {"audio": {"id": "file:42:audio:0", "index": 0}}
        client.replans = [replacement]
        events = [(20, None)] + steady(20, 14) + [(24, "stop")]
        state = self.run_player(client, events, start=0)
        body = replans(client)[0]
        assert_replan_body(self, body, "track_change")
        self.assertEqual(body["selected_tracks"], {"audio": {"index": 0}})
        self.assertEqual(body["position_seconds"], 20)
        self.assertEqual(len(state.plays), 2)

    def test_seek_inside_a_growing_remux_is_reanchored_by_the_server(self):
        self.fake_extras = ScriptedExtras()
        client = FakeClient(hls_decision("server_remux_hls", plan_id="plan:remux-0001", key="v3:remux0001"))
        client.replans = [hls_decision("server_remux_hls", source_start=600, origin=596,
                                       plan_id="plan:remux-0002", key="v3:remux0002")]
        events = [(1, None), (1.25, None), (600, None)] + steady(4, 14) + [(7.5, "stop")]
        state = self.run_player(client, events, start=0)
        body = replans(client)[0]
        assert_replan_body(self, body, "seek_reanchor")
        self.assertEqual(body["position_seconds"], 600)
        self.assertEqual(state.seeks, [4])
        samples = [body["position"] for _, path, body in client.calls if path.endswith("/progress")]
        self.assertTrue(all(position < 2 or position >= 600 for position in samples))
        self.assertEqual(client.calls[-1][2]["position"], 7.5 + 596)
        self.assertEqual(state.result["position_seconds"], 7.5 + 596)
        self.assertGreaterEqual(self.fake_extras.interactions, 1)

    def test_seek_on_a_complete_timeline_stays_local(self):
        self.fake_extras = ScriptedExtras()
        client = FakeClient(hls_decision())
        state = self.run_player(client, [(1, None), (1.25, None), (600, None), (600.25, "stop")], start=0)
        self.assertEqual(replans(client), [])
        self.assertEqual(client.calls[-1][2]["position"], 600)

    def subtitle_tool(self, screen, result):
        calls = []

        def configure(controller, state):
            def open_screen(name, **kwargs):
                calls.append((name, kwargs))
                return result
            controller.open_screen = open_screen

        command = {"screen": screen}
        if screen == "subtitle_sync":
            command["subtitle"] = {"track_id": "file:42:subtitle:0", "sync_key": "external-abc", "url": "/x.vtt"}
        self.configure = configure
        self.fake_extras = ScriptedExtras([command])
        client = FakeClient(hls_decision())
        state = self.run_player(client, [(30, None), (30.25, None), (30.5, "stop")], start=0)
        return calls, state

    def test_subtitle_sync_pauses_opens_the_tool_and_loads_its_file(self):
        calls, state = self.subtitle_tool("subtitle_sync", "/profile/subtitles/silo-subtitle-synced.srt")
        name, kwargs = calls[0]
        self.assertEqual(name, "subtitle_sync")
        self.assertEqual(kwargs["file_id"], "42")
        self.assertEqual(kwargs["item"]["title"], "A title")
        self.assertEqual(kwargs["plan"]["plan_id"], "plan:transcode-0001")
        self.assertEqual(kwargs["subtitle"]["sync_key"], "external-abc")
        self.assertEqual(state.subtitles, ["/profile/subtitles/silo-subtitle-synced.srt"])
        self.assertEqual(state.visibility[-1], True)
        self.assertEqual(state.pauses, 2)  # Paused for the dialog, resumed after.
        self.assertEqual(state.controller.loaded_subtitle, "/profile/subtitles/silo-subtitle-synced.srt")
        self.assertEqual(state.dialogs, [])

    def test_translate_gets_the_source_position_and_search_none_loads_nothing(self):
        calls, state = self.subtitle_tool("subtitle_translate", None)
        self.assertEqual(calls[0][1]["position"], 30)
        self.assertNotIn("subtitle", calls[0][1])
        self.assertEqual(state.subtitles, [])
        calls, state = self.subtitle_tool("subtitle_search", None)
        self.assertEqual(set(calls[0][1]), {"item", "file_id", "plan"})

    def test_subtitle_tool_without_an_application_hook_only_notifies(self):
        self.fake_extras = ScriptedExtras([{"screen": "subtitle_search"}])
        state = self.run_player(FakeClient(hls_decision()), [(30, None), (30.25, "stop")], start=0)
        self.assertEqual(len(state.notices), 1)
        self.assertFalse(getattr(state, "pauses", 0))

    def test_resume_backs_up_by_the_rewind_setting_in_kodi_only(self):
        # The server keeps the saved position, so a failed start cannot move it back.
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(173, None), (174, "stop")], start=180,
                                options={"resume_rewind": 7, "subtitle_mode": "server"})
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertEqual(body["start_position"], 180)
        self.assertEqual(state.listitem_props.get("StartOffset"), "173.000")
        self.assertEqual(state.seeks, [173])

    def test_rewind_never_goes_before_the_start_of_the_stream(self):
        client = FakeClient(playable_decision(source_start=3, player_start=3))
        state = self.run_player(client, [(0.5, None), (1, "stop")], start=3,
                                options={"resume_rewind": 7, "subtitle_mode": "server"})
        self.assertEqual(next(body for _, path, body in client.calls if path == "/playback/start")["start_position"], 3)
        self.assertNotIn("StartOffset", state.listitem_props)
        self.assertEqual(state.seeks, [])

    def test_explicit_start_positions_are_not_rewound(self):
        self.exact_start = True
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        self.run_player(client, [(180, None), (181, "stop")], start=180,
                        options={"resume_rewind": 7, "subtitle_mode": "server"})
        body = next(body for _, path, body in client.calls if path == "/playback/start")
        self.assertEqual(body["start_position"], 180)

    def test_start_over_and_server_resume_are_not_rewound(self):
        for start in (0, None):
            with self.subTest(start=start):
                client = FakeClient()
                self.run_player(client, [(1, None), (2, "stop")], start=start,
                                options={"resume_rewind": 30, "subtitle_mode": "server"})
                body = next(body for _, path, body in client.calls if path == "/playback/start")
                self.assertEqual(body.get("start_position"), start)

    def test_unattended_up_next_keeps_the_streak_across_controllers(self):
        PlaybackController.streak.count = 2
        self.fake_extras = ScriptedExtras([{"stop": "next", "item": {"content_id": "e2"}, "auto": True}])
        state = self.run_player(FakeClient(), [(1, None), (2, None)], start=0)
        self.assertEqual(state.controller.next_action["auto"], True)
        self.assertEqual(PlaybackController.streak.count, 2)

    def test_any_other_ending_resets_the_streak(self):
        for command in ({"stop": "next", "item": {"content_id": "e2"}, "auto": False}, {"stop": "sleep"}, None):
            with self.subTest(command=command):
                PlaybackController.streak.count = 2
                self.fake_extras = ScriptedExtras([command] if command else [])
                self.run_player(FakeClient(), [(1, None), (2, "stop")], start=0)
                self.assertEqual(PlaybackController.streak.count, 0)

    def test_pause_and_resume_count_as_interaction(self):
        self.fake_extras = ScriptedExtras()
        self.run_player(FakeClient(), [(1, None), (1.25, "pause"), (1.25, "resume"), (1.5, "stop")], start=0)
        self.assertEqual(self.fake_extras.interactions, 2)


if __name__ == "__main__":
    unittest.main()
