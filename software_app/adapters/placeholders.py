from __future__ import annotations

import threading
from pathlib import Path

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview


class PlaceholderAdapter(CrawlerAdapter):
    def __init__(
        self,
        module_id: str,
        display_name: str,
        description: str,
        script_root: Path | None = None,
        capabilities: tuple[str, ...] = ("preview",),
    ) -> None:
        self.info = ModuleInfo(
            module_id=module_id,
            display_name=display_name,
            description=description,
            stage="planned",
            capabilities=capabilities,
            script_root=script_root,
        )

    def validate(self) -> list[str]:
        if self.info.script_root and not self.info.script_root.exists():
            return [f"目录不存在: {self.info.script_root}"]
        return ["模块骨架已创建，下载适配尚未接入"]

    def can_handle(self, raw_target: str) -> bool:
        value = raw_target.strip().lower()
        return bool(value and self.module_id in value)

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        return TargetPreview(
            module_id=self.module_id,
            raw_target=raw_target,
            normalized_target=raw_target.strip(),
            title=self.display_name,
            description=self.info.description,
            status="planned",
            warnings=self.validate(),
            metadata={"stage": self.info.stage, "options": options or {}},
        )

    def download(
        self,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> None:
        raise NotImplementedError(f"{self.display_name} 还没有接入下载逻辑")


def create_placeholder_adapters() -> list[PlaceholderAdapter]:
    return [
        PlaceholderAdapter(
            "bluesky",
            "Bluesky",
            "规划中的软件内置爬虫：关注账号、用户/帖子搜索及图片视频下载尚未实现。",
        ),
        PlaceholderAdapter(
            "instagram",
            "Instagram",
            "规划中的软件内置爬虫：账号、帖子、Reels 及需登录的关注关系尚未实现。",
        ),
    ]
