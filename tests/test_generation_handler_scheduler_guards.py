import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.core.models import Token
from src.services.generation_handler import GenerationHandler


class GenerationHandlerSchedulerGuardTests(unittest.IsolatedAsyncioTestCase):
    def _build_handler(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler._update_request_log_progress = AsyncMock()
        return handler

    async def test_acquire_generation_slot_uses_hard_slot_and_releases_pending(self):
        handler = self._build_handler()
        handler.concurrency_manager = SimpleNamespace(
            wait_acquire_image=AsyncMock(return_value=(True, 12)),
            wait_acquire_video=AsyncMock(return_value=(True, 0)),
            release_image=AsyncMock(),
            release_video=AsyncMock(),
        )
        handler.load_balancer = SimpleNamespace(release_pending=AsyncMock())

        token = Token(id=1, st="st-1", at="at-1", email="a@example.com", name="a")
        pending_state = {"active": True}
        perf_trace = {}
        slot_state = {}

        acquired, waited_ms = await handler._acquire_generation_slot(
            token=token,
            generation_type="image",
            request_log_state={"id": 1},
            pending_token_state=pending_state,
            perf_trace=perf_trace,
            slot_state=slot_state,
        )

        self.assertTrue(acquired)
        self.assertEqual(waited_ms, 12)
        self.assertFalse(pending_state["active"])
        self.assertEqual(perf_trace["image_generation"]["slot_wait_ms"], 12)
        self.assertTrue(slot_state["active"])
        self.assertEqual(slot_state["token_id"], token.id)
        handler.load_balancer.release_pending.assert_awaited_once_with(
            token.id,
            for_image_generation=True,
            for_video_generation=False,
        )

        await handler._release_generation_slot(slot_state)
        handler.concurrency_manager.release_image.assert_awaited_once_with(token.id)
        self.assertFalse(slot_state["active"])

    async def test_handle_token_failure_bans_rate_limited_tokens_immediately(self):
        handler = self._build_handler()
        handler.token_manager = SimpleNamespace(
            ban_token_for_429=AsyncMock(),
            record_error=AsyncMock(),
        )

        token = Token(id=2, st="st-2", at="at-2", email="b@example.com", name="b")

        await handler._handle_token_failure(token, "RESOURCE_EXHAUSTED: 429 Too Many Requests")

        handler.token_manager.ban_token_for_429.assert_awaited_once_with(token.id)
        handler.token_manager.record_error.assert_not_awaited()

    async def test_handle_token_failure_records_normal_errors(self):
        handler = self._build_handler()
        handler.token_manager = SimpleNamespace(
            ban_token_for_429=AsyncMock(),
            record_error=AsyncMock(),
        )

        token = Token(id=3, st="st-3", at="at-3", email="c@example.com", name="c")

        await handler._handle_token_failure(token, "generation failed: upstream 500")

        handler.token_manager.record_error.assert_awaited_once_with(token.id)
        handler.token_manager.ban_token_for_429.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
