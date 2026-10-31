"""Process-local admission for managed download/search browser sessions."""

from __future__ import annotations

import threading
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar

from .adapter import TaskCancelled
from .task_control import TaskControl, task_cancelled
from .resource_pressure import BrowserMemoryError, MemoryPressure, saved_memory_policy


_WAIT_CALLBACK = ContextVar("browser_wait_callback", default=None)
_MEMORY_WAIT_CALLBACK = ContextVar("browser_memory_wait_callback", default=None)


def browser_limit(value):
    if isinstance(value, bool):
        raise ValueError("浏览器会话上限必须为 1–10 的整数")
    try:
        text = str(value).strip()
        result = int(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("浏览器会话上限必须为 1–10 的整数") from exc
    if not 1 <= result <= 10:
        raise ValueError("浏览器会话上限必须为 1–10 的整数")
    return result


class BrowserLease:
    def __init__(self, pool):
        self._pool = pool
        self._released = False

    def release(self):
        with self._pool._lock:
            if not self._released:
                self._released = True
                self._pool._active -= 1


class BrowserPool:
    def __init__(self, limit=3):
        self._limit = browser_limit(limit)
        self._active = 0
        self._waiting = deque()
        self._lock = threading.Lock()
        self._memory = MemoryPressure()
        self._generation = 0

    def configure(self, limit, *, memory_policy=None):
        value = browser_limit(limit)
        with self._lock:
            self._limit = value
            if memory_policy is not None:
                self._memory.configure(memory_policy)
            self._generation += 1

    def snapshot(self):
        with self._lock:
            return self._active, self._limit, len(self._waiting)

    def acquire(self, cancel_event=None, on_wait=None, *, on_memory_wait=None):
        stop = cancel_event if cancel_event is not None else threading.Event()
        report = on_wait if on_wait is not None else _WAIT_CALLBACK.get()
        report_memory = on_memory_wait if on_memory_wait is not None else _MEMORY_WAIT_CALLBACK.get()
        ticket, queued = object(), False
        next_notice = 0.0
        notified = False
        memory_notified = False
        next_memory_notice = 0.0
        memory_wait = 0.0
        previous_memory = None
        last_tick = time.monotonic()
        try:
            while True:
                tick = time.monotonic()
                if previous_memory is not None and previous_memory.blocked:
                    memory_wait += tick - last_tick
                # Paused waiters must not hold up runnable tickets behind them.
                # Checkpoints may block, so always call them outside the lock.
                if isinstance(stop, TaskControl) and stop.paused and queued:
                    with self._lock:
                        self._waiting.remove(ticket)
                    queued = False
                if task_cancelled(stop):
                    raise TaskCancelled("等待浏览器会话期间任务已取消")
                # A pause checkpoint can block; its duration is not wait budget.
                last_tick = time.monotonic()
                with self._lock:
                    generation = self._generation
                memory = self._memory.check()
                if previous_memory is None or previous_memory.policy != memory.policy or not memory.blocked:
                    memory_wait = 0.0
                previous_memory = memory
                lease = None
                with self._lock:
                    if generation != self._generation:
                        continue
                    if memory.blocked and memory_wait >= memory.policy.timeout:
                        raise BrowserMemoryError(
                            f"系统内存持续高于恢复阈值 {memory.policy.recovery}%，"
                            f"已等待 {memory.policy.timeout} 秒，未启动新浏览器；可释放内存后重试"
                        )
                    if not queued:
                        self._waiting.append(ticket)
                        queued = True
                    paused = isinstance(stop, TaskControl) and stop.paused
                    if (
                        not paused
                        and not stop.is_set()
                        and not memory.blocked
                        and self._waiting[0] is ticket
                        and self._active < self._limit
                    ):
                        self._waiting.popleft()
                        queued = False
                        self._active += 1
                        lease = BrowserLease(self)
                    state = self._active, self._limit, len(self._waiting)
                if lease is not None:
                    try:
                        if (notified or memory_notified) and report is not None:
                            report(*state, True)
                    except BaseException:
                        lease.release()
                        raise
                    return lease
                if memory.blocked and report_memory is not None and time.monotonic() >= next_memory_notice:
                    report_memory(memory.used, memory.policy.recovery, memory.policy.timeout - memory_wait)
                    memory_notified = True
                    next_memory_notice = time.monotonic() + 5.0
                if not memory.blocked and report is not None and time.monotonic() >= next_notice:
                    report(*state, False)
                    notified = True
                    next_notice = time.monotonic() + 5.0
                # Event.wait deliberately does not pause: the next iteration
                # removes a paused ticket before entering its pause checkpoint.
                stop.wait(0.1)
        finally:
            if queued:
                with self._lock:
                    self._waiting.remove(ticket)

    @contextmanager
    def session(self, cancel_event=None, on_wait=None, *, on_memory_wait=None):
        lease = self.acquire(cancel_event, on_wait, on_memory_wait=on_memory_wait)
        try:
            yield
        finally:
            lease.release()


BROWSER_POOL = BrowserPool()


def apply_browser_settings(settings):
    try:
        limit = browser_limit(settings.get("browser_session_limit", 3))
    except ValueError:
        limit = 3
    BROWSER_POOL.configure(limit, memory_policy=saved_memory_policy(settings))
    return limit


@contextmanager
def browser_task_scope(on_wait, on_memory_wait=None):
    token = _WAIT_CALLBACK.set(on_wait)
    memory_token = _MEMORY_WAIT_CALLBACK.set(on_memory_wait)
    try:
        yield
    finally:
        _WAIT_CALLBACK.reset(token)
        _MEMORY_WAIT_CALLBACK.reset(memory_token)
