import itertools
import json
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from src.services.browser_captcha_personal import (
    BrowserCaptchaService,
    ResidentTabInfo,
    _PersonalBrowserPoolService,
    _build_personal_browser_args,
    _detect_real_browser_executable_path,
    _resolve_browser_executable_path,
    _tune_personal_browser_args_for_desktop_real_browser,
    _patch_nodriver_connection_instance,
)


class _FakeTab:
    def __init__(self, result):
        self._result = result

    async def evaluate(self, expression, await_promise=False, return_by_value=False):
        return self._result


class _FakeNavigableTab:
    def __init__(self, *, current_url="", ready_state="complete", get_result=None):
        self.current_url = current_url
        self.ready_state = ready_state
        self.get_result = get_result
        self.get_calls = []

    async def get(self, url):
        self.get_calls.append(url)
        return self.get_result

    async def evaluate(self, expression, await_promise=False, return_by_value=False):
        if expression == "location.href || ''":
            return self.current_url
        if expression == "document.readyState":
            return self.ready_state
        return None


class _ClosableFakeTab:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True

    async def sleep(self, _seconds):
        return None


class _FakeWebSocket:
    def __init__(self, owner):
        self.owner = owner
        self.close_code = None
        self.messages = []

    async def send(self, message):
        self.messages.append(message)
        payload = json.loads(message)
        transaction = self.owner.mapper[payload["id"]]
        transaction(result={"ok": True})


class _ConnectionWithoutClosed:
    def __init__(self):
        self.mapper = {}
        self.handlers = {}
        self.websocket = None
        self.connect_count = 0
        self.register_count = 0
        self.__count__ = itertools.count(0)

    async def send(self, _cdp_obj, _is_update=False):
        raise AssertionError("original send should be patched")

    async def connect(self):
        self.connect_count += 1
        self.websocket = _FakeWebSocket(self)

    async def _register_handlers(self):
        self.register_count += 1


def _fake_cdp_command():
    result = yield {"method": "Runtime.evaluate", "params": {}}
    return result


class BrowserCaptchaPersonalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()

    @staticmethod
    def _make_remote_object_result(token: str):
        return types.SimpleNamespace(
            type_="object",
            value=None,
            deep_serialized_value=types.SimpleNamespace(
                type_="object",
                value=[
                    ["ok", {"type": "boolean", "value": True}],
                    ["token", {"type": "string", "value": token}],
                ],
            ),
        )

    async def test_tab_evaluate_normalizes_deep_serialized_remote_object(self):
        tab = _FakeTab(self._make_remote_object_result("token-123"))

        result = await self.service._tab_evaluate(
            tab,
            "ignored",
            label="unit_test_tab_evaluate",
            await_promise=True,
            return_by_value=True,
        )

        self.assertEqual(result, {"ok": True, "token": "token-123"})

    async def test_execute_recaptcha_on_tab_accepts_remote_object_success_result(self):
        tab = _FakeTab(self._make_remote_object_result("token-xyz"))

        token = await self.service._execute_recaptcha_on_tab(tab, action="IMAGE_GENERATION")

        self.assertEqual(token, "token-xyz")

    async def test_tab_get_raises_when_browser_lands_on_chrome_error_page(self):
        tab = _FakeNavigableTab(current_url="chrome-error://chromewebdata/")

        with self.assertRaises(RuntimeError):
            await self.service._tab_get(
                tab,
                "https://www.google.com/",
                label="unit_test_tab_get_error_page",
                timeout_seconds=1.0,
            )

        self.assertFalse(self.service._last_health_probe_ok)

    async def test_probe_browser_runtime_fails_when_main_tab_is_chrome_error_page(self):
        self.service.browser = types.SimpleNamespace(
            connection=types.SimpleNamespace(send=AsyncMock(return_value={"ok": True})),
            main_tab=_FakeNavigableTab(current_url="chrome-error://chromewebdata/"),
        )
        self.service._last_health_probe_at = 0.0

        fake_nodriver = types.SimpleNamespace(
            cdp=types.SimpleNamespace(browser=types.SimpleNamespace(get_version=lambda: object()))
        )
        with patch.dict(sys.modules, {"nodriver": fake_nodriver}):
            healthy = await self.service._probe_browser_runtime()

        self.assertFalse(healthy)
        self.assertFalse(self.service._last_health_probe_ok)

    async def test_create_resident_tab_returns_none_when_browser_missing(self):
        self.service.browser = None

        resident_info = await self.service._create_resident_tab("slot-1", project_id="project-1")

        self.assertIsNone(resident_info)

    async def test_close_clears_resident_tabs_when_warmup_task_attr_missing(self):
        tab = _ClosableFakeTab()
        self.service._resident_tabs["slot-1"] = ResidentTabInfo(tab=tab, slot_id="slot-1")
        if hasattr(self.service, "_resident_warmup_task"):
            delattr(self.service, "_resident_warmup_task")

        await self.service.close()

        self.assertEqual(self.service._resident_tabs, {})
        self.assertTrue(tab.closed)

    async def test_create_resident_tab_cleans_tab_when_initialization_fails(self):
        tab = _ClosableFakeTab()
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._create_isolated_context_tab = AsyncMock(return_value=(tab, "context-1"))
        self.service._tab_evaluate = AsyncMock(return_value="complete")
        self.service._apply_token_cookie_binding = AsyncMock(side_effect=RuntimeError("cookie failed"))
        self.service._dispose_browser_context_quietly = AsyncMock()
        self.service._close_tab_quietly = AsyncMock()

        resident_info = await self.service._create_resident_tab("slot-1", project_id="project-1")

        self.assertIsNone(resident_info)
        self.service._dispose_browser_context_quietly.assert_awaited_once_with("context-1")
        self.service._close_tab_quietly.assert_awaited_once_with(tab)

    async def test_restart_browser_for_project_reuses_recent_healthy_runtime(self):
        resident_info = ResidentTabInfo(tab=object(), slot_id="slot-1", project_id="project-1")
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._mark_runtime_restart()
        self.service._probe_browser_runtime = AsyncMock(return_value=True)
        self.service._ensure_resident_tab = AsyncMock(return_value=("slot-1", resident_info))
        self.service._restart_browser_for_project_unlocked = AsyncMock(return_value=True)

        result = await self.service._restart_browser_for_project("project-1")

        self.assertTrue(result)
        self.service._restart_browser_for_project_unlocked.assert_not_awaited()
        self.service._ensure_resident_tab.assert_awaited_once()

    async def test_get_fingerprint_prefers_resident_slot_snapshot(self):
        resident_info = ResidentTabInfo(tab=object(), slot_id="slot-1", project_id="project-1")
        resident_info.fingerprint = {"user_agent": "ua-1"}
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._remember_fingerprint({"user_agent": "ua-last"})

        fingerprint = await self.service.get_fingerprint("slot-1")

        self.assertEqual(fingerprint, {"user_agent": "ua-1"})

    async def test_submit_json_via_browser_uses_resident_tab_context(self):
        resident_info = ResidentTabInfo(tab=object(), slot_id="slot-1", project_id="project-1")
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._tab_get = AsyncMock()
        self.service._tab_evaluate = AsyncMock(
            side_effect=[
                "https://labs.google/fx",
                {
                    "ok": True,
                    "status": 200,
                    "text": '{"operation":"task-1"}',
                    "url": "https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
                },
            ]
        )

        result = await self.service.submit_json_via_browser(
            browser_ref="slot-1",
            url="https://aisandbox-pa.googleapis.com/v1/video:batchAsyncGenerateVideoText",
            headers={"Content-Type": "text/plain;charset=UTF-8"},
            payload={"requests": []},
            timeout_seconds=8,
            referer_url="https://labs.google/fx",
        )

        self.assertEqual(result, {"operation": "task-1"})
        self.service._tab_get.assert_not_awaited()

    async def test_pool_submit_json_via_browser_routes_to_slot_worker(self):
        pool = _PersonalBrowserPoolService()
        worker = types.SimpleNamespace(
            submit_json_via_browser=AsyncMock(return_value={"operation": "task-2"})
        )
        pool._workers = [worker]
        pool._ensure_workers = AsyncMock()

        result = await pool.submit_json_via_browser(
            browser_ref="b1-slot-1",
            url="https://example.com/video",
            headers={},
            payload={},
        )

        self.assertEqual(result, {"operation": "task-2"})
        worker.submit_json_via_browser.assert_awaited_once()

    async def test_wait_for_recaptcha_raises_on_runtime_disconnect(self):
        tab = _ClosableFakeTab()
        runtime_error = ConnectionRefusedError(1225, "远程计算机拒绝网络连接。")
        self.service._inject_recaptcha_bootstrap_script = AsyncMock(return_value="remote")
        self.service._tab_evaluate = AsyncMock(side_effect=runtime_error)

        with self.assertRaises(ConnectionRefusedError):
            await self.service._wait_for_recaptcha(tab)

        self.assertFalse(self.service._last_health_probe_ok)
        self.assertEqual(self.service._tab_evaluate.await_count, 1)

    async def test_force_fresh_flow_error_defers_sync_browser_restart_until_drain(self):
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service.browser = types.SimpleNamespace(stopped=False)
        self.service._initialized = True
        self.service._resident_tabs["slot-1"] = resident_info
        self.service._project_resident_affinity["project-1"] = "slot-1"
        self.service._token_resident_affinity["1"] = "slot-1"
        self.service._maybe_execute_pending_fresh_profile_restart = AsyncMock(return_value=False)
        self.service._restart_browser_for_project = AsyncMock(return_value=True)

        await self.service.report_flow_error(
            "project-1",
            "reCAPTCHA 验证失败",
            error_message="Flow API request failed: PUBLIC_ERROR_UNUSUAL_ACTIVITY: reCAPTCHA evaluation failed",
            token_id=1,
            slot_id="slot-1",
        )

        self.assertIn("slot-1", self.service._resident_unavailable_slots)
        self.assertTrue(self.service._fresh_profile_restart_pending)
        self.assertTrue(self.service._fresh_profile_restart_force_pending)
        self.service._restart_browser_for_project.assert_not_awaited()
        self.service._maybe_execute_pending_fresh_profile_restart.assert_awaited_once()

    async def test_pending_fresh_restart_task_is_preserved_during_runtime_shutdown(self):
        async def runner():
            self.service._fresh_profile_restart_task = asyncio.current_task()
            await self.service._cancel_background_runtime_tasks(reason="unit_test")
            self.assertIs(self.service._fresh_profile_restart_task, asyncio.current_task())

        import asyncio

        task = asyncio.create_task(runner())
        await task

    async def test_get_token_waits_for_pending_fresh_restart_before_resident_pick(self):
        import asyncio

        events = []
        tab = _ClosableFakeTab()
        resident_info = ResidentTabInfo(
            tab=tab,
            slot_id="slot-1",
            project_id="project-1",
            token_id=1,
        )
        resident_info.recaptcha_ready = True
        self.service._fresh_profile_restart_every_n_solves = 5
        self.service._fresh_profile_restart_pending = True
        self.service._fresh_profile_restart_pending_reason = "unit:5/5"
        self.service._has_active_browser_work = AsyncMock(return_value=False)

        async def restart_unlocked(project_id, token_id=None, *, fresh_profile=False):
            events.append("fresh_restart")
            self.assertEqual(project_id, "project-1")
            self.assertTrue(fresh_profile)
            self.service._reset_browser_rotation_budget()
            return True

        async def initialize():
            events.append("initialize")

        async def ensure_resident(*args, **kwargs):
            events.append("ensure_resident")
            return "slot-1", resident_info

        async def solve_resident(*args, **kwargs):
            events.append("solve_resident")
            return "token-1"

        self.service._restart_browser_for_project_unlocked = AsyncMock(side_effect=restart_unlocked)
        self.service.initialize = AsyncMock(side_effect=initialize)
        self.service._ensure_resident_tab = AsyncMock(side_effect=ensure_resident)
        self.service._ensure_resident_token_binding = AsyncMock(return_value=True)
        self.service._solve_with_resident_tab = AsyncMock(side_effect=solve_resident)

        token, slot_id = await self.service._get_token_direct(
            "project-1",
            token_id=1,
            return_slot_id=True,
        )

        self.assertEqual((token, slot_id), ("token-1", "slot-1"))
        self.assertEqual(events, ["fresh_restart", "initialize", "ensure_resident", "solve_resident"])
        self.assertFalse(self.service._fresh_profile_restart_pending)
        self.assertIsNone(self.service._fresh_profile_restart_task)

    async def test_wait_for_pending_fresh_restart_awaits_existing_task(self):
        import asyncio

        events = []
        self.service._fresh_profile_restart_pending = True

        async def restart_task():
            events.append("restart_start")
            await asyncio.sleep(0.01)
            self.service._fresh_profile_restart_pending = False
            events.append("restart_done")
            return True

        task = asyncio.create_task(restart_task())
        self.service._fresh_profile_restart_task = task

        result = await self.service._wait_for_pending_fresh_profile_restart_before_solve(
            "project-1",
            token_id=1,
            source="unit_test",
        )

        self.assertTrue(result)
        self.assertEqual(events, ["restart_start", "restart_done"])
        self.assertTrue(task.done())

    async def test_runtime_surface_profile_contains_extended_browser_environment(self):
        profile = self.service._get_runtime_surface_profile()

        self.assertIn("webgpu", profile)
        self.assertIn("mediaQueries", profile)
        self.assertIn("storage", profile)
        self.assertIn("behavior", profile)
        self.assertIn("visualViewport", profile["window"])
        self.assertIn("supportedExtensions", profile["graphics"])
        self.assertIn("WEBGL_debug_renderer_info", profile["graphics"]["supportedExtensions"])

        source = self.service._build_tab_fingerprint_spoof_source(types.SimpleNamespace(target_id="unit-tab"))
        for marker in (
            "ensureWebGpuEnvironment",
            "ensureMatchMediaEnvironment",
            "ensureVisualViewportEnvironment",
            "navigator.storage",
            "getSupportedConstraints",
            "userActivation",
        ):
            self.assertIn(marker, source)

    async def test_pool_tab_limits_use_browser_count_times_per_worker_tabs(self):
        pool = _PersonalBrowserPoolService()

        self.assertEqual(pool._build_worker_tab_limits(5, 10), [5] * 10)

        capped_limits = pool._build_worker_tab_limits(5, 20)
        self.assertEqual(len(capped_limits), 20)
        self.assertEqual(sum(capped_limits), 50)
        self.assertLessEqual(max(capped_limits), 5)

        warmup_limits = pool._build_worker_tab_limits(
            5,
            10,
            total_limit=5,
            allow_zero=True,
        )
        self.assertEqual(len(warmup_limits), 10)
        self.assertEqual(sum(warmup_limits), 5)
        self.assertEqual(sum(1 for item in warmup_limits if item > 0), 5)

    async def test_pool_dispatch_prefers_cold_idle_worker_over_busy_live_worker(self):
        pool = _PersonalBrowserPoolService()
        live_worker = BrowserCaptchaService(browser_instance_id=1, max_resident_tabs_override=5)
        cold_worker = BrowserCaptchaService(browser_instance_id=2, max_resident_tabs_override=5)
        live_worker._initialized = True
        live_worker.browser = types.SimpleNamespace(stopped=False)
        pool._workers = [live_worker, cold_worker]
        pool._worker_dispatch_reservations = {0: 1}

        self.assertLess(
            pool._worker_dispatch_score(1, cold_worker),
            pool._worker_dispatch_score(0, live_worker),
        )

    async def test_nodriver_send_patch_handles_connection_without_closed_attr(self):
        connection = _ConnectionWithoutClosed()

        _patch_nodriver_connection_instance(connection)
        result = await connection.send(_fake_cdp_command())

        self.assertEqual(result, {"ok": True})
        self.assertEqual(connection.connect_count, 1)
        self.assertEqual(connection.register_count, 1)
        self.assertTrue(getattr(connection, "_flow2api_send_patched", False))

    async def test_build_personal_browser_args_uses_incognito_for_ephemeral_profile(self):
        args = _build_personal_browser_args(headless=True)

        self.assertIn("--bwsi", args)
        self.assertIn("--incognito", args)
        self.assertIn("--disable-extensions", args)
        self.assertNotIn("--profile-directory=Default", args)

    async def test_build_personal_browser_args_preserves_trusted_profile_state(self):
        args = _build_personal_browser_args(
            headless=False,
            preserve_profile_state=True,
            profile_directory="Profile 1",
        )

        self.assertIn("--profile-directory=Profile 1", args)
        self.assertNotIn("--bwsi", args)
        self.assertNotIn("--incognito", args)
        self.assertNotIn("--disable-extensions", args)

    async def test_build_personal_browser_args_keeps_extensions_when_proxy_extension_loaded(self):
        args = _build_personal_browser_args(
            headless=False,
            proxy_extension_dir="/tmp/proxy-ext",
            preserve_profile_state=False,
        )

        self.assertIn("--load-extension=/tmp/proxy-ext", args)
        self.assertNotIn("--bwsi", args)
        self.assertNotIn("--incognito", args)
        self.assertNotIn("--disable-extensions", args)

    async def test_tune_personal_browser_args_for_desktop_real_browser_removes_risky_network_flags(self):
        args = _tune_personal_browser_args_for_desktop_real_browser(
            [
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-domain-reliability",
                "--disable-sync",
                "--bwsi",
                "--incognito",
                "--disable-extensions",
                "--disable-features=UseDnsHttpsSvcb,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,GlobalMediaControls",
                "--window-position=3000,3000",
                "--window-size=1280,720",
                "--no-first-run",
            ]
        )

        self.assertNotIn("--disable-background-networking", args)
        self.assertNotIn("--disable-component-update", args)
        self.assertNotIn("--disable-domain-reliability", args)
        self.assertNotIn("--disable-sync", args)
        self.assertNotIn("--bwsi", args)
        self.assertNotIn("--incognito", args)
        self.assertNotIn("--disable-extensions", args)
        self.assertNotIn(
            "--disable-features=UseDnsHttpsSvcb,OptimizationHints,AutofillServerCommunication,CertificateTransparencyComponentUpdater,MediaRouter,GlobalMediaControls",
            args,
        )
        self.assertIn("--window-size=1366,768", args)
        self.assertIn("--window-position=80,80", args)
        self.assertIn("--no-first-run", args)

    def test_detect_real_browser_executable_path_supports_macos_google_chrome(self):
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with patch("src.services.browser_captcha_personal.sys.platform", "darwin"), patch(
            "src.services.browser_captcha_personal.os.path.exists",
            side_effect=lambda path: path == chrome_path,
        ):
            self.assertEqual(_detect_real_browser_executable_path(), chrome_path)

    def test_resolve_browser_executable_path_auto_detects_macos_google_chrome(self):
        chrome_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        with patch.dict(os.environ, {}, clear=False), patch(
            "src.services.browser_captcha_personal._detect_real_browser_executable_path",
            return_value=chrome_path,
        ):
            self.assertEqual(
                _resolve_browser_executable_path(),
                (chrome_path, "detected"),
            )

    async def test_load_token_cookie_falls_back_to_st_when_cookie_column_missing(self):
        self.service.db = types.SimpleNamespace(
            get_token=AsyncMock(
                return_value=types.SimpleNamespace(
                    st="st-value-123",
                )
            )
        )

        cookie_text = await self.service._load_token_cookie(5)

        self.assertEqual(cookie_text, "__Secure-next-auth.session-token=st-value-123")

    async def test_set_browser_cookie_targets_uses_cookie_jar_when_connection_missing(self):
        cookie_jar = types.SimpleNamespace(set_all=AsyncMock(return_value=None))
        self.service.browser = types.SimpleNamespace(connection=None, cookies=cookie_jar)
        fake_nodriver = types.SimpleNamespace(
            cdp=types.SimpleNamespace(
                network=types.SimpleNamespace(
                    CookieParam=lambda **kwargs: kwargs,
                )
            )
        )

        with patch.dict(sys.modules, {"nodriver": fake_nodriver}):
            cookie_count = await self.service._set_browser_cookie_targets(
                [
                    {
                        "name": "__Secure-next-auth.session-token",
                        "value": "st-value-123",
                        "url": "https://labs.google/",
                        "secure": True,
                    }
                ],
                label="unit_cookie_jar_fallback",
                browser_context_id="context-1",
            )

        self.assertEqual(cookie_count, 1)
        cookie_jar.set_all.assert_awaited_once()

    async def test_resolve_personal_proxy_skips_unreachable_configured_proxy(self):
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(
                return_value=types.SimpleNamespace(
                    browser_proxy_pool="",
                    browser_proxy_enabled=True,
                    browser_proxy_url="http://127.0.0.1:65535",
                )
            ),
            get_proxy_config=AsyncMock(
                return_value=types.SimpleNamespace(
                    enabled=False,
                    proxy_pool="",
                    proxy_url="",
                )
            ),
            pick_browser_proxy_from_pool=AsyncMock(return_value=None),
        )
        self.service._is_tcp_endpoint_reachable = AsyncMock(return_value=False)

        proxy_tuple = await self.service._resolve_personal_proxy()

        self.assertEqual(proxy_tuple, (None, None, None, None, None))
        self.service._is_tcp_endpoint_reachable.assert_awaited_once()

    async def test_resolve_personal_proxy_honors_disable_env(self):
        self.service.db = types.SimpleNamespace(
            get_captcha_config=AsyncMock(),
            get_proxy_config=AsyncMock(),
            pick_browser_proxy_from_pool=AsyncMock(),
        )

        with patch.dict(os.environ, {"PERSONAL_BROWSER_DISABLE_PROXY": "1"}, clear=False):
            proxy_tuple = await self.service._resolve_personal_proxy()

        self.assertEqual(proxy_tuple, (None, None, None, None, None))
        self.service.db.get_captcha_config.assert_not_awaited()
        self.service.db.get_proxy_config.assert_not_awaited()

    async def test_resolve_user_data_dir_clones_trusted_profile_source(self):
        with tempfile.TemporaryDirectory() as source_root, tempfile.TemporaryDirectory() as runtime_root:
            source_root_path = Path(source_root)
            profile_dir = source_root_path / "Default"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (source_root_path / "Local State").write_text('{"ok": true}', encoding="utf-8")
            sqlite_path = profile_dir / "Cookies"
            conn = sqlite3.connect(str(sqlite_path))
            try:
                conn.execute("create table sample(key text, value text)")
                conn.execute("insert into sample(key, value) values (?, ?)", ("token", "abc"))
                conn.commit()
            finally:
                conn.close()

            with patch.dict(
                os.environ,
                {
                    "PERSONAL_BROWSER_PROFILE_SOURCE_DIR": source_root,
                    "PERSONAL_BROWSER_PROFILE_DIRECTORY": "Default",
                },
                clear=False,
            ), patch(
                "src.services.browser_captcha_personal.PERSONAL_RUNTIME_TMP_DIR",
                Path(runtime_root),
            ):
                service = BrowserCaptchaService()
                try:
                    cloned_root = Path(service.user_data_dir)
                    self.assertTrue(cloned_root.exists())
                    self.assertEqual(service._effective_profile_directory_name, "Default")
                    self.assertTrue((cloned_root / "Local State").exists())
                    cloned_db = cloned_root / "Default" / "Cookies"
                    self.assertTrue(cloned_db.exists())
                    clone_conn = sqlite3.connect(str(cloned_db))
                    try:
                        row = clone_conn.execute(
                            "select value from sample where key = ?",
                            ("token",),
                        ).fetchone()
                    finally:
                        clone_conn.close()
                    self.assertEqual(row, ("abc",))
                finally:
                    await service.close()


if __name__ == "__main__":
    unittest.main()
