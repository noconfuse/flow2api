import json
import unittest

from src.core.models import BatchJobItem
from src.services.batch_executor import BatchExecutor


class _FakeDB:
    def __init__(self):
        self.item_updates = []

    async def update_batch_job_item(self, item_id, **kwargs):
        self.item_updates.append((item_id, kwargs))


class _FakeGenerationHandler:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def handle_generation(self, **kwargs):
        self.calls.append(dict(kwargs))
        payload = self._responses.pop(0)
        yield json.dumps(payload, ensure_ascii=False)


class BatchExecutorRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_item_retries_with_excluded_failed_token(self):
        first_failure = {
            "error": {
                "message": "视频生成失败: INTERNAL (code: 13)，请重试",
                "code": "generation_failed",
                "status_code": 500,
            },
            "token_id": 7,
            "project_id": "project-old",
        }
        second_success = {
            "token_id": 8,
            "project_id": "project-new",
            "url": "https://example.com/result.mp4",
            "generated_assets": {"mediaGenerationId": "media-123"},
        }
        db = _FakeDB()
        generation_handler = _FakeGenerationHandler([first_failure, second_success])
        executor = BatchExecutor(db, generation_handler)
        item = BatchJobItem(
            id=11,
            job_id="batch-1",
            row_index=1,
            task_type="video",
            normalized_payload={
                "model": "omni-flash-r2v_landscape_4s",
                "prompt": "test prompt",
                "preferred_token_id": 7,
                "preferred_project_id": "project-old",
            },
        )

        await executor._run_item(item)

        self.assertEqual(len(generation_handler.calls), 2)
        self.assertEqual(generation_handler.calls[0]["preferred_token_id"], 7)
        self.assertEqual(generation_handler.calls[0]["preferred_project_id"], "project-old")
        self.assertEqual(generation_handler.calls[0]["excluded_token_ids"], [])
        self.assertIsNone(generation_handler.calls[1]["preferred_token_id"])
        self.assertIsNone(generation_handler.calls[1]["preferred_project_id"])
        self.assertEqual(generation_handler.calls[1]["excluded_token_ids"], [7])

        final_update = db.item_updates[-1][1]
        self.assertEqual(final_update["status"], "succeeded")
        self.assertEqual(final_update["token_id"], 8)
        self.assertEqual(final_update["project_id"], "project-new")
        self.assertEqual(final_update["result_media_id"], "media-123")
        self.assertEqual(final_update["result_url"], "https://example.com/result.mp4")


if __name__ == "__main__":
    unittest.main()
