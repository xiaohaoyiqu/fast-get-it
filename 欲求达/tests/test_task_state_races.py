from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from software_app.core.adapter import CrawlerAdapter
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class EmptyAdapter(CrawlerAdapter):
    info = ModuleInfo("empty", "Empty")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("empty", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        self.cancel_event = cancel_event


class TaskStateRaceTests(unittest.TestCase):
    def test_late_cancel_does_not_reopen_a_finished_task(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            storage = AppStorage(Path(directory) / "tasks.db")
            storage.create_task("finished", "empty", "target", directory, {})
            storage.update_task_status("finished", "completed")
            manager = TaskManager(storage)
            stop = threading.Event()
            adapter = Mock()
            # The worker has saved its final state and is about to remove this entry.
            manager.running["finished"] = SimpleNamespace(
                task=DownloadTask("finished", "empty", "target", Path(directory)),
                cancel_event=stop,
                adapter=adapter,
            )
            manager.cancel("finished")
            self.assertEqual(storage.get_task("finished")["status"], "completed")
            self.assertFalse(stop.is_set())
            adapter.cancel.assert_not_called()

    def test_cancellation_during_fallback_scan_is_not_reported_as_success(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            storage = AppStorage(Path(directory) / "tasks.db")
            manager = TaskManager(storage)
            adapter = EmptyAdapter()
            manager.register_adapter(adapter)

            def scan(*args, **kwargs):
                adapter.cancel_event.set()
                return []

            with patch.object(storage, "scan_media_files", side_effect=scan):
                task = manager.start_task("empty", "target", directory)
                manager.wait(task.task_id, timeout=5)
            self.assertEqual(storage.get_task(task.task_id)["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
