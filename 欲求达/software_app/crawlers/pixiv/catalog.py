from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Callable
from urllib.parse import quote, urlencode


from .bookmark_discovery import BookmarkDiscovery
from .catalog_discovery import CatalogDiscovery, object_field, list_field


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
        self.last_bookmark_rows: list[dict] = []
        self.last_discovery_status: dict = {}
        self.last_discovery_rows: list[dict] = []

    def _discovery(self, kind, limit):
        return CatalogDiscovery(kind, limit, self.last_discovery_status, self.last_discovery_rows)

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
        def row(item):
            value = artwork_row(item, source=f"漫画系列 {series_id}", fallback_type="漫画")
            if value:
                value.update(series_id=series_id, series_order=item.get("order"))
            return value

        with self._discovery("manga_series", limit) as discovery:
            for page in range(1, 101):
                body = discovery.request(self.body, f"/ajax/series/{series_id}?p={page}&lang=zh")
                data = object_field(body, "page")
                items = list_field(data, "series")
                discovery.add_page(items, row, total=data.get("total"))
                # There is no known requested page size for this route. Missing
                # total must continue until an explicit empty page, not len(rows).
                if discovery.finish(terminal=not items):
                    return list(discovery.rows)

    def novel_series(self, series_id: str, limit: int = 100) -> list[dict]:
        def row(item):
            value = novel_row(item, source=f"小说系列 {series_id}")
            if value:
                value.update(series_id=series_id, series_order=item.get("order"))
            return value

        with self._discovery("novel_series", limit) as discovery:
            last_order = 0
            while True:
                count = min(30, limit - len(discovery.rows))
                body = discovery.request(self.body, f"/ajax/novel/series_content/{series_id}?"
                                 + urlencode({"limit": count, "last_order": last_order, "order_by": "asc", "lang": "zh"}))
                data = object_field(body, "page")
                items = list_field(data, "seriesContents")
                discovery.add_page(items, row, total=data["total"] if data.get("total") is not None else body.get("total"))
                if discovery.finish(terminal=not items or (len(items) < count and discovery.status["total"] is None)):
                    return list(discovery.rows)
                last_order += len(items)

    def novel_search(self, keyword: str, limit: int = 100) -> list[dict]:
        with self._discovery("novel_search", limit) as discovery:
            for page in range(1, 11):
                body = discovery.request(self.body, f"/ajax/search/novels/{quote(keyword)}?"
                                 + urlencode({"word": keyword, "order": "date_d", "mode": "all", "p": page, "s_mode": "s_tag_full", "lang": "zh"}))
                data = object_field(body, "novel")
                items = list_field(data, "data")
                discovery.add_page(items, lambda item: novel_row(item, source=f"小说搜索：{keyword}"), total=data.get("total"))
                if discovery.finish(terminal=not items or (len(items) < 20 and discovery.status["total"] is None)):
                    return list(discovery.rows)

    def bookmarks(
        self, user_id: str, *, limit: int = 100, visibility: str = "show", tag: str = "",
        start_date: str = "", end_date: str = "", date_basis: str = "bookmarked",
    ) -> list[dict]:
        return self._bookmarks(user_id, "work", limit, visibility, tag, start_date, end_date, date_basis)

    def novel_bookmarks(
        self, user_id: str, *, limit: int = 100, visibility: str = "show", tag: str = "",
        start_date: str = "", end_date: str = "", date_basis: str = "bookmarked",
    ) -> list[dict]:
        return self._bookmarks(user_id, "novel", limit, visibility, tag, start_date, end_date, date_basis)

    def _bookmarks(self, user_id, kind, limit, visibility, tag, start_date, end_date, date_basis):
        discovery = BookmarkDiscovery(
            self.body, novel_row if kind == "novel" else artwork_row,
            kind=kind, limit=limit, visibility=visibility, tag=tag,
            start_date=start_date, end_date=end_date, date_basis=date_basis,
        )
        self.last_bookmark_status = discovery.status
        self.last_bookmark_rows = discovery.rows
        return discovery.run(user_id)

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
        def row(item):
            value = artwork_row(item, source=f"排行榜 {mode}")
            if value:
                value["rank"] = item.get("rank")
            return value

        with self._discovery("ranking", limit) as discovery:
            for page in range(1, 21):
                params = {"mode": mode, "p": page, "format": "json"}
                if content:
                    params["content"] = content
                if date:
                    params["date"] = date
                payload = discovery.request(self.json, f"{PIXIV_BASE}/ranking.php?{urlencode(params)}")
                items = list_field(payload, "contents")
                discovery.add_page(items, row, total=payload.get("total"))
                # A selected count is already fulfilled; no cursor outside that
                # selected range is required. Otherwise an explicit end is needed.
                if discovery.finish():
                    return list(discovery.rows)
                if "next" not in payload:
                    discovery.fail("invalid_cursor")
                following = payload["next"]
                if following is False or type(following) is int and following == 0:
                    if discovery.finish(terminal=True):
                        return list(discovery.rows)
                elif following is not True and str(following) != str(page + 1):
                    discovery.fail("invalid_cursor")
                elif not items:
                    discovery.fail("no_progress")

    def new_works(self, query: str, limit: int = 100) -> list[dict]:
        value = str(query or "").casefold()
        type_mode = "manga" if "漫画" in value or "manga" in value else "illust"
        r18 = "r18" in value or "r-18" in value
        with self._discovery("new_works", limit) as discovery:
            last_id, cursors = "0", {"0"}
            while True:
                count = min(60, limit - len(discovery.rows))
                body = discovery.request(self.body, "/ajax/illust/new?"
                                 + urlencode({"lastId": last_id, "limit": count, "type": type_mode, "r18": str(r18).lower(), "lang": "zh"}))
                items = list_field(body, "illusts")
                discovery.add_page(items, lambda item: artwork_row(item, source="Pixiv 新作", fallback_type="漫画" if type_mode == "manga" else "插画"), total=body.get("total"))
                if discovery.finish(terminal=not items):
                    return list(discovery.rows)
                if "lastId" not in body:
                    if len(items) < count and discovery.finish(terminal=True):
                        return list(discovery.rows)
                    discovery.fail("invalid_cursor")
                following = body["lastId"]
                if following in (None, ""):
                    if discovery.finish(terminal=True):
                        return list(discovery.rows)
                next_id = str(following)
                if not next_id.isascii() or not next_id.isdigit() or len(next_id) > 32 or next_id in cursors:
                    discovery.fail("invalid_cursor")
                cursors.add(next_id)
                last_id = next_id

    def author_tag(self, query: str, limit: int = 100) -> list[dict]:
        match = re.match(r"\s*(\d+)\s+(.+?)\s*$", str(query or ""))
        if not match:
            raise ValueError("作者 + 标签请输入：作者ID 标签，例如 123456 风景")
        user_id, tag = match.groups()
        with self._discovery("author_tag", limit) as discovery:
            offset = 0
            while True:
                count = min(48, limit - len(discovery.rows))
                body = discovery.request(self.body, f"/ajax/user/{user_id}/illustmanga/tag?"
                                 + urlencode({"tag": tag, "offset": offset, "limit": count, "lang": "zh"}))
                items = list_field(body, "works", "data", mapping=True)
                discovery.add_page(items, lambda item: artwork_row(item, source=f"作者 {user_id} · {tag}"), total=body.get("total"))
                if discovery.finish(terminal=not items or (len(items) < count and discovery.status["total"] is None)):
                    return list(discovery.rows)
                offset += len(items)

    def artwork_search(self, keyword, limit=20, search_mode="", options=None):
        options = options or {}
        type_value = {"标签（插画）": "illust_and_ugoira", "标签（漫画）": "manga"}.get(search_mode, "all")
        match = "s_tc" if search_mode == "标题 / 简介" else "s_tag_full"
        age = str(options.get("age_mode") or "all").strip().lower()
        params = {"word": keyword, "order": "date_d", "mode": age if age in {"safe", "r18"} else "all"}
        if options.get("start_date"):
            params["scd"] = str(options["start_date"])
        if options.get("end_date"):
            params["ecd"] = str(options["end_date"])
        minimum = max(0, int(options.get("minimum_bookmarks") or 0))
        if minimum:
            params["blt"] = minimum

        def row(item):
            value = artwork_row(item, fallback_type="插画")
            if value:
                value["search_mode"] = search_mode or "标签（全部）"
            return value

        with self._discovery("artwork_search", limit) as discovery:
            for page in range(1, 11):
                body = discovery.request(self.body, f"/ajax/search/artworks/{quote(keyword)}?" + urlencode({**params, "p": page, "s_mode": match, "type": type_value}))
                data = object_field(body, "illustManga")
                items = list_field(data, "data")
                discovery.add_page(items, row, total=data.get("total"))
                if discovery.finish(terminal=not items or (len(items) < 20 and discovery.status["total"] is None)):
                    return list(discovery.rows)


__all__ = ["PixivCatalog", "artwork_row", "novel_row", "filter_bookmark_candidates"]
