"""Offline playback protocol and Kodi boundary regression tests."""

from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

from silo.playback import PlaybackController, PlaybackError, PlaybackSession, kodi_media_url, select_file
from silo.api import SiloClient
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

    def request(self, method, path, body):
        self.calls.append((method, path, deepcopy(body)))
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
            return {"outcome": "applied", "stop_id": body["stop_id"]}
        raise AssertionError("Unexpected HTTP request")


class PlaybackSessionTests(unittest.TestCase):
    def test_start_payload_has_required_v3_fields_and_preserves_opaque_ids(self):
        client = FakeClient()
        session = PlaybackSession(client, "p7_fel")
        session.start("file/0001:opaque")
        method, path, body = client.calls[0]
        self.assertEqual((method, path), ("POST", "/playback/start"))
        self.assertEqual(body["file_id"], "file/0001:opaque")
        self.assertEqual(body["profile_id"], "profile/opaque-01")
        self.assertEqual(body["installation_id"], client.store.get("installation_id"))
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
        self.assertNotEqual(client.calls[0][2]["playback_attempt_id"], client.calls[1][2]["playback_attempt_id"])

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
                body = client.calls[0][2]
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
        self.assertEqual(client.calls[-1][2], first_body)
        session.progress(16, paused=True)
        next_body = client.calls[-1][2]
        self.assertEqual(next_body["sequence"], first_body["sequence"] + 1)
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
        self.assertEqual(len(client.calls), 1)

    def test_terminal_201_decision_never_opens_the_kodi_player(self):
        terminal = {"protocol_version": 3, "server_features": [], "outcome": "terminal",
                    "terminal": {"reason": "no_route", "message": "No supported route", "retryable": False}}
        requests = []

        def transport(method, url, headers, body):
            requests.append((method, url, headers, deepcopy(body)))
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
            PlaybackController(client, addon).play({"file_id": "file-a"})
        self.assertEqual(opened, [])
        self.assertIn("No supported route", dialogs[0][1])
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0], "POST")
        self.assertTrue(requests[0][1].endswith("/api/v2/playback/start"))

    def test_source_selection_skips_unreadable_files_and_preserves_id(self):
        item = {"files": [{"file_id": "bad", "unreadable": True}, {"file_id": "0001/opaque"}]}
        self.assertEqual(select_file(item), "0001/opaque")
        with self.assertRaises(PlaybackError):
            select_file({"files": [{"file_id": "bad", "unreadable": True}]})


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
