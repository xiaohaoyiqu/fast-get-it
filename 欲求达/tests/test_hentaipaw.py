from __future__ import annotations

import json
import errno
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from software_app.adapters import create_adapter_bundle
from software_app.adapters.hentaipaw import HentaiPawAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask
from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import work_from_target
from software_app.core.browser_cookie_capture import cookie_capture_spec, import_cookie_file
from software_app.core.mixed_batch import route_batch
from software_app.core.storage import AppStorage
from software_app.core.task_manager import TaskManager
from software_app.crawlers.common import MediaDownloadResult
from software_app.crawlers.hentaipaw import HentaiPawClient, normalize_article_url, parse_article, parse_reader


HTML = '''<h1>Example <span>Gallery</span></h1>
<img src="https://cdn.imagedeliveries.com/123/thumbnails/cover.webp">
<a href="/viewer?articleId=123&amp;page=1">1</a>
<a href="/viewer?articleId=123&amp;page=1">1</a>
<a href="/viewer?articleId=123&amp;page=2">2</a>
<a href="/viewer?articleId=999&amp;page=90">other</a>
<a href="https://example.org/viewer?articleId=123&amp;page=3">external</a>'''


def reader_html(urls=None, article_id="123", text_record=False, split=False):
    urls = urls or [f"https://cdn.imagedeliveries.com/{article_id}/hash/{i}.webp" for i in (1, 2)]
    payload = ["$", "reader", None, {"articleId": article_id, "slides": [{"src": url} for url in urls]}]
    stream = "6:" + json.dumps(payload) + "\n"
    if text_record:
        text = "<script>\n广告\nnot-json:[]</script>"
        stream = f"5:T{len(text.encode('utf-8')):x}," + text + stream
    frames = [stream[:len(stream)//2], stream[len(stream)//2:]] if split else [stream]
    return "".join("<script>self.__next_f.push(" + json.dumps([1, frame]).replace("<", "\\u003c") + ")</script>" for frame in frames)


class HentaiPawTests(unittest.TestCase):
    def test_canonical_article(self):
        for value in ("123", "https://hentaipaw.com/articles/123/", "https://zh.hentaipaw.com/articles/123#top",
                      "https://zh.hentaipaw.com/viewer?articleId=123&page=2"):
            with self.subTest(value=value):
                self.assertEqual(normalize_article_url(value), "https://zh.hentaipaw.com/articles/123")

    def test_reject_wrong_hosts_and_non_article_pages(self):
        for value in ("", "-1", "https://zh.hentaipaw.com.evil.org/articles/123",
                      "https://evil.org/articles/123", "https://u:p@zh.hentaipaw.com/articles/123",
                      "https://zh.hentaipaw.com:444/articles/123", "http://zh.hentaipaw.com/articles/123",
                      "https://zh.hentaipaw.com/articles/search?keyword=test",
                      "https://zh.hentaipaw.com/viewer?articleId=123&articleId=999",
                      "https://zh.hentaipaw.com/viewer?articleId=123&page=0",
                      "https://zh.hentaipaw.com/viewer?articleId=123&redirect=https://evil.org"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_article_url(value)

    def test_metadata_counts_only_matching_reader_links(self):
        result = parse_article(HTML, "123")
        self.assertEqual(result["title"], "Example Gallery")
        self.assertEqual(result["visible_page_count"], 2)
        self.assertEqual(result["cover_url"], "https://cdn.imagedeliveries.com/123/thumbnails/cover.webp")
        self.assertTrue(result["download_ready"])

    def test_challenge_is_not_a_gallery(self):
        with self.assertRaises(ValueError):
            parse_article("<title>Just a moment</title>", "123")

    def test_local_preview_never_fetches(self):
        with patch.object(HentaiPawClient, "article") as fetch:
            result = HentaiPawAdapter().preview_target("123")
        fetch.assert_not_called()
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.metadata["download_ready"])

    def test_live_preview_uses_bounded_reader(self):
        with patch("software_app.crawlers.hentaipaw.client.WebPageCrawler._fetch_html",
                   side_effect=[(HTML, "https://zh.hentaipaw.com/articles/123"),
                                (reader_html(), "https://zh.hentaipaw.com/viewer?articleId=123&page=1")]) as fetch:
            result = HentaiPawAdapter().preview_target("123", {"live": True})
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(result.title, "Example Gallery")
        self.assertEqual(result.metadata["page_count"], 2)
        self.assertTrue(result.metadata["reader_verified"])
        self.assertEqual(result.metadata["thumbnail_url"], result.metadata["cover_url"])

    def test_redirect_to_other_work_is_rejected(self):
        with patch("software_app.crawlers.hentaipaw.client.WebPageCrawler._fetch_html",
                   return_value=(HTML, "https://zh.hentaipaw.com/articles/999")):
            with self.assertRaises(ValueError):
                with HentaiPawClient() as client:
                    client.article("123")

    def test_cannot_download_thumbnails_as_originals(self):
        with self.assertRaises(ValueError):
            parse_reader(reader_html(["https://cdn.imagedeliveries.com/123/thumbnails/1.webp"]), "123")

    def test_registry_includes_preparation_adapter(self):
        import tempfile

        with tempfile.TemporaryDirectory() as folder:
            bundle = create_adapter_bundle(Path(folder))
        self.assertIn("hentaipaw", {adapter.module_id for adapter in bundle.adapters})
        self.assertFalse([report for report in bundle.reports if report.status == "error"])

    def test_reader_parses_split_frames_and_byte_counted_text(self):
        for split in (False, True):
            with self.subTest(split=split):
                urls = parse_reader(reader_html(text_record=True, split=split), "123")
                self.assertEqual(len(urls), 2)
                self.assertTrue(urls[1].endswith("/2.webp"))

    def test_reader_does_not_use_other_article(self):
        with self.assertRaises(PermissionError):
            parse_reader(reader_html(article_id="999"), "123")

    def test_reader_rejects_unexpected_origins_paths_and_page_order(self):
        for url in ("https://evil.org/123/hash/1.webp", "http://cdn.imagedeliveries.com/123/hash/1.webp",
                    "https://user:pass@cdn.imagedeliveries.com/123/hash/1.webp",
                    "https://cdn.imagedeliveries.com/999/hash/1.webp",
                    "https://cdn.imagedeliveries.com/123/hash/2.webp",
                    "https://cdn.imagedeliveries.com/123/hash/1.webp?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                parse_reader(reader_html([url]), "123")

    def test_reader_missing_data_reports_login_requirement(self):
        with self.assertRaisesRegex(PermissionError, "Cookie"):
            parse_reader("<h1>Login required</h1>", "123")

    def test_unauthorized_response_reports_cookie_action(self):
        response = requests.Response()
        response.status_code = 401
        error = requests.HTTPError(response=response)
        with HentaiPawClient() as client, patch(
            "software_app.crawlers.hentaipaw.client.WebPageCrawler._fetch_html", side_effect=error,
        ):
            with self.assertRaisesRegex(PermissionError, "Cookie"):
                client.article("123")

    def test_reader_login_redirect_is_not_accepted(self):
        with HentaiPawClient() as client, patch(
            "software_app.crawlers.hentaipaw.client.WebPageCrawler._fetch_html",
            return_value=("Login", "https://zh.hentaipaw.com/login"),
        ):
            with self.assertRaises(PermissionError):
                client.reader("123")

    def test_imported_cookie_is_scoped_and_does_not_claim_verified_login(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "source.txt"
            source.write_text("Cookie: session=example", encoding="utf-8")
            adapter = HentaiPawAdapter()
            adapter.runtime_data_dir = directory / "hentaipaw"
            self.assertEqual(adapter.import_cookie_file(source), 1)
            self.assertTrue(adapter.cookie_status()["configured"])
            self.assertFalse(adapter.cookie_status()["login_verified"])
            with adapter._client() as client:
                own = client.session.prepare_request(requests.Request("GET", "https://zh.hentaipaw.com/"))
                external = client.session.prepare_request(requests.Request("GET", "https://cdn.imagedeliveries.com/"))
                self.assertIn("session=example", own.headers.get("Cookie", ""))
                self.assertNotIn("Cookie", external.headers)

    def test_other_platform_cookie_import_is_rejected(self):
        from dataclasses import replace

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "source.json"
            source.write_text(json.dumps([{"name": "session", "value": "example", "domain": ".pixiv.net"}]),
                              encoding="utf-8")
            spec = replace(cookie_capture_spec("HentaiPaw"), destination=directory / "cookies.json")
            with self.assertRaises(ValueError):
                import_cookie_file(spec, source)
            self.assertFalse(spec.destination.exists())

    def test_batch_and_blocklist_share_the_same_work_identity(self):
        rows = route_batch("https://hentaipaw.com/articles/123\nhttps://zh.hentaipaw.com/articles/123")
        self.assertEqual(rows[0].module_id, "hentaipaw")
        self.assertEqual(rows[1].state, "duplicate")
        self.assertEqual(work_from_target("hentaipaw", rows[0].target), ("hentaipaw", "123"))
        self.assertEqual(work_from_target("hentaipaw", "123"), ("hentaipaw", "123"))

    def test_search_deduplicates_articles_and_respects_limit(self):
        html = '<a href="/articles/123"><img alt="Example"></a><a href="/articles/123">Example</a>'
        html += '<a href="https://evil.org/articles/999">bad</a><a href="/articles/456">Second</a>'
        with HentaiPawClient() as client, patch.object(client, "_fetch", return_value=(
            html, "https://zh.hentaipaw.com/articles/search?keyword=a%26b",
        )) as fetch:
            rows = client.search("a&b", 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["title"], "Example")
        self.assertIn("keyword=a%26b", fetch.call_args.args[0])

    def test_download_limit_emits_metadata_and_only_selected_page(self):
        with tempfile.TemporaryDirectory() as folder, HentaiPawClient() as client:
            info = parse_article(HTML, "123")
            urls = parse_reader(reader_html(), "123")
            records = []

            def transfer(session, url, source, destination, cancel_event, suffix, **kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"image fixture")
                self.assertEqual(kwargs["public_referer"], info["reader_url"])
                return MediaDownloadResult(destination.stat().st_size, False, destination)

            with patch.object(client, "article", return_value=info), patch.object(client, "reader", return_value=urls), patch(
                "software_app.crawlers.hentaipaw.client.download_media_file", side_effect=transfer,
            ) as download:
                client.download(DownloadTask("test", "hentaipaw", "123", Path(folder),
                                             {"max_files": 1, "image_format": "original"}),
                                CallbackSet(on_file=records.append), threading.Event())
            download.assert_called_once()
            self.assertEqual([record.media_type for record in records], ["text", "image"])
            self.assertEqual(records[-1].path.name, "0001.webp")
            self.assertEqual(records[-1].metadata["page_count"], 2)

    def test_cancelled_download_makes_no_request(self):
        event = threading.Event()
        event.set()
        with HentaiPawClient() as client, patch.object(client, "article") as fetch:
            with self.assertRaises(TaskCancelled):
                client.download(DownloadTask("test", "hentaipaw", "123", Path(".")), CallbackSet(), event)
        fetch.assert_not_called()

    def test_download_rejects_thumbnail_only_page_before_creating_outputs(self):
        with tempfile.TemporaryDirectory() as folder, HentaiPawClient() as client:
            with patch.object(client, "article", return_value=parse_article(HTML, "123")), patch.object(
                client, "reader", side_effect=PermissionError("Login required"),
            ):
                with self.assertRaises(PermissionError):
                    client.download(DownloadTask("test", "hentaipaw", "123", Path(folder)),
                                    CallbackSet(), threading.Event())
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_search_follows_matching_pagination_and_preserves_card_title(self):
        first = ('<a href="/articles/123"><img alt="Example, English"><div title="Example">Example</div>'
                 '<p>Artist name</p></a><a href="/articles/search?keyword=example&amp;page=2">next</a>')
        second = '<a href="/articles/123">Example</a><a href="/articles/456">Second</a>'
        with HentaiPawClient() as client, patch.object(client, "_fetch", side_effect=[
            (first, "https://zh.hentaipaw.com/articles/search?keyword=example"),
            (second, "https://zh.hentaipaw.com/articles/search?keyword=example&page=2"),
        ]) as fetch:
            rows = client.search("example", 10)
        self.assertEqual([row["title"] for row in rows], ["Example", "Second"])
        self.assertEqual(fetch.call_count, 2)

    def test_search_never_follows_other_keywords_or_external_pagination(self):
        html = ('<a href="/articles/123">Example</a>'
                '<a href="https://evil.org/articles/search?keyword=example&page=2">next</a>'
                '<a href="/articles/search?keyword=other&amp;page=2">next</a>')
        with HentaiPawClient() as client, patch.object(client, "_fetch", return_value=(
            html, "https://zh.hentaipaw.com/articles/search?keyword=example",
        )) as fetch:
            rows = client.search("example", 10)
        fetch.assert_called_once()
        self.assertEqual(len(rows), 1)

    def test_zero_search_limit_does_not_fetch(self):
        with HentaiPawClient() as client, patch.object(client, "_fetch") as fetch:
            self.assertEqual(client.search("example", 0), [])
        fetch.assert_not_called()

    def test_article_login_redirect_has_actionable_message(self):
        with HentaiPawClient() as client, patch.object(client, "_fetch", return_value=(
            "<h1>Log in</h1>", "https://zh.hentaipaw.com/login",
        )):
            with self.assertRaisesRegex(PermissionError, "Cookie"):
                client.article("123")

    def test_cover_loader_rejects_non_cover_urls_before_sending_requests(self):
        adapter = HentaiPawAdapter()
        for url in ("https://evil.org/123/thumbnails/cover.webp", "https://cdn.imagedeliveries.com/123/hash/1.webp",
                    "https://u:p@cdn.imagedeliveries.com/123/thumbnails/cover.webp"):
            with self.subTest(url=url), patch("software_app.adapters.hentaipaw.request_get") as fetch:
                with self.assertRaises(ValueError):
                    adapter.fetch_preview_image_bytes(url)
                fetch.assert_not_called()


class HentaiPawQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.storage = AppStorage(self.root / "state.db")
        self.manager = TaskManager(self.storage)
        self.addCleanup(lambda: self.manager.shutdown(timeout=2))
        self.manager.register_adapter(HentaiPawAdapter())
        self.indices = []
        self.fail_page = 2
        self.local_error = False
        self.patches = [
            patch.object(HentaiPawClient, "article", return_value=parse_article(HTML, "123")),
            patch.object(HentaiPawClient, "reader", return_value=parse_reader(reader_html(), "123")),
            patch("software_app.crawlers.hentaipaw.client.download_media_file", side_effect=self.transfer),
        ]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    def transfer(self, session, url, source, destination, cancel_event, suffix, **kwargs):
        page = int(destination.stem)
        self.indices.append(page)
        if self.local_error:
            raise OSError(errno.ENOSPC, "generated disk full")
        if page == self.fail_page:
            raise ValueError("generated invalid image")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"generated image fixture")
        return MediaDownloadResult(destination.stat().st_size, False, destination)

    def start(self):
        task = self.manager.start_task("hentaipaw", "123", self.root, {"archive_mode": "task", "retries": 0})
        self.manager.wait(task.task_id, 10)
        return task

    def test_partial_download_and_retry_keep_page_results_and_original_folder(self):
        with patch("software_app.core.task_manager.run_archive_postprocess") as archive:
            original = self.start()
        self.assertEqual(self.storage.get_task(original.task_id)["status"], "partial")
        self.assertEqual(self.storage.media_summary(original.task_id), {"saved": 1, "failed": 1})
        archive.assert_not_called()
        failed = self.storage.media_results(original.task_id, unfinished=True)[0]
        self.fail_page = 0
        with patch("software_app.crawlers.hentaipaw.client.platform_output_root") as new_directory:
            retry = self.manager.retry_task(original.task_id, failed_only=True)
            self.manager.wait(retry.task_id, 10)
        new_directory.assert_not_called()
        self.assertEqual(self.indices, [1, 2, 2])
        self.assertEqual(self.storage.get_task(retry.task_id)["status"], "completed")
        self.assertEqual(self.storage.media_results(original.task_id, unfinished=True), [])
        self.assertTrue((self.root / failed["folder"] / "images/0002.png").is_file())

    def test_disk_error_stops_later_pages_and_keeps_them_pending(self):
        self.local_error = True
        task = self.start()
        self.assertEqual(self.indices, [1])
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        self.assertEqual(self.storage.media_summary(task.task_id), {"failed": 1, "pending": 1})

    def test_early_failure_does_not_scan_other_works_into_task(self):
        unrelated = self.root / "other-work.jpg"
        unrelated.write_bytes(b"other work")
        with patch.object(HentaiPawClient, "reader", side_effect=PermissionError("Login required")), patch.object(
            self.storage, "scan_media_files", wraps=self.storage.scan_media_files,
        ) as scan:
            task = self.start()
        self.assertEqual(self.storage.get_task(task.task_id)["status"], "failed")
        scan.assert_not_called()
        self.assertEqual(self.storage.task_file_records(task.task_id), [])
