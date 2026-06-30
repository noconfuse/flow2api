import unittest
from unittest.mock import AsyncMock

from src.services.flow_client import FlowClient


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_project_scoped_upload_uses_new_endpoint_with_project_id(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": {
                    "name": "new-media-id",
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )
        self.assertIn("sessionId", request_calls[0]["json_data"]["clientContext"])

    async def test_project_scoped_upload_accepts_media_list_response(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            return {
                "media": [
                    {
                        "name": "new-media-id",
                        "projectId": "project-123",
                    }
                ]
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id="project-123",
        )

        self.assertEqual(media_id, "new-media-id")
        self.assertEqual(len(request_calls), 1)
        self.assertTrue(request_calls[0]["url"].endswith("/flow/uploadImage"))

    async def test_project_scoped_upload_does_not_fallback_to_legacy_endpoint(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            self.fail("带 project_id 的上传不应回退到 legacy 接口")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        with self.assertRaisesRegex(RuntimeError, "legacy :uploadUserImage fallback is disabled"):
            await client.upload_image(
                at="test-at",
                image_bytes=JPEG_BYTES,
                aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
                project_id="project-123",
            )

        self.assertEqual(len(request_calls), 1)
        self.assertEqual(
            request_calls[0]["json_data"]["clientContext"]["projectId"],
            "project-123",
        )

    async def test_upload_without_project_id_keeps_legacy_fallback(self):
        client = FlowClient(proxy_manager=None)

        request_calls = []

        async def fake_make_request(**kwargs):
            request_calls.append(kwargs)
            if kwargs["url"].endswith("/flow/uploadImage"):
                raise RuntimeError("HTTP 500: upstream failed")
            if kwargs["url"].endswith(":uploadUserImage"):
                return {
                    "mediaGenerationId": {
                        "mediaGenerationId": "legacy-media-id",
                    }
                }
            self.fail(f"Unexpected url: {kwargs['url']}")

        client._make_request = AsyncMock(side_effect=fake_make_request)

        media_id = await client.upload_image(
            at="test-at",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=None,
        )

        self.assertEqual(media_id, "legacy-media-id")
        self.assertEqual(len(request_calls), 2)
        self.assertNotIn(
            "projectId",
            request_calls[1]["json_data"]["clientContext"],
        )


class FlowClientProjectInitialDataTests(unittest.IsolatedAsyncioTestCase):
    async def test_build_trpc_get_url_for_undefined_payload(self):
        client = FlowClient(proxy_manager=None)

        url = client._build_trpc_get_url("general.fetchUserPreferences", payload=None)

        self.assertIn("/trpc/general.fetchUserPreferences?input=", url)
        self.assertIn("%22meta%22", url)
        self.assertIn("%22undefined%22", url)

    async def test_build_media_redirect_url_uses_name_query(self):
        client = FlowClient(proxy_manager=None)

        url = client._build_media_redirect_url("media-123")

        self.assertEqual(
            url,
            f"{client.labs_base_url}/trpc/media.getMediaUrlRedirect?name=media-123",
        )

    async def test_get_flow_project_initial_data_extracts_media(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            self.assertEqual(kwargs["method"], "GET")
            self.assertTrue(kwargs["url"].startswith(client.labs_base_url))
            return {
                "result": {
                    "data": {
                        "json": {
                            "project": {"projectId": "project-123"},
                            "projectContents": {
                                "workflows": [
                                    {
                                        "name": "workflow-1",
                                        "projectId": "project-123",
                                        "metadata": {
                                            "displayName": "uploaded-image.png",
                                            "primaryMediaId": "media-image-1",
                                            "updateTime": "2026-05-30T00:00:10Z",
                                        },
                                    }
                                ]
                            },
                            "media": [
                                {
                                    "name": "media-video-1",
                                    "projectId": "project-123",
                                    "video": {
                                        "outputUri": "https://example.com/video.mp4",
                                        "aspectRatio": "VIDEO_ASPECT_RATIO_LANDSCAPE",
                                    },
                                    "mediaMetadata": {
                                        "mediaStatus": {
                                            "mediaGenerationStatus": "MEDIA_GENERATION_STATUS_SUCCESSFUL"
                                        }
                                    },
                                },
                                {
                                    "name": "media-image-1",
                                    "projectId": "project-123",
                                    "image": {
                                        "fifeUrl": "https://example.com/image.png",
                                        "aspectRatio": "IMAGE_ASPECT_RATIO_PORTRAIT",
                                    },
                                },
                            ],
                        }
                    }
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await client.get_flow_project_initial_data(
            st="test-st",
            project_id="project-123",
        )

        self.assertEqual(result["project_id"], "project-123")
        self.assertEqual(len(result["media"]), 2)
        self.assertEqual(result["media"][0]["name"], "media-video-1")
        self.assertEqual(result["media"][0]["media_type"], "video")
        self.assertEqual(result["media"][0]["preview_url"], "https://example.com/video.mp4")
        self.assertEqual(
            result["media"][0]["status"],
            "MEDIA_GENERATION_STATUS_SUCCESSFUL",
        )
        self.assertEqual(result["media"][1]["media_type"], "image")
        self.assertEqual(result["media"][1]["name"], "uploaded-image.png")
        self.assertEqual(result["media"][1]["workflow_display_name"], "uploaded-image.png")
        self.assertEqual(result["media"][1]["update_time"], "2026-05-30T00:00:10Z")
        self.assertEqual(result["media"][1]["preview_url"], "https://example.com/image.png")

    async def test_get_flow_project_initial_data_prefers_official_media_array_order(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            self.assertEqual(kwargs["method"], "GET")
            return {
                "result": {
                    "data": {
                        "json": {
                            "project": {"projectId": "project-ordered"},
                            "projectContents": {
                                "workflows": [
                                    {
                                        "name": "workflow-a",
                                        "projectId": "project-ordered",
                                        "externalReferenceMedia": [
                                            {
                                                "mediaId": "media-3",
                                                "mediaType": "VIDEO",
                                                "media": {
                                                    "name": "media-3",
                                                    "projectId": "project-ordered",
                                                    "video": {"outputUri": "https://example.com/3.mp4"},
                                                },
                                            },
                                            {
                                                "mediaId": "media-1",
                                                "mediaType": "IMAGE",
                                                "media": {
                                                    "name": "media-1",
                                                    "projectId": "project-ordered",
                                                    "image": {"fifeUrl": "https://example.com/1.png"},
                                                },
                                            },
                                        ],
                                    }
                                ]
                            },
                            "media": [
                                {
                                    "name": "media-1",
                                    "projectId": "project-ordered",
                                    "image": {"fifeUrl": "https://example.com/1.png"},
                                },
                                {
                                    "name": "media-2",
                                    "projectId": "project-ordered",
                                    "video": {"outputUri": "https://example.com/2.mp4"},
                                },
                                {
                                    "name": "media-3",
                                    "projectId": "project-ordered",
                                    "video": {"outputUri": "https://example.com/3.mp4"},
                                },
                            ],
                        }
                    }
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await client.get_flow_project_initial_data(
            st="test-st",
            project_id="project-ordered",
        )

        self.assertEqual(
            [item["media_id"] for item in result["media"]],
            ["media-1", "media-2", "media-3"],
        )

    async def test_get_flow_project_initial_data_sorts_media_by_create_time_for_ui_order(self):
        client = FlowClient(proxy_manager=None)

        async def fake_make_request(**kwargs):
            self.assertEqual(kwargs["method"], "GET")
            return {
                "result": {
                    "data": {
                        "json": {
                            "project": {"projectId": "project-sorted"},
                            "media": [
                                {
                                    "name": "media-middle",
                                    "projectId": "project-sorted",
                                    "video": {"outputUri": "https://example.com/middle.mp4"},
                                    "mediaMetadata": {"createTime": "2026-06-10T07:30:34.675650Z"},
                                },
                                {
                                    "name": "media-latest",
                                    "projectId": "project-sorted",
                                    "video": {"outputUri": "https://example.com/latest.mp4"},
                                    "mediaMetadata": {"createTime": "2026-06-24T17:04:10.501567Z"},
                                },
                                {
                                    "name": "media-earliest",
                                    "projectId": "project-sorted",
                                    "video": {"outputUri": "https://example.com/earliest.mp4"},
                                    "mediaMetadata": {"createTime": "2026-06-10T07:29:08.688995Z"},
                                },
                            ],
                        }
                    }
                }
            }

        client._make_request = AsyncMock(side_effect=fake_make_request)

        result = await client.get_flow_project_initial_data(
            st="test-st",
            project_id="project-sorted",
        )

        self.assertEqual(
            [item["media_id"] for item in result["media"]],
            ["media-earliest", "media-middle", "media-latest"],
        )

    async def test_normalize_upstream_media_item_prefers_media_id_and_nested_metadata(self):
        client = FlowClient(proxy_manager=None)

        media = {
            "mediaId": "media-outer-1",
            "mediaType": "IMAGE",
            "workflowDisplayName": "Example Asset",
            "media": {
                "name": "media-inner-1",
                "projectId": "project-456",
                "image": {
                    "dimensions": {"width": 1024, "height": 768},
                },
                "mediaMetadata": {
                    "createdAt": "2026-05-30T00:00:00Z",
                },
            },
        }

        result = client._normalize_upstream_media_item(media)

        self.assertEqual(result["media_id"], "media-inner-1")
        self.assertEqual(result["name"], "Example Asset")
        self.assertEqual(result["media_type"], "image")
        self.assertEqual(result["create_time"], "2026-05-30T00:00:00Z")
        self.assertEqual(result["width"], 1024)
        self.assertEqual(result["height"], 768)


if __name__ == "__main__":
    unittest.main()
