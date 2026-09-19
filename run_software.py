from __future__ import annotations

import argparse
import sys
from pathlib import Path

from software_app.app import create_app_context
from software_app.core.events import CallbackSet
from software_app.core.models import FileRecord, ProgressEvent
from software_app.core.settings import DEFAULT_OUTPUT_DIR

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class ConsoleCallbacks:
    def on_progress(self, event: ProgressEvent) -> None:
        print(f"[{event.module_id}][{event.level}] {event.message}")

    def on_file(self, record: FileRecord) -> None:
        print(f"[file] {record.media_type}: {record.path}")

    def on_done(self, task_id: str, status: str) -> None:
        print(f"[done] {task_id}: {status}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="欲求达跨平台内容工作台命令行入口")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("modules", help="列出已注册模块")
    subparsers.add_parser("plugins", help="列出内置和外部平台插件加载状态")

    driver_parser = subparsers.add_parser("browser-driver", help="检测或下载全局 ChromeDriver")
    driver_parser.add_argument("--download", action="store_true", help="缺少或版本不匹配时自动下载 ChromeDriver")

    aria2_parser = subparsers.add_parser("aria2", help="检测或安装 aria2 BT 引擎")
    aria2_parser.add_argument("--install", action="store_true", help="从本地官方包安装，缺少时联网下载")

    following_parser = subparsers.add_parser("twitter-following", help="查看或刷新推特关注作者列表")
    following_parser.add_argument("--refresh", action="store_true", help="打开浏览器刷新关注列表")
    following_parser.add_argument("--account", default="", help="自己的账号 handle；刷新时不填则自动识别")
    following_parser.add_argument("--max", type=int, default=0, help="刷新时最多采集多少个，0 表示不限")
    following_parser.add_argument("--mode", choices=("new", "all"), default="new", help="更新新的或更新全部")
    following_parser.add_argument("--limit", type=int, default=50, help="显示多少个本地关注作者")
    following_parser.add_argument(
        "--timeout", type=int, default=0,
        help="异常兜底时限秒数；0 表示不限时并等待懒加载列表收敛",
    )

    profile_parser = subparsers.add_parser("twitter-profile", help="采集推特主页预览和 Skeb 外链")
    profile_parser.add_argument("target", help="用户名、@用户名或主页 URL")
    profile_parser.add_argument("--timeout", type=int, default=60, help="主页加载最长等待秒数")
    profile_parser.add_argument("--local", action="store_true", help="只读取本地缓存/关注/历史，不打开浏览器")
    history_parser = subparsers.add_parser("twitter-history", help="列出推特下载过的作者/文件夹")
    history_parser.add_argument("--limit", type=int, default=50, help="最多显示多少条")

    subparsers.add_parser("twitter-clear-records", help="清空推特下载记录和软件历史索引")
    preview_parser = subparsers.add_parser("preview", help="预览目标")
    preview_parser.add_argument("module")
    preview_parser.add_argument("target")
    preview_parser.add_argument("-t", "--types", default="1234")
    preview_parser.add_argument("--live", action="store_true", help="访问平台获取在线预览和静态 HTML 快照")
    preview_parser.add_argument("--input-kind", default="", help="显式指定目标类型，例如 work/user/search/album/photo")

    search_parser = subparsers.add_parser("search", help="搜索平台目标或页面线索")
    search_parser.add_argument("module")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--headless", action="store_true")

    download_parser = subparsers.add_parser("download", help="启动下载任务")
    download_parser.add_argument("module")
    download_parser.add_argument("target")
    download_parser.add_argument("-t", "--types", default="1234")
    download_parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))

    library_parser = subparsers.add_parser("library", help="列出下载库文件")
    library_parser.add_argument("--module")
    library_parser.add_argument("--limit", type=int, default=30)

    return parser


def print_modules() -> int:
    context = create_app_context()
    for adapter in context.task_manager.list_adapters():
        warnings = adapter.validate()
        warning_text = f" | {len(warnings)} warning(s)" if warnings else ""
        print(f"{adapter.module_id:14} {adapter.display_name:16} {adapter.info.stage}{warning_text}")
    return 0


def print_browser_driver_status(download: bool) -> int:
    from software_app.core.browser_driver import BrowserDriverError, chromedriver_status

    try:
        status = chromedriver_status(auto_download=download)
    except BrowserDriverError as exc:
        print(f"browser_driver_error: {exc}")
        return 1
    print(f"chrome_version: {status.chrome_version or '-'}")
    print(f"driver_version: {status.driver_version or '-'}")
    print(f"driver_path: {status.driver_path or '-'}")
    print(f"source: {status.source}")
    print(f"message: {status.message}")
    return 0


def print_twitter_following(
    refresh: bool, account: str, max_accounts: int, limit: int, timeout: int, update_mode: str
) -> int:
    context = create_app_context()
    adapter = context.task_manager.get_adapter("twitter")
    if refresh:
        try:
            payload = adapter.fetch_following(
                account=account,
                max_accounts=max_accounts,
                timeout_seconds=timeout,
                update_mode=update_mode,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"刷新失败，继续读取本地/部分结果: {exc}")
        else:
            log = str(payload.get("log", "")).strip()
            if log:
                print(log)
            reason = str(payload.get("stopped_reason") or "")
            reason_text = {
                "cancelled": "刷新已停止",
                "time_limit": "已到刷新时间上限",
                "browser_closed": "浏览器已关闭",
                "empty": "X 显示当前没有关注账号",
                "no_content": "关注页暂未加载出账号",
            }.get(reason, "")
            if reason_text:
                suffix = "，继续使用上次列表" if payload.get("kept_previous") else ""
                print(reason_text + suffix)
            if not reason:
                mode_text = "更新新的" if payload.get("update_mode") == "new" else "更新全部"
                summary = f"{mode_text}完成：新增 {int(payload.get('new_count') or 0)} 个"
                if payload.get("update_mode") == "all":
                    summary += f"，移除已取关 {int(payload.get('removed_count') or 0)} 个"
                print(summary)
    items = adapter.load_following_accounts()
    if not items:
        print("还没有本地关注列表。使用 --refresh 采集。")
        return 0
    for item in items[: max(1, limit)]:
        handle = item.get("handle", "")
        display_name = item.get("display_name", "")
        bio = str(item.get("bio", "")).replace("\n", " / ")
        print(f"@{handle:15} {display_name} {bio}".rstrip())
    print(f"total: {len(items)}")
    return 0



def print_twitter_profile(target: str, timeout: int, local: bool = False) -> int:
    context = create_app_context()
    adapter = context.task_manager.get_adapter("twitter")
    if local:
        payload = adapter.load_profile_preview(target, DEFAULT_OUTPUT_DIR)
    else:
        payload = adapter.fetch_profile_preview(target, timeout_seconds=timeout)
        log = str(payload.get("log", "")).strip()
        if log:
            print(log)

    print(f"source: {payload.get('source', 'cached_profile' if not local else 'local')}")
    print(f"handle: @{payload.get('handle', '')}")
    print(f"name: {payload.get('display_name', '') or '-'}")
    print(f"profile: {payload.get('profile_url', '')}")
    bio = str(payload.get("bio", "")).replace("\n", " / ")
    print(f"bio: {bio or '-'}")

    skeb_links = payload.get("skeb_links", []) or []
    print("skeb:")
    if skeb_links:
        for item in skeb_links:
            if isinstance(item, dict):
                print(f"- {item.get('label') or item.get('url')}: {item.get('url')}")
            else:
                print(f"- {item}")
    else:
        print("- 未发现")

    other_links = [item for item in (payload.get("links", []) or []) if item not in skeb_links]
    if other_links:
        print("links:")
        for item in other_links:
            if isinstance(item, dict):
                print(f"- {item.get('label') or item.get('url')}: {item.get('url')}")
            else:
                print(f"- {item}")
    return 0


def print_aria2_status(install: bool) -> int:
    from software_app.core.aria2_bt import Aria2Error, aria2_status, install_aria2

    try:
        status = install_aria2() if install else aria2_status()
    except Aria2Error as exc:
        print(f"aria2_error: {exc}")
        return 1
    print(f"aria2_path: {status.executable or '-'}")
    print(f"aria2_version: {status.version or '-'}")
    print(f"source: {status.source}")
    print(f"message: {status.message}")
    return 0 if status.executable else 1


def print_plugins() -> int:
    context = create_app_context()
    for report in context.plugin_reports:
        message = f" | {report.message}" if report.message else ""
        print(f"{report.plugin_id:18} {report.status:9} {report.name} | {report.source}{message}")
    return 1 if any(report.status == "error" for report in context.plugin_reports) else 0

def print_twitter_history(limit: int) -> int:
    context = create_app_context()
    adapter = context.task_manager.get_adapter("twitter")
    rows = adapter.downloaded_users(DEFAULT_OUTPUT_DIR)
    if not rows:
        print("还没有推特下载历史。")
        return 0
    for row in rows[: max(1, limit)]:
        handle = str(row.get("handle") or "").strip()
        name = row.get("name") or handle or row.get("target") or "-"
        count = row.get("count") or row.get("local_files") or 0
        latest = row.get("latest") or "-"
        print(f"{name}\t@{handle or '-'}\t{count}\t{latest}\t{row.get('source', '-')}")
    print(f"total: {len(rows)}")
    return 0


def clear_twitter_records() -> int:
    context = create_app_context()
    adapter = context.task_manager.get_adapter("twitter")
    record_counts = adapter.clear_download_records()
    db_counts = context.storage.clear_history(module_id="twitter")
    print(f"json: {record_counts}")
    print(f"db: {db_counts}")
    return 0

def preview_target(module_id: str, target: str, types: str, live: bool = False, input_kind: str = "") -> int:
    context = create_app_context()
    options = {
        "types": types,
        "live": live,
        "input_kind": input_kind,
        "proxy_url": str(context.storage.get_setting("proxy_url", "") or ""),
        "retries": int(context.storage.get_setting("task_retries", 1) or 1),
        "page_read_timeout": int(context.storage.get_setting("webpage_read_timeout", 60) or 60),
    }
    preview = context.task_manager.preview_target(module_id, target, options)
    print(f"module: {preview.module_id}")
    print(f"status: {preview.status}")
    print(f"target: {preview.normalized_target}")
    print(f"description: {preview.description}")
    if preview.warnings:
        print("warnings:")
        for warning in preview.warnings:
            print(f"- {warning}")
    metadata = preview.metadata
    for key in ("input_kind", "target_id", "author_id", "author_name", "published_at"):
        if metadata.get(key):
            print(f"{key}: {metadata[key]}")
    if metadata.get("search_results"):
        print(f"results: {len(metadata['search_results'])}")
    return 0


def search_targets(module_id: str, query: str, limit: int, headless: bool = False) -> int:
    context = create_app_context()
    adapter = context.task_manager.get_adapter(module_id)
    try:
        rows = adapter.search_targets(query, limit=limit, options={"headless": headless})
    except Exception as exc:  # noqa: BLE001
        print(f"搜索失败: {exc}", file=sys.stderr)
        return 2
    for index, row in enumerate(rows, 1):
        print(f"{index:3}  {row.get('title') or row.get('name') or row.get('id') or '-'}\t{row.get('url') or row.get('profile_url') or ''}")
    print(f"total: {len(rows)}")
    return 0


def download_target(module_id: str, target: str, types: str, output_dir: str) -> int:
    context = create_app_context()
    callbacks = ConsoleCallbacks()
    task = context.task_manager.start_task(
        module_id,
        target,
        Path(output_dir),
        {"types": types},
        CallbackSet(
            on_progress=callbacks.on_progress,
            on_file=callbacks.on_file,
            on_done=callbacks.on_done,
        ),
    )
    print(f"task: {task.task_id}")
    context.task_manager.wait(task.task_id)
    return 0


def print_library(module_id: str | None, limit: int) -> int:
    context = create_app_context()
    for row in context.storage.list_files(module_id=module_id, limit=limit):
        print(f"{row['module_id']:14} {row['media_type']:7} {row['size']:10} {row['path']}")
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        from software_app.ui.tk_app import main as run_desktop

        run_desktop()
        return 0
    if args.command == "modules":
        return print_modules()
    if args.command == "plugins":
        return print_plugins()
    if args.command == "browser-driver":
        return print_browser_driver_status(args.download)
    if args.command == "aria2":
        return print_aria2_status(args.install)
    if args.command == "twitter-following":
        return print_twitter_following(args.refresh, args.account, args.max, args.limit, args.timeout, args.mode)
    if args.command == "twitter-profile":
        return print_twitter_profile(args.target, args.timeout, args.local)
    if args.command == "twitter-history":
        return print_twitter_history(args.limit)
    if args.command == "twitter-clear-records":
        return clear_twitter_records()
    if args.command == "preview":
        return preview_target(args.module, args.target, args.types, args.live, args.input_kind)
    if args.command == "search":
        return search_targets(args.module, args.query, args.limit, args.headless)
    if args.command == "download":
        return download_target(args.module, args.target, args.types, args.output_dir)
    if args.command == "library":
        return print_library(args.module, args.limit)
    parser.print_help()
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
