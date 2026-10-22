"""Capability promises must remain explicit and match the API contract."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from silo.capabilities import playback_capabilities
from playback_contract import assert_contract


class CapabilityTests(unittest.TestCase):
    def test_every_mode_matches_required_and_allowed_openapi_fields(self):
        for mode in ("auto", "hdr10", "dv", "p7_fel"):
            with self.subTest(mode=mode):
                codecs, context = playback_capabilities(mode)
                assert_contract(self, codecs, "ClientCodecCapabilitiesV3")
                assert_contract(self, context, "ClientPlaybackContextV3")

    def test_full_p7_is_only_promised_by_explicit_fel_mode(self):
        for mode in ("auto", "hdr10", "dv", "p7_fel"):
            with self.subTest(mode=mode):
                codecs, context = playback_capabilities(mode)
                delivery = context["deliveries"]["original_http"]
                self.assertEqual(set(context["deliveries"]), {"original_http", "hls", "progressive"})
                self.assertEqual(delivery["transformations"], [])
                # Managed dynamic range is an original-file claim only.
                self.assertEqual(context["deliveries"]["hls"]["validated_claims"], [])
                if mode == "p7_fel":
                    self.assertIn(7, codecs["hdr_details"]["dolby_vision_profiles"])
                    self.assertIn(7, delivery["hdr_details"]["dolby_vision_profiles"])
                    self.assertIn("client_managed_dynamic_range_v1", delivery["validated_claims"])
                else:
                    self.assertNotIn(7, codecs["hdr_details"]["dolby_vision_profiles"])
                    self.assertNotIn("client_managed_dynamic_range_v1", delivery["validated_claims"])

    def test_automatic_and_unknown_modes_make_no_unverified_hdr_claims(self):
        for mode in ("auto", "CoreELEC", "Shield", "Windows", ""):
            with self.subTest(mode=mode):
                codecs, context = playback_capabilities(mode)
                self.assertFalse(codecs["hdr"])
                self.assertFalse(codecs["hdr_details"]["hdr10"])
                self.assertFalse(codecs["hdr_details"]["hdr10_plus"])
                self.assertEqual(codecs["hdr_details"]["dolby_vision_profiles"], [])
                # The audio-track claim says nothing about HDR output.
                self.assertEqual(context["deliveries"]["original_http"]["validated_claims"],
                                 ["client_selected_audio_track_v1"])

    def test_audio_selection_claim_can_be_withheld_without_touching_hdr_claims(self):
        _, context = playback_capabilities("p7_fel", client_audio_selection=False)
        claims = context["deliveries"]["original_http"]["validated_claims"]
        self.assertEqual(claims, ["client_managed_dynamic_range_v1"])
        _, context = playback_capabilities("auto", client_audio_selection=False)
        self.assertEqual(context["deliveries"]["original_http"]["validated_claims"], [])

    def test_every_mode_claims_client_side_audio_track_selection(self):
        for mode in ("auto", "hdr10", "dv", "p7_fel"):
            with self.subTest(mode=mode):
                _, context = playback_capabilities(mode)
                self.assertIn("client_selected_audio_track_v1",
                              context["deliveries"]["original_http"]["validated_claims"])

    def test_platform_name_does_not_claim_hardware_or_receiver_passthrough(self):
        for mode in ("auto", "hdr10", "dv", "p7_fel"):
            with self.subTest(mode=mode):
                codecs, context = playback_capabilities(mode)
                self.assertEqual(codecs["video_evidence"], "declared")
                self.assertEqual(codecs["audio_evidence"], "declared")
                self.assertEqual(codecs["codecs_video_hardware"], [])
                self.assertNotIn("audio_passthrough", codecs)
                self.assertNotIn("audio_passthrough", context["output"])
                original = context["deliveries"]["original_http"]
                self.assertEqual(original["audio_passthrough_codecs"], [])
                self.assertFalse(original["auth_header_refresh"])
                self.assertNotIn("header_authenticated_media_v1", original["features"])

    def test_capability_results_do_not_share_mutable_state_between_attempts(self):
        codecs, context = playback_capabilities("p7_fel")
        codecs["hdr_details"]["dolby_vision_profiles"].clear()
        context["deliveries"]["original_http"]["validated_claims"].clear()
        next_codecs, next_context = playback_capabilities("p7_fel")
        self.assertIn(7, next_codecs["hdr_details"]["dolby_vision_profiles"])
        self.assertIn("client_managed_dynamic_range_v1", next_context["deliveries"]["original_http"]["validated_claims"])

    def test_resolution_override_only_changes_declared_decoder_limit(self):
        for resolution in ("480p", "720p", "1080p", "2160p"):
            with self.subTest(resolution=resolution):
                codecs, context = playback_capabilities("auto", max_resolution=resolution)
                self.assertEqual(codecs["max_resolution"], resolution)
                self.assertFalse(codecs["hdr"])
                assert_contract(self, codecs, "ClientCodecCapabilitiesV3")

    def test_hls_is_offered_for_converted_streams_and_progressive_is_declined(self):
        for mode in ("auto", "hdr10", "dv", "p7_fel"):
            with self.subTest(mode=mode):
                codecs, context = playback_capabilities(mode)
                hls = context["deliveries"]["hls"]
                self.assertTrue(hls["enabled"] and hls["supported_on_device"])
                # The planner matches the plan's stream container ("hls").
                self.assertIn("hls", hls["containers"])
                self.assertEqual(set(hls["video_codecs"]), {"h264", "hevc"})
                self.assertIn("aac", hls["audio_decode_codecs"])
                self.assertEqual(hls["audio_passthrough_codecs"], [])
                self.assertEqual(hls["hdr_details"], codecs["hdr_details"])
                # No bitmap sidecar support: the server burns those in instead.
                self.assertFalse(hls["subtitles"]["embedded_bitmap"])
                self.assertFalse(hls["subtitles"]["sidecar_bitmap"])
                self.assertTrue(hls["subtitles"]["sidecar_text"])
                progressive = context["deliveries"]["progressive"]
                self.assertFalse(progressive["enabled"])
                self.assertFalse(progressive["supported_on_device"])
                self.assertTrue(progressive["failure_reason"])

    def test_direct_play_declares_what_kodi_decodes(self):
        codecs, context = playback_capabilities("auto")
        original = context["deliveries"]["original_http"]
        for codec in ("h264", "hevc", "av1", "vc1", "vp9", "mpeg2video"):
            self.assertIn(codec, codecs["codecs_video"])
            self.assertIn(codec, original["video_codecs"])
        for codec in ("aac", "ac3", "eac3", "truehd", "dts", "flac", "opus"):
            self.assertIn(codec, original["audio_decode_codecs"])
        # Container names the Silo scanner reports.
        for container in ("mkv", "mp4", "avi", "ts", "m2ts", "webm", "wmv", "ogg"):
            self.assertIn(container, codecs["containers"])

    def test_unknown_resolution_falls_back_per_hdr_mode(self):
        self.assertEqual(playback_capabilities("auto", max_resolution="8k")[0]["max_resolution"], "1080p")
        self.assertEqual(playback_capabilities("hdr10", max_resolution="auto")[0]["max_resolution"], "2160p")


if __name__ == "__main__":
    unittest.main()
