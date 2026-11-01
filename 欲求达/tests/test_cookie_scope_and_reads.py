from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import Mock

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.cookie_store import CookieSession, cookie_jar, parse_cookie_records
from software_app.core.redirect_policy import anonymous_session, get_with_redirects
from software_app.core.response_read import ResponseLimitError, read_response_bytes, read_response_json


class Response:
    def __init__(self, content=b"", *, status=200, headers=None):
        self.content, self.status_code, self.headers = content, status, headers or {}
        self.encoding = "utf-8"
        self.closed = False

    def iter_content(self, **kwargs):
        yield self.content

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def close(self):
        self.closed = True


class CookieScopeAndReadTests(unittest.TestCase):
    def test_prepared_requests_keep_host_path_secure_and_expiry_rules(self):
        records = parse_cookie_records(
            json.dumps(
                [
                    {
                        "name": "host",
                        "value": "only",
                        "domain": "example.test",
                        "hostOnly": True,
                        "path": "/private",
                        "secure": True,
                    },
                    {"name": "shared", "value": "domain", "domain": ".example.test", "hostOnly": False, "path": "/"},
                    {"name": "expired", "value": "old", "domain": ".example.test", "expiry": 0},
                ]
            )
        )
        with CookieSession() as session:
            session.cookies = cookie_jar(records)

            def headers(url):
                return session.prepare_request(requests.Request("GET", url)).headers.get("Cookie", "")

            self.assertIn("host=only", headers("https://example.test/private/image"))
            self.assertNotIn("host=only", headers("https://sub.example.test/private/image"))
            self.assertNotIn("host=only", headers("https://example.test/public"))
            self.assertNotIn("host=only", headers("http://example.test/private/image"))
            self.assertIn("shared=domain", headers("https://sub.example.test/"))
            self.assertNotIn("expired=", headers("https://example.test/"))

    def test_anonymous_public_view_removes_scoped_credentials_without_mutating_origin(self):
        with CookieSession() as session:
            session.headers["Authorization"] = "Bearer synthetic"
            session.cookies.set("login", "synthetic", domain="example.test", path="/")
            view = anonymous_session(session)
            prepared = view.prepare_request(requests.Request("GET", "https://example.test/image"))
            self.assertNotIn("Cookie", prepared.headers)
            self.assertNotIn("Authorization", prepared.headers)
            self.assertEqual(session.cookies.get("login"), "synthetic")
            self.assertIn("Authorization", session.headers)

    def test_same_name_cookies_keep_domain_and_path_metadata(self):
        records = parse_cookie_records(
            json.dumps(
                [
                    {"name": "sid", "value": "a", "domain": "a.test", "path": "/", "hostOnly": True},
                    {
                        "name": "sid",
                        "value": "b",
                        "domain": "b.test",
                        "path": "/restricted",
                        "httpOnly": True,
                        "sameSite": "Lax",
                    },
                ]
            )
        )
        jar = cookie_jar(records)
        self.assertEqual(
            {(cookie.domain, cookie.path, cookie.value) for cookie in jar},
            {("a.test", "/", "a"), ("b.test", "/restricted", "b")},
        )
        second = next(cookie for cookie in jar if cookie.domain == "b.test")
        self.assertTrue(second.has_nonstandard_attr("HttpOnly"))
        self.assertEqual(second.get_nonstandard_attr("SameSite"), "Lax")

    def test_declared_oversize_is_rejected_before_iteration(self):
        response = Response(headers={"Content-Length": "100"})
        response.iter_content = Mock(side_effect=AssertionError("body must not be read"))
        with self.assertRaises(ResponseLimitError):
            read_response_bytes(response, 10)
        response.iter_content.assert_not_called()

    def test_decoded_compressed_body_is_limited_by_actual_bytes(self):
        response = Response(b"01234567890", headers={"Content-Length": "2", "Content-Encoding": "gzip"})
        with self.assertRaises(ResponseLimitError):
            read_response_bytes(response, 10)

    def test_json_accepts_utf16_and_rejects_truncated_content(self):
        payload = {"name": "测试"}
        self.assertEqual(read_response_json(Response(json.dumps(payload).encode("utf-16"))), payload)
        with self.assertRaises(json.JSONDecodeError):
            read_response_json(Response(b'{"name":'))

    def test_cancellation_between_chunks_stops_consumption(self):
        cancel = threading.Event()
        response = Response()

        def chunks(**kwargs):
            yield b"first"
            cancel.set()
            yield b"second"
            raise AssertionError("must not advance past cancellation")

        response.iter_content = chunks
        with self.assertRaises(TaskCancelled):
            read_response_bytes(response, 100, cancel_event=cancel)

    def test_fresh_redirect_transport_lives_until_final_response_closes(self):
        sequence = []
        final = Response(b"data")
        original_close = final.close

        def close_response():
            sequence.append("response")
            original_close()

        final.close = close_response

        class Owned:
            _close_after_redirect = True

            def get(self, url, **kwargs):
                return final

            def close(self):
                sequence.append("session")

        class Session:
            def get(self, url, **kwargs):
                return Response(status=302, headers={"Location": "https://cdn.test/file"})

            def for_redirect(self):
                return Owned()

        response = get_with_redirects(
            Session(), "https://origin.test/file", validate_url=lambda url: None, timeout=(1, 1)
        )
        self.assertEqual(sequence, [])
        self.assertEqual(read_response_bytes(response, 10), b"data")
        response.close()
        response.close()
        self.assertEqual(sequence, ["response", "session"])

    def test_jm_curl_redirect_factory_uses_independent_handles_and_scoped_cookies(self):
        from software_app.crawlers.jmcomic import client as jm

        if jm.curl_requests is None:
            self.skipTest("curl_cffi is optional")
        source = jm._JmCurlSession(
            impersonate="chrome123",
            trust_env=False,
            headers={"Authorization": "Bearer synthetic", "User-Agent": "test"},
        )
        self.addCleanup(source.close)
        source.cookies.set("unscoped", "must-not-copy")
        source.cookies.set("scoped", "allowed", domain="cdn.test", path="/")
        view = source.for_redirect()
        self.addCleanup(view.close)
        self.assertIsNot(source.curl, view.curl)
        self.assertNotIn("Authorization", view.headers)
        self.assertNotIn("unscoped", view.cookies)
        self.assertEqual(view.cookies.get("scoped"), "allowed")
        self.assertEqual(source.cookies.get("unscoped"), "must-not-copy")
