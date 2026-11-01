from __future__ import annotations

import json
import re
import threading
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

import requests

from software_app.core.adapter import TaskCancelled
from software_app.core.media_results import media_descriptor, refreshed_media, retry_folder, run_media_jobs
from software_app.core.models import FileRecord, ProgressEvent, classify_file
from software_app.core.settings import HENTAIPAW_DATA_DIR
from software_app.core.task_control import task_cancelled
from software_app.crawlers.common import (
    download_media_file, make_session, normalized_media_extension, platform_output_root,
    safe_component, write_text_atomic,
)
from software_app.crawlers.webpage.client import WebPageCrawler


BASE_URL = "https://zh.hentaipaw.com"


def normalize_article_url(raw: str) -> str:
    value = str(raw or "").strip()
    if re.fullmatch(r"[0-9]+", value):
        return f"{BASE_URL}/articles/{value}"
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.hostname not in {"zh.hentaipaw.com", "hentaipaw.com"}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        raise ValueError("请输入 HentaiPaw HTTPS 作品链接或数字 ID")
    match = re.fullmatch(r"/articles/([0-9]+)/?", parsed.path)
    if parsed.path == "/viewer":
        query = parse_qs(parsed.query, keep_blank_values=True)
        article = query.get("articleId", [])
        page = query.get("page", ["1"])
        if (set(query) <= {"articleId", "page"} and len(article) == 1
                and re.fullmatch(r"[0-9]+", article[0]) and len(page) == 1 and re.fullmatch(r"[1-9][0-9]*", page[0])):
            return f"{BASE_URL}/articles/{article[0]}"
        raise ValueError("阅读器链接需要有效的 articleId 和页码")
    if not match or parsed.query:
        raise ValueError("请使用 /articles/数字ID 作品链接或阅读器链接")
    return f"{BASE_URL}/articles/{match[1]}"


class _ArticleParser(HTMLParser):
    def __init__(self, article_id: str):
        super().__init__(convert_charrefs=True)
        self.article_id = article_id
        self.in_heading = False
        self.title_parts: list[str] = []
        self.cover = ""
        self.pages: set[int] = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "h1":
            self.in_heading = True
        if tag == "img":
            src = str(values.get("src") or "")
            image = urlsplit(src)
            if (image.scheme == "https" and image.hostname == "cdn.imagedeliveries.com"
                    and image.path == f"/{self.article_id}/thumbnails/cover.webp"):
                self.cover = src
        if tag == "a":
            link = urlsplit(urljoin(BASE_URL, str(values.get("href") or "")))
            query = parse_qs(link.query)
            if link.hostname == "zh.hentaipaw.com" and link.path == "/viewer":
                page = query.get("page", [""])[0]
                if query.get("articleId") == [self.article_id] and re.fullmatch(r"[0-9]{1,6}", page):
                    self.pages.add(int(page))

    def handle_endtag(self, tag):
        if tag == "h1":
            self.in_heading = False

    def handle_data(self, data):
        if self.in_heading:
            self.title_parts.append(data)


def parse_article(html: str, url: str) -> dict:
    normalized = normalize_article_url(url)
    article_id = normalized.rsplit("/", 1)[-1]
    parser = _ArticleParser(article_id)
    parser.feed(html)
    title = " ".join(" ".join(parser.title_parts).split())
    if not title:
        raise ValueError("未找到作品标题；可能遇到验证页面或站点结构已变化")
    return {
        "target_id": article_id,
        "title": title,
        "url": normalized,
        "cover_url": parser.cover,
        "thumbnail_url": parser.cover,
        "visible_page_count": len(parser.pages),
        "reader_url": f"{BASE_URL}/viewer?articleId={article_id}&page=1",
        "download_ready": True,
    }


class _ScriptParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.in_script = False
        self.scripts = []

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self.in_script = True

    def handle_endtag(self, tag):
        if tag == "script":
            self.in_script = False

    def handle_data(self, data):
        if self.in_script:
            self.scripts.append(data)


def _reader_payloads(html):
    parser = _ScriptParser()
    parser.feed(html)
    decoder = json.JSONDecoder()
    frames = []
    for script in parser.scripts:
        for match in re.finditer(r"self\.__next_f\.push\(", script):
            try:
                frame, _ = decoder.raw_decode(script[match.end():])
            except ValueError:
                continue
            if isinstance(frame, list) and len(frame) == 2 and frame[0] == 1 and isinstance(frame[1], str):
                frames.append(frame[1])
    stream = "".join(frames).encode("utf-8")
    cursor = 0
    while cursor < len(stream):
        # Flight text records use a byte length rather than a newline. Ad
        # scripts can contain newlines; consume them before reading JSON rows.
        text_record = re.match(rb"[0-9a-fA-F]+:T([0-9a-fA-F]+),", stream[cursor:])
        if text_record:
            cursor += text_record.end() + int(text_record[1], 16)
            continue
        end = stream.find(b"\n", cursor)
        if end < 0:
            end = len(stream)
        line = stream[cursor:end].decode("utf-8", errors="replace")
        cursor = end + 1
        _key, sep, body = line.partition(":")
        if sep and body.startswith(("[", "{")):
            try:
                yield json.loads(body)
            except ValueError:
                continue


def parse_reader(html: str, article_id: str) -> list[str]:
    """Decode Next flight data as JSON, never evaluate the page's JavaScript."""
    def find(value, depth=0):
        if depth > 100:
            raise ValueError("阅读器数据嵌套过深")
        if isinstance(value, dict):
            if str(value.get("articleId")) == article_id and isinstance(value.get("slides"), list):
                return value["slides"]
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            return None
        for child in children:
            found = find(child, depth + 1)
            if found is not None:
                return found
        return None

    for payload in _reader_payloads(html):
        slides = find(payload)
        if slides is None:
            continue
        if not slides or len(slides) > 10000:
            raise ValueError("阅读器页数为空或超过 10000 页")
        urls = []
        for index, slide in enumerate(slides, 1):
            url = str(slide.get("src") or "") if isinstance(slide, dict) else ""
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname != "cdn.imagedeliveries.com"
                    or parsed.username or parsed.password or parsed.port not in {None, 443}
                    or parsed.query or parsed.fragment
                    or "/thumbnails/" in parsed.path
                    or not re.fullmatch(rf"/{re.escape(article_id)}/[A-Za-z0-9_-]+/{index}\.(?:webp|jpg|jpeg|png|gif)", parsed.path)):
                raise ValueError("阅读器原图地址、作品 ID 或页码顺序无效；未使用缩略图替代")
            urls.append(url)
        return urls
    raise PermissionError("未取得阅读器原图；若页面提示登录，请在设置中导入 HentaiPaw Cookie 后重试")


class _SearchParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = {}
        self.active = None
        self.links = []
        self.named_title = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a":
            self.active = None
            self.named_title = False
            href = str(values.get("href") or "")
            self.links.append(urljoin(BASE_URL, href))
            try:
                url = normalize_article_url(urljoin(BASE_URL, href))
            except ValueError:
                return
            self.active = self.rows.setdefault(url, {"url": url, "title": "", "id": url.rsplit("/", 1)[-1]})
        if tag == "img" and self.active is not None:
            if not self.active["title"]:
                self.active["title"] = str(values.get("alt") or "").strip()
        if self.active is not None and values.get("title"):
            self.active["title"] = str(values["title"]).strip()
            self.named_title = True

    def handle_endtag(self, tag):
        if tag == "a":
            self.active = None

    def handle_data(self, data):
        if self.active is not None and not self.named_title and data.strip():
            if self.active["title"].strip() != data.strip():
                self.active["title"] += " " + data.strip()


class HentaiPawClient:
    def __init__(self, *, cookie_file=None, proxy_url=""):
        self.cookie_file = Path(cookie_file) if cookie_file else Path(HENTAIPAW_DATA_DIR) / "cookies.json"
        self.session = make_session(referer=BASE_URL + "/", cookie_file=self.cookie_file, proxy_url=proxy_url)

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def _fetch(self, url, options):
        options = options or {}
        try:
            html, final_url = WebPageCrawler._fetch_html(
                url, self.session, options.get("cancel_event") or threading.Event(), options,
            )
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code in {401, 403}:
                raise PermissionError("站点拒绝访问或要求登录/验证，请导入本站 Cookie 并核对代理设置") from None
            raise
        final = urlsplit(final_url)
        if final.scheme != "https" or final.hostname != "zh.hentaipaw.com":
            raise PermissionError("HentaiPaw 跳转到其他站点；请核对是否需要登录或验证")
        return html, final_url

    def article(self, raw: str, options: dict | None = None) -> dict:
        url = normalize_article_url(raw)
        html, final_url = self._fetch(url, options)
        try:
            final_url = normalize_article_url(final_url)
        except ValueError:
            raise PermissionError("作品页跳转到登录或验证页面，请检查本站 Cookie 和代理设置") from None
        if final_url != url:
            raise ValueError("作品页跳转到其他作品，请核对链接")
        return parse_article(html, url)

    def reader(self, raw, options=None):
        url = normalize_article_url(raw)
        article_id = url.rsplit("/", 1)[-1]
        reader_url = f"{BASE_URL}/viewer?articleId={article_id}&page=1"
        html, final_url = self._fetch(reader_url, options)
        final = urlsplit(final_url)
        if final.path != "/viewer" or parse_qs(final.query).get("articleId") != [article_id]:
            raise PermissionError("阅读器跳转到登录或其他页面；请导入本站 Cookie 后重试")
        return parse_reader(html, article_id)

    def search(self, query, limit=20, options=None):
        query = str(query or "").strip()
        if not query:
            return []
        limit = max(0, min(int(limit), 100))
        if not limit:
            return []
        rows = []
        seen = set()
        for page in range(1, 11):
            params = {"keyword": query}
            if page > 1:
                params["page"] = page
            url = BASE_URL + "/articles/search?" + urlencode(params)
            html, final_url = self._fetch(url, options)
            final = urlsplit(final_url)
            if final.path != "/articles/search" or parse_qs(final.query).get("keyword") != [query]:
                raise PermissionError("搜索页面发生跳转，请核对站点登录状态")
            parser = _SearchParser()
            parser.feed(html)
            for row in parser.rows.values():
                if row["url"] not in seen:
                    seen.add(row["url"])
                    rows.append({**row, "title": row["title"].strip() or row["id"], "module_id": "hentaipaw"})
                if len(rows) >= limit:
                    return rows
            next_page = False
            for link in parser.links:
                parsed = urlsplit(link)
                values = parse_qs(parsed.query)
                if (parsed.scheme == "https" and parsed.netloc == "zh.hentaipaw.com"
                        and parsed.path == "/articles/search" and values.get("keyword") == [query]
                        and values.get("page") == [str(page + 1)]):
                    next_page = True
                    break
            if not next_page:
                break
        return rows

    def download(self, task, callbacks, cancel_event):
        options = dict(task.options, cancel_event=cancel_event)
        if task_cancelled(cancel_event):
            raise TaskCancelled("任务已取消")
        if "1" not in str(options.get("types", "1234")):
            raise ValueError("HentaiPaw 漫画下载需要选择图片类型")
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", "HentaiPaw 正在读取作品详情",
                                            metadata={"phase": "details"}))
        info = self.article(task.target, options)
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", "HentaiPaw 正在解析阅读器原图列表",
                                            metadata={"phase": "discover"}))
        urls = self.reader(task.target, options)
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", f"HentaiPaw 已核实 {len(urls)} 页，正在准备下载范围",
                                            metadata={"phase": "plan", "page_count": len(urls)}))
        if "_retry_media" in options:
            jobs = []
            media = [{"url": url, "type": "image"} for url in urls]
            for item in options["_retry_media"]:
                if normalize_article_url(item["target"]) != info["url"]:
                    raise ValueError("补下记录与当前作品不匹配，未下载其他作品")
                page = refreshed_media(media, item)
                folder = retry_folder(task, item)
                jobs.append((item, lambda item=item, page=page, folder=folder: self._download_page(
                    task, info, page["url"], folder, int(item["index"]), cancel_event, page_count=len(urls),
                )))
            run_media_jobs(task, callbacks, cancel_event, jobs)
            return
        maximum = max(0, int(options.get("max_files") or 0))
        selected = urls[:maximum] if maximum else urls
        folder = platform_output_root(task.output_dir, "HentaiPaw") / safe_component(
            f"{info['target_id']} {info['title']}", max_length=100,
        )
        document = folder / "metadata.json"
        write_text_atomic(document, json.dumps({**info, "page_count": len(urls)}, ensure_ascii=False, indent=2))
        callbacks.on_file(FileRecord(document.resolve(), task.module_id, task.task_id, "text", document.stat().st_size,
                                     title=info["title"], source_url=info["url"], source_id=info["target_id"]))
        jobs = []
        for index, url in enumerate(selected, 1):
            if task_cancelled(cancel_event):
                raise TaskCancelled("任务已取消")
            item = media_descriptor(task, info["url"], index, "image", url, folder)
            jobs.append((item, lambda index=index, url=url: self._download_page(
                task, info, url, folder, index, cancel_event, page_count=len(urls),
            )))
        run_media_jobs(task, callbacks, cancel_event, jobs)

    def _download_page(self, task, info, url, folder, index, cancel_event, *, page_count=None):
        source_suffix = Path(urlsplit(url).path).suffix
        suffix = normalized_media_extension("image/*", source_suffix, "image", task.options)
        destination = folder / "images" / f"{index:04d}{suffix}"
        source = destination.with_name(f".{index:04d}.source{source_suffix}")
        result = download_media_file(
            self.session, url, source, destination, cancel_event, suffix, verify_image=True,
            conflict_policy=str(task.options.get("file_conflict") or "reuse"), public_referer=info["reader_url"],
        )
        return FileRecord(
            result.path.resolve(), task.module_id, task.task_id, classify_file(result.path), result[0],
            title=info["title"], source_url=info["url"], source_id=info["target_id"],
            metadata={"page": index, "page_count": page_count or info["visible_page_count"], "reused": result[1]},
        )
