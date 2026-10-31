from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

import requests

from software_app.adapters.pixiv import PixivNativeAdapter
from software_app.crawlers.pixiv.bookmark_discovery import merge_bookmark_cache, require_downloadable_discovery
from software_app.crawlers.pixiv.catalog import PixivCatalog
from software_app.crawlers.pixiv.client import PixivCrawler


def row(work_id, visibility="show"):
    return {"id": work_id, "url": f"https://www.pixiv.net/artworks/{work_id}", "visibility": visibility}


class BookmarkDiscoveryTests(unittest.TestCase):
    def test_duplicates_do_not_consume_selection_limit_and_offset_counts_actual_rows(self):
        offsets = []

        def body(path):
            offset = int(parse_qs(urlsplit(path).query)["offset"][0])
            offsets.append(offset)
            ids = ["1", "1", "2"] if offset == 0 else ["3"]
            return {"works": [{"id": value} for value in ids], "total": 3}

        catalog = PixivCatalog(body, Mock())
        self.assertEqual([item["id"] for item in catalog.bookmarks("9", limit=3)], ["1", "2", "3"])
        self.assertEqual(offsets, [0, 3])
        require_downloadable_discovery(catalog.last_bookmark_status)

    def test_repeating_page_stops_as_incomplete(self):
        catalog = PixivCatalog(lambda path: {"works": [{"id": "1"}, {"id": "2"}], "total": 5}, Mock())
        catalog.bookmarks("9", limit=4)
        self.assertEqual(catalog.last_bookmark_status["scopes"][0]["reason"], "no_progress")
        with self.assertRaises(ValueError):
            require_downloadable_discovery(catalog.last_bookmark_status)

    def test_bad_shape_is_not_an_empty_collection(self):
        for payload in ({}, {"works": "bad"}, {"works": [], "total": -1}, []):
            with self.subTest(payload=payload):
                catalog = PixivCatalog(lambda path, payload=payload: payload, Mock())
                with self.assertRaises(ValueError):
                    catalog.bookmarks("9")
                self.assertFalse(catalog.last_bookmark_status["complete"])

    def test_bad_items_and_changed_total_cannot_hide_behind_user_limit(self):
        for first, second in (
            ({"works": [{"id": "1"}, {}], "total": 3}, {"works": [{"id": "2"}], "total": 3}),
            ({"works": [{"id": "1"}], "total": 3}, {"works": [{"id": "2"}], "total": 4}),
        ):
            responses = iter([first, second])
            catalog = PixivCatalog(lambda path, responses=responses: next(responses), Mock())
            catalog.bookmarks("9", limit=2)
            with self.assertRaises(ValueError):
                require_downloadable_discovery(catalog.last_bookmark_status)

    def test_public_refresh_keeps_private_and_unknown_scope(self):
        previous = {"account": "9", "items": [row("1"), row("2", "hide"), {"id": "3", "url": row("3")["url"]}]}
        status = {"complete": True, "scopes": [{"visibility": "show", "complete": True, "reason": "exhausted"}]}
        result = merge_bookmark_cache(previous, [row("4")], status, account_id="9", kind="work", visibility="show")
        self.assertEqual([item["id"] for item in result["items"]], ["4", "2", "3"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["retained_count"], 2)

    def test_empty_or_partial_results_never_clear_previous_collection(self):
        previous = {"account": "9", "items": [row("1")]}
        for rows, status in (
            ([], {"complete": True, "scopes": [{"visibility": "show", "complete": True}]}),
            ([row("2")], {"complete": False, "scopes": [{"visibility": "show", "reason": "limit_reached"}]}),
        ):
            result = merge_bookmark_cache(previous, rows, status, account_id="9", kind="work", visibility="show")
            self.assertFalse(result["complete"])
            self.assertIn("1", [item["id"] for item in result["items"]])

    def test_interrupted_refresh_persists_partial_discovery_but_still_raises(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(directory)
            adapter.cookie_account_id = lambda: "9"
            adapter._write_json_payload(adapter.bookmark_output_file("work"), {"account": "9", "items": [row("8")]})
            client = object.__new__(PixivCrawler)
            client.session, client._json = Mock(), Mock()
            responses = iter([{"works": [{"id": "1"}], "total": 3}, requests.Timeout("synthetic timeout")])

            def body(path):
                result = next(responses)
                if isinstance(result, Exception):
                    raise result
                return result

            client._body = body
            adapter._client = lambda options=None: client
            with self.assertRaises(requests.Timeout):
                adapter.refresh_own_bookmarks("work")
            saved = adapter.load_bookmark_payload("work")
            self.assertEqual({item["id"] for item in saved["items"]}, {"1", "8"})
            self.assertEqual(saved["stop_reason"], "request_failed")
            self.assertIn("synthetic timeout", saved["refresh_error"])
            client.session.close.assert_called_once()

    def test_cache_is_not_visible_when_current_account_is_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = PixivNativeAdapter()
            adapter.runtime_data_dir = Path(directory)
            adapter._write_json_payload(adapter.bookmark_output_file("work"), {"account": "9", "items": [row("1")]})
            adapter.cookie_account_id = lambda: ""
            self.assertEqual(adapter.load_bookmark_candidates("work"), [])
