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

from silo.playback import (CAPABILITY_TERMINALS, PlaybackController, PlaybackError, PlaybackSession,
                           ProgressReporter, ResumeGuard, embedded_subtitle_index, kodi_media_url,
                           select_file, subtitle_extension)
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


def server_capabilities(identity="11111111-2222-4333-8444-555555555555"):
    return {"installation_id": identity, "state": "available", "allowed": True,
            "protocol_versions": [3], "features": ["subrip_sidecar_v1", "sequenced_progress_v1"],
            "deliveries": ["original_http"], "revision": "opaque"}


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
        for bad in (float("nan"), float("inf"), -1):
            with self.subTest(sample=bad), self.assertRaises(PlaybackError):
                session.progress(bad)
        self.assertEqual(len(client.calls), 2)

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

    def test_seek_failure_times_out_without_confirming_zero(self):
        now = [0.0]
        guard = ResumeGuard(180, clock=lambda: now[0])
        now[0] = 10
        with self.assertRaisesRegex(PlaybackError, "saved progress was kept"):
            guard.accept(0)
        self.assertFalse(guard.confirmed)

    def test_restart_zero_needs_no_seek_confirmation(self):
        self.assertTrue(ResumeGuard(0).accept(0))

    def test_source_selection_skips_unreadable_files_and_preserves_id(self):
        item = {"files": [{"file_id": "bad", "unreadable": True}, {"file_id": "0001/opaque"}]}
        self.assertEqual(select_file(item), "0001/opaque")
        with self.assertRaises(PlaybackError):
            select_file({"files": [{"file_id": "bad", "unreadable": True}]})


class ControllerTests(unittest.TestCase):
    def run_player(self, client, events, start=None, options=None, file_id=None):
        """Exercise the real controller against a Kodi callback/event double."""
        state = SimpleNamespace(now=0.0, player=None, dialogs=[], subtitles=[], visibility=[], seeks=[],
                                in_callback=False, logs=[], native_times=0,
                                subtitle_streams=getattr(self, "subtitle_streams", []), subtitle_selected=[],
                                audio_streams=getattr(self, "audio_streams", []), audio_selected=[],
                                profile_dir=getattr(self, "profile_dir", None) or tempfile.mkdtemp())
        pending = list(events)
        polling_thread = threading.get_ident()

        class KodiPlayer:
            def __init__(self):
                state.player = self
                self.position = 0.0
                self.playing = False

            def play(self, *args):
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

            def setProperty(self, *args):
                pass

            def setMimeType(self, *args):
                pass

            def setContentLookup(self, *args):
                pass

        windows = getattr(self, "platform_windows", False)
        xbmc = SimpleNamespace(Player=KodiPlayer, Monitor=Monitor,
                               log=lambda message, level: state.logs.append(message), LOGWARNING=2, LOGINFO=1,
                               getCondVisibility=lambda name: windows and name == "System.Platform.Windows")
        xbmcgui = SimpleNamespace(ListItem=ListItem, Dialog=lambda: SimpleNamespace(ok=lambda *args: state.dialogs.append(args)))
        addon = SimpleNamespace(getSetting=lambda key: "", getAddonInfo=lambda key: "0.2.1")
        xbmcvfs = SimpleNamespace(translatePath=lambda path: state.profile_dir)
        with patch.dict(sys.modules, {"xbmc": xbmc, "xbmcgui": xbmcgui, "xbmcvfs": xbmcvfs}), \
                patch("silo.playback.time.monotonic", side_effect=lambda: state.now):
            controller = PlaybackController(client, addon)
            controller.play({"file_id": "default-file", "title": "A title"},
                            start_position=start, file_id=file_id,
                            options=options or {"progress_interval": 10, "subtitle_mode": "server"})
            state.result = controller.last_result
            if controller.last_reporter:
                self.assertTrue(controller.last_reporter.wait(1), "Reporter did not finish")
        return state

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

    def test_failed_seek_times_out_with_no_zero_progress(self):
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(0, None)] * 50, start=180)
        self.assertIn("saved progress was kept", state.dialogs[0][1])
        self.assertFalse(any(path.endswith("/progress") for _, path, _ in client.calls))
        self.assertNotIn("position", client.calls[-1][2])

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

    def test_embedded_track_beyond_kodis_list_is_left_alone(self):
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
        self.assertEqual(state.visibility, [])


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

    def test_windows_makes_no_startup_track_switch_and_does_not_claim_one(self):
        self.audio_streams = ["eng", "fre"]
        self.platform_windows = True
        try:
            client = FakeClient(self.audio_decision(1))
            state = self.run_player(client, [(1, None), (2, "stop")], start=0)
        finally:
            del self.audio_streams, self.platform_windows
        self.assertEqual(state.audio_selected, [])
        self.assertNotIn("client_selected_audio_track_v1", self.start_claims(client))
        self.assertTrue(any("startup_track_switch_skipped_windows" in line for line in state.logs))
        # Playback and reporting otherwise proceed normally.
        self.assertTrue(any(path.endswith("/progress") for _, path, _ in client.calls))
        self.assertEqual(client.calls[-1][0], "DELETE")


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


class KodiMediaUrlTests(unittest.TestCase):
    def test_invalid_ports_and_parser_controls_are_reported_as_playback_errors(self):
        for value in ("https://silo.invalid:bad/file", "https://silo.invalid:99999/file",
                      "https://[invalid/file", "https://silo.invalid/fi\r\nle",
                      "https://silo.invalid/fi\tle", "https://silo.invalid/fi\\le"):
            with self.subTest(url=value), self.assertRaises(PlaybackError):
                kodi_media_url("https://silo.invalid", {"url": value})

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


if __name__ == "__main__":
    unittest.main()
