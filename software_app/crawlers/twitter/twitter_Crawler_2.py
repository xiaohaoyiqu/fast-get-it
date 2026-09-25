import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from selenium.common.exceptions import TimeoutException
from selenium.webdriver.common.by import By

try:
    from .download_method import DownloadFailureRecord, DownloadRecord, configure_downloads
    from .driver_init import cookies_web, get_twitter_name, initialize_driver
    from .manga_downloader import download_media
except ImportError:  # Preserve direct script execution.
    from download_method import DownloadFailureRecord, DownloadRecord, configure_downloads
    from driver_init import cookies_web, get_twitter_name, initialize_driver
    from manga_downloader import download_media


DEFAULT_CONFIG = {
    "cookie_file": "X_cookie.json",
    "targets": [
        {"target": "", "types": ""},
        {"target": "", "types": ""},
        {"target": "", "types": ""},
        {"target": "", "types": ""},
        {"target": "", "types": ""},
    ],
    "download_types": "",
    "output_dir": ".",
    "download_record_file": "downloaded_urls.json",
    "failed_url_file": "failed_urls.json",
    "max_users": 5,
    "parallel_users": 1,
    "bootstrap_timeout": 20,
    "page_load_timeout": 30,
    "media_wait_timeout": 30,
    "max_idle_rounds": 6,
    "cells_per_round": 7,
    "stable_scroll_rounds": 2,
    "download_workers": 6,
    "request_connect_timeout": 10,
    "request_read_timeout": 60,
    "max_retries": 5,
    "use_system_proxy": True,
    "proxy_url": "",
    "retry_backoff_base": 1.0,
    "retry_backoff_max": 15.0,
    "image_format": "png",
    "audio_format": "mp3",
    "convert_gif": True,
    "keep_gif_mp4": False,
    "gif_fps": 8,
    "gif_width": 720,
}
CONFIG_FILE = "config.json"
BOOTSTRAP_URL = "https://x.com/"
INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CRAWLER_VERSION = "2026-09-25-auto-refresh-v14"
TWITTER_RESERVED_ROUTES = {"home", "explore", "notifications", "messages", "search", "settings", "compose", "i"}
HELP_EPILOG = """
下载类型:
  1    图片
  2    视频
  3    GIF动图
  4    从视频单独提取音频
  12   图片 + 视频
  13   图片 + GIF
  23   视频 + GIF
  123  图片 + 视频 + GIF
  1234 图片 + 视频 + GIF + 音频

X 当前媒体页入口:
  - 图片 (1): /<用户>/media?filter=photo
  - 视频、GIF、音频 (2/3/4): /<用户>/media
  - 组合类型会依次扫描对应入口，并共用下载记录避免重复下载。

GIF 说明:
  - X 标记为 animated_gif 的媒体通常实际分发为 MP4。
  - 类型 3 默认直接保存该 MP4；仅 --convert-gif 时才转换为 .gif。
  - GIF 转换会明显增大文件体积，建议保留默认设置。

命令行示例:
  python .\\twitter_Crawler_2.py @user1::1 @user2::23
  python .\\twitter_Crawler_2.py https://x.com/user/media::4
  python .\\twitter_Crawler_2.py @user1 @user2 -t 13 --convert-gif

配置示例:
  {
    "targets": [
      {"target": "@user1", "types": "1"},
      {"target": "https://x.com/user2/media", "types": "23"}
    ],
    "download_types": "",
    "output_dir": ".",
    "download_record_file": "downloaded_urls.json",
    "failed_url_file": "failed_urls.json",
    "parallel_users": 1,
    "stable_scroll_rounds": 2,
    "download_workers": 6,
    "image_format": "png",
    "audio_format": "mp3",
    "convert_gif": true,
    "keep_gif_mp4": false,
    "gif_fps": 8,
    "gif_width": 720
  }

说明:
  - 最多下载 5 个目标。
  - targets 中的 target 可以写用户名、@用户名或完整URL。
  - 每个目标的 types 优先级高于全局 -t/--types 和 download_types。
  - 不提供目标或下载类型时，程序会进入交互输入。
""".strip()


def load_config(path):
    config = DEFAULT_CONFIG.copy()
    config_path = Path(path)
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as f:
            user_config = json.load(f)
        if not isinstance(user_config, dict):
            raise ValueError(f"{path} 必须是 JSON 对象")
        config.update(user_config)
    return config


def short_error(error):
    message = str(error).strip().splitlines()
    if message:
        return message[0]
    return error.__class__.__name__


class SeleniumStackFilter:
    FILTER_PATTERNS = (
        "Stacktrace:",
        "GetHandleVerifier",
        "(No symbol)",
        "BaseThreadInitThunk",
        "RtlUserThreadStart",
    )

    def __init__(self, stream):
        self.stream = stream
        self._buffer = ""

    def write(self, text):
        if not text:
            return 0

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._write_line(line, "\n")
        return len(text)

    def flush(self):
        if self._buffer:
            self._write_line(self._buffer, "")
            self._buffer = ""
        self.stream.flush()

    def _write_line(self, line, newline):
        if self._should_filter(line):
            return
        self.stream.write(line + newline)

    def _should_filter(self, line):
        return any(pattern in line for pattern in self.FILTER_PATTERNS)

    def __getattr__(self, name):
        return getattr(self.stream, name)


def install_console_filters():
    if not isinstance(sys.stdout, SeleniumStackFilter):
        sys.stdout = SeleniumStackFilter(sys.stdout)
    if not isinstance(sys.stderr, SeleniumStackFilter):
        sys.stderr = SeleniumStackFilter(sys.stderr)


def install_short_exception_hooks():
    def handle_exception(exc_type, exc_value, exc_traceback):
        print(f"未捕获异常: {short_error(exc_value)}")

    def handle_thread_exception(args):
        print(f"线程异常: {args.thread.name}: {short_error(args.exc_value)}")

    sys.excepthook = handle_exception
    threading.excepthook = handle_thread_exception


def parse_args():
    parser = argparse.ArgumentParser(
        description="批量下载 X/Twitter 指定用户媒体资源，最多支持 5 个用户。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HELP_EPILOG,
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="目标 URL 或用户名。可用 目标::类型 单独指定下载类型，例如 @user::13、https://x.com/user/media::2。",
    )
    parser.add_argument(
        "-c",
        "--config",
        default=CONFIG_FILE,
        help="配置文件路径，默认 config.json。",
    )
    parser.add_argument(
        "-t",
        "--types",
        help="下载类型：1图片，2视频，3GIF，4音频，可组合，如 13、4、1234。不传则读取配置，配置为空则交互选择。",
    )
    parser.add_argument("--cookie", help="cookie 文件路径，默认读取配置。")
    parser.add_argument("--output-dir", help="输出目录，默认读取配置。")
    parser.add_argument("--record", help="下载记录文件，默认读取配置。")
    parser.add_argument("--failed-record", help="失败URL清单路径，默认读取配置。")
    parser.add_argument("--parallel-users", type=int, help="同时爬取的用户数，默认读取配置。建议先用 1 或 2。")
    gif_group = parser.add_mutually_exclusive_group()
    gif_group.add_argument("--convert-gif", dest="convert_gif", action="store_true", default=None, help="把 GIF 动图的 mp4 转换成 .gif，比较耗资源，默认读取配置。")
    gif_group.add_argument("--no-convert-gif", dest="convert_gif", action="store_false", help="不把 GIF 动图 mp4 转成 .gif。")
    keep_group = parser.add_mutually_exclusive_group()
    keep_group.add_argument("--keep-gif-mp4", dest="keep_gif_mp4", action="store_true", default=None, help="GIF 转换后保留源 mp4，默认读取配置。")
    keep_group.add_argument("--remove-gif-mp4", dest="keep_gif_mp4", action="store_false", help="GIF 转换成功后删除源 mp4。")
    parser.add_argument("--gif-fps", type=int, help="GIF 转换帧率，默认读取配置。")
    parser.add_argument("--gif-width", type=int, help="GIF 转换宽度，0 表示保持原宽度。")
    parser.add_argument("--audio-format", choices=["m4a", "mp3"], help="音频提取格式，默认读取配置。")
    return parser.parse_args()


def safe_get(driver, url, timeout=30):
    driver.set_page_load_timeout(timeout)
    try:
        driver.get(url)
    except TimeoutException:
        print(f"页面加载超时，停止继续加载: {url}")
        driver.execute_script("window.stop()")


def page_has_login_or_restriction(driver):
    current_url = driver.current_url.lower()
    page_text = driver.page_source.lower()
    markers = [
        "/i/flow/login",
        "log in to x",
        "sign in to x",
        "登录",
        "登入",
        "临时限制",
        "限制你的登录",
        "temporarily limited",
        "unusual login activity",
    ]
    return any(marker in current_url or marker in page_text for marker in markers)


def print_page_status(driver, label):
    print(f"{label}: title={driver.title!r}, url={driver.current_url}")


def page_requests_refresh(driver):
    """Detect X's transient error panel without treating login/rate-limit pages as refreshable."""
    try:
        bodies = driver.find_elements(By.TAG_NAME, "body")
        page_text = str(getattr(bodies[0], "text", "") or "") if bodies else ""
    except Exception:  # noqa: BLE001 - fall back to the page source when a driver is mid-navigation.
        page_text = ""
    if not page_text:
        try:
            page_text = re.sub(r"<[^>]*>", " ", str(driver.page_source or ""))
        except Exception:  # noqa: BLE001 - page may have navigated or closed.
            return False
    normalized = re.sub(r"\s+", " ", page_text).casefold()
    error_markers = (
        "something went wrong",
        "this page failed to load",
        "page failed to load",
        "出错了",
        "出了点问题",
        "页面加载失败",
        "内容加载失败",
    )
    refresh_markers = (
        "try reloading",
        "try refreshing",
        "refresh this page",
        "please reload",
        "please refresh",
        "try again",
        "重新加载",
        "刷新页面",
        "刷新后",
        "再试一次",
        "重试",
    )
    return any(marker in normalized for marker in error_markers) and any(
        marker in normalized for marker in refresh_markers
    )


def wait_for_media_page(driver, timeout=30, cancel_event=None, on_status=None):
    refresh_attempted = False
    timeout = max(1, float(timeout))
    deadline = time.monotonic() + timeout
    while True:
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise RuntimeError("任务已取消")
            if page_has_login_or_restriction(driver):
                raise RuntimeError("当前页面仍是登录/限制页面，说明 cookie 未生效或账号仍被 X 限制。")

            cells = driver.find_elements(By.CSS_SELECTOR, "div[data-testid='cellInnerDiv']")
            if cells:
                print(f"检测到 {len(cells)} 个页面内容块，开始爬取。")
                return

            if not refresh_attempted and page_requests_refresh(driver):
                if cancel_event is not None and cancel_event.is_set():
                    raise RuntimeError("任务已取消")
                refresh_attempted = True
                message = "检测到 X 页面提示重新加载；自动刷新一次并重新等待媒体列表。"
                print(message)
                if on_status is not None:
                    on_status(message)
                try:
                    driver.refresh()
                except TimeoutException:
                    print("刷新后页面加载超时，停止继续加载并检查已加载内容。")
                    try:
                        driver.execute_script("window.stop()")
                    except Exception:  # noqa: BLE001 - continue checking the current document.
                        pass
                deadline = time.monotonic() + timeout
                break

            if cancel_event is not None:
                if cancel_event.wait(1):
                    raise RuntimeError("任务已取消")
            else:
                time.sleep(1)
        else:
            print_page_status(driver, "等待媒体页超时")
            raise RuntimeError("目标页面未加载出媒体列表，已停止，避免程序卡住。")


def normalize_target(target):
    target = target.strip()
    if not target:
        return ""

    if target.startswith("@"):
        target = target[1:]

    if not target.startswith(("http://", "https://")):
        return f"https://x.com/{target}/media"

    parsed = urlparse(target)
    netloc = parsed.netloc.lower()
    if netloc.endswith("twitter.com"):
        target = target.replace(parsed.netloc, "x.com", 1)

    parsed = urlparse(target)
    path = parsed.path.rstrip("/")
    path_head = path.strip("/").split("/", 1)[0].lower() if path.strip("/") else ""
    if path_head in TWITTER_RESERVED_ROUTES:
        return target.rstrip("/")
    # A status URL is already the exact post page. Appending /media produces a
    # different/invalid route and prevents a reverse-image result from being
    # handled as the referenced post.
    if "/status/" not in path and not path.endswith("/media"):
        target = target.rstrip("/") + "/media"
    return target


def is_media_page_url(target_url):
    """Return whether a URL points to X's media route, including query variants."""
    return urlparse(target_url).path.rstrip("/").endswith("/media")


def media_page_url(target_url, media_filter=None):
    """Build an X media URL while replacing, rather than appending, its filter."""
    parsed = urlparse(target_url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() != "filter"
    ]
    if media_filter:
        query_items.append(("filter", media_filter))
    return parsed._replace(query=urlencode(query_items, doseq=True)).geturl()


def build_media_page_tasks(target_url, user_choice):
    """Split X's current photo grid from its video/GIF media view."""
    user_choice = "".join(code for code in "1234" if code in str(user_choice or ""))
    if "/status/" in urlparse(target_url).path.rstrip("/"):
        if not user_choice:
            return []
        if user_choice == "1":
            label = "图片帖子"
        elif "1" not in user_choice:
            label = "视频/GIF/音频帖子"
        else:
            label = "帖子媒体"
        # One post only needs one visit. Profile-only photo/video timeline
        # filters do not apply to /status/<id> pages.
        return [(label, media_page_url(target_url), user_choice)]

    tasks = []
    if "1" in user_choice:
        tasks.append(("图片", media_page_url(target_url, "photo"), "1"))

    video_types = "".join(code for code in "234" if code in user_choice)
    if video_types:
        tasks.append(("视频/GIF/音频", media_page_url(target_url), video_types))
    return tasks


def parse_target_spec(value):
    value = str(value or "").strip()
    if "::" not in value:
        return value, ""

    target, download_types = value.rsplit("::", 1)
    return target.strip(), normalize_download_types(download_types)


def target_from_config_item(item, default_types):
    if isinstance(item, dict):
        target = (
            item.get("target")
            or item.get("url")
            or item.get("handle")
            or item.get("username")
            or ""
        )
        download_types = item.get("types") or item.get("download_types") or default_types
        return str(target).strip(), normalize_download_types(download_types)

    target, inline_types = parse_target_spec(item)
    return target, inline_types or default_types


def unique_target_tasks(targets, max_users, default_types):
    normalized = []
    seen = set()
    for target in targets:
        raw_target, download_types = target_from_config_item(target, default_types)
        url = normalize_target(raw_target)
        if not url or url in seen:
            continue
        seen.add(url)
        normalized.append(
            {
                "url": url,
                "types": normalize_download_types(download_types),
            }
        )
        if len(normalized) >= max_users:
            break
    return normalized


def split_targets(text):
    return [item.strip() for item in re.split(r"[\s,，]+", text) if item.strip()]


def prompt_targets(max_users, default_types):
    print(f"请输入要下载的目标，最多 {max_users} 个。")
    print("可以输入用户名、@用户名、完整URL；也可以写 目标::类型，例如 @user::13。直接回车结束。")
    targets = []
    while len(targets) < max_users:
        value = input(f"目标({len(targets) + 1}/{max_users}): ").strip()
        if not value:
            break

        for item in split_targets(value):
            raw_target, inline_types = parse_target_spec(item)
            download_types = inline_types or default_types
            if not download_types:
                download_types = prompt_download_types(target_label=raw_target)
            targets.append({"target": raw_target, "types": download_types})
            if len(targets) >= max_users:
                break
        targets = targets[:max_users]

    return targets


def normalize_download_types(value):
    value = "".join(sorted(set(str(value or "").strip())))
    if not value:
        return ""
    if any(char not in "1234" for char in value):
        raise ValueError("下载类型只能包含 1、2、3、4，例如 1、23、4、1234。")
    return value


def prompt_download_types(default="123", target_label=None):
    prefix = f"{target_label} 的" if target_label else ""
    while True:
        value = input(
            f'''请输入{prefix}下载类型(1/2/3/4/12/13/23/123/1234)，回车默认下载图片/视频/GIF:
1 - 图片    2 - 视频    3 - GIF动图    4 - 提取音频
'''
        ).strip() or default
        try:
            return normalize_download_types(value)
        except ValueError as error:
            print(error)


def folder_from_target_url(target_url):
    path = urlparse(target_url).path.strip("/")
    handle = path.split("/", 1)[0] if path else "x_user"
    return handle.lstrip("@") or "x_user"


def safe_path_name(value, fallback="x_user"):
    value = INVALID_PATH_CHARS.sub("_", value or "").strip().strip(".")
    if value.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))
    }:
        value = f"_{value}"
    return value or fallback


def ensure_cookie_file(cookie_file):
    if not os.path.exists(cookie_file):
        raise FileNotFoundError(f"找不到 {cookie_file}，请先运行 set_cookie.py 生成 cookie。")


def ensure_folder(path):
    Path(path).mkdir(parents=True, exist_ok=True)


def print_stats(label, stats):
    print(f"\n{label} 下载统计:")
    print(f"  入队: 图片 {stats.get('queued_image', 0)}，视频 {stats.get('queued_video', 0)}，GIF {stats.get('queued_gif', 0)}，音频 {stats.get('queued_audio', 0)}")
    print(f"  成功: 图片 {stats.get('success_image', 0)}，视频 {stats.get('success_video', 0)}，GIF {stats.get('success_gif', 0)}，音频 {stats.get('success_audio', 0)}")
    print(f"  跳过: 记录 {stats.get('skipped_record', 0)}，本地已存在 {stats.get('skipped_existing', 0)}，队列重复 {stats.get('duplicate_queue', 0)}")
    print(f"  失败: 图片 {stats.get('failed_image', 0)}，视频 {stats.get('failed_video', 0)}，GIF {stats.get('failed_gif', 0)}，音频 {stats.get('failed_audio', 0)}")
    print(f"  GIF转换: 成功 {stats.get('converted_gif', 0)}，跳过 {stats.get('skipped_gif_conversion', 0)}，失败 {stats.get('failed_gif_conversion', 0)}")


def merge_stats(total, stats):
    for key, value in stats.items():
        total[key] = total.get(key, 0) + value


def normalize_parallel_users(value, target_count):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(value, target_count, 5))


def config_bool(config, key, default=False):
    value = config.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def config_int(config, key, default=0, minimum=None):
    try:
        value = int(config.get(key, default))
    except (TypeError, ValueError):
        value = int(default)
    if minimum is not None:
        value = max(minimum, value)
    return value


def prepare_default_download_types(args, config):
    if args.types:
        return normalize_download_types(args.types)

    return normalize_download_types(config.get("download_types", ""))


def apply_cli_overrides(args, config):
    if args.convert_gif is not None:
        config["convert_gif"] = args.convert_gif
    if args.keep_gif_mp4 is not None:
        config["keep_gif_mp4"] = args.keep_gif_mp4
    if args.gif_fps is not None:
        config["gif_fps"] = args.gif_fps
    if args.gif_width is not None:
        config["gif_width"] = args.gif_width
    if args.audio_format is not None:
        config["audio_format"] = args.audio_format


def fill_missing_target_types(tasks):
    for task in tasks:
        if not task["types"]:
            task["types"] = prompt_download_types(target_label=task["url"])
    return tasks


def prepare_targets(args, config, default_types):
    config_targets = config.get("targets") or []
    if isinstance(config_targets, str):
        config_targets = split_targets(config_targets)

    targets = args.targets or config_targets
    max_users = int(config.get("max_users", 5))
    max_users = max(1, min(max_users, 5))
    targets = unique_target_tasks(targets, max_users, default_types)
    if not targets:
        targets = unique_target_tasks(prompt_targets(max_users, default_types), max_users, default_types)
    if not targets:
        raise ValueError("没有可用目标，已停止。")
    return fill_missing_target_types(targets)


def download_one_target(
    driver,
    target_url,
    output_dir,
    user_choice,
    config,
    record,
    failure_record,
    cancel_event=None,
    on_status=None,
    on_media_result=None,
):
    print(f"\n开始下载目标: {target_url}")
    page_tasks = build_media_page_tasks(target_url, user_choice)
    if not page_tasks:
        raise ValueError("没有可下载的媒体类型。")
    status_match = re.search(r"/status/(\d+)(?:/|$)", urlparse(target_url).path)
    desired_tweet_id = status_match.group(1) if status_match else None

    folder = None
    video_folder = None
    audio_folder = None
    target_stats = {}

    for page_label, page_url, page_types in page_tasks:
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("任务已取消")
        print(f"打开{page_label}页: {page_url}")
        safe_get(driver, page_url, timeout=int(config.get("page_load_timeout", 30)))
        print_page_status(driver, f"{page_label}页状态")
        wait_for_media_page(
            driver,
            timeout=int(config.get("media_wait_timeout", 30)),
            cancel_event=cancel_event,
            on_status=on_status,
        )

        if folder is None:
            try:
                folder_name = get_twitter_name(driver)
            except Exception as error:
                folder_name = folder_from_target_url(target_url)
                print(f"获取页面用户名失败，改用URL用户名作为文件夹: {folder_name} ({short_error(error)})")

            folder = Path(output_dir) / safe_path_name(folder_name)
            ensure_folder(folder)
            video_folder = folder / "video & gif"
            audio_folder = folder / "audio"
            if "2" in user_choice or "3" in user_choice:
                ensure_folder(video_folder)
            if "4" in user_choice:
                ensure_folder(audio_folder)

        stats = download_media(
            driver,
            folder,
            video_folder,
            audio_folder,
            page_types,
            is_media_page_url(page_url),
            record=record,
            failure_record=failure_record,
            max_idle_rounds=config_int(config, "max_idle_rounds", 6, minimum=1),
            cells_per_round=config_int(config, "cells_per_round", 7, minimum=1),
            download_workers=config_int(config, "download_workers", 6, minimum=1),
            stable_scroll_rounds=config_int(config, "stable_scroll_rounds", 2, minimum=1),
            convert_gif=config_bool(config, "convert_gif", True),
            keep_gif_mp4=config_bool(config, "keep_gif_mp4", False),
            gif_fps=config_int(config, "gif_fps", 8, minimum=1),
            gif_width=config_int(config, "gif_width", 720, minimum=0),
            audio_format=str(config.get("audio_format", "mp3") or "mp3").lower(),
            image_format=str(config.get("image_format", "png") or "png").lower(),
            blocked_tweet_ids=set(config.get("blocked_tweet_ids") or []),
            blocked_author_ids=set(config.get("blocked_author_ids") or []),
            blocked_handles=set(config.get("blocked_handles") or []),
            desired_tweet_id=desired_tweet_id,
            cancel_event=cancel_event,
            on_media_result=on_media_result,
        )
        merge_stats(target_stats, stats)

    print_stats(folder.name, target_stats)
    return target_stats


def initialize_authenticated_driver(cookie_file, config):
    driver = initialize_driver()
    try:
        print("打开 x.com，用于注入 cookie.....")
        safe_get(driver, BOOTSTRAP_URL, timeout=int(config.get("bootstrap_timeout", 20)))
        cookies_web(driver, cookie_file)

        if not driver.get_cookie("auth_token") or not driver.get_cookie("ct0"):
            raise RuntimeError("浏览器中没有成功设置 auth_token/ct0，请检查 X_cookie.json。")
        return driver
    except Exception:
        try:
            driver.quit()
        except Exception:
            pass
        raise


def download_target_with_own_driver(index, total, task, cookie_file, output_dir, config, record, failure_record):
    driver = None
    target_url = task["url"]
    user_choice = task["types"]
    try:
        print(f"\n[{index}/{total}] 启动独立浏览器: {target_url}")
        driver = initialize_authenticated_driver(cookie_file, config)
        return download_one_target(driver, target_url, output_dir, user_choice, config, record, failure_record)
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def main():
    install_console_filters()
    install_short_exception_hooks()
    args = parse_args()
    config = load_config(args.config)
    apply_cli_overrides(args, config)
    print(f"twitter_Crawler_2 version: {CRAWLER_VERSION}")

    cookie_file = args.cookie or config.get("cookie_file", DEFAULT_CONFIG["cookie_file"])
    output_dir = args.output_dir or config.get("output_dir", DEFAULT_CONFIG["output_dir"])
    record_file = args.record or config.get("download_record_file", DEFAULT_CONFIG["download_record_file"])
    failed_url_file = args.failed_record or config.get("failed_url_file", DEFAULT_CONFIG["failed_url_file"])
    default_types = prepare_default_download_types(args, config)
    targets = prepare_targets(args, config, default_types)
    parallel_users = normalize_parallel_users(
        args.parallel_users if args.parallel_users is not None else config.get("parallel_users", 1),
        len(targets),
    )

    configure_downloads(
        max_retries=config.get("max_retries", 5),
        connect_timeout=config.get("request_connect_timeout", 10),
        read_timeout=config.get("request_read_timeout", 60),
        proxy_url=config.get("proxy_url", ""),
        use_system_proxy=config_bool(config, "use_system_proxy", True),
        retry_backoff_base=config.get("retry_backoff_base", 1.0),
        retry_backoff_max=config.get("retry_backoff_max", 15.0),
    )

    ensure_cookie_file(cookie_file)
    ensure_folder(output_dir)
    record = DownloadRecord(record_file)
    failure_record = DownloadFailureRecord(failed_url_file)
    total_stats = {}
    driver = None

    try:
        print(f"批量目标数: {len(targets)}；并行用户数: {parallel_users}")
        for task in targets:
            print(f"  {task['url']} -> 下载类型 {task['types']}")

        if parallel_users == 1:
            driver = initialize_authenticated_driver(cookie_file, config)
            for index, task in enumerate(targets, 1):
                target_url = task["url"]
                user_choice = task["types"]
                print(f"\n[{index}/{len(targets)}]")
                try:
                    stats = download_one_target(driver, target_url, output_dir, user_choice, config, record, failure_record)
                    merge_stats(total_stats, stats)
                except Exception as error:
                    print(f"目标下载失败，继续下一个: {target_url} ({short_error(error)})")
        else:
            with ThreadPoolExecutor(max_workers=parallel_users) as executor:
                futures = {
                    executor.submit(
                        download_target_with_own_driver,
                        index,
                        len(targets),
                        task,
                        cookie_file,
                        output_dir,
                        config,
                        record,
                        failure_record,
                    ): task
                    for index, task in enumerate(targets, 1)
                }
                for future in as_completed(futures):
                    task = futures[future]
                    try:
                        stats = future.result()
                        merge_stats(total_stats, stats)
                    except Exception as error:
                        print(f"目标下载失败: {task['url']} ({short_error(error)})")

        print_stats("全部目标汇总", total_stats)

    except Exception as error:
        print(f"爬取已停止: {short_error(error)}")
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


if __name__ == "__main__":
    main()
