from __future__ import annotations

import json
import multiprocessing
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from software_app.adapters.twitter import TwitterNativeAdapter
from software_app.core.output_lock import output_path_lock, _lock_root
from software_app.crawlers.twitter import download_method as records
from software_app.crawlers.twitter.download_method import DownloadFailureRecord, DownloadRecord, JsonDownloadRecord, JsonListRecord
from software_app.ui.following_tab import FollowingTabMixin


def _isolated_temp(root, name):
    directory = root / "process-temp" / name
    directory.mkdir(parents=True, exist_ok=True)
    for key in ("TEMP", "TMP", "TMPDIR"):
        os.environ[key] = str(directory)
    tempfile.tempdir = None
    _lock_root.cache_clear()


def _worker(kind, directory, index, count, ready, start):
    root = Path(directory)
    _isolated_temp(root, str(index))
    adapter = TwitterNativeAdapter(root / "crawler")
    adapter.runtime_data_dir = root
    # All children prime their independent cache before the parent releases them.
    record = DownloadFailureRecord(root / "failed_urls.json") if kind == "failure" else DownloadRecord(root / "downloaded_urls.json")
    ready.send(True)
    ready.close()
    if not start.wait(15):
        raise RuntimeError("worker start timed out")
    for offset in range(count):
        if kind == "success":
            record.mark_success(f"{index}-{offset}", "image", root / f"{index}-{offset}.png")
        elif kind == "failure":
            record.mark_failed("shared", "video", "retry")
        elif kind == "user":
            adapter._mark_downloaded_user("@shared", "https://x.com/shared/media", root / "media")
        elif kind == "hidden":
            adapter._update_hidden_history_keys(add={f"{index}-{offset}"})
        else:
            raise AssertionError(kind)


def _hold_record(directory, ready):
    _isolated_temp(Path(directory), "holder")
    record = DownloadRecord(Path(directory) / "downloaded_urls.json")
    with record._transaction():
        ready.send(True)
        ready.close()
        # A killed process must not leave a waiter in a shared Condition: the
        # parent's later Event.set() could itself block waiting for its ack.
        threading.Event().wait(30)


class TwitterRecordProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "downloaded_urls.json"
        self.adapter = TwitterNativeAdapter(self.root / "crawler")
        self.adapter.runtime_data_dir = self.root
        self.context = multiprocessing.get_context("spawn")

    def run_workers(self, kind="success", count=12, processes=3, before_release=None):
        start = self.context.Event()
        children, receivers = [], []
        try:
            for index in range(processes):
                receiving, sending = self.context.Pipe(duplex=False)
                child = self.context.Process(target=_worker, args=(kind, str(self.root), index, count, sending, start))
                child.start()
                sending.close()
                children.append(child)
                receivers.append(receiving)
            for receiving in receivers:
                self.assertTrue(receiving.poll(12), "worker failed to prime its cache")
                self.assertTrue(receiving.recv())
            if before_release:
                before_release()
            start.set()
            for child in children:
                child.join(15)
                self.assertEqual(child.exitcode, 0)
        finally:
            start.set()
            for receiving in receivers:
                receiving.close()
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(5)
                child.close()

    def test_real_process_success_updates_keep_all_entries(self):
        record = DownloadRecord(self.path)
        record.mark_success("seed", "image", self.root / "seed.png")
        self.run_workers()
        self.assertEqual(set(record.snapshot()), {"seed", *(f"{index}-{offset}" for index in range(3) for offset in range(12))})

    def test_real_process_failure_attempts_accumulate(self):
        self.run_workers("failure", count=10)
        item = DownloadFailureRecord(self.root / "failed_urls.json").snapshot()["shared"]
        self.assertEqual(item["attempts"], 30)

    def test_real_process_account_download_counts_accumulate(self):
        self.run_workers("user", count=6)
        self.assertEqual(JsonDownloadRecord(self.adapter.downloaded_users_file()).snapshot()["shared"]["downloads"], 18)

    def test_real_process_hidden_additions_merge(self):
        self.run_workers("hidden", count=10)
        self.assertEqual(self.adapter._hidden_history_keys(), {f"{index}-{offset}" for index in range(3) for offset in range(10)})

    def test_stale_child_cannot_restore_removed_success_entry(self):
        record = DownloadRecord(self.path)
        record.mark_success("removed", "image", self.root / "removed.png")
        record.mark_success("keep", "image", self.root / "keep.png")
        self.run_workers(count=2, before_release=lambda: record.update(lambda items: items.pop("removed")))
        self.assertNotIn("removed", record.snapshot())
        self.assertIn("keep", record.snapshot())
        self.assertEqual(len(record.snapshot()), 7)

    def test_stale_child_cannot_undo_full_history_clear(self):
        DownloadRecord(self.path).mark_success("old", "image", self.root / "old.png")
        self.run_workers(count=1, before_release=self.adapter.clear_download_records)
        self.assertEqual(set(DownloadRecord(self.path).snapshot()), {"0-0", "1-0", "2-0"})

    def test_stale_child_cannot_restore_removed_failure_or_old_attempt_count(self):
        path = self.root / "failed_urls.json"
        record = DownloadFailureRecord(path)
        for _ in range(4):
            record.mark_failed("shared", "video", "old")
        self.run_workers("failure", count=1, before_release=lambda: record.remove("shared"))
        self.assertEqual(record.snapshot()["shared"]["attempts"], 3)

    def test_kernel_release_after_actual_process_termination_and_bounded_wait(self):
        record = DownloadRecord(self.path)
        record.mark_success("keep", "image", self.root / "keep.png")
        original = self.path.read_bytes()
        receiving, sending = self.context.Pipe(duplex=False)
        child = self.context.Process(target=_hold_record, args=(str(self.root), sending))
        child.start()
        sending.close()
        try:
            self.assertTrue(receiving.poll(12))
            receiving.recv()
            with patch.object(records, "RECORD_LOCK_TIMEOUT", 0.02):
                with self.assertRaises(TimeoutError):
                    record.mark_success("waiting", "image", self.root / "waiting.png")
            self.assertEqual(self.path.read_bytes(), original)
            child.terminate()
            child.join(5)
            self.assertFalse(child.is_alive())
            record.mark_success("after-exit", "image", self.root / "after.png")
            self.assertEqual(set(record.snapshot()), {"keep", "after-exit"})
        finally:
            receiving.close()
            if child.is_alive():
                child.terminate()
            child.join(5)
            child.close()


class TwitterRecordTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "downloaded_urls.json"
        self.adapter = TwitterNativeAdapter(self.root / "crawler")
        self.adapter.runtime_data_dir = self.root

    def test_write_reloads_even_when_external_file_signature_does_not_change(self):
        self.path.write_text('{"items":{"old":{"file":"old.png"}}}', encoding="utf-8")
        record = DownloadRecord(self.path)
        before = self.path.stat()
        self.path.write_text('{"items":{"new":{"file":"new.png"}}}', encoding="utf-8")
        os.utime(self.path, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(self.path.stat().st_size, before.st_size)
        record.mark_success("third", "image", self.root / "third.png")
        self.assertEqual(set(record.snapshot()), {"new", "third"})

    def test_corrupt_json_is_preserved_by_updates_and_explicit_clear(self):
        for payload in (b'{"items":', b'{"items":[]}', b'[]', b'\xff\xfe'):
            with self.subTest(payload=payload):
                self.path.write_bytes(payload)
                record = DownloadRecord(self.path)
                for action in (lambda: record.mark_success("new", "image", "new.png"), record.clear):
                    with self.assertRaisesRegex(ValueError, "未覆盖原文件"):
                        action()
                    self.assertEqual(self.path.read_bytes(), payload)

    def test_mutator_failure_does_not_publish_nested_changes(self):
        record = JsonDownloadRecord(self.path)
        record.update(lambda items: items.update({"keep": {"count": 1}}))
        before = self.path.read_bytes()

        def fail(items):
            items["keep"]["count"] = 9
            raise RuntimeError("stop before save")

        with self.assertRaises(RuntimeError):
            record.update(fail)
        self.assertEqual(record.snapshot(), {"keep": {"count": 1}})
        self.assertEqual(self.path.read_bytes(), before)

    def test_atomic_replace_failure_preserves_user_and_hidden_records(self):
        self.adapter._mark_downloaded_user("@keep", "https://x.com/keep", self.root)
        self.adapter._update_hidden_history_keys(add={"keep"})
        paths = [self.adapter.downloaded_users_file(), self.adapter._hidden_history_file()]
        before = [path.read_bytes() for path in paths]
        with patch.object(records, "replace_with_retry", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.adapter._mark_downloaded_user("@other", "https://x.com/other", self.root)
            with self.assertRaises(PermissionError):
                self.adapter._update_hidden_history_keys(add={"other"})
        self.assertEqual([path.read_bytes() for path in paths], before)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_legacy_dictionary_and_bom_list_round_trip(self):
        self.path.write_text('{"old":{"file":"old.png"}}', encoding="utf-8-sig")
        record = DownloadRecord(self.path)
        record.mark_success("new", "image", self.root / "new.png")
        self.assertEqual(set(record.snapshot()), {"old", "new"})
        hidden = self.adapter._hidden_history_file()
        hidden.write_text(json.dumps({"items": ["One", "Two"]}), encoding="utf-8-sig")
        self.adapter._update_hidden_history_keys(add={"three"}, remove={"one"})
        self.assertEqual(JsonListRecord(hidden).snapshot(), ["three", "two"])

    def test_corrupt_group_member_prevents_other_selected_cleanup_writes(self):
        self.path.write_bytes(b'{"items":')
        hidden = self.adapter._hidden_history_file()
        hidden.write_text('{"items":["keep"]}', encoding="utf-8")
        before = hidden.read_bytes()
        with self.assertRaises(ValueError):
            self.adapter.clear_selected_download_records([{"handle": "remove"}])
        self.assertEqual(hidden.read_bytes(), before)

    def test_selected_cleanup_preserves_unrelated_concurrent_history(self):
        media = self.root / "remove" / "image.png"
        media.parent.mkdir()
        media.write_bytes(b"generated sample")
        DownloadRecord(self.path).mark_success("remove-media", "image", media)
        self.adapter._mark_downloaded_user("@remove", "https://x.com/remove", self.root)

        def clear():
            for index in range(8):
                self.adapter.clear_selected_download_records([{"handle": "remove", "name": f"hidden-{index}"}])

        def update():
            for _ in range(8):
                self.adapter._mark_downloaded_user("@keep", "https://x.com/keep", self.root)

        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(clear), pool.submit(update)]
            for job in jobs:
                job.result(timeout=10)
        self.assertEqual(JsonDownloadRecord(self.adapter.downloaded_users_file()).snapshot()["keep"]["downloads"], 8)
        self.assertEqual(self.adapter._hidden_history_keys(), {"remove", *(f"hidden-{index}" for index in range(8))})
        self.assertFalse(DownloadRecord(self.path).has("remove-media"))
        self.assertTrue(media.is_file())

    def test_runtime_initialization_cannot_overwrite_a_waiting_record_update(self):
        source = self.root / "crawler"
        source.mkdir()
        (source / "downloaded_urls.json").write_text('{"items":{"seed":{}}}', encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        real_write = __import__("software_app.adapters.twitter", fromlist=["write_text_atomic"]).write_text_atomic

        def write(path, text, **kwargs):
            if path == self.path:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test failed to release initialization")
            return real_write(path, text, **kwargs)

        with patch("software_app.adapters.twitter.write_text_atomic", side_effect=write), ThreadPoolExecutor(max_workers=2) as pool:
            initialized = pool.submit(self.adapter._ensure_runtime_files)
            try:
                self.assertTrue(entered.wait(3))
                def save():
                    DownloadRecord(self.path).mark_success("new", "image", self.root / "new.png")
                saved = pool.submit(save)
                self.assertFalse(saved.done())
            finally:
                release.set()
            initialized.result(timeout=5)
            saved.result(timeout=5)
        self.assertEqual(set(DownloadRecord(self.path).snapshot()), {"seed", "new"})

    def test_snapshot_is_independent_and_group_lock_reentrancy_does_not_deadlock(self):
        with output_path_lock(self.path):
            record = JsonDownloadRecord(self.path)
            record.update(lambda items: items.update({"nested": {"count": 1}}))
            snapshot = record.snapshot()
            snapshot["nested"]["count"] = 99
            self.assertEqual(record.snapshot()["nested"]["count"], 1)

    def test_failed_full_clear_does_not_delete_database_records(self):
        adapter = Mock()
        adapter.clear_selected_download_records.side_effect = TimeoutError("busy")
        desktop = SimpleNamespace(manager=Mock(), storage=Mock(), twitter_history_rows=[])
        desktop.manager.get_adapter.return_value = adapter
        with patch("software_app.ui.following_tab.messagebox") as messages:
            messages.askyesno.return_value = True
            FollowingTabMixin._clear_twitter_download_records(desktop)
        messages.showerror.assert_called_once()
        desktop.storage.delete_task_records.assert_not_called()

    def test_failed_selected_clear_does_not_delete_database_records(self):
        adapter = Mock()
        adapter.clear_selected_download_records.side_effect = TimeoutError("busy")
        desktop = SimpleNamespace(manager=Mock(), storage=Mock(), module_var=Mock(),
                                  _selected_platform_histories=lambda: [{"handle": "keep"}])
        desktop.module_var.get.return_value = "twitter"
        desktop.manager.get_adapter.return_value = adapter
        with patch("software_app.ui.following_tab.messagebox") as messages:
            messages.askyesno.return_value = True
            FollowingTabMixin._clear_selected_platform_history(desktop)
        messages.showerror.assert_called_once()
        desktop.storage.delete_task_records.assert_not_called()


if __name__ == "__main__":
    unittest.main()
