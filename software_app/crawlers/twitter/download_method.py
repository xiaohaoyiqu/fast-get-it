import hashlib
from io import BytesIO
import json
import os
import random
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import requests
from PIL import Image, UnidentifiedImageError

from software_app.core.external_tools import find_ffmpeg


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


class DownloadRecord:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.items = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            print(f"下载记录读取失败，将重新生成: {self.path}")
            return

        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.items = data["items"]
        elif isinstance(data, dict):
            self.items = data

    def has(self, url):
        with self._lock:
            return url in self.items

    def mark_success(self, url, media_type, file_path):
        with self._lock:
            self.items[url] = {
                "type": media_type,
                "file": str(file_path),
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_locked()

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": self.items,
        }
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


class DownloadFailureRecord:
    def __init__(self, path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.items = {}
        self._load()

    def _load(self):
        if not self.path.exists():
            return

        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            print(f"失败清单读取失败，将重新生成: {self.path}")
            return

        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            self.items = data["items"]
        elif isinstance(data, dict):
            self.items = data

    def mark_failed(self, url, media_type, reason):
        with self._lock:
            item = self.items.get(url, {})
            attempts = int(item.get("attempts", 0)) + 1
            self.items[url] = {
                "type": media_type,
                "reason": reason,
                "attempts": attempts,
                "last_failed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
            self._save_locked()

    def remove(self, url):
        with self._lock:
            if url in self.items:
                self.items.pop(url, None)
                self._save_locked()

    def _save_locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "items": self.items,
        }
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)


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
):
    global MAX_RETRIES, REQUEST_TIMEOUT, REQUEST_PROXIES, REQUEST_PROXY_SOURCE
    global RETRY_BACKOFF_BASE, RETRY_BACKOFF_MAX
    if max_retries is not None:
        MAX_RETRIES = int(max_retries)
    if connect_timeout is not None or read_timeout is not None:
        connect = REQUEST_TIMEOUT[0] if connect_timeout is None else int(connect_timeout)
        read = REQUEST_TIMEOUT[1] if read_timeout is None else int(read_timeout)
        REQUEST_TIMEOUT = (connect, read)

    if retry_backoff_base is not None:
        RETRY_BACKOFF_BASE = max(0.1, float(retry_backoff_base))
    if retry_backoff_max is not None:
        RETRY_BACKOFF_MAX = max(RETRY_BACKOFF_BASE, float(retry_backoff_max))

    explicit_proxies = proxy_mapping(proxy_url)
    if explicit_proxies:
        REQUEST_PROXIES = explicit_proxies
        REQUEST_PROXY_SOURCE = "configured"
    elif use_system_proxy:
        REQUEST_PROXIES = windows_system_proxies() or None
        REQUEST_PROXY_SOURCE = "windows-system" if REQUEST_PROXIES else "direct"
    else:
        REQUEST_PROXIES = None
        REQUEST_PROXY_SOURCE = "direct"

    print(f"下载网络: {REQUEST_PROXY_SOURCE}")


def retry_delay(attempt, retry_after=None):
    if retry_after:
        try:
            return min(RETRY_BACKOFF_MAX, max(0.0, float(retry_after)))
        except (TypeError, ValueError):
            pass
    exponential = RETRY_BACKOFF_BASE * (2 ** max(0, attempt - 1))
    return min(RETRY_BACKOFF_MAX, exponential) + random.uniform(0.0, 0.25)


def request_with_retries(src, stream=False, cancel_event=None):
    for attempt in range(1, MAX_RETRIES + 1):
        if cancel_event is not None and cancel_event.is_set():
            return None
        response = None
        retry_after = None
        try:
            response = requests.get(
                src,
                stream=stream,
                timeout=REQUEST_TIMEOUT,
                headers=REQUEST_HEADERS,
                proxies=REQUEST_PROXIES,
            )
            if response.status_code == 200:
                successful_response = response
                response = None
                return successful_response

            status_code = response.status_code
            retry_after = response.headers.get("Retry-After")
            response.close()
            response = None
            print(f"状态码 {status_code}，重试 {attempt}/{MAX_RETRIES}")
        except requests.exceptions.RequestException as error:
            print(f"发生错误，重试 {attempt}/{MAX_RETRIES}: {error}")
        finally:
            if response is not None:
                response.close()

        if attempt < MAX_RETRIES:
            delay = retry_delay(attempt, retry_after)
            if cancel_event is not None:
                if cancel_event.wait(delay):
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
    if record and record.has(record_url):
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


def finalize_gif_conversion(video_path, stats, convert_gif=True, keep_gif_mp4=False, gif_fps=12, gif_width=0):
    if not convert_gif:
        return video_path

    gif_path = video_path.with_suffix(".gif")
    if gif_path.exists():
        stats.inc("skipped_gif_conversion")
    else:
        print(f"{gif_path.name} is converting to gif")
        ok, error = convert_mp4_to_gif(video_path, gif_path, fps=gif_fps, width=gif_width)
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
):
    stats = stats or DownloadStats()
    if not src:
        stats.inc("empty_url")
        print("Image URL is empty, skip")
        if on_result:
            on_result("image", "failed", None, "媒体地址为空")
        return
    if record_skip(src, record, stats):
        if on_result:
            on_result("image", "skipped", None, "下载记录中已存在")
        return

    if cancel_event is not None and cancel_event.wait(0.01):
        return
    while not img_sema.acquire(timeout=0.2):
        if cancel_event is not None and cancel_event.is_set():
            return
    try:
        if cancel_event is not None and cancel_event.is_set():
            return
        Path(name).mkdir(parents=True, exist_ok=True)
        # Decode and re-encode so the extension always matches the selected format.
        image_format = normalize_image_format(image_format)
        target_format = "png" if image_format == "original" else image_format
        image_name = media_filename(src, f".{target_format}", filename_base, force_ext=True)
        image_path = Path(name) / image_name

        if image_path.exists():
            print(f"{image_name} already exists.")
            mark_existing(src, record, failure_record, stats, "image", image_path)
            if on_result:
                on_result("image", "skipped", image_path, "文件已存在")
            return

        print(f"{image_name} is downloading")
        response = request_with_retries(src, cancel_event=cancel_event)
        if response is None:
            if cancel_event is not None and cancel_event.is_set():
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
                output_content, _actual_format = image_content_in_format(response.content, image_format)
            except (UnidentifiedImageError, OSError, ValueError) as error:
                stats.inc("failed_image")
                if failure_record:
                    failure_record.mark_failed(src, "image", f"image conversion failed: {error}")
                print(f"Image conversion failed, skip: {src} ({error})")
                if on_result:
                    on_result("image", "failed", None, f"图片转换失败：{error}")
                return

            with image_path.open("wb") as f:
                f.write(output_content)
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
        if cancel_event is not None and cancel_event.is_set():
            return
    try:
        if cancel_event is not None and cancel_event.is_set():
            return
        Path(name).mkdir(parents=True, exist_ok=True)
        record_url = record_identifier(src, media_type)
        video_name = media_filename(src, ".mp4", filename_base)
        video_path = Path(name) / video_name
        gif_path = video_path.with_suffix(".gif") if media_type == "gif" else None

        if record and record.has(record_url):
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

        if media_type == "gif" and convert_gif and gif_path and gif_path.exists():
            print(f"{gif_path.name} already exists.")
            mark_existing(record_url, record, failure_record, stats, media_type, gif_path)
            if on_result:
                on_result(media_type, "skipped", gif_path, "文件已存在")
            return

        if video_path.exists():
            print(f"{video_name} already exists.")
            final_path = finalize_gif_conversion(video_path, stats, convert_gif and media_type == "gif", keep_gif_mp4, gif_fps, gif_width)
            mark_existing(record_url, record, failure_record, stats, media_type, final_path)
            if on_result:
                on_result(media_type, "skipped", final_path, "文件已存在")
            return

        print(f"{video_name} is downloading")
        response = request_with_retries(src, stream=True, cancel_event=cancel_event)
        if response is None:
            if cancel_event is not None and cancel_event.is_set():
                return
            stats.inc(f"failed_{media_type}")
            if failure_record:
                failure_record.mark_failed(record_url, media_type, "max retries exceeded")
            print(f"Video download failed, skip: {src}")
            if on_result:
                on_result(media_type, "failed", None, "重试后请求仍失败")
            return

        temporary = video_path.with_name(video_path.name + ".part")
        try:
            with temporary.open("wb") as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if chunk:
                        f.write(chunk)
            os.replace(temporary, video_path)
            stats.inc(f"success_{media_type}")
            final_path = finalize_gif_conversion(video_path, stats, convert_gif and media_type == "gif", keep_gif_mp4, gif_fps, gif_width)
            if record:
                record.mark_success(record_url, media_type, final_path)
            if failure_record:
                failure_record.remove(record_url)
            if on_result:
                on_result(media_type, "completed", final_path, "")
        finally:
            response.close()
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
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
    if record_skip(record_url, record, stats, display_url=src):
        if on_result:
            on_result("audio", "skipped", None, "下载记录中已存在")
        return

    while not video_sema.acquire(timeout=0.2):
        if cancel_event is not None and cancel_event.is_set():
            return
    temp_path = None
    try:
        Path(name).mkdir(parents=True, exist_ok=True)
        audio_name = media_filename(src, f".{audio_format}", filename_base, force_ext=True)
        audio_path = Path(name) / audio_name
        temp_path = audio_path.with_name(f"{audio_path.stem}.source.mp4")

        if audio_path.exists():
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
        response = request_with_retries(src, stream=True, cancel_event=cancel_event)
        if response is None:
            if cancel_event is not None and cancel_event.is_set():
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
                for chunk in response.iter_content(chunk_size=8192):
                    if cancel_event is not None and cancel_event.is_set():
                        return
                    if chunk:
                        f.write(chunk)
        finally:
            response.close()

        print(f"{audio_name} is extracting audio")
        ok, error = extract_audio_file(temp_path, audio_path, audio_format=audio_format)
        if not ok:
            stats.inc("failed_audio")
            if audio_path.exists():
                try:
                    audio_path.unlink()
                except OSError:
                    pass
            if failure_record:
                failure_record.mark_failed(record_url, "audio", error or "audio extraction failed")
            print(f"Audio extraction failed, skip: {error}")
            if on_result:
                on_result("audio", "failed", None, error or "音频提取失败")
            return

        stats.inc("success_audio")
        if record:
            record.mark_success(record_url, "audio", audio_path)
        if failure_record:
            failure_record.remove(record_url)
        if on_result:
            on_result("audio", "completed", audio_path, "")
    finally:
        if temp_path and temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        video_sema.release()
