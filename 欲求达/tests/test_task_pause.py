from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.models import DownloadTask, ModuleInfo, ProgressEvent, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_control import TaskControl, task_cancelled, task_wait
from software_app.core.task_manager import TaskManager
from software_app.ui.task_queue_tab import TaskQueueTabMixin
from software_app.core.http_transfer import download_http_file


class ControlledAdapter(CrawlerAdapter):
    info = ModuleInfo("controlled", "Controlled")
    max_concurrency = 1

    def __init__(self):
        self.entered = {target: threading.Event() for target in ("first", "second", "third")}
        self.advance = threading.Event()
        self.next_step = threading.Event()
        self.previewed = []

    def preview_target(self, raw_target, options=None):
        self.previewed.append(raw_target)
        return TargetPreview(self.module_id, raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        self.entered[task.target].set()
        if task.target == "first":
            if not self.advance.wait(5):
                raise RuntimeError("test failed to release adapter")
            if task_cancelled(cancel_event):
                raise TaskCancelled("cancelled")
            self.next_step.set()
        callbacks.on_progress(ProgressEvent(task.task_id, self.module_id, "info", "work done", current=1, total=1))


class TaskControlTests(unittest.TestCase):
    def test_pause_does_not_change_nonblocking_event_observation(self):
        control = TaskControl()
        control.pause()
        self.assertFalse(control.is_set())
        started, finished = threading.Event(), threading.Event()
        results = []

        def worker():
            started.set()
            results.append(task_cancelled(control))
            finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(started.wait(2))
            self.assertFalse(finished.wait(0.05))
            control.resume()
            self.assertTrue(finished.wait(2))
            self.assertEqual(results, [False])
        finally:
            control.set()
            thread.join(2)

    def test_cancel_wakes_a_paused_checkpoint(self):
        control = TaskControl()
        control.pause()
        finished = threading.Event()
        results = []

        def worker():
            results.append(task_wait(control, 120))
            finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.05))
            control.set()
            self.assertTrue(finished.wait(2))
            self.assertEqual(results, [True])
        finally:
            control.set()
            thread.join(2)

    def test_pause_time_is_excluded_from_retry_delay(self):
        control = TaskControl()
        control.pause()
        finished = threading.Event()

        def worker():
            task_wait(control, 0.2)
            finished.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.25))
            control.resume()
            self.assertFalse(finished.wait(0.05))
            self.assertTrue(finished.wait(2))
        finally:
            control.set()
            thread.join(2)


class TaskPauseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.storage = AppStorage(Path(self.directory.name) / "tasks.sqlite3")
        self.manager = TaskManager(self.storage)
        self.adapter = ControlledAdapter()
        self.manager.register_adapter(self.adapter)

    def tearDown(self):
        self.adapter.advance.set()
        self.manager.shutdown(timeout=5)
        self.directory.cleanup()

    def start(self, target):
        return self.manager.start_task(self.adapter.module_id, target, Path(self.directory.name) / "out")

    def status(self, task):
        return self.storage.get_task(task.task_id)["status"]

    def wait_status(self, task, status):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if self.status(task) == status:
                return
            threading.Event().wait(0.01)
        self.fail(f"expected {status}, got {self.status(task)}")

    def test_running_task_resumes_in_place_without_redownloading(self):
        task = self.start("first")
        self.assertTrue(self.adapter.entered["first"].wait(3))
        self.assertTrue(self.manager.pause(task.task_id))
        self.assertEqual(self.status(task), "paused")
        self.assertFalse(self.manager.pause(task.task_id))
        self.adapter.advance.set()
        self.assertFalse(self.adapter.next_step.wait(0.1))
        self.assertTrue(self.manager.resume(task.task_id))
        self.manager.wait(task.task_id, 3)
        self.assertTrue(self.adapter.next_step.is_set())
        self.assertEqual(self.status(task), "completed")
        self.assertEqual(self.adapter.previewed.count("first"), 1)
        self.assertEqual(len(self.storage.list_tasks()), 1)

    def test_paused_queued_task_does_not_start_or_block_other_queued_work(self):
        first = self.start("first")
        self.assertTrue(self.adapter.entered["first"].wait(3))
        second = self.start("second")
        self.assertTrue(self.manager.pause(second.task_id))
        self.adapter.advance.set()
        self.manager.wait(first.task_id, 3)
        third = self.start("third")
        self.manager.wait(third.task_id, 3)
        self.assertEqual(self.status(third), "completed")
        self.assertFalse(self.adapter.entered["second"].is_set())
        self.assertNotIn("second", self.adapter.previewed)
        self.assertEqual(self.status(second), "paused")
        self.assertTrue(self.manager.resume(second.task_id))
        self.manager.wait(second.task_id, 3)
        self.assertEqual(self.status(second), "completed")

    def test_paused_running_task_keeps_its_platform_slot(self):
        first = self.start("first")
        self.assertTrue(self.adapter.entered["first"].wait(3))
        self.manager.pause(first.task_id)
        self.adapter.advance.set()
        second = self.start("second")
        self.assertFalse(self.adapter.entered["second"].wait(0.1))
        self.assertEqual(self.status(second), "queued")
        self.manager.resume(first.task_id)
        self.manager.wait(first.task_id, 3)
        self.manager.wait(second.task_id, 3)
        self.assertEqual(self.status(second), "completed")

    def test_cancel_paused_running_task_unblocks_all_checkpoints(self):
        task = self.start("first")
        self.assertTrue(self.adapter.entered["first"].wait(3))
        self.manager.pause(task.task_id)
        self.adapter.advance.set()
        self.manager.cancel(task.task_id)
        self.manager.wait(task.task_id, 3)
        self.assertEqual(self.status(task), "cancelled")
        self.assertFalse(self.adapter.next_step.is_set())
        self.assertFalse(self.manager.resume(task.task_id))

    def test_shutdown_cancels_paused_queued_and_running_tasks(self):
        first = self.start("first")
        self.assertTrue(self.adapter.entered["first"].wait(3))
        second = self.start("second")
        self.manager.pause(first.task_id)
        self.manager.pause(second.task_id)
        self.adapter.advance.set()
        self.assertEqual(self.manager.shutdown(timeout=3), [])
        self.assertEqual(self.status(first), "cancelled")
        self.assertEqual(self.status(second), "cancelled")

    def test_late_pause_and_resume_cannot_reopen_terminal_state(self):
        control = TaskControl()
        task = DownloadTask("terminal", self.adapter.module_id, "first", Path(self.directory.name))
        self.storage.create_task(task.task_id, task.module_id, task.target, str(task.output_dir), {})
        self.storage.update_task_status(task.task_id, "completed")
        self.manager.running[task.task_id] = SimpleNamespace(
            task=task, cancel_event=control, finishing=False, adapter=self.adapter,
        )
        try:
            self.assertFalse(self.manager.pause(task.task_id))
            self.assertFalse(self.manager.resume(task.task_id))
            self.assertEqual(self.status(task), "completed")
            self.assertFalse(control.paused)
        finally:
            self.manager.running.pop(task.task_id)

    def test_restart_marks_paused_task_interrupted_without_auto_start(self):
        self.storage.create_task("old", self.adapter.module_id, "second", self.directory.name, {})
        self.storage.update_task_status("old", "paused")
        self.assertEqual(self.storage.recover_interrupted_tasks(), 1)
        self.assertEqual(self.storage.get_task("old")["status"], "cancelled")

    def test_ui_actions_only_target_eligible_selected_rows(self):
        ui = TaskQueueTabMixin()
        rows = [{"task_id": "q", "status": "queued"}, {"task_id": "p", "status": "paused"},
                {"task_id": "c", "status": "completed"}]
        ui._selected_task_rows = lambda: rows
        ui.manager = SimpleNamespace(pause=Mock(return_value=True), resume=Mock(return_value=True))
        ui.status_var = Mock()
        ui._append_log = Mock()
        ui._refresh_tasks = Mock()
        ui._pause_selected_tasks()
        ui.manager.pause.assert_called_once_with("q")
        ui._resume_selected_tasks()
        ui.manager.resume.assert_called_once_with("p")

    def test_ui_keeps_paused_progress_in_the_aggregate(self):
        ui = TaskQueueTabMixin()
        ui.task_progress_var = Mock()
        ui._task_progress_state = {"p": {"current": 80, "total": 100, "status": "paused"},
                                   "r": {"current": 20, "total": 100, "status": "running"}}
        ui._recompute_task_progress()
        ui.task_progress_var.set.assert_called_once_with(50.0)

    def test_pause_keeps_batch_monitoring_until_tasks_really_finish(self):
        ui = TaskQueueTabMixin()
        ui._task_follow_order = ["p"]
        ui.task_rows = {"p": {"status": "paused"}}
        ui.task_tree = Mock()
        ui.task_tree.selection.return_value = ["p"]
        ui._focus_followed_task()
        self.assertEqual(ui._task_follow_order, ["p"])

    def test_progress_callbacks_are_checkpoints_for_external_adapters(self):
        started, release, after_progress = threading.Event(), threading.Event(), threading.Event()
        original_download = self.adapter.download

        def download(task, callbacks, cancel_event):
            started.set()
            if not release.wait(3):
                raise RuntimeError("test adapter not released")
            callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", "checkpoint"))
            after_progress.set()

        self.adapter.download = download
        try:
            task = self.start("first")
            self.assertTrue(started.wait(3))
            self.manager.pause(task.task_id)
            release.set()
            self.assertFalse(after_progress.wait(0.1))
            self.manager.resume(task.task_id)
            self.manager.wait(task.task_id, 3)
            self.assertTrue(after_progress.is_set())
            self.assertEqual(self.status(task), "completed")
        finally:
            release.set()
            self.adapter.download = original_download

    def test_paused_http_stream_continues_the_same_request_without_losing_bytes(self):
        control = TaskControl()
        first_chunk, continue_stream, done = threading.Event(), threading.Event(), threading.Event()
        failures = []
        destination = Path(self.directory.name) / "stream.mp4"

        def chunks(**_kwargs):
            yield b"abc"
            first_chunk.set()
            if not continue_stream.wait(3):
                raise RuntimeError("test stream not released")
            yield b"def"

        response = SimpleNamespace(status_code=200, headers={"Content-Length": "6", "ETag": '"v1"'},
                                   raise_for_status=Mock(), close=Mock(), iter_content=chunks)
        session = SimpleNamespace(get=Mock(return_value=response))

        def worker():
            try:
                download_http_file(session, "https://example.com/media", destination, control,
                                   timeout=(1, 1), chunk_size=3, validate_url=lambda _: None, retries=0)
            except Exception as exc:
                failures.append(exc)
            finally:
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(first_chunk.wait(3))
            control.pause()
            continue_stream.set()
            self.assertFalse(done.wait(0.1))
            self.assertFalse(destination.exists())
            control.resume()
            self.assertTrue(done.wait(3))
            self.assertEqual(failures, [])
            self.assertEqual(destination.read_bytes(), b"abcdef")
            session.get.assert_called_once()
        finally:
            continue_stream.set()
            control.set()
            thread.join(3)
