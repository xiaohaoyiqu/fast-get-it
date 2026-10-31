from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from software_app.core.adapter import TaskCancelled
from software_app.core.output_lock import output_path_lock
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.core.transfer_cleanup import preview_transfer_caches, execute_transfer_cleanup
from software_app.core.transfer_workspace import owned_workspace


class TransferCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = AppStorage(self.root / "test.db")
        self.manager = TaskManager(self.storage)
        self.output = self.root / "downloads"
        self.output.mkdir()

    def cache(self, name="video.mp4", kind="http"):
        destination = self.output / name
        with owned_workspace(destination, kind=kind) as folder:
            if kind == "http":
                (folder / "payload.part").write_bytes(b"partial media")
                (folder / "resume.json").write_text("{}", encoding="utf-8")
            else:
                (folder / "r000000.ts").write_bytes(b"cached media")
                (folder / "r000000.ts.json").write_text("{}", encoding="utf-8")
                (folder / "state.json").write_text("{}", encoding="utf-8")
        return destination, folder

    def clean(self, plan, selected=None, **kwargs):
        return execute_transfer_cleanup(
            self.manager, plan, selected if selected is not None else [e.path for e in plan.entries], **kwargs
        )

    def test_preview_is_read_only_and_selected_cleanup_preserves_other_cache_and_media(self):
        destination, http = self.cache()
        _unused, hls = self.cache("other.mp4", "hls")
        destination.write_bytes(b"complete output")
        attachment = self.output / "video.mp4.part"
        attachment.write_bytes(b"legitimate attachment")
        before = {path: path.read_bytes() for path in http.iterdir()}
        plan = preview_transfer_caches(self.output)
        self.assertEqual(len(plan.entries), 2)
        self.assertTrue(all(not entry.error for entry in plan.entries))
        self.assertEqual(before, {path: path.read_bytes() for path in http.iterdir()})
        result = self.clean(plan, [http])
        self.assertEqual((result.removed_folders, result.removed_files, result.skipped), (1, 3, 0))
        self.assertEqual(result.removed_bytes, sum(len(value) for value in before.values()))
        self.assertFalse(http.exists())
        self.assertTrue(hls.exists())
        self.assertEqual(destination.read_bytes(), b"complete output")
        self.assertEqual(attachment.read_bytes(), b"legitimate attachment")

    def test_hls_known_resources_and_unique_temporary_files_can_be_cleared(self):
        _destination, folder = self.cache(kind="hls")
        (folder / (".hls-" + "a" * 32 + ".tmp")).write_bytes(b"temporary")
        (folder / "r000001.m4s.part").write_bytes(b"unfinished")
        result = self.clean(preview_transfer_caches(self.output))
        self.assertEqual(result.removed_folders, 1)
        self.assertFalse(folder.exists())

    def test_unknown_contents_block_entire_folder(self):
        for name in ("notes.txt", "nested"):
            with self.subTest(name=name):
                _destination, folder = self.cache(name + ".mp4")
                if name == "nested":
                    (folder / name).mkdir()
                else:
                    (folder / name).write_bytes(b"user file")
                plan = preview_transfer_caches(self.output)
                entry = next(entry for entry in plan.entries if entry.path == folder)
                self.assertTrue(entry.error)
                result = self.clean(plan, [folder])
                self.assertEqual(result.removed_files, 0)
                self.assertTrue((folder / "owner.json").exists())
                self.assertTrue((folder / "payload.part").exists())

    def test_unknown_or_malformed_owner_is_never_adopted(self):
        _destination, folder = self.cache()
        for owner in (b"{}", b"not json", b"\xff", b"x" * 4097):
            with self.subTest(owner=owner[:12]):
                (folder / "owner.json").write_bytes(owner)
                plan = preview_transfer_caches(self.output)
                self.assertTrue(plan.entries[0].error)
                self.assertEqual(self.clean(plan).removed_files, 0)

    def test_changed_file_after_preview_skips_every_file(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        (folder / "payload.part").write_bytes(b"changed after preview")
        result = self.clean(plan)
        self.assertEqual((result.removed_files, result.skipped), (0, 1))
        self.assertTrue((folder / "resume.json").exists())

    def test_new_file_after_preview_skips_every_file(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        (folder / "note.txt").write_bytes(b"new user file")
        result = self.clean(plan)
        self.assertEqual((result.removed_files, result.skipped), (0, 1))
        self.assertTrue((folder / "payload.part").exists())

    def test_replaced_directory_is_not_deleted(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        moved = self.output / "moved-cache"
        folder.rename(moved)
        folder.mkdir()
        for path in moved.iterdir():
            (folder / path.name).write_bytes(path.read_bytes())
        result = self.clean(plan)
        self.assertEqual((result.removed_files, result.skipped), (0, 1))
        self.assertTrue((folder / "owner.json").exists())

    def test_active_destination_is_blocked_by_existing_output_lock(self):
        destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        held, release = threading.Event(), threading.Event()

        def hold():
            with output_path_lock(destination):
                held.set()
                release.wait(5)

        thread = threading.Thread(target=hold)
        thread.start()
        try:
            self.assertTrue(held.wait(3))
            self.assertTrue(preview_transfer_caches(self.output).entries[0].error)
            result = self.clean(plan)
            self.assertEqual((result.removed_files, result.skipped), (0, 1))
            self.assertTrue((folder / "payload.part").exists())
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(self.clean(preview_transfer_caches(self.output)).removed_folders, 1)

    def test_local_active_tasks_block_cleanup(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        with patch.object(self.manager, "running", {"generated": object()}), self.assertRaises(ValueError):
            self.clean(plan)
        self.assertTrue(folder.exists())

    def test_shared_database_queued_task_blocks_cleanup(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        self.storage.create_task("other", "ehentai", "generated", str(self.output), {})
        result = self.clean(plan)
        self.assertEqual(result.removed_files, 0)
        self.assertTrue(result.errors)
        self.assertTrue((folder / "payload.part").exists())

    def test_cancel_before_execution_keeps_all_files(self):
        _destination, folder = self.cache()
        stop = threading.Event()
        stop.set()
        with self.assertRaises(TaskCancelled):
            preview_transfer_caches(self.output, cancel_event=stop)
        plan = preview_transfer_caches(self.output)
        result = self.clean(plan, cancel_event=stop)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.removed_files, 0)
        self.assertTrue(folder.exists())

    def test_partial_cancellation_keeps_owner_and_remaining_resources(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        stop = threading.Event()
        unlink = Path.unlink

        def remove(path, *args, **kwargs):
            result = unlink(path, *args, **kwargs)
            if path.name == "payload.part":
                stop.set()
            return result

        with patch.object(Path, "unlink", remove):
            result = self.clean(plan, cancel_event=stop)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.removed_files, 1)
        self.assertTrue((folder / "owner.json").exists())
        self.assertTrue((folder / "resume.json").exists())
        self.assertEqual(self.clean(preview_transfer_caches(self.output)).removed_folders, 1)

    def test_permission_error_retains_owner_and_reports_failure(self):
        _destination, folder = self.cache()
        plan = preview_transfer_caches(self.output)
        unlink = Path.unlink

        def remove(path, *args, **kwargs):
            if path.name == "payload.part":
                raise PermissionError("generated sharing violation")
            return unlink(path, *args, **kwargs)

        with patch.object(Path, "unlink", remove):
            result = self.clean(plan)
        self.assertEqual((result.removed_files, result.skipped), (0, 1))
        self.assertTrue((folder / "owner.json").exists())

    def test_selection_cannot_expand_beyond_preview(self):
        self.cache()
        with self.assertRaises(ValueError):
            self.clean(preview_transfer_caches(self.output), [self.output / "unreviewed"])

    def test_linked_root_and_cache_files_are_rejected(self):
        _destination, folder = self.cache()
        outside = self.root / "external.txt"
        outside.write_bytes(b"outside")
        link = folder / "resume.json"
        link.unlink()
        try:
            link.symlink_to(outside)
        except OSError as exc:
            self.skipTest(f"symlink creation unavailable: {exc}")
        plan = preview_transfer_caches(self.output)
        self.assertTrue(plan.entries[0].error)
        self.assertEqual(self.clean(plan).removed_files, 0)
        self.assertEqual(outside.read_bytes(), b"outside")
        root_link = self.root / "linked-root"
        root_link.symlink_to(self.output, target_is_directory=True)
        with self.assertRaises(ValueError):
            preview_transfer_caches(root_link)

    def test_scan_limits_require_smaller_scope(self):
        self.cache()
        with patch("software_app.core.transfer_cleanup.MAX_DIRECTORIES", 0), self.assertRaises(ValueError):
            preview_transfer_caches(self.output)
        with patch("software_app.core.transfer_cleanup.MAX_FOLDERS", 0), self.assertRaises(ValueError):
            preview_transfer_caches(self.output)

    def test_reparse_points_are_rejected_on_runtimes_without_is_junction(self):
        from software_app.core.transfer_workspace import _linked

        legacy_path = SimpleNamespace(is_symlink=lambda: False, lstat=lambda: SimpleNamespace(st_file_attributes=0x400))
        self.assertTrue(_linked(legacy_path))
