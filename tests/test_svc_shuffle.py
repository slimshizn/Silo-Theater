"""Shuffle play against a fake client: capability, scopes, the play loop, cleanup."""

import importlib
import unittest
from unittest.mock import MagicMock

svc_shuffle = importlib.import_module("resources.lib.silo.svc_shuffle")


def card(content_id, kind="episode"):
    return {"content_id": content_id, "type": kind, "title": "Example " + content_id}


def shuffle(current, following, shuffle_id="shuffle-1"):
    return {"id": shuffle_id, "scope": {"kind": "series", "id": "series:1", "title": "Example Show"},
            "current": card(current), "next": card(following)}


class FakeClient:
    def __init__(self, capability=None, created=None, advances=()):
        self.calls = []
        self.capability = capability if capability is not None else {
            "state": "available", "scope_kinds": ["library", "series", "season"]}
        self.created = created
        self.advances = list(advances)

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        if (method, path) == ("GET", "/shuffles/capabilities"):
            return self.capability
        if (method, path) == ("POST", "/shuffles"):
            return self.created
        if method == "POST" and path.endswith("/advance"):
            return self.advances.pop(0)
        return None

    def item(self, content_id):
        self.calls.append(("ITEM", content_id, None))
        return {"content_id": content_id, "type": "episode", "versions": [{"file_id": "file-" + content_id}]}


class ShuffleTests(unittest.TestCase):
    def application(self, client, endings):
        """``endings``: one ended flag per play; None means the viewer backed out (play returns None)."""
        app = MagicMock()
        app.client = client
        app.monitor.abortRequested.return_value = False
        app.is_playable.side_effect = lambda item: item.get("type") in ("movie", "episode")
        app.last_playback_result = None
        endings = list(endings)

        def play(item, resume=None, **kwargs):
            ended = endings.pop(0)
            if ended is None:
                return None
            app.last_playback_result = {"has_sample": True, "position_seconds": 10, "ended": ended}
            return item["versions"][0]["file_id"]

        app.play.side_effect = play
        return app

    def test_capability_and_scope(self):
        client = FakeClient()
        self.assertTrue(svc_shuffle.available(client))
        self.assertTrue(svc_shuffle.available(client, "season"))
        self.assertFalse(svc_shuffle.available(client, "user_collection"))
        self.assertFalse(svc_shuffle.available(FakeClient({"state": "not_configured", "scope_kinds": []})))
        broken = MagicMock()
        broken.request.side_effect = OSError("offline")
        self.assertFalse(svc_shuffle.available(broken))
        self.assertEqual(svc_shuffle.scope_of(series_id="series:1", library_id=3), {"kind": "series", "id": "series:1"})
        self.assertEqual(svc_shuffle.scope_of(season_id="season:1", series_id="series:1"),
                         {"kind": "season", "id": "season:1"})
        self.assertEqual(svc_shuffle.scope_of(library_id=3, type="movie"), {"kind": "library", "id": "3"})
        self.assertIsNone(svc_shuffle.scope_of(type="movie"))
        with self.assertRaises(ValueError):
            svc_shuffle.start(client, "genre", "x")

    def test_picks_play_in_turn_until_the_viewer_stops(self):
        client = FakeClient(created=shuffle("episode:1", "episode:5"),
                            advances=[shuffle("episode:5", "episode:3"), shuffle("episode:3", "episode:2")])
        app = self.application(client, [True, True, False])
        self.assertEqual(svc_shuffle.play_shuffle(app, series_id="series:1"), 3)
        self.assertEqual(client.calls[:2], [("GET", "/shuffles/capabilities", None),
                                            ("POST", "/shuffles", {"scope": {"kind": "series", "id": "series:1"}})])
        self.assertEqual([call.args[0]["content_id"] for call in app.play.call_args_list],
                         ["episode:1", "episode:5", "episode:3"])
        for call in app.play.call_args_list:
            self.assertIs(call.kwargs["resume"], False)
        advances = [call for call in client.calls if call[1].endswith("/advance")]
        self.assertEqual(advances, [("POST", "/shuffles/shuffle-1/advance", {"from_content_id": "episode:1"}),
                                    ("POST", "/shuffles/shuffle-1/advance", {"from_content_id": "episode:5"})])
        self.assertEqual(client.calls[-1], ("DELETE", "/shuffles/shuffle-1", None))

    def test_backing_out_of_the_first_pick_ends_the_shuffle(self):
        client = FakeClient(created=shuffle("episode:1", "episode:2"))
        app = self.application(client, [None])
        self.assertEqual(svc_shuffle.play_shuffle(app, season_id="season:1"), 0)
        self.assertEqual(client.calls[1][2], {"scope": {"kind": "season", "id": "season:1"}})
        self.assertFalse(any(call[1].endswith("/advance") for call in client.calls))
        self.assertEqual(client.calls[-1], ("DELETE", "/shuffles/shuffle-1", None))

    def test_a_single_item_scope_plays_once(self):
        client = FakeClient(created=shuffle("episode:1", "episode:1"))
        app = self.application(client, [True])
        self.assertEqual(svc_shuffle.play_shuffle(app, series_id="series:1"), 1)
        self.assertFalse(any(call[1].endswith("/advance") for call in client.calls))

    def test_kodi_exit_stops_between_picks(self):
        client = FakeClient(created=shuffle("episode:1", "episode:2"))
        app = self.application(client, [True])
        app.monitor.abortRequested.side_effect = [False, True]
        self.assertEqual(svc_shuffle.play_shuffle(app, series_id="series:1"), 1)
        self.assertEqual(client.calls[-1], ("DELETE", "/shuffles/shuffle-1", None))

    def test_unavailable_scope_tells_the_viewer_and_starts_nothing(self):
        client = FakeClient(capability={"state": "available", "scope_kinds": ["library"]})
        app = self.application(client, [])
        self.assertIsNone(svc_shuffle.play_shuffle(app, season_id="season:1"))
        app.dialog.ok.assert_called_once()
        self.assertEqual(client.calls, [("GET", "/shuffles/capabilities", None)])

    def test_type_only_scope_picks_a_library(self):
        client = FakeClient(created=shuffle("movie:1", "movie:1"))
        app = self.application(client, [False])
        app.video_libraries.return_value = [{"id": 3, "name": "Example Movies"}, {"id": 4, "name": "Example Kids"}]
        app.dialog.select.return_value = 1
        svc_shuffle.play_shuffle(app, type="movie")
        app.video_libraries.assert_called_once_with("movie")
        self.assertEqual(app.dialog.select.call_args.args[1], ["Example Movies", "Example Kids"])
        self.assertIn(("POST", "/shuffles", {"scope": {"kind": "library", "id": "4"}}), client.calls)
        app.video_libraries.return_value = [{"id": 3, "name": "Example Movies"}]
        app.dialog.select.reset_mock()
        client.calls.clear()
        app.play.side_effect = lambda item, **kwargs: None
        svc_shuffle.play_shuffle(app, type="movie")
        app.dialog.select.assert_not_called()
        self.assertIn(("POST", "/shuffles", {"scope": {"kind": "library", "id": "3"}}), client.calls)
        app.dialog.select.return_value = -1
        app.video_libraries.return_value = [{"id": 3}, {"id": 4}]
        client.calls.clear()
        self.assertIsNone(svc_shuffle.play_shuffle(app, type="movie"))
        self.assertEqual(client.calls, [])

    def test_end_detection_falls_back_to_cached_progress(self):
        client = FakeClient(created=shuffle("episode:1", "episode:2"), advances=[shuffle("episode:2", "episode:3")])
        app = self.application(client, [])
        del app.last_playback_result
        app.play.side_effect = lambda item, **kwargs: "file-" + item["content_id"]
        finished = {"user_data": {"played": True, "is_in_progress": False, "position_seconds": 2500}}
        stopped = {"user_data": {"played": False, "is_in_progress": True, "position_seconds": 300}}
        app.item_after_playback.side_effect = [finished, stopped]
        self.assertEqual(svc_shuffle.play_shuffle(app, series_id="series:1"), 2)
        app.item_after_playback.assert_called_with(
            {"content_id": "episode:2", "type": "episode", "versions": [{"file_id": "file-episode:2"}]},
            "file-episode:2")
        # Without a recorded sample the cached item is unchanged, which never counts as finished.
        item = {"user_data": {"played": True}}
        app.item_after_playback.side_effect = None
        app.item_after_playback.return_value = {"user_data": {"played": True}}
        self.assertFalse(svc_shuffle._ended(app, item, "f"))

    def test_server_errors_still_delete_the_shuffle(self):
        client = FakeClient(created=shuffle("episode:1", "episode:2"))
        app = self.application(client, [True])
        client.advances = []

        original = client.request

        def failing(method, path, body=None, params=None):
            if path.endswith("/advance"):
                client.calls.append((method, path, body))
                raise OSError("offline")
            return original(method, path, body)

        client.request = failing
        with self.assertRaises(OSError):
            svc_shuffle.play_shuffle(app, series_id="series:1")
        self.assertEqual(client.calls[-1][:2], ("DELETE", "/shuffles/shuffle-1"))


if __name__ == "__main__":
    unittest.main()
