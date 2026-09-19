from __future__ import annotations

from dataclasses import dataclass

from .adapters import create_adapter_bundle
from .core.plugin_loader import PluginReport
from .core.storage import AppStorage
from .core.task_manager import TaskManager


@dataclass
class AppContext:
    storage: AppStorage
    task_manager: TaskManager
    plugin_reports: tuple[PluginReport, ...] = ()


def create_app_context() -> AppContext:
    storage = AppStorage()
    storage.recover_interrupted_tasks()
    task_manager = TaskManager(storage)
    bundle = create_adapter_bundle()
    for adapter in bundle.adapters:
        task_manager.register_adapter(adapter)
    return AppContext(storage=storage, task_manager=task_manager, plugin_reports=bundle.reports)
