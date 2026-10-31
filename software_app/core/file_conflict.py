"""Task-owned media conflict decisions. Call while holding the output lock."""
from pathlib import Path
from contextlib import contextmanager
import os
import time

from .output_lock import output_paths_lock
from .task_control import task_cancelled, task_wait
from .adapter import TaskCancelled

CONFLICT_LABELS = {"复用已有文件": "reuse", "覆盖已有文件": "overwrite", "遇到重名时报错": "error",
                   "自动改名保留两者": "rename"}


def conflict_policy(value=None):
    value = value or "reuse"
    if value not in CONFLICT_LABELS.values():
        raise ValueError("无效的文件重名处理策略")
    return value


def reuse_existing(path: Path, policy=None) -> bool:
    policy = conflict_policy(policy)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FileExistsError(f"目标不是普通文件，未覆盖：{path.name}")
    if not path.exists() or not path.stat().st_size:
        return False
    if policy == "error":
        raise FileExistsError(f"目标文件已存在：{path.name}")
    if policy == "rename":
        raise FileExistsError(f"该写入入口未分配新文件名，未覆盖：{path.name}")
    return policy == "reuse"


@contextmanager
def media_output(path: Path, policy=None, cancel_event=None, *, related_paths=(), companions=None):
    """Select and lock the actual output until commit, without empty reservations.

    Every attempt locks requested and candidate paths in canonical order. Recheck
    on acquiring the locks so concurrent threads/processes cannot choose the same
    name. Related source files are protected by the same ordered group.
    """
    policy = conflict_policy(policy)
    path = Path(path)
    companions = companions or (lambda _path: ())
    for index in range(1, 10001):
        if cancel_event is not None and task_cancelled(cancel_event):
            raise TaskCancelled("分配输出文件名时任务已取消")
        candidate = path if index == 1 else path.with_name(f"{path.stem} ({index}){path.suffix}")
        outputs = (candidate, *companions(candidate))
        related = related_paths(candidate) if callable(related_paths) else related_paths
        with output_paths_lock((path, *companions(path), *outputs, *related), cancel_event):
            if policy == "rename" and any(item.exists() or item.is_symlink() for item in outputs):
                continue
            yield candidate
            return
    raise FileExistsError("自动改名超过 10000 个候选，请更换保存目录或文件名")


def replace_output(temporary: Path, destination: Path, cancel_event=None) -> None:
    """Commit a complete artifact, retrying brief Windows sharing violations."""
    for attempt in range(6):
        if cancel_event is not None and task_cancelled(cancel_event):
            raise TaskCancelled("提交输出文件时任务已取消")
        try:
            temporary.replace(destination)
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                raise
            delay = min(0.4, 0.05 * (2 ** attempt))
            if cancel_event is None:
                time.sleep(delay)
            elif task_wait(cancel_event, delay):
                raise TaskCancelled("提交输出文件时任务已取消") from exc
