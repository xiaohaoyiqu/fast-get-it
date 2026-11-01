from __future__ import annotations

from software_app.core.task_control import task_cancelled
from software_app.core.collection_plan import ARTWORK_SEARCH_MODES, publish_collection_plan, restored_collection_ids
from software_app.crawlers.pixiv.collection_media import download_collection_media, retry_collection_media

import html
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

from software_app.core.adapter import TaskCancelled
from software_app.core.network_policy import HTTP_BACKOFF, record_http_backoff
from software_app.crawlers.common import request_get
from software_app.core.response_read import read_response_text
from software_app.core.file_conflict import reuse_existing, media_output
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview
from software_app.crawlers.common import (
    download_media_file,
    extension_from_url,
    make_session,
    normalized_media_extension,
    platform_output_root,
    request_json,
    safe_component,
    write_text_atomic,
)
from software_app.crawlers.pixiv.following import (
    FOLLOWING_PAGE_SIZE,
    normalize_following_user,
    parse_following_html,
    parse_following_payload,
)
from software_app.crawlers.pixiv.catalog import PixivCatalog, novel_row
from software_app.crawlers.pixiv.catalog_discovery import profile_ids
from software_app.crawlers.pixiv.external import FanboxClient, SketchClient
from software_app.crawlers.pixiv.ugoira import download_ugoira
from software_app.crawlers.pixiv.media import download_static_work, retry_static_work
from software_app.core.blocklist import BlocklistStore


PIXIV_BASE = "https://www.pixiv.net"
PIXIV_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


@dataclass(frozen=True)
class PixivTarget:
    kind: str
    value: str


def parse_pixiv_target(raw_target: str, input_kind: str = "") -> PixivTarget:
    value = str(raw_target or "").strip()
    if not value:
        raise ValueError("Pixiv 目标不能为空")
    requested_kind = str(input_kind or "").strip().lower()
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = str(parsed.hostname or "").casefold().rstrip(".")
        is_pixiv = host == "pixiv.net" or host.endswith(".pixiv.net")
        is_fanbox = host == "fanbox.cc" or host.endswith(".fanbox.cc")
        if not (is_pixiv or is_fanbox):
            raise ValueError("目标 URL 不是 Pixiv、FANBOX 或 Sketch 地址")
        if is_fanbox:
            return PixivTarget("fanbox", value)
        if host == "sketch.pixiv.net":
            return PixivTarget("sketch", value)
        path = parsed.path
        match = re.search(r"/(?:artworks|i)/(\d+)", path)
        if match:
            return PixivTarget("work", match.group(1))
        if "/novel/show.php" in path:
            novel_id = str(parse_qs(parsed.query).get("id", [""])[0])
            if novel_id.isdigit():
                return PixivTarget("novel", novel_id)
        match = re.search(r"/novel/series/(\d+)", path)
        if match:
            return PixivTarget("novel_series", match.group(1))
        match = re.search(r"/series/(\d+)", path)
        if match:
            return PixivTarget("manga_series", match.group(1))
        match = re.search(r"/users/(\d+)/bookmarks/(artworks|novels)", path)
        if match:
            return PixivTarget("novel_bookmark" if match.group(2) == "novels" else "bookmark", match.group(1))
        match = re.search(r"/users/(\d+)", path)
        if match:
            return PixivTarget("user_novels" if requested_kind == "user_novels" else "user", match.group(1))
        if is_pixiv and path.rstrip("/") == "/history.php":
            return PixivTarget("history", value)
        raise ValueError("不支持的 Pixiv URL 路径")
    forced = requested_kind
    forced_aliases = {
        "illust": "work", "artwork": "work", "artist": "user", "tag": "search",
        "series": "manga_series", "manga-series": "manga_series",
        "novel-series": "novel_series", "bookmarks": "bookmark",
        "novel-bookmarks": "novel_bookmark", "account-history": "history",
    }
    forced = forced_aliases.get(forced, forced)
    if forced in {"work", "user", "user_novels", "manga_series", "novel", "novel_series"}:
        return PixivTarget(forced, _numeric_id(value))
    if forced in {"search", "ranking", "new", "bookmark", "novel_bookmark", "history", "author_tag", "fanbox", "sketch"}:
        return PixivTarget(forced, value)
    if value.isdigit():
        return PixivTarget("work", value)
    return PixivTarget("search", value)


def _numeric_id(value: str) -> str:
    match = re.search(r"\d+", value)
    if not match:
        raise ValueError("目标中没有可用的 Pixiv ID")
    return match.group(0)


def parse_pixiv_profile_html(html_text: str, user_id: str) -> dict:
    """Extract stable fields from the server-rendered profile in 1.txt's shape."""
    source = str(html_text or "")
    avatar = html.unescape(
        _first_html_group(r'["\'](https?://i\.pximg\.net/user-profile/[^"\']+)["\']', source)
    )
    display_name = PixivCrawler._plain_html_text(
        _first_html_group(r'<h1\b[^>]*>([\s\S]*?)</h1>', source)
    )
    links: list[dict] = []
    seen: set[str] = set()
    for href in re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', source, flags=re.I):
        parsed = urlparse(html.unescape(href))
        if parsed.path != "/jump.php":
            continue
        target = unquote(str(parse_qs(parsed.query).get("url", [""])[0])).strip()
        if target.startswith(("http://", "https://")) and target.casefold() not in seen:
            seen.add(target.casefold())
            links.append({"label": urlparse(target).netloc or "个人网站", "url": target})
    return {
        "title": display_name or user_id,
        "display_name": display_name or user_id,
        "author_id": user_id,
        "avatar_url": avatar,
        "profile_url": f"{PIXIV_BASE}/users/{user_id}",
        "external_links": links,
        "links": links,
    }


def parse_pixiv_history_html(html_text: str, limit: int = 100) -> list[dict]:
    """Read artwork cards from the authenticated history page without generated CSS names."""
    rows: list[dict] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a\b[^>]*\bhref=["\'](?:https://www\.pixiv\.net)?/artworks/(\d+)[^"\']*["\'][^>]*>([\s\S]*?)</a>',
        flags=re.I,
    )
    for work_id, content in pattern.findall(str(html_text or "")):
        if work_id in seen:
            continue
        title = html.unescape(
            _first_html_group(r'(?:alt|title)=["\']([^"\']+)["\']', content)
        ).strip() or f"Pixiv 作品 {work_id}"
        image_url = html.unescape(
            _first_html_group(r'(?:src|data-src)=["\']([^"\']*i\.pximg\.net/[^"\']+)["\']', content)
        ).strip()
        seen.add(work_id)
        rows.append({
            "id": work_id,
            "title": title,
            "url": f"{PIXIV_BASE}/artworks/{work_id}",
            "type": "账号浏览历史",
            "source": "Pixiv 账号浏览历史",
            "input_kind": "work",
            "avatar_url": image_url,
        })
        if len(rows) >= max(1, min(int(limit), 10000)):
            break
    wanted = max(1, min(int(limit), 10000))
    if len(rows) < wanted:
        # Modern Pixiv pages may hydrate cards from embedded JSON instead of rendering anchors.
        for work_id in re.findall(r'(?:/|\\/)artworks(?:/|\\/)(\d+)', str(html_text or ""), flags=re.I):
            if work_id in seen:
                continue
            seen.add(work_id)
            rows.append({
                "id": work_id,
                "title": f"Pixiv 作品 {work_id}",
                "url": f"{PIXIV_BASE}/artworks/{work_id}",
                "type": "账号浏览历史",
                "source": "Pixiv 账号浏览历史",
                "input_kind": "work",
                "avatar_url": "",
            })
            if len(rows) >= wanted:
                break
    return rows


def _first_html_group(pattern: str, source: str) -> str:
    match = re.search(pattern, source, flags=re.I)
    return match.group(1) if match else ""


class PixivCrawler:
    def __init__(self, cookie_file: Path | str | None = None, proxy_url: str = "") -> None:
        self.session = make_session(referer=PIXIV_BASE + "/", cookie_file=cookie_file, proxy_url=proxy_url,
                                    cookie_domains={"PHPSESSID": ".pixiv.net", "FANBOXSESSID": ".fanbox.cc"})
        # Pixiv has an explicit app-level proxy setting. Ignoring ambient HTTP(S)_PROXY
        # makes "blank proxy" a real direct connection instead of a hidden system proxy.
        self.session.trust_env = False

    def preview(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        options = options or {}
        target = parse_pixiv_target(raw_target, str(options.get("input_kind") or ""))
        if not bool(options.get("live", False)):
            return TargetPreview(
                module_id="pixiv",
                raw_target=raw_target,
                normalized_target=self.target_url(target),
                title=f"Pixiv {target.kind}: {target.value}",
                description="已识别目标；启用在线预览后获取标题、作者、标签和页面快照。",
                metadata={"input_kind": target.kind, "target_id": target.value},
            )
        data = self.fetch_preview(target, options)
        return TargetPreview(
            module_id="pixiv",
            raw_target=raw_target,
            normalized_target=self.target_url(target),
            title=str(data.get("title") or target.value),
            description=str(data.get("description") or "Pixiv 在线预览"),
            metadata={**data, "input_kind": target.kind, "target_id": target.value},
        )

    def target_url(self, target: PixivTarget) -> str:
        if target.kind == "work":
            return f"{PIXIV_BASE}/artworks/{target.value}"
        if target.kind in {"user", "user_novels"}:
            return f"{PIXIV_BASE}/users/{target.value}"
        if target.kind == "manga_series":
            return f"{PIXIV_BASE}/series/{target.value}"
        if target.kind == "novel":
            return f"{PIXIV_BASE}/novel/show.php?id={target.value}"
        if target.kind == "novel_series":
            return f"{PIXIV_BASE}/novel/series/{target.value}"
        if target.kind == "fanbox":
            return target.value if target.value.startswith(("http://", "https://")) else f"https://www.fanbox.cc/@{target.value.lstrip('@')}"
        if target.kind == "sketch":
            return target.value if target.value.startswith(("http://", "https://")) else f"https://sketch.pixiv.net/@{target.value.lstrip('@')}"
        if target.kind == "ranking":
            return f"{PIXIV_BASE}/ranking.php"
        if target.kind == "new":
            return f"{PIXIV_BASE}/new_illust.php"
        if target.kind == "bookmark":
            match = re.search(r"\d+", target.value)
            return f"{PIXIV_BASE}/users/{match.group(0)}/bookmarks/artworks" if match else f"{PIXIV_BASE}/bookmark_new_illust.php"
        if target.kind == "novel_bookmark":
            match = re.search(r"\d+", target.value)
            return f"{PIXIV_BASE}/users/{match.group(0)}/bookmarks/novels" if match else f"{PIXIV_BASE}/bookmark_new_novel.php"
        if target.kind == "history":
            return f"{PIXIV_BASE}/history.php"
        return f"{PIXIV_BASE}/tags/{quote(target.value)}/artworks"

    def fetch_preview(self, target: PixivTarget, options: dict | None = None) -> dict:
        options = options or {}
        if target.kind == "work":
            body = self._body(f"/ajax/illust/{target.value}")
            return self._work_summary(body)
        if target.kind in {"user", "user_novels"}:
            return self._user_profile(target.value)
        if target.kind == "history":
            rows = self.account_history(int(options.get("history_limit") or 100))
            return {
                "title": "Pixiv 账号浏览历史",
                "description": f"已读取 {len(rows)} 个最近浏览作品",
                "search_results": rows,
            }
        if target.kind == "novel":
            body = self._body(f"/ajax/novel/{target.value}?lang=zh")
            return self._novel_summary(body, target.value)
        if target.kind == "manga_series":
            rows = self._catalog().manga_series(target.value, 20)
            return {
                "title": f"Pixiv 漫画系列 {target.value}",
                "description": f"已读取 {len(rows)} 个系列作品候选",
                "search_results": rows,
            }
        if target.kind == "novel_series":
            info = self._body(f"/ajax/novel/series/{target.value}?lang=zh")
            rows = self._catalog().novel_series(target.value, 20)
            return {
                "title": str(info.get("title") or f"Pixiv 小说系列 {target.value}") if isinstance(info, dict) else f"Pixiv 小说系列 {target.value}",
                "description": f"已读取 {len(rows)} 篇系列小说",
                "search_results": rows,
                "author_id": str(info.get("userId") or "") if isinstance(info, dict) else "",
                "author_name": str(info.get("userName") or "") if isinstance(info, dict) else "",
            }
        if target.kind == "fanbox":
            with FanboxClient(self.session) as client:
                rows = client.candidates(target.value, 20)
            return {"title": f"FANBOX：{target.value}", "description": f"已读取 {len(rows)} 个帖子", "search_results": rows}
        if target.kind == "sketch":
            with SketchClient(self.session) as client:
                rows = client.candidates(target.value, 20)
            return {"title": f"Pixiv Sketch：{target.value}", "description": f"已读取 {len(rows)} 个帖子", "search_results": rows}
        results = self.search(
            target.value,
            limit=20,
            search_mode=str(options.get("search_mode") or ""),
            options=options,
        )
        items = "".join(
            f'<li><a href="{html.escape(str(item.get("url") or ""), quote=True)}">'
            f'{html.escape(str(item.get("title") or item.get("name") or item.get("id") or ""))}</a>'
            f' — {html.escape(str(item.get("author_name") or item.get("display_name") or ""))}</li>'
            for item in results
        )
        return {
            "title": f"Pixiv 搜索：{target.value}",
            "description": f"找到 {len(results)} 个候选",
            "search_results": results,
            "page_html": f"<h2>搜索结果</h2><ol>{items}</ol>",
        }

    def _catalog(self):
        catalog = PixivCatalog(self._body, self._json)
        self.last_catalog_status = catalog.last_discovery_status
        self.last_catalog_rows = catalog.last_discovery_rows
        return catalog

    def _report_catalog_discovery(self, task, callbacks):
        status = getattr(self, "last_catalog_status", {})
        if status.get("reason") not in {"exhausted", "limit_reached"}:
            return
        ending = "已读到本次列表末尾" if status["complete"] else "达到本次数量上限，后续作品未读取"
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info",
            f"作品列表读取 {status['pages']} 页、选定 {status['selected']} 项：{ending}",
            metadata={"catalog_discovery": dict(status)}))

    def search(self, keyword: str, limit: int = 20, search_mode: str = "", options: dict | None = None) -> list[dict]:
        options = options or {}
        upper_limit = 10000 if search_mode in {"浏览历史（Premium）", "作品收藏", "小说收藏"} else 100
        requested_limit = max(1, min(int(limit), upper_limit))
        catalog = self._catalog()
        if search_mode == "漫画系列":
            return catalog.manga_series(_numeric_id(keyword), requested_limit)
        if search_mode == "小说 ID":
            return [novel_row({"id": _numeric_id(keyword), "title": f"Pixiv 小说 {_numeric_id(keyword)}"})]
        if search_mode == "小说搜索":
            return catalog.novel_search(keyword, requested_limit)
        if search_mode == "小说系列":
            return catalog.novel_series(_numeric_id(keyword), requested_limit)
        if search_mode == "作者搜索":
            return self.search_users(keyword, requested_limit, str(options.get("author_match_mode") or "partial"))
        if search_mode == "排行榜":
            return catalog.ranking(keyword, requested_limit)
        if search_mode == "新作":
            return catalog.new_works(keyword, requested_limit)
        if search_mode in {"作品收藏", "小说收藏"}:
            self.last_bookmark_status, self.last_bookmark_rows = {}, []
            user_id = _numeric_id(keyword) if re.search(r"\d+", keyword) else str(options.get("account_id") or "")
            if not user_id:
                raise ValueError("收藏读取需要用户 ID 或当前 Pixiv 账号 ID")
            fetch = catalog.novel_bookmarks if search_mode == "小说收藏" else catalog.bookmarks
            try:
                return fetch(
                    user_id, limit=requested_limit,
                    visibility=str(options.get("pixiv_visibility") or "show"),
                    tag=str(options.get("bookmark_tag") or ""),
                    start_date=str(options.get("start_date") or ""),
                    end_date=str(options.get("end_date") or ""),
                    date_basis=str(options.get("bookmark_date_basis") or "bookmarked"),
                )
            finally:
                self.last_bookmark_status = catalog.last_bookmark_status
                self.last_bookmark_rows = catalog.last_bookmark_rows
        if search_mode == "浏览历史（Premium）":
            return self.account_history(requested_limit)
        if search_mode == "作者 + 标签":
            return catalog.author_tag(keyword, requested_limit)
        if search_mode == "FANBOX":
            with FanboxClient(self.session) as client:
                return client.candidates(keyword, requested_limit)
        if search_mode == "Sketch":
            with SketchClient(self.session) as client:
                return client.candidates(keyword, requested_limit)
        return catalog.artwork_search(keyword, requested_limit, search_mode, options)

    def _user_profile(self, user_id: str) -> dict:
        body: dict = {}
        api_error: Exception | None = None
        try:
            value = self._body(f"/ajax/user/{user_id}?full=1")
            body = value if isinstance(value, dict) else {}
        except TaskCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the rendered profile is an intentional fallback.
            api_error = exc
        description_html = str(body.get("commentHtml") or body.get("comment") or "")
        external_links = self._profile_links(body)
        result = {
            "title": self._decode_html_entities(body.get("name") or user_id),
            "display_name": self._decode_html_entities(body.get("name") or user_id),
            "description": self._plain_html_text(description_html) or "Pixiv 作者",
            "bio": self._plain_html_text(description_html),
            "author_id": user_id,
            "avatar_url": self._profile_avatar_url(body),
            "profile_url": f"{PIXIV_BASE}/users/{user_id}",
            "external_links": external_links,
            "links": external_links,
            "page_html": description_html,
        }
        needs_html = not result["avatar_url"] or not external_links or not body.get("name")
        if needs_html:
            try:
                text, _ = self._text(f"{PIXIV_BASE}/users/{user_id}", timeout=(5, 20))
                fallback = parse_pixiv_profile_html(text, user_id)
            except TaskCancelled:
                raise
            except Exception as html_error:
                if not body:
                    raise RuntimeError(
                        f"Pixiv 作者资料接口和主页都读取失败；接口：{api_error or '无有效数据'}；主页：{html_error}"
                    ) from html_error
            else:
                for key in ("title", "display_name", "avatar_url", "profile_url"):
                    if not result.get(key) or (key in {"title", "display_name"} and result.get(key) == user_id):
                        result[key] = fallback.get(key) or result.get(key)
                merged = self._merge_links(external_links, fallback.get("external_links") or [])
                result["external_links"] = merged
                result["links"] = merged
        return result

    def search_users(self, keyword: str, limit: int = 20, match_mode: str = "partial") -> list[dict]:
        query = str(keyword or "").strip()
        if not query:
            raise ValueError("Pixiv 作者搜索需要昵称或作者 ID")
        if query.isdigit():
            profile = self._user_profile(query)
            return [{
                "id": query,
                "title": str(profile.get("display_name") or query),
                "author_id": query,
                "author_name": str(profile.get("display_name") or query),
                "url": f"{PIXIV_BASE}/users/{query}",
                "avatar_url": str(profile.get("avatar_url") or ""),
                "bio": str(profile.get("bio") or ""),
                "type": "作者",
                "source": "Pixiv 作者 ID",
                "input_kind": "user",
            }]
        wanted = max(1, min(int(limit), 100))
        s_mode = "s_usr_full" if str(match_mode).lower() in {"exact", "full", "s_usr_full"} else "s_usr"
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 11):
            body = self._body(
                "/ajax/search/users?" + urlencode({"nick": query, "s_mode": s_mode, "i": 1, "p": page, "lang": "zh"})
            )
            candidates: list[dict] = []

            def visit(node: object, candidates: list[dict]) -> None:
                if isinstance(node, dict):
                    if any(key in node for key in ("userId", "user_id")):
                        candidates.append(node)
                    for value in node.values():
                        if isinstance(value, (dict, list)):
                            visit(value, candidates)
                elif isinstance(node, list):
                    for value in node:
                        visit(value, candidates)

            visit(body, candidates)
            before = len(rows)
            for item in candidates:
                normalized = normalize_following_user(item)
                if not normalized:
                    continue
                user_id = normalized["user_id"]
                if user_id in seen:
                    continue
                seen.add(user_id)
                rows.append({
                    "id": user_id,
                    "title": normalized["display_name"] or user_id,
                    "author_id": user_id,
                    "author_name": normalized["display_name"] or user_id,
                    "url": normalized["profile_url"],
                    "avatar_url": normalized["avatar_url"],
                    "bio": normalized["bio"],
                    "type": "作者",
                    "source": "Pixiv 作者搜索",
                    "input_kind": "user",
                })
                if len(rows) >= wanted:
                    return rows
            if len(rows) == before:
                break
        return rows

    def account_history(self, limit: int = 100) -> list[dict]:
        page_text, final_url = self._text(f"{PIXIV_BASE}/history.php", timeout=(5, 25))
        rows = parse_pixiv_history_html(page_text, limit)
        if rows:
            return rows
        text = page_text.casefold()
        if "login" in final_url.casefold() or "ログイン" in text or "登录" in text:
            raise PermissionError("Pixiv 浏览历史需要有效 PHPSESSID")
        raise PermissionError("没有读取到 Pixiv 浏览历史；该功能需要 Pixiv Premium，且只包含作品详情页浏览记录")

    def fetch_following_page(
        self,
        user_id: str,
        page: int = 1,
        visibility: str = "show",
        page_size: int = FOLLOWING_PAGE_SIZE,
    ) -> dict:
        owner_id = _numeric_id(user_id)
        page = max(1, int(page or 1))
        rest = "hide" if visibility == "hide" else "show"
        limit = max(1, min(int(page_size or FOLLOWING_PAGE_SIZE), FOLLOWING_PAGE_SIZE))
        offset = (page - 1) * limit
        api_url = (
            f"{PIXIV_BASE}/ajax/user/{owner_id}/following"
            f"?offset={offset}&limit={limit}&rest={rest}&lang=zh"
        )
        try:
            payload = request_json(self.session, api_url)
            parsed = parse_following_payload(payload, rest)
            parsed.update(
                {
                    "page": page,
                    "page_size": limit,
                    "source": "ajax",
                    "has_more": (
                        offset + len(parsed["items"]) < parsed["total"]
                        if parsed.get("total") is not None
                        else len(parsed["items"]) >= limit
                    ),
                }
            )
            return parsed
        except TaskCancelled:
            raise
        except Exception as api_error:  # The public HTML remains useful when the AJAX login expires.
            if rest == "hide":
                raise RuntimeError(f"读取 Pixiv 非公开关注失败，请检查 cookies.json：{api_error}") from api_error
            try:
                return self.fetch_following_html_page(owner_id, page=page, visibility=rest)
            except TaskCancelled:
                raise
            except Exception as html_error:
                raise RuntimeError(
                    f"Pixiv 关注接口和分页网页均读取失败；请检查 cookies.json。接口：{api_error}；网页：{html_error}"
                ) from html_error

    def fetch_following_html_page(self, user_id: str, page: int = 1, visibility: str = "show") -> dict:
        owner_id = _numeric_id(user_id)
        page = max(1, int(page or 1))
        rest = "hide" if visibility == "hide" else "show"
        query = []
        if page > 1:
            query.append(f"p={page}")
        if rest == "hide":
            query.append("rest=hide")
        url = f"{PIXIV_BASE}/users/{owner_id}/following"
        if query:
            url += "?" + "&".join(query)
        text, final_url = self._text(url, timeout=(10, 45))
        if f"/users/{owner_id}/following" not in final_url and f"/users/{owner_id}/following" not in text:
            raise RuntimeError("Pixiv 将关注页重定向到了登录页")
        parsed = parse_following_html(text, rest)
        parsed.update({"page": page, "source": "html"})
        return parsed

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        target = parse_pixiv_target(task.target, str(task.options.get("input_kind") or ""))
        self.last_catalog_status, self.last_catalog_rows = {}, []
        owner_id = None
        if target.kind in {"bookmark", "manga_series"}:
            owner_id = (_numeric_id(target.value) if re.search(r"\d+", target.value)
                        else str(task.options.get("account_id") or ""))
        elif target.kind == "ranking" or (target.kind == "search" and task.options.get("search_mode", "") in ARTWORK_SEARCH_MODES):
            owner_id = hashlib.sha256(target.value.encode("utf-8")).hexdigest()
        if "_retry_media" in task.options:
            if owner_id is not None:
                ids = restored_collection_ids(task, target.kind, owner_id)
                if ids is None:
                    raise ValueError("缺少原集合范围记录，未重新发现其他作品")
                retry_collection_media(self, task, ids, callbacks, cancel_event)
                return
            if target.kind != "work":
                raise ValueError("Pixiv 逐媒体补下目前支持单作品静态图片、作品收藏、漫画系列、作品搜索和排行榜")
            retry_static_work(self, task, target.value, callbacks, cancel_event)
            return
        if target.kind in {"novel", "novel_series", "user_novels", "novel_bookmark"}:
            self._download_novel_target(task, target, callbacks, cancel_event)
            return
        if target.kind in {"fanbox", "sketch"}:
            root = platform_output_root(task.output_dir, "pixiv")
            if target.kind == "fanbox":
                with FanboxClient(self.session) as client:
                    downloaded = client.download(target.value, root, task, callbacks, cancel_event)
            else:
                with SketchClient(self.session) as client:
                    downloaded = client.download(target.value, root, task, callbacks, cancel_event)
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id, task.module_id, "info", f"{target.kind} 完成：下载 {downloaded} 个文件",
                    current=downloaded, total=downloaded, percent=100.0, metadata={"downloaded": downloaded},
                )
            )
            return
        work_ids = None
        if owner_id is not None:
            work_ids = restored_collection_ids(task, target.kind, owner_id)
        if work_ids is None:
            work_ids = self._work_ids(
                target, int(task.options.get("max_works") or 20),
                str(task.options.get("search_mode") or ""), task.options,
            )
            if owner_id is not None and work_ids:
                publish_collection_plan(task, target.kind, owner_id, work_ids, callbacks)
        if owner_id is not None:
            if not work_ids:
                raise ValueError("Pixiv 没有找到可下载的集合作品")
            self._report_catalog_discovery(task, callbacks)
            download_collection_media(self, task, work_ids, callbacks, cancel_event)
            return
        self._report_catalog_discovery(task, callbacks)
        root = platform_output_root(task.output_dir, "pixiv")
        downloaded = 0
        skipped_ai = 0
        skipped_blocked = 0
        reused = 0
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        blocklist = BlocklistStore(blocklist_path) if blocklist_path else None
        if not work_ids:
            raise ValueError("Pixiv 没有找到可下载的作品")
        callbacks.on_progress(
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"Pixiv 本次范围包含 {len(work_ids)} 个作品",
                current=0,
                total=len(work_ids),
                percent=0.0,
            )
        )
        for work_index, work_id in enumerate(work_ids, 1):
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            detail = self._body(f"/ajax/illust/{work_id}")
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            summary = self._work_summary(detail)
            if blocklist and blocklist.is_blocked("pixiv", f"https://www.pixiv.net/artworks/{work_id}", author_id=str(summary.get("author_id") or "")):
                skipped_blocked += 1
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "warning", f"跨平台黑名单跳过作者作品 {work_id}",
                    current=work_index, total=len(work_ids), percent=work_index / len(work_ids) * 100.0,
                ))
                continue
            if bool(task.options.get("filter_ai", True)) and self._is_ai(detail):
                skipped_ai += 1
                callbacks.on_progress(
                    ProgressEvent(
                        task.task_id,
                        task.module_id,
                        "warning",
                        f"跳过 AI 作品 {work_id}",
                        current=work_index,
                        total=len(work_ids),
                        percent=work_index / len(work_ids) * 100.0,
                    )
                )
                continue
            author_dir = safe_component(f'{summary.get("author_name") or "unknown"}_{summary.get("author_id") or "0"}')
            work_dir = root / author_dir / safe_component(f'{summary.get("title") or work_id}_{work_id}')
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    f"[{work_index}/{len(work_ids)}] 下载 {summary.get('title') or work_id}",
                    current=work_index - 1,
                    total=len(work_ids),
                    percent=(work_index - 1) / len(work_ids) * 100.0,
                )
            )
            if target.kind == "work" and int(detail.get("illustType") or 0) != 2:
                download_static_work(self, task, work_id, summary, work_dir, callbacks, cancel_event)
                return
            if int(detail.get("illustType") or 0) == 2:
                meta = self._body(f"/ajax/illust/{work_id}/ugoira_meta")
                for destination, media_type, source_url in download_ugoira(
                    self.session,
                    meta,
                    work_dir,
                    work_id,
                    task.options,
                    cancel_event,
                ):
                    downloaded += 1
                    callbacks.on_file(
                        FileRecord(
                            path=destination.resolve(),
                            module_id=task.module_id,
                            task_id=task.task_id,
                            media_type=media_type,
                            size=destination.stat().st_size,
                            title=str(summary.get("title") or destination.name),
                            source_url=source_url,
                            source_id=work_id,
                            author_id=str(summary.get("author_id") or ""),
                            author_name=str(summary.get("author_name") or ""),
                            published_at=str(summary.get("published_at") or ""),
                            tags=tuple(str(item) for item in summary.get("tags") or []),
                            metadata={
                                "work_title": str(summary.get("title") or ""),
                                "ai_type": summary.get("ai_type", 0),
                                "pixiv_type": "ugoira",
                            },
                        )
                    )
                pages = []
            else:
                pages = self._body(f"/ajax/illust/{work_id}/pages")
                if task_cancelled(cancel_event):
                    raise TaskCancelled("任务已取消")
                if not isinstance(pages, list):
                    raise ValueError(f"Pixiv 作品 {work_id} 页数据格式异常")
                if not pages:
                    raise ValueError(f"Pixiv 作品 {work_id} 未返回任何图片页，不能标记为下载完成")
            available_pages = 0
            for page_index, page in enumerate(pages, 1):
                if task_cancelled(cancel_event):
                    raise TaskCancelled("任务已取消")
                original = ((page or {}).get("urls") or {}).get("original") if isinstance(page, dict) else ""
                if not original:
                    continue
                available_pages += 1
                suffix = extension_from_url(original, ".jpg", PIXIV_IMAGE_EXTENSIONS)
                final_suffix = normalized_media_extension("image/gif" if suffix == ".gif" else "image/*", suffix, "image", task.options)
                destination = work_dir / f"{page_index:03d}{final_suffix}"
                source_path = destination if final_suffix == suffix else work_dir / f".{page_index:03d}.source{suffix}"
                result = download_media_file(
                    self.session, original, source_path, destination, cancel_event, final_suffix,
                    conflict_policy=task.options.get("file_conflict", "reuse"),
                    public_referer="https://www.pixiv.net/",
                )
                size, was_reused = result
                destination = result.path
                reused += int(was_reused)
                downloaded += int(not was_reused)
                callbacks.on_file(
                    FileRecord(
                        path=destination.resolve(),
                        module_id=task.module_id,
                        task_id=task.task_id,
                        media_type="image",
                        size=size,
                        title=str(summary.get("title") or destination.name),
                        source_url=original,
                        source_id=work_id,
                        author_id=str(summary.get("author_id") or ""),
                        author_name=str(summary.get("author_name") or ""),
                        chapter=str(page_index),
                        published_at=str(summary.get("published_at") or ""),
                        tags=tuple(str(item) for item in summary.get("tags") or []),
                        metadata={
                            "page": page_index,
                            "work_title": str(summary.get("title") or ""),
                            "ai_type": summary.get("ai_type", 0),
                        },
                    )
                )
            if pages and available_pages == 0:
                raise ValueError(f"Pixiv 作品 {work_id} 的图片页均无原图地址，不能标记为下载完成")
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id,
                    task.module_id,
                    "info",
                    f"[{work_index}/{len(work_ids)}] 已完成 {summary.get('title') or work_id}",
                    current=work_index,
                    total=len(work_ids),
                    percent=work_index / len(work_ids) * 100.0,
                )
            )
        callbacks.on_progress(
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"Pixiv 完成：下载 {downloaded} 个文件，已有 {reused} 个，跳过 AI 作品 {skipped_ai} 个，黑名单 {skipped_blocked} 个",
                current=len(work_ids),
                total=len(work_ids),
                percent=100.0,
                metadata={"downloaded": downloaded, "reused": reused, "skipped_ai": skipped_ai, "skipped_blocked": skipped_blocked},
            )
        )

    def _work_ids(self, target: PixivTarget, limit: int, search_mode: str = "", options: dict | None = None) -> list[str]:
        options = options or {}
        limit = max(1, min(limit, 100))
        if target.kind == "work":
            return [target.value]
        if target.kind == "search":
            return [item["id"] for item in self.search(target.value, limit=limit, search_mode=search_mode, options=options)]
        if target.kind == "manga_series":
            return [item["id"] for item in self._catalog().manga_series(target.value, limit)]
        if target.kind in {"ranking", "new", "bookmark", "history", "author_tag"}:
            modes = {
                "ranking": "排行榜",
                "new": "新作",
                "bookmark": "作品收藏",
                "history": "浏览历史（Premium）",
                "author_tag": "作者 + 标签",
            }
            rows = self.search(target.value, limit=limit, search_mode=modes[target.kind], options=options)
            if target.kind == "bookmark":
                from .bookmark_discovery import require_downloadable_discovery

                require_downloadable_discovery(self.last_bookmark_status)
            return [item["id"] for item in rows]
        body = self._body(f"/ajax/user/{target.value}/profile/all")
        return profile_ids(body, ("illusts", "manga"), limit)

    def _download_novel_target(
        self,
        task: DownloadTask,
        target: PixivTarget,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> None:
        limit = max(1, min(int(task.options.get("max_works") or 20), 100))
        if target.kind == "novel":
            novel_ids = [target.value]
            series_id = ""
        elif target.kind == "user_novels":
            profile = self._body(f"/ajax/user/{target.value}/profile/all")
            novel_ids = profile_ids(profile, ("novels",), limit)
            series_id = ""
        elif target.kind == "novel_bookmark":
            owner_id = _numeric_id(target.value) if re.search(r"\d+", target.value) else str(task.options.get("account_id") or "")
            if not owner_id:
                raise ValueError("小说收藏需要 Pixiv 账号 ID 或包含账号 ID 的目标")
            novel_ids = restored_collection_ids(task, target.kind, owner_id)
            if novel_ids is None:
                catalog = self._catalog()
                novel_ids = [
                    str(row["id"])
                    for row in catalog.novel_bookmarks(
                        owner_id, limit=limit,
                        visibility=str(task.options.get("pixiv_visibility") or "show"),
                        tag=str(task.options.get("bookmark_tag") or ""),
                        start_date=str(task.options.get("start_date") or ""),
                        end_date=str(task.options.get("end_date") or ""),
                        date_basis=str(task.options.get("bookmark_date_basis") or "bookmarked"),
                    )
                ]
                from .bookmark_discovery import require_downloadable_discovery

                require_downloadable_discovery(catalog.last_bookmark_status)
                if novel_ids:
                    publish_collection_plan(task, target.kind, owner_id, novel_ids, callbacks)
            series_id = ""
        else:
            series_id = target.value
            novel_ids = [
                str(row["id"])
                for row in self._catalog().novel_series(target.value, limit)
            ]
        if not novel_ids:
            raise ValueError("Pixiv 小说目标没有可下载内容")
        self._report_catalog_discovery(task, callbacks)
        root = platform_output_root(task.output_dir, "pixiv")
        downloaded = 0
        skipped_ai = 0
        skipped_blocked = 0
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        blocklist = BlocklistStore(blocklist_path) if blocklist_path else None
        for index, novel_id in enumerate(novel_ids, 1):
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            body = self._body(f"/ajax/novel/{novel_id}?lang=zh")
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            summary = self._novel_summary(body, novel_id)
            source_url = self.target_url(PixivTarget("novel", novel_id))
            reason = ""
            if blocklist and blocklist.is_blocked("pixiv", source_url, author_id=summary["author_id"]):
                skipped_blocked += 1
                reason = "跨平台黑名单"
            elif bool(task.options.get("filter_ai", True)) and self._is_ai(body):
                skipped_ai += 1
                reason = "AI 作品"
            if reason:
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "warning", f"跳过小说 {novel_id}：{reason}",
                    current=index, total=len(novel_ids), percent=index / len(novel_ids) * 100.0,
                    metadata={"source_id": novel_id, "source_url": source_url, "skip_reason": reason},
                ))
                continue
            author_dir = safe_component(f'{summary.get("author_name") or "unknown"}_{summary.get("author_id") or "0"}')
            folder_label = f'{summary.get("title") or novel_id}_{novel_id}'
            if series_id:
                folder_label = f'小说系列_{series_id}/{folder_label}'
            novel_dir = root / author_dir
            for component in folder_label.split("/"):
                novel_dir /= safe_component(component)
            destination = novel_dir / f"{safe_component(summary.get('title') or novel_id)}_{novel_id}.html"
            text_path = destination.with_suffix(".txt")
            metadata_path = destination.with_suffix(".json")
            with media_output(destination, task.options.get("file_conflict", "reuse"), cancel_event,
                              companions=lambda path: (path.with_suffix(".txt"), path.with_suffix(".json"))) as destination:
                text_path = destination.with_suffix(".txt")
                metadata_path = destination.with_suffix(".json")
                policy = task.options.get("file_conflict", "reuse")
                reuse_html = reuse_existing(destination, policy)
                reuse_text = reuse_existing(text_path, policy)
                destination.parent.mkdir(parents=True, exist_ok=True)
                content = str(body.get("content") or "") if isinstance(body, dict) else ""
                document = (
                    "<!doctype html><html lang=\"zh\"><meta charset=\"utf-8\">"
                    f"<title>{html.escape(str(summary.get('title') or novel_id))}</title>"
                    f"<h1>{html.escape(str(summary.get('title') or novel_id))}</h1>"
                    f"<p>作者：{html.escape(str(summary.get('author_name') or summary.get('author_id') or ''))}</p>"
                    f"<pre style=\"white-space:pre-wrap\">{html.escape(content)}</pre></html>"
                )
                if not reuse_html:
                    write_text_atomic(destination, document)
                text_path = destination.with_suffix(".txt")
                plain_content = self._decode_html_entities(content)
                text_document = "\n".join(
                    part
                    for part in (
                        str(summary.get("title") or novel_id),
                        f"作者：{summary.get('author_name') or summary.get('author_id') or '-'}",
                        f"发布日期：{summary.get('published_at') or '-'}",
                        "标签：" + "、".join(str(tag) for tag in summary.get("tags") or [])
                        if summary.get("tags")
                        else "标签：-",
                        f"来源：{self.target_url(PixivTarget('novel', novel_id))}",
                        "",
                        plain_content,
                    )
                )
                if not reuse_text:
                    write_text_atomic(text_path, text_document, encoding="utf-8-sig")
                metadata_path = destination.with_suffix(".json")
                write_text_atomic(metadata_path, json.dumps(body, ensure_ascii=False, indent=2))
                for path, media_type in (
                    (destination, "text"),
                    (text_path, "text"),
                    (metadata_path, "metadata"),
                ):
                    callbacks.on_file(
                        FileRecord(
                            path=path.resolve(), module_id=task.module_id, task_id=task.task_id,
                            media_type=media_type, size=path.stat().st_size,
                            title=str(summary.get("title") or novel_id), source_url=self.target_url(PixivTarget("novel", novel_id)),
                            source_id=novel_id, author_id=str(summary.get("author_id") or ""),
                            author_name=str(summary.get("author_name") or ""), published_at=str(summary.get("published_at") or ""),
                            tags=tuple(str(item) for item in summary.get("tags") or ()),
                            metadata={"pixiv_type": "novel", "series_id": series_id},
                        )
                    )
                    downloaded += 1
            callbacks.on_progress(
                ProgressEvent(
                    task.task_id, task.module_id, "info", f"[{index}/{len(novel_ids)}] 已保存小说 {summary.get('title') or novel_id}",
                    current=index, total=len(novel_ids), percent=index / len(novel_ids) * 100.0,
                )
            )
        callbacks.on_progress(
            ProgressEvent(
                task.task_id, task.module_id, "info",
                f"Pixiv 小说完成：保存 {downloaded} 个文件，AI 跳过 {skipped_ai} 篇，黑名单跳过 {skipped_blocked} 篇",
                current=len(novel_ids), total=len(novel_ids), percent=100.0,
                metadata={"downloaded": downloaded, "skipped_ai": skipped_ai, "skipped_blocked": skipped_blocked},
            )
        )

    @staticmethod
    def _novel_summary(body: object, novel_id: str) -> dict:
        data = body if isinstance(body, dict) else {}
        tags = ((data.get("tags") or {}).get("tags") or []) if isinstance(data.get("tags"), dict) else data.get("tags", [])
        return {
            "title": PixivCrawler._decode_html_entities(data.get("title") or novel_id),
            "description": PixivCrawler._plain_html_text(data.get("description") or "Pixiv 小说"),
            "author_id": str(data.get("userId") or ""),
            "author_name": PixivCrawler._decode_html_entities(data.get("userName") or ""),
            "published_at": str(data.get("createDate") or ""),
            "tags": [
                PixivCrawler._decode_html_entities(item.get("tag") if isinstance(item, dict) else item)
                for item in tags or []
            ],
            "page_html": str(data.get("description") or ""),
            "novel_id": novel_id,
        }

    def _body(self, path: str, timeout: tuple[int, int] = (5, 20)):
        payload = request_json(self.session, PIXIV_BASE + path, timeout=timeout)
        return payload.get("body")

    def _json(self, url: str) -> dict:
        return request_json(self.session, url)

    def _text(self, url: str, *, timeout: tuple[int, int]) -> tuple[str, str]:
        HTTP_BACKOFF.wait(url)
        response = request_get(self.session, url, timeout=timeout)
        try:
            record_http_backoff(response, url)
            response.raise_for_status()
            return read_response_text(response), str(response.url or url)
        finally:
            response.close()

    @staticmethod
    def _is_ai(detail: dict) -> bool:
        if str(detail.get("aiType") or "") == "2":
            return True
        tags = detail.get("tags") or []
        if isinstance(tags, dict):
            tags = tags.get("tags") or []
        if not isinstance(tags, list):
            return False
        values = {str(item.get("tag") if isinstance(item, dict) else item).strip().casefold() for item in tags}
        return bool(values & {"ai生成", "ai-generated", "aiイラスト"})

    @staticmethod
    def _work_summary(detail: dict) -> dict:
        tags = ((detail.get("tags") or {}).get("tags") or []) if isinstance(detail, dict) else []
        return {
            "title": PixivCrawler._decode_html_entities(detail.get("illustTitle") or detail.get("title") or ""),
            "description": PixivCrawler._plain_html_text(detail.get("description") or ""),
            "author_id": str(detail.get("userId") or ""),
            "author_name": PixivCrawler._decode_html_entities(detail.get("userName") or ""),
            "published_at": str(detail.get("createDate") or ""),
            "tags": [
                PixivCrawler._decode_html_entities(item.get("tag") or "")
                for item in tags
                if isinstance(item, dict)
            ],
            "ai_type": int(detail.get("aiType") or 0),
            "page_html": str(detail.get("description") or ""),
            "avatar_url": str(
                ((detail.get("urls") or {}).get("regular") or (detail.get("urls") or {}).get("small") or "")
                if isinstance(detail.get("urls"), dict)
                else ""
            ),
        }

    @staticmethod
    def _plain_html_text(value: object) -> str:
        text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return PixivCrawler._decode_html_entities(text).strip()

    @staticmethod
    def _decode_html_entities(value: object) -> str:
        text = str(value or "")
        for _ in range(3):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        return text

    @staticmethod
    def _profile_links(body: dict) -> list[dict]:
        links: list[dict] = []
        seen: set[str] = set()

        def add(label: object, value: object) -> None:
            url = str(value or "").strip()
            if not url.startswith(("http://", "https://")) or url.casefold() in seen:
                return
            seen.add(url.casefold())
            links.append({"label": str(label or "网站").strip() or "网站", "url": url})

        add("个人网站", body.get("webpage"))
        social = body.get("social") or body.get("socials")
        if isinstance(social, dict):
            for label, item in social.items():
                if isinstance(item, dict):
                    add(label, item.get("url") or item.get("link"))
                else:
                    add(label, item)
        return links

    @staticmethod
    def _profile_avatar_url(body: dict) -> str:
        preferred = ("imageBig", "profileImageUrl", "profile_image_url", "image", "avatar_url")
        for key in preferred:
            value = body.get(key)
            if isinstance(value, str) and value.startswith("https://"):
                return value
        for value in body.values():
            if isinstance(value, dict):
                found = PixivCrawler._profile_avatar_url(value)
                if found:
                    return found
        return ""

    @staticmethod
    def _merge_links(*groups: object) -> list[dict]:
        result: list[dict] = []
        seen: set[str] = set()
        for group in groups:
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                url = str(item.get("url") or "").strip()
                if not url.startswith(("http://", "https://")) or url.casefold() in seen:
                    continue
                seen.add(url.casefold())
                result.append({"label": str(item.get("label") or url), "url": url})
        return result
