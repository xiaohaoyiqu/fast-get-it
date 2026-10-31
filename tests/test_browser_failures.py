"""Browser discovery failures must reach the task instead of reporting success."""

from __future__ import annotations

import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from selenium.common.exceptions import (
    InvalidSessionIdException,
    NoSuchWindowException,
    TimeoutException,
    WebDriverException,
)
from urllib3.exceptions import MaxRetryError, NewConnectionError

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.error_policy import classify_error
from software_app.core.browser_pool import BrowserPool
from software_app.core.browser_session import BrowserSessionLost, is_browser_session_lost
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.google_image.client import GoogleImageCrawler
from software_app.crawlers.webpage.browser import DynamicPageBrowser
from software_app.crawlers.twitter import manga_downloader as downloader
from software_app.crawlers.twitter import service


class BrowserFailureTests(unittest.TestCase):
    def scan(self, driver, stop=None):
        return downloader.download_media(
            driver,
            Path.cwd(),
            Path.cwd(),
            Path.cwd(),
            "1",
            True,
            download_workers=2,
            cancel_event=stop,
        )

    def test_twitter_closed_browser_is_not_successful_empty_download(self):
        driver = Mock()
        driver.execute_script.side_effect = InvalidSessionIdException("session gone")
        with patch.object(downloader.time, "sleep"):
            with self.assertRaises(Exception) as caught:
                self.scan(driver)
        self.assertEqual(classify_error(caught.exception).code, "browser_session_lost")
        self.assertFalse(classify_error(caught.exception).retryable)
        self.assertEqual(driver.execute_script.call_count, 1)
        self.assertIsInstance(caught.exception.__cause__, InvalidSessionIdException)

    def test_twitter_repeated_scan_failure_propagates_original_error(self):
        failure = ValueError("bad page response")
        driver = Mock()
        driver.execute_script.side_effect = failure
        with patch.object(downloader.time, "sleep"):
            with self.assertRaises(ValueError) as caught:
                self.scan(driver)
        self.assertIs(caught.exception, failure)
        self.assertEqual(driver.execute_script.call_count, 5)

    def test_twitter_transient_page_error_can_recover_normally(self):
        with (
            patch.object(
                downloader, "enqueue_images_from_cell", side_effect=[TimeoutException("loading"), (0, 0)]
            ) as cells,
            patch.object(downloader, "media_video", return_value=0),
            patch.object(downloader, "page_state", return_value={"height": 0, "cells": 0}),
            patch.object(downloader.time, "sleep"),
        ):
            result = downloader.download_media(
                Mock(),
                Path.cwd(),
                Path.cwd(),
                Path.cwd(),
                "1",
                True,
                download_workers=2,
                max_idle_rounds=1,
                stable_scroll_rounds=1,
            )
        self.assertIsInstance(result, dict)
        self.assertEqual(cells.call_count, 2)

    def test_task_manager_persists_failure_without_auto_reopening_browser(self):
        class Adapter(CrawlerAdapter):
            info = ModuleInfo("browser-test", "Browser test")
            attempts = 0

            def preview_target(self, raw_target, options=None):
                return TargetPreview(self.module_id, raw_target, raw_target)

            def download(self, task, callbacks, cancel_event):
                self.attempts += 1
                driver = Mock()
                driver.execute_script.side_effect = InvalidSessionIdException("gone")
                downloader.download_media(
                    driver,
                    task.output_dir,
                    task.output_dir,
                    task.output_dir,
                    "1",
                    True,
                    download_workers=2,
                    cancel_event=cancel_event,
                )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory, patch.object(downloader.time, "sleep"):
            root = Path(directory)
            storage = AppStorage(root / "app.db")
            manager, adapter = TaskManager(storage), Adapter()
            try:
                manager.register_adapter(adapter)
                task = manager.start_task(adapter.module_id, "generated", root / "out", {"retries": 3})
                manager.wait(task.task_id, 10)
                saved = storage.get_task(task.task_id)
                self.assertEqual(saved["status"], "failed")
                self.assertIn("浏览器会话中断", saved["error"])
                self.assertEqual(adapter.attempts, 1)
            finally:
                manager.shutdown()

    def test_twitter_fatal_response_body_error_is_not_skipped(self):
        driver = Mock()
        driver.get_log.return_value = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "requestId": "one",
                                "response": {
                                    "mimeType": "application/json",
                                    "url": "https://x.com/i/api/graphql/xxx/UserMedia",
                                },
                            },
                        }
                    }
                )
            }
        ]
        driver.execute_cdp_cmd.side_effect = InvalidSessionIdException("gone")
        with self.assertRaises(Exception) as caught:
            downloader.media_video(driver, queue.Queue(), "1", True, set(), downloader.DownloadStats(), {})
        self.assertEqual(classify_error(caught.exception).code, "browser_session_lost")

    def test_twitter_cancelled_scan_remains_cancelled(self):
        stop = threading.Event()
        driver = Mock()

        def close(*_args):
            stop.set()
            raise InvalidSessionIdException("gone")

        driver.execute_script.side_effect = close
        with patch.object(downloader.time, "sleep"):
            with self.assertRaises(TaskCancelled):
                self.scan(driver, stop)

    def test_fatal_protocol_errors_are_structured_and_not_automatically_retried(self):
        for failure in (
            InvalidSessionIdException("gone"),
            NoSuchWindowException("gone"),
            WebDriverException("disconnected: not connected to DevTools"),
            WebDriverException("unknown error: tab crashed"),
        ):
            with self.subTest(error=type(failure).__name__):
                self.assertTrue(is_browser_session_lost(failure))
        self.assertFalse(classify_error(BrowserSessionLost("closed")).retryable)

    def test_page_errors_and_arbitrary_message_text_are_not_session_loss(self):
        for failure in (
            TimeoutException("page load timeout"),
            ValueError("invalid session id"),
            WebDriverException("invalid argument: URL https://example.org/disconnected"),
            RuntimeError("no such window"),
        ):
            with self.subTest(error=str(failure)):
                self.assertFalse(is_browser_session_lost(failure))

    def test_driver_transport_failure_requires_evidence_of_owned_process_exit(self):
        failure = MaxRetryError(None, "/session/test", NewConnectionError(None, "connection refused"))
        driver = Mock()
        driver.service.process.poll.return_value = None
        self.assertFalse(is_browser_session_lost(failure, driver))
        driver.service.process.poll.return_value = 0
        self.assertTrue(is_browser_session_lost(failure, driver))
        self.assertFalse(is_browser_session_lost(ValueError("bad data"), driver))

    def test_wrapped_session_loss_retains_classification(self):
        failure = RuntimeError("wrapper")
        failure.__cause__ = InvalidSessionIdException("gone")
        self.assertTrue(is_browser_session_lost(failure))
        failure.__cause__ = failure
        self.assertFalse(is_browser_session_lost(failure))

    def test_dynamic_page_loss_quits_driver_and_releases_admission(self):
        driver, pool = Mock(), BrowserPool(1)
        driver.execute_script.side_effect = InvalidSessionIdException("gone")
        browser = DynamicPageBrowser()
        with (
            patch("software_app.crawlers.webpage.browser.BROWSER_POOL", pool),
            patch.object(browser, "_start_driver", return_value=driver),
            patch.object(browser, "_open_with_optional_cookies"),
        ):
            with self.assertRaises(BrowserSessionLost):
                browser.collect("task", "https://example.org", threading.Event(), {})
        driver.quit.assert_called_once()
        self.assertEqual(pool.snapshot(), (0, 1, 0))
        self.assertEqual(browser._drivers, {})

    def test_dynamic_media_prefetch_loss_is_not_silently_skipped(self):
        from software_app.crawlers.webpage.client import MediaCandidate

        driver = Mock()
        driver.execute_async_script.side_effect = InvalidSessionIdException("gone")
        with self.assertRaises(BrowserSessionLost):
            DynamicPageBrowser()._build_result(
                driver,
                {"exactStatus": True},
                "https://x.com/a/status/1",
                [MediaCandidate("https://pbs.twimg.com/media/a.jpg", "image")],
                {},
                threading.Event(),
            )

    def test_google_loss_quits_driver_and_releases_admission(self):
        crawler, driver, pool = GoogleImageCrawler(), Mock(), BrowserPool(1)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            source = Path(directory) / "sample.png"
            source.write_bytes(b"input")
            with (
                patch("software_app.crawlers.google_image.client.BROWSER_POOL", pool),
                patch.object(crawler, "_start_driver", return_value=driver),
                patch.object(crawler, "_upload", side_effect=InvalidSessionIdException("gone")),
            ):
                with self.assertRaises(BrowserSessionLost) as caught:
                    crawler.search(source)
            self.assertFalse(classify_error(caught.exception).retryable)
        driver.quit.assert_called_once()
        self.assertEqual(pool.snapshot(), (0, 1, 0))
        self.assertEqual(crawler._drivers, {})

    def test_google_close_during_cancellation_is_cancelled(self):
        crawler, driver, stop = GoogleImageCrawler(), Mock(), threading.Event()

        def close(*_args, **_kwargs):
            stop.set()
            raise InvalidSessionIdException("gone")

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            source = Path(directory) / "sample.png"
            source.write_bytes(b"input")
            with (
                patch.object(crawler, "_start_driver", return_value=driver),
                patch.object(crawler, "_upload", side_effect=close),
            ):
                with self.assertRaises(TaskCancelled):
                    crawler.search(source, cancel_event=stop)
        driver.quit.assert_called_once()

    def test_twitter_profile_loss_stops_media_and_releases_browser(self):
        driver, pool = Mock(), BrowserPool(1)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            crawler = service.TwitterCrawlerService(root / "runtime")
            with (
                patch.object(service, "BROWSER_POOL", pool),
                patch.object(service, "ensure_cookie_file"),
                patch.object(service, "initialize_authenticated_driver", return_value=driver),
                patch.object(
                    service, "collect_profile_preview_with_driver", side_effect=InvalidSessionIdException("gone")
                ),
                patch.object(service, "download_one_target") as transfer,
            ):
                with self.assertRaises(BrowserSessionLost):
                    crawler.download(
                        DownloadTask("task", "twitter", "@example", root / "out", {}), CallbackSet(), threading.Event()
                    )
                transfer.assert_not_called()
        driver.quit.assert_called_once()
        self.assertEqual(pool.snapshot(), (0, 1, 0))
        self.assertEqual(crawler._drivers, {})

    def test_twitter_worker_completion_precedes_scan_error_propagation(self):
        finished = threading.Event()
        failure = InvalidSessionIdException("gone")

        def produce(_driver, work, *_args):
            work.put(("generated", "image", "sample"))
            raise failure

        def worker(_index, work, *_args):
            while True:
                item = work.get()
                work.task_done()
                if item is None:
                    return
                finished.set()

        with (
            patch.object(downloader, "url_producer", side_effect=produce),
            patch.object(downloader, "download_worker", side_effect=worker),
            patch.object(downloader.time, "sleep"),
        ):
            with self.assertRaises(InvalidSessionIdException) as caught:
                self.scan(Mock())
        self.assertIs(caught.exception, failure)
        self.assertTrue(finished.is_set())

    def test_twitter_body_evicted_from_devtools_can_still_be_skipped(self):
        driver = Mock()
        driver.get_log.return_value = [
            {
                "message": json.dumps(
                    {
                        "message": {
                            "method": "Network.responseReceived",
                            "params": {
                                "requestId": "one",
                                "response": {
                                    "mimeType": "application/json",
                                    "url": "https://x.com/i/api/graphql/xxx/UserMedia",
                                },
                            },
                        }
                    }
                )
            }
        ]
        driver.execute_cdp_cmd.side_effect = WebDriverException("No resource with given identifier found")
        self.assertEqual(
            downloader.media_video(driver, queue.Queue(), "1", True, set(), downloader.DownloadStats(), {}), 0
        )


if __name__ == "__main__":
    unittest.main()
