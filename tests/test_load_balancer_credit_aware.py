import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from src.core.config import config
from src.core.models import Token
from src.services.load_balancer import LoadBalancer


class _FakeTokenManager:
    def __init__(self, tokens):
        self._tokens = tokens

    async def get_active_tokens(self):
        return list(self._tokens)

    def needs_at_refresh(self, token):
        return False

    async def ensure_valid_token(self, token):
        return token


class LoadBalancerCreditAwareTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_captcha_method = config.captcha_method
        self._original_call_mode = config.call_logic_mode
        self._original_exhausted = config.exhausted_credit_threshold
        self._original_low = config.low_credit_threshold
        config.set_captcha_method("yescaptcha")
        config.set_call_logic_mode("default")
        config._config.setdefault("flow", {})["exhausted_credit_threshold"] = 0
        config._config.setdefault("flow", {})["low_credit_threshold"] = 100

    def tearDown(self):
        config.set_captcha_method(self._original_captcha_method)
        config.set_call_logic_mode(self._original_call_mode)
        config._config.setdefault("flow", {})["exhausted_credit_threshold"] = self._original_exhausted
        config._config.setdefault("flow", {})["low_credit_threshold"] = self._original_low

    async def test_select_token_skips_exhausted_credit_accounts(self):
        exhausted = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="exhausted@example.com",
            name="exhausted",
            credits=0,
        )
        healthy = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="healthy@example.com",
            name="healthy",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([exhausted, healthy]))

        selected = await balancer.select_token(for_image_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, healthy.id)

    async def test_select_token_deprioritizes_low_credit_accounts(self):
        low_credit = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="low@example.com",
            name="low",
            credits=80,
        )
        healthy = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="healthy@example.com",
            name="healthy",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([low_credit, healthy]))

        selected = await balancer.select_token(for_image_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, healthy.id)

    async def test_unavailable_reason_mentions_exhausted_credits(self):
        exhausted = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="exhausted@example.com",
            name="exhausted",
            credits=0,
        )
        balancer = LoadBalancer(_FakeTokenManager([exhausted]))

        reason = await balancer.get_unavailable_reason(for_image_generation=True)

        self.assertIn("余额已全部耗尽", reason)

    async def test_select_token_honors_preferred_token_id(self):
        preferred = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="preferred@example.com",
            name="preferred",
            credits=80,
        )
        fallback = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="fallback@example.com",
            name="fallback",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([preferred, fallback]))

        selected = await balancer.select_token(
            for_video_generation=True,
            preferred_token_id=preferred.id,
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, preferred.id)

    async def test_select_token_does_not_fallback_when_preferred_missing(self):
        fallback = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="fallback@example.com",
            name="fallback",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([fallback]))

        selected = await balancer.select_token(
            for_video_generation=True,
            preferred_token_id=999,
        )

        self.assertIsNone(selected)

    async def test_select_token_skips_locally_excluded_tokens(self):
        primary = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="primary@example.com",
            name="primary",
            credits=300,
        )
        excluded = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="excluded@example.com",
            name="excluded",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([primary, excluded]))

        selected = await balancer.select_token(
            for_video_generation=True,
            excluded_token_ids=[excluded.id],
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, primary.id)

    async def test_unavailable_reason_mentions_all_tokens_excluded_for_retry(self):
        token = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="only@example.com",
            name="only",
            credits=500,
        )
        balancer = LoadBalancer(_FakeTokenManager([token]))

        reason = await balancer.get_unavailable_reason(
            for_video_generation=True,
            excluded_token_ids=[token.id],
        )

        self.assertIn("已排除全部已失败账号", reason)

    async def test_select_token_prefers_healthy_over_cooldown_for_video_extension(self):
        config.set_captcha_method("extension")
        cooling = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="cooling@example.com",
            name="cooling",
            credits=500,
            video_enabled=True,
            automation_risk_score=2,
            automation_risk_state="cooldown",
            automation_cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        healthy = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="healthy@example.com",
            name="healthy",
            credits=500,
            video_enabled=True,
            automation_risk_score=0,
            automation_risk_state="healthy",
        )
        balancer = LoadBalancer(_FakeTokenManager([cooling, healthy]))
        balancer._build_extension_worker_snapshot = AsyncMock(return_value=None)
        balancer._check_extension_binding_snapshot = AsyncMock(return_value=(True, ""))

        selected = await balancer.select_token(for_video_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, healthy.id)

    async def test_select_token_allows_cooldown_token_when_no_healthier_video_token_exists(self):
        config.set_captcha_method("extension")
        cooling = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="cooling@example.com",
            name="cooling",
            credits=500,
            video_enabled=True,
            automation_risk_score=2,
            automation_risk_state="cooldown",
            automation_cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=30),
        )
        balancer = LoadBalancer(_FakeTokenManager([cooling]))
        balancer._build_extension_worker_snapshot = AsyncMock(return_value=None)
        balancer._check_extension_binding_snapshot = AsyncMock(return_value=(True, ""))

        selected = await balancer.select_token(for_video_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, cooling.id)

    async def test_select_token_allows_legacy_blocked_token_as_last_resort(self):
        config.set_captcha_method("extension")
        legacy_blocked = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="legacy@example.com",
            name="legacy",
            credits=500,
            video_enabled=True,
            automation_risk_score=3,
            automation_risk_state="blocked",
        )
        balancer = LoadBalancer(_FakeTokenManager([legacy_blocked]))
        balancer._build_extension_worker_snapshot = AsyncMock(return_value=None)
        balancer._check_extension_binding_snapshot = AsyncMock(return_value=(True, ""))

        selected = await balancer.select_token(for_video_generation=True)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.id, legacy_blocked.id)


if __name__ == "__main__":
    unittest.main()
