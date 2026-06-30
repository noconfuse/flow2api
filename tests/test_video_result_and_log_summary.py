import json
import unittest

from src.api.admin import _extract_log_summary
from src.services.generation_handler import GenerationHandler


class VideoDeliveryInfoTests(unittest.TestCase):
    def setUp(self):
        self.handler = object.__new__(GenerationHandler)

    def test_extract_video_delivery_info_supports_output_uri(self):
        operation = {
            "mediaName": "media-generated-id",
            "operation": {
                "name": "operations/abc",
                "metadata": {
                    "video": {
                        "outputUri": "https://example.com/video/12345678-1234-1234-1234-123456789abc?x=1",
                        "aspectRatio": "VIDEO_ASPECT_RATIO_PORTRAIT",
                    }
                },
            },
        }

        result = self.handler._extract_video_delivery_info(operation)

        self.assertEqual(
            result["video_url"],
            "https://example.com/video/12345678-1234-1234-1234-123456789abc?x=1",
        )
        self.assertEqual(result["video_media_id"], "12345678-1234-1234-1234-123456789abc")
        self.assertEqual(result["aspect_ratio"], "VIDEO_ASPECT_RATIO_PORTRAIT")

    def test_extract_video_delivery_info_falls_back_to_media_name(self):
        operation = {
            "mediaName": "fallback-media-id",
            "operation": {
                "name": "operations/abc",
                "metadata": {
                    "video": {}
                },
            },
        }

        result = self.handler._extract_video_delivery_info(operation)

        self.assertEqual(result["video_url"], "")
        self.assertEqual(result["video_media_id"], "fallback-media-id")


class LogSummaryTests(unittest.TestCase):
    def test_extract_log_summary_reads_generated_video_asset(self):
        log = {
            "request_body": json.dumps({
                "model": "omni-flash-t2v_landscape_4s",
                "prompt": "一只橘猫坐在窗台上晒太阳",
            }, ensure_ascii=False),
            "response_body": json.dumps({
                "url": "http://localhost/tmp/video.mp4",
                "generated_assets": {
                    "type": "video",
                    "final_video_url": "http://localhost/tmp/video.mp4",
                    "mediaGenerationId": "video-media-id",
                },
            }, ensure_ascii=False),
        }

        summary = _extract_log_summary(log)

        self.assertEqual(summary["model"], "omni-flash-t2v_landscape_4s")
        self.assertEqual(summary["asset_type"], "video")
        self.assertEqual(summary["media_url"], "http://localhost/tmp/video.mp4")
        self.assertEqual(summary["media_generation_id"], "video-media-id")
        self.assertTrue(summary["has_generated_asset"])


if __name__ == "__main__":
    unittest.main()
