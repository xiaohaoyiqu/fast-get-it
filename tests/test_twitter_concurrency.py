from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.adapters import twitter as twitter_adapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask
from software_app.crawlers.twitter import download_method as network
from software_app.crawlers.twitter import service
from software_app.crawlers.twitter.download_method import DownloadFailureRecord, DownloadRecord
from software_app.core.network_policy import HTTP_BACKOFF, DomainBackoff


class TwitterConcurrencyTests(unittest.TestCase):
    def setUp(self):
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)

    def test_success_records_from_separate_tasks_are_merged(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "success.json"
            first, second = DownloadRecord(path), DownloadRecord(path)
            first.mark_success("one", "image", "one.png")
            second.mark_success("two", "image", "two.png")
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["items"]), {"one", "two"})
            self.assertTrue(first.has("two"))

    def test_failure_counts_and_removals_are_shared_between_tasks(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "failed.json"
            first, second = DownloadFailureRecord(path), DownloadFailureRecord(path)
            first.mark_failed("one", "video", "temporary error")
            second.mark_failed("one", "video", "temporary error")
            data = json.loads(path.read_text(encoding="utf-8"))["items"]
            self.assertEqual(data["one"]["attempts"], 2)
            first.remove("one")
            second.mark_failed("two", "image", "another error")
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["items"]), {"two"})

    def test_permanent_http_error_is_not_retried(self):
        response = Mock(status_code=404, headers={})
        with (
            patch.object(network, "MAX_RETRIES", 3),
            patch.object(network.requests, "get", return_value=response) as request,
            patch.object(network.time, "sleep") as pause,
        ):
            self.assertIsNone(network.request_with_retries("https://pbs.twimg.com/media/missing.jpg"))
        request.assert_called_once()
        pause.assert_not_called()

    def test_record_save_failure_keeps_previous_json(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "success.json"
            record = DownloadRecord(path)
            record.mark_success("one", "image", "one.png")
            with patch.object(network, "replace_with_retry", side_effect=PermissionError("locked")):
                with self.assertRaises(PermissionError):
                    record.mark_success("two", "image", "two.png")
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["items"]), {"one"})
            self.assertFalse(record.has("two"))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_threads_writing_same_file_keep_all_records(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "success.json"
            records = [DownloadRecord(path) for _ in range(4)]

            def save(index):
                records[index % 4].mark_success(str(index), "image", f"{index}.png")

            with ThreadPoolExecutor(max_workers=4) as pool:
                list(pool.map(save, range(40)))
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))["items"]), {str(i) for i in range(40)})

    def test_record_cache_detects_external_replacement_and_deletion(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "success.json"
            record = DownloadRecord(path)
            record.mark_success("one", "image", "one.png")
            replacement = path.with_suffix(".new")
            replacement.write_text(json.dumps({"items": {"other": {"file": "other.png"}}}), encoding="utf-8")
            replacement.replace(path)
            self.assertTrue(record.has("other"))
            self.assertFalse(record.has("one"))
            path.unlink()
            self.assertFalse(record.has("other"))

    def test_rate_limit_uses_retry_after_and_cancel_interrupts_wait(self):
        responses = [Mock(status_code=429, headers={"Retry-After": "120"}), Mock(status_code=200)]
        config = network.configure_downloads(max_retries=2, retry_backoff_max=15)
        cancel = Mock()
        cancel.is_set.return_value = False
        now = [0.0]
        coordinator = DomainBackoff(clock=lambda: now[0])
        def advance(seconds):
            now[0] += seconds
            return False
        cancel.wait.side_effect = advance
        with patch.object(network, "HTTP_BACKOFF", coordinator), patch.object(network.requests, "get", side_effect=responses) as request:
            self.assertIs(
                network.request_with_retries("https://cdn.example/video", cancel_event=cancel, network_config=config),
                responses[1],
            )
        self.assertEqual(request.call_count, 2)
        cancel.wait.assert_called_once_with(120)
        cancel.wait.side_effect = lambda _seconds: True
        with patch.object(network.requests, "get", return_value=responses[0]) as request:
            self.assertIsNone(
                network.request_with_retries("https://cdn.example/video", cancel_event=cancel, network_config=config)
            )
        request.assert_called_once()

    def test_network_snapshot_does_not_modify_global_defaults(self):
        original = (network.MAX_RETRIES, network.REQUEST_TIMEOUT, network.REQUEST_PROXIES)
        config = network.configure_downloads(max_retries=2, connect_timeout=7, proxy_url="http://127.0.0.1:7899")
        self.assertEqual((network.MAX_RETRIES, network.REQUEST_TIMEOUT, network.REQUEST_PROXIES), original)
        self.assertEqual(config.max_retries, 2)

    def test_retry_after_accepts_http_date_and_ignores_nonfinite_values(self):
        future = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
        self.assertTrue(118 <= network.retry_delay(1, future) <= 121)
        for invalid in ("nan", "inf", "not-a-date"):
            self.assertTrue(1 <= network.retry_delay(1, invalid) <= 1.25)

    def test_service_keeps_network_snapshot_for_browser_and_direct_downloads(self):
        with (
            tempfile.TemporaryDirectory(dir=Path.cwd()) as directory,
            patch.object(service, "ensure_cookie_file"),
            patch.object(service, "initialize_authenticated_driver", return_value=Mock()),
            patch.object(service, "collect_profile_preview_with_driver"),
            patch.object(service, "download_one_target", return_value={}) as browser,
            patch.object(service, "download_status_from_syndication", return_value={"success_image": 1}) as direct,
        ):
            root = Path(directory)
            crawler = service.TwitterCrawlerService(root / "runtime")
            crawler.download(
                DownloadTask("browser", "twitter", "@example", root / "output", {"proxy_url": "http://127.0.0.1:7890"}),
                CallbackSet(),
                threading.Event(),
            )
            first = browser.call_args.kwargs["network_config"]
            crawler.download(
                DownloadTask(
                    "direct",
                    "twitter",
                    "https://x.com/example/status/123",
                    root / "output",
                    {"proxy_url": "http://127.0.0.1:7891"},
                ),
                CallbackSet(),
                threading.Event(),
            )
            second = direct.call_args.kwargs["network_config"]
            self.assertEqual(dict(first.proxies)["https"], "http://127.0.0.1:7890")
            self.assertEqual(dict(second.proxies)["https"], "http://127.0.0.1:7891")

    def test_avatar_request_uses_explicit_proxy_override(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            adapter = twitter_adapter.TwitterNativeAdapter(Path(directory) / "crawler")
            adapter.runtime_data_dir = Path(directory) / "runtime"
            response = Mock(headers={"Content-Type": "image/jpeg"}, content=b"image")
            with patch.object(twitter_adapter, "request_with_retries", return_value=response) as request:
                adapter.cache_profile_avatar(
                    "example", "https://pbs.twimg.com/profile_images/1/avatar.jpg", proxy_url="http://127.0.0.1:7890"
                )
            config = request.call_args.kwargs["network_config"]
            self.assertEqual(dict(config.proxies)["https"], "http://127.0.0.1:7890")

    def test_following_adapter_passes_proxy_to_browser_collector(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            adapter = twitter_adapter.TwitterNativeAdapter(Path(directory) / "crawler")
            adapter.runtime_data_dir = Path(directory)
            (Path(directory) / "X_cookie.json").write_text("[]", encoding="utf-8")
            with (
                patch.object(adapter, "_ensure_runtime_files"),
                patch.object(twitter_adapter, "collect_following") as collect,
            ):
                adapter.fetch_following(proxy_url="http://127.0.0.1:7890")
            self.assertEqual(collect.call_args.kwargs["proxy_url"], "http://127.0.0.1:7890")

    def test_deleted_media_is_not_skipped_just_because_history_contains_it(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "media.png"
            path.write_bytes(b"image")
            record = DownloadRecord(Path(directory) / "success.json")
            record.mark_success("one", "image", path)
            stats = network.DownloadStats()
            self.assertTrue(network.record_skip("one", record, stats))
            path.unlink()
            self.assertTrue(record.has("one"))
            self.assertFalse(network.record_skip("one", record, stats))

    def test_parallel_requests_keep_their_own_proxy_and_timeout(self):
        barrier = threading.Barrier(2)
        seen = {}

        def request(_url, **kwargs):
            seen[threading.current_thread().name] = kwargs
            return Mock(status_code=200)

        def task(port):
            config = network.configure_downloads(
                proxy_url=f"http://127.0.0.1:{port}",
                connect_timeout=port - 7800,
                read_timeout=port - 7700,
                max_retries=1,
            )
            barrier.wait(timeout=3)
            network.request_with_retries("https://pbs.twimg.com/media/example.jpg", network_config=config)
            return threading.current_thread().name

        with patch.object(network.requests, "get", side_effect=request), ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(task, 7890), pool.submit(task, 7891)
            names = [first.result(timeout=5), second.result(timeout=5)]
        self.assertEqual(seen[names[0]]["proxies"]["https"], "http://127.0.0.1:7890")
        self.assertEqual(seen[names[1]]["proxies"]["https"], "http://127.0.0.1:7891")
        self.assertEqual(seen[names[0]]["timeout"], (90, 190))
        self.assertEqual(seen[names[1]]["timeout"], (91, 191))


if __name__ == "__main__":
    unittest.main()
