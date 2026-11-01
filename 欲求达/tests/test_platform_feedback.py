from __future__ import annotations

import errno
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests

from software_app.core.storage import AppStorage
from software_app.ui.feedback import friendly_error
from software_app.ui.hentaipaw import HentaiPawUiMixin
from software_app.ui.settings_tab import SettingsTabMixin


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Desktop(SettingsTabMixin, HentaiPawUiMixin):
    pass


class PlatformFeedbackTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.app = Desktop()
        self.app.storage = AppStorage(self.root / "app.db")
        self.app.ui_queue = queue.Queue()
        self.app.manager = Mock()
        self.app._append_log = Mock()
        self.app._refresh_jm_auth_status = Mock()
        self.app._refresh_platform_tabs = Mock()
        self.app._friendly_error = friendly_error
        for name in (
            "proxy",
            "status",
            "cookie_capture_status",
            "pixiv_connection_status",
            "fanbox_status",
            "jm_connection_status",
        ):
            setattr(self.app, name + "_var", Value())
        self.app.cookie_capture_platform_var = Value("Pixiv")
        self.app.jm_domain_var = Value("https://18comic.vip")
        self.app.jm_user_agent_var = Value()
        for name in (
            "pixiv_connection",
            "fanbox_check",
            "jm_connection",
            "eh_validate",
            "cookie_import",
            "header_convert",
            "jm_header_convert",
            "hentaipaw_access",
        ):
            setattr(self.app, name + "_button", Mock())

    def drain_check(self):
        event, payload = self.app.ui_queue.get_nowait()
        self.assertEqual(event, "settings_check_finished")
        self.app._finish_settings_check(payload)

    def test_unsaved_proxy_warning_applies_to_all_cookie_platforms_without_exposing_credentials(self):
        self.app.proxy_var.set("http://private-user:private-pass@127.0.0.1:1234")
        for platform in (
            "Twitter/X",
            "Pixiv",
            "FANBOX",
            "Instagram",
            "JMComic",
            "E-Hentai 表站",
            "ExHentai 里站",
            "HentaiPaw",
        ):
            with self.subTest(platform=platform):
                note = self.app._settings_check_note(platform)
                self.assertIn("尚未保存", note)
                self.assertNotIn("private-", note)

    def test_jm_unsaved_domain_and_user_agent_warn_and_blank_proxy_uses_system_policy(self):
        self.app.jm_domain_var.set("https://example.org")
        self.assertIn("尚未保存", self.app._settings_check_note("JMComic"))
        self.app.jm_domain_var.set("https://18comic.vip")
        self.app.jm_user_agent_var.set("new UA")
        self.assertIn("尚未保存", self.app._settings_check_note("JMComic"))
        self.assertIn("系统代理", self.app._settings_check_note("JMComic"))
        self.assertIn("直连", self.app._settings_check_note("HentaiPaw"))
        self.assertIn("系统网络配置", self.app._settings_check_note("Pixiv", browser=True))

    def test_check_uses_snapshot_and_persists_elapsed_time(self):
        self.app.proxy_var.set("http://127.0.0.1:1234")
        jobs = []
        adapter = self.app.manager.get_adapter.return_value
        adapter.connection_status.return_value = {"message": "Pixiv 资料可读取"}
        with patch(
            "software_app.ui.settings_tab.threading.Thread",
            side_effect=lambda **kw: SimpleNamespace(start=lambda: jobs.append(kw["target"])),
        ):
            self.app._check_pixiv_connection()
        self.app.proxy_var.set("http://127.0.0.1:5678")
        jobs[0]()
        adapter.connection_status.assert_called_once_with({"proxy_url": "http://127.0.0.1:1234"})
        self.drain_check()
        self.app.pixiv_connection_button.state.assert_called_with(["!disabled"])
        self.assertIn("耗时", self.app.pixiv_connection_status_var.get())
        logs = self.app.storage.recent_logs()
        self.assertEqual(len(logs), 2)
        self.assertIn("elapsed_seconds", logs[0]["metadata_json"])

    def test_fanbox_network_failure_is_not_shown_as_expired_cookie(self):
        self.app.manager.get_adapter.return_value.fanbox_supporting_status.side_effect = requests.Timeout(
            "cookie endpoint timeout"
        )
        with patch(
            "software_app.ui.settings_tab.threading.Thread",
            side_effect=lambda **kw: SimpleNamespace(start=kw["target"]),
        ):
            self.app._check_fanbox_status()
        self.drain_check()
        message = self.app.fanbox_status_var.get()
        self.assertIn("请求超时", message)
        self.assertNotIn("失效", message)
        self.assertEqual(self.app.storage.recent_logs()[0]["level"], "error")

    def test_thread_start_failure_restores_each_check_button(self):
        for method, button in (
            ("_check_pixiv_connection", "pixiv_connection"),
            ("_check_fanbox_status", "fanbox_check"),
            ("_check_jm_connection", "jm_connection"),
        ):
            with self.subTest(method=method), patch("software_app.ui.settings_tab.threading.Thread") as thread:
                thread.return_value.start.side_effect = RuntimeError("cannot start")
                getattr(self.app, method)()
                getattr(self.app, button + "_button").state.assert_called_with(["!disabled"])

    def test_cookie_import_failure_does_not_overwrite_new_platform_and_is_logged(self):
        self.app.cookie_capture_status_var.set("Pixiv 当前提示")
        with patch("software_app.ui.settings_tab.messagebox.showerror") as dialog:
            self.app._finish_cookie_file_import(("Instagram", "Cookie: private-value"), error=True)
        self.assertEqual(self.app.cookie_capture_status_var.get(), "Pixiv 当前提示")
        dialog.assert_not_called()
        self.assertNotIn("private-value", str(dict(self.app.storage.recent_logs()[0])))

    def test_cookie_import_success_for_all_platforms_only_claims_saved(self):
        for platform in (
            "Twitter/X",
            "Pixiv",
            "FANBOX",
            "Instagram",
            "JMComic",
            "E-Hentai 表站",
            "ExHentai 里站",
            "HentaiPaw",
        ):
            with self.subTest(platform=platform):
                self.app.cookie_capture_platform_var.set(platform)
                self.app._finish_cookie_file_import((platform, 2, self.root / "cookies.json"), error=False)
                message = self.app.cookie_capture_status_var.get()
                self.assertIn("Cookie", message)
                self.assertNotIn("登录验证通过", message)
        self.assertEqual(len(self.app.storage.recent_logs()), 8)

    def test_eh_validation_result_is_logged_without_overwriting_other_cookie_platform(self):
        self.app.cookie_capture_status_var.set("Pixiv 当前提示")
        self.app._finish_eh_validation(("ExHentai 里站", "Cookie: private-eh"), error=True)
        self.assertEqual(self.app.cookie_capture_status_var.get(), "Pixiv 当前提示")
        log = self.app.storage.recent_logs()[0]
        self.assertEqual(log["module_id"], "ehentai")
        self.assertNotIn("private-eh", log["message"])

    def test_header_import_result_does_not_overwrite_other_platform(self):
        self.app.cookie_capture_status_var.set("Pixiv 当前提示")
        self.app._finish_header_import(("Instagram", self.root / "export.json", "格式无效"), error=True)
        self.assertEqual(self.app.cookie_capture_status_var.get(), "Pixiv 当前提示")
        self.app.header_convert_button.state.assert_called_with(["!disabled"])
        self.assertEqual(self.app.storage.recent_logs()[0]["module_id"], "instagram")

    def test_hentaipaw_unexpected_access_error_restores_button_and_logs(self):
        self.app.cookie_capture_platform_var.set("HentaiPaw")
        self.app.manager.get_adapter.return_value.access_status.side_effect = RuntimeError("invalid response")
        with patch(
            "software_app.ui.hentaipaw.threading.Thread", side_effect=lambda **kw: SimpleNamespace(start=kw["target"])
        ):
            self.app._check_hentaipaw_access()
        event, payload = self.app.ui_queue.get_nowait()
        self.assertEqual(event, "hentaipaw_access_result")
        self.app._finish_hentaipaw_access(payload)
        self.app.hentaipaw_access_button.state.assert_called_with(["!disabled"])
        self.assertEqual(self.app.storage.recent_logs()[0]["level"], "error")


class FriendlyErrorTests(unittest.TestCase):
    def test_cookie_and_login_advice_are_not_automatically_expired_sessions(self):
        for detail in (
            "Cookie 文件格式无效",
            "无法读取 cookies.json",
            "未取得阅读器原图；若页面提示登录，请导入 Cookie",
            "无法解析登录返回页面",
        ):
            with self.subTest(detail=detail):
                message = friendly_error(detail, "在线预览")
                self.assertIn(detail, message)
                self.assertNotIn("失效", message)

    def test_typed_local_file_error_does_not_become_login_error(self):
        for code, label in ((errno.ENOSPC, "磁盘"), (errno.EACCES, "权限")):
            message = friendly_error(OSError(code, "cookies.json"), "Cookie 导入")
            self.assertIn(label, message)
            self.assertNotIn("登录", message)

    def test_network_tls_challenge_and_login_have_distinct_feedback(self):
        for error, label in (
            (requests.exceptions.ProxyError("bad proxy"), "代理"),
            (requests.exceptions.SSLError("certificate"), "证书"),
            (requests.ConnectionError("bad connection"), "网络连接"),
            ("Cloudflare challenge", "站点验证"),
            ("Login required", "有效登录"),
        ):
            with self.subTest(error=error):
                self.assertIn(label, friendly_error(error, "读取"))
        self.assertNotIn("登录已失效", friendly_error("403 Forbidden", "读取"))

    def test_network_disconnection_is_not_reported_as_closed_browser(self):
        self.assertNotIn("浏览器已经关闭", friendly_error("RemoteDisconnected: disconnected", "读取"))

    def test_error_feedback_redacts_tokens(self):
        message = friendly_error("bad https://example.org/?token=private-token&page=2", "读取")
        self.assertNotIn("private-token", message)
        self.assertIn("page=2", message)
