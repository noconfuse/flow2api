import unittest
from datetime import datetime, timedelta, timezone

from src.core.config import config
from src.core.models import Token
from src.services.token_manager import TokenManager


class _FakeDB:
    def __init__(self, tokens):
        self.tokens = {token.id: token for token in tokens}
        self.updated = []

    async def get_active_tokens(self):
        return [token for token in self.tokens.values() if token.is_active]

    async def get_all_tokens(self):
        return list(self.tokens.values())

    async def get_token(self, token_id):
        return self.tokens.get(token_id)

    async def update_token(self, token_id, **fields):
        token = self.tokens[token_id]
        for key, value in fields.items():
            setattr(token, key, value)
        self.updated.append((token_id, fields))


class _FakeFlowClient:
    def __init__(self, credits_by_at, failing_ats=None, st_to_at_results=None):
        self.credits_by_at = credits_by_at
        self.failing_ats = set(failing_ats or [])
        self.st_to_at_results = dict(st_to_at_results or {})

    async def get_credits(self, at):
        if at in self.failing_ats:
            raise RuntimeError("credit lookup failed")
        return self.credits_by_at[at]

    async def st_to_at(self, st):
        return self.st_to_at_results[st]


class TokenManagerCreditRefreshTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._original_exhausted = config.exhausted_credit_threshold
        config._config.setdefault("flow", {})["exhausted_credit_threshold"] = 0

    def tearDown(self):
        config._config.setdefault("flow", {})["exhausted_credit_threshold"] = self._original_exhausted

    async def test_refresh_active_tokens_credits_auto_disables_exhausted_token(self):
        exhausted = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="a@example.com",
            name="a",
            credits=0,
            is_active=True,
        )
        healthy = Token(
            id=2,
            st="st-2",
            at="at-2",
            email="b@example.com",
            name="b",
            credits=50,
            is_active=True,
        )
        db = _FakeDB([exhausted, healthy])
        flow_client = _FakeFlowClient(
            {
                "at-1": {"credits": 300, "userPaygateTier": "PAYGATE_TIER_ONE"},
                "at-2": {"credits": 80, "userPaygateTier": "PAYGATE_TIER_ONE"},
            }
        )
        manager = TokenManager(db, flow_client)

        async def _ensure_valid_token(token):
            return token

        manager.ensure_valid_token = _ensure_valid_token

        summary = await manager.refresh_active_tokens_credits()

        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["success"], 2)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["disabled"], 1)
        self.assertEqual(summary["reactivated"], 0)
        self.assertEqual(db.tokens[1].credits, 0)
        self.assertFalse(db.tokens[1].is_active)
        self.assertEqual(db.tokens[1].ban_reason, TokenManager.CREDIT_EXHAUSTED_BAN_REASON)
        self.assertTrue(any(item["auto_disabled"] for item in summary["tokens"]))

    async def test_refresh_active_tokens_credits_reactivates_recharged_token(self):
        exhausted = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="a@example.com",
            name="a",
            credits=0,
            is_active=False,
            ban_reason=TokenManager.CREDIT_EXHAUSTED_BAN_REASON,
        )
        db = _FakeDB([exhausted])
        flow_client = _FakeFlowClient(
            {"at-1": {"credits": 300, "userPaygateTier": "PAYGATE_TIER_ONE"}}
        )
        manager = TokenManager(db, flow_client)

        async def _ensure_valid_token(token):
            return token

        manager.ensure_valid_token = _ensure_valid_token

        summary = await manager.refresh_active_tokens_credits()

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["success"], 1)
        self.assertEqual(summary["failed"], 0)
        self.assertEqual(summary["disabled"], 0)
        self.assertEqual(summary["reactivated"], 1)
        self.assertTrue(db.tokens[1].is_active)
        self.assertIsNone(db.tokens[1].ban_reason)
        self.assertEqual(db.tokens[1].credits, 300)
        self.assertTrue(summary["tokens"][0]["reactivated"])

    async def test_refresh_active_tokens_credits_keeps_failure_visible(self):
        token = Token(
            id=1,
            st="st-1",
            at="at-1",
            email="a@example.com",
            name="a",
            credits=0,
            is_active=True,
        )
        db = _FakeDB([token])
        flow_client = _FakeFlowClient({}, failing_ats={"at-1"})
        manager = TokenManager(db, flow_client)

        async def _ensure_valid_token(token):
            return token

        manager.ensure_valid_token = _ensure_valid_token

        summary = await manager.refresh_active_tokens_credits()

        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["success"], 0)
        self.assertEqual(summary["failed"], 1)
        self.assertEqual(summary["disabled"], 0)
        self.assertEqual(summary["reactivated"], 0)
        self.assertIn("error", summary["tokens"][0])

    async def test_do_refresh_at_reactivates_token_after_at_refresh_failed_ban(self):
        token = Token(
            id=9,
            st="st-9",
            at="old-at",
            email="z@example.com",
            name="z",
            is_active=False,
            ban_reason=TokenManager.AT_REFRESH_FAILED_BAN_REASON,
        )
        db = _FakeDB([token])
        flow_client = _FakeFlowClient(
            {
                "new-at": {"credits": 120, "userPaygateTier": "PAYGATE_TIER_ONE"},
            },
            st_to_at_results={
                "st-9": {
                    "access_token": "new-at",
                    "expires": datetime(2026, 1, 1, tzinfo=timezone.utc).isoformat(),
                }
            },
        )
        manager = TokenManager(db, flow_client)

        refreshed = await manager._do_refresh_at(token.id, token.st)

        self.assertTrue(refreshed)
        self.assertTrue(db.tokens[9].is_active)
        self.assertIsNone(db.tokens[9].ban_reason)
        self.assertEqual(db.tokens[9].at, "new-at")
        self.assertTrue(any(fields.get("is_active") is True for _, fields in db.updated))

    async def test_clear_automation_risk_resets_runtime_flags(self):
        token = Token(
            id=11,
            st="st-11",
            at="at-11",
            email="risk@example.com",
            name="risk",
            is_active=True,
            automation_risk_score=2,
            automation_risk_state="cooldown",
            automation_cooldown_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            automation_last_risk_reason="submit_failed:abnormal_activity",
            automation_last_risk_at=datetime.now(timezone.utc),
        )
        db = _FakeDB([token])
        manager = TokenManager(db, _FakeFlowClient({}))

        result = await manager.clear_automation_risk(token.id)

        self.assertEqual(result["automation_risk_score"], 0)
        self.assertEqual(result["automation_risk_state"], "healthy")
        self.assertIsNone(db.tokens[11].automation_cooldown_until)
        self.assertIsNone(db.tokens[11].automation_last_risk_reason)
        self.assertIsNone(db.tokens[11].automation_last_risk_at)


if __name__ == "__main__":
    unittest.main()
