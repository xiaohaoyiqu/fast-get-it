from __future__ import annotations

import html
import json
import re
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview, classify_file
from software_app.crawlers.common import (
    convert_media_file,
    download_file,
    extension_from_url,
    make_session,
    normalized_media_extension,
    platform_output_root,
    safe_component,
)


INSTAGRAM_RESERVED = {"accounts", "direct", "explore", "p", "reel", "reels", "stories", "tv"}
USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


@dataclass(frozen=True)
class InstagramTarget:
    kind: str
    username: str = ""
    shortcode: str = ""

    @property
    def url(self) -> str:
        if self.kind in {"post", "reel", "tv"}:
            return f"https://www.instagram.com/{self.kind}/{self.shortcode}/"
        return f"https://www.instagram.com/{self.username}/"


def parse_instagram_target(raw_target: str) -> InstagramTarget:
    value = str(raw_target or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("Instagram 目标不能为空")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        host = str(parsed.hostname or "").lower()
        if parsed.scheme != "https" or host not in {"instagram.com", "www.instagram.com"}:
            raise ValueError("Instagram 目标必须使用 https://www.instagram.com")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 2 and parts[0] in {"p", "reel", "tv"}:
            return InstagramTarget("post" if parts[0] == "p" else parts[0], shortcode=parts[1])
        if len(parts) == 1 and parts[0].lower() not in INSTAGRAM_RESERVED:
            return InstagramTarget("profile", username=parts[0])
        raise ValueError("请输入 Instagram 账号、帖子或 Reels 链接")
    username = value.lstrip("@").strip()
    if not USERNAME_RE.fullmatch(username) or username.lower() in INSTAGRAM_RESERVED:
        raise ValueError("Instagram 用户名格式无效")
    return InstagramTarget("profile", username=username)


def _safe_media_url(value: object) -> str:
    url = html.unescape(str(value or "")).replace("\\u0026", "&").strip()
    parsed = urlparse(url)
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return ""
    if not (host == "instagram.com" or host.endswith(".instagram.com") or
            host == "cdninstagram.com" or host.endswith(".cdninstagram.com") or
            host == "fbcdn.net" or host.endswith(".fbcdn.net")):
        return ""
    return url


class _InstagramHtml(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta: dict[str, str] = {}
        self.scripts: list[str] = []
        self._script_type = ""
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if tag == "meta":
            key = values.get("property") or values.get("name")
            if key and values.get("content"):
                self.meta[key.lower()] = values["content"]
        elif tag == "script":
            self._script_type = values.get("type", "")
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._script_type in {"application/json", "application/ld+json"}:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script_type:
            text = "".join(self._parts).strip()
            if text:
                self.scripts.append(text)
            self._script_type = ""
            self._parts = []


def _json_media(payload: object, context: dict | None = None) -> list[dict]:
    context = dict(context or {})
    result: list[dict] = []
    if isinstance(payload, list):
        for item in payload:
            result.extend(_json_media(item, context))
        return result
    if not isinstance(payload, dict):
        return result
    local = dict(context)
    owner = payload.get("owner")
    if isinstance(owner, dict):
        local["author"] = str(owner.get("username") or local.get("author") or "")
        local["author_id"] = str(owner.get("id") or local.get("author_id") or "")
    for key in ("shortcode", "code"):
        if payload.get(key):
            local["post_id"] = str(payload[key])
            break
    timestamp = payload.get("taken_at_timestamp") or payload.get("taken_at")
    if timestamp:
        local["published_at"] = str(timestamp)
    caption = payload.get("caption")
    if isinstance(caption, dict):
        local["label"] = str(caption.get("text") or "")
    elif isinstance(caption, str):
        local["label"] = caption
    schema_type = str(payload.get("@type") or "").lower()
    video_value = payload.get("video_url")
    image_value = payload.get("display_url") or payload.get("image")
    if schema_type.endswith("videoobject"):
        video_value = video_value or payload.get("contentUrl")
    if schema_type.endswith("imageobject"):
        image_value = image_value or payload.get("contentUrl")
    video = _safe_media_url(video_value)
    image = _safe_media_url(image_value)
    if video:
        result.append({**local, "type": "video", "url": video,
                       "thumb": _safe_media_url(payload.get("display_url") or payload.get("thumbnailUrl"))})
    elif image:
        result.append({**local, "type": "image", "url": image, "thumb": image})
    for value in payload.values():
        if isinstance(value, (dict, list)):
            result.extend(_json_media(value, local))
    return result


def parse_instagram_html(page: str, source_url: str) -> dict:
    parser = _InstagramHtml()
    parser.feed(str(page or ""))
    parser.close()
    title = parser.meta.get("og:title", "").strip()
    description = parser.meta.get("og:description", "").strip()
    media: list[dict] = []
    video = _safe_media_url(parser.meta.get("og:video") or parser.meta.get("og:video:secure_url"))
    image = _safe_media_url(parser.meta.get("og:image"))
    post_match = re.search(r"/(?:p|reel|tv)/([^/]+)", source_url)
    base = {"post_id": post_match.group(1) if post_match else "", "author": "", "label": description}
    if video:
        media.append({**base, "type": "video", "url": video, "thumb": image})
    elif image and post_match:
        media.append({**base, "type": "image", "url": image, "thumb": image})
    for script in parser.scripts:
        try:
            payload = json.loads(script)
        except (json.JSONDecodeError, ValueError):
            continue
        media.extend(_json_media(payload))
    unique: list[dict] = []
    seen: set[str] = set()
    for item in media:
        url = str(item.get("url") or "")
        if url and url not in seen:
            seen.add(url)
            unique.append(item)
    username = ""
    match = re.search(r"@([A-Za-z0-9._]{1,30})", title + " " + description)
    if match:
        username = match.group(1)
    return {"title": title, "description": description, "username": username,
            "avatar_url": image if not post_match else "", "media": unique}


class InstagramClient:
    def __init__(self, cookie_file: Path | str | None = None, proxy_url: str = "",
                 session: requests.Session | None = None) -> None:
        self.session = session or make_session(
            referer="https://www.instagram.com/", cookie_file=cookie_file, proxy_url=proxy_url,
            extra_headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
        )

    def fetch(self, target: InstagramTarget) -> dict:
        response = self.session.get(target.url, timeout=(10, 45))
        if response.status_code == 429:
            raise RuntimeError("Instagram 请求过于频繁，请稍后重试")
        response.raise_for_status()
        page = response.text
        final_path = urlparse(str(response.url)).path.lower()
        if "/accounts/login" in final_path or 'name="login"' in page[:300000].lower():
            raise PermissionError("Instagram 要求登录；请导入当前浏览器的 Instagram Cookie")
        if "challenge" in final_path:
            raise PermissionError("Instagram 要求在浏览器中完成账号验证")
        payload = parse_instagram_html(page, target.url)
        if not payload.get("title") and not payload.get("media"):
            raise RuntimeError("Instagram 页面没有返回可识别资料，页面结构或登录状态可能已变化")
        return payload

    def preview(self, target: InstagramTarget, live: bool = False) -> TargetPreview:
        if not live:
            label = "帖子详情页" if target.kind == "post" else "Reels 页面" if target.kind == "reel" else "账号主页"
            return TargetPreview("instagram", target.url, target.url, title=target.username or target.shortcode,
                                 description=f"本地识别为 Instagram {label}；在线资料会读取页面元数据和已加载媒体。",
                                 metadata={"input_kind": target.kind, "browser_destination": target.url})
        payload = self.fetch(target)
        media = payload.get("media") or []
        return TargetPreview(
            "instagram", target.url, target.url, title=str(payload.get("title") or target.username or target.shortcode),
            description=str(payload.get("description") or f"已识别 {len(media)} 个媒体"),
            metadata={"input_kind": target.kind, "target_id": target.shortcode or target.username,
                      "author_name": payload.get("username") or target.username,
                      "avatar_url": payload.get("avatar_url") or "",
                      "media_count": len(media), "thumbnail_url": next((item.get("thumb") for item in media if item.get("thumb")), ""),
                      "browser_destination": target.url},
        )

    def search(self, query: str, mode: str) -> list[dict]:
        target = parse_instagram_target(query)
        if mode.startswith("帖子") and target.kind not in {"post", "tv"}:
            raise ValueError("帖子入口需要 /p/{shortcode} 链接")
        if mode.startswith("Reels") and target.kind != "reel":
            raise ValueError("Reels 入口需要 /reel/{shortcode} 链接")
        preview = self.preview(target, live=False)
        return [{"id": target.shortcode or target.username, "target": target.url, "url": target.url,
                 "title": preview.title, "source": f"Instagram {mode}", "input_kind": target.kind}]

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        target = parse_instagram_target(task.target)
        payload = self.fetch(target)
        media = list(payload.get("media") or [])
        if not media:
            if target.kind == "profile":
                raise RuntimeError("账号主页未返回已加载帖子媒体；请使用具体帖子/Reels 链接，或先在浏览器登录")
            raise RuntimeError("当前 Instagram 页面没有可下载媒体")
        limit = max(1, min(int(task.options.get("max_works") or 30), 100))
        types = set(str(task.options.get("types") or "1234").replace(",", ""))
        root = platform_output_root(task.output_dir, "instagram")
        author = str(payload.get("username") or target.username or media[0].get("author") or "unknown")
        folder = root / safe_component(author) / safe_component(target.shortcode or "profile", max_length=80)
        completed = 0
        errors: list[str] = []
        for index, item in enumerate(media[:limit], 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            kind = str(item.get("type") or "")
            if (kind == "image" and "1" not in types) or (kind == "video" and "2" not in types):
                continue
            url = _safe_media_url(item.get("url"))
            if not url:
                continue
            try:
                record = self._download_media(task, item, url, folder, index, author, target, cancel_event)
            except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
                errors.append(str(exc))
                callbacks.on_progress(ProgressEvent(task.task_id, "instagram", "warning", f"媒体 {index} 下载失败：{exc}"))
                continue
            completed += 1
            callbacks.on_file(record)
            callbacks.on_progress(ProgressEvent(task.task_id, "instagram", "info",
                f"已保存 {completed}/{min(len(media), limit)}：{record.path.name}", current=completed,
                total=min(len(media), limit)))
        if not completed:
            raise RuntimeError(errors[0] if errors else "目标没有符合下载类型的媒体")

    def _download_media(self, task: DownloadTask, item: dict, url: str, folder: Path, index: int,
                        author: str, target: InstagramTarget, cancel_event: threading.Event) -> FileRecord:
        folder.mkdir(parents=True, exist_ok=True)
        media_type = str(item.get("type") or "image")
        default = ".mp4" if media_type == "video" else ".jpg"
        allowed = {".mp4", ".mov"} if media_type == "video" else {".jpg", ".jpeg", ".png", ".webp", ".gif"}
        source_suffix = extension_from_url(url, default, allowed)
        output_suffix = normalized_media_extension(
            "video/mp4" if media_type == "video" else "image/jpeg", source_suffix, media_type, task.options,
        )
        stem = safe_component(f"{item.get('published_at') or 'unknown'}_{target.shortcode or item.get('post_id') or 'media'}_{index:02d}")
        source = folder / f".{stem}.source{source_suffix}"
        destination = folder / f"{stem}{output_suffix}"
        download_file(self.session, url, source, cancel_event)
        convert_media_file(source, destination, output_suffix)
        return FileRecord(destination, "instagram", task.task_id, classify_file(destination), destination.stat().st_size,
                          str(item.get("label") or payload_title(author, target)), url,
                          target.shortcode or str(item.get("post_id") or ""), str(item.get("author_id") or author), author,
                          published_at=str(item.get("published_at") or ""), metadata={"post_url": target.url})


def payload_title(author: str, target: InstagramTarget) -> str:
    return f"{author} · {target.shortcode or target.username}"
