from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask
from software_app.core.network_policy import network_task_scope
from software_app.crawlers.pixiv.catalog import PixivCatalog
from software_app.crawlers.pixiv.catalog_discovery import profile_ids
from software_app.crawlers.pixiv.client import PixivCrawler, PixivTarget


def works(*ids):
    return [{"id": str(value), "title": "generated", "illustType": 1} for value in ids]


class CatalogDiscoveryTests(unittest.TestCase):
    def catalog(self, responses):
        self.calls = []
        iterator = iter(responses)

        def fetch(path):
            self.calls.append(path)
            value = next(iterator)
            if isinstance(value, Exception):
                raise value
            return value

        return PixivCatalog(fetch, fetch)

    def test_manga_without_total_reads_following_pages_until_explicit_empty_page(self):
        catalog = self.catalog(
            [{"page": {"series": works(1)}}, {"page": {"series": works(2)}}, {"page": {"series": []}}]
        )
        rows = catalog.manga_series("12")
        self.assertEqual([row["id"] for row in rows], ["1", "2"])
        self.assertEqual([parse_qs(urlsplit(path).query)["p"][0] for path in self.calls], ["1", "2", "3"])
        self.assertTrue(catalog.last_discovery_status["complete"])

    def test_manga_known_total_can_finish_without_an_extra_empty_request(self):
        catalog = self.catalog([{"page": {"series": works(1, 2), "total": "2"}}])
        self.assertEqual(len(catalog.manga_series("12")), 2)
        self.assertEqual(len(self.calls), 1)

    def test_selected_limit_is_distinct_from_complete_remote_collection(self):
        catalog = self.catalog([{"page": {"series": works(1, 2), "total": 10}}])
        self.assertEqual(len(catalog.manga_series("12", 1)), 1)
        self.assertEqual(catalog.last_discovery_status["reason"], "limit_reached")
        self.assertFalse(catalog.last_discovery_status["complete"])

    def test_duplicate_entries_do_not_consume_selected_limit(self):
        catalog = self.catalog([{"page": {"series": works(1, 1)}}, {"page": {"series": works(1, 2)}}])
        self.assertEqual([row["id"] for row in catalog.manga_series("12", 2)], ["1", "2"])
        self.assertEqual(catalog.last_discovery_status["duplicates"], 2)

    def test_repeated_series_page_fails_instead_of_returning_first_page(self):
        catalog = self.catalog([{"page": {"series": works(1)}}, {"page": {"series": works(1)}}])
        with self.assertRaisesRegex(ValueError, "没有新增作品"):
            catalog.manga_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "no_progress")
        self.assertEqual([row["id"] for row in catalog.last_discovery_rows], ["1"])

    def test_missing_or_malformed_lists_are_not_accepted_as_empty_collections(self):
        routes = [
            ("manga_series", "12", {"page": {}}),
            ("novel_series", "12", {"page": {}}),
            ("novel_search", "generated", {"novel": {}}),
            ("ranking", "daily", {"next": False}),
            ("new_works", "", {}),
            ("author_tag", "12 tag", {}),
            ("artwork_search", "tag", {"illustManga": {}}),
        ]
        for method, target, response in routes:
            for value in (response, [], None):
                with self.subTest(method=method, value=value):
                    catalog = self.catalog([value])
                    with self.assertRaises(ValueError):
                        getattr(catalog, method)(target)
                    self.assertEqual(catalog.last_discovery_status["reason"], "invalid_response")

    def test_unrecognizable_entry_prevents_partial_success_even_at_selected_limit(self):
        for bad in (None, {"id": "bad"}, {"id": "\u0661"}, {"id": "1" * 33}, {"id": "2", "aiType": "bad"}):
            with self.subTest(bad=bad):
                catalog = self.catalog([{"page": {"series": [*works(1), bad]}}])
                with self.assertRaises(ValueError):
                    catalog.manga_series("12", 1)
                self.assertEqual(catalog.last_discovery_status["reason"], "invalid_items")

    def test_malformed_totals_are_rejected(self):
        for total in (True, -1, "", "-1", 2.5, "\u0662", "1" * 11):
            with self.subTest(total=total):
                catalog = self.catalog([{"page": {"series": works(1), "total": total}}])
                with self.assertRaises(ValueError):
                    catalog.manga_series("12")

    def test_zero_total_with_nonempty_rows_is_inconsistent(self):
        catalog = self.catalog([{"page": {"series": works(1), "total": 0}}])
        with self.assertRaises(ValueError):
            catalog.manga_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "count_mismatch")

    def test_early_empty_page_before_reported_total_fails(self):
        catalog = self.catalog([{"page": {"series": works(1), "total": 3}}, {"page": {"series": [], "total": 3}}])
        with self.assertRaises(ValueError):
            catalog.manga_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "count_mismatch")

    def test_changing_total_is_not_silently_accepted(self):
        catalog = self.catalog([{"page": {"series": works(1), "total": 3}}, {"page": {"series": works(2), "total": 2}}])
        with self.assertRaises(ValueError):
            catalog.manga_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "total_changed")

    def test_novel_series_short_page_continues_when_reported_total_has_more(self):
        catalog = self.catalog(
            [{"page": {"seriesContents": works(1), "total": 2}}, {"page": {"seriesContents": works(2), "total": 2}}]
        )
        self.assertEqual([row["id"] for row in catalog.novel_series("12")], ["1", "2"])
        self.assertEqual(parse_qs(urlsplit(self.calls[1]).query)["last_order"], ["1"])

    def test_novel_search_short_page_with_more_total_continues(self):
        catalog = self.catalog([{"novel": {"data": works(1), "total": 2}}, {"novel": {"data": works(2), "total": 2}}])
        self.assertEqual([row["id"] for row in catalog.novel_search("tag")], ["1", "2"])

    def test_novel_series_repeat_is_not_a_successful_end(self):
        catalog = self.catalog(
            [{"page": {"seriesContents": works(1), "total": 3}}, {"page": {"seriesContents": works(1), "total": 3}}]
        )
        with self.assertRaises(ValueError):
            catalog.novel_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "no_progress")

    def test_ranking_reads_next_page_and_requires_explicit_terminal_marker(self):
        catalog = self.catalog([{"contents": works(1), "next": 2}, {"contents": works(2), "next": False}])
        self.assertEqual([row["id"] for row in catalog.ranking("daily")], ["1", "2"])
        self.assertTrue(catalog.last_discovery_status["complete"])

    def test_ranking_missing_cyclic_or_skipping_cursor_fails(self):
        for extra in ({}, {"next": None}, {"next": 1}, {"next": 3}, {"next": "bad"}):
            with self.subTest(extra=extra):
                catalog = self.catalog([{"contents": works(1), **extra}])
                with self.assertRaises(ValueError):
                    catalog.ranking("daily")
                self.assertEqual(catalog.last_discovery_status["reason"], "invalid_cursor")

    def test_ranking_end_before_known_total_fails(self):
        catalog = self.catalog([{"contents": works(1), "next": False, "total": 2}])
        with self.assertRaises(ValueError):
            catalog.ranking("daily")
        self.assertEqual(catalog.last_discovery_status["reason"], "count_mismatch")

    def test_new_works_follows_short_page_when_cursor_says_more(self):
        catalog = self.catalog(
            [{"illusts": works(3), "lastId": "3"}, {"illusts": works(2), "lastId": "2"}, {"illusts": [], "lastId": ""}]
        )
        self.assertEqual([row["id"] for row in catalog.new_works("")], ["3", "2"])
        self.assertEqual(len(self.calls), 3)

    def test_new_works_cyclic_cursor_is_rejected_even_with_new_rows(self):
        catalog = self.catalog([{"illusts": works(3), "lastId": "3"}, {"illusts": works(2), "lastId": "0"}])
        with self.assertRaises(ValueError):
            catalog.new_works("")
        self.assertEqual(catalog.last_discovery_status["reason"], "invalid_cursor")

    def test_full_new_page_without_cursor_is_not_an_end_marker(self):
        catalog = self.catalog([{"illusts": works(*range(1, 61))}])
        with self.assertRaises(ValueError):
            catalog.new_works("")

    def test_author_tag_deduplicates_before_selected_limit_and_advances_raw_offset(self):
        catalog = self.catalog([{"works": works(1, 1), "total": 2}, {"works": {"a": works(2)[0]}, "total": 2}])
        self.assertEqual([row["id"] for row in catalog.author_tag("12 tag", 2)], ["1", "2"])
        self.assertEqual(parse_qs(urlsplit(self.calls[1]).query)["offset"], ["2"])

    def test_page_budget_stops_before_another_request(self):
        catalog = self.catalog([{"illusts": works(3), "lastId": "3"}, {"illusts": works(2), "lastId": "2"}])
        with patch("software_app.crawlers.pixiv.catalog_discovery.MAX_PAGES", 2), self.assertRaises(ValueError):
            catalog.new_works("")
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(catalog.last_discovery_status["reason"], "page_limit")

    def test_cancelled_discovery_keeps_task_cancelled(self):
        stop = threading.Event()

        def body(_path):
            stop.set()
            return {"page": {"series": works(1)}}

        catalog = PixivCatalog(body, lambda _url: {})
        with network_task_scope(stop), self.assertRaises(TaskCancelled):
            catalog.manga_series("12")
        self.assertEqual(catalog.last_discovery_status["reason"], "cancelled")

    def test_network_error_propagates_original_type_and_preserves_discovery_evidence(self):
        failure = requests.Timeout("generated")
        catalog = self.catalog([{"page": {"series": works(1)}}, failure])
        with self.assertRaises(requests.Timeout) as captured:
            catalog.manga_series("12")
        self.assertIs(captured.exception, failure)
        self.assertEqual(catalog.last_discovery_status["reason"], "request_failed")
        self.assertEqual(catalog.last_discovery_status["selected"], 1)

    def test_author_profile_missing_keys_or_bad_ids_are_not_empty_success(self):
        for body in ({}, [], {"illusts": []}, {"illusts": {"not-id": None}}):
            with self.subTest(body=body), self.assertRaises(ValueError):
                profile_ids(body, ("illusts", "manga"), 20)
        self.assertEqual(profile_ids({"illusts": None, "manga": {}}, ("illusts", "manga"), 20), [])
        self.assertEqual(
            profile_ids({"illusts": {"1": None, "3": None}, "manga": {"1": None}}, ("illusts", "manga"), 20), ["3", "1"]
        )

    def test_crawler_delegates_search_filters_and_resets_discovery_status(self):
        crawler = object.__new__(PixivCrawler)
        crawler._json = lambda _url: {}
        paths = []

        def body(path):
            paths.append(path)
            return {"illustManga": {"data": works(1), "total": 1}}

        crawler._body = body
        rows = crawler.search(
            "tag",
            search_mode="标题 / 简介",
            options={"age_mode": "SAFE", "minimum_bookmarks": 10, "start_date": "2026-01-01"},
        )
        query = parse_qs(urlsplit(paths[0]).query)
        self.assertEqual(
            (query["mode"], query["s_mode"], query["blt"], query["scd"]), (["safe"], ["s_tc"], ["10"], ["2026-01-01"])
        )
        self.assertEqual(rows[0]["search_mode"], "标题 / 简介")
        self.assertTrue(crawler.last_catalog_status["complete"])
        crawler._body = lambda _path: {}
        with self.assertRaises(ValueError):
            crawler.search("tag")
        self.assertEqual(crawler.last_catalog_status["reason"], "invalid_response")
        self.assertEqual(crawler.last_catalog_rows, [])

    def test_manga_and_novel_discovery_failure_happens_before_any_media_write(self):
        for kind, target in (
            ("manga_series", "https://www.pixiv.net/series/12"),
            ("novel_series", "https://www.pixiv.net/novel/series/12"),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as directory:
                crawler = object.__new__(PixivCrawler)
                calls, files = [], []

                def body(path, calls=calls):
                    calls.append(path)
                    if "/ajax/series/" in path:
                        return {"page": {"series": works(1), "total": 3}}
                    if "/ajax/novel/series_content/" in path:
                        return {"page": {"seriesContents": works(1), "total": 3}}
                    raise AssertionError("transfer phase must not start")

                crawler._body, crawler._json = body, lambda _url: {}
                task = DownloadTask("generated", "pixiv", target, Path(directory), {"input_kind": kind})
                with self.assertRaises(ValueError):
                    crawler.download(task, CallbackSet(on_file=files.append), threading.Event())
                self.assertEqual(files, [])
                self.assertEqual(list(Path(directory).iterdir()), [])
                self.assertEqual(len(calls), 2)

    def test_author_target_rejects_missing_profile_data_before_transfer(self):
        crawler = object.__new__(PixivCrawler)
        crawler._body = lambda _path: {}
        with self.assertRaises(ValueError):
            crawler._work_ids(PixivTarget("user", "12"), 20)
