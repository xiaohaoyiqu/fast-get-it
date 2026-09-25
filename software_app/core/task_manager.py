from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapter import CrawlerAdapter, TaskCancelled
from .archive_service import run_archive_postprocess
from .blocklist import BlocklistStore, work_from_target
from .events import CallbackSet
from .models import DownloadTask, FileRecord, ProgressEvent, TargetPreview
from .storage import AppStorage


@dataclass
class RunningTask:
    task: DownloadTask
    thread: threading.Thread
    cancel_event: threading.Event
    adapter: CrawlerAdapter


class TaskManager:
    def __init__(self, storage: AppStorage) -> None:
        self.storage = storage
        self.blocklist = BlocklistStore(storage.db_path.with_name("blocklist.json"))
        self.adapters: dict[str, CrawlerAdapter] = {}
        self.running: dict[str, RunningTask] = {}
        self._slot_condition = threading.Condition()
        self._active_by_module: dict[str, int] = {}
        self._active_by_group: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def register_adapter(self, adapter: CrawlerAdapter) -> None:
        self.adapters[adapter.module_id] = adapter
        self.storage.register_module(adapter.info)

    def get_concurrency_limits(self, module_id: str) -> tuple[int, int, int]:
        adapter = self.get_adapter(module_id)
        total_default = max(1, int(adapter.max_concurrency))
        total = self._setting_limit(f"task_concurrency_{module_id}", total_default)
        single = self._setting_limit(f"task_concurrency_single_{module_id}", total)
        collection = self._setting_limit(f"task_concurrency_collection_{module_id}", 1)
        return total, single, collection

    def _setting_limit(self, key: str, default: int) -> int:
        try:
            return max(1, min(20, int(self.storage.get_setting(key, default))))
        except (TypeError, ValueError):
            return max(1, min(20, int(default)))

    def list_adapters(self) -> list[CrawlerAdapter]:
        return list(self.adapters.values())

    def get_adapter(self, module_id: str) -> CrawlerAdapter:
        try:
            return self.adapters[module_id]
        except KeyError as exc:
            raise KeyError(f"Unknown module: {module_id}") from exc

    def preview_target(
        self,
        module_id: str,
        raw_target: str,
        options: dict[str, Any] | None = None,
    ) -> TargetPreview:
        return self.get_adapter(module_id).preview_target(raw_target, options)

    def start_task(
        self,
        module_id: str,
        target: str,
        output_dir: Path | str,
        options: dict[str, Any] | None = None,
        callbacks: CallbackSet | None = None,
    ) -> DownloadTask:
        adapter = self.get_adapter(module_id)
        task_options = dict(options or {})
        total_limit, single_limit, collection_limit = self.get_concurrency_limits(module_id)
        task_options["_task_concurrency_total"] = total_limit
        task_options["_task_concurrency_single"] = single_limit
        task_options["_task_concurrency_collection"] = collection_limit
        task_options["_task_concurrency_group"] = self._concurrency_group(module_id, target, task_options)
        author_id = str(task_options.get("author_id") or "")
        inspected_work_id = str(task_options.get("pixiv_work_id") or "")
        if module_id in {"google_image", "website"} and inspected_work_id:
            if work_from_target(module_id, target) != ("pixiv", inspected_work_id):
                author_id = ""
        if self.blocklist.is_blocked(
            module_id, target,
            input_kind=str(task_options.get("input_kind") or ""),
            author_id=author_id,
        ):
            raise ValueError("该作者账号或具体作品已在黑名单中；任务未创建")
        task = DownloadTask(
            task_id=uuid.uuid4().hex[:12],
            module_id=module_id,
            target=target,
            output_dir=Path(output_dir).expanduser(),
            options=task_options,
        )
        self.storage.create_task(task.task_id, module_id, target, str(task.output_dir), task.options)

        cancel_event = threading.Event()
        thread = threading.Thread(
            target=self._run_task,
            args=(adapter, task, callbacks or CallbackSet(), cancel_event),
            name=f"download-{task.task_id}",
            daemon=True,
        )
        with self._lock:
            self.running[task.task_id] = RunningTask(task, thread, cancel_event, adapter)
        thread.start()
        return task

    @staticmethod
    def _concurrency_group(module_id: str, target: str, options: dict[str, Any]) -> str:
        kind = str(options.get("input_kind") or "").strip().casefold()
        scope = str(options.get("content_scope") or options.get("search_mode") or "").strip().casefold()
        value = str(target or "").strip().casefold()
        if module_id == "pixiv":
            if kind in {"work", "novel"}:
                return "single"
            if (kind == "fanbox" and "/posts/" in value) or (kind == "sketch" and "/artworks/" in value):
                return "single"
            return "collection"
        if module_id == "jmcomic":
            return "single" if kind in {"album", "photo"} or scope in {"漫画 id / 链接", "章节 id / 链接"} else "collection"
        if module_id == "twitter":
            return "single" if "/status/" in value else "collection"
        if module_id == "bluesky":
            return "single" if "/post/" in value else "collection"
        if module_id == "instagram":
            return "single" if any(route in value for route in ("/p/", "/reel/", "/tv/")) else "collection"
        if module_id == "ehentai":
            return "single" if scope == "画廊链接 / id" or "/g/" in value else "collection"
        if module_id in {"google_image", "website"}:
            return "single"
        return "single"

    def retry_task(
        self,
        task_id: str,
        callbacks: CallbackSet | None = None,
        *,
        allow_empty_completed: bool = False,
        option_overrides: dict[str, Any] | None = None,
    ) -> DownloadTask:
        """Create a new task from a failed/cancelled task or a legacy empty discovery task."""
        row = self.storage.get_task(task_id)
        if row is None:
            raise KeyError(f"找不到任务: {task_id}")
        status = str(row["status"])
        try:
            options = json.loads(str(row["options_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            options = {}
        if not isinstance(options, dict):
            options = {}
        empty_completed = (
            status == "completed"
            and allow_empty_completed
            and str(row["module_id"]) == "website"
            and options.get("discovery_source") == "google_image"
            and not self.storage.list_files(task_id=task_id, limit=1)
        )
        if status not in {"failed", "cancelled"} and not empty_completed:
            raise ValueError(f"状态为 {status} 的任务不能重试")
        if option_overrides:
            options.update(option_overrides)
        return self.start_task(
            str(row["module_id"]),
            str(row["target"]),
            Path(str(row["output_dir"])),
            options,
            callbacks,
        )

    def cancel(self, task_id: str) -> None:
        with self._lock:
            running = self.running.get(task_id)
        if not running:
            return
        already_requested = running.cancel_event.is_set()
        self.storage.update_task_status(task_id, "cancelling", "正在停止任务")
        running.cancel_event.set()
        if already_requested:
            return
        self.storage.add_log(task_id, running.task.module_id, "warning", "用户请求取消任务")

        # Browser driver shutdown can itself block. Never run it on Tk's UI
        # thread; the shared event already lets cooperative adapters stop now.
        def stop_adapter() -> None:
            try:
                running.adapter.cancel(task_id)
            except Exception as exc:  # noqa: BLE001
                self.storage.add_log(task_id, running.task.module_id, "warning", f"停止适配器时返回: {exc}")

        threading.Thread(target=stop_adapter, name=f"cancel-{task_id}", daemon=True).start()

    def active_task_ids(self) -> list[str]:
        with self._lock:
            return list(self.running)

    def has_active_task(self, module_id: str, target: str, output_dir: str) -> bool:
        normalized_target = str(target or "").strip().casefold()
        normalized_output = str(output_dir or "").strip().casefold()
        with self._lock:
            return any(
                item.task.module_id == module_id
                and item.task.target.strip().casefold() == normalized_target
                and str(item.task.output_dir).strip().casefold() == normalized_output
                for item in self.running.values()
            )

    def shutdown(self, timeout: float = 8.0) -> list[str]:
        """Cancel active work and wait briefly for adapters to release browsers/files."""
        task_ids = self.active_task_ids()
        for task_id in task_ids:
            self.cancel(task_id)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for task_id in task_ids:
            self.wait(task_id, max(0.0, deadline - time.monotonic()))
        unfinished = set(self.active_task_ids())
        for task_id in unfinished:
            self.storage.update_task_status(task_id, "cancelled", "软件退出时停止任务")
        return sorted(unfinished)

    def wait(self, task_id: str, timeout: float | None = None) -> None:
        with self._lock:
            running = self.running.get(task_id)
        if running:
            running.thread.join(timeout)

    def _run_task(
        self,
        adapter: CrawlerAdapter,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> None:
        status = "completed"
        error = ""
        normalized_target = ""
        emitted_records: list[FileRecord] = []
        wrapped_callbacks = self._wrap_callbacks(callbacks, emitted_records)
        acquired = False
        scan_started_at: float | None = None

        try:
            wrapped_callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", "任务已排队，等待平台并发槽位", status="queued")
            )
            acquired = self._acquire_slot(task, cancel_event)
            preview = adapter.preview_target(task.target, task.options)
            normalized_target = preview.normalized_target
            self.storage.update_task_status(task.task_id, "running", normalized_target=normalized_target)
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    f"任务启动: {preview.title or preview.normalized_target}",
                    status="running",
                )
            )
            scan_started_at = time.time() - 2.0
            retries = max(0, min(int(task.options.get("retries", 0) or 0), 5))
            for attempt in range(retries + 1):
                try:
                    runtime_task = DownloadTask(
                        task.task_id, task.module_id, task.target, task.output_dir,
                        {**task.options, "_blocklist_path": str(self.blocklist.path)},
                    )
                    adapter.download(runtime_task, wrapped_callbacks, cancel_event)
                    break
                except TaskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if cancel_event.is_set():
                        raise TaskCancelled("任务已取消") from exc
                    if attempt >= retries:
                        raise
                    wrapped_callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "warning",
                            f"任务失败，将重试 {attempt + 1}/{retries}: {exc}",
                            status="running",
                        )
                    )
                    if cancel_event.wait(min(8.0, 1.5 * (2 ** attempt))):
                        raise TaskCancelled("任务已取消")
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")

            # Some compatibility adapters still rely on the manager's fallback scan instead
            # of emitting FileRecord events. Discover those files before common postprocessing
            # so archive behavior remains platform-independent.
            if not emitted_records:
                scan_root = adapter.fallback_scan_root(task)
                scanned_records = self.storage.scan_media_files(
                    scan_root,
                    task.module_id,
                    task.task_id,
                    modified_after=scan_started_at,
                )
                emitted_records.extend(scanned_records)
                for record in scanned_records:
                    callbacks.on_file(record)

            if emitted_records and (
                str(task.options.get("archive_mode") or "none").casefold() in {"task", "folder"}
                or bool(task.options.get("extract_archives", False))
            ):
                wrapped_callbacks.on_progress(
                    ProgressEvent(
                        task.task_id, task.module_id, "info", "下载完成，开始公共压缩/解压后处理",
                        status="running", metadata={"phase": "archive"},
                    )
                )

                def archive_progress(current: int, total: int, message: str) -> None:
                    wrapped_callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "info",
                            message,
                            status="running",
                            current=current,
                            total=total,
                            percent=(current / total * 100.0) if total else None,
                            metadata={"phase": "archive"},
                        )
                    )

                archive_result = run_archive_postprocess(
                    task,
                    preview.title or preview.normalized_target,
                    list(emitted_records),
                    cancel_event=cancel_event,
                    on_progress=archive_progress,
                )
                removed = {str(path.resolve()).casefold() for path in archive_result.removed_sources}
                if removed:
                    emitted_records[:] = [
                        record for record in emitted_records
                        if str(record.path.resolve()).casefold() not in removed
                    ]
                    for path in archive_result.removed_sources:
                        self.storage.delete_file_record(path)
                for record in archive_result.records:
                    if record.path.is_file():
                        wrapped_callbacks.on_file(record)
                wrapped_callbacks.on_progress(
                    ProgressEvent(
                        task.task_id,
                        task.module_id,
                        "info",
                        f"公共归档后处理完成：新增 {len(archive_result.records)} 个文件"
                        + (f"，校验后清理 {len(archive_result.removed_sources)} 个源文件" if removed else ""),
                        status="running",
                        metadata={"phase": "archive"},
                    )
                )

            records = emitted_records
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    f"任务完成，索引文件 {len(records)} 个",
                    status="completed",
                )
            )
        except TaskCancelled as exc:
            status = "cancelled"
            error = str(exc)
            wrapped_callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "warning", error, status=status)
            )
        except Exception as exc:  # noqa: BLE001
            status = "cancelled" if cancel_event.is_set() else "failed"
            error = "任务已取消" if cancel_event.is_set() else str(exc)
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "warning" if status == "cancelled" else "error",
                    error,
                    status=status,
                )
            )
        finally:
            if status != "completed" and scan_started_at is not None:
                try:
                    scan_root = adapter.fallback_scan_root(task)
                    partial_records = self.storage.scan_media_files(
                        scan_root,
                        task.module_id,
                        task.task_id,
                        modified_after=scan_started_at,
                    )
                    known_paths = {str(record.path).casefold() for record in emitted_records}
                    newly_indexed = []
                    for record in partial_records:
                        path_key = str(record.path).casefold()
                        if path_key in known_paths:
                            continue
                        known_paths.add(path_key)
                        emitted_records.append(record)
                        newly_indexed.append(record)
                        try:
                            callbacks.on_file(record)
                        except Exception as exc:  # noqa: BLE001
                            self.storage.add_log(
                                task.task_id, task.module_id, "warning",
                                f"部分文件已入库，但界面文件通知失败: {exc}",
                            )
                    if newly_indexed:
                        partial_message = (
                            f"任务未完整完成；已保留并登记本次下载的 {len(newly_indexed)} 个文件。"
                        )
                        error = f"{error}\n{partial_message}".strip()
                        try:
                            wrapped_callbacks.on_progress(
                                ProgressEvent(
                                    task.task_id,
                                    task.module_id,
                                    "warning",
                                    partial_message,
                                    status=status,
                                    metadata={"partial_files": len(newly_indexed)},
                                )
                            )
                        except Exception as exc:  # noqa: BLE001
                            self.storage.add_log(
                                task.task_id, task.module_id, "warning",
                                f"部分下载状态通知失败: {exc}",
                            )
                except Exception as exc:  # noqa: BLE001
                    self.storage.add_log(
                        task.task_id, task.module_id, "warning",
                        f"任务结束后扫描部分下载文件失败: {exc}",
                    )
            if acquired:
                self._release_slot(task)
            self.storage.update_task_status(task.task_id, status, error, normalized_target)
            with self._lock:
                self.running.pop(task.task_id, None)
            try:
                callbacks.on_done(task.task_id, status)
            except Exception as exc:  # noqa: BLE001
                # A UI callback must never keep a finished worker registered as
                # active or prevent its final database state from being saved.
                self.storage.add_log(task.task_id, task.module_id, "warning", f"任务结束回调失败: {exc}")

    def _acquire_slot(self, task: DownloadTask, cancel_event: threading.Event) -> bool:
        module_id = task.module_id
        group = str(task.options.get("_task_concurrency_group") or "single")
        total_limit = max(1, min(20, int(task.options.get("_task_concurrency_total") or 1)))
        group_limit = max(
            1,
            min(
                total_limit,
                int(
                    task.options.get(
                        f"_task_concurrency_{group}",
                        total_limit,
                    )
                    or 1
                ),
            ),
        )
        group_key = (module_id, group)
        while True:
            if cancel_event.is_set():
                raise TaskCancelled("任务在排队期间已取消")
            with self._slot_condition:
                module_active = self._active_by_module.get(module_id, 0)
                group_active = self._active_by_group.get(group_key, 0)
                if module_active < total_limit and group_active < group_limit:
                    self._active_by_module[module_id] = module_active + 1
                    self._active_by_group[group_key] = group_active + 1
                    return True
                self._slot_condition.wait(timeout=0.2)

    def _release_slot(self, task: DownloadTask) -> None:
        module_id = task.module_id
        group = str(task.options.get("_task_concurrency_group") or "single")
        group_key = (module_id, group)
        with self._slot_condition:
            module_active = self._active_by_module.get(module_id, 0) - 1
            group_active = self._active_by_group.get(group_key, 0) - 1
            if module_active > 0:
                self._active_by_module[module_id] = module_active
            else:
                self._active_by_module.pop(module_id, None)
            if group_active > 0:
                self._active_by_group[group_key] = group_active
            else:
                self._active_by_group.pop(group_key, None)
            self._slot_condition.notify_all()

    def _wrap_callbacks(self, callbacks: CallbackSet, emitted_records: list[FileRecord]) -> CallbackSet:
        def on_progress(event: ProgressEvent) -> None:
            self.storage.add_log(event.task_id, event.module_id, event.level, event.message, event.metadata)
            callbacks.on_progress(event)

        def on_file(record: FileRecord) -> None:
            self.storage.upsert_file(record)
            emitted_records.append(record)
            callbacks.on_file(record)

        return CallbackSet(on_progress=on_progress, on_file=on_file, on_done=callbacks.on_done)



