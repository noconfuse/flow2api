import sys
import types
import unittest

try:
    import tomli  # type: ignore
except ModuleNotFoundError:
    import tomllib as tomli  # type: ignore
    sys.modules.setdefault("tomli", tomli)

from src.core.model_resolver import resolve_model_name
from src.core.video_reference import parse_video_edit_uri
from src.services.generation_handler import MODEL_CONFIG, GenerationHandler


class OmniFlashResolverTests(unittest.TestCase):
    def test_resolve_omni_flash_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "omni-flash-t2v",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-t2v_portrait")

    def test_resolve_omni_flash_alias_defaults_to_landscape(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="landscape")
        )

        resolved = resolve_model_name(
            "omni-flash-t2v",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-t2v_landscape")

    def test_resolve_omni_flash_duration_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "omni-flash-t2v_6s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-t2v_portrait_6s")

    def test_resolve_omni_flash_edit_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "omni-flash-edit",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-edit_portrait")

    def test_resolve_omni_flash_r2v_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "omni-flash-r2v",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-r2v_portrait")

    def test_resolve_omni_flash_r2v_duration_alias_to_portrait_variant(self):
        request = types.SimpleNamespace(
            generationConfig=types.SimpleNamespace(aspectRatio="portrait")
        )

        resolved = resolve_model_name(
            "omni-flash-r2v_6s",
            request=request,
            model_config=MODEL_CONFIG,
        )

        self.assertEqual(resolved, "omni-flash-r2v_portrait_6s")


class OmniFlashGenerationConfigTests(unittest.TestCase):
    def test_omni_flash_public_model_uses_abra_t2v_model_key(self):
        landscape_cfg = MODEL_CONFIG["omni-flash-t2v_landscape"]
        portrait_cfg = MODEL_CONFIG["omni-flash-t2v_portrait"]
        duration_cfg = MODEL_CONFIG["omni-flash-t2v_6s"]

        self.assertEqual(landscape_cfg["model_key"], "abra_t2v")
        self.assertEqual(portrait_cfg["model_key"], "abra_t2v")
        self.assertEqual(duration_cfg["model_key"], "abra_t2v_6s")
        self.assertFalse(landscape_cfg["allow_tier_upgrade"])
        self.assertFalse(portrait_cfg["allow_tier_upgrade"])
        self.assertFalse(duration_cfg["allow_tier_upgrade"])
        self.assertTrue(landscape_cfg["use_v2_model_config"])
        self.assertTrue(portrait_cfg["use_v2_model_config"])
        self.assertTrue(duration_cfg["use_v2_model_config"])

    def test_omni_flash_model_key_does_not_upgrade_for_tier_two(self):
        handler = GenerationHandler.__new__(GenerationHandler)

        model_key, message = handler._resolve_video_model_key_for_tier(
            MODEL_CONFIG["omni-flash-t2v_landscape"],
            "PAYGATE_TIER_TWO",
        )

        self.assertEqual(model_key, "abra_t2v")
        self.assertIsNone(message)

    def test_omni_flash_edit_public_model_uses_abra_edit_model_key(self):
        landscape_cfg = MODEL_CONFIG["omni-flash-edit_landscape"]
        portrait_cfg = MODEL_CONFIG["omni-flash-edit_portrait"]

        self.assertEqual(landscape_cfg["model_key"], "abra_edit")
        self.assertEqual(portrait_cfg["model_key"], "abra_edit")
        self.assertFalse(landscape_cfg["allow_tier_upgrade"])
        self.assertFalse(portrait_cfg["allow_tier_upgrade"])
        self.assertEqual(landscape_cfg["video_type"], "edit")
        self.assertEqual(portrait_cfg["video_type"], "edit")

    def test_omni_flash_r2v_public_model_uses_abra_r2v_model_key(self):
        landscape_cfg = MODEL_CONFIG["omni-flash-r2v_landscape"]
        portrait_cfg = MODEL_CONFIG["omni-flash-r2v_portrait"]
        alias_cfg = MODEL_CONFIG["omni-flash-r2v_8s"]
        short_cfg = MODEL_CONFIG["omni-flash-r2v_4s"]
        mid_cfg = MODEL_CONFIG["omni-flash-r2v_6s"]
        long_cfg = MODEL_CONFIG["omni-flash-r2v_10s"]

        self.assertEqual(landscape_cfg["model_key"], "abra_r2v")
        self.assertEqual(portrait_cfg["model_key"], "abra_r2v")
        self.assertEqual(alias_cfg["model_key"], "abra_r2v_8s")
        self.assertEqual(short_cfg["model_key"], "abra_r2v_4s")
        self.assertEqual(mid_cfg["model_key"], "abra_r2v_6s")
        self.assertEqual(long_cfg["model_key"], "abra_r2v_10s")
        self.assertFalse(landscape_cfg["allow_tier_upgrade"])
        self.assertFalse(portrait_cfg["allow_tier_upgrade"])
        self.assertFalse(short_cfg["allow_tier_upgrade"])
        self.assertFalse(mid_cfg["allow_tier_upgrade"])
        self.assertFalse(long_cfg["allow_tier_upgrade"])
        self.assertEqual(landscape_cfg["video_type"], "r2v")
        self.assertEqual(portrait_cfg["video_type"], "r2v")


class OmniFlashEditRouteParsingTests(unittest.TestCase):
    def test_parse_edit_reference(self):
        video_edit_params = parse_video_edit_uri(
            "edit://c1d3787f-2272-47b8-a2ba-cfd975db5972?start_frame=0&end_frame=240"
        )

        self.assertEqual(
            video_edit_params,
            {
                "media_id": "c1d3787f-2272-47b8-a2ba-cfd975db5972",
                "start_frame_index": 0,
                "end_frame_index": 240,
                "source_duration_seconds": None,
            },
        )

    def test_parse_time_based_edit_reference(self):
        video_edit_params = parse_video_edit_uri(
            "edit://c1d3787f-2272-47b8-a2ba-cfd975db5972?start_time=0.73&end_time=3.05&source_duration=4.01"
        )

        self.assertEqual(
            video_edit_params,
            {
                "media_id": "c1d3787f-2272-47b8-a2ba-cfd975db5972",
                "start_seconds": 0.73,
                "end_seconds": 3.05,
                "source_duration_seconds": 4.01,
            },
        )


if __name__ == "__main__":
    unittest.main()
