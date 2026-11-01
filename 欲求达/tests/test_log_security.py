import copy
import json
import sqlite3
import threading
import unittest
from unittest.mock import Mock

from software_app.core.events import CallbackSet
from software_app.core.models import ProgressEvent
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class LogSecurityTests(unittest.TestCase):
    def test_storage_redacts_direct_logs_without_changing_the_input(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE logs(task_id, module_id, level, message, metadata_json)")
        storage = object.__new__(AppStorage)
        storage.connect = lambda: connection
        storage._log_write_counter = 0
        metadata = {"access_token": "secret-token", "detail": "Authorization: Bearer secret-auth", "count": 3}
        original = copy.deepcopy(metadata)
        storage.add_log(
            "task", "test", "warning", "failed https://files.example/file?token=secret-url&page=2", metadata
        )
        message, encoded = connection.execute("SELECT message,metadata_json FROM logs").fetchone()
        self.assertNotIn("secret-", message + encoded)
        self.assertIn("page=2", message)
        self.assertEqual(json.loads(encoded)["count"], 3)
        self.assertEqual(metadata, original)

    def test_progress_display_is_redacted_but_recovery_records_keep_original_data(self):
        manager = object.__new__(TaskManager)
        manager.storage = Mock()
        manager._lock = threading.RLock()
        manager.running = {}
        received = []
        result = {"key": "media-key", "state": "saved", "target": "https://files.example/item?token=secret-result"}
        plan = [{"target": "https://files.example/item?token=secret-plan", "index": 1}]
        event = ProgressEvent(
            "task",
            "test",
            "info",
            "https://files.example/item?token=secret-message",
            current=2,
            total=3,
            percent=66,
            metadata={"media_plan": plan, "media_result": result, "access_token": "secret-token", "phase": "download"},
        )
        original = copy.deepcopy(event)
        manager._wrap_callbacks(CallbackSet(on_progress=received.append), [], "ancestor").on_progress(event)
        manager.storage.record_media_result.assert_called_once_with("task", result)
        manager.storage.record_media_results.assert_called_once_with("task", plan)
        manager.storage.resolve_media_result.assert_called_once_with("ancestor", "media-key", "task")
        self.assertEqual(event, original)
        displayed = received[0]
        self.assertIsNot(displayed, event)
        self.assertNotIn("secret-", str(displayed))
        self.assertEqual((displayed.current, displayed.total, displayed.percent), (2, 3, 66))
        self.assertEqual(displayed.metadata["phase"], "download")
        logged = manager.storage.add_log.call_args.args
        self.assertNotIn("secret-", str(logged))
        self.assertNotIn("media_plan", logged[4])
        displayed.metadata["media_result"]["state"] = "changed"
        self.assertEqual(event.metadata["media_result"]["state"], "saved")
