"""Capability promises must remain explicit and match the API contract."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))

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
                self.assertEqual(set(context["deliveries"]), {"original_http"})
                self.assertEqual(delivery["transformations"], [])
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
                self.assertEqual(context["deliveries"]["original_http"]["validated_claims"], [])

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


if __name__ == "__main__":
    unittest.main()
