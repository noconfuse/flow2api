"""Admin API routes"""
import asyncio
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, List, Dict, Any
import secrets
import time
import re
from curl_cffi.requests import AsyncSession
from ..core.auth import AuthManager, verify_api_key_flexible
from ..core.database import Database
from ..core.config import (
    config,
    get_yescaptcha_min_score,
    normalize_yescaptcha_task_type,
    yescaptcha_task_type_requires_proxy,
)
from ..core.monitoring import build_public_health_snapshot
from ..services.token_manager import TokenManager
from ..services.proxy_manager import ProxyManager
from ..services.concurrency_manager import ConcurrencyManager
from ..services.browser_cookie_utils import (
    extract_session_token_from_cookie_payload,
    merge_browser_cookie_payloads,
    merge_browser_cookie_payloads_prefer_live_session,
    normalize_cookie_storage_text,
)
from ..services.browser_profile_bootstrap import (
    make_route_key,
    make_spec,
    prepare_and_optionally_launch,
)

router = APIRouter()

# Dependency injection
token_manager: TokenManager = None
proxy_manager: ProxyManager = None
db: Database = None
concurrency_manager: Optional[ConcurrencyManager] = None

# Store active admin session tokens (in production, use Redis or database)
active_admin_tokens = set()
SUPPORTED_API_CAPTCHA_METHODS = {"yescaptcha", "capmonster", "ezcaptcha", "capsolver"}


def _mask_token(token: Optional[str]) -> str:
    if not token:
        return ""
    if len(token) <= 24:
        return token
    return f"{token[:18]}...{token[-8:]}"


def _truncate_text(text: Any, limit: int = 240) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return f"{value[:limit - 3]}..."


def _extract_error_summary(payload: Any) -> str:
    """从响应体里提取用户可读的错误摘要。"""
    if payload is None:
        return ""

    if isinstance(payload, str):
        raw = payload.strip()
        if not raw:
            return ""
        try:
            return _extract_error_summary(json.loads(raw))
        except Exception:
            return _truncate_text(raw)

    if isinstance(payload, dict):
        for key in ("error_summary", "error_message", "detail", "message"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate_text(value)

        error_value = payload.get("error")
        if isinstance(error_value, dict):
            for key in ("message", "detail", "reason", "code"):
                value = error_value.get(key)
                if isinstance(value, str) and value.strip():
                    return _truncate_text(value)
        elif isinstance(error_value, str) and error_value.strip():
            return _truncate_text(error_value)

        for nested_key in ("response", "data"):
            nested = payload.get(nested_key)
            if isinstance(nested, (dict, list, str)):
                summary = _extract_error_summary(nested)
                if summary:
                    return summary

        return ""

    if isinstance(payload, list):
        for item in payload:
            summary = _extract_error_summary(item)
            if summary:
                return summary
        return ""

    return _truncate_text(payload)


def _safe_json_loads(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if not isinstance(payload, str):
        return None
    raw = payload.strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


async def _bootstrap_browser_profile_for_token(
    token_obj: Any,
    *,
    auto_launch_browser: bool = False,
    chrome_path: Optional[str] = None,
) -> dict:
    token_id = int(getattr(token_obj, "id"))
    route_key = make_route_key(token_id, getattr(token_obj, "extension_route_key", None))

    if str(getattr(token_obj, "extension_route_key", "") or "").strip() != route_key:
        await token_manager.update_token(
            token_id=token_id,
            extension_route_key=route_key,
        )
        token_obj.extension_route_key = route_key

    await db.sync_browser_profiles_from_tokens()
    await db.clear_worker_slots_for_profile(f"token-{token_id}")

    spec = make_spec(
        token_id=token_id,
        email=str(getattr(token_obj, "email", "") or "").strip(),
        route_key=route_key,
        current_project_id=str(getattr(token_obj, "current_project_id", "") or "").strip(),
    )

    try:
        result = await asyncio.to_thread(
            prepare_and_optionally_launch,
            spec,
            launch=bool(auto_launch_browser),
            chrome_path=str(chrome_path or "").strip() or None,
        )
        return {
            "success": True,
            **result,
        }
    except Exception as e:
        return {
            "success": False,
            "prepared": False,
            "launched": False,
            "token_id": token_id,
            "route_key": route_key,
            "client_label": spec.client_label,
            "email": spec.email,
            "error": str(e),
        }


async def _build_account_runtime_snapshot(token_obj: Any) -> dict:
    token_id = int(getattr(token_obj, "id"))
    route_key = str(getattr(token_obj, "extension_route_key", "") or "").strip()
    profile = await db.get_browser_profile_by_token_id(token_id)
    profile_id = str(getattr(profile, "profile_id", "") or "").strip() if profile else ""
    slots = await db.list_worker_slots()

    matched_slot: Optional[Dict[str, Any]] = None
    for slot in slots:
        if route_key and str(slot.get("route_key") or "").strip() == route_key:
            matched_slot = slot
            break
    if matched_slot is None and profile_id:
        for slot in slots:
            if str(slot.get("profile_id") or "").strip() == profile_id:
                matched_slot = slot
                break

    return {
        "route_key": route_key,
        "profile_id": profile_id,
        "profile_health_status": str(getattr(profile, "health_status", "") or "").strip() if profile else "",
        "profile_storage_path": str(getattr(profile, "storage_path", "") or "").strip() if profile else "",
        "browser_online": bool(matched_slot),
        "slot_id": str(matched_slot.get("slot_id") or "").strip() if matched_slot else "",
        "slot_route_key": str(matched_slot.get("route_key") or "").strip() if matched_slot else "",
        "current_email": str(matched_slot.get("current_email") or "").strip() if matched_slot else "",
        "current_project_id": str(matched_slot.get("project_id") or "").strip() if matched_slot else "",
        "session_state": str(matched_slot.get("session_state") or "").strip() if matched_slot else "",
        "page_url": str(matched_slot.get("page_url") or "").strip() if matched_slot else "",
    }


async def _sync_account_credentials(token_id: int) -> dict:
    from ..core.logger import debug_logger
    from ..core.config import config

    token_obj = await token_manager.get_token(token_id)
    if token_obj is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    runtime = await _build_account_runtime_snapshot(token_obj)
    browser_sync: Dict[str, Any] = {
        "attempted": False,
        "success": False,
        "source": "stored_credentials",
        "cookie_count": 0,
        "session_token_present": False,
        "current_email": runtime.get("current_email") or "",
        "error": "",
    }
    debug_logger.log_info(
        f"[API] 同步账号凭证请求: token_id={token_id}, "
        f"captcha_method={config.captcha_method}, route_key={runtime.get('route_key') or '-'}"
    )

    try:
        if runtime.get("browser_online"):
            browser_sync["attempted"] = True
            try:
                await _sync_browser_worker_views()
                from ..services.browser_captcha_extension import ExtensionCaptchaService

                service = await ExtensionCaptchaService.get_instance(db)
                ok, route_key, reason, snapshot = await service.validate_connection_for_token(token_id)
                if not ok:
                    browser_sync["error"] = reason
                else:
                    browser_job = await service.dispatch_ui_job(
                        token_id=token_id,
                        job_type="account_credential_probe",
                        project_id=str(getattr(token_obj, "current_project_id", "") or "").strip() or None,
                        payload={"activate_tab": False},
                        timeout=45,
                    )
                    ui_state = (
                        ((browser_job or {}).get("result") or {}).get("ui_state")
                        if isinstance((browser_job or {}).get("result"), dict)
                        else {}
                    ) or {}
                    cookie_items = ui_state.get("cookie_items") if isinstance(ui_state, dict) else None
                    cookie_count = len(cookie_items) if isinstance(cookie_items, list) else 0
                    browser_sync["cookie_count"] = cookie_count
                    browser_sync["current_email"] = (
                        str(ui_state.get("current_email") or snapshot.get("current_email") or "").strip()
                        if isinstance(snapshot, dict)
                        else str(ui_state.get("current_email") or "").strip()
                    )
                    browser_sync["session_token_present"] = bool(
                        ui_state.get("session_token_present") if isinstance(ui_state, dict) else False
                    )
                    if cookie_count <= 0:
                        browser_sync["error"] = "在线浏览器未返回可用 cookie"
                    else:
                        browser_derived_st = extract_session_token_from_cookie_payload(cookie_items)
                        merged_cookie = merge_browser_cookie_payloads_prefer_live_session(
                            getattr(token_obj, "cookie", None),
                            cookie_items,
                        )
                        normalized_cookie = normalize_cookie_storage_text(merged_cookie)
                        derived_st = browser_derived_st or extract_session_token_from_cookie_payload(
                            normalized_cookie
                        )
                        await token_manager.update_token(
                            token_id=token_id,
                            cookie=normalized_cookie,
                            st=derived_st or None,
                        )
                        browser_sync["success"] = True
                        browser_sync["source"] = "live_browser_profile"
                        browser_sync["session_token_present"] = bool(derived_st)
                        token_obj = await token_manager.get_token(token_id)
                        runtime = await _build_account_runtime_snapshot(token_obj)
                        debug_logger.log_info(
                            f"[API] 浏览器运行时凭证同步成功: token_id={token_id}, "
                            f"route_key={route_key or '-'}, cookie_count={cookie_count}, "
                            f"session_token_present={bool(derived_st)}"
                        )
            except Exception as browser_exc:
                browser_sync["error"] = str(browser_exc)
                debug_logger.log_warning(
                    f"[API] 浏览器运行时凭证同步失败 token_id={token_id}: {browser_exc}"
                )

        refreshed = await token_manager._refresh_at(token_id)
        updated_token = await token_manager.get_token(token_id)
        runtime = await _build_account_runtime_snapshot(updated_token)

        if not refreshed and not browser_sync["success"]:
            has_saved_st = bool(str(getattr(updated_token, "st", "") or "").strip()) if updated_token else False
            has_saved_cookie = bool(str(getattr(updated_token, "cookie", "") or "").strip()) if updated_token else False
            if not has_saved_st and not has_saved_cookie:
                if not runtime.get("browser_online"):
                    raise HTTPException(status_code=409, detail="请先启动该账号的独立浏览器，并等待扩展在线后再同步凭证")
                if not str(runtime.get("current_email") or "").strip():
                    raise HTTPException(status_code=409, detail="浏览器已在线，但还未检测到已登录账号，请先在该 Profile 中登录 Google 账号后再同步凭证")
            error_detail = "账号凭证同步失败"
            if browser_sync["error"]:
                error_detail += f"（浏览器同步失败: {browser_sync['error']}）"
            elif config.captcha_method != "personal":
                error_detail += f"（当前打码模式: {config.captcha_method}，ST 自动刷新仅在 personal 模式下可用）"
            raise HTTPException(status_code=500, detail=error_detail)

        message = "账号凭证同步成功"
        if browser_sync["success"] and refreshed:
            message = "账号凭证同步成功（已从浏览器运行时更新 cookie/ST，并刷新 AT）"
        elif browser_sync["success"] and not refreshed:
            message = "账号浏览器凭证已同步，但 AT 刷新未完成"
        elif refreshed:
            message = "账号凭证同步成功（使用已保存 ST 刷新 AT）"

        return {
            "success": True,
            "message": message,
            "token": {
                "id": updated_token.id,
                "email": updated_token.email,
                "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None,
            },
            "credentials": {
                "st_present": bool(str(getattr(updated_token, "st", "") or "").strip()),
                "cookie_present": bool(str(getattr(updated_token, "cookie", "") or "").strip()),
                "at_present": bool(str(getattr(updated_token, "at", "") or "").strip()),
                "at_expires": updated_token.at_expires.isoformat() if updated_token.at_expires else None,
                "at_refreshed": bool(refreshed),
            },
            "browser_sync": browser_sync,
            "runtime": runtime,
        }
    except HTTPException:
        raise
    except Exception as e:
        debug_logger.log_error(f"[API] 同步账号凭证异常: {str(e)}")
        raise HTTPException(status_code=500, detail=f"账号凭证同步失败: {str(e)}")


def _extract_log_summary(log: Dict[str, Any]) -> Dict[str, Any]:
    request_payload = _safe_json_loads(log.get("request_body"))
    response_payload = _safe_json_loads(log.get("response_body"))

    model = ""
    prompt = ""
    if isinstance(request_payload, dict):
        model = str(request_payload.get("model") or "").strip()
        prompt = str(request_payload.get("prompt") or "").strip()

    generated_assets = {}
    if isinstance(response_payload, dict) and isinstance(response_payload.get("generated_assets"), dict):
        generated_assets = response_payload.get("generated_assets") or {}

    asset_type = str(generated_assets.get("type") or "").strip()
    media_url = (
        generated_assets.get("final_video_url")
        or generated_assets.get("final_image_url")
        or response_payload.get("url") if isinstance(response_payload, dict) else ""
    )
    media_id = generated_assets.get("mediaGenerationId") or ""
    return {
        "model": model,
        "prompt_excerpt": _truncate_text(prompt, limit=120),
        "asset_type": asset_type,
        "media_url": media_url or "",
        "media_generation_id": media_id,
        "has_generated_asset": bool(media_url or media_id),
    }


def _guess_client_hints_from_user_agent(user_agent: str) -> Dict[str, str]:
    """根据 UA 补全常见的 sec-ch-* 头。"""
    ua = (user_agent or "").strip()
    if not ua:
        return {}

    headers: Dict[str, str] = {}
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    is_mobile = any(token in ua for token in ("Android", "iPhone", "iPad", "Mobile"))
    headers["sec-ch-ua-mobile"] = "?1" if is_mobile else "?0"

    if "Windows" in ua:
        headers["sec-ch-ua-platform"] = '"Windows"'
    elif "Macintosh" in ua or "Mac OS X" in ua:
        headers["sec-ch-ua-platform"] = '"macOS"'
    elif "Android" in ua:
        headers["sec-ch-ua-platform"] = '"Android"'
    elif "iPhone" in ua or "iPad" in ua:
        headers["sec-ch-ua-platform"] = '"iOS"'
    elif "Linux" in ua:
        headers["sec-ch-ua-platform"] = '"Linux"'

    if major_match:
        major = major_match.group(1)
        if "Edg/" in ua:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Microsoft Edge";v="{major}", "Chromium";v="{major}"'
            )
        else:
            headers["sec-ch-ua"] = (
                f'"Not:A-Brand";v="99", "Google Chrome";v="{major}", "Chromium";v="{major}"'
            )

    return headers


def _guess_impersonate_from_user_agent(user_agent: str) -> str:
    """从 UA 选择可用的 curl_cffi 浏览器指纹版本。"""
    ua = (user_agent or "").strip()
    major_match = re.search(r"(?:Chrome|Chromium|Edg|EdgA|EdgiOS)/(\d+)", ua)
    if not major_match:
        return "chrome120"

    try:
        major = int(major_match.group(1))
    except Exception:
        return "chrome120"

    if major >= 124:
        return "chrome124"
    if major >= 120:
        return "chrome120"
    return "chrome120"


def _build_proxy_map(proxy_url: str) -> Optional[Dict[str, str]]:
    normalized = (proxy_url or "").strip()
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


async def _resolve_score_test_verify_proxy(
    captcha_method: str,
    browser_proxy_enabled: bool,
    browser_proxy_url: str
) -> tuple[Optional[Dict[str, str]], bool, str, str]:
    """
    选择 score-test 的 verify 请求代理，优先与浏览器打码代理保持一致。
    返回: (proxies, used, source, proxy_url)
    """
    # 浏览器打码模式优先使用 browser_proxy，确保与取 token 出口一致
    if captcha_method in {"browser", "personal"} and browser_proxy_enabled and browser_proxy_url:
        proxy_map = _build_proxy_map(browser_proxy_url)
        if proxy_map:
            return proxy_map, True, "captcha_browser_proxy", browser_proxy_url

    # 退回请求代理配置
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxy_map = _build_proxy_map(proxy_cfg.proxy_url)
                if proxy_map:
                    return proxy_map, True, "request_proxy", proxy_cfg.proxy_url
    except Exception:
        pass

    return None, False, "none", ""


async def _solve_recaptcha_with_api_service(
    method: str,
    website_url: str,
    website_key: str,
    action: str,
    enterprise: bool = False
) -> Optional[str]:
    """使用当前配置的第三方打码服务获取 token。"""
    if method == "yescaptcha":
        client_key = config.yescaptcha_api_key
        base_url = config.yescaptcha_base_url
        task_type = config.yescaptcha_task_type
        min_score = get_yescaptcha_min_score(task_type)
    elif method == "capmonster":
        client_key = config.capmonster_api_key
        base_url = config.capmonster_base_url
        task_type = "RecaptchaV3TaskProxyless"
        min_score = None
    elif method == "ezcaptcha":
        client_key = config.ezcaptcha_api_key
        base_url = config.ezcaptcha_base_url
        task_type = "ReCaptchaV3TaskProxylessS9"
        min_score = None
    elif method == "capsolver":
        client_key = config.capsolver_api_key
        base_url = config.capsolver_base_url
        task_type = "ReCaptchaV3EnterpriseTaskProxyLess" if enterprise else "ReCaptchaV3TaskProxyLess"
        min_score = None
    else:
        raise RuntimeError(f"不支持的打码方式: {method}")

    if not client_key:
        raise RuntimeError(f"{method} API Key 未配置")

    task: Dict[str, Any] = {
        "websiteURL": website_url,
        "websiteKey": website_key,
        "type": task_type,
        "pageAction": action,
    }
    if min_score is not None:
        task["minScore"] = min_score

    if enterprise and method == "capsolver":
        task["isEnterprise"] = True

    create_url = f"{base_url.rstrip('/')}/createTask"
    get_url = f"{base_url.rstrip('/')}/getTaskResult"

    # 获取代理配置
    proxies = None
    try:
        if proxy_manager:
            proxy_cfg = await proxy_manager.get_proxy_config()
            if proxy_cfg and proxy_cfg.enabled and proxy_cfg.proxy_url:
                proxies = {"http": proxy_cfg.proxy_url, "https": proxy_cfg.proxy_url}
                if method == "yescaptcha" and yescaptcha_task_type_requires_proxy(task_type):
                    task.update(proxy_manager.build_captcha_task_proxy_fields(proxy_cfg.proxy_url) or {})
    except Exception:
        pass

    if method == "yescaptcha" and yescaptcha_task_type_requires_proxy(task_type):
        has_task_proxy = bool(task.get("proxyAddress") and task.get("proxyPort") and task.get("proxyType"))
        if not has_task_proxy:
            raise RuntimeError("YesCaptcha 非 ProxyLess 模式需要先配置请求代理")

    async with AsyncSession() as session:
        create_resp = await session.post(
            create_url,
            json={"clientKey": client_key, "task": task},
            impersonate="chrome120",
            timeout=30,
            proxies=proxies
        )
        create_json = create_resp.json()
        task_id = create_json.get("taskId")

        if not task_id:
            error_desc = create_json.get("errorDescription") or create_json.get("errorMessage") or str(create_json)
            raise RuntimeError(f"{method} createTask 失败: {error_desc}")

        for _ in range(40):
            poll_resp = await session.post(
                get_url,
                json={"clientKey": client_key, "taskId": task_id},
                impersonate="chrome120",
                timeout=30,
                proxies=proxies
            )
            poll_json = poll_resp.json()
            if poll_json.get("status") == "ready":
                solution = poll_json.get("solution", {}) or {}
                token = solution.get("gRecaptchaResponse") or solution.get("token")
                if token:
                    return token
                raise RuntimeError(f"{method} 返回结果缺少 token: {poll_json}")

            if poll_json.get("errorId") not in (None, 0):
                error_desc = poll_json.get("errorDescription") or poll_json.get("errorMessage") or str(poll_json)
                raise RuntimeError(f"{method} getTaskResult 失败: {error_desc}")

            await asyncio.sleep(3)

    raise RuntimeError(f"{method} 获取 token 超时")


def set_dependencies(tm: TokenManager, pm: ProxyManager, database: Database, cm: Optional[ConcurrencyManager] = None):
    """Set service instances"""
    global token_manager, proxy_manager, db, concurrency_manager
    token_manager = tm
    proxy_manager = pm
    db = database
    concurrency_manager = cm


# ========== Request Models ==========

class LoginRequest(BaseModel):
    username: str
    password: str


class AddTokenRequest(BaseModel):
    email: str
    st: Optional[str] = None
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1
    auto_create_profile: bool = True
    auto_launch_browser: bool = False
    chrome_path: Optional[str] = None


class BrowserProfileBootstrapRequest(BaseModel):
    auto_launch_browser: bool = False
    chrome_path: Optional[str] = None


class UpdateTokenRequest(BaseModel):
    st: Optional[str] = None  # Session Token (选填，填写时会尝试刷新AT)
    project_id: Optional[str] = None  # 用户可选输入project_id
    project_name: Optional[str] = None
    remark: Optional[str] = None
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: Optional[bool] = None
    video_enabled: Optional[bool] = None
    image_concurrency: Optional[int] = None
    video_concurrency: Optional[int] = None


class BrowserLauncherTokenQueryRequest(BaseModel):
    token_ids: Optional[List[int]] = None
    active_only: bool = False
    sync_route_keys: bool = False


class ImportTokenCookieRequest(BaseModel):
    cookie: str
    sync_st_from_cookie: bool = True
    verify_resident_binding: bool = True


class ProxyConfigRequest(BaseModel):
    proxy_enabled: bool
    proxy_url: Optional[str] = None
    media_proxy_enabled: Optional[bool] = None
    media_proxy_url: Optional[str] = None


class ProxyTestRequest(BaseModel):
    proxy_url: str
    test_url: Optional[str] = "https://labs.google/"
    timeout_seconds: Optional[int] = 15


class CaptchaScoreTestRequest(BaseModel):
    website_url: Optional[str] = "https://antcpt.com/score_detector/"
    website_key: Optional[str] = "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf"
    action: Optional[str] = "homepage"
    verify_url: Optional[str] = "https://antcpt.com/score_detector/verify.php"
    enterprise: Optional[bool] = False


class GenerationConfigRequest(BaseModel):
    image_timeout: Optional[int] = None
    video_timeout: Optional[int] = None
    max_retries: Optional[int] = None


class CallLogicConfigRequest(BaseModel):
    call_mode: str


class SchedulerConfigRequest(BaseModel):
    call_mode: str
    exhausted_credit_threshold: Optional[int] = None
    low_credit_threshold: Optional[int] = None
    image_slot_wait_timeout: Optional[float] = None
    video_slot_wait_timeout: Optional[float] = None
    active_token_credit_refresh_interval_seconds: Optional[int] = None
    rate_limit_auto_unban_hours: Optional[int] = None
    error_ban_threshold: Optional[int] = None


class ChangePasswordRequest(BaseModel):
    username: Optional[str] = None
    old_password: str
    new_password: str


class UpdateAPIKeyRequest(BaseModel):
    new_api_key: str


class UpdateDebugConfigRequest(BaseModel):
    enabled: bool


class UpdateAdminConfigRequest(BaseModel):
    error_ban_threshold: int


class ImportTokenItem(BaseModel):
    """导入账号项"""
    email: Optional[str] = None
    access_token: Optional[str] = None
    session_token: Optional[str] = None
    is_active: bool = True
    captcha_proxy_url: Optional[str] = None
    extension_route_key: Optional[str] = None
    image_enabled: bool = True
    video_enabled: bool = True
    image_concurrency: int = -1
    video_concurrency: int = -1


class ImportTokensRequest(BaseModel):
    """导入账号请求"""
    tokens: List[ImportTokenItem]


class UpstreamProjectMediaQuery(BaseModel):
    token_id: int
    project_id: Optional[str] = None
    include_raw: bool = False


class DispatchBrowserWorkerJobRequest(BaseModel):
    token_id: int
    job_type: str
    project_id: Optional[str] = None
    payload: Dict[str, Any] = {}
    timeout: int = 75


class TokenCaptureTaskRequest(BaseModel):
    task_type: str
    activate_tab: bool = True
    project_id: Optional[str] = None


# ========== Auth Middleware ==========

async def verify_admin_token(authorization: str = Header(None)):
    """Verify admin session token (NOT API key)"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing authorization")

    token = authorization[7:]

    # Check if token is in active session tokens
    if token not in active_admin_tokens:
        raise HTTPException(status_code=401, detail="Invalid or expired admin token")

    return token


# ========== Auth Endpoints ==========

@router.post("/api/admin/login")
async def admin_login(request: LoginRequest):
    """Admin login - returns session token (NOT API key)"""
    admin_config = await db.get_admin_config()

    if not AuthManager.verify_admin(request.username, request.password):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Generate independent session token
    session_token = f"admin-{secrets.token_urlsafe(32)}"

    # Store in active tokens
    active_admin_tokens.add(session_token)

    return {
        "success": True,
        "token": session_token,  # Session token (NOT API key)
        "username": admin_config.username
    }


@router.post("/api/admin/logout")
async def admin_logout(token: str = Depends(verify_admin_token)):
    """Admin logout - invalidate session token"""
    active_admin_tokens.discard(token)
    return {"success": True, "message": "退出登录成功"}


@router.post("/api/admin/change-password")
async def change_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Change admin password"""
    admin_config = await db.get_admin_config()

    # Verify old password
    if not AuthManager.verify_admin(admin_config.username, request.old_password):
        raise HTTPException(status_code=400, detail="旧密码错误")

    # Update password and username in database
    update_params = {"password": request.new_password}
    if request.username:
        update_params["username"] = request.username

    await db.update_admin_config(**update_params)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # 🔑 Invalidate all admin session tokens (force re-login for security)
    active_admin_tokens.clear()

    return {"success": True, "message": "密码修改成功,请重新登录"}


# ========== Token Management ==========

@router.get("/api/tokens")
async def get_tokens(token: str = Depends(verify_admin_token)):
    """Get all tokens with statistics"""
    token_rows = await db.get_all_tokens_with_stats()
    route_status_by_key = {}
    worker_binding_by_token_id = {}
    try:
        for route in await _sync_browser_worker_views():
            route_key = str(route.get("route_key") or "").strip()
            if route_key:
                route_status_by_key[route_key] = route
        for binding in await db.list_token_worker_bindings():
            token_id = binding.get("token_id")
            if token_id is not None:
                worker_binding_by_token_id[int(token_id)] = binding
    except Exception:
        route_status_by_key = {}
        worker_binding_by_token_id = {}

    to_iso = lambda value: value.isoformat() if hasattr(value, "isoformat") else value
    now = datetime.now(timezone.utc)
    exhausted_credit_threshold = config.exhausted_credit_threshold
    low_credit_threshold = config.low_credit_threshold

    def normalize_dt(value):
        if not value:
            return None
        if isinstance(value, str):
            try:
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except Exception:
                return None
        if getattr(value, "tzinfo", None) is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def normalize_credits(value):
        try:
            return int(value or 0)
        except Exception:
            return 0

    result = []
    for row in token_rows:
        route_key = str(row.get("extension_route_key") or "").strip()
        route_status = route_status_by_key.get(route_key) if route_key else None
        worker_binding = worker_binding_by_token_id.get(int(row.get("id")))
        result.append({
            "id": row.get("id"),
            "st": row.get("st"),  # Session Token for editing
            "at": row.get("at"),  # Access Token for editing (从ST转换而来)
            "at_expires": to_iso(row.get("at_expires")) if row.get("at_expires") else None,  # 🆕 AT过期时间
            "at_expired": bool(normalize_dt(row.get("at_expires")) and normalize_dt(row.get("at_expires")) <= now),
            "at_expiring_within_1h": bool(
                normalize_dt(row.get("at_expires"))
                and normalize_dt(row.get("at_expires")) > now
                and (normalize_dt(row.get("at_expires")) - now).total_seconds() < 3600
            ),
            "token": row.get("at"),  # 兼容前端 token.token 的访问方式
            "email": row.get("email"),
            "name": row.get("name"),
            "remark": row.get("remark"),
            "is_active": bool(row.get("is_active")),
            "created_at": to_iso(row.get("created_at")) if row.get("created_at") else None,
            "last_used_at": to_iso(row.get("last_used_at")) if row.get("last_used_at") else None,
            "use_count": row.get("use_count"),
            "credits": normalize_credits(row.get("credits")),  # 🆕 余额
            "credit_exhausted": normalize_credits(row.get("credits")) <= exhausted_credit_threshold,
            "credit_low": exhausted_credit_threshold < normalize_credits(row.get("credits")) <= low_credit_threshold,
            "credit_auto_disabled": (row.get("ban_reason") == "credits_exhausted" and not bool(row.get("is_active"))),
            "credit_status": (
                "auto_disabled_exhausted"
                if row.get("ban_reason") == "credits_exhausted" and not bool(row.get("is_active"))
                else "exhausted"
                if normalize_credits(row.get("credits")) <= exhausted_credit_threshold
                else "low"
                if normalize_credits(row.get("credits")) <= low_credit_threshold
                else "healthy"
            ),
            "user_paygate_tier": row.get("user_paygate_tier"),
            "current_project_id": row.get("current_project_id"),  # 🆕 项目ID
            "current_project_name": row.get("current_project_name"),  # 🆕 项目名称
            "captcha_proxy_url": row.get("captcha_proxy_url") or "",
            "extension_route_key": route_key,
            "extension_route_online": bool(route_status),
            "extension_route_binding_status": str(route_status.get("binding_status") or "") if route_status else "",
            "extension_current_email": str(route_status.get("current_email") or "") if route_status else "",
            "extension_current_project_id": str(route_status.get("project_id") or "") if route_status else "",
            "extension_page_url": str(route_status.get("page_url") or "") if route_status else "",
            "extension_worker_mode": str(route_status.get("worker_mode") or "") if route_status else "",
            "browser_profile_id": str(worker_binding.get("profile_id") or "") if worker_binding else "",
            "browser_profile_health_status": str(worker_binding.get("profile_health_status") or "") if worker_binding else "",
            "browser_profile_proxy_binding": str(worker_binding.get("profile_proxy_binding") or "") if worker_binding else "",
            "browser_slot_id": str(worker_binding.get("slot_id") or "") if worker_binding else "",
            "browser_slot_online": bool(worker_binding and worker_binding.get("slot_id")),
            "browser_slot_worker_node_id": str(worker_binding.get("worker_node_id") or "") if worker_binding else "",
            "browser_slot_route_key": str(worker_binding.get("route_key") or "") if worker_binding else "",
            "browser_slot_current_email": str(worker_binding.get("current_email") or "") if worker_binding else "",
            "browser_slot_project_id": str(worker_binding.get("project_id") or "") if worker_binding else "",
            "browser_slot_session_state": str(worker_binding.get("session_state") or "") if worker_binding else "",
            "browser_slot_page_url": str(worker_binding.get("page_url") or "") if worker_binding else "",
            "image_enabled": bool(row.get("image_enabled")),
            "video_enabled": bool(row.get("video_enabled")),
            "image_concurrency": row.get("image_concurrency"),
            "video_concurrency": row.get("video_concurrency"),
            "image_count": row.get("image_count", 0),
            "video_count": row.get("video_count", 0),
            "error_count": row.get("error_count", 0),
            "today_error_count": row.get("today_error_count", 0),
            "consecutive_error_count": row.get("consecutive_error_count", 0),
            "last_error_at": to_iso(row.get("last_error_at")) if row.get("last_error_at") else None,
            "ban_reason": row.get("ban_reason"),
            "banned_at": to_iso(row.get("banned_at")) if row.get("banned_at") else None,
            "automation_risk_score": row.get("automation_risk_score", 0),
            "automation_risk_state": row.get("automation_risk_state") or "healthy",
            "automation_cooldown_until": (
                to_iso(row.get("automation_cooldown_until")) if row.get("automation_cooldown_until") else None
            ),
            "automation_last_risk_reason": row.get("automation_last_risk_reason"),
            "automation_last_risk_at": (
                to_iso(row.get("automation_last_risk_at")) if row.get("automation_last_risk_at") else None
            ),
        })
    return result  # 直接返回数组,兼容前端


@router.post("/api/internal/browser-launch/tokens")
async def get_browser_launch_tokens(
    request: BrowserLauncherTokenQueryRequest,
    api_key: str = Depends(verify_api_key_flexible),
):
    """供宿主浏览器启动器读取 token 元数据，避免直接访问 SQLite。"""
    requested_ids = {int(token_id) for token_id in (request.token_ids or [])}
    token_rows = await db.get_all_tokens_with_stats()
    result = []

    for row in token_rows:
        token_id = int(row.get("id"))
        if requested_ids and token_id not in requested_ids:
            continue

        is_active = bool(row.get("is_active"))
        if request.active_only and not is_active:
            continue

        route_key = make_route_key(token_id, row.get("extension_route_key"))
        if request.sync_route_keys and str(row.get("extension_route_key") or "").strip() != route_key:
            await token_manager.update_token(token_id=token_id, extension_route_key=route_key)

        result.append(
            {
                "id": token_id,
                "email": str(row.get("email") or "").strip(),
                "is_active": is_active,
                "extension_route_key": route_key,
                "current_project_id": str(row.get("current_project_id") or "").strip(),
                "current_project_name": str(row.get("current_project_name") or "").strip(),
            }
        )

    return {"tokens": result}


@router.post("/api/tokens")
async def add_token(
    request: AddTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Add a new browser-backed account."""
    try:
        new_token = await token_manager.add_token(
            st=str(request.st or "").strip() or None,
            email=request.email,
            project_id=request.project_id,
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )

        # 热更新并发限制，避免必须重启服务
        if concurrency_manager:
            await concurrency_manager.reset_token(
                new_token.id,
                image_concurrency=new_token.image_concurrency,
                video_concurrency=new_token.video_concurrency
            )

        profile_bootstrap = None
        if request.auto_create_profile or request.auto_launch_browser:
            profile_bootstrap = await _bootstrap_browser_profile_for_token(
                new_token,
                auto_launch_browser=request.auto_launch_browser,
                chrome_path=request.chrome_path,
            )

        return {
            "success": True,
            "message": (
                "账号添加成功"
                if not profile_bootstrap
                else (
                    "账号添加成功，独立浏览器 profile 已准备"
                    if profile_bootstrap.get("success")
                    else "账号添加成功，但独立浏览器 profile 初始化失败"
                )
            ),
            "token": {
                "id": new_token.id,
                "email": new_token.email,
                "credits": new_token.credits,
                "project_id": new_token.current_project_id,
                "project_name": new_token.current_project_name,
                "extension_route_key": getattr(new_token, "extension_route_key", None),
            },
            "profile_bootstrap": profile_bootstrap,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"添加账号失败: {str(e)}")


@router.post("/api/tokens/{token_id}/browser-profile")
async def bootstrap_token_browser_profile(
    token_id: int,
    request: BrowserProfileBootstrapRequest,
    token: str = Depends(verify_admin_token),
):
    """为已有 token 准备或启动独立浏览器 profile。"""
    token_obj = await db.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    profile_bootstrap = await _bootstrap_browser_profile_for_token(
        token_obj,
        auto_launch_browser=request.auto_launch_browser,
        chrome_path=request.chrome_path,
    )
    return {
        "success": bool(profile_bootstrap.get("success")),
        "message": (
            "独立浏览器 profile 已准备"
            if profile_bootstrap.get("success") and not profile_bootstrap.get("launched")
            else "独立浏览器 profile 已启动"
            if profile_bootstrap.get("success")
            else "独立浏览器 profile 初始化失败"
        ),
        "profile_bootstrap": profile_bootstrap,
    }


@router.put("/api/tokens/{token_id}")
async def update_token(
    token_id: int,
    request: UpdateTokenRequest,
    token: str = Depends(verify_admin_token)
):
    """Update account metadata; refresh AT only when ST is provided."""
    try:
        normalized_st = str(request.st or "").strip()
        at = None
        at_expires = None
        if normalized_st:
            result = await token_manager.flow_client.st_to_at(normalized_st)
            at = result["access_token"]
            expires = result.get("expires")
            if expires:
                try:
                    at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                except Exception:
                    pass

        await token_manager.update_token(
            token_id=token_id,
            st=normalized_st or None,
            at=at,
            at_expires=at_expires,
            project_id=request.project_id,
            project_name=request.project_name,
            remark=request.remark,
            captcha_proxy_url=request.captcha_proxy_url.strip() if request.captcha_proxy_url is not None else None,
            extension_route_key=request.extension_route_key.strip() if request.extension_route_key is not None else None,
            image_enabled=request.image_enabled,
            video_enabled=request.video_enabled,
            image_concurrency=request.image_concurrency,
            video_concurrency=request.video_concurrency
        )

        # 热更新并发限制，确保管理台修改立即生效
        if concurrency_manager:
            updated_token = await token_manager.get_token(token_id)
            if updated_token:
                await concurrency_manager.reset_token(
                    token_id,
                    image_concurrency=updated_token.image_concurrency,
                    video_concurrency=updated_token.video_concurrency
                )

        return {
            "success": True,
            "message": "账号更新成功" if not normalized_st else "账号更新成功，并已刷新 AT",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/tokens/{token_id}")
async def delete_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Delete token"""
    try:
        await token_manager.delete_token(token_id)
        if concurrency_manager:
            await concurrency_manager.remove_token(token_id)
        return {"success": True, "message": "账号删除成功"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/api/tokens/{token_id}/enable")
async def enable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Enable token"""
    await token_manager.enable_token(token_id)
    return {"success": True, "message": "账号已启用"}


@router.post("/api/tokens/{token_id}/disable")
async def disable_token(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Disable token"""
    await token_manager.disable_token(token_id)
    return {"success": True, "message": "账号已禁用"}


@router.post("/api/tokens/{token_id}/clear-automation-risk")
async def clear_automation_risk(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """Clear automation risk state for a token."""
    result = await token_manager.clear_automation_risk(token_id)
    if not result:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"success": True, "message": "自动化风控已清除", "risk_state": result}


@router.post("/api/tokens/{token_id}/refresh-credits")
async def refresh_credits(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """刷新账号余额"""
    try:
        credits = await token_manager.refresh_credits(token_id)
        return {
            "success": True,
            "message": "余额刷新成功",
            "credits": credits
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"刷新余额失败: {str(e)}")


@router.post("/api/tokens/refresh-credits")
async def refresh_active_tokens_credits(
    token: str = Depends(verify_admin_token)
):
    """批量刷新活跃账号余额，便于充值后立即召回可用账号。"""
    try:
        summary = await token_manager.refresh_active_tokens_credits()
        return {
            "success": True,
            "message": (
                f"余额批量刷新完成: 总计 {summary['total']} 个, "
                f"成功 {summary['success']} 个, 失败 {summary['failed']} 个, "
                f"自动停用 {summary['disabled']} 个, "
                f"召回 {summary['reactivated']} 个"
            ),
            "summary": summary,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"批量刷新余额失败: {str(e)}")


@router.post("/api/tokens/{token_id}/refresh-at")
async def refresh_at(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """兼容旧入口：手动刷新账号 AT。"""
    return await _sync_account_credentials(token_id)


@router.post("/api/tokens/{token_id}/sync-credentials")
async def sync_account_credentials(
    token_id: int,
    token: str = Depends(verify_admin_token)
):
    """同步账号凭证：优先复用当前保存的 ST/cookie，并兼容后续浏览器反向同步。"""
    return await _sync_account_credentials(token_id)


@router.post("/api/tokens/{token_id}/import-cookie")
async def import_token_cookie(
    token_id: int,
    request: ImportTokenCookieRequest,
    token: str = Depends(verify_admin_token),
):
    """导入单个 token 的明文 Cookie，并可立即验证 resident 绑定。"""
    token_obj = await token_manager.get_token(token_id)
    if token_obj is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    normalized_cookie = normalize_cookie_storage_text(request.cookie)
    if not normalized_cookie:
        raise HTTPException(status_code=400, detail="Cookie为空或格式无效")

    cookie_derived_st = extract_session_token_from_cookie_payload(normalized_cookie)
    st_to_save = token_obj.st
    st_changed = False
    if request.sync_st_from_cookie and cookie_derived_st:
        st_to_save = cookie_derived_st
        st_changed = cookie_derived_st != str(getattr(token_obj, "st", "") or "")

    await token_manager.update_token(
        token_id=token_id,
        cookie=normalized_cookie,
        st=st_to_save if request.sync_st_from_cookie and cookie_derived_st else None,
    )

    resident_binding = {
        "attempted": False,
        "ok": False,
        "slot_id": "",
        "reason": "",
    }

    if config.captcha_method == "personal" and request.verify_resident_binding:
        resident_binding["attempted"] = True
        project_id = str(getattr(token_obj, "current_project_id", "") or "").strip()
        if not project_id:
            resident_binding["reason"] = "token 缺少 current_project_id"
        else:
            try:
                from ..services.browser_captcha_personal import BrowserCaptchaService

                service = await BrowserCaptchaService.get_instance(db)
                await service.initialize()
                slot_id, resident_info = await service._ensure_resident_tab(
                    project_id,
                    token_id=token_id,
                    return_slot_key=True,
                )
                resident_binding["slot_id"] = str(slot_id or "")
                resident_binding["ok"] = bool(
                    await service._ensure_resident_token_binding(
                        resident_info,
                        token_id,
                        label=f"admin_import_cookie:{slot_id or 'resident'}",
                    )
                )
                if not resident_binding["ok"]:
                    resident_binding["reason"] = "resident token cookie 绑定未成功"
            except Exception as e:
                resident_binding["reason"] = str(e)

    return {
        "success": True,
        "message": "账号 Cookie 导入成功",
        "token": {
            "id": token_id,
            "cookie_present": True,
            "cookie_length": len(normalized_cookie),
            "session_token_present": bool(cookie_derived_st),
            "st_changed": st_changed,
        },
        "resident_binding": resident_binding,
    }


@router.post("/api/tokens/import")
async def import_tokens(
    request: ImportTokensRequest,
    token: str = Depends(verify_admin_token)
):
    """批量导入账号"""
    from datetime import datetime, timezone

    added = 0
    updated = 0
    errors = []
    # 保持与历史逻辑一致：按 created_at DESC 的结果中，优先命中同邮箱“最新一条”
    existing_by_email = {}
    for existing_token in await token_manager.get_all_tokens():
        if existing_token.email and existing_token.email not in existing_by_email:
            existing_by_email[existing_token.email] = existing_token

    for idx, item in enumerate(request.tokens):
        try:
            st = item.session_token

            if not st:
                errors.append(f"第{idx+1}项: 缺少 session_token")
                continue

            # 使用 ST 转 AT 获取用户信息
            try:
                result = await token_manager.flow_client.st_to_at(st)
                at = result["access_token"]
                email = result.get("user", {}).get("email")
                expires = result.get("expires")

                if not email:
                    errors.append(f"第{idx+1}项: 无法获取邮箱信息")
                    continue

                # 解析过期时间
                at_expires = None
                is_expired = False
                if expires:
                    try:
                        at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
                        # 判断是否过期
                        now = datetime.now(timezone.utc)
                        is_expired = at_expires <= now
                    except:
                        pass

                # 使用邮箱检查是否已存在
                existing = existing_by_email.get(email)

                if existing:
                    # 更新现有账号
                    await token_manager.update_token(
                        token_id=existing.id,
                        st=st,
                        at=at,
                        at_expires=at_expires,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency
                    )
                    # 如果过期则禁用
                    if is_expired:
                        await token_manager.disable_token(existing.id)
                        existing.is_active = False
                    existing.st = st
                    existing.at = at
                    existing.at_expires = at_expires
                    existing.captcha_proxy_url = item.captcha_proxy_url
                    existing.extension_route_key = item.extension_route_key
                    existing.image_enabled = item.image_enabled
                    existing.video_enabled = item.video_enabled
                    existing.image_concurrency = item.image_concurrency
                    existing.video_concurrency = item.video_concurrency
                    updated += 1
                else:
                    # 添加新账号
                    new_token = await token_manager.add_token(
                        st=st,
                        captcha_proxy_url=item.captcha_proxy_url.strip() if item.captcha_proxy_url is not None else None,
                        extension_route_key=item.extension_route_key.strip() if item.extension_route_key is not None else None,
                        image_enabled=item.image_enabled,
                        video_enabled=item.video_enabled,
                        image_concurrency=item.image_concurrency,
                        video_concurrency=item.video_concurrency
                    )
                    # 如果过期则禁用
                    if is_expired:
                        await token_manager.disable_token(new_token.id)
                        new_token.is_active = False
                    existing_by_email[email] = new_token
                    added += 1

            except Exception as e:
                errors.append(f"第{idx+1}项: {str(e)}")

        except Exception as e:
            errors.append(f"第{idx+1}项: {str(e)}")

    return {
        "success": True,
        "added": added,
        "updated": updated,
        "errors": errors if errors else None,
        "message": f"导入完成: 新增 {added} 个, 更新 {updated} 个" + (f", {len(errors)} 个失败" if errors else "")
    }


# ========== Config Management ==========

@router.get("/api/config/proxy")
async def get_proxy_config(token: str = Depends(verify_admin_token)):
    """Get proxy configuration"""
    config = await proxy_manager.get_proxy_config()
    return {
        "success": True,
        "config": {
            "enabled": config.enabled,
            "proxy_url": config.proxy_url,
            "media_proxy_enabled": config.media_proxy_enabled,
            "media_proxy_url": config.media_proxy_url
        }
    }


@router.get("/api/proxy/config")
async def get_proxy_config_alias(token: str = Depends(verify_admin_token)):
    """Get proxy configuration (alias for frontend compatibility)"""
    config = await proxy_manager.get_proxy_config()
    return {
        "proxy_enabled": config.enabled,  # Frontend expects proxy_enabled
        "proxy_url": config.proxy_url,
        "media_proxy_enabled": config.media_proxy_enabled,
        "media_proxy_url": config.media_proxy_url
    }


@router.post("/api/proxy/config")
async def update_proxy_config_alias(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration (alias for frontend compatibility)"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/config/proxy")
async def update_proxy_config(
    request: ProxyConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update proxy configuration"""
    try:
        await proxy_manager.update_proxy_config(
            enabled=request.proxy_enabled,
            proxy_url=request.proxy_url,
            media_proxy_enabled=request.media_proxy_enabled,
            media_proxy_url=request.media_proxy_url
        )
    except ValueError as e:
        return {"success": False, "message": str(e)}
    return {"success": True, "message": "代理配置更新成功"}


@router.post("/api/proxy/test")
async def test_proxy_connectivity(
    request: ProxyTestRequest,
    token: str = Depends(verify_admin_token)
):
    """测试代理是否可访问目标站点（默认 https://labs.google/）"""
    proxy_input = (request.proxy_url or "").strip()
    test_url = (request.test_url or "https://labs.google/").strip()
    timeout_seconds = int(request.timeout_seconds or 15)
    timeout_seconds = max(5, min(timeout_seconds, 60))

    if not proxy_input:
        return {
            "success": False,
            "message": "代理地址为空",
            "test_url": test_url
        }

    try:
        proxy_url = proxy_manager.normalize_proxy_url(proxy_input)
    except ValueError as e:
        return {
            "success": False,
            "message": str(e),
            "test_url": test_url
        }

    start_time = time.time()
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        async with AsyncSession() as session:
            resp = await session.get(
                test_url,
                proxies=proxies,
                timeout=timeout_seconds,
                impersonate="chrome120",
                allow_redirects=True,
                verify=False
            )

        elapsed_ms = int((time.time() - start_time) * 1000)
        status_code = resp.status_code
        final_url = str(resp.url)
        ok = 200 <= status_code < 400

        return {
            "success": ok,
            "message": "代理可用" if ok else f"代理可连通，但目标返回状态码 {status_code}",
            "test_url": test_url,
            "final_url": final_url,
            "status_code": status_code,
            "elapsed_ms": elapsed_ms
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        return {
            "success": False,
            "message": f"代理测试失败: {str(e)}",
            "test_url": test_url,
            "elapsed_ms": elapsed_ms
        }


@router.get("/api/config/generation")
async def get_generation_config(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    config = await db.get_generation_config()
    return {
        "success": True,
        "config": {
            "image_timeout": config.image_timeout,
            "video_timeout": config.video_timeout,
            "max_retries": config.max_retries,
        }
    }


@router.post("/api/config/generation")
async def update_generation_config(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


@router.get("/api/call-logic/config")
async def get_call_logic_config(token: str = Depends(verify_admin_token)):
    """Get token call logic configuration."""
    config_obj = await db.get_call_logic_config()
    call_mode = getattr(config_obj, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(config_obj, "polling_mode_enabled", False) else "default"
    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


@router.post("/api/call-logic/config")
async def update_call_logic_config(
    request: CallLogicConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update token call logic configuration."""
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None:
        raise HTTPException(status_code=400, detail="Invalid call_mode")

    await db.update_call_logic_config(call_mode)
    await db.reload_config_to_memory()

    return {
        "success": True,
        "message": "Token轮询模式保存成功",
        "config": {
            "call_mode": call_mode,
            "polling_mode_enabled": call_mode == "polling",
        }
    }


@router.get("/api/scheduler/config")
async def get_scheduler_config(token: str = Depends(verify_admin_token)):
    """Get centralized scheduler strategy configuration."""
    scheduler_config = await db.get_scheduler_config()
    call_logic_config = await db.get_call_logic_config()
    admin_config = await db.get_admin_config()
    call_mode = getattr(call_logic_config, "call_mode", None)
    if call_mode not in ("default", "polling"):
        call_mode = "polling" if getattr(call_logic_config, "polling_mode_enabled", False) else "default"

    return {
        "success": True,
        "config": {
            "call_mode": call_mode,
            "exhausted_credit_threshold": scheduler_config.exhausted_credit_threshold,
            "low_credit_threshold": scheduler_config.low_credit_threshold,
            "image_slot_wait_timeout": scheduler_config.image_slot_wait_timeout,
            "video_slot_wait_timeout": scheduler_config.video_slot_wait_timeout,
            "active_token_credit_refresh_interval_seconds": (
                scheduler_config.active_token_credit_refresh_interval_seconds
            ),
            "rate_limit_auto_unban_hours": scheduler_config.rate_limit_auto_unban_hours,
            "error_ban_threshold": admin_config.error_ban_threshold if admin_config else 3,
        }
    }


@router.post("/api/scheduler/config")
async def update_scheduler_config(
    request: SchedulerConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update centralized scheduler strategy configuration."""
    call_mode = request.call_mode if request.call_mode in ("default", "polling") else None
    if call_mode is None:
        raise HTTPException(status_code=400, detail="Invalid call_mode")

    if request.error_ban_threshold is not None and int(request.error_ban_threshold) < 1:
        raise HTTPException(status_code=400, detail="Invalid error_ban_threshold")

    await db.update_call_logic_config(call_mode)
    if request.error_ban_threshold is not None:
        await db.update_admin_config(error_ban_threshold=max(1, int(request.error_ban_threshold)))
    await db.update_scheduler_config(
        exhausted_credit_threshold=request.exhausted_credit_threshold,
        low_credit_threshold=request.low_credit_threshold,
        image_slot_wait_timeout=request.image_slot_wait_timeout,
        video_slot_wait_timeout=request.video_slot_wait_timeout,
        active_token_credit_refresh_interval_seconds=request.active_token_credit_refresh_interval_seconds,
        rate_limit_auto_unban_hours=request.rate_limit_auto_unban_hours,
    )
    await db.reload_config_to_memory()

    scheduler_config = await db.get_scheduler_config()
    admin_config = await db.get_admin_config()
    return {
        "success": True,
        "message": "调度策略配置保存成功",
        "config": {
            "call_mode": call_mode,
            "exhausted_credit_threshold": scheduler_config.exhausted_credit_threshold,
            "low_credit_threshold": scheduler_config.low_credit_threshold,
            "image_slot_wait_timeout": scheduler_config.image_slot_wait_timeout,
            "video_slot_wait_timeout": scheduler_config.video_slot_wait_timeout,
            "active_token_credit_refresh_interval_seconds": (
                scheduler_config.active_token_credit_refresh_interval_seconds
            ),
            "rate_limit_auto_unban_hours": scheduler_config.rate_limit_auto_unban_hours,
            "error_ban_threshold": admin_config.error_ban_threshold if admin_config else 3,
        }
    }


# ========== System Info ==========

@router.get("/api/system/info")
async def get_system_info(token: str = Depends(verify_admin_token)):
    """Get system information"""
    stats = await db.get_system_info_stats()

    return {
        "success": True,
        "info": {
            "total_tokens": stats["total_tokens"],
            "active_tokens": stats["active_tokens"],
            "total_credits": stats["total_credits"],
            "version": "1.0.0"
        }
    }


# ========== Additional Routes for Frontend Compatibility ==========

@router.post("/api/login")
async def login(request: LoginRequest):
    """Login endpoint (alias for /api/admin/login)"""
    return await admin_login(request)


@router.post("/api/logout")
async def logout(token: str = Depends(verify_admin_token)):
    """Logout endpoint (alias for /api/admin/logout)"""
    return await admin_logout(token)


@router.get("/health")
async def health_check():
    """Public health check endpoint - no auth required"""
    try:
        return await build_public_health_snapshot(db)
    except Exception:
        return {"backend_running": True, "has_active_tokens": False}


@router.get("/api/stats")
async def get_stats(token: str = Depends(verify_admin_token)):
    """Get statistics for dashboard"""
    return await db.get_dashboard_stats()


@router.get("/api/logs")
async def get_logs(
    limit: int = 100,
    include_summary: bool = False,
    token: str = Depends(verify_admin_token)
):
    """Get lightweight request logs for list view"""
    limit = max(1, min(limit, 100))
    logs = await db.get_logs(limit=limit, include_payload=include_summary)

    result = []
    for log in logs:
        raw_status_code = log.get("status_code")
        try:
            status_code = int(raw_status_code) if raw_status_code is not None else None
        except (TypeError, ValueError):
            status_code = None
        item = {
            "id": log.get("id"),
            "token_id": log.get("token_id"),
            "token_email": log.get("token_email"),
            "token_username": log.get("token_username"),
            "operation": log.get("operation"),
            "status_code": status_code if status_code is not None else raw_status_code,
            "duration": log.get("duration"),
            "status_text": log.get("status_text") or "",
            "progress": log.get("progress") or 0,
            "created_at": log.get("created_at"),
            "updated_at": log.get("updated_at"),
            "error_summary": _extract_error_summary(log.get("response_body_excerpt")) if status_code is not None and status_code >= 400 else "",
        }
        if include_summary:
            item.update(_extract_log_summary(log))
        result.append(item)
    return result


@router.get("/api/logs/{log_id}")
async def get_log_detail(
    log_id: int,
    token: str = Depends(verify_admin_token)
):
    """Get single request log detail (payload loaded on demand)"""
    log = await db.get_log_detail(log_id)
    if not log:
        raise HTTPException(status_code=404, detail="日志不存在")

    error_summary = _extract_error_summary(log.get("response_body"))

    return {
        "id": log.get("id"),
        "token_id": log.get("token_id"),
        "token_email": log.get("token_email"),
        "token_username": log.get("token_username"),
        "operation": log.get("operation"),
        "status_code": log.get("status_code"),
        "duration": log.get("duration"),
        "status_text": log.get("status_text") or "",
        "progress": log.get("progress") or 0,
        "created_at": log.get("created_at"),
        "updated_at": log.get("updated_at"),
        "error_summary": error_summary,
        "request_body": log.get("request_body"),
        "response_body": log.get("response_body")
    }


@router.get("/api/media/upstream/project")
async def get_upstream_project_media(
    token_id: int,
    project_id: Optional[str] = None,
    include_raw: bool = False,
    token: str = Depends(verify_admin_token),
):
    """按 token 拉取上游 Flow 项目初始化数据与媒体列表。"""
    token_obj = await token_manager.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    token_obj = await token_manager.ensure_valid_token(token_obj)
    if not token_obj:
        raise HTTPException(status_code=400, detail="Token无效或AT刷新失败")

    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        raise HTTPException(status_code=400, detail="project_id 是必填参数，请先选择项目")

    known_projects = [item for item in await db.get_projects_by_token(token_id) if item.is_active]
    project_name_by_id = {
        str(item.project_id or "").strip(): str(item.project_name or "").strip()
        for item in known_projects
        if str(item.project_id or "").strip()
    }
    selected_project_name = project_name_by_id.get(resolved_project_id)
    if not selected_project_name and resolved_project_id == str(token_obj.current_project_id or "").strip():
        selected_project_name = str(token_obj.current_project_name or "").strip()


    try:
        upstream_data = await token_manager.flow_client.get_flow_project_initial_data(
            st=token_obj.st,
            project_id=resolved_project_id,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"读取上游项目数据失败: {str(e)}")

    response_payload = {
        "success": True,
        "token": {
            "id": token_obj.id,
            "email": token_obj.email,
            "project_id": resolved_project_id,
            "project_name": selected_project_name or "",
        },
        "media": upstream_data.get("media") or [],
        "media_count": len(upstream_data.get("media") or []),
    }
    if include_raw:
        response_payload["raw"] = upstream_data.get("raw")
    return response_payload


@router.get("/api/media/upstream/projects")
async def get_upstream_media_projects(
    token_id: int,
    token: str = Depends(verify_admin_token),
):
    """返回某个 token 下可供媒体工作台选择的项目列表。"""
    token_obj = await token_manager.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    token_obj = await token_manager.ensure_valid_token(token_obj)
    if not token_obj:
        raise HTTPException(status_code=400, detail="Token无效或AT刷新失败")

    current_project_id = str(token_obj.current_project_id or "").strip()
    current_project_name = str(token_obj.current_project_name or "").strip()
    seen_project_ids = set()
    project_items = []

    for item in [project for project in await db.get_projects_by_token(token_id) if project.is_active]:
        normalized_project_id = str(item.project_id or "").strip()
        if not normalized_project_id or normalized_project_id in seen_project_ids:
            continue
        seen_project_ids.add(normalized_project_id)
        project_items.append({
            "project_id": normalized_project_id,
            "project_name": str(item.project_name or "").strip(),
            "is_current": normalized_project_id == current_project_id,
        })

    if current_project_id and current_project_id not in seen_project_ids:
        project_items.insert(0, {
            "project_id": current_project_id,
            "project_name": current_project_name or current_project_id,
            "is_current": True,
        })

    return {
        "success": True,
        "token": {
            "id": token_obj.id,
            "email": token_obj.email,
            "current_project_id": current_project_id,
            "current_project_name": current_project_name,
        },
        "projects": project_items,
        "project_count": len(project_items),
    }


@router.get("/api/media/upstream/tokens")
async def get_upstream_media_tokens(
    active_only: bool = False,
    token: str = Depends(verify_admin_token),
):
    """返回上游媒体工作台需要的轻量 token 列表，不包含媒体详情。"""
    token_items = (
        await token_manager.get_active_tokens()
        if active_only
        else await token_manager.get_all_tokens()
    )
    return {
        "success": True,
        "tokens": [
            {
                "id": item.id,
                "email": item.email,
                "name": item.name or "",
                "remark": item.remark or "",
                "is_active": bool(item.is_active),
                "current_project_id": item.current_project_id or "",
                "current_project_name": item.current_project_name or "",
                "credits": int(item.credits or 0),
            }
            for item in token_items
        ],
    }


@router.get("/api/media/upstream/preview")
async def resolve_upstream_media_preview(
    token_id: int,
    media_name: str,
    project_id: Optional[str] = None,
    token: str = Depends(verify_admin_token),
):
    """按需解析单个上游媒体的签名预览地址。"""
    token_obj = await token_manager.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    token_obj = await token_manager.ensure_valid_token(token_obj)
    if not token_obj:
        raise HTTPException(status_code=400, detail="Token无效或AT刷新失败")

    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        raise HTTPException(status_code=400, detail="project_id 是必填参数，请先选择项目")
    try:
        preview_url = await token_manager.flow_client.resolve_media_redirect_url(
            st=token_obj.st,
            media_name=media_name,
            project_id=resolved_project_id or None,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"解析上游媒体预览失败: {str(e)}")

    return {
        "success": True,
        "token_id": token_obj.id,
        "media_name": media_name,
        "project_id": resolved_project_id,
        "preview_url": preview_url,
    }


@router.post("/api/media/upstream/upload-image")
async def upload_upstream_project_image(
    token_id: int = Form(...),
    project_id: str = Form(...),
    file: UploadFile = File(...),
    aspect_ratio: str = Form("IMAGE_ASPECT_RATIO_LANDSCAPE"),
    token: str = Depends(verify_admin_token),
):
    """上传图片到指定上游项目。"""
    token_obj = await token_manager.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    token_obj = await token_manager.ensure_valid_token(token_obj)
    if not token_obj:
        raise HTTPException(status_code=400, detail="Token无效或AT刷新失败")

    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        raise HTTPException(status_code=400, detail="project_id 是必填参数，请先选择项目")

    upload_name = str(getattr(file, "filename", "") or "").strip()
    if not upload_name:
        raise HTTPException(status_code=400, detail="缺少上传文件名")

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    detected_mime_type = token_manager.flow_client._detect_image_mime_type(image_bytes)
    if not detected_mime_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="仅支持上传图片文件")

    try:
        media_id = await token_manager.flow_client.upload_image(
            at=str(token_obj.at or "").strip(),
            image_bytes=image_bytes,
            aspect_ratio=aspect_ratio,
            project_id=resolved_project_id,
            token_id=token_obj.id,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上传上游图片失败: {str(e)}")

    return {
        "success": True,
        "token": {
            "id": token_obj.id,
            "email": token_obj.email,
        },
        "project_id": resolved_project_id,
        "file_name": upload_name,
        "mime_type": detected_mime_type,
        "media_id": media_id,
    }


@router.post("/api/media/upstream/upload-video")
async def upload_upstream_project_video(
    token_id: int = Form(...),
    project_id: str = Form(...),
    file: UploadFile = File(...),
    token: str = Depends(verify_admin_token),
):
    """上传视频到指定上游项目。"""
    token_obj = await token_manager.get_token(token_id)
    if not token_obj:
        raise HTTPException(status_code=404, detail="账号不存在")

    token_obj = await token_manager.ensure_valid_token(token_obj)
    if not token_obj:
        raise HTTPException(status_code=400, detail="Token无效或AT刷新失败")

    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        raise HTTPException(status_code=400, detail="project_id 是必填参数，请先选择项目")

    upload_name = str(getattr(file, "filename", "") or "").strip()
    if not upload_name:
        raise HTTPException(status_code=400, detail="缺少上传文件名")

    video_bytes = await file.read()
    if not video_bytes:
        raise HTTPException(status_code=400, detail="上传文件为空")

    detected_mime_type = token_manager.flow_client._detect_video_mime_type(
        video_bytes,
        file_name=upload_name,
        declared_mime_type=str(getattr(file, "content_type", "") or "").strip(),
    )
    if not detected_mime_type.startswith("video/"):
        raise HTTPException(status_code=400, detail="仅支持上传视频文件")

    try:
        media_id = await token_manager.flow_client.upload_video(
            video_bytes=video_bytes,
            file_name=upload_name,
            project_id=resolved_project_id,
            token_id=token_obj.id,
            mime_type=detected_mime_type,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"上传上游视频失败: {str(e)}")

    return {
        "success": True,
        "token": {
            "id": token_obj.id,
            "email": token_obj.email,
        },
        "project_id": resolved_project_id,
        "file_name": upload_name,
        "mime_type": detected_mime_type,
        "media_id": media_id,
    }


@router.delete("/api/logs")
async def clear_logs(token: str = Depends(verify_admin_token)):
    """Clear all logs"""
    try:
        await db.clear_all_logs()
        return {"success": True, "message": "所有日志已清空"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/admin/config")
async def get_admin_config(token: str = Depends(verify_admin_token)):
    """Get admin configuration"""
    admin_config = await db.get_admin_config()

    return {
        "admin_username": admin_config.username,
        "api_key": admin_config.api_key,
        "error_ban_threshold": admin_config.error_ban_threshold,
        "debug_enabled": config.debug_enabled  # Return actual debug status
    }


@router.post("/api/admin/config")
async def update_admin_config(
    request: UpdateAdminConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin configuration (error_ban_threshold)"""
    # Update error_ban_threshold in database
    await db.update_admin_config(error_ban_threshold=request.error_ban_threshold)

    return {"success": True, "message": "配置更新成功"}


@router.post("/api/admin/password")
async def update_admin_password(
    request: ChangePasswordRequest,
    token: str = Depends(verify_admin_token)
):
    """Update admin password"""
    return await change_password(request, token)


@router.post("/api/admin/apikey")
async def update_api_key(
    request: UpdateAPIKeyRequest,
    token: str = Depends(verify_admin_token)
):
    """Update API key (for external API calls, NOT for admin login)"""
    # Update API key in database
    await db.update_admin_config(api_key=request.new_api_key)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "API Key更新成功"}


@router.post("/api/admin/debug")
async def update_debug_config(
    request: UpdateDebugConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update debug configuration"""
    try:
        # Update in-memory config only (not database)
        # This ensures debug mode is automatically disabled on restart
        config.set_debug_enabled(request.enabled)

        status = "enabled" if request.enabled else "disabled"
        return {"success": True, "message": f"Debug mode {status}", "enabled": request.enabled}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update debug config: {str(e)}")


@router.get("/api/generation/timeout")
async def get_generation_timeout(token: str = Depends(verify_admin_token)):
    """Get generation timeout configuration"""
    return await get_generation_config(token)


@router.post("/api/generation/timeout")
async def update_generation_timeout(
    request: GenerationConfigRequest,
    token: str = Depends(verify_admin_token)
):
    """Update generation timeout configuration"""
    await db.update_generation_config(
        image_timeout=request.image_timeout,
        video_timeout=request.video_timeout,
        max_retries=request.max_retries,
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    return {"success": True, "message": "生成配置更新成功"}


# ========== AT Auto Refresh Config ==========

@router.get("/api/token-refresh/config")
async def get_token_refresh_config(token: str = Depends(verify_admin_token)):
    """Get AT auto refresh configuration (默认启用)"""
    return {
        "success": True,
        "config": {
            "at_auto_refresh_enabled": True  # Flow2API默认启用AT自动刷新
        }
    }


@router.post("/api/token-refresh/enabled")
async def update_token_refresh_enabled(
    token: str = Depends(verify_admin_token)
):
    """Update AT auto refresh enabled (Flow2API固定启用,此接口仅用于前端兼容)"""
    return {
        "success": True,
        "message": "Flow2API的AT自动刷新默认启用且无法关闭"
    }


async def _sync_runtime_cache_config():
    from . import routes
    if routes.generation_handler and routes.generation_handler.file_cache:
        file_cache = routes.generation_handler.file_cache
        file_cache.set_timeout(config.cache_timeout)
        await file_cache.refresh_cleanup_task()

# ========== Cache Configuration Endpoints ==========

@router.get("/api/cache/config")
async def get_cache_config(token: str = Depends(verify_admin_token)):
    """Get cache configuration"""
    cache_config = await db.get_cache_config()

    # Calculate effective base URL
    effective_base_url = cache_config.cache_base_url if cache_config.cache_base_url else f"http://127.0.0.1:8000"

    return {
        "success": True,
        "config": {
            "enabled": cache_config.cache_enabled,
            "timeout": cache_config.cache_timeout,
            "base_url": cache_config.cache_base_url or "",
            "effective_base_url": effective_base_url
        }
    }


@router.post("/api/cache/enabled")
async def update_cache_enabled(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache enabled status"""
    enabled = request.get("enabled", False)
    await db.update_cache_config(enabled=enabled)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": f"缓存已{'启用' if enabled else '禁用'}"}


@router.post("/api/cache/config")
async def update_cache_config_full(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update complete cache configuration"""
    enabled = request.get("enabled")
    timeout = request.get("timeout")
    base_url = request.get("base_url")

    if timeout is not None:
        try:
            timeout = int(timeout)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="缓存超时时间必须为整数")
        if timeout < 0:
            raise HTTPException(status_code=400, detail="缓存超时时间不能小于 0")

    await db.update_cache_config(enabled=enabled, timeout=timeout, base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存配置更新成功"}


@router.post("/api/cache/base-url")
async def update_cache_base_url(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update cache base URL"""
    base_url = request.get("base_url", "")
    await db.update_cache_config(base_url=base_url)

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()
    await _sync_runtime_cache_config()

    return {"success": True, "message": "缓存Base URL更新成功"}


@router.post("/api/captcha/config")
async def update_captcha_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update captcha configuration"""
    from ..services.browser_captcha import validate_browser_proxy_url

    captcha_method = request.get("captcha_method")
    yescaptcha_api_key = request.get("yescaptcha_api_key")
    yescaptcha_base_url = request.get("yescaptcha_base_url")
    yescaptcha_task_type = normalize_yescaptcha_task_type(request.get("yescaptcha_task_type"))
    capmonster_api_key = request.get("capmonster_api_key")
    capmonster_base_url = request.get("capmonster_base_url")
    ezcaptcha_api_key = request.get("ezcaptcha_api_key")
    ezcaptcha_base_url = request.get("ezcaptcha_base_url")
    capsolver_api_key = request.get("capsolver_api_key")
    capsolver_base_url = request.get("capsolver_base_url")
    browser_proxy_enabled = request.get("browser_proxy_enabled", False)
    browser_proxy_url = request.get("browser_proxy_url", "")
    browser_count = request.get("browser_count", 1)
    personal_project_pool_size = request.get("personal_project_pool_size")
    personal_max_resident_tabs = request.get("personal_max_resident_tabs")
    browser_personal_fresh_restart_every_n_solves = request.get(
        "browser_personal_fresh_restart_every_n_solves",
        10,
    )
    personal_idle_tab_ttl_seconds = request.get("personal_idle_tab_ttl_seconds")

    # 验证浏览器代理URL格式
    if browser_proxy_enabled and browser_proxy_url:
        is_valid, error_msg = validate_browser_proxy_url(browser_proxy_url)
        if not is_valid:
            return {"success": False, "message": error_msg}

    try:
        browser_count = max(1, min(20, int(browser_count or 1)))
    except Exception:
        return {"success": False, "message": "浏览器实例数量必须是整数"}
    try:
        browser_personal_fresh_restart_every_n_solves = max(
            0,
            int(browser_personal_fresh_restart_every_n_solves if browser_personal_fresh_restart_every_n_solves is not None else 10),
        )
    except Exception:
        return {"success": False, "message": "重置码数必须是整数，0 表示禁用"}

    await db.update_captcha_config(
        captcha_method=captcha_method,
        yescaptcha_api_key=yescaptcha_api_key,
        yescaptcha_base_url=yescaptcha_base_url,
        yescaptcha_task_type=yescaptcha_task_type,
        capmonster_api_key=capmonster_api_key,
        capmonster_base_url=capmonster_base_url,
        ezcaptcha_api_key=ezcaptcha_api_key,
        ezcaptcha_base_url=ezcaptcha_base_url,
        capsolver_api_key=capsolver_api_key,
        capsolver_base_url=capsolver_base_url,
        browser_proxy_enabled=browser_proxy_enabled,
        browser_proxy_url=browser_proxy_url if browser_proxy_enabled else None,
        browser_count=browser_count,
        personal_project_pool_size=personal_project_pool_size,
        personal_max_resident_tabs=personal_max_resident_tabs,
        browser_personal_fresh_restart_every_n_solves=browser_personal_fresh_restart_every_n_solves,
        personal_idle_tab_ttl_seconds=personal_idle_tab_ttl_seconds
    )

    # 🔥 Hot reload: sync database config to memory
    await db.reload_config_to_memory()

    # 如果使用 browser 打码，热重载浏览器数量配置
    if captcha_method == "browser":
        try:
            from ..services.browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            await service.reload_browser_count()
        except Exception:
            pass

    # 如果使用 personal 打码，热重载配置
    if captcha_method == "personal":
        try:
            from ..services.browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            await service.reload_config()
        except Exception as e:
            print(f"[Admin] Personal 配置热更新失败: {e}")

    return {"success": True, "message": "验证码配置更新成功"}


@router.get("/api/captcha/config")
async def get_captcha_config(token: str = Depends(verify_admin_token)):
    """Get captcha configuration"""
    captcha_config = await db.get_captcha_config()
    return {
        "captcha_method": captcha_config.captcha_method,
        "yescaptcha_api_key": captcha_config.yescaptcha_api_key,
        "yescaptcha_base_url": captcha_config.yescaptcha_base_url,
        "yescaptcha_task_type": normalize_yescaptcha_task_type(captcha_config.yescaptcha_task_type),
        "capmonster_api_key": captcha_config.capmonster_api_key,
        "capmonster_base_url": captcha_config.capmonster_base_url,
        "ezcaptcha_api_key": captcha_config.ezcaptcha_api_key,
        "ezcaptcha_base_url": captcha_config.ezcaptcha_base_url,
        "capsolver_api_key": captcha_config.capsolver_api_key,
        "capsolver_base_url": captcha_config.capsolver_base_url,
        "browser_proxy_enabled": captcha_config.browser_proxy_enabled,
        "browser_proxy_url": captcha_config.browser_proxy_url or "",
        "browser_count": captcha_config.browser_count,
        "personal_project_pool_size": captcha_config.personal_project_pool_size,
        "personal_max_resident_tabs": captcha_config.personal_max_resident_tabs,
        "browser_personal_fresh_restart_every_n_solves": captcha_config.browser_personal_fresh_restart_every_n_solves,
        "personal_idle_tab_ttl_seconds": captcha_config.personal_idle_tab_ttl_seconds
    }


@router.post("/api/captcha/score-test")
async def test_captcha_score(
    _request: Optional[CaptchaScoreTestRequest] = None,
    _token: str = Depends(verify_admin_token)
):
    """使用当前打码方式获取 token，并提交到 antcpt 校验分数。"""
    req = _request or CaptchaScoreTestRequest()
    website_url = (req.website_url or "https://antcpt.com/score_detector/").strip()
    website_key = (req.website_key or "6LcR_okUAAAAAPYrPe-HK_0RULO1aZM15ENyM-Mf").strip()
    action = (req.action or "homepage").strip()
    verify_url = (req.verify_url or "https://antcpt.com/score_detector/verify.php").strip()
    enterprise = bool(req.enterprise)

    started_at = time.time()
    captcha_config = await db.get_captcha_config()
    captcha_method = (captcha_config.captcha_method or config.captcha_method or "").strip().lower()
    browser_proxy_enabled = bool(captcha_config.browser_proxy_enabled)
    browser_proxy_url = captcha_config.browser_proxy_url or ""

    token_value: Optional[str] = None
    fingerprint: Optional[Dict[str, Any]] = None
    token_elapsed_ms = 0
    verify_elapsed_ms = 0
    verify_http_status = None
    verify_result: Dict[str, Any] = {}
    verify_headers: Dict[str, str] = {}
    verify_proxy_used = False
    verify_proxy_source = "none"
    verify_proxy_url = ""
    verify_impersonate = "chrome120"
    page_verify_only = captcha_method in {"browser", "personal"}
    verify_mode = "browser_page" if page_verify_only else "server_post"

    try:
        token_start = time.time()
        if captcha_method == "browser":
            from ..services.browser_captcha import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload, browser_id = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = await service.get_fingerprint(browser_id)
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method == "personal":
            from ..services.browser_captcha_personal import BrowserCaptchaService
            service = await BrowserCaptchaService.get_instance(db)
            score_payload = await service.get_custom_score(
                website_url=website_url,
                website_key=website_key,
                verify_url=verify_url,
                action=action,
                enterprise=enterprise
            )
            if isinstance(score_payload, dict):
                token_value = score_payload.get("token")
                verify_elapsed_ms = int(score_payload.get("verify_elapsed_ms") or 0)
                verify_http_status = score_payload.get("verify_http_status")
                verify_result = score_payload.get("verify_result") if isinstance(score_payload.get("verify_result"), dict) else {}
                verify_mode = score_payload.get("verify_mode") or "browser_page"
                score_token_elapsed = score_payload.get("token_elapsed_ms")
                if isinstance(score_token_elapsed, (int, float)):
                    token_elapsed_ms = int(score_token_elapsed)
            if token_value:
                fingerprint = service.get_last_fingerprint()
                verify_proxy_used = bool(browser_proxy_enabled and browser_proxy_url)
                verify_proxy_source = "captcha_browser_proxy" if verify_proxy_used else "browser_direct"
                verify_proxy_url = browser_proxy_url if verify_proxy_used else ""
        elif captcha_method in SUPPORTED_API_CAPTCHA_METHODS:
            if captcha_method == "capsolver" and "antcpt.com" in website_url:
                # CapSolver specifically blocks antcpt.com. Test against labs.google to verify API key config.
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url="https://labs.google/",
                    website_key="6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                    action="IMAGE_GENERATION",
                    enterprise=True
                )
                if token_value:
                    if token_elapsed_ms <= 0:
                        token_elapsed_ms = int((time.time() - token_start) * 1000)
                    return {
                        "success": True,
                        "message": "CapSolver不支持antcpt。已成功用 Google Labs 测试连通性",
                        "captcha_method": captcha_method,
                        "website_url": "https://labs.google/",
                        "website_key": "6LdsFiUsAAAAAIjVDZcuLhaHiDn5nnHVXVRQGeMV",
                        "action": "IMAGE_GENERATION",
                        "verify_url": "",
                        "enterprise": True,
                        "token_acquired": True,
                        "token_preview": _mask_token(token_value),
                        "token_elapsed_ms": token_elapsed_ms,
                        "verify_elapsed_ms": 0,
                        "verify_http_status": 200,
                        "score": 0.9,
                        "verify_result": {"success": True, "message": "跳过分数校验"},
                        "verify_request_meta": {},
                        "browser_proxy_enabled": browser_proxy_enabled,
                        "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                        "fingerprint": fingerprint,
                        "elapsed_ms": int((time.time() - started_at) * 1000)
                    }
            else:
                token_value = await _solve_recaptcha_with_api_service(
                    method=captcha_method,
                    website_url=website_url,
                    website_key=website_key,
                    action=action,
                    enterprise=enterprise
                )
        else:
            return {
                "success": False,
                "message": f"当前打码方式不支持分数测试: {captcha_method}",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }
        if token_elapsed_ms <= 0:
            token_elapsed_ms = int((time.time() - token_start) * 1000)

        if not token_value:
            return {
                "success": False,
                "message": "未获取到 reCAPTCHA token",
                "captcha_method": captcha_method,
                "website_url": website_url,
                "website_key": website_key,
                "action": action,
                "verify_url": verify_url,
                "enterprise": enterprise,
                "token_acquired": False,
                "token_elapsed_ms": token_elapsed_ms,
                "browser_proxy_enabled": browser_proxy_enabled,
                "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
                "fingerprint": fingerprint,
                "elapsed_ms": int((time.time() - started_at) * 1000)
            }

        if verify_mode == "server_post" and not page_verify_only:
            verify_start = time.time()
            verify_headers = {
                "accept": "application/json, text/javascript, */*; q=0.01",
                "content-type": "application/json",
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                ua = (fingerprint.get("user_agent") or "").strip()
                lang = (fingerprint.get("accept_language") or "").strip()
                sec_ch_ua = (fingerprint.get("sec_ch_ua") or "").strip()
                sec_ch_ua_mobile = (fingerprint.get("sec_ch_ua_mobile") or "").strip()
                sec_ch_ua_platform = (fingerprint.get("sec_ch_ua_platform") or "").strip()

                if ua:
                    verify_headers["user-agent"] = ua
                if lang:
                    verify_headers["accept-language"] = lang if "," in lang else f"{lang},zh;q=0.9"
                if sec_ch_ua:
                    verify_headers["sec-ch-ua"] = sec_ch_ua
                if sec_ch_ua_mobile:
                    verify_headers["sec-ch-ua-mobile"] = sec_ch_ua_mobile
                if sec_ch_ua_platform:
                    verify_headers["sec-ch-ua-platform"] = sec_ch_ua_platform

            if verify_headers.get("user-agent"):
                for header_name, header_value in _guess_client_hints_from_user_agent(
                    verify_headers.get("user-agent", "")
                ).items():
                    if header_value and not verify_headers.get(header_name):
                        verify_headers[header_name] = header_value
                verify_impersonate = _guess_impersonate_from_user_agent(verify_headers.get("user-agent", ""))

            verify_proxies, verify_proxy_used, verify_proxy_source, verify_proxy_url = (
                await _resolve_score_test_verify_proxy(
                    captcha_method=captcha_method,
                    browser_proxy_enabled=browser_proxy_enabled,
                    browser_proxy_url=browser_proxy_url
                )
            )

            async with AsyncSession() as session:
                verify_resp = await session.post(
                    verify_url,
                    json={"g-recaptcha-response": token_value},
                    headers=verify_headers,
                    proxies=verify_proxies,
                    impersonate=verify_impersonate,
                    timeout=30
                )
            verify_elapsed_ms = int((time.time() - verify_start) * 1000)
            verify_http_status = verify_resp.status_code

            try:
                verify_result = verify_resp.json()
            except Exception:
                verify_result = {"raw": verify_resp.text}
        else:
            verify_headers = {
                "origin": "https://antcpt.com",
                "referer": website_url,
                "x-requested-with": "XMLHttpRequest",
            }
            if isinstance(fingerprint, dict):
                verify_headers.update({
                    "user-agent": fingerprint.get("user_agent", ""),
                    "accept-language": fingerprint.get("accept_language", ""),
                    "sec-ch-ua": fingerprint.get("sec_ch_ua", ""),
                    "sec-ch-ua-mobile": fingerprint.get("sec_ch_ua_mobile", ""),
                    "sec-ch-ua-platform": fingerprint.get("sec_ch_ua_platform", ""),
                })

        verify_success = bool(verify_result.get("success")) if isinstance(verify_result, dict) else False
        score_value = verify_result.get("score") if isinstance(verify_result, dict) else None

        return {
            "success": verify_success,
            "message": "分数校验成功" if verify_success else "分数校验未通过",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": True,
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "score": score_value,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"分数测试失败: {str(e)}",
            "captcha_method": captcha_method,
            "website_url": website_url,
            "website_key": website_key,
            "action": action,
            "verify_url": verify_url,
            "enterprise": enterprise,
            "token_acquired": bool(token_value),
            "token_preview": _mask_token(token_value),
            "token_elapsed_ms": token_elapsed_ms,
            "verify_elapsed_ms": verify_elapsed_ms,
            "verify_http_status": verify_http_status,
            "verify_result": verify_result,
            "verify_request_meta": {
                "mode": verify_mode,
                "proxy_used": verify_proxy_used,
                "user_agent": verify_headers.get("user-agent", ""),
                "accept_language": verify_headers.get("accept-language", ""),
                "sec_ch_ua": verify_headers.get("sec-ch-ua", ""),
                "sec_ch_ua_mobile": verify_headers.get("sec-ch-ua-mobile", ""),
                "sec_ch_ua_platform": verify_headers.get("sec-ch-ua-platform", ""),
                "origin": verify_headers.get("origin", ""),
                "referer": verify_headers.get("referer", ""),
                "x_requested_with": verify_headers.get("x-requested-with", ""),
                "proxy_source": verify_proxy_source,
                "proxy_url": verify_proxy_url,
                "impersonate": verify_impersonate,
            },
            "browser_proxy_enabled": browser_proxy_enabled,
            "browser_proxy_url": browser_proxy_url if browser_proxy_enabled else "",
            "fingerprint": fingerprint,
            "elapsed_ms": int((time.time() - started_at) * 1000)
        }


# ========== Plugin Configuration Endpoints ==========


async def _sync_browser_worker_views() -> list[dict]:
    """Refresh Phase-A browser worker views from current token and extension state."""
    await db.sync_browser_profiles_from_tokens()
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db)
    routes = await service.list_route_status()
    await db.sync_extension_routes_to_worker_slots(routes)
    return routes


CAPTURE_TASK_TYPE_LABELS: Dict[str, str] = {
    "text_to_video": "文生视频",
    "image_to_video": "图生视频",
    "edit_existing_video": "从已有视频编辑",
    "edit_uploaded_video": "上传视频编辑",
}


def _normalize_capture_task_type(raw_value: Optional[str]) -> tuple[str, str]:
    raw = str(raw_value or "").strip()
    alias_map = {
        "text_to_video": "text_to_video",
        "文生视频": "text_to_video",
        "image_to_video": "image_to_video",
        "img_to_video": "image_to_video",
        "图生视频": "image_to_video",
        "edit_existing_video": "edit_existing_video",
        "existing_video_edit": "edit_existing_video",
        "从已有视频编辑": "edit_existing_video",
        "edit_uploaded_video": "edit_uploaded_video",
        "uploaded_video_edit": "edit_uploaded_video",
        "上传视频编辑": "edit_uploaded_video",
    }
    normalized = alias_map.get(raw)
    if not normalized:
        supported = ", ".join(CAPTURE_TASK_TYPE_LABELS.keys())
        raise HTTPException(status_code=400, detail=f"不支持的采集任务类型: {raw or '-'}，支持: {supported}")
    return normalized, CAPTURE_TASK_TYPE_LABELS[normalized]


async def _dispatch_token_capture_task(
    *,
    token_id: int,
    action: str,
    task_type: str,
    activate_tab: bool = True,
    project_id: Optional[str] = None,
) -> Dict[str, Any]:
    action_name = str(action or "").strip()
    if action_name not in {"start", "status", "stop"}:
        raise HTTPException(status_code=400, detail=f"不支持的采集动作: {action_name or '-'}")

    token_obj = await token_manager.get_token(token_id)
    if token_obj is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    normalized_task_type, task_label = _normalize_capture_task_type(task_type)

    await _sync_browser_worker_views()
    runtime = await _build_account_runtime_snapshot(token_obj)

    resolved_project_id = str(project_id or "").strip()
    if not resolved_project_id:
        resolved_project_id = str(runtime.get("current_project_id") or "").strip()
    if not resolved_project_id:
        resolved_project_id = str(getattr(token_obj, "current_project_id", "") or "").strip()

    from ..services.browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db)
    request_payload: Dict[str, Any] = {
        "activate_tab": bool(activate_tab),
        "capture_task_type": normalized_task_type,
        "capture_task_label": task_label,
    }
    if action_name == "start":
        timestamp = int(time.time())
        request_payload.update({
            "trace_id": f"capture-{normalized_task_type}-{token_id}-{timestamp}",
            "run_id": f"run-{normalized_task_type}-{timestamp}",
            "session_id": f"capture-{normalized_task_type}",
        })

    try:
        result = await service.dispatch_ui_job(
            token_id=token_id,
            job_type=f"capture_mode_{action_name}",
            project_id=resolved_project_id or None,
            payload=request_payload,
            timeout=90,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    result_payload = result.get("result") if isinstance(result, dict) else {}
    capture_status = result_payload.get("capture_status") if isinstance(result_payload, dict) else {}

    return {
        "success": str(result.get("status") or "") == "success",
        "action": action_name,
        "token_id": token_id,
        "email": str(getattr(token_obj, "email", "") or "").strip(),
        "task_type": normalized_task_type,
        "task_label": task_label,
        "project_id": resolved_project_id,
        "runtime": runtime,
        "job": result,
        "capture_status": capture_status if isinstance(capture_status, dict) else {},
        "trace_id": str((result_payload or {}).get("trace_id") or ((capture_status or {}).get("trace_id") if isinstance(capture_status, dict) else "") or "").strip(),
        "run_id": str((result_payload or {}).get("run_id") or ((capture_status or {}).get("run_id") if isinstance(capture_status, dict) else "") or "").strip(),
    }

@router.get("/api/extension/routes")
async def get_extension_routes(token: str = Depends(verify_admin_token)):
    """Get active extension/browser worker routes with binding summaries."""
    routes = await _sync_browser_worker_views()
    return {
        "success": True,
        "routes": routes,
    }


@router.get("/api/browser-worker/profiles")
async def get_browser_worker_profiles(token: str = Depends(verify_admin_token)):
    """List browser profile assets generated for token pool accounts."""
    await db.sync_browser_profiles_from_tokens()
    return {
        "success": True,
        "profiles": await db.list_browser_profiles(),
    }


@router.get("/api/browser-worker/nodes")
async def get_browser_worker_nodes(token: str = Depends(verify_admin_token)):
    """List worker nodes that can host browser slots."""
    await _sync_browser_worker_views()
    return {
        "success": True,
        "nodes": [item.model_dump() for item in await db.list_worker_nodes()],
    }


@router.get("/api/browser-worker/slots")
async def get_browser_worker_slots(token: str = Depends(verify_admin_token)):
    """List current online worker slots derived from extension routes."""
    await _sync_browser_worker_views()
    return {
        "success": True,
        "slots": await db.list_worker_slots(),
    }


@router.get("/api/browser-worker/jobs")
async def get_browser_worker_jobs(limit: int = 100, token: str = Depends(verify_admin_token)):
    """List recent browser worker jobs."""
    return {
        "success": True,
        "jobs": [item.model_dump() for item in await db.list_worker_jobs(limit=limit)],
    }


@router.post("/api/browser-worker/jobs/dispatch")
async def dispatch_browser_worker_job(
    request: DispatchBrowserWorkerJobRequest,
    token: str = Depends(verify_admin_token),
):
    """Dispatch a UI-level browser worker job to the matched extension slot."""
    await _sync_browser_worker_views()
    from ..services.browser_captcha_extension import ExtensionCaptchaService

    service = await ExtensionCaptchaService.get_instance(db)
    result = await service.dispatch_ui_job(
        token_id=request.token_id,
        job_type=request.job_type,
        project_id=request.project_id,
        payload=request.payload,
        timeout=request.timeout,
    )
    return {
        "success": result.get("status") == "success",
        "job": result,
    }


@router.post("/api/tokens/{token_id}/capture/start")
async def start_token_capture_task(
    token_id: int,
    request: TokenCaptureTaskRequest,
    token: str = Depends(verify_admin_token),
):
    """Start capture mode for a selected token using a predefined workflow type."""
    _ = token
    return await _dispatch_token_capture_task(
        token_id=token_id,
        action="start",
        task_type=request.task_type,
        activate_tab=request.activate_tab,
        project_id=request.project_id,
    )


@router.post("/api/tokens/{token_id}/capture/status")
async def get_token_capture_task_status(
    token_id: int,
    request: TokenCaptureTaskRequest,
    token: str = Depends(verify_admin_token),
):
    """Get capture mode status for a selected token."""
    _ = token
    return await _dispatch_token_capture_task(
        token_id=token_id,
        action="status",
        task_type=request.task_type,
        activate_tab=request.activate_tab,
        project_id=request.project_id,
    )


@router.post("/api/tokens/{token_id}/capture/stop")
async def stop_token_capture_task(
    token_id: int,
    request: TokenCaptureTaskRequest,
    token: str = Depends(verify_admin_token),
):
    """Stop capture mode for a selected token."""
    _ = token
    return await _dispatch_token_capture_task(
        token_id=token_id,
        action="stop",
        task_type=request.task_type,
        activate_tab=request.activate_tab,
        project_id=request.project_id,
    )


@router.get("/api/plugin/config")
async def get_plugin_config(request: Request, token: str = Depends(verify_admin_token)):
    """Get plugin configuration"""
    plugin_config = await db.get_plugin_config()

    # Get the actual domain and port from the request
    # This allows the connection URL to reflect the user's actual access path
    host_header = request.headers.get("host", "")

    # Generate connection URL based on actual request
    if host_header:
        # Use the actual domain/IP and port from the request
        connection_url = f"http://{host_header}/api/plugin/update-token"
    else:
        # Fallback to config-based URL
        from ..core.config import config
        server_host = config.server_host
        server_port = config.server_port

        if server_host == "0.0.0.0":
            connection_url = f"http://127.0.0.1:{server_port}/api/plugin/update-token"
        else:
            connection_url = f"http://{server_host}:{server_port}/api/plugin/update-token"

    return {
        "success": True,
        "config": {
            "connection_token": plugin_config.connection_token,
            "connection_url": connection_url,
            "auto_enable_on_update": plugin_config.auto_enable_on_update
        }
    }


@router.post("/api/plugin/config")
async def update_plugin_config(
    request: dict,
    token: str = Depends(verify_admin_token)
):
    """Update plugin configuration"""
    connection_token = request.get("connection_token", "")
    auto_enable_on_update = request.get("auto_enable_on_update", True)  # 默认开启

    # Generate random token if empty
    if not connection_token:
        connection_token = secrets.token_urlsafe(32)

    await db.update_plugin_config(
        connection_token=connection_token,
        auto_enable_on_update=auto_enable_on_update
    )

    return {
        "success": True,
        "message": "插件配置更新成功",
        "connection_token": connection_token,
        "auto_enable_on_update": auto_enable_on_update
    }


@router.post("/api/plugin/update-token")
async def plugin_update_token(request: dict, authorization: Optional[str] = Header(None)):
    """Receive token update from Chrome extension (no admin auth required, uses connection_token)"""
    # Verify connection token
    plugin_config = await db.get_plugin_config()

    # Extract token from Authorization header
    provided_token = None
    if authorization:
        if authorization.startswith("Bearer "):
            provided_token = authorization[7:]
        else:
            provided_token = authorization

    # Check if token matches
    if not plugin_config.connection_token or provided_token != plugin_config.connection_token:
        raise HTTPException(status_code=401, detail="Invalid connection token")

    # Extract session token from request
    session_token = request.get("session_token")

    if not session_token:
        raise HTTPException(status_code=400, detail="Missing session_token")

    # Step 1: Convert ST to AT to get user info (including email)
    try:
        result = await token_manager.flow_client.st_to_at(session_token)
        at = result["access_token"]
        expires = result.get("expires")
        user_info = result.get("user", {})
        email = user_info.get("email", "")

        if not email:
            raise HTTPException(status_code=400, detail="Failed to get email from session token")

        # Parse expiration time
        from datetime import datetime
        at_expires = None
        if expires:
            try:
                at_expires = datetime.fromisoformat(expires.replace('Z', '+00:00'))
            except:
                pass

    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid session token: {str(e)}")

    # Step 2: Check if token with this email exists
    existing_token = await db.get_token_by_email(email)

    if existing_token:
        # Update existing token
        try:
            # Update token
            await token_manager.update_token(
                token_id=existing_token.id,
                st=session_token,
                at=at,
                at_expires=at_expires
            )

            # Check if auto-enable is enabled and token is disabled
            if plugin_config.auto_enable_on_update and not existing_token.is_active:
                await token_manager.enable_token(existing_token.id)
                return {
                    "success": True,
                    "message": f"Token updated and auto-enabled for {email}",
                    "action": "updated",
                    "auto_enabled": True
                }

            return {
                "success": True,
                "message": f"Token updated for {email}",
                "action": "updated"
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to update token: {str(e)}")
    else:
        # Add new token
        try:
            new_token = await token_manager.add_token(
                st=session_token,
                remark="Added by Chrome Extension"
            )

            return {
                "success": True,
                "message": f"Token added for {new_token.email}",
                "action": "added",
                "token_id": new_token.id
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to add token: {str(e)}")
