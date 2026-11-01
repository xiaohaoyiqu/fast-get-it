"""Cooperative pause checkpoints without changing threading.Event semantics."""
from __future__ import annotations

import threading
import time


class TaskControl(threading.Event):
    """Cancellation remains nonblocking; only explicit worker checkpoints pause."""
    def __init__(self):
        super().__init__()
        self._pause_condition = threading.Condition()
        self._paused = False

    @property
    def paused(self) -> bool:
        with self._pause_condition:
            return self._paused

    def pause(self) -> None:
        with self._pause_condition:
            if not self.is_set():
                self._paused = True
                self._pause_condition.notify_all()

    def resume(self) -> None:
        with self._pause_condition:
            self._paused = False
            self._pause_condition.notify_all()

    def set(self) -> None:
        with self._pause_condition:
            super().set()
            self._pause_condition.notify_all()

    def checkpoint(self) -> bool:
        with self._pause_condition:
            while self._paused and not self.is_set():
                self._pause_condition.wait()
            return self.is_set()

    def wait_active(self, timeout: float | None = None) -> bool:
        """An interruptible delay whose budget excludes time spent paused."""
        remaining = None if timeout is None else max(0.0, timeout)
        with self._pause_condition:
            while not self.is_set():
                if self._paused:
                    self._pause_condition.wait()
                    continue
                if remaining is not None and remaining <= 0:
                    return False
                started = time.monotonic()
                self._pause_condition.wait(remaining)
                if remaining is not None:
                    remaining -= time.monotonic() - started
            return True


def task_cancelled(event: threading.Event) -> bool:
    if isinstance(event, TaskControl):
        return event.checkpoint()
    return event.is_set()


def task_wait(event: threading.Event, timeout: float | None = None) -> bool:
    if isinstance(event, TaskControl):
        return event.wait_active(timeout)
    return event.wait(timeout)
