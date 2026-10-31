"""A shared, live-configurable media HTTP budget for one application process."""
from __future__ import annotations

import math
import threading
import time
from collections import deque

from .adapter import TaskCancelled
from .task_control import TaskControl, task_cancelled


DEFAULT_SPEED_KIB = 1024
MAX_SPEED_KIB = 1048576


def validate_speed_limit(enabled, kib):
    try:
        value = int(str(kib))
    except (TypeError, ValueError) as exc:
        raise ValueError("总限速须为 1–1048576 KiB/s 的整数") from exc
    if not isinstance(enabled, bool) or not 1 <= value <= MAX_SPEED_KIB:
        raise ValueError("总限速须为 1–1048576 KiB/s 的整数，开关须为布尔值")
    return enabled, value


def saved_speed_limit(settings):
    try:
        return validate_speed_limit(settings.get("speed_limit_enabled", False),
                                    settings.get("speed_limit_kib", DEFAULT_SPEED_KIB))
    except ValueError:
        return False, DEFAULT_SPEED_KIB


class BandwidthLimiter:
    def __init__(self, *, clock=time.monotonic):
        self._condition = threading.Condition()
        self._clock = clock
        self._enabled = False
        self._rate = DEFAULT_SPEED_KIB * 1024
        self._tokens = 0.0
        self._last = clock()
        self._waiting = deque()

    @property
    def settings(self):
        with self._condition:
            return self._enabled, self._rate

    def configure(self, enabled: bool, bytes_per_second=None):
        with self._condition:
            rate = self._rate if bytes_per_second is None else float(bytes_per_second)
            if not isinstance(enabled, bool) or not math.isfinite(rate) or rate <= 0:
                raise ValueError("限速设置无效")
            if enabled != self._enabled or rate != self._rate:
                self._enabled, self._rate = enabled, rate
                self._tokens = 0.0
                self._last = self._clock()
                self._condition.notify_all()

    def chunk_size(self, requested):
        with self._condition:
            return min(requested, max(1024, int(self._rate * 0.1))) if self._enabled else requested

    def consume(self, size: int, cancel_event=None):
        remaining = size
        while remaining > 0:
            if cancel_event is not None and task_cancelled(cancel_event):
                raise TaskCancelled("限速等待期间任务已取消")
            ticket = object()
            with self._condition:
                if not self._enabled:
                    return
                self._waiting.append(ticket)
            try:
                with self._condition:
                    while self._enabled:
                        if cancel_event is not None and cancel_event.is_set():
                            raise TaskCancelled("限速等待期间任务已取消")
                        if isinstance(cancel_event, TaskControl) and cancel_event.paused:
                            # Leave the fair queue before blocking at a pause
                            # checkpoint, so other tasks can use this budget.
                            break
                        if self._waiting[0] is not ticket:
                            self._condition.wait(0.05)
                            continue
                        now = self._clock()
                        capacity = max(1.0, self._rate * 0.1)
                        self._tokens = min(capacity, self._tokens + max(0, now - self._last) * self._rate)
                        self._last = now
                        amount = min(remaining, int(self._tokens))
                        if amount:
                            self._tokens -= amount
                            remaining -= amount
                            break
                        desired = min(remaining, capacity)
                        delay = max(0.001, min(0.1, (desired - self._tokens) / self._rate))
                        self._condition.wait(delay)
                    else:
                        return
            finally:
                with self._condition:
                    self._waiting.remove(ticket)
                    self._condition.notify_all()


MEDIA_BANDWIDTH = BandwidthLimiter()


def apply_bandwidth_settings(settings):
    enabled, kib = saved_speed_limit(settings)
    MEDIA_BANDWIDTH.configure(enabled, kib * 1024)


def limited_chunks(response, cancel_event=None, *, chunk_size=128 * 1024):
    for chunk in response.iter_content(chunk_size=MEDIA_BANDWIDTH.chunk_size(chunk_size)):
        if chunk:
            MEDIA_BANDWIDTH.consume(len(chunk), cancel_event)
        yield chunk


def limited_content(response, cancel_event=None):
    return b"".join(limited_chunks(response, cancel_event))
