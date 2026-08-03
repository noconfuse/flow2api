"""Load balancing module for Flow2API"""
import asyncio
import random
from datetime import datetime, timezone
from typing import Optional, Dict, Any, Iterable
from ..core.models import Token
from ..core.config import config
from ..core.account_tiers import (
    get_paygate_tier_label,
    get_required_paygate_tier_for_model,
    normalize_user_paygate_tier,
    supports_model_for_tier,
)
from .concurrency_manager import ConcurrencyManager
from .browser_profile_runtime import ensure_token_browser_ready
from ..core.logger import debug_logger


class LoadBalancer:
    """Token load balancer with load-aware selection"""

    def __init__(self, token_manager, concurrency_manager: Optional[ConcurrencyManager] = None):
        self.token_manager = token_manager
        self.concurrency_manager = concurrency_manager
        self._image_pending: Dict[int, int] = {}
        self._video_pending: Dict[int, int] = {}
        self._pending_lock = asyncio.Lock()
        self._round_robin_state: Dict[str, Optional[int]] = {"image": None, "video": None, "default": None}
        self._rr_lock = asyncio.Lock()

    def _get_token_credits(self, token: Token) -> int:
        try:
            return int(token.credits or 0)
        except Exception:
            return 0

    def _is_credit_exhausted(self, token: Token) -> bool:
        return self._get_token_credits(token) <= config.exhausted_credit_threshold

    def _is_low_credit(self, token: Token) -> bool:
        credits = self._get_token_credits(token)
        return config.exhausted_credit_threshold < credits <= config.low_credit_threshold

    def _is_credit_insufficient_for(self, token: Token, required_credits: Optional[int]) -> bool:
        """当任务显式声明了积分消耗时，账号剩余 credits 必须 >= required_credits。

        避免在提交前选择了一个快没积分的账号，导致 UI 端积分耗尽、提交按钮被替换。
        required_credits 为 0/None 表示任务无积分消耗或尚未识别，仍按既有规则放行。
        """
        if not required_credits or required_credits <= 0:
            return False
        return self._get_token_credits(token) < int(required_credits)

    def _get_automation_risk_score(self, token: Token) -> int:
        try:
            return max(0, int(token.automation_risk_score or 0))
        except Exception:
            return 0

    def _get_automation_risk_state(self, token: Token) -> str:
        return str(token.automation_risk_state or "healthy").strip().lower()

    def _is_automation_risk_blocked(self, token: Token, for_video_generation: bool) -> tuple[bool, str]:
        return False, ""

    def _is_automation_risk_cooling_down(self, token: Token, for_video_generation: bool) -> tuple[bool, str]:
        if not for_video_generation or config.captcha_method != "extension":
            return False, ""

        state = self._get_automation_risk_state(token)
        cooldown_until = token.automation_cooldown_until
        if state == "blocked":
            return True, f"自动化风控高风险冷却中 (legacy blocked, score={self._get_automation_risk_score(token)})"
        if state != "cooldown" or not cooldown_until:
            return False, ""

        now = datetime.now(timezone.utc)
        if cooldown_until.tzinfo is None:
            cooldown_until = cooldown_until.replace(tzinfo=timezone.utc)
        if cooldown_until > now:
            return True, f"自动化风控冷却中，直到 {cooldown_until.isoformat()}"
        return False, ""

    async def _get_pending_count(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> int:
        async with self._pending_lock:
            if for_image_generation:
                return max(0, int(self._image_pending.get(token_id, 0)))
            if for_video_generation:
                return max(0, int(self._video_pending.get(token_id, 0)))
            return 0

    async def _add_pending(self, token_id: int, for_image_generation: bool, for_video_generation: bool):
        async with self._pending_lock:
            if for_image_generation:
                self._image_pending[token_id] = max(0, int(self._image_pending.get(token_id, 0))) + 1
            elif for_video_generation:
                self._video_pending[token_id] = max(0, int(self._video_pending.get(token_id, 0))) + 1

    async def release_pending(self, token_id: int, for_image_generation: bool = False, for_video_generation: bool = False):
        async with self._pending_lock:
            if for_image_generation:
                current = max(0, int(self._image_pending.get(token_id, 0)))
                if current <= 1:
                    self._image_pending.pop(token_id, None)
                else:
                    self._image_pending[token_id] = current - 1
            elif for_video_generation:
                current = max(0, int(self._video_pending.get(token_id, 0)))
                if current <= 1:
                    self._video_pending.pop(token_id, None)
                else:
                    self._video_pending[token_id] = current - 1

    async def _get_token_load(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> tuple[int, Optional[int]]:
        """获取 token 当前负载。

        Returns:
            (inflight, remaining)
            remaining 为 None 表示无限制
        """
        if not self.concurrency_manager:
            return 0, None

        if for_image_generation:
            inflight = await self.concurrency_manager.get_image_inflight(token_id)
            remaining = await self.concurrency_manager.get_image_remaining(token_id)
            pending = await self._get_pending_count(token_id, True, False)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        if for_video_generation:
            inflight = await self.concurrency_manager.get_video_inflight(token_id)
            remaining = await self.concurrency_manager.get_video_remaining(token_id)
            pending = await self._get_pending_count(token_id, False, True)
            effective_inflight = inflight + pending
            if remaining is not None:
                remaining = max(0, remaining - pending)
            return effective_inflight, remaining

        return 0, None

    async def _reserve_slot(self, token_id: int, for_image_generation: bool, for_video_generation: bool) -> bool:
        """尝试为当前 token 预占一个生成槽位。"""
        if not self.concurrency_manager:
            return True

        if for_image_generation:
            return await self.concurrency_manager.acquire_image(token_id)

        if for_video_generation:
            return await self.concurrency_manager.acquire_video(token_id)

        return True

    async def _select_round_robin(self, tokens: list[dict], scenario: str) -> Optional[dict]:
        """Select candidate in round-robin order for the given scenario."""
        if not tokens:
            return None

        tokens_sorted = sorted(tokens, key=lambda item: item["token"].id or 0)
        async with self._rr_lock:
            last_id = self._round_robin_state.get(scenario)
            start_idx = 0
            if last_id is not None:
                for idx, item in enumerate(tokens_sorted):
                    if item["token"].id == last_id:
                        start_idx = (idx + 1) % len(tokens_sorted)
                        break
            selected = tokens_sorted[start_idx]
            self._round_robin_state[scenario] = selected["token"].id
        return selected

    async def _check_extension_route(self, token: Token) -> tuple[bool, str]:
        """Ensure extension captcha requests are routed to the selected account."""
        if config.captcha_method != "extension":
            return True, ""

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            db = getattr(self.token_manager, "db", None)
            service = await ExtensionCaptchaService.get_instance(db)
            if db:
                try:
                    await db.sync_browser_profiles_from_tokens()
                    await db.sync_extension_routes_to_worker_slots(await service.list_route_status())
                    binding = await db.get_token_worker_binding(token.id)
                except Exception as exc:
                    binding = None
                    debug_logger.log_warning(
                        f"[LOAD_BALANCER] 构建 token {token.id} 的 profile/slot 绑定视图失败，将回退到 route 校验: {exc}"
                    )
                if binding:
                    slot_id = str(binding.get("slot_id") or "").strip()
                    profile_id = str(binding.get("profile_id") or "").strip()
                    slot_email = str(binding.get("current_email") or "").strip().lower()
                    token_email = str(token.email or "").strip().lower()
                    if not profile_id:
                        return False, f"token {token.id} 尚未生成 browser profile 资产"
                    if slot_id:
                        if slot_email and token_email and slot_email != token_email:
                            return (
                                False,
                                f"browser slot {slot_id} 当前上报账号 {binding.get('current_email')}，"
                                f"与 token {token.id} 的邮箱 {token.email} 不一致",
                            )
                        return True, ""

            route_ok, route_key, route_error, _ = await service.validate_connection_for_token(token.id)
            if route_ok:
                return True, ""
            return False, route_error
        except Exception as exc:
            return False, f"扩展路由检查失败: {exc}"

    async def _build_extension_worker_snapshot(self) -> Optional[Dict[str, Any]]:
        """Build a snapshot of token -> profile -> slot availability for extension mode."""
        if config.captcha_method != "extension":
            return None

        db = getattr(self.token_manager, "db", None)
        if not db:
            return None

        try:
            from .browser_captcha_extension import ExtensionCaptchaService

            service = await ExtensionCaptchaService.get_instance(db)
            await db.sync_browser_profiles_from_tokens()
            await db.sync_extension_routes_to_worker_slots(await service.list_route_status())
            binding_rows = await db.list_token_worker_bindings()
            binding_map = {
                int(item["token_id"]): item
                for item in binding_rows
                if item.get("token_id") is not None
            }
            return {
                "bindings": binding_map,
                "service": service,
            }
        except Exception as exc:
            debug_logger.log_warning(
                f"[LOAD_BALANCER] 构建 extension profile/slot snapshot 失败，将退回逐 token route 校验: {exc}"
            )
            return None

    async def _check_extension_binding_snapshot(
        self,
        token: Token,
        snapshot: Optional[Dict[str, Any]],
    ) -> tuple[bool, str]:
        """Validate extension availability using the Phase-B profile/slot snapshot."""
        if config.captcha_method != "extension":
            return True, ""

        if not snapshot:
            return await self._check_extension_route(token)

        binding = (snapshot.get("bindings") or {}).get(token.id)
        if binding:
            profile_id = str(binding.get("profile_id") or "").strip()
            slot_id = str(binding.get("slot_id") or "").strip()
            current_email = str(binding.get("current_email") or "").strip().lower()
            token_email = str(token.email or "").strip().lower()
            if slot_id:
                if current_email and token_email and current_email != token_email:
                    return (
                        False,
                        f"browser slot {slot_id} 当前上报账号 {binding.get('current_email')}，"
                        f"与 token {token.id} 的邮箱 {token.email} 不一致",
                    )
                return True, ""
            if not profile_id:
                route_ok, _, route_error, _ = await snapshot["service"].validate_connection_for_token(token.id)
                if route_ok:
                    return True, ""
                return False, route_error or f"token {token.id} 尚未生成 browser profile 资产"

        service = snapshot.get("service")
        if not service:
            return await self._check_extension_route(token)

        route_ok, route_key, route_error, _ = await service.validate_connection_for_token(token.id)
        if route_ok:
            return True, ""
        return False, route_error

    async def select_token(
        self,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        reserve: bool = False,
        enforce_concurrency_filter: bool = True,
        track_pending: bool = False,
        preferred_token_id: Optional[int] = None,
        excluded_token_ids: Optional[Iterable[int]] = None,
        required_credits: Optional[int] = None,
        _auto_launch_attempted: bool = False,
    ) -> Optional[Token]:
        """
        Select a token using load-aware balancing

        Args:
            for_image_generation: If True, only select tokens with image_enabled=True
            for_video_generation: If True, only select tokens with video_enabled=True
            model: Model name (used to filter tokens for specific models)
            reserve: Whether to atomically reserve one concurrency slot for the selected token
            enforce_concurrency_filter:
                Whether to pre-filter tokens by current inflight/remaining capacity.
                For reserve=False generation paths, this should usually be False so
                requests can enter the downstream wait queue instead of failing fast.
            track_pending:
                Whether to count the selected token as a queued request immediately.
                This smooths burst distribution before the hard concurrency slot is acquired.

        Returns:
            Selected token or None if no available tokens
        """
        excluded_ids = {
            int(token_id)
            for token_id in (excluded_token_ids or [])
            if token_id is not None
        }
        debug_logger.log_info(
            f"[LOAD_BALANCER] 开始选择Token (图片生成={for_image_generation}, "
            f"视频生成={for_video_generation}, 模型={model}, 预占槽位={reserve}, "
            f"preferred_token_id={preferred_token_id}, excluded_token_ids={sorted(excluded_ids)})"
        )

        active_tokens = await self.token_manager.get_active_tokens()
        debug_logger.log_info(f"[LOAD_BALANCER] 获取到 {len(active_tokens)} 个活跃Token")

        if not active_tokens:
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有活跃的Token")
            return None

        if preferred_token_id is not None:
            active_tokens = [token for token in active_tokens if token.id == preferred_token_id]
            debug_logger.log_info(
                f"[LOAD_BALANCER] 指定Token模式，过滤后剩余 {len(active_tokens)} 个候选"
            )
            if not active_tokens:
                return None

        if excluded_ids:
            active_tokens = [token for token in active_tokens if token.id not in excluded_ids]
            debug_logger.log_info(
                f"[LOAD_BALANCER] 已排除 {len(excluded_ids)} 个局部失败Token，剩余 {len(active_tokens)} 个候选"
            )
            if not active_tokens:
                return None

        available_tokens = []
        launchable_extension_tokens = []
        filtered_reasons = {}
        required_tier = get_required_paygate_tier_for_model(model)
        extension_snapshot = await self._build_extension_worker_snapshot()

        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                filtered_reasons[token.id] = '账号等级不足，需要 ' + get_paygate_tier_label(required_tier)
                continue
            if self._is_credit_exhausted(token):
                filtered_reasons[token.id] = f"账号余额已耗尽 (credits={self._get_token_credits(token)})"
                continue
            if self._is_credit_insufficient_for(token, required_credits):
                filtered_reasons[token.id] = (
                    f"账号余额不足以支付本次任务 (credits={self._get_token_credits(token)}, "
                    f"required={int(required_credits)})"
                )
                continue
            automation_risk_blocked, automation_risk_reason = self._is_automation_risk_blocked(
                token,
                for_video_generation=for_video_generation,
            )
            if automation_risk_blocked:
                filtered_reasons[token.id] = automation_risk_reason
                continue
            launchable_extension_tokens.append(token)
            if for_image_generation:
                if not token.image_enabled:
                    launchable_extension_tokens.pop()
                    filtered_reasons[token.id] = "图片生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_binding_snapshot(token, extension_snapshot)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_image(token.id)
                ):
                    filtered_reasons[token.id] = "图片并发已满"
                    continue

            if for_video_generation:
                if not token.video_enabled:
                    launchable_extension_tokens.pop()
                    filtered_reasons[token.id] = "视频生成已禁用"
                    continue

                route_ok, route_reason = await self._check_extension_binding_snapshot(token, extension_snapshot)
                if not route_ok:
                    filtered_reasons[token.id] = route_reason
                    continue

                if (
                    enforce_concurrency_filter
                    and self.concurrency_manager
                    and not await self.concurrency_manager.can_use_video(token.id)
                ):
                    filtered_reasons[token.id] = "视频并发已满"
                    continue

            inflight, remaining = await self._get_token_load(
                token.id,
                for_image_generation=for_image_generation,
                for_video_generation=for_video_generation
            )
            available_tokens.append({
                "token": token,
                "inflight": inflight,
                "remaining": remaining,
                "needs_refresh": self.token_manager.needs_at_refresh(token),
                "low_credit": self._is_low_credit(token),
                "credits": self._get_token_credits(token),
                "automation_risk_score": self._get_automation_risk_score(token),
                "cooling_down": self._is_automation_risk_cooling_down(
                    token,
                    for_video_generation=for_video_generation,
                )[0],
                "random": random.random()
            })

        if filtered_reasons:
            debug_logger.log_info(f"[LOAD_BALANCER] 已过滤Token:")
            for token_id, reason in filtered_reasons.items():
                debug_logger.log_info(f"[LOAD_BALANCER]   - Token {token_id}: {reason}")

        if not available_tokens:
            if (
                config.captcha_method == "extension"
                and not _auto_launch_attempted
                and launchable_extension_tokens
            ):
                candidate = launchable_extension_tokens[0]
                debug_logger.log_info(
                    f"[LOAD_BALANCER] 尝试自动拉起离线浏览器账号 token {candidate.id} ({candidate.email})"
                )
                try:
                    db = getattr(self.token_manager, "db", None)
                    if db:
                        launch_state = await ensure_token_browser_ready(
                            db,
                            token_id=int(candidate.id),
                        )
                        if launch_state.get("success"):
                            debug_logger.log_info(
                                f"[LOAD_BALANCER] token {candidate.id} 浏览器已自动拉起，重新选择账号"
                            )
                            return await self.select_token(
                                for_image_generation=for_image_generation,
                                for_video_generation=for_video_generation,
                                model=model,
                                reserve=reserve,
                                enforce_concurrency_filter=enforce_concurrency_filter,
                                track_pending=track_pending,
                                preferred_token_id=preferred_token_id,
                                excluded_token_ids=excluded_ids,
                                _auto_launch_attempted=True,
                            )
                        debug_logger.log_warning(
                            f"[LOAD_BALANCER] token {candidate.id} 自动拉起失败: {launch_state.get('error')}"
                        )
                except Exception as exc:
                    debug_logger.log_warning(
                        f"[LOAD_BALANCER] 自动拉起离线浏览器账号失败 token {candidate.id}: {exc}"
                    )
            debug_logger.log_info(f"[LOAD_BALANCER] ❌ 没有可用的Token (图片生成={for_image_generation}, 视频生成={for_video_generation})")
            return None

        # 最低 in-flight 优先；有并发上限时，剩余槽位更多的 token 优先；最后随机打散
        call_mode = config.call_logic_mode
        if call_mode == "polling":
            scenario = "default"
            if for_image_generation:
                scenario = "image"
            elif for_video_generation:
                scenario = "video"

            ordered_candidates = []
            healthy_candidates = [item for item in available_tokens if not item["cooling_down"] and not item["low_credit"]]
            healthy_low_credit_candidates = [item for item in available_tokens if not item["cooling_down"] and item["low_credit"]]
            cooling_candidates = [item for item in available_tokens if item["cooling_down"] and not item["low_credit"]]
            cooling_low_credit_candidates = [item for item in available_tokens if item["cooling_down"] and item["low_credit"]]
            primary_candidates = healthy_candidates or healthy_low_credit_candidates or cooling_candidates or cooling_low_credit_candidates
            secondary_candidates = []
            if primary_candidates is healthy_candidates:
                secondary_candidates = healthy_low_credit_candidates + cooling_candidates + cooling_low_credit_candidates
            elif primary_candidates is healthy_low_credit_candidates:
                secondary_candidates = cooling_candidates + cooling_low_credit_candidates
            elif primary_candidates is cooling_candidates:
                secondary_candidates = cooling_low_credit_candidates

            first_candidate = await self._select_round_robin(primary_candidates, scenario)
            if first_candidate is not None:
                ordered_candidates.append(first_candidate)
                ordered_candidates.extend(
                    item for item in sorted(primary_candidates, key=lambda item: item["token"].id or 0)
                    if item["token"].id != first_candidate["token"].id
                )
                ordered_candidates.extend(
                    item for item in sorted(secondary_candidates, key=lambda item: item["token"].id or 0)
                    if item["token"].id != first_candidate["token"].id
                )
            available_tokens = ordered_candidates
        else:
            available_tokens.sort(
                key=lambda item: (
                    1 if item["needs_refresh"] else 0,
                    1 if item["low_credit"] else 0,
                    1 if item["cooling_down"] else 0,
                    item["automation_risk_score"],
                    item["inflight"],
                    0 if item["remaining"] is None else 1,
                    -(item["remaining"] or 0),
                    -item["credits"],
                    item["random"]
                )
            )

        ready_candidates = [item for item in available_tokens if not item["needs_refresh"]]
        refresh_candidates = [item for item in available_tokens if item["needs_refresh"]]
        if ready_candidates and refresh_candidates:
            available_tokens = ready_candidates + refresh_candidates

        debug_logger.log_info("[LOAD_BALANCER] 候选Token负载:")
        for item in available_tokens:
            token = item["token"]
            remaining = "unlimited" if item["remaining"] is None else item["remaining"]
            debug_logger.log_info(
                f"[LOAD_BALANCER]   - Token {token.id} ({token.email}) "
                f"inflight={item['inflight']}, remaining={remaining}, "
                f"needs_refresh={item['needs_refresh']}, cooldown={item['cooling_down']}, "
                f"credits={token.credits}"
            )

        # 只为候选列表中真正尝试到的 token 做 AT 校验，避免每次请求把所有 token 全扫一遍
        for item in available_tokens:
            token = item["token"]
            token_id = token.id

            token = await self.token_manager.ensure_valid_token(token)
            if not token:
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token_id}: AT无效或已过期")
                continue

            if self._is_credit_exhausted(token):
                debug_logger.log_info(
                    f"[LOAD_BALANCER] 跳过 Token {token.id}: 余额已耗尽 (credits={self._get_token_credits(token)})"
                )
                continue

            if reserve and not await self._reserve_slot(token.id, for_image_generation, for_video_generation):
                debug_logger.log_info(f"[LOAD_BALANCER] 跳过 Token {token.id}: 预占槽位失败")
                continue

            if track_pending:
                await self._add_pending(token.id, for_image_generation, for_video_generation)

            debug_logger.log_info(
                f"[LOAD_BALANCER] ✅ 已选择Token {token.id} ({token.email}) - "
                f"余额: {token.credits}, inflight={item['inflight']}"
            )
            return token

        debug_logger.log_info(f"[LOAD_BALANCER] ❌ 候选Token均不可用 (图片生成={for_image_generation}, 视频生成={for_video_generation})")
        return None

    async def get_unavailable_reason(
        self,
        *,
        for_image_generation: bool = False,
        for_video_generation: bool = False,
        model: Optional[str] = None,
        excluded_token_ids: Optional[Iterable[int]] = None,
    ) -> Optional[str]:
        """给出更明确的“无可用账号”原因，优先用于分辨率/tier 档位提示。"""
        active_tokens = await self.token_manager.get_active_tokens()
        if not active_tokens:
            return None

        excluded_ids = {
            int(token_id)
            for token_id in (excluded_token_ids or [])
            if token_id is not None
        }
        if excluded_ids:
            active_tokens = [token for token in active_tokens if token.id not in excluded_ids]
            if not active_tokens:
                return "当前批量重试已排除全部已失败账号，暂无其它可用账号。"

        required_tier = get_required_paygate_tier_for_model(model)
        supported_tokens = []
        for token in active_tokens:
            normalized_tier = normalize_user_paygate_tier(token.user_paygate_tier)
            if model and not supports_model_for_tier(model, normalized_tier):
                continue
            supported_tokens.append(token)

        if model and not supported_tokens:
            tier_label = get_paygate_tier_label(required_tier)
            return f"当前模型需要 {tier_label} 账号，但没有可用的 {tier_label} 账号: {model}"

        capability_tokens = []
        for token in supported_tokens:
            if for_image_generation and not token.image_enabled:
                continue
            if for_video_generation and not token.video_enabled:
                continue
            capability_tokens.append(token)

        if supported_tokens and not capability_tokens:
            if for_image_generation:
                return "当前有符合档位的账号，但图片生成功能已全部禁用。"
            if for_video_generation:
                return "当前有符合档位的账号，但视频生成功能已全部禁用。"

        credit_tokens = [token for token in capability_tokens if not self._is_credit_exhausted(token)]
        if capability_tokens and not credit_tokens:
            return (
                "当前有符合条件的账号，但余额已全部耗尽。"
                f" exhausted_credit_threshold={config.exhausted_credit_threshold}"
            )

        if config.captcha_method == "extension":
            snapshot = await self._build_extension_worker_snapshot()
            if snapshot:
                has_profile = False
                has_online_slot = False
                for token in credit_tokens:
                    binding = (snapshot.get("bindings") or {}).get(token.id) or {}
                    if binding.get("profile_id"):
                        has_profile = True
                    if binding.get("slot_id"):
                        current_email = str(binding.get("current_email") or "").strip().lower()
                        token_email = str(token.email or "").strip().lower()
                        if current_email and token_email and current_email != token_email:
                            continue
                        has_online_slot = True
                        break
                if credit_tokens and not has_profile:
                    return "当前 extension 账号尚未建立 browser profile 资产。"
                if has_profile and not has_online_slot:
                    return "当前已有 browser profile 资产，但没有可用的在线 worker slot。"

        return None
