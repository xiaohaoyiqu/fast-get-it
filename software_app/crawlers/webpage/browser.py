from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service

from software_app.core.adapter import TaskCancelled
from software_app.core.browser_driver import ensure_chromedriver

from .client import MediaCandidate, normalize_page_url


COLLECT_RENDERED_MEDIA_SCRIPT = r"""
const requestedUrl = String(arguments[0] || "");
const statusMatch = requestedUrl.match(/\/status(?:es)?\/(\d+)/i);
let scope = document;
if (statusMatch) {
  const wanted = statusMatch[1];
  const articles = Array.from(document.querySelectorAll("article[data-testid='tweet'], article"));
  const exact = articles.find(article => Array.from(article.querySelectorAll("a[href*='/status/']")).some(anchor => {
    const href = anchor.getAttribute("href") || "";
    const match = href.match(/\/status(?:es)?\/(\d+)/i);
    return match && match[1] === wanted;
  }));
  if (exact) scope = exact;
}

const rows = [];
const seen = new Set();
function add(raw, kind) {
  try {
    const url = new URL(String(raw || ""), location.href).href;
    if (!/^https?:/i.test(url) || seen.has(url)) return;
    seen.add(url);
    rows.push({url, kind});
  } catch (_) {}
}

scope.querySelectorAll("img").forEach(img => {
  const values = [img.currentSrc, img.src, img.getAttribute("data-src"), img.getAttribute("data-original")];
  values.forEach(value => add(value, "image"));
  const srcset = img.getAttribute("srcset") || "";
  srcset.split(",").forEach(part => add(part.trim().split(/\s+/)[0], "image"));
});
scope.querySelectorAll("video").forEach(node => {
  add(node.poster, "image");
  add(node.currentSrc || node.src, "video");
});
scope.querySelectorAll("audio").forEach(node => add(node.currentSrc || node.src, "audio"));
scope.querySelectorAll("source").forEach(node => add(node.src, "media"));
scope.querySelectorAll("a[href*='pbs.twimg.com/media/']").forEach(node => add(node.href, "image"));

if (!statusMatch) {
  document.querySelectorAll("meta[property='og:image'],meta[name='twitter:image']")
    .forEach(node => add(node.content, "metadata"));
}
return {url: location.href, title: document.title || "", media: rows, exactStatus: Boolean(statusMatch)};
"""

FETCH_RENDERED_MEDIA_SCRIPT = r"""
const mediaUrl = String(arguments[0] || "");
const maxBytes = Number(arguments[1] || 0);
const done = arguments[arguments.length - 1];
fetch(mediaUrl, {
  credentials: "omit",
  cache: "force-cache",
  referrer: location.href,
  referrerPolicy: "strict-origin-when-cross-origin"
}).then(response => {
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.blob().then(blob => ({response, blob}));
}).then(({response, blob}) => {
  if (maxBytes > 0 && blob.size > maxBytes) throw new Error("resource-too-large");
  const reader = new FileReader();
  reader.onerror = () => done({ok: false, error: "file-reader-failed"});
  reader.onload = () => {
    const value = String(reader.result || "");
    const comma = value.indexOf(",");
    done({
      ok: comma >= 0,
      data: comma >= 0 ? value.slice(comma + 1) : "",
      contentType: blob.type || response.headers.get("content-type") || "",
      finalUrl: response.url || mediaUrl,
      size: blob.size
    });
  };
  reader.readAsDataURL(blob);
}).catch(error => done({ok: false, error: String(error && error.message || error)}));
"""


@dataclass(frozen=True)
class BrowserMediaPayload:
    data: bytes
    content_type: str
    final_url: str


@dataclass(frozen=True)
class BrowserPageResult:
    final_url: str
    title: str
    candidates: list[MediaCandidate]
    media_payloads: dict[str, BrowserMediaPayload] = field(default_factory=dict)


def media_candidates_from_browser_payload(payload: object) -> list[MediaCandidate]:
    data = payload if isinstance(payload, dict) else {}
    result: list[MediaCandidate] = []
    seen: set[str] = set()
    for item in data.get("media", []):
        if not isinstance(item, dict):
            continue
        try:
            url = normalize_page_url(str(item.get("url") or ""))
        except ValueError:
            continue
        # X detail pages contain avatars and emoji beside the post. Its actual
        # attached images use the media CDN path; keep only those for an exact
        # status page.
        if data.get("exactStatus"):
            parsed = urlparse(url)
            if (parsed.hostname or "").lower() != "pbs.twimg.com" or "/media/" not in parsed.path:
                continue
            query_items = parse_qsl(parsed.query, keep_blank_values=True)
            image_format = next((value for key, value in query_items if key == "format"), "")
            path = parsed.path
            if image_format and not Path(path).suffix:
                path = f"{path}.{image_format.lower()}"
            query = [(key, value) for key, value in query_items if key not in {"name", "format"}]
            query.append(("name", "orig"))
            url = parsed._replace(path=path, query=urlencode(query)).geturl()
        if url in seen:
            continue
        seen.add(url)
        result.append(MediaCandidate(url, str(item.get("kind") or "image")))
    return result


class DynamicPageBrowser:
    """Open a selected result page and collect media from its rendered DOM."""

    def __init__(self) -> None:
        self._drivers: dict[str, webdriver.Chrome] = {}
        self._lock = threading.Lock()

    def collect(
        self,
        task_id: str,
        url: str,
        cancel_event: threading.Event,
        options: dict | None = None,
    ) -> BrowserPageResult:
        options = options or {}
        driver = self._start_driver(options)
        with self._lock:
            self._drivers[task_id] = driver
        try:
            self._open_with_optional_cookies(driver, url, options, cancel_event)
            wait_seconds = max(3, min(int(options.get("browser_wait_seconds") or 20), 120))
            deadline = time.monotonic() + wait_seconds
            last_count = -1
            stable_rounds = 0
            payload: dict = {}
            while time.monotonic() < deadline:
                self._check_cancel(cancel_event)
                try:
                    payload = driver.execute_script(COLLECT_RENDERED_MEDIA_SCRIPT, url) or {}
                except WebDriverException as exc:
                    if cancel_event.is_set():
                        raise TaskCancelled("任务已取消") from exc
                    raise RuntimeError("候选网页浏览器已关闭，任务不能继续") from exc
                candidates = media_candidates_from_browser_payload(payload)
                count = len(candidates)
                stable_rounds = stable_rounds + 1 if count == last_count else 0
                last_count = count
                if count and stable_rounds >= 2:
                    return self._build_result(driver, payload, url, candidates, options, cancel_event)
                try:
                    driver.execute_script("window.scrollBy(0, Math.max(500, window.innerHeight * 0.7));")
                except WebDriverException:
                    pass
                cancel_event.wait(0.6)

            candidates = media_candidates_from_browser_payload(payload)
            return self._build_result(driver, payload, url, candidates, options, cancel_event)
        finally:
            with self._lock:
                self._drivers.pop(task_id, None)
            try:
                driver.quit()
            except Exception:
                pass

    def cancel(self, task_id: str) -> None:
        with self._lock:
            driver = self._drivers.get(task_id)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    def _build_result(
        self,
        driver,
        payload: dict,
        requested_url: str,
        candidates: list[MediaCandidate],
        options: dict,
        cancel_event: threading.Event,
    ) -> BrowserPageResult:
        final_url = normalize_page_url(str(payload.get("url") or requested_url))
        media_payloads: dict[str, BrowserMediaPayload] = {}
        # pbs.twimg.com can display normally in Chrome while resetting a separate
        # requests/TLS connection. Read exact-status attachments through the same
        # already-open browser, then let the crawler persist and classify them.
        if payload.get("exactStatus") and options.get("browser_media_fallback", True):
            max_files = max(1, min(int(options.get("max_files") or 50), 200))
            max_bytes = max(1, min(int(options.get("max_file_mb") or 200), 2048)) * 1024 * 1024
            script_timeout = max(30, min(int(options.get("media_read_timeout") or 120), 1200))
            try:
                driver.set_script_timeout(script_timeout)
            except WebDriverException:
                pass
            for candidate in candidates[:max_files]:
                self._check_cancel(cancel_event)
                try:
                    raw = driver.execute_async_script(FETCH_RENDERED_MEDIA_SCRIPT, candidate.url, max_bytes) or {}
                    if not isinstance(raw, dict) or not raw.get("ok") or not raw.get("data"):
                        continue
                    data = base64.b64decode(str(raw["data"]), validate=True)
                    if not data or len(data) > max_bytes:
                        continue
                    media_payloads[candidate.url] = BrowserMediaPayload(
                        data=data,
                        content_type=str(raw.get("contentType") or "").split(";", 1)[0].strip().lower(),
                        final_url=normalize_page_url(str(raw.get("finalUrl") or candidate.url)),
                    )
                except (ValueError, WebDriverException):
                    continue
        return BrowserPageResult(final_url, str(payload.get("title") or ""), candidates, media_payloads)

    @staticmethod
    def _start_driver(options: dict) -> webdriver.Chrome:
        driver_path = ensure_chromedriver(auto_download=True)
        if not driver_path:
            raise FileNotFoundError("没有可用的 ChromeDriver")
        chrome_options = ChromeOptions()
        for argument in (
            "--disable-extensions",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--lang=zh-CN",
            "--window-size=1365,900",
        ):
            chrome_options.add_argument(argument)
        if options.get("headless"):
            chrome_options.add_argument("--headless=new")
        proxy_url = str(options.get("proxy_url") or "").strip()
        if proxy_url:
            chrome_options.add_argument(f"--proxy-server={proxy_url}")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        driver = webdriver.Chrome(service=Service(str(driver_path)), options=chrome_options)
        driver.set_page_load_timeout(max(10, min(int(options.get("page_load_timeout") or 30), 120)))
        driver.execute_cdp_cmd(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"},
        )
        return driver

    @staticmethod
    def _open_with_optional_cookies(driver, url: str, options: dict, cancel_event: threading.Event) -> None:
        cookie_file = str(options.get("cookie_file") or "").strip()
        host = (urlparse(url).hostname or "").lower()
        if not cookie_file and (host == "x.com" or host.endswith(".x.com") or host.endswith("twitter.com")):
            from software_app.core.settings import TWITTER_DATA_DIR

            default_cookie = Path(TWITTER_DATA_DIR) / "X_cookie.json"
            if default_cookie.is_file():
                cookie_file = str(default_cookie)
        if cookie_file and Path(cookie_file).is_file():
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}/"
            DynamicPageBrowser._safe_get(driver, origin)
            try:
                cookies = json.loads(Path(cookie_file).read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                cookies = []
            if isinstance(cookies, dict):
                cookies = [
                    {"name": str(name), "value": str(value), "domain": host, "path": "/"}
                    for name, value in cookies.items()
                    if str(name).strip() and value is not None
                ]
            for raw_cookie in cookies if isinstance(cookies, list) else []:
                if not isinstance(raw_cookie, dict) or not raw_cookie.get("name"):
                    continue
                cookie = dict(raw_cookie)
                if "expirationDate" in cookie and "expiry" not in cookie:
                    try:
                        cookie["expiry"] = int(float(cookie.pop("expirationDate")))
                    except (TypeError, ValueError):
                        cookie.pop("expirationDate", None)
                for key in ("hostOnly", "session", "storeId", "id"):
                    cookie.pop(key, None)
                try:
                    driver.add_cookie(cookie)
                except Exception:
                    continue
        DynamicPageBrowser._check_cancel(cancel_event)
        DynamicPageBrowser._safe_get(driver, url)

    @staticmethod
    def _safe_get(driver, url: str) -> None:
        try:
            driver.get(url)
        except TimeoutException:
            driver.execute_script("window.stop()")

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise TaskCancelled("任务已取消")
