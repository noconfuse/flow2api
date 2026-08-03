from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

from ..core.logger import debug_logger
from .browser_profile_bootstrap import (
    make_route_key,
    make_spec,
    prepare_and_optionally_launch,
)
from .proxy_pool_service import ProxyPoolService
from .proxy_manager import ProxyManager


_launch_guard = asyncio.Lock()
_launch_locks: dict[int, asyncio.Lock] = {}
_recent_successful_launches: dict[int, float] = {}
RECENT_BROWSER_LAUNCH_REUSE_WINDOW_SECONDS = 20.0


async def _get_launch_lock(token_id: int) -> asyncio.Lock:
    async with _launch_guard:
        lock = _launch_locks.get(int(token_id))
        if lock is None:
            lock = asyncio.Lock()
            _launch_locks[int(token_id)] = lock
        return lock


def _binding_matches_token(binding: Optional[Dict[str, Any]], token_email: str) -> bool:
    if not binding:
        return False
    slot_id = str(binding.get("slot_id") or "").strip()
    if not slot_id:
        return False
    current_email = str(binding.get("current_email") or "").strip().lower()
    normalized_token_email = str(token_email or "").strip().lower()
    if current_email and normalized_token_email and current_email != normalized_token_email:
        return False
    return True


def _mark_recent_successful_launch(token_id: int) -> None:
    _recent_successful_launches[int(token_id)] = time.monotonic()


def get_recent_token_browser_launch_age(token_id: int) -> Optional[float]:
    launched_at = _recent_successful_launches.get(int(token_id))
    if launched_at is None:
        return None
    age = time.monotonic() - launched_at
    if age < 0:
        _recent_successful_launches.pop(int(token_id), None)
        return None
    if age > max(RECENT_BROWSER_LAUNCH_REUSE_WINDOW_SECONDS * 3, 60.0):
        _recent_successful_launches.pop(int(token_id), None)
        return None
    return age


def was_token_browser_recently_launched(
    token_id: int,
    *,
    within_seconds: float = RECENT_BROWSER_LAUNCH_REUSE_WINDOW_SECONDS,
) -> bool:
    age = get_recent_token_browser_launch_age(int(token_id))
    return age is not None and age <= max(0.0, float(within_seconds or 0.0))


async def sync_extension_worker_views(db) -> tuple[Any, list[dict]]:
    await db.sync_browser_profiles_from_tokens()
    from .browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db)
    routes = await service.list_route_status()
    await db.sync_extension_routes_to_worker_slots(routes)
    return service, routes


async def _resolve_browser_proxy_args(
    db,
    token_id: int,
    pool_service: Optional[ProxyPoolService],
    *,
    excluded_entry_ids: Optional[List[int]] = None,
) -> List[str]:
    """Resolve Chrome --proxy-server arg for the given token.

    Priority:
      1. IP pool selection (stage 1)
      2. token.captcha_proxy_url (legacy per-token override)
      3. nothing (fall back to host network)
    """
    if pool_service is None:
        # Lazy-import to avoid circular import at module load time.
        # `proxy_pool_service` is a module-level singleton installed by
        # `src.api.admin.set_dependencies` during app startup. The previous
        # `from .main import proxy_pool_service` was wrong because there is no
        # `src/services/main.py` module, causing `No module named 'src.services.main'`.
        try:
            from ..api.admin import proxy_pool_service as global_pool_service
        except Exception as exc:  # pragma: no cover - defensive
            debug_logger.log_warning(
                f"[BROWSER_PROFILE_RUNTIME] 读取全局 proxy_pool_service 失败: {exc}"
            )
            global_pool_service = None
        pool_service = global_pool_service

    if pool_service is not None:
        try:
            proxy_url, _info = await pool_service.resolve_orchestrator_proxy_url(
                token_id,
                excluded_entry_ids=excluded_entry_ids,
            )
            if proxy_url:
                return [f"--proxy-server={proxy_url}"]
        except Exception as exc:
            debug_logger.log_warning(
                f"[BROWSER_PROFILE_RUNTIME] proxy pool resolve failed for token {token_id}: {exc}"
            )

    try:
        token = await db.get_token(int(token_id))
        legacy_proxy = str(getattr(token, "captcha_proxy_url", "") or "").strip() if token else ""
        if legacy_proxy:
            return [f"--proxy-server={legacy_proxy}"]
    except Exception:
        pass

    return []


async def ensure_token_browser_ready(
    db,
    *,
    token_id: int,
    wait_timeout: float = 25.0,
    chrome_path: Optional[str] = None,
    force_relaunch: bool = False,
    pool_service: Optional[ProxyPoolService] = None,
    excluded_proxy_entry_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    token = await db.get_token(int(token_id))
    if not token:
        return {
            "success": False,
            "attempted_launch": False,
            "error": f"token 不存在: {token_id}",
        }

    route_key = make_route_key(int(token.id), getattr(token, "extension_route_key", None))
    if str(getattr(token, "extension_route_key", "") or "").strip() != route_key:
        await db.update_token(int(token.id), extension_route_key=route_key)
        token.extension_route_key = route_key

    proxy_args = await _resolve_browser_proxy_args(
        db,
        token_id=int(token.id),
        pool_service=pool_service,
        excluded_entry_ids=excluded_proxy_entry_ids,
    )

    async with await _get_launch_lock(int(token.id)):
        service, _routes = await sync_extension_worker_views(db)
        binding = await db.get_token_worker_binding(int(token.id))
        route_ok, resolved_route_key, route_error, snapshot = await service.validate_connection_for_token(int(token.id))
        if route_ok and _binding_matches_token(binding, getattr(token, "email", "")) and not force_relaunch:
            return {
                "success": True,
                "attempted_launch": False,
                "already_online": True,
                "token_id": int(token.id),
                "route_key": resolved_route_key or route_key,
                "binding": binding,
                "snapshot": snapshot,
                "proxy_args": proxy_args,
            }

        await db.clear_worker_slots_for_profile(f"token-{int(token.id)}")
        spec = make_spec(
            token_id=int(token.id),
            email=str(getattr(token, "email", "") or "").strip(),
            route_key=route_key,
            current_project_id=str(getattr(token, "current_project_id", "") or "").strip(),
        )

        try:
            launch_result = await asyncio.to_thread(
                prepare_and_optionally_launch,
                spec,
                launch=True,
                chrome_path=str(chrome_path or "").strip() or None,
                extra_args=proxy_args or None,
            )
        except Exception as exc:
            return {
                "success": False,
                "attempted_launch": True,
                "token_id": int(token.id),
                "route_key": route_key,
                "error": str(exc),
                "proxy_args": proxy_args,
            }

        deadline = time.monotonic() + max(3.0, float(wait_timeout or 0))
        last_route_error = route_error
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            service, _routes = await sync_extension_worker_views(db)
            binding = await db.get_token_worker_binding(int(token.id))
            route_ok, resolved_route_key, route_error, snapshot = await service.validate_connection_for_token(int(token.id))
            last_route_error = route_error or last_route_error
            if route_ok and _binding_matches_token(binding, getattr(token, "email", "")):
                _mark_recent_successful_launch(int(token.id))
                debug_logger.log_info(
                    f"[BROWSER_PROFILE_RUNTIME] token {token.id} browser auto-launch ready: "
                    f"route_key={resolved_route_key or route_key}, slot_id={binding.get('slot_id') if binding else ''}"
                )
                return {
                    "success": True,
                    "attempted_launch": True,
                    "already_online": False,
                    "token_id": int(token.id),
                    "route_key": resolved_route_key or route_key,
                    "binding": binding,
                    "snapshot": snapshot,
                    "launch_result": launch_result,
                "force_relaunch": bool(force_relaunch),
                }

        return {
            "success": False,
            "attempted_launch": True,
            "already_online": False,
            "token_id": int(token.id),
            "route_key": route_key,
            "binding": binding,
            "snapshot": snapshot,
            "launch_result": launch_result,
            "force_relaunch": bool(force_relaunch),
            "error": last_route_error or "浏览器已尝试启动，但在超时时间内没有等到在线 worker slot",
        }
