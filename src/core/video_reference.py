"""Helpers for parsing video reference URIs used by the unified generation APIs."""

from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse


def parse_video_edit_uri(uri: str) -> Dict[str, Any]:
    """Parse edit://MEDIA_ID?start_time=0.00&end_time=4.00 or frame-based references."""
    parsed = urlparse(uri)
    media_id = (parsed.netloc or parsed.path.lstrip("/")).strip()
    if not media_id:
        raise ValueError("edit:// 引用缺少 mediaGenerationId")

    query = parse_qs(parsed.query, keep_blank_values=False)

    def _read_int(*names: str) -> Optional[int]:
        for name in names:
            values = query.get(name)
            if not values:
                continue
            raw = str(values[0]).strip()
            if raw == "":
                continue
            try:
                return int(raw)
            except ValueError as exc:
                raise ValueError(f"edit:// 参数 {name} 必须是整数") from exc
        return None

    def _read_float(*names: str) -> Optional[float]:
        for name in names:
            values = query.get(name)
            if not values:
                continue
            raw = str(values[0]).strip()
            if raw == "":
                continue
            try:
                value = float(raw)
            except ValueError as exc:
                raise ValueError(f"edit:// 参数 {name} 必须是数字") from exc
            if value < 0:
                raise ValueError(f"edit:// 参数 {name} 不能为负数")
            return value
        return None

    start_frame_index = _read_int("start_frame", "startFrame", "startFrameIndex")
    end_frame_index = _read_int("end_frame", "endFrame", "endFrameIndex")
    selected_material_index = _read_int(
        "selected_material_index",
        "selectedMaterialIndex",
        "material_index",
        "materialIndex",
        "source_index",
        "sourceIndex",
    )
    start_seconds = _read_float("start_time", "startTime", "start_seconds", "startSeconds")
    end_seconds = _read_float("end_time", "endTime", "end_seconds", "endSeconds")
    source_duration_seconds = _read_float(
        "source_duration",
        "sourceDuration",
        "source_duration_seconds",
        "sourceDurationSeconds",
        "duration",
    )

    has_frame_range = start_frame_index is not None or end_frame_index is not None
    has_time_range = start_seconds is not None or end_seconds is not None
    if has_frame_range and has_time_range:
        raise ValueError("edit:// 不能同时混用 frame 范围和 time 范围参数")
    if has_time_range:
        if start_seconds is None or end_seconds is None:
            raise ValueError("edit:// 需要同时提供 start_time 和 end_time 参数")
        if end_seconds < start_seconds:
            raise ValueError("edit:// 的 end_time 不能小于 start_time")
        return {
            "media_id": media_id,
            "selected_material_index": selected_material_index,
            "start_seconds": float(start_seconds),
            "end_seconds": float(end_seconds),
            "source_duration_seconds": float(source_duration_seconds) if source_duration_seconds is not None else None,
        }

    if start_frame_index is None or end_frame_index is None:
        raise ValueError("edit:// 需要同时提供 start_time/end_time 或 start_frame/end_frame 参数")
    if start_frame_index < 0 or end_frame_index < 0:
        raise ValueError("edit:// 的帧范围不能为负数")
    if end_frame_index < start_frame_index:
        raise ValueError("edit:// 的 end_frame 不能小于 start_frame")

    return {
        "media_id": media_id,
        "selected_material_index": selected_material_index,
        "start_frame_index": start_frame_index,
        "end_frame_index": end_frame_index,
        "source_duration_seconds": float(source_duration_seconds) if source_duration_seconds is not None else None,
    }
