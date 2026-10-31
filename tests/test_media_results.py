from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.media_results import (
    MediaGroupResult,
    media_descriptor,
    refreshed_media,
    retry_folder,
    run_media_jobs,
)
from software_app.core.models import DownloadTask, FileRecord, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class MediaAdapter(CrawlerAdapter):
    info = ModuleInfo("bluesky", "Generated media")
    supports_media_retry = True

    def __init__(self):
        self.fail = True
        self.indices = []

    def preview_target(self, raw_target, options=None):
        return TargetPreview("bluesky", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        items = task.options.get("_retry_media")
        if items is None:
            items = [
                media_descriptor(
                    task,
                    task.target,
                    index,
                    "image",
                    f"https://cdn.example/{index}.jpg?sig=secret",
                    task.output_dir / "original-date",
                )
                for index in (1, 2)
            ]

        def download(item):
            index = item["index"]
            self.indices.append(index)
            if self.fail and index == 2:
                raise ValueError("generated missing media")
            folder = retry_folder(task, item)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{index}.jpg"
            path.write_bytes(b"generated sample")
            return FileRecord(path, "bluesky", task.task_id, "image", path.stat().st_size)

        run_media_jobs(task, callbacks, cancel_event, [(item, lambda item=item: download(item)) for item in items])


class MediaResultTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "state.db")
        self.manager = TaskManager(self.storage)
        self.addCleanup(lambda: self.manager.shutdown(timeout=1))
        self.adapter = MediaAdapter()
        self.manager.register_adapter(self.adapter)
        self.target = "https://bsky.app/profile/example.test/post/abc"

    def start(self, callbacks=None):
        task = self.manager.start_task("bluesky", self.target, self.root, {"archive_mode": "task"}, callbacks)
        self.manager.wait(task.task_id, 5)
        return task

    def test_partial_task_preserves_failed_media_and_skips_archive_cleanup(self):
        with patch("software_app.core.task_manager.run_archive_postprocess") as archive:
            task = self.start()
            archive.assert_not_called()
        row = self.storage.get_task(task.task_id)
        self.assertEqual(row["status"], "partial")
        self.assertIsNotNone(row["finished_at"])
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 1, "failed": 1})
        self.assertEqual(self.storage.media_results(task.task_id, unfinished=True)[0]["index"], 2)
        self.assertNotIn("secret", str(self.storage.media_results(task.task_id)))

    def test_group_commit_is_not_redownloaded_when_companion_notification_fails(self):
        task = DownloadTask("group", "pixiv", "https://www.pixiv.net/artworks/11", self.root)
        folder = self.root / "group"
        folder.mkdir()
        primary, companion = folder / "11.zip", folder / "11.json"
        primary.write_bytes(b"generated archive")
        companion.write_text("{}", encoding="utf-8")
        descriptor = media_descriptor(task, task.target, 1, "animation", "https://i.pximg.net/11.zip", folder)
        records = [
            FileRecord(path, "pixiv", "group", "animation", path.stat().st_size) for path in (primary, companion)
        ]
        events = []
        callbacks = CallbackSet(
            on_progress=events.append, on_file=lambda _record: (_ for _ in ()).throw(RuntimeError("index failed"))
        )
        with self.assertRaisesRegex(RuntimeError, "index failed"):
            run_media_jobs(
                task, callbacks, threading.Event(), [(descriptor, lambda: MediaGroupResult(records[0], (records[1],)))]
            )
        result = [event.metadata["media_result"] for event in events if "media_result" in event.metadata]
        self.assertEqual(result[-1]["state"], "saved")
        self.assertEqual(result[-1]["path"], str(primary.resolve()))

    def test_retry_downloads_only_failed_item_and_marks_original_repaired(self):
        task = self.start()
        self.adapter.fail = False
        retried = self.manager.retry_task(task.task_id, failed_only=True)
        self.manager.wait(retried.task_id, 5)
        self.assertEqual(self.adapter.indices, [1, 2, 2])
        self.assertEqual(self.storage.get_task(retried.task_id)["status"], "completed")
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(self.storage.media_results(task.task_id, unfinished=True), [])
        self.assertEqual(self.storage.media_summary(task.task_id)["repaired"], 1)
        self.assertEqual(retried.options["archive_mode"], "none")
        self.assertTrue((self.root / "original-date" / "2.jpg").is_file())
        with self.assertRaises(ValueError):
            self.manager.retry_task(task.task_id, failed_only=True)

    def test_nested_retry_repairs_ancestors_and_task_deletion_removes_ledger(self):
        task = self.start()
        second = self.manager.retry_task(task.task_id, failed_only=True)
        self.manager.wait(second.task_id, 5)
        self.adapter.fail = False
        third = self.manager.retry_task(second.task_id, failed_only=True)
        self.manager.wait(third.task_id, 5)
        self.assertEqual(self.storage.media_results(task.task_id, unfinished=True), [])
        self.assertEqual(self.storage.media_results(second.task_id, unfinished=True), [])
        self.storage.delete_task_records([task.task_id])
        self.assertEqual(self.storage.media_results(task.task_id), [])
        self.assertTrue((self.root / "original-date" / "1.jpg").is_file())

    def test_cancel_after_file_commit_preserves_saved_and_pending_items(self):
        def cancel(record):
            self.manager.cancel(record.task_id)

        task = self.start(CallbackSet(on_file=cancel))
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 1, "pending": 1})

    def test_url_refresh_accepts_signature_change_but_rejects_reordered_media(self):
        task = DownloadTask("sample", "bluesky", self.target, self.root)
        item = media_descriptor(
            task, self.target, 1, "image", "https://cdn.example/one.jpg?sig=old", self.root / "saved"
        )
        refreshed = {"type": "image", "url": "https://new-cdn.example/one.jpg?sig=new"}
        self.assertIs(refreshed_media([refreshed], item), refreshed)
        with self.assertRaises(ValueError):
            refreshed_media([{"type": "image", "url": "https://cdn.example/different.jpg"}], item)
        with self.assertRaises(ValueError):
            retry_folder(task, {**item, "folder": "../escape"})

    def test_cancellation_never_becomes_an_ordinary_media_failure(self):
        task = DownloadTask("sample", "bluesky", self.target, self.root)
        item = media_descriptor(task, self.target, 1, "image", "https://cdn.example/one.jpg", self.root / "saved")

        def cancel():
            raise TaskCancelled("cancelled")

        events = []
        with self.assertRaises(TaskCancelled):
            run_media_jobs(task, CallbackSet(on_progress=events.append), threading.Event(), [(item, cancel)])
        self.assertFalse(any(event.metadata.get("media_result", {}).get("state") == "failed" for event in events))

    def test_instagram_retry_refreshes_post_and_downloads_only_selected_index(self):
        from software_app.crawlers.instagram import InstagramClient, parse_instagram_target

        target = parse_instagram_target("https://www.instagram.com/p/ABC/")
        self.assertEqual(target.url, "https://www.instagram.com/p/ABC/")
        task = DownloadTask("sample", "instagram", target.url, self.root)
        item = media_descriptor(
            task, target.url, 2, "image", "https://cdn.cdninstagram.com/two.jpg?sig=old", self.root / "original-date"
        )
        task = DownloadTask(task.task_id, task.module_id, task.target, task.output_dir, {"_retry_media": [item]})
        media = [
            {"type": "image", "url": "https://cdn.cdninstagram.com/one.jpg"},
            {"type": "image", "url": "https://cdn.cdninstagram.com/two.jpg?sig=new"},
        ]
        client = InstagramClient()
        record = FileRecord(self.root / "two.jpg", "instagram", task.task_id)
        with (
            patch.object(client, "fetch", return_value={"media": media}) as fetch,
            patch.object(client, "_download_media", return_value=record) as download,
        ):
            client.download(task, CallbackSet(), threading.Event())
        fetch.assert_called_once()
        self.assertEqual(fetch.call_args.args[0].url, target.url)
        download.assert_called_once()
        self.assertIs(download.call_args.args[1], media[1])
        self.assertEqual(download.call_args.args[4], 2)

    def test_bluesky_retry_avoids_author_scan(self):
        from software_app.crawlers.bluesky import BlueskyClient

        task = DownloadTask("sample", "bluesky", "https://bsky.app/profile/example.test", self.root)
        item = media_descriptor(task, self.target, 2, "image", "https://cdn.bsky.app/two.jpg", self.root / "saved")
        task = DownloadTask(task.task_id, task.module_id, task.target, task.output_dir, {"_retry_media": [item]})
        media = [
            {"type": "image", "url": "https://cdn.bsky.app/one.jpg"},
            {"type": "image", "url": "https://cdn.bsky.app/two.jpg"},
        ]
        client = BlueskyClient()
        record = FileRecord(self.root / "two.jpg", "bluesky", task.task_id)
        with (
            patch.object(client, "post", return_value={"media": media}) as post,
            patch.object(client, "author_posts") as author,
            patch.object(client, "_download_media", return_value=record) as download,
        ):
            client.download(task, CallbackSet(), threading.Event())
        author.assert_not_called()
        post.assert_called_once()
        self.assertIs(download.call_args.args[2], media[1])

    def test_disk_full_stops_before_attempting_other_media(self):
        task = DownloadTask("sample", "bluesky", self.target, self.root)
        first = media_descriptor(task, self.target, 1, "image", "https://cdn.example/one.jpg", self.root / "saved")
        second = media_descriptor(task, self.target, 2, "image", "https://cdn.example/two.jpg", self.root / "saved")
        failure = OSError(errno.ENOSPC, "disk full")
        calls, events = [], []

        def fail():
            raise failure

        with self.assertRaises(OSError) as caught:
            run_media_jobs(
                task,
                CallbackSet(on_progress=events.append),
                threading.Event(),
                [(first, fail), (second, lambda: calls.append(2))],
            )
        self.assertIs(caught.exception, failure)
        self.assertEqual(calls, [])
        self.assertEqual(events[-1].metadata["media_result"]["state"], "failed")
        self.assertEqual(len(events[0].metadata["media_plan"]), 2)

    def test_file_notification_failure_still_reports_committed_media(self):
        task = DownloadTask("sample", "bluesky", self.target, self.root)
        item = media_descriptor(task, self.target, 1, "image", "https://cdn.example/one.jpg", self.root / "saved")
        path = self.root / "one.jpg"
        path.write_bytes(b"generated sample")
        record = FileRecord(path, "bluesky", task.task_id)
        failure = RuntimeError("notification failed")
        events = []

        def notify(_record):
            raise failure

        with self.assertRaises(RuntimeError) as caught:
            run_media_jobs(
                task,
                CallbackSet(on_progress=events.append, on_file=notify),
                threading.Event(),
                [(item, lambda: record)],
            )
        self.assertIs(caught.exception, failure)
        self.assertEqual(events[-1].metadata["media_result"]["state"], "saved")
        self.assertTrue(failure._media_transfer_failure)
        self.assertTrue(path.is_file())

    def test_selected_retry_keeps_other_failed_items_untouched(self):
        original = self.start()
        item = media_descriptor(
            original, self.target, 3, "image", "https://cdn.example/3.jpg", self.root / "original-date"
        )
        self.storage.record_media_result(original.task_id, {**item, "state": "failed", "error": "generated"})
        self.adapter.fail = False
        retry = self.manager.retry_task(original.task_id, failed_only=True, media_keys=[item["key"]])
        self.manager.wait(retry.task_id, 5)
        self.assertEqual(self.adapter.indices, [1, 2, 3])
        self.assertEqual([row["index"] for row in self.storage.media_results(original.task_id, unfinished=True)], [2])
        with self.assertRaisesRegex(ValueError, "状态已变化"):
            self.manager.retry_task(original.task_id, failed_only=True, media_keys=[item["key"]])

    def test_direct_subset_creation_cannot_reenable_archive_cleanup(self):
        original = self.start()
        self.adapter.fail = False
        options = {
            "_retry_media": self.storage.media_results(original.task_id, unfinished=True),
            "archive_mode": "task",
            "extract_archives": True,
            "archive_cleanup_sources": True,
        }
        with patch("software_app.core.task_manager.run_archive_postprocess") as archive:
            task = self.manager.start_task("bluesky", self.target, self.root, options)
            self.manager.wait(task.task_id, 5)
        archive.assert_not_called()
        self.assertEqual(task.options["archive_mode"], "none")
        self.assertFalse(task.options["extract_archives"])
        self.assertFalse(task.options["archive_cleanup_sources"])

    def test_recovered_subset_disables_and_persists_archive_protection(self):
        original = self.start()
        self.adapter.fail = False
        options = {
            "_retry_media": self.storage.media_results(original.task_id, unfinished=True),
            "archive_mode": "task",
            "extract_archives": True,
            "archive_cleanup_sources": True,
        }
        self.storage.create_task("old-subset", "bluesky", self.target, str(self.root), options)
        self.storage.update_task_status("old-subset", "running")
        self.storage.recover_interrupted_tasks()
        with patch("software_app.core.task_manager.run_archive_postprocess") as archive:
            task = self.manager.restore_interrupted_task("old-subset")
            self.manager.wait(task.task_id, 5)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        archive.assert_not_called()
        persisted = json.loads(self.storage.get_task(task.task_id)["options_json"])
        self.assertEqual(persisted["archive_mode"], "none")
        self.assertFalse(persisted["extract_archives"])
        self.assertFalse(persisted["archive_cleanup_sources"])

    def test_invalid_subset_recovery_keeps_recovery_marker(self):
        self.storage.create_task("bad-subset", "bluesky", self.target, str(self.root), {"_retry_media": []})
        self.storage.update_task_status("bad-subset", "running")
        self.storage.recover_interrupted_tasks()
        with self.assertRaisesRegex(ValueError, "补下参数"):
            self.manager.restore_interrupted_task("bad-subset")
        self.assertTrue(self.storage.get_task("bad-subset")["recovery_status"])
        self.assertEqual(self.manager.active_task_ids(), [])
        self.assertEqual(self.adapter.indices, [])

    def test_media_pages_filter_and_bound_results(self):
        original = self.start()
        failed = self.storage.media_results(original.task_id, unfinished=True)[0]
        total, page = self.storage.media_page(original.task_id, state="failed", limit=1)
        self.assertEqual(total, 1)
        self.assertEqual(page[0]["key"], failed["key"])
        self.assertEqual(self.storage.media_page(original.task_id, state="failed", offset=1)[1], [])
        self.storage.resolve_media_result(original.task_id, failed["key"], "retry")
        self.assertEqual(self.storage.media_page(original.task_id, state="failed")[0], 0)
        self.assertEqual(self.storage.media_page(original.task_id, state="repaired")[0], 1)
        with self.assertRaises(ValueError):
            self.storage.media_page(original.task_id, state="invalid")

    def seeded_recovery(self, missing=False, reassigned=False):
        entered, release = threading.Event(), threading.Event()

        def preview(target, _options):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("seed timed out")
            return TargetPreview("bluesky", target, target)

        self.adapter.fail = False
        archive_result = SimpleNamespace(removed_sources=[], records=[], warnings=[])
        with (
            patch.object(self.adapter, "preview_target", side_effect=preview),
            patch("software_app.core.task_manager.run_archive_postprocess", return_value=archive_result) as archive,
        ):
            task = self.manager.start_task("bluesky", self.target, self.root, {"archive_mode": "task"})
            try:
                self.assertTrue(entered.wait(2))
                folder = self.root / "original-date"
                folder.mkdir()
                old = folder / "1.jpg"
                if not missing:
                    old.write_bytes(b"previous run")
                    os.utime(old, (1, 1))
                self.storage.upsert_file(
                    FileRecord(old, "bluesky", "other-task" if reassigned else task.task_id, "image")
                )
                for index, state in ((1, "saved"), (2, "pending")):
                    item = media_descriptor(
                        task, self.target, index, "image", f"https://cdn.example/{index}.jpg", folder
                    )
                    self.storage.record_media_result(
                        task.task_id, {**item, "state": state, **({"path": str(old)} if index == 1 else {})}
                    )
            finally:
                release.set()
                self.manager.wait(task.task_id, 5)
        return task, archive

    def test_recovery_archive_includes_previously_indexed_files(self):
        task, archive = self.seeded_recovery()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        archive.assert_called_once()
        self.assertEqual({record.path.name for record in archive.call_args.args[2]}, {"1.jpg", "2.jpg"})
        self.assertEqual(self.adapter.indices, [2])

    def test_missing_old_file_prevents_incomplete_recovery_archive(self):
        task, archive = self.seeded_recovery(missing=True)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertIn("成品已缺失", self.storage.get_task(task.task_id)["error"])
        archive.assert_not_called()

    def test_recovery_archive_refuses_files_reindexed_by_other_tasks(self):
        task, archive = self.seeded_recovery(reassigned=True)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertIn("索引已清理或归属已变化", self.storage.get_task(task.task_id)["error"])
        archive.assert_not_called()


if __name__ == "__main__":
    unittest.main()
