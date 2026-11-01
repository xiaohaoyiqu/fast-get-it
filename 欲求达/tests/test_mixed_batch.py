from __future__ import annotations

import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.mixed_batch import BatchTaskPlan, REPORT_NAME, route_batch, start_batch, write_batch_report
from software_app.core.models import FileRecord, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.ui.desktop_support import retry_route_for_task
from software_app.ui.tk_app import SoftwareDesktop


class BatchRoutingTests(unittest.TestCase):
    def test_supported_platforms_and_scopes(self):
        cases = (
            ("https://mobile.twitter.com/Example/status/123/photo/1?s=20", "twitter", "post", "帖子 / 媒体 URL"),
            ("https://x.com/Example/media", "twitter", "user", "用户 / @用户名"),
            ("https://www.pixiv.net/en/artworks/123", "pixiv", "work", "作品 ID"),
            ("https://www.pixiv.net/users/123", "pixiv", "user", "作者 ID"),
            ("https://www.pixiv.net/novel/show.php?id=123", "pixiv", "novel", "小说 ID"),
            ("https://www.pixiv.net/novel/series/123", "pixiv", "novel_series", "小说系列"),
            ("https://www.pixiv.net/series/123", "pixiv", "manga_series", "漫画系列"),
            ("https://www.pixiv.net/users/123/bookmarks/artworks", "pixiv", "bookmark", "作品收藏"),
            ("https://www.pixiv.net/users/123/bookmarks/novels", "pixiv", "novel_bookmark", "小说收藏"),
            ("https://www.pixiv.net/history.php", "pixiv", "history", "浏览历史（Premium）"),
            ("https://www.fanbox.cc/@example/posts/123", "pixiv", "fanbox", "FANBOX"),
            ("https://sketch.pixiv.net/items/123", "pixiv", "sketch", "Sketch"),
            ("https://18comic.vip/album/123/title", "jmcomic", "album", "漫画 ID / 链接"),
            ("https://18comic.vip/photo/123", "jmcomic", "photo", "章节 ID / 链接"),
            ("https://bsky.app/profile/example.bsky.social/post/abc", "bluesky", "post", "帖子 / 媒体"),
            ("https://bsky.app/profile/example.bsky.social", "bluesky", "profile", "关注账号"),
            ("https://www.instagram.com/p/AbC_def/", "instagram", "post", "帖子 / 链接"),
            ("https://www.instagram.com/reel/AbC_def/", "instagram", "reel", "Reels / 链接"),
            ("https://www.instagram.com/Example/", "instagram", "profile", "账号 / 用户名"),
            ("https://e-hentai.org/g/123/abcdef1234/", "ehentai", "gallery", "画廊链接 / ID"),
            ("https://exhentai.org/?f_search=example", "ehentai", "search", "里站关键词（需登录）"),
            ("https://e-hentai.org/favorites.php?favcat=2", "ehentai", "favorites", "表站收藏夹（需登录）"),
            ("https://example.com/gallery", "website", "page", "网页"),
        )
        for target, module, kind, scope in cases:
            with self.subTest(target=target):
                item = route_batch(target)[0]
                self.assertEqual((item.state, item.module_id, item.kind, item.scope), ("ready", module, kind, scope))

    def test_known_hosts_with_unsupported_paths_never_fall_back_to_webpage(self):
        for target in ("https://x.com/search?q=foo", "https://www.pixiv.net/tags/foo", "https://18comic.vip/novel/123",
                       "https://instagram.com/accounts/login", "https://bsky.app/search?q=x", "https://e-hentai.org/s/token/123-1",
                       "https://sketch.pixiv.net/help", "https://www.fanbox.cc/", "https://pixiv.net/artworks/123/nonsense"):
            with self.subTest(target=target):
                self.assertEqual(route_batch(target)[0].state, "error")

    def test_host_matching_is_exact_and_unknown_hosts_have_visible_fallback(self):
        for target in ("https://x.com.example.org/user/status/1", "https://example.com/?url=https://pixiv.net/artworks/1",
                       "https://notpixiv.net/artworks/123"):
            item = route_batch(target)[0]
            self.assertEqual(item.module_id, "website")
            self.assertTrue(item.note)

    def test_malformed_private_and_credential_urls_are_rejected(self):
        for target in ("123", "@example", "file:///C:/data", "https://user:secret@example.com/x", "https://example.com:bad/",
                       "http://127.0.0.1/a", "http://[::1]/a", "https://localhost/a", "https://foo.local/a",
                       "https://example.com/a b", "https://x.com:8443/user/status/1", "https://example.com/\x00",
                       "https://example.com\\@x.com/user", "http://instagram.com/p/AbCd"):
            with self.subTest(target=target):
                self.assertEqual(route_batch(target)[0].state, "error")

    def test_duplicates_use_platform_identity_without_lowercasing_shortcodes(self):
        routes = route_batch("\n".join(("https://x.com/foo/status/123?s=1", "https://twitter.com/bar/status/123#fragment",
            "https://www.pixiv.net/en/artworks/456", "https://pixiv.net/artworks/456?ref=x",
            "https://instagram.com/p/AbC/", "https://www.instagram.com/reel/AbC/", "https://instagram.com/p/abc/")))
        self.assertEqual([row.state for row in routes], ["ready", "duplicate", "ready", "duplicate", "ready", "duplicate", "ready"])
        self.assertEqual(routes[4].target, "https://www.instagram.com/p/AbC/")
        self.assertEqual(routes[0].target, "https://x.com/foo/status/123")
        for first, second in (("https://www.fanbox.cc/@creator/posts/123", "https://creator.fanbox.cc/posts/123?ref=share"),
                              ("https://sketch.pixiv.net/items/123?ref=share", "https://sketch.pixiv.net/items/123")):
            self.assertEqual([item.state for item in route_batch(first + "\n" + second)], ["ready", "duplicate"])

    def test_query_urls_are_not_split_and_filters_are_preserved(self):
        targets = ["https://example.com/a?next=/A/B&label=a、b&token=secret", "https://e-hentai.org/favorites.php?favcat=2&f_search=Foo"]
        self.assertEqual([row.target for row in route_batch("\n".join(targets))], targets)

    def test_custom_jm_host_and_module_availability(self):
        item = route_batch("https://jm.example.org/album/42", jm_domain="https://jm.example.org")[0]
        self.assertEqual(item.module_id, "jmcomic")
        self.assertEqual(route_batch("https://x.com/foo", enabled_modules={"website"})[0].state, "error")

    def test_routing_is_offline(self):
        with patch("requests.sessions.Session.request", side_effect=AssertionError("network")), patch("socket.getaddrinfo", side_effect=AssertionError("DNS")):
            routes = route_batch("https://pixiv.net/artworks/1\nhttps://instagram.com/p/AbC/\nhttps://example.com/a")
        self.assertTrue(all(item.state == "ready" for item in routes))

    def test_input_limits_and_original_line_numbers(self):
        for text in ("", "\n", "x" * 200001, "\n".join(f"https://example.com/{i}" for i in range(101))):
            with self.assertRaises(ValueError):
                route_batch(text)
        self.assertEqual([item.line for item in route_batch("\n https://x.com/foo \n\n https://x.com/bar")], [2, 4])

    def test_sketch_items_use_single_item_concurrency(self):
        self.assertEqual(TaskManager._concurrency_group("pixiv", "https://sketch.pixiv.net/items/123", {"input_kind": "sketch"}), "single")

    def test_explicit_platform_options_do_not_read_or_modify_current_ui_scope(self):
        desktop = object.__new__(SoftwareDesktop)
        desktop.storage = Mock()
        desktop.module_var = Mock()
        desktop.content_scope_var = Mock()
        desktop.bookmark_filter_start_var = Mock()
        desktop.bookmark_filter_end_var = Mock()
        desktop.bookmark_date_basis_var = Mock()
        desktop.manager = Mock()
        desktop.manager.get_adapter.return_value.cookie_account_id.return_value = "123"
        desktop._selected_types = Mock(return_value="1 2")
        options = desktop._current_target_options("https://pixiv.net/users/123/bookmarks/artworks", module_id="pixiv",
            scope="作品收藏", settings={"pixiv_start_date": "2026-01-01", "archive_mode": "folder"}, use_view_filters=False)
        self.assertEqual((options["input_kind"], options["start_date"], options["archive_mode"]), ("bookmark", "2026-01-01", "folder"))
        desktop.module_var.get.assert_not_called()
        desktop.content_scope_var.get.assert_not_called()
        desktop.bookmark_filter_start_var.get.assert_not_called()
        desktop.bookmark_date_basis_var.get.assert_not_called()
        desktop.storage.get_settings.assert_not_called()


class LocalAdapter(CrawlerAdapter):
    def __init__(self, module_id, entered=None, release=None):
        self.info = ModuleInfo(module_id, module_id)
        self.entered, self.release = entered, release

    def preview_target(self, raw_target, options=None):
        return TargetPreview(self.module_id, raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        if self.entered:
            self.entered.set()
        if self.release and not self.release.wait(5):
            raise RuntimeError("test timed out")
        if "fail" in task.target:
            raise ValueError("test failure")
        task.output_dir.mkdir(parents=True, exist_ok=True)
        path = task.output_dir / "sample.txt"
        path.write_text(task.target, encoding="utf-8")
        callbacks.on_file(FileRecord(path, self.module_id, task.task_id, "text", path.stat().st_size))


class BatchExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = AppStorage(self.root / "app.db")
        self.manager = TaskManager(self.storage)
        self.manager.register_adapter(LocalAdapter("twitter"))
        self.manager.register_adapter(LocalAdapter("website"))
        self.addCleanup(self.manager.shutdown)
        self.batch_id = uuid.uuid4().hex
        self.output = self.root / "batch"

    def plans(self, text="https://x.com/foo\nhttps://example.com/a"):
        return [BatchTaskPlan(item, {"discovery_source": "mixed_batch", "retries": 0}) for item in route_batch(text)]

    def finish(self, result):
        for task_id in result.task_ids:
            self.manager.wait(task_id, 8)
        self.assertFalse(self.manager.active_task_ids())
        # A task is removed before its report/callback finish; use an explicit final snapshot too.
        write_batch_report(self.storage, self.batch_id)

    def test_multiple_platforms_queue_and_finish_in_independent_directories(self):
        result = start_batch(self.manager, self.plans(), self.output, self.batch_id)
        self.finish(result)
        self.assertEqual((len(result.task_ids), result.failures), (2, 0))
        text = result.report.read_text(encoding="utf-8-sig")
        self.assertIn("已完成 2", text)
        self.assertTrue((self.output / "001_twitter" / "sample.txt").is_file())
        self.assertTrue((self.output / "002_website" / "sample.txt").is_file())
        self.assertNotIn(str(result.report), [str(row["path"]) for row in self.storage.list_files(limit=100)])
        self.assertEqual(len(self.storage.list_files(limit=100)), 2)

    def test_failed_enqueue_does_not_block_later_platform(self):
        original = self.manager.start_task

        def start(module_id, *args, **kwargs):
            if module_id == "twitter":
                raise ValueError("cannot start")
            return original(module_id, *args, **kwargs)

        with patch.object(self.manager, "start_task", side_effect=start):
            result = start_batch(self.manager, self.plans(), self.output, self.batch_id)
        self.finish(result)
        _, items = self.storage.batch_snapshot(self.batch_id)
        self.assertEqual([item["status"] for item in items], ["enqueue_failed", "completed"])
        self.assertEqual(result.failures, 1)

    def test_stop_only_prevents_remaining_enqueue(self):
        stop = threading.Event()
        original = self.manager.start_task

        def start(*args, **kwargs):
            task = original(*args, **kwargs)
            stop.set()
            return task

        with patch.object(self.manager, "start_task", side_effect=start):
            result = start_batch(self.manager, self.plans(), self.output, self.batch_id, stop_enqueuing=stop)
        self.finish(result)
        self.assertEqual((len(result.task_ids), result.failures), (1, 1))
        self.assertEqual(self.storage.get_task(result.task_ids[0])["status"], "completed")

    def test_platform_slots_are_independent(self):
        release, twitter, website = threading.Event(), threading.Event(), threading.Event()
        self.manager.register_adapter(LocalAdapter("twitter", twitter, release))
        self.manager.register_adapter(LocalAdapter("website", website, release))
        try:
            result = start_batch(self.manager, self.plans(), self.output, self.batch_id)
            self.assertTrue(twitter.wait(3))
            self.assertTrue(website.wait(3))
            self.assertTrue(self.manager.pause(result.task_ids[0]))
            self.assertIn("已暂停", result.report.read_text(encoding="utf-8-sig"))
            self.assertTrue(self.manager.resume(result.task_ids[0]))
        finally:
            release.set()
        self.finish(result)

    def test_batch_binding_is_atomic_and_cannot_be_reused(self):
        self.storage.create_batch(self.batch_id, str(self.output), [(0, "twitter", "https://x.com/foo")])
        options = {"_batch_id": self.batch_id, "_batch_position": 0}
        self.storage.create_task("first", "twitter", "https://x.com/foo", str(self.output), options)
        with self.assertRaises(ValueError):
            self.storage.create_task("second", "twitter", "https://x.com/foo", str(self.output), options)
        self.assertIsNone(self.storage.get_task("second"))
        self.assertEqual(self.storage.batch_snapshot(self.batch_id)[1][0]["task_id"], "first")

    def test_existing_output_folder_is_never_reused(self):
        self.output.mkdir()
        sentinel = self.output / REPORT_NAME
        sentinel.write_text("keep", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            start_batch(self.manager, self.plans(), self.output, self.batch_id)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertFalse(self.storage.list_tasks())

    def test_report_redacts_sensitive_query_and_error(self):
        target = "https://example.com/a?token=supersecret&label=hello"
        result = start_batch(self.manager, self.plans(target), self.output, self.batch_id)
        self.finish(result)
        self.storage.update_task_status(result.task_ids[0], "failed", "failure " + target)
        text = write_batch_report(self.storage, self.batch_id).read_text(encoding="utf-8-sig")
        self.assertNotIn("supersecret", text)
        self.assertIn("label=hello", text)

    def test_final_snapshot_survives_deleting_task_history(self):
        result = start_batch(self.manager, self.plans(), self.output, self.batch_id)
        self.finish(result)
        self.storage.clear_history()
        self.assertEqual([item["status"] for item in self.storage.batch_snapshot(self.batch_id)[1]], ["completed", "completed"])
        self.assertIn("已完成 2", write_batch_report(self.storage, self.batch_id).read_text(encoding="utf-8-sig"))

    def test_retry_detaches_from_original_batch_and_preserves_route(self):
        result = start_batch(self.manager, self.plans("https://example.com/fail"), self.output, self.batch_id)
        self.finish(result)
        retry = self.manager.retry_task(result.task_ids[0])
        self.manager.wait(retry.task_id, 5)
        self.assertNotIn("_batch_id", retry.options)
        self.assertEqual(self.storage.batch_snapshot(self.batch_id)[1][0]["task_id"], result.task_ids[0])
        module, options = retry_route_for_task("website", retry.target, {**retry.options, "max_files": 50, "page_read_timeout": 60,
            "_batch_id": self.batch_id, "_batch_position": 0})
        self.assertEqual((module, options["discovery_source"]), ("website", "mixed_batch"))
        self.assertNotIn("_batch_id", options)
        self.assertNotIn("browser_render", options)

    def test_report_write_failure_does_not_change_task_outcome(self):
        task = self.manager.start_task("website", "https://example.com/a", self.root / "out", callbacks=CallbackSet())
        self.manager.wait(task.task_id, 5)
        task.options["_batch_id"] = "missing"
        self.manager._refresh_batch_report(task)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")

    def test_dead_owner_recovery_and_pending_items_never_auto_start(self):
        self.output.mkdir()
        self.storage.create_batch(self.batch_id, str(self.output), [(0, "twitter", "https://x.com/foo"), (1, "website", "https://example.com/a")])
        self.storage.create_task("orphan", "twitter", "https://x.com/foo", str(self.output),
            {"_batch_id": self.batch_id, "_batch_position": 0}, owner_id="a" * 32)
        self.assertIn(self.batch_id, self.storage.unfinished_batch_ids())
        self.assertEqual(self.storage.recover_interrupted_tasks(), 1)
        _, items = self.storage.batch_snapshot(self.batch_id)
        self.assertEqual([item["status"] for item in items], ["cancelled", "pending"])
        self.assertFalse(self.manager.active_task_ids())

    def test_task_options_are_snapshot_copies(self):
        plans = self.plans()
        before = json.dumps([plan.options for plan in plans])
        result = start_batch(self.manager, plans, self.output, self.batch_id)
        self.finish(result)
        self.assertEqual(json.dumps([plan.options for plan in plans]), before)


if __name__ == "__main__":
    unittest.main()
