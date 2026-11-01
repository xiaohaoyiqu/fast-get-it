from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.core.adapter import TaskCancelled
from software_app.core.browser_pool import BrowserPool, browser_task_scope, browser_limit
from software_app.core.task_control import TaskControl


class BrowserPoolTests(unittest.TestCase):
    def setUp(self):
        self.pool = BrowserPool(1)
        self.errors = []
        self.order = []
        self.threads = []
        self.controls = []
        self.gates = []
        self.addCleanup(self.cleanup_workers)

    def cleanup_workers(self):
        for control in self.controls:
            control.set()
        for gate in self.gates:
            gate.set()
        for thread in self.threads:
            thread.join(3)

    def worker(self, index, control=None):
        control = control if control is not None else threading.Event()
        waiting, entered, release = threading.Event(), threading.Event(), threading.Event()
        self.controls.append(control)
        self.gates.append(release)

        def run():
            try:
                with self.pool.session(control, lambda *_args: waiting.set()):
                    self.order.append(index)
                    entered.set()
                    release.wait(3)
            except BaseException as exc:
                self.errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        self.threads.append(thread)
        thread.start()
        return waiting, entered, release, thread

    def test_waiting_sessions_are_admitted_in_arrival_order(self):
        held = self.pool.acquire()
        self.addCleanup(held.release)
        first_wait, first_entered, first_release, first = self.worker(1)
        self.assertTrue(first_wait.wait(2))
        second_wait, second_entered, second_release, second = self.worker(2)
        self.assertTrue(second_wait.wait(2))
        held.release()
        self.assertTrue(first_entered.wait(2))
        self.assertFalse(second_entered.is_set())
        first_release.set()
        self.assertTrue(second_entered.wait(2))
        second_release.set()
        first.join(2)
        second.join(2)
        self.assertEqual(self.order, [1, 2])
        self.assertEqual(self.errors, [])
        self.assertEqual(self.pool.snapshot(), (0, 1, 0))

    def test_cancelled_head_does_not_block_the_next_waiter(self):
        held = self.pool.acquire()
        self.addCleanup(held.release)
        control = threading.Event()
        first_wait, _entered, _release, first = self.worker(1, control)
        self.assertTrue(first_wait.wait(2))
        second_wait, entered, release, second = self.worker(2)
        self.assertTrue(second_wait.wait(2))
        control.set()
        first.join(2)
        held.release()
        self.assertTrue(entered.wait(2))
        release.set()
        second.join(2)
        self.assertEqual(self.order, [2])
        self.assertEqual(len(self.errors), 1)
        self.assertIsInstance(self.errors[0], TaskCancelled)

    def test_paused_head_rejoins_after_runnable_waiters(self):
        held = self.pool.acquire()
        self.addCleanup(held.release)
        control = TaskControl()
        first_wait, first_entered, first_release, first = self.worker(1, control)
        self.assertTrue(first_wait.wait(2))
        control.pause()
        deadline = time.monotonic() + 2
        while self.pool.snapshot()[2] and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(self.pool.snapshot()[2], 0)
        second_wait, second_entered, second_release, second = self.worker(2)
        self.assertTrue(second_wait.wait(2))
        held.release()
        self.assertTrue(second_entered.wait(2))
        self.assertFalse(first_entered.is_set())
        second_release.set()
        second.join(2)
        control.resume()
        self.assertTrue(first_entered.wait(2))
        first_release.set()
        first.join(2)
        self.assertEqual(self.order, [2, 1])
        self.assertEqual(self.errors, [])

    def test_reducing_limit_waits_for_existing_sessions_to_finish(self):
        self.pool.configure(2)
        first, second = self.pool.acquire(), self.pool.acquire()
        self.addCleanup(first.release)
        self.addCleanup(second.release)
        self.pool.configure(1)
        waiting, entered, release, thread = self.worker(3)
        self.assertTrue(waiting.wait(2))
        first.release()
        self.assertFalse(entered.wait(0.05))
        second.release()
        self.assertTrue(entered.wait(2))
        release.set()
        thread.join(2)
        self.assertEqual(self.errors, [])

    def test_startup_exception_and_double_release_do_not_leak_slots(self):
        with self.assertRaisesRegex(RuntimeError, "startup"):
            with self.pool.session():
                raise RuntimeError("startup")
        lease = self.pool.acquire()
        lease.release()
        lease.release()
        self.assertEqual(self.pool.snapshot(), (0, 1, 0))

    def test_context_callback_is_removed_after_failure(self):
        held = self.pool.acquire()
        self.addCleanup(held.release)

        def fail(*_args):
            raise ValueError("notice failed")

        with browser_task_scope(fail), self.assertRaises(ValueError):
            self.pool.acquire()
        self.assertEqual(self.pool.snapshot(), (1, 1, 0))
        held.release()
        with self.pool.session():
            self.assertEqual(self.pool.snapshot(), (1, 1, 0))

    def test_invalid_limits_cannot_disable_admission(self):
        for value in (0, -1, 11, True, "3.5", "nan"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                browser_limit(value)

    def test_dynamic_page_setup_failure_quits_driver_and_releases_slot(self):
        from software_app.crawlers.webpage.browser import DynamicPageBrowser

        driver = Mock()
        driver.set_page_load_timeout.side_effect = RuntimeError("setup failed")
        prefix = "software_app.crawlers.webpage.browser."
        with (
            patch(prefix + "BROWSER_POOL", self.pool),
            patch(prefix + "ensure_chromedriver", return_value=Path("chromedriver.exe")),
            patch(prefix + "webdriver.Chrome", return_value=driver),
        ):
            with self.assertRaisesRegex(RuntimeError, "setup failed"):
                DynamicPageBrowser().collect("sample", "https://example.com", threading.Event(), {})
        driver.quit.assert_called_once()
        self.assertEqual(self.pool.snapshot(), (0, 1, 0))

    def test_admission_notice_failure_releases_newly_acquired_slot(self):
        held = self.pool.acquire()
        self.addCleanup(held.release)

        def notice(_active, _limit, _waiting, admitted):
            if admitted:
                raise ValueError("admission notice failed")
            held.release()

        with self.assertRaisesRegex(ValueError, "admission notice failed"):
            self.pool.acquire(on_wait=notice)
        self.assertEqual(self.pool.snapshot(), (0, 1, 0))
