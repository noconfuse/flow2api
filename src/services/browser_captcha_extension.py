import asyncio
from datetime import datetime
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from fastapi import WebSocket

from ..core.logger import debug_logger
from ..core.models import WorkerJob
from .browser_profile_bootstrap import (
    current_extension_source_fingerprint,
    current_extension_version_tag,
)


@dataclass
class ExtensionConnection:
    websocket: WebSocket
    route_key: str = ""
    client_label: str = ""
    connected_at: float = field(default_factory=time.time)
    last_seen_at: float = field(default_factory=time.time)
    current_email: str = ""
    page_url: str = ""
    page_title: str = ""
    project_id: str = ""
    tool_name: str = ""
    session_state: str = ""
    capabilities: list[str] = field(default_factory=list)
    worker_mode: str = ""
    browser_user_agent: str = ""
    last_error: str = ""
    extension_version_tag: str = ""
    extension_source_fingerprint: str = ""


class ExtensionCaptchaService:
    _instance: Optional["ExtensionCaptchaService"] = None
    _lock = asyncio.Lock()

    def __init__(self, db=None):
        self.db = db
        self.active_connections: list[ExtensionConnection] = []
        self.pending_requests: dict[str, tuple[asyncio.Future, WebSocket]] = {}

    @classmethod
    async def get_instance(cls, db=None) -> "ExtensionCaptchaService":
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls(db=db)
        elif db is not None and cls._instance.db is None:
            cls._instance.db = db
        return cls._instance

    async def connect(self, websocket: WebSocket) -> bool:
        route_key = (websocket.query_params.get("route_key") or "").strip()
        client_label = (websocket.query_params.get("client_label") or "").strip()
        if not await self._is_allowed_route_key(route_key):
            debug_logger.log_warning(
                f"[Extension Captcha] Rejecting orphan route at connect: route_key={route_key or '-'}, "
                f"label={client_label or '-'}"
            )
            await websocket.close(code=1008)
            return False
        await websocket.accept()
        conn = ExtensionConnection(
            websocket=websocket,
            route_key=route_key,
            client_label=client_label,
        )
        self.active_connections.append(conn)
        debug_logger.log_info(
            f"[Extension Captcha] Client connected. Total: {len(self.active_connections)}, "
            f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
        )
        return True

    def disconnect(self, websocket: WebSocket):
        for conn in list(self.active_connections):
            if conn.websocket is websocket:
                self.active_connections.remove(conn)
                debug_logger.log_info(
                    f"[Extension Captcha] Client disconnected. Total: {len(self.active_connections)}, "
                    f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
                )
                return

    def _find_connection(self, websocket: WebSocket) -> Optional[ExtensionConnection]:
        for conn in self.active_connections:
            if conn.websocket is websocket:
                return conn
        return None

    def _select_connection(self, route_key: str) -> Optional[ExtensionConnection]:
        normalized_key = (route_key or "").strip()
        if normalized_key:
            for conn in self.active_connections:
                if conn.route_key == normalized_key:
                    return conn
            return None
        # Empty token routes are only allowed to use an empty extension route.
        # A keyed route such as "9223" belongs to a specific browser/account
        # and must never be borrowed by another token just because it is the
        # only extension online.
        for conn in self.active_connections:
            if not conn.route_key:
                return conn
        return None

    def _describe_routes(self) -> str:
        labels = []
        for conn in self.active_connections:
            label = conn.route_key or "(empty)"
            if conn.client_label:
                label = f"{label}:{conn.client_label}"
            if conn.current_email:
                label = f"{label}<{conn.current_email}>"
            labels.append(label)
        return ", ".join(labels)

    def describe_routes(self) -> str:
        return self._describe_routes()

    async def _send_ack(self, websocket: WebSocket, payload: Dict[str, Any]):
        try:
            await websocket.send_text(json.dumps(payload))
        except Exception:
            pass

    @staticmethod
    def _normalize_email(value: Optional[str]) -> str:
        return (value or "").strip().lower()

    async def _is_allowed_route_key(self, route_key: str) -> bool:
        normalized = (route_key or "").strip()
        if not normalized:
            return True
        if not self.db:
            return True
        try:
            tokens = await self.db.get_all_tokens()
        except Exception as exc:
            debug_logger.log_warning(
                f"[Extension Captcha] Failed to validate route_key={normalized}: {exc}"
            )
            return True
        allowed = {
            (getattr(token, "extension_route_key", "") or "").strip()
            for token in tokens
            if (getattr(token, "extension_route_key", "") or "").strip()
        }
        return normalized in allowed

    @staticmethod
    def _extract_project_id(page_url: str) -> str:
        if not page_url:
            return ""
        try:
            path = urlparse(page_url).path
        except Exception:
            return ""
        parts = [part for part in path.split("/") if part]
        if "projects" in parts:
            index = parts.index("projects")
            if index + 1 < len(parts):
                return parts[index + 1]
        return ""

    def _update_connection_metadata(self, conn: ExtensionConnection, payload: Dict[str, Any]):
        conn.last_seen_at = time.time()
        if "route_key" in payload:
            conn.route_key = (payload.get("route_key") or conn.route_key or "").strip()
        if "client_label" in payload:
            conn.client_label = (payload.get("client_label") or conn.client_label or "").strip()
        if "current_email" in payload:
            conn.current_email = (payload.get("current_email") or "").strip()
        if "page_url" in payload:
            conn.page_url = (payload.get("page_url") or "").strip()
        if "page_title" in payload:
            conn.page_title = (payload.get("page_title") or "").strip()
        if "project_id" in payload:
            conn.project_id = (payload.get("project_id") or "").strip()
        if not conn.project_id:
            conn.project_id = self._extract_project_id(conn.page_url)
        if "tool_name" in payload:
            conn.tool_name = (payload.get("tool_name") or "").strip()
        if "session_state" in payload:
            conn.session_state = (payload.get("session_state") or "").strip()
        if "worker_mode" in payload:
            conn.worker_mode = (payload.get("worker_mode") or "").strip()
        if "browser_user_agent" in payload:
            conn.browser_user_agent = (payload.get("browser_user_agent") or "").strip()
        if "last_error" in payload:
            conn.last_error = (payload.get("last_error") or "").strip()
        if "extension_version_tag" in payload:
            conn.extension_version_tag = (payload.get("extension_version_tag") or "").strip()
        if "extension_source_fingerprint" in payload:
            conn.extension_source_fingerprint = (payload.get("extension_source_fingerprint") or "").strip()

        raw_capabilities = payload.get("capabilities")
        if isinstance(raw_capabilities, list):
            conn.capabilities = [
                str(item).strip() for item in raw_capabilities if str(item).strip()
            ]

    def _serialize_connection(self, conn: ExtensionConnection) -> Dict[str, Any]:
        return {
            "route_key": conn.route_key,
            "client_label": conn.client_label,
            "connected_at": conn.connected_at,
            "last_seen_at": conn.last_seen_at,
            "connected_for_seconds": max(0, int(time.time() - conn.connected_at)),
            "idle_for_seconds": max(0, int(time.time() - conn.last_seen_at)),
            "current_email": conn.current_email,
            "page_url": conn.page_url,
            "page_title": conn.page_title,
            "project_id": conn.project_id,
            "tool_name": conn.tool_name,
            "session_state": conn.session_state,
            "worker_mode": conn.worker_mode,
            "browser_user_agent": conn.browser_user_agent,
            "last_error": conn.last_error,
            "extension_version_tag": conn.extension_version_tag,
            "extension_source_fingerprint": conn.extension_source_fingerprint,
            "capabilities": list(conn.capabilities),
        }

    async def _resolve_route_key(self, token_id: Optional[int]) -> str:
        if not token_id or not self.db:
            return ""
        try:
            token = await self.db.get_token(token_id)
            if token and token.extension_route_key:
                return token.extension_route_key.strip()
        except Exception as e:
            debug_logger.log_warning(f"[Extension Captcha] Failed to resolve route key for token {token_id}: {e}")
        return ""

    def _has_connection_for_route_key(self, route_key: str) -> bool:
        return self._select_connection(route_key) is not None

    async def has_connection_for_token(self, token_id: Optional[int]) -> tuple[bool, str]:
        route_key = await self._resolve_route_key(token_id)
        return self._has_connection_for_route_key(route_key), route_key

    async def validate_connection_for_token(
        self, token_id: Optional[int]
    ) -> tuple[bool, str, str, Optional[Dict[str, Any]]]:
        route_key = await self._resolve_route_key(token_id)
        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            if route_key:
                return False, route_key, f"扩展路由 {route_key} 未连接（可用路由: {available}）", None
            return False, route_key, f"扩展路由未配置或匿名插件未连接（可用路由: {available}）", None

        snapshot = self._serialize_connection(conn)
        if token_id and self.db:
            try:
                token = await self.db.get_token(token_id)
            except Exception as exc:
                return False, route_key, f"扩展路由校验失败: {exc}", snapshot

            if token:
                expected_email = self._normalize_email(token.email)
                reported_email = self._normalize_email(conn.current_email)
                if expected_email and reported_email and expected_email != reported_email:
                    return (
                        False,
                        route_key,
                        (
                            f"扩展路由 {route_key or '(empty)'} 当前上报账号 {conn.current_email}，"
                            f"与 token {token_id} 的邮箱 {token.email} 不一致"
                        ),
                        snapshot,
                    )
                expected_version_tag = current_extension_version_tag()
                expected_source_fingerprint = current_extension_source_fingerprint()
                reported_version_tag = str(conn.extension_version_tag or "").strip()
                reported_source_fingerprint = str(conn.extension_source_fingerprint or "").strip()
                if (
                    expected_version_tag
                    and expected_source_fingerprint
                    and (
                        reported_version_tag != expected_version_tag
                        or reported_source_fingerprint != expected_source_fingerprint
                    )
                ):
                    return (
                        False,
                        route_key,
                        (
                            f"扩展路由 {route_key or '(empty)'} 当前在线版本 "
                            f"{reported_version_tag or '(missing)'} / {reported_source_fingerprint or '(missing)'} "
                            f"落后于源码 {expected_version_tag} / {expected_source_fingerprint}"
                        ),
                        snapshot,
                    )

        return True, route_key, "", snapshot

    async def list_route_status(self) -> list[Dict[str, Any]]:
        route_bindings: dict[str, list[Dict[str, Any]]] = {}
        if self.db:
            try:
                for token in await self.db.get_all_tokens():
                    route_key = (token.extension_route_key or "").strip()
                    route_bindings.setdefault(route_key, []).append(
                        {
                            "token_id": token.id,
                            "token_email": token.email,
                            "current_project_id": token.current_project_id,
                            "is_active": token.is_active,
                        }
                    )
            except Exception as exc:
                debug_logger.log_warning(f"[Extension Captcha] Failed to build route bindings snapshot: {exc}")

        routes: list[Dict[str, Any]] = []
        for conn in self.active_connections:
            route = self._serialize_connection(conn)
            route["binding_status"] = (
                "matched"
                if any(
                    self._normalize_email(item.get("token_email")) == self._normalize_email(conn.current_email)
                    for item in route_bindings.get(conn.route_key, [])
                    if conn.current_email
                )
                else "unverified"
            )
            route["assigned_tokens"] = route_bindings.get(conn.route_key, [])
            routes.append(route)

        return sorted(routes, key=lambda item: (item.get("route_key") or "", item.get("client_label") or ""))

    async def handle_message(self, websocket: WebSocket, data: str):
        try:
            payload = json.loads(data)
            message_type = payload.get("type")

            if message_type == "register":
                conn = self._find_connection(websocket)
                if conn:
                    self._update_connection_metadata(conn, payload)
                    if not await self._is_allowed_route_key(conn.route_key):
                        debug_logger.log_warning(
                            f"[Extension Captcha] Disconnecting orphan route at register: "
                            f"route_key={conn.route_key or '-'}, label={conn.client_label or '-'}"
                        )
                        await websocket.close(code=1008)
                        self.disconnect(websocket)
                        return
                    if conn.route_key:
                        self.active_connections = [
                            item
                            for item in self.active_connections
                            if item.websocket is websocket or item.route_key != conn.route_key
                        ]
                    debug_logger.log_info(
                        f"[Extension Captcha] Client registered route_key={conn.route_key or '-'}, "
                        f"label={conn.client_label or '-'}"
                    )
                    await self._send_ack(
                        websocket,
                        {
                            "type": "register_ack",
                            "route_key": conn.route_key,
                            "client_label": conn.client_label,
                            "current_email": conn.current_email,
                        },
                    )
                return

            if message_type in {"status", "heartbeat", "ping"}:
                conn = self._find_connection(websocket)
                if conn:
                    self._update_connection_metadata(conn, payload)
                return

            req_id = payload.get("req_id")
            if req_id and req_id in self.pending_requests:
                future, owner_websocket = self.pending_requests[req_id]
                if websocket is not owner_websocket:
                    debug_logger.log_warning(f"[Extension Captcha] Ignoring response from non-owner connection: {req_id}")
                    return
                if not future.done():
                    future.set_result(payload)
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Error handling message: {e}")

    async def _dispatch_request(
        self,
        *,
        conn: ExtensionConnection,
        route_key: str,
        request_type: str,
        payload: Dict[str, Any],
        timeout: int,
    ) -> Dict[str, Any]:
        req_id = f"req_{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self.pending_requests[req_id] = (future, conn.websocket)
        request_data = {
            "type": request_type,
            "req_id": req_id,
            "route_key": route_key,
            **dict(payload or {}),
        }
        try:
            await conn.websocket.send_text(json.dumps(request_data))
            result = await asyncio.wait_for(future, timeout=timeout)
            return dict(result or {})
        finally:
            self.pending_requests.pop(req_id, None)

    async def _resolve_dispatch_target(
        self,
        token_id: Optional[int],
    ) -> tuple[ExtensionConnection, str, Optional[Dict[str, Any]]]:
        """Resolve the active extension connection and Phase-B binding for a token."""
        if not self.active_connections:
            raise RuntimeError("Chrome Extension not connected or Google Labs tab not open.")

        route_ok, route_key, route_error, _snapshot = await self.validate_connection_for_token(token_id)
        if not route_ok:
            raise RuntimeError(route_error)

        binding = None
        if token_id and self.db:
            try:
                binding = await self.db.get_token_worker_binding(token_id)
            except Exception as exc:
                debug_logger.log_warning(
                    f"[Extension Captcha] Failed to read token worker binding for token {token_id}: {exc}"
                )

        conn = self._select_connection(route_key)
        if conn is None:
            available = self._describe_routes() or "none"
            raise RuntimeError(
                f"No Chrome Extension connection matches token_id={token_id} route_key='{route_key}'. "
                f"Available route keys: {available}"
            )
        return conn, route_key, binding

    async def _resolve_dispatch_target_with_auto_launch(
        self,
        token_id: Optional[int],
    ) -> tuple[ExtensionConnection, str, Optional[Dict[str, Any]]]:
        try:
            return await self._resolve_dispatch_target(token_id)
        except RuntimeError as exc:
            if token_id and self.db:
                from .browser_profile_runtime import ensure_token_browser_ready

                launch_state = await ensure_token_browser_ready(
                    self.db,
                    token_id=int(token_id),
                )
                if launch_state.get("success"):
                    return await self._resolve_dispatch_target(token_id)
                launch_error = str(launch_state.get("error") or "").strip()
                raise RuntimeError(launch_error or str(exc)) from exc
            raise

    @staticmethod
    def _job_requires_browser_relaunch(job_type: str) -> bool:
        return str(job_type or "").strip() in {
            "video_ui_probe",
            "video_ui_prepare",
            "video_submode_probe",
            "video_ui_workflow",
        }

    async def get_token(
        self,
        project_id: str,
        action: str = "IMAGE_GENERATION",
        timeout: int = 20,
        token_id: Optional[int] = None,
    ) -> Optional[str]:
        conn, route_key, _binding = await self._resolve_dispatch_target_with_auto_launch(token_id)

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching token request via route_key={route_key or '-'}, "
                f"label={conn.client_label or '-'}, project_id={project_id}, action={action}"
            )
            result = await self._dispatch_request(
                conn=conn,
                route_key=route_key,
                request_type="get_token",
                payload={
                    "action": action,
                    "project_id": project_id,
                },
                timeout=timeout,
            )
            debug_context = result.get("debug_context")
            if debug_context:
                debug_context_text = json.dumps(debug_context, ensure_ascii=False)[:1600]
                debug_logger.log_info(
                    f"[Extension Captcha] get_token debug_context route_key={route_key or '-'}: "
                    f"{debug_context_text}"
                )
                print(
                    f"[Extension Captcha] get_token debug_context route_key={route_key or '-'}: "
                    f"{debug_context_text}"
                )

            if result.get("status") == "success":
                return result.get("token")

            error_msg = result.get("error")
            debug_logger.log_error(f"[Extension Captcha] Error from extension: {error_msg}")
            return None

        except asyncio.TimeoutError:
            debug_logger.log_error(f"[Extension Captcha] Timeout waiting for token (req_id: {req_id})")
            return None
        except Exception as e:
            debug_logger.log_error(f"[Extension Captcha] Communication error: {e}")
            return None

    async def submit_json_via_browser(
        self,
        *,
        token_id: Optional[int],
        url: str,
        headers: Dict[str, Any],
        payload: Dict[str, Any],
        project_id: Optional[str] = None,
        timeout: int = 75,
    ) -> Dict[str, Any]:
        conn, route_key, _binding = await self._resolve_dispatch_target_with_auto_launch(token_id)

        debug_logger.log_info(
            f"[Extension Captcha] Dispatching browser submit via route_key={route_key or '-'}, "
            f"label={conn.client_label or '-'}, token_id={token_id}, url={url}"
        )
        result = await self._dispatch_request(
            conn=conn,
            route_key=route_key,
            request_type="submit_json",
            payload={
                "url": str(url or ""),
                "headers": dict(headers or {}),
                "payload": dict(payload or {}),
                "project_id": str(project_id or ""),
            },
            timeout=timeout,
        )
        debug_context = result.get("debug_context")
        if debug_context:
            debug_context_text = json.dumps(debug_context, ensure_ascii=False)[:1600]
            debug_logger.log_info(
                f"[Extension Captcha] submit_json debug_context route_key={route_key or '-'}: "
                f"{debug_context_text}"
            )

        if result.get("status") != "success":
            error_msg = str(result.get("error") or "").strip()
            status_code = int(result.get("http_status") or 0)
            response_payload = result.get("response")
            response_text = str(result.get("text") or "")
            content_type = str(result.get("content_type") or "")
            if not error_msg:
                detail = ""
                if isinstance(response_payload, dict):
                    error_payload = response_payload.get("error")
                    if isinstance(error_payload, dict):
                        detail = (
                            str(error_payload.get("message") or "")
                            or str(error_payload.get("status") or "")
                            or str(error_payload.get("code") or "")
                        )
                    if not detail:
                        detail = str(response_payload.get("detail") or response_payload.get("message") or "")
                if not detail:
                    detail = response_text[:300] or "browser submit failed"
                error_msg = (
                    f"browser submit failed: status={status_code or 0}; "
                    f"detail={detail}; content_type={content_type or '-'}; "
                    f"response={json.dumps(response_payload, ensure_ascii=False)[:1200] if response_payload is not None else '-'}; "
                    f"text={response_text[:1200] or '-'}"
                )
            raise RuntimeError(error_msg)

        status_code = int(result.get("http_status") or 0)
        response_payload = result.get("response")
        response_text = str(result.get("text") or "")
        content_type = str(result.get("content_type") or "")

        if status_code >= 400:
            detail = ""
            if isinstance(response_payload, dict):
                error_payload = response_payload.get("error")
                if isinstance(error_payload, dict):
                    detail = (
                        str(error_payload.get("message") or "")
                        or str(error_payload.get("status") or "")
                        or str(error_payload.get("code") or "")
                    )
                if not detail:
                    detail = str(response_payload.get("detail") or response_payload.get("message") or "")
            if not detail:
                detail = response_text[:300] or f"HTTP {status_code}"
            debug_summary = {
                "status_code": status_code,
                "content_type": content_type,
                "response_payload": response_payload,
                "response_text_prefix": response_text[:1200],
            }
            debug_logger.log_error(
                "[Extension Captcha] browser submit upstream error: "
                f"{json.dumps(debug_summary, ensure_ascii=False)}"
            )
            raise RuntimeError(
                "browser submit failed: "
                f"status={status_code}; detail={detail}; "
                f"content_type={content_type or '-'}; "
                f"response={json.dumps(response_payload, ensure_ascii=False)[:1200] if response_payload is not None else '-'}; "
                f"text={response_text[:1200] or '-'}"
            )

        if not isinstance(response_payload, dict):
            raise RuntimeError(f"browser submit returned non-json response: {response_text[:300]}")

        return response_payload

    async def submit_request_via_browser(
        self,
        *,
        token_id: Optional[int],
        url: str,
        method: str,
        headers: Dict[str, Any],
        body_mode: str = "json",
        body: Any = None,
        project_id: Optional[str] = None,
        timeout: int = 75,
        response_header_names: Optional[list[str]] = None,
    ) -> Dict[str, Any]:
        conn, route_key, _binding = await self._resolve_dispatch_target_with_auto_launch(token_id)

        normalized_method = str(method or "POST").strip().upper() or "POST"
        normalized_body_mode = str(body_mode or "json").strip().lower() or "json"
        debug_logger.log_info(
            f"[Extension Captcha] Dispatching browser request via route_key={route_key or '-'}, "
            f"label={conn.client_label or '-'}, token_id={token_id}, method={normalized_method}, url={url}"
        )
        result = await self._dispatch_request(
            conn=conn,
            route_key=route_key,
            request_type="submit_request",
            payload={
                "url": str(url or ""),
                "method": normalized_method,
                "headers": dict(headers or {}),
                "body_mode": normalized_body_mode,
                "body": body,
                "project_id": str(project_id or ""),
                "response_header_names": list(response_header_names or []),
            },
            timeout=timeout,
        )
        debug_context = result.get("debug_context")
        if debug_context:
            debug_context_text = json.dumps(debug_context, ensure_ascii=False)[:1600]
            debug_logger.log_info(
                f"[Extension Captcha] submit_request debug_context route_key={route_key or '-'}: "
                f"{debug_context_text}"
            )

        status_code = int(result.get("http_status") or 0)
        response_payload = result.get("response")
        response_text = str(result.get("text") or "")
        content_type = str(result.get("content_type") or "")
        response_headers = result.get("response_headers")
        if not isinstance(response_headers, dict):
            response_headers = {}

        if result.get("status") != "success":
            error_msg = str(result.get("error") or "").strip()
            if not error_msg:
                detail = response_text[:300] or "browser request failed"
                if isinstance(response_payload, dict):
                    detail = (
                        str(response_payload.get("detail") or "")
                        or str(response_payload.get("message") or "")
                        or detail
                    )
                error_msg = (
                    f"browser request failed: status={status_code or 0}; "
                    f"detail={detail}; content_type={content_type or '-'}; "
                    f"response={json.dumps(response_payload, ensure_ascii=False)[:1200] if response_payload is not None else '-'}; "
                    f"text={response_text[:1200] or '-'}; "
                    f"headers={json.dumps(response_headers, ensure_ascii=False)[:800] if response_headers else '-'}"
                )
            raise RuntimeError(error_msg)

        if status_code >= 400:
            detail = response_text[:300] or f"HTTP {status_code}"
            if isinstance(response_payload, dict):
                detail = (
                    str(response_payload.get("detail") or "")
                    or str(response_payload.get("message") or "")
                    or detail
                )
            raise RuntimeError(
                "browser request failed: "
                f"status={status_code}; detail={detail}; "
                f"content_type={content_type or '-'}; "
                f"response={json.dumps(response_payload, ensure_ascii=False)[:1200] if response_payload is not None else '-'}; "
                f"text={response_text[:1200] or '-'}; "
                f"headers={json.dumps(response_headers, ensure_ascii=False)[:800] if response_headers else '-'}"
            )

        return {
            "http_status": status_code,
            "response": response_payload,
            "text": response_text,
            "content_type": content_type,
            "response_headers": response_headers,
        }

    async def dispatch_ui_job(
        self,
        *,
        token_id: Optional[int],
        job_type: str,
        project_id: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        timeout: int = 75,
    ) -> Dict[str, Any]:
        """Dispatch a UI-level browser worker job to the matched extension slot."""
        if token_id and self.db and self._job_requires_browser_relaunch(job_type):
            from .browser_profile_runtime import (
                ensure_token_browser_ready,
                get_recent_token_browser_launch_age,
                was_token_browser_recently_launched,
            )

            recent_launch_age = get_recent_token_browser_launch_age(int(token_id))
            if was_token_browser_recently_launched(int(token_id)):
                debug_logger.log_info(
                    "[Extension Captcha] Skip browser force relaunch for token "
                    f"{int(token_id)}: recent auto-launch age={recent_launch_age:.2f}s"
                )
            else:
                launch_state = await ensure_token_browser_ready(
                    self.db,
                    token_id=int(token_id),
                    force_relaunch=True,
                )
                if not launch_state.get("success"):
                    raise RuntimeError(str(launch_state.get("error") or "任务前强制重启浏览器失败"))
        conn, route_key, binding = await self._resolve_dispatch_target_with_auto_launch(token_id)
        job_id = f"job_{uuid.uuid4().hex}"
        profile_id = str((binding or {}).get("profile_id") or "")
        slot_id = str((binding or {}).get("slot_id") or "")
        normalized_payload = dict(payload or {})

        if self.db:
            try:
                await self.db.create_worker_job(
                    WorkerJob(
                        job_id=job_id,
                        token_id=token_id,
                        profile_id=profile_id or None,
                        slot_id=slot_id or None,
                        route_key=route_key or None,
                        job_type=job_type,
                        status="queued",
                        request_payload=json.dumps(
                            {
                                "project_id": str(project_id or ""),
                                "payload": normalized_payload,
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                await self.db.update_worker_job(
                    job_id,
                    status="running",
                    started_at=datetime.utcnow(),
                )
            except Exception as exc:
                debug_logger.log_warning(f"[Extension Captcha] Failed to persist worker job {job_id}: {exc}")

        try:
            debug_logger.log_info(
                f"[Extension Captcha] Dispatching UI job via route_key={route_key or '-'}, "
                f"label={conn.client_label or '-'}, job_id={job_id}, job_type={job_type}, project_id={project_id or '-'}"
            )
            result = await self._dispatch_request(
                conn=conn,
                route_key=route_key,
                request_type="run_job",
                payload={
                    "job_id": job_id,
                    "job_type": str(job_type or "").strip(),
                    "project_id": str(project_id or ""),
                    "job_payload": normalized_payload,
                },
                timeout=timeout,
            )
            status = "succeeded" if result.get("status") == "success" else "failed"
            if self.db:
                try:
                    await self.db.update_worker_job(
                        job_id,
                        status=status,
                        result_payload=json.dumps(result, ensure_ascii=False),
                        error_message=str(result.get("error") or "") or None,
                        finished_at=datetime.utcnow(),
                    )
                except Exception as exc:
                    debug_logger.log_warning(
                        f"[Extension Captcha] Failed to update worker job {job_id} result: {exc}"
                    )
            return {
                "job_id": job_id,
                "slot_id": slot_id,
                "profile_id": profile_id,
                **dict(result or {}),
            }
        except Exception as exc:
            if self.db:
                try:
                    await self.db.update_worker_job(
                        job_id,
                        status="failed",
                        error_message=str(exc),
                        finished_at=datetime.utcnow(),
                    )
                except Exception as update_exc:
                    debug_logger.log_warning(
                        f"[Extension Captcha] Failed to mark worker job {job_id} as failed: {update_exc}"
                    )
            raise RuntimeError(str(exc))

    async def report_flow_error(self, project_id: str, error_reason: str, error_message: str = ""):
        _ = project_id, error_message
        debug_logger.log_warning(f"[Extension Captcha] Flow error reported (ignoring): {error_reason}")
