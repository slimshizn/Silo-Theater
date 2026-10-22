"""Offline playback protocol and Kodi boundary regression tests."""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo.playback import PlaybackController, PlaybackError, PlaybackSession, ResumeGuard, kodi_media_url, select_file
from silo.api import ApiError, SiloClient
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
                               log=lambda *args: None, LOGWARNING=2)
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
        state = SimpleNamespace(now=0.0, player=None, dialogs=[], subtitles=[], visibility=[], seeks=[])
        pending = list(events)

        class KodiPlayer:
            def __init__(self):
                state.player = self
                self.position = 0.0
                self.playing = False

            def play(self, *args):
                self.playing = True
                self.onAVStarted()

            def isPlaying(self):
                return self.playing

            def getTime(self):
                return self.position if self.playing else 0.0

            def seekTime(self, seconds):
                state.seeks.append(seconds)

            def stop(self):
                self.onPlayBackStopped()
                self.playing = False

            def showSubtitles(self, enabled):
                state.visibility.append(enabled)

            def setSubtitles(self, url):
                state.subtitles.append(url)

        class Monitor:
            def waitForAbort(self, seconds):
                state.now += seconds
                if pending:
                    position, action = pending.pop(0)
                    state.player.position = position
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

        xbmc = SimpleNamespace(Player=KodiPlayer, Monitor=Monitor, log=lambda *args: None, LOGWARNING=2)
        xbmcgui = SimpleNamespace(ListItem=ListItem, Dialog=lambda: SimpleNamespace(ok=lambda *args: state.dialogs.append(args)))
        addon = SimpleNamespace(getSetting=lambda key: "", getAddonInfo=lambda key: "0.2.0")
        with patch.dict(sys.modules, {"xbmc": xbmc, "xbmcgui": xbmcgui}), patch("silo.playback.time.monotonic", side_effect=lambda: state.now):
            PlaybackController(client, addon).play({"file_id": "default-file", "title": "A title"},
                                                  start_position=start, file_id=file_id,
                                                  options=options or {"progress_interval": 10, "subtitle_mode": "server"})
        return state

    def test_resume_waits_for_seek_before_first_sample_and_captures_final_callback(self):
        client = FakeClient(playable_decision(source_start=180, player_start=180))
        state = self.run_player(client, [(0, None), (0.2, None), (180.1, None), (180.6, "stop")], start=180)
        self.assertEqual(state.seeks, [180])
        samples = [body for _, path, body in client.calls if path.endswith("/progress")]
        self.assertEqual(samples[0]["position"], 180.1)
        self.assertTrue(all(sample["position"] >= 180 for sample in samples))
        self.assertEqual(client.calls[-1][0], "DELETE")
        self.assertEqual(client.calls[-1][2]["position"], 180.6)
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
        self.assertEqual(client.calls[-1][2]["position"], 5.7)

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
        self.assertEqual(client.calls[-1][2]["position"], 141)

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
        self.assertEqual(stops[0]["position"], 1.7)


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
