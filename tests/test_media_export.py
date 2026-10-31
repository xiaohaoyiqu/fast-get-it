from __future__ import annotations

import csv
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from software_app.core.adapter import TaskCancelled
from software_app.core.media_export import export_unfinished_media, unfinished_media_snapshot
from software_app.core.media_results import media_descriptor
from software_app.core.models import DownloadTask, FileRecord
from software_app.core.storage import AppStorage


class MediaExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "state.db")
        self.storage.create_task("sample", "bluesky", "https://bsky.app/profile/example.test", str(self.root), {})
        task = DownloadTask("sample", "bluesky", "https://bsky.app/profile/example.test", self.root)
        self.items = [
            media_descriptor(
                task,
                "https://bsky.app/profile/example.test/post/abc?token=secret",
                index,
                "image",
                f"https://cdn.example/{index}.jpg?sig=private",
                self.root / "saved",
            )
            for index in (1, 2)
        ]
        self.storage.record_media_results(
            "sample",
            [{**self.items[0], "state": "failed", "error": '=WEBSERVICE("x")'}, {**self.items[1], "state": "pending"}],
        )

    def test_csv_redacts_credentials_and_escapes_spreadsheet_formulas(self):
        destination = self.root / "export.csv"
        self.assertEqual(export_unfinished_media(self.storage, ["sample"], destination), 2)
        with destination.open(encoding="utf-8-sig", newline="") as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 2)
        self.assertTrue(next(row["error"] for row in rows if row["state"] == "failed").startswith("'="))
        content = destination.read_text(encoding="utf-8-sig")
        self.assertNotIn("secret", content)
        self.assertNotIn("private", content)
        self.assertEqual(self.storage.media_summary("sample"), {"failed": 1, "pending": 1})

    def test_repaired_items_are_excluded_from_snapshot_and_txt_export(self):
        self.storage.resolve_media_result("sample", self.items[0]["key"], "retry")
        rows = unfinished_media_snapshot(self.storage, ["sample", "sample"])
        self.assertEqual([row["media_index"] for row in rows], [2])
        destination = self.root / "export.txt"
        self.assertEqual(export_unfinished_media(self.storage, ["sample"], destination), 1)
        self.assertIn("媒体序号：2", destination.read_text(encoding="utf-8-sig"))

    def test_cancel_before_commit_preserves_previous_export_and_removes_temp_file(self):
        destination = self.root / "export.csv"
        destination.write_bytes(b"previous export")
        event = threading.Event()
        with self.assertRaises(TaskCancelled):
            export_unfinished_media(
                self.storage,
                ["sample"],
                destination,
                cancel_event=event,
                on_progress=lambda _current, _total: event.set(),
            )
        self.assertEqual(destination.read_bytes(), b"previous export")
        self.assertFalse(list(self.root.glob(".media-export-*.tmp")))

    def test_export_callback_failure_preserves_previous_export(self):
        destination = self.root / "export.txt"
        destination.write_bytes(b"previous export")

        def fail(_current, _total):
            raise RuntimeError("notification failed")

        with self.assertRaises(RuntimeError):
            export_unfinished_media(self.storage, ["sample"], destination, on_progress=fail)
        self.assertEqual(destination.read_bytes(), b"previous export")
        self.assertFalse(list(self.root.glob(".media-export-*.tmp")))

    def test_exceeding_limit_does_not_create_or_replace_export(self):
        destination = self.root / "export.txt"
        destination.write_bytes(b"previous export")
        with patch("software_app.core.media_export.MAX_EXPORT_ITEMS", 1), self.assertRaises(ValueError):
            export_unfinished_media(self.storage, ["sample"], destination)
        self.assertEqual(destination.read_bytes(), b"previous export")

    def test_export_cannot_replace_an_indexed_resource(self):
        destination = self.root / "novel.txt"
        destination.write_bytes(b"downloaded novel")
        self.storage.upsert_file(FileRecord(destination, "pixiv", media_type="text"))
        with self.assertRaises(ValueError):
            export_unfinished_media(self.storage, ["sample"], destination)
        self.assertEqual(destination.read_bytes(), b"downloaded novel")

    def test_export_cannot_replace_database_even_when_its_suffix_is_txt(self):
        storage = AppStorage(self.root / "database.txt")
        before = storage.db_path.read_bytes()
        with self.assertRaises(ValueError):
            export_unfinished_media(storage, ["sample"], storage.db_path)
        self.assertEqual(storage.db_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
