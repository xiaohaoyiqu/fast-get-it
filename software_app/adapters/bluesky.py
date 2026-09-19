from __future__ import annotations

import threading

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.crawlers.bluesky import BlueskyClient, parse_bluesky_target


class BlueskyNativeAdapter(CrawlerAdapter):
    max_concurrency = 3

    def __init__(self) -> None:
        self.info = ModuleInfo(
            module_id="bluesky",
            display_name="Bluesky",
            description="使用 Bluesky API 读取用户、公开关注、帖子和媒体，并以临时应用密码管理单项关注。",
            stage="ready",
            capabilities=(
                "preview", "search", "download", "native", "profile-links", "public-api", "account-management",
            ),
        )

    @staticmethod
    def _client(options: dict | None = None) -> BlueskyClient:
        return BlueskyClient(proxy_url=str((options or {}).get("proxy_url") or ""))

    def validate(self) -> list[str]:
        return ["公开读取无需登录；关注写入及拉黑/静音读取会临时询问应用密码，密码不保存"]

    def can_handle(self, raw_target: str) -> bool:
        try:
            parse_bluesky_target(raw_target)
            return True
        except ValueError:
            return False

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        target = parse_bluesky_target(raw_target)
        return self._client(options).preview(target, live=bool((options or {}).get("live")))

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        mode = str((options or {}).get("search_mode") or "用户搜索").replace("（规划）", "")
        return self._client(options).search(query, mode, limit)

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        self._client(task.options).download(task, callbacks, cancel_event)
