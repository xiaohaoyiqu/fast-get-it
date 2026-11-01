"""Shared, conservative error feedback for all platform screens."""

from __future__ import annotations

import re

from software_app.core.error_policy import classify_error
from software_app.core.security import safe_error_message


def friendly_error(message: object, action: str) -> str:
    raw = safe_error_message(str(message or "")).strip()
    lowered = raw.lower()
    code = classify_error(message).code if isinstance(message, BaseException) else ""
    if code in {"disk_full", "file_permission", "file_busy", "file_error", "file_conflict"}:
        return f"{action}未完成：{classify_error(message).label}；请检查保存目录、可用空间和文件占用。"
    if code == "proxy_error" or any(
        marker in lowered for marker in ("proxyerror", "unable to connect to proxy", "proxy connection")
    ):
        return f"当前设置的代理无法连接，{action}未完成；请在“设置 → 代理 URL”中修改或清空代理后重试。"
    if any(marker in lowered for marker in ("no such window", "invalid session id", "target window already closed")):
        return f"浏览器已经关闭，{action}已结束。"
    if code == "tls_error" or any(marker in lowered for marker in ("sslerror", "certificate verify failed")):
        return f"{action}未完成：TLS 连接或证书校验失败；请检查系统时间、网络和代理。"
    if code == "timeout" or "timeout" in lowered or "超时" in raw:
        if "pixiv" in action.casefold():
            return (
                f"Pixiv 网络请求连接超时，{action}未完成；第 1 页或资料内容尚未返回。"
                "这不是软件按总时长定时关闭，请检查网络，或在“设置 → 代理 URL”中更换可用代理后重试。"
            )
        return f"{action}请求超时；请检查网络和代理，连接恢复后重试。网络超时无法判断登录是否有效。"
    if code == "network_error" or any(
        marker in lowered
        for marker in ("connectionerror", "connection refused", "name resolution", "getaddrinfo failed")
    ):
        return f"{action}未完成：网络连接失败；请检查网络、站点地址和代理。"
    if any(marker in lowered for marker in ("cloudflare", "captcha", "人机验证", "验证码", "验证页面")):
        return f"{action}遇到站点验证或访问限制；请在浏览器确认页面，并保持相同网络出口。"
    # Cookie filenames, format errors and conditional advice are not evidence
    # of an expired login. Preserve those details instead of guessing.
    if any(
        marker in lowered
        for marker in (
            "login required",
            "login_required",
            "login expired",
            "not logged in",
            "未登录",
            "登录已失效",
            "登录已过期",
            "需要登录",
            "请先登录",
            "跳转到登录",
        )
    ):
        return f"{action}需要有效登录；请更新对应平台 Cookie，并使用平台提供的验证入口检查。"
    if "未取得阅读器原图" in raw or "结构已变化" in raw:
        return f"{action}未完成：{raw.splitlines()[0]}"
    if code == "access_denied" or re.search(r"\b(?:401|403)\b", lowered):
        return f"{action}被站点拒绝访问；可能涉及账号权限、站点验证或网络出口，请核对具体页面。"
    first_line = raw.splitlines()[0] if raw else "暂时没有完成"
    if len(first_line) > 180:
        first_line = first_line[:177] + "…"
    return f"{action}暂未完成：{first_line}"
