#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SETTING_TOML = REPO_ROOT / "config" / "setting.toml"
EXTENSION_SRC_DIR = REPO_ROOT / "extension"
STATE_ROOT = REPO_ROOT / "browser_data" / "profile_launcher"
EXTENSION_BUILD_ROOT = STATE_ROOT / "extensions"
USER_DATA_ROOT = STATE_ROOT / "user_data"
METADATA_ROOT = STATE_ROOT / "metadata"
DEFAULT_LABS_URL = "https://labs.google/fx/zh/tools/flow"

def chrome_candidates() -> list[str]:
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        program_files = os.environ.get("PROGRAMFILES", "")
        program_files_x86 = os.environ.get("PROGRAMFILES(X86)", "")
        return [
            str(REPO_ROOT / "browser_data" / "host_browsers" / "chrome-win64" / "chrome-win64" / "chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome for Testing", "Application", "chrome.exe"),
            os.path.join(program_files, "Google", "Chrome for Testing", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome for Testing", "Application", "chrome.exe"),
            os.path.join(program_files, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(local_app_data, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(program_files, "Chromium", "Application", "chrome.exe"),
            os.path.join(program_files_x86, "Chromium", "Application", "chrome.exe"),
        ]

    return [
        str(
            REPO_ROOT
            / "browser_data"
            / "host_browsers"
            / "chrome-mac-arm64"
            / "chrome-mac-arm64"
            / "Google Chrome for Testing.app"
            / "Contents"
            / "MacOS"
            / "Google Chrome for Testing"
        ),
        "/Applications/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]


def is_chrome_for_testing_path(path: Path) -> bool:
    resolved = path.expanduser().resolve(strict=False)
    resolved_text = str(resolved).lower()
    if "chrome for testing" in resolved_text:
        return True
    try:
        resolved.relative_to(REPO_ROOT / "browser_data" / "host_browsers")
        return True
    except ValueError:
        return False


def current_extension_version_tag() -> str:
    try:
        manifest = json.loads((EXTENSION_SRC_DIR / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    version = str(manifest.get("version") or "unknown").strip() or "unknown"
    fingerprint = current_extension_source_fingerprint()
    safe = "".join(ch if ch.isalnum() else "-" for ch in f"{version}-{fingerprint}")
    return safe.strip("-") or "unknown"


def current_extension_source_fingerprint() -> str:
    digest = hashlib.sha256()
    try:
        for path in sorted(EXTENSION_SRC_DIR.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(EXTENSION_SRC_DIR).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    except Exception:
        return "unknown"
    return digest.hexdigest()[:10]


def default_launch_args() -> list[str]:
    args: list[str] = ["--hide-crash-restore-bubble"]
    if sys.platform == "darwin":
        # Chrome 149 on this macOS host crashes during sandbox/GPU init unless sandbox is disabled.
        args.append("--no-sandbox")
    return args


@dataclass
class TokenRecord:
    id: int
    email: str
    is_active: bool
    extension_route_key: str
    current_project_id: str
    current_project_name: str

    @property
    def effective_route_key(self) -> str:
        return self.extension_route_key.strip() or f"token{self.id}-worker"

    @property
    def client_label(self) -> str:
        return f"chrome-token{self.id}"

    @property
    def user_data_dir(self) -> Path:
        return USER_DATA_ROOT / f"token-{self.id}"

    @property
    def extension_dir(self) -> Path:
        return EXTENSION_BUILD_ROOT / f"token-{self.id}-v{current_extension_version_tag()}"

    @property
    def metadata_path(self) -> Path:
        return METADATA_ROOT / f"token-{self.id}.json"

    @property
    def startup_url(self) -> str:
        if self.current_project_id.strip():
            return f"https://labs.google/fx/zh/tools/flow/project/{self.current_project_id.strip()}"
        return DEFAULT_LABS_URL


def load_runtime_settings() -> dict:
    with SETTING_TOML.open("rb") as f:
        settings = tomllib.load(f)

    global_settings = settings.get("global") or {}
    api_key = str(global_settings.get("api_key") or settings.get("api_key") or "").strip()
    if not api_key:
        raise RuntimeError(f"`{SETTING_TOML}` 缺少 `api_key`，无法生成扩展 bootstrap 配置")

    server = settings.get("server") or {}
    host = str(server.get("host") or "127.0.0.1").strip()
    port = int(server.get("port") or 8000)
    if host in {"0.0.0.0", "::", ""}:
        host = "127.0.0.1"

    return {
        "api_key": api_key,
        "api_base_url": f"http://{host}:{port}",
        "ws_url": f"ws://{host}:{port}/captcha_ws",
    }


def _request_json(runtime_settings: dict, method: str, path: str, payload: dict | None = None) -> dict:
    body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{runtime_settings['api_base_url']}{path}",
        data=body,
        method=method,
        headers={
            "Authorization": f"Bearer {runtime_settings['api_key']}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"请求 {path} 失败: HTTP {exc.code} {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"请求 {path} 失败: {exc}") from exc


def fetch_tokens_from_api(
    runtime_settings: dict,
    token_ids: list[int] | None = None,
    *,
    active_only: bool = False,
    sync_route_keys: bool = False,
) -> list[TokenRecord]:
    rows = _request_json(
        runtime_settings,
        "POST",
        "/api/internal/browser-launch/tokens",
        {
            "token_ids": token_ids or None,
            "active_only": active_only,
            "sync_route_keys": sync_route_keys,
        },
    ).get("tokens") or []
    return [
        TokenRecord(
            id=int(row.get("id")),
            email=str(row.get("email") or "").strip(),
            is_active=bool(row.get("is_active")),
            extension_route_key=str(row.get("extension_route_key") or "").strip(),
            current_project_id=str(row.get("current_project_id") or "").strip(),
            current_project_name=str(row.get("current_project_name") or "").strip(),
        )
        for row in rows
    ]


def ensure_state_dirs() -> None:
    for path in (STATE_ROOT, EXTENSION_BUILD_ROOT, USER_DATA_ROOT, METADATA_ROOT):
        path.mkdir(parents=True, exist_ok=True)


def clear_profile_session_restore_files(user_data_dir: Path) -> list[str]:
    removed: list[str] = []
    default_dir = user_data_dir / "Default"
    if not default_dir.exists():
        return removed

    session_paths = [
        default_dir / "Current Session",
        default_dir / "Current Tabs",
        default_dir / "Last Session",
        default_dir / "Last Tabs",
    ]
    sessions_dir = default_dir / "Sessions"
    if sessions_dir.exists():
        session_paths.extend(sorted(sessions_dir.glob("Session_*")))
        session_paths.extend(sorted(sessions_dir.glob("Tabs_*")))

    for path in session_paths:
        if not path.exists():
            continue
        try:
            path.unlink()
            removed.append(str(path))
        except IsADirectoryError:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        except Exception:
            continue
    return removed


def mark_profile_clean_exit(user_data_dir: Path) -> bool:
    preferences_path = user_data_dir / "Default" / "Preferences"
    if not preferences_path.exists():
        return False
    try:
        data = json.loads(preferences_path.read_text(encoding="utf-8"))
        profile = data.get("profile")
        if not isinstance(profile, dict):
            profile = {}
            data["profile"] = profile
        profile["exit_type"] = "Normal"
        profile["exited_cleanly"] = True
        preferences_path.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        return True
    except Exception:
        return False


# #region debug-point B:debug-report-helper
def report_token5_video_media_debug(hypothesis_id: str, location: str, msg: str, data: dict) -> None:
    import urllib.request

    payload = {
        "sessionId": "token5-video-media",
        "runId": "pre-fix",
        "hypothesisId": str(hypothesis_id),
        "location": location,
        "msg": msg,
        "data": data,
    }
    try:
        urllib.request.urlopen(
            urllib.request.Request(
                "http://127.0.0.1:7777/event",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            ),
            timeout=2,
        ).read()
    except Exception:
        pass


# #endregion
def build_extension_bundle(token: TokenRecord, runtime_settings: dict) -> None:
    ensure_state_dirs()
    version_tag = current_extension_version_tag()
    source_fingerprint = current_extension_source_fingerprint()
    for stale_dir in EXTENSION_BUILD_ROOT.glob(f"token-{token.id}-v*"):
        if stale_dir == token.extension_dir:
            continue
        shutil.rmtree(stale_dir, ignore_errors=True)
    if token.extension_dir.exists():
        shutil.rmtree(token.extension_dir)
    shutil.copytree(EXTENSION_SRC_DIR, token.extension_dir)
    # #region debug-point B:bundle-built
    report_token5_video_media_debug(
        "B",
        "scripts/browser_profile_launcher.py:build_extension_bundle",
        "[DEBUG] extension bundle built",
        {
            "token_id": token.id,
            "extension_dir": str(token.extension_dir),
            "manifest_version": current_extension_version_tag(),
            "route_key": token.effective_route_key,
        },
    )
    # #endregion

    bootstrap = {
        "serverUrl": runtime_settings["ws_url"],
        "apiKey": runtime_settings["api_key"],
        "routeKey": token.effective_route_key,
        "clientLabel": token.client_label,
        "extensionVersionTag": version_tag,
        "extensionSourceFingerprint": source_fingerprint,
    }
    (token.extension_dir / "bootstrap-settings.json").write_text(
        json.dumps(bootstrap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    token.metadata_path.write_text(
        json.dumps(
            {
                "token_id": token.id,
                "email": token.email,
                "route_key": token.effective_route_key,
                "client_label": token.client_label,
                "user_data_dir": str(token.user_data_dir),
                "extension_dir": str(token.extension_dir),
                "startup_url": token.startup_url,
                "extension_version_tag": version_tag,
                "extension_source_fingerprint": source_fingerprint,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def clear_profile_extension_state(user_data_dir: Path) -> list[str]:
    removed: list[str] = []
    default_dir = user_data_dir / "Default"
    if not default_dir.exists():
        return removed

    for path in (
        default_dir / "Extension Rules",
        default_dir / "Extension Scripts",
        default_dir / "Extension State",
        default_dir / "Local Extension Settings",
    ):
        if not path.exists():
            continue
        try:
            shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        except Exception:
            continue
    return removed


def detect_chrome_path(explicit_path: str | None) -> str:
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.exists() and is_chrome_for_testing_path(path):
            return str(path)
        if path.exists():
            raise RuntimeError(f"指定的浏览器不是 Chrome for Testing: {path}")
        raise RuntimeError(f"指定的浏览器路径不存在: {path}")

    env_path = os.environ.get("FLOW2API_CHROME_PATH", "").strip()
    if env_path:
        path = Path(env_path).expanduser()
        if path.exists() and is_chrome_for_testing_path(path):
            return str(path)
        if path.exists():
            raise RuntimeError(f"`FLOW2API_CHROME_PATH` 必须指向 Chrome for Testing: {path}")

    runtime_browser_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
    if runtime_browser_path:
        path = Path(runtime_browser_path).expanduser()
        if path.exists() and is_chrome_for_testing_path(path):
            return str(path)
        if path.exists():
            raise RuntimeError(f"`BROWSER_EXECUTABLE_PATH` 必须指向 Chrome for Testing: {path}")

    for candidate in chrome_candidates():
        if Path(candidate).exists():
            return candidate

    raise RuntimeError(
        "未找到可用的 Chrome for Testing。请安装 Chrome for Testing，或通过 `--chrome-path` / `FLOW2API_CHROME_PATH` 指向 Chrome for Testing。"
    )


def find_profile_process_pids(user_data_dir: Path) -> list[int]:
    target = str(user_data_dir)
    target_variants = {
        target,
        target.replace("\\", "/"),
        target.replace("/", "\\"),
    }
    if os.name == "nt":
        try:
            output = subprocess.check_output(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress",
                ],
                text=True,
            )
            rows = json.loads(output or "[]")
        except Exception:
            return []
        if isinstance(rows, dict):
            rows = [rows]
        pids: list[int] = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            command = str(row.get("CommandLine") or "")
            lowered = command.lower()
            if ".exe" not in lowered or ("chrome" not in lowered and "chromium" not in lowered):
                continue
            normalized_command = command.replace("\\", "/").lower()
            if not any(str(item).replace("\\", "/").lower() in normalized_command for item in target_variants):
                continue
            try:
                pid = int(row.get("ProcessId"))
            except Exception:
                continue
            if pid == os.getpid():
                continue
            pids.append(pid)
        return sorted(set(pids))
    try:
        output = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    except Exception:
        return []

    pids: list[int] = []
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line or target not in line:
            continue
        parts = line.split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        command = parts[1] if len(parts) > 1 else ""
        lowered = command.lower()
        if "chrome" not in lowered and "chromium" not in lowered:
            continue
        if pid == os.getpid():
            continue
        pids.append(pid)
    return sorted(set(pids))


def stop_profile_processes_for_user_data_dir(user_data_dir: Path) -> list[int]:
    pids = find_profile_process_pids(user_data_dir)
    if not pids:
        return []

    if os.name == "nt":
        alive = set(pids)
        for pid in pids:
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                continue

        deadline = time.time() + 5
        while alive and time.time() < deadline:
            remaining: set[int] = set()
            for pid in alive:
                try:
                    os.kill(pid, 0)
                    remaining.add(pid)
                except ProcessLookupError:
                    continue
                except Exception:
                    remaining.add(pid)
            alive = remaining
            if alive:
                time.sleep(0.2)

        for pid in sorted(alive):
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                continue
        return pids

    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except Exception:
            continue

    deadline = time.time() + 5
    alive = set(pids)
    while alive and time.time() < deadline:
        remaining: set[int] = set()
        for pid in alive:
            try:
                os.kill(pid, 0)
                remaining.add(pid)
            except ProcessLookupError:
                continue
            except Exception:
                remaining.add(pid)
        alive = remaining
        if alive:
            time.sleep(0.2)

    for pid in sorted(alive):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except Exception:
            continue

    return pids


def stop_profile_processes(token: TokenRecord) -> list[int]:
    return stop_profile_processes_for_user_data_dir(token.user_data_dir)


def launch_profile_with_paths(
    *,
    token_id: int | None,
    user_data_dir: Path,
    extension_dir: Path,
    startup_url: str,
    chrome_path: str,
    extra_args: list[str] | None = None,
) -> dict[str, object]:
    ensure_state_dirs()
    user_data_dir.mkdir(parents=True, exist_ok=True)
    if not extension_dir.exists():
        raise RuntimeError(f"扩展目录不存在: {extension_dir}")
    stopped_pids = stop_profile_processes_for_user_data_dir(user_data_dir)
    cleared_session_files = clear_profile_session_restore_files(user_data_dir)
    marked_clean_exit = mark_profile_clean_exit(user_data_dir)
    cleared_extension_state = clear_profile_extension_state(user_data_dir)
    # #region debug-point B:profile-stop-existing
    report_token5_video_media_debug(
        "B",
        "scripts/browser_profile_launcher.py:launch_profile",
        "[DEBUG] stopped existing profile processes",
        {
            "token_id": token_id,
            "user_data_dir": str(user_data_dir),
            "stopped_pids": stopped_pids,
            "cleared_session_files": cleared_session_files,
            "marked_clean_exit": marked_clean_exit,
            "cleared_extension_state": cleared_extension_state,
        },
    )
    # #endregion
    command = [
        chrome_path,
        f"--user-data-dir={user_data_dir}",
        "--profile-directory=Default",
        f"--disable-extensions-except={extension_dir}",
        f"--load-extension={extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        startup_url,
    ]
    launch_args = [*default_launch_args(), *(extra_args or [])]
    if launch_args:
        command[1:1] = launch_args
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    # #region debug-point B:profile-launched
    report_token5_video_media_debug(
        "B",
        "scripts/browser_profile_launcher.py:launch_profile",
        "[DEBUG] browser profile launched",
        {
            "token_id": token_id,
            "extension_dir": str(extension_dir),
            "user_data_dir": str(user_data_dir),
            "chrome_path": chrome_path,
            "startup_url": startup_url,
            "command": command,
        },
    )
    # #endregion
    return {
        "pid": process.pid,
        "token_id": token_id,
        "user_data_dir": str(user_data_dir),
        "extension_dir": str(extension_dir),
        "startup_url": startup_url,
        "chrome_path": chrome_path,
        "command": command,
        "stopped_pids": stopped_pids,
        "cleared_session_files": cleared_session_files,
        "marked_clean_exit": marked_clean_exit,
        "cleared_extension_state": cleared_extension_state,
    }


def launch_profile(token: TokenRecord, chrome_path: str, extra_args: list[str] | None = None) -> dict[str, object]:
    return launch_profile_with_paths(
        token_id=token.id,
        user_data_dir=token.user_data_dir,
        extension_dir=token.extension_dir,
        startup_url=token.startup_url,
        chrome_path=chrome_path,
        extra_args=extra_args,
    )


def parse_token_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(part.strip()) for part in raw.split(",") if part.strip()]


def print_token_table(tokens: list[TokenRecord]) -> None:
    if not tokens:
        print("没有找到 token。")
        return
    print("token_id | active | email | route_key | startup_url")
    print("-" * 120)
    for token in tokens:
        print(
            f"{token.id:<8} | "
            f"{'yes' if token.is_active else 'no ':<6} | "
            f"{token.email or '-':<28} | "
            f"{token.effective_route_key:<20} | "
            f"{token.startup_url}"
        )


def command_list(args: argparse.Namespace) -> int:
    runtime_settings = load_runtime_settings()
    tokens = fetch_tokens_from_api(runtime_settings, parse_token_ids(args.token_ids))
    print_token_table(tokens)
    return 0


def resolve_tokens_for_launch(args: argparse.Namespace) -> tuple[list[TokenRecord], dict]:
    runtime_settings = load_runtime_settings()
    requested_ids = parse_token_ids(args.token_ids)
    tokens = fetch_tokens_from_api(
        runtime_settings,
        requested_ids or None,
        active_only=bool(args.active_only),
        sync_route_keys=True,
    )
    if not tokens:
        raise RuntimeError("没有找到可启动的 token。")

    return tokens, runtime_settings


def command_prepare(args: argparse.Namespace) -> int:
    tokens, runtime_settings = resolve_tokens_for_launch(args)
    for token in tokens:
        build_extension_bundle(token, runtime_settings)
        print(
            f"[prepared] token={token.id} email={token.email or '-'} "
            f"route_key={token.effective_route_key} "
            f"user_data_dir={token.user_data_dir}"
        )
    return 0


def command_launch(args: argparse.Namespace) -> int:
    tokens, runtime_settings = resolve_tokens_for_launch(args)
    chrome_path = detect_chrome_path(args.chrome_path)
    extra_args = args.chrome_arg or []
    for token in tokens:
        build_extension_bundle(token, runtime_settings)
        launch_result = launch_profile(token, chrome_path, extra_args=extra_args)
        if args.json:
            print(json.dumps(launch_result, ensure_ascii=False))
            continue
        print(
            f"[launched] token={token.id} email={token.email or '-'} "
            f"route_key={token.effective_route_key}"
        )
        print("  chrome_path:", launch_result["chrome_path"])
        print("  command:", " ".join(launch_result["command"]))
        print("  note: 首次启动这个 profile 时，只需要在打开的 Chrome 窗口里登录一次对应 Google 账号。")
    return 0


def command_launch_explicit(args: argparse.Namespace) -> int:
    chrome_path = detect_chrome_path(args.chrome_path)
    extra_args = args.chrome_arg or []
    user_data_dir = Path(str(args.user_data_dir)).expanduser()
    extension_dir = Path(str(args.extension_dir)).expanduser()
    startup_url = str(args.startup_url or "").strip()
    if not startup_url:
        raise RuntimeError("startup_url 不能为空")
    launch_result = launch_profile_with_paths(
        token_id=int(args.token_id) if args.token_id is not None else None,
        user_data_dir=user_data_dir,
        extension_dir=extension_dir,
        startup_url=startup_url,
        chrome_path=chrome_path,
        extra_args=extra_args,
    )
    if args.json:
        print(json.dumps(launch_result, ensure_ascii=False))
        return 0
    print(
        f"[launched-explicit] token={args.token_id if args.token_id is not None else '-'} "
        f"user_data_dir={user_data_dir}"
    )
    print("  chrome_path:", launch_result["chrome_path"])
    print("  command:", " ".join(launch_result["command"]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="为 Flow2API token 生成独立浏览器 profile，并自动加载带 route_key 的扩展。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="列出 token 与 route_key/profile 启动信息")
    list_parser.add_argument("--token-ids", help="逗号分隔的 token id 列表，例如 5,6,7")
    list_parser.set_defaults(func=command_list)

    prepare_parser = subparsers.add_parser("prepare", help="为 token 准备独立扩展副本与 profile 目录")
    prepare_parser.add_argument("--token-ids", help="逗号分隔的 token id 列表；为空时处理全部 token")
    prepare_parser.add_argument("--active-only", action="store_true", help="仅处理已启用的 token")
    prepare_parser.set_defaults(func=command_prepare)

    launch_parser = subparsers.add_parser("launch", help="启动一个或多个 token 对应的常驻浏览器 profile")
    launch_parser.add_argument("--token-ids", help="逗号分隔的 token id 列表；为空时处理全部 token")
    launch_parser.add_argument("--active-only", action="store_true", help="仅启动已启用的 token")
    launch_parser.add_argument("--chrome-path", help="显式指定 Chrome for Testing 可执行文件路径")
    launch_parser.add_argument(
        "--chrome-arg",
        action="append",
        default=[],
        help="额外传给 Chrome 的参数，可重复传入多次",
    )
    launch_parser.add_argument("--json", action="store_true", help="以 JSON 输出实际启动结果")
    launch_parser.set_defaults(func=command_launch)

    explicit_launch_parser = subparsers.add_parser(
        "launch-explicit",
        help="按显式传入的 profile/extension/startup 参数启动浏览器，不访问数据库",
    )
    explicit_launch_parser.add_argument("--token-id", type=int, help="可选，仅用于日志")
    explicit_launch_parser.add_argument("--user-data-dir", required=True, help="浏览器 user data 目录绝对路径")
    explicit_launch_parser.add_argument("--extension-dir", required=True, help="扩展目录绝对路径")
    explicit_launch_parser.add_argument("--startup-url", required=True, help="浏览器启动 URL")
    explicit_launch_parser.add_argument("--chrome-path", help="显式指定 Chrome for Testing 可执行文件路径")
    explicit_launch_parser.add_argument(
        "--chrome-arg",
        action="append",
        default=[],
        help="额外传给 Chrome 的参数，可重复传入多次",
    )
    explicit_launch_parser.add_argument("--json", action="store_true", help="以 JSON 输出实际启动结果")
    explicit_launch_parser.set_defaults(func=command_launch_explicit)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已取消。", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - CLI entry
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
