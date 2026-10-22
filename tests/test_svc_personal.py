"""Watchlist, favorites, watched and Continue Watching calls use Silo's /api/v2 routes."""

import unittest
from unittest.mock import MagicMock

from resources.lib.silo import svc_personal


class PersonalServiceTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()

    def test_marks_use_put_post_and_delete_with_escaped_ids(self):
        svc_personal.set_watchlist(self.client, "movie:a b", True)
        self.client.request.assert_called_with("PUT", "/watchlist/movie%3Aa%20b")
        svc_personal.set_favorite(self.client, "series:1", False)
        self.client.request.assert_called_with("DELETE", "/favorites/series%3A1")
        svc_personal.set_watched(self.client, "season:1", True)
        self.client.request.assert_called_with("POST", "/watched/season%3A1")
        svc_personal.set_watched(self.client, "episode:1", False)
        self.client.request.assert_called_with("DELETE", "/watched/episode%3A1")

    def test_dismiss_only_known_surfaces(self):
        svc_personal.dismiss(self.client, "next_up", "episode:2", {"series_id": "series:1"})
        self.client.request.assert_called_with("PUT", "/home/dismissals/next_up/episode%3A2",
                                               {"series_id": "series:1"})
        svc_personal.dismiss(self.client, "continue_watching", "movie:1",
                             {"progress_updated_at": "2026-10-22T18:39:59Z"})
        self.client.request.assert_called_with("PUT", "/home/dismissals/continue_watching/movie%3A1",
                                               {"progress_updated_at": "2026-10-22T18:39:59Z"})
        with self.assertRaises(ValueError):
            svc_personal.dismiss(self.client, "next_up", "episode:2", {})
        with self.assertRaises(ValueError):
            svc_personal.dismiss(self.client, "trending", "x")
        self.assertEqual(svc_personal.dismiss_surface({"item_source": "in_progress"}), "continue_watching")
        self.assertIsNone(svc_personal.dismiss_surface({}))

    def test_lists_page_with_cursor_and_drop_bad_rows(self):
        self.client.request.return_value = {"items": [{"content_id": "m:1"}, "bad"],
                                            "page": {"has_more": True, "next_cursor": "c2"}, "total": 3}
        page = svc_personal.watchlist(self.client, cursor="c1", limit=25)
        self.client.request.assert_called_with("GET", "/watchlist", params={"limit": 25, "cursor": "c1"})
        self.assertEqual(page, {"items": [{"content_id": "m:1"}], "next_cursor": "c2", "total": 3})
        self.client.request.return_value = {"items": [], "page": {"has_more": False, "next_cursor": "stale"}}
        self.assertIsNone(svc_personal.history(self.client)["next_cursor"])

    def test_state_reads_catalog_and_detail_shapes(self):
        self.assertEqual(svc_personal.state({"user_state": {"in_watchlist": True, "played": True}}),
                         {"watchlist": True, "favorite": False, "watched": True})
        self.assertEqual(svc_personal.state({"user_data": {"is_favorite": True}}),
                         {"watchlist": False, "favorite": True, "watched": False})


if __name__ == "__main__":
    unittest.main()
