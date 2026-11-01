"""Offline URL routing and persistent, independently queued platform batches."""
from __future__ import annotations

import ipaddress
import re
import threading
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .output_lock import output_path_lock
from .security import redact_url_query, safe_error_message
from software_app.crawlers.common import write_text_atomic


MAX_BATCH_ITEMS = 100
REPORT_NAME = "批次汇总.txt"
STATUS_LABELS = {
    "pending": "未入队", "queued": "排队中", "running": "运行中", "paused": "已暂停",
    "cancelling": "停止中", "completed": "已完成", "partial": "部分完成", "failed": "失败",
    "cancelled": "已取消", "enqueue_failed": "未能入队",
}
PIXIV_SCOPES = {
    "work": "作品 ID", "user": "作者 ID", "user_novels": "作者小说",
    "manga_series": "漫画系列", "novel": "小说 ID", "novel_series": "小说系列",
    "bookmark": "作品收藏", "novel_bookmark": "小说收藏", "history": "浏览历史（Premium）",
    "fanbox": "FANBOX", "sketch": "Sketch",
}


@dataclass(frozen=True)
class BatchRoute:
    line: int
    raw: str
    target: str = ""
    module_id: str = ""
    kind: str = ""
    scope: str = ""
    state: str = "ready"
    note: str = ""
    identity: str = ""


@dataclass(frozen=True)
class BatchTaskPlan:
    route: BatchRoute
    options: dict


@dataclass
class BatchResult:
    task_ids: list[str]
    failures: int
    report: Path
    warnings: list[str]


def _url(raw: str):
    if len(raw) > 8192 or any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in raw):
        raise ValueError("链接过长或包含空白、控制字符")
    parsed = urlsplit(raw)
    host = str(parsed.hostname or "").casefold().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("请提供完整的 HTTP(S) 链接；ID、用户名请使用原平台输入框")
    if parsed.username is not None or parsed.password is not None or "\\" in raw:
        raise ValueError("不接受含登录凭据或反斜杠的链接")
    port = parsed.port  # Raises for malformed/out-of-range ports.
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if "." not in host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError("请使用公网域名") from None
        if not re.fullmatch(r"[a-z0-9\u0080-\uffff.-]+", host):
            raise ValueError("域名格式无效")
    else:
        if not address.is_global:
            raise ValueError("不接受本地或保留 IP 地址")
    netloc = f"[{host}]" if ":" in host else host
    if port and port != (443 if parsed.scheme == "https" else 80):
        netloc += f":{port}"
    return parsed._replace(netloc=netloc, fragment=""), host, port


def _route(line: int, raw: str, jm_domain: str) -> BatchRoute:
    parsed, host, port = _url(raw)
    target = urlunsplit(parsed)
    path = parsed.path.rstrip("/")
    module, kind, scope, key, note = "", "", "", "", ""
    twitter_hosts = {f"{prefix}{domain}" for prefix in ("", "www.", "mobile.", "m.") for domain in ("x.com", "twitter.com")}
    jm_host = str(urlsplit(jm_domain if "://" in jm_domain else "https://" + jm_domain).hostname or "").lower()
    if host in twitter_hosts:
        module = "twitter"
        post = re.fullmatch(r"/(?:[A-Za-z0-9_]{1,15}|i)/status/(\d+)(?:/(?:photo|video)/\d+)?", path)
        profile = re.fullmatch(r"/([A-Za-z0-9_]{1,15})(?:/(?:media|with_replies))?", path)
        reserved = {"home", "explore", "search", "settings", "notifications", "messages", "i", "intent", "login", "logout", "signup", "share", "compose", "tos", "privacy"}
        if post:
            kind, scope, key = "post", "帖子 / 媒体 URL", post.group(1)
            target = f"https://x.com/{path.split('/')[1].lower()}/status/{key}"
        elif profile and profile.group(1).lower() not in reserved:
            kind, scope, key = "user", "用户 / @用户名", profile.group(1).lower()
            target = f"https://x.com/{key}"
        else:
            raise ValueError("不支持的 Twitter/X 路径，请使用账号或帖子链接")
    elif host == "pixiv.net" or host.endswith(".pixiv.net") or host == "fanbox.cc" or host.endswith(".fanbox.cc"):
        from software_app.crawlers.pixiv.client import parse_pixiv_target

        module = "pixiv"
        if host == "sketch.pixiv.net":
            if not re.fullmatch(r"/(?:items/\d+|@[A-Za-z0-9_.-]+)", path):
                raise ValueError("Sketch 请使用 /items/数字ID 或 /@用户名 链接")
        elif host == "fanbox.cc" or host.endswith(".fanbox.cc"):
            if not re.fullmatch(r"/(?:@[A-Za-z0-9_.-]+(?:/posts/\d+)?|posts/\d+)", path):
                raise ValueError("FANBOX 请使用 /@创作者 或 /posts/数字ID 链接")
        elif not re.fullmatch(r"(?:/[a-z]{2})?/(?:artworks/\d+|i/\d+|users/\d+(?:/bookmarks/(?:artworks|novels))?|(?:novel/)?series/\d+|novel/show\.php|history\.php)", path):
            raise ValueError("不支持的 Pixiv 路径")
        pixiv = parse_pixiv_target(target)
        kind, scope, key = pixiv.kind, PIXIV_SCOPES[pixiv.kind], pixiv.value
        if kind in {"work", "user", "manga_series", "novel", "novel_series"}:
            suffix = {"work": f"artworks/{key}", "user": f"users/{key}", "manga_series": f"series/{key}",
                      "novel": f"novel/show.php?id={key}", "novel_series": f"novel/series/{key}"}[kind]
            target = "https://www.pixiv.net/" + suffix
        elif kind in {"fanbox", "sketch"}:
            post = re.search(r"/(?:posts|items)/(\d+)", path)
            key = "post:" + post.group(1) if post else "creator:" + path.rsplit("/@", 1)[-1].lower()
        elif kind in {"history", "bookmark", "novel_bookmark"}:
            # Queries may contain collection filters; preserve them in the identity.
            key = target
    elif host in {"18comic.vip", "www.18comic.vip", jm_host}:
        module = "jmcomic"
        match = re.fullmatch(r"/(album|albums|photo|photos)/(\d+)(?:/[^/]+)?", path)
        if not match:
            raise ValueError("JM 混合批次暂支持漫画、章节链接；小说仍未接入下载")
        kind = "photo" if match.group(1).startswith("photo") else "album"
        scope = "章节 ID / 链接" if kind == "photo" else "漫画 ID / 链接"
        key = match.group(2)
        target = urlunsplit(parsed._replace(path=f"/{kind}/{key}", query=""))
    elif host in {"bsky.app", "www.bsky.app"}:
        from software_app.crawlers.bluesky import parse_bluesky_target

        module = "bluesky"
        if not re.fullmatch(r"/profile/[^/]+(?:/post/[^/]+)?", path):
            raise ValueError("Bluesky 请使用账号或帖子链接")
        item = parse_bluesky_target(target)
        kind, target = item.kind, item.url
        scope = "帖子 / 媒体" if kind == "post" else "关注账号"
        key = target
    elif host in {"instagram.com", "www.instagram.com"}:
        from software_app.crawlers.instagram import parse_instagram_target

        module = "instagram"
        if not re.fullmatch(r"/(?:[A-Za-z0-9_.]+|(?:p|reel|tv)/[A-Za-z0-9_-]+)", path):
            raise ValueError("Instagram 请使用账号、帖子或 Reels 链接")
        item = parse_instagram_target(target)
        kind = item.kind
        scope = "账号 / 用户名" if kind == "profile" else "Reels / 链接" if kind == "reel" else "帖子 / 链接"
        # InstagramTarget.url uses /post/ for post, while Instagram accepts /p/.
        target = f"https://www.instagram.com/{item.username.lower()}/" if kind == "profile" else f"https://www.instagram.com/{'p' if kind == 'post' else kind}/{item.shortcode}/"
        key = item.username.lower() if kind == "profile" else item.shortcode
        kind_key = "profile" if kind == "profile" else "media"
        return _dedicated(BatchRoute(line, raw, target, module, kind, scope, identity=f"{module}:{kind_key}:{key}"), parsed, port)
    elif host in {"e-hentai.org", "www.e-hentai.org", "exhentai.org", "www.exhentai.org"}:
        from software_app.crawlers.ehentai.client import parse_ehentai_target

        module = "ehentai"
        item = parse_ehentai_target(target)
        kind = item.kind
        prefix = "里站" if item.site == "exhentai" else "表站"
        scope = "画廊链接 / ID" if kind == "gallery" else prefix + ("收藏夹（需登录）" if kind == "favorites" else "关键词（需登录）" if prefix == "里站" else "关键词")
        # Keep query flags, especially selected favorite category/search filters.
        key = f"{item.site}:{item.gid}:{item.token}" if kind == "gallery" else target
        if kind == "gallery":
            target = item.url
    elif host in {"hentaipaw.com", "zh.hentaipaw.com"}:
        from software_app.crawlers.hentaipaw import normalize_article_url

        target = normalize_article_url(target)
        module, kind, scope, key = "hentaipaw", "article", "作品链接 / ID", target.rsplit("/", 1)[-1]
    else:
        module, kind, scope, key = "website", "page", "网页", target
        note = "未匹配专用平台，将按普通网页提取媒体"
    return _dedicated(BatchRoute(line, raw, target, module, kind, scope, note=note, identity=f"{module}:{kind}:{key}"), parsed, port)


def _dedicated(route, parsed, port):
    if route.module_id != "website" and port not in {None, 443 if parsed.scheme == "https" else 80}:
        raise ValueError("平台专用链接不支持自定义端口")
    return route


def route_batch(text: str, *, enabled_modules=None, jm_domain: str = "https://18comic.vip") -> list[BatchRoute]:
    if len(text) > 200_000:
        raise ValueError("批次输入过长（最多 200000 字符）")
    lines = [(index, raw.strip()) for index, raw in enumerate(text.splitlines(), 1) if raw.strip()]
    if not lines or len(lines) > MAX_BATCH_ITEMS:
        raise ValueError(f"每行一个完整链接，一次支持 1–{MAX_BATCH_ITEMS} 行")
    seen, routes = set(), []
    for line, raw in lines:
        try:
            route = _route(line, raw, jm_domain)
            if enabled_modules is not None and route.module_id not in enabled_modules:
                route = replace(route, state="error", note="对应平台模块未启用")
            elif route.identity in seen:
                route = replace(route, state="duplicate", note="与本批次前面的目标重复")
            else:
                seen.add(route.identity)
        except (ValueError, KeyError) as exc:
            route = BatchRoute(line, raw, state="error", note=safe_error_message(exc))
        routes.append(route)
    return routes


def write_batch_report(storage, batch_id: str) -> Path:
    batch, _ = storage.batch_snapshot(batch_id)
    path = Path(batch["output_dir"]) / REPORT_NAME
    # Read inside the file lock: a slower writer must not replace a newer snapshot.
    with output_path_lock(path):
        batch, items = storage.batch_snapshot(batch_id)
        counts = Counter(item["status"] for item in items)
        lines = ["欲求达 · 混合平台批次", f"批次：{batch_id}", f"创建：{batch['created_at']}",
                 f"保存目录：{batch['output_dir']}", f"条目数：{len(items)}",
                 "状态：" + "；".join(f"{STATUS_LABELS.get(key, key)} {value}" for key, value in sorted(counts.items())),
                 "说明：状态随任务变化更新；未入队条目不会在重启后自动执行。重试生成独立任务。", ""]
        for item in items:
            lines.extend([f"{item['position'] + 1}. [{item['module_id']}] {STATUS_LABELS.get(item['status'], item['status'])}",
                          f"链接：{redact_url_query(item['target'])}", f"任务：{item['task_id'] or '—'}",
                          f"条目目录：{Path(batch['output_dir']) / item_directory(item['position'], item['module_id'])}"])
            if item["error"]:
                lines.append("原因：" + safe_error_message(item["error"]))
            lines.append("")
        write_text_atomic(path, "\n".join(lines), encoding="utf-8-sig")
    return path


def item_directory(position: int, module_id: str) -> str:
    if not re.fullmatch(r"[a-z0-9_]+", module_id):
        raise ValueError("无效的平台 ID")
    return f"{position + 1:03d}_{module_id}"


def start_batch(manager, plans: list[BatchTaskPlan], output_dir: Path, batch_id: str,
                callbacks=None, stop_enqueuing: threading.Event | None = None) -> BatchResult:
    if not re.fullmatch(r"[a-f0-9]{32}", batch_id) or not 1 <= len(plans) <= MAX_BATCH_ITEMS:
        raise ValueError("批次 ID 或条目数量无效")
    if any(plan.route.state != "ready" for plan in plans):
        raise ValueError("批次包含不可入队条目，请重新预览")
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    manager.storage.create_batch(batch_id, str(output_dir), [(i, plan.route.module_id, plan.route.target) for i, plan in enumerate(plans)])
    try:
        report = write_batch_report(manager.storage, batch_id)
    except Exception as exc:
        for index in range(len(plans)):
            manager.storage.fail_batch_item(batch_id, index, safe_error_message(exc))
        raise
    result = BatchResult([], 0, report, [])
    for index, plan in enumerate(plans):
        try:
            if stop_enqueuing is not None and stop_enqueuing.is_set():
                raise ValueError("用户停止后续入队；已创建的任务继续由任务队列管理")
            options = {**plan.options, "_batch_id": batch_id, "_batch_position": index}
            item_output = output_dir / item_directory(index, plan.route.module_id)
            item_output.mkdir(exist_ok=False)
            task = manager.start_task(plan.route.module_id, plan.route.target, item_output, options, callbacks)
            result.task_ids.append(task.task_id)
        except Exception as exc:
            manager.storage.fail_batch_item(batch_id, index, safe_error_message(exc))
            result.failures += 1
    try:
        write_batch_report(manager.storage, batch_id)
    except Exception as exc:
        result.warnings.append("汇总更新失败：" + safe_error_message(exc))
    return result
