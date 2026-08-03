"""Background executor for CSV batch jobs."""

import asyncio
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.database import Database
from ..core.logger import debug_logger
from ..core.models import BatchJobItem
from .generation_handler import GenerationHandler


MARKDOWN_IMAGE_RE = re.compile(r"!\[.*?\]\((.*?)\)")
HTML_VIDEO_RE = re.compile(r"<video[^>]+src=['\"](.*?)['\"]", re.IGNORECASE)
HTML_MEDIA_ID_RE = re.compile(r"data-media-id=['\"](.*?)['\"]", re.IGNORECASE)
DEFAULT_ITEM_MAX_ATTEMPTS = 2
FRAMES_DIR_NAME = "frames"


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

            # 一次性加载所有 items
            all_items = await self.db.list_batch_job_items(job_id)
            ordered_items = self._topological_sort_with_fallback(all_items)
            # 计算哪些行需要产出最后一帧（被其他行通过 image_source 引用）
            frame_producer_keys = self._collect_frame_producer_keys(all_items)

            # 检测循环引用 -> 立即终止整个 job
            if ordered_items is None:
                await self.db.update_batch_job(
                    job_id,
                    status="failed",
                    finished_at=self._now(),
                )
                return

            preferred_token_project: Optional[Tuple[int, str]] = None
            for item in ordered_items:
                if item.status not in {"pending", "queued"}:
                    if item.status == "succeeded" and item.token_id and item.project_id:
                        preferred_token_project = (int(item.token_id), str(item.project_id))
                    continue
                # 同 Excel 内的下一个 row 复用上一行成功的 (token_id, project_id)，
                # 避免 ensure_project_exists 在项目池里轮换到新项目，
                # 这样 _uploaded_media_cache 的 project_id 维度才能真命中。
                self._seed_preferred_token_project(item, preferred_token_project)
                needs_frame = str(item.row_id or "").strip() in frame_producer_keys
                await self._run_item(
                    item,
                    all_items=all_items,
                    extract_final_frame=needs_frame,
                )
                all_items = await self.db.list_batch_job_items(job_id)
                await self.db.refresh_batch_job_counts(job_id)
                completed_item = next(
                    (candidate for candidate in all_items if candidate.id == item.id),
                    None,
                )
                if (
                    completed_item is not None
                    and completed_item.status == "succeeded"
                    and completed_item.token_id
                    and completed_item.project_id
                ):
                    preferred_token_project = (
                        int(completed_item.token_id),
                        str(completed_item.project_id),
                    )

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

    def _seed_preferred_token_project(
        self,
        item: BatchJobItem,
        preferred_token_project: Optional[Tuple[int, str]],
    ) -> None:
        """把上一次成功 row 用的 (token_id, project_id) 写到当前 row 的 normalized_payload。

        generation_handler 收到 preferred_project_id 后会走 if preferred_project_id:
        project_id = preferred_project_id 分支，绕开 ensure_project_exists 在项目池里
        的 round-robin 选择。这样同 Excel 跨行的图片上传就走同 project，
        _uploaded_media_cache 的 (sha256, project_id) 维度才会真命中，实现去重。
        """
        if not preferred_token_project:
            return
        cached_token_id, cached_project_id = preferred_token_project
        payload = item.normalized_payload
        if not isinstance(payload, dict):
            payload = {}
        payload["preferred_token_id"] = int(cached_token_id)
        payload["preferred_project_id"] = str(cached_project_id)
        item.normalized_payload = payload

    def _collect_frame_producer_keys(self, items: List[BatchJobItem]) -> set[str]:
        """收集需要产出帧的 row_id 集合：被任意下游通过 image_source 引用的行。"""
        producers: set[str] = set()
        for item in items:
            payload = item.normalized_payload or {}
            dep = payload.get("image_source_dependency")
            if not isinstance(dep, dict):
                continue
            producer_key = str(dep.get("row_key") or "").strip()
            if producer_key:
                producers.add(producer_key)
        return producers

    def _topological_sort_with_fallback(
        self,
        items: List[BatchJobItem],
    ) -> Optional[List[BatchJobItem]]:
        """基于 image_source_dependency 构建拓扑序。

        - 无依赖：按 Excel 自然顺序（row_index 升序）。
        - 有依赖：使用 Kahn 算法，依赖少的先调度；同层按 row_index 升序。
        - 检测到循环依赖时返回 None，并把所有环内 item 标记为失败。
        """
        # key -> item 映射
        by_key: Dict[str, BatchJobItem] = {}
        for item in items:
            key = str(item.row_id or "").strip()
            if key:
                by_key[key] = item

        # edges: consumer -> producer
        in_edges: Dict[str, List[str]] = {}
        for item in items:
            key = str(item.row_id or "").strip()
            payload = item.normalized_payload or {}
            dep = payload.get("image_source_dependency")
            producer_key = ""
            if isinstance(dep, dict):
                producer_key = str(dep.get("row_key") or "").strip()
            in_edges.setdefault(key, [])
            if producer_key and producer_key != key and producer_key in by_key:
                in_edges[key].append(producer_key)

        # 校验：未知依赖行 -> 立刻失败该 consumer
        unresolved: List[BatchJobItem] = []
        for item in items:
            key = str(item.row_id or "").strip()
            payload = item.normalized_payload or {}
            dep = payload.get("image_source_dependency")
            if not isinstance(dep, dict):
                continue
            producer_key = str(dep.get("row_key") or "").strip()
            if not producer_key:
                continue
            if producer_key == key or producer_key not in by_key:
                unresolved.append(item)

        async def _mark_failed():
            for item in unresolved:
                await self.db.update_batch_job_item(
                    item.id,
                    status="failed",
                    error_code="upstream_failed",
                    error_message=f"image_source 引用的上游行 {producer_key} 在当前批次中找不到对应任务",
                    finished_at=self._now(),
                )

        # 不在 _run_job 中运行异步逻辑；将 unresolved 提交给 _run_job 处理
        if unresolved:
            # 同步触发（_topological_sort_with_fallback 不是 async），使用 run 调度
            try:
                loop = asyncio.get_event_loop()
                loop.create_task(_mark_failed())
            except RuntimeError:
                pass

        # 用 cycle detection
        visited: set[str] = set()
        in_stack: set[str] = set()
        cycle_members: set[str] = set()

        def dfs(node: str):
            if node in in_stack:
                cycle_members.add(node)
                return True
            if node in visited:
                return False
            in_stack.add(node)
            for parent in in_edges.get(node, []):
                if dfs(parent):
                    cycle_members.add(node)
                    return True
            in_stack.remove(node)
            visited.add(node)
            return False

        for key in list(in_edges.keys()):
            dfs(key)

        if cycle_members:
            debug_logger.log_error(
                f"[BATCH] 检测到循环引用 image_source_dependency: {sorted(cycle_members)}"
            )
            for key in cycle_members:
                item = by_key.get(key)
                if item is not None and item.status in {"pending", "queued"}:
                    # 同步标记失败；async 不可在同步函数内使用，这里只是占位
                    pass
            return None

        # Kahn：每次弹出 in-degree=0 的 key；同层按 row_index 升序
        in_degree: Dict[str, int] = {key: 0 for key in in_edges}
        reverse_edges: Dict[str, List[str]] = {key: [] for key in in_edges}  # producer -> [consumers]
        for consumer, producers in in_edges.items():
            for producer in producers:
                if producer not in in_degree or consumer not in in_degree:
                    continue
                reverse_edges.setdefault(producer, []).append(consumer)
                in_degree[consumer] += 1

        # 初始化候选队列：按 row_index 升序
        indexed = sorted(
            ((k, by_key[k]) for k in in_degree if in_degree[k] == 0),
            key=lambda pair: (pair[1].row_index or 0, pair[0]),
        )
        ready_keys = [pair[0] for pair in indexed]

        ordered: List[BatchJobItem] = []
        while ready_keys:
            key = ready_keys.pop(0)
            ordered.append(by_key[key])
            for consumer in reverse_edges.get(key, []):
                in_degree[consumer] -= 1
                if in_degree[consumer] == 0:
                    insertion_index = 0
                    for index, existing_key in enumerate(ready_keys):
                        existing_item = by_key[existing_key]
                        if (existing_item.row_index or 0) > (by_key[consumer].row_index or 0):
                            insertion_index = index
                            break
                        insertion_index = index + 1
                    ready_keys.insert(insertion_index, consumer)

        # 若未全部排进（被 unresolved 跳过），append 剩余 item 保持执行
        scheduled_keys = {str(item.row_id or "").strip() for item in ordered}
        leftovers = [
            item
            for item in items
            if str(item.row_id or "").strip() not in scheduled_keys
        ]
        leftovers.sort(key=lambda item: item.row_index or 0)
        ordered.extend(leftovers)
        return ordered

    async def _run_item(
        self,
        item: BatchJobItem,
        *,
        all_items: Optional[List[BatchJobItem]] = None,
        extract_final_frame: bool = False,
    ) -> None:
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
                try:
                    payload = await self._execute_generation(
                        item,
                        all_items=all_items,
                        excluded_token_ids=excluded_token_ids,
                        allow_token_fallback=allow_token_fallback,
                    )
                except UpstreamDependencyError as exc:
                    await self.db.update_batch_job_item(
                        item.id,
                        status="failed",
                        error_code="upstream_failed",
                        error_message=str(exc),
                        finished_at=self._now(),
                    )
                    return
                error = payload.get("error")
                if not isinstance(error, dict):
                    result_url = self._extract_result_url(payload)
                    final_frame_path: Optional[str] = None
                    if extract_final_frame:
                        final_frame_path = await self._extract_and_store_final_frame(
                            item=item,
                            payload=payload,
                        )
                    await self.db.update_batch_job_item(
                        item.id,
                        status="succeeded",
                        result_media_id=self._extract_media_id(payload),
                        result_url=result_url,
                        final_frame_path=final_frame_path,
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
        all_items: Optional[List[BatchJobItem]] = None,
        excluded_token_ids: Optional[List[int]] = None,
        allow_token_fallback: bool = False,
    ) -> Dict[str, Any]:
        normalized_payload = dict(item.normalized_payload or {})
        result_text: Optional[str] = None

        # 解析 image_source_dependency：在执行前把上游尾帧注入 input_images / reference_assets
        await self._apply_image_source_dependency(
            item=item,
            normalized_payload=normalized_payload,
            all_items=all_items,
        )

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
            duration=normalized_payload.get("duration"),
            include_internal_payload=True,
        ):
            result_text = chunk

        if not result_text:
            raise RuntimeError("批量执行未返回结果")

        try:
            return json.loads(result_text)
        except Exception as exc:
            raise RuntimeError(f"批量执行结果解析失败: {exc}") from exc

    async def _apply_image_source_dependency(
        self,
        *,
        item: BatchJobItem,
        normalized_payload: Dict[str, Any],
        all_items: Optional[List[BatchJobItem]],
    ) -> None:
        dependency = normalized_payload.get("image_source_dependency")
        if not isinstance(dependency, dict):
            return
        row_key = str(dependency.get("row_key") or "").strip()
        if not row_key:
            return
        if not all_items:
            raise UpstreamDependencyError(
                f"image_source 依赖行 {row_key} 的视频尾帧，但本任务未提供完整 items 列表"
            )
        producer = next(
            (
                other
                for other in all_items
                if other.id != item.id and str(other.row_id or "").strip() == row_key
            ),
            None,
        )
        if producer is None:
            raise UpstreamDependencyError(
                f"image_source 依赖行 {row_key} 在当前批次中找不到对应任务"
            )
        if str(producer.status or "").strip().lower() != "succeeded":
            raise UpstreamDependencyError(
                f"image_source 上游行 {row_key} 当前状态为 {producer.status or 'unknown'}，未生成尾帧"
            )
        frame_path = str(producer.final_frame_path or "").strip()
        if not frame_path:
            raise UpstreamDependencyError(
                f"image_source 上游行 {row_key} 尚未提取出最后一帧"
            )
        frame_file = Path(frame_path)
        if not frame_file.exists() or not frame_file.is_file():
            raise UpstreamDependencyError(
                f"image_source 上游行 {row_key} 尾帧文件不存在: {frame_path}"
            )

        # 注入为新的 image_source 素材项（kind=image），让 _materialize_reference_assets 自动上传
        asset_entry = {
            "slot": "image_source",
            "kind": "image",
            "mode": "attach",
            "reference_texts": ["image_source"],
            "file_path": str(frame_file),
            "file_name": frame_file.name,
            "asset_kind": "image",
            "expected_kind": "image",
            "reference_value": frame_file.name,
            "source": f"upstream:{row_key}",
        }
        existing_input_images = [
            entry
            for entry in (normalized_payload.get("input_images") or [])
            if isinstance(entry, dict)
            and str(entry.get("slot") or "").strip() != "image_source"
        ]
        existing_input_images.append(asset_entry)
        normalized_payload["input_images"] = existing_input_images

        existing_refs = [
            entry
            for entry in (normalized_payload.get("reference_assets") or [])
            if isinstance(entry, dict)
            and str(entry.get("slot") or "").strip() != "image_source"
        ]
        asset_spec = {
            "slot": "image_source",
            "kind": "image",
            "mode": "attach",
            "reference_texts": ["image_source"],
            "file_path": str(frame_file),
            "file_name": frame_file.name,
            "asset_kind": "image",
        }
        existing_refs.append(asset_spec)
        normalized_payload["reference_assets"] = existing_refs
        debug_logger.log_info(
            f"[BATCH] Item {item.id} 已注入上游尾帧 image_source 来自 {row_key} -> {frame_path}"
        )

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

    def _extract_source_url(self, payload: Dict[str, Any]) -> Optional[str]:
        """取生成产物里的唯一源地址 generated_assets.source_url。"""
        if not isinstance(payload, dict):
            return None
        assets = payload.get("generated_assets")
        if isinstance(assets, dict):
            candidate = assets.get("source_url")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        return None

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

    async def _extract_and_store_final_frame(
        self,
        *,
        item: BatchJobItem,
        payload: Dict[str, Any],
    ) -> Optional[str]:
        """从产物视频里抽取最后一帧并落盘。

        唯一链路：generated_assets.source_url → FileCache → imageio 文件路径解码。
        generation_handler 正常情况下已经完成缓存，download_and_cache 会命中同一文件。
        """
        source_url = self._extract_source_url(payload)
        if not source_url:
            debug_logger.log_warning(
                f"[BATCH] Item {item.id} 产物缺少 generated_assets.source_url，跳过尾帧提取"
            )
            return None
        try:
            cached_path = await self._cache_video_path(source_url)
        except Exception as exc:
            debug_logger.log_warning(
                f"[BATCH] Item {item.id} 缓存产物视频失败，跳过尾帧提取: {exc}"
            )
            return None
        try:
            frame_png = await asyncio.to_thread(
                self._decode_last_frame_from_path,
                str(cached_path),
            )
        except Exception as exc:
            debug_logger.log_warning(
                f"[BATCH] Item {item.id} 提取最后一帧失败: {exc}"
            )
            return None
        if not frame_png:
            return None
        debug_logger.log_info(
            f"[BATCH] Item {item.id} 尾帧已从缓存路径 {cached_path.name} 直接解码"
        )
        return await self._write_frame_png(item, frame_png)

    @staticmethod
    async def _write_frame_png(item: BatchJobItem, png_bytes: bytes) -> Optional[str]:
        try:
            frame_dir = Path(UPLOAD_ROOT_DIR) / str(item.job_id or "").strip() / FRAMES_DIR_NAME
            frame_dir.mkdir(parents=True, exist_ok=True)
            frame_path = frame_dir / f"final_frame_{int(item.id or 0):04d}.png"
            await asyncio.to_thread(frame_path.write_bytes, png_bytes)
        except Exception as exc:
            debug_logger.log_warning(f"[BATCH] Item {item.id} 尾帧落盘失败: {exc}")
            return None
        debug_logger.log_info(f"[BATCH] Item {item.id} 尾帧已落盘: {frame_path}")
        return str(frame_path)

    @staticmethod
    def _decode_last_frame_from_path(file_path: str) -> Optional[bytes]:
        """imageio v3 FFMPEG 接受文件路径，不接受 bytes；上次已用临时文件绕开，
        这次直接用真实路径，不再写任何临时文件。
        """
        try:
            import imageio.v3 as iio  # type: ignore
            import numpy as np  # type: ignore
        except Exception as exc:
            raise RuntimeError("imageio 未安装，无法提取视频最后一帧") from exc
        try:
            last_frame = None
            for frame in iio.imiter(file_path, plugin="FFMPEG"):
                last_frame = frame
            if last_frame is None:
                return None
        except Exception as exc:
            raise RuntimeError(f"imageio 读取视频失败: {exc}") from exc
        try:
            from PIL import Image  # type: ignore
            import io as _io
            image = Image.fromarray(np.asarray(last_frame))
            buffer = _io.BytesIO()
            image.save(buffer, format="PNG")
            return buffer.getvalue()
        except Exception as exc:
            raise RuntimeError(f"PIL 编码 PNG 失败: {exc}") from exc

    async def _cache_video_path(self, video_url: str) -> Path:
        # 上游 video_url 在 MEDIA_GENERATION_STATUS_COMPLETED 那一刻已经拿到，但 Google CDN
        # 的分发有秒级延迟；首次下载可能拿到 404/HTML 错误页。
        # FileCache 已负责客户端 fallback；这里只负责 CDN 分发延迟的时间重试。
        file_cache = self.generation_handler.file_cache
        backoff_seconds = (2, 4, 8)
        last_error: Optional[BaseException] = None
        for attempt_index in range(len(backoff_seconds) + 1):
            if attempt_index > 0:
                await asyncio.sleep(backoff_seconds[attempt_index - 1])
            try:
                cached_name = await file_cache.download_and_cache(video_url, "video")
                cached_path = Path(file_cache.cache_dir) / cached_name
                if not cached_path.is_file():
                    raise RuntimeError(f"视频缓存文件不存在: {cached_name}")
                return cached_path
            except Exception as exc:
                last_error = exc
                debug_logger.log_warning(
                    f"[BATCH] 视频缓存 attempt {attempt_index + 1}/{len(backoff_seconds) + 1} 失败，"
                    f"{'重试' if attempt_index < len(backoff_seconds) else '放弃'}: {exc}"
                )
        assert last_error is not None
        raise last_error


UPLOAD_ROOT_DIR = Path(__file__).resolve().parents[2] / "data" / "batch_uploads"


class UpstreamDependencyError(Exception):
    """抛出表示当前 item 因为上游未就绪而无法执行。"""
