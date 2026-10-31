"""Cancellable media engines with continuously drained, bounded diagnostics."""

from __future__ import annotations

import os
import subprocess
import threading
import time

from .adapter import TaskCancelled
from .task_control import task_cancelled, task_wait


STDERR_LIMIT = 64 * 1024


def _stop(process):
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    else:
        process.wait()


def run_media_process(command, cancel_event, *, env=None, timeout=None):
    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    tail = bytearray()
    read_errors = []

    def drain():
        try:
            with process.stderr as stream:
                while chunk := stream.read(8192):
                    tail.extend(chunk)
                    if len(tail) > STDERR_LIMIT:
                        del tail[:-STDERR_LIMIT]
        except OSError as exc:
            read_errors.append(exc)

    reader = threading.Thread(target=drain, name="media-stderr", daemon=True)
    started = time.monotonic()
    reader_started = False
    try:
        reader.start()
        reader_started = True
        while process.poll() is None:
            if read_errors:
                raise RuntimeError("读取外部引擎诊断失败") from read_errors[0]
            if timeout is not None and time.monotonic() - started >= timeout:
                raise TimeoutError("外部媒体引擎运行超时")
            if task_wait(cancel_event, 0.1):
                raise TaskCancelled("任务已取消")
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
    finally:
        try:
            _stop(process)
        finally:
            if reader_started:
                reader.join(timeout=3)
            else:
                process.stderr.close()
    if reader.is_alive():
        raise RuntimeError("外部引擎已退出，但诊断管道仍被占用")
    if read_errors:
        raise RuntimeError("读取外部引擎诊断失败") from read_errors[0]
    return subprocess.CompletedProcess(command, process.returncode, stderr=tail.decode("utf-8", errors="replace"))
