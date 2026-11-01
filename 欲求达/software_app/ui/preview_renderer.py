from __future__ import annotations

import html
import re
import tkinter as tk
from html.parser import HTMLParser
from urllib.parse import urlparse

try:
    from tkinterweb import HtmlFrame
except Exception:  # Optional dependency; fall back to plain Tk text.
    HtmlFrame = None  # type: ignore[assignment]


class TargetPreviewRenderer:
    def __init__(self, parent: tk.Widget) -> None:
        self.html_enabled = HtmlFrame is not None
        if self.html_enabled:
            self.widget = HtmlFrame(parent, messages_enabled=False, height=60)  # type: ignore[misc]
        else:
            self.widget = tk.Text(parent, height=3, wrap="word", borderwidth=0, padx=6, pady=6)

    def grid(self, *args, **kwargs) -> None:
        self.widget.grid(*args, **kwargs)

    def render_text(self, text: str) -> None:
        if self.html_enabled:
            self.render_html(_plain_text_html(text), text)
            return
        self.widget.configure(state=tk.NORMAL)
        self.widget.delete("1.0", tk.END)
        self.widget.insert(tk.END, text)
        self.widget.configure(state=tk.DISABLED)

    def render_html(self, html_content: str, fallback_text: str = "") -> None:
        if self.html_enabled:
            self.widget.load_html(html_content)  # type: ignore[attr-defined]
            return
        self.render_text(fallback_text or _html_to_text(html_content))

    def render_profile(self, payload: dict, source: str, fallback_text: str = "") -> None:
        self.render_html(build_profile_preview_html(payload, source), fallback_text or build_profile_preview_text(payload, source))

    def render_page_html(self, html_content: str, source_url: str = "", fallback_text: str = "") -> None:
        """Render untrusted crawler HTML as a static, sanitized document."""
        safe_html = sanitize_page_html(html_content, source_url=source_url)
        self.render_html(safe_html, fallback_text or _html_to_text(safe_html))


SAFE_PAGE_TAGS = {
    "a", "article", "b", "blockquote", "body", "br", "code", "dd", "div", "dl", "dt",
    "em", "figcaption", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "html", "i", "img", "li", "main", "nav", "ol", "p", "pre",
    "section", "small", "span", "strong", "table", "tbody", "td", "th", "thead", "tr", "ul",
}
DROP_PAGE_TAGS = {"applet", "audio", "button", "canvas", "embed", "form", "iframe", "input", "link", "meta", "object", "script", "select", "source", "style", "svg", "textarea", "video"}
DROP_VOID_PAGE_TAGS = {"embed", "input", "link", "meta", "source"}
VOID_PAGE_TAGS = {"br", "hr", "img"}


class _StaticPageSanitizer(HTMLParser):
    def __init__(self, source_url: str = "") -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.parts: list[str] = []
        self._drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in DROP_PAGE_TAGS:
            if tag not in DROP_VOID_PAGE_TAGS:
                self._drop_depth += 1
            return
        if self._drop_depth or tag not in SAFE_PAGE_TAGS:
            return
        safe_attrs: list[str] = []
        for name, value in attrs:
            name = name.lower()
            value = str(value or "").strip()
            if name.startswith("on") or name in {"style", "srcset"}:
                continue
            if tag == "a" and name == "href" and _safe_page_url(value):
                safe_attrs.append(f'href="{html.escape(value, quote=True)}"')
            elif tag == "img" and name == "src" and _safe_page_url(value, allow_data=True):
                safe_attrs.append(f'src="{html.escape(value, quote=True)}"')
            elif name in {"alt", "title"}:
                safe_attrs.append(f'{name}="{html.escape(value, quote=True)}"')
        suffix = f" {' '.join(safe_attrs)}" if safe_attrs else ""
        self.parts.append(f"<{tag}{suffix}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in DROP_PAGE_TAGS:
            self._drop_depth = max(0, self._drop_depth - 1)
            return
        if not self._drop_depth and tag in SAFE_PAGE_TAGS and tag not in VOID_PAGE_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._drop_depth:
            self.parts.append(html.escape(data))


def _safe_page_url(value: str, allow_data: bool = False) -> bool:
    if not value:
        return False
    scheme = urlparse(value).scheme.lower()
    if not scheme:
        return True
    return scheme in ({"http", "https", "data"} if allow_data else {"http", "https"})


def sanitize_page_html(html_content: str, source_url: str = "") -> str:
    sanitizer = _StaticPageSanitizer(source_url)
    sanitizer.feed(str(html_content or ""))
    sanitizer.close()
    source = html.escape(source_url, quote=True)
    source_banner = f'<div class="source">静态页面快照：<a href="{source}">{source}</a></div>' if source else ""
    body = "".join(sanitizer.parts)
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><style>
body {{ margin:0; padding:10px; background:#fff; color:#1f2937; font:13px/1.55 "Microsoft YaHei UI","Segoe UI",sans-serif; }}
.source {{ padding:7px 9px; margin-bottom:10px; background:#eff6ff; border-left:3px solid #2563eb; word-break:break-all; }}
img {{ max-width:100%; height:auto; }} table {{ border-collapse:collapse; max-width:100%; }} td,th {{ border:1px solid #d1d5db; padding:4px 6px; }}
a {{ color:#2563eb; word-break:break-all; }} pre {{ white-space:pre-wrap; }}
</style></head><body>{source_banner}{body}</body></html>"""


def build_profile_preview_html(payload: dict, source: str) -> str:
    handle = str(payload.get("handle") or "").strip().lstrip("@")
    name = str(payload.get("display_name") or handle or "目标").strip()
    bio = str(payload.get("bio") or "").strip()
    profile_url = str(payload.get("profile_url") or "").strip()
    media_url = str(payload.get("media_url") or payload.get("normalized_target") or "").strip()
    download_description = str(payload.get("download_description") or "").strip()
    skeb_links = _link_items(payload.get("skeb_links", []) or [])
    links = _link_items(payload.get("links", []) or [])
    skeb_urls = {item["url"] for item in skeb_links}
    other_links = [item for item in links if item["url"] not in skeb_urls]
    warnings = [str(item) for item in (payload.get("warnings") or []) if str(item).strip()]

    handle_html = f'<div class="handle">@{html.escape(handle)}</div>' if handle else ""
    bio_html = f'<p class="bio">{html.escape(bio)}</p>' if bio else '<p class="muted">暂无简介</p>'
    profile_link = _render_link("主页", profile_url) if profile_url else ""
    media_link = _render_link("媒体页", media_url) if media_url else ""
    download_html = f'<div class="pill">{html.escape(download_description)}</div>' if download_description else ""

    skeb_html = _render_link_list(skeb_links, "本地资料未发现；需要最新外链时点击“在线获取资料”")
    other_html = _render_link_list(other_links, "暂无其他外链")
    warnings_html = _render_warning_list(warnings)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{
      margin: 0;
      padding: 0;
      background: #ffffff;
      color: #1f2937;
      font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
      font-size: 13px;
      line-height: 1.5;
    }}
    .card {{ padding: 10px 8px; }}
    .title {{ color: #111827; font-size: 16px; font-weight: 700; margin-bottom: 1px; }}
    .handle, .muted {{ color: #6b7280; }}
    .source-row {{ margin: 8px 0; }}
    .pill {{
      display: inline-block;
      margin-right: 5px;
      margin-bottom: 5px;
      padding: 2px 7px;
      border: 1px solid #d1d5db;
      border-radius: 4px;
      background: #f9fafb;
      color: #374151;
    }}
    .bio {{ margin: 8px 0; white-space: pre-wrap; }}
    .section {{ margin-top: 10px; }}
    .section-title {{ color: #111827; font-weight: 700; margin-bottom: 4px; }}
    a {{ color: #2563eb; text-decoration: none; word-break: break-all; }}
    ul {{ margin: 4px 0 0 18px; padding: 0; }}
    li {{ margin-bottom: 3px; }}
    .warning {{ color: #92400e; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="title">{html.escape(name)}</div>
    {handle_html}
    <div class="source-row">
      <span class="pill">来源: {html.escape(source)}</span>
      {download_html}
    </div>
    {bio_html}
    {profile_link}
    {media_link}
    <div class="section">
      <div class="section-title">Skeb 链接</div>
      {skeb_html}
    </div>
    <div class="section">
      <div class="section-title">其他外链</div>
      {other_html}
    </div>
    {warnings_html}
  </div>
</body>
</html>"""


def build_profile_preview_text(payload: dict, source: str) -> str:
    handle = str(payload.get("handle") or "").strip().lstrip("@")
    name = str(payload.get("display_name") or handle or "目标").strip()
    bio = str(payload.get("bio") or "").strip()
    profile_url = str(payload.get("profile_url") or "").strip()
    media_url = str(payload.get("media_url") or payload.get("normalized_target") or "").strip()
    download_description = str(payload.get("download_description") or "").strip()
    skeb_links = _link_items(payload.get("skeb_links", []) or [])
    links = _link_items(payload.get("links", []) or [])
    skeb_urls = {item["url"] for item in skeb_links}
    other_links = [item for item in links if item["url"] not in skeb_urls]

    lines = [name]
    if handle:
        lines.append(f"@{handle}")
    lines.append(f"来源: {source}")
    if download_description:
        lines.append(download_description)
    if bio:
        lines.extend(["", bio])
    if profile_url:
        lines.extend(["", f"主页: {profile_url}"])
    if media_url:
        lines.append(f"媒体页: {media_url}")
    lines.extend(["", "Skeb 链接:"])
    if skeb_links:
        lines.extend(f"- {item['label']}: {item['url']}" for item in skeb_links)
    else:
        lines.append("- 本地资料未发现；需要最新外链时点击“在线获取资料”")
    if other_links:
        lines.extend(["", "其他外链:"])
        lines.extend(f"- {item['label']}: {item['url']}" for item in other_links)
    warnings = [str(item) for item in (payload.get("warnings") or []) if str(item).strip()]
    if warnings:
        lines.extend(["", "检查:"])
        lines.extend(f"- {warning}" for warning in warnings)
    return "\n".join(lines)


def _plain_text_html(text: str) -> str:
    escaped = html.escape(text).replace("\n", "<br>")
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body {{
      margin: 0;
      padding: 8px;
      background: #ffffff;
      color: #1f2937;
      font-family: "Microsoft YaHei UI", "Segoe UI", Arial, sans-serif;
      font-size: 13px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>{escaped}</body>
</html>"""


def _link_items(raw_items: object) -> list[dict[str, str]]:
    if not isinstance(raw_items, list):
        return []
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw_items:
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("label") or "").strip()
            label = str(item.get("label") or url or "链接").strip()
        else:
            url = str(item or "").strip()
            label = url or "链接"
        if not url or url in seen:
            continue
        seen.add(url)
        result.append({"label": label, "url": url})
    return result


def _render_link(label: str, url: str) -> str:
    if not url:
        return ""
    safe_url = html.escape(url, quote=True)
    return f'<div><span class="muted">{html.escape(label)}: </span><a href="{safe_url}">{html.escape(url)}</a></div>'


def _render_link_list(items: list[dict[str, str]], empty_text: str) -> str:
    if not items:
        return f'<div class="muted">{html.escape(empty_text)}</div>'
    links = "".join(
        f'<li><a href="{html.escape(item["url"], quote=True)}">{html.escape(item["label"])}</a></li>'
        for item in items
    )
    return f"<ul>{links}</ul>"


def _render_warning_list(warnings: list[str]) -> str:
    if not warnings:
        return ""
    items = "".join(f'<li class="warning">{html.escape(item)}</li>' for item in warnings)
    return f'<div class="section"><div class="section-title">检查</div><ul>{items}</ul></div>'


def _html_to_text(html_content: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html_content, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()
