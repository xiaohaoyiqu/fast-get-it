from __future__ import annotations

import json
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .adapter import CrawlerAdapter, TaskCancelled
from .collection_plan import collection_label
from .archive_service import run_archive_postprocess
from .blocklist import BlocklistStore, work_from_target
from .events import CallbackSet
from .models import DownloadTask, FileRecord, ProgressEvent, TargetPreview
from .security import redact_sensitive_data, safe_error_message
from .storage import AppStorage
from .task_control import TaskControl, task_cancelled, task_wait
from .instance_lease import InstanceLease
from .bandwidth import apply_bandwidth_settings
from .file_conflict import conflict_policy
from .error_policy import classify_error
from .network_policy import network_task_scope
from .browser_pool import apply_browser_settings, browser_task_scope
from .person_config import PersonConfigs
from .task_routing import retry_route_for_task


@dataclass
class RunningTask:
    task: DownloadTask
    thread: threading.Thread
    cancel_event: threading.Event
    adapter: CrawlerAdapter
    finishing: bool = False


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
        self._library_maintenance = False
        self._instance_lease = InstanceLease(storage.db_path, acquire=False)
        self._shutting_down = False
        apply_bandwidth_settings(storage.get_settings())
        apply_browser_settings(storage.get_settings())

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
        *,
        retry_of: str = "",
        collection_plan: dict | None = None,
    ) -> DownloadTask:
        adapter = self.get_adapter(module_id)
        if collection_plan is not None and not getattr(adapter, "supports_collection_plan", False):
            raise ValueError("此平台尚不支持保存集合发现计划")
        output_dir, task_options = self.prepare_task_settings(module_id, target, output_dir, options or {})
        task_options.pop("_collection_plan", None)
        self._protect_media_retry(adapter, task_options)
        task_options["file_conflict"] = conflict_policy(task_options.get("file_conflict") or self.storage.get_setting("file_conflict", "reuse"))
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

        cancel_event = TaskControl()
        thread = threading.Thread(
            target=self._run_task,
            args=(adapter, task, callbacks or CallbackSet(), cancel_event),
            name=f"download-{task.task_id}",
            daemon=True,
        )
        with self._lock:
            if self._shutting_down:
                raise ValueError("软件正在退出，不能创建任务")
            if self._library_maintenance:
                raise ValueError("下载库正在清理，请完成后再创建任务")
            self._instance_lease.acquire()
            created = False
            try:
                self.storage.create_task(task.task_id, module_id, target, str(task.output_dir), task.options,
                                         owner_id=self._instance_lease.owner_id, retry_of=retry_of,
                                         collection_plan=collection_plan)
                created = True
                self.running[task.task_id] = RunningTask(task, thread, cancel_event, adapter)
                thread.start()
            except Exception:
                self.running.pop(task.task_id, None)
                if created:
                    self.storage.update_task_status(task.task_id, "failed", "任务线程未能启动")
                if not self.running:
                    self._instance_lease.close()
                raise
        return task

    def prepare_task_settings(self, module_id, target, output_dir, options):
        directory, prepared = PersonConfigs(self.storage).prepare(module_id, target, output_dir, options)
        if module_id == "ehentai":
            from .aria2_bt import Aria2Limits, saved_aria2_limits, aria2_limits_from_options

            defaults = Aria2Limits() if options.get("_keep_task_settings") else saved_aria2_limits(self.storage.get_settings())
            prepared.update(aria2_limits_from_options({**defaults.options(), **prepared}).options())
        return directory, prepared

    @staticmethod
    def _protect_media_retry(adapter: CrawlerAdapter, options: dict[str, Any]) -> None:
        if "_retry_media" not in options:
            return
        media = options["_retry_media"]
        if (not getattr(adapter, "supports_media_retry", False) or not isinstance(media, list)
                or not media or len(media) > 10000 or any(not isinstance(item, dict) for item in media)):
            raise ValueError("平台不支持逐媒体补下，或补下参数格式/数量无效")
        options["_retry_media"] = json.loads(json.dumps(media, ensure_ascii=False))
        # Apply on both creation and recovery, before claiming a task. Persisted
        # legacy options must not archive a subset or delete original sources.
        options.update({"archive_mode": "none", "extract_archives": False, "archive_cleanup_sources": False})

    @contextmanager
    def library_maintenance(self):
        with self._lock:
            if self.running or self._library_maintenance:
                raise ValueError("请先结束下载任务（含暂停任务）及其他清理操作")
            self._library_maintenance = True
        try:
            yield
        finally:
            with self._lock:
                self._library_maintenance = False

    @staticmethod
    def _concurrency_group(module_id: str, target: str, options: dict[str, Any]) -> str:
        kind = str(options.get("input_kind") or "").strip().casefold()
        scope = str(options.get("content_scope") or options.get("search_mode") or "").strip().casefold()
        value = str(target or "").strip().casefold()
        if module_id == "pixiv":
            if kind in {"work", "novel"}:
                return "single"
            if (kind == "fanbox" and "/posts/" in value) or (kind == "sketch" and any(path in value for path in ("/artworks/", "/items/"))):
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
        failed_only: bool = False,
        media_keys: list[str] | None = None,
        use_current_settings: bool = False,
        before_start: Callable[[str, str, Path, dict], None] | None = None,
    ) -> DownloadTask:
        """Create a new task from a failed/cancelled task or a legacy empty discovery task."""
        row = self.storage.get_task(task_id)
        if row is None:
            raise KeyError(f"找不到任务: {task_id}")
        status = str(row["status"])
        try:
            options = json.loads(str(row["options_json"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("历史任务参数损坏，未重试；请重新配置目标") from None
        if not isinstance(options, dict):
            raise ValueError("历史任务参数不是有效配置，未重试；请重新配置目标")
        original_module, target = str(row["module_id"]), str(row["target"])
        if (use_current_settings and original_module == "pixiv"
                and (failed_only or "_retry_media" in options)
                and (plan := self.storage.collection_plan(task_id)) is not None):
            label = collection_label(plan["kind"])
            raise ValueError(f"{label}精确补下沿用原范围和设置；如需重新发现，请对原{label}任务按当前设置完整重试")
        binding = options.get("_settings_binding")
        if binding is not None and binding != {"module_id": original_module, "target": target}:
            raise ValueError("历史任务设置与目标不匹配，未重试")
        module_id, options = retry_route_for_task(original_module, target, options)
        empty_completed = (
            status == "completed"
            and allow_empty_completed
            and module_id == "website"
            and options.get("discovery_source") == "google_image"
            and not self.storage.list_files(task_id=task_id, limit=1)
        )
        if status not in {"partial", "failed", "cancelled"} and not empty_completed:
            raise ValueError(f"状态为 {status} 的任务不能重试")
        if media_keys is not None and not failed_only:
            raise ValueError("选择媒体只能用于补下未完成项")
        if use_current_settings and module_id == "ehentai":
            from .aria2_bt import saved_aria2_limits

            options.update(saved_aria2_limits(self.storage.get_settings()).options())
        if option_overrides:
            options.update(option_overrides)
        # Apply compatibility routing after overrides too, and keep the origin
        # task association even when its platform changes.
        module_id, options = retry_route_for_task(module_id, target, options)
        if use_current_settings:
            for name in ("_person_config", "_settings_binding", "_task_override", "_keep_task_settings"):
                options.pop(name, None)
        else:
            if module_id != original_module:
                options.pop("_person_config", None)
                options["_settings_binding"] = {"module_id": module_id, "target": target}
            options["_keep_task_settings"] = True
        options["_retry_of"] = task_id
        if failed_only:
            if not getattr(self.get_adapter(module_id), "supports_media_retry", False):
                raise ValueError("此平台尚未接入逐媒体补下，请使用批量重试")
            media = self.storage.media_results(task_id, unfinished=True)
            if media_keys is not None:
                keys = set(media_keys)
                if not keys or len(keys) > 10000:
                    raise ValueError("请选择 1–10000 项未完成媒体")
                media = [item for item in media if item["key"] in keys]
                if {item["key"] for item in media} != keys:
                    raise ValueError("所选媒体状态已变化，请刷新列表后重新选择")
            if not media or len(media) > 10000:
                raise ValueError("没有已记录的未完成媒体，或超过单次 10000 项上限")
            options["_retry_media"] = media
            options["_media_retry_of"] = task_id
            # A subset archive must never replace the original complete archive
            # or clean up sources needed by other parts of the original task.
            options.update({"archive_mode": "none", "extract_archives": False, "archive_cleanup_sources": False})
        # A retry is a new task; the original batch item already owns its task ID.
        options.pop("_batch_id", None)
        options.pop("_batch_position", None)
        output_dir, options = self.prepare_task_settings(module_id, target, Path(str(row["output_dir"])), options)
        if before_start is not None:
            before_start(module_id, target, output_dir, options)
        return self.start_task(
            module_id,
            target,
            output_dir,
            options,
            callbacks,
            retry_of=task_id,
            collection_plan=(self.storage.collection_plan(task_id)
                             if not use_current_settings else None),
        )

    def cancel(self, task_id: str) -> None:
        with self._lock:
            running = self.running.get(task_id)
        if not running:
            return
        if running.cancel_event.is_set() or not self.storage.request_task_cancel(task_id):
            return
        running.cancel_event.set()
        self.storage.add_log(task_id, running.task.module_id, "warning", "用户请求取消任务")
        self._refresh_batch_report(running.task)

        # Browser driver shutdown can itself block. Never run it on Tk's UI
        # thread; the shared event already lets cooperative adapters stop now.
        def stop_adapter() -> None:
            try:
                running.adapter.cancel(task_id)
            except Exception as exc:  # noqa: BLE001
                self.storage.add_log(task_id, running.task.module_id, "warning", f"停止适配器时返回: {exc}")

        threading.Thread(target=stop_adapter, name=f"cancel-{task_id}", daemon=True).start()

    def restore_interrupted_task(self, task_id: str, callbacks: CallbackSet | None = None) -> DownloadTask:
        row = self.storage.get_task(task_id)
        if row is None or not row["recovery_status"] or row["status"] != "cancelled":
            raise ValueError("该任务不是可恢复的中断任务")
        options = json.loads(row["options_json"])
        if not isinstance(options, dict):
            raise ValueError("中断任务参数损坏，未启动")
        if "_person_config" in options or "_settings_binding" in options:
            _, options = self.prepare_task_settings(row["module_id"], row["target"], row["output_dir"], options)
        adapter = self.get_adapter(row["module_id"])
        self._protect_media_retry(adapter, options)
        if self.blocklist.is_blocked(row["module_id"], row["target"],
                                     input_kind=str(options.get("input_kind") or ""),
                                     author_id=str(options.get("author_id") or "")):
            raise ValueError("该目标已在黑名单中，未恢复")
        task = DownloadTask(task_id, row["module_id"], row["target"], Path(row["output_dir"]), options)
        control = TaskControl()
        if row["recovery_status"] == "paused":
            control.pause()
        thread = threading.Thread(target=self._run_task,
                                  args=(adapter, task, callbacks or CallbackSet(), control),
                                  name=f"download-{task_id}", daemon=True)
        with self._lock:
            if self._shutting_down or self._library_maintenance:
                raise ValueError("软件正在退出或下载库正在维护，不能恢复任务")
            self._instance_lease.acquire()
            claimed = False
            try:
                claimed = self.storage.claim_interrupted_task(task_id, self._instance_lease.owner_id, options=options)
                if not claimed:
                    raise ValueError("任务已被其他实例恢复或已发生变化")
                self.running[task_id] = RunningTask(task, thread, control, adapter)
                thread.start()
            except Exception:
                self.running.pop(task_id, None)
                if claimed:
                    self.storage.update_task_status(task_id, "failed", "恢复任务的线程未能启动")
                if not self.running:
                    self._instance_lease.close()
                raise
        self._refresh_batch_report(task)
        return task

    def pause(self, task_id: str) -> bool:
        with self._lock:
            running = self.running.get(task_id)
            if (not running or running.finishing or not isinstance(running.cancel_event, TaskControl)
                    or running.cancel_event.is_set()):
                return False
            if not self.storage.transition_active_task(task_id, "paused", ("queued", "running")):
                return False
            running.cancel_event.pause()
        self.storage.add_log(task_id, running.task.module_id, "info", "已请求暂停；在安全检查点停下")
        self._refresh_batch_report(running.task)
        return True

    def resume(self, task_id: str) -> bool:
        with self._lock:
            running = self.running.get(task_id)
            if (not running or running.finishing or not isinstance(running.cancel_event, TaskControl)
                    or running.cancel_event.is_set()):
                return False
            row = self.storage.get_task(task_id)
            if row is None:
                return False
            status = "running" if row["started_at"] else "queued"
            if not self.storage.transition_active_task(task_id, status, ("paused",)):
                return False
            running.cancel_event.resume()
        self.storage.add_log(task_id, running.task.module_id, "info", "任务已恢复")
        with self._slot_condition:
            self._slot_condition.notify_all()
        self._refresh_batch_report(running.task)
        return True

    def _refresh_batch_report(self, task: DownloadTask) -> None:
        if not task.options.get("_batch_id"):
            return
        from .mixed_batch import write_batch_report

        try:
            write_batch_report(self.storage, task.options["_batch_id"])
        except Exception as exc:
            self.storage.add_log(task.task_id, task.module_id, "warning", "批次汇总更新失败：" + safe_error_message(exc))

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
        with self._lock:
            self._shutting_down = True
            self.storage.mark_shutdown_tasks(self._instance_lease.owner_id)
        task_ids = self.active_task_ids()
        for task_id in task_ids:
            self.cancel(task_id)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for task_id in task_ids:
            self.wait(task_id, max(0.0, deadline - time.monotonic()))
        unfinished = set(self.active_task_ids())
        if not unfinished:
            self._instance_lease.close()
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
        def publish_wait(message, metadata):
            with self._lock:
                running = self.running.get(task.task_id)
                if running is None or running.finishing or cancel_event.is_set():
                    return
                if isinstance(cancel_event, TaskControl) and cancel_event.paused:
                    return
                row = self.storage.get_task(task.task_id)
                if row is None or row["status"] != "running":
                    return
                event = ProgressEvent(task.task_id, task.module_id, "info", message,
                                      metadata=metadata)
                self.storage.add_log(task.task_id, task.module_id, "info", message, event.metadata)
            callbacks.on_progress(event)

        def on_wait(host, seconds):
            endpoint = f"{host[0]}:{host[1]}"
            message = (f"等待站点 {endpoint} 解除限流或暂时不可用，剩余约 {max(1, int(seconds + 0.999))} 秒"
                       if seconds > 0 else f"站点 {endpoint} 等待结束，继续请求")
            publish_wait(message, {"phase": "network_wait", "wait_seconds": seconds})

        def on_browser_wait(active, limit, waiting, admitted=False):
            message = "已取得浏览器会话名额，正在启动浏览器" if admitted else f"等待浏览器会话：使用 {active}/{limit}，等待 {waiting} 项"
            publish_wait(message, {"phase": "browser_start" if admitted else "browser_wait"})

        def on_memory_wait(used, recovery, remaining):
            from .resource_pressure import memory_wait_message

            publish_wait(memory_wait_message(used, recovery, remaining), {
                "phase": "browser_memory_wait", "memory_percent": used,
                "memory_recovery_percent": recovery, "wait_seconds": remaining,
            })

        with network_task_scope(cancel_event, on_wait), browser_task_scope(on_browser_wait, on_memory_wait):
            self._execute_task(adapter, task, callbacks, cancel_event)

    def _execute_task(
        self, adapter: CrawlerAdapter, task: DownloadTask,
        callbacks: CallbackSet, cancel_event: threading.Event,
    ) -> None:
        status = "completed"
        error = ""
        normalized_target = ""
        emitted_records: list[FileRecord] = []
        wrapped_callbacks = self._wrap_callbacks(callbacks, emitted_records, str(task.options.get("_media_retry_of") or ""))
        acquired = False
        scan_started_at: float | None = None

        try:
            wrapped_callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", "任务已排队，等待平台并发槽位", status="queued")
            )
            acquired = self._acquire_slot(task, cancel_event)
            # The task owns a platform slot now, even if target discovery takes
            # a while. Publish the transition before preview_target performs
            # network/browser work so active concurrent tasks do not look queued.
            while True:
                if task_cancelled(cancel_event):
                    raise TaskCancelled("任务已取消")
                with self._lock:
                    if isinstance(cancel_event, TaskControl) and cancel_event.paused:
                        continue
                    if not self.storage.transition_active_task(task.task_id, "running", ("queued",)):
                        raise TaskCancelled("任务在启动前已停止")
                    break
            self._refresh_batch_report(task)
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    "任务已启动，正在读取目标资料",
                    status="running",
                    metadata={"phase": "prepare"},
                )
            )
            collection_plan = self.storage.collection_plan(task.task_id)
            preview = (TargetPreview(task.module_id, task.target, task.target, title="沿用已保存的下载范围")
                       if "_retry_media" in task.options or collection_plan is not None
                       else adapter.preview_target(task.target, task.options))
            normalized_target = preview.normalized_target
            self.storage.update_task_normalized_target(task.task_id, normalized_target)
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
                    if task_cancelled(cancel_event):
                        raise TaskCancelled("任务已取消")
                    runtime_options = {**task.options, "_blocklist_path": str(self.blocklist.path)}
                    runtime_options.pop("_collection_plan", None)
                    collection_plan = self.storage.collection_plan(task.task_id)
                    if collection_plan is not None:
                        if not getattr(adapter, "supports_collection_plan", False):
                            raise ValueError("当前平台不支持此集合发现计划，未重新选择其他作品")
                        runtime_options["_collection_plan"] = collection_plan
                    if getattr(adapter, "supports_media_retry", False) and self.storage.media_summary(task.task_id):
                        runtime_options["_retry_media"] = self.storage.media_results(task.task_id, unfinished=True)
                    runtime_task = DownloadTask(
                        task.task_id, task.module_id, task.target, task.output_dir,
                        runtime_options,
                    )
                    adapter.download(runtime_task, wrapped_callbacks, cancel_event)
                    break
                except TaskCancelled:
                    raise
                except Exception as exc:  # noqa: BLE001
                    if cancel_event.is_set():
                        raise TaskCancelled("任务已取消") from exc
                    error_info = classify_error(exc)
                    if attempt >= retries or not error_info.retryable:
                        raise
                    wrapped_callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "warning",
                            f"[{error_info.label}] 将重试 {attempt + 1}/{retries}: {safe_error_message(exc)}",
                            status="running",
                            metadata={"error_code": error_info.code, "retryable": error_info.retryable},
                        )
                    )
                    if task_wait(cancel_event, min(8.0, 1.5 * (2 ** attempt))):
                        raise TaskCancelled("任务已取消") from exc
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")

            # Some compatibility adapters still rely on the manager's fallback scan instead
            # of emitting FileRecord events. Discover those files before common postprocessing
            # so archive behavior remains platform-independent.
            if self.storage.media_summary(task.task_id):
                known = {str(record.path.resolve()).casefold() for record in emitted_records}
                for record in self.storage.task_file_records(task.task_id):
                    if task_cancelled(cancel_event):
                        raise TaskCancelled("任务已取消")
                    if not record.path.is_file():
                        raise FileNotFoundError(f"此前登记的成品已缺失，未执行不完整归档：{record.path}")
                    if str(record.path.resolve()).casefold() not in known:
                        known.add(str(record.path.resolve()).casefold())
                        emitted_records.append(record)
                # Index ownership can move when another task reuses a path.
                # Keep the durable media path so that a lost index cannot
                # silently omit a source or archive another task's replacement.
                root = task.output_dir.resolve()
                for item in self.storage.media_results(task.task_id):
                    if task_cancelled(cancel_event):
                        raise TaskCancelled("任务已取消")
                    if item["state"] not in {"saved", "reused"} or not item.get("path"):
                        continue
                    path = Path(item["path"]).resolve()
                    if root not in path.parents:
                        raise ValueError("媒体成品记录超出原任务输出目录")
                    if not path.is_file():
                        raise FileNotFoundError(f"此前登记的成品已缺失，未执行不完整归档：{path}")
                    if str(path).casefold() not in known:
                        raise ValueError(f"此前成品的索引已清理或归属已变化，请检查后重新创建完整任务：{path}")
            if not emitted_records and not getattr(adapter, "reports_files", False):
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

            media_counts = self.storage.media_summary(task.task_id)
            incomplete_media = media_counts.get("failed", 0) + media_counts.get("pending", 0)
            if incomplete_media:
                wrapped_callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "warning",
                    "存在未完成媒体，本次保留源文件并跳过公共归档后处理"))
            if emitted_records and not incomplete_media and (
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
                for warning in archive_result.warnings:
                    wrapped_callbacks.on_progress(ProgressEvent(
                        task.task_id, task.module_id, "warning", warning,
                        status="running", metadata={"phase": "archive"},
                    ))
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

            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            records = emitted_records
            summary = self.storage.media_summary(task.task_id)
            if summary:
                result_text = (f"已保存 {summary.get('saved', 0)}，复用 {summary.get('reused', 0)}，"
                               f"失败 {summary.get('failed', 0)}，待处理 {summary.get('pending', 0)}")
                if summary.get("failed", 0) or summary.get("pending", 0):
                    status = "partial"
                    error = result_text
                else:
                    result_text = "任务完成：" + result_text
            else:
                result_text = f"任务完成，索引文件 {len(records)} 个"
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "warning" if status == "partial" else "info",
                    result_text,
                    status=status,
                    metadata={"media_summary": summary},
                    current=sum(summary.get(key, 0) for key in ("saved", "reused", "repaired")) if summary else None,
                    total=sum(summary.values()) if summary else None,
                )
            )
        except TaskCancelled as exc:
            status = "cancelled"
            error = safe_error_message(exc)
            wrapped_callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "warning", error, status=status)
            )
        except Exception as exc:  # noqa: BLE001
            status = "cancelled" if cancel_event.is_set() else "failed"
            error_info = classify_error(exc)
            error = "任务已取消" if cancel_event.is_set() else f"[{error_info.label}] {safe_error_message(exc)}"
            wrapped_callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "warning" if status == "cancelled" else "error",
                    error,
                    status=status,
                    metadata={"error_code": error_info.code, "retryable": error_info.retryable},
                )
            )
        finally:
            with self._lock:
                running = self.running.get(task.task_id)
                if running:
                    running.finishing = True
            if isinstance(cancel_event, TaskControl):
                cancel_event.resume()
            if status not in {"completed", "partial"} and scan_started_at is not None:
                try:
                    scan_root = adapter.fallback_scan_root(task)
                    # A media plan already identifies this task's work. A broad
                    # directory scan could steal recent outputs from the parent
                    # task after a filtered or failed subset refresh.
                    partial_records = [] if self.storage.media_summary(task.task_id) else self.storage.scan_media_files(
                        scan_root, task.module_id, task.task_id, modified_after=scan_started_at,
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
            with self._lock:
                self.storage.update_task_status(task.task_id, status, error, normalized_target)
            try:
                self._refresh_batch_report(task)
            finally:
                with self._lock:
                    self.running.pop(task.task_id, None)
                    if not self.running:
                        self._instance_lease.close()
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
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务在排队期间已取消")
            with self._slot_condition:
                if isinstance(cancel_event, TaskControl) and cancel_event.paused:
                    continue
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

    def _wrap_callbacks(self, callbacks: CallbackSet, emitted_records: list[FileRecord], media_retry_of: str = "") -> CallbackSet:
        def on_progress(event: ProgressEvent) -> None:
            metadata = event.metadata or {}
            result = metadata.get("media_result")
            committed = result and result.get("state") in {"saved", "reused"}
            if committed:
                # A cancellation arriving after file commit must not turn that
                # completed item back into pending work (especially with rename).
                self.storage.record_media_result(event.task_id, result)
                if media_retry_of:
                    self.storage.resolve_media_result(media_retry_of, result["key"], event.task_id)
            if event.status not in {"completed", "partial", "failed", "cancelled"}:
                with self._lock:
                    running = self.running.get(event.task_id)
                    control = running.cancel_event if running and not running.finishing else None
                if control is not None and task_cancelled(control):
                    raise TaskCancelled("任务已取消")
            if result and not committed:
                self.storage.record_media_result(event.task_id, result)
            if "media_plan" in metadata:
                self.storage.record_media_results(event.task_id, metadata["media_plan"])
            if "collection_plan" in metadata:
                self.storage.record_collection_plan(event.task_id, metadata["collection_plan"])
            # Persist recovery data above before creating a separate display copy.
            # Never redact the adapter's original event or runtime parameters.
            display_event = replace(
                event, message=safe_error_message(event.message), metadata=redact_sensitive_data(metadata),
            )
            log_metadata = {key: value for key, value in display_event.metadata.items() if key not in {"media_plan", "collection_plan"}}
            self.storage.add_log(event.task_id, event.module_id, event.level, display_event.message, log_metadata)
            callbacks.on_progress(display_event)

        def on_file(record: FileRecord) -> None:
            self.storage.upsert_file(record)
            emitted_records.append(record)
            callbacks.on_file(record)

        return CallbackSet(on_progress=on_progress, on_file=on_file, on_done=callbacks.on_done)



