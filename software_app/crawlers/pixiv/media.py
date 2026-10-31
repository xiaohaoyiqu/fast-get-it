"""Per-page outcomes for a single static Pixiv artwork."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.media_results import media_descriptor, refreshed_media, retry_folder, run_media_jobs
from software_app.core.models import FileRecord, ProgressEvent
from software_app.core.task_control import task_cancelled
from software_app.crawlers.common import download_media_file, extension_from_url, normalized_media_extension


def _pages(crawler, work_id, cancel_event):
    pages = crawler._body(f"/ajax/illust/{work_id}/pages")
    if not isinstance(pages, list):
        raise ValueError(f"Pixiv 作品 {work_id} 页数据格式异常")
    if not pages:
        raise ValueError(f"Pixiv 作品 {work_id} 未返回任何图片页，不能标记为下载完成")
    if len(pages) > 10000:
        raise ValueError("Pixiv 单作品逐媒体计划最多 10000 页")
    result = []
    for page in pages:
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        urls = page.get("urls") if isinstance(page, dict) else None
        original = urls.get("original") if isinstance(urls, dict) else None
        original = original if isinstance(original, str) else ""
        try:
            urlsplit(original)
        except ValueError:
            original = ""
        result.append({"type": "image", "url": original})
    return result


def _download_page(crawler, task, work_id, summary, page, index, folder, cancel_event):
    original = page["url"]
    parsed = urlsplit(original)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not (host == "pximg.net" or host.endswith(".pximg.net"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Pixiv 图片页没有有效的 HTTPS 原图地址，或地址不属于 pximg.net")
    suffix = extension_from_url(original, ".jpg", {".jpg", ".jpeg", ".png", ".gif", ".webp"})
    final_suffix = normalized_media_extension(
        "image/gif" if suffix == ".gif" else "image/*", suffix, "image", task.options
    )
    destination = Path(folder) / f"{index:03d}{final_suffix}"
    source = destination if suffix == final_suffix else Path(folder) / f".{index:03d}.source{suffix}"
    result = download_media_file(
        crawler.session,
        original,
        source,
        destination,
        cancel_event,
        final_suffix,
        public_referer="https://www.pixiv.net/",
        verify_image=True,
        conflict_policy=task.options.get("file_conflict", "reuse"),
    )
    return FileRecord(
        path=result.path.resolve(),
        module_id=task.module_id,
        task_id=task.task_id,
        media_type="image",
        size=result[0],
        title=str(summary.get("title") or result.path.name),
        source_url=original,
        source_id=work_id,
        author_id=str(summary.get("author_id") or ""),
        author_name=str(summary.get("author_name") or ""),
        chapter=str(index),
        published_at=str(summary.get("published_at") or ""),
        tags=tuple(str(tag) for tag in summary.get("tags") or []),
        metadata={
            "page": index,
            "work_title": str(summary.get("title") or ""),
            "ai_type": summary.get("ai_type", 0),
            "pixiv_type": "static",
            "reused": bool(result[1]),
        },
    )


def download_static_work(crawler, task, work_id, summary, folder, callbacks, cancel_event):
    pages = _pages(crawler, work_id, cancel_event)
    jobs, records = [], []
    for index, page in enumerate(pages, 1):
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        descriptor = media_descriptor(
            task, f"https://www.pixiv.net/artworks/{work_id}", index, "image", page["url"], folder
        )

        def download(page=page, index=index):
            record = _download_page(crawler, task, work_id, summary, page, index, folder, cancel_event)
            records.append(record)
            return record

        jobs.append((descriptor, download))
    run_media_jobs(task, callbacks, cancel_event, jobs)
    reused = sum(bool(record.metadata.get("reused")) for record in records)
    failed = len(pages) - len(records)
    callbacks.on_progress(
        ProgressEvent(
            task.task_id,
            task.module_id,
            "warning" if failed else "info",
            f"Pixiv 图片处理结束：下载 {len(records) - reused} 页，复用 {reused} 页，失败 {failed} 页",
            current=len(records),
            total=len(pages),
            percent=len(records) / len(pages) * 100,
            metadata={"downloaded": len(records) - reused, "reused": reused, "failed": failed},
        )
    )


def _retry_data(crawler, task, work_id, cancel_event):
    detail = crawler._body(f"/ajax/illust/{work_id}")
    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    if not isinstance(detail, dict):
        raise ValueError("Pixiv 作品详情格式异常")
    if int(detail.get("illustType") or 0) == 2:
        raise ValueError("作品现为动图，未按静态图片补下")
    summary = crawler._work_summary(detail)
    blocklist_path = str(task.options.get("_blocklist_path") or "")
    if blocklist_path and BlocklistStore(blocklist_path).is_blocked(
        "pixiv", f"https://www.pixiv.net/artworks/{work_id}", author_id=str(summary.get("author_id") or "")
    ):
        raise ValueError("作品或作者已在黑名单中，未补下")
    if bool(task.options.get("filter_ai", True)) and crawler._is_ai(detail):
        raise ValueError("作品现被 AI 过滤规则排除，未补下")
    return {"summary": summary, "pages": _pages(crawler, work_id, cancel_event)}


def retry_static_work(crawler, task, work_id, callbacks, cancel_event):
    cache = {}

    def download(item):
        if item["target"] != f"https://www.pixiv.net/artworks/{work_id}" or item["kind"] != "image":
            raise ValueError("失败记录不属于原 Pixiv 单作品图片任务")
        if not cache:
            try:
                cache.update(_retry_data(crawler, task, work_id, cancel_event))
            except TaskCancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                cache["error"] = exc
                raise
        if "error" in cache:
            raise cache["error"].with_traceback(None)
        page = refreshed_media(cache["pages"], item)
        return _download_page(
            crawler, task, work_id, cache["summary"], page, int(item["index"]), retry_folder(task, item), cancel_event
        )

    run_media_jobs(
        task,
        callbacks,
        cancel_event,
        [(item, lambda item=item: download(item)) for item in task.options["_retry_media"]],
    )
