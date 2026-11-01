"""Installer failures must preserve the previous executable."""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
import threading
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from software_app.core import aria2_bt, browser_driver


def archive_payload(name, content=b"new executable"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("package/" + name, content)
    return buffer.getvalue()


class ToolInstallTests(unittest.TestCase):
    @contextmanager
    def chrome(self, root, content=b"new executable"):
        target = root / "chromedriver.exe"
        with ExitStack() as stack:
            for name, value in (
                ("global_driver_path", target),
                ("chrome_version", "123.0.1.0"),
                ("find_chromedriver_download_url", "https://example.invalid/driver.zip"),
                ("download_url", archive_payload(target.name, content)),
            ):
                stack.enter_context(patch.object(browser_driver, name, return_value=value))
            stack.enter_context(patch.object(browser_driver.time, "sleep"))
            yield target

    @contextmanager
    def aria(self, root, content=b"new executable"):
        target = root / "aria2c.exe"
        payload = archive_payload(target.name, content)
        with (
            patch.object(aria2_bt, "bundled_aria2_path", return_value=target),
            patch.object(aria2_bt, "ARIA2_WINDOWS_X64_SHA256", hashlib.sha256(payload).hexdigest()),
        ):
            yield target, payload

    def test_chrome_unrunnable_candidate_preserves_old_driver(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")
            with patch.object(browser_driver, "chromedriver_version", return_value="") as check:
                with self.assertRaises(browser_driver.BrowserDriverError):
                    browser_driver.download_chromedriver()
            self.assertEqual(target.read_bytes(), b"old driver")
            self.assertEqual(check.call_count, 3)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_mismatched_candidate_preserves_old_driver(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")
            with patch.object(browser_driver, "chromedriver_version", return_value="124.0.1.0"):
                with self.assertRaises(browser_driver.BrowserDriverError):
                    browser_driver.download_chromedriver()
            self.assertEqual(target.read_bytes(), b"old driver")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_validation_reads_candidate_while_old_driver_is_intact(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")

            def check(candidate):
                self.assertNotEqual(candidate, target)
                self.assertEqual(candidate.parent, target.parent)
                self.assertEqual(candidate.suffix, ".exe")
                self.assertEqual(candidate.read_bytes(), b"new executable")
                self.assertEqual(target.read_bytes(), b"old driver")
                return "123.0.2.0"

            with patch.object(browser_driver, "chromedriver_version", side_effect=check):
                self.assertEqual(browser_driver.download_chromedriver(), target.resolve())
            self.assertEqual(target.read_bytes(), b"new executable")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_first_install_creates_parent_and_validates_before_commit(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory) / "new") as target:
            with patch.object(browser_driver, "chromedriver_version", return_value="123.0.2.0"):
                browser_driver.download_chromedriver()
            self.assertEqual(target.read_bytes(), b"new executable")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_commit_failure_preserves_old_driver_and_cleans_candidate(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")
            with (
                patch.object(browser_driver, "chromedriver_version", return_value="123.0.2.0"),
                patch.object(Path, "replace", side_effect=PermissionError("driver in use")),
            ):
                with self.assertRaises(PermissionError):
                    browser_driver.download_chromedriver()
            self.assertEqual(target.read_bytes(), b"old driver")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_concurrent_installs_use_independent_candidates(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")
            barrier = threading.Barrier(2)
            candidates = []
            lock = threading.Lock()

            def check(candidate):
                with lock:
                    candidates.append(candidate)
                barrier.wait(timeout=5)
                self.assertEqual(candidate.read_bytes(), b"new executable")
                return "123.0.2.0"

            with patch.object(browser_driver, "chromedriver_version", side_effect=check), ThreadPoolExecutor(2) as pool:
                futures = [pool.submit(browser_driver.download_chromedriver) for _ in range(2)]
                committed = 0
                for future in futures:
                    try:
                        self.assertEqual(future.result(timeout=10), target.resolve())
                        committed += 1
                    except PermissionError:
                        # Windows can reject simultaneous replacement of an exe;
                        # that must preserve the other committed tool as well.
                        if os.name != "nt":
                            raise
            self.assertGreaterEqual(committed, 1)
            self.assertEqual(len(set(candidates)), 2)
            self.assertEqual(target.read_bytes(), b"new executable")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_aria_unrunnable_candidate_preserves_old_tool(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.aria(Path(directory)) as (target, payload):
            target.write_bytes(b"old aria2")
            with patch.object(aria2_bt, "aria2_status", side_effect=aria2_bt.Aria2Error("cannot run")):
                with self.assertRaises(aria2_bt.Aria2Error):
                    aria2_bt._install_archive_payload(payload)
            self.assertEqual(target.read_bytes(), b"old aria2")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_aria_validation_reads_candidate_and_returns_installed_path(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.aria(Path(directory)) as (target, payload):
            target.write_bytes(b"old aria2")

            def check(candidate):
                self.assertNotEqual(candidate, target)
                self.assertEqual(candidate.suffix, ".exe")
                self.assertEqual(candidate.read_bytes(), b"new executable")
                self.assertEqual(target.read_bytes(), b"old aria2")
                return aria2_bt.Aria2Status(candidate, "aria2 version 1.37.0", "configured", "aria2 可用")

            with patch.object(aria2_bt, "aria2_status", side_effect=check):
                result = aria2_bt._install_archive_payload(payload)
            self.assertEqual(result.executable, target.resolve())
            self.assertEqual(result.version, "aria2 version 1.37.0")
            self.assertEqual(target.read_bytes(), b"new executable")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_aria_commit_failure_preserves_old_tool(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.aria(Path(directory)) as (target, payload):
            target.write_bytes(b"old aria2")
            with (
                patch.object(aria2_bt, "aria2_status", return_value=aria2_bt.Aria2Status(target)),
                patch.object(Path, "replace", side_effect=PermissionError("tool in use")),
            ):
                with self.assertRaises(PermissionError):
                    aria2_bt._install_archive_payload(payload)
            self.assertEqual(target.read_bytes(), b"old aria2")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_chrome_failed_first_install_leaves_no_executable(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            with patch.object(browser_driver, "chromedriver_version", return_value=""):
                with self.assertRaises(browser_driver.BrowserDriverError):
                    browser_driver.download_chromedriver()
            self.assertEqual(list(target.parent.iterdir()), [])

    def test_chrome_interrupted_validation_preserves_old_driver(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, self.chrome(Path(directory)) as target:
            target.write_bytes(b"old driver")
            with patch.object(browser_driver, "chromedriver_version", side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    browser_driver.download_chromedriver()
            self.assertEqual(target.read_bytes(), b"old driver")
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_aria_document_failure_does_not_replace_executable(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            target = root / "aria2c.exe"
            target.write_bytes(b"old aria2")
            (root / "COPYING").mkdir()  # Cannot replace a directory with a file.
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                archive.writestr("aria2c.exe", b"new executable")
                archive.writestr("COPYING", b"license")
            payload = buffer.getvalue()
            with (
                patch.object(aria2_bt, "bundled_aria2_path", return_value=target),
                patch.object(aria2_bt, "ARIA2_WINDOWS_X64_SHA256", hashlib.sha256(payload).hexdigest()),
                patch.object(aria2_bt, "aria2_status", return_value=aria2_bt.Aria2Status(target)),
            ):
                with self.assertRaises(OSError):
                    aria2_bt._install_archive_payload(payload)
            self.assertEqual(target.read_bytes(), b"old aria2")
            self.assertEqual({path.name for path in root.iterdir()}, {"aria2c.exe", "COPYING"})


if __name__ == "__main__":
    unittest.main()
