from __future__ import annotations

import csv
import html as html_module
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from selenium import webdriver
from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
from selenium.webdriver import ChromeOptions
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By

from software_app.core.adapter import TaskCancelled
from software_app.core.browser_driver import ensure_chromedriver
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, FileRecord, ProgressEvent, TargetPreview
from software_app.crawlers.common import platform_output_root, safe_component


EXTRACT_LINKS_SCRIPT = r"""
const urls = new Set();
const imageExt = /\.(jpg|jpeg|png|webp|gif|bmp|svg|avif)$/i;
function blocked(host) {
  host = host.toLowerCase();
  return host.includes('google') || host.endsWith('gstatic.com') ||
         host.endsWith('googleusercontent.com') || host.endsWith('googleapis.com') ||
         host === 'schema.org' || host === 'www.schema.org' ||
         host === 'w3.org' || host.endsWith('.w3.org') || host === 't.co';
}
function normalize(raw) {
  if (!raw) return null;
  let parsed;
  try { parsed = new URL(raw, location.href); } catch (_) { return null; }
  const host = parsed.hostname.toLowerCase();
  if (host.includes('google.') && parsed.pathname === '/url') {
    const target = parsed.searchParams.get('q') || parsed.searchParams.get('url');
    if (target) try { parsed = new URL(target); } catch (_) { return null; }
  }
  if (host.includes('google.') && parsed.pathname.includes('/imgres')) {
    const target = parsed.searchParams.get('imgrefurl') || parsed.searchParams.get('imgurl');
    if (target) try { parsed = new URL(target); } catch (_) { return null; }
  }
  if (!['http:', 'https:'].includes(parsed.protocol) || blocked(parsed.hostname) ||
      !/^[a-z0-9.-]+$/i.test(parsed.hostname) || !parsed.hostname.includes('.') ||
      imageExt.test(parsed.pathname)) return null;
  parsed.hash = '';
  return parsed.href;
}
const attrs = ['href', 'data-url', 'data-lpage', 'data-href', 'data-source-url', 'data-context-item-id'];
// Google Lens may open an ordinary Search results page.  Heading links point
// to the actual result pages; navigation, license and footer links do not.
document.querySelectorAll('h3').forEach(heading => {
  const anchor = heading.closest('a[href]');
  const url = normalize(anchor && anchor.getAttribute('href'));
  if (url) urls.add(url);
});
if (urls.size) return Array.from(urls);
document.querySelectorAll('a[href]').forEach(anchor => {
  if (!anchor.querySelector('img')) return;
  const url = normalize(anchor.getAttribute('href'));
  if (url) urls.add(url);
});
if (urls.size) return Array.from(urls);
document.querySelectorAll('*').forEach(node => {
  attrs.forEach(name => { const url = normalize(node.getAttribute && node.getAttribute(name)); if (url) urls.add(url); });
});
return Array.from(urls);
"""

URL_IN_HTML_RE = re.compile(r"https?(?::|%3A)(?:\\?/|%2F){2}[^\s\"'<>]+", re.IGNORECASE)
IMAGE_PATH_RE = re.compile(r"\.(?:jpe?g|png|webp|gif|bmp|svg|avif)(?:$|[?#])", re.IGNORECASE)


def normalize_candidate_url(raw: str) -> str:
    value = html_module.unescape(str(raw or "").strip())
    value = value.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    if "%3a" in value.lower() and "%2f" in value.lower():
        value = unquote(value)
    value = value.rstrip("\\,;)]}")
    try:
        parsed = urlparse(value)
    except ValueError:
        return ""
    if parsed.scheme not in {"http", "https"}:
        return ""
    host = (parsed.hostname or "").lower()
    if "google" in host or host.endswith(("gstatic.com", "googleusercontent.com", "googleapis.com")):
        query = parse_qs(parsed.query)
        wrapped = next((query.get(key, [""])[0] for key in ("q", "url", "imgrefurl") if query.get(key)), "")
        if wrapped and wrapped != value:
            return normalize_candidate_url(wrapped)
        return ""
    if (
        not host
        or not re.fullmatch(r"[a-z0-9.-]+", host, re.IGNORECASE)
        or "." not in host
        or host in {"schema.org", "www.schema.org", "w3.org", "www.w3.org", "t.co", "www.t.co"}
        or host.endswith(".w3.org")
        or IMAGE_PATH_RE.search(parsed.path)
    ):
        return ""
    return value.split("#", 1)[0]


def extract_candidate_links_from_html(page_html: str, limit: int = 200) -> list[str]:
    decoded = html_module.unescape(str(page_html or ""))
    decoded = decoded.replace("\\u003d", "=").replace("\\u0026", "&").replace("\\/", "/")
    result: list[str] = []
    seen: set[str] = set()
    for raw in URL_IN_HTML_RE.findall(decoded):
        url = normalize_candidate_url(raw)
        key = url.lower()
        if not url or key in seen:
            continue
        seen.add(key)
        result.append(url)
        if len(result) >= max(1, int(limit)):
            break
    return result


def prefer_specific_pages(urls: list[str]) -> list[str]:
    """Drop a site's bare home page when Lens also exposed a concrete result page."""

    detailed_hosts = {
        (urlparse(url).hostname or "").lower()
        for url in urls
        if urlparse(url).path.strip("/")
    }
    return [
        url
        for url in urls
        if urlparse(url).path.strip("/") or (urlparse(url).hostname or "").lower() not in detailed_hosts
    ]


def is_google_challenge_page(page_html: str) -> bool:
    content = str(page_html or "").casefold()
    return (
        "unusual traffic" in content
        or "异常流量" in content
        or ("captcha" in content and ("recaptcha" in content or "getelementbyid('captcha')" in content))
    )


def is_google_image_expired_page(page_text: str) -> bool:
    content = str(page_text or "").casefold()
    return (
        "视觉搜索内容已过期" in content
        or "图片未找到" in content and "重新上传" in content
        or "visual search content has expired" in content
        or "image not found" in content and "upload" in content
    )


class BrowserClosedByUser(RuntimeError):
    pass


def is_browser_closed_error(error: BaseException) -> bool:
    message = str(error).lower()
    return isinstance(error, NoSuchWindowException) or any(
        marker in message
        for marker in ("no such window", "invalid session", "disconnected", "not connected to devtools", "target window")
    )


class GoogleImageCrawler:
    def __init__(self) -> None:
        self._drivers: dict[str, webdriver.Chrome] = {}
        self._lock = threading.Lock()

    def preview(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        image_path = Path(raw_target).expanduser()
        exists = image_path.is_file()
        return TargetPreview(
            module_id="google_image",
            raw_target=raw_target,
            normalized_target=str(image_path.resolve()) if exists else str(image_path),
            title=image_path.name or "Google 相似图片",
            description="图片有效，可以执行相似页面 URL 搜索" if exists else "找不到图片文件",
            status="ok" if exists else "error",
            metadata={"input_kind": "image", "exists": exists},
        )

    def search(
        self,
        image_path: Path | str,
        *,
        max_results: int = 50,
        headless: bool = False,
        proxy_url: str = "",
        task_id: str = "search",
        cancel_event: threading.Event | None = None,
        manual_wait_seconds: int = 60,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[list[str], str]:
        source = Path(image_path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(source)
        cancel_event = cancel_event or threading.Event()
        driver = self._start_driver(headless=headless, proxy_url=proxy_url)
        with self._lock:
            self._drivers[task_id] = driver
        try:
            self._upload(
                driver,
                source,
                cancel_event,
                control_timeout=max(20, min(int(manual_wait_seconds or 0), 300)),
                on_status=on_status,
            )
            self._wait_for_initial_results(
                driver,
                cancel_event,
                wait_seconds=max(0, min(int(manual_wait_seconds or 0), 300)),
                on_status=on_status,
            )
            links = self._collect(driver, max(1, min(int(max_results), 200)), cancel_event)
            page_html = driver.page_source
            if not links and is_google_image_expired_page(driver.find_element(By.TAG_NAME, "body").text):
                raise RuntimeError("Google 此次上传的图片已过期；请重新上传图片并重试。若仍出现，请检查代理连接是否稳定")
            if not links and is_google_challenge_page(page_html):
                raise RuntimeError("Google 要求人机验证；请在可见浏览器中完成验证后重试，可适当延长人工验证等待时间")
            if on_status and not links:
                on_status("当前页面没有提取到来源候选；请先检查是否有人机验证或图片已过期，再尝试重新上传")
            return links, page_html
        except WebDriverException as error:
            if is_browser_closed_error(error):
                raise BrowserClosedByUser("浏览器已关闭，相似搜索已结束") from error
            raise
        finally:
            with self._lock:
                self._drivers.pop(task_id, None)
            try:
                driver.quit()
            except Exception:
                pass

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "info", "正在用软件内置 Google 图片搜索器上传图片"))
        links, _page_html = self.search(
            task.target,
            max_results=int(task.options.get("max_results") or 50),
            headless=bool(task.options.get("headless", False)),
            proxy_url=str(task.options.get("proxy_url") or ""),
            task_id=task.task_id,
            cancel_event=cancel_event,
            manual_wait_seconds=int(task.options.get("manual_wait_seconds", 60)),
            on_status=lambda message: callbacks.on_progress(
                ProgressEvent(task.task_id, task.module_id, "info", message)
            ),
        )
        root = platform_output_root(task.output_dir, "google_image")
        stem = safe_component(Path(task.target).stem, "image")
        txt_path = root / f"{stem}-urls.txt"
        csv_path = root / f"{stem}-urls.csv"
        txt_path.write_text("\n".join(links) + ("\n" if links else ""), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as output:
            writer = csv.writer(output)
            writer.writerow(["index", "url"])
            writer.writerows((index, url) for index, url in enumerate(links, 1))
        for path in (txt_path, csv_path):
            callbacks.on_file(
                FileRecord(
                    path=path.resolve(),
                    module_id=task.module_id,
                    task_id=task.task_id,
                    media_type="text",
                    size=path.stat().st_size,
                    title=path.name,
                    source_url="https://images.google.com/",
                    source_id=stem,
                    metadata={"result_count": len(links), "source_image": str(Path(task.target).resolve())},
                )
            )
        callbacks.on_progress(
            ProgressEvent(task.task_id, task.module_id, "info", f"Google 来源候选：找到 {len(links)} 个页面 URL，需打开核对", metadata={"results": len(links)})
        )

    def cancel(self, task_id: str) -> None:
        with self._lock:
            driver = self._drivers.get(task_id)
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass

    @staticmethod
    def _start_driver(*, headless: bool, proxy_url: str) -> webdriver.Chrome:
        driver_path = ensure_chromedriver(auto_download=True)
        if not driver_path:
            raise FileNotFoundError("没有可用的 ChromeDriver")
        options = ChromeOptions()
        for argument in ("--disable-extensions", "--disable-gpu", "--no-sandbox", "--disable-dev-shm-usage", "--lang=zh-CN", "--window-size=1365,900"):
            options.add_argument(argument)
        if headless:
            options.add_argument("--headless=new")
        if proxy_url:
            options.add_argument(f"--proxy-server={proxy_url}")
        options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        driver = webdriver.Chrome(service=Service(str(driver_path)), options=options)
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"})
        return driver

    def _upload(
        self,
        driver: webdriver.Chrome,
        image_path: Path,
        cancel_event: threading.Event,
        *,
        control_timeout: int = 20,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        driver.get("https://images.google.com/")
        self._check_cancel(cancel_event)
        self._dismiss_popups(driver)
        if on_status:
            on_status("浏览器已打开；若出现人机验证，请在窗口中手动完成")
        button = self._wait_any(
            driver,
            [
                (By.CSS_SELECTOR, '[aria-label*="按图搜索"]'),
                (By.CSS_SELECTOR, '[aria-label*="Search by image"]'),
                (By.CSS_SELECTOR, '[aria-label*="Search with an image"]'),
                (By.XPATH, '//*[contains(@aria-label,"Google Lens") or contains(@title,"Google Lens")]'),
            ],
            cancel_event,
            timeout=max(20, control_timeout),
            on_status=on_status,
            status_message="等待 Google 搜索控件 / 人机验证",
        )
        button.click()
        file_input = self._wait_any(driver, [(By.CSS_SELECTOR, 'input[type="file"]')], cancel_event, timeout=15, require_visible=False)
        file_input.send_keys(str(image_path))

    def _wait_for_initial_results(
        self,
        driver: webdriver.Chrome,
        cancel_event: threading.Event,
        *,
        wait_seconds: int,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        """Keep the visible browser open while a user completes a challenge.

        Return as soon as candidate links appear.  This retains the legacy
        manual-verification window without imposing a fixed delay on normal
        searches.
        """
        deadline = time.monotonic() + wait_seconds
        last_reported = None
        while time.monotonic() < deadline:
            self._check_cancel(cancel_event)
            try:
                if self._read_candidate_links(driver, 1):
                    if on_status:
                        on_status("已检测到来源页面候选，正在整理链接")
                    return
            except WebDriverException as error:
                if is_browser_closed_error(error):
                    raise BrowserClosedByUser("浏览器已关闭，相似搜索已结束") from error
                raise
            remaining = max(0, int(deadline - time.monotonic()))
            report_value = remaining // 5
            if on_status and report_value != last_reported:
                on_status(f"等待页面结果 / 人机验证，剩余约 {remaining} 秒")
                last_reported = report_value
            cancel_event.wait(1.0)
        if on_status:
            on_status("验证等待结束，正在尝试读取当前页面结果")

    def _collect(self, driver: webdriver.Chrome, max_results: int, cancel_event: threading.Event) -> list[str]:
        # Anonymous Lens uploads can lose their transient image when navigating
        # to another tab. Extract source cards from the current results page.
        links: list[str] = []
        seen: set[str] = set()
        stable = 0
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and stable < 4:
            self._check_cancel(cancel_event)
            before = len(links)
            for url in self._read_candidate_links(driver, max_results):
                if url not in seen:
                    seen.add(url)
                    links.append(url)
                    if len(links) >= max_results:
                        return prefer_specific_pages(links[:max_results])
            stable = stable + 1 if len(links) == before else 0
            driver.execute_script("""
                const nodes = [document.scrollingElement, document.documentElement, document.body]
                  .concat(Array.from(document.querySelectorAll('div')));
                nodes.forEach(node => {
                  if (node && node.scrollHeight > node.clientHeight + 80) node.scrollTop = node.scrollHeight;
                });
                window.scrollBy(0, Math.max(window.innerHeight, 800));
            """)
            cancel_event.wait(1.2)
        return prefer_specific_pages(links[:max_results])

    @staticmethod
    def _read_candidate_links(driver, limit: int) -> list[str]:
        try:
            raw_values = list(driver.execute_script(EXTRACT_LINKS_SCRIPT) or [])
        except WebDriverException as error:
            if is_browser_closed_error(error):
                raise BrowserClosedByUser("浏览器已关闭，相似搜索已结束") from error
            raise
        result: list[str] = []
        seen: set[str] = set()
        for raw in raw_values:
            url = normalize_candidate_url(raw)
            if not url or url.lower() in seen:
                continue
            seen.add(url.lower())
            result.append(url)
            if len(result) >= limit:
                break
        if not result:
            result = extract_candidate_links_from_html(
                getattr(driver, "page_source", ""), limit=max(20, limit * 4)
            )[:limit]
        return prefer_specific_pages(result)

    @staticmethod
    def _wait_any(
        driver,
        locators,
        cancel_event: threading.Event,
        *,
        timeout: int,
        require_visible: bool = True,
        on_status: Callable[[str], None] | None = None,
        status_message: str = "等待页面控件",
    ):
        deadline = time.monotonic() + timeout
        last_reported = None
        while time.monotonic() < deadline:
            GoogleImageCrawler._check_cancel(cancel_event)
            try:
                for by, selector in locators:
                    for element in driver.find_elements(by, selector):
                        if not require_visible or (element.is_displayed() and element.is_enabled()):
                            return element
            except WebDriverException as error:
                if is_browser_closed_error(error):
                    raise BrowserClosedByUser("浏览器已关闭，相似搜索已结束") from error
                raise
            remaining = max(0, int(deadline - time.monotonic()))
            report_value = remaining // 5
            if on_status and report_value != last_reported:
                on_status(f"{status_message}，剩余约 {remaining} 秒")
                last_reported = report_value
            cancel_event.wait(0.4)
        raise TimeoutException("等待 Google 图片搜索控件超时")

    @staticmethod
    def _dismiss_popups(driver) -> None:
        for text in ("接受全部", "我同意", "Accept all", "I agree", "Reject all"):
            try:
                elements = driver.find_elements(By.XPATH, f'//button//*[contains(text(),"{text}")]/ancestor::button')
                if elements:
                    elements[0].click()
                    return
            except Exception:
                continue

    @staticmethod
    def _check_cancel(cancel_event: threading.Event) -> None:
        if cancel_event.is_set():
            raise TaskCancelled("任务已取消")
