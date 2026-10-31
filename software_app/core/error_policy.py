"""Structured retry decisions; never infer error types from message text."""
from __future__ import annotations

import errno
from dataclasses import dataclass

import requests

from .adapter import TaskCancelled


@dataclass(frozen=True)
class ErrorInfo:
    code: str
    label: str
    retryable: bool


class InvalidMediaError(ValueError):
    """Malformed image data is distinct from a local disk failure."""
    _download_error_info = ErrorInfo("invalid_media", "媒体内容无效", False)


def http_error_info(status: int) -> ErrorInfo:
    if status == 429:
        return ErrorInfo("rate_limited", "站点限流", True)
    if status in {401, 403}:
        return ErrorInfo("access_denied", "登录或访问被拒绝", False)
    if status in {404, 410}:
        return ErrorInfo("not_found", "资源不存在", False)
    if status in {408, 425} or 500 <= status < 600:
        return ErrorInfo("server_temporary", "站点暂时不可用", True)
    return ErrorInfo("http_permanent", "HTTP 请求被拒绝", False)


def classify_error(error: BaseException) -> ErrorInfo:
    current, seen = error, set()
    while current is not None and id(current) not in seen and len(seen) < 12:
        seen.add(id(current))
        if isinstance(current, TaskCancelled):
            return ErrorInfo("cancelled", "任务已取消", False)
        if isinstance(current, FileExistsError):
            return ErrorInfo("file_conflict", "文件重名", False)
        if isinstance(current, OSError) and getattr(current, "winerror", None) in {32, 33}:
            return ErrorInfo("file_busy", "文件被占用", False)
        if isinstance(current, OSError) and current.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
            return ErrorInfo("disk_full", "磁盘空间或配额不足", False)
        if isinstance(current, OSError) and current.errno in {errno.EACCES, errno.EPERM, errno.EROFS}:
            return ErrorInfo("file_permission", "文件访问权限不足", False)
        if isinstance(current, ConnectionError):
            return ErrorInfo("network_error", "网络连接失败", True)
        if (isinstance(current, OSError) and not isinstance(current, (requests.RequestException, TimeoutError))
                and current.errno is not None):
            return ErrorInfo("file_error", "本地文件操作失败", False)
        if isinstance(current, requests.exceptions.SSLError):
            return ErrorInfo("tls_error", "TLS 连接或证书错误", False)
        tagged = getattr(current, "_download_error_info", None)
        if isinstance(tagged, ErrorInfo):
            return tagged
        response = getattr(current, "response", None)
        status = getattr(response, "status_code", None)
        if isinstance(status, int) and 400 <= status < 600:
            return http_error_info(status)
        if isinstance(current, requests.exceptions.ProxyError):
            return ErrorInfo("proxy_error", "代理连接失败", True)
        if isinstance(current, (requests.exceptions.Timeout, TimeoutError)):
            return ErrorInfo("timeout", "网络请求超时", True)
        if isinstance(current, (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                                requests.exceptions.MissingSchema, requests.exceptions.TooManyRedirects)):
            return ErrorInfo("request_invalid", "请求地址或跳转无效", False)
        if isinstance(current, requests.RequestException):
            return ErrorInfo("network_error", "网络传输失败", True)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    if isinstance(error, PermissionError):
        return ErrorInfo("access_denied", "访问权限不足", False)
    if isinstance(error, OSError):
        return ErrorInfo("file_error", "本地文件操作失败", False)
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return ErrorInfo("invalid_data", "参数或返回数据无效", False)
    # Preserve finite task-level retries for unclassified adapter/plugin exceptions.
    return ErrorInfo("unknown", "未分类错误", True)
