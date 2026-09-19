from __future__ import annotations

import base64
import hashlib
import hmac
import json
import platform
import re
import socket
import sqlite3
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.browser_cookie_capture import CookieCaptureResult, cookie_capture_spec, save_captured_cookies
from software_app.core.browser_driver import chrome_version, ensure_chromedriver, find_chrome_executable
from software_app.core.settings import BROWSER_TOOLS_DIR, JMCOMIC_DATA_DIR, MANUAL_BROWSER_PROFILE_ROOT, ensure_data_dirs


_LEGACY_JM_PROFILE_DIR = Path(BROWSER_TOOLS_DIR) / "jmcomic_manual_profile"
# Preserve an existing login without copying credential-bearing browser data.
# New installations use the shared manual-login profile registry.
JM_PROFILE_DIR = (
    _LEGACY_JM_PROFILE_DIR
    if _LEGACY_JM_PROFILE_DIR.exists()
    else Path(MANUAL_BROWSER_PROFILE_ROOT) / "jmcomic"
)
SAFE_REQUEST_HEADERS = {
    "accept", "accept-encoding", "accept-language", "cache-control", "pragma", "referer", "user-agent",
    "sec-ch-ua", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
    "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-model", "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
}


@dataclass(frozen=True)
class JmPlainBrowserStatus:
    profile_dir: Path
    debugger_address: str
    reused: bool = False


def _enable_session_restore(profile_dir: Path) -> None:
    """Keep session cookies when the dedicated login Chrome exits normally."""
    preferences = Path(profile_dir) / "Default" / "Preferences"
    try:
        payload = json.loads(preferences.read_text(encoding="utf-8")) if preferences.is_file() else {}
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    session = payload.get("session")
    if not isinstance(session, dict):
        session = {}
        payload["session"] = session
    session["restore_on_startup"] = 1
    _atomic_json(preferences, payload)


def _origin(domain: str) -> tuple[str, str]:
    value = str(domain or "").strip().rstrip("/")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("JMComic 站点必须是完整的 http/https URL")
    return value, parsed.hostname.casefold().rstrip(".")


def parse_devtools_active_port(text: str) -> str:
    lines = str(text or "").splitlines()
    try:
        port = int(lines[0].strip())
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("JMComic Chrome 调试端口文件无效") from exc
    if not 1 <= port <= 65535:
        raise ValueError("JMComic Chrome 调试端口超出范围")
    return f"127.0.0.1:{port}"


def _debugger_is_listening(address: str, timeout: float = 0.5) -> bool:
    host, _, port_text = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port_text)), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def debugger_address(profile_dir: Path = JM_PROFILE_DIR) -> str:
    path = Path(profile_dir) / "DevToolsActivePort"
    try:
        address = parse_devtools_active_port(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("尚未发现 JMComic 普通 Chrome；请先点击“打开普通 Chrome 登录”") from exc
    if not _debugger_is_listening(address):
        raise RuntimeError("JMComic 普通 Chrome 已关闭；请重新打开后再获取")
    return address


def open_jmcomic_plain_chrome(
    domain: str,
    *,
    proxy_url: str = "",
    profile_dir: Path = JM_PROFILE_DIR,
    wait_seconds: float = 15.0,
) -> JmPlainBrowserStatus:
    """Launch a normal persistent Chrome with no automation or debugging flags."""
    origin, _host = _origin(domain)
    del wait_seconds
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("未找到 Google Chrome")
    ensure_data_dirs()
    profile = Path(profile_dir)
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
    command.append(origin)
    subprocess.Popen(command)
    return JmPlainBrowserStatus(profile, "", False)


def _chrome_user_agent() -> str:
    version = chrome_version().strip()
    if not version:
        return ""
    major = version.split(".", 1)[0]
    if not major.isdigit():
        return ""
    system = platform.system()
    if system == "Windows":
        platform_text = "Windows NT 10.0; Win64; x64"
    elif system == "Darwin":
        platform_text = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        platform_text = "X11; Linux x86_64"
    return (
        f"Mozilla/5.0 ({platform_text}) AppleWebKit/537.36 "
        f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36"
    )


def chrome_request_headers(origin: str, user_agent: str) -> dict[str, str]:
    """Build the safe navigation headers exposed by the dedicated Chrome."""
    match = re.search(r"Chrome/(\d+)", str(user_agent or ""))
    major = match.group(1) if match else ""
    headers = {
        "accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
        "cache-control": "max-age=0",
        "referer": str(origin or "").rstrip("/") + "/",
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "user-agent": str(user_agent or "").strip(),
    }
    if major:
        headers["sec-ch-ua"] = (
            f'"Not_A Brand";v="8", "Chromium";v="{major}", '
            f'"Google Chrome";v="{major}"'
        )
    return {name: value for name, value in headers.items() if value}


def _chrome_master_key(profile_dir: Path) -> bytes:
    if platform.system() != "Windows":
        raise RuntimeError("当前系统暂不支持直接读取正在运行的 Chrome Cookie")
    try:
        import win32crypt
    except ImportError as exc:
        raise RuntimeError("缺少 pywin32，无法读取 Chrome Cookie") from exc

    state_path = Path(profile_dir) / "Local State"
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        encrypted_key = base64.b64decode(state["os_crypt"]["encrypted_key"])
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("专用 Chrome 登录资料缺少有效的加密密钥") from exc
    if not encrypted_key.startswith(b"DPAPI"):
        raise RuntimeError("专用 Chrome Cookie 使用了当前版本不支持的加密方式")
    try:
        return bytes(win32crypt.CryptUnprotectData(encrypted_key[5:], None, None, None, 0)[1])
    except Exception as exc:
        raise RuntimeError("无法解锁专用 Chrome Cookie；请使用启动软件的同一 Windows 账号") from exc


def _decrypt_chrome_cookie_value(
    encrypted_value: bytes,
    master_key: bytes,
    host: str,
    database_version: int,
) -> str:
    payload = bytes(encrypted_value or b"")
    if not payload:
        return ""
    if not payload.startswith((b"v10", b"v11")):
        if platform.system() != "Windows":
            raise RuntimeError("不支持当前 Chrome Cookie 加密方式")
        import win32crypt

        return bytes(win32crypt.CryptUnprotectData(payload, None, None, None, 0)[1]).decode("utf-8")

    try:
        from Crypto.Cipher import AES

        nonce = payload[3:15]
        encrypted = payload[15:-16]
        tag = payload[-16:]
        plaintext = AES.new(master_key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(encrypted, tag)
    except Exception as exc:
        raise RuntimeError("无法解密专用 Chrome Cookie") from exc

    if database_version >= 24:
        expected_hash = hashlib.sha256(str(host).encode("utf-8")).digest()
        if len(plaintext) < len(expected_hash) or not hmac.compare_digest(plaintext[:32], expected_hash):
            raise RuntimeError("专用 Chrome Cookie 域名校验失败")
        plaintext = plaintext[32:]
    return plaintext.decode("utf-8")


def _chrome_expiry(value: object) -> int | None:
    try:
        chrome_time = int(value or 0)
    except (TypeError, ValueError):
        return None
    if chrome_time <= 0:
        return None
    unix_time = int(chrome_time / 1_000_000 - 11_644_473_600)
    return unix_time if unix_time > 0 else None


def read_chrome_profile_cookies(
    profile_dir: Path,
    *,
    master_key: bytes | None = None,
    hosts: set[str] | None = None,
) -> list[dict]:
    """Read committed cookies from the persistent Chrome profile without WebDriver."""
    profile = Path(profile_dir)
    cookie_db = profile / "Default" / "Network" / "Cookies"
    if not cookie_db.is_file():
        raise RuntimeError("专用 Chrome 登录资料中没有 Cookie 数据库")
    key = master_key if master_key is not None else _chrome_master_key(profile)
    uri = cookie_db.resolve().as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key = ?", ("version",)).fetchone()
            database_version = int(row[0]) if row else 0
            records = connection.execute(
                "SELECT host_key, name, value, encrypted_value, path, is_secure, "
                "is_httponly, expires_utc FROM cookies"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RuntimeError(
            "专用 Chrome 仍在使用登录资料；请关闭由软件打开的全部专用 Chrome 窗口后重试"
        ) from exc

    cookies: list[dict] = []
    for host, name, value, encrypted_value, path, secure, http_only, expires_utc in records:
        if hosts and not any(_host_matches(str(host or ""), expected) for expected in hosts):
            continue
        try:
            cookie_value = str(value or "") or _decrypt_chrome_cookie_value(
                bytes(encrypted_value or b""), key, str(host or ""), database_version
            )
        except (RuntimeError, UnicodeDecodeError):
            continue
        if not name or not cookie_value:
            continue
        cookie = {
            "name": str(name),
            "value": cookie_value,
            "domain": str(host or ""),
            "path": str(path or "/"),
            "secure": bool(secure),
            "httpOnly": bool(http_only),
        }
        expiry = _chrome_expiry(expires_utc)
        if expiry is not None:
            cookie["expiry"] = expiry
        cookies.append(cookie)
    return cookies


def _open_cookie_reader(
    profile_dir: Path,
    *,
    proxy_url: str = "",
    wait_seconds: float = 15.0,
) -> tuple[subprocess.Popen, str]:
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError("未找到 Google Chrome")
    active_port = Path(profile_dir) / "DevToolsActivePort"
    active_port.unlink(missing_ok=True)
    command = [
        str(chrome),
        "--remote-debugging-port=0",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=900,640",
    ]
    proxy = str(proxy_url or "").strip()
    if proxy:
        endpoint = proxy if "://" in proxy else f"http://{proxy}"
        command.append(f"--proxy-server={endpoint}")
    command.append("about:blank")
    process = subprocess.Popen(command)
    deadline = time.monotonic() + max(3.0, min(float(wait_seconds), 30.0))
    while time.monotonic() < deadline:
        try:
            return process, debugger_address(profile_dir)
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("无法读取 JMComic 浏览器资料；请先关闭“打开普通 Chrome 登录”产生的全部窗口，再点击获取")


def _host_matches(candidate: str, expected: str) -> bool:
    left = str(candidate or "").casefold().lstrip(".").rstrip(".")
    right = str(expected or "").casefold().lstrip(".").rstrip(".")
    return bool(left and right) and (left == right or left.endswith("." + right) or right.endswith("." + left))


def filter_jm_cookies(cookies: list[dict], hosts: set[str]) -> list[dict]:
    return [
        item for item in cookies
        if isinstance(item, dict) and any(_host_matches(str(item.get("domain") or ""), host) for host in hosts)
    ]


def safe_request_headers(headers: dict | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in (headers or {}).items():
        normalized = str(name or "").casefold().strip()
        text = str(value or "").strip()
        if normalized in SAFE_REQUEST_HEADERS and text and "\r" not in text and "\n" not in text:
            result[normalized] = text
    return result


def headers_from_performance_logs(entries: list[dict], account_url: str) -> dict[str, str]:
    expected_path = urlparse(account_url).path.rstrip("/")
    candidates: list[dict[str, str]] = []
    for entry in entries:
        try:
            envelope = json.loads(str(entry.get("message") or "{}"))
            message = envelope.get("message", {})
            if message.get("method") != "Network.requestWillBeSent":
                continue
            request = message.get("params", {}).get("request", {})
            if urlparse(str(request.get("url") or "")).path.rstrip("/") != expected_path:
                continue
            headers = safe_request_headers(request.get("headers"))
            if headers:
                candidates.append(headers)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
    return candidates[-1] if candidates else {}


def _atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def capture_jmcomic_plain_chrome(
    domain: str,
    username: str,
    *,
    profile_dir: Path = JM_PROFILE_DIR,
    proxy_url: str = "",
    wait_seconds: float = 15.0,
) -> CookieCaptureResult:
    """Read AVS directly from the manual profile, updating saved state only on success."""
    from selenium import webdriver
    from selenium.webdriver import ChromeOptions
    from selenium.webdriver.chrome.service import Service

    origin, configured_host = _origin(domain)
    account = str(username or "").strip().lstrip("@").strip("/")
    if not account:
        raise ValueError("请先填写 JMComic 账号用户名")
    profile = Path(profile_dir)
    if not profile.exists():
        raise RuntimeError("尚未建立 JMComic 登录资料；请先打开普通 Chrome 登录")
    if platform.system() == "Windows":
        deadline = time.monotonic() + max(1.0, min(float(wait_seconds), 30.0))
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                raw_cookies = read_chrome_profile_cookies(profile, hosts={configured_host})
                cookies = filter_jm_cookies(raw_cookies, {configured_host})
                avs = next((item for item in cookies if item.get("name") == "AVS" and item.get("value")), None)
                if avs is not None:
                    user_agent = _chrome_user_agent()
                    headers = chrome_request_headers(origin, user_agent)
                    spec = cookie_capture_spec("JMComic", jmcomic_domain=origin, jmcomic_username=account)
                    result = save_captured_cookies(spec, cookies)
                    _atomic_json(Path(JMCOMIC_DATA_DIR) / "browser_headers.json", headers)
                    return CookieCaptureResult(
                        platform=result.platform,
                        cookie_count=result.cookie_count,
                        cookie_names=result.cookie_names,
                        destination=result.destination,
                        user_agent=user_agent,
                        current_host=configured_host,
                    )
                last_error = None
            except Exception as exc:  # The live database can be briefly locked while Chrome commits.
                last_error = exc
            time.sleep(0.5)
        if last_error is not None:
            raise RuntimeError(f"读取 JMComic Chrome Cookie 失败：{last_error}") from last_error
        raise PermissionError(
            "没有获取到当前域名的 AVS；请重新打开普通 Chrome，确认账号已登录后关闭其全部窗口，"
            "再点击“登录完成后获取”"
        )

    # Compatibility fallback for platforms where the native Chrome cookie store
    # cannot yet be decrypted directly. This path still uses the short-lived reader.
    reader_process, address = _open_cookie_reader(profile, proxy_url=proxy_url)
    driver_path = ensure_chromedriver(auto_download=True)
    if driver_path is None:
        raise RuntimeError("未找到可用的 ChromeDriver")
    options = ChromeOptions()
    options.debugger_address = address
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
    try:
        driver = webdriver.Chrome(service=Service(str(driver_path)), options=options)
    except Exception:
        try:
            reader_process.terminate()
        except Exception:
            pass
        raise
    try:
        raw_cookies = driver.execute_cdp_cmd("Network.getAllCookies", {}).get("cookies", [])
        cookies = filter_jm_cookies(raw_cookies, {configured_host})
        names = {str(item.get("name") or "") for item in cookies}
        if "AVS" not in names:
            raise PermissionError("没有检测到 AVS；请在普通 Chrome 中完成登录、确认个人收藏页可见，然后关闭该窗口再获取")
        user_agent = str(driver.execute_script("return navigator.userAgent") or "").strip()
        headers = chrome_request_headers(origin, user_agent)
        spec = cookie_capture_spec("JMComic", jmcomic_domain=origin, jmcomic_username=account)
        result = save_captured_cookies(spec, cookies)
        _atomic_json(Path(JMCOMIC_DATA_DIR) / "browser_headers.json", headers)
        return CookieCaptureResult(
            platform=result.platform,
            cookie_count=result.cookie_count,
            cookie_names=result.cookie_names,
            destination=result.destination,
            user_agent=user_agent,
            current_host=configured_host,
        )
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        try:
            reader_process.wait(timeout=5)
        except Exception:
            pass
