from __future__ import annotations

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image

from software_app.core.adapter import CrawlerAdapter
from software_app.core.file_conflict import reuse_existing
from software_app.core.instance_lease import InstanceLease
from software_app.core.models import ModuleInfo, TargetPreview, DownloadTask
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.common import download_media_file
from software_app.crawlers.twitter import download_method as twitter


class Adapter(CrawlerAdapter):
    info = ModuleInfo("test", "Test")

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.tasks = []

    def preview_target(self, raw_target, options=None):
        return TargetPreview("test", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        self.tasks.append(task)
        self.entered.set()
        self.release.wait(3)


class RestartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "app.sqlite3")
        self.manager = TaskManager(self.storage)
        self.adapter = Adapter()
        self.manager.register_adapter(self.adapter)
        self.addCleanup(self.stop)

    def stop(self):
        self.adapter.release.set()
        self.manager.shutdown(5)

    def interrupted(self, status="running", options=None):
        self.storage.create_task("old", "test", "target", str(self.root), options or {"image_format": "webp"})
        self.storage.update_task_status("old", status)
        self.storage.recover_interrupted_tasks()

    def test_restore_keeps_identity_options_directory_and_history(self):
        self.interrupted()
        self.storage.add_log("old", "test", "info", "previous log")
        task = self.manager.restore_interrupted_task("old")
        self.assertTrue(self.adapter.entered.wait(2))
        self.assertEqual(task.task_id, "old")
        self.assertEqual(task.output_dir, self.root)
        self.assertEqual(task.options["image_format"], "webp")
        self.assertFalse(self.storage.interrupted_tasks())
        self.assertEqual(len(self.storage.list_tasks()), 1)
        with self.assertRaises(ValueError):
            self.manager.restore_interrupted_task("old")

    def test_paused_task_stays_paused_until_resume(self):
        self.interrupted("paused")
        # A task paused after execution started has an old started_at timestamp.
        with self.storage.connect() as conn:
            conn.execute("UPDATE tasks SET started_at='2026-10-27 12:00:00' WHERE task_id='old'")
        self.manager.restore_interrupted_task("old")
        self.assertFalse(self.adapter.entered.wait(0.1))
        self.assertEqual(self.storage.get_task("old")["status"], "paused")
        self.assertTrue(self.manager.resume("old"))
        self.assertTrue(self.adapter.entered.wait(2))

    def test_user_cancel_and_completed_are_not_recovery_candidates(self):
        self.interrupted("cancelling")
        self.assertFalse(self.storage.interrupted_tasks())
        with self.assertRaises(ValueError):
            self.manager.restore_interrupted_task("old")

    def test_shutdown_marks_unfinished_task_for_next_start(self):
        task = self.manager.start_task("test", "target", self.root)
        self.assertTrue(self.adapter.entered.wait(2))
        self.manager.shutdown(0)
        self.adapter.release.set()
        self.manager.wait(task.task_id, 3)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertEqual(self.storage.interrupted_tasks()[0]["task_id"], task.task_id)

    def test_live_owner_cannot_be_claimed(self):
        self.interrupted()
        lease = InstanceLease(self.storage.db_path)
        try:
            with self.storage.connect() as conn:
                conn.execute("UPDATE tasks SET owner_id=? WHERE task_id='old'", (lease.owner_id,))
            self.assertFalse(self.storage.claim_interrupted_task("old", "other"))
        finally:
            lease.close()

    def test_two_instances_cannot_claim_same_task(self):
        self.interrupted()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda owner: self.storage.claim_interrupted_task("old", owner), ("a", "b")))
        self.assertEqual(results.count(True), 1)

    def test_manual_retry_consumes_original_recovery_marker(self):
        self.interrupted()
        task = self.manager.retry_task("old")
        self.assertNotEqual(task.task_id, "old")
        self.assertFalse(self.storage.interrupted_tasks())
        self.assertEqual(self.storage.get_task("old")["status"], "cancelled")

    def test_bad_options_remain_available_for_repair(self):
        self.interrupted()
        with self.storage.connect() as conn:
            conn.execute("UPDATE tasks SET options_json='[]' WHERE task_id='old'")
        with self.assertRaises(ValueError):
            self.manager.restore_interrupted_task("old")
        self.assertEqual(len(self.storage.interrupted_tasks()), 1)

    def test_thread_start_failure_does_not_leave_ghost_task(self):
        self.interrupted()
        with patch("threading.Thread.start", side_effect=RuntimeError("thread failed")):
            with self.assertRaises(RuntimeError):
                self.manager.restore_interrupted_task("old")
        self.assertFalse(self.manager.running)
        self.assertEqual(self.storage.get_task("old")["status"], "failed")

    def test_restore_keeps_batch_binding(self):
        self.storage.create_batch("batch", str(self.root), [(1, "test", "target")])
        self.interrupted(options={"_batch_id": "batch", "_batch_position": 1})
        self.manager.restore_interrupted_task("old")
        self.assertTrue(self.adapter.entered.wait(2))
        _batch, rows = self.storage.batch_snapshot("batch")
        self.assertEqual(rows[0]["task_id"], "old")

    def test_saved_conflict_is_copied_into_new_task(self):
        self.storage.set_setting("file_conflict", "error")
        task = self.manager.start_task("test", "target", self.root)
        self.assertEqual(task.options["file_conflict"], "error")
        self.storage.set_setting("file_conflict", "overwrite")
        self.assertEqual(task.options["file_conflict"], "error")

    def test_explicit_cancel_is_not_marked_by_later_shutdown(self):
        task = self.manager.start_task("test", "target", self.root)
        self.assertTrue(self.adapter.entered.wait(2))
        self.manager.cancel(task.task_id)
        self.adapter.release.set()
        self.manager.wait(task.task_id, 3)
        self.manager.shutdown()
        self.assertFalse(self.storage.interrupted_tasks())


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "media.mp4"
        self.path.write_bytes(b"previous")

    def transfer(self, policy):
        return download_media_file(Mock(), "https://example.com/media", self.root / ".source.mp4",
                                   self.path, threading.Event(), ".mp4", conflict_policy=policy)

    def test_reuse_performs_no_transfer(self):
        with patch("software_app.crawlers.common.download_file") as download:
            self.assertEqual(self.transfer("reuse"), (8, True))
        download.assert_not_called()

    def test_conflict_error_preserves_previous_file(self):
        with patch("software_app.crawlers.common.download_file") as download:
            with self.assertRaises(FileExistsError):
                self.transfer("error")
        download.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"previous")

    def test_overwrite_failure_preserves_previous_file(self):
        with patch("software_app.crawlers.common.download_file", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                self.transfer("overwrite")
        self.assertEqual(self.path.read_bytes(), b"previous")

    def test_overwrite_success_commits_new_file(self):
        def download(_session, _url, path, *_args, **_kwargs):
            path.write_bytes(b"new")
        with patch("software_app.crawlers.common.download_file", side_effect=download):
            self.assertEqual(self.transfer("overwrite"), (3, False))
        self.assertEqual(self.path.read_bytes(), b"new")

    def test_directory_and_invalid_policy_are_rejected(self):
        with self.assertRaises(FileExistsError):
            reuse_existing(self.root, "overwrite")
        with self.assertRaises(ValueError):
            reuse_existing(self.path, "invalid")

    def test_twitter_overwrite_ignores_success_record(self):
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, "PNG")
        destination = self.root / "named.png"
        destination.write_bytes(b"previous")
        response = Mock()
        response.iter_content.return_value = [buffer.getvalue()]
        record = twitter.DownloadRecord(self.root / "records.json")
        record.mark_success("https://example.com/a.jpg", "image", destination)
        with patch.object(twitter, "request_with_retries", return_value=response):
            twitter.download_pic("https://example.com/a.jpg", self.root, record=record, filename_base="named",
                                 network_config=twitter.configure_downloads(file_conflict="overwrite"))
        with Image.open(destination) as image:
            self.assertEqual(image.size, (2, 2))

    def test_failed_audio_conversion_preserves_previous_file(self):
        destination = self.root / "named.mp3"
        destination.write_bytes(b"previous")
        response = Mock()
        response.iter_content.return_value = [b"video"]
        with patch.object(twitter, "request_with_retries", return_value=response), patch.object(
            twitter, "extract_audio_file", return_value=(False, "conversion failed")):
            twitter.download_audio("https://example.com/video.mp4", self.root, filename_base="named",
                                   network_config=twitter.configure_downloads(file_conflict="overwrite"))
        self.assertEqual(destination.read_bytes(), b"previous")

    def test_failed_gif_conversion_preserves_previous_file_and_source(self):
        gif_path = self.path.with_suffix(".gif")
        gif_path.write_bytes(b"previous gif")
        with patch.object(twitter, "convert_mp4_to_gif", return_value=(False, "failed")):
            result = twitter.finalize_gif_conversion(self.path, twitter.DownloadStats(), file_conflict="overwrite")
        self.assertEqual(result, self.path)
        self.assertEqual(gif_path.read_bytes(), b"previous gif")
        self.assertEqual(self.path.read_bytes(), b"previous")

    def test_failed_ugoira_conversion_preserves_previous_animation(self):
        from software_app.crawlers.pixiv import ugoira
        final = self.root / "123.gif"
        final.write_bytes(b"old gif")
        body = {"src": "https://example.com/frames.zip", "frames": [{"file": "1.jpg", "delay": 10}]}
        with patch.object(ugoira, "download_file"), patch.object(ugoira, "_extract_frames", return_value=[]), patch.object(
                ugoira, "_write_gif", side_effect=RuntimeError("failed")):
            with self.assertRaises(RuntimeError):
                ugoira.download_ugoira(Mock(), body, self.root, "123", {"file_conflict": "overwrite"}, threading.Event())
        self.assertEqual(final.read_bytes(), b"old gif")

    def test_browser_payload_reuses_old_media_and_rejects_error_policy(self):
        from software_app.crawlers.webpage.client import WebPageCrawler
        from software_app.crawlers.webpage.client import MediaCandidate
        final = self.root / "001-asset.png"
        final.write_bytes(b"old image")
        candidate = MediaCandidate("https://example.com/asset.png", "image")
        payload = SimpleNamespace(data=b"new image", content_type="image/png", final_url=candidate.url)
        task = DownloadTask("test", "website", "https://example.com", self.root)
        with patch("software_app.crawlers.webpage.client.convert_media_file") as convert:
            record = WebPageCrawler._download_browser_payload(payload, candidate, self.root, 1, 100, task, task.target)
        convert.assert_not_called()
        self.assertEqual(record.size, 9)
        self.assertEqual(final.read_bytes(), b"old image")
        error_task = DownloadTask("test", "website", task.target, self.root, {"file_conflict": "error"})
        with self.assertRaises(FileExistsError):
            WebPageCrawler._download_browser_payload(payload, candidate, self.root, 1, 100, error_task, task.target)

    def test_hls_reuse_and_error_never_start_ffmpeg(self):
        from software_app.crawlers.bluesky import BlueskyClient
        client = BlueskyClient()
        with patch.object(client, "_download_hls_locked") as download:
            client._download_hls("https://example.com/video", self.path, threading.Event(), "reuse")
            with self.assertRaises(FileExistsError):
                client._download_hls("https://example.com/video", self.path, threading.Event(), "error")
            download.assert_not_called()
            client._download_hls("https://example.com/video", self.path, threading.Event(), "overwrite")
            download.assert_called_once()


if __name__ == "__main__":
    unittest.main()
