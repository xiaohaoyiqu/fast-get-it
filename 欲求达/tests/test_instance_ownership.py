from __future__ import annotations

import multiprocessing
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from software_app.app import create_app_context
from software_app.core.adapter import CrawlerAdapter
from software_app.core.instance_lease import InstanceLease, owner_is_alive
from software_app.core.models import ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage, SCHEMA
from software_app.core.task_manager import TaskManager


def _hold_owned_task(db_path, pipe):
    storage = AppStorage(db_path)
    lease = InstanceLease(Path(db_path))
    storage.create_task("child", "test", "target", str(Path(db_path).parent), {}, owner_id=lease.owner_id)
    storage.update_task_status("child", "running")
    pipe.send(lease.owner_id)
    pipe.close()
    while True:
        time.sleep(1)


class BlockingAdapter(CrawlerAdapter):
    info = ModuleInfo("test", "Test")

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def preview_target(self, raw_target, options=None):
        return TargetPreview("test", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        self.entered.set()
        self.release.wait(5)


class InstanceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.storage = AppStorage(self.root / "tasks.sqlite3")
        self.manager = TaskManager(self.storage)
        self.adapter = BlockingAdapter()
        self.manager.register_adapter(self.adapter)

    def tearDown(self):
        self.adapter.release.set()
        self.manager.shutdown(5)
        self.directory.cleanup()

    def start(self):
        task = self.manager.start_task("test", "target", self.root)
        self.assertTrue(self.adapter.entered.wait(3))
        return task

    def test_second_app_context_does_not_cancel_a_live_instance(self):
        task = self.start()
        with patch("software_app.app.AppStorage", return_value=self.storage), patch(
            "software_app.app.create_adapter_bundle", return_value=SimpleNamespace(adapters=[], reports=()),
        ):
            context = create_app_context()
        try:
            self.assertEqual(self.storage.get_task(task.task_id)["status"], "running")
            self.assertEqual(self.storage.get_task(task.task_id)["owner_id"], self.manager._instance_lease.owner_id)
        finally:
            context.task_manager.shutdown()

    def test_paused_live_owner_and_legacy_rows_are_not_recovered_prematurely(self):
        task = self.start()
        self.manager.pause(task.task_id)
        self.storage.create_task("legacy", "test", "old", str(self.root), {})
        self.assertEqual(self.storage.recover_interrupted_tasks(), 0)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "paused")
        self.assertEqual(self.storage.get_task("legacy")["status"], "queued")

    def test_dead_owner_is_recovered_and_live_owner_is_kept(self):
        task = self.start()
        lease = InstanceLease(self.storage.db_path)
        try:
            self.storage.create_task("dead", "test", "old", str(self.root), {}, owner_id=lease.owner_id)
        finally:
            lease.close()
        self.assertEqual(self.storage.recover_interrupted_tasks(), 1)
        self.assertEqual(self.storage.get_task("dead")["status"], "cancelled")
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "running")

    def test_uncertain_liveness_is_not_treated_as_a_crash(self):
        lease = InstanceLease(self.storage.db_path)
        try:
            self.storage.create_task("uncertain", "test", "x", str(self.root), {}, owner_id=lease.owner_id)
            with patch("software_app.core.storage.owner_is_alive", side_effect=OSError("cannot inspect")):
                self.assertEqual(self.storage.recover_interrupted_tasks(), 0)
            self.assertEqual(self.storage.get_task("uncertain")["status"], "queued")
        finally:
            lease.close()

    def test_shutdown_does_not_hide_an_unfinished_worker_from_other_instances(self):
        task = self.start()
        owner = self.storage.get_task(task.task_id)["owner_id"]
        self.assertEqual(self.manager.shutdown(timeout=0), [task.task_id])
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelling")
        self.assertTrue(owner_is_alive(self.storage.db_path, owner))
        self.assertEqual(self.storage.recover_interrupted_tasks(), 0)
        self.adapter.release.set()
        self.manager.wait(task.task_id, 3)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertFalse(owner_is_alive(self.storage.db_path, owner))

    def test_idle_manager_releases_lease_and_can_start_a_new_task(self):
        first = self.start()
        self.adapter.release.set()
        self.manager.wait(first.task_id, 3)
        owner = self.storage.get_task(first.task_id)["owner_id"]
        self.assertFalse(owner_is_alive(self.storage.db_path, owner))
        self.adapter.release.clear()
        second = self.manager.start_task("test", "next", self.root)
        try:
            self.assertTrue(owner_is_alive(self.storage.db_path, owner))
            self.assertEqual(self.storage.recover_interrupted_tasks(), 0)
        finally:
            self.adapter.release.set()
            self.manager.wait(second.task_id, 3)

    def test_live_task_records_and_logs_are_protected_from_other_instances(self):
        task = self.start()
        self.storage.add_log(task.task_id, "test", "info", "keep")
        counts = self.storage.delete_task_records([task.task_id])
        self.assertEqual(counts, {"logs": 0, "files": 0, "tasks": 0})
        with self.assertRaises(ValueError):
            self.storage.clear_history("test")
        self.assertIsNotNone(self.storage.get_task(task.task_id))
        self.assertTrue(self.storage.list_logs(task.task_id))

    def test_thread_start_failure_does_not_leave_an_active_owner_or_ghost_task(self):
        with patch("software_app.core.task_manager.threading.Thread.start", side_effect=RuntimeError("no thread")):
            with self.assertRaises(RuntimeError):
                self.manager.start_task("test", "target", self.root)
        self.assertEqual(self.manager.active_task_ids(), [])
        self.assertEqual(self.storage.list_tasks()[0]["status"], "failed")
        self.assertFalse(owner_is_alive(self.storage.db_path, self.manager._instance_lease.owner_id))

    def test_kernel_releases_owner_lease_after_actual_process_termination(self):
        context = multiprocessing.get_context("spawn")
        receiving, sending = context.Pipe(duplex=False)
        process = context.Process(target=_hold_owned_task, args=(str(self.storage.db_path), sending))
        process.start()
        sending.close()
        try:
            self.assertTrue(receiving.poll(8))
            owner = receiving.recv()
            self.assertTrue(owner_is_alive(self.storage.db_path, owner))
            self.assertEqual(self.storage.recover_interrupted_tasks(), 0)
            process.terminate()
            process.join(5)

            self.assertFalse(process.is_alive())
            self.assertFalse(owner_is_alive(self.storage.db_path, owner))
            self.assertEqual(self.storage.recover_interrupted_tasks(), 1)
            self.assertEqual(self.storage.get_task("child")["status"], "cancelled")
        finally:
            receiving.close()
            if process.is_alive():
                process.terminate()
            process.join(5)

    def test_legacy_task_schema_migrates_without_losing_history(self):
        path = self.root / "legacy.sqlite3"
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.executescript(SCHEMA.replace("    owner_id TEXT NOT NULL DEFAULT '',\n", ""))
            conn.execute("INSERT INTO tasks(task_id,module_id,target,status,output_dir) VALUES ('old','test','x','paused',?)",
                         (str(self.root),))
        storage = AppStorage(path)
        self.assertEqual(storage.get_task("old")["owner_id"], "")
        self.assertEqual(storage.recover_interrupted_tasks(), 1)
        self.assertEqual(storage.get_task("old")["status"], "cancelled")
