from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import quote, urlencode


PIXIV_BASE = "https://www.pixiv.net"


def _visible_text(value: object) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def _tags(item: dict) -> list[str]:
    value = item.get("tags") or []
    if isinstance(value, dict):
        value = value.get("tags") or []
    if not isinstance(value, list):
        return []
    return [
        _visible_text(tag.get("tag") if isinstance(tag, dict) else tag)
        for tag in value
        if str(tag.get("tag") if isinstance(tag, dict) else tag).strip()
    ]


def _preview_image_url(item: dict) -> str:
    for key in ("url", "imageUrl", "image_url", "profileImageUrl", "coverUrl"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    for key in ("urls", "images", "cover"):
        value = item.get(key)
        if isinstance(value, dict):
            for size in ("regular", "small", "medium", "original", "url"):
                candidate = value.get(size)
                if isinstance(candidate, str) and candidate.startswith(("http://", "https://")):
                    return candidate
    return ""


def artwork_row(item: object, *, source: str = "Pixiv", fallback_type: str = "作品") -> dict | None:
    if not isinstance(item, dict):
        return None
    work_id = str(
        item.get("id")
        or item.get("workId")
        or item.get("illustId")
        or item.get("illust_id")
        or ""
    ).strip()
    if not work_id.isdigit():
        return None
    try:
        illust_type = int(item.get("illustType", item.get("illust_type", -1)))
    except (TypeError, ValueError):
        illust_type = -1
    kind = {0: "插画", 1: "漫画", 2: "动图"}.get(illust_type, fallback_type)
    bookmark_data = item.get("bookmarkData") if isinstance(item.get("bookmarkData"), dict) else {}
    return {
        "id": work_id,
        "title": _visible_text(item.get("title") or item.get("illust_title") or work_id),
        "author_id": str(item.get("userId") or item.get("user_id") or ""),
        "author_name": _visible_text(item.get("userName") or item.get("user_name") or ""),
        "tags": _tags(item),
        "url": f"{PIXIV_BASE}/artworks/{work_id}",
        "ai_type": int(item.get("aiType") or 0),
        "type": kind,
        "source": source,
        "published_at": str(item.get("createDate") or item.get("date") or ""),
        "page_count": item.get("pageCount", item.get("page_count", "")),
        "likes": item.get("likeCount", item.get("likes", "")),
        "bookmarks": item.get("bookmarkCount", item.get("bookmarks", "")),
        "views": item.get("viewCount", item.get("views", "")),
        "bookmark_id": str(bookmark_data.get("id") or item.get("bookmark_id") or ""),
        "bookmarked_at": str(
            bookmark_data.get("bookmarkDate")
            or bookmark_data.get("createDate")
            or item.get("bookmarkDate")
            or ""
        ),
        "avatar_url": _preview_image_url(item),
        "thumbnail_url": _preview_image_url(item),
    }


def novel_row(item: object, *, source: str = "Pixiv 小说") -> dict | None:
    if not isinstance(item, dict):
        return None
    novel_id = str(item.get("id") or item.get("novelId") or item.get("workId") or "").strip()
    if not novel_id.isdigit():
        return None
    return {
        "id": novel_id,
        "title": _visible_text(item.get("title") or novel_id),
        "author_id": str(item.get("userId") or item.get("user_id") or ""),
        "author_name": _visible_text(item.get("userName") or item.get("user_name") or ""),
        "tags": _tags(item),
        "url": f"{PIXIV_BASE}/novel/show.php?id={novel_id}",
        "type": "小说",
        "source": source,
        "published_at": str(item.get("createDate") or item.get("date") or ""),
        "page_count": item.get("pageCount", item.get("page_count", "")),
        "likes": item.get("likeCount", item.get("likes", "")),
        "bookmarks": item.get("bookmarkCount", item.get("bookmarks", "")),
        "views": item.get("viewCount", item.get("views", "")),
        "input_kind": "novel",
        "avatar_url": _preview_image_url(item),
        "thumbnail_url": _preview_image_url(item),
    }


def filter_bookmark_candidates(
    rows: list[dict], *, query: str = "", field: str = "全部",
    date_basis: str = "收藏时间", start_date: str = "", end_date: str = "",
) -> list[dict]:
    """Filter an already cached collection without changing its persisted contents."""
    needle = unicodedata.normalize("NFKC", query.strip()).casefold()
    fields = {
        "作品 ID": ("id",),
        "作者": ("author_id", "author_name"),
        "标签": ("tags",),
        "标题": ("title",),
    }.get(field, ("id", "author_id", "author_name", "tags", "title"))
    date_key = "published_at" if date_basis == "发布时间" else "bookmarked_at"
    result: list[dict] = []
    for row in rows:
        if needle:
            if field == "作品 ID" and needle != str(row.get("id") or "").strip():
                continue
            values = []
            for key in fields:
                value = row.get(key, "")
                values.extend(value if isinstance(value, list) else [value])
            if not any(needle in unicodedata.normalize("NFKC", str(value)).casefold() for value in values):
                continue
        if start_date or end_date:
            value = str(row.get(date_key) or "")[:10]
            if not value or (start_date and value < start_date) or (end_date and value > end_date):
                continue
        result.append(row)
    return result


class PixivCatalog:
    """Small, testable catalog queries shared by previews and download planning."""

    def __init__(self, body_getter: Callable[[str], object], json_getter: Callable[[str], dict]) -> None:
        self.body = body_getter
        self.json = json_getter
        self.last_bookmark_status: dict = {}

    @staticmethod
    def _unique(rows: list[dict], limit: int) -> list[dict]:
        result: list[dict] = []
        seen: set[tuple[str, str]] = set()
        for row in rows:
            key = (str(row.get("input_kind") or "work"), str(row.get("id") or ""))
            if not key[1] or key in seen:
                continue
            seen.add(key)
            result.append(row)
            if len(result) >= limit:
                break
        return result

    def manga_series(self, series_id: str, limit: int = 100) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 101):
            body = self.body(f"/ajax/series/{series_id}?p={page}&lang=zh")
            page_data = body.get("page", {}) if isinstance(body, dict) else {}
            items = page_data.get("series", []) if isinstance(page_data, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                row = artwork_row(item, source=f"漫画系列 {series_id}", fallback_type="漫画")
                if row:
                    row["series_id"] = series_id
                    row["series_order"] = (item or {}).get("order") if isinstance(item, dict) else None
                    rows.append(row)
            if len(rows) >= limit or len(rows) >= int(page_data.get("total") or len(rows)):
                break
        return self._unique(rows, limit)

    def novel_series(self, series_id: str, limit: int = 100) -> list[dict]:
        rows: list[dict] = []
        last_order = 0
        while len(rows) < limit:
            page_limit = min(30, limit - len(rows))
            body = self.body(
                f"/ajax/novel/series_content/{series_id}?"
                + urlencode({"limit": page_limit, "last_order": last_order, "order_by": "asc", "lang": "zh"})
            )
            page_data = body.get("page", {}) if isinstance(body, dict) else {}
            items = page_data.get("seriesContents", []) if isinstance(page_data, dict) else []
            if not isinstance(items, list) or not items:
                break
            before = len(rows)
            for item in items:
                row = novel_row(item, source=f"小说系列 {series_id}")
                if row:
                    row["series_id"] = series_id
                    row["series_order"] = (item or {}).get("order") if isinstance(item, dict) else None
                    rows.append(row)
            if len(rows) == before:
                break
            last_order += len(items)
            total = int(page_data.get("total") or body.get("total") or 0) if isinstance(body, dict) else 0
            if (total and len(rows) >= total) or len(items) < page_limit:
                break
        return self._unique(rows, limit)

    def novel_search(self, keyword: str, limit: int = 100) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, 11):
            body = self.body(
                f"/ajax/search/novels/{quote(keyword)}?"
                + urlencode({"word": keyword, "order": "date_d", "mode": "all", "p": page, "s_mode": "s_tag_full", "lang": "zh"})
            )
            container = body.get("novel", {}) if isinstance(body, dict) else {}
            items = container.get("data", []) if isinstance(container, dict) else []
            if not isinstance(items, list) or not items:
                break
            rows.extend(row for item in items if (row := novel_row(item, source=f"小说搜索：{keyword}")))
            total = int(container.get("total") or 0) if isinstance(container, dict) else 0
            if len(rows) >= limit or (total and len(rows) >= total) or len(items) < 20:
                break
        return self._unique(rows, limit)

    def bookmarks(
        self,
        user_id: str,
        *,
        limit: int = 100,
        visibility: str = "show",
        tag: str = "",
        start_date: str = "",
        end_date: str = "",
        date_basis: str = "bookmarked",
    ) -> list[dict]:
        rows: list[dict] = []
        rest_values = ("show", "hide") if visibility == "both" else (("hide",) if visibility == "hide" else ("show",))
        scope_status: list[dict] = []
        for rest in rest_values:
            offset = 0
            exhausted = False
            known_total = 0
            fetched_ids: set[str] = set()
            page_count = 0
            while len(rows) < limit and page_count < 250:
                page_count += 1
                count = min(48, limit - len(rows))
                body = self.body(
                    f"/ajax/user/{user_id}/illusts/bookmarks?"
                    + urlencode({"tag": tag, "offset": offset, "limit": count, "rest": rest, "lang": "zh"})
                )
                items = body.get("works", []) if isinstance(body, dict) else []
                known_total = int(body.get("total") or 0) if isinstance(body, dict) else 0
                if not isinstance(items, list) or not items:
                    exhausted = not known_total or (offset >= known_total and len(fetched_ids) >= known_total)
                    break
                for item in items:
                    row = artwork_row(item, source=f"作品收藏（{'私密' if rest == 'hide' else '公开'}）")
                    if row:
                        fetched_ids.add(row["id"])
                        row["visibility"] = rest
                        date_key = "published_at" if date_basis == "published" else "bookmarked_at"
                        filter_date = str(row.get(date_key) or "")[:10]
                        if (start_date or end_date) and not filter_date:
                            continue
                        if start_date and filter_date < start_date:
                            continue
                        if end_date and filter_date > end_date:
                            continue
                        rows.append(row)
                offset += count
                exhausted = (
                    (len(items) < count and len(fetched_ids) >= known_total)
                    if known_total else len(items) < count
                )
                if len(rows) >= limit or exhausted:
                    break
            scope_status.append({"visibility": rest, "complete": exhausted, "total": known_total, "fetched": len(fetched_ids)})
        self.last_bookmark_status = {
            "complete": len(scope_status) == len(rest_values) and all(item["complete"] for item in scope_status),
            "scopes": scope_status,
        }
        return self._unique(rows, limit)

    def novel_bookmarks(
        self,
        user_id: str,
        *,
        limit: int = 100,
        visibility: str = "show",
        tag: str = "",
        start_date: str = "",
        end_date: str = "",
        date_basis: str = "bookmarked",
    ) -> list[dict]:
        rows: list[dict] = []
        rest_values = ("show", "hide") if visibility == "both" else (("hide",) if visibility == "hide" else ("show",))
        scope_status: list[dict] = []
        for rest in rest_values:
            offset = 0
            exhausted = False
            known_total = 0
            fetched_ids: set[str] = set()
            page_count = 0
            while len(rows) < limit and page_count < 500:
                page_count += 1
                count = min(24, limit - len(rows))
                body = self.body(
                    f"/ajax/user/{user_id}/novels/bookmarks?"
                    + urlencode({"tag": tag, "offset": offset, "limit": count, "rest": rest, "lang": "zh"})
                )
                items = body.get("works", body.get("novels", body.get("data", []))) if isinstance(body, dict) else []
                known_total = int(body.get("total") or 0) if isinstance(body, dict) else 0
                if isinstance(items, dict):
                    items = list(items.values())
                if not isinstance(items, list) or not items:
                    exhausted = not known_total or (offset >= known_total and len(fetched_ids) >= known_total)
                    break
                for item in items:
                    row = novel_row(item, source=f"小说收藏（{'私密' if rest == 'hide' else '公开'}）")
                    if not row:
                        continue
                    fetched_ids.add(row["id"])
                    bookmark_data = item.get("bookmarkData") if isinstance(item, dict) and isinstance(item.get("bookmarkData"), dict) else {}
                    row["visibility"] = rest
                    row["bookmarked_at"] = str(
                        bookmark_data.get("bookmarkDate")
                        or bookmark_data.get("createDate")
                        or (item.get("bookmarkDate") if isinstance(item, dict) else "")
                        or ""
                    )
                    date_key = "published_at" if date_basis == "published" else "bookmarked_at"
                    filter_date = str(row.get(date_key) or "")[:10]
                    if (start_date or end_date) and not filter_date:
                        continue
                    if start_date and filter_date < start_date:
                        continue
                    if end_date and filter_date > end_date:
                        continue
                    rows.append(row)
                offset += count
                exhausted = (
                    (len(items) < count and len(fetched_ids) >= known_total)
                    if known_total else len(items) < count
                )
                if len(rows) >= limit or exhausted:
                    break
            scope_status.append({"visibility": rest, "complete": exhausted, "total": known_total, "fetched": len(fetched_ids)})
        self.last_bookmark_status = {
            "complete": len(scope_status) == len(rest_values) and all(item["complete"] for item in scope_status),
            "scopes": scope_status,
        }
        return self._unique(rows, limit)

    def ranking(self, query: str, limit: int = 100) -> list[dict]:
        tokens = [token for token in re.split(r"[\s,，]+", str(query or "").strip()) if token]
        aliases = {
            "日榜": "daily", "周榜": "weekly", "月榜": "monthly", "新人": "rookie",
            "原创": "original", "男性": "male", "女性": "female", "R18日榜": "daily_r18",
            "R18周榜": "weekly_r18", "daily": "daily", "weekly": "weekly", "monthly": "monthly",
        }
        mode = next((aliases[token] for token in tokens if token in aliases), "daily")
        content_aliases = {"插画": "illust", "漫画": "manga", "动图": "ugoira", "illust": "illust", "manga": "manga", "ugoira": "ugoira"}
        content = next((content_aliases[token] for token in tokens if token in content_aliases), "")
        date = next((token.replace("-", "") for token in tokens if re.fullmatch(r"\d{4}-?\d{2}-?\d{2}", token)), "")
        rows: list[dict] = []
        for page in range(1, 21):
            params = {"mode": mode, "p": page, "format": "json"}
            if content:
                params["content"] = content
            if date:
                params["date"] = date
            payload = self.json(f"{PIXIV_BASE}/ranking.php?{urlencode(params)}")
            items = payload.get("contents", []) if isinstance(payload, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                row = artwork_row(item, source=f"排行榜 {mode}")
                if row:
                    row["rank"] = (item or {}).get("rank") if isinstance(item, dict) else None
                    rows.append(row)
            if len(rows) >= limit or not payload.get("next"):
                break
        return self._unique(rows, limit)

    def new_works(self, query: str, limit: int = 100) -> list[dict]:
        value = str(query or "").casefold()
        type_mode = "manga" if "漫画" in value or "manga" in value else "illust"
        r18 = "r18" in value or "r-18" in value
        last_id = "0"
        rows: list[dict] = []
        while len(rows) < limit:
            count = min(60, limit - len(rows))
            body = self.body(
                f"/ajax/illust/new?"
                + urlencode({"lastId": last_id, "limit": count, "type": type_mode, "r18": str(r18).lower(), "lang": "zh"})
            )
            items = body.get("illusts", []) if isinstance(body, dict) else []
            if not isinstance(items, list) or not items:
                break
            for item in items:
                row = artwork_row(item, source="Pixiv 新作", fallback_type="漫画" if type_mode == "manga" else "插画")
                if row:
                    rows.append(row)
            next_id = str(body.get("lastId") or "") if isinstance(body, dict) else ""
            if not next_id or next_id == last_id or len(items) < count:
                break
            last_id = next_id
        return self._unique(rows, limit)

    def author_tag(self, query: str, limit: int = 100) -> list[dict]:
        match = re.match(r"\s*(\d+)\s+(.+?)\s*$", str(query or ""))
        if not match:
            raise ValueError("作者 + 标签请输入：作者ID 标签，例如 123456 风景")
        user_id, tag = match.groups()
        rows: list[dict] = []
        offset = 0
        while len(rows) < limit:
            count = min(48, limit - len(rows))
            body = self.body(
                f"/ajax/user/{user_id}/illustmanga/tag?"
                + urlencode({"tag": tag, "offset": offset, "limit": count, "lang": "zh"})
            )
            items = body.get("works", body.get("data", [])) if isinstance(body, dict) else []
            if isinstance(items, dict):
                items = list(items.values())
            if not isinstance(items, list) or not items:
                break
            rows.extend(row for item in items if (row := artwork_row(item, source=f"作者 {user_id} · {tag}")))
            offset += len(items)
            if len(items) < count:
                break
        return self._unique(rows, limit)


__all__ = ["PixivCatalog", "artwork_row", "novel_row", "filter_bookmark_candidates"]
