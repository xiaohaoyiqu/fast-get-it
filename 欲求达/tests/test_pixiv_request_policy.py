import json
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.error_policy import classify_error
from software_app.core.network_policy import HTTP_BACKOFF, network_task_scope
from software_app.crawlers.pixiv.client import PixivCrawler


class PixivRequestPolicyTests(unittest.TestCase):
    def setUp(self):
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)
        # All requests use mocks; URL validation must not resolve real DNS.
        validation = patch("software_app.crawlers.common.validate_external_url", side_effect=lambda url: url)
        validation.start()
        self.addCleanup(validation.stop)
        self.crawler = object.__new__(PixivCrawler)
        self.crawler.session = Mock()

    def requests(self):
        return (
            lambda: self.crawler._json("https://www.pixiv.net/ranking.php"),
            lambda: self.crawler._text("https://www.pixiv.net/users/123", timeout=(5, 20)),
            lambda: self.crawler.account_history(),
            lambda: self.crawler.fetch_following_html_page("123"),
        )

    def response(self, status=200):
        response = Mock(status_code=status, headers={"Retry-After": "7"}, url="https://final.example/page")
        response.json.return_value = {"body": {"name": "Artist"}}
        response.text = json.dumps(response.json.return_value)
        response.encoding = "utf-8"
        response.iter_content.side_effect = lambda **_kwargs: iter([response.text.encode("utf-8")])
        if status >= 400:
            response.raise_for_status.side_effect = requests.HTTPError("request failed", response=response)
        self.crawler.session.get.return_value = response
        return response

    def test_http_errors_keep_status_close_response_and_publish_backoff(self):
        for call in self.requests():
            for status in (429, 503, 404):
                with self.subTest(call=call, status=status):
                    HTTP_BACKOFF.clear()
                    response = self.response(status)
                    with self.assertRaises(requests.HTTPError) as caught:
                        call()
                    response.close.assert_called_once()
                    self.assertIs(caught.exception.response, response)
                    self.assertEqual(
                        classify_error(caught.exception).code,
                        {429: "rate_limited", 503: "server_temporary", 404: "not_found"}[status],
                    )
                    original_url = self.crawler.session.get.call_args.args[0]
                    self.assertEqual(HTTP_BACKOFF.remaining(original_url) > 0, status in (429, 503))
                    self.assertEqual(HTTP_BACKOFF.remaining(response.url) > 0, status in (429, 503))

    def test_cancellation_prevents_requests_at_each_entry(self):
        event = threading.Event()
        event.set()
        calls = (
            *self.requests(),
            lambda: self.crawler._user_profile("123"),
            lambda: self.crawler.fetch_following_page("123"),
        )
        for call in calls:
            with self.subTest(call=call), network_task_scope(event), self.assertRaises(TaskCancelled):
                call()
        self.crawler.session.get.assert_not_called()

    def test_profile_api_cancellation_does_not_start_html_fallback(self):
        error = TaskCancelled("cancelled at API")
        self.crawler._body = Mock(side_effect=error)
        with self.assertRaises(TaskCancelled) as caught:
            self.crawler._user_profile("123")
        self.assertIs(caught.exception, error)
        self.crawler.session.get.assert_not_called()

    def test_following_api_cancellation_does_not_start_html_fallback(self):
        self.crawler.fetch_following_html_page = Mock()
        error = TaskCancelled("cancelled at API")
        with patch("software_app.crawlers.pixiv.client.request_json", side_effect=error):
            for visibility in ("show", "hide"):
                with self.subTest(visibility=visibility), self.assertRaises(TaskCancelled) as caught:
                    self.crawler.fetch_following_page("123", visibility=visibility)
                self.assertIs(caught.exception, error)
        self.crawler.fetch_following_html_page.assert_not_called()

    def test_cancellation_during_fallback_is_not_wrapped_or_suppressed(self):
        error = TaskCancelled("cancelled at HTML")
        self.crawler._body = Mock(return_value={"name": "Artist"})
        self.crawler._text = Mock(side_effect=error)
        with self.assertRaises(TaskCancelled) as caught:
            self.crawler._user_profile("123")
        self.assertIs(caught.exception, error)
        with patch("software_app.crawlers.pixiv.client.request_json", side_effect=ValueError("API unavailable")):
            with self.assertRaises(TaskCancelled) as caught:
                self.crawler.fetch_following_page("123")
        self.assertIs(caught.exception, error)

    def test_json_parse_failure_and_success_both_close_response(self):
        response = self.response()
        self.assertEqual(self.requests()[0](), {"body": {"name": "Artist"}})
        response.close.assert_called_once()
        response = self.response()
        response.text = "invalid JSON"
        with self.assertRaises(json.JSONDecodeError):
            self.requests()[0]()
        response.close.assert_called_once()

    def test_html_is_closed_before_parsing_and_login_redirect_detection(self):
        response = self.response()
        with patch("software_app.crawlers.pixiv.client.parse_pixiv_history_html", return_value=[]) as parse:
            response.url = "https://accounts.pixiv.net/login"
            with self.assertRaisesRegex(PermissionError, "PHPSESSID"):
                self.crawler.account_history()
            parse.assert_called_once_with(response.text, 100)
        response.close.assert_called_once()
        response = self.response()
        self.crawler._body = Mock(return_value={})
        with patch("software_app.crawlers.pixiv.client.parse_pixiv_profile_html", side_effect=ValueError("bad HTML")):
            with self.assertRaises(RuntimeError):
                self.crawler._user_profile("123")
        response.close.assert_called_once()

    def test_html_waits_before_request_and_preserves_timeout(self):
        self.response()
        with patch.object(HTTP_BACKOFF, "wait", side_effect=TaskCancelled("cancelled while waiting")):
            with self.assertRaises(TaskCancelled):
                self.crawler._text("https://www.pixiv.net/users/123", timeout=(5, 20))
        self.crawler.session.get.assert_not_called()
        self.crawler._text("https://www.pixiv.net/users/123", timeout=(5, 20))
        self.crawler.session.get.assert_called_once()
        self.assertEqual(self.crawler.session.get.call_args.args[0], "https://www.pixiv.net/users/123")
        self.assertEqual(self.crawler.session.get.call_args.kwargs["timeout"], (5, 20))
        self.assertTrue(self.crawler.session.get.call_args.kwargs["stream"])
        self.assertFalse(self.crawler.session.get.call_args.kwargs["allow_redirects"])
