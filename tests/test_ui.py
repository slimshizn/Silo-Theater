"""Exercise user journeys with Kodi adapters stubbed; no server or Kodi needed."""

import importlib
from pathlib import Path
import re
import sys
import types
import unittest
from unittest.mock import MagicMock, patch
import xml.etree.ElementTree as ET


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
        self.app.browse = None

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

    def test_library_navigation_preserves_filters_and_uses_library_scope(self):
        self.app.browse = {"params": {"library_id": "old", "type": "movie", "sort": "-added_at"}}
        self.app.client.items.return_value = {"items": [], "page": {"has_more": False}, "total": 0}
        window = MagicMock()
        window.sections = [{"id": "new", "title": "Cinema", "library": {"id": "new"}}]
        self.app.show_section(window, window.sections[0])
        self.app.client.items.assert_called_once_with(library_id="new", type="movie", sort="-added_at", limit=50)
        self.assertNotIn("source", self.app.browse["params"])

    def test_catalog_cursor_is_carried_to_next_page(self):
        self.app.browse = {"heading": "Movies", "sections": [], "params": {"library_id": "movies", "type": "video"}}
        self.app.client.items.side_effect = [
            {"items": [{"content_id": "m:1"}], "page": {"has_more": True, "next_cursor": "opaque"}},
            {"items": [{"content_id": "m:2"}], "page": {"has_more": False}},
        ]
        window = MagicMock()
        self.app._browse_page(window)
        self.app.next_page(window)
        self.assertEqual(self.app.client.items.call_args.kwargs["cursor"], "opaque")
        self.assertIsNone(self.app.browse["next_cursor"])
        self.assertEqual(window.display.call_args.args[2], [{"content_id": "m:2"}])

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

    def test_configured_catalog_page_size_is_used(self):
        self.app.addon.getSetting.side_effect = lambda key: "100" if key == "page_size" else ""
        self.app.browse = {"heading": "Movies", "sections": [], "params": {"type": "movie"}}
        self.app.client.items.return_value = {"items": [], "page": {"has_more": False}}
        self.app._browse_page(MagicMock())
        self.app.client.items.assert_called_once_with(type="movie", limit=100)

    def test_session_storage_failure_is_shown_inside_startup_error_handler(self):
        self.app.store = None
        with patch.object(application_module, "SessionStore", side_effect=application_module.StorageError("Cannot read session")):
            self.app.run()
        self.app.dialog.ok.assert_called_once_with("Silo", "Cannot read session")


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.app = MagicMock()
        self.app.guard.side_effect = lambda callback: callback()
        self.app.artwork.return_value = {}
        self.app.store = Store()
        self.app.options.return_value = {"file_selection": "ask", "show_plot": True, "resume_behavior": "resume"}
        self.app.resume_seconds.side_effect = Application.resume_seconds
        self.app.is_playable.side_effect = Application.is_playable

    def test_item_selection_opens_corresponding_detail(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        first, second = {"content_id": "movie:1"}, {"content_id": "movie:2"}
        window.items = [first, second]
        window.getControl(500).getSelectedPosition.return_value = 1
        window.onClick(500)
        self.app.open_item.assert_called_once_with(second)

    def test_empty_media_selection_is_inert(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        window.onClick(500)
        self.app.open_item.assert_not_called()

    def test_back_closes_window(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        action = MagicMock()
        action.getId.return_value = 92
        window.onAction(action)
        self.assertTrue(window.closed)

    def test_profile_button_returns_action_for_application_loop(self):
        window = ui.HomeWindow("SiloHome.xml", ".")
        window.application = self.app
        window.onClick(103)
        self.assertTrue(window.closed)
        self.assertEqual(window.result, "profile")

    def test_detail_refreshes_resume_after_playback(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application = self.app
        original = movie_item(0)
        window.item = original
        updated = movie_item(75)
        self.app.play.return_value = "file-a"
        self.app.item_after_playback.return_value = updated
        window.onClick(100)
        self.app.play.assert_called_once_with(original, resume=None, file_id=None, subtitle_index=None)
        self.assertEqual(window.properties["silo.resume_label"], "Resume from 1:15")
        self.assertIs(window.item, updated)
        self.app.refresh_item.assert_not_called()
        self.app.item_after_playback.assert_called_once_with(original, "file-a")

    def test_manual_file_choice_is_visible_and_preserved_after_playback_refresh(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, movie_item(multiple=True)
        self.app.choose_file.return_value = "file-b"
        refreshed = movie_item(120, multiple=True)
        self.app.refresh_item.return_value = refreshed
        self.app.item_after_playback.return_value = refreshed
        window.onClick(103)
        self.assertEqual(window.selected_file_id, "file-b")
        self.assertIn("Example.HD.mkv", window.properties["silo.file_label"])
        self.assertIn("1080p", window.properties["silo.file_label"])
        self.app.play.return_value = "file-b"
        window.onClick(101)
        self.app.play.assert_called_once_with(refreshed, resume=True, file_id="file-b", subtitle_index=None)
        self.assertEqual(window.selected_file_id, "file-b")
        self.assertTrue(window.file_confirmed)

    def test_file_picker_cancel_leaves_selection_untouched(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, movie_item(multiple=True)
        window.selected_file_id = "file-a"
        self.app.choose_file.return_value = None
        window.onClick(103)
        self.assertEqual(window.selected_file_id, "file-a")
        self.app.refresh_item.assert_not_called()

    def test_play_start_and_resume_controls_keep_distinct_intents(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, movie_item()
        self.app.play.return_value = None
        for control_id, intent in ((100, None), (101, True), (104, False)):
            window.onClick(control_id)
            self.assertIs(self.app.play.call_args.kwargs["resume"], intent)

    def test_primary_focus_is_generic_play_even_when_resume_exists(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, movie_item()
        self.app.options.return_value["resume_behavior"] = "start"
        window.onInit()
        self.assertEqual(window.focus, 100)
        self.assertEqual(window.properties["silo.resume"], "true")

    def subtitled_item(self, multiple=True):
        item = movie_item(multiple=multiple)
        item["versions"][0]["subtitle_tracks"] = [
            {"index": 3, "language": "eng", "title": "Full", "external": False},
            {"index": 4, "language": "fre", "forced": True, "external": False},
            {"index": 5, "language": "eng", "external": True, "file_name": "Example.en.srt"},
        ]
        return item

    def test_play_uses_the_displayed_version_without_asking_again(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, movie_item(multiple=True)
        self.app.options.return_value["file_selection"] = "ask"
        window.onInit()
        self.assertEqual(window.selected_file_id, "file-a")
        self.app.play.return_value = "file-a"
        window.onClick(100)
        self.assertEqual(self.app.play.call_args.kwargs["file_id"], "file-a")
        self.app.choose_file.assert_not_called()

    def test_subtitle_button_lists_tracks_and_play_sends_the_choice(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application, window.item = self.app, self.subtitled_item()
        window.onInit()
        self.assertEqual(window.properties["silo.has_subtitles"], "true")
        self.assertEqual(window.properties["silo.subtitle_label"], "Subtitles: Off")
        offered = None

        def choose(choices, current):
            nonlocal offered
            offered = choices
            return 1  # first embedded track, after the one external file

        self.app.choose_subtitle.side_effect = choose
        window.onClick(106)
        self.assertEqual([index for index, _ in offered], [0, 1, 2])
        self.assertIn("External file", offered[0][1])
        self.assertEqual(window.subtitle_index, 1)
        self.assertEqual(window.properties["silo.subtitle_label"], "Subtitles: ENG · Full")
        self.app.play.return_value = "file-a"
        window.onClick(100)
        self.assertEqual(self.app.play.call_args.kwargs["subtitle_index"], 1)

    def test_subtitle_cancel_keeps_choice_and_new_version_resets_it(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        item = self.subtitled_item()
        window.application, window.item = self.app, item
        window.onInit()
        self.app.choose_subtitle.return_value = 2
        window.onClick(106)
        self.app.choose_subtitle.return_value = False
        window.onClick(106)
        self.assertEqual(window.subtitle_index, 2)
        self.app.choose_file.return_value = "file-b"
        self.app.refresh_item.return_value = item
        window.onClick(103)
        self.assertIsNone(window.subtitle_index)
        self.assertEqual(window.properties["silo.has_subtitles"], "")

    def test_completed_progress_hides_resume_and_spoiler_setting_keeps_cast_ratings(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        item = movie_item()
        item.update(overview="A spoiler", cast=[{"name": "Actor"}], ratings=[{"name": "IMDb", "display": "8.5"}])
        item["user_data"]["played"] = True
        window.application, window.item = self.app, item
        self.app.options.return_value["show_plot"] = False
        window.onInit()
        self.assertEqual(window.properties["silo.resume"], "")
        self.assertNotIn("A spoiler", window.properties["silo.plot"])
        self.assertIn("Actor", window.properties["silo.plot"])
        self.assertIn("IMDb 8.5", window.properties["silo.plot"])

    def test_series_continue_button_opens_the_next_episode_detail(self):
        window = ui.DetailsWindow("SiloDetails.xml", ".")
        window.application = self.app
        window.item = {"content_id": "series:1", "type": "series", "play_content_id": "episode:2"}
        window.onClick(105)
        self.app.open_item.assert_called_once_with({"content_id": "episode:2"})

    def test_remote_navigation_always_targets_visible_enabled_controls(self):
        tree = ET.parse(Path(__file__).resolve().parents[1] / "resources/skins/Default/1080i/SiloDetails.xml")
        controls = {int(node.attrib["id"]): node for node in tree.iter("control") if "id" in node.attrib}

        def condition_matches(condition, visible, enabled):
            for term in condition.split(" + "):
                match = re.fullmatch(r"(!?)Control.Is(Visible|Enabled)\((\d+)\)", term)
                self.assertIsNotNone(match, term)
                negated, kind, target = match.groups()
                value = int(target) in (visible if kind == "Visible" else enabled)
                if value == bool(negated):
                    return False
            return True

        # Movie with/without saved progress, series with/without Next Up, empty detail.
        for visible, disabled in (({100, 101, 102, 103, 104}, set()), ({100, 102, 103}, set()),
                                  ({102, 105, 500}, set()), ({102, 500}, set()),
                                  ({102}, set()), ({100, 102, 103}, {100, 103})):
            enabled = visible - disabled
            for current in enabled:
                for direction in ("onleft", "onright", "onup", "ondown"):
                    choices = controls[current].findall(direction)
                    target = next((int(node.text) for node in choices if not node.get("condition")
                                   or condition_matches(node.attrib["condition"], visible, enabled)), None)
                    self.assertIn(target, enabled, (current, direction, visible))

    def test_skin_control_ids_match_controllers(self):
        root = Path(__file__).resolve().parents[1] / "resources/skins/Default/1080i"
        for name, required in (("SiloHome.xml", {100, 101, 102, 103, 104, 105, 106, 300, 500}),
                               ("SiloDetails.xml", {100, 101, 102, 103, 104, 105, 500})):
            tree = ET.parse(root / name)
            ids = [int(control.attrib["id"]) for control in tree.iter("control") if "id" in control.attrib]
            self.assertEqual(len(ids), len(set(ids)), name)
            self.assertTrue(required.issubset(set(ids)), name)
            for direction in ("onleft", "onright", "onup", "ondown"):
                for node in tree.iter(direction):
                    self.assertIn(int(node.text), ids, name)


if __name__ == "__main__":
    unittest.main()
