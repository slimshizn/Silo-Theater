"""Exercise user journeys with Kodi adapters stubbed; no server or Kodi needed."""

import importlib
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


class FakeWindow:
    def __new__(cls, *args, **kwargs):
        instance = super().__new__(cls)
        instance.properties = {}
        instance.controls = {}
        instance.closed = False
        return instance

    def getControl(self, control_id):
        if control_id not in self.controls:
            self.controls[control_id] = MagicMock()
            self.controls[control_id].getSelectedPosition.return_value = 0
        return self.controls[control_id]

    def setProperty(self, key, value):
        self.properties[key] = value

    def setFocusId(self, control_id):
        self.focus = control_id

    def close(self):
        self.closed = True


KODI_MODULES = {
    "xbmc": types.SimpleNamespace(Monitor=MagicMock, log=MagicMock(), LOGERROR=4),
    "xbmcaddon": types.SimpleNamespace(Addon=MagicMock),
    "xbmcgui": types.SimpleNamespace(WindowXML=FakeWindow, Dialog=MagicMock, ListItem=MagicMock,
                                     DialogProgress=MagicMock, INPUT_ALPHANUM=0, ALPHANUM_HIDE_INPUT=1),
    "xbmcvfs": types.SimpleNamespace(translatePath=lambda path: path),
}
with patch.dict(sys.modules, KODI_MODULES):
    application_module = importlib.import_module("resources.lib.silo.application")
    ui = importlib.import_module("resources.lib.silo.ui")

Application = application_module.Application
ApiError = application_module.ApiError


def movie_item(position=75, multiple=False):
    versions = [{"file_id": "file-a", "resolution": "2160p", "hdr": True, "file_name": "Example.UHD.mkv"}]
    if multiple:
        versions.append({"file_id": "file-b", "resolution": "1080p", "hdr": False, "file_name": "Example.HD.mkv"})
    return {"content_id": "movie:1", "type": "movie", "title": "Example", "versions": versions,
            "user_data": {"position_seconds": position, "duration_seconds": 7200, "last_file_id": "file-a"}}


class Store(dict):
    def set(self, key, value):
        self[key] = value

    def delete(self, key):
        self.pop(key, None)


class ApplicationFlowTests(unittest.TestCase):
    def setUp(self):
        self.app = Application.__new__(Application)
        self.app.addon = MagicMock()
        self.app.addon.getSetting.return_value = "code"
        self.app.dialog = MagicMock()
        self.app.monitor = MagicMock()
        self.app.monitor.abortRequested.return_value = False
        self.app.store = Store()
        self.app.client = MagicMock()
        self.app._libraries = None
        self.app.profile_avatar = ""

    def test_pin_refusal_attempts_once_and_never_selects_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.side_effect = ApiError(403, "Incorrect PIN")
        self.assertFalse(self.app.choose_profile())
        self.app.client.verify_pin.assert_called_once_with("locked", "1234")
        self.app.dialog.input.assert_called_once()
        self.app.client.select_profile.assert_not_called()

    def test_pin_success_attaches_returned_proof_to_selected_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.return_value = {"valid": True, "profile_token": "proof"}
        self.assertTrue(self.app.choose_profile())
        self.app.client.select_profile.assert_called_once_with("locked", "proof")
        self.assertEqual(self.app.profile_name, "Adult")

    def test_invalid_pin_success_response_cannot_select_profile(self):
        self.app.client.profiles.return_value = {"items": [{"id": "locked", "name": "Adult", "has_pin": True}]}
        self.app.dialog.input.return_value = "1234"
        self.app.client.verify_pin.return_value = {"valid": False}
        self.assertFalse(self.app.choose_profile())
        self.app.client.select_profile.assert_not_called()

    def test_temporary_session_reuses_bound_profile_without_pin_or_profile_picker(self):
        self.app.store.update(temporary_session=True, bound_profile_id="bound", profile_token="proof")
        self.assertTrue(self.app.choose_profile())
        self.app.client.select_profile.assert_called_once_with("bound", "proof")
        self.app.client.profiles.assert_not_called()
        self.app.client.verify_pin.assert_not_called()
        self.app.dialog.input.assert_not_called()
        self.app.dialog.select.assert_not_called()

    def test_saved_code_method_is_used_after_forced_sign_in(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(return_value=True)
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.device_sign_in.assert_called_once()
        self.app.dialog.select.assert_not_called()
        self.app.dialog.input.assert_not_called()

    def test_back_from_code_sign_in_does_not_open_password_prompt(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(side_effect=application_module.DeviceAuthCancelled())
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertFalse(self.app.sign_in())
        self.app.dialog.input.assert_not_called()

    def test_password_only_retry_does_not_offer_unsupported_code(self):
        self.app.password_sign_in = MagicMock(side_effect=[ApiError(401, "Incorrect password"), True])
        self.app.dialog.select.return_value = 0
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["password"]
            self.assertTrue(self.app.sign_in())
        self.assertEqual(self.app.password_sign_in.call_count, 2)
        choices = self.app.dialog.select.call_args.args[1]
        self.assertFalse(any("code" in value.lower() for value in choices))

    def test_code_failure_can_fall_back_to_password(self):
        self.app.store["auth_method"] = "device"
        self.app.device_sign_in = MagicMock(side_effect=ApiError(404, "Unavailable"))
        self.app.password_sign_in = MagicMock(return_value=True)
        self.app.dialog.select.return_value = 1
        with patch.object(application_module, "AuthController") as auth:
            auth.return_value.available_methods.return_value = ["device", "password"]
            self.assertTrue(self.app.sign_in())
        self.app.password_sign_in.assert_called_once()

    def test_series_detail_loads_seasons_without_starting_playback(self):
        item = {"content_id": "series:1", "type": "series", "title": "Show"}
        season = {"content_id": "season:1", "type": "season", "title": "Season 1"}
        self.app.client.item.return_value = item
        self.app.client.children.return_value = {"items": [season]}
        with patch.object(application_module, "DetailsWindow") as window:
            self.app.open_item(item)
        self.app.client.children.assert_called_once_with(item)
        self.assertEqual(window.return_value.children, [season])
        window.return_value.doModal.assert_called_once()

    def test_resume_uses_latest_saved_source_position_and_start_passes_zero(self):
        item = movie_item()
        refreshed = movie_item(125.75)
        self.app.client.item.return_value = refreshed
        with patch.object(application_module, "PlaybackController") as player:
            self.app.play(item, resume=True)
            self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], 125.75)
            self.assertEqual(player.return_value.play.call_args.kwargs["file_id"], "file-a")
            self.assertIs(player.return_value.play.call_args.args[0], refreshed)
            self.app.play(item, resume=False)
            self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], 0)
        self.app.client.item.assert_called_with("movie:1", file_id="file-a")

    def test_multiple_files_prompt_can_select_an_alternate_opaque_id(self):
        item = movie_item(multiple=True)
        self.app.dialog.select.return_value = 1
        self.app.client.item.return_value = item
        with patch.object(application_module, "PlaybackController") as player:
            selected = self.app.play(item, resume=True)
        self.assertEqual(selected, "file-b")
        self.assertEqual(player.return_value.play.call_args.kwargs["file_id"], "file-b")
        self.app.client.item.assert_called_with("movie:1", file_id="file-b")
        self.assertTrue(self.app.dialog.select.call_args.kwargs["useDetails"])

    def test_cancel_file_picker_never_starts_or_changes_progress(self):
        self.app.dialog.select.return_value = -1
        with patch.object(application_module, "PlaybackController") as player:
            self.assertIsNone(self.app.play(movie_item(multiple=True)))
        player.assert_not_called()
        self.app.client.item.assert_not_called()

    def test_choose_subtitle_offers_off_first_and_keeps_choice_on_cancel(self):
        choices = [(0, "ENG (External file)"), (1, "ENG · Full"), (2, "FRE")]
        self.app.dialog.select.return_value = 2
        self.assertEqual(self.app.choose_subtitle(choices, current=None), 1)
        labels = self.app.dialog.select.call_args.args[1]
        self.assertEqual(labels[0], "Off")
        self.assertEqual(self.app.dialog.select.call_args.kwargs["preselect"], 0)
        self.app.dialog.select.return_value = 0
        self.assertIsNone(self.app.choose_subtitle(choices, current=1))
        self.assertEqual(self.app.dialog.select.call_args.kwargs["preselect"], 2)
        self.app.dialog.select.return_value = -1
        self.assertIs(self.app.choose_subtitle(choices, current=1), False)

    def test_chosen_subtitle_reaches_playback_options(self):
        self.app.client.item.return_value = movie_item()
        with patch.object(application_module, "PlaybackController") as player:
            self.app.play(movie_item(), resume=False, file_id="file-a", subtitle_index=3)
            self.assertEqual(player.return_value.play.call_args.kwargs["options"]["subtitle_track_index"], 3)
            self.app.play(movie_item(), resume=False, file_id="file-a")
            self.assertNotIn("subtitle_track_index", player.return_value.play.call_args.kwargs["options"])

    def test_explicit_selected_file_skips_repeat_prompt(self):
        self.app.client.item.return_value = movie_item(multiple=True)
        with patch.object(application_module, "PlaybackController") as player:
            self.assertEqual(self.app.play(movie_item(multiple=True), file_id="file-b", resume=True), "file-b")
        self.app.dialog.select.assert_not_called()
        self.assertEqual(player.return_value.play.call_args.kwargs["file_id"], "file-b")

    def test_removed_explicit_file_is_not_replaced_silently(self):
        with patch.object(application_module, "PlaybackController") as player:
            self.assertIsNone(self.app.play(movie_item(), file_id="removed", resume=True))
        player.assert_not_called()
        self.app.dialog.ok.assert_called_once()

    def test_generic_play_respects_start_preference_but_explicit_resume_overrides(self):
        self.app.addon.getSetting.side_effect = lambda key: "start" if key == "resume_behavior" else ""
        self.app.client.item.return_value = movie_item()
        with patch.object(application_module, "PlaybackController") as player:
            self.app.play(movie_item())
            self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], 0)
            self.app.play(movie_item(), resume=True)
            self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], 75)

    def test_ask_resume_policy_can_resume_start_or_cancel(self):
        self.app.addon.getSetting.side_effect = lambda key: "ask" if key == "resume_behavior" else ""
        self.app.client.item.return_value = movie_item()
        for selected, expected in ((0, 75), (1, 0), (-1, None)):
            self.app.dialog.select.return_value = selected
            with patch.object(application_module, "PlaybackController") as player:
                result = self.app.play(movie_item())
            if expected is None:
                self.assertIsNone(result)
                player.assert_not_called()
            else:
                self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], expected)

    def test_multipart_resume_defers_offset_to_server_but_start_is_explicit_zero(self):
        item = movie_item()
        item["versions"][0].update(presentation_kind="multipart_movie", presentation_part_index=2, presentation_part_total=2)
        self.app.client.item.return_value = item
        with patch.object(application_module, "PlaybackController") as player:
            self.app.play(item, resume=True)
            self.assertIsNone(player.return_value.play.call_args.kwargs["start_position"])
            self.app.play(item, resume=False)
            self.assertEqual(player.return_value.play.call_args.kwargs["start_position"], 0)

    def test_post_stop_details_use_confirmed_sample_when_catalog_becomes_unreachable(self):
        original, fresh = movie_item(30), movie_item(75)
        self.app.client.item.side_effect = [fresh, ApiError(0, "Catalog is now unreachable")]
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, original
        with patch.object(application_module, "PlaybackController") as controller:
            controller.return_value.last_result = {"has_sample": True, "position_seconds": 125.75, "ended": False}
            window.onClick(100)
        # Only the preplay detail request is allowed; the queued failure is never read.
        self.app.client.item.assert_called_once_with("movie:1", file_id="file-a")
        self.assertEqual(window.item["user_data"]["position_seconds"], 125.75)
        self.assertEqual(window.item["user_data"]["last_file_id"], "file-a")
        self.assertEqual(window.properties["silo.resume_label"], "Resume from 2:05")
        self.assertEqual(window.selected_file_id, "file-a")
        self.app.dialog.ok.assert_not_called()

    def test_unconfirmed_or_missing_controller_result_preserves_server_progress(self):
        fresh = movie_item(125.75)
        self.app.client.item.return_value = fresh
        results = (None, {}, {"has_sample": False, "position_seconds": 0, "ended": True},
                   {"has_sample": True, "position_seconds": float("nan"), "ended": True})
        for result in results:
            with self.subTest(result=result), patch.object(application_module, "PlaybackController") as controller:
                controller.return_value.last_result = result
                self.app.play(movie_item(), resume=False)
            cached = self.app.item_after_playback(movie_item(), "file-a")
            self.assertEqual(cached["user_data"], fresh["user_data"])
            self.assertEqual(self.app.resume_seconds(cached), 125.75)
        self.assertEqual(fresh["user_data"]["position_seconds"], 125.75)

    def test_confirmed_ended_result_hides_resume_and_remembers_explicit_file(self):
        fresh = movie_item(75, multiple=True)
        self.app.client.item.return_value = fresh
        with patch.object(application_module, "PlaybackController") as controller:
            controller.return_value.last_result = {"has_sample": True, "position_seconds": 7199.5, "ended": True}
            self.app.play(fresh, file_id="file-b")
        cached = self.app.item_after_playback(fresh, "file-b")
        self.assertTrue(cached["user_data"]["played"])
        self.assertFalse(cached["user_data"]["is_in_progress"])
        self.assertEqual(cached["user_data"]["last_file_id"], "file-b")
        self.assertEqual(self.app.resume_seconds(cached), 0)
        self.assertNotIn("played", fresh["user_data"])

    def test_part_local_samples_do_not_overwrite_global_item_progress(self):
        fresh = movie_item(5000)
        fresh["versions"][0].update(presentation_kind="multipart_movie", presentation_part_index=2, presentation_part_total=2)
        self.app.client.item.return_value = fresh
        with patch.object(application_module, "PlaybackController") as controller:
            controller.return_value.last_result = {"has_sample": True, "position_seconds": 120, "ended": True}
            self.app.play(fresh, resume=True)
        cached = self.app.item_after_playback(fresh, "file-a")
        self.assertEqual(cached["user_data"], fresh["user_data"])

    def test_cache_miss_and_profile_change_never_fall_back_to_http(self):
        item = movie_item()
        self.app.store["profile_id"] = "first"
        self.app._cache_playback_item(item, "file-a", {"has_sample": True, "position_seconds": 500, "ended": False})
        self.app.client.item.side_effect = AssertionError("Post-stop HTTP is forbidden")
        self.app.store["profile_id"] = "second"
        fallback = self.app.item_after_playback(item, "file-a")
        self.assertEqual(fallback, item)
        self.assertIsNot(fallback, item)
        self.app.client.item.assert_not_called()

    def test_manual_file_refresh_still_requests_fresh_metadata(self):
        item = movie_item(multiple=True)
        self.app.client.item.return_value = item
        self.assertIs(self.app.refresh_item(item, "file-b"), item)
        self.app.client.item.assert_called_once_with("movie:1", file_id="file-b")

    def test_continue_watching_card_opens_playable_episode_but_library_series_stays_series(self):
        episode = {"content_id": "episode:2", "type": "episode", "title": "Episode 2"}
        card = {"content_id": "series:1", "type": "series", "play_content_id": "episode:2", "item_source": "next_up"}
        self.app.client.item.return_value = episode
        with patch.object(application_module, "DetailsWindow"):
            self.app.open_item(card)
        self.app.client.item.assert_called_with("episode:2")
        self.app.client.children.assert_not_called()
        self.app.client.item.reset_mock()
        series = {"content_id": "series:1", "type": "series", "title": "Series", "play_content_id": "episode:2"}
        self.app.client.item.return_value = series
        self.app.client.children.return_value = {"items": []}
        with patch.object(application_module, "DetailsWindow"):
            self.app.open_item({key: value for key, value in card.items() if key != "item_source"})
        self.app.client.item.assert_called_once_with("series:1")
        self.app.client.children.assert_called_once_with(series)

    def test_home_sections_keep_server_order_and_drop_empty_or_book_rows(self):
        movie = {"content_id": "movie:1", "type": "movie"}
        self.app.client.hubs.return_value = {"sections": [
            {"id": "a", "title": "Continue Watching", "items": [movie]},
            {"id": "b", "title": "Continue Listening", "items": [{"content_id": "book:1", "type": "audiobook"}]},
            {"id": "c", "title": "Empty", "items": []},
            {"id": "d", "title": "Recently Added", "items": [movie, {"type": "movie"}]},
        ]}
        sections = self.app.home_sections()
        self.assertEqual([section["id"] for section in sections], ["a", "d"])
        self.assertEqual(sections[1]["items"], [movie])

    def test_browse_adds_page_size_cursor_and_reports_next_page(self):
        self.app.addon.getSetting.side_effect = lambda key: "100" if key == "page_size" else ""
        self.app.client.items.return_value = {"items": [{"content_id": "m:2", "type": "movie"}],
                                              "page": {"has_more": True, "next_cursor": "next"}, "total": 7}
        page = self.app.browse({"type": "movie", "sort": "title"}, cursor="opaque")
        self.app.client.items.assert_called_once_with(type="movie", sort="title", limit=100, cursor="opaque")
        self.assertEqual(page, {"items": [{"content_id": "m:2", "type": "movie"}], "next_cursor": "next", "total": 7})
        self.app.client.items.return_value = {"items": [], "page": {"has_more": False, "next_cursor": "stale"}}
        self.assertIsNone(self.app.browse({"type": "movie"})["next_cursor"])

    def test_video_libraries_by_kind_are_cached_until_profile_changes(self):
        self.app.client.libraries.return_value = {"items": [
            {"id": 1, "name": "Films", "type": "movies"}, {"id": 2, "name": "Shows", "type": "series"},
            {"id": 3, "name": "Mixed", "type": "mixed"}, {"id": 4, "name": "Books", "type": "audiobooks"}]}
        self.assertEqual([library["id"] for library in self.app.video_libraries("movie")], [1, 3])
        self.assertEqual([library["id"] for library in self.app.video_libraries("series")], [2, 3])
        self.app.client.libraries.assert_called_once()
        self.app.client.profiles.return_value = {"items": [{"id": "p", "name": "Example"}]}
        self.assertTrue(self.app.choose_profile(force=True))
        self.assertIsNone(self.app._libraries)

    def test_similar_failures_hide_the_row(self):
        self.app.client.similar.return_value = {"items": [{"content_id": "m:3", "type": "movie"}]}
        self.assertEqual(self.app.similar({"content_id": "m:1"}), [{"content_id": "m:3", "type": "movie"}])
        self.app.client.similar.side_effect = ApiError(503, "Recommendations are off")
        self.assertEqual(self.app.similar({"content_id": "m:1"}), [])

    def test_play_next_plays_the_series_next_up_episode(self):
        episode = movie_item()
        episode.update(content_id="episode:2", type="episode")
        self.app.client.item.return_value = episode
        with patch.object(application_module, "PlaybackController") as player:
            self.assertEqual(self.app.play_next({"content_id": "series:1", "play_content_id": "episode:2"}), "file-a")
        self.app.client.item.assert_any_call("episode:2")
        self.assertIs(player.return_value.play.call_args.args[0], episode)
        self.assertIsNone(self.app.play_next({"content_id": "series:1"}))

    def test_profile_choice_records_avatar_as_png(self):
        self.app.client.profiles.return_value = {"items": [
            {"id": "p", "name": "Example", "avatar_url": "https://avatars.example.invalid/9.x/robot/svg?seed=x"}]}
        self.assertTrue(self.app.choose_profile(force=True))
        self.assertEqual(self.app.profile_avatar, "https://avatars.example.invalid/9.x/robot/png?seed=x")

    def test_session_storage_failure_is_shown_inside_startup_error_handler(self):
        self.app.store = None
        with patch.object(application_module, "SessionStore", side_effect=application_module.StorageError("Cannot read session")):
            self.app.run()
        self.app.dialog.ok.assert_called_once_with("Silo", "Cannot read session")


if __name__ == "__main__":
    unittest.main()
