import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


class _FakeProxyManager:
    pass


class _FakeDb:
    def __init__(self):
        self.get_token = AsyncMock(return_value=type("TokenRow", (), {"st": "st-token"})())


class TextToVideoWorkflowRouteTests(unittest.IsolatedAsyncioTestCase):
    def test_derive_extension_video_ui_settings_uses_specific_veo_labels(self):
        client = FlowClient(_FakeProxyManager(), db=_FakeDb())

        lite = client._derive_extension_video_ui_settings(
            model_key="veo_3_1_t2v_lite_4s",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            outputs_per_prompt=1,
        )
        fast = client._derive_extension_video_ui_settings(
            model_key="veo_3_1_r2v_fast_landscape_ultra_relaxed",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            outputs_per_prompt=1,
        )
        quality = client._derive_extension_video_ui_settings(
            model_key="veo_3_1_t2v",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            outputs_per_prompt=1,
        )

        self.assertEqual(lite["desired_model_display_name"], "Veo 3.1 - Lite")
        self.assertEqual(fast["desired_model_display_name"], "Veo 3.1 - Fast")
        self.assertEqual(quality["desired_model_display_name"], "Veo 3.1 - Quality")

    async def test_submit_video_text_via_extension_ui_uses_video_ui_workflow(self):
        client = FlowClient(_FakeProxyManager(), db=_FakeDb())
        client.get_flow_project_initial_data = AsyncMock(return_value={"raw": {}})
        client._collect_video_operations_from_project_snapshot = lambda snapshot, fallback_project_id=None: []
        client._derive_extension_video_ui_settings = lambda **kwargs: {
            "desired_model_display_name": "",
            "desired_duration_seconds": "",
            "desired_aspect_ratio_label": "9:16",
            "desired_outputs_per_prompt": 1,
        }
        client._wait_for_new_video_operations_after_ui_submit = AsyncMock(
            return_value=[{"operation": {"name": "task-1"}, "projectId": "project-1"}]
        )
        client.get_credits = AsyncMock(return_value={"credits": 50})

        fake_service = type("Svc", (), {})()
        fake_service.dispatch_ui_job = AsyncMock(
            return_value={
                "status": "success",
                "job_id": "job-1",
                "result": {
                    "ui_state": {
                        "success": True,
                        "result_version": "video_ui_workflow_v1",
                        "workflow_mode": "text_to_video",
                        "steps": [],
                    }
                },
            }
        )

        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=fake_service),
        ):
            result = await client._submit_video_text_via_extension_ui(
                at="at-token",
                token_id=6,
                project_id="project-1",
                prompt="horse",
                model_key="veo_3_1_t2v_lite",
                aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            )

        fake_service.dispatch_ui_job.assert_awaited_once()
        kwargs = fake_service.dispatch_ui_job.await_args.kwargs
        self.assertEqual(kwargs["job_type"], "video_ui_workflow")
        self.assertEqual(kwargs["payload"]["workflow_mode"], "text_to_video")
        self.assertEqual(kwargs["payload"]["preferred_submode"], "文本")
        self.assertIn("desired_model_display_name", kwargs["payload"])
        self.assertIn("desired_duration_seconds", kwargs["payload"])
        self.assertEqual(result["ui_submit"]["workflow_mode"], "text_to_video")
        self.assertEqual(result["remainingCredits"], 50)

    async def test_submit_video_edit_via_extension_ui_includes_model_and_duration_settings(self):
        client = FlowClient(_FakeProxyManager(), db=_FakeDb())
        client.get_flow_project_initial_data = AsyncMock(
            return_value={
                "media": [{"media_id": "media-image-2", "media_type": "image"}],
                "raw": {},
            }
        )
        client._collect_video_operations_from_project_snapshot = lambda snapshot, fallback_project_id=None: []
        client._build_video_reference_texts_from_project_snapshot = lambda snapshot, source_media_id: ["orange cat window"]
        client._derive_extension_video_ui_settings = lambda **kwargs: {
            "desired_model_display_name": "Omni Flash",
            "desired_duration_seconds": "4",
            "desired_aspect_ratio_label": "9:16",
            "desired_outputs_per_prompt": 1,
        }
        client._wait_for_new_video_operations_after_ui_submit = AsyncMock(
            return_value=[{"operation": {"name": "task-2"}, "projectId": "project-1"}]
        )
        client.get_credits = AsyncMock(return_value={"credits": 40})

        fake_service = type("Svc", (), {})()
        fake_service.dispatch_ui_job = AsyncMock(
            return_value={
                "status": "success",
                "job_id": "job-2",
                "result": {
                    "ui_state": {
                        "success": True,
                        "result_version": "video_ui_workflow_v1",
                        "workflow_mode": "edit_existing_video",
                        "steps": [],
                    }
                },
            }
        )

        with patch(
            "src.services.browser_captcha_extension.ExtensionCaptchaService.get_instance",
            new=AsyncMock(return_value=fake_service),
        ):
            await client._submit_video_edit_via_extension_ui(
                at="at-token",
                token_id=6,
                project_id="project-1",
                prompt="horse",
                model_key="abra_r2v_4s",
                aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
                source_media_id="media-image-2",
                reference_media_kind="image",
            )

        kwargs = fake_service.dispatch_ui_job.await_args.kwargs
        self.assertEqual(kwargs["job_type"], "video_ui_workflow")
        self.assertEqual(kwargs["payload"]["workflow_mode"], "edit_existing_video")
        self.assertEqual(kwargs["payload"]["reference_media_kind"], "image")
        self.assertEqual(kwargs["payload"]["desired_model_display_name"], "Omni Flash")
        self.assertEqual(kwargs["payload"]["desired_duration_seconds"], "4")
        self.assertEqual(kwargs["payload"]["desired_aspect_ratio_label"], "9:16")
        self.assertEqual(kwargs["payload"]["desired_outputs_per_prompt"], 1)

    async def test_generate_video_start_image_routes_single_image_to_video_ui_workflow(self):
        client = FlowClient(_FakeProxyManager(), db=_FakeDb())
        client._submit_video_edit_via_extension_ui = AsyncMock(return_value={"projectId": "project-1"})
        client._normalize_video_generation_response = lambda payload, fallback_project_id=None: {
            "projectId": fallback_project_id,
            "raw": payload,
        }

        result = await client.generate_video_start_image(
            at="at-token",
            project_id="project-1",
            prompt="horse",
            model_key="veo_3_1_i2v_lite",
            aspect_ratio="VIDEO_ASPECT_RATIO_PORTRAIT",
            start_media_id="media-image-1",
            token_id=6,
        )

        client._submit_video_edit_via_extension_ui.assert_awaited_once()
        kwargs = client._submit_video_edit_via_extension_ui.await_args.kwargs
        self.assertEqual(kwargs["source_media_id"], "media-image-1")
        self.assertEqual(kwargs["reference_media_kind"], "image")
        self.assertEqual(result["projectId"], "project-1")

    async def test_generate_video_reference_images_routes_single_asset_to_video_ui_workflow(self):
        client = FlowClient(_FakeProxyManager(), db=_FakeDb())
        client._submit_video_edit_via_extension_ui = AsyncMock(return_value={"projectId": "project-1"})
        client._normalize_video_generation_response = lambda payload, fallback_project_id=None: {
            "projectId": fallback_project_id,
            "raw": payload,
        }

        result = await client.generate_video_reference_images(
            at="at-token",
            project_id="project-1",
            prompt="horse",
            model_key="veo_3_1_r2v_fast_landscape",
            aspect_ratio="VIDEO_ASPECT_RATIO_LANDSCAPE",
            reference_images=[{"imageUsageType": "IMAGE_USAGE_TYPE_ASSET", "mediaId": "media-image-2"}],
            token_id=6,
        )

        client._submit_video_edit_via_extension_ui.assert_awaited_once()
        kwargs = client._submit_video_edit_via_extension_ui.await_args.kwargs
        self.assertEqual(kwargs["source_media_id"], "media-image-2")
        self.assertEqual(kwargs["reference_media_kind"], "image")
        self.assertEqual(result["projectId"], "project-1")


if __name__ == "__main__":
    unittest.main()
