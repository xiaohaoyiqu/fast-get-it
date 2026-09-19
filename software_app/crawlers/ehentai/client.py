from __future__ import annotations

import html
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

import requests
from PIL import Image

from software_app.core.adapter import TaskCancelled
from software_app.core.aria2_bt import Aria2Error, download_torrent_with_aria2
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview, classify_file
from software_app.core.torrent_metadata import parse_torrent
from software_app.crawlers.common import (
    download_file,
    extension_from_url,
    load_cookie_file,
    make_session,
    normalized_media_extension,
    convert_media_file,
    platform_output_root,
    safe_component,
)


EH_HOSTS = {"e-hentai.org", "www.e-hentai.org", "exhentai.org", "www.exhentai.org"}
GALLERY_PATH_RE = re.compile(r"^/g/(\d+)/([0-9a-f]{10})/?$", re.IGNORECASE)
IMAGE_PAGE_PATH_RE = re.compile(r"^/s/([0-9a-f]{10})/(\d+)-(\d+)/?$", re.IGNORECASE)
SHORT_GALLERY_RE = re.compile(r"^(\d+)/([0-9a-f]{10})$", re.IGNORECASE)
AI_TAG_MARKERS = ("ai generated", "ai-generated", "ai assisted", "ai-assisted")
_SEARCH_LOCK = threading.Lock()
_LAST_SEARCH_AT = 0.0


@dataclass(frozen=True)
class EhentaiTarget:
    kind: str
    site: str = "e-hentai"
    gid: str = ""
    token: str = ""
    query: str = ""

    @property
    def origin(self) -> str:
        return "https://exhentai.org" if self.site == "exhentai" else "https://e-hentai.org"

    @property
    def url(self) -> str:
        if self.kind == "gallery":
            return f"{self.origin}/g/{self.gid}/{self.token}/"
        if self.kind == "favorites":
            suffix = f"?{urlencode({'f_search': self.query})}" if self.query else ""
            return f"{self.origin}/favorites.php{suffix}"
        suffix = f"?{urlencode({'f_search': self.query})}" if self.query else ""
        return f"{self.origin}/{suffix}"


def parse_ehentai_target(raw_target: str, *, default_site: str = "e-hentai") -> EhentaiTarget:
    value = str(raw_target or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("E-Hentai 目标不能为空")
    site = "exhentai" if str(default_site).casefold().startswith("ex") else "e-hentai"
    short = SHORT_GALLERY_RE.fullmatch(value.strip("/"))
    if short:
        return EhentaiTarget("gallery", site, short.group(1), short.group(2).lower())
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = str(parsed.hostname or "").casefold().rstrip(".")
        if parsed.scheme != "https" or host not in EH_HOSTS:
            raise ValueError("EH 目标必须使用 e-hentai.org 或 exhentai.org 的 HTTPS 地址")
        site = "exhentai" if host.endswith("exhentai.org") else "e-hentai"
        gallery = GALLERY_PATH_RE.fullmatch(parsed.path)
        if gallery:
            return EhentaiTarget("gallery", site, gallery.group(1), gallery.group(2).lower())
        query = str(parse_qs(parsed.query).get("f_search", [""])[0]).strip()
        if parsed.path.rstrip("/").casefold() == "/favorites.php":
            return EhentaiTarget("favorites", site, query=query)
        if parsed.path in {"", "/"}:
            return EhentaiTarget("search", site, query=query)
        raise ValueError("请输入 EH 画廊链接、搜索页或收藏夹地址")
    if len(value) > 200 or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("EH 搜索词过长或包含无效字符")
    return EhentaiTarget("search", site, query=value)


class _EhHtmlParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_en = ""
        self.title_jpn = ""
        self.thumbnail_url = ""
        self.gallery_rows: list[dict] = []
        self.tags: list[str] = []
        self.page_links: list[str] = []
        self.torrent_page_url = ""
        self.archive_url = ""
        self.torrent_links: list[str] = []
        self._heading = ""
        self._heading_parts: list[str] = []
        self._anchor: dict | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag == "meta" and (values.get("property") or "").casefold() == "og:image":
            self.thumbnail_url = values.get("content", "")
        if tag == "h1" and values.get("id") in {"gn", "gj"}:
            self._heading = values["id"]
            self._heading_parts = []
        if tag == "a":
            href = html.unescape(values.get("href", ""))
            onclick = html.unescape(values.get("onclick", ""))
            popup = re.search(r"popUp\(['\"]([^'\"]+)", onclick)
            if popup and ("gallerytorrents.php" in popup.group(1) or "archiver.php" in popup.group(1)):
                href = popup.group(1)
            self._anchor = {"href": href, "class": values.get("class", ""),
                            "title": values.get("title", ""), "parts": [], "image": ""}
        elif tag == "img" and self._anchor is not None:
            self._anchor["image"] = values.get("data-src") or values.get("src") or ""
        elif self._anchor is not None:
            if values.get("class"):
                self._anchor["class"] = f"{self._anchor.get('class', '')} {values['class']}".strip()
            style = values.get("style", "")
            match = re.search(r"url\((?:&quot;|['\"])?([^)'\"]+)", style)
            if match and not self._anchor.get("image"):
                self._anchor["image"] = html.unescape(match.group(1))

    def handle_data(self, data: str) -> None:
        if self._heading:
            self._heading_parts.append(data)
        if self._anchor is not None:
            self._anchor["parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h1" and self._heading:
            text = " ".join("".join(self._heading_parts).split())
            if self._heading == "gn":
                self.title_en = text
            else:
                self.title_jpn = text
            self._heading = ""
            self._heading_parts = []
        if tag != "a" or self._anchor is None:
            return
        item = self._anchor
        self._anchor = None
        href = str(item.get("href") or "").strip()
        label = " ".join("".join(item.get("parts") or []).split()) or str(item.get("title") or "").strip()
        parsed = urlparse(href)
        gallery = GALLERY_PATH_RE.fullmatch(parsed.path)
        if gallery and str(parsed.hostname or "").casefold() in EH_HOSTS:
            self.gallery_rows.append({
                "gid": gallery.group(1),
                "token": gallery.group(2).lower(),
                "url": href,
                "title": label,
                "thumbnail_url": str(item.get("image") or ""),
            })
        if "/s/" in parsed.path:
            self.page_links.append(href)
        if "gallerytorrents.php" in parsed.path:
            self.torrent_page_url = href
        if "archiver.php" in parsed.path:
            self.archive_url = href
        if parsed.path.casefold().endswith(".torrent"):
            self.torrent_links.append(href)
        classes = set(str(item.get("class") or "").split())
        if any(name == "gt" or name.startswith("gtl") for name in classes) and label:
            self.tags.append(label)


class _FavoritePopupParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.category: int | None = None
        self.is_favorited = False
        self.note = ""
        self.categories: dict[int, str] = {}
        self._in_note = False
        self._note_parts: list[str] = []
        self._category_capture: tuple[int, list[str]] | None = None

    def _finish_category_capture(self) -> None:
        if self._category_capture is None:
            return
        category, parts = self._category_capture
        label = " ".join("".join(parts).split())
        if label:
            self.categories[category] = label
        self._category_capture = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): str(value or "") for key, value in attrs}
        if tag == "input" and values.get("name") == "favcat":
            value = values.get("value", "")
            input_id = values.get("id", "")
            self._finish_category_capture()
            if value == "favdel" or input_id == "favdel":
                self.is_favorited = True
            if "checked" in values and value.isdigit() and 0 <= int(value) <= 9:
                self.category = int(value)
                self.is_favorited = True
            if value.isdigit() and 0 <= int(value) <= 9:
                self._category_capture = (int(value), [])
        elif tag == "textarea" and values.get("name") == "favnote":
            self._finish_category_capture()
            self._in_note = True
            self._note_parts = []

    def handle_data(self, data: str) -> None:
        if self._in_note:
            self._note_parts.append(data)
        if self._category_capture is not None:
            self._category_capture[1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea" and self._in_note:
            self.note = "".join(self._note_parts)
            self._in_note = False


class _ImagePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.image_url = ""
        self.next_page_url = ""
        self.original_url = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).casefold(): html.unescape(str(value or "")) for key, value in attrs}
        if tag == "img" and values.get("id") == "img":
            self.image_url = values.get("src", "")
        elif tag == "a":
            href = values.get("href", "")
            if values.get("id") == "next":
                self.next_page_url = href
            if urlparse(href).path.casefold().endswith("/fullimg.php"):
                self.original_url = href


def _parse_html(page: str) -> _EhHtmlParser:
    parser = _EhHtmlParser()
    parser.feed(str(page or ""))
    parser.close()
    return parser


def parse_search_html(page: str, base_url: str) -> list[dict]:
    parser = _parse_html(page)
    rows: list[dict] = []
    seen: set[str] = set()
    for item in parser.gallery_rows:
        url = urljoin(base_url, str(item.get("url") or ""))
        key = f"{item.get('gid')}/{item.get('token')}"
        if key in seen:
            continue
        seen.add(key)
        rows.append({**item, "url": url, "target": url, "input_kind": "gallery"})
    return rows


def parse_gallery_html(page: str, base_url: str) -> dict:
    parser = _parse_html(page)
    return {
        "title": parser.title_en or parser.title_jpn,
        "title_jpn": parser.title_jpn,
        "thumbnail_url": urljoin(base_url, parser.thumbnail_url) if parser.thumbnail_url else "",
        "tags": list(dict.fromkeys(parser.tags)),
        "page_links": list(dict.fromkeys(urljoin(base_url, value) for value in parser.page_links)),
        "torrent_page_url": urljoin(base_url, parser.torrent_page_url) if parser.torrent_page_url else "",
        "archive_url": urljoin(base_url, parser.archive_url) if parser.archive_url else "",
        "torrent_links": list(dict.fromkeys(urljoin(base_url, value) for value in parser.torrent_links)),
    }


def parse_image_page_html(page: str, base_url: str) -> dict[str, str]:
    parser = _ImagePageParser()
    parser.feed(str(page or ""))
    parser.close()
    return {
        "image_url": urljoin(base_url, parser.image_url) if parser.image_url else "",
        "next_page_url": urljoin(base_url, parser.next_page_url) if parser.next_page_url else "",
        # Exposed only as availability. The downloader never requests this URL.
        "original_url": urljoin(base_url, parser.original_url) if parser.original_url else "",
    }


def _allowed_display_image_url(url: str) -> bool:
    parsed = urlparse(str(url or ""))
    host = str(parsed.hostname or "").casefold().rstrip(".")
    return parsed.scheme == "https" and (
        host == "hath.network"
        or host.endswith(".hath.network")
        or host == "ehgt.org"
        or host.endswith(".ehgt.org")
    )


def _posted_iso(value: object) -> str:
    try:
        return datetime.fromtimestamp(int(str(value)), timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _contains_ai(tags: object) -> bool:
    return any(marker in str(tag).casefold() for tag in (tags or []) for marker in AI_TAG_MARKERS)


class EhentaiClient:
    def __init__(self, cookie_file: Path | str | None = None, proxy_url: str = "",
                 session: requests.Session | None = None, *, site: str = "e-hentai") -> None:
        self.site = "exhentai" if str(site).casefold().startswith("ex") else "e-hentai"
        self.cookie_file = Path(cookie_file) if cookie_file else None
        self.session = session or make_session(
            referer=f"https://{self.site}.org/",
            cookie_file=None,
            proxy_url=proxy_url,
            extra_headers={"Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8"},
        )
        if session is None:
            # Keep the two identities isolated and never send either login to
            # the image CDN or torrent tracker.  Name-only cookies loaded into
            # requests would otherwise be attached to every host in the session.
            cookie_domain = ".exhentai.org" if self.site == "exhentai" else ".e-hentai.org"
            for name, value in load_cookie_file(cookie_file).items():
                self.session.cookies.set(name, value, domain=cookie_domain, path="/", secure=True)

    def _get_text(self, url: str, *, timeout: tuple[int, int] = (10, 45)) -> str:
        response = self.session.get(url, timeout=timeout)
        if response.status_code == 429:
            raise RuntimeError("EH 请求过于频繁；官方搜索限制要求至少间隔约 3 秒")
        if response.status_code in {403, 451, 509}:
            raise RuntimeError(f"EH 返回 HTTP {response.status_code}；请检查代理、访问额度或站点限制")
        response.raise_for_status()
        page = response.text
        host = str(urlparse(url).hostname or "").casefold()
        if host.endswith("exhentai.org") and not page.strip():
            raise PermissionError("ExHentai 返回空白页；当前 Cookie 没有里站访问权限")
        lowered = page[:200000].casefold()
        if "act=login" in str(response.url).casefold() or "name=\"ips_username\"" in lowered:
            raise PermissionError("EH 登录状态无效；请重新导入 Cookie")
        return page

    def gallery_metadata_many(self, targets: list[EhentaiTarget]) -> dict[str, dict]:
        pairs = [[int(item.gid), item.token] for item in targets[:25] if item.kind == "gallery"]
        if not pairs:
            return {}
        response = self.session.post(
            "https://api.e-hentai.org/api.php",
            json={"method": "gdata", "gidlist": pairs, "namespace": 1},
            timeout=(10, 45),
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("gmetadata") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise RuntimeError("E-Hentai API 未返回画廊元数据列表")
        result: dict[str, dict] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            gid = str(row.get("gid") or "")
            token = str(row.get("token") or "")
            result[f"{gid}/{token}"] = row
            if gid and f"{gid}/" not in result:
                result[f"{gid}/"] = row
        return result

    def gallery_metadata(self, target: EhentaiTarget) -> dict:
        rows = self.gallery_metadata_many([target])
        row = rows.get(f"{target.gid}/{target.token}") or rows.get(f"{target.gid}/") or {}
        if row.get("error"):
            raise RuntimeError(f"E-Hentai API 拒绝画廊令牌：{row.get('error')}")
        return dict(row)

    def _require_login(self, action: str) -> None:
        if not {"ipb_member_id", "ipb_pass_hash"}.issubset(self.session.cookies.get_dict()):
            raise PermissionError(f"{action}需要当前站点的 ipb_member_id 与 ipb_pass_hash Cookie")

    def _favorite_popup_url(self, target: EhentaiTarget) -> str:
        if target.kind != "gallery" or target.site != self.site:
            raise ValueError("收藏操作需要与当前表站/里站会话一致的具体画廊链接")
        return f"{target.origin}/gallerypopups.php?{urlencode({'gid': target.gid, 't': target.token, 'act': 'addfav'})}"

    def favorite_state(self, target: EhentaiTarget) -> dict[str, object]:
        self._require_login("读取 EH 收藏状态")
        page = self._get_text(self._favorite_popup_url(target))
        parser = _FavoritePopupParser()
        parser.feed(page)
        parser.close()
        lowered = page.casefold()
        if "favcat" not in lowered or "favnote" not in lowered:
            raise RuntimeError("EH 收藏页面没有返回可识别表单；Cookie 可能失效或页面结构已变化")
        return {
            "favorited": parser.is_favorited,
            "category": parser.category,
            "note": parser.note,
            "categories": [parser.categories.get(index, f"分类 {index}") for index in range(10)],
        }

    def set_favorite(self, target: EhentaiTarget, category: int | None, note: str = "") -> dict[str, object]:
        self._require_login("修改 EH 收藏")
        if category is not None and (not isinstance(category, int) or isinstance(category, bool) or not 0 <= category <= 9):
            raise ValueError("EH 收藏分类必须是 0–9，移除收藏时使用空分类")
        normalized_note = str(note or "").strip()
        if len(normalized_note.encode("utf-8")) > 200:
            raise ValueError("EH 收藏注释按 UTF-8 编码后不能超过 200 字节")
        before = self.favorite_state(target)
        desired_favorited = category is not None
        if desired_favorited and before["favorited"] and before["category"] == category and before["note"] == normalized_note:
            return {**before, "changed": False}
        if not desired_favorited and not before["favorited"]:
            return {**before, "changed": False}

        endpoint = self._favorite_popup_url(target)
        response = self.session.post(
            endpoint,
            data={
                "favcat": str(category) if desired_favorited else "favdel",
                "favnote": normalized_note if desired_favorited else "",
                "apply": "Apply Changes" if before["favorited"] else "Add to Favorites",
                "update": "1",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=(10, 45),
        )
        if response.status_code in {401, 403}:
            raise PermissionError("EH 收藏修改被拒绝；请重新获取当前站点 Cookie")
        if response.status_code == 429:
            raise RuntimeError("EH 收藏修改请求过于频繁，请稍后重试")
        response.raise_for_status()
        after = self.favorite_state(target)
        verified = (
            after["favorited"]
            and after["category"] == category
            and after["note"] == normalized_note
            if desired_favorited else not after["favorited"]
        )
        if not verified:
            raise RuntimeError("EH 收藏请求已发送，但重新读取后状态未生效；未将其标记为成功")
        return {**after, "changed": True}

    def fetch_gallery(self, target: EhentaiTarget) -> dict:
        if target.kind != "gallery":
            raise ValueError("需要具体 EH 画廊链接")
        page = self._get_text(target.url)
        parsed = parse_gallery_html(page, target.url)
        if not parsed.get("title"):
            raise RuntimeError("EH 画廊页面没有返回可识别标题，可能已删除或被访问限制")
        return parsed

    @staticmethod
    def _validated_image_page_links(values: list[str], target: EhentaiTarget) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()
        for value in values:
            url = urljoin(target.url, str(value or ""))
            parsed = urlparse(url)
            match = IMAGE_PAGE_PATH_RE.fullmatch(parsed.path)
            host = str(parsed.hostname or "").casefold().rstrip(".")
            if parsed.scheme != "https" or host not in EH_HOSTS or not match or match.group(2) != target.gid:
                continue
            key = url.split("#", 1)[0]
            if key not in seen:
                seen.add(key)
                links.append(key)
        return links

    def gallery_image_page_links(self, target: EhentaiTarget, gallery: dict, file_count: int) -> list[str]:
        """Collect normal image-viewer pages without touching original/archive endpoints."""
        links = self._validated_image_page_links(list(gallery.get("page_links") or []), target)
        per_page = len(links)
        expected = max(0, int(file_count or 0))
        if per_page and expected > per_page:
            page_count = min(500, (expected + per_page - 1) // per_page)
            for page_index in range(1, page_count):
                page = self._get_text(f"{target.url}?{urlencode({'p': page_index})}")
                parsed = parse_gallery_html(page, target.url)
                more = self._validated_image_page_links(list(parsed.get("page_links") or []), target)
                if not more:
                    break
                known = set(links)
                links.extend(value for value in more if value not in known)
                if len(links) >= expected:
                    break
        if expected and len(links) < expected:
            raise RuntimeError(f"EH 画廊声明 {expected} 页，但只取得 {len(links)} 个图片页链接；未开始不完整下载")
        return links[:expected] if expected else links

    def _download_display_images(
        self,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
        target: EhentaiTarget,
        gallery: dict,
        metadata: dict,
        folder: Path,
        title: str,
        posted: str,
        tags: tuple[str, ...],
    ) -> None:
        file_count = int(metadata.get("filecount") or 0)
        maximum = max(0, min(int(task.options.get("eh_max_images") or 0), 10000))
        requested_count = min(file_count, maximum) if file_count and maximum else file_count
        page_links = self.gallery_image_page_links(target, gallery, requested_count)
        if maximum:
            page_links = page_links[:maximum]
        if not page_links:
            raise RuntimeError("EH 画廊没有返回可识别的图片阅读页链接")
        image_dir = folder / "images"
        for page_number, image_page_url in enumerate(page_links, 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            page = self._get_text(image_page_url)
            parsed = parse_image_page_html(page, image_page_url)
            image_url = str(parsed.get("image_url") or "")
            if not _allowed_display_image_url(image_url):
                raise RuntimeError(f"EH 第 {page_number} 页没有返回允许的 HTTPS 展示图地址")
            source_suffix = extension_from_url(
                image_url, ".jpg", allowed={".jpg", ".jpeg", ".png", ".gif", ".webp"}
            )
            output_suffix = normalized_media_extension("image/*", source_suffix, "image", task.options)
            destination = image_dir / f"{page_number:04d}{output_suffix}"
            source_path = (
                destination
                if output_suffix == source_suffix
                else image_dir / f".{page_number:04d}.source{source_suffix}"
            )
            try:
                download_file(self.session, image_url, source_path, cancel_event, timeout=(10, 120))
                with Image.open(source_path) as opened:
                    opened.verify()
                if source_path != destination:
                    convert_media_file(source_path, destination, output_suffix)
            finally:
                if source_path != destination:
                    source_path.unlink(missing_ok=True)
            size = destination.stat().st_size
            callbacks.on_file(FileRecord(
                destination.resolve(), task.module_id, task.task_id, "image", size,
                title=title, source_url=image_page_url, source_id=target.gid,
                author_name=str(metadata.get("uploader") or ""), published_at=posted, tags=tags,
                chapter=f"第 {page_number} 页",
                metadata={
                    "kind": "eh_display_image", "site": target.site, "page": page_number,
                    "original_available": bool(parsed.get("original_url")), "uses_original": False,
                },
            ))
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "info", f"已保存 EH 展示图 {page_number}/{len(page_links)}",
                current=page_number, total=len(page_links),
            ))

    def preview(self, target: EhentaiTarget, *, live: bool = False) -> TargetPreview:
        if target.kind != "gallery":
            label = "收藏夹" if target.kind == "favorites" else "搜索"
            return TargetPreview(
                "ehentai", target.url, target.url,
                title=target.query or f"EH {label}",
                description=f"本地识别为 {target.site} {label}目标；在线操作会读取候选，不会自动购买站点归档。",
                metadata={"input_kind": target.kind, "site": target.site, "browser_destination": target.url},
            )
        if not live:
            return TargetPreview(
                "ehentai", target.url, target.url, title=f"EH {target.gid}",
                description="已识别画廊 ID 与令牌；在线资料使用官方元数据 API 并核对画廊页面。",
                metadata={"input_kind": "gallery", "site": target.site, "target_id": target.gid,
                          "browser_destination": target.url},
            )
        metadata = self.gallery_metadata(target)
        page = self.fetch_gallery(target)
        tags = list(metadata.get("tags") or page.get("tags") or [])
        torrent_count = int(metadata.get("torrentcount") or len(metadata.get("torrents") or []))
        posted = _posted_iso(metadata.get("posted"))
        warnings = ["官方 ZIP 归档可能扣除 GP/Credits；软件不会自动购买"]
        if target.site == "exhentai":
            warnings.append("里站访问依赖当前账号权限及 ipb/igneous Cookie")
        return TargetPreview(
            "ehentai", target.url, target.url,
            title=str(metadata.get("title") or page.get("title") or f"EH {target.gid}"),
            description=(
                f"{metadata.get('category') or 'Gallery'} · {metadata.get('filecount') or '?'} 页 · "
                f"种子 {torrent_count} 个 · 发布 {posted or '未知'}"
            ),
            warnings=warnings,
            metadata={
                "input_kind": "gallery", "site": target.site, "target_id": target.gid,
                "gallery_token": target.token, "author_name": str(metadata.get("uploader") or ""),
                "published_at": posted, "tags": tags, "file_count": int(metadata.get("filecount") or 0),
                "file_size": int(metadata.get("filesize") or 0), "torrent_count": torrent_count,
                "has_torrent": bool(torrent_count or page.get("torrent_page_url")),
                "archive_available": bool(page.get("archive_url")),
                "thumbnail_url": str(metadata.get("thumb") or page.get("thumbnail_url") or ""),
                "browser_destination": target.url,
            },
        )

    def search(self, query: str, mode: str, limit: int = 20, *, filter_ai: bool = False) -> list[dict]:
        mode_text = str(mode or "表站关键词")
        site = "exhentai" if "里站" in mode_text else "e-hentai"
        kind = "favorites" if "收藏夹" in mode_text else "search"
        normalized_query = "" if str(query).strip() in {"全部", "*"} else str(query).strip()
        target = EhentaiTarget(kind, site, query=normalized_query)
        cookies = self.session.cookies.get_dict()
        if kind == "favorites" and not {"ipb_member_id", "ipb_pass_hash"}.issubset(cookies):
            raise PermissionError("读取 EH 收藏夹需要当前站点的 ipb_member_id 与 ipb_pass_hash Cookie")
        global _LAST_SEARCH_AT
        with _SEARCH_LOCK:
            remaining = 3.05 - (time.monotonic() - _LAST_SEARCH_AT)
            if remaining > 0:
                time.sleep(remaining)
            page = self._get_text(target.url)
            _LAST_SEARCH_AT = time.monotonic()
        rows = parse_search_html(page, target.origin)
        targets = [parse_ehentai_target(row["url"]) for row in rows[:25]]
        metadata = self.gallery_metadata_many(targets) if targets else {}
        result: list[dict] = []
        for row in rows:
            detail = metadata.get(f"{row['gid']}/{row['token']}") or metadata.get(f"{row['gid']}/") or {}
            tags = list(detail.get("tags") or [])
            if filter_ai and _contains_ai(tags):
                continue
            posted = _posted_iso(detail.get("posted"))
            result.append({
                **row,
                "title": str(detail.get("title") or row.get("title") or f"EH {row['gid']}"),
                "source": f"{site} {'收藏夹' if kind == 'favorites' else '搜索'}",
                "author_name": str(detail.get("uploader") or ""),
                "published_at": posted,
                "tags": tags,
                "thumbnail_url": str(detail.get("thumb") or row.get("thumbnail_url") or ""),
                "torrent_count": int(detail.get("torrentcount") or 0),
                "downloadable": True,
            })
            if len(result) >= max(1, min(int(limit), 25)):
                break
        return result

    def _torrent_links(self, gallery: dict) -> list[str]:
        page_url = str(gallery.get("torrent_page_url") or "")
        if not page_url:
            return []
        page = self._get_text(page_url)
        parsed = parse_gallery_html(page, page_url)
        links: list[str] = []
        for value in parsed.get("torrent_links") or []:
            url = str(value)
            host = str(urlparse(url).hostname or "").casefold()
            if urlparse(url).scheme == "https" and (host == "ehtracker.org" or host.endswith(".ehtracker.org") or host in EH_HOSTS):
                links.append(url)
        return list(dict.fromkeys(links))

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        target = parse_ehentai_target(task.target)
        if target.kind != "gallery":
            raise ValueError("请先从 EH 搜索/收藏夹候选中选择具体画廊")
        if cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        method = str(task.options.get("eh_download_method") or "metadata").casefold()
        if method == "archive":
            raise RuntimeError("官方 ZIP 归档可能扣除 GP/Credits；当前版本不会自动购买，请在浏览器确认费用")
        callbacks.on_progress(ProgressEvent(
            task.task_id, task.module_id, "info", "正在读取 EH 官方元数据并核对画廊页面",
        ))
        metadata = self.gallery_metadata(target)
        gallery = self.fetch_gallery(target)
        tags = tuple(str(value) for value in (metadata.get("tags") or gallery.get("tags") or []))
        if bool(task.options.get("filter_ai")) and _contains_ai(tags):
            raise RuntimeError("该 EH 画廊含 AI 生成/辅助标签，已按设置跳过")
        title = str(metadata.get("title") or gallery.get("title") or f"EH {target.gid}")
        root = platform_output_root(task.output_dir, "ehentai")
        folder = root / safe_component(f"{target.gid} - {title}", max_length=100)
        documents = folder / "documents"
        documents.mkdir(parents=True, exist_ok=True)
        posted = _posted_iso(metadata.get("posted"))
        record_payload = {
            "schema": 1,
            "platform": target.site,
            "source_url": target.url,
            "gid": target.gid,
            "token": target.token,
            "title": title,
            "title_jpn": str(metadata.get("title_jpn") or gallery.get("title_jpn") or ""),
            "category": str(metadata.get("category") or ""),
            "uploader": str(metadata.get("uploader") or ""),
            "posted_at": posted,
            "file_count": int(metadata.get("filecount") or 0),
            "file_size": int(metadata.get("filesize") or 0),
            "rating": str(metadata.get("rating") or ""),
            "torrent_count": int(metadata.get("torrentcount") or len(metadata.get("torrents") or [])),
            "tags": list(tags),
            "archive_available": bool(gallery.get("archive_url")),
            "torrent_available": bool(gallery.get("torrent_page_url")),
            "saved_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_path = documents / f"{target.gid}.json"
        temporary = metadata_path.with_name(f".{metadata_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(record_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(metadata_path)
        finally:
            temporary.unlink(missing_ok=True)
        callbacks.on_file(FileRecord(
            metadata_path.resolve(), task.module_id, task.task_id, "text", metadata_path.stat().st_size,
            title=title, source_url=target.url, source_id=target.gid,
            author_name=str(metadata.get("uploader") or ""), published_at=posted, tags=tags,
            metadata={"kind": "eh_metadata", "site": target.site},
        ))
        callbacks.on_progress(ProgressEvent(
            task.task_id, task.module_id, "info", f"已保存 EH 元数据：{metadata_path.name}", current=1, total=1,
        ))
        download_images = bool(task.options.get("eh_download_images", method == "images"))
        bt_download_enabled = bool(task.options.get("eh_bt_download_enabled", False))
        download_torrent = bool(task.options.get("eh_download_torrent", method == "torrent")) or bt_download_enabled
        if download_images:
            self._download_display_images(
                task, callbacks, cancel_event, target, gallery, metadata, folder, title, posted, tags,
            )
        if not download_torrent:
            return
        links = self._torrent_links(gallery)
        if not links:
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "warning", "当前画廊没有可下载种子；已保留元数据",
            ))
            return
        maximum = max(1, min(int(task.options.get("max_torrents") or 1), 10))
        torrent_dir = folder / "torrents"
        for index, url in enumerate(reversed(links[-maximum:]), 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            filename = safe_component(Path(urlparse(url).path).name or f"{target.gid}-{index}.torrent", max_length=100)
            if not filename.casefold().endswith(".torrent"):
                filename += ".torrent"
            destination = torrent_dir / filename
            size = download_file(self.session, url, destination, cancel_event, timeout=(10, 90))
            try:
                torrent_summary = parse_torrent(destination.read_bytes())
            except (OSError, ValueError) as exc:
                destination.unlink(missing_ok=True)
                raise RuntimeError("EH 种子响应不是结构完整且安全的 BitTorrent 元数据") from exc
            callbacks.on_file(FileRecord(
                destination.resolve(), task.module_id, task.task_id, "other", size,
                title=title, source_url=target.url, source_id=target.gid,
                author_name=str(metadata.get("uploader") or ""), published_at=posted, tags=tags,
                metadata={
                    "kind": "torrent", "site": target.site,
                    "torrent_info_hash": torrent_summary.info_hash,
                    "torrent_file_count": torrent_summary.file_count,
                    "torrent_total_size": torrent_summary.total_size,
                    "torrent_piece_count": torrent_summary.piece_count,
                    "torrent_tracker_count": torrent_summary.tracker_count,
                    "torrent_private": torrent_summary.private,
                },
            ))
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "info", f"已保存种子文件 {index}/{min(maximum, len(links))}",
                current=index, total=min(maximum, len(links)),
            ))
            if not bt_download_enabled:
                continue
            bt_output = folder / "bt-content" / torrent_summary.info_hash
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "info", "已交给 aria2 下载种子内容；BT 下载期间可能产生上传流量",
            ))
            try:
                result = download_torrent_with_aria2(
                    destination,
                    bt_output,
                    cancel_event,
                    configured_path=str(task.options.get("aria2_path") or ""),
                )
            except Aria2Error as exc:
                raise RuntimeError(str(exc)) from exc
            for downloaded in result.files:
                callbacks.on_file(FileRecord(
                    downloaded, task.module_id, task.task_id, classify_file(downloaded), downloaded.stat().st_size,
                    title=title, source_url=target.url, source_id=target.gid,
                    author_name=str(metadata.get("uploader") or ""), published_at=posted, tags=tags,
                    metadata={
                        "kind": "torrent_content", "site": target.site,
                        "torrent_info_hash": torrent_summary.info_hash,
                    },
                ))
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "info",
                f"aria2 已完成种子内容下载：{len(result.files)} 个文件",
            ))
