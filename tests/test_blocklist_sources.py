from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from software_app.core.blocklist import BlocklistStore
from software_app.core.blocklist_sources import (
    fetch_bluesky_moderation,
    fetch_pixiv_mutes,
    parse_jmcomic_tag_block_html,
    read_pixiv_settings_html,
    read_x_archive,
)
from software_app.crawlers.twitter.manga_downloader import extract_media_from_json


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class BlocklistSourceTests(unittest.TestCase):
    def test_parse_jmcomic_tag_block_page(self):
        records = parse_jmcomic_tag_block_html(
            '<a href="/search/photos?search_query=Blocked%20Tag">Blocked Tag</a>'
            '<button data-tag="Another">删除</button>'
            '<input name="tag_name" value="Third">'
        )
        self.assertEqual(
            {tuple(item["primary"]) for item in records},
            {("jmcomic_tag", "blocked tag"), ("jmcomic_tag", "another"), ("jmcomic_tag", "third")},
        )

    def test_x_archive_recovers_block_and_mute_ids_and_merges_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "x-archive.zip"
            with zipfile.ZipFile(archive_path, "w") as archive:
                archive.writestr("data/block.js", 'window.YTD.block.part0 = [{"blocking":{"accountId":"12345678901234567"}}]')
                archive.writestr("data/mute.js", 'window.YTD.mute.part0 = [{"muting":{"accountId":"23456789012345678"}}]')
            records = read_x_archive(archive_path)
            self.assertEqual(len(records), 2)
            store = BlocklistStore(Path(directory) / "blocklist.json")
            self.assertEqual(store.merge_account_records(records), 2)
            self.assertEqual(store.merge_account_records(records), 0)
            self.assertTrue(store.is_blocked("twitter", "https://x.com/artist/status/9", author_id="12345678901234567"))
            self.assertFalse(store.is_blocked("twitter", "https://x.com/artist/status/9"))

    def test_pixiv_localized_account_and_work_urls_match_rules(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.add_group(("pixiv", "123"))
            store.add_work("pixiv", "https://www.pixiv.net/artworks/456")
            self.assertTrue(store.is_blocked("pixiv", "https://www.pixiv.net/en/users/123"))
            self.assertTrue(store.is_blocked("pixiv", "https://www.pixiv.net/en/artworks/456"))

    def test_pixiv_saved_settings_extracts_only_account_links(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mute.html"
            path.write_text('<html><title>ミュート設定 | pixiv</title>'
                            '<a href="/users/123">A</a><a href="https://www.pixiv.net/users/456">B</a>'
                            '<a href="https://pixiv.net.evil.example/users/999">spoof</a>'
                            '<a href="https://evil.example/pixiv.net/users/888">spoof</a>'
                            '<a href="/artworks/789">work</a></html>', encoding="utf-8")
            records = read_pixiv_settings_html(path)
            self.assertEqual({item["primary"] for item in records}, {("pixiv", "123"), ("pixiv", "456")})

    def test_pixiv_live_mute_uses_imported_cookie_and_reads_settings(self) -> None:
        class Session:
            trust_env = True

            def __init__(self):
                self.closed = False

            def get(self, url, timeout):
                self.url = url
                return type("Response", (), {
                    "url": url, "text": '<title>ミュート設定 | pixiv</title><a href="/users/123">A</a>',
                    "raise_for_status": lambda self: None,
                })()

            def close(self):
                self.closed = True

        session = Session()
        with patch("software_app.crawlers.common.load_cookie_file", return_value={"PHPSESSID": "secret"}), \
             patch("software_app.crawlers.common.make_session", return_value=session):
            records = fetch_pixiv_mutes("cookies.json")
        self.assertEqual([item["primary"] for item in records], [("pixiv", "123")])
        self.assertEqual(session.url, "https://www.pixiv.net/settings/viewing/mute")
        self.assertTrue(session.closed)

    def test_bluesky_live_reader_pages_and_keeps_did_with_current_handle(self) -> None:
        responses = [
            {"accessJwt": "token", "handle": "me.bsky.social", "did": "did:plc:me"},
            {"blocks": [{"did": "did:plc:artist", "handle": "old.bsky.social"}], "cursor": "next"},
            {"blocks": [{"did": "did:plc:other", "handle": "other.bsky.social"}]},
            {"mutes": [{"did": "did:plc:artist", "handle": "new.bsky.social"}]},
        ]
        urls = []

        def fake_open(request, timeout):
            urls.append(request.full_url)
            return _Response(json.dumps(responses.pop(0)).encode("utf-8"))

        with patch("software_app.core.blocklist_sources.urlopen", side_effect=fake_open):
            records = fetch_bluesky_moderation("me.bsky.social", "app-password")
        self.assertEqual(len(records), 2)
        self.assertIn("cursor=next", urls[2])
        with tempfile.TemporaryDirectory() as directory:
            store = BlocklistStore(Path(directory) / "blocklist.json")
            store.merge_account_records(records)
            self.assertTrue(store.is_blocked("bluesky", "https://bsky.app/profile/new.bsky.social"))
            self.assertTrue(store.is_blocked("bluesky", "https://bsky.app/profile/did:plc:artist"))
            self.assertFalse(store.is_blocked("bluesky", "https://bsky.app/profile/old.bsky.social"))
            refreshed = [{"primary": ("bluesky", "did:plc:artist"),
                          "linked": [("bluesky", "latest.bsky.social")], "source": "Bluesky 拉黑"}]
            store.merge_account_records(refreshed)
            self.assertFalse(store.is_blocked("bluesky", "https://bsky.app/profile/new.bsky.social"))
            self.assertTrue(store.is_blocked("bluesky", "https://bsky.app/profile/latest.bsky.social"))

    def test_x_media_author_id_skips_archived_block(self) -> None:
        payload = {"rest_id": "9", "legacy": {"full_text": "post", "user_id_str": "12345678901234567",
                                          "extended_entities": {"media": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/A.jpg"}]}}}
        self.assertEqual(extract_media_from_json(payload, "1", blocked_author_ids={"12345678901234567"}), [])
        self.assertEqual(len(extract_media_from_json(payload, "1")), 1)


if __name__ == "__main__":
    unittest.main()
