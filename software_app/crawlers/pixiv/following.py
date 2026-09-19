from __future__ import annotations

import html
import re
import threading
from datetime import datetime
from html.parser import HTMLParser
from typing import Callable, Iterable


PIXIV_BASE = "https://www.pixiv.net"
FOLLOWING_PAGE_SIZE = 48
_USER_PATH = re.compile(r"^/(?:[a-z]{2}/)?users/(\d+)/?$")
_PAGE_QUERY = re.compile(r"(?:[?&])p=(\d+)")


def _unescape_visible(value: object) -> str:
    text = str(value or "")
    for _ in range(3):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def normalize_following_user(item: object, visibility: str = "show") -> dict | None:
    if not isinstance(item, dict):
        return None
    user_id = str(item.get("userId") or item.get("user_id") or item.get("id") or "").strip()
    if not user_id.isdigit():
        return None
    name = str(item.get("userName") or item.get("name") or item.get("display_name") or "").strip()
    avatar = str(
        item.get("profileImageUrl")
        or item.get("profile_image_url")
        or item.get("imageBig")
        or item.get("image")
        or item.get("avatar_url")
        or ""
    ).strip()
    bio = str(
        item.get("userComment")
        or item.get("comment")
        or item.get("commentHtml")
        or item.get("bio")
        or ""
    ).strip()
    bio = re.sub(r"<br\s*/?>", "\n", bio, flags=re.I)
    bio = re.sub(r"<[^>]+>", "", bio)
    return {
        "user_id": user_id,
        "display_name": _unescape_visible(name),
        "bio": _unescape_visible(bio).strip(),
        "avatar_url": avatar,
        "profile_url": f"{PIXIV_BASE}/users/{user_id}",
        "visibility": "hide" if visibility == "hide" else "show",
    }


class _FollowingHTMLParser(HTMLParser):
    """Read stable user links/GTМ attributes without depending on generated CSS names."""

    def __init__(self, visibility: str) -> None:
        super().__init__(convert_charrefs=True)
        self.visibility = visibility
        self.items: dict[str, dict] = {}
        self.order: list[str] = []
        self.current_user_id = ""
        self.anchor_user_id = ""
        self.anchor_text: list[str] = []
        self.bio_text: list[str] = []
        self.header_done = False
        self.current_page = 1
        self.max_page = 1

    def handle_starttag(self, tag: str, attrs_list: list[tuple[str, str | None]]) -> None:
        attrs = {key: value or "" for key, value in attrs_list}
        href = attrs.get("href", "")
        page_match = _PAGE_QUERY.search(href)
        if page_match and "/following" in href:
            self.max_page = max(self.max_page, int(page_match.group(1)))
        if attrs.get("aria-current") == "page":
            self.current_page = 0

        if tag == "a":
            match = _USER_PATH.match(href)
            gtm_id = attrs.get("data-gtm-value", "")
            if match and (not gtm_id or gtm_id == match.group(1)):
                user_id = match.group(1)
                if gtm_id:
                    if user_id != self.current_user_id:
                        self.current_user_id = user_id
                        self.header_done = False
                        self.bio_text = []
                    if user_id not in self.items:
                        self.order.append(user_id)
                        self.items[user_id] = normalize_following_user(
                            {"userId": user_id}, self.visibility
                        ) or {}
                    self.anchor_user_id = user_id
                    self.anchor_text = []

        if tag == "img" and self.anchor_user_id:
            row = self.items.get(self.anchor_user_id, {})
            if attrs.get("src") and not row.get("avatar_url"):
                row["avatar_url"] = attrs["src"]
            if attrs.get("alt") and not row.get("display_name"):
                row["display_name"] = attrs["alt"].strip()

        button_user_id = attrs.get("data-gtm-user-id", "") if tag == "button" else ""
        if button_user_id and button_user_id == self.current_user_id:
            self._finish_bio()
            self.header_done = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self.anchor_user_id:
            text = _clean_text(" ".join(self.anchor_text))
            row = self.items.get(self.anchor_user_id, {})
            if text:
                row["display_name"] = text
            self.anchor_user_id = ""
            self.anchor_text = []

    def handle_data(self, data: str) -> None:
        text = _clean_text(data)
        if not text:
            return
        if self.current_page == 0 and text.isdigit():
            self.current_page = int(text)
            self.max_page = max(self.max_page, self.current_page)
        if self.anchor_user_id:
            self.anchor_text.append(text)
            return
        if self.current_user_id and not self.header_done:
            row = self.items.get(self.current_user_id, {})
            if row.get("display_name"):
                self.bio_text.append(text)

    def close(self) -> None:
        self._finish_bio()
        super().close()

    def _finish_bio(self) -> None:
        if not self.current_user_id or not self.bio_text:
            return
        row = self.items.get(self.current_user_id, {})
        if not row.get("bio"):
            row["bio"] = _clean_text("\n".join(self.bio_text))
        self.bio_text = []


def _clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def parse_following_html(source: str, visibility: str = "show") -> dict:
    source_text = str(source or "")
    parser = _FollowingHTMLParser("hide" if visibility == "hide" else "show")
    parser.feed(source_text)
    parser.close()
    items = [parser.items[user_id] for user_id in parser.order if parser.items.get(user_id)]
    count_match = re.search(
        r"(?:ユーザー|用户|Users?)</h2>.{0,1000}?<span[^>]*>([\d,]+)</span>",
        source_text,
        flags=re.I | re.S,
    )
    total = int(count_match.group(1).replace(",", "")) if count_match else None
    return {
        "items": items,
        "total": total,
        "current_page": max(1, parser.current_page),
        "max_page": max(1, parser.max_page),
        "has_more": parser.current_page < parser.max_page,
    }


def parse_following_payload(payload: object, visibility: str = "show") -> dict:
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        raise ValueError("Pixiv 关注接口未返回有效的 body")
    raw_users = body.get("users")
    if not isinstance(raw_users, list):
        raise ValueError("Pixiv 关注接口未返回 users 列表；Cookie 可能已失效")
    items = [row for row in (normalize_following_user(item, visibility) for item in raw_users) if row]
    total_value = body.get("total")
    try:
        total = max(0, int(total_value))
    except (TypeError, ValueError):
        total = None
    return {"items": items, "total": total}


def merge_following_users(existing: Iterable[dict], fetched: Iterable[dict], *, replace: bool) -> list[dict]:
    old_order: list[str] = []
    old_map: dict[str, dict] = {}
    for item in existing:
        row = normalize_following_user(item, str(item.get("visibility") or "show"))
        if not row:
            continue
        user_id = row["user_id"]
        if user_id not in old_map:
            old_order.append(user_id)
            old_map[user_id] = {**item, **{key: value for key, value in row.items() if value}}

    new_order: list[str] = []
    merged: dict[str, dict] = {} if replace else dict(old_map)
    for item in fetched:
        row = normalize_following_user(item, str(item.get("visibility") or "show"))
        if not row:
            continue
        user_id = row["user_id"]
        if user_id not in new_order:
            new_order.append(user_id)
        base = dict(merged.get(user_id, {}))
        base.update({key: value for key, value in row.items() if value or key in {"bio", "display_name"}})
        base["collected_at"] = str(item.get("collected_at") or base.get("collected_at") or "")
        merged[user_id] = base

    order = new_order if replace else [*new_order, *(user_id for user_id in old_order if user_id not in new_order)]
    return [merged[user_id] for user_id in order if user_id in merged]


def collect_following_pages(
    fetch_page: Callable[[int], dict],
    existing: Iterable[dict] = (),
    *,
    update_mode: str = "new",
    cancel_event: threading.Event | None = None,
    on_status: Callable[[str], None] | None = None,
    on_accounts: Callable[[dict], None] | None = None,
    known_overlap: int = 24,
    max_pages: int = 1000,
) -> dict:
    mode = "all" if update_mode == "all" else "new"
    cancel_event = cancel_event or threading.Event()
    existing_rows = list(existing)
    old_ids = {
        user_id
        for item in existing_rows
        if (user_id := str(item.get("user_id") or item.get("userId") or "").strip())
    }
    fetched: list[dict] = []
    seen: set[str] = set()
    page = 1
    complete = False
    stopped_reason = ""
    source = "ajax"
    total: int | None = None

    while page <= max_pages:
        if cancel_event.is_set():
            stopped_reason = "cancelled"
            break
        if on_status:
            on_status(
                f"正在请求 Pixiv 关注第 {page} 页；服务器尚未返回，"
                f"本次已取得 {len(fetched)} 个"
            )
        try:
            result = fetch_page(page)
        except Exception:
            if not fetched:
                raise
            stopped_reason = "request_error"
            break
        source = str(result.get("source") or source)
        page_items = result.get("items") if isinstance(result.get("items"), list) else []
        if result.get("total") is not None:
            try:
                total = max(0, int(result["total"]))
            except (TypeError, ValueError):
                pass
        for item in page_items:
            user_id = str(item.get("user_id") or "")
            if user_id and user_id not in seen:
                seen.add(user_id)
                fetched.append(item)
        if on_status:
            total_text = f" / 预计 {total} 个" if total is not None else ""
            on_status(
                f"Pixiv 关注第 {page} 页已返回 {len(page_items)} 个；"
                f"本次累计 {len(fetched)} 个{total_text}"
            )
        preview = merge_following_users(existing_rows, fetched, replace=False)
        if on_accounts:
            on_accounts(
                {
                    "items": preview,
                    "page": page,
                    "new_count": len(seen - old_ids),
                    "known_overlap": len(seen & old_ids),
                    "update_mode": mode,
                    "total": total,
                    "fetched_count": len(fetched),
                }
            )
        if total is not None and len(seen) >= total:
            complete = True
            break
        if mode == "new" and old_ids:
            required_overlap = min(max(1, int(known_overlap or 1)), len(old_ids))
            if len(seen & old_ids) >= required_overlap:
                stopped_reason = "known_overlap"
                if on_status:
                    on_status(
                        f"已确认 {required_overlap} 个旧关注画师重叠；更新新的完成，未继续读取旧分页"
                    )
                break
        if result.get("has_more") is False or not page_items:
            if total is not None and len(seen) < total:
                stopped_reason = "incomplete_total"
            elif total is None and source == "html":
                stopped_reason = "incomplete_total"
            elif total is None and not fetched and existing_rows:
                stopped_reason = "uncertain_empty"
            else:
                complete = True
            break
        page += 1
    else:
        stopped_reason = "page_limit"

    replace = mode == "all" and complete and not stopped_reason
    items = merge_following_users(existing_rows, fetched, replace=replace)
    removed_count = len(old_ids - seen) if replace else 0
    return {
        "version": 1,
        "platform": "pixiv",
        "count": len(items),
        "fetched_count": len(fetched),
        "new_count": len(seen - old_ids),
        "known_overlap": len(seen & old_ids),
        "removed_count": removed_count,
        "update_mode": mode,
        "complete": complete,
        "partial": not complete or bool(stopped_reason),
        "kept_previous": not fetched and bool(existing_rows),
        "stopped_reason": stopped_reason,
        "source": source,
        "total": total,
        "collected_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "items": items,
    }
