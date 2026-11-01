"""Durable media identities and cooperative, per-item result reporting."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import requests

from .adapter import TaskCancelled
from .error_policy import classify_error
from .models import FileRecord, ProgressEvent
from .security import safe_error_message
from .task_control import task_cancelled


@dataclass(frozen=True)
class MediaGroupResult:
    primary: FileRecord
    companions: tuple[FileRecord, ...] = ()


def _notify(callback, value):
    try:
        callback(value)
    except Exception as exc:  # noqa: BLE001
        # Notification failures are not an invitation to try browser rendering.
        exc._media_transfer_failure = True
        raise


def media_identity(url: str, kind: str) -> str:
    # Signed queries and CDN hosts can change. A changed path is deliberately
    # treated as a changed resource, rather than downloading a different item.
    return hashlib.sha256(f"{kind}\n{urlsplit(url).path}".encode("utf-8")).hexdigest()


def media_descriptor(task, target, index, kind, url, folder):
    relative = Path(folder).resolve().relative_to(task.output_dir.resolve())
    identity = media_identity(url, kind)
    key = hashlib.sha256(f"{target}\n{index}\n{identity}".encode("utf-8")).hexdigest()
    return {
        "key": key,
        "target": target,
        "index": index,
        "kind": kind,
        "identity": identity,
        "folder": relative.as_posix(),
    }


def retry_folder(task, descriptor):
    root = task.output_dir.resolve()
    relative = Path(descriptor["folder"])
    folder = (root / relative).resolve()
    if relative.is_absolute() or folder == root or root not in folder.parents:
        raise ValueError("失败媒体的保存目录不在原任务目录中")
    return folder


def refreshed_media(media, descriptor):
    index = int(descriptor["index"])
    if not 1 <= index <= len(media):
        raise ValueError("原媒体序号已不存在，请检查作品后重新创建任务")
    item = media[index - 1]
    if media_identity(str(item.get("url") or ""), str(item.get("type") or "")) != descriptor["identity"]:
        raise ValueError("原媒体已变化，未下载其他资源；请检查作品后重新创建任务")
    return item


def run_media_jobs(task, callbacks, cancel_event, jobs):
    """Register all discovered work before transfer, so cancellation retains it."""
    unique = {}
    for item, download in jobs:
        unique.setdefault(item["key"], (item, download))
    jobs = list(unique.values())
    if len(jobs) > 10000:
        raise ValueError("单任务逐媒体计划最多 10000 项，请缩小任务范围")

    def report(item, state, error="", path=None):
        result = {**item, "state": state, "error": error}
        if path is not None:
            result["path"] = str(Path(path).resolve())
        _notify(
            callbacks.on_progress,
            ProgressEvent(
                task.task_id,
                task.module_id,
                "warning" if error else "info",
                f"媒体 {item['index']}："
                + {"pending": "等待下载", "saved": "已保存", "reused": "已复用", "failed": "失败"}[state]
                + (f" · {error}" if error else ""),
                metadata={"media_result": result},
            ),
        )

    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    if jobs:
        _notify(
            callbacks.on_progress,
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"已登记 {len(jobs)} 项待下载媒体",
                metadata={"media_plan": [{**item, "state": "pending"} for item, _ in jobs]},
            ),
        )
    completed, errors = 0, []
    for item, download in jobs:
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        try:
            output = download()
            record = output.primary if isinstance(output, MediaGroupResult) else output
        except TaskCancelled:
            raise
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            errors.append(exc)
            error_info = classify_error(exc)
            report(item, "failed", f"[{error_info.label}] {safe_error_message(exc)}")
            if error_info.code in {"disk_full", "file_permission", "file_error"}:
                exc._media_transfer_failure = True
                raise
            continue
        try:
            if isinstance(output, MediaGroupResult):
                for companion in output.companions:
                    _notify(callbacks.on_file, companion)
            _notify(callbacks.on_file, record)
        finally:
            # The transfer already committed its file. A UI/index notification
            # failure must not turn it into new download work on the next retry.
            report(item, "reused" if record.metadata.get("reused") else "saved", path=record.path)
        completed += 1
        _notify(
            callbacks.on_progress,
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"已保存或复用 {completed}/{len(jobs)}：{record.path.name}",
                current=completed,
                total=len(jobs),
            ),
        )
    if errors and not completed:
        errors[0]._media_transfer_failure = True
        raise errors[0]
