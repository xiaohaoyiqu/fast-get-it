from __future__ import annotations

import ipaddress
import mimetypes
import os
import re
import socket
import threading
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse, urlunparse

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.blocklist import BlocklistStore, work_from_target
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview, classify_file
from software_app.crawlers.common import (
    convert_media_file,
    make_session,
    normalized_media_extension,
    platform_output_root,
    safe_component,
)


DIRECT_MEDIA_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif",
    ".mp4", ".webm", ".mov", ".mkv", ".m4v",
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg",
}
CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/avif": ".avif",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/ogg": ".ogg",
    "audio/wav": ".wav",
}
MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5
HTTP_REDIRECT_STATUSES = {301, 302, 303, 307, 308}


@dataclass(frozen=True)
class MediaCandidate:
    url: str
    kind: str


def normalize_page_url(raw_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        raise ValueError("网页地址不能为空")
    initial = urlparse(value)
    if initial.scheme and initial.scheme.lower() not in {"http", "https"}:
        raise ValueError("只支持 http/https 网页地址")
    if not initial.scheme:
        value = f"https://{value}"
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("只支持 http/https 网页地址")
    if parsed.username or parsed.password:
        raise ValueError("网页地址不能包含账号或密码")
    host = parsed.hostname.rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError("不允许爬取本机地址")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address and not address.is_global:
        raise ValueError("不允许爬取内网或保留地址")
    return urlunparse(parsed._replace(fragment=""))


def _validate_public_destination(raw_url: str) -> str:
    """Reject destinations that resolve to loopback, private, or reserved IPs."""
    url = normalize_page_url(raw_url)
    parsed = urlparse(url)
    host = str(parsed.hostname or "")
    try:
        addresses = {ipaddress.ip_address(host)}
    except ValueError:
        try:
            addresses = {
                ipaddress.ip_address(result[4][0].split("%", 1)[0])
                for result in socket.getaddrinfo(host, parsed.port, type=socket.SOCK_STREAM)
            }
        except (OSError, ValueError) as exc:
            raise ValueError(f"无法安全解析网页地址：{host}") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("不允许访问解析到内网或保留地址的网页")
    return url


def _request_public_url(
    session: requests.Session,
    raw_url: str,
    *,
    timeout: tuple[int, int],
    headers: dict[str, str] | None = None,
) -> requests.Response:
    """Fetch a public URL while validating every redirect before following it."""
    current_url = _validate_public_destination(raw_url)
    request_headers = dict(headers or {})
    original = urlparse(current_url)
    original_origin = (original.scheme, str(original.hostname or "").casefold(), original.port)
    for redirect_count in range(MAX_HTTP_REDIRECTS + 1):
        response = session.get(
            current_url,
            stream=True,
            timeout=timeout,
            allow_redirects=False,
            headers=request_headers,
        )
        if response.status_code not in HTTP_REDIRECT_STATUSES:
            return response
        location = str(response.headers.get("Location") or "").strip()
        response.close()
        if not location:
            raise requests.HTTPError(f"网页重定向缺少目标地址：HTTP {response.status_code}")
        if redirect_count >= MAX_HTTP_REDIRECTS:
            raise requests.TooManyRedirects(f"网页重定向超过 {MAX_HTTP_REDIRECTS} 次")
        next_url = _validate_public_destination(urljoin(current_url, location))
        next_parts = urlparse(next_url)
        next_origin = (next_parts.scheme, str(next_parts.hostname or "").casefold(), next_parts.port)
        if next_origin != original_origin:
            sensitive_headers = {"authorization", "proxy-authorization", "cookie"}
            if session.auth or any(
                str(key).casefold() in sensitive_headers for key in session.headers
            ) or any(
                str(key).casefold() in sensitive_headers for key in request_headers
            ):
                raise ValueError("携带账号凭据的网页不允许跳转到其他主机")
            request_headers = {
                key: value for key, value in request_headers.items()
                if str(key).casefold() not in sensitive_headers | {"referer"}
            }
        current_url = next_url
    raise requests.TooManyRedirects(f"网页重定向超过 {MAX_HTTP_REDIRECTS} 次")


def _media_url(raw_url: str, base_url: str) -> str:
    value = str(raw_url or "").strip()
    if not value:
        return ""
    resolved = urlparse(urljoin(base_url, value))
    if resolved.scheme not in {"http", "https"} or not resolved.hostname:
        return ""
    try:
        return normalize_page_url(urlunparse(resolved._replace(fragment="")))
    except ValueError:
        return ""


def _srcset_urls(value: str) -> list[str]:
    return [part.strip().split()[0] for part in str(value or "").split(",") if part.strip()]


class _MediaHTMLParser(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.rows: list[MediaCandidate] = []
        self.seen: set[str] = set()

    def _add(self, raw_url: str, kind: str) -> None:
        url = _media_url(raw_url, self.base_url)
        if url and url not in self.seen:
            self.seen.add(url)
            self.rows.append(MediaCandidate(url, kind))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        tag = tag.lower()
        if tag == "meta":
            property_name = (values.get("property") or values.get("name") or "").lower()
            if property_name in {"og:image", "twitter:image", "og:video", "twitter:player:stream", "og:audio"}:
                self._add(values.get("content", ""), "metadata")
            return
        if tag == "img":
            for name in ("src", "data-src", "data-original", "data-lazy-src"):
                self._add(values.get(name, ""), "image")
            for url in _srcset_urls(values.get("srcset", "")):
                self._add(url, "image")
            return
        if tag == "video":
            self._add(values.get("poster", ""), "image")
            self._add(values.get("src", ""), "video")
            return
        if tag == "audio":
            self._add(values.get("src", ""), "audio")
            return
        if tag == "source":
            self._add(values.get("src", ""), "media")
            for url in _srcset_urls(values.get("srcset", "")):
                self._add(url, "media")
            return
        if tag == "a":
            href = values.get("href", "")
            if Path(urlparse(href).path).suffix.lower() in DIRECT_MEDIA_EXTENSIONS:
                self._add(href, "link")


def extract_media_candidates(html: str, base_url: str) -> list[MediaCandidate]:
    parser = _MediaHTMLParser(normalize_page_url(base_url))
    parser.feed(str(html or ""))
    parser.close()
    return parser.rows


def _html_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return re.sub(r"\s+", " ", match.group(1)).strip()


class WebPageCrawler:
    def __init__(self, session: requests.Session | None = None, browser=None) -> None:
        self.session = session
        if browser is None:
            from .browser import DynamicPageBrowser

            browser = DynamicPageBrowser()
        self.browser = browser

    def _session(self, options: dict | None = None) -> requests.Session:
        if self.session is not None:
            return self.session
        options = options or {}
        return make_session(
            proxy_url=str(options.get("proxy_url") or ""),
            cookie_file=options.get("cookie_file") or None,
        )

    def preview(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        try:
            url = normalize_page_url(raw_target)
        except ValueError as exc:
            return TargetPreview("website", raw_target, raw_target, title="网页资源", description=str(exc), status="error")
        parsed = urlparse(url)
        metadata: dict = {"input_kind": "webpage", "host": parsed.hostname or ""}
        title = parsed.hostname or "网页资源"
        description = "地址有效，可由软件内置网页资源爬虫提取图片、视频和音频"
        if options and options.get("live"):
            html, final_url = self._fetch_html(url, self._session(options), threading.Event(), options)
            rows = extract_media_candidates(html, final_url)
            title = _html_title(html) or title
            description = f"发现 {len(rows)} 个页面媒体候选"
            metadata.update({"page_html": html, "media_count": len(rows), "final_url": final_url})
        return TargetPreview("website", raw_target, url, title=title, description=description, metadata=metadata)

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        page_url = normalize_page_url(task.target)
        session = self._session(task.options)
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", f"正在分析候选网页: {page_url}"))
        html = ""
        page_title = ""
        browser_payloads: dict[str, object] = {}
        if task.options.get("browser_render"):
            callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", f"正在浏览器中打开候选详情页: {page_url}")
            )
            browser_result = self.browser.collect(task.task_id, page_url, cancel_event, task.options)
            final_url = browser_result.final_url
            page_title = browser_result.title
            candidates = browser_result.candidates
            browser_payloads = browser_result.media_payloads
        else:
            html, final_url = self._fetch_html(page_url, session, cancel_event, task.options)
            candidates = extract_media_candidates(html, final_url)
        allowed_media_hosts = tuple(
            str(host).strip().casefold().lstrip(".")
            for host in task.options.get("_allowed_media_hosts") or ()
            if str(host).strip()
        )
        if allowed_media_hosts:
            candidates = [
                candidate for candidate in candidates
                if (lambda host: any(host == allowed or host.endswith("." + allowed) for allowed in allowed_media_hosts))(
                    str(urlparse(candidate.url).hostname or "").casefold()
                )
            ]
            browser_payloads = {
                url: payload for url, payload in browser_payloads.items()
                if any(
                    str(urlparse(url).hostname or "").casefold() == allowed
                    or str(urlparse(url).hostname or "").casefold().endswith("." + allowed)
                    for allowed in allowed_media_hosts
                )
            }
        blocklist_path = str(task.options.get("_blocklist_path") or "")
        inspected_work_id = str(task.options.get("pixiv_work_id") or "")
        final_work = work_from_target("website", final_url)
        author_id = str(task.options.get("author_id") or "")
        if inspected_work_id and final_work != ("pixiv", inspected_work_id):
            author_id = ""
        if blocklist_path and BlocklistStore(blocklist_path).is_blocked(
            "website", final_url, author_id=author_id
        ):
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "info", f"跳过黑名单页面: {final_url}",
                metadata={"skipped_blocked": 1, "page_url": final_url},
            ))
            return
        if task.options.get("browser_render") and not candidates:
            raise RuntimeError(
                "浏览器已打开候选详情页，但没有发现可下载媒体；"
                "页面可能已删除、需要登录，或网页结构已经变化"
            )
        max_files = max(1, min(int(task.options.get("max_files") or 50), 200))
        max_file_bytes = max(1, min(int(task.options.get("max_file_mb") or 200), 2048)) * 1024 * 1024
        parsed = urlparse(final_url)
        page_name = safe_component(page_title or _html_title(html) or Path(parsed.path).stem or "page")
        output_platform = str(task.options.get("_output_platform") or "website").strip().casefold()
        if output_platform not in {"website", "instagram", "twitter"}:
            output_platform = "website"
        root = platform_output_root(task.output_dir, output_platform) / safe_component(parsed.hostname or "site") / page_name
        downloaded = 0
        skipped = 0
        last_error = ""
        for index, candidate in enumerate(candidates[:max_files], 1):
            if cancel_event.is_set():
                raise TaskCancelled("任务已取消")
            media_retries = max(1, min(int(task.options.get("media_retries") or 3), 6))
            record = None
            for attempt in range(1, media_retries + 1):
                try:
                    prefetched = browser_payloads.get(candidate.url) if attempt == 1 else None
                    if prefetched is not None:
                        record = self._download_browser_payload(
                            prefetched,
                            candidate,
                            root,
                            index,
                            max_file_bytes,
                            task,
                            final_url,
                        )
                    else:
                        record = self._download_candidate(
                            session,
                            candidate,
                            root,
                            index,
                            max_file_bytes,
                            task,
                            final_url,
                            cancel_event,
                            task.options,
                        )
                    break
                except TaskCancelled:
                    raise
                except (requests.RequestException, OSError, ValueError, RuntimeError) as exc:
                    last_error = str(exc)
                    if attempt >= media_retries:
                        break
                    callbacks.on_progress(
                        ProgressEvent(
                            task.task_id,
                            task.module_id,
                            "warning",
                            f"资源下载失败，重试 {attempt}/{media_retries}: {candidate.url} | {exc}",
                        )
                    )
                    if cancel_event.wait(min(5.0, 0.8 * (2 ** (attempt - 1)))):
                        raise TaskCancelled("任务已取消")
            if record is None:
                skipped += 1
                callbacks.on_progress(
                    ProgressEvent(task.task_id, task.module_id, "warning", f"资源多次下载失败: {candidate.url} | {last_error}")
                )
                continue
            downloaded += 1
            callbacks.on_file(record)
            callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", f"已下载 {downloaded}/{min(len(candidates), max_files)}: {record.path.name}", current=downloaded, total=min(len(candidates), max_files))
            )
        if candidates and downloaded == 0:
            raise RuntimeError(f"页面发现 {len(candidates)} 个媒体，但全部下载失败：{last_error or '未知下载错误'}")
        callbacks.on_progress(
            ProgressEvent(
                task.task_id,
                task.module_id,
                "info",
                f"候选网页完成：发现 {len(candidates)} 个资源，下载 {downloaded} 个，跳过 {skipped} 个",
                metadata={"discovered": len(candidates), "downloaded": downloaded, "skipped": skipped, "page_url": final_url},
            )
        )

    def cancel(self, task_id: str) -> None:
        self.browser.cancel(task_id)

    @staticmethod
    def _fetch_html(
        url: str,
        session: requests.Session,
        cancel_event: threading.Event,
        options: dict | None = None,
    ) -> tuple[str, str]:
        if cancel_event.is_set():
            raise TaskCancelled("任务已取消")
        options = options or {}
        connect_timeout = max(10, min(int(options.get("page_connect_timeout") or 20), 120))
        read_timeout = max(30, min(int(options.get("page_read_timeout") or 60), 600))
        with _request_public_url(
            session, url, timeout=(connect_timeout, read_timeout)
        ) as response:
            response.raise_for_status()
            final_url = normalize_page_url(response.url)
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type and "xhtml" not in content_type:
                raise ValueError(f"候选地址不是 HTML 网页: {content_type or '未知类型'}")
            chunks: list[bytes] = []
            size = 0
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if cancel_event.is_set():
                    raise TaskCancelled("任务已取消")
                if not chunk:
                    continue
                size += len(chunk)
                if size > MAX_HTML_BYTES:
                    raise ValueError("网页 HTML 超过 5 MB，已停止分析")
                chunks.append(chunk)
            encoding = response.encoding or response.apparent_encoding or "utf-8"
            return b"".join(chunks).decode(encoding, errors="replace"), final_url

    @staticmethod
    def _download_candidate(
        session: requests.Session,
        candidate: MediaCandidate,
        root: Path,
        index: int,
        max_bytes: int,
        task: DownloadTask,
        page_url: str,
        cancel_event: threading.Event,
        options: dict | None = None,
    ) -> FileRecord:
        headers = {"Referer": page_url}
        options = options or {}
        connect_timeout = max(10, min(int(options.get("media_connect_timeout") or 20), 120))
        read_timeout = max(30, min(int(options.get("media_read_timeout") or 120), 1200))
        with _request_public_url(
            session,
            candidate.url,
            timeout=(connect_timeout, read_timeout),
            headers=headers,
        ) as response:
            response.raise_for_status()
            final_media_url = normalize_page_url(response.url)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not content_type.startswith(("image/", "video/", "audio/")):
                suffix = Path(urlparse(final_media_url).path).suffix.lower()
                if suffix not in DIRECT_MEDIA_EXTENSIONS:
                    raise ValueError(f"不是受支持的媒体类型: {content_type or '未知类型'}")
            declared_size = int(response.headers.get("Content-Length") or 0)
            if declared_size > max_bytes:
                raise ValueError("资源超过单文件大小限制")
            raw_name = unquote(Path(urlparse(final_media_url).path).name)
            suffix = Path(raw_name).suffix.lower()
            if suffix not in DIRECT_MEDIA_EXTENSIONS:
                suffix = CONTENT_TYPE_EXTENSIONS.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"
            stem = safe_component(Path(raw_name).stem, f"media-{index:03d}", max_length=90)
            final_suffix = normalized_media_extension(content_type, suffix, candidate.kind, task.options) or suffix
            destination = root / f"{index:03d}-{stem}{final_suffix}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            source_temporary = destination.with_name(destination.stem + f".source{suffix}.part")
            conversion_source = source_temporary.with_suffix("")
            size = 0
            try:
                with source_temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=128 * 1024):
                        if cancel_event.is_set():
                            raise TaskCancelled("任务已取消")
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > max_bytes:
                            raise ValueError("资源超过单文件大小限制")
                        output.write(chunk)
                os.replace(source_temporary, conversion_source)
                convert_media_file(conversion_source, destination, final_suffix)
                size = destination.stat().st_size
            finally:
                for temporary in (source_temporary, conversion_source):
                    if temporary.exists():
                        try:
                            temporary.unlink()
                        except OSError:
                            pass
        return FileRecord(
            path=destination.resolve(),
            module_id=task.module_id,
            task_id=task.task_id,
            media_type=classify_file(destination),
            size=size,
            title=destination.name,
            source_url=final_media_url,
            source_id=str(index),
            metadata={"page_url": page_url, "candidate_kind": candidate.kind},
        )

    @staticmethod
    def _download_browser_payload(
        payload: object,
        candidate: MediaCandidate,
        root: Path,
        index: int,
        max_bytes: int,
        task: DownloadTask,
        page_url: str,
    ) -> FileRecord:
        data = bytes(getattr(payload, "data", b""))
        content_type = str(getattr(payload, "content_type", "") or "").split(";", 1)[0].strip().lower()
        final_media_url = normalize_page_url(str(getattr(payload, "final_url", "") or candidate.url))
        if not data:
            raise ValueError("浏览器返回了空媒体")
        if len(data) > max_bytes:
            raise ValueError("资源超过单文件大小限制")
        raw_name = unquote(Path(urlparse(final_media_url).path).name)
        suffix = Path(raw_name).suffix.lower()
        if not content_type.startswith(("image/", "video/", "audio/")) and suffix not in DIRECT_MEDIA_EXTENSIONS:
            raise ValueError(f"不是受支持的媒体类型: {content_type or '未知类型'}")
        if suffix not in DIRECT_MEDIA_EXTENSIONS:
            suffix = CONTENT_TYPE_EXTENSIONS.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"
        stem = safe_component(Path(raw_name).stem, f"media-{index:03d}", max_length=90)
        final_suffix = normalized_media_extension(content_type, suffix, candidate.kind, task.options) or suffix
        destination = root / f"{index:03d}-{stem}{final_suffix}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        source_temporary = destination.with_name(destination.stem + f".source{suffix}.part")
        conversion_source = source_temporary.with_suffix("")
        try:
            source_temporary.write_bytes(data)
            os.replace(source_temporary, conversion_source)
            convert_media_file(conversion_source, destination, final_suffix)
        finally:
            for temporary in (source_temporary, conversion_source):
                if temporary.exists():
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
        return FileRecord(
            path=destination.resolve(),
            module_id=task.module_id,
            task_id=task.task_id,
            media_type=classify_file(destination),
            size=destination.stat().st_size,
            title=destination.name,
            source_url=final_media_url,
            source_id=str(index),
            metadata={"page_url": page_url, "candidate_kind": candidate.kind, "transport": "browser"},
        )
