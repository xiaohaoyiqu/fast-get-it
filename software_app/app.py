from __future__ import annotations

from dataclasses import dataclass

from .adapters import create_adapter_bundle
from .core.plugin_loader import PluginReport
from .core.storage import AppStorage
from .core.task_manager import TaskManager
from .core.mixed_batch import write_batch_report
from .core.security import safe_error_message


@dataclass
class AppContext:
    storage: AppStorage
    task_manager: TaskManager
    plugin_reports: tuple[PluginReport, ...] = ()


def create_app_context() -> AppContext:
    storage = AppStorage()
    task_manager = TaskManager(storage)
    unfinished_batches = storage.unfinished_batch_ids()
    storage.recover_interrupted_tasks()
    for batch_id in unfinished_batches:
        try:
            write_batch_report(storage, batch_id)
        except Exception as exc:
            storage.add_log("", "system", "warning", "恢复批次汇总失败：" + safe_error_message(exc))
    bundle = create_adapter_bundle()
    for adapter in bundle.adapters:
        task_manager.register_adapter(adapter)
    return AppContext(storage=storage, task_manager=task_manager, plugin_reports=bundle.reports)
