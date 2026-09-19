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
    build_aria2_command,
    download_torrent_with_aria2,
    install_aria2,
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
            ):
                result = download_torrent_with_aria2(torrent, output, threading.Event())
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
