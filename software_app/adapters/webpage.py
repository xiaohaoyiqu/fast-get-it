from __future__ import annotations

import threading

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.crawlers.webpage import WebPageCrawler


class WebPageCrawlerAdapter(CrawlerAdapter):
    """Task boundary for pages selected from Google similarity results."""

    max_concurrency = 3

    def __init__(self) -> None:
        self._crawler = WebPageCrawler()
        self.info = ModuleInfo(
            module_id="website",
            display_name="网页资源",
            description="软件内置网页资源爬虫；下载用户选中的候选页面内媒体。",
            stage="alpha",
            capabilities=("preview", "download", "native", "google-candidate"),
        )

    def can_handle(self, raw_target: str) -> bool:
        return self._crawler.preview(raw_target).status == "ok"

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        return self._crawler.preview(raw_target, options)

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        self._crawler.download(task, callbacks, cancel_event)

    def cancel(self, task_id: str) -> None:
        self._crawler.cancel(task_id)
