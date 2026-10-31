"""Ownership and lifecycle checks for the Windows browser process job."""

from __future__ import annotations

import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from software_app.core import browser_process as managed
from software_app.core.error_policy import classify_error
from software_app.crawlers.twitter import driver_init


class BrowserProcessTests(unittest.TestCase):
    def setUp(self):
        self.kernel = Mock()
        self.kernel.CreateJobObjectW.return_value = 123
        self.patch = patch.object(managed, "_kernel", return_value=self.kernel)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.kernel.QueryInformationJobObject.side_effect = self.accounting

    @staticmethod
    def accounting(_job, _kind, pointer, _size, _returned):
        pointer._obj.active = 0
        return True

    @staticmethod
    def process(code=None):
        return SimpleNamespace(_handle=987, poll=lambda: code)

    def service(self):
        service = managed.ManagedChromeService("generated-driver")
        service.process = self.process()

        def cleanup():
            service.process = None
            service.stop()

        self.addCleanup(cleanup)
        return service

    def test_job_uses_anonymous_noninherited_handle_and_kill_on_close(self):
        job = managed.WindowsBrowserJob()
        self.kernel.CreateJobObjectW.assert_called_once_with(None, None)
        limits = self.kernel.SetInformationJobObject.call_args.args[2]._obj
        self.assertEqual(limits.basic.flags, 0x2000)
        job.close()
        self.kernel.TerminateJobObject.assert_not_called()

    def test_attach_uses_retained_process_handle_without_opening_pid(self):
        job = managed.WindowsBrowserJob()
        job.attach(self.process())
        self.kernel.AssignProcessToJobObject.assert_called_once_with(123, 987)
        self.kernel.OpenProcess.assert_not_called()
        job.close()
        self.kernel.TerminateJobObject.assert_called_once_with(123, 1)

    def test_exited_or_missing_process_cannot_be_adopted(self):
        for process in (self.process(0), self.process(1), SimpleNamespace(poll=lambda: None)):
            with self.subTest(process=process):
                job = managed.WindowsBrowserJob()
                with self.assertRaises(managed.BrowserProcessError):
                    job.attach(process)
                job.close()
        self.kernel.AssignProcessToJobObject.assert_not_called()
        self.kernel.TerminateJobObject.assert_not_called()

    def test_close_is_idempotent_and_never_terminates_another_job(self):
        first = managed.WindowsBrowserJob()
        self.kernel.CreateJobObjectW.return_value = 456
        second = managed.WindowsBrowserJob()
        first.attach(self.process())
        second.attach(self.process())
        first.close()
        first.close()
        self.kernel.TerminateJobObject.assert_called_once_with(123, 1)
        self.assertEqual(second._handle, 456)
        second.close()
        self.assertEqual([call.args[0] for call in self.kernel.CloseHandle.call_args_list], [123, 456])

    def test_job_creation_failure_starts_no_service(self):
        self.kernel.CreateJobObjectW.return_value = None
        service = self.service()
        with patch.object(managed.ChromeService, "start") as start, patch.object(managed.ChromeService, "stop"):
            with self.assertRaises(managed.BrowserProcessError) as caught:
                service.start()
        start.assert_not_called()
        self.assertFalse(classify_error(caught.exception).retryable)
        self.kernel.AssignProcessToJobObject.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_limit_configuration_failure_closes_empty_handle(self):
        self.kernel.SetInformationJobObject.return_value = False
        with self.assertRaises(managed.BrowserProcessError):
            managed.WindowsBrowserJob()
        self.kernel.CloseHandle.assert_called_once_with(123)
        self.kernel.TerminateJobObject.assert_not_called()

    def test_service_attaches_after_driver_start_before_return(self):
        service = self.service()
        order = []
        self.kernel.AssignProcessToJobObject.side_effect = lambda *_args: order.append("attach") or True
        with (
            patch.object(managed.ChromeService, "start", side_effect=lambda: order.append("driver")),
            patch.object(managed.ChromeService, "stop"),
        ):
            service.start()
            self.assertEqual(order, ["driver", "attach"])
            service.stop()
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_failed_assignment_stops_driver_and_closes_only_empty_job(self):
        service = self.service()
        self.kernel.AssignProcessToJobObject.return_value = False
        with patch.object(managed.ChromeService, "start"), patch.object(managed.ChromeService, "stop") as stop:
            with self.assertRaises(managed.BrowserProcessError):
                service.start()
        stop.assert_called_once()
        self.kernel.CloseHandle.assert_called_once_with(123)
        self.kernel.TerminateJobObject.assert_not_called()

    def test_start_interruption_preserves_error_and_closes_job(self):
        service = self.service()
        error = KeyboardInterrupt()
        with (
            patch.object(managed.ChromeService, "start", side_effect=error),
            patch.object(managed.ChromeService, "stop"),
        ):
            with self.assertRaises(KeyboardInterrupt) as caught:
                service.start()
        self.assertIs(caught.exception, error)
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_shutdown_error_still_closes_own_job(self):
        service = self.service()
        with patch.object(managed.ChromeService, "start"), patch.object(managed.ChromeService, "stop") as stop:
            service.start()
            stop.side_effect = RuntimeError("shutdown failed")
            with self.assertRaises(RuntimeError):
                service.stop()
        self.kernel.TerminateJobObject.assert_called_once_with(123, 1)
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_query_or_termination_failure_still_closes_kernel_handle(self):
        for fail_at in ("TerminateJobObject", "QueryInformationJobObject"):
            with self.subTest(fail_at=fail_at):
                self.kernel.reset_mock()
                self.kernel.TerminateJobObject.return_value = True
                self.kernel.QueryInformationJobObject.side_effect = self.accounting
                job = managed.WindowsBrowserJob()
                job.attach(self.process())
                function = getattr(self.kernel, fail_at)
                function.side_effect = None
                function.return_value = False
                with self.assertRaises(managed.BrowserProcessError):
                    job.close()
                self.kernel.CloseHandle.assert_called_once_with(123)

    def test_process_exit_wait_has_finite_budget(self):
        job = managed.WindowsBrowserJob()
        job.attach(self.process())

        def active(_job, _kind, pointer, _size, _returned):
            pointer._obj.active = 1
            return True

        self.kernel.QueryInformationJobObject.side_effect = active
        with patch.object(managed.time, "monotonic", side_effect=[0, 4]):
            with self.assertRaises(managed.BrowserProcessError):
                job.close()
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_simultaneous_stop_closes_job_once(self):
        service = self.service()
        with patch.object(managed.ChromeService, "start"), patch.object(managed.ChromeService, "stop"):
            service.start()
            threads = [threading.Thread(target=service.stop) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(2)
                self.assertFalse(thread.is_alive())
        self.kernel.CloseHandle.assert_called_once_with(123)

    def test_non_windows_retains_normal_service_lifecycle(self):
        service = self.service()
        with (
            patch.object(managed.os, "name", "posix"),
            patch.object(managed.ChromeService, "start") as start,
            patch.object(managed.ChromeService, "stop") as stop,
        ):
            service.start()
            service.stop()
            start.assert_called_once()
            stop.assert_called_once()
        self.kernel.CreateJobObjectW.assert_not_called()


class TemporaryProfileCleanupTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.profile = self.root / "session_profiles/twitter_generated"
        self.profile.mkdir(parents=True)
        (self.profile / "data").write_bytes(b"generated")
        self.identity = driver_init._profile_identity(self.profile)
        patched = patch.object(driver_init, "BROWSER_TOOLS_DIR", self.root)
        patched.start()
        self.addCleanup(patched.stop)

    def test_transient_sharing_error_retries_only_owned_directory(self):
        original = driver_init.shutil.rmtree
        calls = []

        def remove(path):
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("files settling")
            original(path)

        with patch.object(driver_init.shutil, "rmtree", side_effect=remove), patch.object(driver_init.time, "sleep"):
            driver_init._cleanup_temporary_profile(self.profile, self.identity)
        self.assertEqual(calls, [self.profile, self.profile])
        self.assertFalse(self.profile.exists())

    def test_replaced_directory_is_preserved(self):
        moved = self.profile.with_name("twitter_previous")
        self.profile.rename(moved)
        self.profile.mkdir()
        (self.profile / "unrelated").write_bytes(b"keep")
        with self.assertRaises(managed.BrowserProcessError):
            driver_init._cleanup_temporary_profile(self.profile, self.identity)
        self.assertEqual((self.profile / "unrelated").read_bytes(), b"keep")
        self.assertTrue(moved.exists())

    def test_directory_outside_managed_root_is_preserved(self):
        with patch.object(driver_init, "BROWSER_TOOLS_DIR", self.root / "other"):
            with self.assertRaises(managed.BrowserProcessError):
                driver_init._cleanup_temporary_profile(self.profile, self.identity)
        self.assertTrue((self.profile / "data").exists())

    def test_reparse_directory_is_not_recursively_deleted(self):
        info = self.profile.lstat()
        fake = SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino, st_file_attributes=0x400)
        with patch.object(Path, "lstat", return_value=fake), patch.object(driver_init.shutil, "rmtree") as remove:
            with self.assertRaises(managed.BrowserProcessError):
                driver_init._cleanup_temporary_profile(self.profile, self.identity)
            remove.assert_not_called()
        self.assertTrue(self.profile.exists())

    def test_persistent_occupancy_has_finite_budget_and_preserves_remaining_files(self):
        with (
            patch.object(driver_init.shutil, "rmtree", side_effect=PermissionError("busy")),
            patch.object(driver_init.time, "monotonic", side_effect=[0, 3]),
        ):
            with self.assertRaises(managed.BrowserProcessError):
                driver_init._cleanup_temporary_profile(self.profile, self.identity)
        self.assertTrue((self.profile / "data").exists())

    def test_already_removed_profile_does_not_fail_second_quit(self):
        driver_init._cleanup_temporary_profile(self.profile, self.identity)
        driver_init._cleanup_temporary_profile(self.profile, self.identity)
        self.assertFalse(self.profile.exists())
