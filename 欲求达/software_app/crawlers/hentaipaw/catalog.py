"""Validated public category and ranking routes."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit


BASE_URL = "https://zh.hentaipaw.com"
CATEGORY_SCOPES = {
    "标签分类": "tags",
    "原作分类": "parodies",
    "角色分类": "characters",
    "作者分类": "artists",
    "团队分类": "groups",
}
RANK_SCOPES = {"日榜": "daily", "周榜": "weekly", "月榜": "monthly", "总榜": "all_time"}


def normalize_catalog_url(raw):
    parsed = urlsplit(str(raw or "").strip())
    path = parsed.path.rstrip("/")
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"zh.hentaipaw.com", "hentaipaw.com"}
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not re.fullmatch(
            r"/(?:tags|parodies|characters|artists|groups)(?:/(?:[0-9]+|popular))?|/articles/rank", path
        )
    ):
        raise ValueError("请输入本站分类或排行榜链接")
    query = parse_qs(parsed.query, keep_blank_values=True)
    allowed = {"page", "t"} if path == "/articles/rank" else {"page", "type"}
    if set(query) - allowed or any(len(values) != 1 for values in query.values()):
        raise ValueError("分类或排行链接参数无效")
    if "page" in query and not re.fullmatch(r"[1-9][0-9]{0,3}", query["page"][0]):
        raise ValueError("分类页码需要在 1–9999 范围内")
    if "t" in query and query["t"][0] not in RANK_SCOPES.values():
        raise ValueError("排行榜范围无效")
    if "type" in query and query["type"][0] != "popular":
        raise ValueError("分类排序无效")
    suffix = urlencode({key: values[0] for key, values in query.items()})
    return BASE_URL + path + ("?" + suffix if suffix else "")


class CategoryParser(HTMLParser):
    def __init__(self, group):
        super().__init__(convert_charrefs=True)
        self.group = group
        self.rows = {}
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        self.active = None
        href = str(dict(attrs).get("href") or "")
        try:
            url = normalize_catalog_url(urljoin(BASE_URL, href))
        except ValueError:
            return
        if re.fullmatch(rf"/{self.group}/[0-9]+", urlsplit(url).path):
            self.active = self.rows.setdefault(
                url,
                {
                    "id": url.rsplit("/", 1)[-1],
                    "url": url,
                    "title": "",
                    "module_id": "hentaipaw",
                    "input_kind": "category",
                    "downloadable": False,
                    "read_only_reason": "分类目录，点击展开分类后选择作品下载",
                },
            )

    def handle_endtag(self, tag):
        if tag == "a":
            self.active = None

    def handle_data(self, data):
        if self.active is not None:
            self.active["title"] += data


def browse_catalog(client, query, scope, limit, options=None):
    from .client import _SearchParser

    limit = max(0, min(int(limit), 100))
    if not limit:
        return []
    query = str(query or "").strip()
    group = CATEGORY_SCOPES.get(scope, "tags")
    if scope in RANK_SCOPES:
        start_url = BASE_URL + "/articles/rank?" + urlencode({"t": RANK_SCOPES[scope]})
    elif query.startswith("https://"):
        start_url = normalize_catalog_url(query)
    elif query.isascii() and query.isdigit():
        start_url = f"{BASE_URL}/{group}/{query}"
    else:
        start_url = f"{BASE_URL}/{group}"
    initial = urlsplit(start_url)
    params = {key: values[0] for key, values in parse_qs(initial.query).items()}
    first_page = int(params.get("page", 1))
    is_index = re.fullmatch(r"/(tags|parodies|characters|artists|groups)(?:/popular)?", initial.path)
    rows, seen = [], set()
    for page in range(first_page, first_page + 10):
        current_params = dict(params)
        if page > 1:
            current_params["page"] = str(page)
        url = BASE_URL + initial.path + ("?" + urlencode(current_params) if current_params else "")
        html, final_url = client._fetch(url, options)
        try:
            final = urlsplit(normalize_catalog_url(final_url))
        except ValueError:
            raise PermissionError("分类或排行榜跳转到登录或验证页面，请检查本站会话") from None
        if final.path != initial.path or {k: v[0] for k, v in parse_qs(final.query).items()} != current_params:
            raise PermissionError("分类或排行页面发生跳转，请检查站点访问状态")
        if is_index:
            parser = CategoryParser(is_index[1])
        else:
            parser = _SearchParser()
        parser.feed(html)
        for row in parser.rows.values():
            row = dict(row, title=row["title"].strip() or row["id"], module_id="hentaipaw")
            if is_index and query and not query.startswith("https://") and query not in {"全部", "浏览"}:
                if query.casefold() not in row["title"].casefold():
                    continue
            if row["url"] not in seen:
                seen.add(row["url"])
                rows.append(row)
            if len(rows) >= limit:
                return rows
        # Index pages are browsed explicitly, so filtering never silently scans
        # thousands of categories. Article lists can fill the requested limit.
        if is_index:
            break
        next_params = dict(current_params, page=str(page + 1))
        if not any(
            urlsplit(link).netloc == "zh.hentaipaw.com"
            and urlsplit(link).path == initial.path
            and {k: v[0] for k, v in parse_qs(urlsplit(link).query).items()} == next_params
            for link in parser.links
        ):
            break
    return rows
