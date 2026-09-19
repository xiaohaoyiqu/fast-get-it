from __future__ import annotations

import threading

from software_app.core.adapter import CrawlerAdapter
from software_app.core.browser_driver import chromedriver_status
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.settings import GOOGLE_IMAGE_ROOT
from software_app.crawlers.google_image import GoogleImageCrawler


class GoogleImageNativeAdapter(CrawlerAdapter):
    max_concurrency = 1

    def __init__(self) -> None:
        self._crawler = GoogleImageCrawler()
        self.info = ModuleInfo(
            module_id="google_image",
            display_name="Google 相似图片",
            description="Google Lens 来源页面候选；AI 概览不代表图片匹配，选中候选后请核对原页面。",
            stage="alpha",
            capabilities=("preview", "search", "download", "native", "url-candidates"),
            script_root=GOOGLE_IMAGE_ROOT,
        )

    def validate(self) -> list[str]:
        status = chromedriver_status(auto_download=False)
        return [] if status.source not in {"missing", "incompatible"} else [
            "ChromeDriver 缺失或版本不匹配；搜索时会按本机 Chrome 自动下载"
        ]

    def can_handle(self, raw_target: str) -> bool:
        return self._crawler.preview(raw_target).status == "ok"

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        preview = self._crawler.preview(raw_target, options)
        return TargetPreview(**{**preview.__dict__, "warnings": [*preview.warnings, *self.validate()]})

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        self._crawler.download(task, callbacks, cancel_event)

    def cancel(self, task_id: str) -> None:
        self._crawler.cancel(task_id)

    def search_targets(self, query: str, limit: int = 50, options: dict | None = None) -> list[dict]:
        options = options or {}
        links, _ = self._crawler.search(
            query,
            max_results=limit,
            headless=bool(options.get("headless", False)),
            proxy_url=str(options.get("proxy_url") or ""),
            task_id=str(options.get("task_id") or "search"),
            cancel_event=options.get("cancel_event"),
            manual_wait_seconds=int(options.get("manual_wait_seconds", 60)),
            on_status=options.get("on_status"),
        )
        return [{"title": url, "url": url} for url in links]
