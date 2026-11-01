import queue
import socket
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.network_policy import HTTP_BACKOFF
from software_app.crawlers.webpage import availability
from software_app.ui.google_search_tab import GoogleSearchTabMixin


class LinkAvailabilityTests(unittest.TestCase):
    def setUp(self):
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)
        dns = patch(
            "software_app.crawlers.webpage.client.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
            ],
        )
        dns.start()
        self.addCleanup(dns.stop)
        self.session = Mock(auth=None, headers={})

    def response(self, status=200, *, url="https://public.example/page", headers=None):
        return Mock(status_code=status, url=url, headers=headers or {})

    def test_every_status_keeps_existing_classification_and_closes_response(self):
        for status, state in (
            (200, "available"),
            (404, "missing"),
            (410, "missing"),
            (403, "restricted"),
            (429, "restricted"),
            (503, "temporary"),
        ):
            with self.subTest(status=status):
                HTTP_BACKOFF.clear()
                response = self.response(status)
                self.session.get.return_value = response
                result = availability.check_page_availability(response.url, session=self.session)
                self.assertEqual(result["availability"], state)
                response.close.assert_called_once()
                self.session.close.assert_not_called()
                self.assertFalse(self.session.get.call_args.kwargs["allow_redirects"])

    def test_private_redirect_is_rejected_before_second_request(self):
        for location in ("http://127.0.0.1/private", "http://[::1]/private"):
            with self.subTest(location=location):
                self.session.reset_mock()
                response = self.response(302, headers={"Location": location})
                self.session.get.return_value = response
                result = availability.check_page_availability(response.url, session=self.session)
                self.assertEqual(result["availability"], "unknown")
                self.assertEqual(self.session.get.call_count, 1)
                response.close.assert_called_once()

    def test_dns_resolving_to_private_address_never_reaches_session(self):
        with patch(
            "software_app.crawlers.webpage.client.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.1.1", 443)),
            ],
        ):
            result = availability.check_page_availability("https://internal.example/page", session=self.session)
        self.assertEqual(result["availability"], "unknown")
        self.session.get.assert_not_called()

    def test_public_redirect_is_followed_and_both_responses_close(self):
        redirect = self.response(302, headers={"Location": "/final"})
        final = self.response(url="https://public.example/final")
        self.session.get.side_effect = [redirect, final]
        result = availability.check_page_availability(redirect.url, session=self.session)
        self.assertEqual(result["final_url"], final.url)
        self.assertEqual(self.session.get.call_args.args[0], final.url)
        redirect.close.assert_called_once()
        final.close.assert_called_once()

    def test_cross_origin_authentication_is_not_forwarded(self):
        self.session.headers = {"Authorization": "Bearer secret-token"}
        response = self.response(302, headers={"Location": "https://other.example/page"})
        self.session.get.return_value = response
        result = availability.check_page_availability(response.url, session=self.session)
        self.assertEqual(result["availability"], "unknown")
        self.assertEqual(self.session.get.call_count, 1)
        self.assertNotIn("secret-token", str(result))

    def test_error_is_redacted_and_owned_session_closes(self):
        self.session.get.side_effect = requests.ConnectionError("https://public.example/page?token=secret-token")
        with patch.object(availability, "make_session", return_value=self.session):
            result = availability.check_page_availability("https://public.example/page")
        self.assertEqual(result["availability"], "unknown")
        self.assertNotIn("secret-token", str(result))
        self.session.close.assert_called_once()

    def test_owned_session_and_response_close_on_success(self):
        response = self.response()
        self.session.get.return_value = response
        with patch.object(availability, "make_session", return_value=self.session):
            availability.check_page_availability(response.url)
        response.close.assert_called_once()
        self.session.close.assert_called_once()

    def test_pre_cancelled_check_does_not_create_session_or_resolve_dns(self):
        event = threading.Event()
        event.set()
        with (
            patch.object(availability, "make_session") as create,
            patch("software_app.crawlers.webpage.client.socket.getaddrinfo") as dns,
            self.assertRaises(TaskCancelled),
        ):
            availability.check_page_availability("https://public.example/page", cancel_event=event)
        create.assert_not_called()
        dns.assert_not_called()

    def test_cancel_during_backoff_propagates_and_closes_owned_session(self):
        event = threading.Event()
        HTTP_BACKOFF.defer("https://public.example/page", 30)
        event.set()
        # Set cancellation after initial validation, before the request's wait.
        with (
            patch.object(availability, "task_cancelled", return_value=False),
            patch.object(availability, "make_session", return_value=self.session),
            self.assertRaises(TaskCancelled),
        ):
            availability.check_page_availability("https://public.example/page", cancel_event=event)
        self.session.get.assert_not_called()
        self.session.close.assert_called_once()

    def test_ui_worker_passes_cancellation_and_does_not_mark_cancelled_row_checked(self):
        state = SimpleNamespace(
            google_link_check_cancel_event=None,
            _update_google_result_summary=Mock(),
            status_var=Mock(),
            storage=Mock(),
            ui_queue=queue.Queue(),
        )
        state.storage.get_setting.return_value = ""

        def cancelled(*args, **kwargs):
            self.assertIs(kwargs["cancel_event"], state.google_link_check_cancel_event)
            kwargs["cancel_event"].set()
            raise TaskCancelled("cancelled")

        def thread_factory(*, target, **kwargs):
            return SimpleNamespace(start=target)

        with (
            patch("software_app.ui.google_search_tab.threading.Thread", side_effect=thread_factory),
            patch("software_app.ui.google_search_tab.check_page_availability", side_effect=cancelled) as check,
        ):
            GoogleSearchTabMixin._start_google_link_check(
                state, [(0, "https://one.example"), (1, "https://two.example")]
            )
        self.assertEqual(check.call_count, 1)
        name, result = state.ui_queue.get_nowait()
        self.assertEqual(name, "google_link_check_done")
        self.assertEqual(result["checked"], 0)
        self.assertTrue(result["cancelled"])
        self.assertTrue(state.ui_queue.empty())
