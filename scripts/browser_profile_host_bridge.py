#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
SETTING_TOML = REPO_ROOT / "config" / "setting.toml"
LAUNCHER_SCRIPT = REPO_ROOT / "scripts" / "browser_profile_launcher.py"


def load_api_key() -> str:
    if SETTING_TOML.exists():
        try:
            with SETTING_TOML.open("rb") as f:
                settings = tomllib.load(f)
            global_settings = settings.get("global") or {}
            api_key = str(global_settings.get("api_key") or settings.get("api_key") or "").strip()
            if api_key:
                return api_key
        except Exception:
            pass
    return str(os.environ.get("FLOW2API_BROWSER_LAUNCH_HOST_TOKEN") or "").strip()


API_KEY = load_api_key()
HOST = str(os.environ.get("FLOW2API_BROWSER_LAUNCH_HOST_BIND") or "0.0.0.0").strip()
PORT = int(os.environ.get("FLOW2API_BROWSER_LAUNCH_HOST_PORT") or 8765)


def resolve_repo_relative_path(raw_value: object, *, field_name: str) -> Path:
    value = str(raw_value or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required")
    path = Path(value)
    resolved = (REPO_ROOT / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        resolved.relative_to(REPO_ROOT)
    except ValueError as exc:
        raise ValueError(f"{field_name} must stay within repo root") from exc
    return resolved


class HostBridgeHandler(BaseHTTPRequestHandler):
    server_version = "Flow2APIHostBridge/1.0"

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if self.path != "/launch":
            self._send_json(404, {"success": False, "error": "not_found"})
            return

        expected_key = API_KEY
        if expected_key:
            provided_key = str(self.headers.get("X-Flow2API-Key") or "").strip()
            if provided_key != expected_key:
                self._send_json(403, {"success": False, "error": "forbidden"})
                return

        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except Exception:
            content_length = 0
        raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"

        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except Exception:
            self._send_json(400, {"success": False, "error": "invalid_json"})
            return

        try:
            token_id = int(payload.get("token_id"))
        except Exception:
            self._send_json(400, {"success": False, "error": "token_id is required"})
            return

        try:
            user_data_dir = resolve_repo_relative_path(payload.get("user_data_dir_rel"), field_name="user_data_dir_rel")
            extension_dir = resolve_repo_relative_path(payload.get("extension_dir_rel"), field_name="extension_dir_rel")
        except ValueError as exc:
            self._send_json(400, {"success": False, "error": str(exc)})
            return

        startup_url = str(payload.get("startup_url") or "").strip()
        if not startup_url:
            self._send_json(400, {"success": False, "error": "startup_url is required"})
            return

        chrome_path = str(payload.get("chrome_path") or "").strip()
        chrome_args = payload.get("chrome_args") if isinstance(payload.get("chrome_args"), list) else []

        command = [
            sys.executable,
            str(LAUNCHER_SCRIPT),
            "launch-explicit",
            "--json",
            "--token-id",
            str(token_id),
            "--user-data-dir",
            str(user_data_dir),
            "--extension-dir",
            str(extension_dir),
            "--startup-url",
            startup_url,
        ]
        if chrome_path:
            command.extend(["--chrome-path", chrome_path])
        for item in chrome_args:
            value = str(item or "").strip()
            if value:
                command.append(f"--chrome-arg={value}")

        try:
            completed = subprocess.run(
                command,
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except Exception as exc:
            self._send_json(500, {"success": False, "error": f"launch_failed: {exc}"})
            return

        stdout = str(completed.stdout or "").strip()
        stderr = str(completed.stderr or "").strip()
        if completed.returncode != 0:
            self._send_json(
                500,
                {
                    "success": False,
                    "error": stderr or stdout or f"launcher exit code {completed.returncode}",
                    "exit_code": completed.returncode,
                },
            )
            return

        launch_payload = {}
        if stdout:
            try:
                launch_payload = json.loads(stdout)
            except Exception:
                launch_payload = {}

        self._send_json(
            200,
            {
                "success": True,
                "token_id": token_id,
                "chrome_path": launch_payload.get("chrome_path") or chrome_path or None,
                "command": launch_payload.get("command") or command,
                "pid": launch_payload.get("pid"),
                "launcher_command": command,
                "launcher_token_id": launch_payload.get("token_id"),
                "browser_user_data_dir": launch_payload.get("user_data_dir"),
                "browser_extension_dir": launch_payload.get("extension_dir"),
                "browser_startup_url": launch_payload.get("startup_url"),
                "stopped_pids": launch_payload.get("stopped_pids") or [],
                "cleared_session_files": launch_payload.get("cleared_session_files") or [],
                "cleared_extension_state": launch_payload.get("cleared_extension_state") or [],
                "stdout": stdout,
                "stderr": stderr,
            },
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(
                200,
                {
                    "success": True,
                    "service": "browser_profile_host_bridge",
                    "bind": HOST,
                    "port": PORT,
                    "auth_enabled": bool(API_KEY),
                    "launcher_script": str(LAUNCHER_SCRIPT),
                    "repo_root": str(REPO_ROOT),
                },
            )
            return
        if self.path == "/launch":
            self._send_json(405, {"success": False, "error": "method_not_allowed", "allow": ["POST"]})
            return
        self._send_json(404, {"success": False, "error": "not_found"})

    def log_message(self, format: str, *args) -> None:
        print(f"[host-bridge] {self.address_string()} - {format % args}")


def main() -> int:
    server = ThreadingHTTPServer((HOST, PORT), HostBridgeHandler)
    print(f"[host-bridge] listening on http://{HOST}:{PORT}/launch")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
