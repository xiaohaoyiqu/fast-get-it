"""Bookmark pagination evidence, kept separate from media download results."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from urllib.parse import urlencode

from software_app.core.adapter import TaskCancelled
from software_app.core.network_policy import request_checkpoint
from software_app.core.security import safe_error_message


REASONS = {
    "not_started": "尚未读取",
    "running": "读取中",
    "exhausted": "已读到末尾",
    "limit_reached": "达到本次数量上限",
    "page_limit": "达到分页安全上限",
    "no_progress": "返回重复页，没有新增作品",
    "count_mismatch": "返回数量不足以核对网站总数",
    "total_changed": "读取期间网站总数变化",
    "invalid_items": "存在无法识别的条目",
    "invalid_response": "分页数据格式异常",
    "request_failed": "分页请求失败",
    "cancelled": "读取已取消",
    "empty_preserved": "本次为空，保留原缓存待复核",
    "unknown": "缺少发现完整性记录",
}
VISIBILITY_NAMES = {"show": "公开", "hide": "私密", "both": "公开和私密"}


class BookmarkDiscovery:
    def __init__(self, body_getter, row_factory, *, kind, limit, visibility, tag, start_date, end_date, date_basis):
        if visibility not in VISIBILITY_NAMES:
            raise ValueError("Pixiv 收藏范围必须为 show、hide 或 both")
        self.body = body_getter
        self.row_factory = row_factory
        self.kind = kind
        self.limit = max(1, min(int(limit), 10000))
        self.tag = tag
        self.start_date = start_date
        self.end_date = end_date
        self.date_basis = date_basis
        self.rows = []
        scopes = ("show", "hide") if visibility == "both" else (visibility,)
        self.status = {
            "version": 1,
            "complete": False,
            "visibility": visibility,
            "limit": self.limit,
            "scopes": [
                {
                    "visibility": scope,
                    "complete": False,
                    "reason": "not_started",
                    "total": None,
                    "pages": 0,
                    "fetched": 0,
                    "selected": 0,
                    "filtered": 0,
                    "duplicates": 0,
                    "invalid": 0,
                    "next_offset": 0,
                }
                for scope in scopes
            ],
        }
        self._selected = set()

    def run(self, user_id):
        for scope in self.status["scopes"]:
            if len(self.rows) >= self.limit:
                scope["reason"] = "limit_reached"
                continue
            try:
                self._read_scope(user_id, scope)
            except TaskCancelled:
                scope.update(complete=False, reason="cancelled")
                raise
            except Exception as exc:
                scope.update(
                    complete=False,
                    reason="invalid_response" if isinstance(exc, ValueError) else "request_failed",
                    error=safe_error_message(exc)[:1000],
                )
                raise
            finally:
                self.status["complete"] = all(item["complete"] for item in self.status["scopes"])
        return self.rows

    def _read_scope(self, user_id, scope):
        page_size, max_pages = (24, 500) if self.kind == "novel" else (48, 250)
        endpoint = "novels" if self.kind == "novel" else "illusts"
        seen = set()
        total_changed = False
        scope["reason"] = "running"
        while len(self.rows) < self.limit and scope["pages"] < max_pages:
            request_checkpoint()
            count = min(page_size, self.limit - len(self.rows))
            scope["request_offset"] = scope["next_offset"]
            scope["requested_count"] = count
            scope["attempted_pages"] = scope.get("attempted_pages", 0) + 1
            body = self.body(
                f"/ajax/user/{user_id}/{endpoint}/bookmarks?"
                + urlencode(
                    {
                        "tag": self.tag,
                        "offset": scope["next_offset"],
                        "limit": count,
                        "rest": scope["visibility"],
                        "lang": "zh",
                    }
                )
            )
            items, total = self._page(body)
            scope["pages"] += 1
            if total is not None:
                if scope["total"] is not None and total != scope["total"]:
                    total_changed = True
                scope["total"] = total if scope["total"] is None else max(total, scope["total"])
                scope["reported_total"] = total
                scope["total_changed"] = total_changed
            before = len(seen)
            for item in items:
                request_checkpoint()
                try:
                    row = self.row_factory(
                        item,
                        source=f"{'小说' if self.kind == 'novel' else '作品'}收藏（"
                        f"{VISIBILITY_NAMES[scope['visibility']]}）",
                    )
                except (TypeError, ValueError, OverflowError):
                    row = None
                if row is None:
                    scope["invalid"] += 1
                    continue
                key = row["id"]
                if key in seen:
                    scope["duplicates"] += 1
                    continue
                seen.add(key)
                scope["fetched"] = len(seen)
                row["visibility"] = scope["visibility"]
                if self.kind == "novel":
                    bookmark = item.get("bookmarkData")
                    bookmark = bookmark if isinstance(bookmark, dict) else {}
                    row["bookmarked_at"] = str(
                        bookmark.get("bookmarkDate") or bookmark.get("createDate") or item.get("bookmarkDate") or ""
                    )
                date_key = "published_at" if self.date_basis == "published" else "bookmarked_at"
                date = str(row.get(date_key) or "")[:10]
                if (
                    (self.start_date or self.end_date)
                    and not date
                    or self.start_date
                    and date < self.start_date
                    or self.end_date
                    and date > self.end_date
                ):
                    scope["filtered"] += 1
                    continue
                if key in self._selected:
                    scope["duplicates"] += 1
                    continue
                if len(self.rows) < self.limit:
                    self._selected.add(key)
                    self.rows.append(row)
                    scope["selected"] += 1
            scope["next_offset"] += len(items)
            terminal = len(items) < count
            enough = scope["total"] is None or len(seen) >= scope["total"]
            if terminal and enough:
                reason = "total_changed" if total_changed else "invalid_items" if scope["invalid"] else "exhausted"
                scope.update(complete=reason == "exhausted", reason=reason)
                return
            if not items:
                scope["reason"] = "count_mismatch"
                return
            if len(seen) == before:
                scope["reason"] = "invalid_items" if scope["invalid"] else "no_progress"
                return
        scope["reason"] = "limit_reached" if len(self.rows) >= self.limit else "page_limit"

    def _page(self, body):
        if not isinstance(body, dict):
            raise ValueError("Pixiv 收藏分页不是对象，未按空收藏处理")
        keys = ("works", "novels", "data") if self.kind == "novel" else ("works",)
        items = next((body[key] for key in keys if key in body), None)
        if self.kind == "novel" and isinstance(items, dict):
            items = list(items.values())
        if not isinstance(items, list):
            raise ValueError("Pixiv 收藏分页缺少有效作品列表，未按空收藏处理")
        total = body.get("total")
        if total is not None:
            if isinstance(total, bool) or not str(total).isascii() or not str(total).isdigit():
                raise ValueError("Pixiv 收藏总数格式异常")
            total = int(total)
        return items, total


def require_downloadable_discovery(status):
    scopes = status.get("scopes") if isinstance(status, dict) else None
    if not isinstance(scopes, list) or not scopes:
        raise ValueError("Pixiv 收藏缺少发现记录，未开始集合下载")
    # A user-selected count limit is expected. Other early stops must not turn
    # an incomplete discovery into a successful download of an arbitrary subset.
    for scope in scopes:
        reason = scope.get("reason") if isinstance(scope, dict) else "unknown"
        if isinstance(scope, dict):
            if scope.get("invalid"):
                reason = "invalid_items"
            elif scope.get("total_changed"):
                reason = "total_changed"
        if reason not in {"exhausted", "limit_reached"}:
            label = REASONS.get(str(reason), "发现状态异常")
            raise ValueError(f"Pixiv 收藏发现未完成：{label}，未开始集合下载；请重新读取后重试")


def merge_bookmark_cache(previous, rows, status, *, account_id, kind, visibility, error=""):
    status = deepcopy(status) if isinstance(status, dict) else {}
    requested = {"show", "hide"} if visibility == "both" else {visibility}
    scopes = status.get("scopes", [])
    scopes = scopes if isinstance(scopes, list) else []
    complete = (
        bool(status.get("complete"))
        and not error
        and bool(scopes)
        and requested
        == {item.get("visibility") for item in scopes if isinstance(item, dict) and item.get("complete") is True}
    )
    previous_rows = previous.get("items", []) if previous.get("account") == account_id else []
    reason = "exhausted" if complete else "unknown"
    if not rows and previous_rows:
        complete, reason = False, "empty_preserved"
    retained = 0
    result, seen = [], set()
    for row in rows:
        if isinstance(row, dict) and row.get("url") and row["url"] not in seen:
            result.append(row)
            seen.add(row["url"])
    fetched = len(result)
    for row in previous_rows:
        if not isinstance(row, dict) or not row.get("url") or row["url"] in seen:
            continue
        # An unrequested or unknown scope never becomes a removal instruction.
        if complete and row.get("visibility") in requested:
            continue
        result.append(row)
        seen.add(row["url"])
        retained += 1
    if not complete and reason != "empty_preserved":
        reason = next(
            (item.get("reason", "unknown") for item in scopes if isinstance(item, dict) and not item.get("complete")),
            "unknown",
        )
    return {
        "version": 2,
        "platform": "pixiv",
        "kind": kind,
        "account": account_id,
        "count": len(result),
        "fetched_count": fetched,
        "retained_count": retained,
        "complete": complete,
        "partial": not complete,
        "visibility": visibility,
        "stop_reason": reason,
        "scopes": scopes,
        "discovery": status,
        "refresh_error": safe_error_message(error)[:1000] if error else "",
        "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": result,
    }


def bookmark_cache_details(payload):
    visibility = VISIBILITY_NAMES.get(str(payload.get("visibility") or ""), "旧版未记录范围")
    lines = [
        f"本次刷新范围：{visibility}",
        "发现状态："
        + (
            "所选范围已读到末尾"
            if payload.get("complete")
            else REASONS.get(str(payload.get("stop_reason") or ""), "部分或旧版记录")
        ),
    ]
    scopes = payload.get("scopes", [])
    for scope in scopes if isinstance(scopes, list) else []:
        if isinstance(scope, dict):
            lines.append(
                f"{VISIBILITY_NAMES.get(str(scope.get('visibility') or ''), '未知范围')}："
                f"{scope.get('pages', 0)} 页，发现 {scope.get('fetched', 0)} 项；"
                + REASONS.get(str(scope.get("reason") or ""), "未记录停止原因")
            )
    lines.append(f"本次保留旧条目：{payload.get('retained_count', 0)} 项")
    if payload.get("refresh_error"):
        lines.append("最近读取错误：" + safe_error_message(payload["refresh_error"]))
    return "\n".join(lines)
