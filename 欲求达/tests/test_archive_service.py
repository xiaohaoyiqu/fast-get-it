from __future__ import annotations

import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.archive_service import create_verified_zip, extract_zip_safely
from software_app.core.events import CallbackSet
from software_app.core.models import FileRecord, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class _ArchiveAdapter(CrawlerAdapter):
    def __init__(self) -> None:
        self.info = ModuleInfo("archive-test", "Archive Test")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("archive-test", raw_target, raw_target, title="作者：测试 / 作品")

    def download(self, task, callbacks, cancel_event):
        path = task.output_dir / "作者_日本語" / "第一章" / "图片 01.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"image")
        callbacks.on_file(
            FileRecord(
                path=path.resolve(), module_id=task.module_id, task_id=task.task_id,
                media_type="image", size=path.stat().st_size, title=path.name, chapter="第一章",
            )
        )


class _SilentArchiveAdapter(CrawlerAdapter):
    def __init__(self) -> None:
        self.info = ModuleInfo("silent-archive-test", "Silent Archive Test")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("silent-archive-test", raw_target, raw_target, title="静默下载")

    def download(self, task, callbacks, cancel_event):
        path = task.output_dir / "旧式适配器" / "资源.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content", encoding="utf-8")


class ArchiveServiceTests(unittest.TestCase):
    def test_create_verified_zip_keeps_unicode_paths_and_sources_by_default(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            first = root / "作者_日本語" / "第一章" / "图一.png"
            second = root / "作者_日本語" / "第二章" / "图二.txt"
            first.parent.mkdir(parents=True)
            second.parent.mkdir(parents=True)
            first.write_bytes(b"one")
            second.write_text("二", encoding="utf-8")

            archive = create_verified_zip(
                [first, second], root / "作品.zip", base_dir=root, cancel_event=threading.Event()
            )

            self.assertTrue(first.is_file())
            self.assertTrue(second.is_file())
            with zipfile.ZipFile(archive) as handle:
                self.assertIsNone(handle.testzip())
                self.assertEqual(
                    set(handle.namelist()),
                    {"作者_日本語/第一章/图一.png", "作者_日本語/第二章/图二.txt"},
                )

    def test_cleanup_sources_only_after_verified_archive(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "资源.bin"
            source.write_bytes(b"payload")

            archive = create_verified_zip(
                [source], root / "资源.zip", base_dir=root,
                cleanup_sources=True, cancel_event=threading.Event(),
            )

            self.assertTrue(archive.is_file())
            self.assertFalse(source.exists())
            with zipfile.ZipFile(archive) as handle:
                self.assertEqual(handle.read("资源.bin"), b"payload")

    def test_cancelled_archive_keeps_sources_and_removes_partial_file(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "资源.bin"
            source.write_bytes(b"payload")
            cancelled = threading.Event()
            cancelled.set()

            with self.assertRaises(TaskCancelled):
                create_verified_zip(
                    [source], root / "资源.zip", base_dir=root,
                    cleanup_sources=True, cancel_event=cancelled,
                )

            self.assertTrue(source.is_file())
            self.assertFalse((root / "资源.zip").exists())
            self.assertFalse(any(root.glob("*.partial-*")))

    def test_safe_extract_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.txt", "bad")

            with self.assertRaises(ValueError):
                extract_zip_safely(archive, root / "extract", cancel_event=threading.Event())

            self.assertFalse((root / "outside.txt").exists())
            self.assertFalse((root / "extract").exists())

    def test_safe_extract_commits_unicode_tree_after_complete_read(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            archive = root / "good.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("作者_日本語/第一章/图片.txt", "正文")

            extracted = extract_zip_safely(
                archive, root / "解压结果", cancel_event=threading.Event()
            )

            self.assertEqual(len(extracted), 1)
            self.assertEqual(extracted[0].read_text(encoding="utf-8"), "正文")
            self.assertFalse(any(root.glob("*.partial-*")))

    def test_task_manager_applies_common_archive_to_any_adapter(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            storage = AppStorage(root / "tasks.db")
            manager = TaskManager(storage)
            manager.register_adapter(_ArchiveAdapter())
            completed = []

            task = manager.start_task(
                "archive-test", "target", root / "downloads",
                {"archive_mode": "task", "archive_cleanup_sources": False},
                CallbackSet(on_done=lambda task_id, status: completed.append((task_id, status))),
            )
            manager.wait(task.task_id, timeout=5)

            rows = storage.list_files(task_id=task.task_id, limit=20)
            archive_rows = [row for row in rows if row["media_type"] == "archive"]
            self.assertEqual(completed, [(task.task_id, "completed")])
            self.assertEqual(len(archive_rows), 1)
            self.assertTrue(Path(archive_rows[0]["path"]).is_file())
            self.assertTrue((root / "downloads" / "作者_日本語" / "第一章" / "图片 01.png").is_file())

    def test_task_manager_cleanup_removes_stale_source_index_after_zip_verification(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            storage = AppStorage(root / "tasks.db")
            manager = TaskManager(storage)
            manager.register_adapter(_ArchiveAdapter())

            task = manager.start_task(
                "archive-test", "target", root / "downloads",
                {"archive_mode": "task", "archive_cleanup_sources": True},
            )
            manager.wait(task.task_id, timeout=5)

            rows = storage.list_files(task_id=task.task_id, limit=20)
            self.assertEqual([row["media_type"] for row in rows], ["archive"])
            self.assertFalse((root / "downloads" / "作者_日本語" / "第一章" / "图片 01.png").exists())

    def test_common_archive_also_handles_adapter_that_relies_on_fallback_scan(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            storage = AppStorage(root / "tasks.db")
            manager = TaskManager(storage)
            manager.register_adapter(_SilentArchiveAdapter())

            task = manager.start_task(
                "silent-archive-test", "target", root / "downloads", {"archive_mode": "task"}
            )
            manager.wait(task.task_id, timeout=5)

            rows = storage.list_files(task_id=task.task_id, limit=20)
            self.assertEqual(storage.get_task(task.task_id)["status"], "completed")
            self.assertEqual(len([row for row in rows if row["media_type"] == "archive"]), 1)


if __name__ == "__main__":
    unittest.main()
