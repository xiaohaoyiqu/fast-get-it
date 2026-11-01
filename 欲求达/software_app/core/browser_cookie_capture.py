from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from software_app.crawlers.common import load_cookie_file
from software_app.core.cookie_store import load_cookie_records, normalize_record, merge_cookie_records
from software_app.core.browser_driver import ensure_chromedriver
from software_app.core.settings import (
    BROWSER_TOOLS_DIR,
    EHENTAI_DATA_DIR,
    HENTAIPAW_DATA_DIR,
    INSTAGRAM_DATA_DIR,
    JMCOMIC_DATA_DIR,
    PIXIV_DATA_DIR,
    TWITTER_DATA_DIR,
    ensure_data_dirs,
)


COOKIE_CAPTURE_PLATFORMS = (
    "Twitter/X", "Pixiv", "FANBOX", "Instagram", "E-Hentai 表站", "ExHentai 里站", "HentaiPaw",
)


@dataclass(frozen=True)
class CookieCaptureSpec:
    key: str
    label: str
    login_url: str
    required_names: frozenset[str]
    allowed_hosts: tuple[str, ...]
    destination: Path
    storage_format: str = "name_value"
    merge_existing: bool = False
    validation_kind: str = "cookie_only"
    validation_url: str = ""


@dataclass(frozen=True)
class CookieJsonConversion:
    platform: str
    source: Path
    destination: Path
    cookie_count: int
    cookie_names: tuple[str, ...]


@dataclass(frozen=True)
class CookieCaptureResult:
    platform: str
    cookie_count: int
    cookie_names: tuple[str, ...]
    destination: Path
    user_agent: str = ""
    current_host: str = ""


class CookieCaptureCancelled(RuntimeError):
    pass


def _normalized_origin(value: str) -> tuple[str, str]:
    raw = str(value or "").strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("JMComic 站点必须是完整的 http/https URL")
    return raw, parsed.hostname.lower().rstrip(".")


def cookie_capture_spec(
    platform: str,
    *,
    jmcomic_domain: str = "https://18comic.vip",
    jmcomic_username: str = "",
) -> CookieCaptureSpec:
    label = str(platform or "").strip()
    specs = {
        "HentaiPaw": CookieCaptureSpec(
            key="hentaipaw", label="HentaiPaw", login_url="https://zh.hentaipaw.com/",
            required_names=frozenset(), allowed_hosts=("hentaipaw.com",),
            destination=Path(HENTAIPAW_DATA_DIR) / "cookies.json",
        ),
        "Twitter/X": CookieCaptureSpec(
            key="twitter",
            label="Twitter/X",
            login_url="https://x.com/i/flow/login",
            required_names=frozenset({"auth_token", "ct0"}),
            allowed_hosts=("x.com", "twitter.com"),
            destination=Path(TWITTER_DATA_DIR) / "X_cookie.json",
            storage_format="selenium_list",
        ),
        "Pixiv": CookieCaptureSpec(
            key="pixiv",
            label="Pixiv",
            login_url="https://accounts.pixiv.net/login",
            required_names=frozenset({"PHPSESSID"}),
            allowed_hosts=("pixiv.net",),
            destination=Path(PIXIV_DATA_DIR) / "cookies.json",
            merge_existing=True,
            validation_kind="pixiv_api",
            validation_url="https://www.pixiv.net/ajax/user/extra?lang=en",
        ),
        "FANBOX": CookieCaptureSpec(
            key="fanbox",
            label="FANBOX",
            login_url="https://www.fanbox.cc/",
            required_names=frozenset({"FANBOXSESSID"}),
            allowed_hosts=("fanbox.cc", "accounts.pixiv.net", "pixiv.net"),
            destination=Path(PIXIV_DATA_DIR) / "cookies.json",
            merge_existing=True,
            validation_kind="fanbox_api",
            validation_url="https://api.fanbox.cc/plan.listSupporting",
        ),
        "Instagram": CookieCaptureSpec(
            key="instagram",
            label="Instagram",
            login_url="https://www.instagram.com/accounts/login/",
            required_names=frozenset({"sessionid"}),
            allowed_hosts=("instagram.com",),
            destination=Path(INSTAGRAM_DATA_DIR) / "cookies.json",
        ),
        "E-Hentai 表站": CookieCaptureSpec(
            key="ehentai",
            label="E-Hentai 表站",
            login_url="https://forums.e-hentai.org/index.php?act=Login&CODE=00",
            required_names=frozenset({"ipb_member_id", "ipb_pass_hash"}),
            allowed_hosts=("e-hentai.org",),
            destination=Path(EHENTAI_DATA_DIR) / "e-hentai-cookies.json",
        ),
        "ExHentai 里站": CookieCaptureSpec(
            key="exhentai",
            label="ExHentai 里站",
            login_url="https://forums.e-hentai.org/index.php?act=Login&CODE=00",
            required_names=frozenset({"ipb_member_id", "ipb_pass_hash", "igneous"}),
            allowed_hosts=("e-hentai.org", "exhentai.org"),
            destination=Path(EHENTAI_DATA_DIR) / "exhentai-cookies.json",
        ),
    }
    if label == "JMComic":
        origin, host = _normalized_origin(jmcomic_domain)
        username = str(jmcomic_username or "").strip().lstrip("@").strip("/")
        validation_url = f"{origin}/user/{username}/favorite/albums" if username else ""
        return CookieCaptureSpec(
            key="jmcomic",
            label="JMComic",
            login_url=origin + "/",
            required_names=frozenset({"AVS"}),
            allowed_hosts=(host,),
            destination=Path(JMCOMIC_DATA_DIR) / "cookies.json",
            validation_kind="jmcomic_account" if validation_url else "cookie_only",
            validation_url=validation_url,
        )
    try:
        return specs[label]
    except KeyError as exc:
        raise ValueError(f"不支持浏览器登录获取 Cookie：{label or '未选择平台'}") from exc


def host_is_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    candidate = str(host or "").lower().rstrip(".")
    return bool(candidate) and any(
        candidate == allowed or candidate.endswith("." + allowed)
        for allowed in allowed_hosts
    )


def _cookie_records(cookies: list[dict]) -> list[dict]:
    records: list[dict] = []
    allowed_fields = {"name", "value", "path", "domain", "secure", "httpOnly", "expiry", "sameSite", "hostOnly"}
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name or item.get("value") is None:
            continue
        record = {key: value for key, value in item.items() if key in allowed_fields and value is not None}
        record["name"] = name
        record["value"] = str(item.get("value") or "")
        records.append(record)
    return records


def save_captured_cookies(spec: CookieCaptureSpec, cookies: list[dict]) -> CookieCaptureResult:
    defaults = {"twitter": ".x.com", "pixiv": ".pixiv.net", "fanbox": ".fanbox.cc",
                "instagram": ".instagram.com", "ehentai": ".e-hentai.org", "exhentai": ".exhentai.org"}
    default_domain = defaults.get(spec.key, str(urlparse(spec.login_url).hostname or ""))
    records = []
    for item in _cookie_records(cookies):
        record = normalize_record(item)
        if not record["domain"]:
            domain = {"PHPSESSID": ".pixiv.net", "FANBOXSESSID": ".fanbox.cc"}.get(record["name"], default_domain)
            record.update(domain=domain, hostOnly=not domain.startswith("."),
                          secure=urlparse(spec.login_url).scheme == "https")
        allowed = (*spec.allowed_hosts, "fanbox.cc") if spec.key == "pixiv" else spec.allowed_hosts
        if not host_is_allowed(record["domain"].lstrip("."), allowed):
            raise ValueError("Cookie 域不属于所选平台；请仅导出该平台的登录资料")
        records.append(record)
    names = {str(item["name"]) for item in records
             if item["value"] and ("expiry" not in item or item["expiry"] > time.time())}
    missing = sorted(spec.required_names - names)
    if missing:
        raise ValueError(f"{spec.label} 登录尚未完成，缺少 Cookie：{', '.join(missing)}")

    destination = Path(spec.destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if spec.storage_format == "selenium_list":
        payload: object = [{key: value for key, value in record.items() if key != "hostOnly"} for record in records]
    else:
        if spec.merge_existing:
            payload = merge_cookie_records(load_cookie_records(destination), records)
        else:
            payload = records

    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)

    saved_names = tuple(sorted(
        str(item.get("name")) for item in records
        if str(item.get("name") or "").strip()
    )) if spec.storage_format == "selenium_list" else tuple(sorted(load_cookie_file(destination)))
    return CookieCaptureResult(spec.label, len(saved_names), saved_names, destination)


def import_cookie_file(spec: CookieCaptureSpec, source: Path | str) -> CookieCaptureResult:
    records = load_cookie_records(source, strict=True)
    if not records:
        raise ValueError("Cookie 文件为空或格式无效；支持 JSON、Netscape cookies.txt 或 Cookie 请求头文本")
    return save_captured_cookies(spec, records)


def convert_header_capture_to_cookie_json(
    spec: CookieCaptureSpec,
    source: Path | str,
    destination: Path | str | None = None,
) -> CookieJsonConversion:
    """Convert mixed DevTools request/response headers into cookie-only JSON."""
    source_path = Path(source).expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(f"请求头 TXT 不存在：{source_path.name}")
    values = load_cookie_file(source_path)
    cookies = {
        str(name): str(value)
        for name, value in values.items()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", str(name))
        and value is not None
        and "\r" not in str(value)
        and "\n" not in str(value)
        and len(str(value)) <= 16384
    }
    if not cookies:
        raise ValueError("没有在 TXT 中找到可识别的 Cookie 请求头")
    missing = sorted(spec.required_names - {name for name, value in cookies.items() if value})
    if missing:
        raise PermissionError(f"{spec.label} 缺少必要 Cookie：{', '.join(missing)}；没有生成 JSON")
    if destination is None:
        base = source_path.with_name(f"{source_path.stem}.{spec.key}.cookies.json")
        output_path = base
        counter = 2
        while output_path.exists():
            output_path = base.with_name(f"{base.stem}-{counter}{base.suffix}")
            counter += 1
    else:
        output_path = Path(destination).expanduser().resolve()
        if output_path.exists():
            raise FileExistsError(f"目标 JSON 已存在：{output_path.name}")
    if output_path == source_path:
        raise ValueError("输出 JSON 不能覆盖原始请求头 TXT")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(f".{output_path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return CookieJsonConversion(
        platform=spec.label,
        source=source_path,
        destination=output_path,
        cookie_count=len(cookies),
        cookie_names=tuple(sorted(cookies)),
    )


def _browser_session_is_valid(driver, spec: CookieCaptureSpec, current_url: str) -> bool:
    parsed = urlparse(str(current_url or ""))
    path = parsed.path.casefold()
    if spec.key == "pixiv" and str(parsed.hostname or "").casefold() == "accounts.pixiv.net":
        return False
    if any(marker in path for marker in ("/login", "/challenge", "/checkpoint")):
        return False
    if spec.validation_kind == "cookie_only" or not spec.validation_url:
        return True

    script = """
        const url = arguments[0];
        const done = arguments[arguments.length - 1];
        fetch(url, {credentials: 'include', headers: {'Accept': 'application/json,text/html;q=0.9,*/*;q=0.8'}})
          .then(async response => done({
              status: response.status,
              url: response.url,
              contentType: response.headers.get('content-type') || '',
              text: (await response.text()).slice(0, 200000)
          }))
          .catch(error => done({status: 0, url: '', contentType: '', text: '', error: String(error)}));
    """
    try:
        probe = driver.execute_async_script(script, spec.validation_url)
    except Exception:
        return False
    if not isinstance(probe, dict) or int(probe.get("status") or 0) != 200:
        return False
    final_path = urlparse(str(probe.get("url") or "")).path.casefold()
    text = str(probe.get("text") or "")
    lowered = text.casefold()
    if any(marker in final_path for marker in ("/login", "/challenge", "/checkpoint")):
        return False
    if spec.validation_kind in {"pixiv_api", "fanbox_api"}:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return False
        if not isinstance(payload, dict) or payload.get("error") is True:
            return False
        return True
    if spec.validation_kind == "jmcomic_account":
        if "type=\"password\"" in lowered or "type='password'" in lowered:
            return False
        return "/favorite/albums" in final_path or "/favorite/albums" in lowered
    return True


def capture_browser_cookies(
    spec: CookieCaptureSpec,
    *,
    proxy_url: str = "",
    cancel_event: threading.Event | None = None,
    timeout_seconds: int = 600,
) -> CookieCaptureResult:
    """Open a disposable visible Chrome profile and save a completed login session."""
    if spec.key in {"ehentai", "exhentai"}:
        raise ValueError("EH 仅支持手动填写或导入 Cookie，请在设置页选择对应入口")
    if spec.key == "hentaipaw":
        raise ValueError("HentaiPaw 请导入浏览器导出的 Cookie；当前未确认账号登录接口，不能自动判定登录成功")
    from selenium import webdriver
    from selenium.common.exceptions import TimeoutException, WebDriverException
    from selenium.webdriver import ChromeOptions
    from .browser_process import ManagedChromeService as Service

    ensure_data_dirs()
    driver_path = ensure_chromedriver(auto_download=True)
    if driver_path is None:
        raise RuntimeError("未找到可用的 ChromeDriver")

    profile_root = Path(BROWSER_TOOLS_DIR) / "login_profiles"
    profile_root.mkdir(parents=True, exist_ok=True)
    profile_dir = Path(tempfile.mkdtemp(prefix=f"{spec.key}-", dir=profile_root))
    driver = None
    stop_event = cancel_event or threading.Event()
    deadline = time.monotonic() + max(30, min(int(timeout_seconds), 1800))
    try:
        options = ChromeOptions()
        options.add_argument(f"--user-data-dir={profile_dir}")
        options.add_argument("--window-size=1180,820")
        options.add_argument("--disable-logging")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])
        proxy = str(proxy_url or "").strip()
        if proxy:
            endpoint = proxy if "://" in proxy else f"http://{proxy}"
            options.add_argument(f"--proxy-server={endpoint}")
        driver = webdriver.Chrome(service=Service(str(driver_path)), options=options)
        driver.set_page_load_timeout(45)
        driver.set_script_timeout(20)
        try:
            driver.get(spec.login_url)
        except TimeoutException:
            pass

        last_host = ""
        last_validation_at = 0.0
        while time.monotonic() < deadline:
            if stop_event.is_set():
                raise CookieCaptureCancelled("已停止获取 Cookie")
            try:
                current_url = str(driver.current_url or "")
                last_host = str(urlparse(current_url).hostname or "").lower()
                cookies = driver.get_cookies()
            except WebDriverException as exc:
                if stop_event.is_set():
                    raise CookieCaptureCancelled("已停止获取 Cookie") from exc
                raise CookieCaptureCancelled("登录浏览器已关闭，未保存 Cookie") from exc
            names = {str(item.get("name") or "") for item in cookies if isinstance(item, dict)}
            ready = spec.required_names.issubset(names) and host_is_allowed(last_host, spec.allowed_hosts)
            now = time.monotonic()
            if ready and now - last_validation_at >= 3.0:
                last_validation_at = now
                ready = _browser_session_is_valid(driver, spec, current_url)
            else:
                ready = False
            if ready:
                result = save_captured_cookies(spec, cookies)
                try:
                    user_agent = str(driver.execute_script("return navigator.userAgent") or "").strip()
                except WebDriverException:
                    user_agent = ""
                return CookieCaptureResult(
                    platform=result.platform,
                    cookie_count=result.cookie_count,
                    cookie_names=result.cookie_names,
                    destination=result.destination,
                    user_agent=user_agent,
                    current_host=last_host,
                )
            stop_event.wait(1.0)
        required = ", ".join(sorted(spec.required_names))
        raise TimeoutError(f"等待 {spec.label} 登录超时；未检测到 {required}")
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
        shutil.rmtree(profile_dir, ignore_errors=True)
