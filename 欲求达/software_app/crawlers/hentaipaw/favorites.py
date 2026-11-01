"""A local mirror of the site's browser localStorage favorites format."""

from __future__ import annotations

import json
from pathlib import Path

from software_app.core.output_lock import output_path_lock
from software_app.crawlers.common import write_text_atomic
from .client import BASE_URL, normalize_article_url


MAX_BYTES = 8 * 1024 * 1024
MAX_ITEMS = 10000


def parse_favorites(payload):
    if isinstance(payload, str):
        payload = json.loads(payload)
    if not isinstance(payload, dict) or len(payload) > MAX_ITEMS:
        raise ValueError("收藏需要是本站 favorites JSON 对象，最多 10000 项")
    result = {}
    for key, row in payload.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or len(key) > 30 or key.startswith("0"):
            raise ValueError("收藏作品 ID 无效")
        if not isinstance(row, dict) or not isinstance(row.get("attributes"), dict):
            raise ValueError("收藏条目缺少 attributes")
        attrs = row["attributes"]
        title = attrs.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 1000:
            raise ValueError("收藏作品标题无效")
        if str(row.get("id", key)) != key:
            raise ValueError("收藏 ID 与条目不匹配")
        language = attrs.get("language_id", 0)
        if type(language) is not int or not 1 <= language <= 12:
            language = 0
        result[key] = {
            "id": int(key),
            "attributes": {
                "title": title.strip(),
                "language_id": language,
                "thumbnail": f"{key}/thumbnails/cover.webp",
            },
        }
    return result


class FavoriteStore:
    def __init__(self, path):
        self.path = Path(path)

    def _read(self):
        try:
            with self.path.open("rb") as source:
                data = source.read(MAX_BYTES + 1)
        except FileNotFoundError:
            return {}
        if len(data) > MAX_BYTES:
            raise ValueError("收藏文件超过 8 MiB")
        return parse_favorites(json.loads(data.decode("utf-8-sig")))

    def rows(self, query="", limit=100):
        values = self._read()
        rows = []
        for key, item in values.items():
            title = item["attributes"]["title"]
            if (
                query
                and query not in {"全部", "我的收藏"}
                and query.casefold() not in title.casefold()
                and query != key
            ):
                continue
            rows.append(
                {
                    "id": key,
                    "title": title,
                    "url": f"{BASE_URL}/articles/{key}",
                    "thumbnail_url": f"https://cdn.imagedeliveries.com/{key}/thumbnails/cover.webp",
                    "input_kind": "article",
                    "module_id": "hentaipaw",
                    "source": "软件收藏",
                }
            )
        return rows[: max(0, min(int(limit), MAX_ITEMS))]

    def _save(self, values):
        encoded = json.dumps(values, ensure_ascii=False, indent=2)
        if len(values) > MAX_ITEMS or len(encoded.encode("utf-8")) > MAX_BYTES:
            raise ValueError("收藏数量或文件大小超过上限")
        write_text_atomic(self.path, encoded)

    def import_file(self, source):
        with Path(source).open("rb") as handle:
            raw = handle.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("导入收藏超过 8 MiB")
        incoming = parse_favorites(json.loads(raw.decode("utf-8-sig")))
        with output_path_lock(self.path):
            current = self._read()
            current.update(incoming)
            self._save(current)
        return len(incoming)

    def set(self, target, title, desired):
        key = normalize_article_url(target).rsplit("/", 1)[-1]
        with output_path_lock(self.path):
            values = self._read()
            if desired:
                row = {key: {"id": int(key), "attributes": {"title": str(title or key)}}}
                values.update(parse_favorites(row))
            else:
                values.pop(key, None)
            self._save(values)

    def export_file(self, destination):
        destination = Path(destination).expanduser().resolve()
        if destination.suffix.lower() != ".json" or destination in {
            self.path.resolve(),
            self.path.with_name("cookies.json").resolve(),
        }:
            raise ValueError("请选择独立的 JSON 文件导出收藏")
        with output_path_lock(self.path):
            values = self._read()
        write_text_atomic(destination, json.dumps(values, ensure_ascii=False, indent=2))
        return len(values)
