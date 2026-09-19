from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, ProgressEvent, TargetPreview
from software_app.core.settings import INSTAGRAM_DATA_DIR
from software_app.crawlers.common import load_cookie_file
from software_app.crawlers.instagram import InstagramClient, parse_instagram_target
from software_app.crawlers.webpage import WebPageCrawler


INSTAGRAM_MEDIA_HOSTS = ("cdninstagram.com", "fbcdn.net")


class InstagramNativeAdapter(CrawlerAdapter):
    max_concurrency = 2

    def __init__(self) -> None:
        self.runtime_data_dir = Path(INSTAGRAM_DATA_DIR)
        self._browser_fallback = WebPageCrawler()
        self.info = ModuleInfo(
            module_id="instagram",
            display_name="Instagram",
            description="读取 Instagram 账号、帖子和 Reels 页面元数据及媒体；登录页面使用本机 Cookie。",
            stage="ready",
            capabilities=("preview", "search", "download", "native", "cookie-import", "userscript-reference"),
        )

    def _client(self, options: dict | None = None) -> InstagramClient:
        return InstagramClient(
            cookie_file=self.runtime_data_dir / "cookies.json",
            proxy_url=str((options or {}).get("proxy_url") or ""),
        )

    def validate(self) -> list[str]:
        warnings = ["Meta 官方 API 面向 Business/Creator 专业账号；普通账号使用网页会话，页面变化时可能需要更新解析"]
        if not self.cookie_status()["has_session"]:
            warnings.append("尚未导入包含 sessionid 的 Instagram Cookie，登录限定页面不可用")
        return warnings

    def can_handle(self, raw_target: str) -> bool:
        try:
            parse_instagram_target(raw_target)
            return True
        except ValueError:
            return False

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        options = options or {}
        target = parse_instagram_target(raw_target)
        try:
            return self._client(options).preview(target, live=bool(options.get("live")))
        except RuntimeError:
            if not bool(options.get("live")):
                raise
            browser_options = self._browser_options(options)
            browser_options["browser_media_fallback"] = False
            result = self._browser_fallback.browser.collect(
                f"instagram-preview-{uuid.uuid4().hex}", target.url, threading.Event(), browser_options
            )
            media = [row for row in result.candidates if self._instagram_media_url(row.url)]
            if not media:
                raise RuntimeError(
                    "Instagram 页面可以打开，但浏览器渲染后仍没有发现帖子媒体；请确认登录状态或使用具体帖子/Reels 链接"
                )
            return TargetPreview(
                "instagram", target.url, result.final_url,
                title=result.title or target.username or target.shortcode,
                description=f"浏览器渲染后发现 {len(media)} 个 Instagram 媒体候选",
                metadata={
                    "input_kind": target.kind,
                    "target_id": target.shortcode or target.username,
                    "author_name": target.username,
                    "media_count": len(media),
                    "thumbnail_url": media[0].url,
                    "browser_destination": result.final_url,
                    "transport": "browser",
                },
            )

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        del limit
        mode = str((options or {}).get("search_mode") or "账号 / 用户名").replace("（规划）", "")
        return self._client(options).search(query, mode)

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        try:
            self._client(task.options).download(task, callbacks, cancel_event)
            return
        except RuntimeError as primary_error:
            callbacks.on_progress(ProgressEvent(
                task.task_id, task.module_id, "warning",
                "Instagram 静态资料不可用，正在改用登录浏览器渲染页面",
            ))
            fallback_options = self._browser_options(task.options)
            fallback_options.update({
                "browser_render": True,
                "browser_media_fallback": True,
                "_allowed_media_hosts": INSTAGRAM_MEDIA_HOSTS,
                "_output_platform": "instagram",
                "max_files": max(1, min(int(task.options.get("max_works") or 30), 100)),
            })
            try:
                self._browser_fallback.download(
                    DownloadTask(task.task_id, task.module_id, task.target, task.output_dir, fallback_options),
                    callbacks,
                    cancel_event,
                )
            except Exception as fallback_error:
                raise RuntimeError(
                    "Instagram 静态资料和浏览器渲染均未取得可下载媒体；请重新获取登录 Cookie 或使用具体帖子/Reels 链接"
                ) from fallback_error

    def cancel(self, task_id: str) -> None:
        self._browser_fallback.cancel(task_id)

    def _browser_options(self, options: dict | None = None) -> dict:
        return {
            **(options or {}),
            "cookie_file": str(self.runtime_data_dir / "cookies.json"),
            "proxy_url": str((options or {}).get("proxy_url") or ""),
            "browser_wait_seconds": max(10, min(int((options or {}).get("browser_wait_seconds") or 30), 120)),
        }

    @staticmethod
    def _instagram_media_url(url: str) -> bool:
        host = str(urlparse(str(url or "")).hostname or "").casefold()
        return any(host == allowed or host.endswith("." + allowed) for allowed in INSTAGRAM_MEDIA_HOSTS)

    def import_cookie_file(self, source: Path | str) -> int:
        cookies = load_cookie_file(source)
        if not cookies:
            raise ValueError("Cookie 文件为空或格式无效；支持 JSON、Netscape cookies.txt 或复制的 Cookie 请求头")
        self.runtime_data_dir.mkdir(parents=True, exist_ok=True)
        destination = self.runtime_data_dir / "cookies.json"
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
        return len(cookies)

    def cookie_status(self) -> dict[str, object]:
        cookies = load_cookie_file(self.runtime_data_dir / "cookies.json")
        has_session = "sessionid" in cookies
        if has_session:
            message = "已检测到 Instagram sessionid；有效性会在访问页面时验证"
        elif cookies:
            message = "Cookie 已保存，但没有检测到 sessionid"
        else:
            message = "尚未导入 Instagram Cookie"
        return {"count": len(cookies), "has_session": has_session, "message": message}
