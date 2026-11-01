from __future__ import annotations

import queue
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from software_app.ui.tk_app import SoftwareDesktop


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class SearchStateTests(unittest.TestCase):
    def state(self):
        state = SimpleNamespace(
            module_var=Value("hentaipaw"),
            content_scope_var=Value("关键词搜索"),
            platform_search_var=Value("first"),
            output_dir_var=Value("output"),
            platform_search_request_id=0,
            _platform_search_context=None,
            ui_queue=queue.Queue(),
            platform_search_button=Mock(),
            candidate_refresh_button=Mock(),
            status_var=Value(""),
            _selected_adapter=Mock(),
            _current_target_options=Mock(return_value={"types": "1"}),
            _show_platform_search_results=Mock(),
            _refresh_platform_tabs=Mock(),
            _append_log=Mock(),
            _friendly_error=lambda message, _action: message,
        )
        adapter = Mock(module_id="hentaipaw", display_name="HentaiPaw")
        adapter.info.capabilities = ("search", "download")
        adapter.info.stage = "ready"
        adapter.search_targets.return_value = [{"id": "123"}]
        state._selected_adapter.return_value = adapter
        state._finish_platform_search = lambda payload, **kwargs: SoftwareDesktop._finish_platform_search(
            state, payload, **kwargs
        )
        return state

    def test_search_snapshots_options_before_worker_and_freezes_scope(self):
        state = self.state()
        jobs = []

        def deferred(*, target, name, daemon):
            return SimpleNamespace(start=lambda: jobs.append(target))

        with patch("software_app.ui.tk_app.threading.Thread", side_effect=deferred):
            SoftwareDesktop._search_platform_targets(state)
        state._current_target_options.assert_called_once_with("first")
        state.module_var.set("bluesky")
        state.content_scope_var.set("用户搜索")
        jobs[0]()
        state._selected_adapter.return_value.search_targets.assert_called_once_with(
            "first",
            limit=20,
            options={"types": "1", "output_dir": "output"},
        )
        event, payload = state.ui_queue.get_nowait()
        self.assertEqual(event, "platform_search_loaded")
        self.assertEqual(payload[:4], (1, "hentaipaw", "关键词搜索", "first"))

    def test_late_success_does_not_replace_newer_search_or_enable_button(self):
        state = self.state()
        state.platform_search_request_id = 2
        state._platform_search_context = ("hentaipaw", "关键词搜索", "second")
        SoftwareDesktop._finish_platform_search(state, (1, "hentaipaw", "关键词搜索", "first", []))
        state.platform_search_button.state.assert_not_called()
        state._show_platform_search_results.assert_not_called()
        self.assertEqual(state._platform_search_context[-1], "second")

    def test_late_error_does_not_overwrite_another_platform_status(self):
        state = self.state()
        state._platform_search_context = ("hentaipaw", "关键词搜索", "first")
        state.module_var.set("pixiv")
        state.status_var.set("Pixiv 已就绪")
        SoftwareDesktop._finish_platform_search(state, (0, "hentaipaw", "关键词搜索", "first", "failed"), error=True)
        self.assertEqual(state.status_var.get(), "Pixiv 已就绪")
        state._append_log.assert_not_called()

    def test_switching_away_and_back_invalidates_original_request(self):
        state = self.state()
        state._platform_search_context = ("hentaipaw", "关键词搜索", "first")
        SoftwareDesktop._invalidate_platform_search(state)
        SoftwareDesktop._finish_platform_search(state, (0, "hentaipaw", "关键词搜索", "first", []))
        state._show_platform_search_results.assert_not_called()

    def test_current_success_clears_busy_and_shows_rows(self):
        state = self.state()
        state._platform_search_context = ("hentaipaw", "关键词搜索", "first")
        rows = [{"id": "123"}]
        SoftwareDesktop._finish_platform_search(state, (0, "hentaipaw", "关键词搜索", "first", rows))
        self.assertIsNone(state._platform_search_context)
        state.platform_search_button.state.assert_called_once_with(["!disabled"])
        state._show_platform_search_results.assert_called_once_with("hentaipaw", "first", rows)

    def test_current_error_restores_controls(self):
        state = self.state()
        state._platform_search_context = ("hentaipaw", "关键词搜索", "first")
        SoftwareDesktop._finish_platform_search(state, (0, "hentaipaw", "关键词搜索", "first", "failed"), error=True)
        self.assertIsNone(state._platform_search_context)
        state._refresh_platform_tabs.assert_called_once()
        state.platform_search_button.state.assert_called_once_with(["!disabled"])

    def test_thread_start_failure_restores_controls(self):
        state = self.state()
        with patch(
            "software_app.ui.tk_app.threading.Thread",
            return_value=SimpleNamespace(start=Mock(side_effect=RuntimeError("cannot start"))),
        ):
            SoftwareDesktop._search_platform_targets(state)
        self.assertIsNone(state._platform_search_context)
        self.assertEqual(state.status_var.get(), "目标搜索暂未完成")

    def test_switch_from_planned_scope_restores_download_button(self):
        state = self.state()
        state.start_button = Mock()
        state.module_var.set("instagram")
        state.content_scope_var.set("关注账号（需登录，规划）")
        SoftwareDesktop._configure_scope_actions(state)
        state.start_button.state.assert_called_with(["disabled"])
        state.content_scope_var.set("账号 / 用户名")
        SoftwareDesktop._configure_scope_actions(state)
        state.start_button.state.assert_called_with(["!disabled"])
        state.module_var.set("google_image")
        SoftwareDesktop._configure_scope_actions(state)
        state.start_button.state.assert_called_with(["disabled"])

    def test_batch_preview_keeps_reader_verification_as_boolean(self):
        from software_app.core.models import TargetPreview

        preview = TargetPreview(
            "hentaipaw",
            "123",
            "https://zh.hentaipaw.com/articles/123",
            metadata={"reader_verified": False, "page_count": 0},
        )
        compact = SoftwareDesktop._generic_batch_info_row("123", preview)["_batch_preview"]
        self.assertIs(compact.metadata["reader_verified"], False)
        self.assertEqual(compact.metadata["page_count"], 0)
