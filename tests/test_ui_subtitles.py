"""Subtitle search, AI translation and sync flows, with Kodi stubbed."""

import importlib
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "resources/lib"))


class FakeListItem:
    def __init__(self, label="", label2=""):
        self.label, self.label2, self.properties = label, label2, {}

    def setProperty(self, key, value):
        self.properties[key] = value

    def getProperty(self, key):
        return self.properties.get(key, "")


class FakeProgress:
    created = []
    cancel_after = None  # cancel once update() was called this many times

    def __init__(self):
        self.updates, self.closed, self.heading = [], 0, None
        FakeProgress.created.append(self)

    def create(self, heading, message=""):
        self.heading = heading

    def update(self, percent, message=""):
        self.updates.append((percent, message))

    def iscanceled(self):
        return FakeProgress.cancel_after is not None and len(self.updates) >= FakeProgress.cancel_after

    def close(self):
        self.closed += 1


GUI = SimpleNamespace(ListItem=FakeListItem, DialogProgress=FakeProgress)
VFS = SimpleNamespace(translatePath=lambda path: path)
with patch.dict(sys.modules, {"xbmcgui": GUI, "xbmcvfs": VFS}):
    for name in ("silo.ui_subtitles", "silo.svc_subtitles"):
        sys.modules.pop(name, None)
    ui_subtitles = importlib.import_module("silo.ui_subtitles")
    svc = importlib.import_module("silo.svc_subtitles")

ApiError = ui_subtitles.ApiError  # The class the module under test catches.


class FakeClient:
    def __init__(self, responses, downloads=None):
        self.responses = {key: list(value) if isinstance(value, list) else [value] for key, value in responses.items()}
        self.requests, self.downloads = [], []
        self.download_data = downloads if downloads is not None else b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    def request(self, method, path, body=None, params=None):
        self.requests.append((method, path, body))
        queue = self.responses.get((method, path))
        if not queue:
            raise AssertionError("unexpected request {} {}".format(method, path))
        value = queue[0] if len(queue) == 1 else queue.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    def download(self, url, limit):
        self.downloads.append((url, limit))
        if isinstance(self.download_data, Exception):
            raise self.download_data
        return self.download_data

    def paths(self):
        return [(method, path) for method, path, _ in self.requests]


class FakeDialog:
    def __init__(self, selections=()):
        self.selections = list(selections)
        self.selects, self.oks, self.notes = [], [], []

    def select(self, heading, items, preselect=0, useDetails=False):
        self.selects.append((heading, items, useDetails))
        return self.selections.pop(0) if self.selections else -1

    def ok(self, heading, message):
        self.oks.append((heading, message))
        return True

    def notification(self, heading, message, time=0, sound=True):
        self.notes.append(message)


class FakeAddon:
    def __init__(self, folder):
        self.folder = folder

    def getAddonInfo(self, key):
        assert key == "profile"
        return self.folder


class FakeMonitor:
    def __init__(self, abort=False):
        self.abort, self.waits = abort, 0

    def waitForAbort(self, seconds):
        self.waits += 1
        return self.abort


class FakeApp:
    def __init__(self, client, folder, selections=(), profile=None):
        self.client, self.dialog = client, FakeDialog(selections)
        self.addon, self.monitor = FakeAddon(folder), FakeMonitor()
        self.profile = profile if profile is not None else {"subtitle_language": "fr", "language": "en"}
        self.errors = []

    def guard(self, callback):
        try:
            return callback()
        except ApiError as error:
            self.errors.append(error)
            return None


ITEM = {"content_id": "m1", "title": "Example Movie", "versions": [{
    "file_id": "42",
    "subtitle_tracks": [
        {"index": 3, "language": "en", "codec": "subrip", "title": "Full"},
        {"index": 4, "language": "de", "codec": "hdmv_pgs_subtitle"},
        {"language": "es", "codec": "srt", "external": True, "file_name": "example.es.srt"},
    ],
    "audio_tracks": [{"language": "en", "codec": "eac3"}, {"language": "ja", "codec": "aac", "title": "Original"}],
}]}
PLAN = {"session_id": "sess-1", "stream": {"url": "/api/v2/stream/sess-1?st=tok%2Fen"},
        "subtitle": {"inventory": [
            {"combined_index": 0, "source": "external", "codec": "srt", "language": "es", "label": "example.es.srt",
             "sync_key": "external-" + "a" * 64, "url": "/api/v2/stream/sess-1/subtitles/0.srt?file_id=42&st=tok"},
            {"combined_index": 1, "source": "embedded", "codec": "subrip", "language": "en", "label": "Full"},
        ]}}
RESULT = {"id": "abc123", "provider": "exampleprovider", "language": "fr", "release_name": "Example.Movie.FR",
          "format": "srt", "score": 8.5, "downloads": 1234, "hearing_impaired": True}
STORED = {"id": "77", "media_file_id": "42", "provider": "exampleprovider", "language": "fr", "format": "srt",
          "release_name": "Example.Movie.FR", "timing": {"offset_ms": 0, "scale": 1}}


class Base(unittest.TestCase):
    def setUp(self):
        FakeProgress.created, FakeProgress.cancel_after = [], None
        modules = patch.dict(sys.modules, {"xbmcgui": GUI, "xbmcvfs": VFS})
        modules.start()
        self.addCleanup(modules.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = os.path.join(self.temp.name, "subtitles")

    def app(self, responses, selections=(), **kwargs):
        downloads = kwargs.pop("downloads", None)
        return FakeApp(FakeClient(responses, downloads), self.temp.name, selections, **kwargs)

    def saved(self):
        return sorted(os.listdir(self.folder)) if os.path.isdir(self.folder) else []


class SearchTests(Base):
    def responses(self, extra=None):
        responses = {
            ("GET", "/subtitles/providers/status"): {"schema_version": 1, "enabled": True, "providers": ["exampleprovider"]},
            ("POST", "/subtitles/search"): {"results": [RESULT, {"id": "", "provider": "x"}], "warnings": []},
            ("POST", "/subtitles/download"): {"subtitle": STORED},
        }
        responses.update(extra or {})
        return responses

    def test_search_downloads_and_saves_through_the_session(self):
        os.makedirs(self.folder)
        Path(self.folder, "silo-subtitle-old.srt").write_text("old")
        Path(self.folder, "keep.txt").write_text("other")
        app = self.app(self.responses(), selections=[0, 0])
        path = ui_subtitles.search_subtitles(app, ITEM, "42", plan=PLAN)
        client = app.client
        self.assertEqual(client.requests[1], ("POST", "/subtitles/search", {"media_file_id": "42", "languages": ["fr"]}))
        self.assertEqual(client.requests[2], ("POST", "/subtitles/download", {
            "media_file_id": "42", "provider": "exampleprovider", "subtitle_id": "abc123", "language": "fr",
            "release_name": "Example.Movie.FR", "score": 8.5, "hearing_impaired": True}))
        self.assertEqual(client.downloads, [(
            "api/v2/stream/sess-1/subtitles/0.srt?file_id=42&downloaded_subtitle_id=77&original=1&st=tok%2Fen",
            svc.SUBTITLE_LIMIT)])
        self.assertTrue(path.endswith(".srt"))
        self.assertEqual(os.path.dirname(path), self.folder)
        self.assertEqual(Path(path).read_bytes(), client.download_data)
        self.assertEqual(self.saved(), sorted(["keep.txt", os.path.basename(path)]))
        # Language picker: profile subtitle language first, then audio language.
        heading, labels, _ = app.dialog.selects[0]
        self.assertEqual(labels[:3], ["French", "English", "Spanish"])
        self.assertEqual(len(labels), len(set(labels)))
        _, items, details = app.dialog.selects[1]
        self.assertTrue(details)
        self.assertEqual(len(items), 1)  # The result without an id is dropped.
        self.assertEqual(items[0].label, "Example.Movie.FR")
        self.assertEqual(items[0].label2, "exampleprovider · French · 1,234 downloads · Score 8.5 · SDH · SRT")
        self.assertTrue(all(progress.closed for progress in FakeProgress.created))

    def test_no_provider_hides_search(self):
        app = self.app({("GET", "/subtitles/providers/status"): {"enabled": False, "providers": []}})
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.client.paths(), [("GET", "/subtitles/providers/status")])
        self.assertIn("No subtitle providers", app.dialog.oks[0][1])
        self.assertEqual(app.dialog.selects, [])

    def test_cancel_language_picker(self):
        app = self.app(self.responses(), selections=[-1])
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42"))
        self.assertEqual(app.client.paths(), [("GET", "/subtitles/providers/status")])

    def test_empty_results_returns_to_language_picker_and_warnings_notify(self):
        responses = self.responses(extra={("POST", "/subtitles/search"): [
            {"results": [], "warnings": ["One or more subtitle providers could not complete the search."]},
            {"results": [RESULT], "warnings": []}]})
        app = self.app(responses, selections=[0, 2, -1, -1])
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertIn("No French subtitles", app.dialog.oks[0][1])
        self.assertIn("could not complete", app.dialog.notes[0])
        self.assertEqual(app.client.requests[2][2]["languages"], ["es"])
        self.assertNotIn(("POST", "/subtitles/download"), app.client.paths())

    def test_without_session_stores_on_server_only(self):
        app = self.app(self.responses(), selections=[0, 0])
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42"))
        self.assertIn(("POST", "/subtitles/download"), app.client.paths())
        self.assertEqual(app.client.downloads, [])
        self.assertIn("next time", app.dialog.notes[-1])
        self.assertEqual(self.saved(), [])

    def test_download_error_is_shown_and_never_retried(self):
        error = ApiError(502, "The provider could not be reached.", "upstream_failed")
        app = self.app(self.responses(extra={("POST", "/subtitles/download"): error}), selections=[0, 0])
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.errors, [error])
        self.assertEqual(app.client.paths().count(("POST", "/subtitles/download")), 1)

    def test_fetch_failure_keeps_server_copy_message(self):
        app = self.app(self.responses(), selections=[0, 0], downloads=ApiError(410, "gone"))
        self.assertIsNone(ui_subtitles.search_subtitles(app, ITEM, "42", session_id="sess-9"))
        self.assertEqual(app.client.downloads[0][0],
                         "api/v2/stream/sess-9/subtitles/0.srt?file_id=42&downloaded_subtitle_id=77&original=1")
        self.assertIn("could not be loaded here", app.dialog.oks[-1][1])


class TranslateTests(Base):
    def base(self, enabled=True, transcribe=False, extra=None):
        responses = {
            ("GET", "/subtitles/ai/status"): {"enabled": enabled, "transcribe_enabled": transcribe},
            ("GET", "/subtitles/42"): {"subtitles": [STORED, dict(STORED, id="78", format="ass")]},
            ("POST", "/subtitles/ai/translate"): {"job": {"id": "900", "status": "pending", "progress": 0}},
            ("GET", "/subtitles/ai/jobs/900"): [
                {"job": {"id": "900", "status": "running", "progress": 0.5, "progress_message": "Translating"}},
                {"job": {"id": "900", "status": "completed", "progress": 1, "result_subtitle_id": "78"}}],
        }
        responses.update(extra or {})
        return responses

    def test_disabled_shows_notice_only(self):
        app = self.app({("GET", "/subtitles/ai/status"): {"enabled": False, "transcribe_enabled": False}})
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.client.paths(), [("GET", "/subtitles/ai/status")])
        self.assertIn("not turned on", app.dialog.oks[0][1])

    def test_translate_from_catalog_sources_polls_and_saves(self):
        # Sources without a plan: external es (0), embedded en (1), PGS skipped,
        # stored srt "77" (3), stored ass "78" skipped as a source.
        app = self.app(self.base(), selections=[1, 0])
        path = ui_subtitles.translate_subtitles(app, ITEM, "42", session_id="sess-1", position=12.5)
        _, labels, _ = app.dialog.selects[0]
        self.assertEqual(labels, ["ES · example.es.srt (File)", "EN · Full (Embedded)",
                                  "FR · Example.Movie.FR (Downloaded)"])
        _, targets, _ = app.dialog.selects[1]
        self.assertEqual(targets[0], "French")
        self.assertNotIn("English", targets)  # The source language is not a target.
        create = [request for request in app.client.requests if request[1] == "/subtitles/ai/translate"]
        self.assertEqual(create, [("POST", "/subtitles/ai/translate", {
            "media_file_id": "42", "kind": "translate", "source_index": 1, "source_language": "en",
            "target_language": "fr", "start_position": 12.5})])
        self.assertEqual(app.client.downloads[0][0],
                         "api/v2/stream/sess-1/subtitles/0.ass?file_id=42&downloaded_subtitle_id=78")
        self.assertTrue(path.endswith(".ass"))
        progress = FakeProgress.created[0]
        self.assertEqual(progress.updates, [(0, "Working…"), (50, "Translating")])
        self.assertEqual(progress.closed, 1)

    def test_plan_inventory_is_the_source_authority(self):
        app = self.app(self.base(), selections=[0, 0])
        ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN)
        self.assertNotIn(("GET", "/subtitles/42", None), app.client.requests[:2])
        body = [request[2] for request in app.client.requests if request[1] == "/subtitles/ai/translate"][0]
        self.assertEqual((body["source_index"], body["source_language"]), (0, "es"))

    def test_cancel_calls_cancel_endpoint(self):
        responses = self.base(extra={("GET", "/subtitles/ai/jobs/900"): {"job": {"id": "900", "status": "cancelled"}},
                                 ("POST", "/subtitles/ai/jobs/900/cancel"): None})
        FakeProgress.cancel_after = 1
        app = self.app(responses, selections=[0, 0])
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertIn(("POST", "/subtitles/ai/jobs/900/cancel"), app.client.paths())
        self.assertEqual(app.client.downloads, [])
        self.assertIn("cancelled", app.dialog.notes[-1])

    def test_completion_wins_a_cancel_race(self):
        responses = self.base(extra={("GET", "/subtitles/ai/jobs/900"): {
            "job": {"id": "900", "status": "completed", "result_subtitle_id": "77"}},
            ("POST", "/subtitles/ai/jobs/900/cancel"): None})
        FakeProgress.cancel_after = 1
        app = self.app(responses, selections=[0, 0])
        path = ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN)
        self.assertTrue(path.endswith(".srt"))

    def test_failed_job_shows_safe_message(self):
        responses = self.base(extra={("GET", "/subtitles/ai/jobs/900"): {"job": {
            "id": "900", "status": "failed", "error_message": "The AI provider has no available credit."}}})
        app = self.app(responses, selections=[0, 0])
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.dialog.oks[-1][1], "The AI provider has no available credit.")

    def test_audio_quota_exhausted_stops_before_create(self):
        responses = self.base(enabled=False, transcribe=True, extra={("GET", "/subtitles/ai/quota"): {
            "limited": True, "limit": 5, "used": 5, "remaining": 0, "period": "month"}})
        app = self.app(responses)
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.dialog.oks[-1][1], "You have used all 5 audio transcriptions this month.")
        self.assertNotIn(("POST", "/subtitles/ai/translate"), app.client.paths())

    def test_audio_transcribe_translate_and_quota_rejection(self):
        responses = self.base(transcribe=True, extra={
            ("GET", "/subtitles/ai/quota"): {"limited": True, "limit": 5, "used": 1, "remaining": 4, "period": "month"},
            ("POST", "/subtitles/ai/translate"): ApiError(429, "Transcription quota reached.", "rate_limited")})
        # Mode: audio (second), audio track: Japanese (third), target: French (second).
        app = self.app(responses, selections=[1, 2, 1])
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        body = [request[2] for request in app.client.requests if request[1] == "/subtitles/ai/translate"][0]
        self.assertEqual(body, {"media_file_id": "42", "kind": "transcribe_translate", "source_index": 1,
                                "source_language": "", "target_language": "fr", "start_position": 0.0})
        self.assertEqual(app.dialog.oks[-1], ("AI subtitles", "Transcription quota reached."))
        self.assertEqual(app.errors, [])

    def test_transcribe_only_sends_empty_target(self):
        responses = self.base(enabled=False, transcribe=True, extra={
            ("GET", "/subtitles/ai/quota"): {"limited": False}})
        app = self.app(responses, selections=[0])
        ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN)
        body = [request[2] for request in app.client.requests if request[1] == "/subtitles/ai/translate"][0]
        self.assertEqual((body["kind"], body["source_index"], body["target_language"]), ("transcribe", -1, ""))

    def test_abort_stops_polling(self):
        app = self.app(self.base(), selections=[0, 0])
        app.monitor.abort = True
        self.assertIsNone(ui_subtitles.translate_subtitles(app, ITEM, "42", plan=PLAN))
        self.assertEqual(app.client.downloads, [])
        self.assertEqual(FakeProgress.created[0].closed, 1)


class SyncTests(Base):
    KEY = "external-" + "a" * 64

    def base(self, extra=None):
        path = "/subtitles/42/sync/" + self.KEY
        responses = {
            ("GET", "/subtitles/sync/status"): {"state": "available", "auto_sync": True, "external": True},
            ("POST", path): {"subtitle": {"key": self.KEY, "format": "srt", "sync": {
                "id": "5", "status": "pending", "phase": "queued"}}},
            ("GET", path): [
                {"subtitle": {"key": self.KEY, "format": "srt", "sync": {
                    "id": "5", "status": "running", "phase": "analyzing", "progress": 0.4}}},
                {"subtitle": {"key": self.KEY, "format": "srt", "sync": {
                    "id": "5", "status": "synced", "result": {"offset_ms": 1200, "scale": 1}}}}],
        }
        responses.update(extra or {})
        return responses

    def test_sync_sidecar_refetches_inventory_url(self):
        app = self.app(self.base())
        subtitle = PLAN["subtitle"]["inventory"][0]
        path = ui_subtitles.sync_subtitles(app, ITEM, "42", subtitle, plan=PLAN)
        self.assertIn(("POST", "/subtitles/42/sync/" + self.KEY), app.client.paths())
        self.assertEqual(app.client.downloads[0][0], subtitle["url"])
        self.assertTrue(path.endswith(".srt"))
        self.assertEqual(app.dialog.notes[-1], "Subtitles synced (+1.2 s)")
        self.assertEqual(FakeProgress.created[0].updates,
                         [(0, "Waiting for the server…"), (40, "Listening to the audio…")])

    def test_sync_stored_subtitle_uses_session_route(self):
        key = "stored-77"
        path = "/subtitles/42/sync/" + key
        responses = {
            ("GET", "/subtitles/sync/status"): {"state": "available", "external": False},
            ("POST", path): {"subtitle": {"key": key, "stored_subtitle_id": "77", "format": "srt",
                                          "sync": {"status": "synced", "result": {"offset_ms": -500}}}},
        }
        app = self.app(responses)
        result = ui_subtitles.sync_subtitles(app, ITEM, "42", STORED, session_id="sess-1")
        self.assertEqual(app.client.downloads[0][0],
                         "api/v2/stream/sess-1/subtitles/0.srt?file_id=42&downloaded_subtitle_id=77&original=1")
        self.assertTrue(result.endswith(".srt"))
        self.assertEqual(app.dialog.notes[-1], "Subtitles synced (-0.5 s)")

    def test_unavailable_and_unsyncable(self):
        app = self.app({("GET", "/subtitles/sync/status"): {"state": "not_configured"}})
        self.assertIsNone(ui_subtitles.sync_subtitles(app, ITEM, "42", STORED, plan=PLAN))
        self.assertIn("not available", app.dialog.oks[0][1])
        app = self.app({("GET", "/subtitles/sync/status"): {"state": "available", "external": True}})
        embedded = PLAN["subtitle"]["inventory"][1]
        self.assertIsNone(ui_subtitles.sync_subtitles(app, ITEM, "42", embedded, plan=PLAN))
        self.assertIn("can be synced", app.dialog.oks[0][1])
        app = self.app({("GET", "/subtitles/sync/status"): {"state": "available", "external": False}})
        self.assertIsNone(ui_subtitles.sync_subtitles(app, ITEM, "42", PLAN["subtitle"]["inventory"][0]))
        self.assertIn("next to the video", app.dialog.oks[0][1])
        self.assertEqual(app.client.paths(), [("GET", "/subtitles/sync/status")])

    def test_cancel_stops_waiting_without_a_server_call(self):
        FakeProgress.cancel_after = 1
        app = self.app(self.base())
        self.assertIsNone(ui_subtitles.sync_subtitles(app, ITEM, "42", PLAN["subtitle"]["inventory"][0], plan=PLAN))
        self.assertEqual([p for p in app.client.paths() if p[0] == "GET" and "sync/external" in p[1]], [])
        self.assertIn("continues on the server", app.dialog.notes[-1])
        self.assertEqual(app.client.downloads, [])

    def test_outcomes_without_new_timing(self):
        for status, failure, expected in (("already_synced", None, "already match"),
                                          ("no_match", None, "another release"),
                                          ("failed", "no_audio", "cannot read audio"),
                                          ("failed", None, "could not be synced")):
            job = {"status": status}
            if failure:
                job["failure"] = failure
            responses = self.base(extra={("POST", "/subtitles/42/sync/" + self.KEY): {"subtitle": {"sync": job}}})
            app = self.app(responses)
            self.assertIsNone(ui_subtitles.sync_subtitles(app, ITEM, "42", PLAN["subtitle"]["inventory"][0]))
            shown = " ".join(app.dialog.notes + [message for _, message in app.dialog.oks])
            self.assertIn(expected, shown)
            self.assertEqual(app.client.downloads, [])


class ServiceTests(unittest.TestCase):
    def test_sync_key_variants(self):
        self.assertEqual(svc.sync_key({"sync_key": "stored-9"}), "stored-9")
        self.assertEqual(svc.sync_key(STORED), "stored-77")
        self.assertEqual(svc.sync_key({"url": "/api/v2/stream/s/subtitles/3.vtt?file_id=42&downloaded_subtitle_id=9",
                                       "source": "downloaded"}), "stored-9")
        self.assertEqual(svc.sync_key(dict(STORED, format="sub")), "")
        self.assertEqual(svc.sync_key({"source": "embedded", "codec": "subrip"}), "")

    def test_stream_reference_and_defensive_probes(self):
        self.assertEqual(svc.stream_reference(PLAN), ("sess-1", "tok/en"))
        self.assertEqual(svc.stream_reference(None), ("", ""))
        client = FakeClient({("GET", "/subtitles/providers/status"): None,
                             ("GET", "/subtitles/ai/status"): {"enabled": "yes"},
                             ("GET", "/subtitles/sync/status"): "unexpected"})
        self.assertEqual(svc.provider_status(client), {"enabled": False, "providers": []})
        self.assertEqual(svc.ai_status(client), {"enabled": False, "transcribe_enabled": False})
        self.assertFalse(svc.sync_status(client)["available"])

    def test_ids_are_path_escaped(self):
        client = FakeClient({("GET", "/subtitles/ai/jobs/a%2Fb"): {"job": {"id": "a/b"}}})
        self.assertEqual(svc.ai_job(client, "a/b"), {"id": "a/b"})

    def test_extension_mapping(self):
        self.assertEqual([svc.saved_extension(value) for value in ("subrip", "WebVTT", "ssa", "ass", "pgs", None)],
                         ["srt", "vtt", "ass", "ass", "vtt", "vtt"])


if __name__ == "__main__":
    unittest.main()
