from __future__ import annotations

import html
import io
import queue
import re
import threading
import time
import tkinter as tk
import urllib.request
import webbrowser
import zipfile
from collections import Counter
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlparse

from software_app.app import create_app_context
from software_app.core.blocklist import AccountKey, account_from_url, work_from_target
from software_app.core.blocklist_sources import (
    fetch_bluesky_moderation,
    fetch_jmcomic_tag_blocks,
    fetch_pixiv_mutes,
    read_pixiv_settings_html,
    read_x_archive,
)
from software_app.core.events import CallbackSet
from software_app.core.models import FileRecord, ProgressEvent, TargetPreview
from software_app.core.settings import DEFAULT_OUTPUT_DIR
from software_app.crawlers.common import normalize_output_format
from software_app.ui.desktop_support import (
    bounded_int as _bounded_int,
    collect_image_files as _collect_image_files,
)
from software_app.ui.following_tab import FollowingTabMixin
from software_app.ui.google_search_tab import GOOGLE_BATCH_RESULT_LIMIT, GoogleSearchTabMixin, google_search_failure_hint
from software_app.ui.library_tab import LibraryTabMixin
from software_app.ui.platform_config import (
    JM_SCOPE_HINTS,
    PIXIV_SCOPE_HINTS,
    PLATFORM_CONTENT_SCOPES,
    PLATFORM_SCOPE_HINTS,
    TASK_STATUS_LABELS,
    TYPE_OPTIONS,
)
from software_app.ui.preview_renderer import TargetPreviewRenderer
from software_app.ui.scrolling import bind_canvas_mousewheel
from software_app.ui.settings_tab import (
    ANIMATION_FORMAT_LABELS,
    ARCHIVE_MODE_LABELS,
    AUDIO_FORMAT_LABELS,
    IMAGE_FORMAT_LABELS,
    JM_CATEGORY_LABELS,
    JM_MATCH_LABELS,
    JM_ORDER_LABELS,
    JM_POSTPROCESS_LABELS,
    JM_TIME_LABELS,
    PIXIV_AGE_LABELS,
    PIXIV_AUTHOR_MATCH_LABELS,
    PIXIV_VISIBILITY_LABELS,
    VIDEO_FORMAT_LABELS,
    SettingsTabMixin,
)
from software_app.ui.task_queue_tab import TaskQueueTabMixin
from software_app.ui.theme import apply_desktop_theme

try:
    from PIL import Image, ImageTk
except Exception:  # Pillow is optional for the shell; file opening still works.
    Image = None
    ImageTk = None


DEFAULT_TYPES_VALUE = "1,2,3,4"


def _format_label(labels: dict[str, str], value: object, key: str) -> str:
    normalized = normalize_output_format(key, value)
    return next((label for label, stored in labels.items() if stored == normalized), next(iter(labels)))


def split_task_targets(
    value: str,
    *,
    split_slashes: bool = True,
    module_id: str = "",
) -> list[str]:
    """Split target lists conservatively; slash is ambiguous in URLs and IDs."""
    targets: list[str] = []
    seen: set[str] = set()
    chunks = re.split(r"[、\\\r\n]+", str(value or ""))
    for chunk in chunks:
        chunk = chunk.strip().strip("\"'")
        if not chunk:
            continue
        if re.search(r"https?://", chunk, re.IGNORECASE):
            pieces = re.split(r"/+(?=https?://)", chunk, flags=re.IGNORECASE)
        elif re.match(r"(?i)^(?:www\.)?[a-z0-9.-]+\.[a-z]{2,}(?:/|$)", chunk):
            pieces = [chunk]
        elif module_id == "ehentai" and split_slashes:
            parts = chunk.split("/")
            is_gallery_id = len(parts) >= 2 and len(parts) % 2 == 0 and all(
                parts[index].isdigit() and re.fullmatch(r"[0-9a-fA-F]{10}", parts[index + 1])
                for index in range(0, len(parts), 2)
            )
            pieces = ["/".join(parts[index:index + 2]) for index in range(0, len(parts), 2)] if is_gallery_id else [chunk]
        elif split_slashes:
            pieces = chunk.split("/")
        else:
            pieces = [chunk]
        for piece in pieces:
            target = piece.strip().strip("\"'")
            if target and target.casefold() not in seen:
                seen.add(target.casefold())
                targets.append(target)
    return targets


def slash_separates_targets(module_id: str, scope: str) -> bool:
    """Only split slashes for scopes that accept concrete targets, never free-text queries."""
    normalized_scope = str(scope or "").strip()
    target_scopes = {
        "twitter": {"关注账号", "用户 / @用户名", "帖子 / 媒体 URL"},
        "pixiv": {
            "作品 ID", "作者 ID", "关注画师", "漫画系列", "小说 ID", "作者小说",
            "小说系列", "FANBOX", "Sketch",
        },
        "jmcomic": {"漫画 ID / 链接", "章节 ID / 链接"},
        "bluesky": {"关注账号", "帖子 / 媒体"},
        "instagram": {"账号 / 用户名", "帖子 / 链接", "Reels / 链接"},
        "ehentai": {"画廊链接 / ID"},
        "google_image": {"候选页面"},
        "website": {"网页"},
    }
    return normalized_scope in target_scopes.get(module_id, set())


class SoftwareDesktop(
    FollowingTabMixin,
    GoogleSearchTabMixin,
    LibraryTabMixin,
    SettingsTabMixin,
    TaskQueueTabMixin,
    tk.Tk,
):
    def __init__(self) -> None:
        super().__init__()
        self.title("欲求达")
        self.geometry("1240x780")
        self.minsize(1080, 680)
        self.configure(background="#f3f5f8")

        self.context = create_app_context()
        self.storage = self.context.storage
        self.manager = self.context.task_manager
        self.adapters = self.manager.list_adapters()
        self.ui_queue: queue.Queue[tuple[str, object]] = queue.Queue()

        saved_output_dir = self.storage.get_setting("default_output_dir", str(DEFAULT_OUTPUT_DIR))
        saved_proxy_url = self.storage.get_setting("proxy_url", "")
        saved_retries = _bounded_int(self.storage.get_setting("task_retries", 1), 1, 0, 5)
        saved_google_limit = _bounded_int(self.storage.get_setting("google_candidate_limit", 30), 30, 1, 200)
        saved_google_max_files = _bounded_int(self.storage.get_setting("webpage_max_files", 50), 50, 1, 200)
        saved_google_wait = _bounded_int(self.storage.get_setting("google_manual_wait", 60), 60, 0, 300)
        saved_webpage_timeout = _bounded_int(self.storage.get_setting("webpage_read_timeout", 60), 60, 30, 600)
        saved_pixiv_max_works = _bounded_int(self.storage.get_setting("pixiv_max_works", 20), 20, 1, 100)
        saved_pixiv_filter_ai = bool(self.storage.get_setting("pixiv_filter_ai", True))
        saved_pixiv_visibility = str(self.storage.get_setting("pixiv_visibility", "show") or "show")
        saved_pixiv_start_date = str(self.storage.get_setting("pixiv_start_date", "") or "")
        saved_pixiv_end_date = str(self.storage.get_setting("pixiv_end_date", "") or "")
        saved_pixiv_minimum_bookmarks = _bounded_int(
            self.storage.get_setting("pixiv_minimum_bookmarks", 0), 0, 0, 100000000
        )
        saved_pixiv_age_mode = str(self.storage.get_setting("pixiv_age_mode", "all") or "all")
        saved_pixiv_bookmark_tag = str(self.storage.get_setting("pixiv_bookmark_tag", "") or "")
        saved_pixiv_author_match = str(self.storage.get_setting("pixiv_author_match_mode", "partial") or "partial")
        saved_pixiv_history_limit = _bounded_int(self.storage.get_setting("pixiv_history_limit", 100), 100, 1, 10000)
        saved_jm_domain = str(self.storage.get_setting("jmcomic_domain", "https://18comic.vip") or "https://18comic.vip")
        saved_jm_user_agent = str(self.storage.get_setting("jmcomic_user_agent", "") or "")
        saved_jm_order = str(self.storage.get_setting("jmcomic_order_by", "mr") or "mr")
        saved_jm_time = str(self.storage.get_setting("jmcomic_time_range", "a") or "a")
        saved_jm_category = str(self.storage.get_setting("jmcomic_category", "0") or "0")
        saved_jm_match = str(self.storage.get_setting("jmcomic_match_mode", "fuzzy") or "fuzzy")
        saved_jm_postprocess = str(self.storage.get_setting("jmcomic_postprocess", "none") or "none")
        saved_jm_download_cover = bool(self.storage.get_setting("jmcomic_download_cover", True))
        saved_image_format = self.storage.get_setting("image_output_format", "png")
        saved_video_format = self.storage.get_setting("video_output_format", "mp4")
        saved_animation_format = self.storage.get_setting("animation_output_format", "gif")
        saved_audio_format = self.storage.get_setting("audio_output_format", "mp3")
        saved_archive_mode = str(self.storage.get_setting("archive_mode", "none") or "none")
        saved_extract_archives = bool(self.storage.get_setting("extract_archives", False))
        saved_archive_cleanup_sources = bool(self.storage.get_setting("archive_cleanup_sources", False))
        saved_eh_bt_download_enabled = bool(self.storage.get_setting("eh_bt_download_enabled", False))
        saved_eh_download_torrent = bool(self.storage.get_setting("eh_download_torrent", False)) or saved_eh_bt_download_enabled

        self.current_task_id: str | None = None
        self.current_task_ids: set[str] = set()
        self.pending_task_deletions: set[str] = set()
        self._last_task_status_refresh = 0.0
        self._closing = False
        self.task_rows: dict[str, dict] = {}
        self.file_rows = []
        self.folder_rows = []
        self.folder_file_rows = []
        self.following_rows = []
        self.pixiv_following_rows = []
        self.twitter_history_rows = []
        self.platform_candidate_rows: dict[str, list[dict]] = {}
        self.platform_candidate_scope: dict[str, str] = {}
        self.current_candidate_rows: list[dict] = []
        self.platform_history_rows: list[dict] = []
        self.target_browser_rows = []
        self.batch_info_request_id = 0
        self.batch_info_cancel_event: threading.Event | None = None
        self.google_result_rows: list[dict] = []
        self.google_image_paths: list[Path] = []
        self.google_search_cancel_event: threading.Event | None = None
        self.google_link_check_cancel_event: threading.Event | None = None
        self.cookie_capture_cancel_event: threading.Event | None = None
        self.cookie_capture_running = False
        self.google_search_progress_var = tk.DoubleVar(value=0.0)
        self.following_progress_var = tk.DoubleVar(value=0.0)
        self.following_progress_status_var = tk.StringVar(value="关注读取状态")
        self.google_preview_image = None
        self._google_preview_path: Path | None = None
        self._google_preview_resize_job = None
        self.preview_image = None
        self._preview_path: Path | None = None
        self._preview_fallback: dict = {}
        self._preview_resize_job = None
        self._library_preview_job = None
        self._library_preview_generation = 0
        self._library_preview_request_key: tuple[str, int, int, int, int] | None = None
        self._library_preview_loading = False
        self.library_all_folder_rows: list[dict] = []
        self._library_worker_running = False
        self._library_pending_refresh = False
        self._library_pending_scan = False
        self._library_refresh_job = None
        self._library_filter_job = None
        self._library_index_count = 0
        self._library_index_truncated = False
        self._library_scan_feedback = ""
        self._poll_job = None
        self.profile_avatar_image = None
        self.profile_avatar_request_id = 0
        self.generic_avatar_request_id = 0
        self.generic_avatar_source = ""
        self.generic_preview_request_id = 0
        self.module_var = tk.StringVar(value=self.adapters[0].module_id if self.adapters else "")
        self.module_labels = {}
        self.module_label_var = tk.StringVar()
        self.target_var = tk.StringVar()
        self._prefilled_task_targets: tuple[str, str, list[str]] | None = None
        self.output_dir_var = tk.StringVar(value=str(saved_output_dir or DEFAULT_OUTPUT_DIR))
        self.proxy_var = tk.StringVar(value=str(saved_proxy_url or ""))
        self.retries_var = tk.IntVar(value=saved_retries)
        self.types_var = tk.StringVar(value=DEFAULT_TYPES_VALUE)
        self.platform_concurrency_vars: dict[str, dict[str, tk.IntVar]] = {}
        for adapter in self.adapters:
            module_id = adapter.module_id
            total = _bounded_int(
                self.storage.get_setting(f"task_concurrency_{module_id}", adapter.max_concurrency),
                adapter.max_concurrency,
                1,
                20,
            )
            single = _bounded_int(
                self.storage.get_setting(f"task_concurrency_single_{module_id}", total),
                total,
                1,
                20,
            )
            collection = _bounded_int(
                self.storage.get_setting(f"task_concurrency_collection_{module_id}", 1),
                1,
                1,
                20,
            )
            self.platform_concurrency_vars[module_id] = {
                "total": tk.IntVar(value=total),
                "single": tk.IntVar(value=single),
                "collection": tk.IntVar(value=collection),
            }
        self.google_image_var = tk.StringVar()
        self.google_limit_var = tk.IntVar(value=saved_google_limit)
        self.google_max_files_var = tk.IntVar(value=saved_google_max_files)
        self.google_wait_var = tk.IntVar(value=saved_google_wait)
        self.webpage_timeout_var = tk.IntVar(value=saved_webpage_timeout)
        self.pixiv_max_works_var = tk.IntVar(value=saved_pixiv_max_works)
        self.pixiv_filter_ai_var = tk.BooleanVar(value=saved_pixiv_filter_ai)
        self.pixiv_visibility_var = tk.StringVar(
            value=next(
                (label for label, value in PIXIV_VISIBILITY_LABELS.items() if value == saved_pixiv_visibility),
                "公开",
            )
        )
        self.pixiv_start_date_var = tk.StringVar(value=saved_pixiv_start_date)
        self.pixiv_end_date_var = tk.StringVar(value=saved_pixiv_end_date)
        self.pixiv_minimum_bookmarks_var = tk.IntVar(value=saved_pixiv_minimum_bookmarks)
        self.pixiv_age_mode_var = tk.StringVar(
            value=next(
                (label for label, value in PIXIV_AGE_LABELS.items() if value == saved_pixiv_age_mode),
                "全部",
            )
        )
        self.pixiv_bookmark_tag_var = tk.StringVar(value=saved_pixiv_bookmark_tag)
        self.pixiv_author_match_var = tk.StringVar(
            value=next(
                (label for label, value in PIXIV_AUTHOR_MATCH_LABELS.items() if value == saved_pixiv_author_match),
                "部分匹配",
            )
        )
        self.pixiv_history_limit_var = tk.IntVar(value=saved_pixiv_history_limit)
        self.jm_domain_var = tk.StringVar(value=saved_jm_domain)
        self.jm_user_agent_var = tk.StringVar(value=saved_jm_user_agent)
        self.jm_order_var = tk.StringVar(value=next((label for label, value in JM_ORDER_LABELS.items() if value == saved_jm_order), "最新"))
        self.jm_time_var = tk.StringVar(value=next((label for label, value in JM_TIME_LABELS.items() if value == saved_jm_time), "全部时间"))
        self.jm_category_var = tk.StringVar(value=next((label for label, value in JM_CATEGORY_LABELS.items() if value == saved_jm_category), "全部"))
        self.jm_favorite_username_var = tk.StringVar(value=str(self.storage.get_setting("jmcomic_favorite_username", "") or ""))
        self.jm_favorite_folder_var = tk.StringVar(value=str(self.storage.get_setting("jmcomic_favorite_folder_id", "0") or "0"))
        self.jm_novel_favorite_folder_var = tk.StringVar(value=str(self.storage.get_setting("jmcomic_novel_favorite_folder_id", "0") or "0"))
        self.jm_match_var = tk.StringVar(value=next((label for label, value in JM_MATCH_LABELS.items() if value == saved_jm_match), "模糊匹配"))
        self.jm_postprocess_var = tk.StringVar(value=next((label for label, value in JM_POSTPROCESS_LABELS.items() if value == saved_jm_postprocess), "不合成"))
        self.jm_download_cover_var = tk.BooleanVar(value=saved_jm_download_cover)
        self.image_format_var = tk.StringVar(value=_format_label(IMAGE_FORMAT_LABELS, saved_image_format, "image_format"))
        self.video_format_var = tk.StringVar(value=_format_label(VIDEO_FORMAT_LABELS, saved_video_format, "video_format"))
        self.animation_format_var = tk.StringVar(value=_format_label(ANIMATION_FORMAT_LABELS, saved_animation_format, "animation_format"))
        self.audio_format_var = tk.StringVar(value=_format_label(AUDIO_FORMAT_LABELS, saved_audio_format, "audio_format"))
        self.archive_mode_var = tk.StringVar(
            value=next(
                (label for label, value in ARCHIVE_MODE_LABELS.items() if value == saved_archive_mode),
                "不压缩",
            )
        )
        self.extract_archives_var = tk.BooleanVar(value=saved_extract_archives)
        self.archive_cleanup_sources_var = tk.BooleanVar(value=saved_archive_cleanup_sources)
        self.eh_download_torrent_var = tk.BooleanVar(value=saved_eh_download_torrent)
        self.eh_bt_download_enabled_var = tk.BooleanVar(value=saved_eh_bt_download_enabled)
        self.google_source_name_var = tk.StringVar(value="尚未选择查询图片")
        self.google_source_info_var = tk.StringVar(value="支持 JPG、PNG、WEBP、GIF、BMP、AVIF")
        self.google_result_summary_var = tk.StringVar(value="尚未搜索")
        self.google_search_button_var = tk.StringVar(value="搜索相似页面")
        self.browser_driver_status_var = tk.StringVar(value="尚未检测 Chrome / ChromeDriver")
        self.aria2_status_var = tk.StringVar(value="尚未检测 aria2；仅保存种子不需要安装")
        self.cookie_capture_platform_var = tk.StringVar(value="Twitter/X")
        self.cookie_capture_status_var = tk.StringVar(
            value="选择平台后打开独立登录窗口；登录验证在网站内完成，软件不会保存账号密码。"
        )
        self.pixiv_connection_status_var = tk.StringVar(value="尚未检查 Pixiv 资料与头像连接")
        self.jm_connection_status_var = tk.StringVar(value="尚未检查 JMComic 站点连接")
        self.jm_auth_status_var = tk.StringVar(value="尚未读取 JMComic 登录资料")
        self.fanbox_status_var = tk.StringVar(
            value="尚未检查；信用卡/PayPal 等自动续费通常在每月 1～5 日扣款，预付方案以支付页为准"
        )
        plugin_errors = [report for report in self.context.plugin_reports if report.status == "error"]
        plugin_loaded = [report for report in self.context.plugin_reports if report.status == "loaded"]
        self.plugin_status_var = tk.StringVar(
            value=f"已加载 {len(plugin_loaded)} 个插件/平台模块"
            + (f"；{len(plugin_errors)} 个加载失败" if plugin_errors else "")
        )
        self.task_summary_var = tk.StringVar(value="暂无任务")
        self.task_detail_var = tk.StringVar(value="选择一个任务查看参数、日志和已下载文件")
        self.task_progress_var = tk.DoubleVar(value=0.0)
        self.status_var = tk.StringVar(value="就绪")
        self.library_search_var = tk.StringVar()
        self.library_status_var = tk.StringVar(value="尚未读取下载库")
        self.library_file_status_var = tk.StringVar(value="请选择文件夹")
        self.library_preview_status_var = tk.StringVar(value="请选择图片")
        self.library_preview_progress_var = tk.DoubleVar(value=0.0)
        self.profile_name_var = tk.StringVar(value="未加载主页")
        self.profile_handle_var = tk.StringVar(value="")
        self.profile_bio_var = tk.StringVar(value="")
        self.profile_url_var = tk.StringVar(value="")
        self.profile_target_hint_var = tk.StringVar(value="资料区一次显示一个目标")
        self.profile_links: list[dict] = []
        self.profile_block_key: AccountKey | None = None
        self.profile_link_var = tk.StringVar(value="未获取到主页外链")
        self.profile_link_summary_var = tk.StringVar(value="主页外链 0")
        self.platform_search_var = tk.StringVar()
        self.platform_scope_selection: dict[str, str] = {}
        self.platform_search_hint_var = tk.StringVar(value="按 @用户名查找")
        self.platform_search_button_var = tk.StringVar(value="查找")
        self.download_type_help_var = tk.StringVar(value="1 图片 · 2 视频 · 3 GIF · 4 音频，可组合填写")
        self.target_label_var = tk.StringVar(value="推特 @用户名 / URL")
        self.page_subtitle_var = tk.StringVar(value="选择平台和目标，预览后按需加入下载任务")
        self.content_scope_var = tk.StringVar(value=PLATFORM_CONTENT_SCOPES["twitter"][0])
        self.content_scope_help_var = tk.StringVar(value=PLATFORM_SCOPE_HINTS["twitter"])
        self.candidate_selection_var = tk.StringVar(value="尚未选择候选；单击表格中的一行")
        self.history_selection_var = tk.StringVar(value="尚未选择历史记录")
        self.target_selection_var = tk.StringVar(value="尚未选择左侧目标")
        self.selected_target_browser_key = ""
        self.selected_target_browser_keys: set[str] = set()
        self.following_refreshing = False
        self.following_refresh_mode = "new"
        self.following_cancel_event: threading.Event | None = None
        self._build_styles()
        self._build_layout()
        self.target_var.trace_add("write", self._refresh_profile_target_hint)
        self._refresh_jm_auth_status()
        self.protocol("WM_DELETE_WINDOW", self._request_close)
        self.bind("<Configure>", self._handle_window_resize, add="+")
        self.bind("<Control-o>", lambda _event: self._choose_google_image())
        self.bind("<Control-Return>", lambda _event: self._search_google_image())
        self.bind("<Escape>", lambda _event: self._cancel_task())
        self._load_modules()
        self._refresh_module_detail()
        self._refresh_tasks()
        self._refresh_library()
        self._load_following_accounts()
        self._load_twitter_history()
        self._poll_queue()

    def _handle_window_resize(self, event) -> None:
        if event.widget is not self or not hasattr(self, "preview_text"):
            return
        if int(event.height) < 740:
            self.preview_text.widget.grid_remove()
        else:
            self.preview_text.widget.grid()

    def destroy(self) -> None:
        if self.batch_info_cancel_event is not None:
            self.batch_info_cancel_event.set()
        if self.cookie_capture_cancel_event is not None:
            self.cookie_capture_cancel_event.set()
        if self.following_cancel_event is not None:
            self.following_cancel_event.set()
        if self.google_search_cancel_event is not None:
            self.google_search_cancel_event.set()
            try:
                self.manager.get_adapter("google_image").cancel("google-ui-search")
            except Exception:
                pass
        if self.google_link_check_cancel_event is not None:
            self.google_link_check_cancel_event.set()
        if self._google_preview_resize_job is not None:
            try:
                self.after_cancel(self._google_preview_resize_job)
            except tk.TclError:
                pass
            self._google_preview_resize_job = None
        if self._preview_resize_job is not None:
            try:
                self.after_cancel(self._preview_resize_job)
            except tk.TclError:
                pass
            self._preview_resize_job = None
        for job_name in ("_library_preview_job", "_library_refresh_job", "_library_filter_job"):
            job = getattr(self, job_name, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
                setattr(self, job_name, None)
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self.manager.active_task_ids():
            self.manager.shutdown(timeout=2.0)
        self.preview_image = None
        self.profile_avatar_image = None
        self.google_preview_image = None
        super().destroy()

    def _request_close(self) -> None:
        if self._closing:
            return
        active = self.manager.active_task_ids()
        if active and not messagebox.askyesno(
            "仍有下载任务",
            f"当前还有 {len(active)} 个排队中或运行中的任务。\n\n"
            "选择“是”会停止这些任务、关闭自动化浏览器并退出；本地已下载文件会保留。\n"
            "选择“否”返回软件继续运行。",
        ):
            return
        self._closing = True
        if active:
            self.status_var.set(f"正在停止 {len(active)} 个任务并退出…")
            self.update_idletasks()
            unfinished = self.manager.shutdown(timeout=8.0)
            if unfinished:
                self._append_log(f"退出时仍有 {len(unfinished)} 个任务未及时结束，已标记为取消")
        self.destroy()

    def _build_styles(self) -> None:
        self.palette = apply_desktop_theme(self)

    def _build_layout(self) -> None:
        self.columnconfigure(0, weight=0, minsize=330)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self, style="Sidebar.TFrame", padding=16, width=330)
        left.grid(row=0, column=0, sticky="nsew")
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(4, weight=2, minsize=95)
        left.rowconfigure(5, weight=2, minsize=175)
        left.rowconfigure(7, weight=1, minsize=70)
        ttk.Label(left, text="欲求达", style="SidebarTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(left, text="跨平台查找 · 相似搜索 · 黑名单", style="SidebarText.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 18))
        ttk.Label(left, text="目标与历史", style="SidebarText.TLabel").grid(row=2, column=0, sticky="w")

        target_search = ttk.Frame(left, style="Sidebar.TFrame")
        target_search.grid(row=3, column=0, sticky="ew", pady=(7, 7))
        target_search.columnconfigure(0, weight=1)
        self.platform_search_mode_combo = ttk.Combobox(
            target_search,
            textvariable=self.content_scope_var,
            state="readonly",
            values=PLATFORM_CONTENT_SCOPES["twitter"],
        )
        self.platform_search_mode_combo.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 5))
        self.platform_search_mode_combo.bind("<<ComboboxSelected>>", self._content_scope_changed)
        self.platform_search_entry = ttk.Entry(target_search, textvariable=self.platform_search_var)
        self.platform_search_entry.grid(row=1, column=0, sticky="ew", padx=(0, 6))
        self.platform_search_entry.bind("<Return>", lambda _event: self._search_platform_targets())
        self.platform_search_button = ttk.Button(
            target_search,
            textvariable=self.platform_search_button_var,
            command=self._search_platform_targets,
        )
        self.platform_search_button.grid(row=1, column=1)
        ttk.Label(target_search, textvariable=self.platform_search_hint_var, style="SidebarText.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(3, 0)
        )

        target_list_frame = ttk.Frame(left, style="Panel.TFrame")
        target_list_frame.grid(row=4, column=0, sticky="nsew", pady=(0, 10))
        target_list_frame.columnconfigure(0, weight=1)
        target_list_frame.rowconfigure(0, weight=1)
        self.module_list = tk.Listbox(
            target_list_frame,
            selectmode=tk.EXTENDED,
            borderwidth=0,
            highlightthickness=0,
            activestyle="none",
            exportselection=False,
            background="#ffffff",
            foreground=self.palette["text"],
            selectbackground=self.palette["primary"],
            selectforeground="#ffffff",
            font=("Microsoft YaHei UI", 9),
        )
        self.module_list.grid(row=0, column=0, sticky="nsew")
        target_list_scrollbar = ttk.Scrollbar(target_list_frame, orient="vertical", command=self.module_list.yview)
        target_list_scrollbar.grid(row=0, column=1, sticky="ns")
        self.module_list.configure(yscrollcommand=target_list_scrollbar.set)
        self.module_list.bind("<<ListboxSelect>>", lambda _event: self._select_target_from_browser())
        ttk.Label(
            target_list_frame,
            textvariable=self.target_selection_var,
            style="Small.TLabel",
            wraplength=285,
        ).grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=(4, 4))
        target_actions = ttk.Frame(target_list_frame, style="FlatPanel.TFrame")
        target_actions.grid(row=2, column=0, columnspan=2, sticky="ew", padx=6, pady=(0, 6))
        target_actions.columnconfigure(0, weight=1)
        self.target_queue_button = ttk.Button(
            target_actions,
            text="加入选中到下载队列",
            command=self._download_selected_target_browser,
        )
        self.target_queue_button.grid(row=0, column=0, sticky="ew")
        ttk.Button(target_actions, text="取消多选", command=self._clear_target_browser_selection).grid(
            row=0, column=1, padx=(6, 0)
        )
        self.target_queue_button.state(["disabled"])

        target_card_shell = ttk.Frame(left, style="Panel.TFrame")
        target_card_shell.grid(row=5, column=0, sticky="nsew", pady=(0, 10))
        target_card_shell.columnconfigure(0, weight=1)
        target_card_shell.rowconfigure(0, weight=1)
        target_card_canvas = tk.Canvas(
            target_card_shell, background=self.palette["panel"], highlightthickness=0, width=284
        )
        target_card_scrollbar = ttk.Scrollbar(target_card_shell, orient="vertical", command=target_card_canvas.yview)
        target_card_canvas.configure(yscrollcommand=target_card_scrollbar.set)
        target_card_canvas.grid(row=0, column=0, sticky="nsew")
        target_card_scrollbar.grid(row=0, column=1, sticky="ns")
        target_card = ttk.Frame(target_card_canvas, style="Panel.TFrame", padding=8)
        target_card_window = target_card_canvas.create_window((0, 0), window=target_card, anchor="nw")
        target_card.bind(
            "<Configure>", lambda _event: target_card_canvas.configure(scrollregion=target_card_canvas.bbox("all"))
        )
        target_card_canvas.bind(
            "<Configure>", lambda event: target_card_canvas.itemconfigure(target_card_window, width=event.width)
        )
        target_card.columnconfigure(1, weight=1)
        self.profile_avatar_label = ttk.Label(target_card, text="头像", anchor="center", style="Panel.TLabel", width=8)
        self.profile_avatar_label.grid(row=0, column=0, rowspan=4, sticky="n", padx=(0, 8))
        ttk.Label(target_card, textvariable=self.profile_name_var, style="Name.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(target_card, textvariable=self.profile_handle_var, style="Small.TLabel").grid(row=1, column=1, sticky="w", pady=(2, 0))
        bio_frame = ttk.Frame(target_card, style="Panel.TFrame")
        bio_frame.grid(row=2, column=1, sticky="ew", pady=(4, 0))
        bio_frame.columnconfigure(0, weight=1)
        self.profile_bio_text = tk.Text(
            bio_frame,
            height=4,
            wrap="word",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=self.palette["border"],
            padx=5,
            pady=4,
            background="#ffffff",
            foreground=self.palette["text"],
            font=("Microsoft YaHei UI", 9),
        )
        self.profile_bio_text.grid(row=0, column=0, sticky="ew")
        bio_scrollbar = ttk.Scrollbar(bio_frame, orient="vertical", command=self.profile_bio_text.yview)
        bio_scrollbar.grid(row=0, column=1, sticky="ns")
        self.profile_bio_text.configure(yscrollcommand=bio_scrollbar.set, state="disabled")
        self.profile_bio_var.trace_add("write", self._sync_profile_bio_text)
        self.profile_url_label = ttk.Label(
            target_card,
            textvariable=self.profile_url_var,
            style="Small.TLabel",
            wraplength=240,
        )
        self.profile_url_label.grid(row=3, column=1, sticky="w", pady=(4, 0))
        ttk.Label(
            target_card,
            textvariable=self.profile_target_hint_var,
            style="Small.TLabel",
            wraplength=240,
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        link_panel = ttk.Frame(target_card, style="Panel.TFrame")
        link_panel.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        link_panel.columnconfigure(0, weight=1)
        ttk.Label(link_panel, textvariable=self.profile_link_summary_var, style="Small.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Button(link_panel, text="复制简介", width=7, command=self._copy_profile_bio).grid(
            row=0, column=1, columnspan=2, sticky="e", padx=(5, 0)
        )
        self.profile_link_combo = ttk.Combobox(link_panel, textvariable=self.profile_link_var, state="readonly", width=12)
        self.profile_link_combo.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(4, 0))
        link_actions = ttk.Frame(link_panel, style="Panel.TFrame")
        link_actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        link_actions.columnconfigure((0, 1), weight=1)
        ttk.Button(link_actions, text="打开外链", style="Compact.TButton", command=self._open_selected_profile_link).grid(row=0, column=0, sticky="ew")
        ttk.Button(link_actions, text="复制地址", style="Compact.TButton", command=self._copy_selected_profile_link).grid(
            row=0, column=1, sticky="ew", padx=(6, 0)
        )
        block_actions = ttk.Frame(link_panel, style="Panel.TFrame")
        block_actions.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        block_actions.columnconfigure((0, 1), weight=1)
        ttk.Button(block_actions, text="屏蔽作者", style="Compact.TButton", command=self._block_current_profile).grid(row=0, column=0, sticky="ew")
        ttk.Button(block_actions, text="屏蔽作品/页面", style="Compact.TButton", command=self._block_current_work).grid(row=0, column=1, sticky="ew", padx=(6, 0))
        ttk.Button(block_actions, text="管理黑名单", style="Compact.TButton", command=self._manage_blocklist).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(5, 0))
        self.blocklist_status_var = tk.StringVar(value="黑名单按作者或具体作品生效")
        ttk.Label(link_panel, textvariable=self.blocklist_status_var, style="Small.TLabel").grid(
            row=4, column=0, columnspan=3, sticky="w", pady=(4, 0)
        )
        target_card.bind("<Configure>", self._update_target_card_wrap)
        self.preview_text = TargetPreviewRenderer(target_card)
        self.preview_text.grid(row=6, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.module_detail = self.preview_text
        self._write_text(self.preview_text, "选择历史目标，或在右侧输入目标 / URL 后点击预览。")
        bind_canvas_mousewheel(target_card_canvas, target_card)

        ttk.Label(left, text="运行日志", style="SidebarText.TLabel").grid(row=6, column=0, sticky="w", pady=(0, 6))
        log_frame = ttk.Frame(left, style="Panel.TFrame")
        log_frame.grid(row=7, column=0, sticky="nsew")
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log_text = tk.Text(
            log_frame,
            height=8,
            wrap="word",
            borderwidth=0,
            highlightthickness=0,
            padx=9,
            pady=9,
            background="#f8fafc",
            foreground=self.palette["text"],
            insertbackground=self.palette["primary"],
            font=("Consolas", 9),
        )
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=log_scrollbar.set, state="disabled")
        main = ttk.Frame(self, style="App.TFrame")
        main.grid(row=0, column=1, sticky="nsew", padx=16, pady=14)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        page_header = ttk.Frame(main, style="App.TFrame")
        page_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        page_header.columnconfigure(0, weight=1)
        ttk.Label(page_header, text="欲求达", style="HeaderTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(page_header, text="查找目标、识别相似图片、管理屏蔽规则，按需保存到本地", style="HeaderSub.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="ew", pady=(2, 0)
        )
        self.page_subtitle_label = ttk.Label(
            page_header,
            textvariable=self.page_subtitle_var,
            style="HeaderSub.TLabel",
        )
        self.page_subtitle_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Label(page_header, textvariable=self.status_var, style="Status.TLabel").grid(row=0, column=1, sticky="e")
        header_actions = ttk.Frame(page_header, style="App.TFrame")
        header_actions.grid(row=3, column=0, columnspan=2, sticky="e", pady=(8, 0))
        page_header.bind(
            "<Configure>",
            lambda event: self.page_subtitle_label.configure(wraplength=max(280, event.width - 12)),
        )
        self.start_button = ttk.Button(header_actions, text="开始任务", style="Accent.TButton", command=self._start_download)
        self.start_button.grid(row=0, column=0, padx=(0, 6))
        self.preview_button = ttk.Button(header_actions, text="本地预览", command=self._preview_target)
        self.preview_button.grid(row=0, column=1, padx=(0, 6))
        self.online_button = ttk.Button(header_actions, text="在线获取资料", command=self._preview_online)
        self.online_button.grid(row=0, column=2, padx=(0, 6))
        self.batch_info_button = ttk.Button(header_actions, text="批量获取资料", command=self._batch_fetch_target_info)
        self.batch_info_button.grid(row=0, column=3, padx=(0, 6))
        self.browser_button = ttk.Button(header_actions, text="打开网页", command=self._open_target_in_browser)
        self.browser_button.grid(row=0, column=4, padx=(0, 6))
        ttk.Button(header_actions, text="取消", command=self._cancel_task).grid(row=0, column=5)

        top = ttk.Frame(main, style="Panel.TFrame", padding=9)
        top.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        top.columnconfigure(3, weight=1)

        ttk.Label(top, text="平台", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.module_combo = ttk.Combobox(top, textvariable=self.module_label_var, state="readonly", width=16)
        self.module_combo.grid(row=0, column=1, sticky="w", padx=(0, 10))
        self.module_combo.bind("<<ComboboxSelected>>", lambda _event: self._select_module_from_combo())
        ttk.Label(top, text="保存到", style="Panel.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.output_dir_entry = ttk.Entry(top, textvariable=self.output_dir_var)
        self.output_dir_entry.grid(row=0, column=3, sticky="ew", padx=(0, 10))
        self.output_browse_button = ttk.Button(top, text="浏览", command=self._choose_output_dir)
        self.output_browse_button.grid(row=0, column=4, sticky="ew")
        ttk.Label(top, textvariable=self.target_label_var, style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        self.target_entry = ttk.Entry(top, textvariable=self.target_var)
        self.target_entry.grid(row=1, column=1, columnspan=3, sticky="ew", padx=(0, 10), pady=(10, 0))
        type_label_frame = ttk.Frame(top, style="Panel.TFrame")
        type_label_frame.grid(row=1, column=4, sticky="w", padx=(0, 8), pady=(10, 0))
        ttk.Label(type_label_frame, text="下载类型", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(type_label_frame, text="说明", width=4, command=self._show_download_type_help).grid(row=0, column=1, padx=(5, 0))
        self.types_entry = ttk.Entry(top, textvariable=self.types_var, width=14)
        self.types_entry.grid(row=1, column=5, sticky="ew", pady=(10, 0))
        ttk.Label(
            top,
            text="多目标用 /、反斜杠或顿号分隔；开始任务可批量下载，批量获取资料可逐项查看；URL 和搜索词内的 / 会保留。",
            style="Small.TLabel",
        ).grid(row=2, column=1, columnspan=5, sticky="w", pady=(3, 0))
        self.notebook = ttk.Notebook(main)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.google_search_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.tasks_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.following_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.twitter_history_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.library_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.settings_tab = ttk.Frame(self.notebook, style="Panel.TFrame", padding=10)
        self.notebook.add(self.google_search_tab, text="相似搜索")
        self.notebook.add(self.tasks_tab, text="任务队列")
        self.notebook.add(self.library_tab, text="下载库")
        self.notebook.add(self.following_tab, text="推特关注")
        self.notebook.add(self.twitter_history_tab, text="推特历史")
        self.notebook.add(self.settings_tab, text="设置")

        self._build_google_search_tab()
        self._build_tasks_tab()
        self._build_following_tab()
        self._build_twitter_history_tab()
        self._build_library_tab()
        self._build_settings_tab()

        footer = ttk.Frame(main, style="App.TFrame")
        footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="提示：双击候选页面可先进行安全静态预览", style="HeaderSub.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="本地处理 · 任务可取消", style="HeaderSub.TLabel").grid(row=0, column=1, sticky="e")

    def _create_scrollable_tree(self, parent: tk.Widget, columns: tuple[str, ...], height: int | None = None):
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        options = {"columns": columns, "show": "headings"}
        if height is not None:
            options["height"] = height
        tree = ttk.Treeview(frame, **options)
        tree.grid(row=0, column=0, sticky="nsew")

        vertical_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")
        tree.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        return frame, tree

    def _update_target_card_wrap(self, event=None) -> None:
        if not hasattr(self, "profile_url_label"):
            return
        width = int(getattr(event, "width", 0) or self.profile_url_label.master.winfo_width())
        wraplength = max(120, width - 100)
        self.profile_url_label.configure(wraplength=wraplength)

    def _sync_profile_bio_text(self, *_args) -> None:
        if not hasattr(self, "profile_bio_text"):
            return
        self.profile_bio_text.configure(state="normal")
        self.profile_bio_text.delete("1.0", tk.END)
        self.profile_bio_text.insert("1.0", self.profile_bio_var.get())
        self.profile_bio_text.configure(state="disabled")
        self.profile_bio_text.yview_moveto(0.0)



    def _module_label(self, adapter) -> str:
        marker = {"ready": "可用", "alpha": "试验", "planned": "规划"}.get(adapter.info.stage, adapter.info.stage)
        return f"{adapter.display_name} [{marker}]"

    def _load_modules(self) -> None:
        self.module_labels = {}
        labels = []
        for adapter in self.adapters:
            label = self._module_label(adapter)
            self.module_labels[label] = adapter.module_id
            labels.append(label)
        if hasattr(self, "module_combo"):
            self.module_combo.configure(values=labels)
        if labels:
            current = self.module_var.get() or self.adapters[0].module_id
            selected = next((label for label, module_id in self.module_labels.items() if module_id == current), labels[0])
            self.module_var.set(self.module_labels.get(selected, current))
            self.module_label_var.set(selected)
        self._refresh_target_browser()

    def _select_module_from_combo(self) -> None:
        module_id = self.module_labels.get(self.module_label_var.get(), "")
        if not module_id:
            return
        self.module_var.set(module_id)
        self._refresh_module_detail()
        self._refresh_library(scan=False)

    def _select_target_from_browser(self) -> None:
        rows = self._selected_target_browser_rows()
        if not rows:
            self.selected_target_browser_key = ""
            self.selected_target_browser_keys.clear()
            self.target_selection_var.set("尚未选择左侧目标")
            self.target_queue_button.state(["disabled"])
            return
        keys = {self._target_browser_key(row) for row in rows if self._target_browser_key(row)}
        self.selected_target_browser_keys = keys
        row = rows[0]
        self.selected_target_browser_key = self._target_browser_key(row)
        downloadable = False
        for selected_row in rows:
            module_id = str(selected_row.get("module_id") or self.module_var.get())
            if module_id == "google_image" and str(selected_row.get("target") or "").startswith(("http://", "https://")):
                module_id = "website"
            try:
                adapter = self.manager.get_adapter(module_id)
                downloadable = downloadable or (
                    bool(selected_row.get("downloadable", True))
                    and "download" in adapter.info.capabilities and adapter.info.stage != "planned"
                )
            except KeyError:
                pass
        self.target_queue_button.state(["!disabled"] if downloadable else ["disabled"])
        if len(rows) > 1:
            self.target_selection_var.set(f"已选中 {len(rows)} 项 · 可直接加入下载队列")
            self._write_text(
                self.module_detail,
                "\n".join(
                    f"{index}. {item.get('name') or item.get('target') or '-'}  →  {item.get('target') or '-'}"
                    for index, item in enumerate(rows, 1)
                ),
            )
            return
        if row.get("_batch_result"):
            index = int(row.get("_batch_index") or 1)
            count = int(row.get("_batch_count") or 1)
            target = str(row.get("target") or "").strip()
            self.target_selection_var.set(
                f"批量资料 · 第 {index}/{count} 项 · {row.get('name') or target}"
            )
            payload = row.get("_batch_payload")
            preview = row.get("_batch_preview")
            if isinstance(payload, dict):
                self._write_profile_preview(payload)
            elif preview is not None:
                self._render_generic_preview(preview)
            else:
                self._write_target_browser_detail(row)
            self.profile_target_hint_var.set(f"批量资料第 {index}/{count} 项；左侧资料卡一次显示一项")
            self.status_var.set(f"正在显示第 {index}/{count} 项批量资料")
            return
        target = str(row.get("target") or "").strip()
        source = str(row.get("source") or "目标")
        name = str(row.get("name") or target or "-").strip()
        self.target_selection_var.set(f"已选中 · {source} · {name}")
        self._write_target_browser_detail(row)
        if not target:
            return
        module_id = str(row.get("module_id") or self.module_var.get())
        if module_id != self.module_var.get():
            self._select_module(module_id)
        self.target_var.set(target)
        self._preview_target()

    def _selected_target_browser_rows(self) -> list[dict]:
        return [
            self.target_browser_rows[int(index)]
            for index in self.module_list.curselection()
            if int(index) < len(self.target_browser_rows)
        ]

    def _clear_target_browser_selection(self) -> None:
        self.module_list.selection_clear(0, tk.END)
        self._select_target_from_browser()

    def _download_selected_target_browser(self) -> None:
        rows = self._selected_target_browser_rows()
        if not rows:
            messagebox.showwarning("未选择目标", "请先在左侧选择一个或多个搜索、关注或历史目标")
            return
        self._queue_download_rows(rows)

    def _select_module_from_list(self) -> None:
        self._select_target_from_browser()

    def _selected_adapter(self):
        module_id = self.module_var.get()
        return self.manager.get_adapter(module_id)

    def _select_module(self, module_id: str) -> None:
        self.module_var.set(module_id)
        selected = next((label for label, value in self.module_labels.items() if value == module_id), "")
        if selected:
            self.module_label_var.set(selected)
        self._refresh_module_detail()

    def _refresh_module_detail(self) -> None:
        adapter = self._selected_adapter()
        self._configure_content_scopes(adapter.module_id)
        if adapter.module_id == "pixiv":
            self.pixiv_following_rows = adapter.load_following_accounts()
            if self.content_scope_var.get() in {"作品收藏", "小说收藏"}:
                kind = "novel" if self.content_scope_var.get() == "小说收藏" else "work"
                self.platform_candidate_rows["pixiv"] = adapter.load_bookmark_candidates(kind)
                self.platform_candidate_scope["pixiv"] = self.content_scope_var.get()
            cache_info = adapter.following_cache_info()
            if cache_info.get("valid"):
                cache_state = "部分缓存" if cache_info.get("partial") else "完整缓存"
                self.following_progress_status_var.set(
                    f"本地 {cache_state} · {len(self.pixiv_following_rows)} 个"
                )
            else:
                self.following_progress_status_var.set("尚无有效的 Pixiv 关注缓存")
        if hasattr(self, "module_combo"):
            selected = next((label for label, module_id in self.module_labels.items() if module_id == adapter.module_id), "")
            if selected:
                self.module_label_var.set(selected)
        can_download = "download" in adapter.info.capabilities and adapter.info.stage != "planned"
        target_labels = {
            "twitter": "推特 @用户名 / URL",
            "pixiv": "Pixiv 作品 / 作者",
            "jmcomic": "漫画 ID / URL",
            "bluesky": "Bluesky 用户 / 帖子 URL",
            "instagram": "Instagram 用户 / 帖子 / Reels URL",
            "google_image": "查询图片（见相似搜索页）",
            "website": "候选网页 URL",
        }
        self.target_label_var.set(target_labels.get(adapter.module_id, "目标 / URL"))
        subtitles = {
            "twitter": "按 @用户名查找 · 预览主页资料与外链 · 下载媒体",
            "pixiv": "搜索或输入作品 / 作者目标 · 使用软件内置 Pixiv 爬虫",
            "jmcomic": "漫画、章节和账号列表均可用 · 登录资料与搜索条件在设置页管理",
            "bluesky": "公开用户、关注和帖子搜索 · 图片与视频使用官方 AppView 数据",
            "instagram": "账号、帖子和 Reels 页面媒体 · 登录限定内容需要 Cookie",
            "ehentai": "表站/里站画廊搜索与预览 · 下载正常展示图和元数据，种子默认关闭",
            "google_image": "Google 查找相似页面 · 黑名单自动标记 · 选中后爬取",
            "website": "预览候选页面 · 渲染安全静态 HTML · 下载页面媒体",
        }
        self.page_subtitle_var.set(subtitles.get(adapter.module_id, adapter.info.description))
        if adapter.module_id == "pixiv" and self.content_scope_var.get() == "关注画师":
            self.target_label_var.set("Pixiv 关注页 URL / 账号用户 ID")
            self.page_subtitle_var.set("分页读取关注画师 · 更新新的不误删 · 更新全部同步取关")
        if can_download and adapter.module_id != "google_image":
            self.start_button.state(["!disabled"])
        else:
            self.start_button.state(["disabled"])
        if adapter.module_id == "google_image":
            self.preview_button.state(["disabled"])
            self.online_button.state(["disabled"])
            self.browser_button.state(["disabled"])
            self.notebook.select(self.google_search_tab)
        else:
            self.preview_button.state(["!disabled"])
            self.online_button.state(["!disabled"])
            self.browser_button.state(["!disabled"])
            if self.notebook.select() == str(self.google_search_tab):
                self.notebook.select(self.tasks_tab)
        if adapter.module_id in {"twitter", "bluesky", "instagram"}:
            self.types_entry.state(["!disabled"])
            if adapter.module_id == "twitter":
                self.download_type_help_var.set("1 图片 · 2 视频 · 3 GIF · 4 音频；可用逗号或连续数字组合，例如 1,2 或 123")
            elif adapter.module_id == "bluesky":
                self.download_type_help_var.set("Bluesky：1 图片 · 2 视频；帖子搜索和作者主页下载会按这里筛选媒体")
            else:
                self.download_type_help_var.set("Instagram：1 图片 · 2 视频；GIF/音频转换仍使用公共输出格式设置")
        elif adapter.module_id == "website":
            self.types_entry.state(["disabled"])
            self.download_type_help_var.set("网页资源爬虫会自动识别图片、视频和音频，下载类型无需填写")
        elif adapter.module_id == "google_image":
            self.types_entry.state(["disabled"])
            self.download_type_help_var.set("Google 只查找候选页面；资源类型在选中页面后由网页爬虫自动识别")
        else:
            self.types_entry.state(["disabled"])
            if adapter.module_id == "pixiv":
                self.download_type_help_var.set("Pixiv 的标题、简介和标签是搜索/资料字段；开始任务会下载作品、小说或扩展目标资源")
            else:
                self.download_type_help_var.set(f"{adapter.display_name} 当前按作品/章节目标下载，本栏暂不参与筛选")
        self._configure_scope_actions()
        self._refresh_target_browser()
        self._refresh_platform_tabs()

    def _configure_content_scopes(self, module_id: str) -> None:
        scopes = PLATFORM_CONTENT_SCOPES.get(module_id, ("目标",))
        selected = self.platform_scope_selection.get(module_id, "")
        if selected not in scopes:
            selected = scopes[0]
        self.content_scope_var.set(selected)
        self.platform_scope_selection[module_id] = selected
        if module_id == "pixiv":
            self.content_scope_help_var.set(
                PIXIV_SCOPE_HINTS.get(selected, PLATFORM_SCOPE_HINTS.get(module_id, "选择当前平台的搜索种类。"))
            )
        elif module_id == "jmcomic":
            self.content_scope_help_var.set(
                JM_SCOPE_HINTS.get(selected, PLATFORM_SCOPE_HINTS.get(module_id, "选择当前平台的搜索种类。"))
            )
        else:
            self.content_scope_help_var.set(PLATFORM_SCOPE_HINTS.get(module_id, "选择当前平台的搜索种类。"))
        if hasattr(self, "platform_search_mode_combo"):
            self.platform_search_mode_combo.configure(values=scopes)
        if hasattr(self, "content_scope_combo"):
            self.content_scope_combo.configure(values=scopes)
        self._refresh_profile_target_hint()

    def _content_scope_changed(self, _event=None) -> None:
        module_id = self.module_var.get()
        self.platform_scope_selection[module_id] = self.content_scope_var.get()
        self._refresh_profile_target_hint()
        if module_id == "pixiv":
            self.content_scope_help_var.set(
                PIXIV_SCOPE_HINTS.get(self.content_scope_var.get(), PLATFORM_SCOPE_HINTS["pixiv"])
            )
            if self.content_scope_var.get() in {"作品收藏", "小说收藏"}:
                kind = "novel" if self.content_scope_var.get() == "小说收藏" else "work"
                self.platform_candidate_rows["pixiv"] = self.manager.get_adapter("pixiv").load_bookmark_candidates(kind)
                self.platform_candidate_scope["pixiv"] = self.content_scope_var.get()
        elif module_id == "jmcomic":
            self.content_scope_help_var.set(
                JM_SCOPE_HINTS.get(self.content_scope_var.get(), PLATFORM_SCOPE_HINTS["jmcomic"])
            )
        if module_id == "pixiv" and self.content_scope_var.get() == "关注画师":
            self.target_label_var.set("Pixiv 关注页 URL / 账号用户 ID")
            self.page_subtitle_var.set("分页读取关注画师 · 更新新的不误删 · 更新全部同步取关")
        elif module_id == "pixiv" and self.content_scope_var.get() == "FANBOX":
            self.target_label_var.set("FANBOX 创作者 / 帖子 URL")
            self.page_subtitle_var.set("付费帖需要有效订阅 · 可在设置中检查订阅状态和日期")
        elif module_id == "pixiv":
            self.target_label_var.set(f"Pixiv 目标 · {self.content_scope_var.get()}")
            self.page_subtitle_var.set("搜索或输入作品、系列、小说及扩展目标 · 使用软件内置 Pixiv 爬虫")
        elif module_id == "jmcomic" and self.content_scope_var.get() == "漫画收藏夹":
            self.target_label_var.set("JMComic 收藏夹用户名")
            self.page_subtitle_var.set("需要 AVS 登录会话 · 用户名和漫画收藏目录可在设置中保存")
        elif module_id == "jmcomic" and self.content_scope_var.get() in {
            "小说收藏夹", "追更连载", "漫画观看记录", "小说观看记录",
        }:
            self.target_label_var.set("JMComic 个人主页用户名")
            self.page_subtitle_var.set("从个人主页只读获取 · 需要用户名和 AVS；cf_clearance 单独无效")
        elif module_id == "jmcomic":
            self.target_label_var.set(f"JMComic 目标 · {self.content_scope_var.get()}")
            self.page_subtitle_var.set("分类、排序和时间范围可在设置中调整 · 候选最多 15 个")
        elif module_id == "bluesky":
            self.target_label_var.set(f"Bluesky 目标 · {self.content_scope_var.get()}")
            self.page_subtitle_var.set("公开 AppView API 读取 · 用户、关注和帖子无需登录")
        elif module_id == "instagram":
            self.target_label_var.set(f"Instagram 目标 · {self.content_scope_var.get()}")
            self.page_subtitle_var.set("输入精确用户名或帖子/Reels 链接 · 登录限定页面需要 Cookie")
        elif module_id == "ehentai":
            self.target_label_var.set(f"EH 目标 · {self.content_scope_var.get()}")
            self.page_subtitle_var.set("表/里站账号分开 · 里站尽量使用非亚洲代理/VPN · 不自动购买付费归档")
        self._configure_scope_actions()
        self._refresh_target_browser()
        self._refresh_platform_tabs()

    def _configure_scope_actions(self) -> None:
        module_id = self.module_var.get()
        scope = self.content_scope_var.get()
        if "规划" in scope:
            self.start_button.configure(text="当前功能未接入")
            self.start_button.state(["disabled"])
            return
        if module_id != "pixiv":
            self.start_button.configure(text="开始任务")
            return
        if scope == "关注画师":
            self.start_button.configure(text="从列表下载")
            self.start_button.state(["disabled"])
            return
        discovery = {
            "作者搜索", "标签（全部）", "标签（插画）", "标签（漫画）", "标题 / 简介",
            "作者 + 标签", "排行榜", "新作", "作品收藏", "小说收藏", "浏览历史（Premium）",
        }
        self.start_button.configure(text="搜索并下载结果" if scope in discovery else "开始下载")
        self.start_button.state(["!disabled"])

    @staticmethod
    def _target_browser_key(row: dict) -> str:
        module_id = str(row.get("module_id") or "").strip().lower()
        target = str(row.get("target") or "").strip().lower()
        return f"{module_id}|{target}" if target else ""

    def _restore_target_browser_selection(self, scroll_fraction: float) -> bool:
        wanted = set(self.selected_target_browser_keys)
        if not wanted and self.selected_target_browser_key:
            wanted.add(self.selected_target_browser_key)
        selected_indices = [
            index
            for index, row in enumerate(self.target_browser_rows)
            if self._target_browser_key(row) in wanted
        ]
        if not selected_indices:
            self.module_list.selection_clear(0, tk.END)
            self.target_selection_var.set("尚未选择左侧目标")
            self.target_queue_button.state(["disabled"])
        else:
            self.module_list.selection_clear(0, tk.END)
            for index in selected_indices:
                self.module_list.selection_set(index)
            self.module_list.activate(selected_indices[0])
            self._select_target_from_browser()
        if self.module_list.size():
            self.module_list.yview_moveto(max(0.0, min(float(scroll_fraction), 1.0)))
        return bool(selected_indices)

    def _refresh_target_browser(self) -> None:
        if not hasattr(self, "module_list"):
            return
        current_view = self.module_list.yview()
        scroll_fraction = float(current_view[0]) if current_view else 0.0
        self.target_browser_rows = []
        self.module_list.delete(0, tk.END)
        seen = set()

        adapter = self._selected_adapter()
        if adapter.module_id != "twitter":
            scope = self.content_scope_var.get()
            labels = {
                "google_image": ("输入框不用于 Google 搜索", "打开相似搜索"),
                "website": ("输入网页 URL 后预览或下载", "使用 URL"),
                "pixiv": (
                    f"当前：{scope}",
                    "使用" if scope in {"作品 ID", "作者 ID", "漫画系列", "小说 ID", "作者小说", "小说系列", "FANBOX", "Sketch"} else "搜索",
                ),
                "jmcomic": (f"当前：{scope}", "使用" if scope in {"漫画 ID / 链接", "章节 ID / 链接"} else "搜索"),
                "bluesky": (f"当前：{scope}", "使用" if scope == "帖子 / 媒体" else "搜索"),
                "instagram": (f"当前：{scope}", "使用" if scope != "关注账号（需登录，规划）" else "不可用"),
                "ehentai": (f"当前：{scope}", "使用" if scope == "画廊链接 / ID" else "搜索"),
            }
            hint, button = labels.get(adapter.module_id, ("当前平台暂无目标搜索", "不可用"))
            self.platform_search_hint_var.set(hint)
            self.platform_search_button_var.set(button)
            searchable = ("search" in adapter.info.capabilities or adapter.module_id in {"google_image", "website"}) and "规划" not in scope
            self.platform_search_button.state(["!disabled"] if searchable else ["disabled"])
            self.platform_search_entry.state(["!disabled"] if adapter.module_id != "google_image" and searchable else ["disabled"])

            if adapter.module_id == "pixiv" and scope == "关注画师":
                self.platform_search_hint_var.set("在上方目标框粘贴 Pixiv 关注页；留空使用上次账号")
                self.platform_search_button_var.set("关注列表用上方更新按钮")
                self.platform_search_button.state(["disabled"])
                self.platform_search_entry.state(["disabled"])
                for following in self.pixiv_following_rows:
                    user_id = str(following.get("user_id") or "").strip()
                    if not user_id:
                        continue
                    target = str(following.get("profile_url") or f"https://www.pixiv.net/users/{user_id}")
                    seen.add(target.lower())
                    item = {
                        "module_id": "pixiv",
                        "source": "关注画师",
                        "target": target,
                        "name": str(following.get("display_name") or user_id),
                        "bio": str(following.get("bio") or ""),
                        "avatar_url": str(following.get("avatar_url") or ""),
                        "profile_url": target,
                        "author_id": user_id,
                    }
                    self.target_browser_rows.append(item)
                    self.module_list.insert(tk.END, f"关注  {item['name']}  [{user_id}]")

            if adapter.module_id == "pixiv" and scope in {"作品收藏", "小说收藏", "浏览历史（Premium）"}:
                account_id = str(adapter.cookie_account_id() or "").strip()
                self.platform_search_hint_var.set(
                    f"读取当前 Cookie 账号 {account_id or '（无法识别）'} 的{scope}；不使用手动关键词"
                )
                self.platform_search_button_var.set("更新我的收藏" if "收藏" in scope else "更新我的历史")
                self.platform_search_entry.state(["disabled"])

            if adapter.module_id == "google_image" and self.google_image_var.get().strip():
                image_path = self.google_image_var.get().strip()
                self.target_browser_rows.append(
                    {"module_id": "google_image", "source": "当前图片", "target": image_path, "name": Path(image_path).name}
                )
                self.module_list.insert(tk.END, f"当前图片  {Path(image_path).name}")
            for task_row in self.storage.list_tasks(limit=100):
                row = dict(task_row)
                if str(row.get("module_id") or "") != adapter.module_id:
                    continue
                target = str(row.get("target") or "").strip()
                if not target or target.lower() in seen:
                    continue
                seen.add(target.lower())
                item = {
                    "module_id": adapter.module_id,
                    "source": "任务",
                    "target": target,
                    "name": str(row.get("status") or ""),
                    "profile_url": target if target.startswith(("http://", "https://")) else "",
                }
                self.target_browser_rows.append(item)
                self.module_list.insert(tk.END, f"任务  {target}  [{item['name']}]")
                if adapter.module_id != "pixiv" and len(self.target_browser_rows) >= 30:
                    break
            if not self.target_browser_rows:
                self.module_list.insert(tk.END, f"暂无 {adapter.display_name} 历史目标")
            if not self._restore_target_browser_selection(scroll_fraction):
                self._show_platform_overview(adapter)
            return

        scope = self.content_scope_var.get()
        twitter_hints = {
            "关注账号": "输入用户名或昵称，在已抓取关注和历史中查找",
            "用户 / @用户名": "输入 @用户名、用户名或昵称；精确 @名可直接在线预览",
            "帖子 / 媒体 URL": "输入 x.com 帖子或媒体页 URL",
        }
        self.platform_search_hint_var.set(twitter_hints.get(scope, "输入 @用户名查找"))
        self.platform_search_button_var.set("使用" if scope == "帖子 / 媒体 URL" else "查找")
        self.platform_search_button.state(["!disabled"])
        self.platform_search_entry.state(["!disabled"])

        for row in self.following_rows:
            handle = str(row.get("handle") or "").strip().lstrip("@")
            if not handle:
                continue
            key = f"@{handle}".lower()
            if key in seen:
                continue
            seen.add(key)
            item = {
                "module_id": "twitter",
                "source": "关注",
                "target": f"@{handle}",
                "handle": handle,
                "name": str(row.get("display_name") or "").strip(),
                "bio": str(row.get("bio") or "").strip(),
                "profile_url": row.get("profile_url") or f"https://x.com/{handle}",
            }
            self.target_browser_rows.append(item)
            label = f"关注  @{handle}  {item['name']}".rstrip()
            self.module_list.insert(tk.END, label)

        for row in self.twitter_history_rows:
            target = self._history_row_target(row)
            if not target:
                continue
            key = target.lower()
            if key in seen:
                continue
            seen.add(key)
            handle = str(row.get("handle") or "").strip().lstrip("@")
            name = str(row.get("name") or row.get("target") or target).strip()
            item = {
                "module_id": "twitter",
                "source": "历史",
                "target": target,
                "handle": handle,
                "name": name,
                "bio": str(row.get("bio") or ""),
                "profile_url": row.get("profile_url") or (f"https://x.com/{handle}" if handle else ""),
                "avatar_url": row.get("avatar_url") or "",
                "avatar_path": row.get("avatar_path") or "",
                "links": row.get("links") or [],
                "skeb_links": row.get("skeb_links") or [],
            }
            self.target_browser_rows.append(item)
            handle_text = f"@{handle}" if handle else target
            self.module_list.insert(tk.END, f"历史  {handle_text}  {name}".rstrip())

        if not self.target_browser_rows:
            self.module_list.insert(tk.END, "暂无关注/历史目标")
            self._restore_target_browser_selection(scroll_fraction)
            self._write_text(self.module_detail, "本地还没有可浏览的推特目标。刷新关注列表、下载作者，或在右侧手动输入目标后点击预览。")
        elif not self._restore_target_browser_selection(scroll_fraction):
            self._write_text(self.module_detail, "选择左侧关注或历史目标，会填入右侧目标并显示到同一个主页预览区域。")

    def _show_platform_overview(self, adapter) -> None:
        stage_labels = {"ready": "可用", "alpha": "试验版", "planned": "规划中"}
        self.profile_block_key = None
        self.profile_avatar_image = None
        self.profile_avatar_label.configure(image="", text="平台")
        self.profile_name_var.set(adapter.display_name)
        self.profile_handle_var.set(stage_labels.get(adapter.info.stage, adapter.info.stage))
        self.profile_bio_var.set(adapter.info.description or "暂无平台说明")
        self.profile_url_var.set("")
        self._set_profile_links({})
        scopes = PLATFORM_CONTENT_SCOPES.get(adapter.module_id, ("目标",))
        lines = [
            f"当前平台: {adapter.display_name}",
            f"状态: {stage_labels.get(adapter.info.stage, adapter.info.stage)}",
            "",
            "说明:",
            adapter.info.description,
            "",
            "可用范围:",
            *[f"- {scope}" for scope in scopes],
        ]
        capabilities = [item for item in adapter.info.capabilities if item not in {"native"}]
        if capabilities:
            lines.extend(["", "功能标识:", *[f"- {item}" for item in capabilities]])
        if adapter.module_id == "google_image":
            lines.extend([
                "",
                "请在右侧“相似搜索”页选择图片。Google 返回来源候选，AI 概览不代表图片匹配；请打开候选页面核对。",
                "若提示图片已过期，请重新上传原图；若出现人机验证，请在浏览器完成验证后重试。",
            ])
        elif adapter.module_id == "twitter":
            lines.extend(["", "可在上方输入 @用户名查找；“在线”会刷新简介、头像和主页外链。"])
        self._write_text(self.module_detail, "\n".join(line for line in lines if line is not None))

    def _search_platform_targets(self) -> None:
        module_id = self.module_var.get()
        if module_id == "google_image":
            self.notebook.select(self.google_search_tab)
            self.status_var.set("请在相似搜索页选择本地图片")
            return
        search_mode = self.content_scope_var.get()
        query = self.platform_search_var.get().strip()
        if module_id == "jmcomic" and search_mode == "分类 / 排行" and not query:
            query = "全部"
        elif module_id == "jmcomic" and search_mode == "漫画收藏夹" and not query:
            query = str(self.storage.get_setting("jmcomic_favorite_username", "") or "").strip()
        elif module_id == "jmcomic" and search_mode in {
            "小说收藏夹", "追更连载", "漫画观看记录", "小说观看记录",
        } and not query:
            query = str(self.storage.get_setting("jmcomic_favorite_username", "") or "").strip()
        elif module_id == "pixiv" and search_mode in {"作品收藏", "小说收藏", "浏览历史（Premium）"}:
            query = str(self.manager.get_adapter("pixiv").cookie_account_id() or "").strip()
            if not query:
                messagebox.showwarning("无法识别 Pixiv 账号", "请重新导入包含 PHPSESSID 的 Cookie，再更新自己的收藏或浏览历史。")
                return
        if not query:
            messagebox.showwarning("缺少搜索内容", "请输入 @用户名、关键词或目标 URL")
            return
        if module_id == "website":
            self.target_var.set(query)
            self._preview_target()
            return
        if "规划" in search_mode:
            messagebox.showinfo("搜索种类尚未迁入", f"{search_mode} 来自旧版功能清单，目前还没有迁入软件内置爬虫。")
            return
        direct_modes = {
            "twitter": {"帖子 / 媒体 URL"},
            "pixiv": {"作品 ID", "作者 ID", "漫画系列", "小说 ID", "作者小说", "小说系列", "FANBOX", "Sketch"},
            "jmcomic": {"漫画 ID / 链接", "章节 ID / 链接"},
            "bluesky": {"帖子 / 媒体"},
            "instagram": {"账号 / 用户名", "帖子 / 链接", "Reels / 链接"},
            "ehentai": {"画廊链接 / ID"},
        }
        if search_mode in direct_modes.get(module_id, set()):
            self.target_var.set(query)
            self._preview_target()
            return
        adapter = self._selected_adapter()
        output_dir = self.output_dir_var.get()
        if "search" not in adapter.info.capabilities:
            messagebox.showinfo("当前平台", f"{adapter.display_name} 暂不支持目标搜索")
            return
        self.platform_search_button.state(["disabled"])
        if hasattr(self, "candidate_refresh_button"):
            self.candidate_refresh_button.state(["disabled"])
            if module_id == "pixiv" and search_mode in {"作品收藏", "小说收藏"}:
                self.candidate_refresh_var.set("正在更新我的收藏…")
        self.status_var.set(f"正在查找 {adapter.display_name} 目标")

        def worker() -> None:
            try:
                options = self._current_target_options(query)
                options["output_dir"] = output_dir
                result_limit = 20
                if module_id == "pixiv":
                    result_limit = int(options.get("history_limit") or options.get("max_works") or 20)
                if module_id == "pixiv" and search_mode in {"作品收藏", "小说收藏"}:
                    result_limit = 10000
                    rows = adapter.refresh_own_bookmarks(
                        "novel" if search_mode == "小说收藏" else "work",
                        limit=result_limit,
                        options=options,
                    )
                else:
                    rows = adapter.search_targets(query, limit=result_limit, options=options)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("platform_search_error", (module_id, str(exc))))
                return
            self.ui_queue.put(("platform_search_loaded", (module_id, query, rows)))

        threading.Thread(target=worker, name=f"{module_id}-target-search", daemon=True).start()

    def _show_platform_search_results(self, module_id: str, query: str, rows: list[dict]) -> None:
        if self.module_var.get() != module_id:
            return
        self.platform_candidate_rows[module_id] = list(rows)
        self.platform_candidate_scope[module_id] = self.content_scope_var.get()
        self.selected_target_browser_key = ""
        self.selected_target_browser_keys.clear()
        self.target_selection_var.set("尚未选择左侧目标")
        self.target_browser_rows = []
        self.module_list.delete(0, tk.END)
        blocked_accounts = self.manager.blocklist.blocked_accounts()
        blocked_works = self.manager.blocklist.blocked_works()
        display_rows = [
            row for row in rows
            if not self.manager.blocklist.is_blocked(
                module_id, str(row.get("url") or row.get("target") or ""),
                input_kind=str(row.get("input_kind") or ""),
                author_id=str(row.get("author_id") or ""),
                blocked_accounts=blocked_accounts,
                blocked_works=blocked_works,
            )
        ][:1000]
        for row in display_rows:
            handle = str(row.get("handle") or "").strip().lstrip("@")
            target = f"@{handle}" if module_id == "twitter" and handle else str(
                row.get("url") or row.get("target") or row.get("id") or ""
            ).strip()
            if not target:
                continue
            item = {
                "module_id": module_id,
                "source": row.get("source") or "搜索",
                "target": target,
                "handle": handle,
                "name": row.get("title") or row.get("name") or handle or target,
                "bio": row.get("bio") or "",
                "profile_url": row.get("url") or "",
                "avatar_url": row.get("avatar_url") or row.get("thumbnail_url") or "",
                "input_kind": row.get("input_kind") or "",
                "author_id": row.get("author_id") or "",
                "author_name": row.get("author_name") or "",
                "published_at": row.get("published_at") or "",
                "tags": row.get("tags") or [],
                "restricted": bool(row.get("restricted")),
                "fee_required": int(row.get("fee_required") or 0),
                "downloadable": bool(row.get("downloadable", True)),
                "read_only_reason": row.get("read_only_reason") or "",
            }
            self.target_browser_rows.append(item)
            handle_text = f"@{handle}" if handle else target
            restricted = "  [付费内容，查看说明]" if item.get("restricted") else ""
            self.module_list.insert(tk.END, f"搜索  {handle_text}  {item['name']}{restricted}")
        if not self.target_browser_rows:
            self.module_list.insert(tk.END, f"没有找到“{query}”")
        self._write_text(
            self.module_detail,
            f"{self._selected_adapter().display_name} 搜索“{query}”：共 {len(rows)} 个结果，"
            f"左侧显示 {len(self.target_browser_rows)} 个。收藏可在候选页按字段筛选。",
        )
        self._refresh_platform_tabs()

    def _write_target_browser_detail(self, row: dict) -> None:
        target = str(row.get("target") or "").strip()
        lines = [
            f"来源: {row.get('source') or '-'}",
            f"目标: {row.get('target') or '-'}",
        ]
        handle = str(row.get("handle") or "").strip()
        if handle:
            lines.append(f"账号: @{handle}")
        name = str(row.get("name") or "").strip()
        if name:
            lines.append(f"名称: {name}")
        author_name = str(row.get("author_name") or "").strip()
        author_id = str(row.get("author_id") or "").strip()
        if author_name or author_id:
            lines.append(f"作者: {author_name or '-'}" + (f"（{author_id}）" if author_id else ""))
        published_at = str(row.get("published_at") or "").strip()
        if published_at:
            lines.append(f"发布: {published_at}")
        tags = [str(tag).strip() for tag in row.get("tags") or [] if str(tag).strip()]
        if tags:
            lines.append("标签: " + " · ".join(tags))
        bio = str(row.get("bio") or "").strip()
        if bio:
            lines.extend(["", bio])
        detail = str(row.get("detail") or "").strip()
        if detail and detail != bio:
            lines.extend(["", detail])
        profile_url = str(row.get("profile_url") or "").strip()
        if profile_url:
            lines.extend(["", profile_url])
        if row.get("restricted"):
            fee = int(row.get("fee_required") or 0)
            lines.extend(
                [
                    "",
                    "说明：",
                    "此内容属于 FANBOX 付费内容。",
                    f"所需方案：每月 {fee} JPY。" if fee else "所需方案：请查看创作者页面。",
                    "如当前无法访问，请检查 FANBOX 登录、订阅或续费状态。",
                ]
            )
        read_only_reason = str(row.get("read_only_reason") or "").strip()
        if read_only_reason:
            lines.extend(["", f"只读：{read_only_reason}"])
        self._write_text(self.module_detail, "\n".join(lines))
        self.profile_name_var.set(name or target or "目标预览")
        identifier = handle or str(row.get("author_id") or "").strip()
        self.profile_handle_var.set(f"@{identifier}" if self.module_var.get() == "twitter" and identifier else identifier or self.module_var.get())
        self.profile_bio_var.set(bio or str(row.get("detail") or row.get("source") or ""))
        self.profile_url_var.set(profile_url or target)
        author_id_for_block = str(row.get("author_id") or "").strip()
        self.profile_block_key = (
            ("pixiv", author_id_for_block) if self.module_var.get() == "pixiv" and author_id_for_block
            else account_from_url(profile_url or target)
        )
        self._set_profile_links({})
        avatar_source = str(row.get("avatar_path") or row.get("avatar_url") or "").strip()
        if avatar_source:
            self._load_profile_avatar(avatar_source, allow_remote=True)
        elif hasattr(self, "profile_avatar_label"):
            self.generic_avatar_request_id += 1
            self.generic_avatar_source = ""
            self.profile_avatar_image = None
            self.profile_avatar_label.configure(image="", text="头像")
    def _choose_output_dir(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir_var.get() or str(DEFAULT_OUTPUT_DIR))
        if selected:
            self.output_dir_var.set(selected)
            self._refresh_library()
            self._load_twitter_history()










    def _selected_types(self) -> str:
        return self.types_var.get().strip() or DEFAULT_TYPES_VALUE

    def _show_download_type_help(self) -> None:
        messagebox.showinfo(
            "下载类型说明",
            self.download_type_help_var.get()
            + "\n\nTwitter 类型编号：\n1 = 图片\n2 = 视频\n3 = GIF\n4 = 音频",
        )

    def _preview_target(self) -> None:
        target = self._single_target_for_action("本地预览")
        if not target:
            return
        module_id = self.module_var.get()
        if module_id != "twitter":
            self.generic_preview_request_id += 1
            self.online_button.configure(text="在线获取资料")
            self.online_button.state(["!disabled"])
        try:
            preview = self.manager.preview_target(module_id, target, self._current_target_options(target))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("预览失败", str(exc))
            return

        if module_id == "twitter":
            output_dir = self.output_dir_var.get()
            self.status_var.set("正在读取本地主页资料…")

            def worker() -> None:
                try:
                    adapter = self.manager.get_adapter("twitter")
                    payload = adapter.load_profile_preview(target, Path(output_dir))
                    payload["download_description"] = preview.description
                    payload["normalized_target"] = preview.normalized_target
                    payload["warnings"] = preview.warnings
                    payload["_requested_target"] = target
                    self.ui_queue.put(("profile_preview_local_loaded", payload))
                except Exception as exc:  # noqa: BLE001
                    self.ui_queue.put(("profile_preview_local_error", (target, str(exc))))

            threading.Thread(target=worker, name="twitter-local-preview", daemon=True).start()
            return

        self._render_generic_preview(preview)

    def _render_generic_preview(self, preview) -> None:
        metadata = preview.metadata
        author_id = str(metadata.get("author_id") or "").strip()
        self.profile_block_key = (
            ("pixiv", author_id)
            if preview.module_id == "pixiv" and author_id and metadata.get("input_kind") not in {"fanbox", "sketch"}
            else ("jmcomic_author", " ".join(str(metadata.get("author") or "").split()).casefold())
            if preview.module_id == "jmcomic" and metadata.get("author") and metadata.get("author") != "unknown"
            else account_from_url(preview.normalized_target)
        )
        description = self._decode_visible_text(preview.description)
        title = self._decode_visible_text(preview.title or preview.normalized_target or "目标预览")
        handle = self._target_to_handle(preview.raw_target)
        profile_handle = f"@{handle}" if handle else preview.module_id
        bio_parts = [description] if description else []
        if preview.module_id == "pixiv":
            kind = str(metadata.get("input_kind") or "")
            target_id = str(metadata.get("target_id") or metadata.get("author_id") or "").strip()
            author_name = self._decode_visible_text(metadata.get("author_name") or metadata.get("display_name") or "")
            author_id = str(metadata.get("author_id") or "").strip()
            if kind in {"user", "user_novels"}:
                profile_handle = f"Pixiv ID {author_id or target_id}".strip()
            elif kind in {"work", "novel"}:
                profile_handle = " · ".join(
                    part
                    for part in (
                        author_name,
                        f"Pixiv ID {author_id}" if author_id else f"作品 ID {target_id}",
                    )
                    if part
                )
            else:
                profile_handle = f"Pixiv · {kind or '目标'}" + (f" {target_id}" if target_id else "")
            tags = [
                self._decode_visible_text(tag).strip()
                for tag in metadata.get("tags") or []
                if str(tag).strip()
            ]
            if tags:
                bio_parts.append("标签：" + " · ".join(tags))
            published_at = str(metadata.get("published_at") or "").strip()
            if published_at:
                bio_parts.append(f"发布：{published_at}")
            if isinstance(metadata.get("search_results"), list):
                bio_parts.append(f"候选：{len(metadata['search_results'])} 个")
        elif preview.module_id == "jmcomic":
            kind = str(metadata.get("input_kind") or "")
            target_id = str(metadata.get("target_id") or metadata.get("id") or "").strip()
            author_name = self._decode_visible_text(metadata.get("author") or "")
            profile_handle = " · ".join(part for part in (
                "JMComic 漫画" if kind == "album" else "JMComic 章节" if kind == "photo" else "JMComic 搜索",
                f"JM{target_id}" if target_id else "",
                f"作者 {author_name}" if author_name and author_name != "unknown" else "",
            ) if part)
            tags = [self._decode_visible_text(tag).strip() for tag in metadata.get("tags") or [] if str(tag).strip()]
            if tags:
                bio_parts.append("标签：" + " · ".join(tags))
            if metadata.get("chapters"):
                bio_parts.append(f"章节：{len(metadata['chapters'])} 个")
        self.profile_name_var.set(title)
        self.profile_handle_var.set(profile_handle)
        self.profile_bio_var.set("\n".join(part for part in bio_parts if part))
        self.profile_url_var.set(preview.normalized_target)
        self._set_profile_links(
            {
                "links": metadata.get("external_links") or metadata.get("links") or [],
                "skeb_links": metadata.get("skeb_links") or [],
            }
        )
        if hasattr(self, "profile_avatar_label"):
            avatar_source = str(
                metadata.get("avatar_path")
                 or metadata.get("avatar_url")
                 or metadata.get("profile_image_url")
                 or metadata.get("thumbnail_url")
                or ""
            ).strip()
            if avatar_source:
                self._load_profile_avatar(avatar_source, allow_remote=True, preview_metadata=metadata)
            else:
                self.generic_avatar_request_id += 1
                self.generic_avatar_source = ""
                self.profile_avatar_image = None
                self.profile_avatar_label.configure(image="", text="头像")
        lines = [
            f"模块: {preview.module_id}",
            f"状态: {preview.status}",
            f"目标: {preview.normalized_target}",
            description,
        ]
        if preview.warnings:
            lines.append("")
            lines.append("检查:")
            lines.extend(f"- {warning}" for warning in preview.warnings)
        metadata_lines = []
        for key, value in metadata.items():
            if key in {"page_html", "options"} or value is None or value == "" or value == [] or value == {}:
                continue
            if isinstance(value, (str, int, float, bool)):
                metadata_lines.append(f"- {key}: {value}")
        if metadata_lines:
            lines.extend(["", "抓取信息:", *metadata_lines])
        fallback = "\n".join(line for line in lines if line)
        page_html = str(metadata.get("page_html") or "")
        if page_html and hasattr(self.preview_text, "render_page_html"):
            self.preview_text.render_page_html(page_html, preview.normalized_target, fallback)
        else:
            self._write_text(self.preview_text, fallback)

    def _current_input_targets(self) -> list[str]:
        module_id = self.module_var.get()
        prefilled = getattr(self, "_prefilled_task_targets", None)
        current_value = self.target_var.get().strip()
        if prefilled and prefilled[0] == module_id and prefilled[1] == current_value:
            return list(prefilled[2])
        return split_task_targets(
            current_value,
            split_slashes=slash_separates_targets(module_id, self.content_scope_var.get()),
            module_id=module_id,
        )

    def _is_current_input_target(self, target: str, module_id: str = "") -> bool:
        if module_id and module_id != self.module_var.get():
            return False
        targets = self._current_input_targets()
        return bool(targets and targets[0].casefold() == str(target or "").strip().casefold())

    def _refresh_profile_target_hint(self, *_args) -> None:
        targets = self._current_input_targets()
        if len(targets) > 1:
            self.profile_target_hint_var.set(
                f"当前输入 {len(targets)} 个目标；资料区一次显示一个，预览操作使用第 1 个"
            )
        elif targets:
            self.profile_target_hint_var.set("资料区一次显示一个目标；多目标下载请点“开始任务”")
        else:
            self.profile_target_hint_var.set("资料区一次显示一个目标")

    def _single_target_for_action(self, action: str) -> str:
        targets = self._current_input_targets()
        if not targets:
            messagebox.showwarning("缺少目标", "请输入目标")
            return ""
        if len(targets) > 1:
            selected = targets[0]
            self.profile_target_hint_var.set(
                f"资料区显示第 1/{len(targets)} 个目标：{selected}"
            )
            self.status_var.set(f"{action}只处理第 1/{len(targets)} 个目标；其余目标仍留在输入框中")
            return selected
        self.profile_target_hint_var.set(f"资料区显示：{targets[0]}")
        return targets[0]

    @staticmethod
    def _decode_visible_text(value: object) -> str:
        text = str(value or "")
        for _ in range(3):
            decoded = html.unescape(text)
            if decoded == text:
                break
            text = decoded
        return text

    def _preview_online(self) -> None:
        if self.module_var.get() == "twitter":
            self._preview_twitter_page()
            return
        target = self._single_target_for_action("在线获取资料")
        if not target:
            return
        module_id = self.module_var.get()
        self.status_var.set("正在获取在线预览")

        def worker() -> None:
            try:
                preview = self.manager.preview_target(
                    module_id,
                    target,
                    {**self._current_target_options(target), "live": True},
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("generic_preview_error", (request_id, module_id, target, str(exc))))
                return
            self.ui_queue.put(("generic_preview_loaded", (request_id, module_id, target, preview)))

        self.generic_preview_request_id += 1
        request_id = self.generic_preview_request_id
        self.online_button.state(["disabled"])
        self.online_button.configure(text="资料获取中…")
        threading.Thread(target=worker, name=f"{module_id}-online-preview", daemon=True).start()

    def _batch_fetch_target_info(self) -> None:
        if self.batch_info_cancel_event is not None:
            if not self.batch_info_cancel_event.is_set():
                self._cancel_batch_info()
            return
        targets = self._current_input_targets()
        if not targets:
            messagebox.showwarning("缺少目标", "请输入一个或多个作者/作品目标")
            return
        if len(targets) > 50:
            messagebox.showwarning("目标过多", f"一次最多获取 50 个目标资料；当前识别到 {len(targets)} 个")
            return
        module_id = self.module_var.get()
        target_options = [(target, self._current_target_options(target)) for target in targets]
        self.batch_info_request_id += 1
        request_id = self.batch_info_request_id
        cancel_event = threading.Event()
        self.batch_info_cancel_event = cancel_event
        self.batch_info_button.state(["!disabled"])
        self.batch_info_button.configure(text="取消资料获取")
        self.profile_target_hint_var.set(f"正在批量获取 {len(targets)} 个目标；完成后可逐项查看")
        self.status_var.set(f"开始批量获取 {len(targets)} 个目标资料")

        def worker() -> None:
            rows: list[dict] = []
            try:
                adapter = self.manager.get_adapter(module_id)
                for index, (target, options) in enumerate(target_options, start=1):
                    if cancel_event.is_set():
                        break
                    try:
                        if module_id == "twitter" and "/status/" not in target.casefold():
                            payload = adapter.fetch_profile_preview(target, timeout_seconds=60)
                            row = self._twitter_batch_info_row(target, payload)
                        else:
                            batch_options = dict(options)
                            try:
                                batch_options["page_read_timeout"] = min(
                                    max(30, int(batch_options.get("page_read_timeout") or 60)), 60
                                )
                            except (TypeError, ValueError):
                                batch_options["page_read_timeout"] = 60
                            preview = adapter.preview_target(
                                target,
                                {**batch_options, "live": True},
                            )
                            row = self._generic_batch_info_row(target, preview)
                    except Exception as exc:  # noqa: BLE001
                        message = str(exc).splitlines()
                        row = {
                            "module_id": module_id,
                            "source": "批量资料",
                            "target": target,
                            "name": target,
                            "detail": f"获取失败：{(message[0] if message else type(exc).__name__)[:240]}",
                            "downloadable": False,
                            "_batch_result": True,
                            "_batch_error": True,
                        }
                    row["_batch_index"] = index
                    row["_batch_count"] = len(target_options)
                    rows.append(row)
                    self.ui_queue.put(("batch_info_progress", (request_id, module_id, index, len(target_options), target)))
            except Exception as exc:  # noqa: BLE001
                rows.append({
                    "module_id": module_id,
                    "source": "批量资料",
                    "target": "",
                    "name": "批量资料任务启动失败",
                    "detail": str(exc) or type(exc).__name__,
                    "downloadable": False,
                    "_batch_result": True,
                    "_batch_error": True,
                })
            finally:
                self.ui_queue.put(("batch_info_done", (request_id, module_id, rows, cancel_event.is_set())))

        try:
            threading.Thread(target=worker, name=f"{module_id}-batch-info", daemon=True).start()
        except RuntimeError as exc:
            self.batch_info_cancel_event = None
            self.batch_info_button.configure(text="批量获取资料")
            self.status_var.set(f"无法启动资料获取：{exc}")

    def _cancel_batch_info(self) -> None:
        event = self.batch_info_cancel_event
        if event is None or event.is_set():
            return
        event.set()
        self.batch_info_button.configure(text="正在停止…")
        self.batch_info_button.state(["disabled"])
        self.status_var.set("已请求停止批量获取；当前网络请求结束后停止")

    @staticmethod
    def _generic_batch_info_row(target: str, preview) -> dict:
        metadata = preview.metadata
        return {
            "module_id": preview.module_id,
            "source": "批量资料",
            "target": preview.normalized_target or target,
            "name": preview.title or target,
            "detail": preview.description or preview.status,
            "profile_url": str(metadata.get("profile_url") or preview.normalized_target or target),
            "author_name": str(metadata.get("author_name") or metadata.get("author") or ""),
            "author_id": str(metadata.get("author_id") or ""),
            "published_at": str(metadata.get("published_at") or ""),
            "tags": metadata.get("tags") or [],
            "avatar_url": str(
                metadata.get("avatar_url") or metadata.get("profile_image_url")
                or metadata.get("thumbnail_url") or ""
            ),
            "input_kind": str(metadata.get("input_kind") or ""),
            "downloadable": True,
            "_batch_result": True,
            "_batch_preview": TargetPreview(
                module_id=preview.module_id,
                raw_target=preview.raw_target,
                normalized_target=preview.normalized_target,
                title=str(preview.title or "")[:500],
                description=str(preview.description or "")[:2000],
                status=str(preview.status or "")[:100],
                warnings=[str(item)[:300] for item in preview.warnings[:20]],
                metadata={
                    key: ([str(item)[:200] for item in value[:20]
                           if isinstance(item, (str, int, float, bool))]
                          if key == "tags" and isinstance(value, list)
                          else [None] * min(len(value), 20)
                          if key in {"search_results", "chapters"} and isinstance(value, list)
                          else str(value)[:1000] if isinstance(value, (str, int, float, bool)) else None)
                    for key, value in metadata.items()
                    if key in {
                        "author_id", "author", "input_kind", "target_id", "id", "author_name",
                        "display_name", "tags", "published_at", "search_results", "chapters",
                        "avatar_url", "profile_image_url",
                        "thumbnail_url", "profile_url", "title", "browser_destination",
                    }
                },
            ),
        }

    @staticmethod
    def _twitter_batch_info_row(target: str, payload: dict) -> dict:
        handle = str(payload.get("handle") or "").strip().lstrip("@")
        compact_payload = {
            key: ([item[:1000] for item in value[:20] if isinstance(item, str)]
                  if isinstance(value, list) else str(value or "")[:2000])
            for key, value in payload.items()
            if key in {
                "handle", "display_name", "bio", "profile_url", "avatar_url", "avatar_path",
                "source", "media_url", "normalized_target", "download_description", "warnings",
                "links", "skeb_links",
            }
        }
        return {
            "module_id": "twitter",
            "source": "批量资料",
            "target": str(payload.get("profile_url") or target),
            "handle": handle,
            "name": str(payload.get("display_name") or handle or target),
            "bio": str(payload.get("bio") or ""),
            "profile_url": str(payload.get("profile_url") or target),
            "avatar_url": str(payload.get("avatar_url") or ""),
            "detail": str(payload.get("bio") or payload.get("source") or "Twitter 作者资料"),
            "downloadable": True,
            "_batch_result": True,
            "_batch_payload": compact_payload,
        }

    def _show_batch_info_results(self, module_id: str, rows: list[dict]) -> None:
        if self.module_var.get() != module_id:
            return
        self.target_browser_rows = list(rows)
        self.selected_target_browser_key = ""
        self.selected_target_browser_keys.clear()
        self.module_list.delete(0, tk.END)
        for row in self.target_browser_rows:
            index = int(row.get("_batch_index") or 0)
            name = str(row.get("name") or row.get("target") or "未知目标")
            target = str(row.get("target") or "")
            detail = str(row.get("detail") or "")
            state = "获取失败" if row.get("_batch_error") else "已获取"
            self.module_list.insert(tk.END, f"{index:02d}. {state} · {name} · {target}")
        try:
            adapter = self.manager.get_adapter(module_id)
            can_download = "download" in adapter.info.capabilities and adapter.info.stage != "planned"
        except KeyError:
            can_download = False
        downloadable = can_download and any(bool(row.get("downloadable")) for row in self.target_browser_rows)
        self.target_queue_button.state(["!disabled"] if downloadable else ["disabled"])
        self.target_selection_var.set(f"批量资料共 {len(rows)} 项 · 单击一项查看详情 · 可多选加入队列")
        self._write_text(
            self.module_detail,
            f"批量资料获取完成：成功 {sum(not row.get('_batch_error') for row in rows)} 项，"
            f"失败 {sum(bool(row.get('_batch_error')) for row in rows)} 项。\n"
            "单击左侧一项，在资料卡查看详细信息；勾选多项可直接加入下载队列。",
        )
        self.profile_target_hint_var.set(f"批量资料共 {len(rows)} 项；左侧资料卡一次显示一项")
        if self.target_browser_rows:
            self.module_list.selection_set(0)
            self._select_target_from_browser()

    def _open_target_in_browser(self) -> None:
        target = self._single_target_for_action("打开网页")
        if not target:
            return
        url = target
        browser_destination = "目标网页"
        if self.module_var.get() == "jmcomic":
            try:
                preview = self.manager.preview_target("jmcomic", target, self._current_target_options(target))
                url = preview.normalized_target
                browser_destination = str(preview.metadata.get("browser_destination") or browser_destination)
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("无法打开网页", str(exc))
                return
        elif self.module_var.get() == "twitter" and not target.startswith(("http://", "https://")):
            url = f"https://x.com/{target.lstrip('@')}"
        elif not target.startswith(("http://", "https://")):
            try:
                preview = self.manager.preview_target(
                    self.module_var.get(), target, self._current_target_options(target)
                )
                url = preview.normalized_target
            except Exception as exc:  # noqa: BLE001
                messagebox.showerror("无法打开网页", str(exc))
                return
        if not str(url).startswith(("http://", "https://")):
            messagebox.showwarning("不是网页地址", "当前目标没有可在浏览器中打开的 HTTP/HTTPS 地址")
            return
        if not webbrowser.open(str(url), new=2):
            messagebox.showerror("打开失败", str(url))
            return
        if self.module_var.get() == "jmcomic":
            self.status_var.set(f"已打开 JMComic {browser_destination}；登录页或验证页由站点按当前会话决定")
        else:
            self.status_var.set("已在系统浏览器打开目标网页")

    def _preview_twitter_page(self) -> None:
        target = self._single_target_for_action("在线获取资料")
        if not target:
            return
        self._select_twitter_module()
        output_dir = self.output_dir_var.get()
        types_text = self._selected_types()
        self.status_var.set("正在刷新推特主页")
        self._append_log(f"开始刷新推特主页: {target}")
        self.online_button.state(["disabled"])
        self.online_button.configure(text="资料获取中…")

        def worker() -> None:
            adapter = self.manager.get_adapter("twitter")
            try:
                local_payload = adapter.load_profile_preview(target, Path(output_dir))
                local_payload["download_description"] = f"下载类型: {types_text}"
                local_payload["_requested_target"] = target
                self.ui_queue.put(("profile_preview_local_loaded", local_payload))
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("profile_preview_local_error", (target, str(exc))))
            try:
                payload = adapter.fetch_profile_preview(target, timeout_seconds=60)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("profile_preview_error", (target, str(exc))))
                return
            payload["_requested_target"] = target
            self.ui_queue.put(("profile_preview_loaded", payload))

        threading.Thread(target=worker, name="twitter-profile-preview", daemon=True).start()

    def _write_profile_preview(self, payload: dict) -> None:
        handle = str(payload.get("handle", "")).strip().lstrip("@")
        self.profile_block_key = ("twitter", handle.casefold()) if handle else None
        display_name = str(payload.get("display_name", "")).strip() or handle or "推特主页"
        bio = str(payload.get("bio", "")).strip()
        profile_url = str(payload.get("profile_url", "")).strip()
        self.profile_name_var.set(display_name)
        self.profile_handle_var.set(f"@{handle}" if handle else "")
        self.profile_bio_var.set(bio or "暂无简介")
        self.profile_url_var.set(profile_url)
        avatar_path = str(payload.get("avatar_path") or "").strip()
        avatar_url = str(payload.get("avatar_url") or "").strip()
        if avatar_path:
            self._load_profile_avatar(avatar_path)
        elif avatar_url.startswith("https://") and handle:
            self._load_profile_avatar("")
            self.profile_avatar_label.configure(image="", text="头像加载中…")
            self._cache_profile_avatar(handle, avatar_url)
        else:
            self._load_profile_avatar("")
        self._set_profile_links(payload)

        source_labels = {
            "live_profile": "主页刷新",
            "cached_profile": "主页缓存",
            "following_list": "关注列表",
            "download_history": "下载历史",
            "input": "手动输入",
        }
        source = source_labels.get(str(payload.get("source") or "input"), str(payload.get("source") or "手动输入"))
        lines = [f"资料来源: {source}"]
        download_description = str(payload.get("download_description") or "").strip()
        if download_description:
            lines.append(download_description)
        media_url = str(payload.get("media_url") or payload.get("normalized_target") or "").strip()
        if media_url:
            lines.append(f"媒体页: {media_url}")

        skeb_links = payload.get("skeb_links", []) or []
        links = payload.get("links", []) or []
        other_links = [item for item in links if item not in skeb_links]
        lines.extend(["", "Skeb 链接:"])
        if skeb_links:
            lines.extend(f"- {self._link_label(item)}: {self._link_url(item)}" for item in skeb_links)
        else:
            lines.append("- 本地资料未发现；需要最新外链时点击“在线获取资料”")
        if other_links:
            lines.extend(["", "其他外链:"])
            lines.extend(f"- {self._link_label(item)}: {self._link_url(item)}" for item in other_links)
        if profile_url:
            lines.extend(["", f"主页: {profile_url}"])
        warnings = payload.get("warnings") or []
        if warnings:
            lines.extend(["", "检查:"])
            lines.extend(f"- {warning}" for warning in warnings)
        self._write_left_profile_preview(payload, source, "\n".join(lines))

    def _write_left_profile_preview(self, payload: dict, source: str, fallback_text: str = "") -> None:
        if hasattr(self.module_detail, "render_profile"):
            self.module_detail.render_profile(payload, source, fallback_text)
            return
        handle = str(payload.get("handle") or "").strip().lstrip("@")
        name = str(payload.get("display_name") or handle or "目标").strip()
        bio = str(payload.get("bio") or "").strip()
        profile_url = str(payload.get("profile_url") or "").strip()
        lines = [name]
        if handle:
            lines.append(f"@{handle}")
        lines.append(f"来源: {source}")
        if bio:
            lines.extend(["", bio])
        if profile_url:
            lines.extend(["", profile_url])
        self._write_text(self.module_detail, "\n".join(lines))

    def _link_label(self, item: object) -> str:
        if isinstance(item, dict):
            return str(item.get("label") or item.get("url") or "链接")
        return str(item or "链接")

    def _link_url(self, item: object) -> str:
        if isinstance(item, dict):
            return str(item.get("url") or item.get("label") or "")
        return str(item or "")

    def _set_profile_links(self, payload: dict) -> None:
        skeb_items = list(payload.get("skeb_links") or [])
        all_items = [*skeb_items, *(payload.get("links") or [])]
        normalized: list[dict] = []
        seen: set[str] = set()
        for item in all_items:
            url = self._link_url(item).strip()
            if not url.startswith(("http://", "https://")) or url.lower() in seen:
                continue
            seen.add(url.lower())
            label = self._link_label(item).strip() or url
            is_skeb = "skeb.jp" in f"{label} {url}".lower()
            normalized.append({"label": label, "url": url, "is_skeb": is_skeb})
        normalized.sort(key=lambda item: (not item["is_skeb"], item["label"].lower()))
        self.profile_links = normalized
        values = []
        for item in normalized:
            prefix = "Skeb" if item["is_skeb"] else "外链"
            label = item["label"]
            if label == item["url"] or len(label) > 38:
                label = urlparse(item["url"]).netloc or item["url"]
            values.append(f"{prefix} · {label}")
        self.profile_link_combo.configure(values=values)
        if values:
            self.profile_link_combo.current(0)
            skeb_count = sum(1 for item in normalized if item["is_skeb"])
            suffix = f" · Skeb {skeb_count}" if skeb_count else ""
            self.profile_link_summary_var.set(f"主页外链 {len(normalized)}{suffix}")
        else:
            self.profile_link_var.set("未获取到主页外链")
            self.profile_link_summary_var.set("主页外链 0")
        self._update_blocklist_status()

    def _update_blocklist_status(self) -> None:
        if not hasattr(self, "blocklist_status_var"):
            return
        try:
            blocked = self.manager.blocklist.blocked_accounts()
            blocked_works = self.manager.blocklist.blocked_works()
        except (OSError, ValueError) as exc:
            self.blocklist_status_var.set(f"黑名单读取失败：{exc}")
            return
        key = self.profile_block_key
        state = " · 当前作者已屏蔽" if key and key in blocked else ""
        jm_hint = " · JM 作者按名称匹配" if key and key[0] == "jmcomic_author" else ""
        self.blocklist_status_var.set(f"黑名单 {len(blocked)} 个账号、{len(blocked_works)} 个作品/页面{state}{jm_hint}")

    def _block_current_profile(self) -> None:
        primary = self.profile_block_key
        if not primary:
            messagebox.showinfo("屏蔽作者", "请先读取可识别的作者资料；作品或帖子外链不能单凭地址认定作者。")
            return
        linked = list(dict.fromkeys(
            key for item in self.profile_links
            if (key := account_from_url(str(item.get("url") or ""))) and key != primary
        ))[:100]
        selected_links: list[AccountKey] = []
        if linked:
            preview = "\n".join(f"- {platform}: {account}" for platform, account in linked[:8])
            if len(linked) > 8:
                preview += f"\n…另有 {len(linked) - 8} 个"
            decision = messagebox.askyesnocancel(
                "跨平台屏蔽范围",
                f"当前作者：{primary[0]}: {primary[1]}\n\n资料页找到这些外链账号：\n{preview}\n\n"
                "外链可能指向其他人。选“是”同时屏蔽这些账号；选“否”只屏蔽当前作者；取消则不修改。",
                default="no",
            )
            if decision is None:
                return
            if decision:
                selected_links = linked
        try:
            self.manager.blocklist.add_group(primary, selected_links, label=self.profile_name_var.get())
        except (OSError, ValueError) as exc:
            messagebox.showerror("屏蔽失败", str(exc))
            return
        self._update_blocklist_status()
        self._refresh_platform_tabs()
        self.status_var.set(f"已屏蔽 {primary[0]}: {primary[1]}" + (f" 及 {len(selected_links)} 个外链账号" if selected_links else ""))

        self._refresh_google_blocklist()

    def _block_current_work(self) -> None:
        targets = [self.profile_url_var.get().strip(), self.target_var.get().strip()]
        target = next((value for value in targets if work_from_target(self.module_var.get(), value)), "")
        input_targets = self._current_input_targets()
        if len(input_targets) > 1 and (not target or target == self.target_var.get().strip()):
            target = input_targets[0]
        if not target:
            messagebox.showinfo("屏蔽作品/页面", "请先选中具体作品、帖子或网页；作者主页请使用“屏蔽作者”。")
            return
        if not messagebox.askyesno("屏蔽作品/页面", f"屏蔽这个具体地址？\n{target}"):
            return
        try:
            self.manager.blocklist.add_work(self.module_var.get(), target)
        except (OSError, ValueError) as exc:
            messagebox.showerror("屏蔽失败", str(exc))
            return
        self._update_blocklist_status()
        self._refresh_platform_tabs()
        self._refresh_google_blocklist()
        self.status_var.set(f"已屏蔽作品/页面：{target}")

    def _manage_blocklist(self) -> None:
        window = tk.Toplevel(self)
        window.title("跨平台黑名单")
        window.geometry("960x640")
        window.minsize(860, 560)
        window.transient(self)
        body = ttk.Frame(window, padding=12)
        body.pack(fill="both", expand=True)
        explanation = ttk.Label(body, text="作者按精确账号屏蔽，作品按单个 ID 或具体页面屏蔽；可只读取回平台已有规则，合并后保存在本地，不会修改网站账号。",
                                wraplength=820)
        explanation.pack(anchor="w", pady=(0, 8))
        filters = ttk.Frame(body)
        filters.pack(fill="x", pady=(0, 8))
        platform_var = tk.StringVar(value="全部平台")
        query_var = tk.StringVar()
        ttk.Label(filters, text="平台").pack(side="left")
        ttk.Combobox(filters, textvariable=platform_var, state="readonly", width=14,
                     values=("全部平台", "twitter", "twitter_id", "pixiv", "pixiv_novel", "fanbox", "sketch", "bluesky", "instagram", "ehentai", "jmcomic_author", "jmcomic_tag", "jmcomic", "jmcomic_chapter", "jmcomic_novel", "url")).pack(side="left", padx=(6, 12))
        ttk.Label(filters, text="查找").pack(side="left")
        ttk.Entry(filters, textvariable=query_var, width=32).pack(side="left", padx=6)
        tree = ttk.Treeview(body, columns=("kind", "label", "source", "accounts"), show="headings", selectmode="browse")
        tree.heading("kind", text="类型")
        tree.column("kind", width=78, stretch=False)
        tree.heading("label", text="名称")
        tree.column("label", width=190, stretch=False)
        tree.heading("source", text="来源")
        tree.column("source", width=120, stretch=False)
        tree.heading("accounts", text="屏蔽账号")
        tree.column("accounts", width=480)
        tree.pack(fill="both", expand=True)
        summary_var = tk.StringVar()
        ttk.Label(body, textvariable=summary_var, style="Small.TLabel").pack(anchor="w", pady=(5, 0))
        selected_detail_var = tk.StringVar(value="选中规则可查看来源与关联账号；跨平台关联请确认是同一作者。")
        ttk.Label(body, textvariable=selected_detail_var, style="Small.TLabel", wraplength=820).pack(
            fill="x", pady=(2, 0))

        def show_selected_detail(_event=None) -> None:
            selected = tree.selection()
            if not selected:
                selected_detail_var.set("选中规则可查看来源与关联账号；跨平台关联请确认是同一作者。")
                return
            kind, rule_id = selected[0].split(":", 1)
            if kind == "group":
                group = next((item for item in self.manager.blocklist.groups() if item.get("id") == rule_id), None)
                if group:
                    primary = group.get("primary") or {}
                    source = str(group.get("source") or "本地规则")
                    accounts = {item.get("platform") for item in group.get("accounts") or [] if isinstance(item, dict)}
                    hint = " · 仅有数字 ID 时，先核对主页，再绑定 @用户名。" if "twitter_id" in accounts and "twitter" not in accounts else ""
                    selected_detail_var.set(f"来源：{source} · 主账号：{primary.get('platform')}:{primary.get('account')}{hint}")
            else:
                work = next((item for item in self.manager.blocklist.works() if item.get("id") == rule_id), None)
                if work:
                    selected_detail_var.set(f"单项屏蔽：{work.get('platform')}:{work.get('work')} · 不影响同作者其他作品")

        tree.bind("<<TreeviewSelect>>", show_selected_detail)

        def refresh() -> None:
            for item_id in tree.get_children():
                tree.delete(item_id)
            try:
                groups = self.manager.blocklist.groups()
                works = self.manager.blocklist.works()
            except (OSError, ValueError) as exc:
                messagebox.showerror("黑名单读取失败", str(exc), parent=window)
                return
            visible = 0
            pending_x_ids = sum(
                any(item.get("platform") == "twitter_id" for item in group.get("accounts") or [] if isinstance(item, dict)) and not any(
                    item.get("platform") == "twitter" for item in group.get("accounts") or [] if isinstance(item, dict)
                ) for group in groups
            )
            for group in groups:
                accounts = ", ".join(
                    f"{item.get('platform')}: {item.get('account')}"
                    for item in group.get("accounts") or [] if isinstance(item, dict)
                )
                if platform_var.get() != "全部平台" and not any(
                    (item.get("platform") == platform_var.get() or (platform_var.get() == "twitter" and item.get("platform") == "twitter_id"))
                    for item in group.get("accounts") or [] if isinstance(item, dict)
                ):
                    continue
                if query_var.get().casefold() not in f"{group.get('label') or ''} {group.get('source') or ''} {accounts}".casefold():
                    continue
                account_platforms = {item.get("platform") for item in group.get("accounts") or [] if isinstance(item, dict)}
                kind_label = (
                    "待匹配 ID" if "twitter_id" in account_platforms and "twitter" not in account_platforms
                    else "标签" if "jmcomic_tag" in account_platforms
                    else "作者"
                )
                tree.insert("", tk.END, iid=f"group:{group.get('id')}",
                            values=(kind_label, group.get("label") or "", group.get("source") or "本地规则", accounts))
                visible += 1
            for work in works:
                platform = str(work.get("platform") or "")
                detail = f"{platform}: {work.get('work') or ''}"
                if platform_var.get() != "全部平台" and platform_var.get() != platform:
                    continue
                if query_var.get().casefold() not in f"{work.get('label') or ''} {work.get('source') or ''} {detail}".casefold():
                    continue
                tree.insert("", tk.END, iid=f"work:{work.get('id')}",
                            values=("作品", work.get("label") or "", work.get("source") or "本地规则", detail))
                visible += 1
            summary_var.set(f"显示 {visible} / 共 {len(groups) + len(works)} 条 · 作者 {len(groups)} · 作品/页面 {len(works)} · 待绑定 X ID {pending_x_ids}")
            show_selected_detail()

        platform_var.trace_add("write", lambda *_: refresh())
        query_var.trace_add("write", lambda *_: refresh())

        def remove_selected() -> None:
            selected = tree.selection()
            if not selected:
                return
            if not messagebox.askyesno("移除屏蔽", "移除选中的黑名单规则？", parent=window):
                return
            try:
                kind, rule_id = selected[0].split(":", 1)
                if kind == "group":
                    self.manager.blocklist.remove_group(rule_id)
                else:
                    self.manager.blocklist.remove_work(rule_id)
            except (OSError, ValueError) as exc:
                messagebox.showerror("移除失败", str(exc), parent=window)
                return
            refresh()
            self._update_blocklist_status()
            self._refresh_platform_tabs()
            self._refresh_google_blocklist()

        def changed() -> None:
            refresh()
            self._update_blocklist_status()
            self._refresh_platform_tabs()
            self._refresh_google_blocklist()

        def add_author() -> None:
            platform = platform_var.get()
            if platform in {
                "全部平台", "url", "pixiv_novel", "twitter_id",
                "jmcomic", "jmcomic_chapter", "jmcomic_novel",
            }:
                messagebox.showinfo("选择平台", "请先选择具体的作者平台。", parent=window)
                return
            title = "添加标签屏蔽" if platform == "jmcomic_tag" else "添加作者屏蔽"
            prompt = "输入要屏蔽的 JMComic 标签：" if platform == "jmcomic_tag" else f"输入 {platform} 作者账号或 ID："
            value = simpledialog.askstring(title, prompt, parent=window)
            if not value:
                return
            key = account_from_url(value) if value.startswith(("http://", "https://")) else (platform, value.lstrip("@").strip().casefold())
            if not key or key[0] != platform or not key[1]:
                messagebox.showerror("作者账号无效", "请输入当前平台的作者账号或主页地址。", parent=window)
                return
            try:
                self.manager.blocklist.add_group(key)
            except (OSError, ValueError) as exc:
                messagebox.showerror("添加失败", str(exc), parent=window)
                return
            changed()

        def add_work() -> None:
            platform = platform_var.get()
            prompt = (
                "输入 JMComic 漫画 ID 或漫画详情 URL：" if platform == "jmcomic"
                else "输入 JMComic 章节 ID 或正文 URL：" if platform == "jmcomic_chapter"
                else "输入 JMComic 小说 ID 或详情 URL：" if platform == "jmcomic_novel"
                else "输入 EH 画廊 ID/令牌或完整画廊 URL：" if platform == "ehentai"
                else "输入具体作品、帖子或网页 URL："
            )
            value = simpledialog.askstring("添加作品屏蔽", prompt, parent=window)
            if not value:
                return
            try:
                self.manager.blocklist.add_work(platform_var.get(), value)
            except (OSError, ValueError) as exc:
                messagebox.showerror("添加失败", str(exc), parent=window)
                return
            changed()

        def edit_selected() -> None:
            selected = tree.selection()
            if not selected:
                return
            kind, rule_id = selected[0].split(":", 1)
            try:
                if kind == "group":
                    item = next(group for group in self.manager.blocklist.groups() if group.get("id") == rule_id)
                    label = simpledialog.askstring("编辑作者规则", "显示名称：", initialvalue=item.get("label") or "", parent=window)
                    if label is None:
                        return
                    old = "\n".join(f"{entry.get('platform')}:{entry.get('account')}" for entry in item.get("accounts") or [])
                    raw = simpledialog.askstring("编辑关联账号", "每行一个“平台:账号”；只保留确认属于同一作者的账号：",
                                                 initialvalue=old, parent=window)
                    if raw is None:
                        return
                    accounts = []
                    for line in raw.splitlines():
                        platform, separator, account = line.strip().partition(":")
                        if not separator or not platform or not account:
                            raise ValueError("关联账号应为每行“平台:账号”")
                        accounts.append((platform, account))
                    self.manager.blocklist.update_group(rule_id, label=label, accounts=accounts)
                else:
                    item = next(work for work in self.manager.blocklist.works() if work.get("id") == rule_id)
                    label = simpledialog.askstring("编辑作品规则", "显示名称：", initialvalue=item.get("label") or "", parent=window)
                    if label is None:
                        return
                    target = simpledialog.askstring("编辑作品地址", "新的具体 URL；留空则保留当前作品：",
                                                    initialvalue=item.get("work") if item.get("platform") == "url" else "", parent=window)
                    if target is None:
                        return
                    self.manager.blocklist.update_work(rule_id, label=label, module_id=str(item.get("platform") or ""), target=target)
            except (OSError, ValueError, StopIteration) as exc:
                messagebox.showerror("修改失败", str(exc), parent=window)
                return
            changed()

        def bind_x_handle() -> None:
            selected = tree.selection()
            if not selected or not selected[0].startswith("group:"):
                messagebox.showinfo("绑定 X 用户名", "请先选中从 X 归档取回的数字账号 ID。", parent=window)
                return
            rule_id = selected[0].split(":", 1)[1]
            group = next((item for item in self.manager.blocklist.groups() if item.get("id") == rule_id), None)
            account_id = next((str(item.get("account") or "") for item in (group or {}).get("accounts") or []
                               if isinstance(item, dict) and item.get("platform") == "twitter_id"), "")
            if not account_id:
                messagebox.showinfo("绑定 X 用户名", "请选择数字账号 ID 规则。", parent=window)
                return
            webbrowser.open(f"https://x.com/intent/user?user_id={account_id}")
            handle = simpledialog.askstring(
                "绑定 X 用户名", f"已打开账号 ID {account_id} 的主页。确认用户名后输入 @用户名：",
                parent=window,
            )
            if handle is None:
                return
            handle = handle.strip()
            key = account_from_url(f"https://x.com/{handle.lstrip('@')}") if handle.startswith("@") else None
            if not key or key[0] != "twitter":
                messagebox.showerror("绑定失败", "请输入有效的 @用户名，并确认它属于刚打开的账号。", parent=window)
                return
            accounts = [(str(item.get("platform") or ""), str(item.get("account") or ""))
                        for item in group.get("accounts") or [] if isinstance(item, dict)]
            try:
                self.manager.blocklist.update_group(rule_id, label=str(group.get("label") or ""),
                                                    accounts=[*accounts, key])
            except (OSError, ValueError) as exc:
                messagebox.showerror("绑定失败", str(exc), parent=window)
                return
            changed()

        def import_platform() -> None:
            platform = platform_var.get()
            if platform == "全部平台":
                messagebox.showinfo("选择平台", "请先选择要读取名单的平台。", parent=window)
                return
            source = filedialog.askopenfilename(parent=window, title=f"读取 {platform} 黑名单",
                                               filetypes=[("本软件 JSON 或旧作者文本名单", "*.json *.txt"), ("所有文件", "*.*")])
            if not source:
                return
            try:
                count = self.manager.blocklist.import_platform(platform, source)
            except (OSError, ValueError) as exc:
                messagebox.showerror("读取失败", str(exc), parent=window)
                return
            changed()
            messagebox.showinfo("读取完成", f"已新增 {count} 条 {platform} 规则。", parent=window)

        def export_platform() -> None:
            platform = platform_var.get()
            if platform == "全部平台":
                messagebox.showinfo("选择平台", "请先选择要保存名单的平台。", parent=window)
                return
            destination = filedialog.asksaveasfilename(parent=window, title=f"保存 {platform} 黑名单",
                                                       defaultextension=".json", initialfile=f"{platform}-blocklist.json",
                                                       filetypes=[("JSON", "*.json")])
            if not destination:
                return
            try:
                count = self.manager.blocklist.export_platform(platform, destination)
            except (OSError, ValueError) as exc:
                messagebox.showerror("保存失败", str(exc), parent=window)
                return
            messagebox.showinfo("保存完成", f"已保存 {count} 条规则。", parent=window)

        def review_recovered(records: list[dict], title: str) -> None:
            if not window.winfo_exists():
                return
            if not records:
                source_status_var.set("来源中没有可读取的作者账号")
                messagebox.showinfo(title, "来源中没有可读取的作者账号。", parent=window)
                return
            source_counts = Counter(str(item.get("source") or "未知来源") for item in records)
            existing = self.manager.blocklist.blocked_accounts()
            new_count = len({tuple(item["primary"]) for item in records if tuple(item["primary"]) not in existing})
            summary = "\n".join(f"{name}：{count} 条" for name, count in source_counts.items())
            sample = "\n".join(f"- {item['primary'][0]}:{item['primary'][1]}" for item in records[:5])
            notice = "\n\nX 数字账号 ID 在媒体响应包含作者 ID 时生效；仅有 @用户名 的页面仍需补充用户名规则。" if any(
                item["primary"][0] == "twitter_id" for item in records) else ""
            if not messagebox.askyesno(title, f"读取到 {len(records)} 条，预计新增 {new_count} 条。\n{summary}\n\n示例：\n{sample}{notice}\n\n合并到本地黑名单？", parent=window):
                source_status_var.set("已取消合并，黑名单未修改")
                return
            try:
                added = self.manager.blocklist.merge_account_records(records)
            except (OSError, ValueError) as exc:
                messagebox.showerror("导入失败", str(exc), parent=window)
                return
            changed()
            source_status_var.set(f"已取回 {len(records)} 条，新增 {added} 个作者规则")
            messagebox.showinfo(title, f"已新增 {added} 个作者规则；原有规则保留。", parent=window)

        def recover_x_archive() -> None:
            source = filedialog.askopenfilename(parent=window, title="选择 X 数据归档或 block.js / mute.js",
                                               filetypes=[("X 归档或记录", "*.zip *.js"), ("所有文件", "*.*")])
            if not source:
                return
            source_status_var.set("正在读取 X 数据归档…")
            try:
                records = read_x_archive(source)
            except (OSError, ValueError, json.JSONDecodeError, zipfile.BadZipFile) as exc:
                source_status_var.set("X 数据归档读取失败")
                messagebox.showerror("X 归档读取失败", str(exc), parent=window)
                return
            review_recovered(records, "取回 X 拉黑/静音")

        def recover_pixiv_settings() -> None:
            source = filedialog.askopenfilename(parent=window, title="选择从 Pixiv 设置页保存的 HTML",
                                               filetypes=[("网页 HTML", "*.html *.htm"), ("所有文件", "*.*")])
            if not source:
                return
            source_status_var.set("正在读取 Pixiv 设置页…")
            try:
                records = read_pixiv_settings_html(source)
                account_id = self.manager.get_adapter("pixiv").cookie_account_id()
                records = [item for item in records if item["primary"][1] != account_id]
            except (OSError, ValueError, UnicodeError) as exc:
                source_status_var.set("Pixiv 设置页读取失败")
                messagebox.showerror("Pixiv 设置页读取失败", str(exc), parent=window)
                return
            review_recovered(records, "取回 Pixiv 静音/屏蔽")

        def recover_pixiv_live() -> None:
            adapter = self.manager.get_adapter("pixiv")
            cookie_file = adapter.runtime_data_dir / "cookies.json"
            own_account_id = adapter.cookie_account_id()
            proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
            source_status_var.set("正在读取 Pixiv 在线静音名单…")
            def worker() -> None:
                try:
                    records = fetch_pixiv_mutes(cookie_file, proxy_url)
                    records = [item for item in records if item["primary"][1] != own_account_id]
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda error=str(exc): (
                        source_status_var.set("Pixiv 在线读取失败"),
                        messagebox.showerror("Pixiv 在线读取失败", error, parent=window) if window.winfo_exists() else None
                    ))
                    return
                self.after(0, lambda: (source_status_var.set("Pixiv 在线读取完成"),
                                       review_recovered(records, "取回 Pixiv 在线静音")))
            threading.Thread(target=worker, name="pixiv-mutes-read", daemon=True).start()

        def recover_bluesky() -> None:
            identifier = simpledialog.askstring("读取 Bluesky 已屏蔽账号", "Bluesky 用户名（如 name.bsky.social）：", parent=window)
            if not identifier:
                return
            app_password = simpledialog.askstring("读取 Bluesky 已屏蔽账号", "应用密码（仅用于这次读取，不保存）：",
                                                  show="*", parent=window)
            if not app_password:
                return
            include_mutes = bool(bluesky_mutes_var.get())
            source_status_var.set("正在读取 Bluesky 拉黑/静音名单…")
            def worker() -> None:
                try:
                    records = fetch_bluesky_moderation(identifier, app_password, include_mutes=include_mutes)
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda error=str(exc): (
                        source_status_var.set("Bluesky 读取失败"),
                        messagebox.showerror("Bluesky 读取失败", error, parent=window) if window.winfo_exists() else None
                    ))
                    return
                self.after(0, lambda: (source_status_var.set("Bluesky 读取完成"),
                                       review_recovered(records, "取回 Bluesky 拉黑/静音")))
            threading.Thread(target=worker, name="bluesky-blocklist-read", daemon=True).start()

        def recover_jmcomic_blocks() -> None:
            adapter = self.manager.get_adapter("jmcomic")
            cookie_file = adapter.runtime_data_dir / "cookies.json"
            domain = str(self.storage.get_setting("jmcomic_domain", "https://18comic.vip") or "https://18comic.vip")
            username = str(self.storage.get_setting("jmcomic_favorite_username", "") or "")
            proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
            user_agent = str(self.storage.get_setting("jmcomic_user_agent", "") or "")
            source_status_var.set("正在读取 JMComic 站内屏蔽标签…")

            def worker() -> None:
                try:
                    records = fetch_jmcomic_tag_blocks(
                        domain, username, cookie_file, proxy_url, user_agent, adapter.browser_headers()
                    )
                except Exception as exc:  # noqa: BLE001
                    self.after(0, lambda error=str(exc): (
                        source_status_var.set("JMComic 站内屏蔽读取失败"),
                        messagebox.showerror("JMComic 屏蔽读取失败", error, parent=window) if window.winfo_exists() else None,
                    ))
                    return
                self.after(0, lambda: (
                    source_status_var.set("JMComic 站内屏蔽读取完成"),
                    review_recovered(records, "取回 JMComic 站内屏蔽标签"),
                ))

            threading.Thread(target=worker, name="jmcomic-blocklist-read", daemon=True).start()

        actions = ttk.Frame(body)
        actions.pack(fill="x", pady=(8, 0))
        ttk.Button(actions, text="添加作者", command=add_author).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="添加作品", command=add_work).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="修改选中", command=edit_selected).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="核对并绑定 X 用户名", command=bind_x_handle).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="读取平台名单", command=import_platform).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="保存平台名单", command=export_platform).pack(side="left", padx=(0, 5))
        ttk.Button(actions, text="移除选中", command=remove_selected).pack(side="left")
        ttk.Button(actions, text="关闭", command=window.destroy).pack(side="right")
        recovery = ttk.Frame(body)
        recovery.pack(fill="x", pady=(8, 0))
        ttk.Label(recovery, text="取回旧记录：").pack(side="left")
        ttk.Button(recovery, text="X 数据归档", command=recover_x_archive).pack(side="left", padx=(5, 5))
        ttk.Button(recovery, text="Pixiv 在线静音", command=recover_pixiv_live).pack(side="left", padx=(0, 5))
        ttk.Button(recovery, text="Pixiv 设置页 HTML", command=recover_pixiv_settings).pack(side="left", padx=(0, 5))
        ttk.Button(recovery, text="Bluesky 账号", command=recover_bluesky).pack(side="left", padx=(0, 8))
        bluesky_mutes_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(recovery, text="含静音", variable=bluesky_mutes_var).pack(side="left")
        jm_recovery = ttk.Frame(body)
        jm_recovery.pack(fill="x", pady=(4, 0))
        ttk.Label(jm_recovery, text="JMComic：").pack(side="left")
        ttk.Button(jm_recovery, text="取回站内屏蔽标签", command=recover_jmcomic_blocks).pack(side="left", padx=(5, 0))
        source_status_var = tk.StringVar(value="")
        ttk.Label(body, textvariable=source_status_var, style="Small.TLabel").pack(anchor="w", pady=(4, 0))
        refresh()

    def _selected_profile_link(self) -> dict | None:
        index = self.profile_link_combo.current()
        if index < 0 or index >= len(self.profile_links):
            return None
        return self.profile_links[index]

    def _open_selected_profile_link(self) -> None:
        item = self._selected_profile_link()
        if not item:
            messagebox.showinfo("主页外链", "当前资料没有可打开的主页外链；点击“在线获取资料”重新抓取主页。")
            return
        if webbrowser.open(item["url"], new=2):
            self.status_var.set(f"已打开外链: {item['url']}")
        else:
            messagebox.showerror("打开失败", item["url"])

    def _copy_selected_profile_link(self) -> None:
        item = self._selected_profile_link()
        if not item:
            messagebox.showinfo("主页外链", "当前资料没有可复制的主页外链。")
            return
        self._copy_to_clipboard(item["url"])
        self.status_var.set("主页外链已复制")

    def _copy_profile_bio(self) -> None:
        bio = self.profile_bio_var.get().strip()
        if not bio or bio == "暂无简介":
            messagebox.showinfo("复制简介", "当前资料没有简介。")
            return
        self._copy_to_clipboard(bio)
        self.status_var.set("主页简介已复制")

    def _copy_to_clipboard(self, value: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()

    def _load_profile_avatar(
        self, source: str, *, allow_remote: bool = False, preview_metadata: dict | None = None
    ) -> None:
        self.generic_avatar_request_id += 1
        request_id = self.generic_avatar_request_id
        self.generic_avatar_source = source
        self.profile_avatar_image = None
        if not source:
            self.profile_avatar_label.configure(image="", text="头像")
            return
        if Image is None or ImageTk is None:
            self.profile_avatar_label.configure(image="", text="头像")
            return
        try:
            local_path = Path(source).expanduser()
            if local_path.is_file():
                image = Image.open(local_path)
            elif allow_remote and source.startswith(("http://", "https://")):
                self.profile_avatar_label.configure(image="", text="头像加载中…")

                def worker() -> None:
                    try:
                        if self.module_var.get() == "jmcomic" and preview_metadata is not None:
                            photo_id = ""
                            if source == preview_metadata.get("first_page_url"):
                                photo_id = str(preview_metadata.get("first_page_photo_id") or "")
                            data = self.manager.get_adapter("jmcomic").fetch_preview_image_bytes(
                                source,
                                {"domain": str(self.storage.get_setting("jmcomic_domain", "https://18comic.vip") or "https://18comic.vip"),
                                 "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
                                 "user_agent": str(self.storage.get_setting("jmcomic_user_agent", "") or "")},
                                photo_id=photo_id,
                                scramble_id=str(preview_metadata.get("first_page_scramble_id") or ""),
                            )
                        elif self.module_var.get() == "pixiv" and urlparse(source).hostname and str(urlparse(source).hostname).casefold().endswith(("pximg.net", "pixiv.net")):
                            data = self.manager.get_adapter("pixiv").fetch_preview_image_bytes(
                                source,
                                {"proxy_url": str(self.storage.get_setting("proxy_url", "") or "")},
                            )
                        elif self.module_var.get() == "ehentai":
                            data = self.manager.get_adapter("ehentai").fetch_preview_image_bytes(
                                source,
                                {"proxy_url": str(self.storage.get_setting("proxy_url", "") or "")},
                            )
                        else:
                            headers = {"User-Agent": "Mozilla/5.0"}
                            request = urllib.request.Request(source, headers=headers)
                            with urllib.request.urlopen(request, timeout=20) as response:
                                data = response.read(5 * 1024 * 1024 + 1)
                        if not data or len(data) > 5 * 1024 * 1024:
                            raise ValueError("头像为空或超过 5 MiB")
                    except Exception as exc:  # noqa: BLE001
                        self.ui_queue.put(("generic_avatar_error", (request_id, source, str(exc))))
                        return
                    self.ui_queue.put(("generic_avatar_loaded", (request_id, source, data)))

                threading.Thread(target=worker, name="profile-avatar-loader", daemon=True).start()
                return
            else:
                self.profile_avatar_label.configure(image="", text="头像未缓存")
                return
            image.thumbnail((96, 96))
            self.profile_avatar_image = ImageTk.PhotoImage(image)
            self.profile_avatar_label.configure(image=self.profile_avatar_image, text="")
        except Exception:
            self.profile_avatar_label.configure(image="", text="头像")

    def _cache_profile_avatar(self, handle: str, avatar_url: str) -> None:
        self.profile_avatar_request_id += 1
        request_id = self.profile_avatar_request_id

        def worker() -> None:
            try:
                path = self.manager.get_adapter("twitter").cache_profile_avatar(handle, avatar_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("profile_avatar_error", (request_id, handle, str(exc))))
                return
            self.ui_queue.put(("profile_avatar_loaded", (request_id, handle, path)))

        threading.Thread(target=worker, name=f"twitter-avatar-{handle}", daemon=True).start()

    def _build_task_confirm_message(self, target: str, options: dict) -> str:
        lines = []
        lines.append(f"目标：{target}")
        lines.append(f"平台：{self._selected_adapter().display_name}")
        lines.append(f"保存目录：{self.output_dir_var.get()}")
        total, single, collection = self.manager.get_concurrency_limits(self.module_var.get())
        lines.append(f"并发上限：总计 {total} 路；作品/单项 {single} 路；作者/范围 {collection} 路")
        lines.append("")
        lines.append("【统一输出格式】")
        lines.append(f"  图片：{self.image_format_var.get()}")
        lines.append(f"  视频：{self.video_format_var.get()}")
        lines.append(f"  动图：{self.animation_format_var.get()}")
        lines.append(f"  音频：{self.audio_format_var.get()}")
        lines.append("")
        lines.append("【公共压缩 / 解压】")
        lines.append(f"  任务完成后：{self.archive_mode_var.get()}")
        lines.append(f"  安全解压ZIP：{'开启' if self.extract_archives_var.get() else '关闭'}")
        lines.append(f"  压缩后清理源文件：{'开启' if self.archive_cleanup_sources_var.get() else '关闭'}")
        lines.append("")
        module_id = self.module_var.get()
        if module_id == "twitter":
            lines.append("【Twitter 设置】")
            lines.append(f"  下载类型：{self._selected_types()}（1=图片 2=视频 3=GIF 4=音频）")
            lines.append(f"  失败重试：{self.retries_var.get()} 次")
        elif module_id == "pixiv":
            lines.append("【Pixiv 设置】")
            lines.append(f"  单任务最多作品：{self.pixiv_max_works_var.get()}")
            lines.append(f"  收藏/关注范围：{self.pixiv_visibility_var.get()}")
            lines.append(f"  年龄分区：{self.pixiv_age_mode_var.get()}")
            lines.append(f"  跳过AI作品：{'开启' if self.pixiv_filter_ai_var.get() else '关闭'}")
        elif module_id == "jmcomic":
            lines.append("【JMComic 设置】")
            lines.append(f"  排序：{self.jm_order_var.get()}")
            lines.append(f"  时间范围：{self.jm_time_var.get()}")
            lines.append(f"  后处理：{self.jm_postprocess_var.get()}")
        elif module_id == "google_image":
            lines.append("【相似图片设置】")
            lines.append(f"  每张候选数：{self.google_limit_var.get()}")
            lines.append(f"  验证等待：{self.google_wait_var.get()} 秒")
        else:
            lines.append(f"【{self._selected_adapter().display_name} 设置】")
            lines.append(f"  失败重试：{self.retries_var.get()} 次")
        proxy = str(self.storage.get_setting("proxy_url", "") or "").strip()
        if proxy:
            lines.append(f"  代理：{proxy}")
        lines.append("")
        lines.append("注意：下载和后处理可能占用较多磁盘、网络和 CPU 资源。")
        lines.append("点击“是”开始任务，点击“否”取消。")
        return "\n".join(lines)

    def _start_download(self) -> None:
        target = self.target_var.get().strip()
        if not target:
            messagebox.showwarning("缺少目标", "请输入目标")
            return
        module_id = self.module_var.get()
        scope = self.content_scope_var.get()
        prefilled = getattr(self, "_prefilled_task_targets", None)
        if prefilled and prefilled[0] == module_id and prefilled[1] == target:
            targets = list(prefilled[2])
        else:
            targets = split_task_targets(
                target,
                split_slashes=slash_separates_targets(module_id, scope),
                module_id=module_id,
            )
        if not targets:
            messagebox.showwarning("缺少目标", "没有识别到可加入队列的目标")
            return
        if len(targets) > 100:
            messagebox.showwarning("目标过多", f"一次最多加入 100 个目标；当前识别到 {len(targets)} 个")
            return
        if "规划" in self.content_scope_var.get():
            messagebox.showinfo(
                "下载种类尚未迁入",
                f"{self.content_scope_var.get()} 目前仅列出旧版能力，尚不能启动下载。",
            )
            return
        target_options = [(item, self._current_target_options(item)) for item in targets]
        scope_mismatches = [
            (item, options)
            for item, options in target_options
            if str(options.get("requested_content_scope") or "").strip()
        ]
        if self.module_var.get() == "pixiv" and scope_mismatches:
            descriptions = []
            for item, options in scope_mismatches[:10]:
                actual_scope = str(options.get("content_scope") or "作品")
                descriptions.append(f"{item} → {actual_scope}")
            suffix = f"\n……另有 {len(scope_mismatches) - 10} 个" if len(scope_mismatches) > 10 else ""
            if not messagebox.askyesno(
                "确认 Pixiv 任务类型",
                f"以下 Pixiv 目标会按链接识别出的具体类型下载，而不是按当前搜索种类“{self.content_scope_var.get()}”处理：\n\n"
                + "\n".join(descriptions) + suffix + "\n\n是否继续？",
            ):
                return
        if len(targets) == 1:
            target_description = target
        else:
            shown = "\n".join(f"  {index}. {item}" for index, item in enumerate(targets[:12], start=1))
            if len(targets) > 12:
                shown += f"\n  ……另有 {len(targets) - 12} 个"
            target_description = f"{len(targets)} 个独立任务：\n{shown}"
        if not messagebox.askyesno(
            "确认开始下载",
            self._build_task_confirm_message(target_description, target_options[0][1]),
        ):
            self.status_var.set("已取消任务")
            return
        callbacks = self._task_callbacks()
        created: list[object] = []
        failure = ""
        for item, options in target_options:
            try:
                task = self.manager.start_task(
                    self.module_var.get(),
                    item,
                    Path(self.output_dir_var.get()),
                    options,
                    callbacks,
                )
            except Exception as exc:  # noqa: BLE001
                failure = f"{item}: {exc}"
                break
            created.append(task)
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
            self._append_log(f"创建任务: {task.task_id} | {item}")
        if not created:
            messagebox.showerror("启动失败", failure or "没有目标成功加入队列")
            return
        self.status_var.set(f"已加入 {len(created)} 个任务，平台并发限制将按设置生效")
        if failure:
            messagebox.showwarning("部分加入队列", f"成功加入 {len(created)}/{len(targets)} 个任务。\n\n{failure}")
        self._refresh_tasks()
        self._follow_task_batch([task.task_id for task in created])

    def _cancel_task(self) -> None:
        if self.google_search_cancel_event is not None:
            self.google_search_cancel_event.set()
            self.manager.get_adapter("google_image").cancel("google-ui-search")
            self._append_log("已请求取消 Google 相似图片搜索")
        if self.following_cancel_event is not None:
            self.following_cancel_event.set()
            self.status_var.set("正在停止关注列表刷新，并保留已获取结果")
            self._append_log("已请求停止关注列表刷新")
        task_ids = set(self.current_task_ids)
        if self.current_task_id:
            task_ids.add(self.current_task_id)
        for task_id in task_ids:
            self.manager.cancel(task_id)
        if task_ids:
            self._append_log(f"已请求取消 {len(task_ids)} 个运行中任务")

    def _task_callbacks(self) -> CallbackSet:
        return CallbackSet(
            on_progress=lambda event: self.ui_queue.put(("progress", event)),
            on_file=lambda record: self.ui_queue.put(("file", record)),
            on_done=lambda task_id, status: self.ui_queue.put(("done", (task_id, status))),
        )

    @staticmethod
    def _friendly_error(message: object, action: str) -> str:
        raw = str(message or "").strip()
        lowered = raw.lower()
        if any(marker in lowered for marker in ("proxyerror", "unable to connect to proxy", "proxy connection")):
            return f"当前设置的代理无法连接，{action}未完成；请在“设置 → 代理 URL”中修改或清空代理后重试。"
        if any(marker in lowered for marker in ("no such window", "invalid session", "disconnected", "target window")):
            return f"浏览器已经关闭，{action}已结束。"
        if "pixiv" in action.casefold() and ("timeout" in lowered or "超时" in raw):
            return (
                f"Pixiv 网络请求连接超时，{action}未完成；第 1 页或资料内容尚未返回。"
                "这不是软件按总时长定时关闭，请检查网络，或在“设置 → 代理 URL”中更换可用代理后重试。"
            )
        if "timeout" in lowered or "超时" in raw:
            return f"等待时间较长，{action}已停止；已获取的本地或部分结果仍会保留。"
        if any(marker in lowered for marker in ("login", "auth_token", "cookie")) or "登录" in raw:
            return f"当前登录状态可能已失效，暂时无法完成{action}；可以稍后更新 Cookie 再试。"
        first_line = raw.splitlines()[0] if raw else "暂时没有完成"
        if len(first_line) > 180:
            first_line = first_line[:177] + "…"
        return f"{action}暂未完成：{first_line}"

    def _poll_queue(self) -> None:
        self._poll_job = None
        changed_files = False
        changed_tasks = False
        changed_task_detail = False
        progress_log_lines: list[str] = []
        processed_events = 0
        poll_started = time.monotonic()
        max_events_per_poll = 160
        max_poll_seconds = 0.04
        while processed_events < max_events_per_poll and (
            processed_events == 0 or time.monotonic() - poll_started < max_poll_seconds
        ):
            try:
                event_type, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            processed_events += 1
            if event_type == "progress":
                event = payload
                assert isinstance(event, ProgressEvent)
                progress_log_lines.append(f"[{event.level}] {event.message}")
                if self.task_tree.exists(event.task_id):
                    if event.task_id in self.task_rows:
                        self.task_rows[event.task_id]["status"] = event.status
                    values = list(self.task_tree.item(event.task_id, "values"))
                    if values:
                        values[0] = TASK_STATUS_LABELS.get(event.status, event.status)
                        self.task_tree.item(event.task_id, values=values, tags=(event.status,))
                    if event.task_id in self.task_tree.selection():
                        changed_task_detail = True
                if event.percent is not None:
                    self.task_progress_var.set(max(0.0, min(float(event.percent), 100.0)))
                elif event.current is not None and event.total:
                    self.task_progress_var.set(max(0.0, min(event.current / event.total * 100.0, 100.0)))
                elif event.status == "queued":
                    self.task_progress_var.set(3.0)
                elif event.status == "running" and self.task_progress_var.get() < 8:
                    self.task_progress_var.set(8.0)
                if event.task_id in self.task_tree.selection():
                    changed_task_detail = True
                if event.task_id in self._task_follow_order:
                    self._focus_followed_task()
                    changed_task_detail = True
            elif event_type == "file":
                record = payload
                assert isinstance(record, FileRecord)
                changed_files = True
                if record.task_id in self._task_follow_order:
                    self._focus_followed_task()
                    changed_task_detail = True
            elif event_type == "library_scan_progress":
                self.library_status_var.set(f"正在后台扫描：已发现并索引 {int(payload)} 个媒体文件…")
            elif event_type == "library_loaded":
                result = payload if isinstance(payload, dict) else {}
                self._library_worker_running = False
                self.library_all_folder_rows = list(result.get("rows") or [])
                self._library_index_count = int(result.get("indexed_count") or 0)
                self._library_index_truncated = bool(result.get("truncated"))
                scanned_count = result.get("scanned_count")
                elapsed = float(result.get("elapsed") or 0.0)
                self._library_scan_feedback = (
                    f"后台扫描 {int(scanned_count)} 个，用时 {elapsed:.1f} 秒"
                    if scanned_count is not None
                    else f"索引更新用时 {elapsed:.1f} 秒"
                )
                self._apply_library_filter()
                if scanned_count is not None:
                    self.status_var.set(f"下载库后台扫描完成：{int(scanned_count)} 个媒体文件")
                if self._library_pending_refresh:
                    self._library_refresh_job = self.after(100, self._start_library_refresh)
            elif event_type == "library_error":
                self._library_worker_running = False
                self.library_status_var.set(f"下载库读取失败：{payload}")
                self._append_log(f"下载库后台读取失败: {payload}")
                if self._library_pending_refresh:
                    self._library_refresh_job = self.after(100, self._start_library_refresh)
            elif event_type == "library_preview_progress":
                generation, preview_path, percent, message = payload  # type: ignore[misc]
                if (
                    int(generation) == self._library_preview_generation
                    and self._preview_path is not None
                    and str(self._preview_path) == str(preview_path)
                ):
                    self.library_preview_progress_var.set(max(0.0, min(float(percent), 100.0)))
                    self.library_preview_status_var.set(str(message))
            elif event_type == "library_preview":
                generation, preview_path, preview_image, preview_error = payload  # type: ignore[misc]
                if (
                    int(generation) == self._library_preview_generation
                    and self._preview_path is not None
                    and str(self._preview_path) == str(preview_path)
                ):
                    self._library_preview_loading = False
                    if preview_image is not None and ImageTk is not None:
                        try:
                            self.preview_image = ImageTk.PhotoImage(preview_image)
                            self.file_preview.configure(image=self.preview_image, text="")
                            self.library_preview_progress_var.set(100.0)
                            self.library_preview_status_var.set(f"预览完成：{Path(str(preview_path)).name}")
                        except tk.TclError:
                            pass
                    else:
                        self.library_preview_progress_var.set(0.0)
                        self.library_preview_status_var.set("预览生成失败" if preview_error else "没有可预览图片")
                        fallback = self._preview_fallback
                        self.file_preview.configure(
                            image="",
                            text=(
                                f"无法生成预览\n{Path(str(preview_path)).name}\n{str(preview_error)[:120]}"
                                if preview_error
                                else f"暂无图片\n{fallback.get('name', '')}"
                            ),
                        )
            elif event_type == "done":
                task_id, status = payload  # type: ignore[misc]
                batch_task = task_id in self._task_follow_order
                pending_delete = task_id in self.pending_task_deletions
                if pending_delete:
                    self.storage.delete_task_records([task_id])
                    self.pending_task_deletions.discard(task_id)
                    self.status_var.set(f"任务 {task_id} 已停止并删除记录")
                    self._append_log(f"任务停止后已删除记录: {task_id}")
                else:
                    label = TASK_STATUS_LABELS.get(str(status), str(status))
                    self.status_var.set(f"任务 {task_id}：{label}")
                    self._append_log(f"任务结束: {task_id} {label}")
                    if str(status) == "completed" and not batch_task:
                        try:
                            task_files = list(self.storage.list_files(task_id=task_id, limit=100000))
                            file_count = len(task_files)
                            total_size = sum(int(r.get("size") or 0) for r in task_files)
                            size_mb = total_size / (1024 * 1024)
                            output_dir = self.output_dir_var.get()
                            detail = f"任务ID：{task_id}\n下载文件：{file_count} 个\n总大小：{size_mb:.1f} MB\n保存目录：{output_dir}\n\n可在“下载库”页面查看和管理文件。"
                        except Exception:
                            detail = f"任务ID：{task_id}\n保存目录：{self.output_dir_var.get()}\n\n可在“下载库”页面查看和管理文件。"
                        messagebox.showinfo("下载完成", detail)
                    elif str(status) in {"failed", "error"} and not batch_task:
                        messagebox.showwarning("下载结束", f"任务 {task_id} 未能完成，请查看任务日志了解原因。")
                self.current_task_ids.discard(task_id)
                if task_id == self.current_task_id:
                    self.current_task_id = next(iter(self.current_task_ids), None)
                if self.current_task_ids:
                    self.task_progress_var.set(8.0)
                else:
                    self.task_progress_var.set(100.0 if status == "completed" else 0.0)
                changed_files = True
                changed_tasks = True
                self._load_twitter_history()
                if self.module_var.get() == "twitter" and len(self._current_input_targets()) == 1:
                    self._preview_target()
                if self.module_var.get() != "twitter":
                    self._populate_platform_history(self.module_var.get())
            elif event_type == "batch_info_progress":
                request_id, module_id, index, total, target = payload  # type: ignore[misc]
                if request_id == self.batch_info_request_id and module_id == self.module_var.get():
                    self.status_var.set(f"正在获取资料 {index}/{total}：{target}")
            elif event_type == "twitter_history_loaded":
                rows = payload if isinstance(payload, list) else []
                self._apply_twitter_history(rows)
            elif event_type == "twitter_history_error":
                self._append_log(f"读取推特下载历史失败: {payload}")
            elif event_type == "profile_preview_local_loaded":
                payload_dict = payload if isinstance(payload, dict) else {}
                if payload_dict.get("_requested_target") and not self._is_current_input_target(
                    str(payload_dict["_requested_target"]), "twitter"
                ):
                    continue
                self._write_profile_preview(payload_dict)
                handle = str(payload_dict.get("handle") or "").strip()
                self.status_var.set(f"已显示本地主页资料: @{handle}" if handle else "已显示本地主页资料")
            elif event_type == "profile_preview_local_error":
                if isinstance(payload, tuple):
                    requested_target, raw_message = payload
                    if not self._is_current_input_target(str(requested_target), "twitter"):
                        continue
                else:
                    raw_message = payload
                self._append_log(f"读取本地主页资料失败: {raw_message}")
                self.status_var.set("本地主页资料读取失败")
            elif event_type == "profile_preview_loaded":
                payload_dict = payload if isinstance(payload, dict) else {}
                if payload_dict.get("_requested_target") and not self._is_current_input_target(
                    str(payload_dict["_requested_target"]), "twitter"
                ):
                    continue
                self.online_button.configure(text="在线获取资料")
                if self.module_var.get() != "google_image":
                    self.online_button.state(["!disabled"])
                self._write_profile_preview(payload_dict)
                handle = str(payload_dict.get("handle", "")).strip()
                self.status_var.set(f"主页预览完成: @{handle}" if handle else "主页预览完成")
                self._append_log(self.status_var.get())
            elif event_type == "profile_preview_error":
                if isinstance(payload, tuple):
                    target, raw_message = payload
                    if not self._is_current_input_target(str(target), "twitter"):
                        continue
                else:
                    target, raw_message = self.target_var.get(), payload
                message = self._friendly_error(raw_message, "在线主页刷新")
                self.online_button.configure(text="在线获取资料")
                if self.module_var.get() != "google_image":
                    self.online_button.state(["!disabled"])
                self.status_var.set("在线主页刷新未完成，已保留本地资料")
                self._append_log(message)
            elif event_type == "profile_avatar_loaded":
                request_id, handle, path = payload  # type: ignore[misc]
                current_handle = self.profile_handle_var.get().strip().lstrip("@")
                if request_id == self.profile_avatar_request_id and current_handle.lower() == str(handle).lower():
                    self._load_profile_avatar(str(path))
            elif event_type == "profile_avatar_error":
                request_id, handle, raw_message = payload  # type: ignore[misc]
                current_handle = self.profile_handle_var.get().strip().lstrip("@")
                if request_id == self.profile_avatar_request_id and current_handle.lower() == str(handle).lower():
                    self.profile_avatar_label.configure(image="", text="头像加载失败")
                    self._append_log(f"头像缓存暂未完成: {str(raw_message).splitlines()[0]}")
            elif event_type == "generic_avatar_loaded":
                request_id, source, data = payload  # type: ignore[misc]
                if request_id == self.generic_avatar_request_id and source == self.generic_avatar_source:
                    try:
                        image = Image.open(io.BytesIO(data))
                        image.thumbnail((96, 96))
                        self.profile_avatar_image = ImageTk.PhotoImage(image)
                        self.profile_avatar_label.configure(image=self.profile_avatar_image, text="")
                    except Exception as exc:  # noqa: BLE001
                        self.profile_avatar_label.configure(image="", text="头像加载失败")
                        self._append_log(f"头像解码失败：{exc}")
            elif event_type == "generic_avatar_error":
                request_id, source, raw_message = payload  # type: ignore[misc]
                if request_id == self.generic_avatar_request_id and source == self.generic_avatar_source:
                    self.profile_avatar_label.configure(image="", text="头像加载失败")
                    self._append_log(f"头像加载失败：{str(raw_message).splitlines()[0]}")
            elif event_type == "generic_preview_loaded":
                request_id, module_id, target, preview = payload  # type: ignore[misc]
                if request_id != self.generic_preview_request_id or not self._is_current_input_target(target, module_id):
                    continue
                self.online_button.configure(text="在线获取资料")
                self.online_button.state(["!disabled"])
                self._render_generic_preview(preview)
                if module_id == "pixiv" and str(preview.metadata.get("input_kind") or "") == "history":
                    self._populate_platform_history("pixiv")
                self.status_var.set("在线预览完成")
                self._append_log("在线预览完成")
            elif event_type == "generic_preview_error":
                request_id, module_id, target, raw_message = payload  # type: ignore[misc]
                if request_id != self.generic_preview_request_id or not self._is_current_input_target(target, module_id):
                    continue
                self.online_button.configure(text="在线获取资料")
                self.online_button.state(["!disabled"])
                payload = raw_message
                message = self._friendly_error(payload, "在线预览")
                self.status_var.set("在线预览暂未完成")
                self._append_log(message)
            elif event_type == "platform_search_loaded":
                module_id, query, rows = payload  # type: ignore[misc]
                if self.module_var.get() == module_id:
                    self.platform_search_button.state(["!disabled"])
                    self._show_platform_search_results(module_id, query, rows if isinstance(rows, list) else [])
                    if module_id == "pixiv" and self.content_scope_var.get() in {"作品收藏", "小说收藏"}:
                        kind = "novel" if self.content_scope_var.get() == "小说收藏" else "work"
                        cache = self.manager.get_adapter("pixiv").load_bookmark_payload(kind)
                        state = "完整" if cache.get("complete") else "部分"
                        self.status_var.set(
                            f"Pixiv 收藏已更新：本次取得 {cache.get('fetched_count', 0)} 个，"
                            f"本地保留 {cache.get('count', 0)} 个（{state}缓存）"
                        )
                    else:
                        self.status_var.set(f"目标搜索完成：{len(rows) if isinstance(rows, list) else 0} 个结果")
            elif event_type == "batch_info_done":
                request_id, module_id, rows, cancelled = payload  # type: ignore[misc]
                if request_id == self.batch_info_request_id:
                    self.batch_info_cancel_event = None
                    self.batch_info_button.configure(text="批量获取资料")
                    self.batch_info_button.state(["!disabled"])
                    if self.module_var.get() == module_id:
                        self._show_batch_info_results(module_id, rows if isinstance(rows, list) else [])
                        state = "已停止" if cancelled else "完成"
                        self.status_var.set(f"批量资料获取{state}：{len(rows) if isinstance(rows, list) else 0} 项")
            elif event_type == "platform_search_error":
                module_id, message = payload  # type: ignore[misc]
                if self.module_var.get() == module_id:
                    self.platform_search_button.state(["!disabled"])
                    self._refresh_platform_tabs()
                friendly = self._friendly_error(message, "目标搜索")
                self.status_var.set("目标搜索暂未完成")
                self._append_log(friendly)
            elif event_type == "google_search_progress":
                progress_payload = payload if isinstance(payload, dict) else {}
                self.google_search_progress_var.set(
                    max(0.0, min(float(progress_payload.get("percent") or 0.0), 100.0))
                )
                progress_message = str(progress_payload.get("message") or "").strip()
                if progress_message:
                    self.google_result_summary_var.set(progress_message)
            elif event_type == "google_search_status":
                message = str(payload)
                self.status_var.set(message)
                self.google_result_summary_var.set(message)
                self._append_log(message)
            elif event_type == "google_search_loaded":
                result_payload = payload if isinstance(payload, dict) else {"rows": payload if isinstance(payload, list) else []}
                rows = result_payload.get("rows") if isinstance(result_payload.get("rows"), list) else []
                self.google_search_cancel_event = None
                self.google_search_button.state(["!disabled"])
                self.google_choose_button.state(["!disabled"])
                self.google_folder_button.state(["!disabled"])
                self.google_search_button_var.set("搜索相似页面")
                self._show_google_results(rows)
                processed = int(result_payload.get("processed") or 0)
                total = int(result_payload.get("total") or 0)
                partial_reason = str(result_payload.get("partial_reason") or "").strip()
                if partial_reason or result_payload.get("cancelled"):
                    self.google_search_progress_var.set(
                        max(0.0, min(processed / max(1, total) * 100.0, 100.0))
                    )
                else:
                    self.google_search_progress_var.set(100.0)
                if partial_reason:
                    reason = self._friendly_error(partial_reason, "后续批量搜索")
                    hint = google_search_failure_hint(partial_reason)
                    self.status_var.set(
                        f"已保留 {len(rows)} 个候选；{hint}" if hint
                        else f"已保留 {len(rows)} 个候选；完成图片 {processed}/{total}"
                    )
                    self._append_log(reason)
                elif result_payload.get("cancelled"):
                    self.status_var.set(f"批量搜索已停止，保留 {len(rows)} 个候选；完成图片 {processed}/{total}")
                elif result_payload.get("result_limited"):
                    self.status_var.set(f"已达到 {GOOGLE_BATCH_RESULT_LIMIT} 个候选上限；完成图片 {processed}/{total}")
                else:
                    self.status_var.set(
                        f"相似搜索完成：{len(rows)} 个候选页面；图片 {processed}/{total}"
                        if rows
                        else "本次没有提取到来源页面；可完成验证后重试或更换图片"
                    )
                self._append_log(self.status_var.get())
            elif event_type == "google_search_error":
                raw_message = str(payload)
                message = self._friendly_error(raw_message, "相似搜索")
                was_cancelled = bool(self.google_search_cancel_event and self.google_search_cancel_event.is_set())
                self.google_search_cancel_event = None
                self.google_search_button.state(["!disabled"])
                self.google_choose_button.state(["!disabled"])
                self.google_folder_button.state(["!disabled"])
                self.google_search_button_var.set("搜索相似页面")
                self.google_search_progress_var.set(0.0)
                browser_closed = "浏览器已经关闭" in message or "浏览器已关闭" in raw_message
                if was_cancelled or "取消" in raw_message or browser_closed:
                    self.status_var.set("相似搜索已结束")
                    self.google_result_summary_var.set("浏览器已关闭，搜索已结束；可重新开始")
                    self._append_log("Google 相似图片搜索已结束，未弹出错误")
                else:
                    hint = google_search_failure_hint(raw_message)
                    self.status_var.set(hint or "相似搜索暂未完成")
                    self.google_result_summary_var.set(hint or "暂未取得结果；可查看日志后重试")
                    self._append_log(message)
            elif event_type == "google_link_checked":
                index, url, result = payload  # type: ignore[misc]
                if 0 <= int(index) < len(self.google_result_rows):
                    row = self.google_result_rows[int(index)]
                    if str(row.get("url") or "").strip() == str(url):
                        result_dict = result if isinstance(result, dict) else {}
                        row["availability"] = str(
                            result_dict.get("availability") or result_dict.get("state") or "unknown"
                        )
                        row["availability_source"] = "http"
                        row["status_code"] = result_dict.get("status_code")
                        previous_pixiv_work = self._pixiv_artwork_id(self._pixiv_result_target(row))
                        row["final_url"] = result_dict.get("final_url") or url
                        if previous_pixiv_work != self._pixiv_artwork_id(self._pixiv_result_target(row)):
                            row.pop("author_id", None)
                            row.pop("author_name", None)
                            row.pop("pixiv_inspected_work_id", None)
                            row.pop("pixiv_inspect_error", None)
                        row["availability_message"] = result_dict.get("message") or result_dict.get("error") or ""
                        row["_blocked"] = self._google_row_blocked_by_rules(row)
                        self._refresh_google_result_row(int(index))
                        self._update_google_result_summary()
            elif event_type == "google_link_check_done":
                result_dict = payload if isinstance(payload, dict) else {}
                self.google_link_check_cancel_event = None
                self.platform_candidate_rows["google_image"] = list(self.google_result_rows)
                self._update_google_result_summary()
                if self.module_var.get() == "google_image":
                    self._refresh_platform_tabs()
                if result_dict.get("cancelled"):
                    summary = f"链接检查已停止；已检查 {int(result_dict.get('checked') or 0)} 个"
                else:
                    summary = (
                        f"链接检查完成：检查 {int(result_dict.get('checked') or 0)} 个，"
                        f"失效 {int(result_dict.get('missing') or 0)} 个，"
                        f"登录/访问受限 {int(result_dict.get('restricted') or 0)} 个"
                    )
                self.status_var.set(summary)
                self._append_log(summary)
            elif event_type == "google_pixiv_inspected":
                result_dict = payload if isinstance(payload, dict) else {}
                if result_dict.get("generation") == self.google_results_generation:
                    metadata = result_dict.get("metadata") if isinstance(result_dict.get("metadata"), dict) else {}
                    work_id = str(result_dict.get("work_id") or "")
                    for raw_index in result_dict.get("indexes") or []:
                        index = int(raw_index)
                        if not 0 <= index < len(self.google_result_rows):
                            continue
                        row = self.google_result_rows[index]
                        if self._pixiv_artwork_id(self._pixiv_result_target(row)) != work_id:
                            continue
                        if metadata.get("error"):
                            row["pixiv_inspect_error"] = str(metadata["error"])
                        else:
                            row["author_id"] = str(metadata.get("author_id") or "")
                            row["author_name"] = str(metadata.get("author_name") or "")
                            row["pixiv_inspected_work_id"] = work_id
                            if not row.get("title") or row.get("title") == row.get("url"):
                                row["title"] = str(metadata.get("title") or row.get("url") or "")
                            row.pop("pixiv_inspect_error", None)
                        row["_blocked"] = self._google_row_blocked_by_rules(row)
                        self._refresh_google_result_row(index)
                    self._update_google_result_summary()
            elif event_type == "google_pixiv_inspect_done":
                result_dict = payload if isinstance(payload, dict) else {}
                self.google_pixiv_inspecting = False
                self._update_google_result_summary()
                if result_dict.get("generation") == self.google_results_generation:
                    self.platform_candidate_rows["google_image"] = list(self.google_result_rows)
                    if self.module_var.get() == "google_image":
                        self._refresh_platform_tabs()
                    summary = (f"Pixiv 作者核对完成：成功 {int(result_dict.get('checked') or 0)} 件，"
                               f"失败 {int(result_dict.get('failed') or 0)} 件；黑名单已更新")
                    self.status_var.set(summary)
                    self._append_log(summary)
            elif event_type == "browser_driver_loaded":
                self.driver_check_button.state(["!disabled"])
                self.driver_download_button.state(["!disabled"])
                driver_path = str(getattr(payload, "driver_path", "") or "未找到")
                chrome_version = str(getattr(payload, "chrome_version", "") or "未知")
                driver_version = str(getattr(payload, "driver_version", "") or "未知")
                source = str(getattr(payload, "source", "") or "-")
                message = str(getattr(payload, "message", "") or "检测完成")
                self.browser_driver_status_var.set(
                    f"{message} · Chrome {chrome_version} · Driver {driver_version} · 来源 {source}\n{driver_path}"
                )
                self.status_var.set(message)
                self._append_log(
                    f"浏览器驱动检测完成：{message}，Chrome {chrome_version}，Driver {driver_version}，来源 {source}"
                )
            elif event_type == "browser_driver_error":
                self.driver_check_button.state(["!disabled"])
                self.driver_download_button.state(["!disabled"])
                message = self._friendly_error(payload, "浏览器驱动检测/安装")
                self.browser_driver_status_var.set(message)
                self.status_var.set("浏览器驱动暂不可用")
                self._append_log(message)
            elif event_type == "aria2_loaded":
                self.aria2_check_button.state(["!disabled"])
                self.aria2_install_button.state(["!disabled"])
                executable = str(getattr(payload, "executable", "") or "未找到")
                version = str(getattr(payload, "version", "") or "")
                message = str(getattr(payload, "message", "") or "检测完成")
                self.aria2_status_var.set(f"{message}{' · ' + version if version else ''}\n{executable}")
                self.status_var.set(message)
                self._append_log(f"aria2 检测完成：{message}")
            elif event_type == "aria2_error":
                self.aria2_check_button.state(["!disabled"])
                self.aria2_install_button.state(["!disabled"])
                message = self._friendly_error(payload, "aria2 检测/安装")
                self.aria2_status_var.set(message)
                self.status_var.set("aria2 暂不可用")
                self._append_log(message)
            elif event_type == "cookie_capture_done":
                self._finish_cookie_capture(payload, error=False)
            elif event_type == "cookie_capture_error":
                self._finish_cookie_capture(payload, error=True)
            elif event_type == "eh_plain_opened":
                self.eh_plain_open_button.state(["!disabled"])
                platform = str(getattr(payload, "platform", "EH") or "EH")
                if platform == "ExHentai 里站":
                    message = (
                        "ExHentai 普通登录页已打开；请先完成表站/论坛登录，然后返回软件点击"
                        "“登录后进入里站”。此时不会提前打开无法访问的里站页面。"
                    )
                else:
                    message = (
                        "E-Hentai 表站普通 Chrome 已打开；请完成登录并确认站点设置页可见，"
                        "然后关闭该 Chrome 的全部窗口并点击“登录完成后获取”。"
                    )
                self.cookie_capture_status_var.set(message)
                self.status_var.set(f"等待 {platform} 普通 Chrome 登录")
                self._append_log(f"{platform} 普通 Chrome 已打开；登录期间应用未连接浏览器")
            elif event_type == "eh_plain_open_error":
                self.eh_plain_open_button.state(["!disabled"])
                platform, raw_message = payload if isinstance(payload, tuple) and len(payload) == 2 else ("EH", payload)
                message = self._friendly_error(raw_message, f"{platform} 普通 Chrome")
                self.cookie_capture_status_var.set(message)
                self.status_var.set(f"{platform} 普通 Chrome 打开失败")
                self._append_log(message)
            elif event_type == "eh_inner_opened":
                self.eh_inner_open_button.state(["!disabled"])
                message = (
                    "已用同一个普通 Chrome 登录资料进入 ExHentai；确认页面不是空白页后，"
                    "关闭该 Chrome 的全部窗口，再点击“登录完成后获取”。"
                )
                self.cookie_capture_status_var.set(message)
                self.status_var.set("等待确认 ExHentai 页面")
                self._append_log("已在登录后的同一普通 Chrome 资料中打开 ExHentai")
            elif event_type == "eh_inner_open_error":
                self.eh_inner_open_button.state(["!disabled"])
                message = self._friendly_error(payload, "ExHentai 普通 Chrome")
                self.cookie_capture_status_var.set(message)
                self.status_var.set("进入 ExHentai 失败")
                self._append_log(message)
            elif event_type == "header_import_done":
                self.header_convert_button.state(["!disabled"])
                self.jm_header_convert_button.state(["!disabled"])
                platform, converted, count, verified = payload
                message = (
                    f"{platform} 请求头已转换为 {converted.destination.name}，并导入 {count} 个 Cookie。"
                    + ("已完成目标站点在线验证。" if verified else "请继续使用平台连接检查验证会话。")
                )
                self.cookie_capture_status_var.set(message)
                if platform == "JMComic":
                    self._refresh_jm_auth_status()
                self.status_var.set(f"{platform} Cookie 已导入")
                self._append_log(message)
                self._refresh_platform_tabs()
            elif event_type == "header_import_error":
                self.header_convert_button.state(["!disabled"])
                self.jm_header_convert_button.state(["!disabled"])
                platform, destination, raw_message = payload
                message = self._friendly_error(raw_message, f"{platform} Cookie 导入")
                self.cookie_capture_status_var.set(
                    f"JSON 已生成但未导入：{Path(destination).name}；{message}"
                )
                self.status_var.set(f"{platform} Cookie JSON 导入失败")
                if platform == "JMComic":
                    self.jm_auth_status_var.set(f"JMComic Cookie JSON 导入失败：{message}")
                self._append_log(f"{platform} Cookie JSON 导入失败：{message}")
            elif event_type == "eh_plain_capture_done":
                self._finish_eh_plain_capture(payload, error=False)
            elif event_type == "eh_plain_capture_error":
                self._finish_eh_plain_capture(payload, error=True)
            elif event_type == "bluesky_follow_done":
                self._finish_bluesky_following(payload, error=False)
            elif event_type == "bluesky_follow_error":
                self._finish_bluesky_following(payload, error=True)
            elif event_type == "eh_favorite_done":
                self._finish_eh_favorite(payload, error=False)
            elif event_type == "eh_favorite_error":
                self._finish_eh_favorite(payload, error=True)
            elif event_type == "eh_favorite_prompt":
                self._prompt_eh_favorite(payload)
            elif event_type == "jm_plain_opened":
                self.jm_plain_open_button.state(["!disabled"])
                reused = bool(getattr(payload, "reused", False))
                message = (
                    "JMComic 普通 Chrome 已重新打开；请完成验证和登录，关闭其全部窗口后点击“登录完成后获取”"
                    if reused else
                    "JMComic 普通 Chrome 已打开；请完成验证和登录，关闭其全部窗口后点击“登录完成后获取”"
                )
                self.jm_connection_status_var.set(message)
                self.status_var.set("等待 JMComic 普通 Chrome 登录")
                self._append_log("JMComic 普通 Chrome 已打开；验证期间应用未连接浏览器")
            elif event_type == "jm_plain_open_error":
                self.jm_plain_open_button.state(["!disabled"])
                message = self._friendly_error(payload, "JMComic 普通 Chrome")
                self.jm_connection_status_var.set(message)
                self.status_var.set("JMComic 普通 Chrome 打开失败")
                self._append_log(message)
            elif event_type == "jm_plain_capture_done":
                self._finish_jm_plain_capture(payload, error=False)
            elif event_type == "jm_plain_capture_error":
                self._finish_jm_plain_capture(payload, error=True)
            elif event_type == "fanbox_status_loaded":
                self.fanbox_check_button.state(["!disabled"])
                payload_dict = payload if isinstance(payload, dict) else {}
                message = str(payload_dict.get("message") or "FANBOX 状态读取完成")
                self.fanbox_status_var.set(message)
                self.status_var.set("FANBOX 订阅状态已更新")
                self._append_log(message)
            elif event_type == "pixiv_connection_loaded":
                self.pixiv_connection_button.state(["!disabled"])
                result = payload if isinstance(payload, dict) else {}
                message = str(result.get("message") or "Pixiv 连接检查完成")
                self.pixiv_connection_status_var.set(message)
                self.status_var.set("Pixiv 连接检查完成")
                self._append_log(message)
            elif event_type == "pixiv_connection_error":
                self.pixiv_connection_button.state(["!disabled"])
                message = self._friendly_error(payload, "Pixiv 资料和头像连接检查")
                self.pixiv_connection_status_var.set(message)
                self.status_var.set("Pixiv 连接检查失败")
                self._append_log(message)
            elif event_type == "jm_connection_loaded":
                self.jm_connection_button.state(["!disabled"])
                message = str(payload.get("message") or "JMComic 站点连接检查完成") if isinstance(payload, dict) else str(payload)
                self.jm_connection_status_var.set(message)
                self._refresh_jm_auth_status()
                self.status_var.set("JMComic 连接检查完成")
                self._append_log(message)
            elif event_type == "jm_connection_error":
                self.jm_connection_button.state(["!disabled"])
                message = self._friendly_error(payload, "JMComic 站点连接检查")
                self.jm_connection_status_var.set(message)
                self._refresh_jm_auth_status()
                self.status_var.set("JMComic 连接检查失败")
                self._append_log(message)
            elif event_type == "fanbox_status_error":
                self.fanbox_check_button.state(["!disabled"])
                message = self._friendly_error(payload, "FANBOX 订阅状态")
                self.fanbox_status_var.set("读取失败；请检查 FANBOXSESSID 和代理")
                self.status_var.set("FANBOX 订阅状态读取失败")
                self._append_log(message)
            elif event_type == "pixiv_following_status":
                message = str(payload or "").strip()
                if message and self.following_refreshing:
                    self.following_progress_status_var.set(message)
                    self.status_var.set(message)
                    self._append_log(message)
            elif event_type == "pixiv_following_partial":
                payload_dict = payload if isinstance(payload, dict) else {}
                items = payload_dict.get("items")
                if self.following_refreshing and isinstance(items, list):
                    total = int(payload_dict.get("total") or 0)
                    fetched = int(payload_dict.get("fetched_count") or len(items))
                    if total > 0:
                        self.following_progress_var.set(min(99.0, fetched / total * 100.0))
                    else:
                        self.following_progress_var.set(min(95.0, float(payload_dict.get("page") or 1) * 5.0))
                    self.pixiv_following_rows = items
                    if self.module_var.get() == "pixiv":
                        self._refresh_target_browser()
                        self._refresh_platform_tabs()
                    self.status_var.set(
                        f"Pixiv 已读到第 {payload_dict.get('page') or '-'} 页："
                        f"当前 {len(items)} 个，新增 {int(payload_dict.get('new_count') or 0)} 个"
                    )
                    self.following_progress_status_var.set(
                        f"第 {payload_dict.get('page') or '-'} 页已缓存 · 本次 {fetched} 个 · 列表 {len(items)} 个"
                    )
            elif event_type == "pixiv_following_loaded":
                payload_dict = payload if isinstance(payload, dict) else {}
                self.following_refreshing = False
                self.following_progress_var.set(100.0)
                self.following_cancel_event = None
                saved = self.manager.get_adapter("pixiv").load_following_payload()
                saved_items = saved.get("items") if isinstance(saved, dict) else []
                self.pixiv_following_rows = saved_items if isinstance(saved_items, list) else []
                if self.module_var.get() == "pixiv":
                    self._refresh_target_browser()
                    self._refresh_platform_tabs()
                mode = str(payload_dict.get("update_mode") or self.following_refresh_mode)
                reason = str(payload_dict.get("stopped_reason") or "")
                reason_text = {
                    "cancelled": "更新已停止，已保留取得的分页结果",
                    "request_error": "后续分页请求失败，已保留已有结果",
                    "page_limit": "达到安全页数上限，已保留已有结果",
                    "known_overlap": "已进入旧关注区，更新新的完成",
                    "incomplete_total": "页面只返回了部分画师，已保留旧缓存且未执行取关删除",
                    "uncertain_empty": "接口返回了无法确认的空列表，已保留旧缓存",
                }.get(reason, "Pixiv 关注更新完成")
                summary = (
                    f"{reason_text}：共 {len(self.pixiv_following_rows)} 个，"
                    f"新增 {int(payload_dict.get('new_count') or 0)} 个"
                )
                if mode == "all" and payload_dict.get("complete") and not reason:
                    summary += f"，移除已取关 {int(payload_dict.get('removed_count') or 0)} 个"
                cache_state = "部分缓存" if payload_dict.get("partial") else "完整缓存"
                self.following_progress_status_var.set(f"{cache_state} · {len(self.pixiv_following_rows)} 个")
                self.status_var.set(summary)
                self._append_log(summary)
            elif event_type == "pixiv_following_error":
                message = self._friendly_error(payload, "Pixiv 关注列表刷新")
                self.following_refreshing = False
                self.following_progress_var.set(0.0)
                self.following_cancel_event = None
                self.pixiv_following_rows = self.manager.get_adapter("pixiv").load_following_accounts()
                if self.module_var.get() == "pixiv":
                    self._refresh_target_browser()
                    self._refresh_platform_tabs()
                suffix = f"，继续显示已有 {len(self.pixiv_following_rows)} 个" if self.pixiv_following_rows else ""
                self.following_progress_status_var.set(
                    message + (f"\n继续显示本地缓存 {len(self.pixiv_following_rows)} 个。" if self.pixiv_following_rows else "")
                )
                self.status_var.set(f"Pixiv 关注刷新未完成{suffix}")
                self._append_log(message)
            elif event_type == "following_status":
                message = str(payload or "").strip()
                if message and self.following_refreshing:
                    self.following_progress_status_var.set(message)
                    self.status_var.set(message)
                    self._append_log(message)
            elif event_type == "following_partial":
                payload_dict = payload if isinstance(payload, dict) else {}
                items = payload_dict.get("items")
                if self.following_refreshing and isinstance(items, list):
                    current_progress = float(self.following_progress_var.get() or 0.0)
                    self.following_progress_var.set(min(95.0, max(5.0, current_progress + 2.0)))
                    self.following_rows = items
                    self._refresh_target_browser()
                    self._refresh_platform_tabs()
                    count = len(items)
                    mode = str(payload_dict.get("update_mode") or self.following_refresh_mode)
                    new_count = int(payload_dict.get("new_count") or 0)
                    if mode == "new":
                        self.status_var.set(f"已发现新增 {new_count} 个；左侧共 {count} 个，继续确认旧账号重叠")
                    else:
                        self.status_var.set(f"已获取 {count} 个关注账号，左侧列表已更新；仍在继续懒加载")
            elif event_type == "following_loaded":
                payload_dict = payload if isinstance(payload, dict) else {}
                count = payload_dict.get("count", 0)
                reason = str(payload_dict.get("stopped_reason") or "")
                mode = str(payload_dict.get("update_mode") or self.following_refresh_mode)
                new_count = int(payload_dict.get("new_count") or 0)
                removed_count = int(payload_dict.get("removed_count") or 0)
                partial = "（已保留部分结果）" if payload_dict.get("partial") else ""
                if payload_dict.get("kept_previous"):
                    partial = "（继续显示上次结果）"
                self.following_refreshing = False
                self.following_progress_var.set(100.0)
                self.following_cancel_event = None
                self._load_following_accounts()
                self.following_progress_status_var.set(f"本地缓存 · {count} 个")
                reason_text = {
                    "cancelled": "刷新已停止",
                    "time_limit": "达到本轮时间上限",
                    "browser_closed": "浏览器已关闭",
                    "empty": "X 显示当前没有关注账号",
                    "no_content": "关注页暂未加载出账号",
                }.get(reason, "更新新的关注完成" if mode == "new" else "全部关注更新完成")
                summary = f"{reason_text}{partial}: 共 {count} 个，新增 {new_count} 个"
                if mode == "all" and not reason:
                    summary += f"，移除已取关 {removed_count} 个"
                self.status_var.set(summary)
                self._append_log(self.status_var.get())
            elif event_type == "following_error":
                message = self._friendly_error(payload, "关注列表刷新")
                self.following_refreshing = False
                self.following_progress_var.set(0.0)
                self.following_cancel_event = None
                self._load_following_accounts()
                if self.following_rows:
                    self.following_progress_status_var.set(f"刷新未完成 · 本地仍有 {len(self.following_rows)} 个")
                    self.status_var.set(f"刷新未完成，继续显示已有关注账号 {len(self.following_rows)} 个")
                    self._append_log(f"{message} 已保留 {len(self.following_rows)} 个结果。")
                else:
                    self.status_var.set("关注列表暂时没有取得结果")
                    self._append_log(message)
        if progress_log_lines:
            self._append_log_batch(progress_log_lines)
        if changed_files:
            self._refresh_library(scan=False)
        now = time.monotonic()
        if self.manager.active_task_ids() and now - self._last_task_status_refresh >= 1.0:
            self._last_task_status_refresh = now
            self._refresh_task_statuses()
        if changed_tasks:
            self._refresh_tasks()
            self._task_detail_refresh_pending = False
            self._last_task_detail_render_at = now
        elif changed_task_detail:
            self._task_detail_refresh_pending = True
        if self._task_detail_refresh_pending and now - self._last_task_detail_render_at >= 0.4:
            self._focus_followed_task()
            self._render_selected_task()
            self._task_detail_refresh_pending = False
            self._last_task_detail_render_at = now
        try:
            poll_delay = 10 if not self.ui_queue.empty() else 200
            self._poll_job = self.after(poll_delay, self._poll_queue)
        except tk.TclError:
            self._poll_job = None


    def _target_to_handle(self, value: str) -> str:
        value = str(value or "").strip().strip('"').strip("'")
        if value.startswith("@"):
            return value[1:].strip()
        if value.startswith(("http://", "https://")):
            parts = [part for part in value.split("?", 1)[0].rstrip("/").split("/") if part]
            if parts:
                return parts[-1] if parts[-1] != "media" else (parts[-2] if len(parts) > 1 else "")
        return value if "/" not in value and "\\" not in value else ""

    def _format_size(self, size: int) -> str:
        if size >= 1024 * 1024 * 1024:
            return f"{size / 1024 / 1024 / 1024:.2f} GB"
        if size >= 1024 * 1024:
            return f"{size / 1024 / 1024:.2f} MB"
        if size >= 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size} B"

    def _append_log(self, text: str) -> None:
        self._append_log_batch([text])

    def _append_log_batch(self, lines: list[str]) -> None:
        if not lines:
            return
        self.log_text.configure(state="normal")
        self.log_text.insert(tk.END, "\n".join(lines) + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state="disabled")

    def _write_text(self, widget: tk.Text, text: str) -> None:
        if hasattr(widget, "render_text"):
            widget.render_text(text)
            return
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, text)
        widget.configure(state=tk.DISABLED)
        if hasattr(self, "task_detail_text") and widget is self.task_detail_text:
            if not getattr(self, "_task_detail_scroll_grabbed", False):
                widget.see(tk.END)


def main() -> None:
    app = SoftwareDesktop()
    app.mainloop()






