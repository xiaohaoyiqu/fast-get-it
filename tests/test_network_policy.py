from __future__ import annotations

import errno
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.error_policy import classify_error
from software_app.core.events import CallbackSet
from software_app.core.models import ModuleInfo, TargetPreview
from software_app.core.network_policy import DomainBackoff, HTTP_BACKOFF, network_task_scope, retry_after_seconds
from software_app.core.storage import AppStorage
from software_app.core.task_control import TaskControl
from software_app.core.task_manager import TaskManager
from software_app.core import http_transfer, redirect_policy
from software_app.crawlers import common
from software_app.crawlers.bluesky import BlueskyClient
from software_app.crawlers.instagram import InstagramClient, parse_instagram_target
from software_app.crawlers.pixiv.external import _json_response
from software_app.crawlers.twitter import download_method as twitter


class Response:
    def __init__(self, status=200, headers=None, payload=None):
        self.status_code, self.headers = status, headers or {}
        self.closed = False
        self.payload = payload if payload is not None else {}

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)

    def iter_content(self, **_kwargs):
        yield b"file"

    def json(self):
        return self.payload


class Session:
    auth = None
    headers = {}
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def for_redirect(self):
        # This fake has no credentials; retain its shared response queue and trace.
        return self


class AdvancingEvent(threading.Event):
    def __init__(self, now):
        super().__init__()
        self.now = now
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self.now[0] += timeout
        return self.is_set()


class DomainPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = [0.0]
        self.broker = DomainBackoff(clock=lambda: self.now[0])
        self.url = "https://media.example/file?token=secret"

    def test_retry_after_parses_seconds_dates_and_invalid_values(self):
        now = datetime(2026, 10, 28, tzinfo=timezone.utc)
        future = format_datetime(now + timedelta(seconds=120), usegmt=True)
        self.assertEqual(retry_after_seconds(future, 1, now=now), 120)
        for value, expected in (("12", 12), ("-1", 0), ("999999", 999999), ("nan", 2), ("inf", 2), ("bad", 2)):
            self.assertEqual(retry_after_seconds(value, 2), expected)

    def test_long_retry_after_seconds_and_dates_are_not_shortened(self):
        now = datetime(2026, 10, 31, tzinfo=timezone.utc)
        self.assertEqual(retry_after_seconds("3600", 1), 3600)
        self.assertEqual(retry_after_seconds(format_datetime(now + timedelta(hours=2), usegmt=True), 1, now=now), 7200)

    def test_shared_deadline_survives_first_five_minutes(self):
        self.broker.defer(self.url, 3600)
        self.now[0] = 300
        self.assertEqual(self.broker.remaining(self.url), 3300)
        self.broker.defer(self.url, 5)
        self.assertEqual(self.broker.remaining(self.url), 3300)

    def test_long_wait_is_cancellable_in_short_slices(self):
        event = AdvancingEvent(self.now)
        self.broker.defer(self.url, 3600)

        def cancel_after_slice(timeout):
            event.waits.append(timeout)
            self.now[0] += timeout
            event.set()
            return True

        event.wait = cancel_after_slice
        with self.assertRaises(TaskCancelled):
            self.broker.wait(self.url, event)
        self.assertTrue(event.waits)
        self.assertLessEqual(max(event.waits), 0.1)
        self.assertGreater(self.broker.remaining(self.url), 3500)

    def test_unrepresentable_wait_is_rejected_instead_of_shortened(self):
        with self.assertRaisesRegex(ValueError, "等待范围"):
            retry_after_seconds(str(threading.TIMEOUT_MAX * 2), 1)
        with self.assertRaisesRegex(ValueError, "等待范围"):
            self.broker.defer(self.url, threading.TIMEOUT_MAX * 2)

    def test_long_response_deadline_is_recorded_for_entry_and_final_hosts(self):
        from software_app.core.network_policy import record_http_backoff

        for status in (429, 503):
            with self.subTest(status=status):
                self.now[0] = 0
                self.broker.clear()
                response = Response(status, {"Retry-After": "3600"})
                response.url = "https://cdn.example/file"
                with patch("software_app.core.network_policy.HTTP_BACKOFF", self.broker):
                    record_http_backoff(response, self.url)
                self.now[0] = 300
                self.assertEqual(self.broker.remaining(self.url), 3300)
                self.assertEqual(self.broker.remaining(response.url), 3300)

    def test_wait_notices_are_throttled_and_only_expose_host_and_port(self):
        notices = []
        event = AdvancingEvent(self.now)
        self.broker.defer(self.url, 12)
        with network_task_scope(event, lambda host, delay: notices.append((self.now[0], host, delay))):
            self.broker.wait(self.url)
        self.assertEqual(notices[0][1:], (("media.example", 443), 12))
        self.assertEqual(notices[-1][2], 0)
        self.assertGreaterEqual(len(notices), 3)
        for previous, current in zip(notices[:-2], notices[1:-1]):
            self.assertGreaterEqual(current[0] - previous[0], 5)
        self.assertNotIn("secret", repr(notices))
        before = len(notices)
        self.broker.defer(self.url, 1)
        with network_task_scope(event):
            self.broker.wait(self.url)
        self.assertEqual(len(notices), before)

    def test_wait_cancellation_does_not_emit_a_resumed_notice(self):
        event = AdvancingEvent(self.now)
        notices = []
        self.broker.defer(self.url, 10)

        def cancel(host, delay):
            notices.append(delay)
            event.set()

        with network_task_scope(event, cancel), self.assertRaises(TaskCancelled):
            self.broker.wait(self.url)
        self.assertEqual(notices, [10])


    def test_host_normalization_shares_paths_without_storing_credentials(self):
        self.broker.defer("https://user:password@MEDIA.EXAMPLE.:443/a?token=secret", 10)
        self.assertEqual(self.broker.remaining(self.url), 10)
        self.assertNotIn("secret", repr(self.broker._deadlines))
        self.assertNotIn("password", repr(self.broker._deadlines))

    def test_shorter_response_does_not_shorten_existing_deadline(self):
        self.broker.defer(self.url, 10)
        self.now[0] = 2
        self.broker.defer(self.url, 1)
        self.assertEqual(self.broker.remaining(self.url), 8)
        self.broker.defer(self.url, 20)
        self.assertEqual(self.broker.remaining(self.url), 20)
        self.now[0] = 23
        self.assertEqual(self.broker.remaining(self.url), 0)

    def test_ports_and_subdomains_are_independent(self):
        self.broker.defer(self.url, 10)
        for url in ("https://other.media.example/file", "https://media.example:8443/file", "http://media.example/file"):
            self.assertEqual(self.broker.remaining(url), 0)

    def test_cache_is_bounded_and_expired_entries_are_reclaimed(self):
        broker = DomainBackoff(clock=lambda: self.now[0], max_entries=2)
        for host in ("a", "b", "c"):
            broker.defer(f"https://{host}.example", 10)
        self.assertEqual(len(broker._deadlines), 2)
        self.assertEqual(broker.remaining("https://a.example"), 10)
        self.now[0] = 11
        broker.defer("https://c.example", 1)
        self.assertEqual(len(broker._deadlines), 1)

    def test_bad_urls_and_nonfinite_delays_are_ignored(self):
        for url in ("file:///local", "https://example.com:invalid", "", None):
            self.broker.defer(url, 10)
        for delay in (float("nan"), float("inf"), -1, "bad"):
            self.broker.defer(self.url, delay)
        self.assertFalse(self.broker._deadlines)

    def test_cancellation_does_not_clear_other_tasks_cooldown(self):
        self.broker.defer(self.url, 10)
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(TaskCancelled):
            self.broker.wait(self.url, cancel)
        self.assertEqual(self.broker.remaining(self.url), 10)

    def test_scope_supplies_cancellation_and_is_reset_after_exit(self):
        cancel = threading.Event()
        cancel.set()
        with network_task_scope(cancel):
            with self.assertRaises(TaskCancelled):
                self.broker.wait(self.url)
        self.broker.wait(self.url)

    def test_explicit_event_takes_precedence_over_worker_scope(self):
        cancelled, active = threading.Event(), threading.Event()
        cancelled.set()
        with network_task_scope(cancelled):
            self.broker.wait(self.url, active)

    def test_domain_wait_handles_deadline_extension_while_waiting(self):
        self.broker.defer(self.url, 0.2)
        event = AdvancingEvent(self.now)
        original = event.wait
        def wait(seconds):
            if not event.waits:
                self.broker.defer(self.url, 0.5)
            return original(seconds)
        with patch.object(event, "wait", side_effect=wait):
            self.broker.wait(self.url, event)
        self.assertGreaterEqual(self.now[0], 0.5)

    def test_paused_waiter_can_be_cancelled_without_blocking_other_host(self):
        control = TaskControl()
        control.pause()
        entered = threading.Event()
        def wait():
            entered.set()
            with network_task_scope(control):
                self.broker.wait(self.url)
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(wait)
            self.assertTrue(entered.wait(2))
            self.broker.wait("https://other.example", threading.Event())
            self.assertFalse(future.done())
            control.set()
            with self.assertRaises(TaskCancelled):
                future.result(2)


class NetworkIntegrationTests(unittest.TestCase):
    def setUp(self):
        validation = patch.object(common, "validate_external_url", side_effect=lambda url: url)
        validation.start()
        self.addCleanup(validation.stop)
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)
        self.now = [0.0]
        self.broker = DomainBackoff(clock=lambda: self.now[0])
        self.cancel = AdvancingEvent(self.now)
        self.url = "https://media.example/file"
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "file.mp4"

    def download(self, session, **kwargs):
        return http_transfer.download_http_file(session, self.url, self.path, self.cancel,
                                                timeout=(1, 1), chunk_size=3, validate_url=lambda _url: None,
                                                retries=0, **kwargs)

    def test_exhausted_http_still_defers_twitter_requests_to_same_host(self):
        response = Response(429, {"Retry-After": "12"})
        with patch.object(redirect_policy, "HTTP_BACKOFF", self.broker), patch.object(twitter, "HTTP_BACKOFF", self.broker):
            with self.assertRaises(requests.HTTPError):
                self.download(Session(response))
            self.assertTrue(response.closed)
            self.assertEqual(self.broker.remaining(self.url), 12)
            def get(*_args, **_kwargs):
                self.assertGreaterEqual(self.now[0], 12)
                return Response()
            with patch.object(twitter.requests, "get", side_effect=get):
                result = twitter.request_with_retries(self.url + "?other=1", cancel_event=self.cancel)
            self.assertEqual(result.status_code, 200)

    def test_redirected_host_is_cooled_and_checked_before_next_request(self):
        response = Response(503, {"Retry-After": "10"})
        redirect = Response(302, {"Location": "https://cdn.example/data"})
        with patch.object(redirect_policy, "HTTP_BACKOFF", self.broker):
            with self.assertRaises(requests.HTTPError):
                self.download(Session(redirect, response))
            self.assertEqual(self.broker.remaining(self.url), 10)
            self.assertEqual(self.broker.remaining("https://cdn.example/other"), 10)
            self.download(Session(Response(302, {"Location": "https://cdn.example/data"}), Response()))
        self.assertGreaterEqual(self.now[0], 10)

    def test_permanent_http_error_never_publishes_cooldown(self):
        with patch.object(redirect_policy, "HTTP_BACKOFF", self.broker):
            with self.assertRaises(requests.HTTPError):
                self.download(Session(Response(404)))
        self.assertEqual(self.broker.remaining(self.url), 0)

    def test_success_does_not_clear_concurrent_overload_deadline(self):
        session = Session()
        def get(*_args, **_kwargs):
            self.broker.defer(self.url, 10)
            return Response()
        session.get = get
        with patch.object(redirect_policy, "HTTP_BACKOFF", self.broker):
            self.download(session)
        self.assertEqual(self.broker.remaining(self.url), 10)

    def test_tls_failure_does_not_consume_retry_budget(self):
        session = Session(requests.exceptions.SSLError("bad certificate"), Response())
        with self.assertRaises(requests.exceptions.SSLError):
            http_transfer.download_http_file(session, self.url, self.path, self.cancel, timeout=(1, 1), chunk_size=3,
                                              validate_url=lambda _url: None, retries=3)
        self.assertEqual(len(session.calls), 1)

    def test_metadata_request_closes_error_response_and_defers_host(self):
        response = Response(429, {"Retry-After": "7"})
        with patch.object(common, "HTTP_BACKOFF", self.broker), patch.object(common, "validate_external_url"):
            with self.assertRaises(requests.HTTPError):
                common.request_json(Session(response), self.url)
        self.assertTrue(response.closed)
        self.assertEqual(self.broker.remaining(self.url), 7)

    def test_metadata_request_obeys_task_scope_cancellation(self):
        event = threading.Event()
        event.set()
        session = Session(Response())
        with network_task_scope(event), patch.object(common, "HTTP_BACKOFF", self.broker), patch.object(common, "validate_external_url"):
            with self.assertRaises(TaskCancelled):
                common.request_json(session, self.url)
        self.assertFalse(session.calls)

    def test_jm_wrapped_http_error_keeps_structured_classification(self):
        from software_app.crawlers.jmcomic import client as jm
        crawler = object.__new__(jm.JmComicCrawler)
        response = Response(429, {"Retry-After": "5"})
        crawler.session = Session(response)
        with patch.object(jm, "HTTP_BACKOFF", self.broker):
            with self.assertRaises(RuntimeError) as error:
                crawler._get_response(self.url, (1, 1))
        self.assertEqual(classify_error(error.exception).code, "rate_limited")
        self.assertEqual(self.broker.remaining(self.url), 5)
        self.assertTrue(response.closed)

    def test_webpage_request_defers_host_before_returning_error(self):
        from software_app.crawlers.webpage import client as webpage
        response = Response(429, {"Retry-After": "9"})
        with patch.object(webpage, "HTTP_BACKOFF", self.broker), patch.object(
                webpage, "_validate_public_destination", side_effect=lambda url: url):
            result = webpage._request_public_url(Session(response), self.url, timeout=(1, 1), cancel_event=self.cancel)
        self.assertIs(result, response)
        self.assertEqual(self.broker.remaining(self.url), 9)

    def test_twitter_reports_structured_error_without_retrying_denial(self):
        reporter = Mock()
        config = twitter.configure_downloads(max_retries=3, on_error=reporter)
        with patch.object(twitter.requests, "get", return_value=Response(403)) as get:
            self.assertIsNone(twitter.request_with_retries(self.url, network_config=config))
        get.assert_called_once()
        self.assertEqual(reporter.call_args.args[0].code, "access_denied")


class ErrorClassificationTests(unittest.TestCase):
    def test_http_statuses_and_filesystem_errors_have_explicit_retry_decisions(self):
        for status, code, retryable in ((401, "access_denied", False), (403, "access_denied", False),
                                       (404, "not_found", False), (410, "not_found", False),
                                       (429, "rate_limited", True), (503, "server_temporary", True)):
            info = classify_error(requests.HTTPError(response=Response(status)))
            self.assertEqual((info.code, info.retryable), (code, retryable))
        cases = [(OSError(errno.ENOSPC, "full"), "disk_full"),
                 (PermissionError(errno.EACCES, "denied"), "file_permission"),
                 (FileExistsError("exists"), "file_conflict"),
                 (ValueError("invalid"), "invalid_data")]
        for error, code in cases:
            info = classify_error(error)
            self.assertEqual(info.code, code)
            self.assertFalse(info.retryable)

    def test_timeout_proxy_tls_and_unknown_remain_distinct(self):
        for error, code, retryable in ((requests.Timeout(), "timeout", True),
                                       (requests.exceptions.ProxyError(), "proxy_error", True),
                                       (requests.exceptions.SSLError(), "tls_error", False),
                                       (RuntimeError("plugin"), "unknown", True)):
            info = classify_error(error)
            self.assertEqual((info.code, info.retryable), (code, retryable))

    def test_wrapper_cause_preserves_original_http_status(self):
        error = RuntimeError("platform wrapper")
        error.__cause__ = requests.HTTPError(response=Response(404))
        self.assertEqual(classify_error(error).code, "not_found")

    def test_cycle_in_exception_chain_is_bounded(self):
        first, second = RuntimeError("one"), RuntimeError("two")
        first.__cause__, second.__cause__ = second, first
        self.assertEqual(classify_error(first).code, "unknown")


class RetryAdapter(CrawlerAdapter):
    info = ModuleInfo("retry-policy", "Retry policy")
    def __init__(self, error):
        self.error, self.calls = error, 0
    def preview_target(self, raw_target, options=None):
        return TargetPreview(self.module_id, raw_target, raw_target)
    def download(self, task, callbacks, cancel_event):
        self.calls += 1
        if self.calls == 1:
            raise self.error


class TaskRetryPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "app.sqlite3")
        self.manager = TaskManager(self.storage)
        self.addCleanup(self.manager.shutdown)

    def run_task(self, error):
        adapter = RetryAdapter(error)
        self.manager.register_adapter(adapter)
        events = []
        with patch("software_app.core.task_manager.task_wait", return_value=False):
            task = self.manager.start_task(adapter.module_id, "target", self.root, {"retries": 3},
                                           CallbackSet(on_progress=events.append))
            self.manager.wait(task.task_id, 3)
        return adapter, self.storage.get_task(task.task_id), events

    def test_missing_resource_is_not_retried_by_outer_task(self):
        adapter, row, events = self.run_task(requests.HTTPError("missing", response=Response(404)))
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(row["status"], "failed")
        self.assertIn("资源不存在", row["error"])
        self.assertTrue(any(event.metadata.get("error_code") == "not_found" for event in events))

    def test_disk_full_is_not_retried_by_outer_task(self):
        adapter, row, _events = self.run_task(OSError(errno.ENOSPC, "full"))
        self.assertEqual(adapter.calls, 1)
        self.assertIn("磁盘空间", row["error"])

    def test_timeout_keeps_finite_retry_and_error_metadata(self):
        adapter, row, events = self.run_task(requests.Timeout("temporary"))
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(row["status"], "completed")
        self.assertTrue(any(event.metadata.get("error_code") == "timeout" for event in events))

    def test_worker_cancellation_scope_reaches_metadata_helpers(self):
        entered = threading.Event()
        broker = DomainBackoff()
        url = "https://metadata.example/api"
        broker.defer(url, 120)
        session = Session(Response())
        class Adapter(RetryAdapter):
            def preview_target(self, raw_target, options=None):
                entered.set()
                common.request_json(session, url)
                return super().preview_target(raw_target, options)
        adapter = Adapter(RuntimeError())
        self.manager.register_adapter(adapter)
        with patch.object(redirect_policy, "HTTP_BACKOFF", broker), patch.object(common, "validate_external_url"):
            task = self.manager.start_task(adapter.module_id, "target", self.root)
            self.assertTrue(entered.wait(2))
            self.manager.cancel(task.task_id)
            self.manager.wait(task.task_id, 3)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertFalse(session.calls)


class PrivateRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        validation = patch.object(common, "validate_external_url", side_effect=lambda url: url)
        validation.start()
        self.addCleanup(validation.stop)
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)

    def calls(self, session):
        return (
            lambda: BlueskyClient(session=session).get("app.bsky.actor.getProfile", actor="example.test"),
            lambda: InstagramClient(session=session).fetch(parse_instagram_target("https://www.instagram.com/p/ABC/")),
            lambda: _json_response(session, "https://api.fanbox.cc/post.info?postId=1"),
        )

    def test_http_status_is_preserved_and_response_closed(self):
        for index in range(3):
            for status in (429, 503, 404):
                with self.subTest(client=index, status=status):
                    HTTP_BACKOFF.clear()
                    response = Mock(status_code=status, headers={"Retry-After": "7"}, url="https://final.example/api")
                    response.raise_for_status.side_effect = requests.HTTPError("request failed", response=response)
                    session = Mock()
                    session.get.return_value = response
                    with self.assertRaises(requests.HTTPError) as caught:
                        self.calls(session)[index]()
                    response.close.assert_called_once()
                    self.assertEqual(classify_error(caught.exception).code,
                                     {429: "rate_limited", 503: "server_temporary", 404: "not_found"}[status])
                    original = session.get.call_args.args[0]
                    if status in {429, 503}:
                        self.assertGreater(HTTP_BACKOFF.remaining(original), 0)
                        self.assertGreater(HTTP_BACKOFF.remaining(response.url), 0)
                    else:
                        self.assertEqual(HTTP_BACKOFF.remaining(original), 0)

    def test_cancelled_metadata_requests_do_not_reach_session(self):
        event = threading.Event()
        event.set()
        for index in range(3):
            with self.subTest(client=index):
                session = Mock()
                with network_task_scope(event), self.assertRaises(TaskCancelled):
                    self.calls(session)[index]()
                session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
