from __future__ import annotations

import json
import uuid
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.browser_cookie_capture import (
    CookieCaptureResult,
    CookieJsonConversion,
    convert_header_capture_to_cookie_json,
    cookie_capture_spec,
    save_captured_cookies,
)
from software_app.core.jmcomic_plain_browser import (
    _chrome_user_agent,
)
from software_app.crawlers.common import load_cookie_file, load_request_header_profile, request_get
from software_app.core.cookie_store import cookie_jar, load_cookie_records
from software_app.core.error_policy import classify_error
from software_app.core.response_read import read_response_text
from software_app.crawlers.ehentai import EhentaiClient


EH_PLATFORM_LABELS = {"E-Hentai 表站", "ExHentai 里站"}


def eh_validation_failure(error, *, proxy_url=""):
    """Describe validation failures without echoing URLs, credentials or response text."""
    info = classify_error(error)
    route = "通过已填写的代理" if str(proxy_url or "").strip() else "直连（代理 URL 为空，不使用系统代理）"
    hints = {
        "timeout": "连接或读取超时，尚不能确认 Cookie 是否有效；请检查网络和代理。",
        "proxy_error": "代理连接失败；请检查代理软件是否运行、地址和端口是否正确。",
        "network_error": "网络连接失败；请检查网络和代理。",
        "tls_error": "TLS 连接或证书验证失败；请检查代理与系统时间。",
        "access_denied": "登录或访问被拒绝；请确认 Cookie、账号权限及浏览器与软件的代理出口。",
        "invalid_data": "Cookie、返回页面或配置格式无效；请检查本机登录资料。",
    }
    status = getattr(getattr(error, "response", None), "status_code", None)
    detail = hints.get(info.code, info.label + "；请检查连接和本机登录资料。")
    if isinstance(status, int):
        detail = f"HTTP {status}：{detail}"
    return f"本次{route}；{detail} 已保存的 Cookie 保留。"


def _eh_spec(platform_label: str):
    label = str(platform_label or "").strip()
    if label not in EH_PLATFORM_LABELS:
        raise ValueError("EH Cookie 只支持 E-Hentai 表站或 ExHentai 里站")
    return cookie_capture_spec(label)


def _browser_identity_path(spec) -> Path:
    name = "exhentai-browser.json" if spec.key == "exhentai" else "e-hentai-browser.json"
    return Path(spec.destination).parent / name


def _save_browser_identity(spec, user_agent: str) -> None:
    value = str(user_agent or "").strip()
    if not value or "\r" in value or "\n" in value or len(value) > 512:
        return
    destination = _browser_identity_path(spec)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps({"user_agent": value}, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_eh_session(
    platform_label: str,
    cookies: list[dict],
    *,
    proxy_url: str = "",
    user_agent: str = "",
) -> None:
    spec = _eh_spec(platform_label)
    site = "exhentai" if spec.key == "exhentai" else "e-hentai"
    client = EhentaiClient(proxy_url=proxy_url, site=site)
    if user_agent:
        client.session.headers["User-Agent"] = user_agent
    domain = ".exhentai.org" if site == "exhentai" else ".e-hentai.org"
    client.session.cookies = cookie_jar(cookies, default_domain=domain)
    url = "https://exhentai.org/" if site == "exhentai" else "https://e-hentai.org/uconfig.php"
    try:
        response = request_get(client.session, url, timeout=(10, 45))
        try:
            _check_eh_login_page(spec, site, response)
        finally:
            response.close()
    finally:
        client.session.close()


def _check_eh_login_page(spec, site, response):
    final = urlparse(str(response.url or ""))
    host = str(final.hostname or "").casefold()
    path = final.path.casefold()
    page = read_response_text(response)
    lowered = page[:300000].casefold()
    login_markers = (
        "this page requires you to log on",
        "act=login",
        'name="ips_username"',
        "name='ips_username'",
    )
    if "bounce_login" in path or any(marker in lowered for marker in login_markers):
        raise PermissionError(f"{spec.label} 登录状态无效；站点仍返回登录页")
    if site == "exhentai":
        if host not in {"exhentai.org", "www.exhentai.org"} or not page.strip():
            raise PermissionError("ExHentai 页面为空或被重定向；当前账号、Cookie 或网络出口没有里站权限")
    elif host not in {"e-hentai.org", "www.e-hentai.org"} or "uconfig.php" not in path:
        raise PermissionError("E-Hentai 表站设置页验证失败；请确认浏览器中已经登录")


def import_ehentai_cookie_file(
    platform_label: str,
    source: Path | str,
    *,
    proxy_url: str = "",
    validate_online: bool = False,
) -> CookieCaptureResult:
    """Save a manual export locally; perform a network check only when requested."""
    spec = _eh_spec(platform_label)
    values = load_cookie_file(source)
    if not values:
        raise ValueError(
            "Cookie 文件为空或格式无效；支持 JSON、Netscape cookies.txt、Cookie 请求头，"
            "以及 DevTools 中名称和值分行的请求标头"
        )
    missing = sorted(spec.required_names - {name for name, value in values.items() if value})
    if missing:
        raise PermissionError(f"{spec.label} 缺少必要 Cookie：{', '.join(missing)}；旧 Cookie 未被覆盖")
    request_headers = load_request_header_profile(source)
    captured_user_agent = next(
        (value for name, value in request_headers.items() if str(name).casefold() == "user-agent"), ""
    )
    user_agent = str(captured_user_agent or "").strip() or _chrome_user_agent()
    domain = ".exhentai.org" if spec.key == "exhentai" else ".e-hentai.org"
    records = load_cookie_records(source, strict=True)
    for record in records:
        if not record["domain"]:
            record.update(domain=domain, hostOnly=False, secure=True)
    if validate_online:
        _validate_eh_session(platform_label, records, proxy_url=proxy_url, user_agent=user_agent)
    result = save_captured_cookies(spec, records)
    _save_browser_identity(spec, user_agent)
    return CookieCaptureResult(
        platform=result.platform,
        cookie_count=result.cookie_count,
        cookie_names=result.cookie_names,
        destination=result.destination,
        user_agent=user_agent,
        current_host="exhentai.org" if spec.key == "exhentai" else "e-hentai.org",
    )


def save_ehentai_manual_cookies(platform_label, values):
    """Save explicitly entered fields without opening a browser or making requests."""
    spec = _eh_spec(platform_label)
    records = [{"name": name, "value": str(values.get(name) or "").strip()} for name in sorted(spec.required_names)]
    return save_captured_cookies(spec, records)


def validate_saved_ehentai_cookies(platform_label, *, proxy_url=""):
    spec = _eh_spec(platform_label)
    records = load_cookie_records(spec.destination, strict=True)
    names = {row["name"] for row in records if row["value"]}
    if not spec.required_names.issubset(names):
        raise ValueError("请先填写或导入完整 EH Cookie")
    identity = _browser_identity_path(spec)
    user_agent = ""
    if identity.is_file():
        user_agent = str(json.loads(identity.read_text(encoding="utf-8")).get("user_agent") or "")
    _validate_eh_session(platform_label, records, proxy_url=proxy_url, user_agent=user_agent)


def convert_eh_header_capture_to_json(
    platform_label: str,
    source: Path | str,
    destination: Path | str | None = None,
) -> CookieJsonConversion:
    """Convert a mixed DevTools request/response header capture to cookie JSON."""
    return convert_header_capture_to_cookie_json(_eh_spec(platform_label), source, destination)
