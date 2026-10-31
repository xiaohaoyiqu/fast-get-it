import json
import sqlite3
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.core.models import ModuleInfo
from software_app.core.person_config import PersonConfigs, person_key, validated_options
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager


class PersonConfigTests(unittest.TestCase):
    def setUp(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        self.storage = object.__new__(AppStorage)
        self.storage.connect = lambda: conn
        self.storage._log_write_counter = 0
        self.storage.init_schema()
        self.store = PersonConfigs(self.storage)
        self.manager = object.__new__(TaskManager)
        self.manager.storage = self.storage
        self.manager.adapters = {
            name: Mock(info=ModuleInfo(name, name), supports_media_retry=False)
            for name in ("twitter", "pixiv", "bluesky", "instagram")
        }
        self.manager.get_concurrency_limits = Mock(return_value=(1, 1, 1))
        self.manager.blocklist = Mock()
        self.manager.blocklist.is_blocked.return_value = False
        self.manager._lock = threading.RLock()
        self.manager._shutting_down = False
        self.manager._library_maintenance = False
        self.manager._instance_lease = Mock(owner_id="synthetic-owner")
        self.manager.running = {}
        threads = patch("software_app.core.task_manager.threading.Thread")
        threads.start()
        self.addCleanup(threads.stop)

    def start(self, target="alice", options=None):
        return self.manager.start_task("twitter", target, Path("outputs"), options or {"types": "1"})

    def test_direct_account_aliases_match_but_other_people_and_platforms_do_not(self):
        self.assertEqual(person_key("twitter", "@Alice"), person_key("twitter", "https://x.com/alice/media"))
        self.assertNotEqual(person_key("twitter", "alice"), person_key("instagram", "alice"))
        self.assertNotEqual(person_key("twitter", "alice"), person_key("twitter", "bob"))
        self.assertEqual(person_key("pixiv", "12", "user"), ("pixiv", "12"))
        self.assertIsNone(person_key("pixiv", "12", "work"))
        self.assertNotEqual(
            person_key("bluesky", "did:web:example.com:Alice"), person_key("bluesky", "did:web:example.com:alice")
        )
        self.assertEqual(
            person_key("bluesky", "https://bsky.app/profile/did:web:example.com:Alice"),
            ("bluesky", "did:web:example.com:Alice"),
        )

    def test_ambiguous_or_work_targets_never_infer_person_configuration(self):
        for module, target in (
            ("twitter", "https://x.com/alice/status/123"),
            ("pixiv", "https://www.pixiv.net/artworks/12"),
            ("pixiv", "https://www.pixiv.net/users/12/bookmarks/artworks"),
            ("bluesky", "https://bsky.app/profile/alice.test/post/abc"),
            ("instagram", "https://www.instagram.com/p/abc/"),
            ("website", "https://x.com/alice"),
            ("twitter", "https://["),
        ):
            with self.subTest(target=target):
                self.assertIsNone(person_key(module, target))

    def test_sensitive_internal_or_invalid_parameters_cannot_be_saved(self):
        for options in (
            {"cookie": "secret"},
            {"proxy_url": "secret"},
            {"author_id": "other"},
            {"_retry_media": []},
            {"types": "1,9"},
            {"image_format": "exe"},
            {"max_works": True},
            {"max_works": 101},
            {"filter_ai": "yes"},
        ):
            with self.subTest(options=options), self.assertRaises(ValueError):
                validated_options(options)

    def test_each_task_keeps_its_own_person_and_format_snapshot(self):
        self.store.save(("twitter", "alice"), "Alice images", {"types": "1", "image_format": "png"})
        self.store.save(("twitter", "bob"), "Bob videos", {"types": "2", "image_format": "original"})
        original = {"types": "4", "nested": {"value": 1}}
        alice = self.start("alice", original)
        bob = self.start("bob", original)
        self.assertEqual((alice.options["types"], bob.options["types"]), ("1", "2"))
        self.assertEqual(original["types"], "4")
        original["nested"]["value"] = 2
        self.assertEqual(alice.options["nested"]["value"], 1)
        alice.options["nested"]["value"] = 3
        self.assertEqual(bob.options["nested"]["value"], 1)
        row = self.storage.get_task(alice.task_id)
        self.assertEqual(json.loads(row["options_json"])["_person_config"]["key"], ["twitter", "alice"])

    def test_profile_updates_only_affect_new_tasks_and_explicit_current_retry(self):
        self.store.save(("twitter", "alice"), "first", {"types": "1"})
        first = self.start()
        self.storage.update_task_status(first.task_id, "failed")
        self.store.save(("twitter", "alice"), "second", {"types": "2"})
        original_retry = self.manager.retry_task(first.task_id)
        current_retry = self.manager.retry_task(first.task_id, use_current_settings=True)
        self.assertEqual(first.options["types"], "1")
        self.assertEqual(original_retry.options["types"], "1")
        self.assertEqual(current_retry.options["types"], "2")
        self.assertEqual(original_retry.options["_person_config"]["label"], "first")

    def test_original_global_task_does_not_gain_a_later_person_profile_on_retry(self):
        first = self.start(options={"types": "4"})
        self.storage.update_task_status(first.task_id, "failed")
        self.store.save(("twitter", "alice"), "new profile", {"types": "2"})
        retried = self.manager.retry_task(first.task_id)
        self.assertEqual(retried.options["types"], "4")
        self.assertNotIn("_person_config", retried.options)
        self.assertEqual(self.start().options["types"], "2")

    def test_switching_targets_with_an_old_snapshot_is_rejected(self):
        self.store.save(("twitter", "alice"), "Alice", {"types": "1"})
        task = self.start()
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.start("bob", task.options)
        with self.assertRaises(ValueError):
            self.start("https://x.com/alice/status/123", task.options)

    def test_person_output_is_independent_but_batch_and_subset_directories_are_retained(self):
        preferred = str((Path.cwd() / "person-output").resolve())
        self.store.save(("twitter", "alice"), "Alice", {"types": "1"}, preferred)
        directory, _ = self.store.prepare("twitter", "alice", "normal", {})
        self.assertEqual(str(directory), preferred)
        for options in ({"_batch_id": "batch"}, {"_retry_media": [1]}):
            directory, _ = self.store.prepare("twitter", "alice", "original-subfolder", options)
            self.assertEqual(directory, Path("original-subfolder"))

    def test_profile_removal_does_not_change_existing_task_snapshot(self):
        self.store.save(("twitter", "alice"), "Alice", {"types": "2"})
        first = self.start()
        self.store.delete(("twitter", "alice"))
        self.storage.update_task_status(first.task_id, "failed")
        self.assertEqual(self.manager.retry_task(first.task_id).options["types"], "2")
        self.assertEqual(self.start().options["types"], "1")

    def test_damaged_task_options_stop_retry_before_creating_a_task(self):
        first = self.start()
        self.storage.update_task_status(first.task_id, "failed")
        for encoded in ("{broken", "[]"):
            with self.storage.connect() as conn:
                conn.execute("UPDATE tasks SET options_json=? WHERE task_id=?", (encoded, first.task_id))
            with self.subTest(encoded=encoded), patch.object(self.manager, "start_task") as start:
                with self.assertRaises(ValueError):
                    self.manager.retry_task(first.task_id)
                start.assert_not_called()

    def test_restore_rejects_snapshot_bound_to_a_different_account(self):
        self.store.save(("twitter", "alice"), "Alice", {"types": "1"})
        first = self.start()
        with self.storage.connect() as conn:
            conn.execute(
                "UPDATE tasks SET target='bob',status='cancelled',recovery_status='running' WHERE task_id=?",
                (first.task_id,),
            )
        with self.assertRaisesRegex(ValueError, "不匹配"):
            self.manager.restore_interrupted_task(first.task_id)
        self.manager.running.clear()
        self.store.save(("twitter", "alice"), "changed after interruption", {"types": "2"})
        with self.storage.connect() as conn:
            conn.execute(
                "UPDATE tasks SET target='alice',owner_id='',recovery_status='paused' WHERE task_id=?", (first.task_id,)
            )
        restored = self.manager.restore_interrupted_task(first.task_id)
        self.assertEqual(restored.options["types"], "1")
        self.assertTrue(self.manager.running[first.task_id].cancel_event.paused)
