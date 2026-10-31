"""Preview and remove only reviewed, idle, owned HTTP/HLS scratch files."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import stat

from .adapter import TaskCancelled
from .output_lock import output_key_lock
from .transfer_workspace import _linked, is_transfer_workspace_name


MAX_FOLDERS = 1000
MAX_FILES = 100000
MAX_DIRECTORIES = 100000
_HTTP_FILE = re.compile(r"(?:payload\.part|resume\.json|\.resume\.json\.[0-9a-f]{32}\.tmp)")
_HLS_FILE = re.compile(
    r"(?:r[0-9]{6}\.(?:ts|m4s|mp4|aac|mp3)(?:\.part|\.json)?|state\.json|local\.m3u8|output\.mp4|\.hls-[0-9a-f]{32}\.tmp)"
)
_ACTIVE = "SELECT 1 FROM tasks WHERE status IN ('queued','running','paused','cancelling') LIMIT 1"


def _checkpoint(event):
    if event is not None and event.is_set():
        raise TaskCancelled("缓存操作已停止")


def _plain_ancestors(path):
    for parent in (path, *path.parents):
        if _linked(parent):
            raise ValueError("缓存路径或上级目录是链接，已跳过")


def _signature(path, *, directory=False):
    if _linked(path):
        raise ValueError("缓存包含链接，已跳过")
    info = path.lstat()
    if directory:
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("缓存路径不是目录")
        return info.st_dev, info.st_ino, getattr(info, "st_birthtime_ns", info.st_ctime_ns)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError("缓存包含非普通文件或硬链接，已跳过")
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _snapshot(folder, event=None):
    _checkpoint(event)
    _plain_ancestors(folder)
    directory = _signature(folder, directory=True)
    kind, key = folder.name.split("-")[1:]
    pattern = _HTTP_FILE if kind == "http" else _HLS_FILE
    marker = folder / "owner.json"
    _signature(marker)
    with marker.open("rb") as stream:
        raw = stream.read(4097)
    if len(raw) > 4096 or json.loads(raw) != {"version": 1, "destination_key": key}:
        raise ValueError("缓存归属记录无效，已跳过")
    entries = []
    for child in folder.iterdir():
        _checkpoint(event)
        if len(entries) >= MAX_FILES:
            raise ValueError("缓存文件过多，请缩小范围")
        if child.name != "owner.json" and not pattern.fullmatch(child.name):
            raise ValueError("目录包含未知内容，已整项跳过")
        entries.append((child.name, _signature(child)))
    return directory, tuple(sorted(entries))


@dataclass(frozen=True)
class TransferCacheEntry:
    path: Path
    kind: str
    key: str
    directory: tuple = ()
    files: tuple = ()
    error: str = ""

    @property
    def size(self):
        return sum(signature[2] for _name, signature in self.files)


@dataclass(frozen=True)
class TransferCachePlan:
    root: Path
    entries: tuple[TransferCacheEntry, ...]


def preview_transfer_caches(root, *, cancel_event=None):
    root = Path(os.path.abspath(Path(root).expanduser()))
    _plain_ancestors(root)
    if not root.is_dir() or is_transfer_workspace_name(root.name):
        raise ValueError("请选择下载根目录，不要选择缓存目录本身")
    entries, stack, visited, files = [], [root], 0, 0
    while stack:
        _checkpoint(cancel_event)
        current = stack.pop()
        _plain_ancestors(current)
        visited += 1
        if visited > MAX_DIRECTORIES:
            raise ValueError("扫描目录过多，请缩小范围")
        with os.scandir(current) as children:
            for child in children:
                _checkpoint(cancel_event)
                path = Path(child.path)
                if is_transfer_workspace_name(child.name):
                    if len(entries) >= MAX_FOLDERS:
                        raise ValueError("缓存目录超过 1000 项，请缩小范围")
                    kind, key = child.name.lower().split("-")[1:]
                    try:
                        # A preview must not wait behind a paused/running transfer.
                        with output_key_lock(key, cancel_event, timeout=0):
                            directory, content = _snapshot(path, cancel_event)
                        files += len(content)
                        entries.append(TransferCacheEntry(path, kind, key, directory, content))
                    except TaskCancelled:
                        raise
                    except (OSError, ValueError, RecursionError) as exc:
                        entries.append(TransferCacheEntry(path, kind, key, error=str(exc)))
                    if files > MAX_FILES:
                        raise ValueError("扫描缓存文件过多，请缩小范围")
                elif not child.is_symlink() and not _linked(path) and child.is_dir(follow_symlinks=False):
                    stack.append(path)
    return TransferCachePlan(root, tuple(sorted(entries, key=lambda item: str(item.path))))


@dataclass
class TransferCleanupResult:
    removed_folders: int = 0
    removed_files: int = 0
    removed_bytes: int = 0
    skipped: int = 0
    cancelled: bool = False
    errors: list[str] = field(default_factory=list)


def execute_transfer_cleanup(manager, plan, selected, *, cancel_event=None):
    selected = tuple(dict.fromkeys(Path(path) for path in selected))
    reviewed = {entry.path: entry for entry in plan.entries}
    if not selected or any(path not in reviewed for path in selected):
        raise ValueError("请选择本次预览中的缓存目录")
    result = TransferCleanupResult()
    with manager.library_maintenance():
        for path in selected:
            try:
                _checkpoint(cancel_event)
                entry = reviewed[path]
                if entry.error or plan.root not in path.parents:
                    raise ValueError("缓存未通过预览检查，请重新预览")
                with output_key_lock(entry.key, cancel_event, timeout=0), manager.storage.connect() as conn:
                    # Prevent task creation in the shared database during each
                    # folder's deletion. Other databases use the same output lock.
                    conn.execute("BEGIN IMMEDIATE")
                    if conn.execute(_ACTIVE).fetchone():
                        result.errors.append("共享数据库有未结束任务，已停止清理")
                        break
                    if _snapshot(path, cancel_event) != (entry.directory, entry.files):
                        raise ValueError("缓存已变化，请重新预览")
                    # Never recurse. owner.json is retained until all reviewed
                    # files have gone, so cancellation/IO errors remain recoverable.
                    for name, signature in entry.files:
                        if name == "owner.json":
                            continue
                        _checkpoint(cancel_event)
                        _plain_ancestors(path)
                        target = path / name
                        if _signature(target) != signature:
                            raise ValueError("缓存文件已变化，停止清理本目录")
                        target.unlink()
                        result.removed_files += 1
                        result.removed_bytes += signature[2]
                    marker = path / "owner.json"
                    _checkpoint(cancel_event)
                    if set(item.name for item in path.iterdir()) != {"owner.json"}:
                        raise ValueError("缓存出现新内容，已保留目录")
                    marker_signature = dict(entry.files)["owner.json"]
                    if _signature(marker) != marker_signature:
                        raise ValueError("缓存归属已变化，已保留目录")
                    marker.unlink()
                    result.removed_files += 1
                    result.removed_bytes += marker_signature[2]
                    path.rmdir()
                    result.removed_folders += 1
            except TaskCancelled:
                result.cancelled = True
                break
            except (OSError, ValueError, RecursionError) as exc:
                result.skipped += 1
                result.errors.append(f"{path}: {exc}")
    return result
