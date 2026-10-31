"""Optional Windows physical-memory admission checks for new browser sessions."""

from __future__ import annotations

import ctypes
import math
import sys
import threading
import time
from dataclasses import dataclass

from .error_policy import ErrorInfo


class BrowserMemoryError(RuntimeError):
    _download_error_info = ErrorInfo("browser_memory", "浏览器内存保护", False)


@dataclass(frozen=True)
class MemoryPolicy:
    enabled: bool = False
    high: int = 85
    recovery: int = 75
    timeout: int = 120

    def settings(self):
        return {
            "browser_memory_enabled": self.enabled,
            "browser_memory_high": self.high,
            "browser_memory_recovery": self.recovery,
            "browser_memory_timeout": self.timeout,
        }


def validate_memory_policy(enabled, high=85, recovery=75, timeout=120):
    if not isinstance(enabled, bool):
        raise ValueError("浏览器内存保护开关必须为布尔值")
    values = []
    for value, label, minimum, maximum in (
        (high, "内存暂停阈值", 50, 99),
        (recovery, "内存恢复阈值", 10, 98),
        (timeout, "内存等待上限（秒）", 10, 3600),
    ):
        try:
            if isinstance(value, bool):
                raise ValueError
            number = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{label}必须为 {minimum}–{maximum} 的整数") from exc
        if not minimum <= number <= maximum:
            raise ValueError(f"{label}必须为 {minimum}–{maximum} 的整数")
        values.append(number)
    if values[1] >= values[0]:
        raise ValueError("内存恢复阈值必须低于暂停阈值")
    return MemoryPolicy(enabled, *values)


def saved_memory_policy(settings):
    defaults = MemoryPolicy().settings()
    try:
        return validate_memory_policy(*(settings.get(key, value) for key, value in defaults.items()))
    except ValueError:
        return MemoryPolicy()


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("length", ctypes.c_uint32),
        ("load", ctypes.c_uint32),
        ("total_physical", ctypes.c_uint64),
        ("available_physical", ctypes.c_uint64),
        ("total_page_file", ctypes.c_uint64),
        ("available_page_file", ctypes.c_uint64),
        ("total_virtual", ctypes.c_uint64),
        ("available_virtual", ctypes.c_uint64),
        ("available_extended_virtual", ctypes.c_uint64),
    ]


def system_memory_percent():
    if sys.platform != "win32":
        raise BrowserMemoryError("浏览器内存保护目前只支持 Windows；请在设置中关闭此选项")
    status = _MemoryStatusEx()
    status.length = ctypes.sizeof(status)
    function = ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx
    function.argtypes = [ctypes.POINTER(_MemoryStatusEx)]
    function.restype = ctypes.c_int
    if not function(ctypes.byref(status)) or not status.total_physical:
        raise BrowserMemoryError("无法读取系统物理内存状态；请稍后重试或关闭浏览器内存保护")
    return (1.0 - status.available_physical / status.total_physical) * 100.0


@dataclass(frozen=True)
class MemoryStatus:
    policy: MemoryPolicy
    used: float | None = None
    blocked: bool = False


class MemoryPressure:
    def __init__(self, *, sample=system_memory_percent, clock=time.monotonic):
        self._sample = sample
        self._clock = clock
        self._lock = threading.Lock()
        self._status = MemoryStatus(MemoryPolicy())
        self._next_sample = 0.0

    def configure(self, policy):
        with self._lock:
            if policy != self._status.policy:
                self._status = MemoryStatus(policy)
                self._next_sample = 0.0

    def check(self):
        # Only this short sampler is serialized; no pool lock or callback is held.
        with self._lock:
            previous = self._status
            if not previous.policy.enabled:
                return previous
            now = self._clock()
            if previous.used is not None and now < self._next_sample:
                return previous
            used = float(self._sample())
            if not math.isfinite(used) or not 0 <= used <= 100:
                raise BrowserMemoryError("系统返回了无效的内存占用率，未启动新浏览器")
            blocked = used > previous.policy.recovery if previous.blocked else used >= previous.policy.high
            self._status = MemoryStatus(previous.policy, used, blocked)
            self._next_sample = now + 1.0
            return self._status


def memory_wait_message(used, recovery, remaining):
    return f"等待系统内存回落：当前 {used:.1f}%，降至 {recovery}% 后继续；最多再等 {max(0, math.ceil(remaining))} 秒"
