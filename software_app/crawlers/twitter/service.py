from __future__ import annotations

import threading
import re
from pathlib import Path
from typing import Any

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, classify_file

from .download_method import DownloadFailureRecord, DownloadRecord, configure_downloads
from .manga_downloader import download_status_from_syndication
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

        if cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        ensure_cookie_file(str(cookie_path))
        task.output_dir.mkdir(parents=True, exist_ok=True)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        failed_path.parent.mkdir(parents=True, exist_ok=True)

        configure_downloads(
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

        def report_media_result(media_type: str, result: str, file_path=None, reason: str = "") -> None:
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
            state_label = {"completed": "完成", "skipped": "跳过", "failed": "失败"}.get(result, result)
            detail = f"：{path.name}" if path is not None else ""
            if reason:
                detail += f"（{reason}）"
            level = "error" if result == "failed" else "warning" if result == "skipped" else "info"
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id, task.module_id, level,
                    f"{media_label}{state_label}{detail}",
                    status="running",
                    metadata={"phase": "media", "result": result, "media_type": media_type},
                )
            )

        status_match = re.search(r"/status/(\d+)(?:/|$)", target_url)
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
                    return syndication_stats
                callbacks.on_progress(
                    ProgressEvent(task.task_id, task.module_id, "warning", "X 单帖媒体直连下载未完成，改用浏览器扫描")
                )

        driver = initialize_authenticated_driver(str(cookie_path), config)
        with self._lock:
            self._drivers[task.task_id] = driver
        try:
            if cancel_event.is_set():
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
                    callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "warning",
                            f"主页资料暂未补全，继续下载媒体: {str(exc).splitlines()[0]}",
                        )
                    )
                if cancel_event.is_set():
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
            )
            if cancel_event.is_set():
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
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消") from exc
            raise
        finally:
            with self._lock:
                self._drivers.pop(task.task_id, None)
            try:
                callbacks.on_progress(
                    ProgressEvent(
                        task.task_id, task.module_id, "info", "媒体任务已结束，正在关闭 Twitter 浏览器",
                        status="running", metadata={"phase": "cleanup"},
                    )
                )
            except Exception:
                pass
            try:
                driver.quit()
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
        }
        for option_name, config_name in option_map.items():
            if option_name in options:
                config[config_name] = options[option_name]
