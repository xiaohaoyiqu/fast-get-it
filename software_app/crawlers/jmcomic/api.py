from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import threading
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from Crypto.Cipher import AES

from software_app.crawlers.common import load_cookie_file


APP_VERSION = "2.1.7"
APP_TOKEN_SECRET = "185Hcomic3PAPP7R"
APP_DATA_SECRET = "185Hcomic3PAPP7R"
API_DOMAIN_SERVER_SECRET = "diosfjckwpqpdfjkvnqQjsik"
DEFAULT_API_DOMAINS = (
    "https://www.cdnhjk.net",
    "https://www.cdngwc.cc",
    "https://www.cdngwc.net",
    "https://www.cdngwc.club",
)
API_DOMAIN_SERVERS = (
    "https://rup4a04-c01.tos-ap-southeast-1.bytepluses.com/newsvr-2025.txt",
    "https://rup4a04-c02.tos-cn-hongkong.bytepluses.com/newsvr-2025.txt",
    "https://rup4a04-c03.tos-cn-beijing.bytepluses.com.cn/newsvr-2025.txt",
)


class JmComicApiClient:
    """Small read-only client for account data that the HTML crawler cannot expose."""

    _domain_cache: tuple[str, ...] | None = None
    _domain_cache_lock = threading.Lock()

    def __init__(self, cookie_file: Path | str | None = None, proxy_url: str = "", web_domain: str = "https://18comic.vip") -> None:
        self.cookies = load_cookie_file(cookie_file)
        self.web_domain = str(web_domain or "https://18comic.vip").rstrip("/")
        self.session = requests.Session()
        # The application has an explicit proxy setting. A blank setting must be
        # a direct connection rather than inheriting HTTP(S)_PROXY from the host.
        self.session.trust_env = False
        self.session.headers.update(
            {
                "Accept-Encoding": "gzip, deflate",
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 9; V1938CT Build/PQ3A.190705.11211812; wv) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
                    "Chrome/91.0.4472.114 Safari/537.36"
                ),
            }
        )
        if self.cookies:
            self.session.cookies.update(self.cookies)
        proxy = str(proxy_url or "").strip()
        if proxy:
            self.session.proxies.update({"http": proxy, "https": proxy})

    def watch_history(self, limit: int = 15) -> list[dict]:
        self._require_login_cookie("站内浏览历史")
        return self._collect_pages("watch_list", limit, kind="album")

    def novel_favorites(self, folder_id: str = "0", order_by: str = "mr", limit: int = 15) -> list[dict]:
        self._require_login_cookie("小说收藏夹")
        return self._collect_pages(
            "novel_favorites",
            limit,
            kind="novel",
            extra_params={"folder_id": str(folder_id or "0"), "o": str(order_by or "mr")},
        )

    def _require_login_cookie(self, feature: str) -> None:
        if not self.cookies:
            raise PermissionError(f"JMComic {feature}需要先导入登录 Cookie JSON")
        if "AVS" not in self.cookies:
            if "cf_clearance" in self.cookies:
                raise PermissionError(
                    f"当前只有 Cloudflare 通行 Cookie，没有检测到 AVS 登录会话；无法读取 JMComic {feature}"
                )
            raise PermissionError(f"没有检测到 AVS 登录会话；无法读取 JMComic {feature}")

    def _collect_pages(
        self,
        endpoint: str,
        limit: int,
        *,
        kind: str,
        extra_params: dict[str, str] | None = None,
    ) -> list[dict]:
        wanted = max(1, min(int(limit), 1000))
        rows: list[dict] = []
        seen: set[str] = set()
        for page in range(1, 51):
            payload = self._get(endpoint, {"page": str(page), **(extra_params or {})})
            items = self._payload_items(payload)
            if not items:
                break
            before = len(rows)
            for item in items:
                row = self._candidate(item, kind, endpoint, self.web_domain)
                item_id = str(row.get("id") or "") if row else ""
                if not row or not item_id or item_id in seen:
                    continue
                seen.add(item_id)
                rows.append(row)
                if len(rows) >= wanted:
                    return rows
            if len(rows) == before:
                break
        return rows

    def _get(self, endpoint: str, params: dict[str, str]) -> object:
        timestamp = str(int(time.time()))
        headers = {
            "token": hashlib.md5(f"{timestamp}{APP_TOKEN_SECRET}".encode()).hexdigest(),
            "tokenparam": f"{timestamp},{APP_VERSION}",
        }
        errors: list[str] = []
        unauthorized = False
        forbidden = False
        for domain in self._api_domains():
            url = urljoin(domain.rstrip("/") + "/", endpoint.lstrip("/"))
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=(5, 25))
                if response.status_code == 401:
                    unauthorized = True
                    errors.append(f"{domain}: HTTP 401（账号会话未通过）")
                    continue
                if response.status_code == 403:
                    forbidden = True
                    errors.append(f"{domain}: HTTP 403（访问被拒绝）")
                    continue
                response.raise_for_status()
                try:
                    envelope = response.json()
                except ValueError as exc:
                    content_type = str(response.headers.get("Content-Type") or "").casefold()
                    body_start = str(response.text or "")[:512].casefold()
                    if "text/html" in content_type or "<html" in body_start or "<!doctype" in body_start:
                        raise RuntimeError(
                            "接口返回了网页或访问验证页，而不是账号数据；当前节点可能被 Cloudflare 或站点验证拦截"
                        ) from exc
                    raise RuntimeError("接口没有返回有效的 JSON 账号数据") from exc
                if not isinstance(envelope, dict) or int(envelope.get("code") or 0) != 200:
                    message = envelope.get("message") or envelope.get("msg") if isinstance(envelope, dict) else "响应格式无效"
                    raise RuntimeError(str(message or "接口拒绝请求"))
                data = envelope.get("data")
                if isinstance(data, (dict, list)):
                    return data
                if not isinstance(data, str) or not data:
                    raise RuntimeError("接口没有返回数据")
                return json.loads(self._decrypt(data, timestamp))
            except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
                errors.append(f"{domain}: {exc}")
        if unauthorized:
            raise PermissionError("JMComic 账号接口返回 HTTP 401；AVS 已失效、不是 AVS Cookie，或来自不同的网页域名")
        if forbidden:
            raise PermissionError("JMComic 账号接口返回 HTTP 403；请检查 AVS、站点域名、代理或访问验证状态")
        detail = errors[-1] if errors else "没有可用的 API 域名"
        raise RuntimeError(f"JMComic 账号接口读取失败；请检查 AVS、代理或稍后重试。{detail}")

    def _api_domains(self) -> tuple[str, ...]:
        latest = self._latest_api_domains()
        return tuple(dict.fromkeys((*latest, *DEFAULT_API_DOMAINS)))

    def _latest_api_domains(self) -> tuple[str, ...]:
        cached = type(self)._domain_cache
        if cached is not None:
            return cached
        with type(self)._domain_cache_lock:
            cached = type(self)._domain_cache
            if cached is not None:
                return cached
            discovered: tuple[str, ...] = ()
            # Do not reuse the account session here: the domain-list service
            # does not need AVS and must never receive an account cookie.
            discovery = requests.Session()
            discovery.trust_env = False
            discovery.headers.update({"User-Agent": self.session.headers.get("User-Agent", "Mozilla/5.0")})
            discovery.proxies.update(self.session.proxies)
            for source in API_DOMAIN_SERVERS:
                try:
                    response = discovery.get(source, timeout=(3, 8))
                    response.raise_for_status()
                    text = str(response.text or "")
                    while text and not text[0].isascii():
                        text = text[1:]
                    payload = json.loads(self._decrypt(text.strip(), "", API_DOMAIN_SERVER_SECRET))
                    discovered = self._normalize_api_domains(payload.get("Server") if isinstance(payload, dict) else None)
                    if discovered:
                        break
                except (OSError, ValueError, RuntimeError, requests.RequestException):
                    continue
            discovery.close()
            type(self)._domain_cache = discovered
            return discovered

    @staticmethod
    def _normalize_api_domains(values: object) -> tuple[str, ...]:
        if not isinstance(values, list):
            return ()
        domains: list[str] = []
        for value in values:
            raw = str(value or "").strip()
            if not raw:
                continue
            parsed = urlparse(raw if "://" in raw else f"https://{raw}")
            host = str(parsed.hostname or "").casefold()
            if parsed.scheme != "https" or not host or parsed.username or parsed.password:
                continue
            if host == "localhost" or host.endswith(".local"):
                continue
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                if not address.is_global:
                    continue
            port = f":{parsed.port}" if parsed.port else ""
            origin = f"https://{host}{port}"
            if origin not in domains:
                domains.append(origin)
        return tuple(domains)

    @staticmethod
    def _decrypt(encoded: str, timestamp: str, secret: str = APP_DATA_SECRET) -> str:
        key = hashlib.md5(f"{timestamp}{secret}".encode()).hexdigest().encode()
        decrypted = AES.new(key, AES.MODE_ECB).decrypt(base64.b64decode(encoded))
        padding = decrypted[-1]
        if padding < 1 or padding > AES.block_size:
            raise ValueError("JMComic API 响应填充无效")
        return decrypted[:-padding].decode("utf-8")

    @staticmethod
    def _payload_items(payload: object) -> list[dict]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        for key in ("list", "content", "data", "novels", "favorites", "history"):
            value = payload.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
        return []

    @staticmethod
    def _candidate(item: dict, kind: str, source: str, web_domain: str = "https://18comic.vip") -> dict | None:
        keys = ("nid", "novel_id", "id") if kind == "novel" else ("aid", "album_id", "id")
        item_id = next((str(item.get(key) or "").strip() for key in keys if str(item.get(key) or "").strip()), "")
        if not item_id.isdigit():
            return None
        title = str(item.get("name") or item.get("title") or item_id).strip()
        author = item.get("author") or item.get("authors") or ""
        if isinstance(author, list):
            author = ", ".join(str(value) for value in author if value)
        image = str(item.get("image") or item.get("cover") or item.get("image_url") or "").strip()
        if image and not image.startswith(("http://", "https://")):
            image = ""
        is_novel = kind == "novel"
        return {
            "id": item_id,
            "title": title,
            "url": f"{web_domain.rstrip('/')}/{'novel' if is_novel else 'album'}/{item_id}",
            "cover_url": image,
            "avatar_url": image,
            "author_name": str(author).strip(),
            "type": "小说收藏夹" if is_novel else "站内浏览历史",
            "search_mode": "小说收藏夹" if is_novel else "站内浏览历史",
            "input_kind": "novel" if is_novel else "album",
            "source": "JMComic 小说收藏" if is_novel else "JMComic 站内浏览历史",
            "downloadable": not is_novel,
            "read_only_reason": "JMComic 小说正文下载尚未接入；可查看或加入本地黑名单" if is_novel else "",
        }


__all__ = ["JmComicApiClient", "DEFAULT_API_DOMAINS", "API_DOMAIN_SERVERS"]
