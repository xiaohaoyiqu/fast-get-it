"""Discover the selected collection's media before committing any transfers."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.media_results import (
    MediaGroupResult,
    media_descriptor,
    refreshed_media,
    retry_folder,
    run_media_jobs,
)
from software_app.core.models import FileRecord, ProgressEvent
from software_app.core.task_control import task_cancelled
from software_app.crawlers.common import platform_output_root, safe_component
from .media import _pages, _download_page
from .ugoira import download_ugoira, normalize_ugoira_meta


def _target(work_id):
    return f"https://www.pixiv.net/artworks/{work_id}"


def _animation_source(crawler, work_id, cancel_event):
    body = crawler._body(f"/ajax/illust/{work_id}/ugoira_meta")
    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    source = normalize_ugoira_meta(body)["source_url"]
    parsed = urlsplit(source)
    host = (parsed.hostname or "").casefold().rstrip(".")
    if (
        parsed.scheme != "https"
        or not (host == "pximg.net" or host.endswith(".pximg.net"))
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Pixiv 动图没有有效的 pximg.net HTTPS 源地址")
    return body, source


def _inspect(crawler, task, work_id, cancel_event, *, allow_filtered=False):
    detail = crawler._body(f"/ajax/illust/{work_id}")
    if task_cancelled(cancel_event):
        raise TaskCancelled("任务已取消")
    if not isinstance(detail, dict):
        raise ValueError("Pixiv 作品详情格式异常")
    summary = crawler._work_summary(detail)
    reason = ""
    blocklist = str(task.options.get("_blocklist_path") or "")
    if blocklist and BlocklistStore(blocklist).is_blocked("pixiv", _target(work_id), author_id=summary["author_id"]):
        reason = "作品或作者已在黑名单中"
    elif bool(task.options.get("filter_ai", True)) and crawler._is_ai(detail):
        reason = "作品被 AI 过滤规则排除"
    if reason:
        if allow_filtered:
            return {"filtered": reason}
        raise ValueError(reason + "，未补下")
    kind = "animation" if int(detail.get("illustType") or 0) == 2 else "image"
    if kind == "animation":
        _body, source = _animation_source(crawler, work_id, cancel_event)
        return {"summary": summary, "kind": kind, "source": source}
    return {"summary": summary, "kind": kind, "pages": _pages(crawler, work_id, cancel_event)}


def _animation_record(crawler, task, work_id, data, descriptor, folder, callbacks, cancel_event):
    # Refresh signed URLs, but never replace the recorded resource with a new path.
    body, source = _animation_source(crawler, work_id, cancel_event)
    fresh = media_descriptor(task, _target(work_id), 1, "animation", source, folder)
    if fresh["identity"] != descriptor["identity"] or fresh["key"] != descriptor["key"]:
        raise ValueError("原动图源已变化，未补下其他资源")
    outputs = download_ugoira(crawler.session, body, Path(folder), work_id, task.options, cancel_event)
    if not outputs:
        raise ValueError("Pixiv 动图未返回任何成品")
    records = []
    summary = data["summary"]
    for destination, media_type, source_url in outputs:
        path = Path(destination).resolve()
        if Path(folder).resolve() not in path.parents or not path.is_file():
            raise ValueError("Pixiv 动图成品不存在或超出原作品目录")
        records.append(
            FileRecord(
                path=path,
                module_id=task.module_id,
                task_id=task.task_id,
                media_type=media_type,
                size=path.stat().st_size,
                title=str(summary.get("title") or path.name),
                source_url=source_url,
                source_id=work_id,
                author_id=summary["author_id"],
                author_name=summary["author_name"],
                published_at=summary["published_at"],
                tags=tuple(summary["tags"]),
                metadata={"pixiv_type": "ugoira", "work_title": summary["title"], "ai_type": summary["ai_type"]},
            )
        )
    # Transfer has committed the entire group. The shared notifier records that
    # fact even if indexing a companion fails, avoiding duplicate rename output.
    return MediaGroupResult(records[0], tuple(records[1:]))


def download_collection_media(crawler, task, ids, callbacks, cancel_event):
    root = platform_output_root(task.output_dir, "pixiv")
    jobs = []
    for position, work_id in enumerate(ids, 1):
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        callbacks.on_progress(
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"解析集合作品 {position}/{len(ids)}：{work_id}",
                metadata={"collection_work": work_id, "phase": "discover"},
            )
        )
        data = _inspect(crawler, task, work_id, cancel_event, allow_filtered=True)
        if "filtered" in data:
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "warning",
                    f"跳过集合作品 {work_id}：{data['filtered']}",
                    metadata={"collection_work": work_id, "filtered": data["filtered"]},
                )
            )
            continue
        summary = data["summary"]
        folder = root / safe_component(f"{summary['author_name'] or 'unknown'}_{summary['author_id'] or '0'}")
        folder /= safe_component(f"{summary['title'] or work_id}_{work_id}")
        if data["kind"] == "animation":
            item = media_descriptor(task, _target(work_id), 1, "animation", data["source"], folder)

            def animation(work_id=work_id, data=data, item=item, folder=folder):
                return _animation_record(crawler, task, work_id, data, item, folder, callbacks, cancel_event)

            jobs.append((item, animation))
        else:
            if len(jobs) + len(data["pages"]) > 10000:
                raise ValueError("集合逐媒体计划超过 10000 项，请缩小作品范围")
            for index, page in enumerate(data["pages"], 1):
                item = media_descriptor(task, _target(work_id), index, "image", page["url"], folder)

                def image(work_id=work_id, summary=summary, page=page, index=index, folder=folder):
                    return _download_page(crawler, task, work_id, summary, page, index, folder, cancel_event)

                jobs.append((item, image))
        if len(jobs) > 10000:
            raise ValueError("集合逐媒体计划超过 10000 项，请缩小作品范围")
    # No transfer occurs until every selected work has been discovered and the
    # manager has committed the complete plan via run_media_jobs' first event.
    run_media_jobs(task, callbacks, cancel_event, jobs)


def retry_collection_media(crawler, task, ids, callbacks, cancel_event):
    cache, allowed = {}, set(ids)

    def download(item):
        match = re.fullmatch(r"https://www\.pixiv\.net/artworks/([0-9]{1,32})", str(item.get("target") or ""))
        if not match or match[1] not in allowed or item.get("kind") not in {"image", "animation"}:
            raise ValueError("未完成记录不属于原集合范围")
        work_id = match[1]
        if type(item.get("index")) is not int or not 1 <= item["index"] <= 10000:
            raise ValueError("集合媒体序号无效")
        folder = retry_folder(task, item)
        if work_id not in cache:
            try:
                cache[work_id] = _inspect(crawler, task, work_id, cancel_event)
            except TaskCancelled:
                raise
            except (OSError, RuntimeError, ValueError) as exc:
                cache[work_id] = exc
        data = cache[work_id]
        if isinstance(data, Exception):
            raise data.with_traceback(None)
        if data["kind"] != item["kind"]:
            raise ValueError("作品图片／动图类型已变化，未补下")
        if item["kind"] == "animation":
            if item["index"] != 1:
                raise ValueError("动图组序号无效")
            return _animation_record(crawler, task, work_id, data, item, folder, callbacks, cancel_event)
        page = refreshed_media(data["pages"], item)
        fresh = media_descriptor(task, item["target"], item["index"], "image", page["url"], folder)
        if fresh["key"] != item["key"]:
            raise ValueError("集合媒体记录键不匹配")
        return _download_page(crawler, task, work_id, data["summary"], page, item["index"], folder, cancel_event)

    run_media_jobs(
        task,
        callbacks,
        cancel_event,
        [(item, lambda item=item: download(item)) for item in task.options["_retry_media"]],
    )
