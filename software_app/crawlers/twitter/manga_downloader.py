import json
import queue
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from .download_method import (
        DownloadStats,
        download_audio,
        download_pic,
        download_video,
        request_with_retries,
        safe_filename,
    )
    from .json_process import get_max_bitrate_url, json_value_find
except ImportError:  # Preserve direct script execution.
    from download_method import (
        DownloadStats,
        download_audio,
        download_pic,
        download_video,
        request_with_retries,
        safe_filename,
    )
    from json_process import get_max_bitrate_url, json_value_find


DOWNLOADER_VERSION = "js-scan-v6"
MEDIA_LABELS = {
    "image": "img",
    "video": "video",
    "gif": "gif",
    "audio": "audio",
}
TWITTER_CREATED_AT_FORMAT = "%a %b %d %H:%M:%S %z %Y"
CRAWLER_IMAGE_FILENAME_RE = re.compile(r"^(?:\d{8}_\d{6}_)?(\d{15,25})_img(\d+)$", re.IGNORECASE)
MEDIA_TIMELINE_OPERATIONS = (
    "UserMedia",
    "UserPhotoTimeline",
    "UserVideoTimeline",
)
USER_TIMELINE_OPERATIONS = MEDIA_TIMELINE_OPERATIONS + ("UserTweets", "TweetDetail")


def short_error(error):
    message = str(error).strip().splitlines()
    if message:
        return message[0]
    return error.__class__.__name__


def emit_media_result(callback, media_type, status, path=None, reason=""):
    if callback is None:
        return
    try:
        callback(media_type, status, path, reason)
    except Exception as error:  # UI reporting must never break a download worker.
        print(f"媒体结果通知失败: {short_error(error)}")


def normalize_datetime_text(value):
    if not value:
        return ""

    value = str(value).strip()
    try:
        if value.endswith("Z"):
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        elif "T" in value:
            parsed = datetime.fromisoformat(value)
        else:
            parsed = datetime.strptime(value, TWITTER_CREATED_AT_FORMAT)
    except ValueError:
        return ""

    return parsed.strftime("%Y%m%d_%H%M%S")


def build_filename_base(media_type, tweet_id=None, index=None, created_at=None):
    label = MEDIA_LABELS.get(media_type, media_type)
    date_part = normalize_datetime_text(created_at)
    parts = []

    if date_part:
        parts.append(date_part)
    if tweet_id:
        parts.append(str(tweet_id))

    if not parts:
        return ""

    try:
        index = int(index)
    except (TypeError, ValueError):
        index = 0

    if index > 0:
        parts.append(f"{label}{index:02d}")
    else:
        parts.append(label)
    return "_".join(parts)


def next_filename_base(filename_counters, media_type):
    if filename_counters is None:
        return ""

    label = MEDIA_LABELS.get(media_type, media_type)
    filename_counters[media_type] = filename_counters.get(media_type, 0) + 1
    return f"{label}_{filename_counters[media_type]:04d}"


def enqueue_url(q, queued_urls, src, media_type, stats, filename_base=None, filename_counters=None):
    if not src:
        stats.inc("empty_url")
        return False
    queue_key = (src, media_type)
    filename_key = (media_type, filename_base) if filename_base else None
    seen_filenames = filename_counters.setdefault("_seen_filenames", set()) if filename_counters is not None else set()
    if queue_key in queued_urls or (filename_key is not None and filename_key in seen_filenames):
        stats.inc("duplicate_queue")
        return False

    queued_urls.add(queue_key)
    stats.inc(f"queued_{media_type}")
    if not filename_base:
        filename_base = next_filename_base(filename_counters, media_type)
    if filename_base:
        seen_filenames.add((media_type, filename_base))
    q.put((src, media_type, filename_base))
    return True


def page_state(driver):
    return driver.execute_script(
        r"""
        return {
            height: document.body.scrollHeight || 0,
            y: window.scrollY || 0,
            cells: document.querySelectorAll("div[data-testid='cellInnerDiv']").length
        };
        """
    )


def pop_first_cell_image_sources(driver):
    # 用一次 JS 完成查询和删除，避免 Selenium WebElement 在 React 重绘后变成 stale。
    return driver.execute_script(
        r"""
        const cells = document.querySelectorAll("div[data-testid='cellInnerDiv']");
        if (!cells.length) return null;

        const cell = cells[0];
        // The current photo page is a three-column grid.  Each picture has its
        // own /status/<id>/photo/<n> link, so the context must be read per img.
        const images = Array.from(cell.querySelectorAll("img"))
            .map((img, fallbackIndex) => {
                const src = img.currentSrc || img.src || img.getAttribute("src") || "";
                if (!src.includes("pbs.twimg.com/media/") || src.includes("profile_images")) {
                    return null;
                }

                const anchor = img.closest("a[href*='/status']");
                const statusHref = anchor ? (anchor.getAttribute("href") || "") : "";
                const statusMatch = statusHref.match(/\/status(?:es)?\/(\d+)/);
                const authorMatch = statusHref.match(/^\/?([A-Za-z0-9_]{1,15})\/status(?:es)?\//);
                const mediaIndexMatch = statusHref.match(/\/(?:photo|video)\/(\d+)/);
                const timeElement = cell.querySelector("time");
                return {
                    src,
                    tweet_id: statusMatch ? statusMatch[1] : "",
                    author_handle: authorMatch ? authorMatch[1].toLowerCase() : "",
                    created_at: timeElement ? (timeElement.dateTime || timeElement.getAttribute("datetime") || "") : "",
                    index: mediaIndexMatch ? Number(mediaIndexMatch[1]) : fallbackIndex + 1
                };
            })
            .filter(Boolean);
        cell.remove();
        return images;
        """
    )


def enqueue_images_from_cell(driver, q, queued_urls, user_choice, stats, filename_counters,
                             blocked_tweet_ids=None, blocked_handles=None, desired_tweet_id=None):
    image_sources = pop_first_cell_image_sources(driver)
    if image_sources is None:
        return 0, 0

    new_count = 0
    if "1" not in user_choice:
        return 1, 0

    for fallback_index, image_item in enumerate(image_sources, 1):
        if isinstance(image_item, dict):
            src = image_item.get("src", "")
            tweet_id = image_item.get("tweet_id", "")
            author_handle = str(image_item.get("author_handle") or "").casefold()
            created_at = image_item.get("created_at", "")
            media_index = image_item.get("index") or fallback_index
        else:
            src = image_item
            tweet_id = ""
            author_handle = ""
            created_at = ""
            media_index = fallback_index

        if tweet_id and str(tweet_id) in (blocked_tweet_ids or ()):
            continue
        if desired_tweet_id and str(tweet_id) != str(desired_tweet_id):
            continue
        if author_handle and author_handle in (blocked_handles or ()):
            continue
        if "profile_images" in src or "media" not in src:
            continue

        base_url = src.split("?", 1)[0]
        clean_url = base_url + "?format=png&name=large"
        filename_base = build_filename_base("image", tweet_id, media_index, created_at)
        if enqueue_url(q, queued_urls, clean_url, "image", stats, filename_base, filename_counters):
            new_count += 1

    return 1, new_count


def normalize_photo_url(url):
    """Use PNG consistently so DOM and GraphQL sources share one download URL."""
    if not url:
        return ""

    parsed = urlparse(url)
    base_url = parsed._replace(query="", fragment="").geturl()
    return base_url + "?format=png&name=large"


def cleanup_png_image_duplicates(folder):
    """Remove crawler-generated JPEG siblings only when an equivalent PNG exists."""
    folder = Path(folder)
    groups = {}
    if not folder.exists():
        return {"files": 0, "bytes": 0}

    for path in folder.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            continue
        match = CRAWLER_IMAGE_FILENAME_RE.match(path.stem)
        if not match:
            continue
        key = match.group(1), match.group(2)
        groups.setdefault(key, []).append(path)

    removed_files = 0
    removed_bytes = 0
    for paths in groups.values():
        has_png = any(path.suffix.lower() == ".png" for path in paths)
        if not has_png:
            continue
        for path in paths:
            if path.suffix.lower() not in {".jpg", ".jpeg"}:
                continue
            try:
                removed_bytes += path.stat().st_size
                path.unlink()
                removed_files += 1
            except OSError as error:
                print(f"清理重复 JPG 失败，跳过: {path.name} ({short_error(error)})")

    return {"files": removed_files, "bytes": removed_bytes}


def is_timeline_response_url(response_url, is_media):
    """Match X's media timeline operations without relying on a single legacy name."""
    path = urlparse(response_url or "").path.lower()
    operation_names = MEDIA_TIMELINE_OPERATIONS if is_media else USER_TIMELINE_OPERATIONS
    return any(f"/{operation.lower()}" in path for operation in operation_names)


def tweet_context_from_node(json_obj, context):
    if not isinstance(json_obj, dict):
        return context

    legacy = json_obj.get("legacy")
    if not isinstance(legacy, dict):
        # X's public embed/syndication payload is flatter than GraphQL: tweet
        # fields live at the root and attachments are under mediaDetails.
        tweet_id = json_obj.get("id_str")
        looks_like_tweet = bool(
            tweet_id
            and any(key in json_obj for key in ("text", "entities", "mediaDetails", "created_at"))
        )
        if not looks_like_tweet:
            return context
        user = json_obj.get("user") if isinstance(json_obj.get("user"), dict) else {}
        return {
            "tweet_id": str(tweet_id),
            "author_id": str(user.get("id_str") or user.get("id") or ""),
            "author_handle": str(user.get("screen_name") or "").casefold(),
            "created_at": json_obj.get("created_at") or context.get("created_at", ""),
        }

    looks_like_tweet = (
        "full_text" in legacy
        or "entities" in legacy
        or "extended_entities" in legacy
        or "created_at" in legacy
    )
    tweet_id = json_obj.get("rest_id") or legacy.get("id_str") or legacy.get("conversation_id_str")
    if not looks_like_tweet or not tweet_id:
        return context

    core = json_obj.get("core") if isinstance(json_obj.get("core"), dict) else {}
    user_results = core.get("user_results") if isinstance(core.get("user_results"), dict) else {}
    user = user_results.get("result") if isinstance(user_results.get("result"), dict) else {}
    user_legacy = user.get("legacy") if isinstance(user.get("legacy"), dict) else {}
    legacy_user = legacy.get("user") if isinstance(legacy.get("user"), dict) else {}

    return {
        "tweet_id": str(tweet_id),
        "author_id": str(legacy.get("user_id_str") or user.get("rest_id") or legacy_user.get("id_str") or ""),
        "author_handle": str(user_legacy.get("screen_name") or legacy_user.get("screen_name") or "").casefold(),
        "created_at": legacy.get("created_at") or context.get("created_at", ""),
    }


def iter_media_entities(json_obj, context=None):
    context = context or {}
    if isinstance(json_obj, dict):
        context = tweet_context_from_node(json_obj, context)
        media_url = json_obj.get("media_url_https") or json_obj.get("media_url")
        video_info = json_obj.get("video_info")
        if media_url or isinstance(video_info, dict):
            yield json_obj, context

        for value in json_obj.values():
            yield from iter_media_entities(value, context)
    elif isinstance(json_obj, list):
        for item in json_obj:
            yield from iter_media_entities(item, context)


def iter_variants_with_tweet(json_obj, context=None):
    context = context or {}
    if isinstance(json_obj, dict):
        context = tweet_context_from_node(json_obj, context)
        variants = json_obj.get("variants")
        if isinstance(variants, list):
            yield variants, context
        for value in json_obj.values():
            yield from iter_variants_with_tweet(value, context)
    elif isinstance(json_obj, list):
        for item in json_obj:
            yield from iter_variants_with_tweet(item, context)


def next_media_index(media_counts, media_type, tweet_id):
    key = (tweet_id or "unknown", media_type)
    media_counts[key] = media_counts.get(key, 0) + 1
    return media_counts[key]


def extract_media_from_json(json_obj, user_choice, blocked_tweet_ids=None, blocked_author_ids=None,
                            blocked_handles=None, desired_tweet_id=None):
    media_items = []
    seen = set()
    media_counts = {}

    for media, context in iter_media_entities(json_obj):
        media_type = media.get("type") or media.get("__typename") or ""
        media_type = str(media_type).lower()
        tweet_id = context.get("tweet_id", "")
        if tweet_id and str(tweet_id) in (blocked_tweet_ids or ()):
            continue
        if desired_tweet_id and str(tweet_id) != str(desired_tweet_id):
            continue
        if context.get("author_id") in (blocked_author_ids or ()) or context.get("author_handle") in (blocked_handles or ()):
            continue
        created_at = context.get("created_at", "")

        media_url = media.get("media_url_https") or media.get("media_url")
        if media_url and "1" in user_choice and ("photo" in media_type or not media.get("video_info")):
            photo_url = normalize_photo_url(media_url)
            if photo_url and photo_url not in seen:
                seen.add(photo_url)
                index = next_media_index(media_counts, "image", tweet_id)
                filename_base = build_filename_base("image", tweet_id, index, created_at)
                media_items.append((photo_url, "image", filename_base))

        video_info = media.get("video_info")
        if not isinstance(video_info, dict):
            continue

        variants = video_info.get("variants") or []
        media_video_url = get_max_bitrate_url(variants)
        if not media_video_url:
            continue

        if "animated_gif" in media_type:
            if "3" in user_choice and media_video_url not in seen:
                seen.add(media_video_url)
                index = next_media_index(media_counts, "gif", tweet_id)
                filename_base = build_filename_base("gif", tweet_id, index, created_at)
                media_items.append((media_video_url, "gif", filename_base))
        else:
            if "2" in user_choice and media_video_url not in seen:
                seen.add(media_video_url)
                index = next_media_index(media_counts, "video", tweet_id)
                filename_base = build_filename_base("video", tweet_id, index, created_at)
                media_items.append((media_video_url, "video", filename_base))
            if "4" in user_choice:
                audio_seen_key = (media_video_url, "audio")
                if audio_seen_key not in seen:
                    seen.add(audio_seen_key)
                    index = next_media_index(media_counts, "audio", tweet_id)
                    filename_base = build_filename_base("audio", tweet_id, index, created_at)
                    media_items.append((media_video_url, "audio", filename_base))

    return media_items


def fetch_syndication_tweet(tweet_id, cancel_event=None):
    """Read one public status through the endpoint used by X embed widgets."""
    tweet_id = str(tweet_id or "").strip()
    if not tweet_id.isdigit():
        return {}
    response = request_with_retries(
        f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token=a&lang=en",
        cancel_event=cancel_event,
    )
    if response is None:
        return {}
    try:
        payload = response.json()
    except (ValueError, json.JSONDecodeError):
        return {}
    finally:
        response.close()
    if not isinstance(payload, dict) or str(payload.get("id_str") or "") != tweet_id:
        return {}
    return payload


def download_status_from_syndication(
    tweet_id,
    output_dir,
    user_choice,
    record=None,
    failure_record=None,
    config=None,
    cancel_event=None,
    on_media_result=None,
):
    """Download an exact public status without depending on X's rendered DOM."""
    config = config or {}
    payload = fetch_syndication_tweet(tweet_id, cancel_event)
    if not payload:
        return None
    media_items = extract_media_from_json(
        payload,
        user_choice,
        set(config.get("blocked_tweet_ids") or []),
        set(config.get("blocked_author_ids") or []),
        set(config.get("blocked_handles") or []),
        desired_tweet_id=str(tweet_id),
    )
    if not media_items:
        return None

    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    folder_name = safe_filename(user.get("screen_name") or user.get("name") or "twitter", "twitter")
    folder = Path(output_dir) / folder_name
    video_folder = folder / "video & gif"
    audio_folder = folder / "audio"
    stats = DownloadStats()
    for src, media_type, filename_base in media_items:
        if cancel_event is not None and cancel_event.is_set():
            break
        stats.inc(f"queued_{media_type}")
        reported = [False]
        def report(kind, status, path=None, reason=""):
            reported[0] = True
            emit_media_result(on_media_result, kind, status, path, reason)
        if media_type == "image":
            download_pic(
                src, folder, record, stats, failure_record, filename_base,
                cancel_event=cancel_event, image_format=config.get("image_format", "png"),
                on_result=report,
            )
        elif media_type == "audio":
            download_audio(
                src, audio_folder, record, stats, failure_record, filename_base,
                cancel_event=cancel_event, audio_format=config.get("audio_format", "mp3"),
                on_result=report,
            )
        else:
            download_video(
                src, video_folder, record, stats, media_type, failure_record, filename_base,
                cancel_event=cancel_event,
                convert_gif=config.get("convert_gif", False),
                keep_gif_mp4=config.get("keep_gif_mp4", True),
                gif_fps=config.get("gif_fps", 12),
                gif_width=config.get("gif_width", 0),
                on_result=report,
            )
        if not reported[0] and (cancel_event is None or not cancel_event.is_set()):
            report(media_type, "failed", None, "下载流程未返回结果")
    return stats.snapshot()


def url_producer(
    driver,
    q,
    user_choice,
    is_media,
    producer_done,
    queued_urls,
    stats,
    max_idle_rounds=6,
    cells_per_round=7,
    worker_count=6,
    stable_scroll_rounds=2,
    cancel_event=None,
    blocked_tweet_ids=None,
    blocked_author_ids=None,
    blocked_handles=None,
    desired_tweet_id=None,
):
    # 生产者线程函数，抓取图片URL并放入队列
    idle_rounds = 0
    error_rounds = 0
    stable_rounds = 0
    last_height = 0
    filename_counters = {}
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                processed_cells = 0
                new_urls = 0

                for _ in range(cells_per_round):
                    processed, new_images = enqueue_images_from_cell(
                        driver, q, queued_urls, user_choice, stats, filename_counters,
                        blocked_tweet_ids, blocked_handles, desired_tweet_id
                    )
                    processed_cells += processed
                    new_urls += new_images
                    if not processed:
                        break
                    if cancel_event is not None and cancel_event.wait(0.2):
                        break
                    time.sleep(0 if cancel_event is not None else 0.2)

                new_urls += media_video(driver, q, user_choice, is_media, queued_urls, stats, filename_counters,
                                        blocked_tweet_ids, blocked_author_ids, blocked_handles, desired_tweet_id)

                if processed_cells == 0:
                    driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                    if cancel_event is not None:
                        cancel_event.wait(2)
                    else:
                        time.sleep(2)
                else:
                    driver.execute_script("window.scrollBy(0, Math.floor(window.innerHeight * 0.8));")
                    if cancel_event is not None:
                        cancel_event.wait(0.8)
                    else:
                        time.sleep(0.8)
                after_state = page_state(driver)

                height = int(after_state.get("height") or 0)
                if height <= last_height:
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                    last_height = height

                if new_urls:
                    idle_rounds = 0
                    print(f"本轮发现新资源 {new_urls} 个，已入队 {len(queued_urls)} 个URL")
                else:
                    idle_rounds += 1
                    print(
                        f"未发现新资源，空闲轮次 {idle_rounds}/{max_idle_rounds}，"
                        f"页面稳定 {stable_rounds}/{stable_scroll_rounds}，"
                        f"剩余内容块 {after_state.get('cells', 0)}"
                    )

                if idle_rounds >= max_idle_rounds and stable_rounds >= stable_scroll_rounds:
                    print("连续多轮没有发现新资源，当前用户扫描结束。")
                    break

                error_rounds = 0
            except Exception as error:
                if cancel_event is not None and cancel_event.is_set():
                    break
                error_rounds += 1
                print(f'抓取页面内容失败，重试 {error_rounds}/5: {short_error(error)}')
                if error_rounds >= 5:
                    break
                time.sleep(1)
    finally:
        producer_done.set()
        for _ in range(worker_count):
            q.put(None)  # 生产者完成后给每个worker放入停止信号


def download_worker(worker_id, q, folder, video_folder, audio_folder, record, stats, failure_record, options, cancel_event=None, on_media_result=None):
    while True:
        item = q.get()
        if item is None:
            q.task_done()
            break

        if len(item) == 3:
            src, media_type, filename_base = item
        else:
            src, media_type = item
            filename_base = None
        reported = [False]
        def report(kind, status, path=None, reason=""):
            reported[0] = True
            emit_media_result(on_media_result, kind, status, path, reason)
        try:
            if cancel_event is not None and cancel_event.is_set():
                continue
            if media_type == "image":
                download_pic(
                    src,
                    folder,
                    record,
                    stats,
                    failure_record,
                    filename_base,
                    cancel_event=cancel_event,
                    image_format=options.get("image_format", "png"),
                    on_result=report,
                )
            elif media_type == "audio":
                download_audio(
                    src,
                    audio_folder,
                    record,
                    stats,
                    failure_record,
                    filename_base,
                    cancel_event=cancel_event,
                    audio_format=options.get("audio_format", "mp3"),
                    on_result=report,
                )
            else:
                download_video(
                    src,
                    video_folder,
                    record,
                    stats,
                    media_type,
                    failure_record,
                    filename_base,
                    cancel_event=cancel_event,
                    convert_gif=options.get("convert_gif", False),
                    keep_gif_mp4=options.get("keep_gif_mp4", True),
                    gif_fps=options.get("gif_fps", 12),
                    gif_width=options.get("gif_width", 0),
                    on_result=report,
                )
        except Exception as error:
            print(f"下载worker {worker_id} 失败，跳过: {short_error(error)}")
            if not reported[0]:
                report(media_type, "failed", None, short_error(error))
        finally:
            q.task_done()


def media_video(driver, q, user_choice, is_media, queued_urls, stats, filename_counters,
                blocked_tweet_ids=None, blocked_author_ids=None, blocked_handles=None, desired_tweet_id=None):
    if "1" not in user_choice and "2" not in user_choice and "3" not in user_choice and "4" not in user_choice:
        return 0

    # 从浏览器日志network中提取视频URL
    logs_raw = driver.get_log("performance")
    logs = [json.loads(lr["message"])["message"] for lr in logs_raw]

    def log_filter(log_):
        try:
            return (
                log_["method"] == "Network.responseReceived"
                and "json" in log_["params"]["response"]["mimeType"]
                and is_timeline_response_url(log_["params"]["response"]["url"], is_media)
            )
        except KeyError:
            return False

    variants_lists = []
    new_count = 0
    for log in filter(log_filter, logs):
        request_id = log["params"]["requestId"]
        try:
            res = driver.execute_cdp_cmd("Network.getResponseBody", {"requestId": request_id})['body']
            res = json.loads(res)
        except Exception as error:
            print(f"读取网络响应失败，跳过: {short_error(error)}")
            continue

        new_from_media_entities = 0
        for media_url, media_type, filename_base in extract_media_from_json(
            res, user_choice, blocked_tweet_ids, blocked_author_ids, blocked_handles, desired_tweet_id
        ):
            if enqueue_url(q, queued_urls, media_url, media_type, stats, filename_base, filename_counters):
                new_from_media_entities += 1
        new_count += new_from_media_entities
        if new_from_media_entities:
            continue

        needs_context = blocked_tweet_ids or blocked_author_ids or blocked_handles or desired_tweet_id
        fallback_variants = (
            ((variants, context) for variants, context in iter_variants_with_tweet(res)
             if context.get("tweet_id") and context.get("tweet_id") not in (blocked_tweet_ids or ())
             and (not desired_tweet_id or context.get("tweet_id") == str(desired_tweet_id))
             and context.get("author_id") not in (blocked_author_ids or ())
             and context.get("author_handle") not in (blocked_handles or ()))
            if needs_context else ((variants, {}) for variants in json_value_find(res, "variants"))
        )
        for variants, _context in fallback_variants:
            if variants in variants_lists:
                continue

            variants_lists.append(variants)
            media_url = get_max_bitrate_url(variants)
            if not media_url:
                continue

            if "2" in user_choice and 'pu' in media_url:
                if enqueue_url(q, queued_urls, media_url, "video", stats, filename_counters=filename_counters):
                    new_count += 1
            if "4" in user_choice and 'pu' in media_url:
                if enqueue_url(q, queued_urls, media_url, "audio", stats, filename_counters=filename_counters):
                    new_count += 1
            if "3" in user_choice and 'pu' not in media_url:
                if enqueue_url(q, queued_urls, media_url, "gif", stats, filename_counters=filename_counters):
                    new_count += 1

    return new_count


def download_media(
    driver,
    folder,
    video_folder,
    audio_folder,
    user_choice,
    is_media,
    record=None,
    failure_record=None,
    stats=None,
    max_idle_rounds=6,
    cells_per_round=7,
    download_workers=6,
    stable_scroll_rounds=2,
    convert_gif=True,
    keep_gif_mp4=False,
    gif_fps=12,
    gif_width=0,
    audio_format="mp3",
    image_format="png",
    blocked_tweet_ids=None,
    blocked_author_ids=None,
    blocked_handles=None,
    desired_tweet_id=None,
    cancel_event=None,
    on_media_result=None,
):
    print(f"{folder} | downloader={DOWNLOADER_VERSION}")
    # 下载Twitter页面中的图片和视频
    stats = stats or DownloadStats()
    q = queue.Queue()
    queued_urls = set()
    producer_done = threading.Event()
    download_workers = max(1, int(download_workers))
    options = {
        "convert_gif": bool(convert_gif),
        "keep_gif_mp4": bool(keep_gif_mp4),
        "gif_fps": gif_fps,
        "gif_width": gif_width,
        "audio_format": audio_format,
        "image_format": image_format,
    }
    time.sleep(0.1)
    producer_thread = threading.Thread(
        target=url_producer,
        args=(
            driver,
            q,
            user_choice,
            is_media,
            producer_done,
            queued_urls,
            stats,
            max_idle_rounds,
            cells_per_round,
            download_workers,
            stable_scroll_rounds,
            cancel_event,
            blocked_tweet_ids,
            blocked_author_ids,
            blocked_handles,
            desired_tweet_id,
        ),
        name="url-producer",
    )
    worker_threads = [
        threading.Thread(
            target=download_worker,
            args=(index + 1, q, folder, video_folder, audio_folder, record, stats, failure_record, options, cancel_event, on_media_result),
            name=f"download-worker-{index + 1}",
        )
        for index in range(download_workers)
    ]
    producer_thread.start()
    for thread in worker_threads:
        thread.start()

    producer_thread.join()
    q.join()
    for thread in worker_threads:
        thread.join()
    return stats.snapshot()
