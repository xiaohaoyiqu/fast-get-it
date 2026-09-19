from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse, urlunparse

from .exports import _atomic_write_text


AccountKey = tuple[str, str]
WorkKey = tuple[str, str]
_TWITTER_RESERVED = {"home", "search", "explore", "i", "intent", "messages", "settings", "notifications", "compose"}
_INSTAGRAM_RESERVED = {"accounts", "direct", "explore", "p", "reel", "reels", "stories", "tv"}


def account_from_url(value: str) -> AccountKey | None:
    """Accept direct account pages only; a post or artwork is not proof of authorship."""
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}:
        if parts and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]) and parts[0].casefold() not in _TWITTER_RESERVED:
            if len(parts) == 1 or (len(parts) == 2 and parts[1].casefold() == "media"):
                return "twitter", parts[0].casefold()
        return None
    if host == "pixiv.net" or host.endswith(".pixiv.net"):
        if host == "sketch.pixiv.net":
            if len(parts) == 1 and parts[0].startswith("@") and len(parts[0]) > 1:
                return "sketch", parts[0][1:].casefold()
            return None
        if parts and parts[0] == "en":
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "users" and parts[1].isdigit():
            return "pixiv", parts[1]
        if parsed.path.rstrip("/") == "/member.php":
            legacy_id = parse_qs(parsed.query).get("id", [""])[0]
            return ("pixiv", legacy_id) if legacy_id.isdigit() else None
        return None
    if host == "fanbox.cc" or host.endswith(".fanbox.cc"):
        if len(parts) == 1 and parts[0].startswith("@") and len(parts[0]) > 1:
            return "fanbox", parts[0][1:].casefold()
        subdomain = host.removesuffix(".fanbox.cc")
        if subdomain and subdomain not in {"www", "api", "fanbox"} and not parts:
            return "fanbox", subdomain
        return None
    if host in {"bsky.app", "www.bsky.app"}:
        if len(parts) == 2 and parts[0] == "profile" and parts[1]:
            return "bluesky", parts[1].casefold()
        return None
    if host in {"instagram.com", "www.instagram.com"}:
        if len(parts) == 1 and re.fullmatch(r"[A-Za-z0-9._]{1,30}", parts[0]) and parts[0].casefold() not in _INSTAGRAM_RESERVED:
            return "instagram", parts[0].casefold()
        return None
    return None


def account_from_target(module_id: str, target: str, input_kind: str = "") -> AccountKey | None:
    value = str(target or "").strip()
    if value.startswith(("http://", "https://")):
        account = account_from_url(value)
        if account:
            return account
        parsed = urlparse(value)
        host = str(parsed.hostname or "").casefold().rstrip(".")
        parts = [part for part in parsed.path.split("/") if part]
        if host in {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}:
            if len(parts) >= 3 and parts[1] == "status" and parts[0].casefold() not in _TWITTER_RESERVED and re.fullmatch(r"[A-Za-z0-9_]{1,15}", parts[0]):
                return "twitter", parts[0].casefold()
        if host in {"bsky.app", "www.bsky.app"}:
            if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
                return "bluesky", parts[1].casefold()
        return None
    kind = str(input_kind or "").casefold()
    if module_id == "twitter":
        handle = value.lstrip("@")
        return ("twitter", handle.casefold()) if re.fullmatch(r"[A-Za-z0-9_]{1,15}", handle) else None
    if module_id == "pixiv":
        if kind in {"user", "user_novels", "artist"} and value.isdigit():
            return "pixiv", value
        if kind == "fanbox" and value.lstrip("@"):
            return "fanbox", value.lstrip("@").casefold()
        if kind == "sketch" and value.lstrip("@"):
            return "sketch", value.lstrip("@").casefold()
    if module_id == "jmcomic" and kind in {"author", "jmcomic_author"} and value:
        return "jmcomic_author", " ".join(value.split()).casefold()
    if module_id == "bluesky" and value:
        return "bluesky", value.lstrip("@").casefold()
    if module_id == "instagram" and value and not value.startswith(("http://", "https://")):
        account = value.lstrip("@").casefold()
        return ("instagram", account) if re.fullmatch(r"[a-z0-9._]{1,30}", account) else None
    return None


def work_from_target(module_id: str, target: str, input_kind: str = "") -> WorkKey | None:
    """Identify one work/post or one exact page on an unknown site."""
    value = str(target or "").strip()
    if not value.startswith(("http://", "https://")):
        if module_id == "pixiv" and str(input_kind).casefold() in {"work", "artwork", "novel"} and value.isdigit():
            return ("pixiv_novel" if str(input_kind).casefold() == "novel" else "pixiv", value)
        if module_id in {"jmcomic", "jmcomic_chapter", "jmcomic_novel"}:
            match = re.fullmatch(r"(?:JM)?(\d+)", value, flags=re.IGNORECASE)
            if match:
                kind = str(input_kind).casefold()
                platform = (
                    "jmcomic_chapter" if module_id == "jmcomic_chapter" or kind in {"photo", "chapter"}
                    else "jmcomic_novel" if module_id == "jmcomic_novel" or kind == "novel"
                    else "jmcomic"
                )
                return platform, match.group(1)
        if module_id == "ehentai":
            match = re.fullmatch(r"(\d+)(?:/[0-9a-fA-F]{10})?", value.strip("/"))
            if match:
                return "ehentai", match.group(1)
        return None
    parsed = urlparse(value)
    if not parsed.hostname:
        return None
    host = parsed.hostname.casefold().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    if module_id in {"jmcomic", "jmcomic_chapter", "jmcomic_novel"}:
        if len(parts) >= 2 and parts[0].casefold() in {"album", "albums"} and parts[1].isdigit():
            return "jmcomic", parts[1]
        if len(parts) >= 2 and parts[0].casefold() in {"photo", "photos"} and parts[1].isdigit():
            return "jmcomic_chapter", parts[1]
        if len(parts) >= 2 and parts[0].casefold() in {"novel", "novels"} and parts[1].isdigit():
            return "jmcomic_novel", parts[1]
        legacy_id = parse_qs(parsed.query).get("id", [""])[0]
        if legacy_id.isdigit() and str(input_kind).casefold() in {"album", "work", "photo", "chapter"}:
            platform = "jmcomic_chapter" if str(input_kind).casefold() in {"photo", "chapter"} else "jmcomic"
            return platform, legacy_id
    if (host == "pixiv.net" or host.endswith(".pixiv.net")) and host != "sketch.pixiv.net":
        if parts and parts[0] == "en":
            parts = parts[1:]
        if len(parts) >= 2 and parts[0] == "artworks" and parts[1].isdigit():
            return "pixiv", parts[1]
        if parsed.path.rstrip("/") in {"/novel/show.php", "/en/novel/show.php"}:
            novel_id = parse_qs(parsed.query).get("id", [""])[0]
            if novel_id.isdigit():
                return "pixiv_novel", novel_id
    if host == "fanbox.cc" or host.endswith(".fanbox.cc"):
        if len(parts) >= 2 and parts[-2] == "posts" and parts[-1].isdigit():
            return "fanbox", parts[-1]
    if host == "sketch.pixiv.net" and len(parts) >= 2 and parts[-2] == "items":
        return "sketch", parts[-1]
    if host in {"x.com", "twitter.com", "mobile.twitter.com", "www.x.com", "www.twitter.com"}:
        if len(parts) >= 3 and parts[0] == "i" and parts[1] == "status" and parts[2].isdigit():
            return "twitter", parts[2]
        if len(parts) >= 3 and parts[1] == "status" and parts[2].isdigit():
            return "twitter", parts[2]
    if host in {"bsky.app", "www.bsky.app"}:
        if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
            return "bluesky", f"{parts[1].casefold()}/{parts[3]}"
    if host in {"instagram.com", "www.instagram.com"}:
        if len(parts) >= 2 and parts[0].casefold() in {"p", "reel", "tv"}:
            return "instagram", parts[1]
    if host in {"e-hentai.org", "www.e-hentai.org", "exhentai.org", "www.exhentai.org"}:
        if len(parts) >= 3 and parts[0].casefold() == "g" and parts[1].isdigit() and re.fullmatch(r"[0-9a-fA-F]{10}", parts[2]):
            return "ehentai", parts[1]
    if account_from_url(value):
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    netloc = f"{host}:{port}" if port else host
    clean = parsed._replace(scheme="https", netloc=netloc, path=parsed.path.rstrip("/") or "/", fragment="")
    return "url", urlunparse(clean)


def _import_work_target(platform: str, value: str) -> str:
    if value.startswith(("http://", "https://")):
        target = value
    elif platform == "pixiv" and value.isdigit():
        target = f"https://www.pixiv.net/artworks/{value}"
    elif platform == "pixiv_novel" and value.isdigit():
        target = f"https://www.pixiv.net/novel/show.php?id={value}"
    elif platform == "fanbox" and value.isdigit():
        target = f"https://www.fanbox.cc/posts/{value}"
    elif platform == "twitter" and value.isdigit():
        target = f"https://x.com/i/status/{value}"
    elif platform == "sketch" and value:
        target = f"https://sketch.pixiv.net/items/{value}"
    elif platform == "bluesky" and "/" in value:
        author, post = value.split("/", 1)
        target = f"https://bsky.app/profile/{author}/post/{post}"
    elif platform == "instagram" and value:
        target = f"https://www.instagram.com/p/{value}/"
    elif platform == "jmcomic" and value.isdigit():
        target = f"https://18comic.vip/album/{value}"
    elif platform == "jmcomic_chapter" and value.isdigit():
        target = f"https://18comic.vip/photo/{value}"
    elif platform == "jmcomic_novel" and value.isdigit():
        target = f"https://18comic.vip/novel/{value}"
    elif platform == "ehentai" and (value.isdigit() or re.fullmatch(r"\d+/[0-9a-fA-F]{10}", value.strip("/"))):
        target = value
    else:
        raise ValueError(f"无效的 {platform} 作品规则：{value}")
    key = work_from_target(platform, target)
    if not key or key[0] != platform:
        raise ValueError(f"作品规则与 {platform} 平台不匹配：{value}")
    return target


class BlocklistStore:
    """Explicit account groups; outbound links are added only when the user chooses them."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _payload(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"version": 2, "groups": [], "works": []}
        if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list) or not isinstance(payload.get("works", []), list):
            raise ValueError("跨平台黑名单文件格式无效")
        return payload

    def works(self) -> list[dict]:
        return [item for item in self._payload().get("works", []) if isinstance(item, dict)]

    def blocked_works(self) -> set[WorkKey]:
        return {(str(item.get("platform") or ""), str(item.get("work") or ""))
                for item in self.works() if item.get("platform") and item.get("work")}

    def groups(self) -> list[dict]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return []
        if not isinstance(payload, dict) or not isinstance(payload.get("groups"), list):
            raise ValueError("跨平台黑名单文件格式无效")
        return [item for item in payload["groups"] if isinstance(item, dict)]

    def blocked_accounts(self) -> set[AccountKey]:
        result: set[AccountKey] = set()
        for group in self.groups():
            for item in group.get("accounts") or []:
                if isinstance(item, dict):
                    platform = str(item.get("platform") or "").casefold()
                    account = str(item.get("account") or "").casefold()
                    if platform and account:
                        result.add((platform, account))
        return result

    def is_blocked(
        self, module_id: str, target: str, *, input_kind: str = "", author_id: str = "",
        tags: list[str] | tuple[str, ...] | None = None,
        blocked_accounts: set[AccountKey] | None = None,
        blocked_works: set[WorkKey] | None = None,
    ) -> bool:
        blocked = self.blocked_accounts() if blocked_accounts is None else blocked_accounts
        key = account_from_target(module_id, target, input_kind)
        if key in blocked:
            return True
        work_key = work_from_target(module_id, target, input_kind)
        if author_id:
            is_pixiv_work = module_id == "pixiv" or (
                module_id in {"google_image", "website"} and work_key is not None and work_key[0] == "pixiv"
            )
            if is_pixiv_work and ("pixiv", str(author_id)) in blocked:
                return True
            if module_id == "twitter" and ("twitter_id", str(author_id)) in blocked:
                return True
            if module_id == "jmcomic" and ("jmcomic_author", " ".join(str(author_id).split()).casefold()) in blocked:
                return True
        if module_id == "jmcomic" and any(
            ("jmcomic_tag", " ".join(str(tag).split()).casefold()) in blocked for tag in (tags or []) if str(tag).strip()
        ):
            return True
        works = self.blocked_works() if blocked_works is None else blocked_works
        return work_key in works

    def add_group(self, primary: AccountKey, linked: list[AccountKey] | None = None, *, label: str = "") -> dict:
        primary = (primary[0].casefold(), primary[1].casefold())
        keys = [primary, *((platform.casefold(), account.casefold()) for platform, account in (linked or []))]
        groups = self.groups()
        existing = next((group for group in groups if group.get("primary") == {"platform": primary[0], "account": primary[1]}), None)
        if existing:
            keys.extend(
                (str(item.get("platform") or ""), str(item.get("account") or ""))
                for item in existing.get("accounts") or [] if isinstance(item, dict)
            )
            groups.remove(existing)
        accounts = [{"platform": platform, "account": account} for platform, account in dict.fromkeys(keys) if platform and account]
        group = {
            "id": str(existing.get("id")) if existing else uuid.uuid4().hex[:12],
            "label": str(label or f"{primary[0]}:{primary[1]}")[:120],
            "primary": {"platform": primary[0], "account": primary[1]},
            "accounts": accounts,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        groups.append(group)
        self._save(groups)
        return group

    def merge_account_records(self, records: list[dict]) -> int:
        """Commit a reviewed platform snapshot in one atomic write; keep existing rules."""
        groups = self.groups()
        index = {
            (str(group.get("primary", {}).get("platform") or ""), str(group.get("primary", {}).get("account") or "")): group
            for group in groups if isinstance(group.get("primary"), dict)
        }
        existing = self.blocked_accounts()
        added = 0
        changed = False
        for record in records:
            raw_primary = record.get("primary")
            if not isinstance(raw_primary, (tuple, list)) or len(raw_primary) != 2:
                raise ValueError("导入记录缺少作者账号")
            primary = (str(raw_primary[0]).casefold(), str(raw_primary[1]).casefold())
            if not all(primary):
                raise ValueError("导入记录的作者账号为空")
            linked = [(str(platform).casefold(), str(account).casefold())
                      for platform, account in record.get("linked") or [] if platform and account]
            keys = list(dict.fromkeys([primary, *linked]))
            group = index.get(primary)
            if group is None:
                if primary in existing:
                    continue
                group = {"id": uuid.uuid4().hex[:12], "label": str(record.get("label") or f"{primary[0]}:{primary[1]}")[:120],
                         "primary": {"platform": primary[0], "account": primary[1]}, "accounts": [],
                         "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                         "source": str(record.get("source") or "导入")[:80]}
                groups.append(group)
                index[primary] = group
                added += 1
            current = [(str(item.get("platform") or ""), str(item.get("account") or ""))
                       for item in group.get("accounts") or [] if isinstance(item, dict)]
            original_current = current
            if primary[0] == "bluesky" and str(record.get("source") or "").startswith("Bluesky"):
                # A handle can change owners; the DID is the durable identity.
                current = [key for key in current if key[0] != "bluesky" or key == primary or key in keys]
            merged = list(dict.fromkeys([*current, *keys]))
            if merged != original_current:
                group["accounts"] = [{"platform": platform, "account": account} for platform, account in merged]
                existing.update(merged)
                changed = True
        if changed:
            self._save(groups)
        return added

    def remove_group(self, group_id: str) -> bool:
        groups = self.groups()
        remaining = [group for group in groups if str(group.get("id") or "") != group_id]
        if len(remaining) == len(groups):
            return False
        self._save(remaining)
        return True

    def add_work(self, module_id: str, target: str, *, input_kind: str = "", label: str = "") -> dict:
        key = work_from_target(module_id, target, input_kind)
        if not key:
            raise ValueError("请输入作品、帖子或具体页面地址")
        works = self.works()
        item = next((entry for entry in works if (entry.get("platform"), entry.get("work")) == key), None)
        if item is None:
            item = {"id": uuid.uuid4().hex[:12], "platform": key[0], "work": key[1],
                    "label": str(label or target)[:120], "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
            works.append(item)
        else:
            item["label"] = str(label or item.get("label") or target)[:120]
        self._save(self.groups(), works)
        return item

    def remove_work(self, work_id: str) -> bool:
        works = self.works()
        remaining = [item for item in works if str(item.get("id") or "") != work_id]
        if len(remaining) == len(works):
            return False
        self._save(self.groups(), remaining)
        return True

    def update_group(self, group_id: str, *, label: str, accounts: list[AccountKey]) -> dict:
        groups = self.groups()
        group = next((item for item in groups if item.get("id") == group_id), None)
        if group is None:
            raise ValueError("找不到指定作者规则")
        keys = list(dict.fromkeys((platform.casefold(), account.casefold()) for platform, account in accounts if platform and account))
        if not keys:
            raise ValueError("作者规则至少需要一个账号")
        group.update(label=label[:120], primary={"platform": keys[0][0], "account": keys[0][1]},
                     accounts=[{"platform": platform, "account": account} for platform, account in keys])
        self._save(groups)
        return group

    def update_work(self, work_id: str, *, label: str, module_id: str, target: str) -> dict:
        works = self.works()
        item = next((entry for entry in works if entry.get("id") == work_id), None)
        if item is None:
            raise ValueError("找不到指定作品规则")
        key = work_from_target(module_id, target) if target.strip() else (str(item.get("platform") or ""), str(item.get("work") or ""))
        if key is None:
            raise ValueError("请输入作品、帖子或具体页面地址")
        item.update(platform=key[0], work=key[1], label=label[:120])
        self._save(self.groups(), works)
        return item

    def export_platform(self, platform: str, destination: Path | str) -> int:
        platform = platform.casefold()
        accounts = sorted(account for key_platform, account in self.blocked_accounts() if key_platform == platform)
        works = [item for item in self.works() if item.get("platform") == platform]
        content = json.dumps({"version": 1, "platform": platform, "accounts": accounts,
                              "works": [{"work": item["work"], "label": item.get("label", "")} for item in works]},
                             ensure_ascii=False, indent=2)
        _atomic_write_text(Path(destination), content + "\n", encoding="utf-8")
        return len(accounts) + len(works)

    def import_platform(self, platform: str, source: Path | str) -> int:
        if Path(source).suffix.casefold() == ".txt":
            return self.import_text_accounts(platform, source)
        payload = json.loads(Path(source).read_text(encoding="utf-8-sig"))
        platform = platform.casefold()
        if not isinstance(payload, dict) or payload.get("platform") != platform or not isinstance(payload.get("accounts"), list) or not isinstance(payload.get("works"), list):
            raise ValueError("平台黑名单格式或平台不匹配")
        validated: list[tuple[str, str, str]] = []
        for account in payload["accounts"]:
            if not isinstance(account, str) or not account.strip():
                raise ValueError("无效的作者账号")
            validated.append(("account", account.strip(), ""))
        for work in payload["works"]:
            if not isinstance(work, dict) or not isinstance(work.get("work"), str) or not work["work"].strip():
                raise ValueError("无效的作品规则")
            validated.append(("work", work["work"].strip(), str(work.get("label") or "")))
        prepared = [(kind, _import_work_target(platform, value) if kind == "work" else value, label)
                    for kind, value, label in validated]
        added = 0
        existing_accounts = self.blocked_accounts()
        existing_works = self.blocked_works()
        for kind, value, label in prepared:
            if kind == "account":
                if (platform, value.casefold()) not in existing_accounts:
                    self.add_group((platform, value))
                    existing_accounts.add((platform, value.casefold()))
                    added += 1
                continue
            key = work_from_target(platform, value)
            if key not in existing_works:
                added += 1
                existing_works.add(key)
            self.add_work(platform, value, label=label)
        return added

    def import_text_accounts(self, platform: str, source: Path | str) -> int:
        """Import an old Pixiv blacklist_members.txt or one account URL/handle per line."""
        platform = platform.casefold()
        if platform not in {"twitter", "pixiv", "fanbox", "sketch", "bluesky", "instagram", "jmcomic_author", "jmcomic_tag"}:
            raise ValueError("该平台没有可导入的作者账号名单")
        lines = Path(source).read_text(encoding="utf-8-sig").splitlines()
        if len(lines) > 100000:
            raise ValueError("名单超过 100000 行，请拆分导入")
        keys: set[AccountKey] = set()
        for number, raw in enumerate(lines, 1):
            value = raw.strip()
            if not value or value.startswith("#"):
                continue
            if value.startswith(("http://", "https://")):
                key = account_from_url(value)
                if key is None or key[0] != platform:
                    raise ValueError(f"第 {number} 行不是 {platform} 作者主页")
            else:
                account = value.lstrip("@").casefold()
                if platform == "pixiv" and not account.isdigit():
                    raise ValueError(f"第 {number} 行不是 Pixiv 用户 ID")
                if platform == "twitter" and (not re.fullmatch(r"[A-Za-z0-9_]{1,15}", account) or account.isdigit()):
                    raise ValueError(f"第 {number} 行不是明确的 X 用户名；纯数字归档账号 ID 需要先解析为主页 URL")
                if platform in {"fanbox", "sketch", "bluesky", "instagram"} and (not account or any(char.isspace() for char in account)):
                    raise ValueError(f"第 {number} 行作者账号无效")
                if platform == "jmcomic_author" and not account:
                    raise ValueError(f"第 {number} 行 JMComic 作者名无效")
                if platform == "jmcomic_tag" and not account:
                    raise ValueError(f"第 {number} 行 JMComic 标签无效")
                key = platform, account
            keys.add(key)
        existing = self.blocked_accounts()
        for key in sorted(keys - existing):
            self.add_group(key)
        return len(keys - existing)

    def _save(self, groups: list[dict], works: list[dict] | None = None) -> None:
        content = json.dumps({"version": 2, "groups": groups, "works": self.works() if works is None else works}, ensure_ascii=False, indent=2)
        _atomic_write_text(self.path, content + "\n", encoding="utf-8")


__all__ = ["AccountKey", "WorkKey", "BlocklistStore", "account_from_url", "account_from_target", "work_from_target"]
