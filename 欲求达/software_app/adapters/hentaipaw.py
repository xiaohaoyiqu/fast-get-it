from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

from software_app.core.adapter import CrawlerAdapter
from software_app.core.cookie_store import load_cookie_records
from software_app.core.models import ModuleInfo, TargetPreview
from software_app.core.settings import HENTAIPAW_DATA_DIR
from software_app.crawlers.hentaipaw import HentaiPawClient, normalize_article_url


class HentaiPawAdapter(CrawlerAdapter):
    max_concurrency = 1
    reports_files = True

    def __init__(self):
        self.runtime_data_dir = Path(HENTAIPAW_DATA_DIR)
        self.info = ModuleInfo(
            module_id="hentaipaw",
            display_name="HentaiPaw",
            description="作品信息预览、关键词搜索和阅读器原图下载；可导入独立站点 Cookie。",
            stage="ready",
            capabilities=("preview", "search", "download", "images", "native", "metadata", "cookie-import"),
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
            return False

    def preview_target(self, raw_target, options=None):
        url = normalize_article_url(raw_target)
        metadata = {"target_id": url.rsplit("/", 1)[-1], "input_kind": "article", "download_ready": True}
        if (options or {}).get("live"):
            with self._client(options) as client:
                metadata.update(client.article(url, options))
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
        with self._client(options) as client:
            return client.search(query, limit, options)

    def import_cookie_file(self, source):
        from software_app.core.browser_cookie_capture import cookie_capture_spec, import_cookie_file

        spec = replace(cookie_capture_spec("HentaiPaw"), destination=self.runtime_data_dir / "cookies.json")
        return import_cookie_file(spec, source).cookie_count

    def cookie_status(self):
        records = load_cookie_records(self.runtime_data_dir / "cookies.json")
        count = sum(bool(row["value"]) and row.get("expiry", float("inf")) > time.time() for row in records)
        return {"configured": bool(count), "cookie_count": count, "login_verified": False,
                "message": "已配置本站 Cookie，账号登录状态未验证" if count else "未配置 Cookie；公开作品可匿名读取"}
