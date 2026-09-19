from __future__ import annotations

import csv
import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from software_app.core.adapter import CrawlerAdapter
from software_app.core.events import CallbackSet
from software_app.core.models import DownloadTask, ModuleInfo, TargetPreview
from software_app.core.settings import PIXIV_DATA_DIR, PIXIV_ROOT
from software_app.crawlers.common import load_cookie_file
from software_app.crawlers.pixiv import PixivCrawler, collect_following_pages, parse_pixiv_target
from software_app.crawlers.pixiv.external import FanboxClient
from software_app.crawlers.pixiv.catalog import filter_bookmark_candidates
from software_app.crawlers.pixiv.following import merge_following_users, normalize_following_user


def _is_pixiv_asset_host(host: str) -> bool:
    normalized = str(host or "").strip().casefold().rstrip(".")
    return normalized in {"pixiv.net", "pximg.net"} or normalized.endswith((".pixiv.net", ".pximg.net"))


def _validated_preview_image(response) -> bytes:
    content_type = str(getattr(response, "headers", {}).get("Content-Type") or "").split(";", 1)[0].strip().casefold()
    if not content_type.startswith("image/"):
        raise ValueError("Pixiv 头像地址没有返回图片；可能是登录页、拦截页或失效链接")
    content = response.content
    if not content or len(content) > 5 * 1024 * 1024:
        raise ValueError("Pixiv 预览图为空或超过 5 MiB")
    return content


class PixivNativeAdapter(CrawlerAdapter):
    max_concurrency = 2

    def __init__(self) -> None:
        self.runtime_data_dir = Path(PIXIV_DATA_DIR)
        self.info = ModuleInfo(
            module_id="pixiv",
            display_name="Pixiv",
            description="内置 Pixiv 平台插件，支持作品/动图、作者、系列、小说、排行、新作、收藏、FANBOX、Sketch 和分页关注。",
            stage="ready",
            capabilities=("preview", "search", "download", "following", "native", "ai-filter", "ugoira", "novel", "plugin"),
            script_root=PIXIV_ROOT,
        )

    def _client(self, options: dict | None = None) -> PixivCrawler:
        options = options or {}
        cookie = options.get("cookie") or self.runtime_data_dir / "cookies.json"
        return PixivCrawler(cookie_file=cookie, proxy_url=str(options.get("proxy_url") or ""))

    def validate(self) -> list[str]:
        cookie = self.runtime_data_dir / "cookies.json"
        cookies = load_cookie_file(cookie)
        warnings: list[str] = []
        if not cookies.get("PHPSESSID"):
            warnings.append("未配置有效的 Pixiv PHPSESSID；公开内容可能可用，登录限定内容不可用")
        if not cookies.get("FANBOXSESSID"):
            warnings.append("未配置 FANBOXSESSID；FANBOX 付费或登录限定内容不可用")
        return warnings

    def cookie_account_id(self) -> str:
        cookies = load_cookie_file(self.runtime_data_dir / "cookies.json")
        session_id = str(cookies.get("PHPSESSID") or "").strip()
        match = re.match(r"^(\d+)_", session_id)
        return match.group(1) if match else ""

    def can_handle(self, raw_target: str) -> bool:
        value = raw_target.strip()
        if not value:
            return False
        try:
            parse_pixiv_target(value)
        except ValueError:
            return False
        return True

    def preview_target(self, raw_target: str, options: dict | None = None) -> TargetPreview:
        options = options or {}
        target = parse_pixiv_target(raw_target, str(options.get("input_kind") or ""))
        cached = self.load_profile_preview(target.value) if target.kind in {"user", "user_novels"} else {}
        if target.kind in {"user", "user_novels"} and not cached:
            cached = next(
                (
                    {
                        "author_id": row["user_id"],
                        "display_name": row.get("display_name") or row["user_id"],
                        "bio": row.get("bio") or "",
                        "avatar_url": row.get("avatar_url") or "",
                        "profile_url": row.get("profile_url") or f"https://www.pixiv.net/users/{row['user_id']}",
                        "cache_source": "following",
                    }
                    for row in self.load_following_accounts()
                    if str(row.get("user_id") or "") == target.value
                ),
                {},
            )
        if not bool(options.get("live", False)) and cached:
            preview = TargetPreview(
                module_id="pixiv",
                raw_target=raw_target,
                normalized_target=str(cached.get("profile_url") or f"https://www.pixiv.net/users/{target.value}"),
                title=str(cached.get("display_name") or target.value),
                description=str(cached.get("bio") or "已读取本地缓存的 Pixiv 作者资料"),
                metadata={**cached, "input_kind": target.kind, "target_id": target.value, "cached": True},
            )
        else:
            client = self._client(options)
            preview = client.preview(raw_target, options)
            if bool(options.get("live", False)) and target.kind in {"user", "user_novels"}:
                preview = self._cache_profile_preview(client, target.value, preview)
            elif bool(options.get("live", False)) and target.kind == "history":
                rows = preview.metadata.get("search_results")
                if isinstance(rows, list):
                    self._write_account_history([row for row in rows if isinstance(row, dict)])
        return TargetPreview(**{**preview.__dict__, "warnings": [*preview.warnings, *self.validate()]})

    def download(self, task: DownloadTask, callbacks: CallbackSet, cancel_event: threading.Event) -> None:
        self._client(task.options).download(task, callbacks, cancel_event)

    def inspect_artwork(self, url: str, *, proxy_url: str = "") -> dict:
        """Read one work's author for a reviewed similarity-search candidate."""
        target = parse_pixiv_target(url)
        if target.kind != "work":
            raise ValueError("请选择 Pixiv 作品详情页")
        client = self._client({"proxy_url": proxy_url})
        try:
            summary = client.fetch_preview(target)
        finally:
            client.session.close()
        author_id = str(summary.get("author_id") or "")
        if not author_id.isdigit():
            raise ValueError("Pixiv 作品未返回可识别的作者 ID")
        return {"work_id": target.value, "author_id": author_id,
                "author_name": str(summary.get("author_name") or ""),
                "title": str(summary.get("title") or "")}

    def search_targets(self, query: str, limit: int = 20, options: dict | None = None) -> list[dict]:
        options = options or {}
        if str(options.get("age_mode") or "all").lower() == "r18":
            cookies = load_cookie_file(options.get("cookie") or self.runtime_data_dir / "cookies.json")
            if not cookies.get("PHPSESSID"):
                raise ValueError("R-18 搜索需要有效 PHPSESSID，并在 Pixiv 浏览设置中开启年龄限制作品")
        options = {**options, "account_id": options.get("account_id") or self.cookie_account_id()}
        rows = self._client(options).search(
            query,
            limit=limit,
            search_mode=str(options.get("search_mode") or ""),
            options=options,
        )
        if str(options.get("search_mode") or "") == "浏览历史（Premium）":
            self._write_account_history(rows)
        return rows

    def profile_cache_dir(self) -> Path:
        return self.runtime_data_dir / "profile_previews"

    def load_profile_preview(self, user_id: str) -> dict:
        path = self.profile_cache_dir() / f"{str(user_id).strip()}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict) or str(payload.get("author_id") or "") != str(user_id):
            return {}
        avatar_path = Path(str(payload.get("avatar_path") or ""))
        if not avatar_path.is_file():
            payload.pop("avatar_path", None)
        return payload

    def _cache_profile_preview(self, client: PixivCrawler, user_id: str, preview: TargetPreview) -> TargetPreview:
        cache_dir = self.profile_cache_dir()
        cache_dir.mkdir(parents=True, exist_ok=True)
        metadata = dict(preview.metadata)
        metadata.update(
            {
                "author_id": str(metadata.get("author_id") or user_id),
                "display_name": str(metadata.get("display_name") or preview.title or user_id),
                "bio": str(metadata.get("bio") or preview.description or ""),
                "profile_url": str(metadata.get("profile_url") or preview.normalized_target),
            }
        )
        warnings = list(preview.warnings)
        avatar_url = str(metadata.get("avatar_url") or "").strip()
        if avatar_url.startswith(("http://", "https://")):
            avatar_path = cache_dir / f"{user_id}.avatar"
            try:
                response = client.session.get(avatar_url, timeout=(5, 20), headers={"Referer": "https://www.pixiv.net/"})
                response.raise_for_status()
                content = response.content
                if not content:
                    raise ValueError("头像响应为空")
                temporary_avatar = avatar_path.with_name(f".{avatar_path.name}.{uuid.uuid4().hex}.tmp")
                temporary_avatar.write_bytes(content)
                temporary_avatar.replace(avatar_path)
                metadata["avatar_path"] = str(avatar_path.resolve())
            except Exception as exc:  # Avatar failure must not discard the useful profile data.
                message = str(exc).splitlines()[0]
                if "proxy" in message.casefold():
                    message = "当前设置的代理无法连接；请在设置中修改或清空代理"
                warnings.append(f"作者资料已获取，但头像缓存失败：{message}")
        path = cache_dir / f"{user_id}.json"
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        return TargetPreview(**{**preview.__dict__, "metadata": metadata, "warnings": warnings})

    def account_history_file(self) -> Path:
        return self.runtime_data_dir / "account_history.json"

    def load_account_history(self) -> list[dict]:
        try:
            payload = json.loads(self.account_history_file().read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return []
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return []
        return [
            item for item in items
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://www.pixiv.net/artworks/")
        ]

    def _write_account_history(self, rows: list[dict]) -> None:
        path = self.account_history_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "platform": "pixiv", "source": "account_history", "count": len(rows), "items": rows}
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def clear_account_history(self, targets: set[str] | None = None) -> int:
        rows = self.load_account_history()
        if targets is None:
            removed = len(rows)
            self.account_history_file().unlink(missing_ok=True)
            return removed
        normalized = {str(value).strip().casefold() for value in targets if str(value).strip()}
        kept = [row for row in rows if str(row.get("url") or "").strip().casefold() not in normalized]
        removed = len(rows) - len(kept)
        if kept:
            self._write_account_history(kept)
        else:
            self.account_history_file().unlink(missing_ok=True)
        return removed

    def bookmark_output_file(self, kind: str) -> Path:
        return self.runtime_data_dir / ("bookmarked_novels.json" if kind == "novel" else "bookmarked_works.json")

    def load_bookmark_payload(self, kind: str) -> dict:
        path = self.bookmark_output_file(kind)
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "platform": "pixiv", "kind": kind, "count": 0, "items": []}
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            return {"version": 1, "platform": "pixiv", "kind": kind, "count": 0, "items": []}
        return payload

    def load_bookmark_candidates(
        self, kind: str, *, query: str = "", field: str = "全部",
        date_basis: str = "收藏时间", start_date: str = "", end_date: str = "",
    ) -> list[dict]:
        payload = self.load_bookmark_payload(kind)
        cached_account = str(payload.get("account") or "")
        current_account = self.cookie_account_id()
        if cached_account and current_account and cached_account != current_account:
            return []
        rows = [
            item
            for item in payload.get("items", [])
            if isinstance(item, dict) and str(item.get("url") or "").startswith("https://www.pixiv.net/")
        ]
        return filter_bookmark_candidates(
            rows, query=query, field=field, date_basis=date_basis,
            start_date=start_date, end_date=end_date,
        )

    def refresh_own_bookmarks(
        self,
        kind: str,
        *,
        limit: int = 10000,
        options: dict | None = None,
    ) -> list[dict]:
        bookmark_kind = "novel" if kind == "novel" else "work"
        account_id = self.cookie_account_id()
        if not account_id:
            raise ValueError("无法从 PHPSESSID 识别当前 Pixiv 账号；请重新导入 Cookie")
        request_options = {**(options or {}), "account_id": account_id}
        # A saved collection must not be replaced by a filtered search subset.
        request_options.update({"bookmark_tag": "", "start_date": "", "end_date": ""})
        search_mode = "小说收藏" if bookmark_kind == "novel" else "作品收藏"
        client = self._client(request_options)
        rows = client.search(
            account_id,
            limit=max(1, min(int(limit), 10000)),
            search_mode=search_mode,
            options=request_options,
        )
        status = getattr(client, "last_bookmark_status", {})
        complete = bool(status.get("complete", len(rows) < limit))
        previous = self.load_bookmark_payload(bookmark_kind)
        if rows == [] and previous.get("items") and previous.get("account") == account_id:
            complete = False
        cached_rows = list(rows)
        if not complete and previous.get("account") == account_id:
            seen = {str(item.get("url") or "") for item in cached_rows if isinstance(item, dict)}
            cached_rows.extend(
                item for item in previous.get("items", [])
                if isinstance(item, dict) and str(item.get("url") or "") not in seen
            )
        payload = {
            "version": 1,
            "platform": "pixiv",
            "kind": bookmark_kind,
            "account": account_id,
            "count": len(cached_rows),
            "fetched_count": len(rows),
            "complete": complete,
            "partial": not complete,
            "scopes": status.get("scopes", []),
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "items": cached_rows,
        }
        self._write_json_payload(self.bookmark_output_file(bookmark_kind), payload)
        return cached_rows

    def fanbox_supporting_status(self, options: dict | None = None) -> dict:
        return FanboxClient(self._client(options).session).supporting_status()

    def fetch_preview_image_bytes(self, url: str, options: dict | None = None) -> bytes:
        image_url = str(url or "").strip()
        host = str(urlparse(image_url).hostname or "").casefold()
        if not image_url.startswith("https://") or not _is_pixiv_asset_host(host):
            raise ValueError("Pixiv 预览图必须来自 pixiv.net 或 pximg.net")
        response = self._client(options).session.get(
            image_url,
            timeout=(5, 25),
            headers={"Referer": "https://www.pixiv.net/"},
        )
        response.raise_for_status()
        return _validated_preview_image(response)

    def connection_status(self, options: dict | None = None) -> dict:
        account_id = self.cookie_account_id()
        if not account_id:
            raise ValueError("无法从 PHPSESSID 识别当前 Pixiv 账号")
        client = self._client(options)
        body = client._body(f"/ajax/user/{account_id}?full=1", timeout=(5, 15))
        if not isinstance(body, dict):
            raise ValueError("Pixiv 作者接口没有返回有效资料")
        avatar_url = client._profile_avatar_url(body)
        avatar_ok = False
        if avatar_url:
            avatar_host = str(urlparse(avatar_url).hostname or "").casefold()
            if not str(avatar_url).startswith("https://") or not _is_pixiv_asset_host(avatar_host):
                raise ValueError("Pixiv 资料返回了不受信任的头像地址")
            response = client.session.get(
                avatar_url,
                timeout=(5, 20),
                headers={"Referer": "https://www.pixiv.net/"},
            )
            response.raise_for_status()
            avatar_ok = bool(_validated_preview_image(response))
        return {
            "account_id": account_id,
            "profile_ok": True,
            "avatar_ok": avatar_ok,
            "message": (
                f"Pixiv 连接正常；账号 {account_id} 的资料接口可用；"
                + ("头像图片可读取。" if avatar_ok else "资料未返回头像地址。")
            ),
        }

    def following_output_file(self) -> Path:
        return self.runtime_data_dir / "following_accounts.json"

    def import_cookie_file(self, source: Path | str) -> int:
        source_path = Path(source).expanduser()
        cookies = load_cookie_file(source_path)
        if not cookies:
            raise ValueError("没有从所选 JSON 中读取到 Cookie")
        if "PHPSESSID" not in cookies and "FANBOXSESSID" not in cookies:
            raise ValueError("所选 Cookie 缺少 PHPSESSID 和 FANBOXSESSID；请从已登录的 Pixiv/FANBOX 浏览器重新导出")
        output = self.runtime_data_dir / "cookies.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()
        return len(cookies)

    def load_following_payload(self) -> dict:
        path = self.following_output_file()
        if not path.exists():
            return {"version": 1, "platform": "pixiv", "count": 0, "items": []}
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "platform": "pixiv", "count": 0, "items": []}
        if isinstance(payload, list):
            return {"version": 1, "platform": "pixiv", "count": len(payload), "items": payload}
        return payload if isinstance(payload, dict) else {"version": 1, "platform": "pixiv", "count": 0, "items": []}

    def load_following_accounts(self) -> list[dict]:
        items = self.load_following_payload().get("items")
        if not isinstance(items, list):
            return []
        rows: list[dict] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            row = normalize_following_user(item, str(item.get("visibility") or "show"))
            if not row or row["user_id"] in seen:
                continue
            seen.add(row["user_id"])
            rows.append(row)
        return rows

    def export_following_accounts(self, destination: Path | str) -> int:
        output = Path(destination).expanduser()
        rows = self.load_following_accounts()
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.lower() == ".json":
            output.write_text(
                json.dumps(
                    {"version": 1, "platform": "pixiv", "count": len(rows), "items": rows},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        else:
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=(
                        "user_id",
                        "display_name",
                        "bio",
                        "profile_url",
                        "avatar_url",
                        "visibility",
                    ),
                    extrasaction="ignore",
                )
                writer.writeheader()
                writer.writerows(rows)
        return len(rows)

    def following_cache_info(self) -> dict:
        path = self.following_output_file()
        info = {
            "platform": "Pixiv",
            "path": str(path.resolve()),
            "exists": path.is_file(),
            "valid": False,
            "count": 0,
            "partial": True,
            "collected_at": "",
            "update_mode": "",
            "account": "",
            "error": "",
        }
        if not path.is_file():
            info["error"] = "本地关注缓存文件不存在"
            return info
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            info["error"] = f"本地关注缓存无法读取：{exc}"
            return info
        if isinstance(payload, list):
            items = payload
            metadata = {}
        elif isinstance(payload, dict) and isinstance(payload.get("items"), list):
            items = payload["items"]
            metadata = payload
        else:
            info["error"] = "本地关注缓存必须是账号数组，或包含 items 数组的 JSON"
            return info
        valid_rows = {
            row["user_id"]: row
            for row in (
                normalize_following_user(item, str(item.get("visibility") or "show"))
                for item in items
                if isinstance(item, dict)
            )
            if row
        }
        info.update(
            {
                "valid": True,
                "count": len(valid_rows),
                "partial": bool(metadata.get("partial", False)),
                "collected_at": str(metadata.get("collected_at") or ""),
                "update_mode": str(metadata.get("update_mode") or ""),
                "account": str(metadata.get("account") or ""),
            }
        )
        return info

    def fetch_following(
        self,
        account: str,
        *,
        update_mode: str = "new",
        visibility: str = "show",
        cancel_event: threading.Event | None = None,
        on_status=None,
        on_accounts=None,
        options: dict | None = None,
    ) -> dict:
        raw_account = str(account or "").strip()
        match = re.search(r"/users/(\d+)", raw_account)
        owner_id = raw_account if raw_account.isdigit() else (match.group(1) if match else "")
        if not owner_id:
            raise ValueError("请输入要读取关注列表的 Pixiv 用户 ID 或用户网址")
        client = self._client(options)
        existing = self.load_following_accounts()
        source_url = f"https://www.pixiv.net/users/{owner_id}/following"

        def publish_partial(payload: dict) -> None:
            partial = {
                **payload,
                "version": 1,
                "platform": "pixiv",
                "account": owner_id,
                "source_url": source_url,
                "partial": True,
                "complete": False,
            }
            partial["count"] = len(partial.get("items") or [])
            self._write_following_payload(partial)
            if on_accounts:
                on_accounts(partial)

        if visibility == "both":
            collected: list[dict] = []
            parts: list[dict] = []
            for rest in ("show", "hide"):
                if cancel_event is not None and cancel_event.is_set():
                    break
                old_part = [row for row in existing if str(row.get("visibility") or "show") == rest]
                part = collect_following_pages(
                    lambda page, current_rest=rest: client.fetch_following_page(
                        owner_id, page=page, visibility=current_rest
                    ),
                    old_part,
                    update_mode=update_mode,
                    cancel_event=cancel_event,
                    on_status=(lambda message, current_rest=rest: on_status(
                        f"{'私密' if current_rest == 'hide' else '公开'}关注：{message}"
                    )) if on_status else None,
                    on_accounts=(lambda payload, current_rest=rest: publish_partial({
                        **payload,
                        "visibility": "both",
                        "items": merge_following_users(
                            existing,
                            [*collected, *(payload.get("items") or [])],
                            replace=False,
                        ),
                    })),
                )
                parts.append(part)
                collected.extend(part.get("items", []))
            complete = len(parts) == 2 and all(bool(part.get("complete")) for part in parts)
            # “公开 + 私密”只要有一侧未完整读取，就不能据此删除旧缓存中的账号。
            # 完整更新成功时则不追加旧数据，以便正确反映已经取关的账号。
            if not complete:
                collected.extend(existing)
            seen: set[str] = set()
            items: list[dict] = []
            for row in collected:
                user_id = str(row.get("user_id") or "")
                if not user_id or user_id in seen:
                    continue
                seen.add(user_id)
                items.append(row)
            result = {
                "version": 1,
                "platform": "pixiv",
                "items": items,
                "count": len(items),
                "partial": not complete,
                "complete": complete,
                "update_mode": update_mode,
                "visibility": "both",
                "new_count": sum(int(part.get("new_count") or 0) for part in parts),
                "removed_count": sum(int(part.get("removed_count") or 0) for part in parts),
                "stopped_reason": "+".join(
                    reason
                    for reason in (str(part.get("stopped_reason") or "") for part in parts)
                    if reason
                ),
            }
        else:
            result = collect_following_pages(
                lambda page: client.fetch_following_page(owner_id, page=page, visibility=visibility),
                existing,
                update_mode=update_mode,
                cancel_event=cancel_event,
                on_status=on_status,
                on_accounts=publish_partial,
            )
        result["account"] = owner_id
        result["source_url"] = source_url
        self._write_following_payload(result)
        if visibility == "both" and on_accounts:
            on_accounts(result)
        return result

    def _write_following_payload(self, payload: dict) -> None:
        self._write_json_payload(self.following_output_file(), payload)

    @staticmethod
    def _write_json_payload(output: Path, payload: dict) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(output)
        finally:
            if temporary.exists():
                temporary.unlink()
