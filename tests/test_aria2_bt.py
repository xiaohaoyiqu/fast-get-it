from __future__ import annotations

import hashlib
import io
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from software_app.core.adapter import TaskCancelled
from software_app.core.aria2_bt import (
    Aria2Status,
    Aria2Limits,
    validate_aria2_limits,
    aria2_limits_from_options,
    build_aria2_command,
    download_torrent_with_aria2,
    install_aria2_from_archive,
)


class _FinishedProcess:
    returncode = 0

    def poll(self):
        return 0


class _WaitingProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def wait(self, timeout=None):
        del timeout
        return self.returncode


class Aria2BtTests(unittest.TestCase):
    def test_limits_have_explicit_engine_bytes_and_disabled_means_unlimited(self):
        command = build_aria2_command(Path("aria2c"), Path("input.torrent"), Path("out"), limits=Aria2Limits(True, 256, 32))
        self.assertIn("--max-overall-download-limit=262144", command)
        self.assertIn("--max-overall-upload-limit=32768", command)
        self.assertIn("--no-conf=true", command)
        command = build_aria2_command(Path("aria2c"), Path("input.torrent"), Path("out"))
        self.assertIn("--max-overall-download-limit=0", command)
        self.assertIn("--max-overall-upload-limit=0", command)

    def test_limits_reject_invalid_values_including_tampered_task_options(self):
        for enabled, down, up in ((True, 0, 1), (True, -1, 1), (True, 1, 1048577), (True, "1;bad", 1),
                                  ("true", 1, 1), (True, True, 1), (True, "1.5", 1)):
            with self.subTest(values=(enabled, down, up)), self.assertRaises(ValueError):
                validate_aria2_limits(enabled, down, up)
        with self.assertRaises(ValueError):
            aria2_limits_from_options({"aria2_download_kib": "bad"})

    def test_task_snapshot_preserves_limits_and_old_tasks_keep_unlimited_default(self):
        from software_app.core.storage import AppStorage
        from software_app.core.task_manager import TaskManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = AppStorage(root / "app.db")
            manager = TaskManager(storage)
            storage.set_settings(Aria2Limits(True, 256, 32).options())
            _folder, original = manager.prepare_task_settings("ehentai", "https://e-hentai.org/g/1/token/", root, {})
            storage.set_settings(Aria2Limits(True, 512, 64).options())
            _folder, kept = manager.prepare_task_settings("ehentai", "https://e-hentai.org/g/1/token/", root,
                                                          {**original, "_keep_task_settings": True})
            self.assertEqual(aria2_limits_from_options(kept), Aria2Limits(True, 256, 32))
            _folder, legacy = manager.prepare_task_settings("ehentai", "https://e-hentai.org/g/1/token/", root,
                                                            {"_keep_task_settings": True})
            self.assertFalse(aria2_limits_from_options(legacy).enabled)
            self.assertFalse(aria2_limits_from_options({}).enabled)

    def test_current_settings_retry_replaces_old_limits(self):
        from software_app.core.storage import AppStorage
        from software_app.core.task_manager import TaskManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = AppStorage(root / "app.db")
            manager = TaskManager(storage)
            storage.create_task("old", "ehentai", "https://e-hentai.org/g/1/token/", str(root), Aria2Limits(True, 256, 32).options())
            storage.update_task_status("old", "failed")
            storage.set_settings(Aria2Limits(True, 512, 64).options())
            with patch.object(manager, "start_task", side_effect=lambda _module, _target, _output, options, *_args, **_kwargs: options):
                current = manager.retry_task("old", use_current_settings=True)
                original = manager.retry_task("old", use_current_settings=False)
            self.assertEqual(aria2_limits_from_options(current), Aria2Limits(True, 512, 64))
            self.assertEqual(aria2_limits_from_options(original), Aria2Limits(True, 256, 32))

    def test_command_uses_safe_argument_list_and_stops_seeding(self):
        command = build_aria2_command(Path("aria2c.exe"), Path("sample.torrent"), Path("output folder"))
        self.assertEqual(command[0], "aria2c.exe")
        self.assertIn("--seed-time=0", command)
        self.assertIn("--seed-ratio=0.0", command)
        self.assertIn("--auto-file-renaming=false", command)
        self.assertIn("--enable-dht=false", command)
        self.assertIn("--enable-peer-exchange=false", command)
        self.assertEqual(command[-1], "sample.torrent")

    def test_runner_returns_only_completed_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            torrent = root / "sample.torrent"
            torrent.write_bytes(b"torrent")
            output = root / "output"
            output.mkdir()
            completed = output / "payload.bin"
            completed.write_bytes(b"done")
            (output / "payload.bin.aria2").write_bytes(b"partial")
            with patch("software_app.core.aria2_bt.find_aria2c", return_value=(root / "aria2c.exe", "test")), patch(
                "software_app.core.aria2_bt.subprocess.Popen", return_value=_FinishedProcess()
            ) as launch:
                result = download_torrent_with_aria2(torrent, output, threading.Event(), limits=Aria2Limits(True, 256, 32))
                self.assertIn("--max-overall-download-limit=262144", launch.call_args.args[0])
            self.assertEqual(result.files, (completed.resolve(),))
            self.assertEqual(result.total_size, 4)

    def test_runner_terminates_on_cancellation(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            torrent = root / "sample.torrent"
            torrent.write_bytes(b"torrent")
            cancelled = threading.Event()
            cancelled.set()
            process = _WaitingProcess()
            with patch("software_app.core.aria2_bt.find_aria2c", return_value=(root / "aria2c.exe", "test")), patch(
                "software_app.core.aria2_bt.subprocess.Popen", return_value=process
            ):
                with self.assertRaises(TaskCancelled):
                    download_torrent_with_aria2(torrent, root / "output", cancelled)
            self.assertTrue(process.terminated)

    def test_installer_verifies_archive_and_writes_only_executable(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("aria2-1.37.0/aria2c.exe", b"safe-executable")
            archive.writestr("aria2-1.37.0/README.txt", b"ignored")
        payload = buffer.getvalue()
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            target = Path(directory) / "aria2c.exe"
            status = Aria2Status(target, "aria2 version 1.37.0", "bundled", "aria2 可用")
            archive_path = Path(directory) / "aria2.zip"
            archive_path.write_bytes(payload)
            with patch(
                "software_app.core.aria2_bt.ARIA2_WINDOWS_X64_SHA256", hashlib.sha256(payload).hexdigest()
            ), patch("software_app.core.aria2_bt.bundled_aria2_path", return_value=target), patch(
                "software_app.core.aria2_bt.aria2_status", return_value=status
            ):
                result = install_aria2_from_archive(archive_path)
            self.assertEqual(result, status)
            self.assertEqual(target.read_bytes(), b"safe-executable")
            self.assertFalse((target.parent / "README.txt").exists())


if __name__ == "__main__":
    unittest.main()
