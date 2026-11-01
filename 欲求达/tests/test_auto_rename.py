from __future__ import annotations

import multiprocessing
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import os

from PIL import Image

from software_app.core.adapter import TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.file_conflict import media_output
from software_app.core.models import DownloadTask, FileRecord
from software_app.crawlers.common import download_media_file, write_text_atomic
from software_app.crawlers.twitter import download_method as twitter


def _rename_process(path, ready, start):
    ready.send("ready")
    if not start.wait(10):
        raise RuntimeError("start timeout")
    with media_output(Path(path), "rename") as selected:
        selected.write_bytes(b"child")
        ready.send(str(selected))
    ready.close()


class AutoRenameTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "media.mp4"
        self.path.write_bytes(b"old")

    def test_rename_keeps_existing_and_selects_next_free_name(self):
        (self.root / "media (2).mp4").write_bytes(b"second")
        with media_output(self.path, "rename") as selected:
            self.assertEqual(selected.name, "media (3).mp4")
            selected.write_bytes(b"third")
        self.assertEqual(self.path.read_bytes(), b"old")

    def test_directory_and_companion_collisions_are_preserved(self):
        requested = self.root / "novel.html"
        requested.mkdir()
        (self.root / "novel (2).txt").write_text("orphan", encoding="utf-8")
        with media_output(requested, "rename", companions=lambda p: (p.with_suffix(".txt"),)) as selected:
            self.assertEqual(selected.name, "novel (3).html")
        self.assertTrue(requested.is_dir())

    def test_failure_creates_no_empty_reserved_output(self):
        with self.assertRaises(RuntimeError):
            with media_output(self.path, "rename"):
                raise RuntimeError("failed before commit")
        self.assertFalse((self.root / "media (2).mp4").exists())
        with media_output(self.path, "rename") as selected:
            self.assertEqual(selected.name, "media (2).mp4")

    def test_waiting_for_name_can_be_cancelled(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            with media_output(self.path, "rename", cancel):
                self.fail("cancelled allocation entered")

    def test_threads_never_share_an_allocated_name(self):
        def write(index):
            with media_output(self.path, "rename") as selected:
                selected.write_text(str(index), encoding="utf-8")
                return selected
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(write, range(8)))
        self.assertEqual(len(set(results)), 8)
        self.assertEqual(self.path.read_bytes(), b"old")

    def test_processes_never_share_an_allocated_name(self):
        context = multiprocessing.get_context("spawn")
        start = context.Event()
        children, connections = [], []
        try:
            for _index in range(3):
                receiving, sending = context.Pipe()
                child = context.Process(target=_rename_process, args=(str(self.path), sending, start))
                child.start()
                sending.close()
                children.append(child)
                connections.append(receiving)
            for connection in connections:
                self.assertTrue(connection.poll(10))
                self.assertEqual(connection.recv(), "ready")
            start.set()
            paths = []
            for connection in connections:
                self.assertTrue(connection.poll(10))
                paths.append(connection.recv())
            self.assertEqual(len(set(paths)), 3)
            for child in children:
                child.join(10)
                self.assertEqual(child.exitcode, 0)
        finally:
            for child in children:
                if child.is_alive():
                    child.terminate()
                child.join(5)
            for connection in connections:
                connection.close()
        self.assertEqual(self.path.read_bytes(), b"old")

    def transfer(self):
        return download_media_file(Mock(), "https://example.com/media", self.root / ".source.mp4", self.path,
                                   threading.Event(), ".mp4", conflict_policy="rename")

    def test_http_result_reports_actual_saved_path(self):
        def download(_session, _url, path, *_args, **_kwargs):
            path.write_bytes(b"new")
        with patch("software_app.crawlers.common.download_file", side_effect=download):
            result = self.transfer()
        self.assertEqual(tuple(result), (3, False))
        self.assertEqual(result.path.name, "media (2).mp4")
        self.assertEqual(result.path.read_bytes(), b"new")
        self.assertEqual(self.path.read_bytes(), b"old")

    def test_failed_http_preserves_old_and_leaves_no_false_completed_name(self):
        with patch("software_app.crawlers.common.download_file", side_effect=OSError("failed")):
            with self.assertRaises(OSError):
                self.transfer()
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertFalse((self.root / "media (2).mp4").exists())

    def test_twitter_video_preserves_original_filename_and_reports_new_path(self):
        original = self.root / "source.mp4"
        original.write_bytes(b"original file")
        result = []
        def download(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"new video")
        with patch.object(twitter, "download_http_file", side_effect=download):
            twitter.download_video("https://example.com/source.mp4", self.root, filename_base="media",
                                   network_config=twitter.configure_downloads(file_conflict="rename"),
                                   on_result=lambda *args: result.append(args))
        self.assertEqual(self.path.read_bytes(), b"old")
        self.assertEqual(original.read_bytes(), b"original file")
        self.assertEqual(result[-1][2].name, "media (2).mp4")
        self.assertEqual(result[-1][2].read_bytes(), b"new video")

    def test_twitter_gif_checks_gif_companion_before_allocating(self):
        gif = self.root / "animation.gif"
        gif.write_bytes(b"old gif")
        result = []
        def download(_session, _url, destination, *_args, **_kwargs):
            destination.write_bytes(b"new video")
        def convert(_source, destination, **_kwargs):
            destination.write_bytes(b"new gif")
            return True, ""
        with patch.object(twitter, "download_http_file", side_effect=download), patch.object(
                twitter, "convert_mp4_to_gif", side_effect=convert):
            twitter.download_video("https://example.com/source.mp4", self.root, filename_base="animation",
                                   media_type="gif", convert_gif=True,
                                   network_config=twitter.configure_downloads(file_conflict="rename"),
                                   on_result=lambda *args: result.append(args))
        self.assertEqual(gif.read_bytes(), b"old gif")
        self.assertEqual(result[-1][2].name, "animation (2).gif")

    def test_twitter_image_record_uses_actual_new_path(self):
        existing = self.root / "image.png"
        existing.write_bytes(b"old")
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, "PNG")
        response = Mock()
        response.iter_content.return_value = [buffer.getvalue()]
        record = twitter.DownloadRecord(self.root / "records.json")
        url = "https://example.com/image.jpg"
        record.mark_success(url, "image", existing)
        with patch.object(twitter, "request_with_retries", return_value=response):
            twitter.download_pic(url, self.root, filename_base="image", record=record,
                                 network_config=twitter.configure_downloads(file_conflict="rename"))
        saved = Path(record.snapshot()[url]["file"])
        self.assertEqual(saved.name, "image (2).png")
        self.assertEqual(existing.read_bytes(), b"old")

    def test_browser_file_record_uses_new_path(self):
        from software_app.crawlers.webpage.client import WebPageCrawler, MediaCandidate
        old = self.root / "001-asset.png"
        old.write_bytes(b"old")
        candidate = MediaCandidate("https://example.com/asset.png", "image")
        payload = SimpleNamespace(data=b"new", content_type="image/png", final_url=candidate.url)
        task = DownloadTask("test", "website", "https://example.com", self.root, {"file_conflict": "rename"})
        def convert(source, destination, _suffix, **_kwargs):
            source.replace(destination)
        with patch("software_app.crawlers.webpage.client.convert_media_file", side_effect=convert):
            record = WebPageCrawler._download_browser_payload(payload, candidate, self.root, 1, 100, task, task.target)
        self.assertEqual(record.path.name, "001-asset (2).png")
        self.assertEqual(record.path.read_bytes(), b"new")
        self.assertEqual(old.read_bytes(), b"old")

    def test_hls_returns_renamed_output_path(self):
        from software_app.crawlers.bluesky import BlueskyClient
        client = BlueskyClient()
        def download(_url, destination, _cancel):
            destination.write_bytes(b"new video")
        with patch.object(client, "_download_hls_locked", side_effect=download):
            path = client._download_hls("https://example.com/video", self.path, threading.Event(), "rename")
        self.assertEqual(path.name, "media (2).mp4")
        self.assertEqual(self.path.read_bytes(), b"old")

    def test_fanbox_and_sketch_use_conflict_policy_and_index_new_paths(self):
        from software_app.crawlers.pixiv.external import FanboxClient, SketchClient
        for client_type in (FanboxClient, SketchClient):
            client = object.__new__(client_type)
            client.session = Mock()
            client.posts = lambda *_args: [{"id": "1", "title": "post", "text": "post", "creatorId": "author",
                                           "user": {"unique_name": "author"}}]
            client._get = lambda *_args: {"body": {"id": "1", "title": "post", "creatorId": "author"}}
            task = DownloadTask("test", "pixiv", "1", self.root, {"file_conflict": "rename"})
            records = []
            def download(_session, _url, path, *_args, **_kwargs):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"new")
            with patch("software_app.crawlers.pixiv.external._walk_media_urls", return_value=["https://example.com/video.mp4"]), patch(
                    "software_app.crawlers.common.download_file", side_effect=download):
                client.download("1", self.root, task, CallbackSet(on_file=records.append), threading.Event())
                client.download("1", self.root, task, CallbackSet(on_file=records.append), threading.Event())
            videos = [record for record in records if record.media_type == "video"]
            self.assertEqual(len(videos), 2)
            self.assertNotEqual(videos[0].path, videos[1].path)
            self.assertTrue(videos[-1].path.name.endswith(" (2).mp4"))

    def test_novel_renames_html_text_and_metadata_as_one_group(self):
        from software_app.crawlers.pixiv.client import PixivCrawler
        crawler = object.__new__(PixivCrawler)
        crawler._body = lambda _path: {"title": "novel", "content": "body", "userId": "8", "userName": "author"}
        task = DownloadTask("test", "pixiv", "https://www.pixiv.net/novel/show.php?id=123", self.root,
                            {"input_kind": "novel", "file_conflict": "rename"})
        records = []
        crawler.download(task, CallbackSet(on_file=records.append), threading.Event())
        old = {record.path: record.path.read_bytes() for record in records}
        crawler.download(task, CallbackSet(on_file=records.append), threading.Event())
        self.assertEqual(len({record.path for record in records}), 6)
        new = records[3:]
        self.assertEqual(len({record.path.stem for record in new}), 1)
        self.assertTrue(all(" (2)" in record.path.stem for record in new))
        for path, data in old.items():
            self.assertEqual(path.read_bytes(), data)

    def test_original_ugoira_archive_and_metadata_keep_matching_names(self):
        from software_app.crawlers.pixiv import ugoira
        original = self.root / "123.ugoira.zip"
        original.write_bytes(b"old zip")
        meta = self.root / "123.ugoira.json"
        meta.write_bytes(b"old meta")
        body = {"src": "https://example.com/source.zip", "frames": [{"file": "1.jpg", "delay": 10}]}
        def download(_session, _url, path, *_args, **_kwargs):
            path.write_bytes(b"new zip")
        with patch.object(ugoira, "download_file", side_effect=download):
            results = ugoira.download_ugoira(Mock(), body, self.root, "123",
                                            {"animation_format": "original", "file_conflict": "rename"}, threading.Event())
        self.assertNotEqual(results[0][0], original)
        self.assertEqual(results[0][0].with_suffix(".json"), results[1][0])
        self.assertEqual(original.read_bytes(), b"old zip")
        self.assertEqual(meta.read_bytes(), b"old meta")

    def test_renamed_ugoira_archive_is_not_automatically_unpacked(self):
        from software_app.core.archive_service import run_archive_postprocess
        archive = self.root / "123.ugoira (2).zip"
        archive.write_bytes(b"animation source")
        record = FileRecord(archive, "pixiv", media_type="animation")
        task = DownloadTask("test", "pixiv", "123", self.root, {"extract_archives": True})
        with patch("software_app.core.archive_service.extract_zip_safely") as extract:
            result = run_archive_postprocess(task, "123", [record], cancel_event=threading.Event())
        extract.assert_not_called()
        self.assertFalse(result.records)

    def test_jm_cover_renames_and_reports_actual_path(self):
        from software_app.crawlers.jmcomic.client import JmComicCrawler
        crawler = object.__new__(JmComicCrawler)
        buffer = BytesIO()
        Image.new("RGB", (2, 2), "red").save(buffer, "PNG")
        response = Mock()
        response.iter_content.return_value = [buffer.getvalue()]
        crawler._get_response = Mock(return_value=response)
        task = DownloadTask("test", "jmcomic", "123", self.root, {"file_conflict": "rename"})
        records = []
        old = self.root / "JM123_cover.png"
        old.write_bytes(b"old cover")
        crawler._download_cover("https://example.com/cover.png", "123", self.root,
                                CallbackSet(on_file=records.append), task, threading.Event())
        self.assertEqual(records[0].path.name, "JM123_cover (2).png")
        self.assertEqual(old.read_bytes(), b"old cover")

    def test_audio_rename_preserves_existing_source_and_emits_new_path(self):
        old = self.root / "audio.mp3"
        old.write_bytes(b"old audio")
        old_source = self.root / "audio (2).source.mp4"
        old_source.write_bytes(b"old source")
        response = Mock()
        response.iter_content.return_value = [b"video"]
        results = []
        def extract(_source, destination, **_kwargs):
            destination.write_bytes(b"new audio")
            return True, ""
        with patch.object(twitter, "request_with_retries", return_value=response), patch.object(
                twitter, "extract_audio_file", side_effect=extract):
            twitter.download_audio("https://example.com/video.mp4", self.root, filename_base="audio",
                                   network_config=twitter.configure_downloads(file_conflict="rename"),
                                   on_result=lambda *args: results.append(args))
        self.assertEqual(results[-1][2].name, "audio (3).mp3")
        self.assertEqual(old.read_bytes(), b"old audio")
        self.assertEqual(old_source.read_bytes(), b"old source")

    @unittest.skipUnless(os.name == "nt", "Windows replacement retry")
    def test_text_commit_retries_transient_windows_file_occupation(self):
        destination = self.root / "summary.txt"
        destination.write_text("old", encoding="utf-8")
        busy = PermissionError("busy")
        busy.winerror = 5
        replace = os.replace
        attempts = []
        def commit(source, target):
            attempts.append(source)
            if len(attempts) == 1:
                raise busy
            replace(source, target)
        with patch("software_app.crawlers.common.os.replace", side_effect=commit), patch("software_app.crawlers.common.time.sleep"):
            write_text_atomic(destination, "new")
        self.assertEqual(destination.read_text(encoding="utf-8"), "new")
        self.assertEqual(len(attempts), 2)

    @unittest.skipUnless(os.name == "nt", "Windows replacement retry")
    def test_text_commit_exhaustion_keeps_previous_summary(self):
        destination = self.root / "summary.txt"
        destination.write_text("old", encoding="utf-8")
        busy = PermissionError("busy")
        busy.winerror = 32
        with patch("software_app.crawlers.common.os.replace", side_effect=busy) as commit, patch("software_app.crawlers.common.time.sleep"):
            with self.assertRaises(PermissionError):
                write_text_atomic(destination, "new")
        self.assertEqual(commit.call_count, 6)
        self.assertEqual(destination.read_text(encoding="utf-8"), "old")
        self.assertFalse(list(self.root.glob(".summary.txt.*.tmp")))


if __name__ == "__main__":
    unittest.main()
