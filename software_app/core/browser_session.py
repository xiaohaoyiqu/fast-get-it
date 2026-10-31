"""Recognize lost WebDriver sessions without treating page errors as crashes."""

from __future__ import annotations

from selenium.common.exceptions import InvalidSessionIdException, NoSuchWindowException, WebDriverException
from urllib3.exceptions import HTTPError

from .adapter import TaskCancelled
from .error_policy import ErrorInfo
from .task_control import task_cancelled


class BrowserSessionLost(RuntimeError):
    _download_error_info = ErrorInfo("browser_session_lost", "浏览器会话中断", False)


def is_browser_session_lost(error, driver=None):
    current, seen = error, set()
    while current is not None and id(current) not in seen and len(seen) < 12:
        seen.add(id(current))
        if isinstance(current, (BrowserSessionLost, InvalidSessionIdException, NoSuchWindowException)):
            return True
        if isinstance(current, WebDriverException):
            # ChromeDriver exposes some fatal protocol errors only as "unknown
            # error". Match the protocol message prefix, never arbitrary URLs.
            message = str(current.msg or "").strip().lower()
            if message.startswith(
                (
                    "disconnected:",
                    "chrome not reachable",
                    "tab crashed",
                    "unknown error: chrome not reachable",
                    "unknown error: tab crashed",
                    "invalid session id",
                    "no such window:",
                )
            ):
                return True
        if isinstance(current, (WebDriverException, HTTPError)) and driver is not None:
            process = getattr(getattr(driver, "service", None), "process", None)
            if process is not None:
                code = process.poll()
                if isinstance(code, int):
                    return True
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return False


def raise_browser_failure(error, cancel_event=None, driver=None):
    if cancel_event is not None and task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消") from error
    if is_browser_session_lost(error, driver):
        if isinstance(error, BrowserSessionLost):
            raise error
        raise BrowserSessionLost("浏览器会话已中断（窗口关闭或进程异常退出）；请检查浏览器后手动重试") from error
