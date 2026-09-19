"""Read-only live smoke test for public X pages and Google Lens.

Uses a public Google logo, a temporary image file and the app's saved proxy.
No account state or persistent download data is changed.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from PIL import Image
from selenium.webdriver.common.by import By

from software_app.core.settings import DB_PATH
from software_app.crawlers.google_image import GoogleImageCrawler
from software_app.crawlers.google_image.client import EXTRACT_LINKS_SCRIPT, extract_candidate_links_from_html


LOGO_URL = "https://www.google.com/images/branding/googlelogo/2x/googlelogo_color_272x92dp.png"
NASA_IMAGE_URL = "https://www.nasa.gov/wp-content/uploads/2026/04/art002e000192.jpg"
POST_URL = "https://x.com/pixiv/status/2023978485316768106"


class DiagnosticCrawler(GoogleImageCrawler):
    def __init__(self, screenshot_path: Path | None = None) -> None:
        super().__init__()
        self.screenshot_path = screenshot_path

    def _collect(self, driver, max_results, cancel_event):
        parsed = urlparse(driver.current_url)
        print("lens_browser_path", parsed.path, "query_keys", sorted(parse_qs(parsed.query)))
        visible_text = re.sub(r"\s+", " ", driver.find_element(By.TAG_NAME, "body").text)[:350]
        print("lens_visible_text_unicode", visible_text.encode("unicode_escape").decode("ascii"))
        if self.screenshot_path:
            self.screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            driver.save_screenshot(str(self.screenshot_path))
            print("lens_screenshot", self.screenshot_path)
        dom_links = list(driver.execute_script(EXTRACT_LINKS_SCRIPT) or [])
        html_links = extract_candidate_links_from_html(driver.page_source, limit=30)
        print("lens_dom_link_count", len(dom_links), "examples", dom_links[:12])
        print("lens_html_link_count", len(html_links), "examples", html_links[:12])
        links = super()._collect(driver, max_results, cancel_event)
        print("lens_final_browser_path", urlparse(driver.current_url).path)
        if self.screenshot_path:
            driver.save_screenshot(str(self.screenshot_path))
            print("lens_final_screenshot", self.screenshot_path)
        tabs = driver.execute_script("""
            return Array.from(document.querySelectorAll('*'))
              .filter(el => ['外观匹配', '完全匹配', 'Visual matches', 'Exact matches']
                .includes((el.textContent || '').trim()) && el.children.length === 0)
              .slice(0, 12).map(el => ({text: el.textContent.trim(),
                html: (el.closest('a, [role=tab]') || el.parentElement).outerHTML.slice(0, 900)}));
        """)
        print("lens_tabs", tabs)
        return links


def configured_proxy() -> str:
    with sqlite3.connect(DB_PATH.as_uri() + "?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value_json FROM settings WHERE key=?", ("proxy_url",)).fetchone()
    return str(json.loads(row[0]) if row else "")


def main() -> None:
    visible = "--visible" in sys.argv[1:]
    use_nasa_image = "--nasa" in sys.argv[1:]
    proxy = configured_proxy()
    session = requests.Session()
    session.trust_env = False
    proxies = {"http": proxy, "https": proxy} if proxy else {}
    post = session.get(POST_URL, proxies=proxies, timeout=20)
    print("x_post_http", post.status_code, "id_in_html", "2023978485316768106" in post.text)
    source = session.get(NASA_IMAGE_URL if use_nasa_image else LOGO_URL, proxies=proxies, timeout=30)
    source.raise_for_status()
    with tempfile.TemporaryDirectory(prefix="similarity_smoke_") as directory:
        image_path = Path(directory) / ("nasa_earth.jpg" if use_nasa_image else "google_logo.png")
        if use_nasa_image:
            with Image.open(BytesIO(source.content)) as image:
                image.thumbnail((1200, 1200))
                image.convert("RGB").save(image_path, format="JPEG", quality=88)
        else:
            image_path.write_bytes(source.content)
        print("query_image_bytes", image_path.stat().st_size)
        try:
            screenshot_path = Path("data/software_app/test_runs/lens_diagnostic.png") if "--screenshot" in sys.argv[1:] else None
            links, page_html = DiagnosticCrawler(screenshot_path).search(
                image_path, max_results=10, headless=not visible, proxy_url=proxy,
                manual_wait_seconds=15 if "--fast" in sys.argv[1:] else (60 if visible else 15),
                on_status=lambda status: print("lens_status", status),
            )
        except RuntimeError as error:
            print("lens_blocked", str(error))
            return
        if "--html" in sys.argv[1:]:
            html_path = Path("data/software_app/test_runs/lens_diagnostic.html")
            html_path.parent.mkdir(parents=True, exist_ok=True)
            html_path.write_text(page_html, encoding="utf-8")
            print("lens_html", html_path)
        print("lens_candidate_count", len(links))
        title = re.search(r"<title[^>]*>(.*?)</title>", page_html, re.IGNORECASE | re.DOTALL)
        print("lens_page_title", re.sub(r"\s+", " ", title.group(1))[:120] if title else "")
        print("lens_page_bytes", len(page_html.encode("utf-8")))
        print("lens_challenge_markers", any(
            marker in page_html.casefold() for marker in ("captcha", "unusual traffic", "请证明您不是机器人")
        ))
        for marker in ("captcha", "unusual traffic", "recaptcha", "sorry"):
            match = re.search(marker, page_html, re.IGNORECASE)
            if match:
                print("lens_marker_context", marker, re.sub(
                    r"\s+", " ", page_html[max(0, match.start() - 45):match.end() + 90]
                )[:160])
        for url in links:
            print("lens_candidate", urlparse(url).hostname, url)


if __name__ == "__main__":
    main()
