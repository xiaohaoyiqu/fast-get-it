from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch

from software_app.adapters.hentaipaw import HentaiPawAdapter
from software_app.crawlers.hentaipaw.catalog import BASE_URL, browse_catalog, normalize_catalog_url
from software_app.crawlers.hentaipaw.favorites import FavoriteStore, parse_favorites


class CatalogTests(unittest.TestCase):
    def test_catalog_urls_have_strict_site_paths_and_parameters(self):
        self.assertEqual(normalize_catalog_url("https://hentaipaw.com/tags/12?page=2"), BASE_URL + "/tags/12?page=2")
        for url in (
            "https://evil.org/tags/12",
            "https://u:p@zh.hentaipaw.com/tags/12",
            BASE_URL + "/tags/12?url=x",
            BASE_URL + "/tags?page=0",
            BASE_URL + "/tags?page=1&page=2",
            BASE_URL + "/articles/rank?t=invalid",
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_catalog_url(url)

    def test_index_categories_are_read_only_and_filter_current_page(self):
        html = '<a href="/tags/12">Example</a><a href="/tags/13">Other</a><a href="/artists/4">Wrong group</a>'
        client = Mock()
        client._fetch.return_value = (html, BASE_URL + "/tags")
        rows = browse_catalog(client, "Example", "标签分类", 100)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["downloadable"])
        self.assertEqual(rows[0]["input_kind"], "category")
        self.assertEqual(rows[0]["id"], "12")

    def test_category_id_opens_work_list(self):
        client = Mock()
        client._fetch.return_value = ('<a href="/articles/123">Example</a>', BASE_URL + "/tags/12")
        rows = browse_catalog(client, "12", "标签分类", 10)
        self.assertEqual(rows[0]["id"], "123")
        self.assertNotEqual(rows[0].get("input_kind"), "category")

    def test_rank_range_and_pagination_stay_in_selected_list(self):
        client = Mock()
        client._fetch.side_effect = [
            (
                '<a href="/articles/123">One</a><a href="/articles/rank?t=weekly&amp;page=2">Next</a>',
                BASE_URL + "/articles/rank?t=weekly",
            ),
            ('<a href="/articles/456">Two</a>', BASE_URL + "/articles/rank?t=weekly&page=2"),
        ]
        rows = browse_catalog(client, "", "周榜", 10)
        self.assertEqual([row["id"] for row in rows], ["123", "456"])
        self.assertEqual(client._fetch.call_count, 2)
        client.reset_mock()
        client._fetch.side_effect = None
        client._fetch.return_value = ('<a href="/articles/123">One</a>', BASE_URL + "/articles/rank?t=weekly")
        browse_catalog(client, BASE_URL + "/tags/12", "周榜", 1)
        self.assertEqual(client._fetch.call_args.args[0], BASE_URL + "/articles/rank?t=weekly")

    def test_other_rank_or_external_next_page_is_not_followed(self):
        client = Mock()
        html = (
            '<a href="/articles/123">One</a>'
            '<a href="https://evil.org/articles/rank?t=weekly&amp;page=2">Next</a>'
            '<a href="/articles/rank?t=daily&amp;page=2">Different rank</a>'
        )
        client._fetch.return_value = (html, BASE_URL + "/articles/rank?t=weekly")
        self.assertEqual(len(browse_catalog(client, "", "周榜", 10)), 1)
        client._fetch.assert_called_once()

    def test_login_redirect_is_not_a_catalog(self):
        client = Mock()
        client._fetch.return_value = ("Log in", BASE_URL + "/login")
        with self.assertRaises(PermissionError):
            browse_catalog(client, "", "日榜", 10)

    def test_category_preview_cannot_be_downloaded_as_a_work(self):
        adapter = HentaiPawAdapter()
        preview = adapter.preview_target(BASE_URL + "/tags/12")
        self.assertFalse(preview.metadata["download_ready"])
        self.assertEqual(preview.metadata["input_kind"], "category")

    def test_public_page_does_not_prove_account_login(self):
        adapter = HentaiPawAdapter()
        with (
            patch.object(adapter, "_client") as client,
            patch.object(
                adapter,
                "cookie_status",
                return_value={
                    "configured": True,
                    "cookie_count": 1,
                    "login_verified": False,
                },
            ),
        ):
            client.return_value.__enter__.return_value._fetch.return_value = ("<title>HentaiPaw</title>", BASE_URL)
            result = adapter.access_status()
        self.assertTrue(result["accessible"])
        self.assertTrue(result["configured"])
        self.assertFalse(result["login_verified"])
        self.assertEqual(result["favorites_storage"], "browser_localStorage")


class FavoritesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = FavoriteStore(self.root / "favorites.json")

    @staticmethod
    def payload(key="123", title="Example"):
        return {
            key: {
                "id": int(key),
                "attributes": {
                    "title": title,
                    "thumbnail": "untrusted",
                    "language_id": 3,
                    "cookie": "should-not-be-copied",
                },
            }
        }

    def test_browser_import_merges_and_preserves_only_public_metadata(self):
        self.store.set("456", "Existing", True)
        source = self.root / "import.json"
        source.write_text(json.dumps(self.payload()), encoding="utf-8")
        self.assertEqual(self.store.import_file(source), 1)
        self.assertEqual({row["id"] for row in self.store.rows()}, {"123", "456"})
        text = self.store.path.read_text(encoding="utf-8")
        self.assertNotIn("should-not-be-copied", text)
        self.assertIn("123/thumbnails/cover.webp", text)

    def test_export_is_compatible_with_browser_shape(self):
        self.store.set("123", "Example", True)
        destination = self.root / "backup.json"
        self.assertEqual(self.store.export_file(destination), 1)
        values = parse_favorites(json.loads(destination.read_text(encoding="utf-8")))
        self.assertEqual(values["123"]["attributes"]["title"], "Example")

    def test_export_cannot_replace_store_or_cookies(self):
        for target in (self.store.path, self.root / "cookies.json"):
            with self.subTest(target=target), self.assertRaises(ValueError):
                self.store.export_file(target)

    def test_malformed_import_preserves_existing_favorites(self):
        self.store.set("123", "Example", True)
        before = self.store.path.read_bytes()
        source = self.root / "bad.json"
        source.write_text(json.dumps({"001": {"id": 1, "attributes": {"title": "bad"}}}), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.import_file(source)
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_local_favorites_filter_and_remove(self):
        self.store.set("123", "Example", True)
        self.store.set("456", "Other", True)
        self.assertEqual(len(self.store.rows("example")), 1)
        self.store.set("123", "", False)
        self.assertEqual([row["id"] for row in self.store.rows()], ["456"])

    def test_concurrent_updates_do_not_lose_entries(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda value: self.store.set(str(value), f"Example {value}", True), range(1, 9)))
        self.assertEqual(len(self.store.rows()), 8)

    def test_cookie_file_is_not_a_favorites_export(self):
        with self.assertRaises(ValueError):
            parse_favorites({"session": "example"})

    def test_favorites_search_never_opens_network_client(self):
        adapter = HentaiPawAdapter()
        adapter.runtime_data_dir = self.root
        self.store.set("123", "Example", True)
        with patch.object(adapter, "_client") as client:
            rows = adapter.search_targets("全部", options={"search_mode": "软件收藏"})
        client.assert_not_called()
        self.assertEqual(rows[0]["id"], "123")
