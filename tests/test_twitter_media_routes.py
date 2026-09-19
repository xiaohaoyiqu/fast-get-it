import tempfile
import unittest
import queue
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from software_app.crawlers.twitter.download_method import (
    image_content_in_format,
    image_content_as_png,
    media_filename,
    normalize_proxy_url,
    proxies_from_windows_proxy_server,
    request_with_retries,
)
from software_app.crawlers.twitter import download_method
from software_app.crawlers.twitter.manga_downloader import (
    cleanup_png_image_duplicates,
    enqueue_images_from_cell,
    enqueue_url,
    extract_media_from_json,
    is_timeline_response_url,
    normalize_photo_url,
)
from software_app.crawlers.twitter.download_method import DownloadStats
from software_app.crawlers.twitter.twitter_Crawler_2 import (
    DEFAULT_CONFIG,
    build_media_page_tasks,
    is_media_page_url,
    media_page_url,
    normalize_target as normalize_crawler_target,
)


class MediaPageRouteTests(unittest.TestCase):
    def test_non_200_response_uses_status_before_closing(self):
        class FakeResponse:
            status_code = 429
            headers = {"Retry-After": "0"}

            def close(self):
                return None

        previous_retries = download_method.MAX_RETRIES
        download_method.MAX_RETRIES = 1
        try:
            with patch.object(download_method.requests, "get", return_value=FakeResponse()):
                self.assertIsNone(request_with_retries("https://example.test/media"))
        finally:
            download_method.MAX_RETRIES = previous_retries

    def test_combined_types_scan_photo_and_video_views(self):
        self.assertEqual(
            build_media_page_tasks("https://x.com/example/media", "1234"),
            [
                ("图片", "https://x.com/example/media?filter=photo", "1"),
                ("视频/GIF/音频", "https://x.com/example/media", "234"),
            ],
        )

    def test_exact_status_url_is_not_changed_into_a_media_route(self):
        target = "https://x.com/Poklngyui/status/2085248253013180813"
        self.assertEqual(normalize_crawler_target(target), target)

    def test_reverse_image_status_visits_exact_post_for_images_only(self):
        target = "https://x.com/Poklngyui/status/2085248253013180813"
        self.assertEqual(build_media_page_tasks(target, "1"), [("图片帖子", target, "1")])

    def test_exact_status_scans_all_selected_media_in_one_visit(self):
        target = "https://x.com/Poklngyui/status/2085248253013180813"
        self.assertEqual(build_media_page_tasks(target, "1234"), [("帖子媒体", target, "1234")])

    def test_exact_status_ignores_other_posts_in_graphql_and_page_cells(self):
        payload = {"items": [
            {"rest_id": "123", "legacy": {"full_text": "wanted", "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/Wanted.jpg"}]}}},
            {"rest_id": "999", "legacy": {"full_text": "related", "extended_entities": {"media": [
                {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/Related.jpg"}]}}},
        ]}
        media = extract_media_from_json(payload, "1", desired_tweet_id="123")
        self.assertEqual(len(media), 1)
        self.assertIn("Wanted", media[0][0])

        class OtherPostDriver:
            def execute_script(self, _script):
                return [{"src": "https://pbs.twimg.com/media/Related.jpg", "tweet_id": "999"}]

        processed, added = enqueue_images_from_cell(
            OtherPostDriver(), None, set(), "1", None, {}, desired_tweet_id="123"
        )
        self.assertEqual((processed, added), (1, 0))

    def test_syndication_payload_keeps_exact_tweet_context_and_media(self):
        payload = {
            "__typename": "Tweet",
            "id_str": "2040059770237849635",
            "text": "public sample",
            "created_at": "2026-04-03T01:32:00.000Z",
            "user": {"id_str": "11348282", "screen_name": "NASA"},
            "mediaDetails": [{
                "type": "photo",
                "media_url_https": "https://pbs.twimg.com/media/PublicSample.jpg",
            }],
        }
        media = extract_media_from_json(payload, "1", desired_tweet_id="2040059770237849635")
        self.assertEqual(len(media), 1)
        self.assertEqual(media[0][1], "image")
        self.assertIn("2040059770237849635_img01", media[0][2])
        self.assertEqual(
            extract_media_from_json(payload, "1", desired_tweet_id="999"),
            [],
        )

    def test_same_x_media_filename_is_queued_once_across_sources(self):
        pending = queue.Queue()
        queued = set()
        counters = {}
        stats = DownloadStats()
        self.assertTrue(enqueue_url(
            pending, queued, "https://pbs.twimg.com/media/A?format=png", "image", stats,
            "123_img01", counters,
        ))
        self.assertFalse(enqueue_url(
            pending, queued, "https://pbs.twimg.com/media/A?format=jpg", "image", stats,
            "123_img01", counters,
        ))
        self.assertEqual(pending.qsize(), 1)

    def test_existing_filter_is_replaced_without_losing_other_query_values(self):
        target = "https://x.com/example/media?foo=1&filter=photo"
        self.assertEqual(media_page_url(target), "https://x.com/example/media?foo=1")
        self.assertEqual(media_page_url(target, "photo"), "https://x.com/example/media?foo=1&filter=photo")

    def test_photo_query_is_still_a_media_route(self):
        self.assertTrue(is_media_page_url("https://x.com/example/media?filter=photo"))

    def test_current_x_photo_and_video_timelines_are_collected(self):
        for operation in ("UserPhotoTimeline", "UserVideoTimeline", "UserMedia"):
            with self.subTest(operation=operation):
                self.assertTrue(
                    is_timeline_response_url(
                        f"https://x.com/i/api/graphql/query-id/{operation}",
                        is_media=True,
                    )
                )
        self.assertFalse(
            is_timeline_response_url(
                "https://x.com/i/api/graphql/query-id/UserByScreenName",
                is_media=True,
            )
        )

    def test_windows_proxy_syntax_converts_to_requests_mapping(self):
        self.assertEqual(
            proxies_from_windows_proxy_server("http=127.0.0.1:7890;https=127.0.0.1:7891"),
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7891"},
        )
        self.assertEqual(
            proxies_from_windows_proxy_server("127.0.0.1:7890"),
            {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
        )
        self.assertEqual(normalize_proxy_url("https://example.test:443"), "https://example.test:443")

    def test_media_defaults_output_gif_and_mp3(self):
        self.assertEqual(DEFAULT_CONFIG["gif_fps"], 8)
        self.assertEqual(DEFAULT_CONFIG["gif_width"], 720)
        self.assertTrue(DEFAULT_CONFIG["convert_gif"])
        self.assertFalse(DEFAULT_CONFIG["keep_gif_mp4"])
        self.assertEqual(DEFAULT_CONFIG["audio_format"], "mp3")
        self.assertEqual(DEFAULT_CONFIG["image_format"], "png")

    def test_photo_urls_normalize_to_png_for_deduplication(self):
        self.assertEqual(
            normalize_photo_url("https://pbs.twimg.com/media/example?format=jpg&name=large"),
            "https://pbs.twimg.com/media/example?format=png&name=large",
        )

    def test_downloaded_images_are_encoded_as_png(self):
        source = BytesIO()
        Image.new("RGB", (2, 2), "red").save(source, format="JPEG")
        png = image_content_as_png(source.getvalue())
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(
            media_filename("https://pbs.twimg.com/media/example?format=jpg", ".png", force_ext=True),
            "example.png",
        )

    def test_downloaded_images_can_use_selected_jpeg_or_webp_format(self):
        source = BytesIO()
        Image.new("RGBA", (2, 2), (0, 128, 255, 100)).save(source, format="PNG")
        for requested, expected in (("jpg", "JPEG"), ("webp", "WEBP")):
            with self.subTest(requested=requested):
                content, actual = image_content_in_format(source.getvalue(), requested)
                self.assertEqual(actual, requested)
                with Image.open(BytesIO(content)) as converted:
                    self.assertEqual(converted.format, expected)

    def test_cleanup_removes_only_crawler_jpegs_with_a_png_sibling(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temp_dir:
            folder = Path(temp_dir)
            png = folder / "20260101_120000_1234567890123456789_img01.png"
            duplicate_jpg = folder / "1234567890123456789_img01.jpg"
            jpg_without_png = folder / "1234567890123456790_img01.jpg"
            unrelated_jpg = folder / "manual-photo.jpg"
            png.write_bytes(b"png")
            duplicate_jpg.write_bytes(b"jpg")
            jpg_without_png.write_bytes(b"keep")
            unrelated_jpg.write_bytes(b"keep")
            result = cleanup_png_image_duplicates(folder)
            self.assertEqual(result["files"], 1)
            self.assertFalse(duplicate_jpg.exists())
            self.assertTrue(png.exists())
            self.assertTrue(jpg_without_png.exists())
            self.assertTrue(unrelated_jpg.exists())


if __name__ == "__main__":
    unittest.main()
