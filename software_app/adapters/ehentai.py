from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.settings import EHENTAI_DATA_DIR
from software_app.crawlers.common import load_cookie_file
from software_app.crawlers.ehentai import EhentaiClient, parse_ehentai_target


class EhentaiNativeAdapter(CrawlerAdapter):
    max_concurrency = 1

    def __init__(self) -> None:
        self.runtime_data_dir = Path(EHENTAI_DATA_DIR)
        self.info = ModuleInfo(
            module_id="ehentai",
            display_name="E-Hentai / ExHentai",
            description="搜索并预览表站/里站画廊，管理单项收藏，保存展示图和官方元数据；种子可选且不自动购买归档。",
            stage="alpha",
            capabilities=("preview", "search", "download", "images", "native", "cookie-import", "metadata", "torrent",
                          "favorite-management", "account-management"),
        )

    def _cookie_file(self, site: str) -> Path:
        filename = "exhentai-cookies.json" if site == "exhentai" else "e-hentai-cookies.json"
        return self.runtime_data_dir / filename

    def _client(self, options: dict | None = None, *, site: str = "e-hentai") -> EhentaiClient:
        client = EhentaiClient(
            cookie_file=self._cookie_file(site),
            proxy_url=str((options or {}).get("proxy_url") or ""),
            site=site,
        )
        identity_name = "exhentai-browser.json" if site == "exhentai" else "e-hentai-browser.json"
        try:
            identity = json.loads((self.runtime_data_dir / identity_name).read_text(encoding="utf-8"))
            user_agent = str(identity.get("user_agent") or "").strip() if isinstance(identity, dict) else ""
        except (OSError, json.JSONDecodeError):
            user_agent = ""
        if user_agent and "\r" not in user_agent and "\n" not in user_agent and len(user_agent) <= 512:
            client.session.headers["User-Agent"] = user_agent
        return client

    def validate(self) -> list[str]:
        warnings = [
            "官方 ZIP 归档可能扣除 GP/Credits，当前版本不会自动购买；种子文件只保存，不在软件内启动 P2P",
            "ExHentai 里站登录与访问尽量使用非亚洲代理/VPN，并保持登录与后续访问使用同一出口",
        ]
        if not self.cookie_status("e-hentai")["has_login"]:
            warnings.append("未导入 E-Hentai 表站账号 Cookie；表站收藏夹不可用")
        if not self.cookie_status("exhentai")["has_login"]:
            warnings.append("未导入独立的 ExHentai 里站账号 Cookie；不会复用表站账号")
        if (self.runtime_data_dir / "cookies.json").exists():
            warnings.append("检测到旧版未区分站点的 cookies.json；为防止账号串用，当前版本不会自动读取")
        return warnings

    def can_handle(self, raw_target: str) -> bool:
        value = str(raw_target or "").strip()
        if value.startswith(("http://", "https://")):
            host = str(urlparse(value).hostname or "").casefold()
            if host not in {"e-hentai.org", "www.e-hentai.org", "exhentai.org", "www.exhentai.org"}:
                return False
        elif not value or not re.fullmatch(r"\d+/[0-9a-fA-F]{10}", value.strip("/")):
            return False
        try:
            parse_ehentai_target(value)
            return True
        except ValueError:
            return False

    @staticmethod
    def _default_site(options: dict | None) -> str:
        scope = str((options or {}).get("search_mode") or (options or {}).get("content_scope") or "")
        return "exhentai" if "里站" in scope else "e-hentai"

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        target = parse_ehentai_target(raw_target, default_site=self._default_site(options))
        return self._client(options, site=target.site).preview(target, live=bool((options or {}).get("live")))

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        mode = str((options or {}).get("search_mode") or "表站关键词")
        site = "exhentai" if "里站" in mode else "e-hentai"
        return self._client(options, site=site).search(
            query, mode, limit,
            filter_ai=bool((options or {}).get("filter_ai", True)),
        )

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        target = parse_ehentai_target(task.target, default_site=self._default_site(task.options))
        self._client(task.options, site=target.site).download(task, callbacks, cancel_event)

    def fetch_preview_image_bytes(self, url: str, options: dict | None = None) -> bytes:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not (host == "ehgt.org" or host.endswith(".ehgt.org")):
            raise ValueError("EH 预览图只允许从官方 ehgt.org 图片主机读取")
        # EH 缩略图位于独立的 ehgt.org 主机，不需要登录 Cookie。
        response = EhentaiClient(
            proxy_url=str((options or {}).get("proxy_url") or ""),
        ).session.get(url, timeout=(10, 30))
        response.raise_for_status()
        content_type = str(response.headers.get("content-type") or "").casefold()
        payload = response.content
        if not content_type.startswith("image/") or not payload or len(payload) > 10 * 1024 * 1024:
            raise ValueError("EH 预览图响应类型无效、为空或超过 10 MiB")
        return payload

    def set_favorite(self, raw_target: str, category: int | None, note: str = "",
                     options: dict | None = None) -> dict[str, object]:
        target = parse_ehentai_target(raw_target)
        if target.kind != "gallery":
            raise ValueError("EH 收藏管理只支持具体画廊链接")
        return self._client(options, site=target.site).set_favorite(target, category, note)

    def favorite_state(self, raw_target: str, options: dict | None = None) -> dict[str, object]:
        target = parse_ehentai_target(raw_target)
        if target.kind != "gallery":
            raise ValueError("EH 收藏状态只支持具体画廊链接")
        return self._client(options, site=target.site).favorite_state(target)

    def import_cookie_file(self, source: Path | str, *, site: str = "e-hentai") -> int:
        from software_app.crawlers.common import load_cookie_file

        cookies = load_cookie_file(source)
        if not cookies:
            raise ValueError("Cookie 文件为空或格式无效")
        normalized_site = "exhentai" if str(site).casefold().startswith("ex") else "e-hentai"
        required = {"ipb_member_id", "ipb_pass_hash"}
        if normalized_site == "exhentai":
            required.add("igneous")
        if not required.issubset(cookies):
            raise ValueError(f"{'ExHentai 里站' if normalized_site == 'exhentai' else 'E-Hentai 表站'} Cookie 不完整")
        self.runtime_data_dir.mkdir(parents=True, exist_ok=True)
        destination = self._cookie_file(normalized_site)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return len(cookies)

    def cookie_status(self, site: str = "e-hentai") -> dict[str, object]:
        normalized_site = "exhentai" if str(site).casefold().startswith("ex") else "e-hentai"
        cookies = load_cookie_file(self._cookie_file(normalized_site))
        has_login = {"ipb_member_id", "ipb_pass_hash"}.issubset(cookies)
        has_exhentai = has_login and "igneous" in cookies
        message = (
            "已检测到独立的 ExHentai 里站 Cookie；有效性会在访问时验证"
            if normalized_site == "exhentai" and has_exhentai else
            "ExHentai 里站 Cookie 不完整或尚未导入"
            if normalized_site == "exhentai" else
            "已检测到 E-Hentai 表站登录 Cookie；有效性会在访问时验证"
            if has_login else
            "尚未导入 E-Hentai 表站 Cookie"
        )
        complete_login = has_exhentai if normalized_site == "exhentai" else has_login
        return {"count": len(cookies), "has_login": complete_login, "has_exhentai": has_exhentai, "message": message}
