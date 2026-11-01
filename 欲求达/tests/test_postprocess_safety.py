from __future__ import annotations

import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from software_app.core import archive_service as archives
from software_app.core import image_postprocess as images
from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.models import DownloadTask, FileRecord, ModuleInfo, TargetPreview
from software_app.core.output_lock import output_path_lock
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class ImageArtifactSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source.png"
        Image.new("RGB", (4, 3), "red").save(self.source)
        self.pdf = self.root / "chapter.pdf"
        self.long = self.root / "chapter_long.jpg"

    def build(self, **kwargs):
        return images.build_image_artifacts([self.source], self.root, "chapter", "both", **kwargs)

    def old_outputs(self):
        self.pdf.write_bytes(b"old pdf")
        self.long.write_bytes(b"old long")

    def assert_old_outputs(self):
        self.assertEqual(self.pdf.read_bytes(), b"old pdf")
        self.assertEqual(self.long.read_bytes(), b"old long")
        self.assertFalse(list(self.root.glob(".*.convert.*")))

    def test_rename_keeps_pdf_and_long_image_together(self):
        self.old_outputs()
        paths = [path for path, _kind in self.build(file_conflict="rename")]
        self.assertEqual([path.name for path in paths], ["chapter (2).pdf", "chapter (2)_long.jpg"])
        self.assertTrue(paths[0].read_bytes().startswith(b"%PDF"))
        with Image.open(paths[1]) as opened:
            self.assertEqual(opened.size, (4, 3))
        self.assert_old_outputs()

    def test_reuse_does_not_render_either_existing_artifact(self):
        self.old_outputs()
        with patch.object(images, "_save_pdf") as pdf, patch.object(images, "_save_long_image") as long:
            result = self.build()
        pdf.assert_not_called()
        long.assert_not_called()
        self.assertEqual([path for path, _kind in result], [self.pdf, self.long])

    def test_error_checks_every_output_before_creating_any(self):
        self.long.write_bytes(b"old long")
        with patch.object(images, "_save_pdf") as pdf:
            with self.assertRaises(FileExistsError):
                self.build(file_conflict="error")
        pdf.assert_not_called()
        self.assertFalse(self.pdf.exists())

    def test_second_renderer_failure_preserves_both_old_outputs(self):
        self.old_outputs()
        with patch.object(images, "_save_long_image", side_effect=OSError("render failed")):
            with self.assertRaises(OSError):
                self.build(file_conflict="overwrite")
        self.assert_old_outputs()

    def test_cancel_after_render_preserves_old_outputs(self):
        self.old_outputs()
        cancel = threading.Event()
        save = images._save_long_image
        def render(*args):
            save(*args)
            cancel.set()
        with patch.object(images, "_save_long_image", side_effect=render):
            with self.assertRaises(TaskCancelled):
                self.build(file_conflict="overwrite", cancel_event=cancel)
        self.assert_old_outputs()

    def test_failed_commit_preserves_old_outputs_and_cleans_temporaries(self):
        self.old_outputs()
        with patch.object(images, "replace_output", side_effect=PermissionError("busy")):
            with self.assertRaises(PermissionError):
                self.build(file_conflict="overwrite")
        self.assert_old_outputs()

    def test_two_tasks_get_different_artifact_groups(self):
        self.old_outputs()
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _index: self.build(file_conflict="rename"), range(2)))
        self.assertEqual(len({path for group in results for path, _kind in group}), 4)
        self.assert_old_outputs()

    def test_output_cannot_replace_a_source_image(self):
        source = self.root / "chapter_long.jpg"
        Image.new("RGB", (4, 3), "blue").save(source)
        original = source.read_bytes()
        with self.assertRaises(ValueError):
            images.build_image_artifacts([source], self.root, "chapter", "long", file_conflict="overwrite")
        self.assertEqual(source.read_bytes(), original)

    def test_already_cancelled_job_creates_no_outputs(self):
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            self.build(cancel_event=cancel)
        self.assertFalse(self.pdf.exists())
        self.assertFalse(self.long.exists())


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.first = self.root / "one.txt"
        self.second = self.root / "two.txt"
        self.first.write_bytes(b"first")
        self.second.write_bytes(b"second")
        self.final = self.root / "archive.zip"

    def create(self, **kwargs):
        return archives.create_verified_zip([self.first, self.second], self.final, base_dir=self.root, **kwargs)

    def test_windows_signature_uses_birthtime_across_stat_apis(self):
        fields = dict(st_dev=1, st_ino=2, st_size=5, st_mtime_ns=100, st_birthtime_ns=50)
        path_stat = SimpleNamespace(**fields, st_ctime_ns=50)
        handle_stat = SimpleNamespace(**fields, st_ctime_ns=200)
        with patch.object(archives.os, "name", "nt"):
            self.assertEqual(archives._signature(path_stat), archives._signature(handle_stat))
            self.assertNotEqual(archives._stream_signature(path_stat), archives._stream_signature(handle_stat))

    def test_old_windows_signature_retains_ctime_fallback(self):
        value = SimpleNamespace(st_dev=1, st_ino=2, st_size=5, st_mtime_ns=100, st_ctime_ns=50)
        with patch.object(archives.os, "name", "nt"):
            self.assertEqual(archives._signature(value), (1, 2, 5, 100, 50))

    def test_posix_signature_retains_metadata_change_time(self):
        value = SimpleNamespace(st_dev=1, st_ino=2, st_size=5, st_mtime_ns=100,
                                st_ctime_ns=200, st_birthtime_ns=50)
        with patch.object(archives.os, "name", "posix"):
            self.assertEqual(archives._signature(value), (1, 2, 5, 100, 200))

    def test_change_time_during_compression_rejects_commit(self):
        original = archives.os.fstat
        calls = []

        def changed(fd):
            value = original(fd)
            if (value.st_dev, value.st_ino) != (self.first.stat().st_dev, self.first.stat().st_ino):
                return value
            calls.append(fd)
            fields = {name: getattr(value, name) for name in (
                "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns", "st_birthtime_ns"
            ) if hasattr(value, name)}
            if len(calls) > 1:
                fields["st_ctime_ns"] += 1000000
            return SimpleNamespace(**fields)

        with patch.object(archives.os, "fstat", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "压缩期间变化"):
                self.create(cleanup_sources=True)
        self.assertFalse(self.final.exists())
        self.assertTrue(self.first.exists() and self.second.exists())
        self.assertFalse(list(self.root.glob("*.partial-*")))

    def test_change_time_during_cleanup_hash_preserves_all_sources(self):
        self.create()
        original = archives._digest_stream

        def change(stream, cancel):
            digest = original(stream, cancel)
            if Path(getattr(stream, "name", "")) == self.first:
                self.first.write_bytes(b"other")
                os.utime(self.first, ns=(self.first.stat().st_atime_ns, before.st_mtime_ns))
            return digest

        before = self.first.stat()
        removed, warnings = [], []
        with patch.object(archives, "_digest_stream", side_effect=change):
            self.create(cleanup_sources=True, removed_sources=removed, warnings=warnings)
        self.assertEqual(removed, [])
        self.assertTrue(warnings)
        self.assertEqual(self.first.read_bytes(), b"other")
        self.assertTrue(self.second.exists())

    def test_rename_preserves_previous_archive(self):
        self.create()
        original = self.final.read_bytes()
        self.first.write_bytes(b"new first")
        result = self.create(file_conflict="rename")
        self.assertEqual(result.name, "archive (2).zip")
        self.assertEqual(self.final.read_bytes(), original)
        with zipfile.ZipFile(result) as handle:
            self.assertEqual(handle.read("one.txt"), b"new first")

    def test_reuse_matching_archive_can_clean_verified_sources(self):
        self.create()
        removed, warnings = [], []
        self.create(cleanup_sources=True, removed_sources=removed, warnings=warnings)
        self.assertEqual(removed, [self.first, self.second])
        self.assertFalse(warnings)
        self.assertFalse(self.first.exists())

    def test_reuse_stale_archive_preserves_entire_source_group(self):
        self.create()
        self.second.write_bytes(b"new second")
        removed, warnings = [], []
        self.create(cleanup_sources=True, removed_sources=removed, warnings=warnings)
        self.assertFalse(removed)
        self.assertTrue(self.first.exists())
        self.assertEqual(self.second.read_bytes(), b"new second")
        self.assertTrue(warnings)

    def test_reuse_different_member_list_never_cleans_sources(self):
        with zipfile.ZipFile(self.final, "w") as handle:
            handle.writestr("unrelated.txt", b"other")
        warnings = []
        self.create(cleanup_sources=True, warnings=warnings)
        self.assertTrue(self.first.exists() and self.second.exists())
        self.assertIn("文件列表", warnings[0])

    def test_corrupt_reused_archive_keeps_sources_and_existing_file(self):
        self.final.write_bytes(b"bad zip")
        with self.assertRaises(zipfile.BadZipFile):
            self.create(cleanup_sources=True)
        self.assertEqual(self.final.read_bytes(), b"bad zip")
        self.assertTrue(self.first.exists() and self.second.exists())

    def test_mutation_during_compression_preserves_old_archive(self):
        self.create()
        original = self.final.read_bytes()
        def progress(current, _total, _message):
            if current == 1:
                self.first.write_bytes(b"changed while archiving")
        with self.assertRaises(ValueError):
            self.create(file_conflict="overwrite", cleanup_sources=True, on_progress=progress)
        self.assertEqual(self.final.read_bytes(), original)
        self.assertTrue(self.first.exists() and self.second.exists())
        self.assertFalse(list(self.root.glob("*.partial-*")))

    def test_changed_after_commit_prevents_cleanup_of_any_source(self):
        commit = archives.replace_output
        def replace(*args):
            commit(*args)
            self.second.write_bytes(b"new content")
        removed, warnings = [], []
        with patch.object(archives, "replace_output", side_effect=replace):
            self.create(cleanup_sources=True, removed_sources=removed, warnings=warnings)
        self.assertFalse(removed)
        self.assertTrue(self.first.exists())
        self.assertEqual(self.second.read_bytes(), b"new content")
        with zipfile.ZipFile(self.final) as handle:
            self.assertEqual(handle.read("two.txt"), b"second")
        self.assertTrue(warnings)

    def test_same_size_timestamp_preserving_change_is_not_deleted(self):
        commit = archives.replace_output
        initial = self.second.stat()
        def replace(*args):
            commit(*args)
            self.second.write_bytes(b"ALTER!")
            os.utime(self.second, ns=(initial.st_atime_ns, initial.st_mtime_ns))
        with patch.object(archives, "replace_output", side_effect=replace):
            self.create(cleanup_sources=True)
        self.assertEqual(self.second.read_bytes(), b"ALTER!")
        self.assertTrue(self.first.exists())

    def test_delete_failure_reports_only_actually_removed_sources(self):
        unlink = Path.unlink
        def delete(path, *args, **kwargs):
            if path == self.second:
                raise PermissionError("busy source")
            return unlink(path, *args, **kwargs)
        removed, warnings = [], []
        with patch.object(Path, "unlink", delete):
            self.create(cleanup_sources=True, removed_sources=removed, warnings=warnings)
        self.assertEqual(removed, [self.first])
        self.assertTrue(self.second.exists())
        self.assertTrue(self.final.exists())
        self.assertTrue(warnings)

    def test_change_during_deletion_keeps_later_replacement(self):
        unlink = Path.unlink
        def delete(path, *args, **kwargs):
            if path == self.first:
                self.second.write_bytes(b"replacement")
            return unlink(path, *args, **kwargs)
        removed = []
        with patch.object(Path, "unlink", delete):
            self.create(cleanup_sources=True, removed_sources=removed)
        self.assertEqual(removed, [self.first])
        self.assertEqual(self.second.read_bytes(), b"replacement")

    def test_error_policy_preserves_archive_and_all_sources(self):
        self.create()
        original = self.final.read_bytes()
        with self.assertRaises(FileExistsError):
            self.create(file_conflict="error", cleanup_sources=True)
        self.assertEqual(self.final.read_bytes(), original)
        self.assertTrue(self.first.exists() and self.second.exists())

    def test_cooperating_writer_waits_until_archive_finishes(self):
        holding, release, acquired = threading.Event(), threading.Event(), threading.Event()
        def progress(_current, _total, _message):
            holding.set()
            release.wait(3)
        def writer():
            with output_path_lock(self.first):
                acquired.set()
                self.first.write_bytes(b"new version")
        with ThreadPoolExecutor(2) as pool:
            archive = pool.submit(self.create, on_progress=progress)
            self.assertTrue(holding.wait(2))
            write = pool.submit(writer)
            try:
                self.assertFalse(acquired.wait(0.1))
            finally:
                release.set()
            archive.result(5)
            write.result(5)
        with zipfile.ZipFile(self.final) as handle:
            self.assertEqual(handle.read("one.txt"), b"first")

    def test_extract_does_not_replace_directory_created_during_extraction(self):
        self.create()
        target = self.root / "extracted"
        def progress(_current, _total, _message):
            target.mkdir(exist_ok=True)
            (target / "external.txt").write_bytes(b"external")
        with self.assertRaises(FileExistsError):
            archives.extract_zip_safely(self.final, target, on_progress=progress)
        self.assertEqual((target / "external.txt").read_bytes(), b"external")
        self.assertFalse(list(self.root.glob("extracted.partial-*")))

    def test_cancel_during_compression_leaves_all_sources(self):
        cancel = threading.Event()
        with self.assertRaises(TaskCancelled):
            self.create(cleanup_sources=True, cancel_event=cancel,
                        on_progress=lambda *_args: cancel.set())
        self.assertTrue(self.first.exists() and self.second.exists())
        self.assertFalse(self.final.exists())

    def test_postprocess_returns_actual_archive_name_and_cleanup_list(self):
        task = DownloadTask("task", "test", "target", self.root,
                            {"archive_mode": "task", "archive_cleanup_sources": True, "file_conflict": "rename"})
        existing = self.root / "title_归档_task.zip"
        existing.write_bytes(b"old archive")
        records = [FileRecord(path, "test") for path in (self.first, self.second)]
        result = archives.run_archive_postprocess(task, "title", records, cancel_event=threading.Event())
        self.assertNotEqual(result.records[0].path, existing)
        self.assertEqual(result.removed_sources, [self.first, self.second])
        self.assertEqual(result.records[0].metadata["removed_source_count"], 2)
        self.assertEqual(existing.read_bytes(), b"old archive")

    def test_task_manager_keeps_index_and_warns_when_cleanup_is_skipped(self):
        root = self.root
        class Adapter(CrawlerAdapter):
            info = ModuleInfo("safety", "Safety")
            def preview_target(self, raw_target, options=None):
                return TargetPreview("safety", raw_target, raw_target)
            def download(self, task, callbacks, cancel_event):
                path = root / "download.txt"
                path.write_bytes(b"original")
                callbacks.on_file(FileRecord(path, "safety", task.task_id, "text", path.stat().st_size))
        storage = AppStorage(self.root / "app.sqlite3")
        manager = TaskManager(storage)
        manager.register_adapter(Adapter())
        commit = archives.replace_output
        def replace(*args):
            commit(*args)
            (root / "download.txt").write_bytes(b"replacement")
        try:
            with patch.object(archives, "replace_output", side_effect=replace):
                task = manager.start_task("safety", "target", self.root,
                                          {"archive_mode": "task", "archive_cleanup_sources": True})
                manager.wait(task.task_id, 5)
            self.assertEqual(storage.get_task(task.task_id)["status"], "completed")
            rows = storage.list_files(task_id=task.task_id)
            self.assertEqual({row["media_type"] for row in rows}, {"text", "archive"})
            self.assertTrue(any(row["level"].lower() == "warning" for row in storage.list_logs(task_id=task.task_id)))
        finally:
            manager.shutdown()


if __name__ == "__main__":
    unittest.main()
