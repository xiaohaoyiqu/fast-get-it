"""Bounded catalog discovery evidence, separate from media retry plans."""

from __future__ import annotations

from software_app.core.adapter import TaskCancelled
from software_app.core.network_policy import request_checkpoint


MAX_PAGES = 100
MAX_PAGE_ITEMS = 1000
REASONS = {
    "invalid_response": "分页列表或计数格式异常",
    "invalid_items": "存在无法识别的作品条目",
    "no_progress": "分页没有新增作品",
    "count_mismatch": "已读数量与网站总数不符",
    "total_changed": "读取期间网站总数变化",
    "invalid_cursor": "下一页标记缺失、重复或异常",
    "page_limit": "达到分页安全上限",
}


def count_value(value):
    if value is None:
        return None
    text = str(value)
    if isinstance(value, bool) or not text.isascii() or not text.isdigit() or len(text) > 10:
        raise ValueError("Pixiv 集合总数格式异常")
    return int(text)


def object_field(value, key):
    if not isinstance(value, dict) or not isinstance(value.get(key), dict):
        raise ValueError("Pixiv 集合缺少有效分页对象，未按空集合处理")
    return value[key]


def list_field(value, *keys, mapping=False):
    if not isinstance(value, dict):
        raise ValueError("Pixiv 集合分页不是对象，未按空集合处理")
    items = next((value[key] for key in keys if key in value), None)
    if mapping and isinstance(items, dict):
        items = list(items.values())
    if not isinstance(items, list) or len(items) > MAX_PAGE_ITEMS:
        raise ValueError("Pixiv 集合缺少有效作品列表或单页过大，未按空集合处理")
    return items


class CatalogDiscovery:
    def __init__(self, kind, limit, status, rows):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Pixiv 集合读取数量须为 1–100")
        self.limit, self.rows, self.status = limit, rows, status
        self.seen = set()
        rows.clear()
        status.clear()
        status.update(
            version=1,
            kind=kind,
            limit=limit,
            complete=False,
            reason="running",
            pages=0,
            fetched=0,
            selected=0,
            duplicates=0,
            invalid=0,
            total=None,
        )

    def __enter__(self):
        try:
            request_checkpoint()
        except TaskCancelled:
            self.status["reason"] = "cancelled"
            raise
        return self

    def __exit__(self, error_type, error, _traceback):
        if error is not None and self.status["reason"] == "running":
            self.status["reason"] = (
                "cancelled"
                if isinstance(error, TaskCancelled)
                else "invalid_response"
                if isinstance(error, ValueError)
                else "request_failed"
            )
        if error is None and self.status["reason"] == "running":
            self.fail("page_limit")
        return False

    def fail(self, reason):
        self.status.update(complete=False, reason=reason)
        raise ValueError(f"Pixiv 集合发现未完成：{REASONS[reason]}，未按完整集合返回；请重新读取后重试")

    def request(self, getter, path):
        request_checkpoint()
        if self.status["pages"] >= MAX_PAGES:
            self.fail("page_limit")
        return getter(path)

    def add_page(self, items, row_factory, *, total=None):
        request_checkpoint()
        if self.status["pages"] >= MAX_PAGES:
            self.fail("page_limit")
        self.status["pages"] += 1
        total = count_value(total)
        previous = self.status["total"]
        if total is not None:
            if previous is not None and previous != total:
                self.fail("total_changed")
            self.status["total"] = total
        before = len(self.seen)
        for item in items:
            request_checkpoint()
            try:
                row = row_factory(item)
            except (TypeError, ValueError, OverflowError):
                row = None
            if (
                not isinstance(row, dict)
                or not str(row.get("id") or "").isascii()
                or not str(row.get("id") or "").isdigit()
                or len(str(row["id"])) > 32
            ):
                self.status["invalid"] += 1
                self.fail("invalid_items")
            key = str(row["id"])
            if key in self.seen:
                self.status["duplicates"] += 1
                continue
            self.seen.add(key)
            if len(self.rows) < self.limit:
                self.rows.append(row)
            self.status.update(fetched=len(self.seen), selected=len(self.rows))
        self.status.update(fetched=len(self.seen), selected=len(self.rows))
        if items and len(self.seen) == before:
            self.fail("no_progress")
        if self.status["total"] is not None and len(self.seen) > self.status["total"]:
            self.fail("count_mismatch")

    def finish(self, *, terminal=False):
        """A selected limit is allowed; an early empty/terminal page is checked."""
        request_checkpoint()
        total = self.status["total"]
        if total is not None and len(self.seen) == total and len(self.rows) == len(self.seen):
            self.status.update(reason="exhausted", complete=True)
            return True
        if len(self.rows) >= self.limit:
            self.status.update(reason="limit_reached", complete=False)
            return True
        if terminal:
            if total is not None and len(self.seen) < total:
                self.fail("count_mismatch")
            self.status.update(reason="exhausted", complete=True)
            return True
        return False


def profile_ids(body, keys, limit):
    """profile/all is a single response, but missing fields are not empty works."""
    if not isinstance(body, dict) or not any(key in body for key in keys):
        raise ValueError("Pixiv 作者作品索引缺少必要字段，未按空作者处理")
    ids = set()
    for key in keys:
        values = body.get(key)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ValueError("Pixiv 作者作品索引格式异常")
        for value in values:
            request_checkpoint()
            value = str(value)
            if not value.isascii() or not value.isdigit() or len(value) > 32:
                raise ValueError("Pixiv 作者作品索引含无效作品 ID")
            ids.add(value)
    return sorted(ids, key=int, reverse=True)[:limit]
