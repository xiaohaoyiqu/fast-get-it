import tempfile
import threading
import unittest
import os
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import requests
from PIL import Image

from software_app.core.events import CallbackSet
from software_app.core.blocklist import BlocklistStore
from software_app.core.models import DownloadTask
from software_app.crawlers.common import convert_media_file, normalized_media_extension
from software_app.crawlers.webpage.client import (
    MediaCandidate,
    WebPageCrawler,
    extract_media_candidates,
    normalize_page_url,
)
from software_app.crawlers.webpage.availability import check_page_availability
from software_app.crawlers.webpage.browser import (
    BrowserMediaPayload,
    BrowserPageResult,
    media_candidates_from_browser_payload,
)
from software_app.ui.tk_app import SoftwareDesktop
from software_app.ui.desktop_support import download_types_for_discovered_target, retry_route_for_task


class WebPageCrawlerParsingTests(unittest.TestCase):
    def test_extracts_embedded_media_and_resolves_relative_urls(self):
        html = """
        <html><head><meta property="og:image" content="/cover.jpg"></head>
        <body>
          <img src="/images/a.png" srcset="/images/a-small.webp 1x, /images/a-large.webp 2x">
          <video poster="poster.jpg"><source src="https://cdn.example/video.mp4"></video>
          <a href="/files/original.gif?download=1">original</a>
          <script src="/ignored.js"></script>
        </body></html>
        """

        rows = extract_media_candidates(html, "https://example.com/gallery/page.html")

        self.assertEqual(
            [row.url for row in rows],
            [
                "https://example.com/cover.jpg",
                "https://example.com/images/a.png",
                "https://example.com/images/a-small.webp",
                "https://example.com/images/a-large.webp",
                "https://example.com/gallery/poster.jpg",
                "https://cdn.example/video.mp4",
                "https://example.com/files/original.gif?download=1",
            ],
        )

    def test_deduplicates_urls_and_rejects_unsafe_schemes(self):
        html = """
        <img src="https://example.com/a.jpg#one">
        <img data-src="https://example.com/a.jpg#two">
        <img src="file:///c:/secret.png">
        <img src="javascript:alert(1)">
        <img src="http://127.0.0.1/private.png">
        """

        rows = extract_media_candidates(html, "https://example.com/")

        self.assertEqual([row.url for row in rows], ["https://example.com/a.jpg"])

    def test_page_url_requires_public_http_shape(self):
        self.assertEqual(normalize_page_url("example.com/a"), "https://example.com/a")
        with self.assertRaises(ValueError):
            normalize_page_url("file:///c:/secret.html")
        with self.assertRaises(ValueError):
            normalize_page_url("javascript:alert(1)")
        with self.assertRaises(ValueError):
            normalize_page_url("http://localhost/private")
        with self.assertRaises(ValueError):
            normalize_page_url("http://127.0.0.1/private")

    def test_link_check_only_marks_definite_http_missing_as_missing(self):
        class Response:
            def __init__(self, status_code):
                self.status_code = status_code
                self.url = "https://example.com/final"

            def close(self):
                pass

        class Session:
            def __init__(self, status_code):
                self.status_code = status_code

            def get(self, _url, **_kwargs):
                return Response(self.status_code)

        expectations = {200: "available", 404: "missing", 410: "missing", 403: "restricted", 429: "restricted", 503: "temporary"}
        for status_code, expected in expectations.items():
            with self.subTest(status_code=status_code):
                result = check_page_availability("https://example.com/work", session=Session(status_code))
                self.assertEqual(result["availability"], expected)


class _FakeResponse:
    def __init__(self, url, body, content_type):
        self.url = url
        self._body = body
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.encoding = "utf-8"
        self.apparent_encoding = "utf-8"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size=1):
        del chunk_size
        yield self._body


class _FakeSession:
    def __init__(self):
        self.calls = []

    def get(self, url, **_kwargs):
        self.calls.append((url, _kwargs.get("timeout")))
        if url == "https://example.com/page":
            return _FakeResponse(url, b'<title>Sample</title><img src="/asset.png">', "text/html")
        if url == "https://example.com/asset.png":
            return _FakeResponse(url, b"image-bytes", "image/png")
        raise AssertionError(url)


class _FakeBrowser:
    def __init__(self):
        self.calls = []

    def collect(self, task_id, url, cancel_event, options):
        self.calls.append((task_id, url, cancel_event.is_set(), options.get("browser_render")))
        return BrowserPageResult(url, "Rendered post", [MediaCandidate("https://example.com/asset.png", "image")])

    def cancel(self, task_id):
        self.calls.append(("cancel", task_id))


class _ResettingMediaSession(_FakeSession):
    def get(self, url, **kwargs):
        if url == "https://example.com/asset.png":
            self.calls.append((url, kwargs.get("timeout")))
            raise requests.ConnectionError("connection reset")
        return super().get(url, **kwargs)


class WebPageCrawlerDownloadTests(unittest.TestCase):
    def test_image_conversion_retries_transient_windows_file_lock(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            source = Path(temp_dir) / "source.png"
            destination = Path(temp_dir) / "converted.jpg"
            Image.new("RGB", (2, 2), "red").save(source, format="PNG")
            actual_replace = os.replace
            attempts = []

            def briefly_locked(old, new):
                if str(old).endswith(".convert.part"):
                    attempts.append(str(old))
                    if len(attempts) == 1:
                        error = PermissionError("sharing violation")
                        error.winerror = 32
                        raise error
                return actual_replace(old, new)

            with patch("software_app.crawlers.common.os.replace", side_effect=briefly_locked), \
                 patch("software_app.crawlers.common.time.sleep"):
                convert_media_file(source, destination, ".jpg")
            self.assertGreaterEqual(len(attempts), 2)
            with Image.open(destination) as converted:
                self.assertEqual(converted.format, "JPEG")

    def test_redirect_to_blocked_post_skips_browser_media(self):
        blocked_post = "https://x.com/pixiv/status/2023978485316768106"

        class RedirectBrowser:
            def collect(self, task_id, url, cancel_event, options):
                return BrowserPageResult(blocked_post, "Post", [MediaCandidate("https://example.com/image.jpg", "image")])

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            blocklist = BlocklistStore(root / "blocklist.json")
            blocklist.add_group(("twitter", "pixiv"))
            task = DownloadTask("redirect-task", "website", "https://example.com/redirect", root,
                                {"browser_render": True, "_blocklist_path": str(blocklist.path)})
            files = []
            progress = []
            WebPageCrawler(_FakeSession(), browser=RedirectBrowser()).download(
                task, CallbackSet(on_file=files.append, on_progress=progress.append), threading.Event())
            self.assertEqual(files, [])
            self.assertEqual(progress[-1].metadata["skipped_blocked"], 1)
            self.assertFalse((root / "website").exists())

    def test_redirect_to_pixiv_work_skips_inspected_blocked_author(self):
        class RedirectBrowser:
            def collect(self, task_id, url, cancel_event, options):
                return BrowserPageResult(
                    "https://www.pixiv.net/artworks/456", "Work",
                    [MediaCandidate("https://example.com/image.jpg", "image")],
                )

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            root = Path(temp_dir)
            blocklist = BlocklistStore(root / "blocklist.json")
            blocklist.add_group(("pixiv", "123"))
            task = DownloadTask("redirect-pixiv", "website", "https://example.com/redirect", root,
                                {"browser_render": True, "author_id": "123", "pixiv_work_id": "456",
                                 "_blocklist_path": str(blocklist.path)})
            files = []
            progress = []
            WebPageCrawler(_FakeSession(), browser=RedirectBrowser()).download(
                task, CallbackSet(on_file=files.append, on_progress=progress.append), threading.Event())
            self.assertEqual(files, [])
            self.assertEqual(progress[-1].metadata["skipped_blocked"], 1)

    def test_selected_page_downloads_embedded_media(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask("page-task", "website", "https://example.com/page", Path(temp_dir), {"max_files": 5})
            records = []
            session = _FakeSession()
            WebPageCrawler(session).download(
                task,
                CallbackSet(on_file=records.append),
                threading.Event(),
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].source_url, "https://example.com/asset.png")
            self.assertEqual(records[0].path.read_bytes(), b"image-bytes")
            self.assertEqual(session.calls[0][1], (20, 60))
            self.assertEqual(session.calls[1][1], (20, 120))

    def test_discovered_page_uses_rendered_browser_media(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "rendered-task",
                "website",
                "https://example.com/post",
                Path(temp_dir),
                {"max_files": 5, "browser_render": True},
            )
            records = []
            session = _FakeSession()
            browser = _FakeBrowser()
            WebPageCrawler(session, browser=browser).download(
                task,
                CallbackSet(on_file=records.append),
                threading.Event(),
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].source_url, "https://example.com/asset.png")
            self.assertEqual(browser.calls[0][1], "https://example.com/post")
            self.assertEqual(session.calls, [("https://example.com/asset.png", (20, 120))])

    def test_rendered_fallback_filters_media_hosts_and_uses_requested_platform_root(self):
        class InstagramBrowser:
            def collect(self, task_id, url, cancel_event, options):
                return BrowserPageResult(
                    url,
                    "Instagram",
                    [
                        MediaCandidate("https://scontent.cdninstagram.com/v/post.jpg", "image"),
                        MediaCandidate("https://example.com/unrelated.jpg", "image"),
                    ],
                )

            def cancel(self, task_id):
                return None

        class MediaSession(_FakeSession):
            def get(self, url, **kwargs):
                if url == "https://scontent.cdninstagram.com/v/post.jpg":
                    image = BytesIO()
                    Image.new("RGB", (3, 2), "blue").save(image, format="JPEG")
                    return _FakeResponse(url, image.getvalue(), "image/jpeg")
                return super().get(url, **kwargs)

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "instagram-rendered", "instagram", "https://www.instagram.com/p/ABC/", Path(temp_dir),
                {"browser_render": True, "_allowed_media_hosts": ("cdninstagram.com", "fbcdn.net"),
                 "_output_platform": "instagram", "max_files": 5},
            )
            records = []
            WebPageCrawler(MediaSession(), browser=InstagramBrowser()).download(
                task, CallbackSet(on_file=records.append), threading.Event()
            )
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0].module_id, "instagram")
            self.assertIn("instagram", records[0].path.parts)
            self.assertNotIn("website", records[0].path.parts)

    def test_browser_prefetched_media_bypasses_reset_http_connection(self):
        class _PrefetchedBrowser:
            def collect(self, task_id, url, cancel_event, options):
                media_url = "https://pbs.twimg.com/media/example.jpg?name=orig"
                return BrowserPageResult(
                    url,
                    "Rendered X post",
                    [MediaCandidate(media_url, "image")],
                    {media_url: BrowserMediaPayload(b"browser-image", "image/jpeg", media_url)},
                )

            def cancel(self, task_id):
                return None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "prefetched-task",
                "website",
                "https://x.com/example/status/123",
                Path(temp_dir),
                {"browser_render": True, "image_format": "original"},
            )
            records = []
            session = _ResettingMediaSession()
            WebPageCrawler(session, browser=_PrefetchedBrowser()).download(
                task,
                CallbackSet(on_file=records.append),
                threading.Event(),
            )

            self.assertEqual(session.calls, [])
            self.assertEqual(records[0].path.read_bytes(), b"browser-image")
            self.assertEqual(records[0].metadata["transport"], "browser")

    def test_all_discovered_media_failures_fail_the_task_after_retries(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "failed-media-task",
                "website",
                "https://example.com/post",
                Path(temp_dir),
                {"browser_render": True, "media_retries": 3},
            )
            session = _ResettingMediaSession()
            with self.assertRaisesRegex(RuntimeError, "全部下载失败"):
                WebPageCrawler(session, browser=_FakeBrowser()).download(
                    task,
                    CallbackSet(),
                    threading.Event(),
                )
            self.assertEqual(len(session.calls), 3)

    def test_rendered_page_without_media_fails_instead_of_fake_completion(self):
        class _EmptyBrowser:
            def collect(self, task_id, url, cancel_event, options):
                return BrowserPageResult(url, "No media", [])

            def cancel(self, task_id):
                return None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "empty-rendered-task",
                "website",
                "https://example.com/missing",
                Path(temp_dir),
                {"browser_render": True},
            )
            with self.assertRaisesRegex(RuntimeError, "没有发现可下载媒体"):
                WebPageCrawler(_FakeSession(), browser=_EmptyBrowser()).download(
                    task,
                    CallbackSet(),
                    threading.Event(),
                )

    def test_exact_x_status_filters_avatar_and_keeps_post_media(self):
        rows = media_candidates_from_browser_payload(
            {
                "exactStatus": True,
                "media": [
                    {"url": "https://pbs.twimg.com/profile_images/avatar.jpg", "kind": "image"},
                    {"url": "https://pbs.twimg.com/media/Example?format=jpg&name=large", "kind": "image"},
                ],
            }
        )
        self.assertEqual([row.url for row in rows], ["https://pbs.twimg.com/media/Example.jpg?name=orig"])

    def test_browser_media_output_formats_are_normalized(self):
        self.assertEqual(normalized_media_extension("video/webm", ".webm", "video"), ".mp4")
        self.assertEqual(normalized_media_extension("audio/mp4", ".m4a", "audio"), ".mp3")
        self.assertEqual(normalized_media_extension("image/gif", ".gif", "image"), ".gif")
        self.assertEqual(normalized_media_extension("image/png", ".png", "image", {"image_format": "jpg"}), ".jpg")
        self.assertEqual(normalized_media_extension("video/webm", ".webm", "video", {"video_format": "original"}), ".webm")
        self.assertEqual(normalized_media_extension("image/gif", ".gif", "image", {"animation_format": "mp4"}), ".mp4")
        self.assertEqual(normalized_media_extension("audio/mpeg", ".mp3", "audio", {"audio_format": "m4a"}), ".m4a")

    def test_browser_image_is_reencoded_to_selected_jpeg_format(self):
        source = BytesIO()
        Image.new("RGBA", (3, 2), (255, 0, 0, 128)).save(source, format="PNG")

        class _ImageBrowser:
            def collect(self, task_id, url, cancel_event, options):
                media_url = "https://example.com/source.png"
                return BrowserPageResult(
                    url,
                    "Image",
                    [MediaCandidate(media_url, "image")],
                    {media_url: BrowserMediaPayload(source.getvalue(), "image/png", media_url)},
                )

            def cancel(self, task_id):
                return None

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            task = DownloadTask(
                "jpeg-task",
                "website",
                "https://example.com/post",
                Path(temp_dir),
                {"browser_render": True, "image_format": "jpg"},
            )
            records = []
            progress = []
            try:
                WebPageCrawler(_FakeSession(), browser=_ImageBrowser()).download(
                    task,
                    CallbackSet(on_file=records.append, on_progress=progress.append),
                    threading.Event(),
                )
            except AssertionError as exc:
                self.fail(f"unexpected fallback request: {exc}; progress={[(event.level, event.message) for event in progress]}")
            self.assertEqual(records[0].path.suffix, ".jpg")
            with Image.open(records[0].path) as converted:
                self.assertEqual(converted.format, "JPEG")
                self.assertEqual(converted.size, (3, 2))

    def test_google_candidates_stay_with_browser_page_downloader(self):
        self.assertEqual(
            SoftwareDesktop._pixiv_result_target({
                "url": "https://example.com/redirect",
                "final_url": "https://www.pixiv.net/en/artworks/149596527",
            }),
            "https://www.pixiv.net/en/artworks/149596527",
        )
        self.assertEqual(
            SoftwareDesktop._download_module_for_target(
                "google_image", "https://x.com/artist/status/123"
            ),
            "website",
        )
        self.assertEqual(
            SoftwareDesktop._download_module_for_target("google_image", "https://example.com/work"),
            "website",
        )
        self.assertEqual(
            SoftwareDesktop._download_module_for_target("google_image", "https://www.pixiv.net/artworks/123"),
            "website",
        )

    def test_google_candidate_does_not_rewrite_global_type_selection(self):
        self.assertEqual(download_types_for_discovered_target("google_image", "website", "1,2,3,4"), "1,2,3,4")
        self.assertEqual(download_types_for_discovered_target("google_image", "pixiv", "1,2,3,4"), "1,2,3,4")

    def test_retry_migrates_legacy_discovered_platform_task_to_rendered_page(self):
        module_id, options = retry_route_for_task(
            "twitter",
            "https://x.com/artist/status/123",
            {"max_files": 50, "page_read_timeout": 60, "types": "1,2,3,4"},
        )
        self.assertEqual(module_id, "website")
        self.assertTrue(options["browser_render"])
        self.assertEqual(options["discovery_source"], "google_image")


if __name__ == "__main__":
    unittest.main()
