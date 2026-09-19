from __future__ import annotations

import tempfile
import threading
import unittest
import json
from pathlib import Path

from software_app.core.adapter import CrawlerAdapter
from software_app.core.blocklist import BlocklistStore, account_from_target, account_from_url, work_from_target
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.pixiv.client import PixivCrawler
from software_app.crawlers.pixiv.external import FanboxClient
from software_app.crawlers.twitter.manga_downloader import extract_media_from_json
from software_app.ui.google_search_tab import GoogleSearchTabMixin
from software_app.ui.following_tab import FollowingTabMixin
from types import SimpleNamespace


class _NoDownloadAdapter(CrawlerAdapter):
    info = ModuleInfo("twitter", "Twitter")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("twitter", raw_target, raw_target)

    def download(self, task, callbacks, cancel_event):
        raise AssertionError("blocked task must not run")


class BlocklistTests(unittest.TestCase):
    def test_direct_account_links_are_distinct_from_works_and_posts(self) -> None:
        self.assertEqual(account_from_url("https://x.com/Artist"), ("twitter", "artist"))
        self.assertEqual(account_from_url("https://www.pixiv.net/users/123"), ("pixiv", "123"))
        self.assertEqual(account_from_url("https://bsky.app/profile/Artist.bsky.social"), ("bluesky", "artist.bsky.social"))
        self.assertEqual(account_from_url("https://www.instagram.com/Example.Artist/"), ("instagram", "example.artist"))
        self.assertEqual(account_from_url("https://www.fanbox.cc/@Creator"), ("fanbox", "creator"))
        self.assertEqual(account_from_url("https://sketch.pixiv.net/@Artist"), ("sketch", "artist"))
        self.assertIsNone(account_from_url("https://www.pixiv.net/artworks/123"))
        self.assertIsNone(account_from_url("https://x.com/artist/status/123"))
        self.assertEqual(account_from_target("twitter", "https://x.com/artist/status/123"), ("twitter", "artist"))
        self.assertEqual(account_from_target("bluesky", "https://bsky.app/profile/artist.bsky.social/post/abc"),
                         ("bluesky", "artist.bsky.social"))
        self.assertIsNone(account_from_url("https://pixiv.net.evil.example/users/123"))
        self.assertEqual(account_from_target("twitter", "@Artist"), ("twitter", "artist"))

    def test_group_requires_explicit_links_and_removal_restores_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            first = store.add_group(("twitter", "artist"))
            self.assertEqual(store.blocked_accounts(), {("twitter", "artist")})
            updated = store.add_group(("twitter", "artist"), [("pixiv", "123"), ("fanbox", "creator")])
            self.assertEqual(updated["id"], first["id"])
            self.assertEqual(len(store.groups()), 1)
            self.assertTrue(store.is_blocked("pixiv", "https://www.pixiv.net/users/123"))
            self.assertTrue(store.is_blocked("pixiv", "https://www.pixiv.net/artworks/9", author_id="123"))
            self.assertFalse(store.is_blocked("pixiv", "https://www.pixiv.net/artworks/9", author_id="999"))
            self.assertTrue(store.remove_group(first["id"]))
            self.assertEqual(store.blocked_accounts(), set())

    def test_public_account_and_post_rules_can_be_removed_cleanly(self) -> None:
        # Public posts from the official pixiv X and Bluesky accounts.
        x_post = "https://x.com/pixiv/status/2023978485316768106"
        x_other_post = "https://x.com/pixiv/status/1915708135853236282"
        bsky_post = "https://bsky.app/profile/bsky.app/post/3mu3jzayuys2k"
        bsky_profile = "https://bsky.app/profile/bsky.app"
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            search_state = SimpleNamespace(manager=SimpleNamespace(blocklist=store))
            display_state = GoogleSearchTabMixin()
            display_state.manager = search_state.manager
            candidate_state = SimpleNamespace(
                manager=search_state.manager,
                module_var=SimpleNamespace(get=lambda: "google_image"),
                content_scope_var=SimpleNamespace(get=lambda: "相似搜索"),
                platform_candidate_scope={"google_image": "相似搜索"},
                platform_candidate_rows={"google_image": [
                    {"url": "https://example.com/redirect", "final_url": x_post},
                ]},
            )
            self.assertFalse(store.is_blocked("twitter", x_post))
            self.assertFalse(store.is_blocked("bluesky", bsky_post))

            x_rule = store.add_group(("twitter", "pixiv"))
            bsky_work = store.add_work("bluesky", bsky_post)
            self.assertTrue(store.is_blocked("twitter", x_post))
            self.assertTrue(store.is_blocked("twitter", x_other_post))
            self.assertTrue(store.is_blocked("bluesky", bsky_post))
            self.assertFalse(store.is_blocked("bluesky", bsky_profile))
            self.assertTrue(GoogleSearchTabMixin._google_result_blocked(search_state, {"url": x_post}))
            self.assertTrue(GoogleSearchTabMixin._google_result_blocked(search_state, {"url": bsky_post}))
            redirect_row = {"url": "https://example.com/redirect", "final_url": x_post}
            self.assertTrue(display_state._google_result_blocked(redirect_row))
            self.assertEqual(display_state._google_result_status(redirect_row), "屏蔽（跳转）")
            self.assertEqual(FollowingTabMixin._exportable_candidate_rows(candidate_state), [])

            self.assertTrue(store.remove_group(x_rule["id"]))
            self.assertFalse(store.is_blocked("twitter", x_post))
            bsky_rule = store.add_group(("bluesky", "did:plc:z72i7hdynmk6r22z27h6tvur"),
                                        [("bluesky", "bsky.app")])
            self.assertTrue(store.is_blocked("bluesky", bsky_profile))
            self.assertTrue(store.remove_work(bsky_work["id"]))
            self.assertTrue(store.is_blocked("bluesky", bsky_post))
            self.assertTrue(store.remove_group(bsky_rule["id"]))
            self.assertFalse(store.is_blocked("bluesky", bsky_post))
            self.assertFalse(GoogleSearchTabMixin._google_result_blocked(search_state, {"url": x_post}))
            self.assertFalse(GoogleSearchTabMixin._google_result_blocked(search_state, {"url": bsky_post}))
            self.assertFalse(display_state._google_result_blocked(redirect_row))
            self.assertEqual(len(FollowingTabMixin._exportable_candidate_rows(candidate_state)), 1)
            self.assertEqual(store.blocked_accounts(), set())
            self.assertEqual(store.blocked_works(), set())

    def test_exact_work_rules_persist_and_round_trip_per_platform(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            self.assertEqual(work_from_target("google_image", "https://www.pixiv.net/artworks/42?ref=x"), ("pixiv", "42"))
            self.assertEqual(work_from_target("website", "https://x.com/a/status/55"), ("twitter", "55"))
            self.assertEqual(work_from_target("website", "https://bsky.app/profile/a.bsky.social/post/abc"),
                             ("bluesky", "a.bsky.social/abc"))
            self.assertEqual(work_from_target("website", "https://www.instagram.com/reel/ABC_123/"),
                             ("instagram", "ABC_123"))
            self.assertEqual(work_from_target("website", "https://exhentai.org/g/1234567/abcdef1234/"),
                             ("ehentai", "1234567"))
            self.assertEqual(work_from_target("website", "https://example.com/gallery/1#image"),
                             ("url", "https://example.com/gallery/1"))
            rule = store.add_work("google_image", "https://www.pixiv.net/artworks/42", label="One work")
            self.assertTrue(store.is_blocked("pixiv", "42", input_kind="work"))
            self.assertFalse(store.is_blocked("pixiv", "https://www.pixiv.net/artworks/43"))
            self.assertFalse(store.is_blocked("pixiv", "https://www.pixiv.net/users/42"))
            store.add_group(("pixiv", "999"))
            exported = Path(directory) / "pixiv.json"
            self.assertEqual(store.export_platform("pixiv", exported), 2)
            imported = BlocklistStore(Path(directory) / "imported.json")
            self.assertEqual(imported.import_platform("pixiv", exported), 2)
            self.assertTrue(imported.is_blocked("pixiv", "https://www.pixiv.net/artworks/42"))
            self.assertTrue(imported.is_blocked("pixiv", "https://www.pixiv.net/users/999"))
            store.update_work(rule["id"], label="Renamed", module_id="pixiv", target="")
            self.assertEqual(store.works()[0]["label"], "Renamed")
            self.assertTrue(store.remove_work(rule["id"]))
            self.assertFalse(store.is_blocked("pixiv", "https://www.pixiv.net/artworks/42"))

            eh_rule = store.add_work("ehentai", "https://e-hentai.org/g/1234567/abcdef1234/")
            self.assertTrue(store.is_blocked("ehentai", "https://exhentai.org/g/1234567/0123456789/"))
            eh_export = Path(directory) / "ehentai.json"
            self.assertEqual(store.export_platform("ehentai", eh_export), 1)
            eh_imported = BlocklistStore(Path(directory) / "eh-imported.json")
            self.assertEqual(eh_imported.import_platform("ehentai", eh_export), 1)
            self.assertTrue(eh_imported.is_blocked("ehentai", "1234567/abcdef1234", input_kind="gallery"))
            self.assertTrue(store.remove_work(eh_rule["id"]))

    def test_jmcomic_author_album_and_chapter_rules_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            self.assertEqual(work_from_target("jmcomic", "JM123", "album"), ("jmcomic", "123"))
            self.assertEqual(work_from_target("jmcomic", "https://mirror.example/photo/456"),
                             ("jmcomic_chapter", "456"))
            self.assertEqual(work_from_target("jmcomic", "https://mirror.example/novel/4459", "novel"),
                             ("jmcomic_novel", "4459"))
            store.add_group(("jmcomic_author", "Sample Author"))
            album = store.add_work("jmcomic", "https://mirror.example/album/123")
            chapter = store.add_work("jmcomic", "https://mirror.example/photo/456")
            novel = store.add_work("jmcomic_novel", "https://mirror.example/novel/4459")
            self.assertTrue(store.is_blocked("jmcomic", "JM123", input_kind="album"))
            self.assertFalse(store.is_blocked("jmcomic", "JM124", input_kind="album"))
            self.assertTrue(store.is_blocked("jmcomic", "https://mirror.example/photo/456"))
            self.assertTrue(store.is_blocked("jmcomic", "https://mirror.example/novel/4459", input_kind="novel"))
            self.assertTrue(store.is_blocked("jmcomic", "https://mirror.example/album/999",
                                             author_id=" sample   author "))
            self.assertTrue(store.remove_work(album["id"]))
            self.assertFalse(store.is_blocked("jmcomic", "JM123", input_kind="album"))
            self.assertTrue(store.remove_work(chapter["id"]))
            self.assertTrue(store.remove_work(novel["id"]))

            exported = Path(directory) / "jmcomic.json"
            store.add_work("jmcomic", "JM789")
            self.assertEqual(store.export_platform("jmcomic", exported), 1)
            imported = BlocklistStore(Path(directory) / "jm-imported.json")
            self.assertEqual(imported.import_platform("jmcomic", exported), 1)
            self.assertTrue(imported.is_blocked("jmcomic", "https://another.example/album/789"))

    def test_jmcomic_blocked_tag_skips_matching_album(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.add_group(("jmcomic_tag", "blocked tag"))
            self.assertTrue(store.is_blocked("jmcomic", "JM123", tags=["Other", "Blocked Tag"]))
            self.assertFalse(store.is_blocked("jmcomic", "JM123", tags=["Other"]))

    def test_platform_import_rejects_mixed_site_without_partial_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "wrong.json"
            source.write_text(json.dumps({"platform": "pixiv", "accounts": ["123"],
                                          "works": [{"work": "https://x.com/a/status/8"}]}), encoding="utf-8")
            store = BlocklistStore(Path(directory) / "blocklist.json")
            with self.assertRaisesRegex(ValueError, "平台不匹配"):
                store.import_platform("pixiv", source)
            self.assertEqual(store.blocked_accounts(), set())
            self.assertEqual(store.blocked_works(), set())

    def test_old_pixiv_member_list_imports_and_x_archive_ids_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            old_file = Path(directory) / "blacklist_members.txt"
            old_file.write_text("# old pixiv list\n123\nhttps://www.pixiv.net/users/456\n123\n", encoding="utf-8")
            self.assertEqual(store.import_platform("pixiv", old_file), 2)
            self.assertEqual(store.import_platform("pixiv", old_file), 0)
            self.assertEqual(store.blocked_accounts(), {("pixiv", "123"), ("pixiv", "456")})
            archive_ids = Path(directory) / "x-ids.txt"
            archive_ids.write_text("1234567890\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "归档账号 ID"):
                store.import_platform("twitter", archive_ids)
            self.assertEqual(store.blocked_accounts(), {("pixiv", "123"), ("pixiv", "456")})

    def test_task_manager_rejects_blocked_target_before_creating_task(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            storage = AppStorage(Path(directory) / "tasks.db")
            manager = TaskManager(storage)
            manager.register_adapter(_NoDownloadAdapter())
            manager.blocklist.add_group(("twitter", "artist"))
            with self.assertRaisesRegex(ValueError, "黑名单"):
                manager.start_task("twitter", "@artist", Path(directory))
            with self.assertRaisesRegex(ValueError, "黑名单"):
                manager.start_task("twitter", "https://x.com/artist/status/123", Path(directory))
            manager.blocklist.add_work("twitter", "https://x.com/other/status/456")
            with self.assertRaisesRegex(ValueError, "黑名单"):
                manager.start_task("twitter", "https://twitter.com/other/status/456", Path(directory))
            self.assertEqual(storage.list_tasks(), [])

    def test_google_candidate_uses_same_work_and_author_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            state = SimpleNamespace(manager=SimpleNamespace(blocklist=store))
            pixiv = {"url": "https://www.pixiv.net/artworks/42"}
            tweet = {"url": "https://x.com/artist/status/456"}
            self.assertFalse(GoogleSearchTabMixin._google_result_blocked(state, pixiv))
            store.add_work("pixiv", pixiv["url"])
            store.add_group(("twitter", "artist"))
            self.assertTrue(GoogleSearchTabMixin._google_result_blocked(state, pixiv))
            self.assertTrue(GoogleSearchTabMixin._google_result_blocked(state, tweet))

    def test_google_pixiv_candidate_respects_inspected_author(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.add_group(("pixiv", "123"))
            work_url = "https://www.pixiv.net/en/artworks/456"
            self.assertFalse(store.is_blocked("google_image", work_url))
            self.assertTrue(store.is_blocked("google_image", work_url, author_id="123"))
            self.assertTrue(store.is_blocked("website", work_url, author_id="123"))
            self.assertFalse(store.is_blocked("google_image", work_url, author_id="999"))
            state = SimpleNamespace(manager=SimpleNamespace(blocklist=store))
            self.assertTrue(GoogleSearchTabMixin._google_result_blocked(
                state, {"url": "https://example.com/redirect", "final_url": work_url,
                        "author_id": "123", "pixiv_inspected_work_id": "456"}
            ))
            self.assertFalse(GoogleSearchTabMixin._google_result_blocked(
                state, {"url": "https://www.pixiv.net/artworks/999", "author_id": "123",
                        "pixiv_inspected_work_id": "456"}
            ))
            checked = {"author_id": "123", "pixiv_inspected_work_id": "456"}
            self.assertEqual(FollowingTabMixin._candidate_author_for(checked, work_url, "google_image"), "123")
            self.assertEqual(FollowingTabMixin._candidate_author_for(
                checked, "https://www.pixiv.net/artworks/999", "google_image"
            ), "")

    def test_twitter_timeline_media_skips_blocked_post_id(self) -> None:
        payload = {"tweets": [
            {"rest_id": "1", "legacy": {"full_text": "a", "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg"}]}}},
            {"rest_id": "2", "legacy": {"full_text": "b", "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/B.jpg"}]}}},
        ]}
        media = extract_media_from_json(payload, "1", {"1"})
        self.assertEqual(len(media), 1)
        self.assertIn("B", media[0][0])

    def test_pixiv_work_and_fanbox_post_skip_blocked_creator_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.add_group(("pixiv", "123"), [("fanbox", "creator")])
            crawler = object.__new__(PixivCrawler)
            crawler._body = lambda _path: {"userId": "123", "userName": "Artist", "title": "Work", "illustType": 0}
            progress = []
            task = DownloadTask("blocked", "pixiv", "https://www.pixiv.net/artworks/9", Path(directory),
                                {"_blocklist_path": str(store.path)})
            crawler.download(task, CallbackSet(on_progress=progress.append), threading.Event())
            self.assertEqual(progress[-1].metadata["skipped_blocked"], 1)
            self.assertEqual(list(Path(directory).rglob("*.jpg")), [])

            fanbox = object.__new__(FanboxClient)
            fanbox.posts = lambda _target, _limit: [{"id": "8", "creatorId": "creator"}]
            fanbox._get = lambda *_args: {"body": {"id": "8", "creatorId": "creator", "title": "Post"}}
            fanbox_task = DownloadTask("blocked-fanbox", "pixiv", "https://www.fanbox.cc/posts/8", Path(directory),
                                      {"_blocklist_path": str(store.path)})
            result = fanbox.download(fanbox_task.target, Path(directory), fanbox_task,
                                     CallbackSet(on_progress=progress.append), threading.Event())
            self.assertEqual(result, 0)
            self.assertFalse((Path(directory) / "FANBOX_creator").exists())

    def test_runtime_skips_one_blocked_work_without_blocking_creator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.add_work("pixiv", "https://www.pixiv.net/artworks/9")
            store.add_work("pixiv", "https://www.fanbox.cc/posts/8")
            crawler = object.__new__(PixivCrawler)
            crawler._body = lambda _path: {"userId": "123", "userName": "Artist", "title": "Work", "illustType": 0}
            progress = []
            task = DownloadTask("blocked-work", "pixiv", "https://www.pixiv.net/artworks/9", Path(directory),
                                {"_blocklist_path": str(store.path)})
            crawler.download(task, CallbackSet(on_progress=progress.append), threading.Event())
            self.assertEqual(progress[-1].metadata["skipped_blocked"], 1)
            self.assertFalse(store.is_blocked("pixiv", "https://www.pixiv.net/artworks/10", author_id="123"))

            fanbox = object.__new__(FanboxClient)
            fanbox.posts = lambda _target, _limit: [{"id": "8", "creatorId": "creator"}]
            fanbox._get = lambda *_args: {"body": {"id": "8", "creatorId": "creator", "title": "Post"}}
            fanbox_task = DownloadTask("blocked-post", "pixiv", "https://www.fanbox.cc/posts/8", Path(directory),
                                      {"_blocklist_path": str(store.path)})
            self.assertEqual(fanbox.download(fanbox_task.target, Path(directory), fanbox_task,
                                             CallbackSet(on_progress=progress.append), threading.Event()), 0)
            self.assertFalse((Path(directory) / "FANBOX_creator").exists())


if __name__ == "__main__":
    unittest.main()
