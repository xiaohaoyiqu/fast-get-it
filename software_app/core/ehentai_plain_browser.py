from __future__ import annotations

import platform
import json
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.browser_cookie_capture import (
    CookieCaptureResult,
    CookieJsonConversion,
    convert_header_capture_to_cookie_json,
    cookie_capture_spec,
    save_captured_cookies,
)
from software_app.core.browser_driver import find_chrome_executable
from software_app.core.jmcomic_plain_browser import (
    _chrome_user_agent,
    _enable_session_restore,
    _host_matches,
    read_chrome_profile_cookies,
)
from software_app.core.settings import MANUAL_BROWSER_PROFILE_ROOT, ensure_data_dirs
from software_app.crawlers.common import load_cookie_file, load_request_header_profile
from software_app.crawlers.ehentai import EhentaiClient


EH_MANUAL_PROFILE_ROOT = Path(MANUAL_BROWSER_PROFILE_ROOT)
EH_PLATFORM_LABELS = {"E-Hentai 表站", "ExHentai 里站"}


@dataclass(frozen=True)
class EhPlainBrowserStatus:
    platform: str
    profile_dir: Path
    start_urls: tuple[str, ...]


def _eh_spec(platform_label: str):
    label = str(platform_label or "").strip()
    if label not in EH_PLATFORM_LABELS:
        raise ValueError("EH 普通 Chrome 只支持 E-Hentai 表站或 ExHentai 里站")
    return cookie_capture_spec(label)


def eh_profile_dir(platform_label: str, profile_root: Path = EH_MANUAL_PROFILE_ROOT) -> Path:
    spec = _eh_spec(platform_label)
    return Path(profile_root) / spec.key


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


def open_ehentai_plain_chrome(
    platform_label: str,
    *,
    proxy_url: str = "",
    profile_root: Path = EH_MANUAL_PROFILE_ROOT,
) -> EhPlainBrowserStatus:
    """Open a persistent normal Chrome without WebDriver or remote debugging."""
    spec = _eh_spec(platform_label)
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("未找到 Google Chrome")
    ensure_data_dirs()
    profile = eh_profile_dir(platform_label, profile_root)
    profile.mkdir(parents=True, exist_ok=True)
    _enable_session_restore(profile)
    command = [
        str(chrome),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--restore-last-session",
        "--window-size=1180,820",
    ]
    proxy = str(proxy_url or "").strip()
    if proxy:
        endpoint = proxy if "://" in proxy else f"http://{proxy}"
        command.append(f"--proxy-server={endpoint}")
    # ExHentai normally cannot set ``igneous`` until the forum/table login is
    # complete.  Opening the inner site in parallel races that login and often
    # leaves only an empty pre-login tab, so the inner flow is deliberately
    # split into two user-driven steps.
    start_urls = (spec.login_url,) if spec.key == "exhentai" else ("https://e-hentai.org/uconfig.php",)
    command.extend(start_urls)
    subprocess.Popen(command)
    return EhPlainBrowserStatus(spec.label, profile, start_urls)


def open_exhentai_after_login(
    *,
    proxy_url: str = "",
    profile_root: Path = EH_MANUAL_PROFILE_ROOT,
) -> EhPlainBrowserStatus:
    """Open ExHentai in the already logged-in inner profile as step two."""
    spec = _eh_spec("ExHentai 里站")
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("未找到 Google Chrome")
    ensure_data_dirs()
    profile = eh_profile_dir(spec.label, profile_root)
    if not profile.exists():
        raise RuntimeError("尚未建立 ExHentai 登录资料；请先点击“打开普通 Chrome 登录”")
    command = [
        str(chrome),
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://exhentai.org/",
    ]
    proxy = str(proxy_url or "").strip()
    if proxy:
        endpoint = proxy if "://" in proxy else f"http://{proxy}"
        command.insert(-1, f"--proxy-server={endpoint}")
    subprocess.Popen(command)
    return EhPlainBrowserStatus(spec.label, profile, ("https://exhentai.org/",))


def _site_cookie_records(records: list[dict], *, inner: bool) -> list[dict]:
    allowed = {"e-hentai.org", "exhentai.org"} if inner else {"e-hentai.org"}
    filtered = [
        item for item in records
        if isinstance(item, dict)
        and item.get("name")
        and item.get("value")
        and any(_host_matches(str(item.get("domain") or ""), host) for host in allowed)
    ]
    # save_captured_cookies stores a name/value map.  For an inner account,
    # make a same-named exhentai.org cookie win over a forum/table-domain copy.
    return sorted(
        filtered,
        key=lambda item: 1 if _host_matches(str(item.get("domain") or ""), "exhentai.org") else 0,
    )


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
    values: dict[str, str] = {}
    for item in cookies:
        name = str(item.get("name") or "")
        value = str(item.get("value") or "")
        if name and value:
            values[name] = value
    for name, value in values.items():
        client.session.cookies.set(name, value, domain=domain, path="/", secure=True)
    url = "https://exhentai.org/" if site == "exhentai" else "https://e-hentai.org/uconfig.php"
    response = client.session.get(url, timeout=(10, 45), allow_redirects=True)
    response.raise_for_status()
    final = urlparse(str(response.url or ""))
    host = str(final.hostname or "").casefold()
    path = final.path.casefold()
    page = str(response.text or "")
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
        raise PermissionError("E-Hentai 表站设置页验证失败；请确认普通 Chrome 中已经登录")


def capture_ehentai_plain_chrome(
    platform_label: str,
    *,
    profile_root: Path = EH_MANUAL_PROFILE_ROOT,
    proxy_url: str = "",
    wait_seconds: float = 15.0,
) -> CookieCaptureResult:
    """Read and validate a closed EH manual profile before updating saved cookies."""
    spec = _eh_spec(platform_label)
    profile = eh_profile_dir(platform_label, profile_root)
    if not profile.exists():
        raise RuntimeError(f"尚未建立 {spec.label} 登录资料；请先打开普通 Chrome 登录")
    if platform.system() != "Windows":
        raise RuntimeError("当前系统暂不能直接读取普通 Chrome Cookie；请使用“导入 Cookie 文件”")
    deadline = time.monotonic() + max(1.0, min(float(wait_seconds), 30.0))
    hosts = {"e-hentai.org", "exhentai.org"} if spec.key == "exhentai" else {"e-hentai.org"}
    last_error: Exception | None = None
    cookies: list[dict] = []
    while time.monotonic() < deadline:
        try:
            raw = read_chrome_profile_cookies(profile, hosts=hosts)
            cookies = _site_cookie_records(raw, inner=spec.key == "exhentai")
            names = {str(item.get("name") or "") for item in cookies if item.get("value")}
            if spec.required_names.issubset(names):
                last_error = None
                break
            last_error = None
        except Exception as exc:  # Chrome may still be committing or locking its database.
            last_error = exc
        time.sleep(0.5)
    else:
        if last_error is not None:
            raise RuntimeError(
                f"读取 {spec.label} Chrome Cookie 失败；请关闭该专用 Chrome 的全部窗口后重试：{last_error}"
            ) from last_error
        missing = sorted(spec.required_names - {str(item.get("name") or "") for item in cookies})
        if spec.key == "exhentai" and "igneous" in missing:
            raise PermissionError(
                "ExHentai 里站缺少 igneous；请先在普通登录页完成表站/论坛登录，再点击"
                "“登录后进入里站”，确认里站不是空白页，关闭该 Chrome 的全部窗口后重新获取；"
                "旧 Cookie 未被覆盖"
            )
        raise PermissionError(
            f"{spec.label} 缺少必要 Cookie：{', '.join(missing)}；旧 Cookie 未被覆盖"
        )
    user_agent = _chrome_user_agent()
    _validate_eh_session(
        platform_label,
        cookies,
        proxy_url=proxy_url,
        user_agent=user_agent,
    )
    result = save_captured_cookies(spec, cookies)
    _save_browser_identity(spec, user_agent)
    current_host = "exhentai.org" if spec.key == "exhentai" else "e-hentai.org"
    return CookieCaptureResult(
        platform=result.platform,
        cookie_count=result.cookie_count,
        cookie_names=result.cookie_names,
        destination=result.destination,
        user_agent=user_agent,
        current_host=current_host,
    )


def import_ehentai_cookie_file(
    platform_label: str,
    source: Path | str,
    *,
    proxy_url: str = "",
) -> CookieCaptureResult:
    """Validate a browser export/DevTools header capture before atomic import."""
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
    records = [
        {"name": name, "value": value, "domain": domain, "path": "/", "secure": True}
        for name, value in values.items()
        if name and value
    ]
    _validate_eh_session(
        platform_label,
        records,
        proxy_url=proxy_url,
        user_agent=user_agent,
    )
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


def convert_eh_header_capture_to_json(
    platform_label: str,
    source: Path | str,
    destination: Path | str | None = None,
) -> CookieJsonConversion:
    """Convert a mixed DevTools request/response header capture to cookie JSON."""
    return convert_header_capture_to_cookie_json(_eh_spec(platform_label), source, destination)
