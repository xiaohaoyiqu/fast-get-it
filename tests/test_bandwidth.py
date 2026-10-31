from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

from software_app.core.adapter import TaskCancelled
from software_app.core.bandwidth import (
    BandwidthLimiter, MEDIA_BANDWIDTH, apply_bandwidth_settings, limited_chunks,
    saved_speed_limit, validate_speed_limit,
)
from software_app.core.task_control import TaskControl


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, delay):
        self.value += delay


class BandwidthTests(unittest.TestCase):
    def tearDown(self):
        MEDIA_BANDWIDTH.configure(False, 1024 * 1024)

    def wait_for_waiter(self, limiter):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with limiter._condition:
                if limiter._waiting:
                    return
            time.sleep(0.005)
        self.fail("consumer did not enter the limiter")

    def test_budget_accumulates_across_calls_instead_of_resetting_for_each_file(self):
        clock = Clock()
        limiter = BandwidthLimiter(clock=clock)
        limiter.configure(True, 1000)
        with patch.object(limiter._condition, "wait", side_effect=clock.advance):
            limiter.consume(1000)
            limiter.consume(1000)
        self.assertGreaterEqual(clock.value, 2.0)
        self.assertLess(clock.value, 2.05)

    def test_long_idle_time_cannot_accumulate_an_unbounded_burst(self):
        clock = Clock()
        limiter = BandwidthLimiter(clock=clock)
        limiter.configure(True, 1000)
        clock.advance(100)
        with patch.object(limiter._condition, "wait", side_effect=clock.advance):
            limiter.consume(1000)
        self.assertGreaterEqual(clock.value, 100.9)
        self.assertLess(clock.value, 101.05)

    def test_disable_and_reenable_preserve_the_rate(self):
        limiter = BandwidthLimiter()
        limiter.configure(True, 4096)
        limiter.configure(False)
        limiter.consume(10**9)
        self.assertEqual(limiter.settings, (False, 4096))
        limiter.configure(True)
        self.assertEqual(limiter.settings, (True, 4096))

    def test_two_concurrent_consumers_share_one_total_budget(self):
        limiter = BandwidthLimiter()
        limiter.configure(True, 10000)
        start = threading.Barrier(2)

        def work():
            start.wait(2)
            limiter.consume(2000)

        started = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [pool.submit(work) for _ in range(2)]
            for job in jobs:
                job.result(3)
        self.assertGreaterEqual(time.monotonic() - started, 0.35)
        self.assertFalse(limiter._waiting)

    def test_cancellation_interrupts_a_slow_wait_and_removes_its_ticket(self):
        limiter = BandwidthLimiter()
        limiter.configure(True, 1)
        stop = threading.Event()
        errors = []

        def work():
            try:
                limiter.consume(10000, stop)
            except TaskCancelled:
                errors.append("cancelled")

        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.wait_for_waiter(limiter)
            stop.set()
            thread.join(0.5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, ["cancelled"])
            self.assertFalse(limiter._waiting)
        finally:
            stop.set()
            limiter.configure(False)
            thread.join(2)

    def test_disabling_or_increasing_rate_wakes_existing_consumers(self):
        for enabled, new_rate in ((False, None), (True, 10000000)):
            with self.subTest(enabled=enabled):
                limiter = BandwidthLimiter()
                limiter.configure(True, 1)
                thread = threading.Thread(target=limiter.consume, args=(1000,))
                thread.start()
                try:
                    self.wait_for_waiter(limiter)
                    limiter.configure(enabled, new_rate)
                    thread.join(0.5)
                    self.assertFalse(thread.is_alive())
                finally:
                    limiter.configure(False)
                    thread.join(2)

    def test_paused_waiter_does_not_hold_up_other_tasks(self):
        limiter = BandwidthLimiter()
        limiter.configure(True, 10000)
        control = TaskControl()
        errors = []

        def work():
            try:
                limiter.consume(1000000, control)
            except TaskCancelled:
                errors.append("cancelled")

        thread = threading.Thread(target=work)
        thread.start()
        try:
            self.wait_for_waiter(limiter)
            control.pause()
            started = time.monotonic()
            limiter.consume(100)
            self.assertLess(time.monotonic() - started, 0.5)
            self.assertTrue(thread.is_alive())
            control.set()
            thread.join(1)
            self.assertEqual(errors, ["cancelled"])
        finally:
            control.set()
            limiter.configure(False)
            thread.join(2)

    def test_settings_validation_and_corrupt_saved_values(self):
        self.assertEqual(validate_speed_limit(False, "4096"), (False, 4096))
        for value in ("0", "-1", "1.5", "nan", "inf", "1048577", True, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_speed_limit(True, value)
        self.assertEqual(saved_speed_limit({"speed_limit_enabled": True, "speed_limit_kib": "broken"}), (False, 1024))
        apply_bandwidth_settings({"speed_limit_enabled": True, "speed_limit_kib": 2})
        self.assertEqual(MEDIA_BANDWIDTH.settings, (True, 2048))

    def test_limited_chunk_stream_counts_every_nonempty_byte(self):
        response = SimpleNamespace(iter_content=lambda **_kwargs: iter([b"ab", b"", b"cde"]))
        with patch.object(MEDIA_BANDWIDTH, "consume") as consume:
            self.assertEqual(list(limited_chunks(response)), [b"ab", b"", b"cde"])
        self.assertEqual([call.args[0] for call in consume.call_args_list], [2, 3])
