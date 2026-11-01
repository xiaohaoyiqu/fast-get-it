"""Kernel-owned output locks, shared by threads and application instances.

Lock files stay in an OS-resolved, per-user directory independent of TEMP,
the installation and the task database. Never unlink them on release:
another process may already be waiting on the same inode. The OS releases the
actual lock when a process exits, including abnormal termination.
"""
from __future__ import annotations

import errno
import hashlib
import os
import re
import time
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from functools import lru_cache

from .adapter import TaskCancelled
from .task_control import task_cancelled, task_wait


_owned = threading.local()


@lru_cache(maxsize=1)
def _lock_root() -> Path:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        # Resolve the actual user folder, not the launcher's LOCALAPPDATA/HOME.
        shell = ctypes.WinDLL("shell32", use_last_error=True)
        folder = shell.SHGetFolderPathW
        folder.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.HANDLE,
                           wintypes.DWORD, wintypes.LPWSTR]
        folder.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(32768)
        result = folder(None, 0x001C, None, 0, buffer)  # CSIDL_LOCAL_APPDATA
        if result != 0 or not buffer.value:
            raise OSError("无法确定当前用户的输出锁目录")
        base = Path(buffer.value)
    else:
        import pwd
        base = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".cache"
    # Never fall back to TEMP: that would silently split the lock namespace.
    return base / "yuqiuda" / "output-locks"


def _try_kernel_lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_kernel_lock(handle):
    handle.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _path_key(path: Path | str) -> str:
    return os.path.normcase(str(Path(path).resolve()))


@contextmanager
def output_path_lock(path: Path | str, cancel_event: threading.Event | None = None, *, timeout: float | None = None):
    key = hashlib.sha256(_path_key(path).encode("utf-8")).hexdigest()
    with output_key_lock(key, cancel_event, timeout=timeout):
        yield


@contextmanager
def output_key_lock(key: str, cancel_event: threading.Event | None = None, *, timeout: float | None = None):
    """Use a persisted destination digest without storing its original path."""
    if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("输出锁摘要无效")
    owned = getattr(_owned, "paths", None)
    if owned is None:
        owned = _owned.paths = set()
    if key in owned:
        yield
        return
    lock_root = _lock_root()
    lock_root.mkdir(parents=True, exist_ok=True)
    lock_path = lock_root / (key + ".lock")
    stop = cancel_event if cancel_event is not None else threading.Event()
    deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
    with lock_path.open("a+b") as handle:
        # Byte-range locks also cover an empty file. Initializing its contents
        # before acquisition races with a competing process locking byte zero.
        acquired = False
        try:
            while not acquired:
                if task_cancelled(stop):
                    raise TaskCancelled("等待输出文件时任务已取消")
                try:
                    _try_kernel_lock(handle)
                    acquired = True
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("文件正被其他任务使用，已跳过") from exc
                    task_wait(stop, 0.1)
            owned.add(key)
            yield
        finally:
            if acquired:
                owned.discard(key)
                _unlock_kernel_lock(handle)


@contextmanager
def output_paths_lock(paths, cancel_event: threading.Event | None = None, *, timeout: float | None = None):
    """Acquire a group in canonical order so overlapping media names cannot deadlock."""
    with ExitStack() as stack:
        for key in sorted({_path_key(path) for path in paths}):
            stack.enter_context(output_path_lock(key, cancel_event, timeout=timeout))
        yield
