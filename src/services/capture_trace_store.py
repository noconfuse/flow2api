from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
TRACE_ROOT = REPO_ROOT / ".dbg" / "capture_traces"


class CaptureTraceStore:
    def __init__(self, base_dir: Path | None = None) -> None:
        self.base_dir = base_dir or TRACE_ROOT
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append_event(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        trace_id = self._normalize_trace_id(payload)
        trace_dir = self.base_dir / trace_id
        trace_dir.mkdir(parents=True, exist_ok=True)

        metadata_path = trace_dir / "metadata.json"
        events_path = trace_dir / "events.jsonl"

        event = dict(payload or {})
        event["trace_id"] = trace_id
        event.setdefault("received_at", self._now_iso())

        metadata = self._build_metadata(event, trace_dir)

        with self._lock:
            if metadata_path.exists():
                try:
                    existing = json.loads(metadata_path.read_text(encoding="utf-8") or "{}")
                except Exception:
                    existing = {}
                metadata = {**existing, **metadata}
            metadata_path.write_text(
                json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with events_path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(event, ensure_ascii=False) + "\n")

        return {
            "success": True,
            "trace_id": trace_id,
            "trace_dir": str(trace_dir),
            "event_path": str(events_path),
        }

    def _normalize_trace_id(self, payload: Dict[str, Any]) -> str:
        raw = str(
            payload.get("trace_id")
            or payload.get("traceId")
            or payload.get("session_id")
            or payload.get("sessionId")
            or ""
        ).strip()
        return raw or f"trace-{uuid4().hex[:12]}"

    def _build_metadata(self, event: Dict[str, Any], trace_dir: Path) -> Dict[str, Any]:
        page = event.get("page") if isinstance(event.get("page"), dict) else {}
        return {
            "trace_id": event.get("trace_id"),
            "run_id": event.get("run_id") or event.get("runId") or "",
            "session_id": event.get("session_id") or event.get("sessionId") or "",
            "route_key": event.get("route_key") or "",
            "project_id": event.get("project_id") or page.get("project_id") or "",
            "last_page_url": page.get("url") or "",
            "last_page_title": page.get("title") or "",
            "updated_at": self._now_iso(),
            "trace_dir": str(trace_dir),
        }

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

