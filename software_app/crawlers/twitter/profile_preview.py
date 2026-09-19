from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from selenium.webdriver.common.by import By

try:
    from .driver_init import cookies_web, initialize_driver
    from .following_collector import discover_current_handle, normalize_handle, page_has_login_or_restriction, safe_get, short_error
except ImportError:  # Direct script execution used by the desktop subprocess.
    from driver_init import cookies_web, initialize_driver
    from following_collector import discover_current_handle, normalize_handle, page_has_login_or_restriction, safe_get, short_error


BOOTSTRAP_URL = "https://x.com/"
URL_RE = re.compile(r"https?://[^\s]+|(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
RESERVED_ROUTES = {"home", "explore", "notifications", "messages", "search", "settings", "compose", "i"}


def target_to_handle(value: str) -> str:
    value = str(value or "").strip().strip('"').strip("'")
    if value.startswith("@"):
        return normalize_handle(value)
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value.replace("twitter.com", "x.com"))
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        handle = normalize_handle(parts[0] if parts else "")
        return "" if handle.lower() in RESERVED_ROUTES else handle
    return normalize_handle(value)


def is_home_target(value: str) -> bool:
    parsed = urlparse(str(value or "").strip().replace("twitter.com", "x.com"))
    return parsed.netloc.lower().endswith("x.com") and parsed.path.rstrip("/").lower() in {"", "/home"}


def visible_text(node) -> str:
    try:
        return (node.text or "").strip()
    except Exception:
        return ""


def first_text(driver, selector: str) -> str:
    for node in driver.find_elements(By.CSS_SELECTOR, selector):
        text = visible_text(node)
        if text:
            return text
    return ""



def profile_avatar_node(driver, handle: str):
    selectors = [
        f'a[href="/{handle}/photo"] img',
        f'a[href="/{handle}/photo"] img[src]',
        'div[data-testid^="UserAvatar"] img[src]',
        'img[src*="profile_images"]',
    ]
    for selector in selectors:
        for node in driver.find_elements(By.CSS_SELECTOR, selector):
            src = (node.get_attribute("src") or "").strip()
            if src:
                return node
    return None


def profile_avatar_url(driver, handle: str) -> str:
    node = profile_avatar_node(driver, handle)
    return (node.get_attribute("src") or "").strip() if node is not None else ""


def save_profile_avatar(driver, handle: str, output_file: Path) -> str:
    node = profile_avatar_node(driver, handle)
    if node is None:
        return ""
    avatar_file = output_file.with_name(f"{output_file.stem}-avatar.png")
    try:
        avatar_file.parent.mkdir(parents=True, exist_ok=True)
        if node.screenshot(str(avatar_file)) and avatar_file.is_file() and avatar_file.stat().st_size:
            return str(avatar_file.resolve())
    except Exception:
        pass
    return ""

def external_links(driver) -> list[dict]:
    result: list[dict] = []
    seen: set[str] = set()
    containers = driver.find_elements(
        By.CSS_SELECTOR,
        "div[data-testid='UserProfileHeader_Items'], div[data-testid='UserDescription']",
    )
    links = list(driver.find_elements(By.CSS_SELECTOR, "a[data-testid='UserUrl'][href]"))
    for container in containers:
        links.extend(container.find_elements(By.CSS_SELECTOR, "a[href]"))
    for link in links:
        item = external_link_item(link)
        if not item:
            continue
        key = str(item["url"]).lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    try:
        html_links = extract_profile_links_from_html(str(getattr(driver, "page_source", "") or ""))
    except Exception:
        html_links = []
    for item in html_links:
        key = str(item["url"]).lower()
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def external_link_item(link, *, resolve_shortener: bool = True) -> dict | None:
    href = (link.get_attribute("href") or "").strip()
    text = visible_text(link)
    attributes = [
        (link.get_attribute("data-expanded-url") or "").strip(),
        (link.get_attribute("data-url") or "").strip(),
        (link.get_attribute("title") or "").strip(),
        (link.get_attribute("aria-label") or "").strip(),
        text,
    ]
    return external_link_values(href, attributes, resolve_shortener=resolve_shortener)


def external_link_values(
    href: str,
    attributes: list[str] | tuple[str, ...],
    *,
    resolve_shortener: bool = True,
) -> dict | None:
    href = str(href or "").strip()
    attributes = [str(value or "").strip() for value in attributes]
    candidates: list[dict] = []
    for value in attributes:
        candidates.extend(text_links(value))
    candidates.extend(text_links(href))
    url = next((str(item["url"]) for item in candidates if not _is_x_url(str(item["url"])) and not _is_tco_url(str(item["url"]))), "")
    if not url and href and not _is_x_url(href):
        url = href
    if not url:
        return None
    source_url = href
    if resolve_shortener and _is_tco_url(url):
        url = resolve_tco_url(url)
    if _is_x_url(url) or not url.startswith(("http://", "https://")):
        return None
    label = next((value for value in attributes if value and value != href), "") or url
    item = {"label": label, "url": url}
    if source_url and source_url != url:
        item["source_url"] = source_url
    return item


class _ProfileLinkParser(HTMLParser):
    CONTAINERS = {"UserDescription", "UserProfileHeader_Items"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, resolve_shortener: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.resolve_shortener = resolve_shortener
        self.depth = 0
        self.container_depths: list[int] = []
        self.skeb_depths: list[int] = []
        self.anchor: dict | None = None
        self.links: list[dict] = []
        self.seen: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attributes = {name.lower(): str(value or "") for name, value in attrs}
        self.depth += 1
        if attributes.get("data-testid") in self.CONTAINERS:
            self.container_depths.append(self.depth)
        if "skeb" in attributes.get("class", "").split():
            self.skeb_depths.append(self.depth)
        if tag == "a" and (self.container_depths or self.skeb_depths):
            self.anchor = {"href": attributes.get("href", ""), "attributes": attributes, "text": []}
        if tag == "img" and self.anchor is not None and attributes.get("alt"):
            self.anchor["text"].append(attributes["alt"])
        if tag in self.VOID_TAGS:
            self.depth -= 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self.anchor is not None:
            attributes = self.anchor["attributes"]
            values = [
                attributes.get("data-expanded-url", ""),
                attributes.get("data-url", ""),
                attributes.get("title", ""),
                attributes.get("aria-label", ""),
                "".join(self.anchor["text"]).strip(),
            ]
            item = external_link_values(
                self.anchor["href"], values, resolve_shortener=self.resolve_shortener
            )
            if item:
                key = str(item["url"]).lower()
                if key not in self.seen:
                    self.seen.add(key)
                    self.links.append(item)
            self.anchor = None
        if self.container_depths and self.depth == self.container_depths[-1]:
            self.container_depths.pop()
        if self.skeb_depths and self.depth == self.skeb_depths[-1]:
            self.skeb_depths.pop()
        self.depth = max(0, self.depth - 1)

    def handle_data(self, data: str) -> None:
        if self.anchor is not None:
            self.anchor["text"].append(data)


def extract_profile_links_from_html(html: str, *, resolve_shortener: bool = True) -> list[dict]:
    parser = _ProfileLinkParser(resolve_shortener)
    parser.feed(str(html or ""))
    parser.close()
    return parser.links


def resolve_tco_url(url: str, timeout: int = 6) -> str:
    if not _is_tco_url(url):
        return url
    try:
        request = Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urlopen(request, timeout=timeout) as response:
            expanded = response.geturl()
        return expanded if expanded.startswith(("http://", "https://")) else url
    except Exception:
        return url


def _is_tco_url(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in {"t.co", "www.t.co"}


def _is_x_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "x.com" or host.endswith(".x.com") or host == "twitter.com" or host.endswith(".twitter.com")


def text_links(*values: str) -> list[dict]:
    links: list[dict] = []
    seen: set[str] = set()
    for value in values:
        without_email = EMAIL_RE.sub(" ", value or "")
        for match in URL_RE.findall(without_email):
            url = match.strip().rstrip(".,，。)…」』】》〉")
            if not url:
                continue
            normalized = url if url.startswith(("http://", "https://")) else "https://" + url
            if normalized in seen:
                continue
            seen.add(normalized)
            links.append({"label": url, "url": normalized})
    return links


def merge_links(*groups: list[dict]) -> list[dict]:
    merged: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for item in group:
            url = str(item.get("url") or "").strip()
            label = str(item.get("label") or url).strip()
            parsed = urlparse(url)
            key = (url.rstrip("/") if parsed.path in {"", "/"} else url).lower() or label.lower()
            if not key or key in seen:
                continue
            seen.add(key)
            merged.append({"label": label, "url": url})
    return merged


def is_skeb_link(item: dict) -> bool:
    value = f"{item.get('label', '')} {item.get('url', '')}".lower()
    return "skeb.jp" in value


def wait_for_skeb_button(driver, timeout: float = 8.0) -> bool:
    """Wait for the official extension to finish inserting its profile component."""
    if not getattr(driver, "_skeb_extension_path", None):
        return False
    deadline = time.monotonic() + max(0.0, float(timeout))
    selector = ".skeb a[href*='skeb.jp/@'], a.skeb[href*='skeb.jp/@']"
    while time.monotonic() < deadline:
        try:
            if driver.find_elements(By.CSS_SELECTOR, selector):
                return True
        except Exception:
            return False
        time.sleep(0.25)
    return False


def collect_profile_preview_with_driver(
    driver,
    output_file: Path,
    target: str,
    page_load_timeout: int = 30,
) -> dict:
    handle = target_to_handle(target)
    home_target = is_home_target(target)
    if not handle and not home_target:
        raise ValueError("无法从目标中识别推特用户名")

    if home_target:
        print("打开 X 首页并识别当前登录账号……")
        handle = discover_current_handle(driver, timeout=page_load_timeout)
    profile_url = f"https://x.com/{handle}"
    print(f"打开主页: {profile_url}")
    safe_get(driver, profile_url, timeout=page_load_timeout)
    if page_has_login_or_restriction(driver):
        raise RuntimeError("当前页面仍是登录/限制页面，说明 cookie 未生效或账号受限。")

    deadline = time.monotonic() + page_load_timeout
    while time.monotonic() < deadline and not first_text(driver, "div[data-testid='UserName'] span"):
        if page_has_login_or_restriction(driver):
            raise RuntimeError("当前页面仍是登录/限制页面，说明 cookie 未生效或账号受限。")
        time.sleep(0.5)

    display_name = first_text(driver, "div[data-testid='UserName'] span")
    if not display_name:
        raise RuntimeError("主页已打开，但等待后仍没有抓取到作者资料；请在浏览器中完成验证后重试。")

    skeb_button_loaded = wait_for_skeb_button(driver, timeout=min(8.0, page_load_timeout))

    bio = first_text(driver, "div[data-testid='UserDescription']")
    header_text = first_text(driver, "div[data-testid='UserProfileHeader_Items']")
    avatar_url = profile_avatar_url(driver, handle)
    avatar_path = save_profile_avatar(driver, handle, output_file)
    links = merge_links(external_links(driver), text_links(bio, header_text))
    skeb_links = [item for item in links if is_skeb_link(item)]
    skeb_profiles = [item for item in skeb_links if (urlparse(str(item.get("url") or "")).path or "/") not in {"", "/"}]
    if skeb_profiles:
        skeb_links = skeb_profiles

    payload = {
        "version": 2,
        "handle": handle,
        "display_name": display_name,
        "bio": bio,
        "profile_url": profile_url,
        "avatar_url": avatar_url,
        "avatar_path": avatar_path,
        "links": links,
        "skeb_links": skeb_links,
        "skeb_button_loaded": skeb_button_loaded,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"主页预览已保存: {output_file}")
    if skeb_links:
        print("Skeb 链接:")
        for item in skeb_links:
            print(f"  {item.get('label')}: {item.get('url')}")
    else:
        print("未发现 Skeb 链接")
    return payload


def collect_profile_preview(cookie_file: Path, output_file: Path, target: str, page_load_timeout: int = 30) -> dict:
    driver = initialize_driver()
    try:
        safe_get(driver, BOOTSTRAP_URL, timeout=page_load_timeout)
        cookies_web(driver, str(cookie_file))
        return collect_profile_preview_with_driver(driver, output_file, target, page_load_timeout)
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集 X/Twitter 用户主页信息和外链")
    parser.add_argument("target", help="用户名、@用户名或主页 URL")
    parser.add_argument("--cookie", default="X_cookie.json", help="Cookie 文件路径")
    parser.add_argument("--output", default="profile_preview.json", help="输出 JSON 文件路径")
    parser.add_argument("--page-load-timeout", type=int, default=30, help="页面加载超时秒数")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        collect_profile_preview(
            cookie_file=Path(args.cookie),
            output_file=Path(args.output),
            target=args.target,
            page_load_timeout=max(5, args.page_load_timeout),
        )
        return 0
    except Exception as error:
        print(f"主页预览失败: {short_error(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
