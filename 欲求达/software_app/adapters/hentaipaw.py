from __future__ import annotations

import time
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit

from software_app.core.adapter import CrawlerAdapter
from software_app.core.cookie_store import load_cookie_records
from software_app.core.models import ModuleInfo, TargetPreview
from software_app.core.preview_http import read_preview_image
from software_app.core.settings import HENTAIPAW_DATA_DIR
from software_app.crawlers.hentaipaw import HentaiPawClient, normalize_article_url
from software_app.crawlers.common import make_session, request_get
from software_app.crawlers.hentaipaw.catalog import CATEGORY_SCOPES, RANK_SCOPES, browse_catalog, normalize_catalog_url
from software_app.crawlers.hentaipaw.favorites import FavoriteStore


class HentaiPawAdapter(CrawlerAdapter):
    max_concurrency = 1
    reports_files = True
    supports_media_retry = True

    def __init__(self):
        self.runtime_data_dir = Path(HENTAIPAW_DATA_DIR)
        self.info = ModuleInfo(
            module_id="hentaipaw",
            display_name="HentaiPaw",
            description="作品预览、搜索、分类、排行榜和原图下载；支持软件收藏及浏览器收藏数据导入。",
            stage="ready",
            capabilities=("preview", "search", "download", "images", "native", "metadata", "cookie-import", "local-favorites"),
        )

    def _client(self, options=None):
        return HentaiPawClient(cookie_file=self.runtime_data_dir / "cookies.json",
                              proxy_url=str((options or {}).get("proxy_url") or ""))

    def validate(self):
        return []

    def can_handle(self, raw_target):
        try:
            normalize_article_url(raw_target)
            return True
        except ValueError:
            try:
                normalize_catalog_url(raw_target)
                return True
            except ValueError:
                return False

    def preview_target(self, raw_target, options=None):
        try:
            url = normalize_article_url(raw_target)
        except ValueError:
            url = normalize_catalog_url(raw_target)
            return TargetPreview(self.module_id, raw_target, url, title="HentaiPaw 分类 / 排行",
                                 description="这是浏览目录，请展开分类或执行对应搜索，再选择具体作品下载。",
                                 metadata={"input_kind": "category", "download_ready": False})
        metadata = {"target_id": url.rsplit("/", 1)[-1], "input_kind": "article", "download_ready": True,
                    "reader_verified": False}
        if (options or {}).get("live"):
            with self._client(options) as client:
                metadata.update(client.article(url, options))
                metadata.update(page_count=len(client.reader(url, options)), reader_verified=True)
        return TargetPreview(
            module_id=self.module_id,
            raw_target=raw_target,
            normalized_target=url,
            title=metadata.get("title") or f"HentaiPaw #{metadata['target_id']}",
            description=self.info.description,
            status="ok",
            warnings=self.validate(),
            metadata=metadata,
        )

    def download(self, task, callbacks, cancel_event):
        with self._client(task.options) as client:
            client.download(task, callbacks, cancel_event)

    def search_targets(self, query, limit=20, options=None):
        scope = str((options or {}).get("search_mode") or (options or {}).get("content_scope") or "")
        if scope == "软件收藏":
            return self.favorites.rows(str(query or "").strip(), limit)
        with self._client(options) as client:
            if scope in CATEGORY_SCOPES or scope in RANK_SCOPES:
                return browse_catalog(client, query, scope, limit, options)
            if str(query).startswith("https://"):
                try:
                    catalog = normalize_catalog_url(query)
                except ValueError:
                    pass
                else:
                    return browse_catalog(client, catalog, scope, limit, options)
            return client.search(query, limit, options)

    @property
    def favorites(self):
        return FavoriteStore(self.runtime_data_dir / "favorites.json")

    def access_status(self, options=None):
        from software_app.core.security import safe_error_message

        status = self.cookie_status()
        try:
            with self._client(options) as client:
                html, _url = client._fetch("https://zh.hentaipaw.com/", options)
                accessible = bool(re.search(r"<title[^>]*>[^<]*HentaiPaw", html, re.IGNORECASE))
        except Exception as exc:
            return {**status, "accessible": False, "message": safe_error_message(exc)}
        return {**status, "accessible": accessible, "favorites_storage": "browser_localStorage",
                "message": ("公共页面可访问；账号登录状态未验证。本站收藏位于浏览器 localStorage，需单独导入。"
                            if accessible else "返回页面未能确认为 HentaiPaw，可能需要完成站点验证。")}

    def import_cookie_file(self, source):
        from software_app.core.browser_cookie_capture import cookie_capture_spec, import_cookie_file

        spec = replace(cookie_capture_spec("HentaiPaw"), destination=self.runtime_data_dir / "cookies.json")
        return import_cookie_file(spec, source).cookie_count

    def cookie_status(self):
        records = load_cookie_records(self.runtime_data_dir / "cookies.json")
        count = sum(bool(row["value"]) and row.get("expiry", float("inf")) > time.time() for row in records)
        return {"configured": bool(count), "cookie_count": count, "login_verified": False,
                "message": "已配置本站 Cookie，账号登录状态未验证" if count else "未配置 Cookie；公开作品可匿名读取"}

    def fetch_preview_image_bytes(self, url, options=None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.netloc != "cdn.imagedeliveries.com" or parsed.query or parsed.fragment
                or not re.fullmatch(r"/[0-9]+/thumbnails/cover\.webp", parsed.path)):
            raise ValueError("HentaiPaw 封面只允许从官方图片主机读取")
        with make_session(proxy_url=str((options or {}).get("proxy_url") or "")) as session:
            response = request_get(session, url, timeout=(10, 30))
            return read_preview_image(response, 5 * 1024 * 1024)
