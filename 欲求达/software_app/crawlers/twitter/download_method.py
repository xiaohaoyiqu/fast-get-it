
from software_app.core.task_control import task_cancelled, task_wait
import hashlib
import copy
from contextlib import ExitStack, contextmanager
from io import BytesIO
import json
import math
import os
import random
import re
import shutil
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from weakref import WeakValueDictionary
from typing import Callable

import requests
from PIL import Image, UnidentifiedImageError

from software_app.core.external_tools import find_ffmpeg
from software_app.core.bandwidth import limited_chunks, limited_content
from software_app.core.output_lock import output_path_lock
from software_app.core.adapter import TaskCancelled
from software_app.core.http_transfer import download_http_file
from software_app.core.file_conflict import conflict_policy, reuse_existing, media_output
from software_app.core.network_policy import HTTP_BACKOFF, retry_after_seconds
from software_app.core.error_policy import classify_error, http_error_info
from software_app.crawlers.common import validate_external_url


img_connections = 5  # 定义最大线程数,可根据网速修改
img_sema = threading.BoundedSemaphore(img_connections)  # 或使用Semaphore方法
video_connections = 2  # 定义最大线程数,可根据网速修改
video_sema = threading.BoundedSemaphore(video_connections)  # 或使用Semaphore方法

REQUEST_TIMEOUT = (10, 60)
MAX_RETRIES = 5
RETRY_BACKOFF_BASE = 1.0
RETRY_BACKOFF_MAX = 15.0
REQUEST_PROXIES = None
REQUEST_PROXY_SOURCE = "direct"
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://x.com/",
}
INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


@dataclass(frozen=True)
class DownloadNetworkConfig:
    max_retries: int
    timeout: tuple[int, int]
    proxies: tuple[tuple[str, str], ...]
    proxy_source: str
    backoff_base: float
    backoff_max: float
    file_conflict: str = "reuse"
    on_error: Callable | None = None


class _TransferSession:
    """Use a task's explicit network snapshot with the shared HTTP transfer engine."""
    def __init__(self, network):
        self.network = network
        self.cross_origin = False

    def for_redirect(self):
        view = copy.copy(self)
        view.cross_origin = True
        return view

    def get(self, url, *, headers, **kwargs):
        if self.cross_origin:
            # Suppress netrc origin auth; proxy transport auth is independent.
            kwargs["auth"] = lambda request: request
        return requests.get(
            url, headers={**REQUEST_HEADERS, **headers},
            proxies=dict(self.network.proxies) or None, **kwargs,
        )


class _RecordState:
    def __init__(self):
        self.lock = threading.RLock()
        self.items = {}
        self.signature = None


_record_locks = WeakValueDictionary()
_record_locks_guard = threading.Lock()
RECORD_LOCK_TIMEOUT = 10.0


class JsonDownloadRecord:
    """Cooperating processes serialize the entire read/modify/replace operation."""
    items_type = dict

    def __init__(self, path):
        self.path = Path(path).expanduser().resolve()
        key = (os.path.normcase(str(self.path)), self.items_type)
        with _record_locks_guard:
            state = _record_locks.get(key)
            if state is None:
                state = _RecordState()
                _record_locks[key] = state
            self._state = state
        self._lock = state.lock
        self.items = self.items_type()
        with self._transaction():
            self._load()

    @contextmanager
    def _transaction(self):
        # Always acquire kernel locks before cache locks, including adapter
        # operations that already hold a canonically ordered group of paths.
        with output_path_lock(self.path, timeout=RECORD_LOCK_TIMEOUT), self._lock:
            yield

    def _load(self, *, force=False, strict=False):
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            self._state.items = self.items_type()
            self._state.signature = None
            self.items = self._state.items
            return
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        if not force and self._state.signature == signature:
            self.items = self._state.items
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("记录必须是 JSON 对象")
            items = data.get("items", data if self.items_type is dict else None)
            if not isinstance(items, self.items_type):
                raise ValueError("items 类型无效")
        except (UnicodeError, ValueError) as exc:
            if strict:
                raise ValueError(f"下载记录格式损坏，未覆盖原文件；请先备份并修复：{self.path}") from exc
            print(f"下载记录读取失败，保留原文件: {self.path}")
            items = self.items_type()
        self._state.items = items
        self._state.signature = signature
        self.items = items

    def snapshot(self):
        with self._transaction():
            self._load()
            return copy.deepcopy(self.items)

    def validate(self):
        with self._transaction():
            self._load(force=True, strict=True)

    def update(self, mutate, *, force_write=False):
        """Reload under the process lock; publish the cache only after commit."""
        with self._transaction():
            self._load(force=True, strict=True)
            original = self.items
            self.items = copy.deepcopy(original)
            try:
                result = mutate(self.items)
                if force_write or self.items != original:
                    self._save_locked()
                return result
            finally:
                self.items = self._state.items

    def clear(self):
        def mutate(items):
            count = len(items)
            items.clear()
            return count

        return self.update(mutate, force_write=True)

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent,
                prefix=f".{self.path.name}.", suffix=".tmp", delete=False,
            ) as file:
                temporary = Path(file.name)
                json.dump({"version": 1, "items": self.items}, file, ensure_ascii=False, indent=2)
                file.write("\n")
            replace_with_retry(temporary, self.path)
            stat = self.path.stat()
            self._state.items = self.items
            self._state.signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


def short_error(error):
    message = str(error).strip().splitlines()
    if message:
        return message[0]
    return error.__class__.__name__


class DownloadStats:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts = {
            "queued_image": 0,
            "queued_video": 0,
            "queued_gif": 0,
            "queued_audio": 0,
            "duplicate_queue": 0,
            "skipped_record": 0,
            "skipped_existing": 0,
            "success_image": 0,
            "success_video": 0,
            "success_gif": 0,
            "success_audio": 0,
            "failed_image": 0,
            "failed_video": 0,
            "failed_gif": 0,
            "failed_audio": 0,
            "empty_url": 0,
            "converted_gif": 0,
            "skipped_gif_conversion": 0,
            "failed_gif_conversion": 0,
        }

    def inc(self, key, amount=1):
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + amount

    def snapshot(self):
        with self._lock:
            return dict(self._counts)


class JsonListRecord(JsonDownloadRecord):
    items_type = list


class DownloadRecord(JsonDownloadRecord):
    def has(self, url):
        with self._transaction():
            self._load()
            return url in self.items

    def has_existing_file(self, url):
        with self._transaction():
            self._load()
            item = self.items.get(url)
            filename = item.get("file") if isinstance(item, dict) else None
        if not isinstance(filename, str) or not filename:
            return False
        try:
            return Path(filename).expanduser().is_file()
        except (OSError, ValueError):
            return False

    def mark_success(self, url, media_type, file_path):
        def mutate(items):
            items[url] = {
                "type": media_type,
                "file": str(Path(file_path).expanduser().resolve()),
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        self.update(mutate)

class PendingMediaTracker:
    """Thread-safe record of media URLs queued for download but not yet completed.

    Used at task end to report how many items did not finish and why, and to
    optionally write a plain-text list (filename + URL) for manual recovery.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._items: dict[tuple[str, str], dict] = {}

    def add(self, url: str, media_type: str, filename_base: str = "") -> None:
        if not url:
            return
        with self._lock:
            key = (url, media_type)
            if key not in self._items:
                self._items[key] = {
                    "media_type": media_type,
                    "filename_base": filename_base or "",
                    "reason": "",
                    "status": "pending",
                }

    def complete(self, url: str, media_type: str | None = None) -> None:
        if not url:
            return
        with self._lock:
            keys = [(url, media_type)] if media_type is not None else [key for key in self._items if key[0] == url]
            for key in keys:
                self._items.pop(key, None)

    def fail(self, url: str, reason: str, media_type: str | None = None) -> None:
        if not url:
            return
        with self._lock:
            keys = [(url, media_type)] if media_type is not None else [key for key in self._items if key[0] == url]
            for key in keys:
                item = self._items.get(key)
                if item is not None:
                    item["reason"] = reason or "未知原因"
                    item["status"] = "failed"

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [dict(item, url=key[0]) for key, item in self._items.items()]

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def replace_with_retry(source: Path, destination: Path, max_attempts: int = 5) -> None:
    """Atomically replace destination with source, retrying Windows file-lock errors.

    A locked destination (antivirus, Explorer preview, search indexer) is the most
    common cause of "download succeeded but task failed" on Windows.  Retrying a
    few times with a short backoff usually lets the lock clear; if it still fails,
    the caller can report a clear "rename failed" reason instead of crashing the
    whole task with a raw PermissionError.
    """
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        try:
            os.replace(source, destination)
            return
        except PermissionError as exc:
            last_error = exc
            if getattr(exc, "winerror", None) not in {32, 33} or attempt == max_attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
        except OSError as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
    if last_error is not None:
        raise last_error


class DownloadFailureRecord(JsonDownloadRecord):
    def mark_failed(self, url, media_type, reason):
        def mutate(items):
            item = items.get(url, {})
            attempts = int(item.get("attempts", 0)) + 1
            items[url] = {
                "type": media_type,
                "reason": reason,
                "attempts": attempts,
                "last_failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        self.update(mutate)

    def remove(self, url):
        self.update(lambda items: items.pop(url, None))

def normalize_proxy_url(value):
    value = str(value or "").strip()
    if not value:
        return ""
    return value if "://" in value else f"http://{value}"


def proxies_from_windows_proxy_server(proxy_server):
    """Convert WinINET's proxy syntax to Requests' proxy mapping."""
    proxy_server = str(proxy_server or "").strip()
    if not proxy_server:
        return {}

    if "=" not in proxy_server:
        endpoint = normalize_proxy_url(proxy_server)
        return {"http": endpoint, "https": endpoint}

    proxies = {}
    for item in proxy_server.split(";"):
        if "=" not in item:
            continue
        protocol, endpoint = item.split("=", 1)
        protocol = protocol.strip().lower()
        endpoint = normalize_proxy_url(endpoint)
        if protocol in {"http", "https"} and endpoint:
            proxies[protocol] = endpoint

    if "https" not in proxies and "http" in proxies:
        proxies["https"] = proxies["http"]
    return proxies


def windows_system_proxies():
    """Read the current user's WinINET proxy without exposing its address."""
    if os.name != "nt":
        return {}

    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            enabled = int(winreg.QueryValueEx(key, "ProxyEnable")[0])
            proxy_server = winreg.QueryValueEx(key, "ProxyServer")[0]
    except (ImportError, OSError, TypeError, ValueError):
        return {}

    return proxies_from_windows_proxy_server(proxy_server) if enabled else {}


def proxy_mapping(proxy_url):
    endpoint = normalize_proxy_url(proxy_url)
    return {"http": endpoint, "https": endpoint} if endpoint else {}


def configure_downloads(
    max_retries=None,
    connect_timeout=None,
    read_timeout=None,
    proxy_url=None,
    use_system_proxy=None,
    retry_backoff_base=None,
    retry_backoff_max=None,
    file_conflict=None,
    on_error=None,
):
    """Build a task-owned snapshot; configuring one task never changes another."""
    connect = REQUEST_TIMEOUT[0] if connect_timeout is None else int(connect_timeout)
    read = REQUEST_TIMEOUT[1] if read_timeout is None else int(read_timeout)
    base = RETRY_BACKOFF_BASE if retry_backoff_base is None else float(retry_backoff_base)
    maximum = RETRY_BACKOFF_MAX if retry_backoff_max is None else float(retry_backoff_max)
    if not math.isfinite(base) or not math.isfinite(maximum):
        raise ValueError("重试等待时间必须是有限数值")
    base = max(0.1, base)
    maximum = max(base, maximum)
    explicit_proxies = proxy_mapping(proxy_url)
    if explicit_proxies:
        proxies = explicit_proxies
        source = "configured"
    elif use_system_proxy:
        proxies = windows_system_proxies()
        source = "windows-system" if proxies else "direct"
    else:
        proxies = {}
        source = "direct"
    network = DownloadNetworkConfig(
        max_retries=max(1, min(10, int(MAX_RETRIES if max_retries is None else max_retries))),
        timeout=(max(1, connect), max(1, read)), proxies=tuple(proxies.items()),
        proxy_source=source, backoff_base=base, backoff_max=maximum,
        file_conflict=conflict_policy(file_conflict),
        on_error=on_error,
    )
    print(f"下载网络: {source}")
    return network


def _default_network_config():
    return DownloadNetworkConfig(
        max_retries=max(1, int(MAX_RETRIES)), timeout=REQUEST_TIMEOUT,
        proxies=tuple((REQUEST_PROXIES or {}).items()), proxy_source=REQUEST_PROXY_SOURCE,
        backoff_base=RETRY_BACKOFF_BASE, backoff_max=RETRY_BACKOFF_MAX,
    )


def retry_delay(attempt, retry_after=None, network_config=None):
    network = network_config or _default_network_config()
    exponential = network.backoff_base * (2 ** max(0, attempt - 1))
    fallback = min(network.backoff_max, exponential) + random.uniform(0.0, 0.25)
    return retry_after_seconds(retry_after, fallback)


def request_with_retries(src, stream=False, cancel_event=None, network_config=None):
    network = network_config or _default_network_config()
    for attempt in range(1, network.max_retries + 1):
        if cancel_event is not None and task_cancelled(cancel_event):
            return None
        response = None
        retry_after = None
        try:
            HTTP_BACKOFF.wait(src, cancel_event)
            response = requests.get(
                src,
                stream=stream,
                timeout=network.timeout,
                headers=REQUEST_HEADERS,
                proxies=dict(network.proxies) or None,
            )
            if response.status_code == 200:
                successful_response = response
                response = None
                return successful_response

            status_code = response.status_code
            retry_after = response.headers.get("Retry-After")
            info = http_error_info(status_code)
            if status_code in {429, 503}:
                final_url = getattr(response, "url", None)
                delay = retry_delay(attempt, retry_after, network)
                HTTP_BACKOFF.defer(src, delay)
                if isinstance(final_url, str):
                    HTTP_BACKOFF.defer(final_url, delay)
            response.close()
            response = None
            if network.on_error:
                network.on_error(info, attempt, network.max_retries)
            if not info.retryable:
                print(f"[{info.label}] 状态码 {status_code}，停止重试")
                return None
            print(f"[{info.label}] 状态码 {status_code}，重试 {attempt}/{network.max_retries}")
        except TaskCancelled:
            return None
        except requests.exceptions.RequestException as error:
            info = classify_error(error)
            if network.on_error:
                network.on_error(info, attempt, network.max_retries)
            print(f"[{info.label}] 请求失败，尝试 {attempt}/{network.max_retries}")
            if not info.retryable:
                return None
        finally:
            if response is not None:
                response.close()

        if attempt < network.max_retries:
            delay = retry_delay(attempt, retry_after, network)
            if cancel_event is not None:
                if task_wait(cancel_event, delay):
                    return None
            else:
                time.sleep(delay)

    return None


def safe_filename(value, fallback):
    value = unicodedata.normalize("NFC", unquote(value or "")).strip().strip(".")
    value = "".join("_" if unicodedata.category(char) == "Cs" else char for char in value)
    value = INVALID_FILENAME_CHARS.sub("_", value)
    value = re.sub(r"\s+", " ", value).strip()
    if not value:
        value = fallback
    shortened: list[str] = []
    utf16_units = 0
    for char in value:
        char_units = len(char.encode("utf-16-le")) // 2
        if utf16_units + char_units > 120:
            break
        shortened.append(char)
        utf16_units += char_units
    value = "".join(shortened).rstrip(". ") or fallback
    if value.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
    }:
        value = f"_{value}"
    return value


def media_filename(src, default_ext, filename_base=None, force_ext=False):
    parsed = urlparse(src)
    basename = os.path.basename(parsed.path)
    stem, ext = os.path.splitext(basename)
    query = parse_qs(parsed.query)

    if not force_ext and not ext and query.get("format"):
        ext = "." + query["format"][0].split(",", 1)[0].strip(".")
    if force_ext or not ext:
        ext = default_ext

    fallback = hashlib.sha1(src.encode("utf-8")).hexdigest()[:16]
    if filename_base:
        stem = safe_filename(filename_base, fallback)
    else:
        stem = safe_filename(stem or basename, fallback)
    ext = safe_filename(ext, default_ext)
    if not ext.startswith("."):
        ext = "." + ext
    return stem + ext.lower()


def image_content_as_png(content):
    """Decode image bytes and return a real PNG, regardless of the source format."""
    return image_content_in_format(content, "png")[0]


def normalize_image_format(value):
    value = str(value or "png").strip().lower().lstrip(".")
    if value == "jpeg":
        value = "jpg"
    return value if value in {"png", "jpg", "webp", "original"} else "png"


def image_content_in_format(content, image_format="png"):
    image_format = normalize_image_format(image_format)
    with Image.open(BytesIO(content)) as image:
        image.load()
        if image_format == "original":
            source_format = str(image.format or "png").lower()
            image_format = "jpg" if source_format in {"jpg", "jpeg"} else source_format
            if image_format not in {"png", "jpg", "webp"}:
                image_format = "png"
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
        if image_format == "jpg":
            if "A" in image.getbands():
                background = Image.new("RGB", image.size, "white")
                background.paste(image, mask=image.getchannel("A"))
                image = background
            else:
                image = image.convert("RGB")
        output = BytesIO()
        image.save(output, format={"png": "PNG", "jpg": "JPEG", "webp": "WEBP"}[image_format], quality=95)
        return output.getvalue(), image_format


def normalize_audio_format(value):
    value = str(value or "mp3").strip().lower().lstrip(".")
    if value not in {"m4a", "mp3"}:
        return "mp3"
    return value


def record_identifier(src, media_type):
    if media_type == "audio":
        return f"{src}#audio"
    return src


def record_skip(record_url, record, stats, display_url=None):
    if record and record.has_existing_file(record_url):
        stats.inc("skipped_record")
        print(f"Already in download record, skip: {display_url or record_url}")
        return True
    return False


def mark_existing(record_url, record, failure_record, stats, media_type, file_path):
    stats.inc("skipped_existing")
    if record:
        record.mark_success(record_url, media_type, file_path)
    if failure_record:
        failure_record.remove(record_url)


def ffmpeg_error_text(completed):
    text = (completed.stderr or completed.stdout or "").strip().splitlines()
    return text[-1] if text else f"exit code {completed.returncode}"


def run_ffmpeg(args):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return False, "ffmpeg not found"

    completed = subprocess.run(
        [ffmpeg, "-y", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
    )
    if completed.returncode == 0:
        return True, ""
    return False, ffmpeg_error_text(completed)


def import_video_file_clip():
    try:
        from moviepy import VideoFileClip
    except ImportError:
        from moviepy.editor import VideoFileClip
    return VideoFileClip


def extract_audio_with_moviepy(source_path, audio_path):
    VideoFileClip = import_video_file_clip()
    clip = VideoFileClip(str(source_path))
    try:
        if not clip.audio:
            raise RuntimeError("no audio track")
        clip.audio.write_audiofile(str(audio_path))
    finally:
        clip.close()


def extract_audio_file(source_path, audio_path, audio_format="mp3"):
    audio_format = normalize_audio_format(audio_format)
    if audio_format == "mp3":
        args = ["-i", str(source_path), "-vn", "-codec:a", "libmp3lame", "-q:a", "2", str(audio_path)]
    else:
        args = ["-i", str(source_path), "-vn", "-acodec", "copy", str(audio_path)]

    ok, error = run_ffmpeg(args)
    if ok:
        return True, ""

    try:
        extract_audio_with_moviepy(source_path, audio_path)
        return True, ""
    except Exception as moviepy_error:
        return False, f"{error}; moviepy: {moviepy_error}"


def resize_clip_for_gif(clip, width):
    if not width or int(width) <= 0:
        return clip
    width = int(width)
    if hasattr(clip, "resize"):
        return clip.resize(width=width)
    if hasattr(clip, "resized"):
        return clip.resized(width=width)
    return clip


def convert_gif_with_moviepy(source_path, gif_path, fps=12, width=0):
    VideoFileClip = import_video_file_clip()
    clip = VideoFileClip(str(source_path))
    try:
        clip = resize_clip_for_gif(clip, width)
        clip.write_gif(str(gif_path), fps=int(fps))
    finally:
        clip.close()


def convert_mp4_to_gif(source_path, gif_path, fps=12, width=0):
    filters = [f"fps={max(1, int(fps or 12))}"]
    try:
        width = int(width or 0)
    except (TypeError, ValueError):
        width = 0
    if width > 0:
        filters.append(f"scale={width}:-1:flags=lanczos")

    ok, error = run_ffmpeg(["-i", str(source_path), "-vf", ",".join(filters), "-loop", "0", str(gif_path)])
    if ok:
        return True, ""

    try:
        convert_gif_with_moviepy(source_path, gif_path, fps=fps, width=width)
        return True, ""
    except Exception as moviepy_error:
        return False, f"{error}; moviepy: {moviepy_error}"


def finalize_gif_conversion(video_path, stats, convert_gif=True, keep_gif_mp4=False, gif_fps=12, gif_width=0, file_conflict="reuse"):
    if not convert_gif:
        return video_path

    gif_path = video_path.with_suffix(".gif")
    if reuse_existing(gif_path, file_conflict):
        stats.inc("skipped_gif_conversion")
    else:
        print(f"{gif_path.name} is converting to gif")
        temporary_gif = gif_path.with_name(f".{gif_path.stem}.{time.time_ns()}.convert.gif")
        try:
            ok, error = convert_mp4_to_gif(video_path, temporary_gif, fps=gif_fps, width=gif_width)
            if ok:
                replace_with_retry(temporary_gif, gif_path)
        finally:
            temporary_gif.unlink(missing_ok=True)
        if not ok:
            stats.inc("failed_gif_conversion")
            print(f"GIF conversion failed, keep mp4: {error}")
            return video_path
        stats.inc("converted_gif")

    if not keep_gif_mp4 and video_path.exists():
        try:
            video_path.unlink()
        except OSError as error:
            print(f"Could not delete source mp4 after GIF conversion: {error}")
    return gif_path


def download_pic(
    src,
    name,
    record=None,
    stats=None,
    failure_record=None,
    filename_base=None,
    cancel_event=None,
    image_format="png",
    on_result=None,
    rename_downloads=True,
    delete_source_after_rename=True,
    network_config=None,
):
    stats = stats or DownloadStats()
    if not src:
        stats.inc("empty_url")
        print("Image URL is empty, skip")
        if on_result:
            on_result("image", "failed", None, "媒体地址为空")
        return
    policy = (network_config or _default_network_config()).file_conflict
    if policy == "reuse" and record_skip(src, record, stats):
        if on_result:
            on_result("image", "skipped", None, "下载记录中已存在")
        return

    if cancel_event is not None and task_wait(cancel_event, 0.01):
        return
    while not img_sema.acquire(timeout=0.2):
        if cancel_event is not None and task_cancelled(cancel_event):
            return
    output_locks = ExitStack()
    try:
        if cancel_event is not None and task_cancelled(cancel_event):
            return
        Path(name).mkdir(parents=True, exist_ok=True)
        # Decode and re-encode so the extension always matches the selected format.
        image_format = normalize_image_format(image_format)
        target_format = "png" if image_format == "original" else image_format
        # 原始文件名（不用 filename_base），重命名时作为源文件
        original_name = media_filename(src, f".{target_format}", None, force_ext=True)
        original_path = Path(name) / original_name
        # 目标文件名：开启重命名且有 filename_base 时用日期+ID+索引，否则用原始名
        should_rename = bool(rename_downloads and filename_base)
        image_name = media_filename(src, f".{target_format}", filename_base if should_rename else None, force_ext=True)
        image_path = Path(name) / image_name
        image_path = output_locks.enter_context(media_output(
            image_path, policy, cancel_event,
            companions=lambda path: (path.with_name(f"{path.stem}.original{path.suffix}"),)
            if policy == "rename" and not delete_source_after_rename else (),
            related_paths=lambda path: (original_path, path.with_name(f"{path.stem}.original{path.suffix}")),
        ))
        if policy == "rename":
            original_path = image_path.with_name(f"{image_path.stem}.original{image_path.suffix}")
            should_rename = True
        image_name = image_path.name

        if reuse_existing(image_path, policy):
            print(f"{image_name} already exists.")
            mark_existing(src, record, failure_record, stats, "image", image_path)
            if on_result:
                on_result("image", "skipped", image_path, "文件已存在")
            return

        print(f"{image_name} is downloading")
        response = request_with_retries(src, stream=True, cancel_event=cancel_event, network_config=network_config)
        if response is None:
            if cancel_event is not None and task_cancelled(cancel_event):
                return
            stats.inc("failed_image")
            if failure_record:
                failure_record.mark_failed(src, "image", "max retries exceeded")
            print(f"Image download failed, skip: {src}")
            if on_result:
                on_result("image", "failed", None, "重试后请求仍失败")
            return

        try:
            try:
                output_content, _actual_format = image_content_in_format(limited_content(response, cancel_event), image_format)
            except (UnidentifiedImageError, OSError, ValueError) as error:
                stats.inc("failed_image")
                if failure_record:
                    failure_record.mark_failed(src, "image", f"image conversion failed: {error}")
                print(f"Image conversion failed, skip: {src} ({error})")
                if on_result:
                    on_result("image", "failed", None, f"图片转换失败：{error}")
                return

            keep_original = should_rename and not delete_source_after_rename and original_path != image_path
            write_original = keep_original and not reuse_existing(original_path, policy)
            # Commit from a unique temporary file, preserving the previous image
            # if writing or replacement fails. Renaming need not touch an old original.
            targets = [image_path, *([original_path] if write_original else [])]
            try:
                for target in targets:
                    temporary = None
                    try:
                        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", suffix=".tmp", delete=False) as output:
                            temporary = Path(output.name)
                            output.write(output_content)
                        replace_with_retry(temporary, target)
                    finally:
                        if temporary:
                            temporary.unlink(missing_ok=True)
            except OSError as error:
                stats.inc("failed_image")
                reason = f"写入图片文件失败：{short_error(error)}"
                if failure_record:
                    failure_record.mark_failed(src, "image", reason)
                if on_result:
                    on_result("image", "failed", None, reason)
                return
            stats.inc("success_image")
            if record:
                record.mark_success(src, "image", image_path)
            if failure_record:
                failure_record.remove(src)
            if on_result:
                on_result("image", "completed", image_path, "")
        finally:
            response.close()
    finally:
        try:
            output_locks.close()
        finally:
            img_sema.release()


def download_video(
    src,
    name,
    record=None,
    stats=None,
    media_type="video",
    failure_record=None,
    filename_base=None,
    cancel_event=None,
    convert_gif=True,
    keep_gif_mp4=False,
    gif_fps=12,
    gif_width=0,
    on_result=None,
    rename_downloads=True,
    delete_source_after_rename=True,
    network_config=None,
):
    stats = stats or DownloadStats()
    if media_type not in {"video", "gif"}:
        media_type = "video"
    if not src:
        stats.inc("empty_url")
        print("Video URL is empty, skip")
        if on_result:
            on_result(media_type, "failed", None, "媒体地址为空")
        return

    while not video_sema.acquire(timeout=0.2):
        if cancel_event is not None and task_cancelled(cancel_event):
            return
    output_locks = ExitStack()
    try:
        if cancel_event is not None and task_cancelled(cancel_event):
            return
        Path(name).mkdir(parents=True, exist_ok=True)
        record_url = record_identifier(src, media_type)
        policy = (network_config or _default_network_config()).file_conflict
        # 原始文件名（不用 filename_base），重命名时作为源文件
        original_name = media_filename(src, ".mp4", None)
        original_path = Path(name) / original_name
        # 目标文件名：开启重命名且有 filename_base 时用日期+ID+索引，否则用原始名
        should_rename = bool(rename_downloads and filename_base)
        target_name = media_filename(src, ".mp4", filename_base if should_rename else None)
        video_path = Path(name) / target_name
        gif_path = video_path.with_suffix(".gif") if media_type == "gif" else None
        source_suffix = ".original.mp4" if not delete_source_after_rename else ".source.mp4"
        video_path = output_locks.enter_context(media_output(
            video_path, policy, cancel_event,
            companions=lambda path: (
                *((path.with_suffix(".gif"),) if media_type == "gif" and convert_gif else ()),
                *((path.with_name(f"{path.stem}{source_suffix}"),) if policy == "rename" else ()),
            ),
            related_paths=lambda path: (original_path, path.with_name(f"{path.stem}{source_suffix}")),
        ))
        if policy == "rename":
            original_path = video_path.with_name(f"{video_path.stem}{source_suffix}")
            should_rename = True
        target_name = video_path.name
        gif_path = video_path.with_suffix(".gif") if media_type == "gif" else None

        if policy == "reuse" and record and record.has_existing_file(record_url):
            if media_type == "gif" and convert_gif and video_path.exists() and gif_path and not gif_path.exists():
                final_path = finalize_gif_conversion(video_path, stats, convert_gif, keep_gif_mp4, gif_fps, gif_width)
                record.mark_success(record_url, media_type, final_path)
                if failure_record:
                    failure_record.remove(record_url)
                if on_result:
                    on_result(media_type, "completed", final_path, "")
                return
            stats.inc("skipped_record")
            print(f"Already in download record, skip: {src}")
            if on_result:
                on_result(media_type, "skipped", None, "下载记录中已存在")
            return

        if media_type == "gif" and convert_gif and gif_path and reuse_existing(gif_path, policy):
            print(f"{gif_path.name} already exists.")
            mark_existing(record_url, record, failure_record, stats, media_type, gif_path)
            if on_result:
                on_result(media_type, "skipped", gif_path, "文件已存在")
            return

        if reuse_existing(video_path, policy):
            print(f"{target_name} already exists.")
            final_path = finalize_gif_conversion(video_path, stats, convert_gif and media_type == "gif", keep_gif_mp4, gif_fps, gif_width)
            mark_existing(record_url, record, failure_record, stats, media_type, final_path)
            if on_result:
                on_result(media_type, "skipped", final_path, "文件已存在")
            return

        print(f"{target_name} is downloading")
        network = network_config or _default_network_config()
        try:
            download_http_file(
                _TransferSession(network), src, original_path,
                cancel_event if cancel_event is not None else threading.Event(),
                timeout=network.timeout, chunk_size=128 * 1024,
                validate_url=validate_external_url, retries=network.max_retries - 1,
                overwrite=True, commit=replace_with_retry,
            )
        except TaskCancelled:
            return
        except (requests.RequestException, OSError, ValueError) as error:
            stats.inc(f"failed_{media_type}")
            temporary = original_path.with_name(original_path.name + ".part")
            reason = f"[{classify_error(error).label}] 视频下载或落盘失败：{short_error(error)}"
            if temporary.exists():
                reason += f"；已保留临时文件: {temporary.name}（可验证的部分文件会在重试时续传）"
            if failure_record:
                failure_record.mark_failed(record_url, media_type, reason)
            print(reason)
            if on_result:
                on_result(media_type, "failed", None, reason)
            return
        if should_rename and original_path != video_path and original_path.exists():
            try:
                if delete_source_after_rename:
                    replace_with_retry(original_path, video_path)
                else:
                    shutil.copy2(original_path, video_path)
            except OSError as error:
                stats.inc(f"failed_{media_type}")
                reason = f"重命名到目标文件名失败：{short_error(error)}；原始文件已保留: {original_path.name}"
                if failure_record:
                    failure_record.mark_failed(record_url, media_type, f"rename to target failed: {short_error(error)}")
                print(f"Video rename to target failed, kept original: {src} ({short_error(error)})")
                if on_result:
                    on_result(media_type, "failed", None, reason)
                return
        stats.inc(f"success_{media_type}")
        final_path = finalize_gif_conversion(video_path, stats, convert_gif and media_type == "gif", keep_gif_mp4, gif_fps, gif_width, policy)
        if record:
            record.mark_success(record_url, media_type, final_path)
        if failure_record:
            failure_record.remove(record_url)
        if on_result:
            on_result(media_type, "completed", final_path, "")
    finally:
        try:
            output_locks.close()
        finally:
            video_sema.release()


def download_audio(
    src,
    name,
    record=None,
    stats=None,
    failure_record=None,
    filename_base=None,
    audio_format="mp3",
    cancel_event=None,
    on_result=None,
    network_config=None,
):
    stats = stats or DownloadStats()
    if not src:
        stats.inc("empty_url")
        print("Audio source URL is empty, skip")
        if on_result:
            on_result("audio", "failed", None, "媒体地址为空")
        return

    audio_format = normalize_audio_format(audio_format)
    record_url = record_identifier(src, "audio")
    policy = (network_config or _default_network_config()).file_conflict
    if policy == "reuse" and record_skip(record_url, record, stats, display_url=src):
        if on_result:
            on_result("audio", "skipped", None, "下载记录中已存在")
        return

    while not video_sema.acquire(timeout=0.2):
        if cancel_event is not None and task_cancelled(cancel_event):
            return
    temp_path = None
    converted_path = None
    output_locks = ExitStack()
    try:
        Path(name).mkdir(parents=True, exist_ok=True)
        audio_name = media_filename(src, f".{audio_format}", filename_base, force_ext=True)
        audio_path = Path(name) / audio_name
        audio_path = output_locks.enter_context(media_output(
            audio_path, policy, cancel_event,
            companions=lambda path: (path.with_name(f"{path.stem}.source.mp4"),) if policy == "rename" else (),
            related_paths=lambda path: (path.with_name(f"{path.stem}.source.mp4"),),
        ))
        audio_name = audio_path.name
        temp_path = audio_path.with_name(f"{audio_path.stem}.source.mp4")

        if reuse_existing(audio_path, policy):
            print(f"{audio_name} already exists.")
            mark_existing(record_url, record, failure_record, stats, "audio", audio_path)
            if on_result:
                on_result("audio", "skipped", audio_path, "文件已存在")
            return

        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass

        print(f"{audio_name} source video is downloading for audio extraction")
        response = request_with_retries(src, stream=True, cancel_event=cancel_event, network_config=network_config)
        if response is None:
            if cancel_event is not None and task_cancelled(cancel_event):
                return
            stats.inc("failed_audio")
            if failure_record:
                failure_record.mark_failed(record_url, "audio", "max retries exceeded")
            print(f"Audio source download failed, skip: {src}")
            if on_result:
                on_result("audio", "failed", None, "重试后请求仍失败")
            return

        try:
            with temp_path.open("wb") as f:
                for chunk in limited_chunks(response, cancel_event, chunk_size=8192):
                    if cancel_event is not None and task_cancelled(cancel_event):
                        return
                    if chunk:
                        f.write(chunk)
        except OSError as error:
            stats.inc("failed_audio")
            reason = f"写入音频源文件失败（可能被占用）：{short_error(error)}"
            if failure_record:
                failure_record.mark_failed(record_url, "audio", f"temp write failed: {short_error(error)}")
            print(f"Audio temp file write failed, skip: {src} ({short_error(error)})")
            if on_result:
                on_result("audio", "failed", None, reason)
            return
        finally:
            response.close()

        print(f"{audio_name} is extracting audio")
        converted_path = audio_path.with_name(f".{audio_path.stem}.{time.time_ns()}.convert{audio_path.suffix}")
        ok, error = extract_audio_file(temp_path, converted_path, audio_format=audio_format)
        if not ok:
            stats.inc("failed_audio")
            if failure_record:
                failure_record.mark_failed(record_url, "audio", error or "audio extraction failed")
            print(f"Audio extraction failed, skip: {error}")
            if on_result:
                on_result("audio", "failed", None, error or "音频提取失败")
            return

        replace_with_retry(converted_path, audio_path)
        stats.inc("success_audio")
        if record:
            record.mark_success(record_url, "audio", audio_path)
        if failure_record:
            failure_record.remove(record_url)
        if on_result:
            on_result("audio", "completed", audio_path, "")
    finally:
        try:
            if converted_path:
                converted_path.unlink(missing_ok=True)
            if temp_path:
                temp_path.unlink(missing_ok=True)
        finally:
            try:
                output_locks.close()
            finally:
                video_sema.release()
