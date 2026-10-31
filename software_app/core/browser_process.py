"""Own Windows browser descendants through a private kernel job, never PID scans."""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes

from selenium.webdriver.chrome.service import Service as ChromeService

from .error_policy import ErrorInfo


class BrowserProcessError(RuntimeError):
    _download_error_info = ErrorInfo("browser_process", "浏览器进程管理失败", False)


class _BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_int64),
        ("job_time", ctypes.c_int64),
        ("flags", wintypes.DWORD),
        ("minimum_working_set", ctypes.c_size_t),
        ("maximum_working_set", ctypes.c_size_t),
        ("active_limit", wintypes.DWORD),
        ("affinity", ctypes.c_size_t),
        ("priority", wintypes.DWORD),
        ("scheduling", wintypes.DWORD),
    ]


class _ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", _BasicLimits),
        ("io", ctypes.c_uint64 * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process_memory", ctypes.c_size_t),
        ("peak_job_memory", ctypes.c_size_t),
    ]


class _Accounting(ctypes.Structure):
    _fields_ = [
        ("user_time", ctypes.c_int64),
        ("kernel_time", ctypes.c_int64),
        ("period_user_time", ctypes.c_int64),
        ("period_kernel_time", ctypes.c_int64),
        ("page_faults", wintypes.DWORD),
        ("total", wintypes.DWORD),
        ("active", wintypes.DWORD),
        ("terminated", wintypes.DWORD),
    ]


def _kernel():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, arguments, result in (
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        ("SetInformationJobObject", [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        (
            "QueryInformationJobObject",
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p],
            wintypes.BOOL,
        ),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ):
        function = getattr(kernel, name)
        function.argtypes, function.restype = arguments, result
    return kernel


def _failure(action):
    return BrowserProcessError(
        f"{action}；未接管其他浏览器，请检查系统限制后手动重试（Windows {ctypes.get_last_error()}）"
    )


class WindowsBrowserJob:
    def __init__(self):
        self._kernel = _kernel()
        self._handle = self._kernel.CreateJobObjectW(None, None)
        self._attached = False
        if not self._handle:
            raise _failure("无法创建浏览器进程组")
        limits = _ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; no breakaway.
        if not self._kernel.SetInformationJobObject(self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            error = _failure("无法设置浏览器进程组")
            self._kernel.CloseHandle(self._handle)
            self._handle = None
            raise error

    def attach(self, process):
        # The retained Popen handle is the process identity. Never reopen a PID,
        # use a process-name filter, or discover browsers from a profile string.
        handle = getattr(process, "_handle", None)
        if not self._handle or self._attached or process.poll() is not None or handle is None:
            raise BrowserProcessError("浏览器驱动进程身份不可用，未启动浏览器会话")
        if not self._kernel.AssignProcessToJobObject(self._handle, int(handle)):
            raise _failure("无法绑定浏览器驱动进程组")
        self._attached = True

    def close(self):
        handle, self._handle = self._handle, None
        if not handle:
            return
        try:
            if self._attached:
                if not self._kernel.TerminateJobObject(handle, 1):
                    raise _failure("无法回收浏览器进程组")
                deadline = time.monotonic() + 3
                while True:
                    accounting = _Accounting()
                    if not self._kernel.QueryInformationJobObject(
                        handle, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None
                    ):
                        raise _failure("无法确认浏览器进程组退出")
                    if not accounting.active:
                        break
                    if time.monotonic() >= deadline:
                        raise BrowserProcessError("浏览器进程组退出未能在 3 秒内确认，请稍后检查临时资料")
                    time.sleep(0.05)
        finally:
            # Closing also terminates associated processes if the above fails;
            # the kernel closes this non-inherited handle on application death.
            if not self._kernel.CloseHandle(handle):
                raise _failure("无法关闭浏览器进程组句柄")


class ManagedChromeService(ChromeService):
    def __init__(self, *args, **kwargs):
        self._browser_job = None
        self._lifecycle_lock = threading.RLock()
        super().__init__(*args, **kwargs)

    def start(self):
        with self._lifecycle_lock:
            if self._browser_job is not None:
                raise BrowserProcessError("浏览器服务已启动，未重复创建会话")
            try:
                if os.name == "nt":
                    self._browser_job = WindowsBrowserJob()
                super().start()
                if self._browser_job is not None:
                    # Selenium creates Chrome only after Service.start returns.
                    self._browser_job.attach(self.process)
            except BaseException:
                try:
                    self.stop()
                except Exception:
                    pass
                raise

    def stop(self):
        with self._lifecycle_lock:
            try:
                super().stop()
            finally:
                job, self._browser_job = self._browser_job, None
                if job is not None:
                    job.close()
