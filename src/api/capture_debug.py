from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter
from fastapi import HTTPException

from ..services.browser_captcha_extension import ExtensionCaptchaService
from ..services.capture_trace_store import CaptureTraceStore


router = APIRouter(prefix="/debug/capture-trace", tags=["capture-debug"])
trace_store = CaptureTraceStore()
db = None


def set_dependencies(database) -> None:
    global db
    db = database


@router.get("/health")
async def capture_trace_health() -> Dict[str, Any]:
    return {
        "success": True,
        "service": "capture-trace",
        "base_dir": str(trace_store.base_dir),
    }


@router.post("/event")
async def capture_trace_event(payload: Dict[str, Any]) -> Dict[str, Any]:
    result = trace_store.append_event(payload or {})
    return result


async def _dispatch_capture_job(
    *,
    job_type: str,
    token_id: int,
    project_id: str = "",
    payload: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    if db is None:
        raise HTTPException(status_code=500, detail="capture debug dependencies not initialized")
    service = await ExtensionCaptchaService.get_instance(db)
    try:
        return await service.dispatch_ui_job(
            token_id=token_id,
            job_type=job_type,
            project_id=project_id,
            payload=payload or {},
            timeout=90,
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/start")
async def capture_trace_start(payload: Dict[str, Any]) -> Dict[str, Any]:
    token_id = int(payload.get("token_id") or 0)
    if token_id <= 0:
        raise HTTPException(status_code=400, detail="token_id is required")
    project_id = str(payload.get("project_id") or "").strip()
    job_payload = {
        "trace_id": str(payload.get("trace_id") or "").strip(),
        "run_id": str(payload.get("run_id") or "").strip(),
        "session_id": str(payload.get("session_id") or "video-ui-capture").strip(),
        "capture_task_type": str(payload.get("capture_task_type") or "text_to_video").strip(),
        "capture_task_label": str(payload.get("capture_task_label") or "").strip(),
        "endpoint_url": str(payload.get("endpoint_url") or "").strip(),
        "activate_tab": bool(payload.get("activate_tab")),
    }
    return await _dispatch_capture_job(
        job_type="capture_mode_start",
        token_id=token_id,
        project_id=project_id,
        payload=job_payload,
    )


@router.post("/stop")
async def capture_trace_stop(payload: Dict[str, Any]) -> Dict[str, Any]:
    token_id = int(payload.get("token_id") or 0)
    if token_id <= 0:
        raise HTTPException(status_code=400, detail="token_id is required")
    project_id = str(payload.get("project_id") or "").strip()
    return await _dispatch_capture_job(
        job_type="capture_mode_stop",
        token_id=token_id,
        project_id=project_id,
        payload={"activate_tab": bool(payload.get("activate_tab"))},
    )


@router.post("/status")
async def capture_trace_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    token_id = int(payload.get("token_id") or 0)
    if token_id <= 0:
        raise HTTPException(status_code=400, detail="token_id is required")
    project_id = str(payload.get("project_id") or "").strip()
    return await _dispatch_capture_job(
        job_type="capture_mode_status",
        token_id=token_id,
        project_id=project_id,
        payload={"activate_tab": bool(payload.get("activate_tab"))},
    )
