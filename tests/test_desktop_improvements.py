import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from software_app.core.bluesky_account import BlueskyAccountManager
from software_app.core.browser_driver import BrowserDriverStatus, chromedriver_status, versions_compatible
from software_app.core.network_policy import HTTP_BACKOFF, network_task_scope
from software_app.core.adapter import TaskCancelled
from software_app.core.preflight import inspect_environment
from software_app.crawlers.webpage.client import _request_public_url
from software_app.ui.tk_app import SoftwareDesktop
from software_app.ui.task_queue_tab import TaskQueueTabMixin


class DesktopImprovementTests(unittest.TestCase):
    def setUp(self):
        HTTP_BACKOFF.clear()
        self.addCleanup(HTTP_BACKOFF.clear)

    def test_retry_buttons_keep_original_by_default_and_current_is_explicit(self):
        import json

        row = {
            "task_id": "old",
            "module_id": "twitter",
            "target": "alice",
            "status": "failed",
            "options_json": '{"types":"1"}',
            "output_dir": "outputs",
        }
        state = SimpleNamespace(
            _selected_task_rows=lambda: [row],
            _task_can_retry=lambda item: True,
            manager=Mock(),
            status_var=Mock(),
            _task_callbacks=Mock(),
            _saved_format_options=Mock(return_value={"image_format": "jpg"}),
            _saved_archive_options=Mock(return_value={}),
            _decode_task_options=json.loads,
            _selected_types=lambda: "2",
            storage=Mock(),
            current_task_ids=set(),
            _append_log=Mock(),
            _refresh_tasks=Mock(),
            _follow_task_batch=Mock(),
            task_progress_var=Mock(),
            task_tree=Mock(),
            output_dir_var=Mock(),
        )
        state.manager.adapters = {"twitter": Mock()}
        state.manager.get_adapter.return_value.display_name = "Twitter"
        state.manager.retry_task.return_value.task_id = "new"

        def retry(task_id, _callbacks, **kwargs):
            kwargs["before_start"](row["module_id"], row["target"], Path(row["output_dir"]), {})
            return SimpleNamespace(task_id="new-" + task_id)

        state.manager.retry_task.side_effect = retry
        state.manager.has_active_task.return_value = False
        state.task_tree.exists.return_value = False
        state.storage.get_setting.side_effect = lambda key, default=None: default
        with patch("software_app.ui.task_queue_tab.messagebox.askyesno", return_value=True):
            TaskQueueTabMixin._retry_selected_task(state)
            self.assertNotIn("option_overrides", state.manager.retry_task.call_args.kwargs)
            state._saved_format_options.assert_not_called()
            TaskQueueTabMixin._retry_selected_task(state, use_current_settings=True)
        options = state.manager.retry_task.call_args.kwargs
        self.assertTrue(options["use_current_settings"])
        self.assertEqual(options["option_overrides"]["types"], "2")
        self.assertEqual(options["option_overrides"]["image_format"], "jpg")
        state.manager.retry_task.reset_mock()
        state._selected_task_rows = lambda: [row, dict(row, task_id="duplicate")]
        with patch("software_app.ui.task_queue_tab.messagebox.askyesno", return_value=True):
            TaskQueueTabMixin._retry_selected_task(state)
        self.assertEqual(state.manager.retry_task.call_count, 2)
        state._follow_task_batch.assert_called_with(["new-old"])
        self.assertIn("相同目标", state._append_log.call_args.args[0])
        state.manager.retry_task.reset_mock()
        state._selected_task_rows = lambda: [row]
        state.manager.has_active_task.return_value = True
        with (
            patch("software_app.ui.task_queue_tab.messagebox.askyesno", return_value=True),
            patch("software_app.ui.task_queue_tab.messagebox.showerror"),
        ):
            TaskQueueTabMixin._retry_selected_task(state)
        state.manager.retry_task.assert_called_once()

    def test_unscoped_cookie_cannot_cross_origin_on_redirect(self):
        session = requests.Session()
        self.addCleanup(session.close)
        session.cookies.set("synthetic_session", "secret-value")
        response = Mock(status_code=302, headers={"Location": "https://other.example/page"})
        with (
            patch.object(session, "get", return_value=response) as get,
            patch(
                "software_app.crawlers.webpage.client._validate_public_destination",
                side_effect=lambda url: url,
            ),
        ):
            with self.assertRaisesRegex(ValueError, "凭据"):
                _request_public_url(session, "https://original.example/page", timeout=(1, 1))
        self.assertEqual(get.call_count, 1)
        response.close.assert_called_once()

    def test_driver_without_chrome_is_not_reported_as_compatible(self):
        self.assertFalse(versions_compatible("", "123.0.1"))
        with (
            patch("software_app.core.browser_driver.chrome_version", return_value=""),
            patch(
                "software_app.core.browser_driver.find_existing_driver",
                return_value=(Path("driver.exe"), "path"),
            ),
            patch("software_app.core.browser_driver.chromedriver_version", return_value="123.0.1"),
        ):
            self.assertEqual(chromedriver_status().source, "browser-missing")

    def test_read_only_driver_check_does_not_copy_a_legacy_driver(self):
        with (
            patch("software_app.core.browser_driver.chrome_version", return_value="123.0.1"),
            patch(
                "software_app.core.browser_driver.find_existing_driver",
                return_value=(Path("driver.exe"), "path"),
            ),
            patch("software_app.core.browser_driver.chromedriver_version", return_value="123.0.1"),
            patch(
                "software_app.core.browser_driver.copy_driver_to_global",
            ) as copy,
        ):
            self.assertEqual(chromedriver_status().source, "path")
        copy.assert_not_called()

    def test_preflight_reports_driver_versions_path_and_does_not_install(self):
        status = BrowserDriverStatus(Path("driver.exe"), "123.1", "123.2", "path", "版本匹配")
        with (
            patch("software_app.core.preflight.chromedriver_status", return_value=status) as check,
            patch(
                "software_app.core.preflight.find_ffmpeg",
                return_value="ffmpeg.exe",
            ),
        ):
            report, driver = inspect_environment(Path.cwd())
        self.assertIn("123.1", driver)
        self.assertIn("123.2", driver)
        self.assertIn("driver.exe", driver)
        self.assertIn("ffmpeg.exe", report)
        check.assert_called_once_with(auto_download=False)

    def test_ui_log_redacts_direct_messages_and_trims_old_display_lines(self):
        widget = Mock()
        widget.index.return_value = "5501.0"
        state = SimpleNamespace(log_text=widget)
        SoftwareDesktop._append_log_batch(state, ["https://files.example/file?token=secret-token"])
        self.assertNotIn("secret-token", widget.insert.call_args.args[1])
        widget.delete.assert_called_once_with("1.0", "502.0")
        self.assertNotIn(
            "secret-token", SoftwareDesktop._friendly_error("failed https://files.example?token=secret-token", "预览")
        )

    def test_bluesky_429_retains_http_status_publishes_backoff_and_closes(self):
        session = Mock()
        response = Mock(status_code=429, headers={"Retry-After": "7"}, url="https://bsky.social/api")
        response.raise_for_status.side_effect = requests.HTTPError("429", response=response)
        session.request.return_value = response
        account = BlueskyAccountManager("owner.test", "secret", session=session)
        with self.assertRaises(requests.HTTPError) as caught:
            account._json("POST", response.url, json={"operation": "synthetic"})
        self.assertIs(caught.exception.response, response)
        response.close.assert_called_once()
        self.assertGreater(HTTP_BACKOFF.remaining(response.url), 0)
        self.assertEqual(session.request.call_count, 1)

    def test_cancelled_account_request_does_not_reach_session(self):
        import threading

        event = threading.Event()
        event.set()
        session = Mock()
        with network_task_scope(event), self.assertRaises(TaskCancelled):
            BlueskyAccountManager("owner.test", "secret", session=session)._json("GET", "https://bsky.social/api")
        session.request.assert_not_called()
