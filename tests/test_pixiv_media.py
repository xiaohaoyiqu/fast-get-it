from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from PIL import Image

from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.core.adapter import TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.common import MediaDownloadResult, download_media_file as real_download_media_file
from software_app.crawlers.pixiv.client import PixivCrawler


class PixivMediaTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "state.db")
        self.manager = TaskManager(self.storage)
        self.crawler = object.__new__(PixivCrawler)
        self.crawler.session = Mock()
        self.detail = {
            "illustTitle": "Original",
            "userId": "456",
            "userName": "Artist",
            "illustType": 1,
            "createDate": "2026-10-29T00:00:00Z",
            "tags": {"tags": [{"tag": "sample"}]},
            "aiType": 0,
        }
        self.pages = [
            {"urls": {"original": f"https://i.pximg.net/img-original/123_p{index}.jpg?sig=old"}} for index in (0, 1)
        ]
        self.calls, self.transfers = [], []
        self.fail = {2}
        self.metadata_error = None
        self.rename = False
        self.target = "https://www.pixiv.net/artworks/123"
        self.crawler._body = self.body
        self.adapter = PixivNativeAdapter()
        self.adapter._client = lambda _options=None: self.crawler
        self.adapter.preview_target = lambda target, _options=None: TargetPreview("pixiv", target, target)
        self.manager.register_adapter(self.adapter)
        self.transfer_patch = patch("software_app.crawlers.pixiv.media.download_media_file", side_effect=self.transfer)
        self.transfer_patch.start()
        self.addCleanup(self.transfer_patch.stop)
        self.addCleanup(lambda: self.manager.shutdown(timeout=1))

    def body(self, path):
        self.calls.append(path)
        if self.metadata_error:
            raise self.metadata_error
        return copy.deepcopy(self.pages if path.endswith("/pages") else self.detail)

    def transfer(self, _session, url, _source, destination, _cancel, _suffix, **options):
        self.assertTrue(options["verify_image"])
        index = int(destination.stem)
        self.transfers.append((index, url, destination))
        if index in self.fail:
            raise requests.Timeout("generated timeout")
        if self.rename:
            destination = destination.with_name(f"{destination.stem} (2){destination.suffix}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (8, 8), "blue").save(destination, format="PNG")
        return MediaDownloadResult(destination.stat().st_size, False, destination)

    def start(self):
        task = self.manager.start_task("pixiv", self.target, self.root, {"retries": 0, "filter_ai": True})
        self.manager.wait(task.task_id, 5)
        return task

    def retry(self, task):
        retried = self.manager.retry_task(task.task_id, failed_only=True)
        self.manager.wait(retried.task_id, 5)
        return retried

    def test_single_work_records_partial_pages_and_preserves_metadata(self):
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 1, "failed": 1})
        self.assertEqual(self.storage.media_results(task.task_id, unfinished=True)[0]["index"], 2)
        records = self.storage.task_file_records(task.task_id)
        self.assertEqual((records[0].source_id, records[0].chapter, records[0].author_id), ("123", "1", "456"))
        self.assertEqual(records[0].tags, ("sample",))

    def test_retry_refreshes_only_failed_page_and_keeps_original_folder(self):
        task = self.start()
        original = self.storage.media_results(task.task_id, unfinished=True)[0]
        self.fail.clear()
        self.rename = True
        self.detail.update(illustTitle="Changed", userName="Renamed artist")
        self.pages[1]["urls"]["original"] = self.pages[1]["urls"]["original"].replace("sig=old", "sig=new")
        retry = self.retry(task)
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "completed")
        self.assertEqual([item[0] for item in self.transfers], [1, 2, 2])
        saved = self.storage.media_results(retry.task_id)[0]
        self.assertEqual(Path(saved["path"]).parent, (self.root / original["folder"]).resolve())
        self.assertEqual(Path(saved["path"]).name, "002 (2).png")
        self.assertEqual(self.storage.media_summary(task.task_id)["repaired"], 1)
        self.assertFalse(any("/ajax/user/" in path for path in self.calls))

    def test_changed_page_is_rejected_before_transfer(self):
        task = self.start()
        before = len(self.transfers)
        self.pages[1]["urls"]["original"] = "https://i.pximg.net/img-original/other.jpg"
        retry = self.retry(task)
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "failed")
        self.assertIn("变化", self.storage.get_task(retry.task_id)["error"])
        self.assertEqual(len(self.transfers), before)

    def test_ai_rule_is_rechecked_before_retry(self):
        task = self.start()
        before = len(self.transfers)
        self.detail["aiType"] = 2
        retry = self.retry(task)
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "failed")
        self.assertIn("AI", self.storage.get_task(retry.task_id)["error"])
        self.assertEqual(len(self.transfers), before)
        self.assertEqual(self.storage.task_file_records(retry.task_id), [])
        self.assertEqual(len(self.storage.task_file_records(task.task_id)), 1)

    def test_author_blacklist_is_rechecked_before_retry(self):
        task = self.start()
        before = len(self.transfers)
        self.manager.blocklist.add_group(("pixiv", "456"))
        retry = self.retry(task)
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "failed")
        self.assertIn("黑名单", self.storage.get_task(retry.task_id)["error"])
        self.assertEqual(len(self.transfers), before)

    def test_missing_url_is_a_visible_failure_instead_of_silent_skip(self):
        self.pages[1] = {"urls": {}}
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual([item[0] for item in self.transfers], [1])
        self.assertEqual(self.storage.media_results(task.task_id, unfinished=True)[0]["index"], 2)

    def test_foreign_media_host_is_not_fetched(self):
        self.pages[0]["urls"]["original"] = "https://example.com/123_p0.jpg"
        self.fail.clear()
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual([item[0] for item in self.transfers], [2])

    def test_corrupt_image_does_not_become_a_fatal_disk_error(self):
        def fetch(_session, url, destination, _cancel, **_options):
            destination.parent.mkdir(parents=True, exist_ok=True)
            if "_p0" in url:
                destination.write_bytes(b"<html>login page</html>")
            else:
                Image.new("RGB", (8, 8), "green").save(destination, format="JPEG")
            return destination.stat().st_size

        with (
            patch("software_app.crawlers.pixiv.media.download_media_file", side_effect=real_download_media_file),
            patch("software_app.crawlers.common.download_file", side_effect=fetch),
        ):
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(self.storage.media_summary(task.task_id), {"failed": 1, "saved": 1})
        self.assertIn("媒体内容无效", self.storage.media_results(task.task_id, unfinished=True)[0]["error"])

    def test_failed_metadata_refresh_is_not_repeated_for_every_page(self):
        self.fail = {1, 2}
        task = self.start()
        self.calls.clear()
        self.metadata_error = requests.Timeout("generated metadata timeout")
        retry = self.retry(task)
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "failed")
        self.assertEqual(self.calls, ["/ajax/illust/123"])
        self.assertEqual(self.storage.media_summary(retry.task_id), {"failed": 2})

    def test_cancelled_page_discovery_does_not_start_media_transfers(self):
        cancel = threading.Event()

        def body(path):
            if path.endswith("/pages"):
                cancel.set()
            return self.body(path)

        self.crawler._body = body
        task = DownloadTask("cancel", "pixiv", self.target, self.root)
        with self.assertRaises(TaskCancelled):
            self.crawler.download(task, CallbackSet(), cancel)
        self.assertEqual(self.transfers, [])

    def test_ai_filtered_work_does_not_scan_unrelated_recent_files(self):
        unrelated = self.root / "another-task.jpg"
        Image.new("RGB", (8, 8), "red").save(unrelated)
        self.detail["aiType"] = 2
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        self.assertEqual(self.storage.task_file_records(task.task_id), [])
        self.assertEqual(self.transfers, [])
        self.assertTrue(unrelated.is_file())
