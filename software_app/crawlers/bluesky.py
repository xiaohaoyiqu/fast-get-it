from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.events import CallbackSet
from software_app.core.external_tools import find_ffmpeg
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


APPVIEW = "https://public.api.bsky.app"
HANDLE_RE = re.compile(r"^(?:did:[a-z0-9]+:[A-Za-z0-9._:%-]+|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?)$")
ALLOWED_MEDIA_HOSTS = {"cdn.bsky.app", "video.bsky.app", "video.cdn.bsky.app"}


@dataclass(frozen=True)
class BlueskyTarget:
    kind: str
    actor: str = ""
    post_id: str = ""
    query: str = ""

    @property
    def url(self) -> str:
        if self.kind == "post":
            return f"https://bsky.app/profile/{self.actor}/post/{self.post_id}"
        if self.kind == "profile":
            return f"https://bsky.app/profile/{self.actor}"
        return f"https://bsky.app/search?q={quote_plus(self.query)}"


def parse_bluesky_target(raw_target: str) -> BlueskyTarget:
    value = str(raw_target or "").strip().strip('"').strip("'")
    if not value:
        raise ValueError("Bluesky 目标不能为空")
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value)
        if parsed.scheme != "https" or parsed.hostname not in {"bsky.app", "www.bsky.app"}:
            raise ValueError("Bluesky 目标必须使用 https://bsky.app")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
            return BlueskyTarget("post", parts[1], parts[3])
        if len(parts) >= 2 and parts[0] == "profile":
            return BlueskyTarget("profile", parts[1])
        raise ValueError("请输入 Bluesky 主页或帖子链接")
    actor = value.lstrip("@").strip()
    if HANDLE_RE.fullmatch(actor) and (actor.startswith("did:") or "." in actor):
        return BlueskyTarget("profile", actor)
    return BlueskyTarget("search", query=value)


def _post_id(uri: str) -> str:
    return str(uri or "").rsplit("/", 1)[-1]


def _safe_media_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return ""
    if host not in ALLOWED_MEDIA_HOSTS and not host.endswith(".bsky.app"):
        return ""
    return url


def _media_from_embed(embed: object) -> list[dict]:
    if not isinstance(embed, dict):
        return []
    kind = str(embed.get("$type") or "")
    if "recordWithMedia" in kind:
        return _media_from_embed(embed.get("media"))
    result: list[dict] = []
    if "images" in kind:
        for image in embed.get("images") or []:
            if not isinstance(image, dict):
                continue
            url = _safe_media_url(image.get("fullsize") or image.get("thumb"))
            if url:
                result.append({"type": "image", "url": url, "thumb": image.get("thumb") or url,
                               "alt": str(image.get("alt") or "")})
    elif "video" in kind:
        playlist = _safe_media_url(embed.get("playlist"))
        if playlist:
            result.append({"type": "video", "url": playlist,
                           "thumb": _safe_media_url(embed.get("thumbnail")), "alt": ""})
    return result


def post_candidate(value: object) -> dict:
    post = value.get("post", value) if isinstance(value, dict) else {}
    if not isinstance(post, dict):
        return {}
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    record = post.get("record") if isinstance(post.get("record"), dict) else {}
    uri = str(post.get("uri") or "")
    rkey = _post_id(uri)
    handle = str(author.get("handle") or author.get("did") or "")
    if not handle or not rkey:
        return {}
    media = _media_from_embed(post.get("embed"))
    return {
        "id": rkey,
        "target": f"https://bsky.app/profile/{handle}/post/{rkey}",
        "url": f"https://bsky.app/profile/{handle}/post/{rkey}",
        "title": str(record.get("text") or "").strip()[:180] or f"Bluesky 帖子 {rkey}",
        "description": str(record.get("text") or "").strip(),
        "author_id": str(author.get("did") or ""),
        "author_name": str(author.get("displayName") or handle),
        "handle": handle,
        "avatar_url": str(author.get("avatar") or ""),
        "published_at": str(record.get("createdAt") or post.get("indexedAt") or ""),
        "media": media,
        "thumbnail_url": next((str(item.get("thumb") or item.get("url") or "") for item in media), ""),
        "source": "Bluesky 帖子",
        "input_kind": "post",
    }


class BlueskyClient:
    def __init__(self, proxy_url: str = "", session: requests.Session | None = None) -> None:
        self.proxy_url = str(proxy_url or "").strip()
        self.session = session or make_session(referer="https://bsky.app/", proxy_url=self.proxy_url)

    def get(self, endpoint: str, **params) -> dict:
        response = self.session.get(f"{APPVIEW}/xrpc/{endpoint}", params=params, timeout=(10, 45))
        if response.status_code == 429:
            raise RuntimeError("Bluesky 请求过于频繁，请稍后重试")
        if response.status_code == 403 and endpoint == "app.bsky.feed.searchPosts":
            raise PermissionError("Bluesky 当前拒绝此网络出口的帖子全文搜索；用户、公开关注、作者动态和具体帖子仍可使用")
        if response.status_code in {401, 403}:
            raise PermissionError("Bluesky 当前拒绝这个公开 API 请求，请稍后重试或检查代理")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("Bluesky 接口没有返回对象")
        return payload

    def profile(self, actor: str) -> dict:
        return self.get("app.bsky.actor.getProfile", actor=actor)

    def resolve_did(self, actor: str) -> str:
        if actor.startswith("did:"):
            return actor
        return str(self.profile(actor).get("did") or "")

    def post(self, actor: str, post_id: str) -> dict:
        did = self.resolve_did(actor)
        payload = self.get(
            "app.bsky.feed.getPostThread",
            uri=f"at://{did}/app.bsky.feed.post/{post_id}", depth=0, parentHeight=0,
        )
        thread = payload.get("thread") if isinstance(payload.get("thread"), dict) else {}
        return post_candidate(thread.get("post"))

    def search(self, query: str, mode: str, limit: int) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        if mode == "用户搜索":
            payload = self.get("app.bsky.actor.searchActors", q=query, limit=limit)
            rows = []
            for actor in payload.get("actors") or []:
                if not isinstance(actor, dict) or not actor.get("handle"):
                    continue
                handle = str(actor["handle"])
                rows.append({
                    "id": str(actor.get("did") or handle), "handle": handle,
                    "target": f"https://bsky.app/profile/{handle}", "url": f"https://bsky.app/profile/{handle}",
                    "title": str(actor.get("displayName") or handle), "bio": str(actor.get("description") or ""),
                    "avatar_url": str(actor.get("avatar") or ""), "author_id": str(actor.get("did") or ""),
                    "source": "Bluesky 用户搜索", "input_kind": "profile",
                })
            return rows
        if mode == "关注账号":
            payload = self.get("app.bsky.graph.getFollows", actor=query.lstrip("@"), limit=limit)
            rows = []
            for actor in payload.get("follows") or []:
                if not isinstance(actor, dict) or not actor.get("handle"):
                    continue
                handle = str(actor["handle"])
                rows.append({
                    "id": str(actor.get("did") or handle), "handle": handle,
                    "target": f"https://bsky.app/profile/{handle}", "url": f"https://bsky.app/profile/{handle}",
                    "title": str(actor.get("displayName") or handle), "bio": str(actor.get("description") or ""),
                    "avatar_url": str(actor.get("avatar") or ""), "author_id": str(actor.get("did") or ""),
                    "source": f"Bluesky {query} 的关注", "input_kind": "profile",
                })
            return rows
        payload = self.get("app.bsky.feed.searchPosts", q=query, limit=limit, sort="latest")
        return [row for row in (post_candidate(item) for item in payload.get("posts") or []) if row]

    def author_posts(self, actor: str, limit: int) -> list[dict]:
        remaining = max(1, min(int(limit), 500))
        cursor = ""
        rows: list[dict] = []
        while remaining > 0:
            params = {"actor": actor, "limit": min(100, remaining), "filter": "posts_with_media"}
            if cursor:
                params["cursor"] = cursor
            payload = self.get("app.bsky.feed.getAuthorFeed", **params)
            batch = [row for row in (post_candidate(item) for item in payload.get("feed") or []) if row]
            rows.extend(batch)
            remaining -= len(payload.get("feed") or [])
            cursor = str(payload.get("cursor") or "")
            if not cursor or not payload.get("feed"):
                break
        return rows

    def preview(self, target: BlueskyTarget, live: bool = False) -> TargetPreview:
        if not live:
            label = "帖子详情页" if target.kind == "post" else "用户主页" if target.kind == "profile" else "搜索页"
            return TargetPreview("bluesky", target.url, target.url, title=target.actor or target.query,
                                 description=f"本地识别为 Bluesky {label}；在线资料使用公开 AppView API。",
                                 metadata={"input_kind": target.kind, "browser_destination": target.url})
        if target.kind == "post":
            row = self.post(target.actor, target.post_id)
            if not row:
                raise ValueError("Bluesky 帖子不存在或当前不可见")
            return TargetPreview("bluesky", target.url, target.url, title=row["title"],
                                 description=row.get("description") or f"媒体 {len(row.get('media') or [])} 项",
                                 metadata={**row, "links": [], "browser_destination": target.url})
        profile = self.profile(target.actor)
        handle = str(profile.get("handle") or target.actor)
        links = re.findall(r"https?://[^\s<>()]+", str(profile.get("description") or ""))
        return TargetPreview(
            "bluesky", target.url, f"https://bsky.app/profile/{handle}",
            title=str(profile.get("displayName") or handle), description=str(profile.get("description") or ""),
            metadata={"input_kind": "profile", "author_id": str(profile.get("did") or ""),
                      "author_name": str(profile.get("displayName") or handle), "handle": handle,
                      "avatar_url": str(profile.get("avatar") or ""), "followers": profile.get("followersCount", 0),
                      "follows": profile.get("followsCount", 0), "posts": profile.get("postsCount", 0),
                      "links": [{"label": urlparse(url).hostname or "外链", "url": url.rstrip(".,)")} for url in links],
                      "browser_destination": f"https://bsky.app/profile/{handle}"},
        )

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        target = parse_bluesky_target(task.target)
        limit = int(task.options.get("max_works") or 100)
        if target.kind == "post":
            rows = [self.post(target.actor, target.post_id)]
        elif target.kind == "profile":
            rows = self.author_posts(target.actor, limit)
        else:
            rows = self.search(target.query, "帖子搜索", limit)
        rows = [row for row in rows if row]
        if not rows:
            raise RuntimeError("没有找到可下载的 Bluesky 帖子")
        types = set(str(task.options.get("types") or "1234").replace(",", ""))
        root = platform_output_root(task.output_dir, "bluesky")
        completed = 0
        errors: list[str] = []
        total = sum(len(row.get("media") or []) for row in rows)
        for row in rows:
            handle = str(row.get("handle") or row.get("author_name") or "unknown")
            folder = root / safe_component(handle) / safe_component(f"{row.get('id')}_{row.get('title')}", max_length=90)
            for index, media in enumerate(row.get("media") or [], 1):
                if cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                kind = str(media.get("type") or "")
                if (kind == "image" and "1" not in types) or (kind == "video" and "2" not in types):
                    continue
                try:
                    record = self._download_media(task, row, media, folder, index, cancel_event)
                except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
                    errors.append(str(exc))
                    callbacks.on_progress(ProgressEvent(task.task_id, "bluesky", "warning", f"媒体 {index} 下载失败：{exc}"))
                    continue
                completed += 1
                callbacks.on_file(record)
                callbacks.on_progress(ProgressEvent(task.task_id, "bluesky", "info",
                    f"已保存 {completed}/{total}：{record.path.name}", current=completed, total=total))
        if not completed:
            raise RuntimeError(errors[0] if errors else "目标没有符合下载类型的媒体")

    def _download_media(self, task: DownloadTask, row: dict, media: dict, folder: Path, index: int,
                        cancel_event: threading.Event) -> FileRecord:
        url = _safe_media_url(media.get("url"))
        if not url:
            raise ValueError("Bluesky 返回了不受信任的媒体地址")
        folder.mkdir(parents=True, exist_ok=True)
        stem = safe_component(f"{row.get('published_at') or 'unknown'}_{row.get('id')}_{index:02d}", max_length=100)
        if media.get("type") == "video":
            destination = folder / f"{stem}.mp4"
            self._download_hls(url, destination, cancel_event)
        else:
            source_suffix = extension_from_url(url, ".jpg", {".jpg", ".jpeg", ".png", ".webp", ".gif"})
            output_suffix = normalized_media_extension("image/jpeg", source_suffix, "image", task.options)
            source = folder / f".{stem}.source{source_suffix}"
            destination = folder / f"{stem}{output_suffix}"
            download_file(self.session, url, source, cancel_event)
            convert_media_file(source, destination, output_suffix)
        return FileRecord(destination, "bluesky", task.task_id, classify_file(destination), destination.stat().st_size,
                          str(row.get("title") or ""), url, str(row.get("id") or ""),
                          str(row.get("author_id") or row.get("handle") or ""), str(row.get("author_name") or ""),
                          published_at=str(row.get("published_at") or ""), metadata={"post_url": row.get("url") or ""})

    def _download_hls(self, url: str, destination: Path, cancel_event: threading.Event) -> None:
        ffmpeg = find_ffmpeg()
        if not ffmpeg:
            raise RuntimeError("下载 Bluesky 视频需要 FFmpeg")
        temporary = destination.with_name(destination.name + ".part.mp4")
        env = os.environ.copy()
        if self.proxy_url:
            proxy = self.proxy_url if "://" in self.proxy_url else f"http://{self.proxy_url}"
            env.update({"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy})
        process = subprocess.Popen(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", url, "-c", "copy", str(temporary)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, env=env,
        )
        try:
            while process.poll() is None:
                if cancel_event.wait(0.2):
                    process.terminate()
                    raise TaskCancelled("任务已取消")
            stderr = process.stderr.read() if process.stderr else ""
            if process.returncode != 0 or not temporary.is_file():
                raise RuntimeError((stderr.strip().splitlines() or ["FFmpeg 下载视频失败"])[-1])
            os.replace(temporary, destination)
        finally:
            if process.poll() is None:
                process.kill()
            temporary.unlink(missing_ok=True)
