#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


HOST = os.environ.get("DEBUG_EVENT_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEBUG_EVENT_PORT", "7778"))
ROOT = Path(__file__).resolve().parent.parent
DBG_DIR = ROOT / ".dbg"


def _safe_session_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "-" for ch in value.strip())
    cleaned = cleaned.strip("-_")
    return cleaned or "default"


def _log_path_for_session(session_id: str) -> Path:
    name = _safe_session_name(session_id)
    return DBG_DIR / f"trae-debug-log-{name}.ndjson"


class DebugEventHandler(BaseHTTPRequestHandler):
    server_version = "TraeDebugEventServer/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self._send_json(200, {"ok": True})

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"ok": True, "port": PORT, "root": str(DBG_DIR)})
            return
        self._send_json(404, {"ok": False, "error": "not_found"})

    def do_POST(self) -> None:
        if self.path != "/event":
            self._send_json(404, {"ok": False, "error": "not_found"})
            return
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            self._send_json(400, {"ok": False, "error": f"invalid_json:{type(exc).__name__}"})
            return

        session_id = str(payload.get("sessionId") or payload.get("session_id") or "").strip()
        if not session_id:
            self._send_json(400, {"ok": False, "error": "missing_session_id"})
            return

        DBG_DIR.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": int(time.time() * 1000),
            "session_id": session_id,
            "run_id": payload.get("runId"),
            "hypothesis_id": payload.get("hypothesisId"),
            "location": payload.get("location"),
            "msg": payload.get("msg"),
            "data": payload.get("data"),
            "raw": payload,
        }
        path = _log_path_for_session(session_id)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        self._send_json(200, {"ok": True, "path": str(path)})


def main() -> None:
    DBG_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), DebugEventHandler)
    print(f"debug-event-server listening on http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
