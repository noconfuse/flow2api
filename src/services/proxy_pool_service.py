"""IP pool: select a proxy entry for a token, track health, and report status.

This layer is the single source of truth for assigning an outbound proxy URL
to a token at runtime. It is intentionally kept separate from the legacy
`proxy_config` row so the existing single-proxy behavior continues to work
without any change for tokens that have no pool binding.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from urllib.parse import urlparse

from ..core.database import Database
from ..core.models import ProxyPool, ProxyPoolBinding, ProxyPoolEntry
from .proxy_manager import ProxyManager


# Map policy name -> async selector impl.
SelectorFn = Callable[[ProxyPool, List[ProxyPoolEntry], Optional[int]], Awaitable[Optional[ProxyPoolEntry]]]


@dataclass
class ProxySelection:
    entry: Optional[ProxyPoolEntry]
    pool: Optional[ProxyPool]
    reason: str = ""

    @property
    def has_proxy(self) -> bool:
        return bool(self.entry and self.entry.proxy_url)


class ProxyPoolService:
    """High-level IP pool orchestration on top of the DAO layer."""

    DEFAULT_HEALTH_TIMEOUT = 5.0
    DEFAULT_HEALTH_URL = "http://httpbin.org/ip"

    def __init__(self, db: Database, proxy_manager: ProxyManager):
        self.db = db
        self.proxy_manager = proxy_manager
        self._rr_counters: Dict[int, int] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Selector registry
    # ------------------------------------------------------------------ #
    async def _select_round_robin(
        self,
        pool: ProxyPool,
        entries: List[ProxyPoolEntry],
        token_id: Optional[int],
    ) -> Optional[ProxyPoolEntry]:
        if not entries:
            return None
        async with self._lock:
            index = self._rr_counters.get(pool.id, 0) % len(entries)
            self._rr_counters[pool.id] = index + 1
        return entries[index]

    async def _select_random(
        self,
        pool: ProxyPool,
        entries: List[ProxyPoolEntry],
        token_id: Optional[int],
    ) -> Optional[ProxyPoolEntry]:
        if not entries:
            return None
        import random

        return random.choice(entries)

    async def _select_least_used(
        self,
        pool: ProxyPool,
        entries: List[ProxyPoolEntry],
        token_id: Optional[int],
    ) -> Optional[ProxyPoolEntry]:
        if not entries:
            return None
        return sorted(entries, key=lambda item: (item.use_count or 0, item.id or 0))[0]

    async def _select_sticky(
        self,
        pool: ProxyPool,
        entries: List[ProxyPoolEntry],
        token_id: Optional[int],
    ) -> Optional[ProxyPoolEntry]:
        if not entries:
            return None
        binding = await self.db.get_proxy_pool_binding_for_token(token_id) if token_id else None
        if binding and binding.pinned_entry_id:
            for entry in entries:
                if entry.id == binding.pinned_entry_id:
                    return entry
        if entries:
            return entries[0]
        return None

    SELECTORS: Dict[str, SelectorFn] = {
        "round_robin": _select_round_robin,
        "random": _select_random,
        "least_used": _select_least_used,
        "sticky": _select_sticky,
    }

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    async def select_proxy_for_token(
        self,
        token_id: Optional[int],
        *,
        excluded_entry_ids: Optional[Sequence[int]] = None,
        purpose: str = "browser",
    ) -> ProxySelection:
        """Pick a proxy entry for the given token, respecting bindings and policy.

        Returns `ProxySelection` with `entry=None` if no pool applies.
        The caller is expected to fall back to the legacy single-proxy config
        (`ProxyManager.get_request_proxy_url`) when `has_proxy` is False.
        """
        if not token_id:
            return ProxySelection(entry=None, pool=None, reason="missing_token_id")

        binding = await self.db.get_proxy_pool_binding_for_token(token_id)
        if not binding:
            return ProxySelection(entry=None, pool=None, reason="no_binding")

        pool = await self.db.get_proxy_pool(binding.pool_id)
        if not pool or not pool.is_active:
            return ProxySelection(entry=None, pool=None, reason="pool_inactive")

        excluded = set(int(eid) for eid in (excluded_entry_ids or []) if eid)
        entries = await self.db.list_proxy_pool_entries(pool.id, active_only=True)
        candidate_entries = [
            entry for entry in entries
            if entry.health_status != "failed"
            and (entry.id is None or entry.id not in excluded)
        ]
        if not candidate_entries:
            return ProxySelection(entry=None, pool=pool, reason="no_available_entries")

        strategy = (pool.strategy or "round_robin").lower()
        selector = self.SELECTORS.get(strategy, self._select_round_robin)
        chosen = await selector(self, pool, candidate_entries, token_id)
        if not chosen:
            return ProxySelection(entry=None, pool=pool, reason="selector_returned_none")

        # Track usage without awaiting precise counters failure.
        await self.db.update_proxy_pool_entry(
            chosen.id,
            use_count=(chosen.use_count or 0) + 1,
            last_used_at=time.time(),
        )
        return ProxySelection(entry=chosen, pool=pool, reason="")

    async def report_result(
        self,
        entry: Optional[ProxyPoolEntry],
        success: bool,
        latency_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> None:
        if not entry or entry.id is None:
            return
        fields: Dict[str, Any] = {}
        if success:
            fields["success_count"] = (entry.success_count or 0) + 1
            fields["consecutive_failures"] = 0
            fields["health_status"] = "healthy"
            fields["last_error"] = None
        else:
            fields["failure_count"] = (entry.failure_count or 0) + 1
            fields["consecutive_failures"] = (entry.consecutive_failures or 0) + 1
            fields["last_error"] = (error_message or "")[:512]
            if fields["consecutive_failures"] >= 3:
                fields["health_status"] = "failed"
            elif fields["consecutive_failures"] >= 1:
                fields["health_status"] = "degraded"
        if latency_ms is not None:
            fields["latency_ms"] = int(latency_ms)
        await self.db.update_proxy_pool_entry(entry.id, **fields)

    async def resolve_orchestrator_proxy_url(
        self,
        token_id: Optional[int],
        *,
        excluded_entry_ids: Optional[Sequence[int]] = None,
    ) -> Tuple[Optional[str], Dict[str, Any]]:
        """Resolve the proxy URL the orchestrator should use for `token_id`.

        Returns `(proxy_url, debug_info)`. `proxy_url` may be None if no pool
        is configured (caller should fall back to legacy single-proxy config).
        """
        selection = await self.select_proxy_for_token(
            token_id,
            excluded_entry_ids=excluded_entry_ids,
        )
        info = {
            "selection_reason": selection.reason,
            "pool_id": selection.pool.id if selection.pool else None,
            "pool_name": selection.pool.name if selection.pool else None,
            "entry_id": selection.entry.id if selection.entry else None,
        }
        if selection.has_proxy and selection.entry:
            return selection.entry.proxy_url, info
        return None, info

    async def probe_pool_entry(
        self,
        entry: ProxyPoolEntry,
        *,
        timeout: float = DEFAULT_HEALTH_TIMEOUT,
    ) -> Tuple[bool, Optional[int], str]:
        """Run a simple HTTP probe via the entry's proxy URL.

        Returns `(success, latency_ms, error_message)`.
        """
        try:
            proxy_url = self.proxy_manager.normalize_proxy_url(entry.proxy_url)
        except ValueError as exc:
            return False, None, str(exc)
        if not proxy_url:
            return False, None, "invalid_proxy_url"

        try:
            import aiohttp
        except ImportError:
            return False, None, "aiohttp_not_available"

        parsed = urlparse(proxy_url)
        scheme = parsed.scheme or "http"
        target = self.DEFAULT_HEALTH_URL

        timeout_obj = aiohttp.ClientTimeout(total=timeout)
        start = time.time()
        try:
            async with aiohttp.ClientSession(timeout=timeout_obj) as session:
                async with session.get(target, proxy=proxy_url) as resp:
                    if resp.status >= 500:
                        return False, None, f"http_{resp.status}"
                    latency_ms = int((time.time() - start) * 1000)
                    return True, latency_ms, ""
        except Exception as exc:
            return False, None, str(exc)[:256]

    async def probe_pool(
        self,
        pool: ProxyPool,
    ) -> Dict[str, Any]:
        entries = await self.db.list_proxy_pool_entries(pool.id)
        healthy = 0
        failed = 0
        degraded = 0
        for entry in entries:
            if not entry.is_active:
                continue
            success, latency_ms, error = await self.probe_pool_entry(entry)
            update_fields: Dict[str, Any] = {
                "last_health_check_at": time.time(),
            }
            if success:
                healthy += 1
                update_fields["health_status"] = "healthy"
                update_fields["consecutive_failures"] = 0
                update_fields["last_error"] = None
                if latency_ms is not None:
                    update_fields["latency_ms"] = latency_ms
            else:
                degraded += 1 if entry.consecutive_failures and entry.consecutive_failures >= 1 else 0
                failed += 1
                update_fields["health_status"] = "failed"
                update_fields["last_error"] = error
                update_fields["consecutive_failures"] = (entry.consecutive_failures or 0) + 1
            await self.db.update_proxy_pool_entry(entry.id, **update_fields)

        if failed == 0 and healthy > 0:
            pool_status = "healthy"
        elif healthy == 0:
            pool_status = "failed"
        else:
            pool_status = "degraded"
        await self.db.update_proxy_pool(
            pool.id,
            last_health_check_at=time.time(),
            last_health_status=pool_status,
            consecutive_failures=0 if pool_status == "healthy" else (pool.consecutive_failures or 0) + 1,
        )
        return {
            "pool_id": pool.id,
            "name": pool.name,
            "status": pool_status,
            "healthy": healthy,
            "failed": failed,
            "degraded": degraded,
            "total": len(entries),
        }

    async def probe_all_pools(self) -> List[Dict[str, Any]]:
        pools = await self.db.list_proxy_pools()
        results: List[Dict[str, Any]] = []
        for pool in pools:
            try:
                result = await self.probe_pool(pool)
            except Exception as exc:
                result = {
                    "pool_id": pool.id,
                    "name": pool.name,
                    "status": "failed",
                    "error": str(exc),
                }
            results.append(result)
        return results
