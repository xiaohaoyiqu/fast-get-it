from __future__ import annotations

import json
import re
import threading
from calendar import monthrange
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.blocklist import BlocklistStore

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.models import FileRecord, ProgressEvent
from software_app.crawlers.common import (
    convert_media_file,
    download_file,
    extension_from_url,
    normalized_media_extension,
    safe_component,
)


MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif",
    ".mp4", ".webm", ".mov", ".mkv", ".mp3", ".m4a", ".wav", ".zip", ".psd", ".clip",
}


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _restricted_session(source, cookie_names: set[str]) -> requests.Session:
    """Copy transport settings without sending Pixiv/FANBOX credentials to the wrong host."""
    if not isinstance(source, requests.Session):
        return source
    session = requests.Session()
    session.trust_env = bool(getattr(source, "trust_env", False))
    session.headers.update(dict(getattr(source, "headers", {}) or {}))
    session.proxies.update(dict(getattr(source, "proxies", {}) or {}))
    source_cookies = getattr(source, "cookies", None)
    if source_cookies is not None:
        values = source_cookies.get_dict()
        session.cookies.update({name: value for name, value in values.items() if name in cookie_names})
    return session


def _json_response(session, url: str, *, headers: dict | None = None) -> dict:
    response = session.get(url, headers=headers or {}, timeout=(10, 60))
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"接口未返回 JSON 对象：{url}")
    if payload.get("error") is True:
        raise RuntimeError(str(payload.get("message") or "远程接口返回错误"))
    return payload


def _walk_media_urls(value: object) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()

    def visit(node: object, key: str = "") -> None:
        if isinstance(node, dict):
            for child_key, child in node.items():
                visit(child, str(child_key))
        elif isinstance(node, list):
            for child in node:
                visit(child, key)
        elif isinstance(node, str) and node.startswith("https://"):
            suffix = Path(urlparse(node).path).suffix.lower()
            media_key = key.casefold() in {"originalurl", "downloadurl", "original"}
            media_host = urlparse(node).hostname or ""
            trusted_binary_host = media_host.endswith(("pximg.net", "fanbox.cc", "pixiv.net"))
            if (suffix in MEDIA_SUFFIXES or (media_key and trusted_binary_host)) and node.casefold() not in seen:
                seen.add(node.casefold())
                result.append(node)

    visit(value)
    return result


def _media_kind(url: str) -> tuple[str, str]:
    suffix = extension_from_url(url, ".bin")
    if suffix in {".mp4", ".webm", ".mov", ".mkv"}:
        return "video", suffix
    if suffix in {".mp3", ".m4a", ".wav"}:
        return "audio", suffix
    if suffix == ".gif":
        return "animation", suffix
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".avif"}:
        return "image", suffix
    return "attachment", suffix


class FanboxClient:
    def __init__(self, session) -> None:
        self.session = _restricted_session(session, {"FANBOXSESSID", "cf_clearance"})

    @staticmethod
    def parse_target(raw_target: str) -> tuple[str, str]:
        value = str(raw_target or "").strip()
        parsed = urlparse(value) if value.startswith(("http://", "https://")) else None
        if parsed:
            match = re.search(r"/posts/(\d+)", parsed.path)
            if match:
                return "post", match.group(1)
            match = re.search(r"/@([^/]+)", parsed.path)
            if match:
                return "creator", match.group(1)
        if value.isdigit():
            return "user", value
        value = value.lstrip("@").strip()
        if not value:
            raise ValueError("FANBOX 目标需要创作者 ID、Pixiv 用户 ID 或帖子链接")
        return "creator", value

    def _get(self, path: str, referer: str = "https://www.fanbox.cc/") -> dict:
        return _json_response(
            self.session,
            "https://api.fanbox.cc/" + path.lstrip("/"),
            headers={"Accept": "application/json", "Origin": "https://www.fanbox.cc", "Referer": referer},
        )

    def creator(self, kind: str, value: str) -> dict:
        key = "userId" if kind == "user" else "creatorId"
        payload = self._get(f"creator.get?{key}={value}")
        body = payload.get("body")
        if not isinstance(body, dict):
            raise ValueError("FANBOX 没有返回创作者资料；请检查 FANBOXSESSID 或目标")
        return body

    def supporting_status(self, today: date | None = None) -> dict:
        """Return active support count and the best renewal date information FANBOX exposes."""
        payload = self._get("plan.listSupporting")
        body = payload.get("body")
        plans = body.get("plans", []) if isinstance(body, dict) else body
        if not isinstance(plans, list):
            raise ValueError("FANBOX 订阅方案接口返回格式异常")
        current = today or datetime.now().astimezone().date()
        current_end = date(current.year, current.month, monthrange(current.year, current.month)[1])
        if current.month == 12:
            next_month = date(current.year + 1, 1, 1)
        else:
            next_month = date(current.year, current.month + 1, 1)
        renewal_window_end = date(next_month.year, next_month.month, min(5, monthrange(next_month.year, next_month.month)[1]))
        exact_dates: list[tuple[str, str]] = []
        rows: list[dict] = []
        for plan in plans:
            if not isinstance(plan, dict):
                continue
            exact, exact_kind = self._plan_date(plan)
            if exact:
                exact_dates.append((exact, exact_kind))
            rows.append(
                {
                    "creator_id": str(plan.get("creatorId") or plan.get("creator_id") or ""),
                    "plan_id": str(plan.get("id") or plan.get("planId") or ""),
                    "title": str(plan.get("title") or plan.get("planTitle") or ""),
                    "next_payment_date": exact,
                    "date_kind": exact_kind,
                }
            )
        active_count = len(rows)
        if not active_count:
            message = "当前没有订阅中的 FANBOX 方案，无需续费"
        elif exact_dates:
            exact_date, exact_kind = min(exact_dates, key=lambda item: item[0])
            date_label = "下次支付日期" if exact_kind == "next_payment" else "订阅到期日期"
            message = f"当前订阅 {active_count} 个方案；接口显示最近{date_label} {exact_date}"
        else:
            message = (
                f"当前订阅 {active_count} 个方案；接口未返回精确日期。"
                f"本期订阅至 {current_end.isoformat()}，自动扣款通常在 "
                f"{next_month.isoformat()}～{renewal_window_end.isoformat()}"
            )
        return {
            "active_count": active_count,
            "plans": rows,
            "exact_date": min(exact_dates, key=lambda item: item[0])[0] if exact_dates else "",
            "exact_date_kind": min(exact_dates, key=lambda item: item[0])[1] if exact_dates else "",
            "coverage_end": current_end.isoformat(),
            "renewal_window_start": next_month.isoformat(),
            "renewal_window_end": renewal_window_end.isoformat(),
            "date_source": "api" if exact_dates else "official_rule",
            "message": message,
        }

    @staticmethod
    def _plan_date(plan: dict) -> tuple[str, str]:
        kinds = {
            "nextPaymentDatetime": "next_payment", "nextPaymentDate": "next_payment",
            "next_payment_datetime": "next_payment", "next_payment_date": "next_payment",
            "expiredDatetime": "expiration", "expirationDate": "expiration",
            "expireDate": "expiration", "endDate": "expiration",
        }
        preferred = tuple(kinds)
        values: list[tuple[str, object]] = []

        def visit(node: object) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in preferred or ("next" in key.casefold() and any(word in key.casefold() for word in ("pay", "date", "time"))):
                        values.append((key, value))
                    if isinstance(value, (dict, list)):
                        visit(value)
            elif isinstance(node, list):
                for value in node:
                    visit(value)

        visit(plan)
        values.sort(key=lambda item: preferred.index(item[0]) if item[0] in preferred else len(preferred))
        for _key, value in values:
            match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
            if match:
                kind = kinds.get(_key, "next_payment" if "next" in _key.casefold() else "expiration")
                return match.group(0), kind
        return "", ""

    def posts(self, raw_target: str, limit: int = 100) -> list[dict]:
        kind, value = self.parse_target(raw_target)
        if kind == "post":
            payload = self._get(f"post.info?postId={value}", f"https://www.fanbox.cc/posts/{value}")
            body = payload.get("body")
            return [body] if isinstance(body, dict) else []
        creator = self.creator(kind, value)
        creator_id = str(creator.get("creatorId") or value)
        url = f"post.listCreator?creatorId={creator_id}&limit=10"
        rows: list[dict] = []
        while url and len(rows) < limit:
            payload = self._get(url, f"https://www.fanbox.cc/@{creator_id}") if not url.startswith("https://") else _json_response(
                self.session, url, headers={"Origin": "https://www.fanbox.cc", "Referer": f"https://www.fanbox.cc/@{creator_id}"}
            )
            body = payload.get("body")
            if not isinstance(body, dict):
                break
            items = body.get("items", [])
            if not isinstance(items, list) or not items:
                break
            rows.extend(item for item in items if isinstance(item, dict))
            url = str(body.get("nextUrl") or "")
        return rows[:limit]

    def candidates(self, raw_target: str, limit: int = 100) -> list[dict]:
        result: list[dict] = []
        for post in self.posts(raw_target, limit):
            post_id = str(post.get("id") or "")
            creator_id = str(post.get("creatorId") or "")
            if post_id:
                result.append({
                    "id": post_id,
                    "title": str(post.get("title") or post_id),
                    "author_name": creator_id,
                    "url": f"https://www.fanbox.cc/@{creator_id}/posts/{post_id}" if creator_id else f"https://www.fanbox.cc/posts/{post_id}",
                    "type": "FANBOX 帖子",
                    "input_kind": "fanbox",
                    "fee_required": _nonnegative_int(post.get("feeRequired")),
                    "restricted": bool(post.get("isRestricted")),
                })
        return result

    def download(self, raw_target: str, root: Path, task, callbacks, cancel_event: threading.Event) -> int:
        posts = self.posts(raw_target, max(1, min(int(task.options.get("max_works") or 20), 100)))
        if not posts:
            raise ValueError("FANBOX 没有返回可下载帖子；付费或登录内容需要有效 FANBOXSESSID")
        downloaded = 0
        restricted_posts = 0
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        blocklist = BlocklistStore(blocklist_path) if blocklist_path else None
        for post_index, summary in enumerate(posts, 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            post_id = str(summary.get("id") or "")
            payload = self._get(f"post.info?postId={post_id}", f"https://www.fanbox.cc/posts/{post_id}")
            post = payload.get("body") if isinstance(payload.get("body"), dict) else summary
            creator_id = str(post.get("creatorId") or "unknown")
            title = str(post.get("title") or post_id)
            if blocklist and (("fanbox", creator_id.casefold()) in blocklist.blocked_accounts() or
                              blocklist.is_blocked("pixiv", f"https://www.fanbox.cc/posts/{post_id}")):
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "warning",
                    f"跨平台黑名单跳过 FANBOX 创作者 {creator_id} 的帖子 {post_id}",
                ))
                continue
            if bool(post.get("isRestricted")):
                fee = _nonnegative_int(post.get("feeRequired") or summary.get("feeRequired"))
                restricted_posts += 1
                callbacks.on_progress(
                    ProgressEvent(
                        task.task_id,
                        task.module_id,
                        "warning",
                        f"FANBOX 帖子 {post_id} 属于付费内容；"
                        f"{'所需方案为每月 ' + str(fee) + ' JPY；' if fee else ''}请订阅或续费后重试",
                    )
                )
                continue
            folder = root / safe_component(f"FANBOX_{creator_id}") / safe_component(f"{title}_{post_id}")
            folder.mkdir(parents=True, exist_ok=True)
            metadata = folder / f"{post_id}.json"
            metadata.write_text(json.dumps(post, ensure_ascii=False, indent=2), encoding="utf-8")
            callbacks.on_file(FileRecord(
                path=metadata.resolve(), module_id=task.module_id, task_id=task.task_id, media_type="metadata",
                size=metadata.stat().st_size, title=title, source_url=f"https://www.fanbox.cc/posts/{post_id}",
                source_id=post_id, author_id=creator_id, author_name=creator_id, metadata={"pixiv_type": "fanbox"},
            ))
            downloaded += 1
            for media_index, url in enumerate(_walk_media_urls(post), 1):
                if cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                kind, suffix = _media_kind(url)
                final_suffix = normalized_media_extension("", suffix, kind, task.options) if kind != "attachment" else suffix
                destination = folder / f"{media_index:03d}{final_suffix}"
                if destination.is_file() and destination.stat().st_size:
                    continue
                source = destination if final_suffix == suffix else folder / f".{media_index:03d}.source{suffix}"
                try:
                    download_file(self.session, url, source, cancel_event)
                    if source != destination:
                        convert_media_file(source, destination, final_suffix)
                finally:
                    if source != destination:
                        source.unlink(missing_ok=True)
                callbacks.on_file(FileRecord(
                    path=destination.resolve(), module_id=task.module_id, task_id=task.task_id, media_type=kind,
                    size=destination.stat().st_size, title=title, source_url=url, source_id=post_id,
                    author_id=creator_id, author_name=creator_id, metadata={"pixiv_type": "fanbox"},
                ))
                downloaded += 1
        if not downloaded and restricted_posts:
            raise PermissionError(f"发现 {restricted_posts} 个需要订阅或续费后才能下载的 FANBOX 付费帖子")
        return downloaded


class SketchClient:
    def __init__(self, session) -> None:
        self.session = _restricted_session(session, set())

    @staticmethod
    def parse_target(raw_target: str) -> tuple[str, str]:
        value = str(raw_target or "").strip()
        if value.startswith(("http://", "https://")):
            parsed = urlparse(value)
            match = re.search(r"/items/(\d+)", parsed.path)
            if match:
                return "post", match.group(1)
            match = re.search(r"/@([^/]+)", parsed.path)
            if match:
                return "artist", match.group(1)
        if value.isdigit():
            return "post", value
        value = value.lstrip("@").strip()
        if not value:
            raise ValueError("Sketch 目标需要 @用户名、帖子 ID 或链接")
        return "artist", value

    def _get(self, url: str, referer: str) -> dict:
        return _json_response(
            self.session,
            url,
            headers={"Accept": "application/vnd.sketch-v4+json", "Referer": referer, "X-Requested-With": referer},
        )

    def posts(self, raw_target: str, limit: int = 100) -> list[dict]:
        kind, value = self.parse_target(raw_target)
        if kind == "post":
            payload = self._get(f"https://sketch.pixiv.net/api/replies/{value}.json", f"https://sketch.pixiv.net/items/{value}")
            item = ((payload.get("data") or {}).get("item")) if isinstance(payload.get("data"), dict) else None
            return [item] if isinstance(item, dict) else []
        url = f"https://sketch.pixiv.net/api/walls/@{value}/posts/public.json"
        referer = f"https://sketch.pixiv.net/@{value}"
        rows: list[dict] = []
        while url and len(rows) < limit:
            payload = self._get(url, referer)
            data = payload.get("data")
            if not isinstance(data, dict):
                break
            items = data.get("items", data.get("posts", []))
            if not isinstance(items, list) or not items:
                break
            rows.extend(item for item in items if isinstance(item, dict))
            next_path = str(data.get("next_page") or data.get("nextPage") or "")
            url = "https://sketch.pixiv.net" + next_path if next_path.startswith("/") else next_path
        return rows[:limit]

    def candidates(self, raw_target: str, limit: int = 100) -> list[dict]:
        result: list[dict] = []
        for item in self.posts(raw_target, limit):
            post_id = str(item.get("id") or "")
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            if post_id:
                result.append({
                    "id": post_id, "title": str(item.get("text") or user.get("name") or post_id),
                    "author_name": str(user.get("name") or user.get("unique_name") or ""),
                    "url": f"https://sketch.pixiv.net/items/{post_id}", "type": "Sketch 帖子", "input_kind": "sketch",
                })
        return result

    def download(self, raw_target: str, root: Path, task, callbacks, cancel_event: threading.Event) -> int:
        posts = self.posts(raw_target, max(1, min(int(task.options.get("max_works") or 20), 100)))
        if not posts:
            raise ValueError("Sketch 没有返回可下载帖子")
        downloaded = 0
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        blocklist = BlocklistStore(blocklist_path) if blocklist_path else None
        for item in posts:
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            post_id = str(item.get("id") or "")
            user = item.get("user") if isinstance(item.get("user"), dict) else {}
            author_id = str(user.get("unique_name") or user.get("id") or "unknown")
            title = str(item.get("text") or user.get("name") or post_id)
            if blocklist and (("sketch", author_id.casefold()) in blocklist.blocked_accounts() or
                              blocklist.is_blocked("pixiv", f"https://sketch.pixiv.net/items/{post_id}")):
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "warning",
                    f"跨平台黑名单跳过 Sketch 作者 {author_id} 的帖子 {post_id}",
                ))
                continue
            folder = root / safe_component(f"Sketch_{author_id}") / safe_component(f"{title}_{post_id}")
            for index, url in enumerate(_walk_media_urls(item), 1):
                kind, suffix = _media_kind(url)
                if kind not in {"image", "animation", "video"}:
                    continue
                final_suffix = normalized_media_extension("", suffix, kind, task.options)
                destination = folder / f"{index:03d}{final_suffix}"
                source = destination if final_suffix == suffix else folder / f".{index:03d}.source{suffix}"
                try:
                    download_file(self.session, url, source, cancel_event)
                    if source != destination:
                        convert_media_file(source, destination, final_suffix)
                finally:
                    if source != destination:
                        source.unlink(missing_ok=True)
                callbacks.on_file(FileRecord(
                    path=destination.resolve(), module_id=task.module_id, task_id=task.task_id, media_type=kind,
                    size=destination.stat().st_size, title=title, source_url=url, source_id=post_id,
                    author_id=author_id, author_name=str(user.get("name") or author_id), metadata={"pixiv_type": "sketch"},
                ))
                downloaded += 1
        if not downloaded:
            raise ValueError("Sketch 帖子没有发现可下载媒体")
        return downloaded


__all__ = ["FanboxClient", "SketchClient"]
