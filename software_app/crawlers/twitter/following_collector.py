from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from selenium.common.exceptions import NoSuchWindowException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By

try:
    from .driver_init import cookies_web, initialize_driver
except ImportError:  # Direct script execution used by the desktop subprocess.
    from driver_init import cookies_web, initialize_driver


BOOTSTRAP_URL = "https://x.com/"
HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
MENTION_RE = re.compile(r"@([A-Za-z0-9_]{1,15})")
EXTERNAL_URL_RE = re.compile(r"https?://[^\s]+|(?:www\.)?[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?:/[^\s]*)?")
IGNORED_PATH_HEADS = {
    "compose",
    "explore",
    "home",
    "i",
    "jobs",
    "messages",
    "notifications",
    "search",
    "settings",
}
FOLLOWING_OUTPUT_VERSION = 2
FOLLOWING_UPDATE_MODES = {"all", "new"}


class FollowingRefreshCancelled(RuntimeError):
    """Raised internally when the desktop UI asks a refresh to stop."""


@dataclass(frozen=True)
class FollowingAccount:
    handle: str
    display_name: str = ""
    bio: str = ""
    profile_url: str = ""
    avatar_url: str = ""
    links: list[dict[str, str]] = field(default_factory=list)
    skeb_links: list[dict[str, str]] = field(default_factory=list)
    collected_at: str = ""


def short_error(error: BaseException) -> str:
    lines = str(error).strip().splitlines()
    return lines[0] if lines else error.__class__.__name__


def safe_get(driver, url: str, timeout: int = 30) -> None:
    driver.set_page_load_timeout(timeout)
    try:
        driver.get(url)
    except TimeoutException:
        print(f"页面加载超时，停止继续加载: {url}")
        try:
            driver.execute_script("window.stop()")
        except WebDriverException:
            pass


def page_has_login_or_restriction(driver) -> bool:
    try:
        current_url = driver.current_url.lower()
        page_text = driver.page_source.lower()
    except TimeoutException:
        return False
    markers = [
        "/i/flow/login",
        "log in to x",
        "sign in to x",
        "登录",
        "登入",
        "临时限制",
        "限制你的登录",
        "temporarily limited",
        "unusual login activity",
    ]
    return any(marker in current_url or marker in page_text for marker in markers)


def normalize_handle(value: str) -> str:
    handle = str(value or "").strip().lstrip("@").strip("/")
    return handle if HANDLE_RE.fullmatch(handle) else ""


def handle_from_href(href: str) -> str:
    if not href:
        return ""
    parsed = urlparse(href)
    path = parsed.path if parsed.scheme else href
    parts = [part for part in path.strip("/").split("/") if part]
    if not parts:
        return ""
    if parts[0].lower() in IGNORED_PATH_HEADS:
        return ""
    if len(parts) > 1 and parts[1].lower() == "status":
        return ""
    return normalize_handle(parts[0])


def discover_current_handle(driver, timeout: int = 25, cancel_event=None) -> str:
    safe_get(driver, "https://x.com/home", timeout=timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise FollowingRefreshCancelled("关注列表刷新已停止")
        if page_has_login_or_restriction(driver):
            raise RuntimeError("当前页面仍是登录/限制页面，无法识别自己的账号。")

        profile_links = driver.find_elements(By.CSS_SELECTOR, "a[data-testid='AppTabBar_Profile_Link']")
        for link in profile_links:
            handle = handle_from_href(link.get_attribute("href") or "")
            if handle:
                return handle

        for link in driver.find_elements(By.CSS_SELECTOR, "a[href]"):
            label = (link.get_attribute("aria-label") or "").lower()
            if not any(token in label for token in ("profile", "个人资料", "個人資料", "プロフィール")):
                continue
            handle = handle_from_href(link.get_attribute("href") or "")
            if handle:
                return handle

        time.sleep(1)

    raise RuntimeError("没有在 X 页面中找到当前账号的个人资料链接，请用 --account 手动指定账号。")


def clean_lines(text: str) -> list[str]:
    ignored = {
        "follow",
        "following",
        "关注",
        "正在关注",
        "已关注",
        "follows you",
        "关注了你",
        "verified account",
    }
    lines: list[str] = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower() in ignored:
            continue
        lines.append(line)
    return lines


def _legacy_external_links(legacy: dict) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    entities = legacy.get("entities") if isinstance(legacy.get("entities"), dict) else {}
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for section_name in ("description", "url"):
        section = entities.get(section_name) if isinstance(entities.get(section_name), dict) else {}
        rows = section.get("urls") if isinstance(section.get("urls"), list) else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("expanded_url") or row.get("unwound_url") or row.get("url") or "").strip()
            display = str(row.get("display_url") or url).strip()
            if not url.startswith(("http://", "https://")) and display:
                url = display if display.startswith(("http://", "https://")) else f"https://{display}"
            host = (urlparse(url).hostname or "").lower()
            if not url.startswith(("http://", "https://")) or host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
                continue
            key = url.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append({"label": display or url, "url": url})
    return result, [item for item in result if "skeb.jp" in f"{item['label']} {item['url']}".lower()]


def _dom_external_links(nodes: list) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in nodes:
        try:
            href = str(node.get_attribute("href") or "").strip()
            text = str(node.text or "").strip()
        except Exception:
            continue
        candidates = EXTERNAL_URL_RE.findall(text)
        url = candidates[0] if candidates else href
        if url and not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        host = (urlparse(url).hostname or "").lower()
        if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
            continue
        if host in {"t.co", "www.t.co"} and candidates:
            displayed = candidates[0]
            url = displayed if displayed.startswith(("http://", "https://")) else f"https://{displayed}"
        if not url.startswith(("http://", "https://")) or url.lower() in seen:
            continue
        seen.add(url.lower())
        result.append({"label": text or url, "url": url})
    return result, [item for item in result if "skeb.jp" in f"{item['label']} {item['url']}".lower()]


def parse_user_cell(cell) -> FollowingAccount | None:
    links = cell.find_elements(By.CSS_SELECTOR, "a[href]")
    handle = ""
    profile_url = ""
    for link in links:
        href = link.get_attribute("href") or ""
        candidate = handle_from_href(href)
        if candidate:
            handle = candidate
            profile_url = f"https://x.com/{candidate}"
            break

    text = cell.text or ""
    if not handle:
        match = MENTION_RE.search(text)
        if match:
            handle = match.group(1)
            profile_url = f"https://x.com/{handle}"
    if not handle:
        return None

    lines = clean_lines(text)
    avatar_url = ""
    for image in cell.find_elements(By.CSS_SELECTOR, "img[src]"):
        candidate_url = (image.get_attribute("src") or "").strip()
        if "profile_images" in candidate_url:
            avatar_url = candidate_url
            break
    display_name = ""
    bio_parts: list[str] = []
    seen_handle = False
    for line in lines:
        if line == f"@{handle}":
            seen_handle = True
            continue
        if not display_name and not line.startswith("@"):
            display_name = line
            continue
        if seen_handle and not line.startswith("@"):
            bio_parts.append(line)

    external, skeb_links = _dom_external_links(links)

    return FollowingAccount(
        handle=handle,
        display_name=display_name,
        bio="\n".join(bio_parts),
        profile_url=profile_url or f"https://x.com/{handle}",
        avatar_url=avatar_url,
        links=external,
        skeb_links=skeb_links,
        collected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def visible_user_cells(driver) -> list:
    cells = []
    seen: set[str] = set()
    selectors = (
        "div[data-testid='UserCell']",
        "main section div[data-testid='cellInnerDiv']",
        "main div[data-testid='cellInnerDiv']",
    )
    for selector in selectors:
        try:
            found = driver.find_elements(By.CSS_SELECTOR, selector)
        except (TimeoutException, WebDriverException):
            continue
        for cell in found:
            element_id = str(getattr(cell, "id", "") or id(cell))
            if element_id in seen:
                continue
            seen.add(element_id)
            cells.append(cell)
    return cells


def extract_visible_accounts(driver) -> list[FollowingAccount]:
    accounts: dict[str, FollowingAccount] = {}
    for cell in visible_user_cells(driver):
        try:
            account = parse_user_cell(cell)
        except Exception:
            continue
        if account:
            accounts[account.handle.lower()] = account
    return list(accounts.values())


def extract_accounts_from_api_payload(payload: object) -> list[FollowingAccount]:
    """Recover user rows from X GraphQL Following responses."""
    accounts: dict[str, FollowingAccount] = {}
    collected_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def visit(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        legacy = value.get("legacy") if isinstance(value.get("legacy"), dict) else {}
        core = value.get("core") if isinstance(value.get("core"), dict) else {}
        handle = normalize_handle(
            str(core.get("screen_name") or legacy.get("screen_name") or value.get("screen_name") or "")
        )
        if handle:
            key = handle.lower()
            existing = accounts.get(key)
            links, skeb_links = _legacy_external_links(legacy)
            candidate = FollowingAccount(
                handle=handle,
                display_name=str(core.get("name") or legacy.get("name") or value.get("name") or "").strip(),
                bio=str(legacy.get("description") or value.get("description") or "").strip(),
                profile_url=f"https://x.com/{handle}",
                avatar_url=str(
                    legacy.get("profile_image_url_https")
                    or legacy.get("profile_image_url")
                    or value.get("profile_image_url_https")
                    or ""
                ).strip(),
                links=links,
                skeb_links=skeb_links,
                collected_at=collected_at,
            )
            accounts[key] = FollowingAccount(
                handle=existing.handle if existing else candidate.handle,
                display_name=candidate.display_name or (existing.display_name if existing else ""),
                bio=candidate.bio or (existing.bio if existing else ""),
                profile_url=candidate.profile_url or (existing.profile_url if existing else ""),
                avatar_url=candidate.avatar_url or (existing.avatar_url if existing else ""),
                links=candidate.links or (existing.links if existing else []),
                skeb_links=candidate.skeb_links or (existing.skeb_links if existing else []),
                collected_at=candidate.collected_at,
            )
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(payload)
    return list(accounts.values())


def extract_network_accounts(driver) -> list[FollowingAccount]:
    """Read newly arrived Following GraphQL responses from Chrome performance logs."""
    accounts: dict[str, FollowingAccount] = {}
    try:
        entries = driver.get_log("performance")
    except (TimeoutException, WebDriverException, AttributeError):
        return []
    for entry in entries:
        try:
            message = json.loads(entry.get("message", "{}"))["message"]
            if message.get("method") != "Network.responseReceived":
                continue
            params = message.get("params", {})
            response = params.get("response", {})
            url = str(response.get("url") or "")
            if "following" not in url.lower():
                continue
            body_payload = driver.execute_cdp_cmd(
                "Network.getResponseBody", {"requestId": params["requestId"]}
            )
            body = str(body_payload.get("body") or "")
            if body_payload.get("base64Encoded"):
                body = base64.b64decode(body).decode("utf-8", errors="replace")
            payload = json.loads(body)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, WebDriverException):
            continue
        for account in extract_accounts_from_api_payload(payload):
            accounts[account.handle.lower()] = account
    return list(accounts.values())


def advance_lazy_list(driver) -> dict:
    """Move through X's virtualized list and trigger its next lazy-loaded batch."""
    cells = visible_user_cells(driver)
    if cells:
        try:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'end', inline: 'nearest'});", cells[-1]
            )
        except (TimeoutException, WebDriverException):
            pass
    try:
        result = driver.execute_script(
            """
            const root = document.scrollingElement || document.documentElement;
            const amount = Math.max(640, Math.floor(window.innerHeight * 0.85));
            root.scrollTop = Math.min(root.scrollHeight, root.scrollTop + amount);
            window.dispatchEvent(new Event('scroll'));
            return {
                cells: arguments[0],
                scroll_y: root.scrollTop,
                height: root.scrollHeight,
                viewport: window.innerHeight
            };
            """,
            len(cells),
        )
    except (TimeoutException, WebDriverException):
        result = None
    return result if isinstance(result, dict) else {
        "cells": len(cells), "scroll_y": 0, "height": 0, "viewport": 0
    }


def page_has_explicit_empty_state(driver) -> bool:
    """Return true only when X explicitly says the following list is empty."""

    try:
        text = str(getattr(driver, "page_source", "") or "").lower()
    except TimeoutException:
        return False
    markers = (
        "you aren&#x27;t following anyone yet",
        "you aren't following anyone yet",
        "not following anyone yet",
        "还没有关注任何人",
        "尚未关注任何人",
        "まだ誰もフォローしていません",
    )
    return any(marker in text for marker in markers)


def report_status(on_status, message: str) -> None:
    print(message)
    if on_status:
        try:
            on_status(message)
        except Exception:
            pass


def merge_accounts(
    destination: dict[str, FollowingAccount],
    discovered: list[FollowingAccount],
    max_accounts: int = 0,
) -> int:
    added = 0
    for item in discovered:
        key = item.handle.lower()
        if key not in destination and max_accounts > 0 and len(destination) >= max_accounts:
            continue
        if key not in destination:
            added += 1
            destination[key] = item
        else:
            destination[key] = _account_with_fallback(item, destination[key])
    return added


def _account_with_fallback(current: FollowingAccount, fallback: FollowingAccount) -> FollowingAccount:
    return FollowingAccount(
        handle=current.handle or fallback.handle,
        display_name=current.display_name or fallback.display_name,
        bio=current.bio or fallback.bio,
        profile_url=current.profile_url or fallback.profile_url,
        avatar_url=current.avatar_url or fallback.avatar_url,
        links=current.links or fallback.links,
        skeb_links=current.skeb_links or fallback.skeb_links,
        collected_at=current.collected_at or fallback.collected_at,
    )


def load_cached_accounts(output_file: Path) -> dict[str, FollowingAccount]:
    if not output_file.is_file():
        return {}
    try:
        payload = json.loads(output_file.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    rows = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    accounts: dict[str, FollowingAccount] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        handle = normalize_handle(str(row.get("handle") or row.get("username") or ""))
        if not handle:
            continue
        accounts[handle.lower()] = FollowingAccount(
            handle=handle,
            display_name=str(row.get("display_name") or row.get("name") or "").strip(),
            bio=str(row.get("bio") or row.get("description") or "").strip(),
            profile_url=str(row.get("profile_url") or f"https://x.com/{handle}").strip(),
            avatar_url=str(row.get("avatar_url") or row.get("profile_image_url") or "").strip(),
            links=list(row.get("links") or []) if isinstance(row.get("links"), list) else [],
            skeb_links=list(row.get("skeb_links") or []) if isinstance(row.get("skeb_links"), list) else [],
            collected_at=str(row.get("collected_at") or "").strip(),
        )
    return accounts


def build_updated_accounts(
    scanned: dict[str, FollowingAccount],
    previous: dict[str, FollowingAccount],
    update_mode: str,
) -> dict[str, FollowingAccount]:
    """Build a full replacement or prepend-only incremental result in page order."""
    if update_mode == "all":
        return dict(scanned)
    if update_mode != "new":
        raise ValueError(f"不支持的关注列表更新方式: {update_mode}")

    result: dict[str, FollowingAccount] = {}
    for key, item in scanned.items():
        if key not in previous:
            result[key] = item
    for key, previous_item in previous.items():
        current = scanned.get(key)
        result[key] = _account_with_fallback(current, previous_item) if current else previous_item
    return result


def finish_following_payload(
    output_file: Path,
    account: str,
    source_url: str,
    accounts: dict[str, FollowingAccount],
    *,
    max_accounts: int,
    stopped_reason: str = "",
    metadata: dict | None = None,
) -> dict:
    """Persist useful results without replacing a good cache with an uncertain empty result."""

    uncertain_empty = not accounts and stopped_reason in {
        "browser_closed",
        "cancelled",
        "no_content",
        "time_limit",
    }
    if uncertain_empty and output_file.is_file():
        try:
            previous = json.loads(output_file.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            previous = None
        if isinstance(previous, dict) and isinstance(previous.get("items"), list):
            payload = dict(previous)
            payload["count"] = len(previous["items"])
            payload["partial"] = True
            payload["stopped_reason"] = stopped_reason
            payload["kept_previous"] = True
            payload["refresh_account"] = account
            payload["refresh_source_url"] = source_url
            if metadata:
                payload.update(metadata)
            return payload

    payload = write_following_payload(
        output_file,
        account,
        source_url,
        accounts,
        max_accounts=max_accounts,
        partial=bool(stopped_reason),
        metadata=metadata,
    )
    if stopped_reason:
        payload["stopped_reason"] = stopped_reason
        output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload


def write_following_payload(
    output_file: Path,
    account: str,
    source_url: str,
    accounts: dict[str, FollowingAccount],
    max_accounts: int = 0,
    partial: bool = False,
    metadata: dict | None = None,
) -> dict:
    items = list(accounts.values())
    if max_accounts > 0:
        items = items[:max_accounts]
    payload = {
        "version": FOLLOWING_OUTPUT_VERSION,
        "account": account,
        "source_url": source_url,
        "count": len(items),
        "partial": partial,
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "items": [asdict(item) for item in items],
    }
    if metadata:
        payload.update(metadata)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload

def collect_following(
    cookie_file: Path,
    output_file: Path,
    account: str = "",
    max_accounts: int = 0,
    idle_rounds: int = 15,
    scroll_pause: float = 1.2,
    page_load_timeout: int = 30,
    max_duration_seconds: int = 0,
    update_mode: str = "all",
    known_overlap: int = 30,
    cancel_event=None,
    on_status=None,
    on_accounts=None,
) -> dict:
    requested_update_mode = str(update_mode or "all").strip().lower()
    if requested_update_mode not in FOLLOWING_UPDATE_MODES:
        raise ValueError(f"不支持的关注列表更新方式: {requested_update_mode}")
    previous_accounts = load_cached_accounts(output_file)
    effective_update_mode = "all" if requested_update_mode == "new" and not previous_accounts else requested_update_mode
    known_handles = set(previous_accounts)
    overlap_target = min(max(1, int(known_overlap or 30)), len(known_handles)) if known_handles else 0
    observed_handles: set[str] = set()
    new_handles: set[str] = set()
    known_streak = 0
    overlap_reached = False

    def observe(discovered: list[FollowingAccount]) -> None:
        nonlocal known_streak, overlap_reached
        if effective_update_mode != "new":
            return
        for item in discovered:
            key = item.handle.lower()
            if key in observed_handles:
                continue
            observed_handles.add(key)
            if key in known_handles:
                known_streak += 1
            else:
                new_handles.add(key)
                known_streak = 0
            if overlap_target and known_streak >= overlap_target:
                overlap_reached = True

    def current_result() -> dict[str, FollowingAccount]:
        return build_updated_accounts(accounts, previous_accounts, effective_update_mode)

    def result_metadata(completion_reason: str = "") -> dict:
        result = current_result()
        metadata = {
            "update_mode": effective_update_mode,
            "requested_update_mode": requested_update_mode,
            "previous_count": len(previous_accounts),
            "new_count": len(new_handles) if effective_update_mode == "new" else len(set(accounts) - known_handles),
            "completion_reason": completion_reason,
        }
        if effective_update_mode == "all" and completion_reason == "list_end":
            metadata["removed_count"] = len(known_handles - set(result))
        return metadata

    driver = initialize_driver()
    accounts: dict[str, FollowingAccount] = {}
    started_at = time.monotonic()
    stopped_reason = ""
    account = normalize_handle(account)
    following_url = ""
    try:
        safe_get(driver, BOOTSTRAP_URL, timeout=page_load_timeout)
        cookies_web(driver, str(cookie_file))

        account = account or discover_current_handle(driver, timeout=page_load_timeout, cancel_event=cancel_event)
        following_url = f"https://x.com/{account}/following"
        print(f"打开关注列表: {following_url}")
        safe_get(driver, following_url, timeout=page_load_timeout)

        duration_limit = max(0, int(max_duration_seconds or 0))
        total_deadline = started_at + duration_limit if duration_limit else None
        initial_deadline = time.monotonic() + 35
        if total_deadline is not None:
            initial_deadline = min(total_deadline, initial_deadline)
        last_wait_report = -1
        while time.monotonic() < initial_deadline:
            if cancel_event is not None and cancel_event.is_set():
                stopped_reason = "cancelled"
                break
            try:
                visible = [*extract_visible_accounts(driver), *extract_network_accounts(driver)]
            except TimeoutException:
                visible = []
                report_status(on_status, "X 页面响应较慢，正在继续等待")
            observe(visible)
            merge_accounts(accounts, visible, max_accounts)
            if accounts:
                break
            if page_has_explicit_empty_state(driver):
                stopped_reason = "empty"
                break
            elapsed = int(time.monotonic() - started_at)
            report = elapsed // 5
            should_report = report != last_wait_report
            if should_report:
                message = f"正在等待 X 加载关注账号，已等待约 {elapsed} 秒"
                report_status(on_status, message)
                last_wait_report = report
            scroll_state = advance_lazy_list(driver)
            if not int(scroll_state.get("cells") or 0) and should_report:
                report_status(on_status, "关注列表仍在懒加载，正在滚动页面触发首批账号")
            if cancel_event is not None:
                cancel_event.wait(1)
            else:
                time.sleep(1)

        if not accounts and not stopped_reason:
            stopped_reason = "no_content"

        idle_count = 0
        last_count = 0
        last_saved_count = -1
        furthest_scroll_y = -1
        greatest_height = -1
        completion_reason = ""
        while not stopped_reason:
            if cancel_event is not None and cancel_event.is_set():
                stopped_reason = "cancelled"
                break
            if total_deadline is not None and time.monotonic() >= total_deadline:
                stopped_reason = "time_limit"
                break
            if page_has_login_or_restriction(driver):
                raise RuntimeError("当前页面仍是登录/限制页面，说明 cookie 未生效或账号受限。")

            try:
                discovered = [*extract_visible_accounts(driver), *extract_network_accounts(driver)]
                observe(discovered)
                merge_accounts(accounts, discovered, max_accounts)
            except TimeoutException:
                report_status(on_status, "X 页面响应较慢，已保存现有结果并继续尝试")

            result_accounts = current_result()
            count = len(result_accounts)
            scanned_count = len(accounts)
            if count > 0 and count != last_saved_count:
                partial_payload = write_following_payload(
                    output_file,
                    account,
                    following_url,
                    result_accounts,
                    partial=True,
                    metadata=result_metadata(),
                )
                if on_accounts:
                    try:
                        on_accounts(partial_payload)
                    except Exception:
                        pass
                last_saved_count = count
            if overlap_reached:
                completion_reason = "known_overlap"
                report_status(
                    on_status,
                    f"已连续匹配 {known_streak} 个旧账号，新增 {len(new_handles)} 个；增量更新完成",
                )
                break
            if max_accounts > 0 and scanned_count >= max_accounts:
                completion_reason = "max_accounts"
                break

            scroll_state = advance_lazy_list(driver)
            scroll_y = int(scroll_state.get("scroll_y") or 0)
            height = int(scroll_state.get("height") or 0)
            count_progressed = scanned_count > last_count
            scroll_progressed = scroll_y > furthest_scroll_y + 2 or height > greatest_height + 2
            last_count = max(last_count, scanned_count)
            furthest_scroll_y = max(furthest_scroll_y, scroll_y)
            greatest_height = max(greatest_height, height)
            if count_progressed or scroll_progressed:
                idle_count = 0
            else:
                idle_count += 1
            report_status(
                on_status,
                (
                    f"已发现新增 {len(new_handles)} 个、连续匹配旧账号 {known_streak}/{overlap_target}；"
                    if effective_update_mode == "new"
                    else f"已获取 {count} 个关注账号；"
                )
                + "正在触发下一批懒加载"
                f"（当前节点 {int(scroll_state.get('cells') or 0)}，滚动 {scroll_y}/{height}，"
                f"底部稳定 {idle_count}/{idle_rounds} 轮）",
            )
            if idle_count >= idle_rounds:
                completion_reason = "list_end"
                break
            if cancel_event is not None:
                cancel_event.wait(scroll_pause)
            else:
                time.sleep(scroll_pause)

        final_accounts = current_result()
        if stopped_reason and effective_update_mode == "new" and not new_handles:
            final_accounts = {}
        payload = finish_following_payload(
            output_file,
            account,
            following_url,
            final_accounts,
            max_accounts=0 if effective_update_mode == "new" else max_accounts,
            stopped_reason=stopped_reason,
            metadata=result_metadata(completion_reason),
        )
        print(f"关注列表已保存: {output_file}")
        return payload
    except FollowingRefreshCancelled:
        return finish_following_payload(
            output_file,
            account,
            following_url,
            current_result() if new_handles or effective_update_mode == "all" else {},
            max_accounts=0 if effective_update_mode == "new" else max_accounts,
            stopped_reason="cancelled",
            metadata=result_metadata(),
        )
    except (NoSuchWindowException, WebDriverException) as error:
        message = str(error).lower()
        closed = isinstance(error, NoSuchWindowException) or any(
            marker in message for marker in ("no such window", "invalid session", "disconnected", "not connected")
        )
        if not closed:
            raise
        payload = finish_following_payload(
            output_file,
            account,
            following_url,
            current_result() if new_handles or effective_update_mode == "all" else {},
            max_accounts=0 if effective_update_mode == "new" else max_accounts,
            stopped_reason="browser_closed",
            metadata=result_metadata(),
        )
        return payload
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="采集当前 X/Twitter 账号关注的作者列表")
    parser.add_argument("--cookie", default="X_cookie.json", help="Cookie 文件路径")
    parser.add_argument("--output", default="following_accounts.json", help="输出 JSON 文件路径")
    parser.add_argument("--account", default="", help="自己的账号 handle；不填则自动从登录态识别")
    parser.add_argument("--max", type=int, default=0, help="最多采集多少个，0 表示不限")
    parser.add_argument("--idle-rounds", type=int, default=15, help="账号和滚动位置连续多少轮都无推进后停止")
    parser.add_argument("--scroll-pause", type=float, default=1.2, help="每次滚动后的等待秒数")
    parser.add_argument("--page-load-timeout", type=int, default=30, help="页面加载超时秒数")
    parser.add_argument("--max-duration", type=int, default=0, help="异常兜底时限秒数；0 表示不限时并等待列表收敛")
    parser.add_argument("--mode", choices=("new", "all"), default="all", help="更新新的或更新全部")
    parser.add_argument("--known-overlap", type=int, default=30, help="增量更新连续匹配多少个旧账号后停止")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        collect_following(
            cookie_file=Path(args.cookie),
            output_file=Path(args.output),
            account=args.account,
            max_accounts=max(0, args.max),
            idle_rounds=max(1, args.idle_rounds),
            scroll_pause=max(0.2, args.scroll_pause),
            page_load_timeout=max(5, args.page_load_timeout),
            max_duration_seconds=max(0, args.max_duration),
            update_mode=args.mode,
            known_overlap=max(1, args.known_overlap),
        )
        return 0
    except Exception as error:
        print(f"关注列表采集失败: {short_error(error)}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
