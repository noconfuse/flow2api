#!/usr/bin/env python3
"""Debug helper: dispatch a single video_ui_workflow job end-to-end.

设计目的：
- 不上传媒体：复用 token 当前项目里已有的 media_id（避免每次把图片往 Flow 推）
- 单 token / 单任务 / 单条 prompt，把 workflow_state 全字段 dump 到 .dbg/
- 触发 force_relaunch，确保 extension/video-ui-workflow.js 是最新版本
- 失败时不抛异常，把 status/error/steps 全部落盘，方便贴回来对照

典型用法：
  .venv/bin/python scripts/debug_dispatch_video_workflow.py \\
      --token-id 7 --project-id 6be0a83c-46d2-4207-a921-b9803e8fae4f \\
      --prompt "OMNI PROMPT 1 — Random-Squeeze Comparison Hook. ..." \\
      --config config/setting.toml

  # 重试模式（最多 5 次）：
  .venv/bin/python scripts/debug_dispatch_video_workflow.py \\
      --token-id 7 --retry 5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib import error, request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


REPO_ROOT = Path(__file__).resolve().parent.parent
DEBUG_DIR = REPO_ROOT / ".dbg" / "video_ui_workflow"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)


def load_config_credentials(config_path: str) -> Dict[str, str]:
    if tomllib is None:
        raise RuntimeError("当前 Python 不支持 tomllib，无法读取 TOML 配置")
    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    # setting.toml 里是 [global] 下嵌套的 admin_username / admin_password
    global_section = data.get("global") or {}
    username = str(global_section.get("admin_username") or data.get("admin_username") or "").strip()
    password = str(global_section.get("admin_password") or data.get("admin_password") or "").strip()
    if not username or not password:
        raise RuntimeError(f"配置文件缺少 admin_username/admin_password: {path}")
    return {"username": username, "password": password}


def http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 60.0,
) -> Dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url=url, method=method.upper(), data=body, headers=request_headers)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            detail_json = json.loads(raw) if raw else {}
            if isinstance(detail_json, dict):
                detail = detail_json.get("detail") or detail_json.get("message") or raw
            else:
                detail = raw
        except json.JSONDecodeError:
            detail = raw
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"请求失败: {exc.reason}") from exc


def resolve_admin_token(args: argparse.Namespace) -> str:
    existing = str(args.admin_token or os.getenv("GFLOW_ADMIN_TOKEN") or "").strip()
    if existing:
        return existing

    username = str(args.username or os.getenv("GFLOW_ADMIN_USERNAME") or "").strip()
    password = str(args.password or os.getenv("GFLOW_ADMIN_PASSWORD") or "").strip()

    if args.config:
        config_creds = load_config_credentials(args.config)
        username = username or config_creds["username"]
        password = password or config_creds["password"]

    if not username or not password:
        raise RuntimeError(
            "缺少管理员凭证；请提供 --admin-token，或提供 --username/--password，"
            "或通过 --config 指向包含 admin_username/admin_password 的 TOML 配置"
        )

    login_result = http_json(
        "POST",
        f"{args.base_url}/api/admin/login",
        payload={"username": username, "password": password},
    )
    token = str(login_result.get("token") or "").strip()
    if not token:
        raise RuntimeError("登录成功但未返回 admin token")
    return token


def fetch_token_projects(base_url: str, admin_token: str, token_id: int) -> List[Dict[str, Any]]:
    result = http_json(
        "GET",
        f"{base_url}/api/media/upstream/projects?token_id={token_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    return list(result.get("projects") or [])


def fetch_project_media(base_url: str, admin_token: str, token_id: int, project_id: str) -> List[Dict[str, Any]]:
    result = http_json(
        "GET",
        f"{base_url}/api/media/upstream/project?token_id={token_id}&project_id={project_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    return list(result.get("media") or [])


def derive_video_ui_settings(model_key: str, aspect_ratio: str, outputs_per_prompt: int = 1) -> Dict[str, Any]:
    """Replicate FlowClient._derive_extension_video_ui_settings — keep in sync."""
    normalized_model_key = str(model_key or "").strip().lower()
    normalized_aspect_ratio = str(aspect_ratio or "").strip().upper()

    desired_duration_seconds = ""
    for candidate in ("10", "8", "6", "4"):
        if f"_{candidate}s" in normalized_model_key:
            desired_duration_seconds = candidate
            break

    desired_aspect_ratio_label = ""
    if "PORTRAIT" in normalized_aspect_ratio or "portrait" in normalized_model_key:
        desired_aspect_ratio_label = "9:16"
    elif "LANDSCAPE" in normalized_aspect_ratio or "landscape" in normalized_model_key:
        desired_aspect_ratio_label = "16:9"

    desired_model_display_name = ""
    if (
        "omni-flash" in normalized_model_key
        or "abra_edit" in normalized_model_key
        or normalized_model_key.startswith("abra")
        or "abra_r2v" in normalized_model_key
        or "abra_t2v" in normalized_model_key
    ):
        desired_model_display_name = "Omni Flash"
    elif "veo_3_1" in normalized_model_key or normalized_model_key.startswith("veo"):
        if "lite" in normalized_model_key:
            desired_model_display_name = "Veo 3.1 - Lite"
        elif (
            "fast" in normalized_model_key
            or "ultra" in normalized_model_key
            or "relaxed" in normalized_model_key
        ):
            desired_model_display_name = "Veo 3.1 - Fast"
        else:
            desired_model_display_name = "Veo 3.1 - Quality"

    return {
        "desired_model_display_name": desired_model_display_name,
        "desired_duration_seconds": desired_duration_seconds,
        "desired_aspect_ratio_label": desired_aspect_ratio_label,
        "desired_outputs_per_prompt": max(1, int(outputs_per_prompt or 1)),
    }


def select_image_media(media: List[Dict[str, Any]], wanted_slot: str) -> Optional[Dict[str, Any]]:
    """从项目媒体中选一张 image 类的给 reference_assets 用。

    注意：reference_assets 里的 reference_texts **不**用于在 picker 里定位图片——
    picker 匹配的唯一真源是 media_id（picker tile 文本里的 "flow2api_upload_xxx.png"
    本身就是项目内的 media_id）。这里只发 [slot_name]，跟生产路径 _build_reference_asset_specs 一致。
    """
    for item in media:
        if not isinstance(item, dict):
            continue
        # 上游 API 返回的是 media_type（下划线），早期也曾用 mediaType；两个都认
        kind = (
            str(item.get("media_type") or "").strip().lower()
            or str(item.get("mediaType") or "").strip().lower()
            or str(item.get("kind") or "").strip().lower()
        )
        if "image" in kind:
            # 优先用 media_id（UUID 形式，对应 src URL 里的 ?name= 参数，是 picker 唯一匹配路径），
            # fallback 到 media.name（filename）以兼容上游没有 mediaId 字段的旧数据
            media_id_value = str(item.get("media_id") or item.get("mediaId") or item.get("name") or "").strip()
            return {
                "slot": wanted_slot,
                "media_id": media_id_value,
                "kind": "image",
                "mode": "mention",
                "reference_texts": [wanted_slot],
            }
    return None


def build_default_prompt() -> str:
    return (
        "OMNI PROMPT 1 — Random-Squeeze Comparison Hook.\n"
        "@image_1 = muscular main speaker (deep brown skin, compact short locs, shirtless).\n"
        "@image_2 = slim silent man (high textured afro, fitted black compression shirt).\n"
        "Same narrow sunny high-rise balcony throughout; main foreground screen-right, "
        "slim man screen-left; raw handheld vertical iPhone, 24-28mm lens."
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Debug helper: dispatch a single video_ui_workflow job without uploading media."
    )
    parser.add_argument("--base-url", default=os.getenv("GFLOW_BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--token-id", type=int, required=True, help="目标账号 ID")
    parser.add_argument("--project-id", default="", help="Flow 项目 ID；留空则取 current_project_id")
    parser.add_argument(
        "--model-key",
        default="veo_3_1_t2v_lite_portrait_ultra_relaxed",
        help="desired_model_key；缺省是 10s portrait lite",
    )
    parser.add_argument(
        "--aspect-ratio",
        default="VIDEO_ASPECT_RATIO_PORTRAIT",
        help="VIDEO_ASPECT_RATIO_PORTRAIT / LANDSCAPE",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=10,
        help="视频时长（秒），与 model_key 兼容即可",
    )
    parser.add_argument("--prompt", default="", help="视频 prompt；留空用内置最小 prompt")
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="只探活流程（不实际提交创建）",
    )
    parser.add_argument(
        "--no-references",
        action="store_true",
        help="不发 reference_assets，仅做纯文生视频（用于排除 mention 链路问题）",
    )
    parser.add_argument(
        "--retry",
        type=int,
        default=1,
        help="失败时连续重试次数（含本次）",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=240,
        help="单次 dispatch 的超时（秒）",
    )
    parser.add_argument("--admin-token", default="")
    parser.add_argument("--username", default="")
    parser.add_argument("--password", default="")
    parser.add_argument("--config", default="")
    return parser


def dispatch_once(
    *,
    base_url: str,
    admin_token: str,
    args: argparse.Namespace,
    project_id: str,
    reference_assets: List[Dict[str, Any]],
) -> Dict[str, Any]:
    prompt = args.prompt.strip() or build_default_prompt()
    ui_settings = derive_video_ui_settings(
        model_key=args.model_key,
        aspect_ratio=args.aspect_ratio,
        outputs_per_prompt=1,
    )
    # model_key 没带 _Ns 后缀时，duration/aspect_ratio_label 退回到 args 里的显式值
    desired_duration_seconds = ui_settings["desired_duration_seconds"] or str(int(args.duration))
    desired_aspect_ratio_label = ui_settings["desired_aspect_ratio_label"] or (
        "9:16" if "PORTRAIT" in args.aspect_ratio.upper() else "16:9"
    )
    if not ui_settings["desired_model_display_name"]:
        raise RuntimeError(
            f"无法从 model_key={args.model_key!r} 推导 desired_model_display_name；"
            "请检查 model_key 是否为 veo_3_1_* / omni-flash / abra_* 之一"
        )
    payload = {
        "workflow_mode": "text_to_video",
        "activate_tab": True,
        "prompt": prompt,
        "preferred_submode": "文本",
        "reference_assets": reference_assets,
        "desired_model_key": args.model_key,
        "desired_model_display_name": ui_settings["desired_model_display_name"],
        "desired_duration_seconds": desired_duration_seconds,
        "desired_aspect_ratio": args.aspect_ratio,
        "desired_aspect_ratio_label": desired_aspect_ratio_label,
        "desired_outputs_per_prompt": 1,
        "probe_only": bool(args.probe_only),
    }
    print(f"[*] 派生 UI 设置: model={ui_settings['desired_model_display_name']!r} "
          f"duration={desired_duration_seconds}s aspect={desired_aspect_ratio_label}")
    body = {
        "token_id": args.token_id,
        "job_type": "video_ui_workflow",
        "project_id": project_id,
        "payload": payload,
        "timeout": int(args.timeout),
    }
    print(f"\n>>> dispatching job_type=video_ui_workflow token_id={args.token_id} project_id={project_id}")
    print(f">>> reference_assets: {len(reference_assets)} item(s)")
    return http_json(
        "POST",
        f"{base_url}/api/browser-worker/jobs/dispatch",
        payload=body,
        headers={"Authorization": f"Bearer {admin_token}"},
        timeout=float(args.timeout) + 30.0,
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.base_url = str(args.base_url or "").rstrip("/")
    if not args.base_url:
        print("错误: base_url 不能为空", file=sys.stderr)
        return 2

    try:
        admin_token = resolve_admin_token(args)
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(f"[*] base_url = {args.base_url}")
    print(f"[*] token_id = {args.token_id}")

    # 解析 project
    project_id = args.project_id.strip()
    if not project_id:
        try:
            projects = fetch_token_projects(args.base_url, admin_token, args.token_id)
        except Exception as exc:
            print(f"错误: 拉取 token 项目失败: {exc}", file=sys.stderr)
            return 1
        current = [p for p in projects if p.get("is_current")]
        if current:
            project_id = str(current[0].get("project_id") or "").strip()
        elif projects:
            project_id = str(projects[0].get("project_id") or "").strip()
        if not project_id:
            print(f"错误: token {args.token_id} 没有任何 active project", file=sys.stderr)
            return 1
    print(f"[*] project_id = {project_id}")

    # 拉取项目媒体，挑一张 image 作为 mention 用的 reference_asset
    reference_assets: List[Dict[str, Any]] = []
    if not args.no_references:
        try:
            media = fetch_project_media(args.base_url, admin_token, args.token_id, project_id)
        except Exception as exc:
            print(f"警告: 拉取项目媒体失败，将以 no_references 模式运行: {exc}", file=sys.stderr)
            media = []
        for slot in ("image_1", "image_2", "image_3"):
            picked = select_image_media(media, slot)
            if picked:
                reference_assets.append(picked)
        if not reference_assets:
            print("警告: 项目里没有 image 媒体；自动降级为 no_references", file=sys.stderr)
        else:
            print(f"[*] reference_assets: {[ra['media_id'] for ra in reference_assets]}")

    overall_success = False
    for attempt in range(1, max(1, args.retry) + 1):
        run_id = f"{int(time.time())}-{uuid.uuid4().hex[:8]}-attempt{attempt}"
        log_path = DEBUG_DIR / f"{run_id}.json"
        print(f"\n=== attempt {attempt}/{args.retry} (run_id={run_id}) ===")
        try:
            result = dispatch_once(
                base_url=args.base_url,
                admin_token=admin_token,
                args=args,
                project_id=project_id,
                reference_assets=reference_assets,
            )
        except Exception as exc:
            print(f"错误: dispatch 抛异常: {exc}", file=sys.stderr)
            log_path.write_text(json.dumps({"attempt": attempt, "error": str(exc)}, ensure_ascii=False, indent=2))
            continue

        # 落盘完整 result
        log_path.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"[*] 完整 result 已写入 {log_path}")

        success = bool(result.get("success"))
        job = result.get("job") or {}
        status = str(job.get("status") or "").lower()
        error_msg = job.get("error") or job.get("message")
        ui_state = (job.get("result") or {}).get("ui_state") or {}
        wf_state = (ui_state.get("workflow_state") or {}) if isinstance(ui_state, dict) else {}
        wf_error = wf_state.get("error") or (job.get("result") or {}).get("error")
        prompt_state = wf_state.get("prompt_state") or {}
        submit_state = wf_state.get("submit_state") or {}
        steps = wf_state.get("steps") or []

        print(f"  http_success    = {success}")
        print(f"  job.status      = {status}")
        if error_msg:
            print(f"  job.error       = {error_msg}")
        if wf_error:
            print(f"  workflow.error  = {wf_error}")
        if prompt_state:
            print(f"  prompt_state    = {json.dumps(prompt_state, ensure_ascii=False)[:200]}")
        if submit_state:
            print(f"  submit_state    = {json.dumps(submit_state, ensure_ascii=False)[:200]}")
        print(f"  steps 数量      = {len(steps)}")
        for idx, step in enumerate(steps[:6]):
            label = step.get("label") or step.get("name") or f"step-{idx}"
            err = step.get("error") or step.get("ok")
            print(f"    step[{idx}] = {label} ok={err}")

        if success and status == "success" and not wf_error:
            overall_success = True
            break
        else:
            print(f"  -> attempt {attempt} 失败，{'继续重试' if attempt < args.retry else '放弃'}")

    return 0 if overall_success else 1


if __name__ == "__main__":
    raise SystemExit(main())
