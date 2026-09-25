from __future__ import annotations

import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    import winreg
except ImportError:
    winreg = None

from .settings import BROWSER_TOOLS_DIR, GOOGLE_IMAGE_ROOT, PROJECT_ROOT, TWITTER_ROOT, ensure_data_dirs


CHROME_FOR_TESTING_BUILDS = "https://googlechromelabs.github.io/chrome-for-testing/latest-patch-versions-per-build-with-downloads.json"
CHROME_FOR_TESTING_MILESTONES = "https://googlechromelabs.github.io/chrome-for-testing/latest-versions-per-milestone-with-downloads.json"


@dataclass(frozen=True)
class BrowserDriverStatus:
    driver_path: Path | None
    chrome_version: str = ""
    driver_version: str = ""
    source: str = ""
    message: str = ""


class BrowserDriverError(RuntimeError):
    pass


def driver_name() -> str:
    return "chromedriver.exe" if platform.system() == "Windows" else "chromedriver"


def driver_platform_name() -> str:
    if platform.system() == "Windows":
        return "win64" if sys.maxsize > 2**32 else "win32"
    if platform.system() == "Darwin":
        machine = platform.machine().lower()
        return "mac-arm64" if "arm" in machine else "mac-x64"
    return "linux64"


def global_driver_path() -> Path:
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        direct = exe_dir / driver_name()
        if direct.exists():
            return direct
        internal = exe_dir / "_internal" / driver_name()
        if internal.exists():
            return internal
        return direct
    ensure_data_dirs()
    return BROWSER_TOOLS_DIR / driver_name()


def chrome_executable_candidates() -> list[Path]:
    """Return installed Chrome candidates without starting the browser."""
    locations: list[Path] = []
    command_names: list[str]
    if platform.system() == "Windows":
        locations.extend([
            Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ])
        command_names = ["chrome.exe", "chrome"]
    elif platform.system() == "Darwin":
        locations.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])
        command_names = ["google-chrome", "chrome", "chromium"]
    else:
        locations.extend([
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/snap/bin/chromium"),
        ])
        command_names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]
    for command in command_names:
        resolved = shutil.which(command)
        if resolved:
            locations.append(Path(resolved))
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in locations:
        key = str(candidate).casefold()
        if candidate.exists() and key not in seen:
            seen.add(key)
            result.append(candidate.resolve())
    return result


def find_chrome_executable() -> Path | None:
    candidates = chrome_executable_candidates()
    return candidates[0] if candidates else None


def legacy_driver_candidates() -> list[Path]:
    if getattr(sys, "frozen", False):
        return []
    return [
        TWITTER_ROOT / driver_name(),
        PROJECT_ROOT / "推特爬虫" / driver_name(),
        GOOGLE_IMAGE_ROOT / driver_name(),
    ]


def find_existing_driver(include_legacy: bool = True) -> tuple[Path | None, str]:
    global_path = global_driver_path()
    if global_path.exists():
        return global_path.resolve(), "global"

    resolved = shutil.which(driver_name())
    if resolved:
        return Path(resolved).resolve(), "path"

    if include_legacy:
        for candidate in legacy_driver_candidates():
            if candidate.exists():
                return candidate.resolve(), "legacy"
    return None, ""


def copy_driver_to_global(source: Path) -> Path:
    target = global_driver_path()
    if source.resolve() == target.resolve():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    if platform.system() != "Windows":
        target.chmod(target.stat().st_mode | 0o755)
    return target.resolve()


def executable_version(executable: Path, product_name: str) -> str:
    try:
        completed = subprocess.run(
            [str(executable), "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = str(completed.stdout or "")
    if completed.returncode != 0 or product_name.casefold() not in output.casefold():
        return ""
    match = re.search(r"\b(\d+(?:\.\d+){1,3})\b", output)
    return match.group(1) if match else ""


def chromedriver_version(driver_path: Path) -> str:
    return executable_version(Path(driver_path), "ChromeDriver")


def versions_compatible(browser_version: str, driver_version: str) -> bool:
    if not browser_version or not driver_version:
        return bool(driver_version)
    return browser_version.split(".", 1)[0] == driver_version.split(".", 1)[0]


def chrome_version() -> str:
    if platform.system() == "Windows" and winreg is not None:
        registry_locations = [
            (winreg.HKEY_CURRENT_USER, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\Google\Chrome\BLBeacon"),
            (winreg.HKEY_LOCAL_MACHINE, r"Software\WOW6432Node\Google\Chrome\BLBeacon"),
        ]
        for root_key, key_path in registry_locations:
            try:
                key = winreg.OpenKey(root_key, key_path)
                version = winreg.QueryValueEx(key, "version")[0]
                if version:
                    return str(version)
            except Exception:
                pass

    locations: list[Path] = []
    command_names: list[str]
    if platform.system() == "Windows":
        locations.extend([
            Path(os.environ.get("PROGRAMFILES", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "Application" / "chrome.exe",
        ])
        command_names = ["chrome.exe", "chrome"]
    elif platform.system() == "Darwin":
        locations.extend([
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
        ])
        command_names = ["google-chrome", "chrome", "chromium"]
    else:
        locations.extend([
            Path("/usr/bin/google-chrome"),
            Path("/usr/bin/google-chrome-stable"),
            Path("/usr/bin/chromium"),
            Path("/usr/bin/chromium-browser"),
            Path("/snap/bin/chromium"),
        ])
        command_names = ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"]

    for command in command_names:
        resolved = shutil.which(command)
        if resolved:
            locations.append(Path(resolved))

    seen: set[str] = set()
    for chrome_exe in locations:
        chrome_exe = Path(chrome_exe)
        if not chrome_exe.exists() or str(chrome_exe) in seen:
            continue
        seen.add(str(chrome_exe))
        try:
            kwargs = {"capture_output": True, "text": True, "timeout": 5}
            if platform.system() == "Windows":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            completed = subprocess.run([str(chrome_exe), "--version"], **kwargs)
            output = f"{completed.stdout} {completed.stderr}"
            for part in output.split():
                if part[:1].isdigit():
                    return part.strip()
        except Exception:
            pass
    return ""


def download_url(url: str, timeout: int = 60, max_bytes: int = 64 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_error: Exception | None = None
    payload = b""
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                declared = int(response.headers.get("Content-Length") or 0)
                if declared > max_bytes:
                    raise BrowserDriverError("ChromeDriver 下载响应过大")
                payload = response.read(max_bytes + 1)
            break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1)
    else:
        raise BrowserDriverError("无法连接 Chrome for Testing；请检查网络或代理后重试") from last_error
    if not payload or len(payload) > max_bytes:
        raise BrowserDriverError("ChromeDriver 下载响应为空或过大")
    return payload


def select_chromedriver_download(downloads: list[dict] | None) -> str:
    platform_name = driver_platform_name()
    for item in downloads or []:
        if item.get("platform") == platform_name:
            return str(item.get("url") or "")
    return ""


def find_chromedriver_download_url(version: str) -> str:
    major = version.split(".", 1)[0] if version else ""
    build = ".".join(version.split(".")[:3]) if version else ""
    if not major:
        raise BrowserDriverError("未检测到已安装的 Chrome，无法匹配 ChromeDriver 版本")
    endpoints: list[tuple[str, str, str]] = []
    if build:
        endpoints.append(("builds", build, CHROME_FOR_TESTING_BUILDS))
    if major:
        endpoints.append(("milestones", major, CHROME_FOR_TESTING_MILESTONES))

    for group, key, url in endpoints:
        data = json.loads(download_url(url).decode("utf-8"))
        entry = data.get(group, {}).get(key, {})
        entry_version = str(entry.get("version") or "")
        if not versions_compatible(version, entry_version):
            continue
        driver_url = select_chromedriver_download(entry.get("downloads", {}).get("chromedriver"))
        if driver_url:
            return driver_url
    raise BrowserDriverError(f"Chrome for Testing 未提供与 Chrome {version} 匹配的 ChromeDriver")


def download_chromedriver(timeout: int = 180) -> Path:
    target = global_driver_path()
    version = chrome_version()
    download_link = find_chromedriver_download_url(version)
    archive_bytes = download_url(download_link, timeout=timeout)

    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            matches = [
                item for item in archive.infolist()
                if not item.is_dir()
                and Path(item.filename.replace("\\", "/")).name.casefold() == driver_name().casefold()
            ]
            if len(matches) != 1:
                raise BrowserDriverError("ChromeDriver 压缩包中没有唯一的驱动文件")
            member = matches[0]
            if member.file_size <= 0 or member.file_size > 64 * 1024 * 1024:
                raise BrowserDriverError("ChromeDriver 可执行文件大小异常")
            executable_bytes = archive.read(member)
    except zipfile.BadZipFile as exc:
        raise BrowserDriverError("ChromeDriver 下载结果不是有效 ZIP") from exc
    if not executable_bytes:
        raise BrowserDriverError("ChromeDriver 压缩包中未找到驱动文件")
    temporary = target.with_name(f".{target.name}.installing")
    try:
        temporary.write_bytes(executable_bytes)
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    if platform.system() != "Windows":
        target.chmod(target.stat().st_mode | 0o755)
    installed_version = ""
    for attempt in range(3):
        installed_version = chromedriver_version(target)
        if installed_version:
            break
        if attempt < 2:
            time.sleep(1)
    if not installed_version:
        target.unlink(missing_ok=True)
        raise BrowserDriverError("下载的 ChromeDriver 连续三次无法运行，已删除；请检查安全软件拦截")
    if not versions_compatible(version, installed_version):
        target.unlink(missing_ok=True)
        raise BrowserDriverError(
            f"下载的 ChromeDriver {installed_version} 与本机 Chrome {version} 主版本不匹配，已删除"
        )
    return target.resolve()


def ensure_chromedriver(auto_download: bool = True) -> Path | None:
    existing, source = find_existing_driver(include_legacy=True)
    if existing:
        browser_version = chrome_version()
        driver_version = chromedriver_version(existing)
        if driver_version and versions_compatible(browser_version, driver_version):
            if source == "global":
                return existing
            return copy_driver_to_global(existing)
    if not auto_download:
        return None
    return download_chromedriver()


def chromedriver_status(auto_download: bool = False) -> BrowserDriverStatus:
    version = chrome_version()
    existing, source = find_existing_driver(include_legacy=True)
    if existing:
        driver_version = chromedriver_version(existing)
        if driver_version and versions_compatible(version, driver_version):
            if source != "global":
                existing = copy_driver_to_global(existing)
                source = f"global-copy:{source}"
            return BrowserDriverStatus(existing, version, driver_version, source, "ChromeDriver 可用且版本匹配")
        mismatch = (
            f"ChromeDriver {driver_version or '未知'} 与 Chrome {version or '未知'} 不匹配"
            if driver_version else "现有 ChromeDriver 无法运行"
        )
        if not auto_download:
            return BrowserDriverStatus(existing, version, driver_version, "incompatible", mismatch)
    if not auto_download:
        return BrowserDriverStatus(None, version, "", "missing", "未找到 ChromeDriver")
    try:
        path = download_chromedriver()
    except Exception as exc:
        raise BrowserDriverError(f"自动下载 ChromeDriver 失败: {exc}") from exc
    driver_version = chromedriver_version(path)
    return BrowserDriverStatus(path, version, driver_version, "downloaded", "ChromeDriver 已自动匹配并下载")
