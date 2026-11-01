"""Bounded, process-local cooldowns shared by cooperating HTTP request paths."""
from __future__ import annotations

import math
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from contextlib import contextmanager
from contextvars import ContextVar

from .adapter import TaskCancelled
from .task_control import task_cancelled, task_wait


_REQUEST_CANCEL = ContextVar("http_request_cancel", default=None)
_WAIT_PROGRESS = ContextVar("http_wait_progress", default=None)


def request_checkpoint(cancel_event=None):
    event = cancel_event if cancel_event is not None else _REQUEST_CANCEL.get()
    if event is not None and task_cancelled(event):
        raise TaskCancelled("请求已取消")


@contextmanager
def network_task_scope(cancel_event, on_wait=None):
    """Bind cancellation to this worker only, including its metadata helpers."""
    token = _REQUEST_CANCEL.set(cancel_event)
    progress_token = _WAIT_PROGRESS.set(on_wait)
    try:
        yield
    finally:
        _WAIT_PROGRESS.reset(progress_token)
        _REQUEST_CANCEL.reset(token)


def retry_after_seconds(value, fallback: float, *, now=None, maximum=threading.TIMEOUT_MAX) -> float:
    fallback = float(fallback)
    fallback = fallback if math.isfinite(fallback) else 1.0
    try:
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError("nonfinite Retry-After")
    except (TypeError, ValueError, OverflowError):
        try:
            when = parsedate_to_datetime(str(value))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
        except (TypeError, ValueError, OverflowError):
            seconds = fallback
    # Bound representability, never silently shorten a server's deadline.
    if seconds > maximum:
        raise ValueError("Retry-After 超过系统支持的等待范围，未提前重试")
    return max(0.0, seconds)


def _host_key(url):
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").casefold()
        return host, parts.port or (443 if parts.scheme == "https" else 80)
    except (ValueError, TypeError, UnicodeError):
        return None


class DomainBackoff:
    def __init__(self, *, clock=time.monotonic, max_entries=1024):
        self._clock = clock
        self._max_entries = max(1, int(max_entries))
        self._lock = threading.Lock()
        self._deadlines = {}

    def defer(self, url, seconds) -> None:
        key = _host_key(url)
        try:
            delay = float(seconds)
        except (TypeError, ValueError, OverflowError):
            return
        if key is None or not math.isfinite(delay) or delay <= 0:
            return
        if delay > threading.TIMEOUT_MAX:
            raise ValueError("Retry-After 超过系统支持的等待范围，未提前重试")
        now = self._clock()
        with self._lock:
            self._deadlines = {host: end for host, end in self._deadlines.items() if end > now}
            if key not in self._deadlines and len(self._deadlines) >= self._max_entries:
                # Keep existing deadlines; the new caller still has its local retry wait.
                return
            self._deadlines[key] = max(self._deadlines.get(key, now), now + delay)

    def remaining(self, url) -> float:
        key = _host_key(url)
        with self._lock:
            remaining = self._deadlines.get(key, 0.0) - self._clock()
            if remaining <= 0:
                self._deadlines.pop(key, None)
            return max(0.0, remaining)

    def wait(self, url, cancel_event=None) -> None:
        if cancel_event is None:
            cancel_event = _REQUEST_CANCEL.get()
        on_wait = _WAIT_PROGRESS.get()
        next_notice = 0.0
        notified = False
        host = _host_key(url)
        while True:
            if cancel_event is not None and task_cancelled(cancel_event):
                raise TaskCancelled("等待同站点退避期间任务已取消")
            delay = self.remaining(url)
            if delay <= 0:
                if notified:
                    on_wait(host, 0.0)
                return
            now = self._clock()
            if on_wait is not None and host is not None and now >= next_notice:
                on_wait(host, delay)
                notified = True
                next_notice = now + 5.0
            # Check pause/cancel without holding the coordinator lock. Re-read
            # the deadline after each slice, including after another task extends it.
            if cancel_event is None:
                time.sleep(min(delay, 0.1))
            elif task_wait(cancel_event, min(delay, 0.1)):
                raise TaskCancelled("等待同站点退避期间任务已取消")

    def clear(self) -> None:
        with self._lock:
            self._deadlines.clear()


HTTP_BACKOFF = DomainBackoff()


def record_http_backoff(response, url: str) -> None:
    """Publish server cooldowns before raising, including on the last attempt."""
    if response.status_code in {429, 503}:
        delay = retry_after_seconds(response.headers.get("Retry-After"), 1.0)
        HTTP_BACKOFF.defer(url, delay)
        final_url = getattr(response, "url", None)
        if isinstance(final_url, str):
            HTTP_BACKOFF.defer(final_url, delay)
