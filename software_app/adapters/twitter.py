from __future__ import annotations

import csv
import json
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.adapter import CrawlerAdapter, TaskCancelled
from software_app.core.browser_driver import chromedriver_status
from software_app.core.events import CallbackSet
from software_app.core.exports import safe_csv_cell
from software_app.core.models import DownloadTask, ModuleInfo, ProgressEvent, TargetPreview
from software_app.core.settings import TWITTER_DATA_DIR, TWITTER_ROOT
from software_app.crawlers.common import safe_component
from software_app.crawlers.twitter import TwitterCrawlerService
from software_app.crawlers.twitter.download_method import configure_downloads, request_with_retries
from software_app.crawlers.twitter.following_collector import collect_following
from software_app.crawlers.twitter.twitter_Crawler_2 import DEFAULT_CONFIG, config_bool, load_config


DOWNLOAD_TYPE_LABELS = {
    "1": "图片",
    "2": "视频",
    "3": "GIF",
    "4": "音频",
}
TWITTER_RESERVED_ROUTES = {"home", "explore", "notifications", "messages", "search", "settings", "compose", "i"}



def _read_json_file(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def _write_empty_record(path: Path) -> int:
    payload = _read_json_file(path, {})
    items = payload.get("items", payload) if isinstance(payload, dict) else {}
    count = len(items) if isinstance(items, dict) else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "items": {}}, ensure_ascii=False, indent=2), encoding="utf-8")
    return count


def _latest_time(left: str, right: str) -> str:
    return max(str(left or ""), str(right or ""))


def _safe_profile_name(handle: str) -> str:
    return safe_component(handle, "profile", max_length=60)


def _profile_payload_from_target(target: str) -> dict:
    handle = target_to_handle(target)
    return {
        "handle": handle,
        "display_name": "",
        "bio": "",
        "profile_url": f"https://x.com/{handle}" if handle else str(target or "").strip(),
        "media_url": normalize_target(target) if target else "",
        "links": [],
        "skeb_links": [],
        "avatar_url": "",
        "avatar_path": "",
        "source": "input",
    }


def _merge_profile_payload(base: dict, source: dict, source_name: str) -> dict:
    for key in ("handle", "display_name", "bio", "profile_url", "media_url", "avatar_url", "avatar_path"):
        value = source.get(key)
        if value:
            base[key] = value
    for key in ("links", "skeb_links"):
        value = source.get(key)
        if isinstance(value, list) and value:
            base[key] = value
    if source_name != "input":
        base["source"] = source_name
    return base

def normalize_target(raw_target: str) -> str:
    value = raw_target.strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("@"):
        value = value[1:]
    if not value.startswith(("http://", "https://")):
        return f"https://x.com/{value}/media"

    parsed = urlparse(value)
    if parsed.netloc.lower().endswith("twitter.com"):
        value = value.replace(parsed.netloc, "x.com", 1)

    path = urlparse(value).path.rstrip("/")
    path_head = path.strip("/").split("/", 1)[0].lower() if path.strip("/") else ""
    if path_head in TWITTER_RESERVED_ROUTES:
        return value.rstrip("/")
    if "/status/" not in path and not path.endswith("/media"):
        value = value.rstrip("/") + "/media"
    return value


def target_to_handle(raw_target: str) -> str:
    value = raw_target.strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("@"):
        return value[1:].strip()
    if value.startswith(("http://", "https://")):
        parsed = urlparse(value.replace("twitter.com", "x.com"))
        parts = [part for part in parsed.path.strip("/").split("/") if part]
        handle = parts[0].strip() if parts else ""
        return "" if handle.lower() in TWITTER_RESERVED_ROUTES else handle
    return value.strip()

def normalize_download_types(raw_value: object) -> str:
    value = str(raw_value or "1234").strip()
    if not value:
        return "1234"
    if value == "5":
        return "1234"

    value = value.replace("，", ",").replace(",", "")
    value = "".join(sorted(set(value)))
    if any(char not in DOWNLOAD_TYPE_LABELS for char in value):
        raise ValueError("下载类型只能包含 1、2、3、4，或使用 5 表示全部")
    return value or "1234"


def format_download_types(value: str) -> str:
    return ", ".join(DOWNLOAD_TYPE_LABELS[item] for item in value)


class TwitterNativeAdapter(CrawlerAdapter):
    max_concurrency = 5

    def __init__(self, twitter_root: Path = TWITTER_ROOT) -> None:
        self.twitter_root = Path(twitter_root)
        self.runtime_data_dir = Path(TWITTER_DATA_DIR)
        self.info = ModuleInfo(
            module_id="twitter",
            display_name="推特/X",
            description="软件内置的 X/Twitter 媒体发现与下载实现。",
            stage="ready",
            capabilities=("preview", "search", "download", "native", "profile-links"),
            script_root=self.twitter_root,
        )
        self._crawler = TwitterCrawlerService(self.runtime_data_dir)

    def validate(self) -> list[str]:
        warnings: list[str] = []
        config = self._default_runtime_file("config.json")
        cookie = self._default_runtime_file("X_cookie.json")
        driver_status = chromedriver_status(auto_download=False)
        if not self.twitter_root.exists():
            warnings.append(f"推特脚本目录不存在: {self.twitter_root}")
        if not config.exists():
            warnings.append(f"缺少配置文件: {config}")
        if not cookie.exists():
            warnings.append(f"缺少 Cookie 文件: {cookie}")
        if driver_status.source in {"missing", "incompatible"}:
            warnings.append("ChromeDriver 缺失或版本不匹配，启动浏览器时会按本机 Chrome 自动下载")
        try:
            import selenium  # noqa: F401
        except Exception:
            warnings.append("当前 Python 环境未安装 selenium")
        return warnings

    def can_handle(self, raw_target: str) -> bool:
        value = raw_target.strip().lower()
        return bool(
            value.startswith("@")
            or "twitter.com/" in value
            or "x.com/" in value
            or (value and "/" not in value and "\\" not in value)
        )

    def _default_runtime_file(self, name: str) -> Path:
        data_file = self.runtime_data_dir / name
        if data_file.exists():
            return data_file
        return self.twitter_root / name

    def _ensure_runtime_files(self) -> None:
        self.runtime_data_dir.mkdir(parents=True, exist_ok=True)
        for name in ("config.json", "X_cookie.json", "downloaded_urls.json", "failed_urls.json"):
            target = self.runtime_data_dir / name
            source = self.twitter_root / name
            if not target.exists() and source.exists():
                shutil.copy2(source, target)

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        options = options or {}
        normalized = normalize_target(raw_target)
        download_types = normalize_download_types(options.get("types", "1234"))
        return TargetPreview(
            module_id=self.module_id,
            raw_target=raw_target,
            normalized_target=normalized,
            title=normalized or "空目标",
            description=f"下载类型: {format_download_types(download_types)}",
            status="ok" if normalized else "error",
            warnings=self.validate(),
            metadata={"download_types": download_types},
        )

    def download(
        self,
        task: DownloadTask,
        callbacks: CallbackSet,
        cancel_event: threading.Event,
    ) -> None:
        normalized = normalize_target(task.target)
        if not normalized:
            raise ValueError("目标不能为空")

        self._ensure_runtime_files()
        warnings = self.validate()
        for warning in warnings:
            callbacks.on_progress(ProgressEvent(task.task_id, task.module_id, "warning", warning))

        task.output_dir.mkdir(parents=True, exist_ok=True)
        task.options["types"] = normalize_download_types(task.options.get("types", "1234"))
        self._crawler.download(task, callbacks, cancel_event)
        self._mark_downloaded_user(task.target, normalized, task.output_dir)

    def cancel(self, task_id: str) -> None:
        self._crawler.cancel(task_id)

    def downloaded_users_file(self) -> Path:
        return self.runtime_data_dir / "downloaded_users.json"

    def _download_record_file(self) -> Path:
        return self.runtime_data_dir / "downloaded_urls.json"

    def _failed_record_file(self) -> Path:
        return self.runtime_data_dir / "failed_urls.json"

    def _hidden_history_file(self) -> Path:
        return self.runtime_data_dir / "hidden_history.json"

    def _hidden_history_keys(self) -> set[str]:
        payload = _read_json_file(self._hidden_history_file(), {"items": []})
        items = payload.get("items", []) if isinstance(payload, dict) else []
        return {str(item).strip().casefold() for item in items if str(item).strip()} if isinstance(items, list) else set()

    def _write_hidden_history_keys(self, keys: set[str]) -> None:
        path = self._hidden_history_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"version": 1, "items": sorted(keys)}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _mark_downloaded_user(self, raw_target: str, normalized_target: str, output_dir: Path) -> None:
        handle = target_to_handle(raw_target) or target_to_handle(normalized_target)
        if not handle:
            return
        history_file = self.downloaded_users_file()
        payload = _read_json_file(history_file, {"version": 1, "items": {}})
        if not isinstance(payload, dict):
            payload = {"version": 1, "items": {}}
        items = payload.get("items")
        if not isinstance(items, dict):
            items = {}
        key = handle.lower()
        item = items.get(key, {}) if isinstance(items.get(key), dict) else {}
        item.update(
            {
                "handle": handle,
                "target": raw_target,
                "profile_url": f"https://x.com/{handle}",
                "media_url": normalize_target(raw_target),
                "output_dir": str(output_dir),
                "last_downloaded_at": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
        item["downloads"] = int(item.get("downloads", 0) or 0) + 1
        items[key] = item
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history_file.write_text(json.dumps({"version": 1, "items": items}, ensure_ascii=False, indent=2), encoding="utf-8")
        hidden = self._hidden_history_keys()
        unhide = {key.casefold()}
        for account in self.load_following_accounts():
            if str(account.get("handle") or "").strip().lstrip("@").casefold() == key.casefold():
                unhide.add(str(account.get("display_name") or "").strip().casefold())
                break
        cached_profile = _read_json_file(self.profile_preview_file(handle), {})
        if isinstance(cached_profile, dict):
            unhide.add(str(cached_profile.get("display_name") or "").strip().casefold())
        remaining = hidden - {value for value in unhide if value}
        if remaining != hidden:
            self._write_hidden_history_keys(remaining)

    def downloaded_users(self, output_dir: Path | str | None = None) -> list[dict]:
        self._ensure_runtime_files()
        merged: dict[str, dict] = {}
        local_owner_by_filename: dict[str, str] = {}
        following_profiles = {
            str(item.get("handle") or "").strip().lstrip("@").lower(): item
            for item in self.load_following_accounts()
            if str(item.get("handle") or "").strip()
        }
        cached_profiles: dict[str, dict] = {}
        profile_dir = self.runtime_data_dir / "profile_previews"
        if profile_dir.is_dir():
            for profile_file in profile_dir.glob("*.json"):
                payload = _read_json_file(profile_file, {})
                if not isinstance(payload, dict):
                    continue
                handle = str(payload.get("handle") or "").strip().lstrip("@").lower()
                if handle:
                    cached_profiles[handle] = payload

        display_name_handles: dict[str, set[str]] = {}
        for handle, profile in {**following_profiles, **cached_profiles}.items():
            display_name = str(profile.get("display_name") or "").strip().casefold()
            if display_name:
                display_name_handles.setdefault(display_name, set()).add(handle)
        unique_display_names = {
            display_name: next(iter(handles))
            for display_name, handles in display_name_handles.items()
            if len(handles) == 1
        }

        def history_key(name: str) -> str:
            normalized = str(name or "").strip().lstrip("@").lower()
            if normalized in following_profiles or normalized in cached_profiles:
                return normalized
            return unique_display_names.get(str(name or "").strip().casefold(), normalized)

        output_root = Path(output_dir).expanduser() if output_dir else None
        if output_root:
            try:
                root_exists = output_root.exists()
            except OSError:
                root_exists = False
            if root_exists:
                try:
                    children = list(output_root.iterdir())
                except OSError:
                    children = []
                for child in children:
                    try:
                        if not child.is_dir():
                            continue
                        for local_file in child.rglob("*"):
                            try:
                                if local_file.is_file():
                                    local_owner_by_filename.setdefault(local_file.name.lower(), child.name)
                            except OSError:
                                continue
                    except OSError:
                        continue

        history = _read_json_file(self.downloaded_users_file(), {"items": {}})
        history_items = history.get("items", {}) if isinstance(history, dict) else {}
        if isinstance(history_items, dict):
            for key, item in history_items.items():
                if not isinstance(item, dict):
                    continue
                handle = str(item.get("handle") or key).strip().lstrip("@")
                if not handle:
                    continue
                row = merged.setdefault(handle.lower(), {"name": handle, "handle": handle, "count": 0, "types": set(), "sources": set()})
                row["handle"] = handle
                row["target"] = item.get("target", f"@{handle}")
                row["profile_url"] = item.get("profile_url", f"https://x.com/{handle}")
                row["output_dir"] = item.get("output_dir", "")
                row["count"] += int(item.get("downloads", 0) or 0)
                row["latest"] = _latest_time(row.get("latest", ""), item.get("last_downloaded_at", ""))
                row["sources"].add("任务记录")

        record = _read_json_file(self._download_record_file(), {"items": {}})
        record_items = record.get("items", record) if isinstance(record, dict) else {}
        if isinstance(record_items, dict):
            for item in record_items.values():
                if not isinstance(item, dict):
                    continue
                file_value = str(item.get("file") or "").strip()
                if not file_value:
                    continue
                file_path = Path(file_value)
                name = local_owner_by_filename.get(file_path.name.lower(), "")
                if not name:
                    parts = [part for part in file_value.replace("\\", "/").split("/") if part and part not in {".", "/"}]
                    name = parts[-2] if len(parts) >= 2 else ""
                if not name:
                    continue
                key = history_key(name)
                matched_handle = key if key in following_profiles or key in cached_profiles else ""
                row = merged.setdefault(key, {"name": name, "handle": matched_handle, "count": 0, "types": set(), "sources": set()})
                row["name"] = row.get("name") or name
                row["count"] += 1
                row["types"].add(str(item.get("type") or ""))
                row["latest"] = _latest_time(row.get("latest", ""), item.get("saved_at", ""))
                row["sources"].add("下载记录")

        if output_root:
            root = output_root
            if root.exists():
                for child in root.iterdir():
                    if not child.is_dir():
                        continue
                    media_files = [path for path in child.rglob("*") if path.is_file()]
                    if not media_files:
                        continue
                    key = history_key(child.name)
                    matched_handle = key if key in following_profiles or key in cached_profiles else ""
                    row = merged.setdefault(key, {"name": child.name, "handle": matched_handle, "count": 0, "types": set(), "sources": set()})
                    row["path"] = str(child)
                    row["local_files"] = len(media_files)
                    row["latest"] = _latest_time(row.get("latest", ""), max(str(path.stat().st_mtime) for path in media_files))
                    row["sources"].add("本地文件夹")

        for key, row in merged.items():
            handle = str(row.get("handle") or key).strip().lstrip("@").lower()
            for source_name, profile in (("关注资料", following_profiles.get(handle)), ("主页资料", cached_profiles.get(handle))):
                if not isinstance(profile, dict):
                    continue
                row["name"] = str(profile.get("display_name") or row.get("name") or handle)
                for field in ("bio", "profile_url", "avatar_url", "avatar_path", "links", "skeb_links"):
                    if profile.get(field):
                        row[field] = profile[field]
                row["sources"].add(source_name)

        result: list[dict] = []
        for row in merged.values():
            types = sorted(item for item in row.pop("types", set()) if item)
            sources = sorted(row.pop("sources", set()))
            row["types"] = ",".join(types)
            row["source"] = ",".join(sources)
            result.append(row)
        hidden = self._hidden_history_keys()
        result = [
            item
            for item in result
            if str(item.get("handle") or "").strip().lstrip("@").casefold() not in hidden
            and str(item.get("name") or "").strip().casefold() not in hidden
        ]
        result.sort(key=lambda item: (str(item.get("latest") or ""), str(item.get("name") or "")), reverse=True)
        return result

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        """Search the local following/history cache without opening X."""
        options = options or {}
        needle = str(query or "").strip().lstrip("@").lower()
        if not needle:
            return []
        rows: dict[str, dict] = {}
        for item in self.load_following_accounts():
            handle = str(item.get("handle") or "").strip().lstrip("@")
            name = str(item.get("display_name") or "").strip()
            if needle not in handle.lower() and needle not in name.lower():
                continue
            rows[handle.lower()] = {
                "id": handle,
                "title": name or handle,
                "handle": handle,
                "url": f"https://x.com/{handle}",
                "source": "following_list",
            }
        for item in self.downloaded_users(options.get("output_dir")):
            handle = str(item.get("handle") or "").strip().lstrip("@")
            name = str(item.get("name") or handle).strip()
            if not handle or (needle not in handle.lower() and needle not in name.lower()):
                continue
            rows.setdefault(
                handle.lower(),
                {"id": handle, "title": name, "handle": handle, "url": f"https://x.com/{handle}", "source": "download_history"},
            )
        # An exact @handle is itself a valid X target even when it has not been
        # downloaded or added to the local following cache yet.  Keep it in the
        # result list so the UI can immediately run an online profile preview.
        exact_handle = str(query or "").strip().lstrip("@").strip()
        if (
            exact_handle
            and len(exact_handle) <= 50
            and all(char.isalnum() or char == "_" for char in exact_handle)
            and exact_handle.lower() not in rows
        ):
            rows[exact_handle.lower()] = {
                "id": exact_handle,
                "title": f"@{exact_handle}",
                "handle": exact_handle,
                "url": f"https://x.com/{exact_handle}",
                "source": "direct_handle",
            }
        return list(rows.values())[: max(1, min(int(limit), 20))]
    def clear_download_records(self) -> dict[str, int]:
        self._ensure_runtime_files()
        counts = {
            "downloaded_urls": _write_empty_record(self._download_record_file()),
            "failed_urls": _write_empty_record(self._failed_record_file()),
        }
        history_file = self.downloaded_users_file()
        counts["downloaded_users"] = _write_empty_record(history_file) if history_file.exists() else 0
        return counts

    def clear_selected_download_records(self, rows: list[dict]) -> dict[str, int]:
        """Hide selected authors and remove their JSON download indexes only."""
        handles = {
            str(row.get("handle") or "").strip().lstrip("@").casefold()
            for row in rows
            if str(row.get("handle") or "").strip()
        }
        names = {
            str(row.get("name") or "").strip().casefold()
            for row in rows
            if str(row.get("name") or "").strip()
        }
        identities = handles | names
        if not identities:
            return {"downloaded_users": 0, "downloaded_urls": 0, "hidden": 0}

        hidden = self._hidden_history_keys()
        before_hidden = len(hidden)
        hidden.update(identities)
        self._write_hidden_history_keys(hidden)

        history_path = self.downloaded_users_file()
        history_payload = _read_json_file(history_path, {"version": 1, "items": {}})
        history_items = history_payload.get("items", {}) if isinstance(history_payload, dict) else {}
        removed_users = 0
        if isinstance(history_items, dict):
            kept_users = {}
            for key, value in history_items.items():
                handle = str(value.get("handle") or key).strip().lstrip("@").casefold() if isinstance(value, dict) else str(key).casefold()
                if handle in handles:
                    removed_users += 1
                else:
                    kept_users[key] = value
            history_path.write_text(
                json.dumps({"version": 1, "items": kept_users}, ensure_ascii=False, indent=2), encoding="utf-8"
            )

        record_path = self._download_record_file()
        record_payload = _read_json_file(record_path, {"version": 1, "items": {}})
        record_items = record_payload.get("items", {}) if isinstance(record_payload, dict) else {}
        removed_urls = 0
        if isinstance(record_items, dict):
            kept_urls = {}
            for key, value in record_items.items():
                file_value = str(value.get("file") or "") if isinstance(value, dict) else ""
                parent_name = Path(file_value).parent.name.strip().casefold() if file_value else ""
                if parent_name and parent_name in identities:
                    removed_urls += 1
                else:
                    kept_urls[key] = value
            record_path.write_text(
                json.dumps({"version": 1, "items": kept_urls}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return {
            "downloaded_users": removed_users,
            "downloaded_urls": removed_urls,
            "hidden": len(hidden) - before_hidden,
        }
    def following_output_file(self) -> Path:
        return self.runtime_data_dir / "following_accounts.json"

    def load_following_accounts(self) -> list[dict]:
        output = self.following_output_file()
        if not output.exists():
            return []
        try:
            payload = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for item in payload["items"]:
            row = self._normalize_following_account(item)
            if not row or row["handle"].casefold() in seen:
                continue
            seen.add(row["handle"].casefold())
            rows.append(row)
        return rows

    def following_cache_info(self) -> dict:
        output = self.following_output_file()
        info = {
            "platform": "Twitter/X",
            "path": str(output.resolve()),
            "exists": output.is_file(),
            "valid": False,
            "count": 0,
            "partial": True,
            "collected_at": "",
            "update_mode": "",
            "imported_at": "",
            "error": "",
        }
        if not output.is_file():
            info["error"] = "本地关注缓存文件不存在"
            return info
        try:
            payload = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            info["error"] = f"本地关注缓存无法读取：{exc}"
            return info
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            info["error"] = "本地关注缓存必须是包含 items 数组的 JSON"
            return info
        valid_rows = {
            row["handle"].casefold(): row
            for row in (self._normalize_following_account(item) for item in payload["items"])
            if row
        }
        info.update(
            {
                "valid": True,
                "count": len(valid_rows),
                "partial": bool(payload.get("partial", False)),
                "collected_at": str(payload.get("collected_at") or ""),
                "update_mode": str(payload.get("update_mode") or ""),
                "imported_at": str(payload.get("imported_at") or ""),
            }
        )
        return info

    @staticmethod
    def _normalize_following_account(item: object) -> dict | None:
        if isinstance(item, str):
            item = {"handle": item}
        if not isinstance(item, dict):
            return None
        handle = str(
            item.get("handle")
            or item.get("username")
            or item.get("screen_name")
            or item.get("profile_url")
            or item.get("url")
            or ""
        ).strip()
        if handle.startswith(("http://", "https://")):
            handle = target_to_handle(handle)
        handle = handle.lstrip("@").strip()
        if (
            not handle
            or not re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle)
            or handle.lower() in TWITTER_RESERVED_ROUTES
        ):
            return None
        return {
            "handle": handle,
            "display_name": str(item.get("display_name") or item.get("name") or "").strip(),
            "bio": str(item.get("bio") or item.get("description") or "").strip(),
            # A local file must not be able to turn a Twitter candidate into an
            # unrelated external profile link.  The handle remains the identity.
            "profile_url": f"https://x.com/{handle}",
            "avatar_url": str(item.get("avatar_url") or item.get("profile_image_url") or "").strip(),
            "links": list(item.get("links") or []) if isinstance(item.get("links"), list) else [],
            "skeb_links": list(item.get("skeb_links") or []) if isinstance(item.get("skeb_links"), list) else [],
            "collected_at": str(item.get("collected_at") or "").strip(),
            "source": str(item.get("source") or "").strip(),
            "import_source": str(item.get("import_source") or "").strip(),
        }

    def _read_following_import(self, source: Path | str) -> tuple[Path, list[object]]:
        path = Path(source).expanduser()
        if not path.is_file():
            raise FileNotFoundError(path)
        raw_items: list[object]
        if path.suffix.lower() == ".csv":
            with path.open("r", encoding="utf-8-sig", newline="") as input_file:
                reader = csv.DictReader(input_file)
                fields = {str(field or "").strip().lower() for field in (reader.fieldnames or [])}
                if not fields & {"handle", "username", "screen_name", "profile_url", "url"}:
                    raise ValueError("CSV 没有 handle、username、screen_name、profile_url 或 url 列")
                raw_items = list(reader)
        elif path.suffix.lower() in {".txt", ".list"}:
            raw_items = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        else:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(payload, list):
                raw_items = payload
            elif isinstance(payload, dict):
                platform = str(payload.get("platform") or "").strip().lower()
                if platform and platform not in {"twitter", "twitter/x", "x"}:
                    raise ValueError(f"这是 {platform} 数据，不是 Twitter 关注列表")
                if "items" in payload:
                    items = payload.get("items")
                    if isinstance(items, list):
                        raw_items = items
                    elif isinstance(items, dict):
                        raw_items = [
                            {"handle": key, **value} if isinstance(value, dict) else {"handle": key}
                            for key, value in items.items()
                        ]
                    else:
                        raise ValueError("JSON 关注列表的 items 必须是数组或账号映射")
                else:
                    raise ValueError("JSON 关注列表必须包含 items，不能把普通配置对象作为账号映射")
            else:
                raise ValueError("JSON 关注列表格式无效")
        return path, raw_items

    def preview_following_import(self, source: Path | str) -> dict:
        path, raw_items = self._read_following_import(source)
        normalized_items: list[dict] = []
        seen: set[str] = set()
        invalid_samples: list[str] = []
        invalid_count = 0
        for item in raw_items:
            row = self._normalize_following_account(item)
            if not row:
                invalid_count += 1
                if len(invalid_samples) < 3:
                    invalid_samples.append(str(item)[:100])
                continue
            key = row["handle"].casefold()
            if key in seen:
                continue
            seen.add(key)
            normalized_items.append(row)
        if not normalized_items:
            raise ValueError("文件中没有有效的 Twitter 用户名；本地缓存未修改")
        existing = {
            row["handle"].casefold(): row
            for row in (self._normalize_following_account(item) for item in self.load_following_accounts())
            if row
        }
        return {
            "path": str(path.resolve()),
            "source_count": len(raw_items),
            "valid_count": len(normalized_items),
            "invalid_count": invalid_count,
            "added": sum(1 for row in normalized_items if row["handle"].casefold() not in existing),
            "updated": sum(1 for row in normalized_items if row["handle"].casefold() in existing),
            "invalid_samples": invalid_samples,
            "normalized_items": normalized_items,
        }

    def import_following_accounts(self, source: Path | str) -> dict[str, object]:
        """Merge a JSON, CSV, or one-handle-per-line file into the local cache."""
        preview = self.preview_following_import(source)
        path = Path(preview["path"])
        raw_items = preview["normalized_items"]

        existing_payload = _read_json_file(self.following_output_file(), {})
        existing_rows = self.load_following_accounts()
        merged: dict[str, dict] = {}
        for item in existing_rows:
            normalized = self._normalize_following_account(item)
            if normalized:
                merged[normalized["handle"].lower()] = normalized

        added = 0
        updated = 0
        skipped = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for item in raw_items:
            normalized = self._normalize_following_account(item)
            if not normalized:
                skipped += 1
                continue
            key = normalized["handle"].lower()
            if key in merged:
                current = merged[key]
                for field, value in normalized.items():
                    if field != "handle" and value:
                        current[field] = value
                current["collected_at"] = current.get("collected_at") or now
                current["source"] = current.get("source") or "file_import"
                current["import_source"] = str(path.resolve())
                updated += 1
            else:
                normalized["collected_at"] = normalized.get("collected_at") or now
                normalized["source"] = normalized.get("source") or "file_import"
                normalized["import_source"] = str(path.resolve())
                merged[key] = normalized
                added += 1

        output = self.following_output_file()
        output.parent.mkdir(parents=True, exist_ok=True)
        items = list(merged.values())
        base = existing_payload if isinstance(existing_payload, dict) else {}
        payload = {
            "version": 2,
            "platform": "twitter",
            "account": str(base.get("account") or ""),
            "source_url": str(base.get("source_url") or ""),
            "count": len(items),
            # A file merge cannot prove that it contains the account's complete
            # current following list.  Only a successful full online update can.
            "partial": True,
            "update_mode": "file_merge",
            "collected_at": str(base.get("collected_at") or ""),
            "imported_at": now,
            "import_source": str(path.resolve()),
            "items": items,
        }
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.import.tmp")
        backup = ""
        try:
            if output.is_file():
                backup_path = output.with_name(
                    f"{output.stem}.pre-import-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.bak"
                )
                shutil.copy2(output, backup_path)
                backup = str(backup_path.resolve())
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()
        return {
            "added": added,
            "updated": updated,
            "skipped": int(preview["invalid_count"]),
            "total": len(items),
            "backup": backup,
        }

    def export_following_accounts(self, destination: Path | str) -> int:
        rows = [item for item in (self._normalize_following_account(row) for row in self.load_following_accounts()) if item]
        path = Path(destination).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.export.tmp")
        try:
            if path.suffix.lower() == ".csv":
                with temporary.open("w", encoding="utf-8-sig", newline="") as output:
                    fields = ("handle", "display_name", "bio", "profile_url", "avatar_url", "collected_at")
                    writer = csv.DictWriter(output, fieldnames=fields)
                    writer.writeheader()
                    for row in rows:
                        writer.writerow({field: safe_csv_cell(row.get(field, "")) for field in fields})
            else:
                payload = {
                    "version": 1,
                    "platform": "twitter",
                    "count": len(rows),
                    "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "items": rows,
                }
                temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        finally:
            if temporary.exists():
                temporary.unlink()
        return len(rows)

    def fetch_following(
        self,
        account: str = "",
        max_accounts: int = 0,
        idle_rounds: int = 15,
        scroll_pause: float = 1.2,
        timeout_seconds: int = 0,
        update_mode: str = "all",
        known_overlap: int = 30,
        cancel_event: threading.Event | None = None,
        on_status=None,
        on_accounts=None,
    ) -> dict:
        self._ensure_runtime_files()
        output = self.following_output_file()
        cookie = self.runtime_data_dir / "X_cookie.json"
        if not cookie.exists():
            raise FileNotFoundError("尚未找到 X Cookie；可以先继续使用本地关注列表")
        return collect_following(
            cookie_file=cookie,
            output_file=output,
            account=account,
            max_accounts=max(0, int(max_accounts or 0)),
            idle_rounds=max(2, int(idle_rounds or 2)),
            scroll_pause=max(0.4, float(scroll_pause or 1.2)),
            page_load_timeout=30,
            max_duration_seconds=max(0, int(timeout_seconds or 0)),
            update_mode=update_mode,
            known_overlap=max(1, int(known_overlap or 30)),
            cancel_event=cancel_event,
            on_status=on_status,
            on_accounts=on_accounts,
        )
    def profile_preview_file(self, target: str) -> Path:
        handle = target_to_handle(target) or "profile"
        return self.runtime_data_dir / "profile_previews" / f"{_safe_profile_name(handle)}.json"

    def profile_avatar_cache_file(self, target: str) -> Path:
        handle = target_to_handle(target) or "profile"
        return self.runtime_data_dir / "profile_previews" / f"{_safe_profile_name(handle)}-avatar-cdn.img"

    def cache_profile_avatar(self, target: str, avatar_url: str) -> str:
        """Cache an already-discovered Twitter CDN avatar without opening a profile browser."""
        parsed = urlparse(str(avatar_url or "").strip())
        if parsed.scheme != "https" or (parsed.hostname or "").lower() != "pbs.twimg.com":
            raise ValueError("只允许缓存 Twitter 官方头像 CDN")
        output = self.profile_avatar_cache_file(target)
        if output.is_file() and output.stat().st_size:
            return str(output.resolve())
        config_file = self.runtime_data_dir / "config.json"
        config = load_config(str(config_file)) if config_file.is_file() else dict(DEFAULT_CONFIG)
        configure_downloads(
            max_retries=config.get("max_retries", 5),
            connect_timeout=config.get("request_connect_timeout", 10),
            read_timeout=config.get("request_read_timeout", 60),
            proxy_url=config.get("proxy_url", ""),
            use_system_proxy=config_bool(config, "use_system_proxy", True),
            retry_backoff_base=config.get("retry_backoff_base", 1.0),
            retry_backoff_max=config.get("retry_backoff_max", 15.0),
        )
        response = request_with_retries(avatar_url)
        if response is None:
            raise OSError("Twitter 头像 CDN 请求重试后仍未成功")
        try:
            content_type = str(response.headers.get("Content-Type") or "").lower()
            if not content_type.startswith("image/"):
                raise ValueError("头像地址没有返回图片")
            content = response.content
        finally:
            response.close()
        if not content or len(content) > 5 * 1024 * 1024:
            raise ValueError("头像为空或超过 5 MB")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(output.suffix + ".part")
        temporary.write_bytes(content)
        temporary.replace(output)
        return str(output.resolve())

    def load_profile_preview(self, target: str, output_dir: Path | str | None = None) -> dict:
        self._ensure_runtime_files()
        payload = _profile_payload_from_target(target)
        handle = str(payload.get("handle") or "").strip().lstrip("@")
        lookup = handle.lower()
        target_lookup = str(target or "").strip().lstrip("@").lower()

        for row in self.downloaded_users(output_dir):
            row_handle = str(row.get("handle") or "").strip().lstrip("@")
            row_name = str(row.get("name") or "").strip()
            row_target = str(row.get("target") or "").strip().lstrip("@")
            if not any(
                value and value.lower() in {lookup, target_lookup}
                for value in (row_handle, row_name, row_target)
            ):
                continue
            _merge_profile_payload(
                payload,
                {
                    "handle": row_handle or handle,
                    "display_name": row_name or row_handle,
                    "profile_url": row.get("profile_url") or (f"https://x.com/{row_handle}" if row_handle else ""),
                    "media_url": row.get("media_url") or row.get("target") or "",
                },
                "download_history",
            )
            break

        for row in self.load_following_accounts():
            row_handle = str(row.get("handle") or "").strip().lstrip("@")
            row_name = str(row.get("display_name") or "").strip()
            if not any(
                value and value.lower() in {lookup, target_lookup}
                for value in (row_handle, row_name)
            ):
                continue
            _merge_profile_payload(
                payload,
                {
                    "handle": row_handle or handle,
                    "display_name": row_name or row_handle,
                    "bio": row.get("bio") or "",
                    "profile_url": row.get("profile_url") or (f"https://x.com/{row_handle}" if row_handle else ""),
                    "avatar_url": row.get("avatar_url") or "",
                    "links": row.get("links") or [],
                    "skeb_links": row.get("skeb_links") or [],
                },
                "following_list",
            )
            break

        cached = _read_json_file(self.profile_preview_file(handle or target), {})
        if isinstance(cached, dict) and cached:
            _merge_profile_payload(payload, cached, "cached_profile")

        avatar_cache = self.profile_avatar_cache_file(handle or target)
        if avatar_cache.is_file() and avatar_cache.stat().st_size:
            payload["avatar_path"] = str(avatar_cache.resolve())

        if not payload.get("display_name"):
            payload["display_name"] = payload.get("handle") or str(target or "").strip() or "推特主页"
        if not payload.get("profile_url") and payload.get("handle"):
            payload["profile_url"] = f"https://x.com/{payload['handle']}"
        return payload

    def fetch_profile_preview(self, target: str, timeout_seconds: int = 60) -> dict:
        self._ensure_runtime_files()
        output = self.profile_preview_file(target)
        cookie = self.runtime_data_dir / "X_cookie.json"
        script = self.twitter_root / "profile_preview.py"
        if not script.exists():
            raise FileNotFoundError(script)

        python_exe = sys.executable
        if getattr(sys, "frozen", False):
            import shutil
            python_exe = shutil.which("py") or shutil.which("python") or shutil.which("python3")
            if not python_exe:
                raise RuntimeError(
                    "打包版运行推特主页预览需要系统安装 Python。\n"
                    "请安装 Python 3.10+ 并确保 py 或 python 命令可用，\n"
                    "或使用源码版运行。"
                )

        command = [
            python_exe,
            str(script),
            target,
            "--cookie",
            str(cookie),
            "--output",
            str(output),
            "--page-load-timeout",
            str(max(10, min(int(timeout_seconds or 60), 30))),
        ]
        try:
            result = subprocess.run(
                command,
                cwd=str(self.twitter_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=max(60, int(timeout_seconds or 60) + 45),
            )
        except subprocess.TimeoutExpired as exc:
            output_text = (exc.stdout or "").strip()
            detail = f"\n{output_text}" if output_text else ""
            raise RuntimeError(f"主页预览超时，已停止。{detail}") from exc

        if result.returncode != 0:
            message = (result.stdout or "").strip() or f"主页预览脚本退出码: {result.returncode}"
            raise RuntimeError(message)
        payload = json.loads(output.read_text(encoding="utf-8-sig"))
        payload["source"] = "live_profile"
        payload["log"] = result.stdout or ""
        return payload


# Compatibility for older imports; new code should use TwitterNativeAdapter.
TwitterScriptAdapter = TwitterNativeAdapter



