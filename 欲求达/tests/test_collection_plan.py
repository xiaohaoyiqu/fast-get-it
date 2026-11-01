from __future__ import annotations

import copy
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.core.collection_plan import selection_key
from software_app.core.models import DownloadTask, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.pixiv.client import PixivCrawler


class CollectionPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.storage = AppStorage(self.root / "state.db")
        self.manager = TaskManager(self.storage)
        self.addCleanup(lambda: self.manager.shutdown(3))
        self.crawler = object.__new__(PixivCrawler)
        self.crawler.session = Mock()
        self.crawler.search = Mock(side_effect=self.discover)
        self.crawler._body = Mock(side_effect=self.details)
        self.crawler._download_novel_target = Mock(wraps=self.crawler._download_novel_target)
        self.adapter = PixivNativeAdapter()
        self.adapter._client = lambda options=None: self.crawler
        self.adapter.preview_target = Mock(
            side_effect=lambda target, options=None: TargetPreview("pixiv", target, target)
        )
        self.manager.register_adapter(self.adapter)
        self.ids = ["11", "12"]
        self.target = "https://www.pixiv.net/users/9/bookmarks/artworks"
        self.fail_once = False
        self.observed = []

    def discover(self, *_args, **_kwargs):
        self.crawler.last_bookmark_status = {"complete": True, "scopes": [{"reason": "exhausted", "complete": True}]}
        return [{"id": work_id} for work_id in self.ids]

    def details(self, path):
        if "/ajax/illust/" not in path or path.endswith("/pages"):
            raise AssertionError(f"Unexpected request: {path}")
        task_id = self.active_task_id()
        persisted = AppStorage(self.storage.db_path).collection_plan(task_id)
        self.assertEqual(persisted["ids"], ["11", "12"])
        self.observed.append(path)
        if self.fail_once:
            self.fail_once = False
            # A remote collection changed after the first attempt.
            self.ids = ["99"]
            raise requests.Timeout("interrupted after selection commit")
        return {"illustType": 0, "aiType": 2, "illustTitle": "Filtered", "userId": "1"}

    def active_task_id(self):
        return next(row["task_id"] for row in self.storage.list_tasks() if row["status"] == "running")

    def start(self, **options):
        task = self.manager.start_task("pixiv", self.target, self.root, {"filter_ai": True, **options})
        self.manager.wait(task.task_id, 5)
        self.assertNotIn(task.task_id, self.manager.running)
        return task

    def seed(self, status="failed"):
        self.storage.create_task("old", "pixiv", self.target, str(self.root), {"filter_ai": True})
        plan = {
            "version": 1,
            "module_id": "pixiv",
            "target": self.target,
            "kind": "bookmark",
            "owner_id": "9",
            "ids": ["11", "12"],
            "selection_key": selection_key({"filter_ai": True}),
        }
        self.storage.record_collection_plan("old", plan)
        self.storage.update_task_status("old", status)
        return plan

    def test_plan_commits_before_first_work_and_retry_keeps_original_ids(self):
        self.fail_once = True
        with patch("software_app.core.task_manager.task_wait", return_value=False):
            task = self.start(retries=1)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        self.crawler.search.assert_called_once()
        self.assertEqual(self.observed, ["/ajax/illust/11", "/ajax/illust/11", "/ajax/illust/12"])

    def test_original_retry_copies_plan_and_skips_collection_preview(self):
        self.seed()
        task = self.manager.retry_task("old")
        self.manager.wait(task.task_id, 5)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")
        self.assertEqual(self.storage.collection_plan(task.task_id)["ids"], ["11", "12"])
        self.crawler.search.assert_not_called()
        self.adapter.preview_target.assert_not_called()

    def test_current_retry_discovers_new_selection(self):
        self.seed()
        self.ids = ["21"]
        self.crawler._body = Mock(return_value={"aiType": 2})
        task = self.manager.retry_task("old", use_current_settings=True)
        self.manager.wait(task.task_id, 5)
        self.assertEqual(self.storage.collection_plan(task.task_id)["ids"], ["21"])
        self.crawler.search.assert_called_once()
        self.adapter.preview_target.assert_called_once()

    def test_interrupted_task_restores_its_original_plan(self):
        self.seed("running")
        self.storage.recover_interrupted_tasks()
        task = self.manager.restore_interrupted_task("old")
        self.manager.wait(task.task_id, 5)
        self.assertEqual(task.task_id, "old")
        self.assertEqual(self.storage.get_task("old")["status"], "completed")
        self.crawler.search.assert_not_called()

    def test_invalid_plan_rolls_back_new_task_and_parent_marker(self):
        plan = self.seed("cancelled")
        with self.storage.connect() as conn:
            conn.execute("UPDATE tasks SET recovery_status='running' WHERE task_id='old'")
        plan["ids"] = ["11", "11"]
        with self.assertRaises(ValueError):
            self.storage.create_task(
                "bad", "pixiv", self.target, str(self.root), {"filter_ai": True}, retry_of="old", collection_plan=plan
            )
        self.assertIsNone(self.storage.get_task("bad"))
        self.assertEqual(self.storage.get_task("old")["recovery_status"], "running")

    def test_existing_plan_is_immutable_and_log_cleanup_preserves_it(self):
        plan = self.seed()
        changed = copy.deepcopy(plan)
        changed["ids"] = ["99"]
        with self.assertRaises(ValueError):
            self.storage.record_collection_plan("old", changed)
        self.storage.add_log("old", "pixiv", "info", "temporary log")
        self.storage.delete_task_logs(["old"])
        self.assertEqual(self.storage.collection_plan("old"), plan)
        self.storage.delete_task_records(["old"])
        with self.storage.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM task_collections").fetchone()[0], 0)

    def test_corrupt_plan_fails_original_retry_but_current_retry_can_recover(self):
        self.seed()
        with self.storage.connect() as conn:
            conn.execute("UPDATE task_collections SET plan_json='[]' WHERE task_id='old'")
        with self.assertRaises(ValueError):
            self.manager.retry_task("old")
        task = self.manager.retry_task("old", use_current_settings=True)
        self.manager.wait(task.task_id, 5)
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "completed")

    def test_selected_parameter_change_rejects_original_plan_without_new_task(self):
        self.seed()
        with self.assertRaises(ValueError):
            self.manager.retry_task("old", option_overrides={"max_works": 1})
        self.assertEqual(len(self.storage.list_tasks()), 1)

    def test_failed_commit_prevents_transfer(self):
        with patch.object(self.storage, "record_collection_plan", side_effect=ValueError("cannot commit selection")):
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.crawler._body.assert_not_called()

    def test_new_table_can_be_added_to_existing_database_without_changing_tasks(self):
        self.seed()
        with self.storage.connect() as conn:
            conn.execute("DROP TABLE task_collections")
        upgraded = AppStorage(self.storage.db_path)
        self.assertEqual(upgraded.get_task("old")["status"], "failed")
        self.assertIsNone(upgraded.collection_plan("old"))

    def test_novel_selection_commits_before_novel_details(self):
        target = "https://www.pixiv.net/users/9/bookmarks/novels"
        client = object.__new__(PixivCrawler)

        def body(path):
            if "/novels/bookmarks?" in path:
                return {"works": [{"id": "11"}], "total": 1}
            plan = self.storage.collection_plan("novel")
            self.assertEqual((plan["kind"], plan["ids"]), ("novel_bookmark", ["11"]))
            return {"id": "11", "aiType": 2, "userId": "1"}

        client._body, client._json = body, Mock()
        self.storage.create_task("novel", "pixiv", target, str(self.root), {})
        from software_app.core.events import CallbackSet

        callbacks = CallbackSet(
            on_progress=lambda event: (
                self.storage.record_collection_plan("novel", event.metadata["collection_plan"])
                if "collection_plan" in event.metadata
                else None
            )
        )
        client.download(DownloadTask("novel", "pixiv", target, self.root), callbacks, threading.Event())

    def test_legacy_bookmark_key_ignores_filters_not_used_by_bookmarks(self):
        from software_app.core.collection_plan import validated_collection_plan

        plan = self.seed()
        options = {"filter_ai": True, "age_mode": "safe", "minimum_bookmarks": 10}
        self.assertEqual(validated_collection_plan(plan, "pixiv", self.target, options), plan)
        options.update(age_mode="r18", minimum_bookmarks=20)
        self.assertEqual(validated_collection_plan(plan, "pixiv", self.target, options), plan)
