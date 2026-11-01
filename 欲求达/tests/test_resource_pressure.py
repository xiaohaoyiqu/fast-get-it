from __future__ import annotations

import sys
import threading
import unittest
from unittest.mock import Mock, patch

from software_app.core.adapter import TaskCancelled
from software_app.core.browser_pool import BrowserPool
from software_app.core.error_policy import classify_error
from software_app.core.resource_pressure import BrowserMemoryError, MemoryPolicy, MemoryPressure, system_memory_percent
from software_app.core.task_control import TaskControl


class ResourcePressureTests(unittest.TestCase):
    def test_disabled_guard_does_not_sample(self):
        sample = Mock(side_effect=AssertionError("disabled sampler"))
        self.assertFalse(MemoryPressure(sample=sample).check().blocked)
        sample.assert_not_called()

    def test_pressure_requires_low_threshold_and_samples_are_shared(self):
        now = [0.0]
        sample = Mock(side_effect=[90, 80, 75])
        guard = MemoryPressure(sample=sample, clock=lambda: now[0])
        guard.configure(MemoryPolicy(True))
        self.assertTrue(guard.check().blocked)
        self.assertTrue(guard.check().blocked)
        self.assertEqual(sample.call_count, 1)
        now[0] += 1
        self.assertTrue(guard.check().blocked)
        now[0] += 1
        self.assertFalse(guard.check().blocked)

    def test_cancelled_memory_wait_releases_ticket_without_taking_slot(self):
        pool, control = BrowserPool(1), threading.Event()
        pool._memory = MemoryPressure(sample=lambda: 99)
        pool.configure(1, memory_policy=MemoryPolicy(True))
        with self.assertRaises(TaskCancelled):
            pool.acquire(control, on_memory_wait=lambda *_args: control.set())
        self.assertEqual(pool.snapshot(), (0, 1, 0))

    def test_disabling_protection_releases_waiter_without_killing_active_session(self):
        pool = BrowserPool(2)
        held = pool.acquire()
        self.addCleanup(held.release)
        pool._memory = MemoryPressure(sample=lambda: 99)
        pool.configure(2, memory_policy=MemoryPolicy(True))

        def release_guard(*_args):
            self.assertEqual(pool.snapshot()[0], 1)
            pool.configure(2, memory_policy=MemoryPolicy(False))

        lease = pool.acquire(on_memory_wait=release_guard)
        self.assertEqual(pool.snapshot(), (2, 2, 0))
        lease.release()

    def test_timeout_is_resource_error_and_leaves_no_ticket(self):
        pool = BrowserPool(1)
        pool._memory = MemoryPressure(sample=lambda: 99)
        pool.configure(1, memory_policy=MemoryPolicy(True, timeout=10))
        now = [0.0]

        class Event(threading.Event):
            def wait(self, seconds=None):
                now[0] += 11
                return False

        with patch("software_app.core.browser_pool.time.monotonic", side_effect=lambda: now[0]):
            with self.assertRaises(BrowserMemoryError) as caught:
                pool.acquire(Event())
        self.assertEqual(classify_error(caught.exception).code, "browser_memory")
        self.assertFalse(classify_error(caught.exception).retryable)
        self.assertEqual(pool.snapshot(), (0, 1, 0))

    def test_sampler_failure_does_not_silently_admit(self):
        pool = BrowserPool()
        pool._memory = MemoryPressure(sample=lambda: float("nan"))
        pool.configure(1, memory_policy=MemoryPolicy(True))
        with self.assertRaises(BrowserMemoryError):
            pool.acquire()
        self.assertEqual(pool.snapshot(), (0, 1, 0))

    def test_time_paused_does_not_exhaust_memory_wait_budget(self):
        pool, paused = BrowserPool(1), threading.Event()
        now, errors, completed = [0.0], [], threading.Event()
        pool._memory = MemoryPressure(sample=lambda: 99)
        pool.configure(1, memory_policy=MemoryPolicy(True, timeout=10))

        class Control(TaskControl):
            def checkpoint(self):
                if self.paused:
                    paused.set()
                return super().checkpoint()

        control, notices = Control(), []

        def on_memory(*args):
            notices.append(args)
            if len(notices) == 1:
                control.pause()
            else:
                pool.configure(1, memory_policy=MemoryPolicy(False))

        def run():
            try:
                with pool.session(control, on_memory_wait=on_memory):
                    completed.set()
            except BaseException as exc:
                errors.append(exc)

        with patch("software_app.core.browser_pool.time.monotonic", side_effect=lambda: now[0]):
            worker = threading.Thread(target=run, daemon=True)
            worker.start()
            try:
                self.assertTrue(paused.wait(2))
                self.assertEqual(pool.snapshot(), (0, 1, 0))
                now[0] += 1000
                control.resume()
                self.assertTrue(completed.wait(2))
            finally:
                control.set()
                worker.join(2)
        self.assertEqual(errors, [])
        self.assertEqual(pool.snapshot(), (0, 1, 0))

    @unittest.skipUnless(sys.platform == "win32", "Windows memory API")
    def test_real_windows_memory_sample_is_a_finite_physical_ratio(self):
        sample = system_memory_percent()
        self.assertGreaterEqual(sample, 0)
        self.assertLessEqual(sample, 100)
