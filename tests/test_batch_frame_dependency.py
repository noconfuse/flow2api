"""BatchExecutor image_source_dependency / final-frame extraction 单元测试。"""

import json
import unittest
from pathlib import Path

from src.core.models import BatchJobItem
from src.services.batch_executor import BatchExecutor, UpstreamDependencyError


class _FakeDB:
    def __init__(self):
        self.updates = []

    async def update_batch_job_item(self, item_id, **kwargs):
        self.updates.append((item_id, kwargs))

    async def update_batch_job(self, job_id, **kwargs):
        self.job_updates = getattr(self, "job_updates", [])
        self.job_updates.append((job_id, kwargs))

    async def list_batch_job_items(self, job_id):
        return self._items

    def set_items(self, items):
        self._items = items


class _FakeGenerationHandler:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    async def handle_generation(self, **kwargs):
        self.calls.append(dict(kwargs))
        payload = self._responses.pop(0)
        yield json.dumps(payload, ensure_ascii=False)


class _StubBatchExecutor(BatchExecutor):
    """Skip network/IO for the frame-extraction path."""

    def __init__(self, db, generation_handler, *, frame_path=None):
        super().__init__(db, generation_handler)
        self._stub_frame_path = frame_path
        self.extraction_calls = 0

    async def _extract_and_store_final_frame(self, *, item, payload):  # type: ignore[override]
        self.extraction_calls += 1
        return self._stub_frame_path

    async def _read_file_bytes(self, raw_item, *, expected_kind):  # type: ignore[override]
        return b"stub-frame-bytes"


class BatchExecutorDependencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_dependency_injects_upstream_frame_into_input_images(self):
        db = _FakeDB()
        generation_handler = _FakeGenerationHandler(
            responses=[
                {"url": "https://example.com/producer.mp4", "project_id": "p1"},
                {"url": "https://example.com/consumer.mp4", "project_id": "p1"},
            ]
        )

        frame_path = "/tmp/final_frame_0001.png"
        Path(frame_path).write_bytes(b"\x89PNG\r\n\x1a\n")

        producer_item = BatchJobItem(
            id=1,
            job_id="batch-1",
            row_index=1,
            row_id="row_0001",
            task_type="video",
            status="succeeded",
            normalized_payload={"prompt": "p1", "model": "m"},
            result_url="https://example.com/producer.mp4",
            final_frame_path=frame_path,
        )
        consumer_item = BatchJobItem(
            id=2,
            job_id="batch-1",
            row_index=2,
            row_id="row_0002",
            task_type="video",
            status="pending",
            normalized_payload={
                "prompt": "p2",
                "model": "m",
                "image_source_dependency": {"row_key": "row_0001"},
                "input_images": [],
                "reference_assets": [],
            },
        )
        db.set_items([producer_item, consumer_item])

        executor = _StubBatchExecutor(db, generation_handler, frame_path=frame_path)
        await executor._run_item(producer_item, all_items=[producer_item, consumer_item])
        producer_item.status = "succeeded"
        producer_item.final_frame_path = frame_path

        await executor._run_item(consumer_item, all_items=[producer_item, consumer_item])

        consumer_call = generation_handler.calls[-1]
        input_images = consumer_call.get("images")
        self.assertIsNotNone(input_images)
        self.assertGreaterEqual(len(input_images), 1)
        ref_assets = consumer_call.get("reference_assets")
        self.assertIsNotNone(ref_assets)
        frame_ref = next(
            (
                entry
                for entry in ref_assets
                if isinstance(entry, dict)
                and str(entry.get("slot") or "") == "image_source"
            ),
            None,
        )
        self.assertIsNotNone(frame_ref)
        self.assertEqual(str(frame_ref.get("file_path")), frame_path)

        Path(frame_path).unlink(missing_ok=True)

    async def test_dependency_fails_when_producer_not_succeeded(self):
        db = _FakeDB()
        generation_handler = _FakeGenerationHandler(responses=[{"url": "x"}])

        producer_item = BatchJobItem(
            id=1,
            job_id="batch-1",
            row_index=1,
            row_id="row_0001",
            task_type="video",
            status="failed",
            normalized_payload={"prompt": "p1", "model": "m"},
        )
        consumer_item = BatchJobItem(
            id=2,
            job_id="batch-1",
            row_index=2,
            row_id="row_0002",
            task_type="video",
            status="pending",
            normalized_payload={
                "prompt": "p2",
                "model": "m",
                "image_source_dependency": {"row_key": "row_0001"},
                "input_images": [],
                "reference_assets": [],
            },
        )
        db.set_items([producer_item, consumer_item])

        executor = BatchExecutor(db, generation_handler)
        await executor._run_item(consumer_item, all_items=[producer_item, consumer_item])

        final_update = next(
            update
            for update in db.updates
            if update[0] == consumer_item.id and update[1].get("status") == "failed"
        )
        self.assertEqual(final_update[1].get("error_code"), "upstream_failed")
        self.assertIn("row_0001", final_update[1].get("error_message", ""))
        self.assertEqual(len(generation_handler.calls), 0)

    async def test_dependency_fails_when_producer_missing_final_frame_path(self):
        db = _FakeDB()
        generation_handler = _FakeGenerationHandler(responses=[{"url": "x"}])

        producer_item = BatchJobItem(
            id=1,
            job_id="batch-1",
            row_index=1,
            row_id="row_0001",
            task_type="video",
            status="succeeded",
            normalized_payload={"prompt": "p1", "model": "m"},
            final_frame_path=None,
        )
        consumer_item = BatchJobItem(
            id=2,
            job_id="batch-1",
            row_index=2,
            row_id="row_0002",
            task_type="video",
            status="pending",
            normalized_payload={
                "prompt": "p2",
                "model": "m",
                "image_source_dependency": {"row_key": "row_0001"},
                "input_images": [],
                "reference_assets": [],
            },
        )
        db.set_items([producer_item, consumer_item])

        executor = BatchExecutor(db, generation_handler)
        await executor._run_item(consumer_item, all_items=[producer_item, consumer_item])

        final_update = next(
            update
            for update in db.updates
            if update[0] == consumer_item.id and update[1].get("status") == "failed"
        )
        self.assertEqual(final_update[1].get("error_code"), "upstream_failed")
        self.assertIn("尚未提取", final_update[1].get("error_message", ""))

    async def test_dependency_fails_when_row_key_unknown(self):
        db = _FakeDB()
        generation_handler = _FakeGenerationHandler(responses=[{"url": "x"}])

        producer_item = BatchJobItem(
            id=1,
            job_id="batch-1",
            row_index=1,
            row_id="row_0001",
            task_type="video",
            status="succeeded",
            normalized_payload={"prompt": "p1", "model": "m"},
            final_frame_path="/tmp/x.png",
        )
        consumer_item = BatchJobItem(
            id=2,
            job_id="batch-1",
            row_index=2,
            row_id="row_0002",
            task_type="video",
            status="pending",
            normalized_payload={
                "prompt": "p2",
                "model": "m",
                "image_source_dependency": {"row_key": "row_9999"},
                "input_images": [],
                "reference_assets": [],
            },
        )
        db.set_items([producer_item, consumer_item])

        executor = BatchExecutor(db, generation_handler)
        await executor._run_item(consumer_item, all_items=[producer_item, consumer_item])

        final_update = next(
            update
            for update in db.updates
            if update[0] == consumer_item.id and update[1].get("status") == "failed"
        )
        self.assertEqual(final_update[1].get("error_code"), "upstream_failed")
        self.assertIn("找不到", final_update[1].get("error_message", ""))


class TopologySchedulingTests(unittest.IsolatedAsyncioTestCase):
    def _make_item(self, *, id, row_index, row_id, dep=None):
        payload: dict = {"prompt": f"p{row_index}", "model": "m"}
        if dep is not None:
            payload["image_source_dependency"] = {"row_key": dep}
        return BatchJobItem(
            id=id,
            job_id="batch-1",
            row_index=row_index,
            row_id=row_id,
            task_type="video",
            status="pending",
            normalized_payload=payload,
        )

    def test_topological_order_chain(self):
        """3 条链式依赖：row_0001 -> row_0002 -> row_0003。"""
        db = _FakeDB()
        executor = BatchExecutor(db, _FakeGenerationHandler([]))
        items = [
            self._make_item(id=1, row_index=1, row_id="row_0001"),
            self._make_item(id=2, row_index=2, row_id="row_0002", dep="row_0001"),
            self._make_item(id=3, row_index=3, row_id="row_0003", dep="row_0002"),
        ]
        ordered = executor._topological_sort_with_fallback(items)
        keys = [str(item.row_id) for item in ordered]
        self.assertEqual(keys, ["row_0001", "row_0002", "row_0003"])

    def test_topological_order_diamond(self):
        """钻石型：A->B, A->C, B->D, C->D → A, B/C (并列), D。"""
        db = _FakeDB()
        executor = BatchExecutor(db, _FakeGenerationHandler([]))
        items = [
            self._make_item(id=1, row_index=1, row_id="row_0001"),
            self._make_item(id=2, row_index=2, row_id="row_0002", dep="row_0001"),
            self._make_item(id=3, row_index=3, row_id="row_0003", dep="row_0001"),
            self._make_item(id=4, row_index=4, row_id="row_0004", dep="row_0002"),
            self._make_item(id=5, row_index=5, row_id="row_0005", dep="row_0003"),
        ]
        ordered = executor._topological_sort_with_fallback(items)
        keys = [str(item.row_id) for item in ordered]
        self.assertEqual(keys, ["row_0001", "row_0002", "row_0003", "row_0004", "row_0005"])

    def test_cycle_detection_returns_none(self):
        """A->B, B->A → 检测到环，返回 None。"""
        db = _FakeDB()
        executor = BatchExecutor(db, _FakeGenerationHandler([]))
        items = [
            self._make_item(id=1, row_index=1, row_id="row_0001", dep="row_0002"),
            self._make_item(id=2, row_index=2, row_id="row_0002", dep="row_0001"),
        ]
        result = executor._topological_sort_with_fallback(items)
        self.assertIsNone(result)

    def test_topological_fallback_to_natural_order_when_no_deps(self):
        """无依赖时按 row_index 升序排列。"""
        db = _FakeDB()
        executor = BatchExecutor(db, _FakeGenerationHandler([]))
        items = [
            self._make_item(id=1, row_index=3, row_id="row_0003"),
            self._make_item(id=2, row_index=1, row_id="row_0001"),
            self._make_item(id=3, row_index=2, row_id="row_0002"),
        ]
        ordered = executor._topological_sort_with_fallback(items)
        keys = [str(item.row_id) for item in ordered]
        self.assertEqual(keys, ["row_0001", "row_0002", "row_0003"])

    def test_frame_producer_keys_collects_only_referenced_rows(self):
        """仅被 image_source 依赖引用的行才需提取帧。"""
        db = _FakeDB()
        executor = BatchExecutor(db, _FakeGenerationHandler([]))
        items = [
            self._make_item(id=1, row_index=1, row_id="row_0001"),
            self._make_item(id=2, row_index=2, row_id="row_0002", dep="row_0001"),
            self._make_item(id=3, row_index=3, row_id="row_0003"),  # 无人引用
        ]
        producers = executor._collect_frame_producer_keys(items)
        self.assertEqual(producers, {"row_0001"})

    def test_source_url_has_no_legacy_fallback(self):
        executor = BatchExecutor(_FakeDB(), _FakeGenerationHandler([]))
        self.assertIsNone(
            executor._extract_source_url(
                {
                    "url": "http://localhost/tmp/video.mp4",
                    "generated_assets": {
                        "final_video_url": "http://localhost/tmp/video.mp4",
                    },
                }
            )
        )
        self.assertEqual(
            executor._extract_source_url(
                {"generated_assets": {"source_url": "https://cdn.example/video.mp4"}}
            ),
            "https://cdn.example/video.mp4",
        )

    def test_preferred_project_is_explicit_job_local_state(self):
        executor = BatchExecutor(_FakeDB(), _FakeGenerationHandler([]))
        item = self._make_item(id=1, row_index=1, row_id="row_0001")
        executor._seed_preferred_token_project(item, (7, "project-7"))
        self.assertEqual(item.normalized_payload["preferred_token_id"], 7)
        self.assertEqual(item.normalized_payload["preferred_project_id"], "project-7")


if __name__ == "__main__":
    unittest.main()
"""GenerationHandler credit cost estimator unit tests."""

import unittest

from src.services.generation_handler import estimate_required_credits


class CreditCostEstimatorTests(unittest.TestCase):
    def test_omni_flash_tier_by_duration(self):
        self.assertEqual(estimate_required_credits("abra_t2v", "4s"), 7)
        self.assertEqual(estimate_required_credits("omni-flash-t2v", "6s"), 10)
        self.assertEqual(estimate_required_credits("omni-flash-r2v", "8s"), 12)
        self.assertEqual(estimate_required_credits("abra_r2v", "10s"), 15)
        self.assertEqual(estimate_required_credits("abra_t2v", 4), 7)

    def test_veo_lite_fast_quality(self):
        self.assertEqual(estimate_required_credits("veo_3_1_t2v_lite", "4s"), 10)
        self.assertEqual(estimate_required_credits("veo_3_1_i2v_lite", "10s"), 10)
        self.assertEqual(estimate_required_credits("veo_3_1_interpolation_lite", "4s"), 10)
        self.assertEqual(estimate_required_credits("veo_3_1_t2v_fast", "4s"), 20)
        self.assertEqual(estimate_required_credits("veo_3_1_i2v_s_fast", "8s"), 20)
        self.assertEqual(estimate_required_credits("veo_3_1_r2v_fast", "10s"), 20)
        self.assertEqual(estimate_required_credits("veo_3_1_quality", "6s"), 100)

    def test_unknown_model_or_duration_returns_zero(self):
        self.assertEqual(estimate_required_credits(None, "4s"), 0)
        self.assertEqual(estimate_required_credits("gemini-3.0-pro-image", "4s"), 0)
        self.assertEqual(estimate_required_credits("abra_t2v", None), 0)
        self.assertEqual(estimate_required_credits("abra_t2v", "12s"), 0)


if __name__ == "__main__":
    unittest.main()
