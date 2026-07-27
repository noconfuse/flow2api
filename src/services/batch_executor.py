"""Background executor for CSV batch jobs."""

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import BatchJobItem
from .generation_handler import GenerationHandler


MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
HTML_MEDIA_ID_RE = re.compile(r"data-media-id=['\"](.*?)['\"]", re.IGNORECASE)
DEFAULT_ITEM_MAX_ATTEMPTS = 2


class BatchExecutor:
    """Run batch jobs sequentially in the background."""

    def __init__(self, db: Database, generation_handler: GenerationHandler):
        self.db = db
        self.generation_handler = generation_handler
        self._job_tasks: Dict[str, asyncio.Task] = {}
        self._schedule_lock = asyncio.Lock()

    async def schedule(self, job_id: str) -> bool:
        """Schedule a batch job once."""
        normalized_job_id = str(job_id or "").strip()
        if not normalized_job_id:
            return False

        async with self._schedule_lock:
            existing = self._job_tasks.get(normalized_job_id)
            if existing and not existing.done():
                return False

            task = asyncio.create_task(
                self._run_job(normalized_job_id),
                name=f"batch-job:{normalized_job_id}",
            )
            self._job_tasks[normalized_job_id] = task
            return True

    async def _run_job(self, job_id: str) -> None:
        started_at = self._now()
        try:
            await self.db.update_batch_job(job_id, status="running", started_at=started_at)
            await self.db.refresh_batch_job_counts(job_id)

            items = await self.db.list_batch_job_items(job_id)
            for item in items:
                if item.status not in {"pending", "queued"}:
                    continue
                await self._run_item(item)
                await self.db.refresh_batch_job_counts(job_id)

            counts = await self.db.refresh_batch_job_counts(job_id)
            final_status = self._resolve_final_job_status(counts)
            await self.db.update_batch_job(
                job_id,
                status=final_status,
                finished_at=self._now(),
            )
        except Exception as exc:
            debug_logger.log_error(f"[BATCH] Job {job_id} failed: {exc}")
            await self.db.refresh_batch_job_counts(job_id)
            await self.db.update_batch_job(
                job_id,
                status="failed",
                finished_at=self._now(),
            )
        finally:
            async with self._schedule_lock:
                current = self._job_tasks.get(job_id)
                if current and current.done():
                    self._job_tasks.pop(job_id, None)
                elif current is asyncio.current_task():
                    self._job_tasks.pop(job_id, None)

    async def _run_item(self, item: BatchJobItem) -> None:
        await self.db.update_batch_job_item(
            item.id,
            status="running",
            started_at=self._now(),
        )

        try:
            excluded_token_ids: List[int] = []
            attempt_summaries: List[str] = []
            last_error_payload: Optional[Dict[str, Any]] = None

            for attempt_index in range(1, DEFAULT_ITEM_MAX_ATTEMPTS + 1):
                allow_token_fallback = attempt_index > 1
                payload = await self._execute_generation(
                    item,
                    excluded_token_ids=excluded_token_ids,
                    allow_token_fallback=allow_token_fallback,
                )
                error = payload.get("error")
                if not isinstance(error, dict):
                    await self.db.update_batch_job_item(
                        item.id,
                        status="succeeded",
                        result_media_id=self._extract_media_id(payload),
                        result_url=self._extract_result_url(payload),
                        token_id=self._extract_token_id(payload),
                        project_id=self._extract_project_id(payload),
                        finished_at=self._now(),
                    )
                    return

                last_error_payload = payload
                failed_token_id = self._extract_token_id(payload)
                error_message = str(error.get("message") or "生成失败")
                attempt_summaries.append(
                    self._format_attempt_summary(
                        attempt_index=attempt_index,
                        token_id=failed_token_id,
                        error_message=error_message,
                    )
                )

                should_retry = (
                    attempt_index < DEFAULT_ITEM_MAX_ATTEMPTS
                    and self._is_retryable_generation_error(error)
                    and failed_token_id is not None
                    and failed_token_id not in excluded_token_ids
                )
                if not should_retry:
                    break

                excluded_token_ids.append(failed_token_id)
                debug_logger.log_warning(
                    f"[BATCH] Item {item.id} attempt {attempt_index} failed on token {failed_token_id}, "
                    f"retrying with excluded_token_ids={excluded_token_ids}: {error_message}"
                )

            if last_error_payload is None:
                raise RuntimeError("批量执行未返回错误结果")

            final_error = last_error_payload.get("error") if isinstance(last_error_payload, dict) else {}
            final_error_message = self._build_final_error_message(
                str(final_error.get("message") or "生成失败"),
                attempt_summaries,
            )
            await self.db.update_batch_job_item(
                item.id,
                status="failed",
                error_code=str(final_error.get("code") or "generation_failed"),
                error_message=final_error_message,
                token_id=self._extract_token_id(last_error_payload),
                project_id=self._extract_project_id(last_error_payload),
                finished_at=self._now(),
            )
        except Exception as exc:
            debug_logger.log_error(f"[BATCH] Item {item.id} failed: {exc}")
            await self.db.update_batch_job_item(
                item.id,
                status="failed",
                error_code="executor_error",
                error_message=str(exc),
                finished_at=self._now(),
            )

    async def _execute_generation(
        self,
        item: BatchJobItem,
        *,
        excluded_token_ids: Optional[List[int]] = None,
        allow_token_fallback: bool = False,
    ) -> Dict[str, Any]:
        normalized_payload = dict(item.normalized_payload or {})
        result_text: Optional[str] = None
        images = await self._load_image_bytes_list(normalized_payload.get("input_images"))
        video_edit_input = await self._load_video_edit_input(normalized_payload.get("video_edit_input"))
        preferred_token_id = normalized_payload.get("preferred_token_id")
        preferred_project_id = normalized_payload.get("preferred_project_id")
        if allow_token_fallback:
            preferred_token_id = None
            preferred_project_id = None

        async for chunk in self.generation_handler.handle_generation(
            model=str(normalized_payload.get("model") or "").strip(),
            prompt=str(normalized_payload.get("prompt") or "").strip(),
            images=images or None,
            stream=False,
            video_edit_params=normalized_payload.get("video_edit_params"),
            video_edit_input=video_edit_input,
            preferred_token_id=preferred_token_id,
            preferred_project_id=preferred_project_id,
            excluded_token_ids=excluded_token_ids,
            source_image_media_ids=normalized_payload.get("source_image_media_ids"),
            reference_assets=normalized_payload.get("reference_assets"),
            include_internal_payload=True,
        ):
            result_text = chunk

        if not result_text:
            raise RuntimeError("批量执行未返回结果")

        try:
            return json.loads(result_text)
        except Exception as exc:
            raise RuntimeError(f"批量执行结果解析失败: {exc}") from exc

    async def _load_image_bytes_list(self, raw_items: Any) -> List[bytes]:
        if not isinstance(raw_items, list):
            return []
        images: List[bytes] = []
        for item in raw_items:
            images.append(await self._read_file_bytes(item, expected_kind="image"))
        return images

    async def _load_video_edit_input(self, raw_item: Any) -> Optional[Dict[str, Any]]:
        if not isinstance(raw_item, dict):
            return None
        video_bytes = await self._read_file_bytes(raw_item, expected_kind="video")
        payload = dict(raw_item)
        payload["video_bytes"] = video_bytes
        return payload

    async def _read_file_bytes(self, raw_item: Any, *, expected_kind: str) -> bytes:
        if not isinstance(raw_item, dict):
            raise RuntimeError("批量素材元数据格式不正确")
        source_url = str(raw_item.get("source_url") or "").strip()
        file_path_text = str(raw_item.get("file_path") or "").strip()
        file_path = Path(file_path_text) if file_path_text else None
        file_name = str(raw_item.get("file_name") or (file_path.name if file_path else "")).strip()
        asset_kind = str(raw_item.get("asset_kind") or "").strip()

        if asset_kind and asset_kind != expected_kind:
            raise RuntimeError(f"批量素材类型不匹配: {file_name}")
        if source_url:
            return await self._read_remote_file_bytes(source_url, expected_kind=expected_kind, file_name=file_name)
        if not file_path:
            raise RuntimeError(f"批量素材缺少文件路径: {file_name or '-'}")
        if not file_path.exists() or not file_path.is_file():
            raise RuntimeError(f"批量素材文件不存在: {file_name}")

        file_bytes = file_path.read_bytes()
        if not file_bytes:
            raise RuntimeError(f"批量素材文件为空: {file_name}")
        return file_bytes

    async def _read_remote_file_bytes(self, source_url: str, *, expected_kind: str, file_name: str) -> bytes:
        file_cache = getattr(self.generation_handler, "file_cache", None)
        if file_cache is None or not hasattr(file_cache, "download_and_cache"):
            raise RuntimeError("当前服务未初始化远程素材下载能力")
        cached_name = await file_cache.download_and_cache(source_url, expected_kind)
        if not cached_name:
            raise RuntimeError(f"远程素材下载失败: {file_name or source_url}")
        cache_dir = Path(getattr(file_cache, "cache_dir", "tmp"))
        cached_path = cache_dir / cached_name
        if not cached_path.exists() or not cached_path.is_file():
            raise RuntimeError(f"远程素材缓存文件不存在: {file_name or source_url}")
        content = cached_path.read_bytes()
        if not content:
            raise RuntimeError(f"远程素材文件为空: {file_name or source_url}")
        return content

    def _resolve_final_job_status(self, counts: Dict[str, int]) -> str:
        total = int(counts.get("total_count") or 0)
        pending = int(counts.get("pending_count") or 0)
        running = int(counts.get("running_count") or 0)
        success = int(counts.get("success_count") or 0)
        failed = int(counts.get("failed_count") or 0)

        if pending > 0 or running > 0:
            return "running"
        if total <= 0:
            return "failed"
        if success == total:
            return "completed"
        if failed == total:
            return "failed"
        if success > 0 and failed > 0:
            return "partially_failed"
        return "failed"

    def _extract_result_url(self, payload: Dict[str, Any]) -> Optional[str]:
        direct_url = payload.get("url")
        if isinstance(direct_url, str) and direct_url.strip():
            return direct_url.strip()

        content = self._extract_message_content(payload)
        image_match = MARKDOWN_IMAGE_RE.search(content)
        if image_match:
            return image_match.group(1).strip()

        video_match = HTML_VIDEO_RE.search(content)
        if video_match:
            return video_match.group(1).strip()

        return None

    def _extract_media_id(self, payload: Dict[str, Any]) -> Optional[str]:
        generated_assets = payload.get("generated_assets")
        if isinstance(generated_assets, dict):
            media_id = (
                generated_assets.get("mediaGenerationId")
                or generated_assets.get("media_generation_id")
            )
            if isinstance(media_id, str) and media_id.strip():
                return media_id.strip()

        content = self._extract_message_content(payload)
        match = HTML_MEDIA_ID_RE.search(content)
        if match:
            return match.group(1).strip()
        return None

    def _extract_token_id(self, payload: Dict[str, Any]) -> Optional[int]:
        value = payload.get("token_id")
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    def _extract_project_id(self, payload: Dict[str, Any]) -> Optional[str]:
        value = payload.get("project_id")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _extract_message_content(self, payload: Dict[str, Any]) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else {}
        content = message.get("content") if isinstance(message, dict) else ""
        return content if isinstance(content, str) else ""

    def _is_retryable_generation_error(self, error: Dict[str, Any]) -> bool:
        status_code = error.get("status_code")
        try:
            normalized_status = int(status_code)
        except Exception:
            normalized_status = 500
        return normalized_status >= 500

    def _format_attempt_summary(
        self,
        *,
        attempt_index: int,
        token_id: Optional[int],
        error_message: str,
    ) -> str:
        token_text = f"token={token_id}" if token_id is not None else "token=unknown"
        return f"第{attempt_index}次({token_text}): {error_message}"

    def _build_final_error_message(self, error_message: str, attempt_summaries: List[str]) -> str:
        if len(attempt_summaries) <= 1:
            return error_message
        return f"{error_message}；重试轨迹: {' | '.join(attempt_summaries)}"

    def _now(self) -> str:
        return datetime.utcnow().isoformat(sep=" ", timespec="seconds")
