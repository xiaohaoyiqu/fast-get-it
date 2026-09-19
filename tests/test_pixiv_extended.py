import json
import re
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, TargetPreview, classify_file
from software_app.core.plugin_loader import load_adapter_plugins, set_external_plugin_enabled
from software_app.crawlers.pixiv.catalog import PixivCatalog, filter_bookmark_candidates
from software_app.crawlers.pixiv.client import (
    PixivCrawler,
    parse_pixiv_history_html,
    parse_pixiv_profile_html,
    parse_pixiv_target,
)
from software_app.crawlers.pixiv.external import FanboxClient, SketchClient
from software_app.ui.google_search_tab import GoogleSearchTabMixin
from software_app.ui.settings_tab import plugin_purpose


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class _Session:
    def __init__(self, routes):
        self.routes = routes
        self.urls = []

    def get(self, url, **_kwargs):
        self.urls.append(url)
        for marker, payload in self.routes:
            if marker in url:
                return _Response(payload)
        raise AssertionError(f"unexpected URL: {url}")


class PixivExtendedTests(unittest.TestCase):
    def test_pixiv_session_uses_only_the_app_proxy_setting(self):
        crawler = PixivCrawler(cookie_file=Path("missing-cookie.json"), proxy_url="")
        self.assertFalse(crawler.session.trust_env)

    def test_preview_avatar_rejects_lookalike_hosts_and_non_images(self):
        class Response:
            content = b"not-an-image"
            headers = {"Content-Type": "text/html; charset=utf-8"}

            def raise_for_status(self):
                return None

        class Client:
            class Session:
                def get(self, _url, **_kwargs):
                    return Response()

            session = Session()

        adapter = PixivNativeAdapter()
        adapter._client = lambda _options=None: Client()
        with self.assertRaises(ValueError):
            adapter.fetch_preview_image_bytes("https://evilpixiv.net/avatar.png")
        with self.assertRaises(ValueError):
            adapter.fetch_preview_image_bytes("https://i.pximg.net/avatar.png")

    def test_gif_is_classified_as_animation(self):
        self.assertEqual(classify_file("ugoira.gif"), "animation")

    def test_extended_pixiv_target_routes(self):
        self.assertEqual(parse_pixiv_target("https://www.pixiv.net/series/12").kind, "manga_series")
        self.assertEqual(parse_pixiv_target("https://www.pixiv.net/novel/show.php?id=34").kind, "novel")
        self.assertEqual(parse_pixiv_target("https://www.pixiv.net/novel/series/56").kind, "novel_series")
        self.assertEqual(parse_pixiv_target("https://creator.fanbox.cc/posts/78").kind, "fanbox")
        self.assertEqual(parse_pixiv_target("https://sketch.pixiv.net/items/90").kind, "sketch")

    def test_online_search_preview_keeps_current_filters(self):
        crawler = object.__new__(PixivCrawler)
        captured = {}

        def search(_keyword, limit=20, search_mode="", options=None):
            captured.update({"limit": limit, "search_mode": search_mode, "options": options})
            return []

        crawler.search = search
        filters = {"search_mode": "标签（插画）", "age_mode": "safe", "minimum_bookmarks": 100}
        crawler.fetch_preview(parse_pixiv_target("风景"), filters)
        self.assertEqual(captured["options"], filters)

    def test_profile_html_fallback_reads_avatar_name_and_jump_link(self):
        parsed = parse_pixiv_profile_html(
            '<div data-gtm-user-id="28245"><img alt="Artist" '
            'src="https://i.pximg.net/user-profile/img/a_170.png"></div>'
            '<h1 class="generated">Artist Name</h1>'
            '<a href="/jump.php?url=https%3A%2F%2Fexample.com%2Fprofile">Site</a>',
            "28245",
        )
        self.assertEqual(parsed["avatar_url"], "https://i.pximg.net/user-profile/img/a_170.png")
        self.assertEqual(parsed["display_name"], "Artist Name")
        self.assertEqual(parsed["external_links"][0]["url"], "https://example.com/profile")

    def test_history_html_extracts_unique_artwork_candidates(self):
        rows = parse_pixiv_history_html(
            '<a href="/artworks/101"><img alt="First" src="https://i.pximg.net/c/250x250/img-master/a.jpg"></a>'
            '<a href="https://www.pixiv.net/artworks/101">duplicate</a>'
            '<a href="/artworks/202" title="Second"></a>',
            limit=20,
        )
        self.assertEqual([row["id"] for row in rows], ["101", "202"])
        self.assertEqual(rows[0]["avatar_url"], "https://i.pximg.net/c/250x250/img-master/a.jpg")

    def test_catalog_reads_manga_and_novel_series(self):
        def body(path):
            if "/ajax/series/12" in path:
                return {"page": {"total": 1, "series": [{"workId": "100", "order": 1}]}}
            if "series_content/56" in path:
                return {"page": {"total": 1, "seriesContents": [{"id": "200", "title": "Novel", "order": 1}]}}
            raise AssertionError(path)

        catalog = PixivCatalog(body, lambda _url: {})
        manga = catalog.manga_series("12")
        novels = catalog.novel_series("56")
        self.assertEqual(manga[0]["url"], "https://www.pixiv.net/artworks/100")
        self.assertEqual(manga[0]["series_order"], 1)
        self.assertEqual(novels[0]["url"], "https://www.pixiv.net/novel/show.php?id=200")
        self.assertEqual(novels[0]["input_kind"], "novel")

    def test_catalog_reads_ranking_new_bookmarks_and_author_tag(self):
        calls = []

        def body(path):
            calls.append(path)
            if "illust/new" in path:
                return {"illusts": [{"id": "2", "title": "New"}], "lastId": ""}
            if "illusts/bookmarks" in path:
                return {"works": [{"id": "3", "title": "Saved"}], "total": 1}
            if "illustmanga/tag" in path:
                return {"works": [{"id": "4", "title": "Tagged"}]}
            raise AssertionError(path)

        catalog = PixivCatalog(
            body,
            lambda url: {"contents": [{"illust_id": "1", "title": "Ranked", "rank": 1}], "next": False},
        )
        self.assertEqual(catalog.ranking("周榜 漫画 2026-09-01")[0]["id"], "1")
        self.assertEqual(catalog.new_works("漫画")[0]["id"], "2")
        self.assertEqual(catalog.bookmarks("99")[0]["id"], "3")
        self.assertEqual(catalog.author_tag("99 风景")[0]["id"], "4")
        self.assertTrue(any("rest=show" in path for path in calls))

    def test_pixiv_author_search_and_novel_bookmarks(self):
        calls = []

        def body(path):
            calls.append(path)
            if "search/users" in path:
                return {"users": [{"userId": "7", "name": "Artist", "image": "https://i.pximg.net/avatar.png"}]}
            if "novels/bookmarks" in path:
                return {"works": [{"id": "8", "title": "Novel", "userId": "7", "userName": "Artist"}], "total": 1}
            raise AssertionError(path)

        crawler = object.__new__(PixivCrawler)
        crawler._body = body
        crawler._json = lambda _url: {}
        authors = crawler.search("Artist", search_mode="作者搜索", options={"author_match_mode": "partial"})
        novels = crawler.search("7", search_mode="小说收藏", options={"account_id": "7", "pixiv_visibility": "show"})
        self.assertEqual(authors[0]["input_kind"], "user")
        self.assertEqual(authors[0]["avatar_url"], "https://i.pximg.net/avatar.png")
        self.assertEqual(novels[0]["input_kind"], "novel")
        self.assertTrue(any("novels/bookmarks" in path for path in calls))

    def test_novel_download_writes_readable_html_txt_and_raw_json(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            crawler = object.__new__(PixivCrawler)
            crawler._body = lambda _path: {
                "title": "小说标题", "content": "第一行\n第二行", "userId": "8", "userName": "作者",
                "createDate": "2026-09-01", "tags": {"tags": [{"tag": "测试"}]},
            }
            files = []
            progress = []
            task = DownloadTask(
                "novel-task", "pixiv", "https://www.pixiv.net/novel/show.php?id=123",
                Path(temp_dir), {"input_kind": "novel"},
            )
            crawler.download(task, CallbackSet(on_file=files.append, on_progress=progress.append), threading.Event())
            self.assertEqual(len(files), 3)
            self.assertEqual({record.media_type for record in files}, {"text", "metadata"})
            html_file = next(record.path for record in files if record.path.suffix == ".html")
            txt_file = next(record.path for record in files if record.path.suffix == ".txt")
            self.assertIn("第一行", html_file.read_text(encoding="utf-8"))
            txt = txt_file.read_text(encoding="utf-8-sig")
            self.assertIn("小说标题", txt)
            self.assertIn("作者：作者", txt)
            self.assertIn("标签：测试", txt)
            self.assertIn("第一行\n第二行", txt)
            self.assertEqual(progress[-1].percent, 100.0)

    def test_own_bookmarks_are_cached_and_can_be_loaded_offline(self):
        class Client:
            def search(self, query, limit=20, search_mode="", options=None):
                self.call = (query, limit, search_mode, options)
                return [{"id": "8", "title": "Saved", "url": "https://www.pixiv.net/artworks/8"}]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            adapter.cookie_account_id = lambda: "99"
            client = Client()
            adapter._client = lambda _options=None: client

            rows = adapter.refresh_own_bookmarks("work", limit=50, options={"pixiv_visibility": "show"})

            self.assertEqual(client.call[0], "99")
            self.assertEqual(client.call[2], "作品收藏")
            self.assertEqual(adapter.load_bookmark_candidates("work"), rows)
            payload = json.loads((Path(temp_dir) / "bookmarked_works.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["account"], "99")
            self.assertEqual(payload["count"], 1)
            adapter.cookie_account_id = lambda: "100"
            self.assertEqual(adapter.load_bookmark_candidates("work"), [])

    def test_bookmark_scope_limit_is_reported_as_partial(self):
        def body(path):
            if "rest=show" in path:
                return {"works": [{"id": "1"}, {"id": "2"}], "total": 2}
            return {"works": [{"id": "3"}], "total": 1}

        catalog = PixivCatalog(body, lambda _url: {})
        self.assertEqual(len(catalog.bookmarks("9", limit=2, visibility="both")), 2)
        self.assertFalse(catalog.last_bookmark_status["complete"])
        self.assertEqual(len(catalog.bookmarks("9", limit=4, visibility="both")), 3)
        self.assertTrue(catalog.last_bookmark_status["complete"])

    def test_bookmark_search_fetches_beyond_the_old_hundred_item_cap(self):
        crawler = object.__new__(PixivCrawler)
        offsets = []

        def body(path):
            offset = int(re.search(r"offset=(\d+)", path).group(1))
            count = int(re.search(r"limit=(\d+)", path).group(1))
            offsets.append(offset)
            return {"works": [{"id": str(index)} for index in range(offset + 1, min(offset + count, 120) + 1)],
                    "total": 120}

        crawler._body = body
        crawler._json = lambda _url: {}
        rows = crawler.search("9", limit=10000, search_mode="作品收藏", options={"account_id": "9"})
        self.assertEqual(len(rows), 120)
        self.assertEqual(offsets, [0, 48, 96])
        self.assertTrue(crawler.last_bookmark_status["complete"])

    def test_full_bookmark_page_checks_next_page_even_if_reported_total_is_reached(self):
        offsets = []

        def body(path):
            offset = int(re.search(r"offset=(\d+)", path).group(1))
            offsets.append(offset)
            items = [{"id": str(index)} for index in range(1, 49)] if offset == 0 else (
                [{"id": "49"}] if offset == 48 else []
            )
            return {"works": items, "total": 48}

        catalog = PixivCatalog(body, lambda _url: {})
        rows = catalog.bookmarks("9", limit=100)
        self.assertEqual(len(rows), 49)
        self.assertEqual(offsets, [0, 48])
        self.assertTrue(catalog.last_bookmark_status["complete"])

    def test_partial_bookmark_refresh_preserves_previous_cache_and_ignores_display_filters(self):
        class Client:
            last_bookmark_status = {"complete": False, "scopes": [{"visibility": "show", "total": 3}]}

            def search(self, query, limit=20, search_mode="", options=None):
                self.options = options
                self.limit = limit
                return [{"id": "1", "url": "https://www.pixiv.net/artworks/1"}]

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            adapter.cookie_account_id = lambda: "9"
            adapter._write_json_payload(adapter.bookmark_output_file("work"), {
                "account": "9", "items": [
                    {"id": "2", "url": "https://www.pixiv.net/artworks/2"},
                ],
            })
            client = Client()
            adapter._client = lambda _options=None: client
            rows = adapter.refresh_own_bookmarks(
                "work", options={"bookmark_tag": "风景", "start_date": "2026-01-01"},
            )
            self.assertEqual(client.limit, 10000)
            self.assertEqual(client.options["bookmark_tag"], "")
            self.assertEqual(client.options["start_date"], "")
            self.assertEqual({item["id"] for item in rows}, {"1", "2"})
            self.assertTrue(adapter.load_bookmark_payload("work")["partial"])

    def test_bookmark_filters_keep_publication_and_added_dates_separate(self):
        rows = [
            {"id": "12", "author_id": "7", "author_name": "画师甲", "title": "晨光", "tags": ["风景"],
             "published_at": "2025-01-01", "bookmarked_at": "2026-02-02"},
            {"id": "13", "author_id": "8", "author_name": "画师乙", "title": "夜景", "tags": ["城市"],
             "published_at": "2026-03-03", "bookmarked_at": ""},
        ]
        self.assertEqual(filter_bookmark_candidates(rows, query="12", field="作品 ID"), rows[:1])
        self.assertEqual(filter_bookmark_candidates(rows, query="画师乙", field="作者"), rows[1:])
        self.assertEqual(filter_bookmark_candidates(rows, query="风景", field="标签"), rows[:1])
        self.assertEqual(filter_bookmark_candidates(rows, query="晨", field="标题"), rows[:1])
        self.assertEqual(filter_bookmark_candidates(rows, start_date="2026-01-01"), rows[:1])
        self.assertEqual(
            filter_bookmark_candidates(rows, date_basis="发布时间", start_date="2026-01-01"), rows[1:],
        )

    def test_empty_pixiv_work_pages_fail_instead_of_reporting_completion(self):
        crawler = object.__new__(PixivCrawler)
        crawler._work_ids = lambda *_args, **_kwargs: ["12"]
        crawler._body = lambda path: {"illustType": 0, "title": "空作品"} if path.endswith("/12") else []
        task = DownloadTask("empty-work", "pixiv", "https://www.pixiv.net/artworks/12", Path.cwd(), {})
        with self.assertRaisesRegex(ValueError, "未返回任何图片页"):
            crawler.download(task, CallbackSet(), threading.Event())

    def test_builtin_plugin_purpose_explains_user_visible_capability(self):
        self.assertIn("关注", plugin_purpose("twitter"))
        self.assertIn("小说", plugin_purpose("pixiv"))
        self.assertIn("候选", plugin_purpose("google_image"))

    def test_fanbox_and_sketch_candidates_use_native_api_shapes(self):
        fanbox = FanboxClient(_Session([
            ("creator.get", {"body": {"creatorId": "artist"}}),
            ("post.listCreator", {"body": {"items": [{"id": "11", "title": "Post", "creatorId": "artist"}], "nextUrl": ""}}),
        ]))
        self.assertEqual(fanbox.candidates("artist")[0]["url"], "https://www.fanbox.cc/@artist/posts/11")

        sketch = SketchClient(_Session([
            ("posts/public", {"data": {"items": [{"id": "22", "text": "Sketch", "user": {"name": "A"}}]}}),
        ]))
        self.assertEqual(sketch.candidates("@artist")[0]["url"], "https://sketch.pixiv.net/items/22")

    def test_fanbox_support_status_handles_no_plan_and_official_window(self):
        empty = FanboxClient(_Session([("plan.listSupporting", {"body": []})]))
        status = empty.supporting_status(date(2026, 9, 13))
        self.assertEqual(status["active_count"], 0)
        self.assertIn("无需续费", status["message"])

        active = FanboxClient(_Session([("plan.listSupporting", {"body": [{"id": "1", "title": "Plan"}]})]))
        status = active.supporting_status(date(2026, 9, 13))
        self.assertEqual(status["coverage_end"], "2026-09-30")
        self.assertEqual(status["renewal_window_start"], "2026-10-01")
        self.assertEqual(status["renewal_window_end"], "2026-10-05")
        self.assertEqual(status["date_source"], "official_rule")

    def test_fanbox_support_status_prefers_exact_api_date(self):
        fanbox = FanboxClient(_Session([(
            "plan.listSupporting",
            {"body": [{"id": "1", "nextPaymentDatetime": "2026-10-03T00:00:00+09:00"}]},
        )]))
        status = fanbox.supporting_status(date(2026, 9, 13))
        self.assertEqual(status["exact_date"], "2026-10-03")
        self.assertEqual(status["exact_date_kind"], "next_payment")
        self.assertEqual(status["date_source"], "api")

    def test_r18_search_requires_pixiv_login_cookie(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            with self.assertRaisesRegex(ValueError, "R-18"):
                adapter.search_targets("tag", options={"age_mode": "r18"})

    def test_external_plugins_are_explicit_and_cannot_silently_override(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            disabled = root / "disabled"
            disabled.mkdir()
            (disabled / "plugin.json").write_text(
                json.dumps({"id": "disabled", "name": "Disabled", "api_version": 1, "enabled": False}),
                encoding="utf-8",
            )
            enabled = root / "sample"
            enabled.mkdir()
            (enabled / "plugin.json").write_text(
                json.dumps({"id": "sample", "name": "Sample", "api_version": 1, "enabled": True, "module": "plugin.py"}),
                encoding="utf-8",
            )
            (enabled / "plugin.py").write_text(
                "from software_app.core.adapter import CrawlerAdapter\n"
                "from software_app.core.models import ModuleInfo, TargetPreview\n"
                "class A(CrawlerAdapter):\n"
                "    info=ModuleInfo('sample_platform','Sample Platform')\n"
                "    def preview_target(self, raw_target, options=None): return TargetPreview('sample_platform',raw_target,raw_target)\n"
                "    def download(self, task, callbacks, cancel_event): return None\n"
                "def create_adapters(): return A()\n",
                encoding="utf-8",
            )
            bundle = load_adapter_plugins([], root)
            self.assertEqual([adapter.module_id for adapter in bundle.adapters], ["sample_platform"])
            self.assertEqual({report.status for report in bundle.reports}, {"disabled", "loaded"})

    def test_external_plugin_can_explicitly_replace_twitter_and_be_toggled(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            plugin = root / "twitter-custom"
            plugin.mkdir()
            manifest = plugin / "plugin.json"
            manifest.write_text(
                json.dumps({
                    "id": "twitter-custom",
                    "name": "Twitter Custom",
                    "api_version": 1,
                    "enabled": True,
                    "module": "plugin.py",
                    "replaces": "twitter",
                }),
                encoding="utf-8",
            )
            (plugin / "plugin.py").write_text(
                "from software_app.adapters.pixiv import PixivNativeAdapter\n"
                "def create_adapters():\n"
                "    adapter=PixivNativeAdapter()\n"
                "    adapter.info=adapter.info.__class__('twitter','Twitter Custom')\n"
                "    return adapter\n",
                encoding="utf-8",
            )
            bundle = load_adapter_plugins(
                [("twitter", "Twitter Builtin", lambda: self._adapter_with_id("twitter"))],
                root,
            )
            statuses = {report.plugin_id: report.status for report in bundle.reports}
            self.assertEqual(statuses, {"twitter": "replaced", "twitter-custom": "loaded"})
            self.assertEqual(bundle.reports[-1].replaces, "twitter")

            updated = set_external_plugin_enabled(plugin, False, root)
            self.assertFalse(updated["enabled"])
            self.assertFalse(json.loads(manifest.read_text(encoding="utf-8"))["enabled"])

    @staticmethod
    def _adapter_with_id(module_id: str):
        adapter = PixivNativeAdapter()
        adapter.info = adapter.info.__class__(module_id, module_id)
        return adapter

    def test_following_both_has_clean_completion_and_preserves_cache_when_partial(self):
        adapter = PixivNativeAdapter()
        existing = [
            {"user_id": "1", "display_name": "public old", "visibility": "show"},
            {"user_id": "2", "display_name": "private old", "visibility": "hide"},
        ]
        adapter.load_following_accounts = lambda: existing
        adapter._client = lambda _options=None: object()
        adapter._write_following_payload = lambda _payload: None

        complete_parts = [
            {"items": [{"user_id": "3", "visibility": "show"}], "complete": True, "new_count": 1},
            {"items": [{"user_id": "4", "visibility": "hide"}], "complete": True, "new_count": 1},
        ]
        with patch("software_app.adapters.pixiv.collect_following_pages", side_effect=complete_parts):
            complete = adapter.fetch_following("99", visibility="both", update_mode="all")
        self.assertEqual(complete["stopped_reason"], "")
        self.assertEqual({row["user_id"] for row in complete["items"]}, {"3", "4"})

        partial_parts = [
            {"items": [{"user_id": "3", "visibility": "show"}], "complete": True, "new_count": 1},
            {"items": [], "complete": False, "new_count": 0, "stopped_reason": "cancelled"},
        ]
        with patch("software_app.adapters.pixiv.collect_following_pages", side_effect=partial_parts):
            partial = adapter.fetch_following("99", visibility="both", update_mode="all")
        self.assertEqual(partial["stopped_reason"], "cancelled")
        self.assertEqual({row["user_id"] for row in partial["items"]}, {"1", "2", "3"})

    def test_pixiv_profile_and_account_history_cache(self):
        class AvatarResponse:
            content = b"avatar-bytes"

            @staticmethod
            def raise_for_status():
                return None

        class AvatarSession:
            @staticmethod
            def get(_url, **_kwargs):
                return AvatarResponse()

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            preview = TargetPreview(
                "pixiv",
                "7",
                "https://www.pixiv.net/users/7",
                title="Artist",
                description="Bio",
                metadata={"author_id": "7", "avatar_url": "https://i.pximg.net/avatar.png"},
            )
            cached = adapter._cache_profile_preview(type("Client", (), {"session": AvatarSession()})(), "7", preview)
            self.assertTrue(Path(cached.metadata["avatar_path"]).is_file())
            self.assertEqual(adapter.load_profile_preview("7")["display_name"], "Artist")
            adapter._write_account_history([{"url": "https://www.pixiv.net/artworks/8", "title": "Work"}])
            self.assertEqual(len(adapter.load_account_history()), 1)
            self.assertEqual(adapter.clear_account_history({"https://www.pixiv.net/artworks/8"}), 1)
            self.assertEqual(adapter.load_account_history(), [])

    def test_google_pixiv_routing_is_explicit_and_strict(self):
        self.assertEqual(
            GoogleSearchTabMixin._pixiv_artwork_id("https://www.pixiv.net/artworks/123456"),
            "123456",
        )
        self.assertEqual(GoogleSearchTabMixin._pixiv_artwork_id("https://x.com/a/status/123456"), "")
        self.assertEqual(GoogleSearchTabMixin._pixiv_artwork_id("https://pixiv.net/users/123456"), "")

    def test_pixiv_local_author_preview_uses_following_cache_before_network(self):
        adapter = PixivNativeAdapter()
        adapter.load_profile_preview = lambda _user_id: {}
        adapter.load_following_accounts = lambda: [
            {
                "user_id": "7",
                "display_name": "Artist & Friend",
                "bio": "A & B",
                "avatar_url": "https://i.pximg.net/avatar.jpg",
                "profile_url": "https://www.pixiv.net/users/7",
            }
        ]
        adapter._client = lambda _options=None: (_ for _ in ()).throw(AssertionError("local preview must not use network"))

        preview = adapter.preview_target("https://www.pixiv.net/users/7", {"input_kind": "user"})

        self.assertEqual(preview.title, "Artist & Friend")
        self.assertEqual(preview.description, "A & B")
        self.assertEqual(preview.metadata["avatar_url"], "https://i.pximg.net/avatar.jpg")
        self.assertEqual(preview.metadata["cache_source"], "following")

    def test_pixiv_following_partial_page_is_cached_before_callback(self):
        class Client:
            @staticmethod
            def fetch_following_page(_owner_id, page=1, visibility="show"):
                return {
                    "items": [{"user_id": "7", "display_name": "Artist", "visibility": visibility}],
                    "total": 1,
                    "has_more": False,
                    "source": "ajax",
                    "page": page,
                }

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            adapter._client = lambda _options=None: Client()
            cached_counts = []

            adapter.fetch_following(
                "99",
                on_accounts=lambda _payload: cached_counts.append(len(adapter.load_following_accounts())),
            )

            self.assertEqual(cached_counts, [1])

    def test_pixiv_work_summary_decodes_html_entities_for_visible_fields(self):
        summary = PixivCrawler._work_summary(
            {
                "illustTitle": "A &amp; B",
                "description": "Hello &amp;amp; world<br>next",
                "userName": "Painter &amp; Co",
                "tags": {"tags": [{"tag": "red&amp;blue"}]},
            }
        )
        self.assertEqual(summary["title"], "A & B")
        self.assertEqual(summary["description"], "Hello & world\nnext")
        self.assertEqual(summary["author_name"], "Painter & Co")
        self.assertEqual(summary["tags"], ["red&blue"])


if __name__ == "__main__":
    unittest.main()
