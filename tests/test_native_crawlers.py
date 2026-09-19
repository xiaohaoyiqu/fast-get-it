import ast
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.events import CallbackSet
from software_app.core.exports import export_url_candidates
from software_app.core.image_postprocess import build_image_artifacts
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.models import FileRecord
from software_app.core.security import redact_sensitive_data
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.common import make_session, path_component_error, safe_component
from software_app.crawlers.google_image import GoogleImageCrawler
from software_app.crawlers.google_image.client import (
    extract_candidate_links_from_html,
    is_google_challenge_page,
    is_google_image_expired_page,
    normalize_candidate_url,
    prefer_specific_pages,
)
from software_app.crawlers.jmcomic import JmComicCrawler, parse_jm_target
from software_app.crawlers.jmcomic.api import JmComicApiClient
from software_app.crawlers.pixiv.client import PixivCrawler, parse_pixiv_target
from software_app.crawlers.pixiv.following import (
    collect_following_pages,
    parse_following_html,
    parse_following_payload,
)
from software_app.crawlers.pixiv.ugoira import download_ugoira, normalize_ugoira_meta
from software_app.crawlers.twitter import following_collector as following_module
from software_app.crawlers.twitter import service as twitter_service_module
from software_app.crawlers.twitter.following_collector import (
    FollowingAccount,
    build_updated_accounts,
    collect_following,
    extract_accounts_from_api_payload,
    finish_following_payload,
)
from software_app.crawlers.twitter.profile_preview import (
    external_link_item,
    extract_profile_links_from_html,
    is_skeb_link,
    text_links,
)
from software_app.crawlers.twitter.driver_init import find_skeb_button_extension
from software_app.crawlers.twitter.service import TwitterCrawlerService
from software_app.ui.tk_app import SoftwareDesktop, _bounded_int, _collect_image_files
from software_app.ui.google_search_tab import GoogleSearchTabMixin, google_search_failure_hint
from software_app.ui.desktop_support import matches_search, normalized_search_text
from software_app.adapters import twitter as twitter_adapter_module
from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.adapters.jmcomic import JmComicNativeAdapter
from software_app.adapters.twitter import TwitterNativeAdapter, normalize_target, target_to_handle


class NativeCrawlerTargetTests(unittest.TestCase):
    class _Link:
        def __init__(self, href: str, text: str = "", **attributes):
            self.text = text
            self.attributes = {"href": href, **attributes}

        def get_attribute(self, name):
            return self.attributes.get(name, "")

    def test_common_session_uses_only_explicit_app_proxy(self):
        direct = make_session()
        proxied = make_session(proxy_url="http://127.0.0.1:7890")
        self.assertFalse(direct.trust_env)
        self.assertEqual(direct.proxies, {})
        self.assertFalse(proxied.trust_env)
        self.assertEqual(proxied.proxies["http"], "http://127.0.0.1:7890")
        self.assertEqual(proxied.proxies["https"], "http://127.0.0.1:7890")

    def test_pixiv_target_routing(self):
        self.assertEqual(parse_pixiv_target("https://www.pixiv.net/artworks/123").kind, "work")
        self.assertEqual(parse_pixiv_target("https://www.pixiv.net/users/456").kind, "user")
        self.assertEqual(parse_pixiv_target("landscape").kind, "search")

    def test_pixiv_candidate_inspection_reads_author_and_closes_session(self):
        class FakeSession:
            closed = False

            def close(self):
                self.closed = True

        class FakeClient:
            session = FakeSession()

            def fetch_preview(self, target):
                self.target = target
                return {"author_id": "456", "author_name": "Artist", "title": "Example"}

        adapter = PixivNativeAdapter()
        client = FakeClient()
        with patch.object(adapter, "_client", return_value=client) as client_factory:
            result = adapter.inspect_artwork("https://www.pixiv.net/en/artworks/123", proxy_url="")
        self.assertEqual(result["work_id"], "123")
        self.assertEqual(result["author_id"], "456")
        self.assertEqual(client.target.kind, "work")
        self.assertTrue(client.session.closed)
        client_factory.assert_called_once_with({"proxy_url": ""})

    def test_similarity_candidate_can_route_only_x_post_to_native_downloader(self):
        row = {"url": "https://example.com/redirect",
               "final_url": "https://twitter.com/pixiv/status/2023978485316768106/photo/1?ref=abc"}
        self.assertEqual(GoogleSearchTabMixin._twitter_result_target(row),
                         "https://x.com/pixiv/status/2023978485316768106")
        self.assertEqual(GoogleSearchTabMixin._twitter_result_target(
            {"url": "https://x.com/pixiv/status/2023978485316768106.evil.example"}), "")
        self.assertEqual(GoogleSearchTabMixin._twitter_result_target(
            {"url": "https://x.com.evil.example/pixiv/status/2023978485316768106"}), "")

    def test_google_challenge_is_distinguished_from_empty_results(self):
        self.assertTrue(is_google_challenge_page(
            "<body onload=\"getElementById('captcha')\"><script src='recaptcha__zh_cn.js'></script></body>"
        ))
        self.assertFalse(is_google_challenge_page("<html><title>Image results</title></html>"))

    def test_jmcomic_target_routing(self):
        self.assertEqual(parse_jm_target("JM123").kind, "album")
        self.assertEqual(parse_jm_target("https://18comic.vip/photo/456").kind, "photo")
        self.assertEqual(parse_jm_target("https://18comic.vip/photo/456", "photo").value, "456")
        self.assertEqual(parse_jm_target("https://18comic.vip/album/?id=789", "album").value, "789")
        self.assertEqual(parse_jm_target("keyword").kind, "search")

    def test_pixiv_platform_is_marked_ready(self):
        self.assertEqual(PixivNativeAdapter().info.stage, "ready")

    def test_jmcomic_photo_parser_uses_image_domain_and_safe_scramble_fallback(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        page = '<script>var page_arr = ["00001.jpg", "../bad.jpg"];</script>' \
               '<img src="https://cdn.example/media/albums/blank.jpg">'
        photo = crawler._parse_photo(page, "400000")
        self.assertEqual(photo["images"], ["https://cdn.example/media/photos/400000/00001.jpg"])
        self.assertEqual(photo["scramble_id"], "220980")

    def test_jmcomic_empty_chapter_fails_instead_of_reporting_success(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler._get_text = lambda _url: "<html><title>Blocked</title></html>"
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            task = DownloadTask("jm-empty", "jmcomic", "123", Path(directory), {"input_kind": "photo"})
            with self.assertRaisesRegex(ValueError, "未返回图片列表"):
                crawler.download(task, CallbackSet(), threading.Event())
            self.assertFalse(list(Path(directory).rglob("*.part")))

    def test_jmcomic_photo_download_saves_valid_image_atomically(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler._get_text = lambda _url: (
            '<title>Chapter | JM</title><script>var scramble_id = 0; '
            'var page_arr = ["00001.jpg"];</script>'
            '<img data-original="https://cdn.example/media/photos/123/00001.jpg">'
        )
        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (10, 20, 30)).save(payload, format="PNG")
        crawler._get_response = lambda _url, timeout: type("Response", (), {"content": payload.getvalue()})()
        records = []
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            task = DownloadTask("jm-image", "jmcomic", "123", Path(directory),
                                {"input_kind": "photo", "image_format": "png"})
            crawler.download(task, CallbackSet(on_file=records.append), threading.Event())
            self.assertEqual(len(records), 1)
            with Image.open(records[0].path) as saved:
                self.assertEqual(saved.getpixel((0, 0)), (10, 20, 30))
            self.assertFalse(list(Path(directory).rglob("*.part")))

    def test_jmcomic_candidate_tags_reach_album_folder_with_five_tag_limit(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler._get_text = lambda _url: (
            '<h1 class="book-name" id="book-name">Title</h1>'
            '<span itemprop="genre" data-type="tags"><a>site</a></span>'
        )
        folders = []
        crawler._download_photo = lambda _photo, _album, folder, *_args: folders.append(folder)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            task = DownloadTask("jm-tags", "jmcomic", "123", Path(directory), {
                "jm_download_cover": False,
                "jm_candidate_tags": ["one", "two", "three", "four", "five"],
            })
            crawler.download(task, CallbackSet(), threading.Event())
        self.assertEqual(len(folders), 1)
        self.assertIn("[one] [two] [three] [four] [five]", str(folders[0]))
        self.assertNotIn("[site]", str(folders[0]))

    def test_jmcomic_live_album_preview_shows_cover_and_first_page(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        album = (
            '<h1 class="book-name" id="book-name">Title</h1>'
            '<meta property="og:image" content="https://cdn.example/cover.jpg">'
            '<a href="/photo/456">Chapter 1</a>'
        )
        photo = (
            '<script>var page_arr = ["00001.jpg"];</script>'
            '<img data-original="https://cdn.example/media/photos/456/00001.jpg">'
        )
        crawler._get_text = lambda url: photo if "/photo/" in url else album
        preview = crawler.preview("123", {"live": True})
        self.assertEqual(preview.metadata["first_page_url"], "https://cdn.example/media/photos/456/00001.jpg")
        self.assertEqual(preview.metadata["avatar_url"], "https://cdn.example/cover.jpg")
        self.assertIn("打开第一页", preview.metadata["page_html"])

    def test_jmcomic_album_parses_legacy_episode_markup(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        detail = crawler._parse_album(
            '<h1 class="book-name" id="book-name">Title</h1>'
            '<a data-album="456"><li>第1話 起始</li></a>', "123"
        )
        self.assertEqual(detail["chapters"], [{"id": "456", "title": "第1話 起始"}])

    def test_jmcomic_preview_image_uses_crawler_session_and_checks_chapter(self):
        payload = io.BytesIO()
        Image.new("RGB", (4, 4), (10, 20, 30)).save(payload, format="PNG")

        class FakeClient:
            def _get_response(self, _url, timeout):
                self.timeout = timeout
                return type("Response", (), {"content": payload.getvalue()})()

        adapter = JmComicNativeAdapter()
        client = FakeClient()
        with patch.object(adapter, "_client", return_value=client):
            data = adapter.fetch_preview_image_bytes(
                "https://cdn.example/media/photos/456/00001.jpg", photo_id="456", scramble_id="0"
            )
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.size, (4, 4))
        self.assertEqual(client.timeout, (5, 30))
        with self.assertRaisesRegex(ValueError, "当前章节"):
            adapter.fetch_preview_image_bytes("https://cdn.example/media/photos/123/00001.jpg", photo_id="456")

    def test_jmcomic_download_skips_blocked_author_after_reading_album(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler._get_text = lambda _url: (
            '<h1 class="book-name" id="book-name">Title</h1>'
            '作者：<span itemprop="author" data-type="author"><a>Blocked Author</a></span>'
        )
        crawler._download_photo = lambda *_args: self.fail("blocked author must not download")
        progress = []
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            blocklist = BlocklistStore(Path(directory) / "blocklist.json")
            blocklist.add_group(("jmcomic_author", "blocked author"))
            task = DownloadTask("jm-blocked", "jmcomic", "123", Path(directory), {
                "_blocklist_path": str(blocklist.path), "jm_download_cover": False,
            })
            crawler.download(task, CallbackSet(on_progress=progress.append), threading.Event())
        self.assertTrue(any("黑名单已跳过" in event.message for event in progress))

    def test_pixiv_search_mode_is_sent_to_native_search(self):
        crawler = object.__new__(PixivCrawler)
        paths = []

        def body(path):
            paths.append(path)
            return {"illustManga": {"data": [{"id": "1", "title": "M", "illustType": 1}]}}

        crawler._body = body
        rows = crawler.search("tag", search_mode="标签（漫画）")
        self.assertIn("type=manga", paths[0])
        self.assertEqual(rows[0]["type"], "漫画")
        crawler.search("caption", search_mode="标题 / 简介")
        self.assertIn("s_mode=s_tc", paths[1])

    def test_pixiv_search_uses_additional_pages_for_larger_limits(self):
        crawler = object.__new__(PixivCrawler)
        paths = []

        def body(path):
            paths.append(path)
            page = 2 if "&p=2&" in path else 1
            start = 1 if page == 1 else 21
            return {
                "illustManga": {
                    "total": 40,
                    "data": [
                        {"id": str(index), "title": f"work-{index}", "illustType": 0}
                        for index in range(start, start + 20)
                    ],
                }
            }

        crawler._body = body
        rows = crawler.search("tag", limit=30)

        self.assertEqual(len(rows), 30)
        self.assertEqual(len(paths), 2)
        self.assertIn("&p=2&", paths[1])

    def test_pixiv_live_search_preview_keeps_selected_search_mode(self):
        crawler = object.__new__(PixivCrawler)
        seen = []
        crawler.search = lambda keyword, limit=20, search_mode="", options=None: seen.append(
            (keyword, search_mode, options)
        ) or []

        preview = crawler.preview("landscape", {"live": True, "search_mode": "标题 / 简介"})

        self.assertEqual(seen, [("landscape", "标题 / 简介", {"live": True, "search_mode": "标题 / 简介"})])
        self.assertEqual(preview.metadata["input_kind"], "search")

    def test_pixiv_user_preview_cleans_bio_and_keeps_external_links(self):
        crawler = object.__new__(PixivCrawler)
        crawler._body = lambda _path: {
            "name": "画师",
            "commentHtml": "接受委托<br>欢迎联系",
            "imageBig": "https://i.pximg.net/avatar.jpg",
            "webpage": "https://artist.example/",
            "social": {
                "twitter": {"url": "https://x.com/artist"},
                "empty": {"url": ""},
            },
        }

        preview = crawler.preview(
            "https://www.pixiv.net/users/123",
            {"live": True, "input_kind": "user"},
        )

        self.assertEqual(preview.description, "接受委托\n欢迎联系")
        self.assertEqual(
            [item["url"] for item in preview.metadata["external_links"]],
            ["https://artist.example/", "https://x.com/artist"],
        )

    def test_pixiv_following_ajax_payload_keeps_profile_fields(self):
        result = parse_following_payload(
            {
                "body": {
                    "total": 260,
                    "users": [
                        {
                            "userId": "33854864",
                            "userName": "だいすきつね",
                            "profileImageUrl": "https://i.pximg.net/avatar.jpg",
                            "userComment": "Skeb 受付中",
                        }
                    ],
                }
            }
        )
        self.assertEqual(result["total"], 260)
        self.assertEqual(result["items"][0]["user_id"], "33854864")
        self.assertEqual(result["items"][0]["display_name"], "だいすきつね")
        self.assertEqual(result["items"][0]["bio"], "Skeb 受付中")

    def test_pixiv_following_html_uses_stable_user_attributes_and_pages(self):
        source = """
        <h2>用户</h2><div><span>260</span></div>
        <a href="/users/53292267/following?p=1">1</a>
        <span aria-current="page">2</span>
        <a href="/users/53292267/following?p=7">7</a>
        <a data-gtm-value="33854864" href="/users/33854864">
          <img alt="だいすきつね" src="https://i.pximg.net/avatar.jpg">
        </a>
        <a data-gtm-value="33854864" href="/users/33854864">だいすきつね</a>
        <div>ご依頼は Skeb でお願いします</div>
        <button data-gtm-user-id="33854864">フォロー</button>
        """
        result = parse_following_html(source)
        self.assertEqual((result["current_page"], result["max_page"], result["total"]), (2, 7, 260))
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["bio"], "ご依頼は Skeb でお願いします")

    def test_pixiv_new_update_merges_missing_old_but_full_update_removes_it(self):
        existing = [
            {"user_id": "2", "display_name": "old two"},
            {"user_id": "9", "display_name": "unfollowed"},
        ]

        def fetch_page(page):
            pages = {
                1: {
                    "items": [
                        {"user_id": "1", "display_name": "one"},
                        {"user_id": "2", "display_name": "new two"},
                    ],
                    "total": 3,
                    "has_more": True,
                },
                2: {
                    "items": [{"user_id": "3", "display_name": "three"}],
                    "total": 3,
                    "has_more": False,
                },
            }
            return pages[page]

        incremental = collect_following_pages(fetch_page, existing, update_mode="new")
        full = collect_following_pages(fetch_page, existing, update_mode="all")
        self.assertEqual([row["user_id"] for row in incremental["items"]], ["1", "2", "3", "9"])
        self.assertEqual(incremental["removed_count"], 0)
        self.assertEqual([row["user_id"] for row in full["items"]], ["1", "2", "3"])
        self.assertEqual(full["removed_count"], 1)

    def test_pixiv_new_update_stops_after_stable_old_id_overlap(self):
        existing = [{"user_id": str(index), "display_name": f"old-{index}"} for index in range(1, 51)]
        calls = []

        def fetch_page(page):
            calls.append(page)
            return {
                "items": [
                    {"user_id": "100", "display_name": "new"},
                    *({"user_id": str(index), "display_name": f"old-{index}"} for index in range(1, 25)),
                ],
                "total": 100,
                "has_more": True,
                "source": "ajax",
            }

        result = collect_following_pages(fetch_page, existing, update_mode="new", known_overlap=24)

        self.assertEqual(calls, [1])
        self.assertEqual(result["stopped_reason"], "known_overlap")
        self.assertEqual(result["new_count"], 1)
        self.assertEqual(len(result["items"]), 51)

    def test_pixiv_download_honors_a_pre_cancelled_task(self):
        crawler = object.__new__(PixivCrawler)
        cancelled = threading.Event()
        cancelled.set()
        task = DownloadTask(
            task_id="pixiv-cancel",
            module_id="pixiv",
            target="123",
            output_dir=Path.cwd(),
            options={"input_kind": "work"},
        )
        with self.assertRaises(TaskCancelled):
            crawler.download(task, CallbackSet(), cancelled)

    def test_pixiv_download_reports_progress_and_structured_file_metadata(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            crawler = object.__new__(PixivCrawler)
            crawler.session = object()

            def body(path):
                if path.endswith("/pages"):
                    return [{"urls": {"original": "https://i.pximg.net/work_p0.jpg"}}]
                return {
                    "illustTitle": "作品标题",
                    "userId": "456",
                    "userName": "画师",
                    "createDate": "2026-09-01T12:00:00+09:00",
                    "tags": {"tags": [{"tag": "风景"}]},
                    "aiType": 0,
                }

            crawler._body = body
            progress = []
            files = []
            task = DownloadTask(
                task_id="pixiv-download",
                module_id="pixiv",
                target="123",
                output_dir=Path(temp_dir),
                options={"input_kind": "work", "image_format": "original", "filter_ai": False},
            )

            def fake_download(_session, _url, destination, _cancel_event):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"image")
                return 5

            with patch("software_app.crawlers.pixiv.client.download_file", side_effect=fake_download):
                crawler.download(
                    task,
                    CallbackSet(on_progress=progress.append, on_file=files.append),
                    threading.Event(),
                )

            self.assertEqual(progress[-1].percent, 100.0)
            self.assertEqual(files[0].title, "作品标题")
            self.assertEqual(files[0].chapter, "1")
            self.assertEqual(files[0].metadata["work_title"], "作品标题")

    def test_pixiv_ugoira_uses_frame_delays_and_safe_archive(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            archive_bytes = io.BytesIO()
            with zipfile.ZipFile(archive_bytes, "w") as archive:
                for filename, color in (("000000.jpg", "red"), ("000001.jpg", "blue")):
                    image_bytes = io.BytesIO()
                    Image.new("RGB", (8, 8), color).save(image_bytes, format="JPEG")
                    archive.writestr(filename, image_bytes.getvalue())

            def fake_download(_session, _url, destination, _cancel_event):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive_bytes.getvalue())
                return destination.stat().st_size

            meta = {
                "originalSrc": "https://i.pximg.net/animation.zip",
                "frames": [
                    {"file": "000000.jpg", "delay": 80},
                    {"file": "000001.jpg", "delay": 160},
                ],
            }
            with patch("software_app.crawlers.pixiv.ugoira.download_file", side_effect=fake_download):
                files = download_ugoira(object(), meta, root, "123", {"animation_format": "gif"}, threading.Event())

            self.assertEqual(files[0][0].suffix, ".gif")
            with Image.open(files[0][0]) as animation:
                self.assertEqual(animation.n_frames, 2)
                animation.seek(0)
                self.assertEqual(animation.info["duration"], 80)
                animation.seek(1)
                self.assertEqual(animation.info["duration"], 160)
            with self.assertRaises(ValueError):
                normalize_ugoira_meta({"originalSrc": "x", "frames": [{"file": "a.jpg"}, {"file": "../a.jpg"}]})

    def test_pixiv_incomplete_html_total_never_deletes_old_cache(self):
        result = collect_following_pages(
            lambda _page: {
                "items": [{"user_id": "1", "display_name": "one"}],
                "total": 260,
                "has_more": False,
                "source": "html",
            },
            [{"user_id": "9", "display_name": "old"}],
            update_mode="all",
        )
        self.assertFalse(result["complete"])
        self.assertEqual(result["stopped_reason"], "incomplete_total")
        self.assertEqual([row["user_id"] for row in result["items"]], ["1", "9"])

    def test_pixiv_cookie_import_accepts_browser_export_without_logging_values(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            source = root / "browser-cookies.json"
            source.write_text(
                json.dumps(
                    [
                        {"name": "PHPSESSID", "value": "12345_token", "domain": ".pixiv.net"},
                        {"name": "p_ab_id", "value": "variant"},
                    ]
                ),
                encoding="utf-8",
            )
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = root / "runtime"
            self.assertEqual(adapter.import_cookie_file(source), 2)
            saved = json.loads((adapter.runtime_data_dir / "cookies.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["PHPSESSID"], "12345_token")
            self.assertEqual(adapter.cookie_account_id(), "12345")
            self.assertFalse(any("Pixiv PHPSESSID" in warning for warning in adapter.validate()))

    def test_pixiv_following_cache_info_filters_invalid_rows(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(temp_dir)
            adapter.following_output_file().write_text(
                json.dumps(
                    {
                        "platform": "pixiv",
                        "account": "53292267",
                        "partial": True,
                        "update_mode": "new",
                        "items": [
                            {"user_id": "123", "display_name": "Artist"},
                            {"user_id": "not-a-number"},
                            {"user_id": "123", "display_name": "Duplicate"},
                        ],
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual([row["user_id"] for row in adapter.load_following_accounts()], ["123"])
            info = adapter.following_cache_info()
            self.assertTrue(info["valid"])
            self.assertEqual(info["count"], 1)
            self.assertTrue(info["partial"])
            self.assertEqual(info["account"], "53292267")
            csv_output = Path(temp_dir) / "导出.csv"
            json_output = Path(temp_dir) / "导出.json"
            self.assertEqual(adapter.export_following_accounts(csv_output), 1)
            self.assertEqual(adapter.export_following_accounts(json_output), 1)
            self.assertIn("Artist", csv_output.read_text(encoding="utf-8-sig"))
            self.assertEqual(json.loads(json_output.read_text(encoding="utf-8"))["platform"], "pixiv")

    def test_jmcomic_author_search_uses_original_main_tag(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        urls = []

        def get_text(url):
            urls.append(url)
            return '<a href="/album/123/work"><span title="Book"></span></a>'

        crawler._get_text = get_text
        rows = crawler.search("artist", search_mode="作者搜索", options={"match_mode": "exact"})
        self.assertIn("main_tag=2", urls[0])
        self.assertEqual(rows[0]["search_mode"], "作者搜索")

    def test_jmcomic_category_uses_saved_filters_and_cover(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        urls = []
        crawler._get_text = lambda url: urls.append(url) or (
            '<a href="/album/123/work"><img data-original="/cover/123.jpg" title="Book"></a>'
        )
        rows = crawler.search(
            "",
            search_mode="分类 / 排行",
            options={"category": "doujin", "order_by": "mv", "time_range": "w"},
        )
        self.assertIn("/albums/doujin?", urls[0])
        self.assertIn("o=mv", urls[0])
        self.assertIn("t=w", urls[0])
        self.assertEqual(len(urls), 2)
        self.assertEqual(rows[0]["cover_url"], "https://18comic.test/cover/123.jpg")

    def test_jmcomic_favorites_requires_cookie_and_builds_folder_url(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler.session = type("Session", (), {"cookies": {}})()
        with self.assertRaisesRegex(PermissionError, "Cookie"):
            crawler.search("alice", search_mode="收藏夹")

        crawler.session.cookies = {"AVS": "present"}
        urls = []
        crawler._get_account_text = lambda url, _feature: urls.append(url) or '<a href="/album/9/a" title="Saved"></a>'
        rows = crawler.search(
            "alice", search_mode="收藏夹", options={"favorite_folder_id": "7", "order_by": "tf"}
        )
        self.assertIn("/user/alice/favorite/albums?", urls[0])
        self.assertIn("folder=7", urls[0])
        self.assertIn("folder_id=7", urls[0])
        self.assertEqual(rows[0]["id"], "9")

    def test_jmcomic_personal_page_routes_and_novel_read_only_candidates(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler.session = type("Session", (), {"cookies": {"AVS": "present"}})()
        urls = []

        def get_account_text(url, _feature):
            urls.append(url)
            if "/novel" in url:
                return '<a href="/novel/88/story" title="Saved novel"><img src="/n.jpg"></a>'
            return '<a href="/album/77/story" title="Saved album"><img src="/a.jpg"></a>'

        crawler._get_account_text = get_account_text
        options = {"favorite_username": "@cacanide", "novel_favorite_folder_id": "4459"}
        cases = (
            ("追更连载", "/user/cacanide/tracking?"),
            ("漫画观看记录", "/user/cacanide/favorite/watchlist?"),
            ("小说观看记录", "/user/cacanide/favorite/novel_watchlist?"),
            ("小说收藏夹", "/user/cacanide/favorite/novels?"),
        )
        for mode, expected_path in cases:
            rows = crawler.search("", search_mode=mode, options=options)
            self.assertIn(expected_path, urls[-1])
            self.assertEqual(len(rows), 1)
            if "小说" in mode:
                self.assertFalse(rows[0]["downloadable"])
                self.assertEqual(rows[0]["input_kind"], "novel")

    def test_jmcomic_cf_clearance_is_not_treated_as_login(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        crawler.session = type("Session", (), {"cookies": {"cf_clearance": "fixture"}})()
        with self.assertRaisesRegex(PermissionError, "AVS"):
            crawler.search("alice", search_mode="漫画收藏夹")

    def test_jmcomic_cookie_status_and_account_candidates_do_not_expose_values(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = JmComicNativeAdapter()
            adapter.runtime_data_dir = root
            source = root / "source.json"
            source.write_text(json.dumps({"cf_clearance": "secret"}), encoding="utf-8")
            adapter.import_cookie_file(source)
            status = adapter.cookie_status()
            self.assertTrue(status["has_clearance"])
            self.assertFalse(status["has_login"])
            self.assertNotIn("secret", str(status))

            source.write_text(json.dumps({"AVS": "login-secret"}), encoding="utf-8")
            adapter.import_cookie_file(source)
            login_status = adapter.cookie_status()
            self.assertTrue(login_status["has_login"])
            self.assertTrue(login_status["has_login_candidate"])
            self.assertIn("候选", login_status["message"])

        novel = JmComicApiClient._candidate(
            {"id": "4459", "name": "Saved novel", "author": ["Writer"]},
            "novel", "novel_favorites", "https://mirror.example",
        )
        history = JmComicApiClient._candidate(
            {"id": "4721881", "name": "Read album"},
            "album", "watch_list", "https://mirror.example",
        )
        self.assertEqual(novel["url"], "https://mirror.example/novel/4459")
        self.assertFalse(novel["downloadable"])
        self.assertEqual(history["url"], "https://mirror.example/album/4721881")
        self.assertTrue(history["downloadable"])

    def test_jmcomic_imports_copied_cookie_request_header(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = JmComicNativeAdapter()
            adapter.runtime_data_dir = root
            source = root / "cookies.txt"
            source.write_text(
                "Cookie:\nAVS=session-value; cf_clearance=clearance-value; ipm5=route-value",
                encoding="utf-8",
            )
            (root / "browser_headers.json").write_text('{"referer": "stale"}', encoding="utf-8")
            self.assertEqual(adapter.import_cookie_file(source), 3)
            status = adapter.cookie_status()
            self.assertTrue(status["has_login_candidate"])
            self.assertTrue(status["has_clearance"])
            self.assertNotIn("session-value", str(status))
            self.assertFalse((root / "browser_headers.json").exists())

    def test_jmcomic_imports_full_request_headers_without_persisting_cookie_header(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = JmComicNativeAdapter()
            adapter.runtime_data_dir = root
            source = root / "request-headers.txt"
            source.write_text(
                ":authority:\n18comic.example\n"
                "accept:\ntext/html,application/xhtml+xml\n"
                "cookie:\nAVS=session-value; cf_clearance=clearance-value\n"
                "sec-ch-ua:\n\"Chromium\";v=\"122\"\n"
                "user-agent:\nMozilla/5.0 Test Browser\n"
                "cache-control:\nmax-age=0\n"
                "x-unsafe-header:\nshould-not-be-saved\n"
                "Cache-Control:\npublic, max-age=86400\n",
                encoding="utf-8",
            )
            self.assertEqual(adapter.import_cookie_file(source), 2)
            headers = adapter.browser_headers()
            self.assertEqual(headers["accept"], "text/html,application/xhtml+xml")
            self.assertEqual(headers["user-agent"], "Mozilla/5.0 Test Browser")
            self.assertEqual(headers["cache-control"], "max-age=0")
            self.assertNotIn("cookie", {name.casefold() for name in headers})
            self.assertNotIn("x-unsafe-header", headers)
            self.assertNotIn("session-value", (root / "browser_headers.json").read_text(encoding="utf-8"))

    def test_jmcomic_api_ignores_ambient_proxy_and_labels_html_challenge(self):
        client = JmComicApiClient()
        self.assertFalse(client.session.trust_env)
        previous_cache = JmComicApiClient._domain_cache
        JmComicApiClient._domain_cache = ()
        self.addCleanup(setattr, JmComicApiClient, "_domain_cache", previous_cache)

        class HtmlResponse:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = "<!doctype html><html><title>Just a moment</title></html>"

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                raise ValueError("not json")

        client.session.get = lambda *_args, **_kwargs: HtmlResponse()
        with self.assertRaisesRegex(RuntimeError, "验证页"):
            client._get("watch_list", {"page": "1"})

        self.assertEqual(
            JmComicApiClient._normalize_api_domains(
                ["api.example", "https://api.example", "http://plain.example", "https://127.0.0.1", ""]
            ),
            ("https://api.example",),
        )

    def test_jmcomic_api_labels_rejected_avs(self):
        client = JmComicApiClient()
        previous_cache = JmComicApiClient._domain_cache
        JmComicApiClient._domain_cache = ()
        self.addCleanup(setattr, JmComicApiClient, "_domain_cache", previous_cache)

        class UnauthorizedResponse:
            status_code = 401

        client.session.get = lambda *_args, **_kwargs: UnauthorizedResponse()
        with self.assertRaisesRegex(PermissionError, "AVS 已失效"):
            client._get("watch_list", {"page": "1"})

    def test_jmcomic_exact_search_continues_until_exact_title(self):
        crawler = object.__new__(JmComicCrawler)
        crawler.domain = "https://18comic.test"
        urls = []

        def get_text(url):
            urls.append(url)
            if "page=1" in url:
                return '<a href="/album/1/a" title="Similar"></a>'
            if "page=2" in url:
                return '<a href="/album/2/a" title="Wanted"></a>'
            return ""

        crawler._get_text = get_text
        rows = crawler.search("Wanted", options={"match_mode": "exact"})
        self.assertEqual([row["id"] for row in rows], ["2"])
        self.assertTrue(any("page=2" in url for url in urls))

    def test_jmcomic_access_block_in_success_response_is_an_error(self):
        crawler = object.__new__(JmComicCrawler)
        crawler._get_response = lambda _url, timeout: type("Response", (), {
            "text": "<html>Restricted Access!</html>", "apparent_encoding": "utf-8", "encoding": "",
        })()
        with self.assertRaisesRegex(PermissionError, "更换可用域名"):
            crawler._get_text("https://18comic.test/")

    def test_jmcomic_retries_one_transient_403(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code
                self.closed = False

            def close(self):
                self.closed = True

            def raise_for_status(self):
                if self.status_code >= 400:
                    raise RuntimeError(str(self.status_code))

        first = Response(403)
        second = Response(200)
        responses = iter((first, second))
        crawler = object.__new__(JmComicCrawler)
        crawler.session = type("Session", (), {"get": lambda _self, *_args, **_kwargs: next(responses)})()
        with patch("software_app.crawlers.jmcomic.client.time.sleep") as sleep:
            result = crawler._get_response("https://18comic.test/", (1, 1))
        self.assertIs(result, second)
        self.assertTrue(first.closed)
        sleep.assert_called_once_with(0.6)

    def test_common_image_postprocess_splits_at_thirty(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            sources = []
            for index in range(31):
                path = root / f"{index:02d}.png"
                Image.new("RGB", (4, 4), (index, 0, 0)).save(path)
                sources.append(path)
            artifacts = build_image_artifacts(sources, root / "out", "chapter", "both")
            self.assertEqual(len(artifacts), 4)
            self.assertTrue(all(path.is_file() and path.stat().st_size for path, _kind in artifacts))

    def test_google_image_preview_requires_a_file(self):
        preview = GoogleImageCrawler().preview("does-not-exist.png")
        self.assertEqual(preview.status, "error")

    def test_google_image_expired_message_is_distinguished_from_empty_results(self):
        self.assertTrue(is_google_image_expired_page(
            "视觉搜索内容已过期 图片未找到 您用于搜索的图片未与您的账号关联。请重新上传图片，然后重试"
        ))
        self.assertFalse(is_google_image_expired_page("AI 概览 搜索结果 相关图片"))

    def test_google_verification_wait_finishes_when_results_appear(self):
        class ResultDriver:
            @staticmethod
            def execute_script(_script):
                return ["https://example.com/page"]

        messages = []
        GoogleImageCrawler()._wait_for_initial_results(
            ResultDriver(), threading.Event(), wait_seconds=60, on_status=messages.append
        )
        self.assertIn("已检测到来源页面候选", messages[-1])

    def test_google_search_errors_have_visible_recovery_hints(self):
        self.assertIn("重新上传", google_search_failure_hint("Google 此次上传的图片已过期"))
        self.assertIn("浏览器", google_search_failure_hint("Google 要求人机验证"))
        self.assertEqual(google_search_failure_hint("普通网络错误"), "")

    def test_google_candidate_links_are_recovered_from_escaped_page_data(self):
        page = r'{"result":"https:\/\/artist.example\/works\/123?from=lens\u0026page=1"}'
        self.assertEqual(
            extract_candidate_links_from_html(page),
            ["https://artist.example/works/123?from=lens&page=1"],
        )

    def test_google_wrapped_public_posts_keep_original_platform_url(self):
        self.assertEqual(
            normalize_candidate_url("https://www.google.com/url?q=https%3A%2F%2Fx.com%2Fpixiv%2Fstatus%2F2023978485316768106"),
            "https://x.com/pixiv/status/2023978485316768106",
        )
        self.assertEqual(
            normalize_candidate_url("https://www.google.com/imgres?imgrefurl=https%3A%2F%2Fbsky.app%2Fprofile%2Fbsky.app%2Fpost%2F3mu3jzayuys2k"),
            "https://bsky.app/profile/bsky.app/post/3mu3jzayuys2k",
        )

    def test_google_verification_page_metadata_is_not_a_candidate(self):
        self.assertEqual(normalize_candidate_url("http://schema.org/WebPage"), "")
        self.assertEqual(normalize_candidate_url("http://www.w3.org/2000/svg"), "")
        self.assertEqual(normalize_candidate_url("http://��https://��"), "")
        self.assertEqual(normalize_candidate_url("https://t.co"), "")

    def test_google_homepage_is_removed_when_a_specific_page_exists(self):
        self.assertEqual(
            prefer_specific_pages(
                ["https://example.com", "https://example.com/work/1", "https://other.example"]
            ),
            ["https://example.com/work/1", "https://other.example"],
        )

    def test_twitter_exact_handle_is_searchable_without_local_history(self):
        rows = TwitterNativeAdapter().search_targets("@OpenAI")
        self.assertEqual(rows[-1]["handle"], "OpenAI")
        self.assertEqual(rows[-1]["source"], "direct_handle")

    def test_twitter_online_preview_uses_current_python_runtime(self):
        self.assertIs(twitter_adapter_module.sys, sys)

    def test_twitter_home_is_not_mistaken_for_a_user_named_home(self):
        self.assertEqual(target_to_handle("https://x.com/home"), "")
        self.assertEqual(normalize_target("https://x.com/home"), "https://x.com/home")

    def test_twitter_profile_link_prefers_expanded_skeb_url(self):
        item = external_link_item(
            self._Link("https://t.co/abc", **{"data-expanded-url": "https://skeb.jp/@artist"}),
            resolve_shortener=False,
        )
        self.assertEqual(item["url"], "https://skeb.jp/@artist")
        self.assertTrue(is_skeb_link(item))

    def test_twitter_profile_link_cleans_display_url_ellipsis(self):
        item = external_link_item(
            self._Link("https://t.co/abc", text="skeb.jp/@artist…"),
            resolve_shortener=False,
        )
        self.assertEqual(item["url"], "https://skeb.jp/@artist")

    def test_twitter_profile_text_links_do_not_turn_email_into_websites(self):
        links = text_links("mail: artist.name@gmail.com / skeb.jp/@artist")
        self.assertEqual(links, [{"label": "skeb.jp/@artist", "url": "https://skeb.jp/@artist"}])

    def test_twitter_absolute_download_path_uses_author_folder_not_drive_name(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            output = root / "downloads"
            author = output / "artist"
            author.mkdir(parents=True)
            media = author / "123_img01.png"
            media.write_bytes(b"png")
            adapter = TwitterNativeAdapter(root / "crawler")
            adapter.runtime_data_dir = root / "runtime"
            adapter.runtime_data_dir.mkdir()
            (adapter.runtime_data_dir / "downloaded_urls.json").write_text(
                '{"items":{"1":{"file":' + json.dumps(str(media)) + ',"type":"image"}}}',
                encoding="utf-8",
            )
            rows = adapter.downloaded_users(output)
            self.assertEqual([row["name"] for row in rows], ["artist"])

    def test_twitter_history_merges_unique_display_name_with_profile(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            runtime = root / "runtime"
            output = root / "downloads"
            author = output / "Artist Name"
            runtime.mkdir()
            author.mkdir(parents=True)
            media = author / "1.png"
            media.write_bytes(b"png")
            (runtime / "following_accounts.json").write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "handle": "artist_id",
                                "display_name": "Artist Name",
                                "bio": "Artist bio",
                                "links": [{"label": "Site", "url": "https://artist.example"}],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "downloaded_users.json").write_text(
                json.dumps(
                    {
                        "items": {
                            "artist_id": {
                                "handle": "artist_id",
                                "downloads": 2,
                                "last_downloaded_at": "2026-09-13 00:00:00",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (runtime / "downloaded_urls.json").write_text(
                json.dumps(
                    {
                        "items": {
                            "media": {
                                "file": str(media),
                                "type": "image",
                                "saved_at": "2026-09-13 00:00:01",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            adapter = TwitterNativeAdapter(root / "crawler")
            adapter.runtime_data_dir = runtime
            rows = adapter.downloaded_users(output)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["handle"], "artist_id")
            self.assertEqual(rows[0]["name"], "Artist Name")
            self.assertEqual(rows[0]["bio"], "Artist bio")
            self.assertEqual(rows[0]["links"][0]["url"], "https://artist.example")
            self.assertEqual(rows[0]["count"], 3)

    def test_twitter_history_can_clear_selected_without_deleting_local_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            runtime = root / "runtime"
            output = root / "downloads"
            artist = output / "Artist Name"
            runtime.mkdir()
            artist.mkdir(parents=True)
            media = artist / "1.png"
            media.write_bytes(b"png")
            (runtime / "following_accounts.json").write_text(
                json.dumps({"items": [{"handle": "artist_id", "display_name": "Artist Name"}]}), encoding="utf-8"
            )
            (runtime / "downloaded_users.json").write_text(
                json.dumps({"items": {"artist_id": {"handle": "artist_id", "downloads": 1}}}), encoding="utf-8"
            )
            (runtime / "downloaded_urls.json").write_text(
                json.dumps({"items": {"media": {"file": str(media), "type": "image"}}}), encoding="utf-8"
            )
            adapter = TwitterNativeAdapter(root / "crawler")
            adapter.runtime_data_dir = runtime

            counts = adapter.clear_selected_download_records([{"handle": "artist_id", "name": "Artist Name"}])

            self.assertEqual(counts["downloaded_users"], 1)
            self.assertEqual(counts["downloaded_urls"], 1)
            self.assertTrue(media.is_file())
            self.assertEqual(adapter.downloaded_users(output), [])

    def test_twitter_known_avatar_url_is_cached_without_profile_browser(self):
        class FakeResponse:
            headers = {"Content-Type": "image/jpeg"}
            content = b"jpeg-data"

            @staticmethod
            def close():
                return None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = TwitterNativeAdapter(root / "crawler")
            adapter.runtime_data_dir = root / "runtime"
            with patch.object(twitter_adapter_module, "configure_downloads"), patch.object(
                twitter_adapter_module, "request_with_retries", return_value=FakeResponse()
            ) as mocked:
                first = adapter.cache_profile_avatar(
                    "@artist", "https://pbs.twimg.com/profile_images/123/avatar_normal.jpg"
                )
                second = adapter.cache_profile_avatar(
                    "@artist", "https://pbs.twimg.com/profile_images/123/avatar_normal.jpg"
                )
            self.assertEqual(first, second)
            self.assertEqual(Path(first).read_bytes(), b"jpeg-data")
            mocked.assert_called_once()
            with self.assertRaises(ValueError):
                adapter.cache_profile_avatar("@artist", "https://example.com/avatar.jpg")

    def test_uncertain_empty_following_refresh_keeps_previous_cache(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            output = Path(temp_dir) / "following.json"
            output.write_text(
                json.dumps({"count": 1, "items": [{"handle": "artist"}], "partial": False}),
                encoding="utf-8",
            )
            payload = finish_following_payload(
                output,
                "me",
                "https://x.com/me/following",
                {},
                max_accounts=0,
                stopped_reason="no_content",
            )
            self.assertEqual(payload["count"], 1)
            self.assertEqual(payload["items"][0]["handle"], "artist")
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["partial"], False)

    def test_following_graphql_payload_recovers_lazy_loaded_users(self):
        payload = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "instructions": [
                                {
                                    "entries": [
                                        {
                                            "content": {
                                                "itemContent": {
                                                    "user_results": {
                                                        "result": {
                                                            "rest_id": "123",
                                                            "core": {"name": "Artist", "screen_name": "lazy_artist"},
                                                            "legacy": {
                                                                "description": "Lazy bio",
                                                                "profile_image_url_https": "https://pbs.twimg.com/profile_images/a.jpg",
                                                                "entities": {
                                                                    "description": {
                                                                        "urls": [
                                                                            {
                                                                                "display_url": "artist.fanbox.cc",
                                                                                "expanded_url": "https://artist.fanbox.cc/",
                                                                                "url": "https://t.co/fanbox",
                                                                            }
                                                                        ]
                                                                    },
                                                                    "url": {
                                                                        "urls": [
                                                                            {
                                                                                "display_url": "skeb.jp/@lazy_artist",
                                                                                "expanded_url": "https://skeb.jp/@lazy_artist",
                                                                                "url": "https://t.co/skeb",
                                                                            }
                                                                        ]
                                                                    },
                                                                },
                                                            },
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
        rows = extract_accounts_from_api_payload(payload)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].handle, "lazy_artist")
        self.assertEqual(rows[0].display_name, "Artist")
        self.assertEqual(rows[0].bio, "Lazy bio")
        self.assertEqual(
            [item["url"] for item in rows[0].links],
            ["https://artist.fanbox.cc/", "https://skeb.jp/@lazy_artist"],
        )
        self.assertEqual([item["url"] for item in rows[0].skeb_links], ["https://skeb.jp/@lazy_artist"])

    def test_following_dom_row_recovers_displayed_external_links(self):
        links, skeb_links = following_module._dom_external_links(
            [
                self._Link("https://x.com/lazy_artist", text="Lazy Artist"),
                self._Link("https://t.co/fanbox", text="artist.fanbox.cc"),
                self._Link("https://skeb.jp/@lazy_artist", text="skeb.jp/@lazy_artist"),
            ]
        )
        self.assertEqual(
            [item["url"] for item in links],
            ["https://artist.fanbox.cc", "https://skeb.jp/@lazy_artist"],
        )
        self.assertEqual([item["url"] for item in skeb_links], ["https://skeb.jp/@lazy_artist"])

    def test_profile_html_extracts_description_header_and_skeb_links(self):
        html = """
        <div data-testid="UserDescription">
          <a href="https://t.co/a"><span>artist.fanbox.cc</span></a>
        </div>
        <div data-testid="UserProfileHeader_Items">
          <a data-testid="UserUrl" href="https://t.co/b"><span>lit.link/en/artist</span></a>
          <div class="skeb"><a href="https://skeb.jp/@artist"><strong>受付中</strong></a></div>
        </div>
        <nav><a href="https://unrelated.example/">unrelated.example</a></nav>
        """
        links = extract_profile_links_from_html(html, resolve_shortener=False)
        self.assertEqual(
            [item["url"] for item in links],
            ["https://artist.fanbox.cc", "https://lit.link/en/artist", "https://skeb.jp/@artist"],
        )

    def test_profile_html_extracts_standalone_skeb_extension_component(self):
        html = """
        <div class="skeb card">
          <a class="skeb" href="https://skeb.jp">Skeb</a>
          <a class="skeb" href="https://skeb.jp/@artist"><strong>受付中</strong></a>
        </div>
        <nav><a href="https://unrelated.example">unrelated.example</a></nav>
        """
        links = extract_profile_links_from_html(html, resolve_shortener=False)
        self.assertEqual(
            [item["url"] for item in links],
            ["https://skeb.jp", "https://skeb.jp/@artist"],
        )

    def test_official_skeb_extension_is_found_across_chrome_profiles(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            extension = root / "Profile 1" / "Extensions" / "onjegdbehgoamaiochjfnkokondpgoim" / "1.10_0"
            extension.mkdir(parents=True)
            (extension / "manifest.json").write_text(
                json.dumps({"name": "Skeb Button", "version": "1.10"}), encoding="utf-8"
            )
            (extension / "index.js").write_text("'use strict';", encoding="utf-8")
            self.assertEqual(find_skeb_button_extension([root]), extension)

    def test_twitter_download_attempts_profile_enrichment_before_media(self):
        class FakeDriver:
            def quit(self):
                return None

        order = []
        callbacks = CallbackSet(on_progress=lambda _event: None)
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir, patch.object(
            twitter_service_module, "ensure_cookie_file"
        ), patch.object(
            twitter_service_module, "initialize_authenticated_driver", return_value=FakeDriver()
        ), patch.object(
            twitter_service_module,
            "collect_profile_preview_with_driver",
            side_effect=lambda *_args, **_kwargs: order.append("profile"),
        ), patch.object(
            twitter_service_module,
            "download_one_target",
            side_effect=lambda *_args, **_kwargs: order.append("download") or {"downloaded": 0},
        ):
            root = Path(temp_dir)
            result = TwitterCrawlerService(root / "runtime").download(
                DownloadTask("task", "twitter", "@artist", root / "output", {"types": "1"}),
                callbacks,
                threading.Event(),
            )
        self.assertEqual(order, ["profile", "download"])
        self.assertEqual(result, {"downloaded": 0})

    def test_following_initial_wait_scrolls_and_emits_partial_rows(self):
        class FakeDriver:
            current_url = "https://x.com/me/following"
            page_source = ""

            def set_page_load_timeout(self, _timeout):
                return None

            def get(self, url):
                self.current_url = url

            def quit(self):
                return None

        class FastCancel:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return False

        account = FollowingAccount(handle="lazy_artist", display_name="Artist")
        visible_batches = iter(([], [account], [account], [account]))
        partials = []
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir, patch.object(
            following_module, "initialize_driver", return_value=FakeDriver()
        ), patch.object(following_module, "cookies_web"), patch.object(
            following_module, "extract_visible_accounts", side_effect=lambda _driver: next(visible_batches, [account])
        ), patch.object(following_module, "extract_network_accounts", return_value=[]), patch.object(
            following_module, "advance_lazy_list", return_value={"cells": 0, "scroll_y": 600, "height": 1200}
        ) as advance:
            output = Path(temp_dir) / "following.json"
            payload = collect_following(
                cookie_file=Path(temp_dir) / "cookie.json",
                output_file=output,
                account="me",
                idle_rounds=1,
                scroll_pause=0.01,
                cancel_event=FastCancel(),
                on_accounts=partials.append,
            )
        self.assertTrue(advance.called)
        self.assertEqual(payload["count"], 1)
        self.assertTrue(partials)
        self.assertEqual(partials[0]["items"][0]["handle"], "lazy_artist")

    def test_following_waits_until_account_and_scroll_progress_both_stop(self):
        class FakeDriver:
            current_url = "https://x.com/me/following"
            page_source = ""

            def set_page_load_timeout(self, _timeout):
                return None

            def get(self, url):
                self.current_url = url

            def quit(self):
                return None

        class FastCancel:
            def is_set(self):
                return False

            def wait(self, _seconds):
                return False

        account = FollowingAccount(handle="lazy_artist", display_name="Artist")
        scroll_states = iter(
            (
                {"cells": 4, "scroll_y": 600, "height": 1800},
                {"cells": 4, "scroll_y": 1200, "height": 1800},
                {"cells": 4, "scroll_y": 1200, "height": 1800},
            )
        )
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir, patch.object(
            following_module, "initialize_driver", return_value=FakeDriver()
        ), patch.object(following_module, "cookies_web"), patch.object(
            following_module, "extract_visible_accounts", return_value=[account]
        ), patch.object(following_module, "extract_network_accounts", return_value=[]), patch.object(
            following_module, "advance_lazy_list", side_effect=lambda _driver: next(scroll_states)
        ) as advance:
            payload = collect_following(
                cookie_file=Path(temp_dir) / "cookie.json",
                output_file=Path(temp_dir) / "following.json",
                account="me",
                idle_rounds=1,
                scroll_pause=0.01,
                max_duration_seconds=0,
                cancel_event=FastCancel(),
            )
        self.assertEqual(payload["count"], 1)
        self.assertFalse(payload["partial"])
        self.assertEqual(advance.call_count, 3)

    def test_following_new_update_matches_handles_not_shifted_positions(self):
        previous = {
            item.handle.lower(): item
            for item in (
                FollowingAccount(handle="old_first", display_name="Unfollowed later"),
                FollowingAccount(handle="known_two", display_name="Old two"),
                FollowingAccount(handle="known_three", display_name="Old three"),
            )
        }
        scanned = {
            item.handle.lower(): item
            for item in (
                FollowingAccount(handle="brand_new", display_name="New"),
                FollowingAccount(handle="known_two", display_name="Updated two"),
                FollowingAccount(handle="known_three", display_name="Old three"),
            )
        }
        result = build_updated_accounts(scanned, previous, "new")
        self.assertEqual(list(result), ["brand_new", "old_first", "known_two", "known_three"])
        self.assertEqual(result["known_two"].display_name, "Updated two")

        full_result = build_updated_accounts(scanned, previous, "all")
        self.assertEqual(list(full_result), ["brand_new", "known_two", "known_three"])
        self.assertNotIn("old_first", full_result)

    def test_following_new_update_stops_at_known_overlap_and_keeps_unfollowed_cache(self):
        class FakeDriver:
            current_url = "https://x.com/me/following"
            page_source = ""

            def set_page_load_timeout(self, _timeout):
                return None

            def get(self, url):
                self.current_url = url

            def quit(self):
                return None

        discovered = [
            FollowingAccount(handle="brand_new", display_name="New"),
            FollowingAccount(handle="known_two", display_name="Updated two"),
            FollowingAccount(handle="known_three", display_name="Old three"),
        ]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            output = Path(temp_dir) / "following.json"
            output.write_text(
                json.dumps(
                    {
                        "items": [
                            {"handle": "old_first", "display_name": "No longer followed"},
                            {"handle": "known_two", "display_name": "Old two"},
                            {"handle": "known_three", "display_name": "Old three"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(following_module, "initialize_driver", return_value=FakeDriver()), patch.object(
                following_module, "cookies_web"
            ), patch.object(following_module, "extract_visible_accounts", return_value=discovered), patch.object(
                following_module, "extract_network_accounts", return_value=[]
            ), patch.object(following_module, "advance_lazy_list") as advance:
                payload = collect_following(
                    cookie_file=Path(temp_dir) / "cookie.json",
                    output_file=output,
                    account="me",
                    update_mode="new",
                    known_overlap=2,
                )
        self.assertEqual(payload["completion_reason"], "known_overlap")
        self.assertEqual(payload["new_count"], 1)
        self.assertEqual(
            [row["handle"] for row in payload["items"]],
            ["brand_new", "old_first", "known_two", "known_three"],
        )
        self.assertFalse(payload["partial"])
        self.assertFalse(advance.called)

    def test_following_full_update_replaces_unfollowed_and_preserves_page_order(self):
        previous = {
            "removed": FollowingAccount(handle="removed"),
            "known": FollowingAccount(handle="known", bio="old bio"),
        }
        scanned = {
            "newest": FollowingAccount(handle="newest"),
            "known": FollowingAccount(handle="known", bio="fresh bio"),
        }

        refreshed = build_updated_accounts(scanned, previous, "all")

        self.assertEqual(list(refreshed), ["newest", "known"])
        self.assertNotIn("removed", refreshed)
        self.assertEqual(refreshed["known"].bio, "fresh bio")

    def test_twitter_following_import_merges_and_export_round_trips(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = TwitterNativeAdapter()
            adapter.runtime_data_dir = root / "runtime"
            adapter.runtime_data_dir.mkdir()
            adapter.following_output_file().write_text(
                json.dumps(
                    {
                        "version": 1,
                        "items": [
                            {
                                "handle": "artist",
                                "display_name": "Old Name",
                                "bio": "keep this bio",
                                "profile_url": "https://x.com/artist",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            source = root / "following.json"
            source.write_text(
                json.dumps(
                    [
                        {"username": "@Artist", "name": "New Name"},
                        {"handle": "second_user", "description": "Second bio"},
                        {"handle": "bad handle"},
                    ]
                ),
                encoding="utf-8",
            )

            summary = adapter.import_following_accounts(source)
            self.assertEqual(
                {key: summary[key] for key in ("added", "updated", "skipped", "total")},
                {"added": 1, "updated": 1, "skipped": 1, "total": 2},
            )
            self.assertTrue(Path(str(summary["backup"])).is_file())
            rows = adapter.load_following_accounts()
            self.assertEqual([row["handle"] for row in rows], ["artist", "second_user"])
            self.assertEqual(rows[0]["display_name"], "New Name")
            self.assertEqual(rows[0]["bio"], "keep this bio")
            cache_info = adapter.following_cache_info()
            self.assertTrue(cache_info["valid"])
            self.assertTrue(cache_info["partial"])
            self.assertEqual(cache_info["update_mode"], "file_merge")

            exported = root / "export.csv"
            self.assertEqual(adapter.export_following_accounts(exported), 2)
            target = TwitterNativeAdapter()
            target.runtime_data_dir = root / "imported-runtime"
            self.assertEqual(target.import_following_accounts(exported)["total"], 2)
            self.assertEqual(
                [row["handle"] for row in target.load_following_accounts()],
                ["artist", "second_user"],
            )

    def test_twitter_following_import_rejects_wrong_files_without_touching_cache(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = TwitterNativeAdapter()
            adapter.runtime_data_dir = root / "runtime"
            adapter.runtime_data_dir.mkdir()
            cache = adapter.following_output_file()
            cache.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "platform": "twitter",
                        "items": [{"handle": "safe_user"}, {"handle": "bad handle"}, {"handle": "safe_user"}],
                    }
                ),
                encoding="utf-8",
            )
            original = cache.read_bytes()
            self.assertEqual([row["handle"] for row in adapter.load_following_accounts()], ["safe_user"])

            wrong_platform = root / "pixiv.json"
            wrong_platform.write_text(
                json.dumps({"platform": "pixiv", "items": [{"user_id": "123"}]}), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "不是 Twitter"):
                adapter.import_following_accounts(wrong_platform)
            self.assertEqual(cache.read_bytes(), original)

            settings = root / "settings.json"
            settings.write_text(json.dumps({"version": 2, "count": 99}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "普通配置对象"):
                adapter.import_following_accounts(settings)
            self.assertEqual(cache.read_bytes(), original)

            invalid = root / "invalid.txt"
            invalid.write_text("中文账号\nthis_handle_is_far_too_long\nbad handle\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "没有有效"):
                adapter.import_following_accounts(invalid)
            self.assertEqual(cache.read_bytes(), original)

    def test_twitter_following_import_preview_does_not_write(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            adapter = TwitterNativeAdapter()
            adapter.runtime_data_dir = root / "runtime"
            adapter.runtime_data_dir.mkdir()
            cache = adapter.following_output_file()
            cache.write_text(json.dumps({"items": [{"handle": "existing"}]}), encoding="utf-8")
            original = cache.read_bytes()
            source = root / "following.txt"
            source.write_text("existing\nnew_user\nbad handle\n", encoding="utf-8")

            preview = adapter.preview_following_import(source)

            self.assertEqual(preview["valid_count"], 2)
            self.assertEqual(preview["invalid_count"], 1)
            self.assertEqual(preview["added"], 1)
            self.assertEqual(preview["updated"], 1)
            self.assertEqual(cache.read_bytes(), original)

    def test_candidate_export_deduplicates_urls_and_blocks_csv_formulas(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            output = Path(temp_dir) / "candidates.csv"
            count = export_url_candidates(
                [
                    {"url": "https://example.com/a", "title": "=unsafe", "source_image": "a.png"},
                    {"url": "https://example.com/a", "title": "duplicate"},
                    {
                        "url": "https://example.com/b",
                        "title": "Safe",
                        "source_image": "b.png",
                        "availability": "missing",
                        "status_code": 404,
                    },
                ],
                output,
            )
            self.assertEqual(count, 2)
            text = output.read_text(encoding="utf-8-sig")
            self.assertIn("'=unsafe", text)
            self.assertEqual(text.count("https://example.com/a"), 1)
            self.assertIn("availability", text)
            self.assertIn("missing,404", text)

    def test_google_batch_image_collection_is_bounded_and_ignores_other_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            nested = root / "nested"
            nested.mkdir()
            (root / "z.png").write_bytes(b"z")
            (nested / "a.jpg").write_bytes(b"a")
            (nested / "b.webp").write_bytes(b"b")
            (nested / "notes.txt").write_text("skip", encoding="utf-8")

            paths, truncated = _collect_image_files(root, limit=2)
            self.assertTrue(truncated)
            self.assertEqual(len(paths), 2)
            self.assertTrue(all(path.suffix.lower() in {".png", ".jpg", ".webp"} for path in paths))

    def test_safe_component_removes_windows_path_characters(self):
        self.assertEqual(safe_component('a<b>:c/"d"'), "a_b__c__d_")

    def test_path_components_handle_unicode_reserved_names_and_utf16_limits(self):
        self.assertEqual(safe_component("CON.txt"), "_CON.txt")
        self.assertEqual(safe_component("cafe\u0301.png"), "caf\u00e9.png")
        self.assertLessEqual(len(safe_component("😀" * 100).encode("utf-16-le")) // 2, 120)
        self.assertIn("保留设备名", path_component_error("LPT1.png"))
        self.assertIn("结尾", path_component_error("name. "))

    def test_library_search_is_unicode_normalized_casefolded_and_tokenized(self):
        indexed = normalized_search_text("ＰＩＸＩＶ", "作者 Café", Path("作品/猫咪.PNG"))
        self.assertTrue(matches_search(indexed, "pixiv café"))
        self.assertTrue(matches_search(indexed, "作者 猫咪"))
        self.assertFalse(matches_search(indexed, "作者 狗"))


class ResourceStorageTests(unittest.TestCase):
    def test_selected_download_types_remains_an_instance_method(self):
        desktop = object.__new__(SoftwareDesktop)
        desktop.types_var = type("FakeVar", (), {"get": lambda self: " 1,3 "})()
        self.assertEqual(desktop._selected_types(), "1,3")

    def test_library_preview_request_is_deduplicated_for_the_same_image_and_size(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            image_path = Path(temp_dir) / "预览图.png"
            image_path.write_bytes(b"same-image")
            first = SoftwareDesktop._library_image_request_key(image_path, 401, 301)
            same_bucket = SoftwareDesktop._library_image_request_key(image_path, 415, 319)
            wider_bucket = SoftwareDesktop._library_image_request_key(image_path, 420, 319)
            self.assertEqual(first, same_bucket)
            self.assertNotEqual(first, wider_bucket)

            class FakePreview:
                def __init__(self):
                    self.configure_calls = []

                def winfo_width(self):
                    return 421

                def winfo_height(self):
                    return 321

                def configure(self, **options):
                    self.configure_calls.append(options)

            desktop = object.__new__(SoftwareDesktop)
            desktop.file_preview = FakePreview()
            desktop._library_preview_request_key = SoftwareDesktop._library_image_request_key(
                image_path, 401, 301
            )
            desktop._preview_path = image_path
            desktop._preview_fallback = {}
            desktop._render_image_preview(image_path)
            self.assertEqual(desktop.file_preview.configure_calls, [])

    def test_ui_static_methods_do_not_reference_self(self):
        for path in Path("software_app/ui").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                is_static = any(isinstance(item, ast.Name) and item.id == "staticmethod" for item in node.decorator_list)
                references_self = any(
                    isinstance(item, ast.Name) and isinstance(item.ctx, ast.Load) and item.id == "self"
                    for item in ast.walk(node)
                )
                self.assertFalse(is_static and references_self, f"{path}:{node.lineno} 静态方法错误引用 self")

    def test_ui_instance_methods_keep_self_as_first_argument(self):
        for path in Path("software_app/ui").glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for class_node in (item for item in tree.body if isinstance(item, ast.ClassDef)):
                for node in (
                    item for item in class_node.body if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                ):
                    decorators = {
                        item.id for item in node.decorator_list if isinstance(item, ast.Name)
                    }
                    if decorators & {"staticmethod", "classmethod"}:
                        continue
                    arguments = [item.arg for item in node.args.args]
                    self.assertTrue(
                        arguments and arguments[0] == "self",
                        f"{path}:{node.lineno} 实例方法缺少首参数 self",
                    )

    def test_task_option_redaction_hides_nested_credentials(self):
        safe = redact_sensitive_data(
            {
                "types": "1,2",
                "auth_token": "private-token",
                "nested": {"password": "private-password"},
                "proxy_url": "http://user:private-proxy@example.com:8080",
            }
        )
        rendered = json.dumps(safe)
        self.assertIn("1,2", rendered)
        self.assertNotIn("private-token", rendered)
        self.assertNotIn("private-password", rendered)
        self.assertNotIn("private-proxy", rendered)

    def test_global_settings_round_trip_typed_values(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "settings.db")
            storage.set_setting("task_retries", 3)
            storage.set_setting("proxy_url", "http://127.0.0.1:7890")
            self.assertEqual(storage.get_setting("task_retries"), 3)
            self.assertEqual(storage.get_setting("proxy_url"), "http://127.0.0.1:7890")
            self.assertEqual(storage.get_setting("missing", "fallback"), "fallback")
            self.assertEqual(_bounded_int("99", 1, 0, 5), 5)

    def test_proxy_error_has_actionable_desktop_message(self):
        message = SoftwareDesktop._friendly_error(
            "ProxyError: Unable to connect to proxy",
            "Pixiv 作者资料",
        )
        self.assertIn("代理", message)
        self.assertIn("设置", message)

    def test_pixiv_timeout_explains_that_the_first_page_never_returned(self):
        message = SoftwareDesktop._friendly_error(
            "ConnectTimeout: www.pixiv.net timed out",
            "Pixiv 关注列表刷新",
        )
        self.assertIn("网络请求", message)
        self.assertIn("尚未返回", message)
        self.assertIn("代理", message)

    def test_saved_output_formats_are_used_for_new_and_retried_tasks(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "settings.db")
            storage.set_setting("image_output_format", "webp")
            storage.set_setting("video_output_format", "original")
            storage.set_setting("animation_output_format", "mp4")
            storage.set_setting("audio_output_format", "m4a")
            desktop = object.__new__(SoftwareDesktop)
            desktop.storage = storage

            options = desktop._saved_format_options()

            self.assertEqual(options["image_format"], "webp")
            self.assertEqual(options["video_format"], "original")
            self.assertEqual(options["animation_format"], "mp4")
            self.assertEqual(options["audio_format"], "m4a")
            self.assertFalse(options["convert_gif"])
            self.assertTrue(options["keep_gif_mp4"])

    def test_saved_archive_options_are_shared_by_all_platform_tasks(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "settings.db")
            storage.set_setting("archive_mode", "folder")
            storage.set_setting("extract_archives", True)
            storage.set_setting("archive_cleanup_sources", True)
            desktop = object.__new__(SoftwareDesktop)
            desktop.storage = storage

            self.assertEqual(
                desktop._saved_archive_options(),
                {
                    "archive_mode": "folder",
                    "extract_archives": True,
                    "archive_cleanup_sources": True,
                },
            )

    def test_pixiv_task_uses_saved_work_limit_and_ai_filter(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "settings.db")
            storage.set_setting("pixiv_max_works", 37)
            storage.set_setting("pixiv_filter_ai", False)

            class FakeVar:
                def __init__(self, value):
                    self.value = value

                def get(self):
                    return self.value

            desktop = object.__new__(SoftwareDesktop)
            desktop.storage = storage
            desktop.module_var = FakeVar("pixiv")
            desktop.content_scope_var = FakeVar("作者 ID")
            desktop.types_var = FakeVar("1")

            options = desktop._current_target_options("https://www.pixiv.net/users/123")

            self.assertEqual(options["input_kind"], "user")
            self.assertEqual(options["max_works"], 37)
            self.assertFalse(options["filter_ai"])

            desktop.content_scope_var = FakeVar("标题 / 简介")
            resolved = desktop._current_target_options("https://www.pixiv.net/artworks/149596527")
            self.assertEqual(resolved["content_scope"], "作品 ID")
            self.assertEqual(resolved["search_mode"], "作品 ID")
            self.assertEqual(resolved["input_kind"], "work")
            self.assertEqual(resolved["requested_content_scope"], "标题 / 简介")
            self.assertEqual(
                desktop._history_content_scope(
                    "pixiv",
                    "https://www.pixiv.net/artworks/149596527",
                    '{"content_scope":"标题 / 简介"}',
                ),
                "作品 ID",
            )

    def test_eh_task_downloads_display_images_and_keeps_torrent_off_by_default(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "settings.db")

            class FakeVar:
                def __init__(self, value):
                    self.value = value

                def get(self):
                    return self.value

            desktop = object.__new__(SoftwareDesktop)
            desktop.storage = storage
            desktop.module_var = FakeVar("ehentai")
            desktop.content_scope_var = FakeVar("表站关键词")
            desktop.types_var = FakeVar("1")

            gallery_url = "https://e-hentai.org/g/1234567/abcdef1234/"
            options = desktop._current_target_options(gallery_url)
            self.assertTrue(options["eh_download_images"])
            self.assertFalse(options["eh_download_torrent"])
            self.assertFalse(options["eh_bt_download_enabled"])
            self.assertEqual(options["eh_download_method"], "images")

            storage.set_setting("eh_download_torrent", True)
            self.assertTrue(desktop._current_target_options(gallery_url)["eh_download_torrent"])
            storage.set_setting("eh_bt_download_enabled", True)
            self.assertTrue(desktop._current_target_options(gallery_url)["eh_bt_download_enabled"])

    def test_structured_resource_fields_are_persisted(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            database = Path(temp_dir) / "library.db"
            media = Path(temp_dir) / "001.png"
            media.write_bytes(b"png")
            storage = AppStorage(database)
            storage.upsert_file(
                FileRecord(
                    path=media,
                    module_id="pixiv",
                    media_type="image",
                    size=3,
                    title="001.png",
                    source_url="https://i.pximg.net/example.png",
                    source_id="123",
                    author_id="456",
                    author_name="artist",
                    chapter="1",
                    published_at="2026-01-01",
                    tags=("tag-a", "tag-b"),
                )
            )
            row = storage.list_files(module_id="pixiv", limit=1)[0]
            self.assertEqual(row["source_id"], "123")
            self.assertEqual(row["author_id"], "456")
            self.assertEqual(row["tags_json"], '["tag-a", "tag-b"]')
            storage.scan_media_files(temp_dir, "pixiv")
            row = storage.list_files(module_id="pixiv", limit=1)[0]
            self.assertEqual(row["source_id"], "123")
            self.assertEqual(row["author_name"], "artist")
            with closing(sqlite3.connect(database)) as connection:
                self.assertIn("published_at", {item[1] for item in connection.execute("PRAGMA table_info(files)")})

    def test_manual_scan_does_not_reassign_an_indexed_file_to_another_platform(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            database = Path(temp_dir) / "library.db"
            media = Path(temp_dir) / "known.png"
            media.write_bytes(b"png")
            storage = AppStorage(database)
            storage.upsert_file(FileRecord(path=media, module_id="pixiv", media_type="image", size=3))
            storage.scan_media_files(temp_dir, "twitter")
            self.assertEqual(storage.list_files(limit=1)[0]["module_id"], "pixiv")

    def test_media_scan_batches_progress_and_preserves_task_ownership(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            storage = AppStorage(root / "library.db")
            media = root / "中文😀.png"
            media.write_bytes(b"png")
            storage.upsert_file(
                FileRecord(path=media, module_id="pixiv", task_id="task-owned", media_type="image", size=3)
            )
            progress = []

            records = storage.scan_media_files(root, "twitter", on_progress=progress.append)

            self.assertEqual(len(records), 1)
            self.assertEqual(progress, [1])
            row = storage.list_files(limit=1)[0]
            self.assertEqual(row["module_id"], "pixiv")
            self.assertEqual(row["task_id"], "task-owned")

    def test_rename_and_delete_file_index(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "library.db")
            old_path = Path(temp_dir) / "old.png"
            new_path = Path(temp_dir) / "new.png"
            old_path.write_bytes(b"png")
            storage.upsert_file(FileRecord(path=old_path, module_id="twitter", media_type="image", size=3))
            old_path.rename(new_path)
            self.assertEqual(storage.rename_file_record(old_path, new_path), 1)
            self.assertEqual(Path(storage.list_files(limit=1)[0]["path"]), new_path.resolve())
            self.assertEqual(storage.delete_file_record(new_path), 1)
            self.assertEqual(storage.list_files(), [])

    def test_task_detail_queries_include_only_owned_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "library.db")
            storage.create_task("task-a", "twitter", "@artist", temp_dir, {"types": "1,2"})
            owned = Path(temp_dir) / "owned.png"
            other = Path(temp_dir) / "other.png"
            storage.upsert_file(FileRecord(owned, "twitter", "task-a", "image", 1, owned.name))
            storage.upsert_file(FileRecord(other, "twitter", "task-b", "image", 1, other.name))

            task = storage.get_task("task-a")
            self.assertIsNotNone(task)
            self.assertEqual(task["target"], "@artist")
            self.assertEqual(
                [Path(row["path"]).name for row in storage.list_files(task_id="task-a")],
                ["owned.png"],
            )
            self.assertIsNone(storage.get_task("missing"))


class _RetryAdapter(CrawlerAdapter):
    max_concurrency = 1

    def __init__(self) -> None:
        self.info = ModuleInfo("retry", "Retry")
        self.attempts = 0

    def validate(self):
        return []

    def can_handle(self, raw_target):
        return True

    def preview_target(self, raw_target, options=None):
        return TargetPreview("retry", raw_target, raw_target, title=raw_target)

    def download(self, task, callbacks, cancel_event):
        self.attempts += 1
        if self.attempts == 1:
            raise RuntimeError("retry me")


class _EmittingAdapter(CrawlerAdapter):
    def __init__(self) -> None:
        self.info = ModuleInfo("emitting", "Emitting")

    def preview_target(self, raw_target, options=None):
        return TargetPreview("emitting", raw_target, raw_target, title=raw_target)

    def download(self, task, callbacks, cancel_event):
        emitted = task.output_dir / "owned.png"
        unrelated = task.output_dir / "unrelated.png"
        emitted.write_bytes(b"owned")
        unrelated.write_bytes(b"other")
        callbacks.on_file(FileRecord(emitted, task.module_id, task.task_id, "image", 5, emitted.name))


class _CancelThenErrorAdapter(CrawlerAdapter):
    def __init__(self) -> None:
        self.info = ModuleInfo("cancel-error", "Cancel Error")
        self.started = threading.Event()

    def preview_target(self, raw_target, options=None):
        return TargetPreview("cancel-error", raw_target, raw_target, title=raw_target)

    def download(self, task, callbacks, cancel_event):
        self.started.set()
        cancel_event.wait(5)
        raise RuntimeError("browser closed during cancellation")


class _BlockingCancelAdapter(CrawlerAdapter):
    def __init__(self) -> None:
        self.info = ModuleInfo("blocking-cancel", "Blocking Cancel")
        self.started = threading.Event()
        self.release_cancel = threading.Event()

    def preview_target(self, raw_target, options=None):
        return TargetPreview("blocking-cancel", raw_target, raw_target, title=raw_target)

    def download(self, task, callbacks, cancel_event):
        self.started.set()
        cancel_event.wait(5)

    def cancel(self, task_id):
        self.release_cancel.wait(3)


class TaskLifecycleTests(unittest.TestCase):
    def test_cancelled_adapter_error_is_persisted_as_cancelled(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _CancelThenErrorAdapter()
            manager.register_adapter(adapter)
            task = manager.start_task("cancel-error", "target", temp_dir)
            self.assertTrue(adapter.started.wait(2))

            manager.cancel(task.task_id)
            self.assertIn(storage.get_task(task.task_id)["status"], {"cancelling", "cancelled"})
            manager.wait(task.task_id, timeout=5)

            self.assertEqual(storage.get_task(task.task_id)["status"], "cancelled")

    def test_cancel_does_not_block_on_browser_shutdown(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _BlockingCancelAdapter()
            manager.register_adapter(adapter)
            task = manager.start_task("blocking-cancel", "target", temp_dir)
            self.assertTrue(adapter.started.wait(2))

            started = time.monotonic()
            manager.cancel(task.task_id)
            elapsed = time.monotonic() - started
            manager.wait(task.task_id, timeout=2)
            adapter.release_cancel.set()

            self.assertLess(elapsed, 0.5)
            self.assertEqual(storage.get_task(task.task_id)["status"], "cancelled")
            self.assertNotIn(task.task_id, manager.active_task_ids())

    def test_done_callback_error_cannot_leave_ghost_running_task(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            adapter.attempts = 1
            manager.register_adapter(adapter)

            def broken_done(_task_id, _status):
                raise RuntimeError("UI already closed")

            task = manager.start_task("retry", "target", temp_dir, callbacks=CallbackSet(on_done=broken_done))
            manager.wait(task.task_id, timeout=2)

            self.assertEqual(storage.get_task(task.task_id)["status"], "completed")
            self.assertNotIn(task.task_id, manager.active_task_ids())

    def test_delete_task_records_keeps_local_files(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            storage = AppStorage(root / "tasks.db")
            storage.create_task("remove-me", "test", "target", str(root), {})
            media = root / "kept.png"
            media.write_bytes(b"png")
            storage.upsert_file(FileRecord(media, "test", "remove-me", "image", 3, media.name))
            storage.add_log("remove-me", "test", "info", "log")

            counts = storage.delete_task_records(["remove-me"])

            self.assertEqual(counts, {"logs": 1, "files": 1, "tasks": 1})
            self.assertTrue(media.is_file())
            self.assertIsNone(storage.get_task("remove-me"))

    def test_startup_recovers_interrupted_task_status(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            storage.create_task("interrupted", "test", "target", temp_dir, {})
            storage.update_task_status("interrupted", "running")

            self.assertEqual(storage.recover_interrupted_tasks(), 1)

            row = storage.get_task("interrupted")
            self.assertEqual(row["status"], "cancelled")
            self.assertIn("上次软件退出", row["error"])

    def test_task_retries_inside_platform_slot(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            manager.register_adapter(adapter)
            completed = []
            task = manager.start_task(
                "retry",
                "target",
                temp_dir,
                {"retries": 1},
                CallbackSet(on_done=lambda task_id, status: completed.append((task_id, status))),
            )
            manager.wait(task.task_id, timeout=5)
            self.assertEqual(adapter.attempts, 2)
            self.assertEqual(completed, [(task.task_id, "completed")])

    def test_failed_task_can_be_restarted_from_persisted_inputs(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            manager.register_adapter(adapter)

            failed = manager.start_task("retry", "target", temp_dir, {"types": "1"})
            manager.wait(failed.task_id, timeout=5)
            self.assertEqual(storage.get_task(failed.task_id)["status"], "failed")

            completed = []
            restarted = manager.retry_task(
                failed.task_id,
                CallbackSet(on_done=lambda task_id, status: completed.append((task_id, status))),
            )
            manager.wait(restarted.task_id, timeout=5)

            self.assertNotEqual(restarted.task_id, failed.task_id)
            self.assertEqual(restarted.target, "target")
            self.assertEqual(restarted.options, {"types": "1"})
            self.assertEqual(completed, [(restarted.task_id, "completed")])

    def test_retried_task_accepts_current_output_format_overrides(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            manager.register_adapter(adapter)
            failed = manager.start_task("retry", "target", temp_dir, {"image_format": "png", "audio_format": "mp3"})
            manager.wait(failed.task_id, timeout=5)

            restarted = manager.retry_task(
                failed.task_id,
                option_overrides={"image_format": "webp", "audio_format": "m4a"},
            )
            manager.wait(restarted.task_id, timeout=5)

            self.assertEqual(restarted.options["image_format"], "webp")
            self.assertEqual(restarted.options["audio_format"], "m4a")

    def test_completed_task_is_not_retryable(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            adapter.attempts = 1
            manager.register_adapter(adapter)
            task = manager.start_task("retry", "target", temp_dir)
            manager.wait(task.task_id, timeout=5)

            with self.assertRaisesRegex(ValueError, "不能重试"):
                manager.retry_task(task.task_id)

    def test_empty_completed_discovered_page_can_be_retried_explicitly(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            adapter = _RetryAdapter()
            adapter.info = ModuleInfo("website", "Website")
            adapter.attempts = 1
            manager.register_adapter(adapter)
            storage.create_task(
                "empty-page",
                "website",
                "https://example.com/post",
                temp_dir,
                {"discovery_source": "google_image", "browser_render": True},
            )
            storage.update_task_status("empty-page", "completed")

            restarted = manager.retry_task("empty-page", allow_empty_completed=True)
            manager.wait(restarted.task_id, timeout=5)

            self.assertEqual(storage.get_task(restarted.task_id)["status"], "completed")

    def test_emitted_files_do_not_trigger_whole_output_scan(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            storage = AppStorage(Path(temp_dir) / "tasks.db")
            manager = TaskManager(storage)
            manager.register_adapter(_EmittingAdapter())
            task = manager.start_task("emitting", "target", temp_dir)
            manager.wait(task.task_id, timeout=5)

            rows = storage.list_files(module_id="emitting")
            self.assertEqual([Path(row["path"]).name for row in rows], ["owned.png"])


if __name__ == "__main__":
    unittest.main()
