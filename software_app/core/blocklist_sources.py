"""Read prior moderation records without changing any remote account."""

from __future__ import annotations

import json
import re
import time
import zipfile
import html as html_module
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


_ARCHIVE_NAMES = {"block.js": "X 拉黑", "mute.js": "X 静音"}
_MAX_SOURCE_BYTES = 10 * 1024 * 1024


def _parse_x_js(name: str, content: bytes) -> list[dict]:
    if len(content) > _MAX_SOURCE_BYTES:
        raise ValueError(f"{name} 超过 10 MiB")
    text = content.decode("utf-8-sig").strip()
    match = re.match(r"window\.YTD\.(block|mute)\.part\d+\s*=\s*", text)
    if not match or f"{match.group(1)}.js" != name:
        raise ValueError(f"{name} 不是可识别的 X 归档文件")
    payload = json.loads(text[match.end():].rstrip("; \r\n"))
    if not isinstance(payload, list):
        raise ValueError(f"{name} 的记录格式无效")
    field = "blocking" if name == "block.js" else "muting"
    records = []
    for index, item in enumerate(payload, 1):
        details = item.get(field) if isinstance(item, dict) else None
        account_id = str(details.get("accountId") or "") if isinstance(details, dict) else ""
        if not re.fullmatch(r"\d{1,20}", account_id):
            raise ValueError(f"{name} 第 {index} 项没有有效账号 ID")
        records.append({"primary": ("twitter_id", account_id), "linked": [], "source": _ARCHIVE_NAMES[name]})
    return records


def read_x_archive(path: Path | str) -> list[dict]:
    """Read only block.js/mute.js from a user-supplied X archive or standalone file."""
    source = Path(path)
    if source.suffix.casefold() == ".zip":
        with zipfile.ZipFile(source) as archive:
            selected: dict[str, zipfile.ZipInfo] = {}
            for info in archive.infolist():
                member_path = info.filename.replace("\\", "/").casefold()
                name = Path(member_path).name
                if name in _ARCHIVE_NAMES and "/data/" in f"/{member_path}":
                    if info.file_size > _MAX_SOURCE_BYTES:
                        raise ValueError(f"{name} 超过 10 MiB")
                    if name in selected:
                        raise ValueError(f"归档中存在多个 {name}，请单独选择文件")
                    selected[name] = info
            if not selected:
                raise ValueError("归档中未找到 data/block.js 或 data/mute.js")
            records = []
            for name, info in selected.items():
                records.extend(_parse_x_js(name, archive.read(info)))
            return records
    name = source.name.casefold()
    if name not in _ARCHIVE_NAMES:
        raise ValueError("请选择 X 归档 ZIP、block.js 或 mute.js")
    if source.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError(f"{name} 超过 10 MiB")
    return _parse_x_js(name, source.read_bytes())


class _PixivSettingsLinks(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.accounts: set[str] = set()
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        parsed = urlparse(href)
        host = str(parsed.hostname or "").casefold().rstrip(".")
        if host and host != "pixiv.net" and not host.endswith(".pixiv.net"):
            return
        if not host and not href.startswith("/"):
            return
        match = re.fullmatch(r"/(?:en/)?users/(\d+)/?", parsed.path)
        if match:
            self.accounts.add(match.group(1))

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def parse_pixiv_settings_html(content: str) -> list[dict]:
    if len(content.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("Pixiv 设置页超过 10 MiB")
    parser = _PixivSettingsLinks()
    parser.feed(content)
    title = parser.title.casefold()
    if "pixiv" not in title or not any(word in title for word in ("mute", "block", "ミュート", "ブロック", "静音", "屏蔽")):
        raise ValueError("这不是可识别的 Pixiv 静音/屏蔽设置页；请保存设置列表页 HTML")
    if not parser.accounts:
        raise ValueError("设置页中未找到作者主页链接；可改用一行一个用户 ID 的 TXT 名单")
    return [{"primary": ("pixiv", account), "linked": [], "source": "Pixiv 设置页"}
            for account in sorted(parser.accounts)]


def read_pixiv_settings_html(path: Path | str) -> list[dict]:
    source = Path(path)
    if source.stat().st_size > _MAX_SOURCE_BYTES:
        raise ValueError("Pixiv 设置页超过 10 MiB")
    return parse_pixiv_settings_html(source.read_text(encoding="utf-8-sig"))


def parse_jmcomic_tag_block_html(content: str) -> list[dict]:
    """Extract blocked tag names from the account tag_block page."""
    if len(content.encode("utf-8")) > _MAX_SOURCE_BYTES:
        raise ValueError("JMComic 屏蔽页超过 10 MiB")
    tags: set[str] = set()
    for pattern in (
        r'\bdata-tag=["\']([^"\']+)["\']',
        r'<input\b[^>]*\bname=["\'](?:tag|tag_name)["\'][^>]*\bvalue=["\']([^"\']+)["\']',
        r'<input\b[^>]*\bvalue=["\']([^"\']+)["\'][^>]*\bname=["\'](?:tag|tag_name)["\']',
    ):
        for value in re.findall(pattern, content, flags=re.IGNORECASE):
            value = html_module.unescape(re.sub(r"<[^>]+>", "", value)).strip()
            if value and len(value) <= 100:
                tags.add(value)
    for href in re.findall(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', content, flags=re.IGNORECASE):
        parsed = urlparse(html_module.unescape(href))
        if "/search" not in parsed.path.casefold():
            continue
        query = parse_qs(parsed.query)
        for key in ("search_query", "tag"):
            for value in query.get(key, []):
                value = str(value or "").strip()
                if value and len(value) <= 100:
                    tags.add(value)
    return [
        {"primary": ("jmcomic_tag", tag.casefold()), "linked": [], "label": tag, "source": "JMComic 站内屏蔽标签"}
        for tag in sorted(tags, key=str.casefold)
    ]


def fetch_jmcomic_tag_blocks(
    domain: str, username: str, cookie_file: Path | str, proxy_url: str = "", user_agent: str = "",
    browser_headers: dict[str, str] | None = None,
) -> list[dict]:
    from software_app.crawlers.common import load_cookie_file
    from software_app.crawlers.jmcomic.client import jm_response_text, make_jm_session

    parsed_domain = urlparse(str(domain or "").strip().rstrip("/"))
    if parsed_domain.scheme not in {"http", "https"} or not parsed_domain.hostname:
        raise ValueError("请先配置完整的 JMComic 站点域名")
    username = str(username or "").strip().lstrip("@")
    if not username:
        raise ValueError("请先在设置中填写 JMComic 个人主页用户名")
    if not load_cookie_file(cookie_file).get("AVS"):
        raise ValueError("请先导入包含 AVS 的 JMComic Cookie")
    origin = f"{parsed_domain.scheme}://{parsed_domain.netloc}"
    session = make_jm_session(
        origin, cookie_file, proxy_url, user_agent=user_agent, browser_headers=browser_headers
    )
    session.trust_env = False
    try:
        try:
            response = session.get(f"{origin}/user/{quote(username)}/tag_block", timeout=(10, 45))
            if response.status_code == 403:
                response.close()
                time.sleep(0.6)
                response = session.get(f"{origin}/user/{quote(username)}/tag_block", timeout=(10, 45))
        except Exception as exc:
            from requests import exceptions as request_errors
            if isinstance(exc, request_errors.ProxyError):
                raise RuntimeError("JMComic 代理连接失败；请检查工作台代理设置") from exc
            if isinstance(exc, request_errors.ConnectTimeout):
                raise TimeoutError("JMComic 站内屏蔽页连接超时；请检查域名或代理") from exc
            if isinstance(exc, request_errors.Timeout):
                raise TimeoutError("JMComic 站内屏蔽页响应超时；请稍后重试") from exc
            if isinstance(exc, request_errors.RequestException):
                raise RuntimeError(f"JMComic 站内屏蔽页读取失败：{exc}") from exc
            raise
        if response.status_code == 403:
            raise PermissionError(
                "JMComic 站内屏蔽页返回 HTTP 403；请导入与当前域名、代理出口和浏览器一致的 cf_clearance"
            )
        response.raise_for_status()
        page = jm_response_text(response)
        final_path = urlparse(str(response.url or "")).path.casefold().rstrip("/")
        if final_path == "/login" or any(
            marker in page for marker in ("請先登入", "请先登录", "會員登入", "会员登录")
        ):
            raise PermissionError("JMComic 站内屏蔽页跳转到了登录页；请更新当前域名的 AVS")
        return parse_jmcomic_tag_block_html(page)
    finally:
        session.close()


def fetch_pixiv_mutes(cookie_file: Path | str, proxy_url: str = "") -> list[dict]:
    from software_app.crawlers.common import load_cookie_file, make_session

    if not load_cookie_file(cookie_file).get("PHPSESSID"):
        raise ValueError("请先在 Pixiv 设置中导入登录 Cookie")
    session = make_session(referer="https://www.pixiv.net/", cookie_file=cookie_file, proxy_url=proxy_url)
    session.trust_env = False
    try:
        response = session.get("https://www.pixiv.net/settings/viewing/mute", timeout=(5, 25))
        response.raise_for_status()
        host = str(urlparse(str(response.url)).hostname or "").casefold()
        if host != "pixiv.net" and not host.endswith(".pixiv.net"):
            raise ValueError("Pixiv 设置页跳转到其他网站，可能需要重新登录")
        records = parse_pixiv_settings_html(response.text)
        for item in records:
            item["source"] = "Pixiv 在线静音"
        return records
    finally:
        session.close()


def fetch_bluesky_moderation(identifier: str, app_password: str, *, include_mutes: bool = True) -> list[dict]:
    """Use an ephemeral app-password session to read official paginated moderation APIs."""
    identifier = identifier.strip()
    if not identifier or not app_password:
        raise ValueError("请输入 Bluesky 账号和应用密码")
    body = json.dumps({"identifier": identifier, "password": app_password}).encode("utf-8")
    request = Request("https://bsky.social/xrpc/com.atproto.server.createSession", data=body,
                      headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urlopen(request, timeout=20) as response:
        session = json.load(response)
    token = str(session.get("accessJwt") or "")
    if not token:
        raise ValueError("Bluesky 登录未返回访问令牌")
    records: dict[str, dict] = {}
    endpoints = [("getBlocks", "blocks", "Bluesky 拉黑")]
    if include_mutes:
        endpoints.append(("getMutes", "mutes", "Bluesky 静音"))
    for method, field, source in endpoints:
        cursor = ""
        seen_cursors: set[str] = set()
        for _page in range(1000):
            url = f"https://bsky.social/xrpc/app.bsky.graph.{method}?limit=100"
            if cursor:
                url += f"&cursor={quote(cursor, safe='')}"
            request = Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
            with urlopen(request, timeout=20) as response:
                payload = json.load(response)
            items = payload.get(field)
            if not isinstance(items, list):
                raise ValueError(f"Bluesky {method} 响应格式无效")
            for item in items:
                if not isinstance(item, dict):
                    continue
                did = str(item.get("did") or "").casefold()
                handle = str(item.get("handle") or "").casefold()
                if not did.startswith(("did:plc:", "did:web:")):
                    continue
                linked = [("bluesky", handle)] if handle else []
                previous = records.get(did)
                source_label = f"{previous['source']}、静音" if previous and source == "Bluesky 静音" else source
                records[did] = {"primary": ("bluesky", did), "linked": linked,
                                "label": str(item.get("displayName") or handle or did)[:120], "source": source_label}
            next_cursor = str(payload.get("cursor") or "")
            if not next_cursor:
                break
            if next_cursor in seen_cursors:
                raise ValueError("Bluesky 分页游标重复，已停止读取")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        else:
            raise ValueError("Bluesky 名单超过 1000 页，已停止读取")
    return list(records.values())
