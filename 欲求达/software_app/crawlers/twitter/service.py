from __future__ import annotations

from software_app.core.browser_pool import BROWSER_POOL
from software_app.core.browser_session import raise_browser_failure

from software_app.core.task_control import task_cancelled

import threading
import re
from pathlib import Path
from typing import Any

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.events import CallbackSet
from software_app.core.security import safe_error_message
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, classify_file

from .download_method import DownloadFailureRecord, DownloadRecord, configure_downloads, media_filename
from .manga_downloader import PendingMediaTracker, download_status_from_syndication
from .profile_preview import collect_profile_preview_with_driver, target_to_handle as profile_target_to_handle
from .twitter_Crawler_2 import (
    CRAWLER_VERSION,
    DEFAULT_CONFIG,
    config_bool,
    download_one_target,
    ensure_cookie_file,
    initialize_authenticated_driver,
    load_config,
    normalize_target,
)


def _media_default_ext(media_type: str) -> str:
    return {"image": ".png", "video": ".mp4", "gif": ".gif", "audio": ".mp3"}.get(media_type, ".bin")


def report_pending_summary(task, callbacks, pending_tracker, save_unfinished_txt: bool) -> None:
    """Report unfinished media count at task end and optionally write a TXT list.

    The list contains one line per unfinished item:  filename<TAB>download_url.
    This lets the user manually retry or diagnose whether the failure was a
    network download or a local rename/permission problem.
    """
    items = pending_tracker.snapshot()
    if not items:
        return
    rename_failed = sum(1 for item in items if "重命名" in item.get("reason", "") or "被占用" in item.get("reason", ""))
    download_failed = len(items) - rename_failed
    callbacks.on_progress(
        ProgressEvent(
            task.task_id,
            task.module_id,
            "warning",
            f"未完成文件 {len(items)} 个（下载失败 {download_failed}，重命名/写入失败 {rename_failed}）",
            status="running",
            metadata={"phase": "pending_summary", "pending_count": len(items),
                      "download_failed": download_failed, "rename_failed": rename_failed},
        )
    )
    if not save_unfinished_txt:
        return
    try:
        txt_path = Path(task.output_dir) / f"未完成_{task.task_id}.txt"
        txt_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for item in items:
            url = item.get("url", "")
            if not url:
                continue
            filename = media_filename(url, _media_default_ext(item.get("media_type", "")), item.get("filename_base", ""))
            reason = item.get("reason", "")
            if reason:
                lines.append(f"{filename}\t{url}\t{reason}")
            else:
                lines.append(f"{filename}\t{url}")
        if lines:
            txt_path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id, task.module_id, "info",
                    f"未完成清单已保存: {txt_path.name}",
                    status="running",
                    metadata={"phase": "pending_txt", "path": str(txt_path)},
                )
            )
    except OSError as exc:
        callbacks.on_progress(
            ProgressEvent(
                task.task_id, task.module_id, "error",
                f"未完成清单保存失败: {str(exc).splitlines()[0]}",
                status="running",
            )
        )


class TwitterCrawlerService:
    """Run the software-owned crawler directly without a crawler subprocess."""

    def __init__(self, runtime_data_dir: Path) -> None:
        self.runtime_data_dir = Path(runtime_data_dir)
        self._drivers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def download(
        self,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> dict[str, int]:
        config_path = Path(task.options.get("config") or self.runtime_data_dir / "config.json")
        config = load_config(str(config_path)) if config_path.exists() else dict(DEFAULT_CONFIG)
        self._apply_options(config, task.options)
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        if blocklist_path:
            blocklist = BlocklistStore(blocklist_path)
            config["blocked_tweet_ids"] = [work for platform, work in blocklist.blocked_works() if platform == "twitter"]
            config["blocked_author_ids"] = [account for platform, account in blocklist.blocked_accounts() if platform == "twitter_id"]
            config["blocked_handles"] = [account for platform, account in blocklist.blocked_accounts() if platform == "twitter"]

        cookie_path = Path(task.options.get("cookie") or self.runtime_data_dir / "X_cookie.json")
        record_path = Path(task.options.get("record") or self.runtime_data_dir / "downloaded_urls.json")
        failed_path = Path(task.options.get("failed_record") or self.runtime_data_dir / "failed_urls.json")
        target_url = normalize_target(task.target)
        download_types = str(task.options.get("types") or "1234").replace(",", "").replace("，", "")
        save_unfinished_txt = bool(task.options.get("save_unfinished_txt", False))

        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        ensure_cookie_file(str(cookie_path))
        task.output_dir.mkdir(parents=True, exist_ok=True)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        failed_path.parent.mkdir(parents=True, exist_ok=True)

        def network_error(info, attempt, limit):
            action = f"请求将重试 {attempt}/{limit}" if info.retryable and attempt < limit else "本轮请求停止"
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "warning", f"[{info.label}] {action}",
                metadata={"phase": "network", "error_code": info.code, "retryable": info.retryable},
            ))

        network_config = configure_downloads(
            on_error=network_error,
            file_conflict=task.options.get("file_conflict", "reuse"),
            max_retries=config.get("max_retries", 5),
            connect_timeout=config.get("request_connect_timeout", 10),
            read_timeout=config.get("request_read_timeout", 60),
            proxy_url=config.get("proxy_url", ""),
            use_system_proxy=config_bool(config, "use_system_proxy", True),
            retry_backoff_base=config.get("retry_backoff_base", 1.0),
            retry_backoff_max=config.get("retry_backoff_max", 15.0),
        )
        callbacks.on_progress(
            ProgressEvent(task.task_id, task.module_id, "info", f"启动软件内置 Twitter 爬虫 {CRAWLER_VERSION}")
        )

        _media_progress = {"total": 0, "done": 0}

        def report_media_result(media_type: str, result: str, file_path=None, reason: str = "") -> None:
            if result == "processing":
                _media_progress["total"] += 1
            elif result in {"completed", "skipped", "failed"}:
                _media_progress["done"] += 1
            media_label = {"image": "图片", "video": "视频", "gif": "GIF", "audio": "音频"}.get(media_type, media_type)
            path = Path(file_path) if file_path else None
            if result in {"completed", "skipped"} and path is not None and path.is_file():
                try:
                    callbacks.on_file(
                        FileRecord(
                            path=path.resolve(), module_id=task.module_id, task_id=task.task_id,
                            media_type=classify_file(path), size=path.stat().st_size,
                            title=path.name, metadata={"source": "twitter_media"},
                        )
                    )
                except OSError:
                    pass
            state_label = {"processing": "下载中", "completed": "完成", "skipped": "跳过", "failed": "失败"}.get(result, result)
            detail = f"：{path.name}" if path is not None else ""
            if reason:
                detail += f"（{reason}）"
            level = "error" if result == "failed" else "warning" if result == "skipped" else "info"
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id, task.module_id, level,
                    f"{media_label}{state_label}{detail}",
                    status="running",
                    current=_media_progress["done"],
                    total=_media_progress["total"],
                    metadata={
                        "phase": "media", "result": result, "media_type": media_type,
                        "filename": path.name if path is not None else "",
                    },
                )
            )

        status_match = re.search(r"/status/(\d+)(?:/|$)", target_url)
        pending_tracker = PendingMediaTracker()
        if status_match:
            callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", "正在读取 X 公开单帖媒体资料")
            )
            syndication_stats = download_status_from_syndication(
                status_match.group(1),
                task.output_dir,
                download_types,
                DownloadRecord(record_path),
                DownloadFailureRecord(failed_path),
                config,
                cancel_event,
                report_media_result,
                pending_tracker=pending_tracker, network_config=network_config,
            )
            if syndication_stats is not None:
                success = sum(int(syndication_stats.get(f"success_{kind}") or 0) for kind in ("image", "video", "gif", "audio"))
                reused = int(syndication_stats.get("skipped_record") or 0) + int(syndication_stats.get("skipped_existing") or 0)
                if success or reused:
                    callbacks.on_progress(
                        ProgressEvent(
                            task.task_id, task.module_id, "info", "X 公开单帖媒体下载完成",
                            metadata={"stats": syndication_stats, "crawler_version": CRAWLER_VERSION,
                                      "source": "syndication"},
                        )
                    )
                    report_pending_summary(task, callbacks, pending_tracker, save_unfinished_txt)
                    return syndication_stats
                callbacks.on_progress(
                    ProgressEvent(task.task_id, task.module_id, "warning", "X 单帖媒体直连下载未完成，改用浏览器扫描")
                )

        browser_lease = BROWSER_POOL.acquire(cancel_event)
        try:
            driver = initialize_authenticated_driver(str(cookie_path), config)
        except BaseException:
            browser_lease.release()
            raise
        with self._lock:
            self._drivers[task.task_id] = driver
        try:
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            profile_handle = profile_target_to_handle(task.target)
            if profile_handle:
                profile_file = self.runtime_data_dir / "profile_previews" / f"{profile_handle}.json"
                callbacks.on_progress(
                    ProgressEvent(task.task_id, task.module_id, "info", f"下载前补充主页资料: @{profile_handle}")
                )
                try:
                    collect_profile_preview_with_driver(
                        driver,
                        profile_file,
                        task.target,
                        page_load_timeout=max(10, min(int(config.get("page_load_timeout", 30) or 30), 30)),
                    )
                except Exception as exc:  # Profile enrichment must not block media downloading.
                    raise_browser_failure(exc, cancel_event, driver)
                    callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "warning",
                            f"主页资料暂未补全，继续下载媒体: {str(exc).splitlines()[0]}",
                        )
                    )
                if task_cancelled(cancel_event):
                    raise TaskCancelled("任务已取消")
            stats = download_one_target(
                driver,
                target_url,
                task.output_dir,
                download_types,
                config,
                DownloadRecord(record_path),
                DownloadFailureRecord(failed_path),
                cancel_event=cancel_event,
                on_status=lambda message: callbacks.on_progress(
                    ProgressEvent(task.task_id, task.module_id, "warning", message, status="running")
                ),
                on_media_result=report_media_result,
                pending_tracker=pending_tracker, network_config=network_config,
            )
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    "Twitter 媒体发现与下载完成",
                    metadata={"stats": stats, "crawler_version": CRAWLER_VERSION},
                )
            )
            return stats
        except Exception as exc:
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消") from exc
            raise_browser_failure(exc, cancel_event, driver)
            raise
        finally:
            try:
                self._finish_browser(driver, task, callbacks, pending_tracker, save_unfinished_txt)
            finally:
                browser_lease.release()

    def _finish_browser(self, driver, task, callbacks, pending_tracker, save_unfinished_txt):
        try:
            with self._lock:
                self._drivers.pop(task.task_id, None)
            try:
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "info", "媒体任务已结束，正在关闭 Twitter 浏览器",
                    status="running", metadata={"phase": "cleanup"}))
            except Exception:
                pass
        finally:
            try:
                driver.quit()
            except Exception as exc:
                try:
                    callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "warning",
                        "Twitter 浏览器退出未确认：" + safe_error_message(exc)))
                except Exception:
                    pass
            finally:
                try:
                    report_pending_summary(task, callbacks, pending_tracker, save_unfinished_txt)
                except Exception:
                    pass

    def cancel(self, task_id: str) -> None:
        with self._lock:
            driver = self._drivers.get(task_id)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def _apply_options(config: dict[str, Any], options: dict[str, Any]) -> None:
        option_map = {
            "convert_gif": "convert_gif",
            "keep_gif_mp4": "keep_gif_mp4",
            "gif_fps": "gif_fps",
            "gif_width": "gif_width",
            "audio_format": "audio_format",
            "image_format": "image_format",
            "download_workers": "download_workers",
            "max_retries": "max_retries",
            "proxy_url": "proxy_url",
            "use_system_proxy": "use_system_proxy",
            "rename_downloads": "rename_downloads",
            "delete_source_after_rename": "delete_source_after_rename",
        }
        for option_name, config_name in option_map.items():
            if option_name in options:
                config[config_name] = options[option_name]
