from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from .events import CallbackSet
from .models import DownloadTask, ModuleInfo, TargetPreview


class TaskCancelled(RuntimeError):
    """Raised by adapters when a task is stopped by user request."""


class CrawlerAdapter(ABC):
    info: ModuleInfo
    max_concurrency: int = 1

    @property
    def module_id(self) -> str:
        return self.info.module_id

    @property
    def display_name(self) -> str:
        return self.info.display_name

    def validate(self) -> list[str]:
        return []

    def can_handle(self, raw_target: str) -> bool:
        return False

    @abstractmethod
    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        """Return a cheap target preview. Heavy browser checks can be added later."""

    @abstractmethod
    def download(
        self,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> None:
        """Run the task and report progress through callbacks."""

    def cancel(self, task_id: str) -> None:
        return None

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        raise NotImplementedError(f"{self.display_name} 不支持搜索")
