from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from software_app.adapters import create_default_adapters
from software_app.crawlers.bluesky import BlueskyClient, parse_bluesky_target, post_candidate
from software_app.crawlers.instagram import parse_instagram_html, parse_instagram_target
from software_app.adapters.instagram import InstagramNativeAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask


class SocialTargetTests(unittest.TestCase):
    def test_bluesky_target_routes_and_rejects_lookalike_host(self) -> None:
        post = parse_bluesky_target("https://bsky.app/profile/example.test/post/3abc")
        self.assertEqual((post.kind, post.actor, post.post_id), ("post", "example.test", "3abc"))
        self.assertEqual(parse_bluesky_target("@example.test").kind, "profile")
        self.assertEqual(parse_bluesky_target("landscape art").kind, "search")
        with self.assertRaises(ValueError):
            parse_bluesky_target("https://bsky.app.evil.example/profile/example.test")

    def test_instagram_target_routes_and_rejects_lookalike_host(self) -> None:
        reel = parse_instagram_target("https://www.instagram.com/reel/ABC_123/")
        self.assertEqual((reel.kind, reel.shortcode), ("reel", "ABC_123"))
        self.assertEqual(parse_instagram_target("@example.user").kind, "profile")
        with self.assertRaises(ValueError):
            parse_instagram_target("https://instagram.com.evil.example/p/ABC/")

    def test_bluesky_post_media_uses_api_fullsize_and_playlist(self) -> None:
        row = post_candidate({
            "uri": "at://did:plc:test/app.bsky.feed.post/3abc",
            "author": {"did": "did:plc:test", "handle": "example.test", "displayName": "Example"},
            "record": {"text": "hello", "createdAt": "2026-01-01T00:00:00Z"},
            "embed": {"$type": "app.bsky.embed.recordWithMedia#view", "media": {
                "$type": "app.bsky.embed.images#view",
                "images": [{"fullsize": "https://cdn.bsky.app/img/feed_fullsize/plain/did/x.jpg", "alt": "x"}],
            }},
        })
        self.assertEqual(row["id"], "3abc")
        self.assertEqual(row["media"][0]["type"], "image")
        self.assertIn("feed_fullsize", row["media"][0]["url"])

    def test_instagram_html_reads_og_and_json_ld_without_duplicate(self) -> None:
        media_url = "https://scontent.cdninstagram.com/v/example.jpg"
        payload = {"@type": "ImageObject", "contentUrl": media_url, "caption": "sample"}
        page = (
            '<meta property="og:title" content="Example (@example) on Instagram">'
            f'<meta property="og:image" content="{media_url}">'
            f'<script type="application/ld+json">{json.dumps(payload)}</script>'
        )
        parsed = parse_instagram_html(page, "https://www.instagram.com/p/ABC/")
        self.assertEqual(parsed["username"], "example")
        self.assertEqual(len(parsed["media"]), 1)
        self.assertEqual(parsed["media"][0]["url"], media_url)

    def test_social_adapters_replace_planned_placeholders(self) -> None:
        adapters = {adapter.module_id: adapter for adapter in create_default_adapters(Path(tempfile.mkdtemp()))}
        self.assertEqual(adapters["bluesky"].info.stage, "ready")
        self.assertEqual(adapters["instagram"].info.stage, "ready")
        self.assertIn("download", adapters["bluesky"].info.capabilities)
        self.assertIn("cookie-import", adapters["instagram"].info.capabilities)
        self.assertEqual(adapters["ehentai"].info.stage, "alpha")
        self.assertIn("torrent", adapters["ehentai"].info.capabilities)

    def test_bluesky_search_forbidden_explains_partial_limitation(self) -> None:
        class Response:
            status_code = 403

        class Session:
            def get(self, *_args, **_kwargs):
                return Response()

        client = BlueskyClient(session=Session())
        with self.assertRaisesRegex(PermissionError, "用户、公开关注、作者动态和具体帖子仍可使用"):
            client.get("app.bsky.feed.searchPosts", q="sample")

    def test_instagram_download_falls_back_to_logged_in_browser(self) -> None:
        class StaticClient:
            def download(self, *_args):
                raise RuntimeError("dynamic shell")

        calls = []

        class BrowserFallback:
            def download(self, task, callbacks, cancel_event):
                calls.append(task)

            def cancel(self, task_id):
                return None

        adapter = InstagramNativeAdapter()
        adapter.runtime_data_dir = Path(tempfile.mkdtemp())
        adapter._browser_fallback = BrowserFallback()
        task = DownloadTask(
            "instagram-fallback", "instagram", "https://www.instagram.com/p/ABC/", Path.cwd(),
            {"proxy_url": "http://127.0.0.1:7890", "max_works": 2},
        )
        with patch.object(adapter, "_client", return_value=StaticClient()):
            adapter.download(task, CallbackSet(), threading.Event())
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].options["browser_render"])
        self.assertEqual(calls[0].options["_output_platform"], "instagram")
        self.assertEqual(calls[0].options["max_files"], 2)
        self.assertIn("cdninstagram.com", calls[0].options["_allowed_media_hosts"])


if __name__ == "__main__":
    unittest.main()
