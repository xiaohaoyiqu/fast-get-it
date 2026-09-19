from __future__ import annotations

import json
import ipaddress
import threading
import uuid
from io import BytesIO
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.settings import JMCOMIC_DATA_DIR, JMCOMIC_ROOT
from software_app.crawlers.jmcomic import JmComicCrawler
from software_app.crawlers.jmcomic.client import decode_scrambled_image, jm_response_text, segmentation_count
from software_app.crawlers.common import load_cookie_file, load_request_header_profile


class JmComicNativeAdapter(CrawlerAdapter):
    max_concurrency = 3

    def __init__(self) -> None:
        self.runtime_data_dir = Path(JMCOMIC_DATA_DIR)
        self.info = ModuleInfo(
            module_id="jmcomic",
            display_name="JMComic",
            description="软件内置 JMComic 爬虫，支持漫画、章节、搜索、分类排行、收藏、追更、观看记录和站内屏蔽标签读取。",
            stage="ready",
            capabilities=(
                "preview", "search", "download", "native", "scramble-decode",
                "account-lists", "blocklist-import",
            ),
            script_root=JMCOMIC_ROOT,
        )

    def _client(self, options: dict | None = None) -> JmComicCrawler:
        options = options or {}
        browser_headers = options.get("browser_headers")
        if not isinstance(browser_headers, dict):
            browser_headers = self.browser_headers()
        return JmComicCrawler(
            domain=str(options.get("domain") or "https://18comic.vip"),
            cookie_file=options.get("cookie") or self.runtime_data_dir / "cookies.json",
            proxy_url=str(options.get("proxy_url") or ""),
            user_agent=str(options.get("user_agent") or ""),
            browser_headers=browser_headers,
        )

    def browser_headers(self) -> dict[str, str]:
        path = self.runtime_data_dir / "browser_headers.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return {str(key): str(value) for key, value in payload.items()} if isinstance(payload, dict) else {}

    def validate(self) -> list[str]:
        return [
            "站点域名、Cloudflare 验证或页面结构变化时，可能需要重新导入同一浏览器会话的请求标头",
            "漫画/小说收藏、追更、观看记录和站内屏蔽标签需要 AVS 与个人主页用户名；cf_clearance 只代表通过 Cloudflare 验证",
        ]

    def connection_status(self, options: dict | None = None) -> dict[str, str]:
        client = self._client(options)
        response = client._get_response(client.domain + "/", timeout=(10, 45))
        page = jm_response_text(response)
        if "<html" not in page.casefold():
            raise RuntimeError("JMComic 站点没有返回网页；请检查当前域名或代理")
        if "Restricted Access!" in page:
            raise PermissionError("JMComic 拒绝当前地区或网络访问；请更换可用域名或代理")
        if "Could not connect to mysql!" in page:
            raise RuntimeError("JMComic 服务器内部错误；请稍后重试")
        effective = urlparse(str(response.url or client.domain))
        effective_origin = f"{effective.scheme}://{effective.netloc}" if effective.scheme and effective.netloc else client.domain
        route = (
            f"入口 {client.domain}，实际访问 {effective_origin}"
            if effective_origin.rstrip("/") != client.domain.rstrip("/")
            else client.domain
        )
        status = self.cookie_status()
        return {"message": f"JMComic 站点已返回网页：{route}；{status['message']}", "effective_domain": effective_origin}

    def fetch_preview_image_bytes(
        self, url: str, options: dict | None = None, *, photo_id: str = "", scramble_id: str = ""
    ) -> bytes:
        parsed = urlparse(str(url or ""))
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme != "https" or not host or host == "localhost" or host.endswith(".local") or not parsed.path.startswith("/media/"):
            raise ValueError("JMComic 预览图必须是站点返回的 HTTPS 媒体地址")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if not address.is_global:
                raise ValueError("JMComic 预览图地址无效")
        if photo_id and not parsed.path.startswith(f"/media/photos/{photo_id}/"):
            raise ValueError("JMComic 预览图与当前章节不一致")
        client = self._client(options)
        response = client._get_response(url, timeout=(5, 30))
        if len(response.content) > 25 * 1024 * 1024:
            raise ValueError("JMComic 预览图超过 25 MiB")
        segments = segmentation_count(scramble_id, photo_id, Path(parsed.path).name) if photo_id else 0
        with decode_scrambled_image(response.content, segments) as image:
            image.thumbnail((512, 512), Image.Resampling.LANCZOS)
            with BytesIO() as output:
                with image.convert("RGB") as rgb:
                    rgb.save(output, format="JPEG", quality=85)
                return output.getvalue()

    def can_handle(self, raw_target: str) -> bool:
        value = raw_target.strip().lower()
        return bool(value and ("18comic" in value or "jmcomic" in value or value.startswith("jm") or value.isdigit()))

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        preview = self._client(options).preview(raw_target, options)
        return TargetPreview(**{**preview.__dict__, "warnings": [*preview.warnings, *self.validate()]})

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        self._client(task.options).download(task, callbacks, cancel_event)

    def search_targets(self, query: str, limit: int = 15, options: dict | None = None) -> list[dict]:
        options = options or {}
        return self._client(options).search(
            query,
            limit=min(limit, 15),
            search_mode=str(options.get("search_mode") or ""),
            options=options,
        )

    def import_cookie_file(self, source: Path | str) -> int:
        source_path = Path(source).expanduser()
        cookies = load_cookie_file(source_path)
        if not cookies:
            raise ValueError(
                "Cookie 文件为空或格式无效；支持 JSON、Netscape cookies.txt，或开发者工具复制的请求标头"
            )
        self.runtime_data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.runtime_data_dir / "cookies.json"
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        browser_headers = load_request_header_profile(source_path)
        header_path = self.runtime_data_dir / "browser_headers.json"
        if browser_headers:
            temporary = header_path.with_name(f".{header_path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(json.dumps(browser_headers, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(header_path)
            finally:
                temporary.unlink(missing_ok=True)
        else:
            header_path.unlink(missing_ok=True)
        return len(cookies)

    def cookie_status(self) -> dict[str, object]:
        cookies = load_cookie_file(self.runtime_data_dir / "cookies.json")
        names = {str(name) for name in cookies}
        has_login = "AVS" in names
        has_clearance = "cf_clearance" in names
        if has_login:
            message = "已检测到 AVS 登录会话候选；是否有效会在读取收藏夹或站内历史时在线验证"
        elif has_clearance:
            message = "已检测到 cf_clearance，但没有 AVS；只能表示通过 Cloudflare 验证，不能读取账号收藏和历史"
        elif names:
            message = "Cookie 已保存，但没有检测到 AVS 登录会话"
        else:
            message = "尚未导入 JMComic Cookie"
        return {
            "count": len(cookies),
            "has_login": has_login,
            "has_login_candidate": has_login,
            "has_clearance": has_clearance,
            "message": message,
        }
