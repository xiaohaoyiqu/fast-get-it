from __future__ import annotations

from software_app.core.task_control import task_cancelled, task_wait

import ipaddress
import os
import re
import socket
import threading
import time
import unicodedata
import uuid
from contextlib import contextmanager, nullcontext
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from PIL import Image

from software_app.core.external_tools import find_ffmpeg
from software_app.core.http_transfer import download_http_file
from software_app.core.media_process import run_media_process
from software_app.core.output_lock import output_paths_lock
from software_app.core.file_conflict import reuse_existing, media_output
from software_app.core.network_policy import HTTP_BACKOFF, retry_after_seconds
from software_app.core.error_policy import InvalidMediaError

from software_app.core.adapter import TaskCancelled
from software_app.core.cookie_store import CookieSession, cookie_jar, load_cookie_records
from software_app.core.response_read import read_response_json


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}

# Successful resolutions expire; failed lookups must never become permission
# to fetch an unvalidated destination.
DNS_CACHE_TTL = 60.0
DNS_CACHE_MAX_ENTRIES = 1024
_dns_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_dns_cache_lock = threading.Lock()


def _resolve_host_ips(hostname: str) -> list[str]:
    """Resolve hostname to IPs with a small in-memory cache."""
    hostname = hostname.rstrip(".").casefold()
    with _dns_cache_lock:
        cached = _dns_cache.get(hostname)
        if cached and cached[0] > time.monotonic():
            return list(cached[1])
        _dns_cache.pop(hostname, None)
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
        ips = list({info[4][0] for info in infos})
    except (socket.gaierror, socket.herror, OSError):
        ips = []
    if ips:
        with _dns_cache_lock:
            now = time.monotonic()
            expired = [key for key, (deadline, _) in _dns_cache.items() if deadline <= now]
            for key in expired:
                del _dns_cache[key]
            if hostname not in _dns_cache and len(_dns_cache) >= DNS_CACHE_MAX_ENTRIES:
                del _dns_cache[next(iter(_dns_cache))]
            _dns_cache[hostname] = (now + DNS_CACHE_TTL, tuple(ips))
    return ips


def validate_external_url(url: str) -> str:
    """Validate that a URL is safe to fetch from this desktop app.

    Checks:
    - scheme must be http or https
    - host must not resolve to a loopback, link-local, private, or reserved IP
      (SSRF defence against crawler-supplied or user-injected URLs)

    Returns the URL unchanged if valid, raises ValueError otherwise.
    """
    if not url or not isinstance(url, str):
        raise ValueError("下载地址为空")
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise ValueError(f"下载地址无法解析: {exc}") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError(f"不支持的下载协议: {parsed.scheme or '（空）'}")
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("下载地址缺少主机名")
    # If the host is already an IP literal, check it directly.
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        pass  # Not an IP literal; resolve via DNS below.
    else:
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"下载地址指向受限网络: {hostname}")
        return url
    ips = _resolve_host_ips(hostname)
    if not ips:
        raise ValueError(f"无法安全解析下载地址: {hostname}")
    for ip_text in ips:
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as exc:
            raise ValueError(f"下载地址返回无效解析结果: {hostname}") from exc
        if not ip.is_global or ip.is_multicast:
            raise ValueError(f"下载地址解析到受限网络: {hostname} -> {ip_text}")
    return url


def _filesystem_text(value: object) -> str:
    """Return stable NFC text without surrogate code points Tk/Windows cannot display."""
    text = unicodedata.normalize("NFC", str(value or ""))
    return "".join("_" if unicodedata.category(char) == "Cs" else char for char in text)


def normalized_path_component(value: object) -> str:
    return _filesystem_text(value)


def _truncate_utf16(text: str, maximum_units: int) -> str:
    result: list[str] = []
    units = 0
    for char in text:
        char_units = len(char.encode("utf-16-le")) // 2
        if units + char_units > maximum_units:
            break
        result.append(char)
        units += char_units
    return "".join(result)


def path_component_error(value: object, max_length: int = 120) -> str:
    """Explain why a user-entered single path component is unsafe cross-platform."""
    raw = _filesystem_text(value)
    if not raw:
        return "文件名不能为空"
    if raw in {".", ".."}:
        return "文件名不能是 . 或 .."
    if INVALID_PATH_CHARS.search(raw):
        return '文件名不能包含 < > : " / \\ | ? * 或控制字符'
    if raw.endswith((" ", ".")):
        return "Windows 文件名不能以空格或句点结尾"
    if raw.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        return f"{raw.split('.', 1)[0]} 是 Windows 保留设备名"
    if len(raw.encode("utf-16-le")) // 2 > max_length:
        return f"文件名过长，最多 {max_length} 个 UTF-16 单元"
    return ""


def safe_component(value: object, fallback: str = "item", max_length: int = 120) -> str:
    name = INVALID_PATH_CHARS.sub("_", _filesystem_text(value)).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    name = _truncate_utf16(name, max_length).rstrip(". ")
    if name.split(".", 1)[0].upper() in WINDOWS_RESERVED_NAMES:
        name = f"_{name}"
    return name or fallback


def platform_output_root(output_dir: Path | str, platform: str) -> Path:
    root = Path(output_dir).expanduser().resolve()
    target = (root / safe_component(platform) / date.today().isoformat()).resolve()
    if root != target and root not in target.parents:
        raise ValueError("输出路径超出指定下载目录")
    target.mkdir(parents=True, exist_ok=True)
    return target


def load_cookie_file(path: Path | str | None) -> dict[str, str]:
    # Compatibility view for name checks only; HTTP sessions use full records.
    return {row["name"]: row["value"] for row in load_cookie_records(path)}


def _parse_cookie_text(raw: str) -> dict[str, str]:
    """Read a copied Cookie request header or a Netscape cookies.txt export."""
    if len(raw.encode("utf-8")) > 10 * 1024 * 1024:
        return {}
    netscape: dict[str, str] = {}
    for line in raw.splitlines():
        value = line.strip()
        if not value or (value.startswith("#") and not value.startswith("#HttpOnly_")):
            continue
        columns = value.split("\t")
        if len(columns) >= 7 and re.fullmatch(r"[^\s=;]+", columns[5]):
            netscape[columns[5]] = columns[6]
    if netscape:
        return netscape

    text = raw.strip()
    header_cookie = ""
    lines = raw.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.casefold() == "cookie:" and index + 1 < len(lines):
            header_cookie = lines[index + 1].strip()
            break
        if stripped.casefold().startswith("cookie:"):
            header_cookie = stripped.split(":", 1)[1].strip()
            break
    if header_cookie:
        text = header_cookie
    elif "\n" in text or ";" not in text:
        return {}
    result: dict[str, str] = {}
    for part in text.replace("\r", "").replace("\n", ";").split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and re.fullmatch(r"[A-Za-z0-9_.-]+", name):
            result[name] = value.strip()
    return result


def load_request_header_profile(path: Path | str | None) -> dict[str, str]:
    """Read only harmless browser request headers used to reproduce a Cloudflare session."""
    if not path:
        return {}
    try:
        raw = Path(path).expanduser().read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    if len(raw.encode("utf-8")) > 10 * 1024 * 1024:
        return {}
    allowed = {
        "accept", "accept-encoding", "accept-language", "cache-control", "pragma", "referer", "user-agent",
        "sec-ch-ua", "sec-ch-ua-arch", "sec-ch-ua-bitness", "sec-ch-ua-full-version",
        "sec-ch-ua-full-version-list", "sec-ch-ua-mobile", "sec-ch-ua-model", "sec-ch-ua-platform",
        "sec-ch-ua-platform-version",
        "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site", "sec-fetch-user",
        "upgrade-insecure-requests", "priority",
    }
    lines = raw.splitlines()
    result: dict[str, str] = {}
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        name = ""
        value = ""
        if line.endswith(":") and index + 1 < len(lines):
            name = line[:-1].strip()
            value = lines[index + 1].strip()
            index += 2
        else:
            name, separator, value = line.partition(":")
            index += 1
            if not separator:
                continue
            name = name.strip()
            value = value.strip()
        if name.casefold() in allowed and value and "\r" not in value and "\n" not in value:
            # DevTools copies may contain request headers followed by response
            # headers. Keep the first occurrence so response Cache-Control does
            # not replace the browser's request value.
            if not any(existing.casefold() == name.casefold() for existing in result):
                result[name] = value
    return result


def make_session(
    *,
    referer: str = "",
    cookie_file: Path | str | None = None,
    proxy_url: str = "",
    extra_headers: dict[str, str] | None = None,
    cookie_domains: dict[str, str] | None = None,
) -> requests.Session:
    session = CookieSession()
    # Network routing is controlled by the app's explicit proxy setting.  In
    # particular, an empty setting must mean a real direct connection instead
    # of silently inheriting stale HTTP(S)_PROXY values from the launcher.
    session.trust_env = False
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    if referer:
        session.headers["Referer"] = referer
    if extra_headers:
        session.headers.update(extra_headers)
    default_domain = str(urlparse(referer).hostname or "") if referer else ""
    session.cookies = cookie_jar(load_cookie_records(cookie_file), default_domain=default_domain,
                                 cookie_domains=cookie_domains, default_secure=urlparse(referer).scheme != "http")
    proxy_url = str(proxy_url or "").strip()
    if proxy_url:
        endpoint = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
        session.proxies.update({"http": endpoint, "https": endpoint})
    return session


def request_get(session, url: str, *, timeout=(10, 45), headers=None, params=None, cancel_event=None):
    from software_app.core.redirect_policy import get_with_redirects

    return get_with_redirects(session, url, timeout=timeout, headers=headers, params=params,
                              cancel_event=cancel_event, validate_url=validate_external_url)


def request_json(session: requests.Session, url: str, timeout: tuple[int, int] = (10, 45), *, cancel_event=None) -> dict:
    response = request_get(session, url, timeout=timeout, cancel_event=cancel_event)
    try:
        if getattr(response, "status_code", 0) in {429, 503}:
            delay = retry_after_seconds(response.headers.get("Retry-After"), 1.0)
            HTTP_BACKOFF.defer(url, delay)
            if isinstance(getattr(response, "url", None), str):
                HTTP_BACKOFF.defer(response.url, delay)
        response.raise_for_status()
        payload = read_response_json(response, cancel_event=cancel_event)
        if not isinstance(payload, dict):
            raise ValueError(f"接口未返回对象: {url}")
        if payload.get("error") is True:
            raise RuntimeError(str(payload.get("message") or "远程接口返回错误"))
        return payload
    finally:
        response.close()


def download_file(
    session: requests.Session,
    url: str,
    destination: Path,
    cancel_event: threading.Event,
    *,
    timeout: tuple[int, int] = (10, 90),
    chunk_size: int = 128 * 1024,
    resume: bool = True,
    overwrite: bool = False,
    retries: int = 2,
    public_referer: str | None = None,
) -> int:
    if public_referer is not None:
        from software_app.core.redirect_policy import anonymous_session

        session = anonymous_session(session)
        if public_referer:
            session.headers["Referer"] = public_referer
    return download_http_file(
        session, url, destination, cancel_event, timeout=timeout, chunk_size=chunk_size,
        validate_url=validate_external_url, resume=resume, overwrite=overwrite, retries=retries,
    )


def extension_from_url(url: str, default: str = ".bin", allowed: Iterable[str] | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if allowed is not None and suffix not in set(allowed):
        return default
    return suffix or default


class MediaDownloadResult(tuple):
    """Keep size/reused tuple compatibility while exposing the actual output path."""
    def __new__(cls, size, reused, path):
        result = super().__new__(cls, (size, reused))
        result.path = path
        return result


@contextmanager
def _image_content_errors():
    try:
        yield
    except (ValueError, SyntaxError, Image.DecompressionBombError) as exc:
        raise InvalidMediaError("图片内容或结构无效") from exc
    except OSError as exc:
        if exc.errno is not None or getattr(exc, "winerror", None) is not None:
            raise
        raise InvalidMediaError("图片内容或结构无效") from exc


def download_media_file(
    session: requests.Session, url: str, source: Path, destination: Path,
    cancel_event: threading.Event, final_suffix: str, *,
    timeout: tuple[int, int] = (10, 90), verify_image: bool = False, conflict_policy: str = "reuse",
    public_referer: str | None = None,
) -> tuple[int, bool]:
    """Lock the entire transfer/conversion pipeline; return (size, reused).

    A second task reuses the completed destination only after the first task has
    finished converting it. Partial network state stays beside the hidden source.
    """
    if source.resolve() == destination.resolve():
        source = destination.with_name(f".{destination.stem}.source{destination.suffix}")
    with media_output(destination, conflict_policy, cancel_event, related_paths=(source,)) as destination:
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        if reuse_existing(destination, conflict_policy):
            if verify_image:
                with _image_content_errors(), Image.open(destination) as opened:
                    opened.verify()
            return MediaDownloadResult(destination.stat().st_size, True, destination)
        try:
            arguments = {"public_referer": public_referer} if public_referer is not None else {}
            download_file(session, url, source, cancel_event, timeout=timeout, overwrite=True, **arguments)
            if verify_image:
                with _image_content_errors(), Image.open(source) as opened:
                    opened.verify()
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            with _image_content_errors() if verify_image else nullcontext():
                convert_media_file(source, destination, final_suffix, cancel_event=cancel_event)
            return MediaDownloadResult(destination.stat().st_size, False, destination)
        finally:
            source.unlink(missing_ok=True)


def write_text_atomic(path: Path, content: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(content, encoding=encoding)
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError as exc:
                if os.name != "nt" or getattr(exc, "winerror", None) not in {5, 32, 33} or attempt == 5:
                    raise
                time.sleep(min(0.4, 0.05 * (2 ** attempt)))
    finally:
        temporary.unlink(missing_ok=True)


OUTPUT_FORMAT_DEFAULTS = {
    "image_format": "png",
    "video_format": "mp4",
    "animation_format": "gif",
    "audio_format": "mp3",
}
OUTPUT_FORMAT_CHOICES = {
    "image_format": {"png", "jpg", "webp", "original"},
    "video_format": {"mp4", "original"},
    "animation_format": {"gif", "mp4", "original"},
    "audio_format": {"mp3", "m4a", "original"},
}


def normalize_output_format(key: str, value: object) -> str:
    default = OUTPUT_FORMAT_DEFAULTS[key]
    normalized = str(value or default).strip().lower().lstrip(".")
    if normalized == "jpeg":
        normalized = "jpg"
    return normalized if normalized in OUTPUT_FORMAT_CHOICES[key] else default


def normalize_output_formats(options: dict | None = None) -> dict[str, str]:
    values = options or {}
    return {key: normalize_output_format(key, values.get(key)) for key in OUTPUT_FORMAT_DEFAULTS}


def normalized_media_extension(
    content_type: str,
    source_suffix: str,
    candidate_kind: str = "",
    options: dict | None = None,
) -> str:
    """Return the user-facing output format for a downloaded media resource."""
    content_type = str(content_type or "").lower()
    source_suffix = str(source_suffix or "").lower()
    candidate_kind = str(candidate_kind or "").lower()
    formats = normalize_output_formats(options)
    if content_type == "image/gif" or source_suffix == ".gif" or candidate_kind in {"gif", "animation"}:
        selected = formats["animation_format"]
        return source_suffix if selected == "original" else f".{selected}"
    if content_type.startswith("video/") or candidate_kind == "video":
        selected = formats["video_format"]
        return source_suffix if selected == "original" else f".{selected}"
    if content_type.startswith("audio/") or candidate_kind == "audio":
        selected = formats["audio_format"]
        return source_suffix if selected == "original" else f".{selected}"
    if content_type.startswith("image/") or candidate_kind in {"image", "metadata", "link"}:
        selected = formats["image_format"]
        return source_suffix if selected == "original" else f".{selected}"
    return source_suffix


def convert_media_file(source: Path, destination: Path, media_extension: str, *, cancel_event=None) -> None:
    """Convert a real media file to the selected output format."""
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    if task_cancelled(cancel_event):
        raise TaskCancelled("转换前任务已取消")
    with output_paths_lock((source, destination), cancel_event):
        _convert_media_file(source, destination, media_extension, cancel_event)


def _convert_media_file(source: Path, destination: Path, media_extension: str, cancel_event) -> None:
    source = Path(source)
    destination = Path(destination)
    media_extension = media_extension.lower()
    if task_cancelled(cancel_event):
        raise TaskCancelled("转换前任务已取消")
    if source.suffix.lower() == media_extension:
        os.replace(source, destination)
        return
    if media_extension in {".png", ".jpg", ".jpeg", ".webp"}:
        temporary = destination.with_name(destination.name + ".convert.part")
        output_format = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".webp": "WEBP"}[media_extension]
        try:
            with Image.open(source) as image:
                image.load()
                if output_format == "JPEG":
                    if "A" in image.getbands():
                        background = Image.new("RGB", image.size, "white")
                        background.paste(image, mask=image.getchannel("A"))
                        image = background
                    else:
                        image = image.convert("RGB")
                image.save(temporary, format=output_format, quality=95)
            for attempt in range(5):
                if task_cancelled(cancel_event):
                    raise TaskCancelled("图片转换提交前任务已取消")
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in {32, 33} or attempt == 4:
                        raise
                    if task_wait(cancel_event, 0.1 * (attempt + 1)):
                        raise TaskCancelled("等待转换文件提交期间任务已取消") from exc
            source.unlink(missing_ok=True)
        finally:
            temporary.unlink(missing_ok=True)
        return
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(f"需要 ffmpeg 才能转换为 {media_extension}")
    if media_extension == ".mp3":
        arguments = ["-vn", "-codec:a", "libmp3lame", "-q:a", "2"]
    elif media_extension == ".m4a":
        arguments = ["-vn", "-codec:a", "aac", "-b:a", "192k"]
    elif media_extension == ".mp4":
        arguments = ["-c:v", "libx264", "-c:a", "aac", "-movflags", "+faststart"]
    elif media_extension == ".gif":
        arguments = ["-vf", "fps=12,scale='min(1280,iw)':-2:flags=lanczos", "-loop", "0"]
    else:
        if task_cancelled(cancel_event):
            raise TaskCancelled("转换提交前任务已取消")
        os.replace(source, destination)
        return
    temporary = destination.with_name(f".{destination.stem}.{uuid.uuid4().hex}.convert{media_extension}")
    try:
        completed = run_media_process(
            [ffmpeg, "-y", "-i", str(source), *arguments, str(temporary)],
            cancel_event,
            timeout=1800,
        )
        if completed.returncode != 0 or not temporary.is_file() or not temporary.stat().st_size:
            message = (completed.stderr or completed.stdout or "转换失败").strip().splitlines()
            raise RuntimeError(message[-1] if message else "媒体格式转换失败")
        if task_cancelled(cancel_event):
            raise TaskCancelled("转换提交前任务已取消")
        os.replace(temporary, destination)
        source.unlink(missing_ok=True)
    finally:
        temporary.unlink(missing_ok=True)
