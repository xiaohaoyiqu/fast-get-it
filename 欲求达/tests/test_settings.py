from __future__ import annotations

import os
import queue
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from types import SimpleNamespace

from software_app.core.storage import AppStorage
from software_app.ui.settings_tab import SettingsTabMixin
from software_app.ui.tk_app import SoftwareDesktop
from software_app.core.bandwidth import MEDIA_BANDWIDTH
from software_app.core.browser_pool import BROWSER_POOL


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class Desktop(SettingsTabMixin):
    def __init__(self, storage, directory):
        self.storage = storage
        defaults = {
            "speed_limit_enabled": False,
            "speed_limit_kib": "1024",
            "browser_session_limit": "3",
            "browser_memory_enabled": False,
            "browser_memory_high": "85",
            "browser_memory_recovery": "75",
            "browser_memory_timeout": "120",
            "output_dir": str(directory),
            "proxy": "http://127.0.0.1:7890",
            "retries": 3,
            "google_limit": 45,
            "google_wait": 20,
            "google_max_files": 60,
            "webpage_timeout": 90,
            "pixiv_max_works": 37,
            "pixiv_filter_ai": False,
            "pixiv_visibility": "私密",
            "pixiv_start_date": "2026-09-22",
            "pixiv_end_date": "2026-10-28",
            "pixiv_minimum_bookmarks": 10,
            "pixiv_age_mode": "全部",
            "pixiv_bookmark_tag": "tag",
            "pixiv_author_match": "完全匹配",
            "pixiv_history_limit": 120,
            "jm_domain": "https://18comic.vip",
            "jm_user_agent": "Chrome test",
            "jm_order": "最多浏览",
            "jm_time": "本周",
            "jm_category": "同人",
            "jm_favorite_username": "example",
            "jm_favorite_folder": "12",
            "jm_novel_favorite_folder": "34",
            "jm_match": "标题精确匹配",
            "jm_postprocess": "按章节 PDF",
            "jm_download_cover": False,
            "eh_download_torrent": False,
            "eh_bt_download_enabled": False,
            "aria2_limit_enabled": False,
            "aria2_download_kib": "1024",
            "aria2_upload_kib": "128",
            "image_format": "WebP",
            "video_format": "保留原格式",
            "animation_format": "MP4",
            "audio_format": "M4A",
            "archive_mode": "按整个任务压缩",
            "extract_archives": True,
            "archive_cleanup_sources": False,
            "save_unfinished_txt": True,
            "rename_downloads": False,
            "delete_source_after_rename": False,
            "file_conflict": "复用已有文件",
            "status": "",
        }
        for name, value in defaults.items():
            setattr(self, name + "_var", Value(value))
        self.platform_concurrency_vars = {
            "twitter": {"total": Value(4), "single": Value(3), "collection": Value(1)},
        }
        self._refresh_jm_auth_status = Mock()
        self._append_log = Mock()


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = AppStorage(self.root / "settings.db")
        self.desktop = Desktop(self.storage, self.root / "downloads")
        self.messages = patch("software_app.ui.settings_tab.messagebox").start()
        self.addCleanup(patch.stopall)
        self.addCleanup(lambda: MEDIA_BANDWIDTH.configure(False, 1024 * 1024))
        self.addCleanup(lambda: BROWSER_POOL.configure(3))

    def test_directory_selection_survives_new_storage_instance_without_saving_settings(self):
        desktop = object.__new__(SoftwareDesktop)
        desktop.storage = self.storage
        desktop.output_dir_var = Value(str(self.root))
        desktop._refresh_library = Mock()
        desktop._load_twitter_history = Mock()
        chosen = self.root / "chosen"
        chosen.mkdir()
        with patch("software_app.ui.tk_app.filedialog.askdirectory", return_value=str(chosen)):
            desktop._choose_output_dir()
        self.assertEqual(AppStorage(self.storage.db_path).get_setting("default_output_dir"), str(chosen))
        self.assertIsNone(self.storage.get_setting("proxy_url"))

    def test_cancelled_directory_dialog_keeps_saved_path(self):
        self.storage.set_setting("default_output_dir", str(self.root))
        desktop = object.__new__(SoftwareDesktop)
        desktop.storage = self.storage
        desktop.output_dir_var = Value(str(self.root))
        with patch("software_app.ui.tk_app.filedialog.askdirectory", return_value=""):
            desktop._choose_output_dir()
        self.assertEqual(self.storage.get_setting("default_output_dir"), str(self.root))

    def test_manual_directory_edit_is_remembered_and_empty_edit_keeps_last_path(self):
        self.assertTrue(self.desktop._remember_output_dir())
        chosen = self.desktop.output_dir_var.get()
        self.desktop.output_dir_var.set("")
        self.assertTrue(self.desktop._remember_output_dir())
        self.assertEqual(self.desktop.output_dir_var.get(), chosen)

    def test_first_empty_directory_uses_default(self):
        self.desktop.output_dir_var.set("")
        with patch("software_app.core.settings.DEFAULT_OUTPUT_DIR", self.root / "default"):
            self.assertTrue(self.desktop._remember_output_dir())
        self.assertEqual(self.storage.get_setting("default_output_dir"), str(self.root / "default"))

    def test_invalid_directory_never_overwrites_last_path(self):
        self.storage.set_setting("default_output_dir", str(self.root))
        for invalid in (str(self.storage.db_path), "bad\x00path", "bad\ud800path"):
            with self.subTest(invalid=repr(invalid)):
                self.desktop.output_dir_var.set(invalid)
                self.assertFalse(self.desktop._remember_output_dir())
                self.assertEqual(self.storage.get_setting("default_output_dir"), str(self.root))

    def test_all_settings_save_and_reload_in_one_transaction(self):
        with patch.object(self.storage, "connect", wraps=self.storage.connect) as connect:
            self.desktop._save_settings()
        # One previous-value read and one atomic write for this configuration.
        self.assertEqual(connect.call_count, 2)
        values = AppStorage(self.storage.db_path).get_settings()
        self.assertEqual(len(values), 54)
        self.assertEqual(values["file_conflict"], "reuse")
        self.assertFalse(values["speed_limit_enabled"])
        self.assertEqual(values["speed_limit_kib"], 1024)
        self.assertEqual(values["browser_session_limit"], 3)
        self.assertFalse(values["browser_memory_enabled"])
        self.assertEqual((values["browser_memory_high"], values["browser_memory_recovery"],
                          values["browser_memory_timeout"]), (85, 75, 120))
        self.assertEqual(values["default_output_dir"], str(self.root / "downloads"))
        self.assertEqual(values["proxy_url"], "http://127.0.0.1:7890")
        self.assertEqual(values["task_retries"], 3)
        self.assertFalse(values["pixiv_filter_ai"])
        self.assertEqual(values["image_output_format"], "webp")
        self.assertEqual(values["video_output_format"], "original")
        self.assertEqual(values["animation_output_format"], "mp4")
        self.assertEqual(values["audio_output_format"], "m4a")
        self.assertEqual(values["jmcomic_postprocess"], "pdf")
        self.assertEqual(values["task_concurrency_twitter"], 4)
        self.assertTrue(values["save_unfinished_txt"])
        self.assertFalse(values["rename_downloads"])
        self.messages.showwarning.assert_not_called()

    def test_bad_dates_reject_entire_settings_save(self):
        for start, end in (("2026-02-30", ""), ("2026-9-22", ""), ("2026-10-28", "2026-09-22")):
            with self.subTest(start=start, end=end):
                self.desktop.pixiv_start_date_var.set(start)
                self.desktop.pixiv_end_date_var.set(end)
                self.desktop._save_settings()
                self.assertEqual(self.storage.get_settings(), {})

    def test_malformed_proxy_and_domain_do_not_raise_or_save(self):
        for proxy in (
            "http://[::1",
            "http://localhost:abc",
            "http://localhost:99999",
            "http://localhost:0",
            "http://bad host:80",
            "http://user:secret@localhost:80",
        ):
            with self.subTest(proxy=proxy):
                self.desktop.proxy_var.set(proxy)
                self.desktop._save_settings()
                self.assertEqual(self.storage.get_settings(), {})
        self.desktop.proxy_var.set("")
        for domain in ("https://[broken", "https://18comic.vip:99999", "https://user:secret@18comic.vip"):
            with self.subTest(domain=domain):
                self.desktop.jm_domain_var.set(domain)
                self.desktop._save_settings()
                self.assertEqual(self.storage.get_settings(), {})

    def test_database_write_failure_does_not_report_success(self):
        with patch.object(self.storage, "set_settings", side_effect=sqlite3.OperationalError("locked")):
            self.desktop._save_settings()
        self.messages.showerror.assert_called_once()
        self.desktop._append_log.assert_not_called()
        self.assertEqual(self.storage.get_settings(), {})

    def test_speed_limit_save_updates_running_budget_and_disabling_keeps_value(self):
        self.desktop.speed_limit_enabled_var.set(True)
        self.desktop.speed_limit_kib_var.set("2048")
        self.desktop._save_settings()
        self.assertEqual(MEDIA_BANDWIDTH.settings, (True, 2048 * 1024))
        self.desktop.speed_limit_enabled_var.set(False)
        self.desktop._save_settings()
        self.assertEqual(MEDIA_BANDWIDTH.settings, (False, 2048 * 1024))
        self.assertEqual(self.storage.get_setting("speed_limit_kib"), 2048)

    def test_invalid_speed_limit_rejects_entire_settings_save(self):
        self.desktop.speed_limit_kib_var.set("0")
        self.desktop._save_settings()
        self.assertEqual(self.storage.get_settings(), {})

    def test_aria2_limits_save_separately_and_bad_value_rejects_entire_transaction(self):
        self.desktop.aria2_limit_enabled_var.set(True)
        self.desktop.aria2_download_kib_var.set("256")
        self.desktop.aria2_upload_kib_var.set("32")
        self.desktop._save_settings()
        values = self.storage.get_settings()
        self.assertEqual((values["aria2_limit_enabled"], values["aria2_download_kib"], values["aria2_upload_kib"]), (True, 256, 32))
        self.assertFalse(values["speed_limit_enabled"])
        self.desktop.aria2_download_kib_var.set("0")
        self.desktop.proxy_var.set("http://127.0.0.1:12345")
        self.desktop._save_settings()
        self.assertEqual(self.storage.get_settings(), values)
        self.messages.showwarning.assert_called_once()

    def test_failed_settings_commit_does_not_apply_speed_limit(self):
        self.desktop.speed_limit_enabled_var.set(True)
        self.desktop.speed_limit_kib_var.set("2")
        with patch.object(self.storage, "set_settings", side_effect=sqlite3.OperationalError("locked")):
            self.desktop._save_settings()
        self.assertEqual(MEDIA_BANDWIDTH.settings, (False, 1024 * 1024))

    def test_browser_session_limit_applies_only_after_successful_commit(self):
        self.desktop.browser_session_limit_var.set("2")
        with patch.object(self.storage, "set_settings", side_effect=sqlite3.OperationalError("locked")):
            self.desktop._save_settings()
        self.assertEqual(BROWSER_POOL.snapshot()[1], 3)
        self.assertEqual(self.storage.get_settings(), {})
        self.desktop._save_settings()
        self.assertEqual(BROWSER_POOL.snapshot()[1], 2)
        self.assertEqual(self.storage.get_setting("browser_session_limit"), 2)

    def test_invalid_browser_session_limit_rejects_entire_save(self):
        self.desktop.browser_session_limit_var.set("0")
        self.desktop._save_settings()
        self.assertEqual(self.storage.get_settings(), {})
        self.assertEqual(BROWSER_POOL.snapshot()[1], 3)

    def test_concurrency_limits_are_clamped_and_disabled_archive_cannot_delete_sources(self):
        self.desktop.platform_concurrency_vars["twitter"]["single"].set(20)
        self.desktop.platform_concurrency_vars["twitter"]["collection"].set(20)
        self.desktop.archive_mode_var.set("不压缩")
        self.desktop.archive_cleanup_sources_var.set(True)
        self.desktop._save_settings()
        values = self.storage.get_settings()
        self.assertEqual(values["task_concurrency_single_twitter"], 4)
        self.assertEqual(values["task_concurrency_collection_twitter"], 4)
        self.assertFalse(values["archive_cleanup_sources"])
        self.messages.askyesno.assert_not_called()

    def test_declining_bt_enable_does_not_write_settings(self):
        self.desktop.eh_bt_download_enabled_var.set(True)
        self.messages.askyesno.return_value = False
        self.desktop._save_settings()
        self.assertEqual(self.storage.get_settings(), {})

    def test_cookie_online_validation_runs_in_worker_and_finish_restores_button(self):
        desktop = self.desktop
        desktop.cookie_capture_platform_var = Value("E-Hentai 表站")
        desktop.cookie_capture_status_var = Value("")
        desktop.cookie_import_button = Mock()
        desktop._refresh_platform_tabs = Mock()
        desktop.ui_queue = queue.Queue()
        result = SimpleNamespace(cookie_count=2, destination=self.root / "cookies.json")
        with (
            patch("software_app.ui.settings_tab.filedialog.askopenfilename", return_value="cookies.txt"),
            patch("software_app.ui.settings_tab.cookie_capture_spec", return_value=object()),
            patch("software_app.ui.settings_tab.import_ehentai_cookie_file", return_value=result) as importer,
            patch("software_app.ui.settings_tab.threading.Thread") as thread,
        ):
            desktop._import_selected_cookie_file()
            importer.assert_not_called()
            thread.return_value.start.assert_called_once()
            thread.call_args.kwargs["target"]()
        importer.assert_called_once_with("E-Hentai 表站", "cookies.txt", proxy_url="http://127.0.0.1:7890")
        event, payload = desktop.ui_queue.get_nowait()
        self.assertEqual(event, "cookie_import_done")
        desktop._finish_cookie_file_import(payload, error=False)
        desktop.cookie_import_button.state.assert_called_with(["!disabled"])
        self.assertIn("已导入", desktop.status_var.get())

    def test_batch_write_rolls_back_when_any_row_fails(self):
        self.storage.set_setting("original", 1)
        with self.storage.connect() as conn:
            conn.execute(
                "CREATE TRIGGER reject_key BEFORE INSERT ON settings WHEN NEW.key='bad' BEGIN SELECT RAISE(ABORT, 'bad'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.storage.set_settings({"original": 2, "bad": True})
        self.assertEqual(self.storage.get_settings(), {"original": 1})

    def test_batch_read_ignores_corrupt_json_and_preserves_types(self):
        self.storage.set_settings({"enabled": False, "count": 7, "options": {"items": [1]}})
        with self.storage.connect() as conn:
            conn.execute("INSERT INTO settings(key, value_json) VALUES ('broken', '{')")
        self.assertEqual(self.storage.get_settings(), {"enabled": False, "count": 7, "options": {"items": [1]}})

    def test_new_task_options_read_one_consistent_snapshot(self):
        self.storage.set_settings({"image_output_format": "webp", "pixiv_max_works": 37, "rename_downloads": False})
        desktop = object.__new__(SoftwareDesktop)
        desktop.storage = self.storage
        desktop.module_var = Value("pixiv")
        desktop.content_scope_var = Value("作者 ID")
        desktop.types_var = Value("1")
        with patch.object(self.storage, "connect", wraps=self.storage.connect) as connect:
            options = desktop._current_target_options("123")
        self.assertEqual(connect.call_count, 1)
        self.assertEqual(options["max_works"], 37)
        self.assertEqual(options["image_format"], "webp")
        self.assertFalse(options["rename_downloads"])

    def test_recovery_scan_traverses_old_ancestors_and_changed_existing_files(self):
        parent = self.root / "old" / "chapter"
        parent.mkdir(parents=True)
        media = parent / "001.png"
        media.write_bytes(b"new")
        threshold = time.time() - 10
        for path in (parent, parent.parent):
            os.utime(path, (threshold - 100, threshold - 100))
        rows = self.storage.scan_media_files(self.root / "old", "test", modified_after=threshold)
        self.assertEqual([row.path for row in rows], [media.resolve()])


if __name__ == "__main__":
    unittest.main()
