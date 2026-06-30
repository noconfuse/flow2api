import unittest
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

fastapi_stub = types.ModuleType("fastapi")
fastapi_stub.WebSocket = object
sys.modules.setdefault("fastapi", fastapi_stub)

from src.services.browser_captcha_extension import ExtensionCaptchaService


class _FakeDb:
    def __init__(self):
        self.create_worker_job = AsyncMock()
        self.update_worker_job = AsyncMock()


class BrowserUiRelaunchGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_dispatch_ui_job_skips_force_relaunch_after_recent_auto_launch(self):
        service = ExtensionCaptchaService(db=_FakeDb())
        service._resolve_dispatch_target_with_auto_launch = AsyncMock(
            return_value=(
                SimpleNamespace(client_label="chrome-token6"),
                "token6-worker",
                {"profile_id": "token-6", "slot_id": "token6-worker"},
            )
        )
        service._dispatch_request = AsyncMock(return_value={"status": "success"})

        with patch(
            "src.services.browser_profile_runtime.was_token_browser_recently_launched",
            return_value=True,
        ), patch(
            "src.services.browser_profile_runtime.get_recent_token_browser_launch_age",
            return_value=1.25,
        ), patch(
            "src.services.browser_profile_runtime.ensure_token_browser_ready",
            new=AsyncMock(return_value={"success": True}),
        ) as ensure_ready:
            result = await service.dispatch_ui_job(
                token_id=6,
                job_type="video_ui_workflow",
                project_id="project-1",
                payload={"foo": "bar"},
            )

        self.assertEqual(result["status"], "success")
        ensure_ready.assert_not_awaited()

    async def test_dispatch_ui_job_force_relaunches_when_auto_launch_is_not_recent(self):
        service = ExtensionCaptchaService(db=_FakeDb())
        service._resolve_dispatch_target_with_auto_launch = AsyncMock(
            return_value=(
                SimpleNamespace(client_label="chrome-token6"),
                "token6-worker",
                {"profile_id": "token-6", "slot_id": "token6-worker"},
            )
        )
        service._dispatch_request = AsyncMock(return_value={"status": "success"})

        with patch(
            "src.services.browser_profile_runtime.was_token_browser_recently_launched",
            return_value=False,
        ), patch(
            "src.services.browser_profile_runtime.get_recent_token_browser_launch_age",
            return_value=None,
        ), patch(
            "src.services.browser_profile_runtime.ensure_token_browser_ready",
            new=AsyncMock(return_value={"success": True}),
        ) as ensure_ready:
            result = await service.dispatch_ui_job(
                token_id=6,
                job_type="video_ui_workflow",
                project_id="project-1",
                payload={"foo": "bar"},
            )

        self.assertEqual(result["status"], "success")
        ensure_ready.assert_awaited_once_with(
            service.db,
            token_id=6,
            force_relaunch=True,
        )


if __name__ == "__main__":
    unittest.main()
