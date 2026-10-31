from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from software_app.core import library_integrity as integrity
from software_app.core.adapter import TaskCancelled
from software_app.core.library_service import LibraryFilter
from software_app.core.models import FileRecord
from software_app.core.output_lock import output_path_lock
from software_app.core.storage import AppStorage


class LibraryIntegrityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.root = self.home / "media"
        self.root.mkdir()
        self.storage = AppStorage(self.home / "index.sqlite")
        self.manifest = self.home / "baseline.json"
        self.report = self.home / "report.json"

    def add(self, name="作者/作品.bin", content=b"generated media", **metadata):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is not None:
            path.write_bytes(content)
        self.storage.upsert_file(FileRecord(path, "pixiv", "task", "image", 0, **metadata))
        return path

    def export(self, **kwargs):
        return integrity.export_integrity_manifest(self.storage, LibraryFilter(), self.root, self.manifest, **kwargs)

    def verify(self, **kwargs):
        return integrity.verify_integrity_manifest(self.storage, self.manifest, self.root, self.report, **kwargs)

    def document(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def rewrite(self, document):
        self.manifest.write_text(json.dumps(document), encoding="utf-8")

    def snapshot(self):
        with self.storage.connect() as conn:
            return {
                table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                for table in ("files", "tasks", "logs")
            }

    def test_roundtrip_uses_actual_size_digest_and_preserves_database_and_bytes(self):
        path = self.add()
        before, content = self.snapshot(), path.read_bytes()
        self.assertEqual(self.export(), {"recorded": 1})
        item = self.document()["entries"][0]
        self.assertEqual(
            (item["path"], item["size"], item["sha256"]),
            ("作者/作品.bin", len(content), hashlib.sha256(content).hexdigest()),
        )
        self.assertEqual(self.verify(), {"matched": 1})
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(path.read_bytes(), content)

    def test_missing_changed_and_no_baseline_are_distinct(self):
        changed, missing = self.add("changed.bin", b"one"), self.add("missing.bin")
        restored = self.add("restored.bin", None)
        self.assertEqual(self.export(), {"recorded": 2, "missing": 1})
        changed.write_bytes(b"two")  # Same size still requires a digest comparison.
        missing.unlink()
        restored.write_bytes(b"newly restored")
        self.assertEqual(self.verify(), {"changed": 1, "missing": 1, "no_baseline": 1})

    def test_zero_length_file_is_a_valid_local_baseline(self):
        self.add(content=b"")
        self.assertEqual(self.export(), {"recorded": 1})
        self.assertEqual(self.verify(), {"matched": 1})

    def test_metadata_whitelist_removes_credentials_queries_and_engine_metadata(self):
        self.add(
            source_url="https://user:private@media.example/a.jpg?sig=secret#private",
            source_id="123",
            title="作品",
            metadata={"cookie": "private-cookie", "auth": "private-auth"},
        )
        self.export()
        text = self.manifest.read_text(encoding="utf-8")
        for secret in ("private", "secret", "cookie", "metadata_json"):
            self.assertNotIn(secret, text)
        self.assertEqual(self.document()["entries"][0]["source_page"], "https://media.example/a.jpg")
        self.assertNotIn(str(self.root), text)

    def test_manifest_can_verify_a_relocated_explicit_root(self):
        self.add()
        self.export()
        moved = self.home / "moved"
        self.root.rename(moved)
        counts = integrity.verify_integrity_manifest(self.storage, self.manifest, moved, self.report)
        self.assertEqual(counts, {"matched": 1})

    def test_filter_uses_full_index_without_ui_query(self):
        self.add("first.bin", title="selected")
        self.add("second.bin", title="other")
        with patch.object(self.storage, "list_library_files", side_effect=AssertionError("UI limit")):
            counts = integrity.export_integrity_manifest(
                self.storage, LibraryFilter(title="selected"), self.root, self.manifest
            )
        self.assertEqual(counts, {"recorded": 1})
        self.assertEqual([item["path"] for item in self.document()["entries"]], ["first.bin"])

    def test_hashing_does_not_hold_a_database_read_snapshot(self):
        self.add()
        original_rows, original_hash = integrity.iter_library_rows, integrity._hash
        active = [False]

        def rows(*args, **kwargs):
            active[0] = True
            try:
                yield from original_rows(*args, **kwargs)
            finally:
                active[0] = False

        def read(*args):
            self.assertFalse(active[0], "hashing must not pin the WAL read snapshot")
            return original_hash(*args)

        with patch.object(integrity, "iter_library_rows", rows), patch.object(integrity, "_hash", read):
            self.assertEqual(self.export(), {"recorded": 1})

    def test_selected_resource_outside_root_rejects_export_without_output(self):
        self.add()
        outside = self.home / "outside.bin"
        outside.write_bytes(b"outside")
        self.storage.upsert_file(FileRecord(outside, "pixiv"))
        with self.assertRaisesRegex(ValueError, "根目录之外"):
            self.export()
        self.assertFalse(self.manifest.exists())
        self.assertEqual(outside.read_bytes(), b"outside")

    def test_existing_manifest_report_and_media_are_never_overwritten(self):
        path = self.add("keep.json", b"keep")
        self.export()
        baseline = self.manifest.read_bytes()
        with self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.manifest.read_bytes(), baseline)
        self.report.write_bytes(b"previous report")
        with self.assertRaises(FileExistsError):
            self.verify()
        with self.assertRaises(FileExistsError):
            integrity.export_integrity_manifest(self.storage, LibraryFilter(), self.root, path)
        self.assertEqual(self.report.read_bytes(), b"previous report")
        self.assertEqual(path.read_bytes(), b"keep")

    def test_missing_indexed_destination_cannot_become_manifest(self):
        target = self.add("reserved.json", None)
        self.add()
        with self.assertRaisesRegex(ValueError, "索引"):
            integrity.export_integrity_manifest(self.storage, LibraryFilter(title="nothing"), self.root, target)
        self.assertFalse(target.exists())

    def test_long_name_output_alias_does_not_replace_a_missing_index_key(self):
        target = self.add("reserved.json", None)
        self.add()
        with self.assertRaisesRegex(ValueError, "索引"):
            integrity.export_integrity_manifest(self.storage, LibraryFilter(), self.root, target.resolve())
        self.assertFalse(target.exists())

    def test_cancel_before_and_during_hash_never_publishes(self):
        self.add(content=b"x" * (2 * integrity.CHUNK_BYTES))
        event = threading.Event()
        event.set()
        with self.assertRaises(TaskCancelled):
            self.export(cancel_event=event)
        event.clear()
        real = hashlib.sha256

        class Digest:
            def __init__(self):
                self.inner = real()

            def update(self, data):
                self.inner.update(data)
                event.set()

            def hexdigest(self):
                return self.inner.hexdigest()

        with patch.object(integrity, "hashlib", SimpleNamespace(sha256=Digest)), self.assertRaises(TaskCancelled):
            self.export(cancel_event=event)
        self.assertFalse(self.manifest.exists())
        self.assertFalse(list(self.home.glob(".integrity-*.tmp")))

    def test_cancel_verification_does_not_publish_or_change_baseline(self):
        self.add()
        self.export()
        original = self.manifest.read_bytes()
        event = threading.Event()
        event.set()
        with self.assertRaises(TaskCancelled):
            self.verify(cancel_event=event)
        self.assertFalse(self.report.exists())
        self.assertEqual(self.manifest.read_bytes(), original)

    def test_file_changes_during_hash_do_not_create_a_trusted_baseline(self):
        path = self.add(content=b"x" * (2 * integrity.CHUNK_BYTES))
        real = hashlib.sha256

        class Digest:
            def __init__(self):
                self.inner, self.changed = real(), False

            def update(self, data):
                self.inner.update(data)
                if not self.changed:
                    self.changed = True
                    path.write_bytes(b"shortened")

            def hexdigest(self):
                return self.inner.hexdigest()

        with patch.object(integrity, "hashlib", SimpleNamespace(sha256=Digest)):
            self.assertEqual(self.export(), {"changed": 1})
        self.assertIsNone(self.document()["entries"][0]["sha256"])

    def test_busy_output_is_reported_without_reading_and_cancellation_remains_available(self):
        path = self.add()
        ready, release = threading.Event(), threading.Event()

        def owner():
            with output_path_lock(path):
                ready.set()
                release.wait(5)

        thread = threading.Thread(target=owner)
        thread.start()
        try:
            self.assertTrue(ready.wait(3))
            self.assertEqual(self.export(), {"busy": 1})
            self.assertIsNone(self.document()["entries"][0]["sha256"])
        finally:
            release.set()
            thread.join(3)

    def test_parent_reparse_point_is_reported_as_error(self):
        path = self.add()
        original = Path.lstat

        def lstat(item, *args, **kwargs):
            info = original(item, *args, **kwargs)
            if item == path.parent:
                return SimpleNamespace(st_mode=info.st_mode, st_file_attributes=0x400)
            return info

        with patch.object(Path, "lstat", lstat):
            self.assertEqual(self.export(), {"error": 1})

    def test_invalid_paths_are_rejected_before_any_target_file_is_read(self):
        self.add()
        self.export()
        document = self.document()
        for relative in (
            "../outside",
            "/absolute",
            "C:/outside",
            "//server/share",
            "a/../b",
            "a\\b",
            "a/",
            "NUL",
            "file:stream",
        ):
            with self.subTest(relative=relative):
                altered = copy.deepcopy(document)
                altered["entries"][0]["path"] = relative
                self.rewrite(altered)
                with (
                    patch.object(integrity, "_inspect", side_effect=AssertionError("read escaped file")),
                    self.assertRaises(ValueError),
                ):
                    self.verify()
        self.assertFalse(self.report.exists())

    def test_bad_types_digests_and_duplicate_entries_are_rejected(self):
        self.add()
        self.export()
        document = self.document()
        for mutation in ({"size": True}, {"size": -1}, {"sha256": "bad"}, {"status": []}, {"path": 1}):
            altered = copy.deepcopy(document)
            altered["entries"][0].update(mutation)
            self.rewrite(altered)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify()
        document["entries"].append(copy.deepcopy(document["entries"][0]))
        self.rewrite(document)
        with self.assertRaisesRegex(ValueError, "重复"):
            self.verify()

    def test_unknown_version_and_duplicate_json_fields_are_rejected(self):
        self.add()
        self.export()
        document = self.document()
        document["version"] = 2
        self.rewrite(document)
        with self.assertRaises(ValueError):
            self.verify()
        self.manifest.write_text('{"version": 1, "version": 2}')
        with self.assertRaisesRegex(ValueError, "重复"):
            self.verify()

    def test_limits_fail_without_publishing_partial_data(self):
        self.add("one.bin")
        self.add("two.bin")
        with patch.object(integrity, "MAX_ENTRIES", 1), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(self.manifest.exists())
        with patch.object(integrity, "MAX_JSON_BYTES", 100), self.assertRaises(ValueError):
            self.export()
        self.assertFalse(list(self.home.glob(".integrity-*.tmp")))

    def test_exclusive_publication_keeps_an_external_file_created_at_commit(self):
        self.add()
        link = os.link

        def race(source, destination):
            Path(destination).write_bytes(b"created by another program")
            link(source, destination)

        with patch.object(integrity.os, "link", side_effect=race), self.assertRaises(FileExistsError):
            self.export()
        self.assertEqual(self.manifest.read_bytes(), b"created by another program")
        self.assertFalse(list(self.home.glob(".integrity-*.tmp")))

    def test_imported_manifest_resource_cannot_become_report_even_when_missing_and_unindexed(self):
        path = self.add("resource.json")
        self.export()
        path.unlink()
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM files")
        with self.assertRaisesRegex(ValueError, "资源路径"):
            integrity.verify_integrity_manifest(self.storage, self.manifest, self.root, path)
        self.assertFalse(path.exists())
