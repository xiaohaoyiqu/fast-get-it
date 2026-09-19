from __future__ import annotations

import hashlib
import html as html_module
import json
import math
import re
import threading
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, urlencode, urljoin, urlparse

import requests
from PIL import Image

try:
    from curl_cffi import requests as curl_requests
    from curl_cffi.requests.errors import RequestsError as CurlRequestsError
except ImportError:  # pragma: no cover - packaged builds install requirements-app.
    curl_requests = None

    class CurlRequestsError(Exception):
        pass

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore
from software_app.core.events import CallbackSet
from software_app.core.image_postprocess import build_image_artifacts
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview
from software_app.crawlers.common import (
    load_cookie_file,
    make_session,
    normalize_output_format,
    platform_output_root,
    safe_component,
)
from software_app.crawlers.jmcomic.api import JmComicApiClient


DEFAULT_DOMAIN = "https://18comic.vip"
SCRAMBLE_268850 = 268850
SCRAMBLE_421926 = 421926
SCRAMBLE_FALLBACK = 220980

JM_ORDER_VALUES = {"最新": "mr", "最多浏览": "mv", "最多图片": "mp", "最多收藏": "tf"}
JM_TIME_VALUES = {"今天": "t", "本周": "w", "本月": "m", "全部时间": "a"}
JM_CATEGORY_VALUES = {
    "全部": "0", "同人": "doujin", "单本": "single", "短篇": "short", "其他": "another",
    "韩漫": "hanman", "美漫": "meiman", "Cosplay": "doujin_cosplay", "3D": "3D", "英文": "english_site",
}


def make_jm_session(
    domain: str, cookie_file: Path | str | None = None, proxy_url: str = "", *,
    user_agent: str = "", browser_headers: dict[str, str] | None = None,
):
    headers = dict(browser_headers or {})
    if str(user_agent or "").strip():
        headers["User-Agent"] = str(user_agent).strip()
    fallback = make_session(
        referer=domain.rstrip("/") + "/", cookie_file=cookie_file, proxy_url=proxy_url,
        extra_headers=headers or None,
    )
    fallback.trust_env = False
    if curl_requests is None:
        return fallback
    curl_session = curl_requests.Session(
        impersonate="chrome123", headers=dict(fallback.headers), proxies=dict(fallback.proxies)
    )
    curl_session.trust_env = False
    curl_session.cookies.update(load_cookie_file(cookie_file))
    fallback.close()
    return curl_session


def jm_response_text(response) -> str:
    if not getattr(response, "encoding", None):
        response.encoding = "utf-8"
    return response.text


@dataclass(frozen=True)
class JmTarget:
    kind: str
    value: str


def parse_jm_target(raw_target: str, input_kind: str = "") -> JmTarget:
    value = str(raw_target or "").strip()
    if not value:
        raise ValueError("JMComic 目标不能为空")
    forced = str(input_kind or "").strip().lower()
    if forced in {"album", "work"}:
        return JmTarget("album", _jm_id(value))
    if forced in {"photo", "chapter"}:
        return JmTarget("photo", _jm_id(value))
    if forced == "search":
        return JmTarget("search", value)
    if forced == "novel":
        return JmTarget("novel", _jm_id(value))
    match = re.search(r"/(album|photo|novel)s?/(\d+)", value, flags=re.IGNORECASE)
    if match:
        return JmTarget(match.group(1).lower(), match.group(2))
    if re.fullmatch(r"(?:JM)?\d+", value, flags=re.IGNORECASE):
        return JmTarget("album", _jm_id(value))
    return JmTarget("search", value)


def _jm_id(value: str) -> str:
    text = str(value or "").strip()
    for pattern in (r"/(?:albums?|photos?|novels?)/(\d+)", r"[?&](?:id|nid)=(\d+)(?:&|$)", r"^(?:JM)?(\d+)$"):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    raise ValueError("目标中没有可用的 JMComic ID")


def segmentation_count(scramble_id: int | str, photo_id: int | str, filename: str) -> int:
    scramble = int(scramble_id or 0)
    aid = int(photo_id)
    if not scramble or aid < scramble:
        return 0
    if aid < SCRAMBLE_268850:
        return 10
    divisor = 10 if aid < SCRAMBLE_421926 else 8
    digest = hashlib.md5(f"{aid}{filename}".encode()).hexdigest()
    return (ord(digest[-1]) % divisor) * 2 + 2


def decode_scrambled_image(content: bytes, count: int) -> Image.Image:
    with Image.open(BytesIO(content)) as source:
        source.load()
        if not count:
            return source.copy()
        width, height = source.size
        decoded = Image.new("RGB", (width, height))
        over = height % count
        for index in range(count):
            move = math.floor(height / count)
            source_y = height - move * (index + 1) - over
            target_y = move * index
            if index == 0:
                move += over
            else:
                target_y += over
            decoded.paste(source.crop((0, source_y, width, source_y + move)), (0, target_y, width, target_y + move))
        return decoded


class JmComicCrawler:
    def __init__(
        self, domain: str = DEFAULT_DOMAIN, cookie_file: Path | str | None = None,
        proxy_url: str = "", user_agent: str = "", browser_headers: dict[str, str] | None = None,
    ) -> None:
        self.domain = str(domain or DEFAULT_DOMAIN).rstrip("/")
        self.cookie_file = cookie_file
        self.session = make_jm_session(
            self.domain, cookie_file, proxy_url, user_agent=user_agent, browser_headers=browser_headers
        )

    def preview(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        options = options or {}
        target = parse_jm_target(raw_target, str(options.get("input_kind") or ""))
        target_url = self.target_url(target)
        if not bool(options.get("live", False)):
            destination = {
                "album": "漫画详情/章节目录页",
                "photo": "章节正文阅读页",
                "novel": "小说详情页",
                "search": "搜索结果页",
            }[target.kind]
            online_fields = {
                "album": "标题、作者、标签、章节、封面和可用时的第一页",
                "photo": "章节标题、图片列表和可用时的第一页",
                "novel": "小说页面；正文下载尚未接入",
                "search": "最多 15 个漫画候选",
            }[target.kind]
            return TargetPreview(
                module_id="jmcomic",
                raw_target=raw_target,
                normalized_target=target_url,
                title=f"JMComic {target.kind}: {target.value}",
                description=f"本地已识别为{destination}，未访问网站；在线获取资料可读取{online_fields}。",
                metadata={"input_kind": target.kind, "target_id": target.value, "browser_destination": destination},
            )
        if target.kind == "search":
            rows = self.search(target.value, limit=15, search_mode=str(options.get("search_mode") or ""), options=options)
            items = "".join(f'<li><a href="{row["url"]}">{html_module.escape(row["title"])}</a></li>' for row in rows)
            return TargetPreview(
                module_id="jmcomic",
                raw_target=raw_target,
                normalized_target=target_url,
                title=f"JMComic 搜索：{target.value}",
                description=f"找到 {len(rows)} 个候选",
                metadata={"input_kind": "search", "search_results": rows, "page_html": f"<h2>搜索结果</h2><ol>{items}</ol>"},
            )
        if target.kind == "novel":
            return TargetPreview(
                module_id="jmcomic",
                raw_target=raw_target,
                normalized_target=target_url,
                title=f"JMComic 小说 {target.value}",
                description="已识别小说详情页；当前支持从账号读取小说收藏候选，正文下载尚未接入。",
                metadata={"input_kind": "novel", "target_id": target.value, "browser_destination": "小说详情页"},
            )
        html_text = self._get_text(target_url)
        detail = self._parse_album(html_text, target.value) if target.kind == "album" else self._parse_photo(html_text, target.value)
        warnings: list[str] = []
        if target.kind == "album" and detail.get("chapters"):
            first_chapter_id = str(detail["chapters"][0]["id"])
            try:
                first_photo = self._parse_photo(self._get_text(f"{self.domain}/photo/{first_chapter_id}"), first_chapter_id)
                detail["first_page_url"] = next(iter(first_photo["images"]), "")
                detail["first_page_photo_id"] = first_chapter_id
                detail["first_page_scramble_id"] = first_photo["scramble_id"]
            except (OSError, RuntimeError, ValueError, PermissionError) as exc:
                warnings.append(f"第一页暂时无法预览：{exc}")
        elif target.kind == "photo":
            detail["first_page_url"] = next(iter(detail["images"]), "")
            detail["first_page_photo_id"] = target.value
            detail["first_page_scramble_id"] = detail["scramble_id"]
        if detail.get("first_page_url") and not detail.get("avatar_url"):
            detail["avatar_url"] = detail["first_page_url"]
        return TargetPreview(
            module_id="jmcomic",
            raw_target=raw_target,
            normalized_target=target_url,
            title=str(detail.get("title") or target.value),
            description=(
                f"作者 {detail.get('author') or '未知'}；章节 {len(detail.get('chapters') or [])}；标签 {', '.join(detail.get('tags') or [])}"
                if target.kind == "album"
                else f"章节图片 {len(detail.get('images') or [])} 张"
            ),
            warnings=warnings,
            metadata={
                **detail,
                "input_kind": target.kind,
                "browser_destination": "漫画详情/章节目录页" if target.kind == "album" else "章节正文阅读页",
                "page_html": self._summary_html(detail),
            },
        )

    def target_url(self, target: JmTarget) -> str:
        if target.kind == "search":
            return f"{self.domain}/search/photos?search_query={quote(target.value)}"
        return f"{self.domain}/{target.kind}/{target.value}"

    def search(self, keyword: str, limit: int = 15, search_mode: str = "", options: dict | None = None) -> list[dict]:
        options = options or {}
        result_limit = max(1, min(int(limit), 15))
        if search_mode == "分类 / 排行":
            return self.category(limit=result_limit, options=options)
        if search_mode in {"收藏夹", "漫画收藏夹"}:
            return self.favorites(keyword, limit=result_limit, options=options)
        if search_mode == "小说收藏夹":
            return self.account_novels(
                keyword, "favorite/novels", "小说收藏夹", limit=result_limit, options=options,
                folder_id=str(options.get("novel_favorite_folder_id") or "0"),
            )
        if search_mode == "追更连载":
            return self.account_albums(keyword, "tracking", "追更连载", limit=result_limit, options=options)
        if search_mode in {"站内浏览历史", "漫画观看记录"}:
            return self.account_albums(
                keyword, "favorite/watchlist", "漫画观看记录", limit=result_limit, options=options
            )
        if search_mode == "小说观看记录":
            return self.account_novels(
                keyword, "favorite/novel_watchlist", "小说观看记录", limit=result_limit, options=options
            )
        main_tag = {
            "综合搜索": 0,
            "作品搜索": 1,
            "作者搜索": 2,
            "标签搜索": 3,
            "角色搜索": 4,
        }.get(str(search_mode or ""), 0)
        order_by = self._option_code(options.get("order_by"), JM_ORDER_VALUES, "mr")
        time_range = self._option_code(options.get("time_range"), JM_TIME_VALUES, "a")
        exact_match = (
            str(options.get("match_mode") or "fuzzy").lower() == "exact"
            and search_mode in {"", "综合搜索", "作品搜索"}
        )
        wanted_title = keyword.strip().casefold()
        rows: list[dict] = []
        seen: set[str] = set()
        seen_pages: set[str] = set()
        for page in range(1, 11):
            query = urlencode({"search_query": keyword, "main_tag": main_tag, "page": page, "o": order_by, "t": time_range})
            page_rows = self._parse_album_cards(self._get_text(f"{self.domain}/search/photos?{query}"), search_mode or "综合搜索")
            if not page_rows:
                break
            page_ids = {str(row["id"]) for row in page_rows}
            if page_ids <= seen_pages:
                break
            seen_pages.update(page_ids)
            if exact_match:
                page_rows = [row for row in page_rows if str(row.get("title") or "").strip().casefold() == wanted_title]
            self._extend_unique(rows, page_rows, seen, result_limit)
            if len(rows) >= result_limit:
                break
        return rows

    def category(self, limit: int = 15, options: dict | None = None) -> list[dict]:
        options = options or {}
        result_limit = max(1, min(int(limit), 15))
        category = self._option_code(options.get("category"), JM_CATEGORY_VALUES, "0")
        order_by = self._option_code(options.get("order_by"), JM_ORDER_VALUES, "mr")
        time_range = self._option_code(options.get("time_range"), JM_TIME_VALUES, "a")
        path = "/albums" if category == "0" else f"/albums/{quote(category)}"
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 11):
            query = urlencode({"page": page, "o": order_by, "t": time_range})
            page_rows = self._parse_album_cards(self._get_text(f"{self.domain}{path}?{query}"), "分类 / 排行")
            if not page_rows:
                break
            before = len(seen)
            self._extend_unique(rows, page_rows, seen, result_limit)
            if len(rows) >= result_limit or len(seen) == before:
                break
        return rows

    def favorites(self, username: str, limit: int = 15, options: dict | None = None) -> list[dict]:
        options = options or {}
        return self.account_albums(
            username, "favorite/albums", "漫画收藏夹", limit=limit, options=options,
            folder_id=str(options.get("favorite_folder_id") or "0"),
        )

    def account_albums(
        self, username: str, route: str, result_type: str, *, limit: int = 15,
        options: dict | None = None, folder_id: str = "",
    ) -> list[dict]:
        return self._account_candidates(
            username, route, result_type, self._parse_album_cards, limit=limit,
            options=options, folder_id=folder_id,
        )

    def account_novels(
        self, username: str, route: str, result_type: str, *, limit: int = 15,
        options: dict | None = None, folder_id: str = "",
    ) -> list[dict]:
        return self._account_candidates(
            username, route, result_type, self._parse_novel_cards, limit=limit,
            options=options, folder_id=folder_id,
        )

    def _account_candidates(
        self, username: str, route: str, result_type: str, parser, *, limit: int,
        options: dict | None = None, folder_id: str = "",
    ) -> list[dict]:
        options = options or {}
        username = str(options.get("favorite_username") or username or "").strip().lstrip("@")
        if not username:
            raise ValueError(f"JMComic {result_type}需要填写个人主页用户名")
        self._require_account_cookie(result_type)
        order_by = self._option_code(options.get("order_by"), JM_ORDER_VALUES, "mr")
        folder_id = str(folder_id or "").strip()
        result_limit = max(1, min(int(limit), 15))
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 11):
            query_values = {"page": page, "o": order_by}
            if folder_id:
                # Mirrors have used both parameter names. Supplying both is harmless
                # and keeps previously saved folder IDs usable.
                query_values.update({"folder": folder_id, "folder_id": folder_id})
            query = urlencode(query_values)
            url = f"{self.domain}/user/{quote(username)}/{route.strip('/')}?{query}"
            page_rows = parser(self._get_account_text(url, result_type), result_type)
            if not page_rows:
                break
            before = len(seen)
            self._extend_unique(rows, page_rows, seen, result_limit)
            if len(rows) >= result_limit or len(seen) == before:
                break
        return rows

    def _require_account_cookie(self, feature: str) -> None:
        if not self.session.cookies:
            raise PermissionError(f"JMComic {feature}需要先导入已登录账号的 Cookie 或请求标头")
        if "AVS" not in self.session.cookies:
            if "cf_clearance" in self.session.cookies:
                raise PermissionError(f"当前只有 cf_clearance，没有 AVS 登录会话；无法读取 JMComic {feature}")
            raise PermissionError(f"没有检测到 AVS 登录会话；无法读取 JMComic {feature}")

    def _get_account_text(self, url: str, feature: str) -> str:
        response = self._get_response(url, timeout=(10, 45))
        page = jm_response_text(response)
        final_path = urlparse(str(response.url or "")).path.casefold().rstrip("/")
        if final_path == "/login" or any(
            text in page for text in ("請先登入", "请先登录", "會員登入", "会员登录")
        ):
            raise PermissionError(f"JMComic 登录会话无效，站点把{feature}跳转到了登录页；请重新导入当前域名的 AVS")
        if "Restricted Access!" in page:
            raise PermissionError("JMComic 拒绝当前地区或网络访问；请更换可用域名或代理")
        if "Could not connect to mysql!" in page:
            raise RuntimeError("JMComic 服务器内部错误；请稍后重试")
        return page

    @staticmethod
    def _option_code(value: object, mapping: dict[str, str], default: str) -> str:
        text = str(value or "").strip()
        return mapping.get(text, text if text in mapping.values() else default)

    def _parse_album_cards(self, html_text: str, result_type: str) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        anchor_pattern = re.compile(r'<a\b([^>]*href=["\'](?:https?://[^/"\']+)?/album/(\d+)(?:/[^"\']*)?["\'][^>]*)>([\s\S]*?)</a>', re.IGNORECASE)
        for attributes, album_id, content in anchor_pattern.findall(html_text):
            if album_id in seen:
                continue
            title = _first_group(r'(?:title|alt)=["\']([^"\']+)["\']', attributes + " " + content)
            if not title:
                title = _strip_tags(content)
            title = html_module.unescape(_strip_tags(title)).strip()
            if not title:
                continue
            cover = html_module.unescape(_first_group(r'(?:data-original|data-src|src)=["\']([^"\']+)["\']', content))
            seen.add(album_id)
            rows.append({
                "id": album_id,
                "title": title,
                "url": f"{self.domain}/album/{album_id}",
                "cover_url": urljoin(self.domain + "/", cover) if cover else "",
                "avatar_url": urljoin(self.domain + "/", cover) if cover else "",
                "type": result_type,
                "search_mode": result_type,
            })
        return rows

    def _parse_novel_cards(self, html_text: str, result_type: str) -> list[dict]:
        rows: list[dict] = []
        seen: set[str] = set()
        pattern = re.compile(
            r'<a\b([^>]*href=["\'](?:https?://[^/"\']+)?/novel/(\d+)(?:/[^"\']*)?["\'][^>]*)>([\s\S]*?)</a>',
            re.IGNORECASE,
        )
        for attributes, novel_id, content in pattern.findall(html_text):
            if novel_id in seen:
                continue
            title = _first_group(r'(?:title|alt)=["\']([^"\']+)["\']', attributes + " " + content)
            if not title:
                title = _strip_tags(content)
            title = html_module.unescape(_strip_tags(title)).strip()
            if not title:
                continue
            cover = html_module.unescape(
                _first_group(r'(?:data-original|data-src|src)=["\']([^"\']+)["\']', content)
            )
            seen.add(novel_id)
            rows.append({
                "id": novel_id,
                "title": title,
                "url": f"{self.domain}/novel/{novel_id}",
                "cover_url": urljoin(self.domain + "/", cover) if cover else "",
                "avatar_url": urljoin(self.domain + "/", cover) if cover else "",
                "type": result_type,
                "search_mode": result_type,
                "input_kind": "novel",
                "source": f"JMComic {result_type}",
                "downloadable": False,
                "read_only_reason": "JMComic 小说正文下载尚未接入；可查看或加入本地黑名单",
            })
        return rows

    @staticmethod
    def _extend_unique(target: list[dict], source: list[dict], seen: set[str], limit: int) -> None:
        for row in source:
            album_id = str(row.get("id") or "")
            if not album_id or album_id in seen:
                continue
            seen.add(album_id)
            target.append(row)
            if len(target) >= limit:
                break

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        target = parse_jm_target(task.target, str(task.options.get("input_kind") or ""))
        if target.kind == "novel":
            raise NotImplementedError("JMComic 小说正文下载尚未接入；当前可读取小说收藏候选并进行屏蔽管理")
        if target.kind == "search":
            album_ids = [
                row["id"]
                for row in self.search(
                    target.value,
                    limit=int(task.options.get("max_albums") or 15),
                    search_mode=str(task.options.get("search_mode") or ""),
                    options=task.options,
                )
            ]
        elif target.kind == "album":
            album_ids = [target.value]
        else:
            album_ids = []
        if target.kind == "search" and not album_ids:
            raise LookupError("JMComic 没有找到可下载的漫画；请检查关键词、筛选条件或登录状态")
        root = platform_output_root(task.output_dir, "jmcomic")
        if target.kind == "photo":
            self._download_photo(target.value, target.value, root / f"JM{target.value}", callbacks, task, cancel_event)
            return
        for album_index, album_id in enumerate(album_ids, 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            album_html = self._get_text(f"{self.domain}/album/{album_id}")
            album = self._parse_album(album_html, album_id)
            blocklist_path = str(task.options.get("_blocklist_path") or "")
            blocklist = BlocklistStore(blocklist_path) if blocklist_path else None
            if blocklist and blocklist.is_blocked(
                "jmcomic", f"{self.domain}/album/{album_id}", input_kind="album",
                author_id=str(album.get("author") or ""), tags=album.get("tags") or [],
            ):
                callbacks.on_progress(ProgressEvent(
                    task.task_id, task.module_id, "warning",
                    f"黑名单已跳过 JM{album_id}（作者：{album.get('author') or '未知'}）",
                ))
                continue
            candidate_tags = task.options.get("jm_candidate_tags") or []
            if isinstance(candidate_tags, str):
                candidate_tags = [part.strip() for part in candidate_tags.replace("，", ",").split(",")]
            tags = list(dict.fromkeys(
                str(tag).strip() for tag in [*candidate_tags, *(album.get("tags") or [])] if str(tag).strip()
            ))[:5]
            tag_suffix = " ".join(f"[{tag}]" for tag in tags)
            album_dir = root / safe_component(
                f'{album.get("author") or "unknown"} - {album.get("title") or "JM" + album_id} {tag_suffix}'.strip()
            )
            if bool(task.options.get("jm_download_cover", True)) and album.get("cover_url"):
                try:
                    self._download_cover(str(album["cover_url"]), album_id, album_dir, callbacks, task)
                except Exception as exc:  # noqa: BLE001 - a missing thumbnail must not discard the manga pages.
                    callbacks.on_progress(ProgressEvent(
                        task.task_id, task.module_id, "warning", f"JM{album_id} 封面下载失败，继续下载章节：{exc}"
                    ))
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            chapters = album.get("chapters") or [{"id": album_id, "title": album.get("title") or album_id}]
            callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", f"[{album_index}/{len(album_ids)}] 下载 {album.get('title') or album_id}"))
            for chapter in chapters:
                chapter_id = str(chapter.get("id") or album_id)
                chapter_dir = album_dir / safe_component(str(chapter.get("title") or chapter_id))
                self._download_photo(chapter_id, album_id, chapter_dir, callbacks, task, cancel_event)

    def _download_photo(self, photo_id: str, album_id: str, folder: Path, callbacks: CallbackSet, task: DownloadTask, cancel_event: threading.Event) -> None:
        html_text = self._get_text(f"{self.domain}/photo/{photo_id}")
        photo = self._parse_photo(html_text, photo_id)
        image_urls = photo.get("images") or []
        if not image_urls:
            raise ValueError(f"JMComic 章节 {photo_id} 未返回图片列表；请检查页面是否已失效、需要登录或站点结构已变化")
        scramble_value = photo.get("scramble_id")
        scramble_id = int(SCRAMBLE_FALLBACK if scramble_value is None else scramble_value)
        downloaded_paths: list[Path] = []
        scramble_notice_sent = False
        for index, url in enumerate(image_urls, 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            filename = Path(urlparse(url).path).name or f"{index:05d}.jpg"
            segment_count = segmentation_count(scramble_id, photo_id, filename)
            if segment_count and not scramble_notice_sent:
                callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", f"JM{album_id} 检测到分割图，正在按章节信息还原"))
                scramble_notice_sent = True
            response = self._get_response(url, timeout=(10, 90))
            try:
                image = decode_scrambled_image(response.content, segment_count)
            except Exception as exc:
                raise ValueError(f"JMComic 章节 {photo_id} 第 {index} 张未返回有效图片；请检查站点或代理") from exc
            image_format = normalize_output_format("image_format", task.options.get("image_format"))
            image_format = "jpg" if image_format == "original" else image_format
            destination = folder / f"{index:03d}.{image_format}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + ".part")
            output_format = {"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[image_format]
            output_image = image.convert("RGB") if image_format == "jpg" else image
            try:
                output_image.save(temporary, format=output_format, quality=95)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
                if output_image is not image:
                    output_image.close()
                image.close()
            downloaded_paths.append(destination)
            callbacks.on_file(
                FileRecord(
                    path=destination.resolve(),
                    module_id=task.module_id,
                    task_id=task.task_id,
                    media_type="image",
                    size=destination.stat().st_size,
                    title=destination.name,
                    source_url=url,
                    source_id=album_id,
                    chapter=photo_id,
                    metadata={"page": index, "scramble_id": scramble_id},
                )
            )
            callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", f"JM{album_id} 章节 {photo_id}: {index}/{len(image_urls)}"))
        postprocess_mode = str(task.options.get("jm_postprocess") or "none")
        for artifact, media_type in build_image_artifacts(
            downloaded_paths,
            folder,
            safe_component(f"JM{album_id}_{photo_id}"),
            postprocess_mode,
            group_size=30,
        ):
            callbacks.on_file(FileRecord(
                path=artifact.resolve(), module_id=task.module_id, task_id=task.task_id,
                media_type=media_type, size=artifact.stat().st_size, title=artifact.name,
                source_url=f"{self.domain}/photo/{photo_id}", source_id=album_id, chapter=photo_id,
                metadata={"postprocess": postprocess_mode, "max_images_per_group": 30},
            ))

    def _download_cover(self, url: str, album_id: str, folder: Path, callbacks: CallbackSet, task: DownloadTask) -> None:
        response = self._get_response(url, timeout=(10, 60))
        image_format = normalize_output_format("image_format", task.options.get("image_format"))
        image_format = "jpg" if image_format == "original" else image_format
        destination = folder / f"JM{album_id}_cover.{image_format}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(destination.name + ".part")
        with Image.open(BytesIO(response.content)) as source:
            output = source.convert("RGB") if image_format == "jpg" else source.copy()
            try:
                output.save(temporary, format={"jpg": "JPEG", "png": "PNG", "webp": "WEBP"}[image_format], quality=95)
                temporary.replace(destination)
            finally:
                temporary.unlink(missing_ok=True)
                output.close()
        callbacks.on_file(FileRecord(
            path=destination.resolve(), module_id=task.module_id, task_id=task.task_id,
            media_type="image", size=destination.stat().st_size, title=destination.name,
            source_url=url, source_id=album_id, metadata={"jmcomic_kind": "cover"},
        ))

    def _get_text(self, url: str) -> str:
        response = self._get_response(url, timeout=(10, 45))
        page = jm_response_text(response)
        if "Restricted Access!" in page:
            raise PermissionError("JMComic 拒绝当前地区或网络访问；请更换可用域名或代理")
        if "Could not connect to mysql!" in page:
            raise RuntimeError("JMComic 服务器内部错误；请稍后重试")
        return page

    def _get_response(self, url: str, timeout: tuple[int, int]):
        response = None
        for attempt in range(2):
            try:
                response = self.session.get(url, timeout=timeout)
            except requests.exceptions.ProxyError as exc:
                raise RuntimeError("JMComic 代理连接失败；请在设置中检查代理地址或暂时清空代理") from exc
            except requests.exceptions.ConnectTimeout as exc:
                raise TimeoutError("JMComic 站点连接超时；请检查当前域名、代理或网络后重试") from exc
            except requests.exceptions.Timeout as exc:
                raise TimeoutError("JMComic 响应超时；站点可能繁忙，请稍后重试") from exc
            except requests.exceptions.RequestException as exc:
                raise RuntimeError(f"JMComic 网络请求失败：{exc}") from exc
            except CurlRequestsError as exc:
                message = str(exc)
                if "timed out" in message.casefold() or "timeout" in message.casefold():
                    raise TimeoutError("JMComic 连接或响应超时；请检查当前域名、代理或网络后重试") from exc
                if "proxy" in message.casefold():
                    raise RuntimeError("JMComic 代理连接失败；请在设置中检查代理地址") from exc
                raise RuntimeError(f"JMComic 浏览器指纹请求失败：{message}") from exc
            if int(getattr(response, "status_code", 0) or 0) != 403 or attempt:
                break
            response.close()
            time.sleep(0.6)
        assert response is not None
        try:
            response.raise_for_status()
        except Exception as exc:
            status = int(getattr(response, "status_code", 0) or 0)
            if status == 429:
                raise RuntimeError("JMComic 请求过于频繁（HTTP 429），请稍后再试并降低连续搜索/下载频率") from exc
            if status == 403:
                raise PermissionError(
                    "JMComic 返回 HTTP 403；请导入与当前域名、代理出口和浏览器一致的 cf_clearance，或在浏览器重新完成验证"
                ) from exc
            if status == 401:
                raise PermissionError("JMComic 拒绝访问；请检查站点域名、Cookie、地区网络或访问频率") from exc
            raise
        return response

    def _parse_album(self, html_text: str, album_id: str) -> dict:
        title = _first_group(r'<h1 class="book-name" id="book-name">([\s\S]*?)</h1>', html_text)
        tags = self._section_links(html_text, r'<span itemprop="genre" data-type="tags">([\s\S]*?)</span>')
        authors = self._section_links(html_text, r'作者：\s*<span itemprop="author" data-type="author">([\s\S]*?)</span>')
        cover = html_module.unescape(
            _first_group(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html_text)
            or _first_group(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', html_text)
        )
        chapters: list[dict] = []
        seen: set[str] = set()
        for photo_id, text in re.findall(r'href=["\'](?:https?://[^/"\']+)?/photo/(\d+)[^"\']*["\'][^>]*>([\s\S]*?)</a>', html_text, flags=re.IGNORECASE):
            if photo_id in seen:
                continue
            seen.add(photo_id)
            label = html_module.unescape(_strip_tags(text)).strip() or f"章节 {photo_id}"
            chapters.append({"id": photo_id, "title": label})
        for photo_id, text in re.findall(
            r'data-album=["\'](\d+)["\'][^>]*>\s*<li[^>]*>([\s\S]*?)</li>', html_text, flags=re.IGNORECASE
        ):
            if photo_id in seen:
                continue
            seen.add(photo_id)
            label = html_module.unescape(_strip_tags(text)).strip() or f"章节 {photo_id}"
            chapters.append({"id": photo_id, "title": label})
        return {
            "id": album_id,
            "title": html_module.unescape(_strip_tags(title)).strip() or f"JM{album_id}",
            "author": authors[0] if authors else "unknown",
            "authors": authors,
            "tags": tags,
            "chapters": chapters,
            "cover_url": urljoin(self.domain + "/", cover) if cover else "",
            "avatar_url": urljoin(self.domain + "/", cover) if cover else "",
        }

    def _parse_photo(self, html_text: str, photo_id: str) -> dict:
        title = _first_group(r'<title>([\s\S]*?)\|', html_text)
        scramble = _first_group(r'var\s+scramble_id\s*=\s*(\d+)', html_text) or str(SCRAMBLE_FALLBACK)
        page_arr_text = _first_group(r'var\s+page_arr\s*=\s*(.*?);', html_text)
        first_url = html_module.unescape(_first_group(r'data-original=["\']([^"\']+)["\']', html_text))
        image_names: list[str] = []
        if page_arr_text:
            try:
                value = json.loads(page_arr_text)
                if isinstance(value, list):
                    image_names = [str(item) for item in value if re.fullmatch(r"[\w.-]+\.(?:jpe?g|png|webp|gif)", str(item), re.IGNORECASE)]
            except json.JSONDecodeError:
                pass
        domain = urlparse(first_url).netloc
        if not domain:
            domain = _first_group(r'https?://([^/"\']+)/media/albums/blank', html_text)
        query = ("?" + urlparse(first_url).query) if urlparse(first_url).query else ""
        images = [f"https://{domain}/media/photos/{photo_id}/{name}{query}" for name in image_names] if domain else []
        if not images:
            images = [html_module.unescape(url) for url in re.findall(r'data-original=["\']([^"\']+/media/photos/[^"\']+)["\']', html_text)]
        return {"id": photo_id, "title": html_module.unescape(_strip_tags(title)).strip(), "scramble_id": scramble, "images": list(dict.fromkeys(images))}

    @staticmethod
    def _section_links(html_text: str, section_pattern: str) -> list[str]:
        section = _first_group(section_pattern, html_text)
        return [html_module.unescape(_strip_tags(item)).strip() for item in re.findall(r'<a[^>]*>([\s\S]*?)</a>', section) if _strip_tags(item).strip()]

    @staticmethod
    def _summary_html(detail: dict) -> str:
        tags = "".join(f"<li>{html_module.escape(str(item))}</li>" for item in detail.get("tags") or [])
        chapters = "".join(f"<li>{html_module.escape(str(item.get('title') or item.get('id')))}</li>" for item in detail.get("chapters") or [])
        cover = html_module.escape(str(detail.get("cover_url") or ""), quote=True)
        first_page = html_module.escape(str(detail.get("first_page_url") or ""), quote=True)
        links = (f'<p><a href="{cover}">打开封面</a></p>' if cover else "") + (
            f'<p><a href="{first_page}">打开第一页</a></p>' if first_page else ""
        )
        return f"<h2>{html_module.escape(str(detail.get('title') or 'JMComic'))}</h2><p>作者：{html_module.escape(str(detail.get('author') or '-'))}</p>{links}<h3>标签</h3><ul>{tags}</ul><h3>章节</h3><ol>{chapters}</ol>"


def _first_group(pattern: str, text: str) -> str:
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", "", str(value or ""))
