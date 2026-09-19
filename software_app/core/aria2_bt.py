from __future__ import annotations

import hashlib
import io
import os
import platform
import shutil
import subprocess
import time
import urllib.request
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Callable

from .adapter import TaskCancelled
from .settings import INSTALL_ROOT, PROJECT_ROOT, SOFTWARE_TOOLS_DIR, ensure_data_dirs


ARIA2_VERSION = "1.37.0"
ARIA2_ARCHIVE_NAME = "aria2-1.37.0-win-64bit-build1.zip"
ARIA2_WINDOWS_X64_URL = (
    "https://github.com/aria2/aria2/releases/download/release-1.37.0/"
    "aria2-1.37.0-win-64bit-build1.zip"
)
ARIA2_WINDOWS_X64_SHA256 = "67d015301eef0b612191212d564c5bb0a14b5b9c4796b76454276a4d28d9b288"
MAX_ARIA2_ARCHIVE_BYTES = 32 * 1024 * 1024


class Aria2Error(RuntimeError):
    pass


@dataclass(frozen=True)
class Aria2Status:
    executable: Path | None
    version: str = ""
    source: str = "missing"
    message: str = "未找到 aria2"


@dataclass(frozen=True)
class BtDownloadResult:
    output_dir: Path
    files: tuple[Path, ...]
    total_size: int


def aria2_executable_name() -> str:
    return "aria2c.exe" if platform.system() == "Windows" else "aria2c"


def bundled_aria2_path() -> Path:
    ensure_data_dirs()
    return SOFTWARE_TOOLS_DIR / "aria2" / aria2_executable_name()


def find_aria2c(configured_path: str | Path | None = None) -> tuple[Path | None, str]:
    candidates: list[tuple[Path, str]] = []
    if configured_path:
        candidates.append((Path(configured_path).expanduser(), "configured"))
    candidates.append((bundled_aria2_path(), "bundled"))
    resolved = shutil.which(aria2_executable_name()) or shutil.which("aria2c")
    if resolved:
        candidates.append((Path(resolved), "path"))
    seen: set[str] = set()
    for candidate, source in candidates:
        try:
            path = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        key = str(path).casefold()
        if key not in seen and path.is_file():
            seen.add(key)
            return path, source
    return None, "missing"


def _creation_flags() -> int:
    return int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0


def aria2_status(configured_path: str | Path | None = None) -> Aria2Status:
    executable, source = find_aria2c(configured_path)
    if executable is None:
        return Aria2Status(None)
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
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise Aria2Error("aria2 可执行文件无法启动") from exc
    first_line = (completed.stdout or "").splitlines()[:1]
    version = first_line[0].strip() if first_line else ""
    if completed.returncode != 0 or "aria2 version" not in version.casefold():
        raise Aria2Error("检测到的程序不是可用的 aria2c")
    return Aria2Status(executable, version, source, "aria2 可用")


def _download_archive(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "ContentDownloader/2 aria2-installer"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            declared = int(response.headers.get("Content-Length") or 0)
            if declared > MAX_ARIA2_ARCHIVE_BYTES:
                raise Aria2Error("aria2 安装包大小异常")
            payload = response.read(MAX_ARIA2_ARCHIVE_BYTES + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise Aria2Error("无法连接 aria2 官方 GitHub Release；请检查网络或代理后重试") from exc
    if not payload or len(payload) > MAX_ARIA2_ARCHIVE_BYTES:
        raise Aria2Error("aria2 安装包为空或过大")
    return payload


def find_aria2_archive() -> Path | None:
    candidates = [
        Path(INSTALL_ROOT) / ARIA2_ARCHIVE_NAME,
        Path(PROJECT_ROOT) / ARIA2_ARCHIVE_NAME,
    ]
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError):
            continue
        key = str(resolved).casefold()
        if key not in seen and resolved.is_file():
            seen.add(key)
            return resolved
    return None


def _install_archive_payload(payload: bytes) -> Aria2Status:
    digest = hashlib.sha256(payload).hexdigest()
    if digest.casefold() != ARIA2_WINDOWS_X64_SHA256:
        raise Aria2Error("aria2 安装包 SHA-256 校验失败，已拒绝安装")
    selected: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            matches = [
                item for item in archive.infolist()
                if not item.is_dir() and Path(item.filename.replace("\\", "/")).name.casefold() == "aria2c.exe"
            ]
            if len(matches) != 1:
                raise Aria2Error("aria2 安装包中没有唯一的 aria2c.exe")
            member = matches[0]
            if member.file_size <= 0 or member.file_size > 20 * 1024 * 1024:
                raise Aria2Error("aria2c.exe 大小异常")
            selected["aria2c.exe"] = archive.read(member)
            allowed_documents = {"copying", "license.openssl", "readme.mingw", "readme.html"}
            for item in archive.infolist():
                basename = Path(item.filename.replace("\\", "/")).name.casefold()
                if item.is_dir() or basename not in allowed_documents or item.file_size > 2 * 1024 * 1024:
                    continue
                selected[Path(item.filename.replace("\\", "/")).name] = archive.read(item)
    except (zipfile.BadZipFile, OSError) as exc:
        raise Aria2Error("aria2 安装包无法读取") from exc
    target = bundled_aria2_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.installing")
    try:
        temporary.write_bytes(selected["aria2c.exe"])
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)
    for name, content in selected.items():
        if name.casefold() == "aria2c.exe":
            continue
        destination = target.parent / name
        document_temporary = destination.with_name(f".{destination.name}.installing")
        try:
            document_temporary.write_bytes(content)
            document_temporary.replace(destination)
        finally:
            document_temporary.unlink(missing_ok=True)
    return aria2_status(target)


def install_aria2_from_archive(archive_path: str | Path) -> Aria2Status:
    source = Path(archive_path).expanduser().resolve(strict=True)
    if source.stat().st_size > MAX_ARIA2_ARCHIVE_BYTES:
        raise Aria2Error("aria2 安装包大小异常")
    return _install_archive_payload(source.read_bytes())


def install_aria2(timeout: int = 180) -> Aria2Status:
    if platform.system() != "Windows" or platform.machine().casefold() not in {"amd64", "x86_64"}:
        raise Aria2Error("自动安装目前仅支持 64 位 Windows；其他系统可安装 aria2c 到 PATH")
    local_archive = find_aria2_archive()
    if local_archive is not None:
        return install_aria2_from_archive(local_archive)
    return _install_archive_payload(_download_archive(ARIA2_WINDOWS_X64_URL, timeout))


def build_aria2_command(executable: Path, torrent_path: Path, output_dir: Path) -> list[str]:
    return [
        str(executable),
        "--dir=" + str(output_dir),
        "--seed-time=0",
        "--seed-ratio=0.0",
        "--continue=true",
        "--file-allocation=none",
        "--allow-overwrite=false",
        "--auto-file-renaming=false",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--download-result=hide",
        "--bt-save-metadata=false",
        "--enable-dht=false",
        "--enable-dht6=false",
        "--enable-peer-exchange=false",
        "--bt-enable-lpd=false",
        "--follow-torrent=true",
        str(torrent_path),
    ]


def download_torrent_with_aria2(
    torrent_path: Path,
    output_dir: Path,
    cancel_event: Event,
    *,
    configured_path: str | Path | None = None,
    on_wait: Callable[[], None] | None = None,
) -> BtDownloadResult:
    torrent_path = Path(torrent_path).resolve(strict=True)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    executable, _source = find_aria2c(configured_path)
    if executable is None:
        raise Aria2Error("未找到 aria2c；请先在设置页检测或安装 aria2")
    command = build_aria2_command(executable, torrent_path, output_dir)
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=_creation_flags(),
        )
    except OSError as exc:
        raise Aria2Error("无法启动 aria2c") from exc
    try:
        while process.poll() is None:
            if cancel_event.wait(0.25):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                raise TaskCancelled("任务已取消")
            if on_wait is not None:
                on_wait()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    if process.returncode != 0:
        raise Aria2Error(f"aria2 下载失败（退出码 {process.returncode}）")
    files: list[Path] = []
    total_size = 0
    for path in output_dir.rglob("*"):
        if not path.is_file() or path.name.endswith(".aria2"):
            continue
        resolved = path.resolve()
        if output_dir != resolved and output_dir not in resolved.parents:
            raise Aria2Error("aria2 生成了超出下载目录的路径")
        files.append(resolved)
        total_size += resolved.stat().st_size
    if not files:
        raise Aria2Error("aria2 已退出，但未发现完整下载文件")
    return BtDownloadResult(output_dir, tuple(sorted(files)), total_size)


__all__ = [
    "ARIA2_VERSION", "Aria2Error", "Aria2Status", "BtDownloadResult", "aria2_status",
    "build_aria2_command", "bundled_aria2_path", "download_torrent_with_aria2", "find_aria2_archive",
    "find_aria2c", "install_aria2", "install_aria2_from_archive",
]
