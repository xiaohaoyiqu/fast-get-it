from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from software_app.core.candidate_lists import export_candidate_list, import_candidate_list
from software_app.ui.following_tab import FollowingTabMixin


class CandidateListTests(unittest.TestCase):
    def test_pixiv_collection_export_preserves_full_list_and_unknown_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "collection.csv"
            rows = [
                {"id": str(index), "url": f"https://www.pixiv.net/artworks/{index}",
                 "title": f"作品 {index}", "author_name": "画师", "bookmarked": True}
                for index in range(1, 1002)
            ]
            self.assertEqual(export_candidate_list(rows, path, "pixiv"), 1001)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                exported = list(csv.DictReader(handle))
            self.assertEqual(len(exported), 1001)
            self.assertEqual(exported[-1]["id"], "1001")
            self.assertEqual(exported[-1]["bookmarked"], "True")
            self.assertEqual(exported[-1]["likes"], "")

    def test_csv_handle_round_trip_and_platform_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.csv"
            export_candidate_list([{"target": "@artist", "name": "=formula"}], path, "twitter")
            imported = import_candidate_list(path, "twitter")
            self.assertEqual(imported[0]["target"], "@artist")
            self.assertEqual(imported[0]["import_source"], "candidate_list")
            with self.assertRaisesRegex(ValueError, "其他平台"):
                import_candidate_list(path, "pixiv")

    def test_jmcomic_candidate_tags_survive_csv_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "jm.csv"
            export_candidate_list([
                {"target": "https://18comic.vip/album/123", "title": "Book", "tags": ["收藏", "稍后看"]}
            ], path, "jmcomic")
            imported = import_candidate_list(path, "jmcomic")
            self.assertEqual(imported[0]["tags"], ["收藏", "稍后看"])

    def test_text_and_json_lists_deduplicate_without_starting_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text_path = root / "targets.txt"
            text_path.write_text("# remark\nhttps://www.pixiv.net/artworks/12\nhttps://www.pixiv.net/artworks/12\n", encoding="utf-8")
            self.assertEqual(len(import_candidate_list(text_path, "pixiv")), 1)
            json_path = root / "targets.json"
            json_path.write_text(json.dumps({"items": [{"target": "https://x.com/user"}]}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "twitter"):
                import_candidate_list(json_path, "pixiv")

    def test_pixiv_export_uses_full_filtered_cache_beyond_visible_rows(self) -> None:
        state = SimpleNamespace(
            module_var=SimpleNamespace(get=lambda: "pixiv"),
            content_scope_var=SimpleNamespace(get=lambda: "作品收藏"),
            platform_candidate_scope={"pixiv": "作品收藏"},
            platform_candidate_rows={"pixiv": [{"id": str(index)} for index in range(1001)]},
            current_candidate_rows=[{"id": "0"}],
            _filter_pixiv_bookmark_rows=lambda rows: rows,
            manager=SimpleNamespace(blocklist=SimpleNamespace(
                blocked_accounts=lambda: set(), is_blocked=lambda *_args, **_kwargs: False,
            )),
        )
        self.assertEqual(len(FollowingTabMixin._exportable_candidate_rows(state)), 1001)


if __name__ == "__main__":
    unittest.main()
