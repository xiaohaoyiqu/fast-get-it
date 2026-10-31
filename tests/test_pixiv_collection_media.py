from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from PIL import Image

from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.core.models import TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.core.media_export import unfinished_media_snapshot
from software_app.crawlers.common import MediaDownloadResult
from software_app.crawlers.pixiv.client import PixivCrawler


class PixivCollectionMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.storage = AppStorage(self.root / "state.sqlite")
        self.manager = TaskManager(self.storage)
        self.addCleanup(lambda: self.manager.shutdown(3))
        self.crawler = object.__new__(PixivCrawler)
        self.crawler.session = Mock()
        self.ids = ["11", "12"]
        self.target = "https://www.pixiv.net/users/9/bookmarks/artworks"
        self.details = {
            work_id: {
                "illustTitle": "Work " + work_id,
                "userId": "1" + work_id,
                "userName": "Artist",
                "illustType": 0,
                "aiType": 0,
            }
            for work_id in ("11", "12", "13")
        }
        self.pages = {
            work_id: [
                {"urls": {"original": f"https://i.pximg.net/img-original/{work_id}_p{index}.jpg?sig=old"}}
                for index in range(count)
            ]
            for work_id, count in (("11", 2), ("12", 1))
        }
        self.calls, self.transfers = [], []
        self.failed_pages = {("11", 2)}
        self.discovery_failure = None
        self.cancel_after = None
        self.fail_once = False
        self.animation_fail = True
        self.animation_transfers = []
        self.animation_path = "13"
        self.crawler._body = self.body
        self.crawler.search = Mock(side_effect=self.discover)
        adapter = PixivNativeAdapter()
        adapter._client = lambda _options=None: self.crawler
        adapter.preview_target = Mock(side_effect=lambda target, _options=None: TargetPreview("pixiv", target, target))
        self.adapter = adapter
        self.manager.register_adapter(adapter)
        transfer = patch("software_app.crawlers.pixiv.media.download_media_file", side_effect=self.transfer)
        transfer.start()
        self.addCleanup(transfer.stop)

    def discover(self, *_args, **_kwargs):
        self.crawler.last_bookmark_status = {"complete": True, "scopes": [{"reason": "exhausted", "complete": True}]}
        return [{"id": work_id} for work_id in self.ids]

    def body(self, path):
        self.calls.append(path)
        work_id = re.search(r"/illust/(\d+)", path)[1]
        if work_id == self.discovery_failure:
            raise requests.Timeout("generated discovery timeout")
        if path.endswith("/ugoira_meta"):
            return {
                "originalSrc": f"https://i.pximg.net/img-zip-ugoira/{self.animation_path}.zip?sig=old",
                "frames": [{"file": "000000.jpg", "delay": 100}],
            }
        return copy.deepcopy(self.pages[work_id] if path.endswith("/pages") else self.details[work_id])

    def transfer(self, _session, url, _source, destination, _cancel, _suffix, **options):
        work_id = re.search(r"/(\d+)_p", url)[1]
        index = int(destination.stem)
        self.assertTrue(options["verify_image"])
        row = next(row for row in self.storage.list_tasks() if row["status"] == "running")
        # Discovery of every work has finished and its complete media plan is durable.
        self.assertTrue(self.storage.media_summary(row["task_id"]))
        if not self.transfers:
            self.assertIn("/ajax/illust/12/pages", self.calls)
        self.transfers.append((work_id, index, destination, url))
        if (work_id, index) in self.failed_pages:
            if self.fail_once:
                self.failed_pages.remove((work_id, index))
            raise requests.Timeout("generated image timeout")
        destination.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (4, 4), "blue").save(destination, format="PNG")
        if self.cancel_after and len(self.transfers) == self.cancel_after:
            self.manager.cancel(row["task_id"])
        return MediaDownloadResult(destination.stat().st_size, False, destination)

    def start(self, **options):
        task = self.manager.start_task("pixiv", self.target, self.root, {"filter_ai": True, "retries": 0, **options})
        self.manager.wait(task.task_id, 10)
        self.assertNotIn(task.task_id, self.manager.running)
        return task

    def retry(self, task, **options):
        child = self.manager.retry_task(task.task_id, failed_only=True, **options)
        self.manager.wait(child.task_id, 10)
        return child

    def test_partial_collection_has_per_work_page_results_and_export(self):
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 2, "failed": 1})
        self.assertEqual(self.storage.collection_plan(task.task_id)["ids"], ["11", "12"])
        exported = unfinished_media_snapshot(self.storage, [task.task_id])
        self.assertEqual(
            [(r["page_url"], r["media_index"]) for r in exported], [("https://www.pixiv.net/artworks/11", 2)]
        )

    def test_only_failed_page_retries_with_original_plan_folder_and_settings(self):
        task = self.start(image_format="original", file_conflict="rename")
        original = self.storage.media_results(task.task_id, unfinished=True)[0]
        self.failed_pages.clear()
        self.ids = ["99"]
        self.details["11"].update(illustTitle="Renamed", userName="Changed")
        self.pages["11"][1]["urls"]["original"] = self.pages["11"][1]["urls"]["original"].replace("old", "new")
        self.calls.clear()
        with patch(
            "software_app.crawlers.pixiv.collection_media.platform_output_root",
            side_effect=AssertionError("new directory"),
        ):
            child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "completed")
        self.assertEqual([(r[0], r[1]) for r in self.transfers], [("11", 1), ("11", 2), ("12", 1), ("11", 2)])
        self.assertEqual(self.transfers[-1][2].parent, (self.root / original["folder"]).resolve())
        self.assertTrue(str(self.transfers[-1][2]).endswith(".jpg"))
        self.assertEqual(self.storage.collection_plan(child.task_id)["ids"], ["11", "12"])
        self.assertNotIn("/ajax/illust/12", self.calls)
        self.assertEqual(self.storage.media_summary(task.task_id)["repaired"], 1)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")

    def test_discovery_failure_has_no_incomplete_media_plan_or_transfer(self):
        self.discovery_failure = "12"
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertEqual(self.storage.media_results(task.task_id), [])
        self.assertEqual(self.transfers, [])
        self.discovery_failure = None
        self.ids = ["99"]
        self.failed_pages.clear()
        child = self.manager.retry_task(task.task_id)
        self.manager.wait(child.task_id, 10)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "completed")
        self.assertEqual(self.crawler.search.call_count, 1)
        self.assertEqual({row[0] for row in self.transfers}, {"11", "12"})

    def test_plan_commit_failure_prevents_all_transfers(self):
        with patch.object(self.storage, "record_media_results", side_effect=ValueError("generated commit failure")):
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertEqual(self.transfers, [])

    def test_cancelled_task_has_all_pending_pages_and_restores_without_saved_page(self):
        self.failed_pages.clear()
        self.cancel_after = 1
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 1, "pending": 2})
        with self.storage.connect() as conn:
            conn.execute("UPDATE tasks SET status='running',owner_id='' WHERE task_id=?", (task.task_id,))
        self.storage.recover_interrupted_tasks()
        self.cancel_after = None
        self.ids = ["99"]
        restored = self.manager.restore_interrupted_task(task.task_id)
        self.manager.wait(restored.task_id, 10)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        self.assertCountEqual([(r[0], r[1]) for r in self.transfers], [("11", 1), ("11", 2), ("12", 1)])

    def test_retry_rechecks_ai_blacklist_type_and_page_identity(self):
        task = self.start()
        before = len(self.transfers)
        self.details["11"]["aiType"] = 2
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.details["11"]["aiType"] = 0
        self.manager.blocklist.add_group(("pixiv", "111"))
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(len(self.transfers), before)

    def test_changed_page_path_does_not_download_another_resource(self):
        task = self.start()
        before = len(self.transfers)
        self.pages["11"][1]["urls"]["original"] = "https://i.pximg.net/other.jpg"
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(len(self.transfers), before)

    def test_out_of_scope_record_is_rejected_before_metadata_request(self):
        task = self.start()
        item = self.storage.media_results(task.task_id, unfinished=True)[0]
        self.storage.record_media_result(task.task_id, {**item, "target": "https://www.pixiv.net/artworks/99"})
        self.calls.clear()
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_total_page_limit_fails_before_transfer_or_media_registration(self):
        many = [self.pages["11"][0]] * 6000
        with patch("software_app.crawlers.pixiv.collection_media._pages", return_value=many):
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertEqual(self.transfers, [])
        self.assertEqual(self.storage.media_results(task.task_id), [])

    def animation(self, _session, body, folder, work_id, _options, _cancel):
        self.animation_transfers.append((work_id, folder))
        if self.animation_fail:
            raise requests.Timeout("generated animation timeout")
        folder.mkdir(parents=True, exist_ok=True)
        primary, companion = folder / f"{work_id}.ugoira.zip", folder / f"{work_id}.ugoira.json"
        primary.write_bytes(b"generated archive")
        companion.write_text("{}", encoding="utf-8")
        return [(primary, "animation", body["originalSrc"]), (companion, "metadata", body["originalSrc"])]

    def test_mixed_collection_retries_animation_group_with_companion_and_original_folder(self):
        self.ids.append("13")
        self.details["13"]["illustType"] = 2
        self.failed_pages.clear()
        with patch("software_app.crawlers.pixiv.collection_media.download_ugoira", side_effect=self.animation):
            task = self.start(animation_format="original")
            self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 3, "failed": 1})
            self.animation_fail = False
            self.details["13"].update(illustTitle="Changed", userName="Renamed")
            child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "completed")
        self.assertEqual(len(self.transfers), 3)
        self.assertEqual(self.animation_transfers[0][1], self.animation_transfers[1][1])
        self.assertEqual(len(self.storage.task_file_records(child.task_id)), 2)
        self.assertEqual(self.storage.media_summary(task.task_id)["repaired"], 1)

    def test_changed_animation_source_does_not_transfer(self):
        self.ids.append("13")
        self.details["13"]["illustType"] = 2
        self.failed_pages.clear()
        with patch("software_app.crawlers.pixiv.collection_media.download_ugoira", side_effect=self.animation):
            task = self.start()
            self.animation_fail = False
            self.animation_path = "other"
            child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(len(self.animation_transfers), 1)

    def test_automatic_retry_keeps_range_and_refreshes_registered_media_only(self):
        self.failed_pages = {("11", 1), ("11", 2), ("12", 1)}
        self.fail_once = True
        with patch("software_app.core.task_manager.task_wait", return_value=False):
            task = self.start(retries=1)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 3})
        self.assertEqual(self.crawler.search.call_count, 1)
        self.assertEqual(len(self.transfers), 6)

    def test_metadata_failure_is_cached_for_all_failed_pages_of_same_work(self):
        self.failed_pages = {("11", 1), ("11", 2)}
        task = self.start()
        self.discovery_failure = "11"
        self.calls.clear()
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(self.calls, ["/ajax/illust/11"])
        self.assertEqual(self.storage.media_summary(child.task_id), {"failed": 2})

    def test_selected_page_retry_does_not_repair_unselected_failures(self):
        self.failed_pages = {("11", 1), ("11", 2)}
        task = self.start()
        selected = self.storage.media_results(task.task_id, unfinished=True)[0]
        self.failed_pages.clear()
        child = self.retry(task, media_keys=[selected["key"]])
        self.assertEqual(self.storage.media_summary(child.task_id), {"saved": 1})
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 1, "failed": 1, "repaired": 1})
        self.assertEqual(child.options["archive_mode"], "none")
        self.assertFalse(child.options["archive_cleanup_sources"])

    def test_static_to_animation_change_is_rejected(self):
        task = self.start()
        self.details["11"]["illustType"] = 2
        before = len(self.transfers)
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(len(self.transfers), before)

    def test_discovery_cancellation_does_not_register_or_download_a_subset(self):
        def body(path):
            result = self.body(path)
            if path == "/ajax/illust/12/pages":
                row = next(row for row in self.storage.list_tasks() if row["status"] == "running")
                self.manager.cancel(row["task_id"])
            return result

        self.crawler._body = body
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "cancelled")
        self.assertEqual(self.storage.media_results(task.task_id), [])
        self.assertEqual(self.transfers, [])

    def test_current_settings_retry_can_select_a_new_collection_range(self):
        self.discovery_failure = "12"
        task = self.start()
        self.discovery_failure = None
        self.ids = ["12"]
        child = self.manager.retry_task(task.task_id, use_current_settings=True)
        self.manager.wait(child.task_id, 10)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "completed")
        self.assertEqual(self.storage.collection_plan(child.task_id)["ids"], ["12"])
        self.assertEqual(self.storage.collection_plan(task.task_id)["ids"], ["11", "12"])

    def test_failed_only_retry_without_original_plan_never_rediscovers(self):
        task = self.start()
        with self.storage.connect() as conn:
            conn.execute("DELETE FROM task_collections WHERE task_id=?", (task.task_id,))
        self.calls.clear()
        child = self.retry(task)
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.crawler.search.call_count, 1)

    def test_partial_collection_and_subset_never_archive_or_remove_original_sources(self):
        with patch("software_app.core.task_manager.run_archive_postprocess") as archive:
            task = self.start(archive_mode="task", extract_archives=True, archive_cleanup_sources=True)
            self.failed_pages.clear()
            child = self.retry(task)
        archive.assert_not_called()
        self.assertEqual(self.storage.get_task(child.task_id)["status"], "completed")
        self.assertEqual(child.options["archive_mode"], "none")
        self.assertFalse(child.options["extract_archives"])
        self.assertFalse(child.options["archive_cleanup_sources"])

    def test_current_settings_does_not_silently_expand_a_subset_retry(self):
        task = self.start()
        child = self.retry(task)
        count = len(self.storage.list_tasks())
        with self.assertRaisesRegex(ValueError, "原.*任务"):
            self.manager.retry_task(child.task_id, use_current_settings=True)
        with self.assertRaisesRegex(ValueError, "原.*任务"):
            self.manager.retry_task(task.task_id, use_current_settings=True, failed_only=True)
        self.assertEqual(len(self.storage.list_tasks()), count)


class PixivSeriesMediaTests(PixivCollectionMediaTests):
    """The same recovery contract must hold for a series, independent of bookmarks."""

    def setUp(self):
        super().setUp()
        self.target = "https://www.pixiv.net/user/9/series/7"
        self.crawler._catalog = Mock(return_value=Mock(manga_series=self.crawler.search))

    def test_series_scope_commits_before_details_and_commit_failure_stops_requests(self):
        with patch.object(self.storage, "record_collection_plan", side_effect=ValueError("cannot commit series")):
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transfers, [])

    def test_series_plan_uses_series_identity_and_rejects_other_series(self):
        task = self.start()
        plan = self.storage.collection_plan(task.task_id)
        self.assertEqual((plan["kind"], plan["owner_id"]), ("manga_series", "7"))
        from software_app.core.collection_plan import restored_collection_ids

        with self.assertRaises(ValueError):
            restored_collection_ids(
                type(task)(
                    task.task_id, "pixiv", task.target, task.output_dir, {**task.options, "_collection_plan": plan}
                ),
                "manga_series",
                "8",
            )

    def test_real_series_pagination_is_fixed_before_work_discovery(self):
        from software_app.crawlers.pixiv.catalog import PixivCatalog

        def body(path):
            if path.startswith("/ajax/series/7?"):
                self.calls.append(path)
                ids = ["11"] if "?p=1&" in path else ["12"]
                return {"page": {"total": 2, "series": [{"workId": value} for value in ids]}}
            row = next(row for row in self.storage.list_tasks() if row["status"] == "running")
            self.assertEqual(self.storage.collection_plan(row["task_id"])["ids"], ["11", "12"])
            return self.body(path)

        self.crawler._catalog = lambda: PixivCatalog(body, Mock())
        self.crawler._body = body
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(len([path for path in self.calls if path.startswith("/ajax/series/")]), 2)
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 2, "failed": 1})

    def test_incomplete_series_pagination_never_commits_a_partial_selection(self):
        from software_app.crawlers.pixiv.catalog import PixivCatalog

        def body(path):
            if "?p=1&" in path:
                return {"page": {"total": 2, "series": [{"workId": "11"}]}}
            raise requests.Timeout("generated series page failure")

        self.crawler._catalog = lambda: PixivCatalog(body, Mock())
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertIsNone(self.storage.collection_plan(task.task_id))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transfers, [])


class PixivSearchMediaTests(PixivCollectionMediaTests):
    def setUp(self):
        super().setUp()
        self.target = "tag 123 空"

    def test_search_identity_uses_whole_keyword_and_scope_commit_precedes_details(self):
        task = self.start(age_mode="safe", minimum_bookmarks=10)
        plan = self.storage.collection_plan(task.task_id)
        self.assertEqual((plan["kind"], plan["owner_id"]), ("search", hashlib.sha256(self.target.encode()).hexdigest()))
        self.assertEqual(plan["ids"], ["11", "12"])
        with patch.object(self.storage, "record_collection_plan", side_effect=ValueError("cannot commit search")):
            self.calls.clear()
            failed = self.start()
        self.assertEqual(self.storage.get_task(failed.task_id)["status"], "failed")
        self.assertEqual(self.calls, [])

    def test_search_filter_change_rejects_original_retry_before_creating_task(self):
        task = self.start(age_mode="safe", minimum_bookmarks=10)
        count = len(self.storage.list_tasks())
        for overrides in ({"age_mode": "r18"}, {"minimum_bookmarks": 20}, {"search_mode": "标题 / 简介"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.manager.retry_task(task.task_id, option_overrides=overrides)
        self.assertEqual(len(self.storage.list_tasks()), count)

    def test_search_pages_and_filters_commit_before_media_requests(self):
        from urllib.parse import parse_qs, urlsplit

        paths = []

        def body(path):
            if path.startswith("/ajax/search/artworks/"):
                paths.append(path)
                query = parse_qs(urlsplit(path).query)
                self.assertEqual((query["mode"], query["blt"], query["s_mode"]), (["safe"], ["10"], ["s_tc"]))
                return {"illustManga": {"data": [{"id": "11" if query["p"] == ["1"] else "12"}], "total": 2}}
            row = next(row for row in self.storage.list_tasks() if row["status"] == "running")
            self.assertEqual(self.storage.collection_plan(row["task_id"])["ids"], ["11", "12"])
            return self.body(path)

        self.crawler.search = PixivCrawler.search.__get__(self.crawler)
        self.crawler._body, self.crawler._json = body, Mock()
        task = self.start(search_mode="标题 / 简介", age_mode="safe", minimum_bookmarks=10)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(len(paths), 2)
        self.assertEqual(self.storage.media_summary(task.task_id), {"saved": 2, "failed": 1})

    def test_search_page_failure_does_not_commit_partial_scope(self):
        def body(path):
            if "&p=1&" in path:
                return {"illustManga": {"data": [{"id": "11"}], "total": 2}}
            raise requests.Timeout("generated search pagination failure")

        self.crawler.search = PixivCrawler.search.__get__(self.crawler)
        self.crawler._body, self.crawler._json = body, Mock()
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertIsNone(self.storage.collection_plan(task.task_id))
        self.assertEqual(self.storage.media_results(task.task_id), [])
        self.assertEqual(self.transfers, [])

    def test_corrupt_search_identity_is_rejected(self):
        from software_app.core.collection_plan import validated_collection_plan

        task = self.start()
        plan = self.storage.collection_plan(task.task_id)
        plan["owner_id"] = hashlib.sha256(b"different keyword 123").hexdigest()
        with self.assertRaisesRegex(ValueError, "身份"):
            validated_collection_plan(plan, "pixiv", self.target, task.options)

    def test_search_plan_cannot_be_reused_for_author_or_novel_search(self):
        from software_app.core.collection_plan import validated_collection_plan

        task = self.start()
        plan = self.storage.collection_plan(task.task_id)
        for mode in ("作者搜索", "小说搜索", "小说系列", "FANBOX", "Sketch"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "搜索类型"):
                validated_collection_plan(plan, "pixiv", self.target, {**task.options, "search_mode": mode})


class PixivRankingMediaTests(PixivCollectionMediaTests):
    def setUp(self):
        super().setUp()
        self.target = "daily 漫画 20261030"

    def start(self, **options):
        return super().start(input_kind="ranking", **options)

    def test_ranking_identity_includes_mode_content_and_date(self):
        task = self.start()
        plan = self.storage.collection_plan(task.task_id)
        self.assertEqual(
            (plan["kind"], plan["owner_id"]), ("ranking", hashlib.sha256(self.target.encode()).hexdigest())
        )

    def test_ranking_real_pagination_commits_before_media_requests(self):
        from urllib.parse import parse_qs, urlsplit

        paths = []

        def ranking(url):
            paths.append(url)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual((query["mode"], query["content"], query["date"]), (["daily"], ["manga"], ["20261030"]))
            first = query["p"] == ["1"]
            return {"contents": [{"illust_id": "11" if first else "12"}], "next": 2 if first else False, "total": 2}

        def body(path):
            row = next(row for row in self.storage.list_tasks() if row["status"] == "running")
            self.assertEqual(self.storage.collection_plan(row["task_id"])["ids"], ["11", "12"])
            return self.body(path)

        self.crawler.search = PixivCrawler.search.__get__(self.crawler)
        self.crawler._json, self.crawler._body = ranking, body
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "partial")
        self.assertEqual(len(paths), 2)

    def test_ranking_pagination_failure_does_not_commit_partial_scope(self):
        def ranking(url):
            if "p=1&" in url:
                return {"contents": [{"illust_id": "11"}], "next": 2, "total": 2}
            raise requests.Timeout("generated ranking pagination failure")

        self.crawler.search = PixivCrawler.search.__get__(self.crawler)
        self.crawler._json = ranking
        task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertIsNone(self.storage.collection_plan(task.task_id))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.transfers, [])
