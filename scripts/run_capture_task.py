#!/usr/bin/env python3
import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional
from urllib import error, request

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    tomllib = None


TASK_TYPES = (
    "text_to_video",
    "image_to_video",
    "edit_existing_video",
    "edit_uploaded_video",
)


def load_config_credentials(config_path: str) -> Dict[str, str]:
    if tomllib is None:
        raise RuntimeError("当前 Python 不支持 tomllib，无法读取 TOML 配置")
    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    username = str(data.get("admin_username") or "").strip()
    password = str(data.get("admin_password") or "").strip()
    if not username or not password:
        raise RuntimeError(f"配置文件缺少 admin_username/admin_password: {path}")
    return {"username": username, "password": password}


def http_json(
    method: str,
    url: str,
    payload: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json"}
    if headers:
        request_headers.update(headers)
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = request.Request(url=url, method=method.upper(), data=body, headers=request_headers)
    try:
        with request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        detail = raw
        try:
            detail_json = json.loads(raw) if raw else {}
            if isinstance(detail_json, dict):
                detail = detail_json.get("detail") or detail_json.get("message") or raw
        except json.JSONDecodeError:
            pass
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="通过 admin API 启动、查看或停止浏览器自动化采集任务"
    )
    parser.add_argument("action", choices=("start", "status", "stop"), help="采集动作")
    parser.add_argument(
        "--task-type",
        required=True,
        choices=TASK_TYPES,
        help="采集任务类型",
    )
    parser.add_argument("--token-id", required=True, type=int, help="目标账号 ID")
    parser.add_argument(
        "--base-url",
        default=os.getenv("GFLOW_BASE_URL", "http://127.0.0.1:8000"),
        help="服务地址，默认读取 GFLOW_BASE_URL 或 http://127.0.0.1:8000",
    )
    parser.add_argument("--project-id", default="", help="可选，覆盖采集使用的项目 ID")
    parser.add_argument(
        "--activate-tab",
        dest="activate_tab",
        action="store_true",
        default=True,
        help="启动采集时激活目标浏览器标签页，默认开启",
    )
    parser.add_argument(
        "--no-activate-tab",
        dest="activate_tab",
        action="store_false",
        help="启动采集时不激活目标浏览器标签页",
    )
    parser.add_argument("--admin-token", default="", help="已有管理员会话 token")
    parser.add_argument("--username", default="", help="管理员用户名")
    parser.add_argument("--password", default="", help="管理员密码")
    parser.add_argument(
        "--config",
        default="",
        help="可选，读取 TOML 配置中的 admin_username/admin_password",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.base_url = str(args.base_url or "").rstrip("/")
    if not args.base_url:
        print("错误: base_url 不能为空", file=sys.stderr)
        return 2

    try:
        admin_token = resolve_admin_token(args)
        payload: Dict[str, Any] = {
            "task_type": args.task_type,
            "activate_tab": bool(args.activate_tab),
        }
        if args.project_id:
            payload["project_id"] = args.project_id
        result = http_json(
            "POST",
            f"{args.base_url}/api/tokens/{args.token_id}/capture/{args.action}",
            payload=payload,
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    except Exception as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
