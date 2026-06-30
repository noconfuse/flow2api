from __future__ import annotations

import hashlib
import json
import os
import signal
import shutil
import subprocess
import time
import tomllib
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..core.logger import debug_logger


REPO_ROOT = Path(__file__).resolve().parents[2]
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


@dataclass
class TokenBrowserProfileSpec:
    token_id: int
    email: str
    route_key: str
    client_label: str
    current_project_id: str

    @property
    def user_data_dir(self) -> Path:
        return USER_DATA_ROOT / f"token-{self.token_id}"

    @property
    def extension_dir(self) -> Path:
        return EXTENSION_BUILD_ROOT / f"token-{self.token_id}-v{current_extension_version_tag()}"

    @property
    def metadata_path(self) -> Path:
        return METADATA_ROOT / f"token-{self.token_id}.json"

    @property
    def startup_url(self) -> str:
        project_id = str(self.current_project_id or "").strip()
        if project_id:
            return f"https://labs.google/fx/zh/tools/flow/project/{project_id}"
        return DEFAULT_LABS_URL


def make_route_key(token_id: int, route_key: Optional[str] = None) -> str:
    normalized = str(route_key or "").strip()
    return normalized or f"token{int(token_id)}-worker"


def make_client_label(token_id: int) -> str:
    return f"chrome-token{int(token_id)}"


def make_spec(
    *,
    token_id: int,
    email: str,
    route_key: Optional[str] = None,
    current_project_id: Optional[str] = None,
) -> TokenBrowserProfileSpec:
    return TokenBrowserProfileSpec(
        token_id=int(token_id),
        email=str(email or "").strip(),
        route_key=make_route_key(token_id, route_key),
        client_label=make_client_label(token_id),
        current_project_id=str(current_project_id or "").strip(),
    )


def ensure_state_dirs() -> None:
    for path in (STATE_ROOT, EXTENSION_BUILD_ROOT, USER_DATA_ROOT, METADATA_ROOT):
        path.mkdir(parents=True, exist_ok=True)


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


def current_extension_version_tag() -> str:
    try:
        manifest = json.loads((EXTENSION_SRC_DIR / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        return "unknown"
    version = str(manifest.get("version") or "unknown").strip() or "unknown"
    fingerprint = current_extension_source_fingerprint()
    safe = "".join(ch if ch.isalnum() else "-" for ch in f"{version}-{fingerprint}")
    return safe.strip("-") or "unknown"


def host_bridge_launch_url_candidates() -> list[str]:
    explicit = str(os.environ.get("FLOW2API_BROWSER_LAUNCH_HOST_URL") or "").strip()
    if explicit:
        return [explicit]
    return [
        "http://127.0.0.1:8765/launch",
        "http://localhost:8765/launch",
        "http://host.docker.internal:8765/launch",
    ]


def health_url_from_launch_url(launch_url: str) -> str:
    return launch_url[:-7] + "/health" if launch_url.endswith("/launch") else launch_url.rstrip("/") + "/health"


def host_bridge_launch_url_if_available() -> Optional[str]:
    for launch_url in host_bridge_launch_url_candidates():
        try:
            request = urllib.request.Request(health_url_from_launch_url(launch_url), method="GET")
            with urllib.request.urlopen(request, timeout=2) as response:
                body = response.read().decode("utf-8", errors="replace")
            payload = json.loads(body or "{}")
            if isinstance(payload, dict) and payload.get("success"):
                return launch_url
        except Exception:
            continue
    return None


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
        "ws_url": f"ws://{host}:{port}/captcha_ws",
    }


def build_extension_bundle(spec: TokenBrowserProfileSpec, runtime_settings: Optional[dict] = None) -> dict:
    ensure_state_dirs()
    runtime = runtime_settings or load_runtime_settings()
    version_tag = current_extension_version_tag()
    source_fingerprint = current_extension_source_fingerprint()
    for stale_dir in EXTENSION_BUILD_ROOT.glob(f"token-{spec.token_id}-v*"):
        if stale_dir == spec.extension_dir:
            continue
        shutil.rmtree(stale_dir, ignore_errors=True)
    if spec.extension_dir.exists():
        shutil.rmtree(spec.extension_dir)
    shutil.copytree(EXTENSION_SRC_DIR, spec.extension_dir)

    bootstrap = {
        "serverUrl": runtime["ws_url"],
        "apiKey": runtime["api_key"],
        "routeKey": spec.route_key,
        "clientLabel": spec.client_label,
        "extensionVersionTag": version_tag,
        "extensionSourceFingerprint": source_fingerprint,
    }
    (spec.extension_dir / "bootstrap-settings.json").write_text(
        json.dumps(bootstrap, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    metadata = {
        "token_id": spec.token_id,
        "email": spec.email,
        "route_key": spec.route_key,
        "client_label": spec.client_label,
        "user_data_dir": str(spec.user_data_dir),
        "extension_dir": str(spec.extension_dir),
        "startup_url": spec.startup_url,
        "extension_version_tag": version_tag,
        "extension_source_fingerprint": source_fingerprint,
    }
    spec.metadata_path.write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    spec.user_data_dir.mkdir(parents=True, exist_ok=True)
    return metadata


def find_profile_process_pids(user_data_dir: Path) -> list[int]:
    target = str(user_data_dir)
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


def stop_profile_processes(spec: TokenBrowserProfileSpec) -> list[int]:
    pids = find_profile_process_pids(spec.user_data_dir)
    if not pids:
        return []

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


def detect_chrome_path(explicit_path: Optional[str] = None) -> str:
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


def launch_profile(
    spec: TokenBrowserProfileSpec,
    *,
    chrome_path: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> dict:
    ensure_state_dirs()
    spec.user_data_dir.mkdir(parents=True, exist_ok=True)
    stopped_pids = stop_profile_processes(spec)
    cleared_session_files = clear_profile_session_restore_files(spec.user_data_dir)
    cleared_extension_state = clear_profile_extension_state(spec.user_data_dir)
    resolved_chrome_path = detect_chrome_path(chrome_path)
    command = [
        resolved_chrome_path,
        f"--user-data-dir={spec.user_data_dir}",
        "--profile-directory=Default",
        f"--disable-extensions-except={spec.extension_dir}",
        f"--load-extension={spec.extension_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        spec.startup_url,
    ]
    if extra_args:
        command[1:1] = list(extra_args)
    process = subprocess.Popen(
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {
        "pid": process.pid,
        "chrome_path": resolved_chrome_path,
        "command": command,
        "stopped_pids": stopped_pids,
        "cleared_session_files": cleared_session_files,
        "cleared_extension_state": cleared_extension_state,
    }


def launch_profile_via_host_bridge(
    spec: TokenBrowserProfileSpec,
    *,
    chrome_path: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> dict:
    bridge_url = host_bridge_launch_url_if_available()
    if not bridge_url:
        raise RuntimeError("宿主机浏览器启动桥不可用: 未发现可用的 host bridge 服务")
    auth_source = "none"
    auth_token = str(os.environ.get("FLOW2API_BROWSER_LAUNCH_HOST_TOKEN") or "").strip()
    if auth_token:
        auth_source = "env:FLOW2API_BROWSER_LAUNCH_HOST_TOKEN"
    elif SETTING_TOML.exists():
        try:
            with SETTING_TOML.open("rb") as f:
                settings = tomllib.load(f)
            global_settings = settings.get("global") or {}
            auth_token = str(global_settings.get("api_key") or settings.get("api_key") or "").strip()
            if auth_token:
                auth_source = "setting.toml:api_key"
        except Exception:
            auth_token = ""
    if not auth_token and str(os.environ.get("FLOW2API_API_KEY") or "").strip():
        auth_token = str(os.environ.get("FLOW2API_API_KEY") or "").strip()
        auth_source = "env:FLOW2API_API_KEY"
    try:
        user_data_dir_rel = spec.user_data_dir.relative_to(REPO_ROOT).as_posix()
        extension_dir_rel = spec.extension_dir.relative_to(REPO_ROOT).as_posix()
    except Exception as exc:
        raise RuntimeError(f"无法为 host bridge 生成 repo 内相对路径: {exc}") from exc
    payload = {
        "token_id": int(spec.token_id),
        "user_data_dir_rel": user_data_dir_rel,
        "extension_dir_rel": extension_dir_rel,
        "startup_url": spec.startup_url,
        "chrome_path": str(chrome_path or "").strip() or None,
        "chrome_args": list(extra_args or []),
    }
    headers = {"Content-Type": "application/json"}
    if auth_token:
        headers["X-Flow2API-Key"] = auth_token
    #region debug-point browser-autolaunch-host-bridge-request
    debug_logger.log_info(
        "[BROWSER_PROFILE_BOOTSTRAP] host bridge launch request "
        f"token_id={spec.token_id} bridge_url={bridge_url} auth_source={auth_source} "
        f"has_auth={'yes' if bool(auth_token) else 'no'}"
    )
    #endregion
    request = urllib.request.Request(
        bridge_url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        #region debug-point browser-autolaunch-host-bridge-response
        debug_logger.log_warning(
            "[BROWSER_PROFILE_BOOTSTRAP] host bridge launch failed "
            f"token_id={spec.token_id} status={exc.code} auth_source={auth_source} "
            f"has_auth={'yes' if bool(auth_token) else 'no'} detail={detail[:300]}"
        )
        #endregion
        raise RuntimeError(f"宿主机浏览器启动桥请求失败: HTTP {exc.code} {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"宿主机浏览器启动桥不可用: {exc}") from exc

    try:
        data = json.loads(body or "{}")
    except Exception as exc:
        raise RuntimeError(f"宿主机浏览器启动桥返回了无效响应: {body[:300]}") from exc

    if not isinstance(data, dict) or not data.get("success"):
        raise RuntimeError(str((data or {}).get("error") or "宿主机浏览器启动桥执行失败"))

    return {
        "pid": data.get("pid"),
        "chrome_path": data.get("chrome_path"),
        "command": data.get("command"),
        "launch_mode": "host_bridge",
        "bridge_url": bridge_url,
    }


def prepare_and_optionally_launch(
    spec: TokenBrowserProfileSpec,
    *,
    runtime_settings: Optional[dict] = None,
    launch: bool = False,
    chrome_path: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
) -> dict:
    metadata = build_extension_bundle(spec, runtime_settings=runtime_settings)
    result = {
        "prepared": True,
        "launched": False,
        "token_id": spec.token_id,
        "route_key": spec.route_key,
        "client_label": spec.client_label,
        "email": spec.email,
        "user_data_dir": metadata["user_data_dir"],
        "extension_dir": metadata["extension_dir"],
        "startup_url": metadata["startup_url"],
        "metadata_path": str(spec.metadata_path),
    }
    if launch:
        launch_mode = str(os.environ.get("FLOW2API_BROWSER_LAUNCH_MODE") or "").strip().lower()
        if not launch_mode:
            launch_mode = "host_bridge" if host_bridge_launch_url_if_available() else "local"
        if launch_mode == "host_bridge":
            launch_result = launch_profile_via_host_bridge(
                spec,
                chrome_path=chrome_path,
                extra_args=extra_args,
            )
        else:
            launch_result = launch_profile(
                spec,
                chrome_path=chrome_path,
                extra_args=extra_args,
            )
        result.update({
            "launched": True,
            "pid": launch_result["pid"],
            "chrome_path": launch_result["chrome_path"],
            "command": launch_result["command"],
            "stopped_pids": launch_result.get("stopped_pids") or [],
            "cleared_session_files": launch_result.get("cleared_session_files") or [],
            "cleared_extension_state": launch_result.get("cleared_extension_state") or [],
            "launch_mode": launch_result.get("launch_mode") or launch_mode or "local",
            "bridge_url": launch_result.get("bridge_url"),
        })
    return result
