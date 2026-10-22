"""Catalog file choices and source-time progress work without Kodi installed."""

import unittest

from resources.lib.silo.media import (
    available_versions, default_version, format_time, is_multipart,
    resume_position, subtitle_choices, version_label, version_name,
)


class SubtitleChoiceTests(unittest.TestCase):
    def test_external_files_come_first_then_embedded_in_catalog_order(self):
        version = {"subtitle_tracks": [
            {"index": 2, "language": "eng", "title": "Full", "external": False},
            {"index": 3, "language": "spa", "hearing_impaired": True, "external": False},
            {"index": 4, "language": "eng", "external": True, "forced": True},
            {"index": 5, "language": "fre", "external": True},
        ]}
        self.assertEqual(subtitle_choices(version), [
            (0, "ENG (Forced, External file)"),
            (1, "FRE (External file)"),
            (2, "ENG · Full"),
            (3, "SPA (SDH)"),
        ])

    def test_missing_or_malformed_tracks_give_no_choices(self):
        for version in (None, {}, {"subtitle_tracks": None}, {"subtitle_tracks": ["x", 3]}):
            with self.subTest(version=version):
                self.assertEqual(subtitle_choices(version), [])


class CatalogPresentationTests(unittest.TestCase):
    def setUp(self):
        self.first = {"file_id": "source-a", "resolution": "2160p", "hdr": True,
                      "codec_video": "hevc", "codec_audio": "truehd", "container": "mkv",
                      "file_size": 20 * 1024 ** 3, "bitrate": 40000,
                      "edition_raw": "Director's Cut", "file_name": "Example.2160p.mkv"}
        self.last = {"file_id": "source-b", "resolution": "1080p", "hdr": False, "file_name": "Example.1080p.mkv"}
        self.item = {"versions": [self.first, self.last], "user_data": {"last_file_id": "source-b"}}

    def test_file_labels_distinguish_returned_quality_edition_and_filename(self):
        label = version_label(self.first)
        for value in ("2160p", "HDR", "HEVC", "TRUEHD", "MKV", "20.0 GiB", "40.0 Mbps", "Director's Cut"):
            self.assertIn(value, label)
        self.assertNotEqual(label, version_label(self.last))
        self.assertEqual(version_name(self.first), "Example.2160p.mkv")
        self.assertIn("SDR", version_label(self.last))

    def test_selected_opaque_file_survives_order_changes_and_last_played_changes(self):
        refreshed = {"versions": [self.last, self.first], "user_data": {"last_file_id": "source-b"}}
        self.assertIs(default_version(refreshed, "first", "source-a"), self.first)

    def test_first_and_last_played_preferences_choose_different_files(self):
        self.assertIs(default_version(self.item, "first"), self.first)
        self.assertIs(default_version(self.item, "last_played"), self.last)
        self.assertIs(default_version(self.item, "ask"), self.last)

    def test_missing_last_played_falls_back_to_existing_file(self):
        self.item["user_data"]["last_file_id"] = "removed"
        self.assertIs(default_version(self.item, "last_played"), self.first)

    def test_empty_and_duplicate_file_records_do_not_create_phantom_choices(self):
        item = {"versions": [self.first, {"title": "No file ID"}, None, dict(self.first)]}
        self.assertEqual(available_versions(item), [self.first])
        self.assertIsNone(default_version({"versions": []}))

    def test_filename_fallback_uses_basename_and_never_exposes_full_path(self):
        self.assertEqual(version_name({"file_id": "x", "file_path": "C:\\private\\Example.mkv"}), "Example.mkv")
        self.assertEqual(version_name({"file_id": "opaque:id"}), "File opaque:id")

    def test_completed_title_does_not_offer_stale_resume(self):
        item = {"user_data": {"played": True, "position_seconds": 300}}
        self.assertEqual(resume_position(item), 0)
        item["user_data"]["is_in_progress"] = True
        self.assertEqual(resume_position(item), 300)

    def test_resume_preserves_fractional_source_seconds_and_rejects_invalid_positions(self):
        self.assertEqual(resume_position({"user_data": {"position_seconds": 125.75}}), 125.75)
        for value in (float("nan"), float("inf"), -1, "invalid", True):
            self.assertEqual(resume_position({"position_seconds": value}), 0)
        self.assertEqual(resume_position({"position_seconds": 100, "duration_seconds": 100}), 0)
        self.assertEqual(format_time(3723.7), "1:02:03")

    def test_multipart_metadata_requires_server_resolved_resume(self):
        for version in ({"presentation_kind": "multipart_movie"}, {"presentation_kind": "split_episode"},
                        {"presentation_part_index": 2}, {"presentation_part_total": 3}):
            self.assertTrue(is_multipart(version))
        self.assertFalse(is_multipart(self.first))


if __name__ == "__main__":
    unittest.main()
