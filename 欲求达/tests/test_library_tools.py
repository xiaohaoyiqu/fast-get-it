from __future__ import annotations

import csv
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.library_service import (
    LibraryFilter, export_library_csv, plan_library_cleanup, execute_library_cleanup,
    plan_library_paths, rename_library_file,
)
from software_app.core.models import FileRecord, ModuleInfo, TargetPreview
from software_app.core.output_lock import output_path_lock
from software_app.core.storage import AppStorage, SCHEMA, UPSERT_FILE_SQL, _file_record_values
from software_app.core.task_manager import TaskManager
from software_app.ui.library_tools import LibraryToolsDialog
from software_app.ui.library_tab import LibraryTabMixin
from types import SimpleNamespace


class EmptyAdapter(CrawlerAdapter):
    info = ModuleInfo("pixiv", "Pixiv")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("pixiv", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        pass


class LibraryToolsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.storage = AppStorage(self.root / "library.sqlite3")
        self.manager = TaskManager(self.storage)
        self.manager.register_adapter(EmptyAdapter())

    def tearDown(self):
        self.manager.shutdown(3)
        self.directory.cleanup()

    def add_file(self, name="one.jpg", *, exists=True, **kwargs):
        path = self.root / name
        if exists:
            path.write_bytes(b"image")
        record = FileRecord(path, kwargs.pop("module_id", "pixiv"), kwargs.pop("task_id", "old"),
                            kwargs.pop("media_type", "image"), kwargs.pop("size", 5), **kwargs)
        self.storage.upsert_file(record)
        return record

    def records(self):
        return {str(row["path"]): dict(row) for row in self.storage.list_files(limit=10000)}

    def test_filters_combine_exact_tags_unicode_author_and_inclusive_dates(self):
        record = self.add_file(author_id="123", author_name="ＡＲＴＩＳＴ", title="标题 测试", tags=("风景",),
                               published_at="2026-10-28T10:00:00+08:00")
        self.add_file("other.jpg", author_id="123", title="标题 测试", tags=("风景画",), published_at="2026-10-28")
        filters = LibraryFilter(module_id="pixiv", media_type="image", author="artist", title="测试", tag="风景",
                                query="标题 one.jpg", start_date="2026-10-28", end_date="2026-10-28", date_basis="published_at")
        plan = plan_library_cleanup(self.storage, filters)
        self.assertEqual([entry.path for entry in plan.entries], [str(record.path)])
        self.assertEqual(len(plan_library_cleanup(self.storage, LibraryFilter(author="123")).entries), 2)

    def test_unknown_dates_are_excluded_and_date_bases_are_independent(self):
        self.add_file(published_at="2026-09-01")
        self.add_file("unknown.jpg")
        with self.storage.connect() as conn:
            conn.execute("UPDATE files SET created_at='2026-10-28 12:00:00'")
        self.assertEqual(len(plan_library_cleanup(self.storage, LibraryFilter(start_date="2026-10-01")).entries), 2)
        self.assertEqual(len(plan_library_cleanup(self.storage, LibraryFilter(
            start_date="2026-10-01", date_basis="published_at")).entries), 0)

    def test_invalid_dates_and_date_basis_are_rejected_before_query(self):
        for options in ({"start_date": "2026-02-30"}, {"start_date": "20261028"},
                        {"start_date": "2026-10-28", "end_date": "2026-10-01"}, {"date_basis": "mtime"}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                LibraryFilter(**options)

    def test_export_reads_beyond_ui_limits_and_preserves_zero_and_unicode(self):
        records = [FileRecord(self.root / f"{index}.jpg", "pixiv", None, "image", 0, title=f"标题{index}")
                   for index in range(601)]
        with self.storage.connect() as conn:
            conn.executemany(UPSERT_FILE_SQL, [_file_record_values(record) for record in records])
        target = self.root / "export.csv"
        with patch.object(self.storage, "list_library_files", side_effect=AssertionError("must not use UI limits")):
            self.assertEqual(export_library_csv(self.storage, LibraryFilter(), target), 601)
        with target.open(encoding="utf-8-sig", newline="") as output:
            rows = list(csv.DictReader(output))
        self.assertEqual(len(rows), 601)
        self.assertEqual(rows[0]["size"], "0")
        self.assertEqual(rows[0]["local_exists"], "false")
        self.assertTrue(rows[0]["title"].startswith("标题"))
        self.assertTrue(target.read_bytes().startswith(b"\xef\xbb\xbf"))

    def test_export_quotes_formulas_and_redacts_urls_without_raw_metadata(self):
        self.add_file(title='=HYPERLINK("bad")', source_url="https://example.com/image?token=secret-value&format=png",
                      metadata={"cookie": "private-cookie"})
        target = self.root / "export.csv"
        export_library_csv(self.storage, LibraryFilter(), target)
        with target.open(encoding="utf-8-sig", newline="") as output:
            row = next(csv.DictReader(output))
        self.assertTrue(row["title"].startswith("'="))
        content = target.read_text(encoding="utf-8-sig")
        self.assertNotIn("secret-value", content)
        self.assertNotIn("private-cookie", content)
        self.assertIn("format=png", row["source_url"])

    def test_export_never_overwrites_a_resource_outside_the_selected_filter(self):
        record = self.add_file("keep.csv", module_id="twitter")
        with self.assertRaises(ValueError):
            export_library_csv(self.storage, LibraryFilter(module_id="pixiv"), record.path)
        self.assertEqual(record.path.read_bytes(), b"image")

    def test_cancelled_export_preserves_previous_csv_and_removes_temporary(self):
        self.add_file()
        target = self.root / "export.csv"
        target.write_text("previous", encoding="utf-8")
        stop = threading.Event()
        stop.set()
        with self.assertRaises(TaskCancelled):
            export_library_csv(self.storage, LibraryFilter(), target, cancel_event=stop)
        self.assertEqual(target.read_text(), "previous")
        self.assertEqual(list(self.root.glob(".library-*.tmp")), [])

    def test_preview_counts_missing_files_and_refuses_silent_truncation(self):
        self.add_file()
        self.add_file("missing.jpg", exists=False)
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        self.assertEqual((len(plan.entries), plan.size, plan.missing), (2, 5, 1))
        with self.assertRaisesRegex(ValueError, "缩小"):
            plan_library_cleanup(self.storage, LibraryFilter(), limit=1)
        self.assertEqual(len(self.records()), 2)

    def test_index_only_cleanup_keeps_files_tasks_and_logs(self):
        record = self.add_file()
        self.storage.create_task("old", "pixiv", "123", str(self.root), {})
        self.storage.update_task_status("old", "completed")
        self.storage.add_log("old", "pixiv", "info", "keep history")
        recycle = Mock()
        result = execute_library_cleanup(self.manager, plan_library_cleanup(self.storage, LibraryFilter()),
                                         mode="index", recycle=recycle)
        self.assertEqual(result.removed_indexes, 1)
        self.assertTrue(record.path.exists())
        self.assertEqual(self.storage.get_task("old")["status"], "completed")
        self.assertEqual(len(self.storage.list_logs("old")), 1)
        recycle.assert_not_called()

    def test_recycle_cleanup_only_removes_matching_files_and_indexes(self):
        selected = self.add_file(author_id="1")
        other = self.add_file("other.jpg", author_id="2")
        self.add_file("missing.jpg", exists=False, author_id="1")
        plan = plan_library_cleanup(self.storage, LibraryFilter(author="1"))
        recycled = []

        def recycle(path):
            recycled.append(path)
            path.unlink()

        result = execute_library_cleanup(self.manager, plan, mode="recycle", recycle=recycle)
        self.assertEqual((result.removed_indexes, result.recycled), (2, 1))
        self.assertEqual(recycled, [selected.path])
        self.assertEqual(set(self.records()), {str(other.path)})
        self.assertTrue(other.path.exists())

    def test_changed_files_and_changed_or_reinserted_indexes_are_skipped(self):
        first = self.add_file()
        second = self.add_file("second.jpg")
        third = self.add_file("third.jpg")
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        first.path.write_bytes(b"changed bytes")
        self.storage.upsert_file(second)
        self.storage.delete_file_record(third.path)
        self.storage.upsert_file(third)
        recycle = Mock()
        result = execute_library_cleanup(self.manager, plan, mode="recycle", recycle=recycle)
        self.assertEqual((result.skipped, result.removed_indexes), (3, 0))
        self.assertEqual(len(self.records()), 3)
        recycle.assert_not_called()

    def test_failed_recycle_keeps_index_and_file(self):
        record = self.add_file()
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        result = execute_library_cleanup(self.manager, plan, mode="recycle", recycle=Mock(side_effect=OSError("locked")))
        self.assertEqual(result.removed_indexes, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertTrue(record.path.exists())
        self.assertIn(str(record.path), self.records())

    def test_recycle_that_does_not_move_file_keeps_its_index(self):
        self.add_file()
        result = execute_library_cleanup(self.manager, plan_library_cleanup(self.storage, LibraryFilter()),
                                         mode="recycle", recycle=Mock())
        self.assertEqual(result.removed_indexes, 0)
        self.assertEqual(len(self.records()), 1)
        self.assertTrue(result.errors)

    def test_cancelled_cleanup_does_not_modify_remaining_items(self):
        self.add_file()
        stop = threading.Event()
        stop.set()
        result = execute_library_cleanup(self.manager, plan_library_cleanup(self.storage, LibraryFilter()),
                                         mode="index", cancel_event=stop)
        self.assertTrue(result.cancelled)
        self.assertEqual(len(self.records()), 1)

    def test_maintenance_blocks_new_tasks_without_creating_ghost_rows(self):
        with self.manager.library_maintenance():
            with self.assertRaisesRegex(ValueError, "清理"):
                self.manager.start_task("pixiv", "123", self.root)
        self.assertEqual(self.storage.list_tasks(), [])
        self.assertFalse(self.manager._library_maintenance)

    def test_active_tasks_in_another_manager_prevent_cleanup(self):
        self.add_file()
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        for status in ("queued", "running", "paused", "cancelling"):
            with self.subTest(status=status):
                with self.storage.connect() as conn:
                    conn.execute("DELETE FROM tasks")
                self.storage.create_task("other-process", "pixiv", "123", str(self.root), {})
                self.storage.update_task_status("other-process", status)
                result = execute_library_cleanup(self.manager, plan, mode="index")
                self.assertEqual(result.removed_indexes, 0)
                self.assertTrue(result.errors)
                self.assertEqual(len(self.records()), 1)

    def test_busy_output_is_skipped_instead_of_waiting_indefinitely(self):
        record = self.add_file()
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        acquired, release = threading.Event(), threading.Event()

        def owner():
            with output_path_lock(record.path):
                acquired.set()
                release.wait(5)

        thread = threading.Thread(target=owner)
        thread.start()
        try:
            self.assertTrue(acquired.wait(3))
            result = execute_library_cleanup(self.manager, plan, mode="index")
            self.assertEqual(result.skipped, 1)
            self.assertEqual(len(self.records()), 1)
        finally:
            release.set()
            thread.join(3)

    def test_new_upserts_change_index_revisions(self):
        record = self.add_file()
        with self.storage.connect() as conn:
            first = conn.execute("SELECT revision FROM files").fetchone()[0]
        self.storage.upsert_file(record)
        with self.storage.connect() as conn:
            second = conn.execute("SELECT revision FROM files").fetchone()[0]
        self.assertNotEqual(first, second)
        self.assertEqual(len(second), 32)

    def test_legacy_database_gains_revision_column_without_losing_rows(self):
        legacy_path = self.root / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(SCHEMA.replace("    revision TEXT NOT NULL DEFAULT '',\n", ""))
            conn.execute("INSERT INTO files(path,module_id,media_type) VALUES (?,?,?)", (str(self.root / "legacy.jpg"), "pixiv", "image"))
            conn.commit()
        finally:
            conn.close()
        storage = AppStorage(legacy_path)
        row = storage.list_files()[0]
        self.assertEqual(row["revision"], "")
        self.assertEqual(len(plan_library_cleanup(storage, LibraryFilter()).entries), 1)

    def test_failed_export_keeps_previous_destination_and_cleans_temporary(self):
        record = self.add_file()
        target = self.root / "export.csv"
        target.write_text("previous", encoding="utf-8")

        def rows(*_args, **_kwargs):
            yield self.records()[str(record.path)]
            raise OSError("database read failed")

        with patch("software_app.core.library_service.iter_library_rows", side_effect=rows):
            with self.assertRaises(OSError):
                export_library_csv(self.storage, LibraryFilter(), target)
        self.assertEqual(target.read_text(), "previous")
        self.assertFalse(list(self.root.glob(".library-*.tmp")))

    def test_malformed_tags_do_not_break_other_filters(self):
        record = self.add_file(title="标题")
        with self.storage.connect() as conn:
            conn.execute("UPDATE files SET tags_json=? WHERE path=?", ("{bad", str(record.path)))
        self.assertEqual(len(plan_library_cleanup(self.storage, LibraryFilter(title="标题")).entries), 1)
        self.assertEqual(len(plan_library_cleanup(self.storage, LibraryFilter(tag="风景")).entries), 0)

    def test_declining_cleanup_confirmation_never_starts_the_worker(self):
        self.add_file()
        filters = LibraryFilter()
        ui = SimpleNamespace(_busy=False, _plan=plan_library_cleanup(self.storage, filters),
                             _plan_filters=filters, _filters=lambda: filters, _start=Mock())
        with patch("software_app.ui.library_tools.messagebox.askyesno", return_value=False) as confirm:
            LibraryToolsDialog._cleanup(ui, "recycle")
        confirm.assert_called_once()
        ui._start.assert_not_called()
        self.assertEqual(len(self.records()), 1)

    def test_cleanup_in_same_manager_rejects_even_paused_work(self):
        self.add_file()
        plan = plan_library_cleanup(self.storage, LibraryFilter())
        self.manager.running["paused-worker"] = object()
        try:
            with self.assertRaisesRegex(ValueError, "暂停"):
                execute_library_cleanup(self.manager, plan, mode="index")
            self.assertEqual(len(self.records()), 1)
        finally:
            self.manager.running.pop("paused-worker")

    def test_selected_path_plan_does_not_expand_to_other_records(self):
        first = self.add_file()
        self.add_file("other.jpg")
        plan = plan_library_paths(self.storage, [first.path, first.path])
        self.assertEqual([entry.path for entry in plan.entries], [str(first.path)])
        with self.assertRaises(ValueError):
            plan_library_paths(self.storage, [self.root / "unindexed.jpg"])

    def test_rename_updates_exact_index_and_preserves_other_metadata(self):
        record = self.add_file(author_id="123", source_url="https://example.com/source")
        entry = plan_library_paths(self.storage, [record.path]).entries[0]
        destination = self.root / "renamed.jpg"
        self.assertTrue(rename_library_file(self.manager, entry, destination))
        self.assertFalse(record.path.exists())
        self.assertEqual(destination.read_bytes(), b"image")
        row = self.records()[str(destination)]
        self.assertEqual(row["author_id"], "123")
        self.assertEqual(row["source_url"], record.source_url)
        self.assertNotEqual(row["revision"], entry.revision)

    def test_rename_rejects_changed_file_or_index(self):
        for changed in ("file", "index"):
            with self.subTest(changed=changed):
                record = self.add_file(changed + ".jpg")
                entry = plan_library_paths(self.storage, [record.path]).entries[0]
                if changed == "file":
                    record.path.write_bytes(b"updated")
                else:
                    self.storage.upsert_file(record)
                destination = record.path.with_name(changed + "-new.jpg")
                with self.assertRaises(ValueError):
                    rename_library_file(self.manager, entry, destination)
                self.assertTrue(record.path.exists())
                self.assertFalse(destination.exists())

    def test_rename_never_overwrites_existing_file_or_reserved_index(self):
        record = self.add_file()
        entry = plan_library_paths(self.storage, [record.path]).entries[0]
        other = self.add_file("other.jpg")
        with self.assertRaises(FileExistsError):
            rename_library_file(self.manager, entry, other.path)
        other.path.unlink()
        with self.assertRaises(FileExistsError):
            rename_library_file(self.manager, entry, other.path)
        self.assertTrue(record.path.exists())

    def test_rename_rolls_back_file_when_index_update_fails(self):
        record = self.add_file()
        entry = plan_library_paths(self.storage, [record.path]).entries[0]
        with self.storage.connect() as conn:
            conn.execute("CREATE TRIGGER reject_rename BEFORE UPDATE OF path ON files BEGIN SELECT RAISE(ABORT,'test failure'); END")
        destination = self.root / "new.jpg"
        with self.assertRaises(sqlite3.DatabaseError):
            rename_library_file(self.manager, entry, destination)
        self.assertEqual(record.path.read_bytes(), b"image")
        self.assertFalse(destination.exists())
        self.assertIn(str(record.path), self.records())

    def test_rename_rejects_shared_database_active_tasks(self):
        record = self.add_file()
        entry = plan_library_paths(self.storage, [record.path]).entries[0]
        self.storage.create_task("foreign", "pixiv", "x", str(self.root), {})
        with self.assertRaises(ValueError):
            rename_library_file(self.manager, entry, self.root / "new.jpg")
        self.assertTrue(record.path.exists())

    def test_original_rename_button_rejects_a_file_changed_during_name_prompt(self):
        record = self.add_file()
        ui = SimpleNamespace(storage=self.storage, manager=self.manager,
                             _selected_library_file_paths=lambda: [record.path])

        def prompt(*_args, **_kwargs):
            record.path.write_bytes(b"changed during prompt")
            return "new.jpg"

        with patch("software_app.ui.library_tab.simpledialog.askstring", side_effect=prompt), patch(
            "software_app.ui.library_tab.messagebox.showerror",
        ) as error:
            LibraryTabMixin._rename_selected_file(ui)
        error.assert_called_once()
        self.assertTrue(record.path.exists())
        self.assertFalse((self.root / "new.jpg").exists())

    def test_original_recycle_button_obeys_maintenance_guard(self):
        record = self.add_file()
        ui = SimpleNamespace(storage=self.storage, manager=self.manager,
                             _selected_library_file_paths=lambda: [record.path])
        self.manager.running["busy"] = object()
        try:
            with patch("software_app.ui.library_tab.messagebox.askyesno", return_value=True), patch(
                "software_app.ui.library_tab.messagebox.showerror",
            ) as error, patch("software_app.ui.library_tab._send_path_to_recycle_bin") as recycle:
                LibraryTabMixin._recycle_selected_files(ui)
            error.assert_called_once()
            recycle.assert_not_called()
            self.assertTrue(record.path.exists())
        finally:
            self.manager.running.pop("busy")

    def test_original_recycle_button_does_nothing_when_confirmation_is_declined(self):
        record = self.add_file()
        ui = SimpleNamespace(storage=self.storage, manager=self.manager,
                             _selected_library_file_paths=lambda: [record.path])
        with patch("software_app.ui.library_tab.messagebox.askyesno", return_value=False), patch(
            "software_app.ui.library_tab._send_path_to_recycle_bin",
        ) as recycle:
            LibraryTabMixin._recycle_selected_files(ui)
        recycle.assert_not_called()
        self.assertIn(str(record.path), self.records())
