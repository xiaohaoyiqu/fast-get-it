from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from PIL import Image

from software_app.core.external_tools import find_ffmpeg

from software_app.core.adapter import TaskCancelled


INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


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
    if not path:
        return {}
    cookie_path = Path(path).expanduser()
    if not cookie_path.exists():
        return {}
    try:
        raw = cookie_path.read_text(encoding="utf-8-sig")
    except OSError:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _parse_cookie_text(raw)
    if isinstance(payload, dict):
        return {str(key): str(value) for key, value in payload.items() if value is not None}
    if isinstance(payload, list):
        result: dict[str, str] = {}
        for item in payload:
            if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                result[str(item["name"])] = str(item["value"])
        return result
    return {}


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
) -> requests.Session:
    session = requests.Session()
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
    cookies = load_cookie_file(cookie_file)
    if cookies:
        session.cookies.update(cookies)
    proxy_url = str(proxy_url or "").strip()
    if proxy_url:
        endpoint = proxy_url if "://" in proxy_url else f"http://{proxy_url}"
        session.proxies.update({"http": endpoint, "https": endpoint})
    return session


def request_json(session: requests.Session, url: str, timeout: tuple[int, int] = (10, 45)) -> dict:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"接口未返回对象: {url}")
    if payload.get("error") is True:
        raise RuntimeError(str(payload.get("message") or "远程接口返回错误"))
    return payload


def download_file(
    session: requests.Session,
    url: str,
    destination: Path,
    cancel_event: threading.Event,
    *,
    timeout: tuple[int, int] = (10, 90),
    chunk_size: int = 128 * 1024,
) -> int:
    if cancel_event.is_set():
        raise TaskCancelled("任务已取消")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    try:
        with session.get(url, stream=True, timeout=timeout) as response:
            response.raise_for_status()
            with temporary.open("wb") as output:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if cancel_event.is_set():
                        raise TaskCancelled("任务已取消")
                    if chunk:
                        output.write(chunk)
        os.replace(temporary, destination)
        return destination.stat().st_size
    finally:
        if temporary.exists():
            try:
                temporary.unlink()
            except OSError:
                pass


def extension_from_url(url: str, default: str = ".bin", allowed: Iterable[str] | None = None) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if allowed is not None and suffix not in set(allowed):
        return default
    return suffix or default


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


def convert_media_file(source: Path, destination: Path, media_extension: str) -> None:
    """Convert a real media file to the selected output format."""
    source = Path(source)
    destination = Path(destination)
    media_extension = media_extension.lower()
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
                try:
                    os.replace(temporary, destination)
                    break
                except PermissionError as exc:
                    if getattr(exc, "winerror", None) not in {32, 33} or attempt == 4:
                        raise
                    time.sleep(0.1 * (attempt + 1))
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
        os.replace(source, destination)
        return
    completed = subprocess.run(
        [ffmpeg, "-y", "-i", str(source), *arguments, str(destination)],
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if completed.returncode != 0 or not destination.is_file():
        message = (completed.stderr or completed.stdout or "转换失败").strip().splitlines()
        raise RuntimeError(message[-1] if message else "媒体格式转换失败")
    source.unlink(missing_ok=True)
