from __future__ import annotations

import os
import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from software_app.crawlers.common import normalize_output_format
from software_app.core.browser_cookie_capture import (
    COOKIE_CAPTURE_PLATFORMS,
    capture_browser_cookies,
    convert_header_capture_to_cookie_json,
    cookie_capture_spec,
    import_cookie_file as import_browser_cookie_file,
)
from software_app.core.ehentai_plain_browser import (
    capture_ehentai_plain_chrome,
    import_ehentai_cookie_file,
    open_ehentai_plain_chrome,
    open_exhentai_after_login,
)
from software_app.core.jmcomic_plain_browser import capture_jmcomic_plain_chrome, open_jmcomic_plain_chrome
from software_app.core.plugin_loader import external_plugin_manifest, set_external_plugin_enabled
from software_app.core.settings import PLUGINS_DIR, PROJECT_ROOT


IMAGE_FORMAT_LABELS = {"PNG": "png", "JPEG": "jpg", "WebP": "webp", "保留原格式": "original"}
VIDEO_FORMAT_LABELS = {"MP4": "mp4", "保留原格式": "original"}
ANIMATION_FORMAT_LABELS = {"GIF": "gif", "MP4": "mp4"}
AUDIO_FORMAT_LABELS = {"MP3": "mp3", "M4A": "m4a"}
ARCHIVE_MODE_LABELS = {"不压缩": "none", "按整个任务压缩": "task", "按资源文件夹压缩": "folder"}
PIXIV_VISIBILITY_LABELS = {"公开": "show", "私密": "hide", "公开 + 私密": "both"}
PIXIV_AGE_LABELS = {"全部": "all", "全年龄": "safe", "R-18": "r18"}
PIXIV_AUTHOR_MATCH_LABELS = {"部分匹配": "partial", "完全匹配": "exact"}
JM_ORDER_LABELS = {"最新": "mr", "最多浏览": "mv", "最多图片": "mp", "最多收藏": "tf"}
JM_TIME_LABELS = {"今天": "t", "本周": "w", "本月": "m", "全部时间": "a"}
JM_CATEGORY_LABELS = {
    "全部": "0", "同人": "doujin", "单本": "single", "短篇": "short", "其他": "another",
    "韩漫": "hanman", "美漫": "meiman", "Cosplay": "doujin_cosplay", "3D": "3D", "英文": "english_site",
}
JM_MATCH_LABELS = {"模糊匹配": "fuzzy", "标题精确匹配": "exact"}
JM_POSTPROCESS_LABELS = {"不合成": "none", "按章节 PDF": "pdf", "按章节长图": "long", "PDF + 长图": "both"}

PLUGIN_PURPOSES = {
    "twitter": "读取 X/Twitter 作者资料、关注列表和帖子媒体，并把下载结果写入统一任务与下载库。",
    "pixiv": "读取 Pixiv 作者、作品、收藏、小说和关注列表；同时提供 ugoira、FANBOX 与 Sketch 的目标处理。",
    "jmcomic": "搜索并下载 JMComic 漫画与章节，读取个人收藏、追更、观看记录及站内屏蔽标签，可使用 PDF/长图后处理。",
    "google_image": "用本地图片发现相似页面候选；只负责发现，不会绕过确认自动下载候选页面。",
    "website": "打开用户选定的普通网页并提取其中的图片、视频和音频资源。",
    "bluesky": "通过公开 AppView API 读取用户、公开关注、帖子、图片和视频；账号拉黑/静音可在黑名单管理中取回。",
    "instagram": "读取账号、帖子和 Reels 页面媒体；登录限定页面使用本机 Cookie，普通账号关注列表暂未接入。",
    "ehentai": "搜索和预览 E-Hentai/ExHentai 画廊，保存正常展示图与元数据；种子可手动开启，不购买官方归档或启动 P2P。",
}


def plugin_purpose(plugin_id: str) -> str:
    return PLUGIN_PURPOSES.get(str(plugin_id or "").strip().lower(), "扩展新的内容平台或显式替换现有平台处理器。")


class SettingsTabMixin:
    def _build_settings_tab(self) -> None:
        self.settings_tab.columnconfigure(0, weight=1)
        self.settings_tab.rowconfigure(1, weight=1)
        ttk.Label(self.settings_tab, text="全局设置", style="Title.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))

        scroll_host = ttk.Frame(self.settings_tab, style="Panel.TFrame")
        scroll_host.grid(row=1, column=0, sticky="nsew")
        scroll_host.columnconfigure(0, weight=1)
        scroll_host.rowconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(
            scroll_host,
            highlightthickness=0,
            borderwidth=0,
            background="#ffffff",
        )
        settings_scrollbar = ttk.Scrollbar(scroll_host, orient="vertical", command=self.settings_canvas.yview)
        self.settings_canvas.configure(yscrollcommand=settings_scrollbar.set)
        self.settings_canvas.grid(row=0, column=0, sticky="nsew")
        settings_scrollbar.grid(row=0, column=1, sticky="ns")

        body = ttk.Frame(self.settings_canvas, style="Panel.TFrame", padding=12)
        self._settings_canvas_window = self.settings_canvas.create_window((0, 0), window=body, anchor="nw")
        body.columnconfigure(1, weight=1)
        body.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(self._settings_canvas_window, width=max(1, event.width)),
        )
        ttk.Label(body, text="默认保存目录", style="Panel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 10))
        ttk.Entry(body, textvariable=self.output_dir_var).grid(row=0, column=1, sticky="ew")
        ttk.Button(body, text="浏览", command=self._choose_output_dir).grid(row=0, column=2, padx=(8, 0))

        ttk.Label(body, text="代理 URL", style="Panel.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(10, 0))
        ttk.Entry(body, textvariable=self.proxy_var).grid(row=1, column=1, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(
            body,
            text="可留空；支持 http、https、socks5、socks5h。为避免凭据泄漏，不在这里保存含用户名或密码的代理。",
            style="Small.TLabel",
            wraplength=680,
        ).grid(row=2, column=1, columnspan=2, sticky="w", pady=(3, 0))

        option_grid = ttk.Frame(body, style="FlatPanel.TFrame")
        option_grid.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        for column in (1, 3, 5, 7):
            option_grid.columnconfigure(column, weight=1)
        ttk.Label(option_grid, text="失败自动重试", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(option_grid, from_=0, to=5, textvariable=self.retries_var, width=7).grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Label(option_grid, text="每张候选数", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Spinbox(option_grid, from_=1, to=200, textvariable=self.google_limit_var, width=7).grid(row=0, column=3, sticky="w", padx=(6, 18))
        ttk.Label(option_grid, text="验证等待秒数", style="Panel.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Spinbox(option_grid, from_=0, to=300, textvariable=self.google_wait_var, width=7).grid(row=0, column=5, sticky="w", padx=(6, 18))
        ttk.Label(option_grid, text="网页最多文件", style="Panel.TLabel").grid(row=0, column=6, sticky="w")
        ttk.Spinbox(option_grid, from_=1, to=200, textvariable=self.google_max_files_var, width=7).grid(row=0, column=7, sticky="w", padx=(6, 0))
        ttk.Label(option_grid, text="网页读取超时（秒）", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(9, 0))
        ttk.Spinbox(option_grid, from_=30, to=600, textvariable=self.webpage_timeout_var, width=7).grid(
            row=1, column=1, sticky="w", padx=(6, 18), pady=(9, 0)
        )

        concurrency_grid = ttk.LabelFrame(option_grid, text="下载任务并发上限", style="Panel.TLabelframe", padding=8)
        concurrency_grid.grid(row=2, column=0, columnspan=8, sticky="ew", pady=(12, 0))
        for column in (1, 2, 3):
            concurrency_grid.columnconfigure(column, weight=1)
        for column, title in enumerate(("平台", "平台总上限", "作品 / 单项", "作者 / 范围")):
            ttk.Label(concurrency_grid, text=title, style="Panel.TLabel").grid(
                row=0, column=column, sticky="w", padx=(0, 10), pady=(0, 4)
            )
        for row_index, adapter in enumerate(self.adapters, start=1):
            variables = self.platform_concurrency_vars[adapter.module_id]
            ttk.Label(concurrency_grid, text=adapter.display_name, style="Panel.TLabel").grid(
                row=row_index, column=0, sticky="w", padx=(0, 10), pady=2
            )
            for column, key in enumerate(("total", "single", "collection"), start=1):
                ttk.Spinbox(
                    concurrency_grid,
                    from_=1,
                    to=20,
                    textvariable=variables[key],
                    width=6,
                ).grid(row=row_index, column=column, sticky="w", padx=(0, 10), pady=2)
        ttk.Label(
            concurrency_grid,
            text="默认作品 / 单项上限跟随平台总上限，作者 / 范围上限为 1。单平台总并发会同时限制两类任务；设置只影响之后加入队列的任务。",
            style="Small.TLabel",
            wraplength=700,
        ).grid(row=len(self.adapters) + 1, column=0, columnspan=4, sticky="w", pady=(6, 0))

        format_grid = ttk.LabelFrame(body, text="统一输出格式", style="Panel.TLabelframe", padding=10)
        format_grid.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        for column in (1, 3, 5, 7):
            format_grid.columnconfigure(column, weight=1)
        format_fields = (
            ("图片", self.image_format_var, tuple(IMAGE_FORMAT_LABELS), 0),
            ("视频", self.video_format_var, tuple(VIDEO_FORMAT_LABELS), 2),
            ("动图", self.animation_format_var, tuple(ANIMATION_FORMAT_LABELS), 4),
            ("音频", self.audio_format_var, tuple(AUDIO_FORMAT_LABELS), 6),
        )
        for label, variable, values, column in format_fields:
            ttk.Label(format_grid, text=label, style="Panel.TLabel").grid(row=0, column=column, sticky="w")
            ttk.Combobox(format_grid, textvariable=variable, values=values, state="readonly", width=11).grid(
                row=0, column=column + 1, sticky="w", padx=(6, 18 if column < 6 else 0)
            )
        ttk.Label(
            format_grid,
            text="转换发生在文件落盘时；图片支持 PNG/JPEG/WebP，视频 MP4，动图 GIF/MP4，音频 MP3/M4A。转换视频、动图和音频需要 FFmpeg。",
            style="Small.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=8, sticky="w", pady=(7, 0))

        archive_grid = ttk.LabelFrame(body, text="公共压缩 / 解压（调试）", style="Panel.TLabelframe", padding=10)
        archive_grid.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Label(archive_grid, text="任务完成后", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(
            archive_grid,
            textvariable=self.archive_mode_var,
            values=tuple(ARCHIVE_MODE_LABELS),
            state="readonly",
            width=18,
        ).grid(row=0, column=1, sticky="w", padx=(6, 18))
        ttk.Checkbutton(
            archive_grid, text="安全解压下载到的 ZIP", variable=self.extract_archives_var,
        ).grid(row=0, column=2, sticky="w", padx=(0, 18))
        ttk.Checkbutton(
            archive_grid, text="压缩校验成功后清理源文件", variable=self.archive_cleanup_sources_var,
        ).grid(row=0, column=3, sticky="w")
        ttk.Label(
            archive_grid,
            text=(
                "参考 PixivUtil2 的 ZIP 校验和成功后清理流程，已重写为所有平台共用的安全后处理。"
                "当前调试版只创建/解压 ZIP；默认保留源文件。按资源文件夹适合漫画章节，按整个任务适合一次下载的完整目标。"
            ),
            style="Small.TLabel",
            wraplength=650,
        ).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(7, 0))
        archive_grid.columnconfigure(3, weight=1)

        pixiv_grid = ttk.LabelFrame(body, text="Pixiv 下载", style="Panel.TLabelframe", padding=10)
        pixiv_grid.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        ttk.Label(pixiv_grid, text="单任务最多作品", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(pixiv_grid, from_=1, to=100, textvariable=self.pixiv_max_works_var, width=7).grid(
            row=0, column=1, sticky="w", padx=(6, 18)
        )
        ttk.Checkbutton(
            pixiv_grid,
            text="跳过 Pixiv 明确标记的 AI 作品",
            variable=self.pixiv_filter_ai_var,
        ).grid(row=0, column=2, sticky="w")
        ttk.Label(
            pixiv_grid,
            text="作品上限用于作者和搜索下载；单个作品目标不受此数量影响。AI 判断结果会写入任务日志。",
            style="Small.TLabel",
            wraplength=760,
        ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(pixiv_grid, text="收藏/关注范围", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(9, 0))
        ttk.Combobox(
            pixiv_grid, textvariable=self.pixiv_visibility_var,
            values=tuple(PIXIV_VISIBILITY_LABELS), state="readonly", width=13,
        ).grid(row=2, column=1, sticky="w", padx=(6, 18), pady=(9, 0))
        ttk.Label(pixiv_grid, text="年龄分区", style="Panel.TLabel").grid(row=2, column=2, sticky="w", pady=(9, 0))
        self.pixiv_age_combo = ttk.Combobox(
            pixiv_grid, textvariable=self.pixiv_age_mode_var,
            values=tuple(PIXIV_AGE_LABELS), state="readonly", width=8,
        )
        self.pixiv_age_combo.grid(row=2, column=3, sticky="w", padx=(6, 0), pady=(9, 0))
        self.pixiv_age_combo.bind("<<ComboboxSelected>>", self._pixiv_age_selection_changed)
        ttk.Label(pixiv_grid, text="开始日期", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(pixiv_grid, textvariable=self.pixiv_start_date_var, width=12).grid(row=3, column=1, sticky="w", padx=(6, 18), pady=(9, 0))
        ttk.Label(pixiv_grid, text="结束日期", style="Panel.TLabel").grid(row=3, column=2, sticky="w", pady=(9, 0))
        ttk.Entry(pixiv_grid, textvariable=self.pixiv_end_date_var, width=12).grid(row=3, column=3, sticky="w", padx=(6, 18), pady=(9, 0))
        ttk.Label(pixiv_grid, text="最低收藏数", style="Panel.TLabel").grid(row=3, column=4, sticky="w", pady=(9, 0))
        ttk.Spinbox(
            pixiv_grid, from_=0, to=100000000, textvariable=self.pixiv_minimum_bookmarks_var, width=10,
        ).grid(row=3, column=5, sticky="w", padx=(6, 0), pady=(9, 0))
        ttk.Label(
            pixiv_grid,
            text="日期格式 YYYY-MM-DD；日期和最低收藏数用于标签/标题搜索。私密关注、R-18 和 FANBOX 付费内容需要有效登录 Cookie；R-18 还必须在 Pixiv 浏览设置中开启。",
            style="Small.TLabel", wraplength=760,
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(7, 0))
        ttk.Label(pixiv_grid, text="收藏标签", style="Panel.TLabel").grid(row=5, column=0, sticky="w", pady=(9, 0))
        ttk.Entry(pixiv_grid, textvariable=self.pixiv_bookmark_tag_var, width=16).grid(
            row=5, column=1, sticky="w", padx=(6, 18), pady=(9, 0)
        )
        ttk.Label(pixiv_grid, text="作者搜索", style="Panel.TLabel").grid(row=5, column=2, sticky="w", pady=(9, 0))
        ttk.Combobox(
            pixiv_grid, textvariable=self.pixiv_author_match_var,
            values=tuple(PIXIV_AUTHOR_MATCH_LABELS), state="readonly", width=10,
        ).grid(row=5, column=3, sticky="w", padx=(6, 18), pady=(9, 0))
        ttk.Label(pixiv_grid, text="历史数量", style="Panel.TLabel").grid(row=5, column=4, sticky="w", pady=(9, 0))
        ttk.Spinbox(pixiv_grid, from_=1, to=10000, textvariable=self.pixiv_history_limit_var, width=8).grid(
            row=5, column=5, sticky="w", padx=(6, 0), pady=(9, 0)
        )
        ttk.Label(
            pixiv_grid,
            text="收藏标签用于作品/小说收藏；账号浏览历史仅适用于 Pixiv Premium。",
            style="Small.TLabel", wraplength=760,
        ).grid(row=6, column=0, columnspan=6, sticky="w", pady=(7, 0))
        self.pixiv_connection_button = ttk.Button(
            pixiv_grid,
            text="检查 Pixiv 连接/头像",
            command=self._check_pixiv_connection,
        )
        self.pixiv_connection_button.grid(row=7, column=0, columnspan=2, sticky="w", pady=(9, 0))
        ttk.Label(
            pixiv_grid,
            textvariable=self.pixiv_connection_status_var,
            style="Small.TLabel",
            wraplength=600,
        ).grid(row=7, column=2, columnspan=4, sticky="ew", padx=(8, 0), pady=(9, 0))
        self.fanbox_check_button = ttk.Button(
            pixiv_grid,
            text="检查 FANBOX 订阅状态",
            command=self._check_fanbox_status,
        )
        self.fanbox_check_button.grid(row=8, column=0, columnspan=2, sticky="w", pady=(9, 0))
        ttk.Label(
            pixiv_grid,
            textvariable=self.fanbox_status_var,
            style="Small.TLabel",
            wraplength=600,
        ).grid(row=8, column=2, columnspan=4, sticky="ew", padx=(8, 0), pady=(9, 0))

        jm_grid = ttk.LabelFrame(body, text="JMComic · 可用", style="Panel.TLabelframe", padding=10)
        self.jm_settings_frame = jm_grid
        jm_grid.grid(row=7, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        jm_grid.columnconfigure(0, weight=1)

        jm_login = ttk.LabelFrame(jm_grid, text="站点与登录资料", style="Panel.TLabelframe", padding=9)
        self.jm_login_frame = jm_login
        jm_login.grid(row=0, column=0, sticky="ew")
        jm_login.columnconfigure(1, weight=1)
        ttk.Label(jm_login, text="站点域名", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Entry(jm_login, textvariable=self.jm_domain_var, width=30).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=(6, 8)
        )
        self.jm_connection_button = ttk.Button(jm_login, text="检查连接", command=self._check_jm_connection)
        self.jm_connection_button.grid(row=0, column=4, padx=(0, 6))
        ttk.Button(jm_login, text="使用说明", command=self._open_jmcomic_guide).grid(row=0, column=5)
        ttk.Label(jm_login, text="浏览器 User-Agent", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(jm_login, textvariable=self.jm_user_agent_var).grid(
            row=1, column=1, columnspan=3, sticky="ew", padx=(6, 8), pady=(8, 0)
        )
        jm_browser_actions = ttk.Frame(jm_login, style="FlatPanel.TFrame")
        jm_browser_actions.grid(row=2, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.jm_plain_open_button = ttk.Button(
            jm_browser_actions, text="打开普通 Chrome 登录", command=self._open_jm_plain_browser,
        )
        self.jm_plain_open_button.grid(row=0, column=0, sticky="w")
        self.jm_plain_capture_button = ttk.Button(
            jm_browser_actions, text="登录完成后获取", command=self._capture_jm_plain_browser,
        )
        self.jm_plain_capture_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Button(jm_browser_actions, text="导入完整请求标头", command=self._import_jmcomic_cookie).grid(
            row=1, column=0, sticky="w", pady=(7, 0)
        )
        self.jm_header_convert_button = ttk.Button(
            jm_browser_actions, text="请求头 TXT → JSON", command=self._convert_jm_header_txt,
        )
        self.jm_header_convert_button.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(7, 0))
        ttk.Label(
            jm_browser_actions,
            text="确认账号已登录后关闭该 Chrome 的全部窗口，再点击获取；检测到 AVS 才会更新。",
            style="Small.TLabel",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(7, 0))
        ttk.Label(jm_login, text="账号用户名", style="Panel.TLabel").grid(row=3, column=0, sticky="w", pady=(8, 0))
        ttk.Entry(jm_login, textvariable=self.jm_favorite_username_var, width=18).grid(
            row=3, column=1, sticky="ew", padx=(6, 14), pady=(8, 0)
        )
        ttk.Label(jm_login, text="漫画收藏目录 ID", style="Panel.TLabel").grid(row=3, column=2, sticky="w", pady=(8, 0))
        ttk.Entry(jm_login, textvariable=self.jm_favorite_folder_var, width=10).grid(
            row=3, column=3, sticky="ew", padx=(6, 14), pady=(8, 0)
        )
        ttk.Label(jm_login, text="小说收藏目录 ID", style="Panel.TLabel").grid(row=3, column=4, sticky="w", pady=(8, 0))
        ttk.Entry(jm_login, textvariable=self.jm_novel_favorite_folder_var, width=10).grid(
            row=3, column=5, sticky="ew", padx=(6, 0), pady=(8, 0)
        )
        self.jm_auth_status_label = ttk.Label(
            jm_login, textvariable=self.jm_auth_status_var, style="Small.TLabel", wraplength=720,
        )
        self.jm_auth_status_label.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        self.jm_connection_status_label = ttk.Label(
            jm_login, textvariable=self.jm_connection_status_var, style="Small.TLabel", wraplength=720,
        )
        self.jm_connection_status_label.grid(row=5, column=0, columnspan=6, sticky="ew", pady=(4, 0))

        jm_search = ttk.LabelFrame(jm_grid, text="搜索与下载", style="Panel.TLabelframe", padding=9)
        self.jm_search_frame = jm_search
        jm_search.grid(row=1, column=0, sticky="ew", pady=(9, 0))
        for column in (1, 3, 5):
            jm_search.columnconfigure(column, weight=1)
        ttk.Label(jm_search, text="排序", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Combobox(jm_search, textvariable=self.jm_order_var, values=tuple(JM_ORDER_LABELS), state="readonly", width=11).grid(row=0, column=1, sticky="ew", padx=(6, 14))
        ttk.Label(jm_search, text="时间", style="Panel.TLabel").grid(row=0, column=2, sticky="w")
        ttk.Combobox(jm_search, textvariable=self.jm_time_var, values=tuple(JM_TIME_LABELS), state="readonly", width=11).grid(row=0, column=3, sticky="ew", padx=(6, 14))
        ttk.Label(jm_search, text="分类", style="Panel.TLabel").grid(row=0, column=4, sticky="w")
        ttk.Combobox(jm_search, textvariable=self.jm_category_var, values=tuple(JM_CATEGORY_LABELS), state="readonly", width=11).grid(row=0, column=5, sticky="ew", padx=(6, 0))
        ttk.Label(jm_search, text="名称匹配", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Combobox(jm_search, textvariable=self.jm_match_var, values=tuple(JM_MATCH_LABELS), state="readonly", width=13).grid(row=1, column=1, sticky="ew", padx=(6, 14), pady=(8, 0))
        ttk.Label(jm_search, text="章节后处理", style="Panel.TLabel").grid(row=1, column=2, sticky="w", pady=(8, 0))
        ttk.Combobox(jm_search, textvariable=self.jm_postprocess_var, values=tuple(JM_POSTPROCESS_LABELS), state="readonly", width=13).grid(row=1, column=3, sticky="ew", padx=(6, 14), pady=(8, 0))
        ttk.Checkbutton(jm_search, text="下载作品封面", variable=self.jm_download_cover_var).grid(
            row=1, column=4, columnspan=2, sticky="w", pady=(8, 0)
        )
        self.jm_help_label = ttk.Label(
            jm_grid,
            text="账号列表和站内屏蔽标签需要用户名与 AVS；cf_clearance 只处理 Cloudflare 验证。导入项仅保存在本机，不保存密码。每次最多读取 15 个候选。",
            style="Small.TLabel", wraplength=760,
        )
        self.jm_help_label.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        jm_grid.bind(
            "<Configure>",
            lambda event: (
                self.jm_auth_status_label.configure(wraplength=max(260, event.width - 42)),
                self.jm_connection_status_label.configure(wraplength=max(260, event.width - 42)),
                self.jm_help_label.configure(wraplength=max(260, event.width - 24)),
            ),
            add="+",
        )

        eh_download_grid = ttk.LabelFrame(body, text="E-Hentai / ExHentai 下载", style="Panel.TLabelframe", padding=10)
        self.eh_download_settings_frame = eh_download_grid
        eh_download_grid.grid(row=8, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        self.eh_torrent_checkbutton = ttk.Checkbutton(
            eh_download_grid,
            text="同时保存可用的 .torrent 种子文件（默认关闭）",
            variable=self.eh_download_torrent_var,
        )
        self.eh_torrent_checkbutton.grid(row=0, column=0, sticky="w")
        self.eh_bt_checkbutton = ttk.Checkbutton(
            eh_download_grid,
            text="使用 aria2 下载种子内容（默认关闭）",
            variable=self.eh_bt_download_enabled_var,
        )
        self.eh_bt_checkbutton.grid(row=1, column=0, sticky="w", pady=(6, 0))
        aria2_actions = ttk.Frame(eh_download_grid, style="Panel.TFrame")
        aria2_actions.grid(row=2, column=0, sticky="ew", pady=(7, 0))
        aria2_actions.columnconfigure(1, weight=1)
        ttk.Label(
            aria2_actions, textvariable=self.aria2_status_var, style="Small.TLabel", wraplength=560,
        ).grid(row=0, column=0, columnspan=2, sticky="ew")
        self.aria2_check_button = ttk.Button(aria2_actions, text="检测 aria2", command=lambda: self._check_aria2(False))
        self.aria2_check_button.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.aria2_install_button = ttk.Button(aria2_actions, text="安装官方便携版", command=lambda: self._check_aria2(True))
        self.aria2_install_button.grid(row=1, column=1, sticky="w", padx=(8, 0), pady=(6, 0))
        ttk.Label(
            eh_download_grid,
            text=(
                "画廊任务会保存元数据和页面正常展示的图片；不使用 Download original，也不自动申请官方 Archive ZIP。"
                "保存种子与启动 BT 是两个独立开关。BT 会连接其他节点，可能暴露公网 IP，下载期间也可能上传；"
                "软件会在完成后停止做种。请只下载有权使用的内容。"
            ),
            style="Small.TLabel",
            wraplength=600,
        ).grid(row=3, column=0, sticky="ew", pady=(7, 0))
        eh_download_grid.columnconfigure(0, weight=1)

        cookie_grid = ttk.LabelFrame(body, text="浏览器登录与 Cookie", style="Panel.TLabelframe", padding=10)
        self.cookie_settings_frame = cookie_grid
        cookie_grid.grid(row=9, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        cookie_grid.columnconfigure(1, weight=1)
        ttk.Label(cookie_grid, text="平台", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        self.cookie_capture_combo = ttk.Combobox(
            cookie_grid,
            textvariable=self.cookie_capture_platform_var,
            values=COOKIE_CAPTURE_PLATFORMS,
            state="readonly",
            width=15,
        )
        self.cookie_capture_combo.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self.cookie_capture_combo.bind("<<ComboboxSelected>>", self._cookie_capture_selection_changed)
        cookie_actions = ttk.Frame(cookie_grid, style="FlatPanel.TFrame")
        cookie_actions.grid(row=1, column=0, columnspan=4, sticky="w", pady=(8, 0))
        self.cookie_capture_button = ttk.Button(
            cookie_actions,
            text="打开登录浏览器并获取",
            command=self._start_cookie_capture,
        )
        self.cookie_capture_button.grid(row=0, column=0, sticky="w")
        self.eh_plain_open_button = ttk.Button(
            cookie_actions, text="打开普通 Chrome 登录", command=self._open_eh_plain_browser,
        )
        self.eh_plain_open_button.grid(row=0, column=0, sticky="w")
        self.eh_plain_open_button.grid_remove()
        self.eh_plain_capture_button = ttk.Button(
            cookie_actions, text="登录完成后获取", command=self._capture_eh_plain_browser,
        )
        self.eh_plain_capture_button.grid(row=0, column=1, sticky="w", padx=(8, 0))
        self.eh_plain_capture_button.grid_remove()
        self.eh_inner_open_button = ttk.Button(
            cookie_actions, text="登录后进入里站", command=self._open_exhentai_after_login,
        )
        self.eh_inner_open_button.grid(row=0, column=2, sticky="w", padx=(8, 0))
        self.eh_inner_open_button.grid_remove()
        self.header_convert_button = ttk.Button(
            cookie_actions, text="请求头 TXT → JSON", command=self._convert_header_txt,
        )
        self.header_convert_button.grid(row=1, column=0, sticky="w", pady=(8, 0))
        ttk.Button(cookie_actions, text="导入 Cookie 文件", command=self._import_selected_cookie_file).grid(
            row=1, column=1, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        ttk.Button(cookie_actions, text="使用说明", command=self._open_cookie_capture_guide).grid(
            row=1, column=2, sticky="w", padx=(8, 0), pady=(8, 0)
        )
        self.cookie_capture_status_label = ttk.Label(
            cookie_grid,
            textvariable=self.cookie_capture_status_var,
            style="Small.TLabel",
            wraplength=720,
        )
        self.cookie_capture_status_label.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        cookie_grid.bind(
            "<Configure>",
            lambda event: self.cookie_capture_status_label.configure(
                wraplength=max(260, min(560, event.width - 24))
            ),
        )

        plugin_grid = ttk.LabelFrame(body, text="平台接入插件（与上方压缩后处理不同）", style="Panel.TLabelframe", padding=10)
        plugin_grid.grid(row=10, column=0, columnspan=3, sticky="ew", pady=(14, 0))
        plugin_grid.columnconfigure(0, weight=1)
        ttk.Label(plugin_grid, textvariable=self.plugin_status_var, style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        plugin_actions = ttk.Frame(plugin_grid, style="FlatPanel.TFrame")
        plugin_actions.grid(row=0, column=1, sticky="e")
        self.plugin_toggle_button = ttk.Button(
            plugin_actions,
            text="启用/停用",
            command=self._toggle_selected_plugin,
        )
        self.plugin_toggle_button.grid(row=0, column=0)
        self.plugin_toggle_button.state(["disabled"])
        ttk.Button(plugin_actions, text="打开目录", command=self._open_plugins_folder).grid(row=0, column=1, padx=(6, 0))
        ttk.Button(plugin_actions, text="开发说明", command=self._open_plugin_guide).grid(row=0, column=2, padx=(6, 0))

        plugin_columns = ("plugin", "kind", "status", "replaces", "detail")
        plugin_tree_frame, self.plugin_tree = self._create_scrollable_tree(plugin_grid, plugin_columns)
        self.plugin_tree.configure(height=7, selectmode="browse")
        self.plugin_tree.heading("plugin", text="插件")
        self.plugin_tree.heading("kind", text="类型")
        self.plugin_tree.heading("status", text="状态")
        self.plugin_tree.heading("replaces", text="接管模块")
        self.plugin_tree.heading("detail", text="说明")
        self.plugin_tree.column("plugin", width=145, stretch=False)
        self.plugin_tree.column("kind", width=60, stretch=False, anchor="center")
        self.plugin_tree.column("status", width=80, stretch=False, anchor="center")
        self.plugin_tree.column("replaces", width=80, stretch=False, anchor="center")
        self.plugin_tree.column("detail", width=270)
        self.plugin_tree.bind("<<TreeviewSelect>>", lambda _event: self._plugin_selection_changed())
        plugin_tree_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        self.plugin_detail_var = tk.StringVar(
            value="这里显示平台 Adapter；PixivUtil2 参考功能迁移和公共压缩调试请看上方。外部插件修改后需重启软件。"
        )
        self.plugin_detail_label = ttk.Label(
            plugin_grid,
            textvariable=self.plugin_detail_var,
            style="Small.TLabel", wraplength=760,
        )
        self.plugin_detail_label.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        plugin_grid.bind(
            "<Configure>",
            lambda event: self.plugin_detail_label.configure(wraplength=max(260, event.width - 24)),
        )
        self._refresh_plugin_tree()

        actions = ttk.Frame(body, style="FlatPanel.TFrame")
        actions.grid(row=11, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        actions.columnconfigure(0, weight=1)
        ttk.Button(actions, text="保存设置", style="Accent.TButton", command=self._save_settings).grid(row=0, column=0, sticky="w")
        self.driver_check_button = ttk.Button(actions, text="检测浏览器驱动", command=lambda: self._check_browser_driver(False))
        self.driver_check_button.grid(row=0, column=1, padx=(8, 0))
        self.driver_download_button = ttk.Button(actions, text="安装 / 更新驱动", command=lambda: self._check_browser_driver(True))
        self.driver_download_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            body,
            textvariable=self.browser_driver_status_var,
            style="Section.TLabel",
            wraplength=760,
        ).grid(row=11, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        ttk.Label(
            body,
            text="设置影响之后创建的任务；重试任务会采用当前保存的统一格式。Cookie 继续保存在各平台运行数据目录，不会显示或写入日志。",
            style="Small.TLabel",
            wraplength=760,
        ).grid(row=12, column=0, columnspan=3, sticky="ew", pady=(16, 0))
        self.bind_all("<MouseWheel>", self._scroll_settings_canvas, add="+")


    def _scroll_settings_canvas(self, event) -> str | None:
        if not hasattr(self, "settings_canvas") or self.notebook.select() != str(self.settings_tab):
            return None
        x = self.winfo_pointerx()
        y = self.winfo_pointery()
        left = self.settings_canvas.winfo_rootx()
        top = self.settings_canvas.winfo_rooty()
        if not (left <= x < left + self.settings_canvas.winfo_width() and top <= y < top + self.settings_canvas.winfo_height()):
            return None
        delta = int(getattr(event, "delta", 0) or 0)
        if delta:
            self.settings_canvas.yview_scroll(-1 if delta > 0 else 1, "units")
            return "break"
        return None


    def _global_format_options(self) -> dict[str, object]:
        image_format = IMAGE_FORMAT_LABELS.get(self.image_format_var.get())
        video_format = VIDEO_FORMAT_LABELS.get(self.video_format_var.get())
        animation_format = ANIMATION_FORMAT_LABELS.get(self.animation_format_var.get())
        audio_format = AUDIO_FORMAT_LABELS.get(self.audio_format_var.get())
        if not all((image_format, video_format, animation_format, audio_format)):
            raise ValueError("统一输出格式选项无效")
        return {
            "image_format": image_format,
            "video_format": video_format,
            "animation_format": animation_format,
            "audio_format": audio_format,
            "convert_gif": animation_format == "gif",
            "keep_gif_mp4": animation_format != "gif",
        }


    def _saved_format_options(self) -> dict[str, object]:
        image_format = normalize_output_format(
            "image_format", self.storage.get_setting("image_output_format", "png")
        )
        video_format = normalize_output_format(
            "video_format", self.storage.get_setting("video_output_format", "mp4")
        )
        animation_format = normalize_output_format(
            "animation_format", self.storage.get_setting("animation_output_format", "gif")
        )
        audio_format = normalize_output_format(
            "audio_format", self.storage.get_setting("audio_output_format", "mp3")
        )
        return {
            "image_format": image_format,
            "video_format": video_format,
            "animation_format": animation_format,
            "audio_format": audio_format,
            "convert_gif": animation_format == "gif",
            "keep_gif_mp4": animation_format != "gif",
        }


    def _saved_archive_options(self) -> dict[str, object]:
        mode = str(self.storage.get_setting("archive_mode", "none") or "none").strip().casefold()
        if mode not in {"none", "task", "folder"}:
            mode = "none"
        return {
            "archive_mode": mode,
            "extract_archives": bool(self.storage.get_setting("extract_archives", False)),
            "archive_cleanup_sources": bool(self.storage.get_setting("archive_cleanup_sources", False)),
        }


    def _global_archive_options(self) -> dict[str, object]:
        mode = ARCHIVE_MODE_LABELS.get(str(self.archive_mode_var.get() or ""), "")
        if not mode:
            raise ValueError("公共压缩方式无效")
        return {
            "archive_mode": mode,
            "extract_archives": bool(self.extract_archives_var.get()),
            "archive_cleanup_sources": mode != "none" and bool(self.archive_cleanup_sources_var.get()),
        }


    def _save_settings(self) -> None:
        output_text = self.output_dir_var.get().strip()
        if not output_text:
            messagebox.showwarning("保存目录无效", "默认保存目录不能为空")
            return
        try:
            output_text.encode("utf-16-le", errors="strict")
            if "\x00" in output_text:
                raise ValueError("路径包含空字符")
            output_dir = Path(output_text).expanduser()
            if output_dir.exists() and not output_dir.is_dir():
                messagebox.showwarning("保存目录无效", "所选保存位置是一个文件")
                return
        except (OSError, UnicodeError, ValueError) as exc:
            messagebox.showwarning("保存目录无效", f"路径字符无法在当前系统使用：{exc}")
            return
        proxy_url = self.proxy_var.get().strip()
        if proxy_url:
            parsed = urlparse(proxy_url)
            if parsed.scheme.lower() not in {"http", "https", "socks5", "socks5h"} or not parsed.hostname:
                messagebox.showwarning("代理 URL 无效", "代理必须包含 http、https、socks5 或 socks5h 协议及主机名")
                return
            if parsed.username or parsed.password:
                messagebox.showwarning("不保存代理凭据", "请不要在代理 URL 中填写用户名或密码")
                return
        try:
            retries = max(0, min(int(self.retries_var.get()), 5))
            candidate_limit = max(1, min(int(self.google_limit_var.get()), 200))
            manual_wait = max(0, min(int(self.google_wait_var.get()), 300))
            max_files = max(1, min(int(self.google_max_files_var.get()), 200))
            webpage_timeout = max(30, min(int(self.webpage_timeout_var.get()), 600))
            task_concurrency_values = {}
            for module_id, variables in self.platform_concurrency_vars.items():
                total = max(1, min(20, int(variables["total"].get())))
                task_concurrency_values[module_id] = {
                    "total": total,
                    "single": min(total, max(1, min(20, int(variables["single"].get())))),
                    "collection": min(total, max(1, min(20, int(variables["collection"].get())))),
                }
            pixiv_max_works = max(1, min(int(self.pixiv_max_works_var.get()), 100))
            pixiv_filter_ai = bool(self.pixiv_filter_ai_var.get())
            pixiv_visibility = PIXIV_VISIBILITY_LABELS.get(str(self.pixiv_visibility_var.get() or ""), "")
            pixiv_start_date = self.pixiv_start_date_var.get().strip()
            pixiv_end_date = self.pixiv_end_date_var.get().strip()
            pixiv_minimum_bookmarks = max(0, min(int(self.pixiv_minimum_bookmarks_var.get()), 100000000))
            pixiv_age_mode = PIXIV_AGE_LABELS.get(str(self.pixiv_age_mode_var.get() or ""), "")
            pixiv_bookmark_tag = self.pixiv_bookmark_tag_var.get().strip()
            pixiv_author_match = PIXIV_AUTHOR_MATCH_LABELS.get(str(self.pixiv_author_match_var.get() or ""), "")
            pixiv_history_limit = max(1, min(int(self.pixiv_history_limit_var.get()), 10000))
            jm_domain = self.jm_domain_var.get().strip().rstrip("/")
            jm_user_agent = self.jm_user_agent_var.get().strip()
            jm_order = JM_ORDER_LABELS.get(str(self.jm_order_var.get() or ""), "")
            jm_time = JM_TIME_LABELS.get(str(self.jm_time_var.get() or ""), "")
            jm_category = JM_CATEGORY_LABELS.get(str(self.jm_category_var.get() or ""), "")
            jm_favorite_username = self.jm_favorite_username_var.get().strip()
            jm_favorite_folder = self.jm_favorite_folder_var.get().strip() or "0"
            jm_novel_favorite_folder = self.jm_novel_favorite_folder_var.get().strip() or "0"
            jm_match = JM_MATCH_LABELS.get(str(self.jm_match_var.get() or ""), "")
            jm_postprocess = JM_POSTPROCESS_LABELS.get(str(self.jm_postprocess_var.get() or ""), "")
            jm_download_cover = bool(self.jm_download_cover_var.get())
            eh_download_torrent = bool(self.eh_download_torrent_var.get())
            eh_bt_download_enabled = bool(self.eh_bt_download_enabled_var.get())
            format_options = self._global_format_options()
            archive_options = self._global_archive_options()
        except (TypeError, ValueError, tk.TclError):
            messagebox.showwarning(
                "设置无效",
                "请检查重试、候选数、等待时间、网页超时、文件数、平台并发数、Pixiv 作品上限、输出格式和公共压缩方式",
            )
            return
        for label, value in (("开始日期", pixiv_start_date), ("结束日期", pixiv_end_date)):
            if value and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                messagebox.showwarning("Pixiv 日期无效", f"{label}必须使用 YYYY-MM-DD，例如 2026-09-13")
                return
        if pixiv_visibility not in {"show", "hide", "both"} or pixiv_age_mode not in {"all", "safe", "r18"} or not pixiv_author_match:
            messagebox.showwarning("Pixiv 设置无效", "收藏/关注范围或年龄分区无效")
            return
        jm_parsed = urlparse(jm_domain)
        if jm_parsed.scheme not in {"http", "https"} or not jm_parsed.hostname:
            messagebox.showwarning("JMComic 设置无效", "站点域名必须是完整的 http/https URL")
            return
        if len(jm_user_agent) > 1000 or "\n" in jm_user_agent or "\r" in jm_user_agent:
            messagebox.showwarning("JMComic 设置无效", "浏览器 User-Agent 必须是单行且不超过 1000 个字符")
            return
        if (
            not jm_order or not jm_time or not jm_category or not jm_match or not jm_postprocess
            or not re.fullmatch(r"\d+", jm_favorite_folder)
            or not re.fullmatch(r"\d+", jm_novel_favorite_folder)
        ):
            messagebox.showwarning("JMComic 设置无效", "请检查排序、时间、分类及漫画/小说收藏目录 ID")
            return
        if (
            eh_bt_download_enabled
            and not bool(self.storage.get_setting("eh_bt_download_enabled", False))
            and not messagebox.askyesno(
                "确认启用 BT/P2P",
                "aria2 会连接 Tracker 和其他节点，公网 IP 可能对节点可见，下载期间也可能产生上传流量。"
                "软件只设置完成后停止做种，不能保证下载过程零上传。请确认你有权下载相关内容。仍要启用吗？",
            )
        ):
            return
        if eh_bt_download_enabled:
            eh_download_torrent = True
            self.eh_download_torrent_var.set(True)
        if (
            archive_options["archive_mode"] != "none"
            and archive_options["archive_cleanup_sources"]
            and not bool(self.storage.get_setting("archive_cleanup_sources", False))
            and not messagebox.askyesno(
                "确认压缩后清理",
                "启用后，软件只会在 ZIP 完整写入并通过校验后删除本任务的源文件。"
                "建议先关闭清理完成调试。仍要启用吗？",
            )
        ):
            return
        previous_age_mode = str(self.storage.get_setting("pixiv_age_mode", "all") or "all")
        if pixiv_age_mode == "r18" and previous_age_mode != "r18" and not messagebox.askyesno(
            "确认启用 R-18",
            "该筛选仅适用于已满 18 岁的账号。还需要在 Pixiv 的浏览设置中开启年龄限制作品；"
            "否则搜索结果可能为空。确认保存 R-18 设置吗？",
        ):
            return
        try:
            output_dir = output_dir.resolve()
        except (OSError, RuntimeError) as exc:
            messagebox.showwarning("保存目录无效", f"无法解析保存目录：{exc}")
            return
        if os.name == "nt" and len(str(output_dir)) > 180 and not messagebox.askyesno(
            "保存目录较长",
            f"当前目录已有 {len(str(output_dir))} 个字符。下载时还会追加平台、日期、作者和作品目录，"
            "可能超过未启用长路径支持的软件或 FFmpeg 的限制。\n\n仍要保存这个目录吗？",
        ):
            return
        self.output_dir_var.set(str(output_dir))
        self.retries_var.set(retries)
        self.google_limit_var.set(candidate_limit)
        self.google_wait_var.set(manual_wait)
        self.google_max_files_var.set(max_files)
        self.webpage_timeout_var.set(webpage_timeout)
        for module_id, limits in task_concurrency_values.items():
            for key, value in limits.items():
                self.platform_concurrency_vars[module_id][key].set(value)
        self.pixiv_max_works_var.set(pixiv_max_works)
        self.pixiv_filter_ai_var.set(pixiv_filter_ai)
        self.pixiv_visibility_var.set(
            next(label for label, value in PIXIV_VISIBILITY_LABELS.items() if value == pixiv_visibility)
        )
        self.pixiv_start_date_var.set(pixiv_start_date)
        self.pixiv_end_date_var.set(pixiv_end_date)
        self.pixiv_minimum_bookmarks_var.set(pixiv_minimum_bookmarks)
        self.pixiv_age_mode_var.set(
            next(label for label, value in PIXIV_AGE_LABELS.items() if value == pixiv_age_mode)
        )
        self.pixiv_bookmark_tag_var.set(pixiv_bookmark_tag)
        self.pixiv_author_match_var.set(
            next(label for label, value in PIXIV_AUTHOR_MATCH_LABELS.items() if value == pixiv_author_match)
        )
        self.pixiv_history_limit_var.set(pixiv_history_limit)
        self.jm_domain_var.set(jm_domain)
        self.jm_user_agent_var.set(jm_user_agent)
        self.jm_favorite_username_var.set(jm_favorite_username)
        self.jm_favorite_folder_var.set(jm_favorite_folder)
        self.jm_novel_favorite_folder_var.set(jm_novel_favorite_folder)
        self.archive_mode_var.set(
            next(label for label, value in ARCHIVE_MODE_LABELS.items() if value == archive_options["archive_mode"])
        )
        self.extract_archives_var.set(bool(archive_options["extract_archives"]))
        self.archive_cleanup_sources_var.set(bool(archive_options["archive_cleanup_sources"]))
        self.storage.set_setting("default_output_dir", str(output_dir))
        self.storage.set_setting("proxy_url", proxy_url)
        self.storage.set_setting("task_retries", retries)
        self.storage.set_setting("google_candidate_limit", candidate_limit)
        self.storage.set_setting("google_manual_wait", manual_wait)
        self.storage.set_setting("webpage_max_files", max_files)
        self.storage.set_setting("webpage_read_timeout", webpage_timeout)
        for module_id, limits in task_concurrency_values.items():
            self.storage.set_setting(f"task_concurrency_{module_id}", limits["total"])
            self.storage.set_setting(f"task_concurrency_single_{module_id}", limits["single"])
            self.storage.set_setting(f"task_concurrency_collection_{module_id}", limits["collection"])
        self.storage.set_setting("pixiv_max_works", pixiv_max_works)
        self.storage.set_setting("pixiv_filter_ai", pixiv_filter_ai)
        self.storage.set_setting("pixiv_visibility", pixiv_visibility)
        self.storage.set_setting("pixiv_start_date", pixiv_start_date)
        self.storage.set_setting("pixiv_end_date", pixiv_end_date)
        self.storage.set_setting("pixiv_minimum_bookmarks", pixiv_minimum_bookmarks)
        self.storage.set_setting("pixiv_age_mode", pixiv_age_mode)
        self.storage.set_setting("pixiv_bookmark_tag", pixiv_bookmark_tag)
        self.storage.set_setting("pixiv_author_match_mode", pixiv_author_match)
        self.storage.set_setting("pixiv_history_limit", pixiv_history_limit)
        self.storage.set_setting("jmcomic_domain", jm_domain)
        self.storage.set_setting("jmcomic_user_agent", jm_user_agent)
        self.storage.set_setting("jmcomic_order_by", jm_order)
        self.storage.set_setting("jmcomic_time_range", jm_time)
        self.storage.set_setting("jmcomic_category", jm_category)
        self.storage.set_setting("jmcomic_favorite_username", jm_favorite_username)
        self.storage.set_setting("jmcomic_favorite_folder_id", jm_favorite_folder)
        self.storage.set_setting("jmcomic_novel_favorite_folder_id", jm_novel_favorite_folder)
        self.storage.set_setting("jmcomic_match_mode", jm_match)
        self.storage.set_setting("jmcomic_postprocess", jm_postprocess)
        self.storage.set_setting("jmcomic_download_cover", jm_download_cover)
        self.storage.set_setting("eh_download_torrent", eh_download_torrent)
        self.storage.set_setting("eh_bt_download_enabled", eh_bt_download_enabled)
        self.storage.set_setting("image_output_format", format_options["image_format"])
        self.storage.set_setting("video_output_format", format_options["video_format"])
        self.storage.set_setting("animation_output_format", format_options["animation_format"])
        self.storage.set_setting("audio_output_format", format_options["audio_format"])
        self.storage.set_setting("archive_mode", archive_options["archive_mode"])
        self.storage.set_setting("extract_archives", archive_options["extract_archives"])
        self.storage.set_setting("archive_cleanup_sources", archive_options["archive_cleanup_sources"])
        self._refresh_jm_auth_status()
        self.status_var.set("全局设置已保存，将用于之后创建的任务")
        self._append_log("已保存全局设置（代理凭据不会写入日志）")


    def _pixiv_age_selection_changed(self, _event=None) -> None:
        if PIXIV_AGE_LABELS.get(self.pixiv_age_mode_var.get()) != "r18":
            return
        if getattr(self, "_pixiv_r18_notice_shown", False):
            return
        self._pixiv_r18_notice_shown = True
        if messagebox.askyesno(
            "Pixiv R-18 浏览设置",
            "软件只能请求 R-18 分区，不能替你修改 Pixiv 账号设置。账号必须已满 18 岁，并在 Pixiv“浏览设置”中开启年龄限制作品。\n\n是否现在打开 Pixiv 设置页？",
        ):
            webbrowser.open("https://www.pixiv.net/settings/viewing")


    def _check_fanbox_status(self) -> None:
        self.fanbox_check_button.state(["disabled"])
        self.fanbox_status_var.set("正在读取 FANBOX 订阅状态…")

        def worker() -> None:
            try:
                adapter = self.manager.get_adapter("pixiv")
                proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
                status = adapter.fanbox_supporting_status({"proxy_url": proxy_url})
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("fanbox_status_error", str(exc)))
                return
            self.ui_queue.put(("fanbox_status_loaded", status))

        threading.Thread(target=worker, name="fanbox-support-status", daemon=True).start()

    def _check_pixiv_connection(self) -> None:
        self.pixiv_connection_button.state(["disabled"])
        self.pixiv_connection_status_var.set("正在检查 Pixiv 资料接口和头像图片…")

        def worker() -> None:
            try:
                adapter = self.manager.get_adapter("pixiv")
                proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
                result = adapter.connection_status({"proxy_url": proxy_url})
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("pixiv_connection_error", str(exc)))
                return
            self.ui_queue.put(("pixiv_connection_loaded", result))

        threading.Thread(target=worker, name="pixiv-connection-status", daemon=True).start()

    def _check_jm_connection(self) -> None:
        domain = self.jm_domain_var.get().strip().rstrip("/")
        parsed = urlparse(domain)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            messagebox.showwarning("JMComic 域名无效", "请先输入完整的 http/https 站点 URL")
            return
        self.jm_connection_button.state(["disabled"])
        self.jm_connection_status_var.set("正在检查 JMComic 站点…")
        proxy_url = self.proxy_var.get().strip()
        user_agent = self.jm_user_agent_var.get().strip()

        def worker() -> None:
            try:
                adapter = self.manager.get_adapter("jmcomic")
                result = adapter.connection_status({"domain": domain, "proxy_url": proxy_url, "user_agent": user_agent})
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("jm_connection_error", str(exc)))
                return
            self.ui_queue.put(("jm_connection_loaded", result))

        threading.Thread(target=worker, name="jm-connection-status", daemon=True).start()

    def _open_jm_plain_browser(self) -> None:
        domain = self.jm_domain_var.get().strip().rstrip("/")
        parsed = urlparse(domain)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            messagebox.showwarning("JMComic 域名无效", "请先输入完整的 http/https 站点 URL")
            return
        self.jm_plain_open_button.state(["disabled"])
        self.jm_connection_status_var.set("正在打开 JMComic 专用普通 Chrome…")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                status = open_jmcomic_plain_chrome(domain, proxy_url=proxy_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("jm_plain_open_error", str(exc)))
                return
            self.ui_queue.put(("jm_plain_opened", status))

        threading.Thread(target=worker, name="jm-plain-browser-open", daemon=True).start()

    def _capture_jm_plain_browser(self) -> None:
        domain = self.jm_domain_var.get().strip().rstrip("/")
        username = self.jm_favorite_username_var.get().strip().lstrip("@").strip("/")
        if not username:
            messagebox.showwarning("缺少 JMComic 用户名", "请先填写个人主页中的账号用户名")
            return
        self.jm_plain_capture_button.state(["disabled"])
        self.jm_auth_status_var.set("正在读取已关闭的普通 Chrome 资料；检测到 AVS 前不会覆盖现有 Cookie…")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                result = capture_jmcomic_plain_chrome(domain, username, proxy_url=proxy_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("jm_plain_capture_error", str(exc)))
                return
            self.ui_queue.put(("jm_plain_capture_done", result))

        threading.Thread(target=worker, name="jm-plain-browser-capture", daemon=True).start()

    def _finish_jm_plain_capture(self, payload, *, error: bool) -> None:
        self.jm_plain_capture_button.state(["!disabled"])
        if error:
            message = str(payload or "JMComic 登录资料获取失败")
            self.jm_auth_status_var.set(message)
            self.status_var.set("JMComic 登录资料未更新")
            self._append_log(f"JMComic 普通 Chrome 获取失败：{message}")
            return
        count = int(getattr(payload, "cookie_count", 0) or 0)
        user_agent = str(getattr(payload, "user_agent", "") or "").strip()
        domain = self.jm_domain_var.get().strip().rstrip("/")
        username = self.jm_favorite_username_var.get().strip().lstrip("@").strip("/")
        self.storage.set_setting("jmcomic_domain", domain)
        self.storage.set_setting("jmcomic_favorite_username", username)
        if user_agent:
            self.jm_user_agent_var.set(user_agent)
            self.storage.set_setting("jmcomic_user_agent", user_agent)
        self._refresh_jm_auth_status()
        self.status_var.set("JMComic 登录资料已更新")
        self.jm_connection_status_var.set("已从关闭的普通 Chrome 读取登录资料；账号页有效性会在读取列表时在线验证")
        self._append_log(f"JMComic 已从普通 Chrome 保存 {count} 个 Cookie 和安全请求头")
        self._refresh_platform_tabs()

    def _refresh_jm_auth_status(self) -> None:
        try:
            adapter = self.manager.get_adapter("jmcomic")
            status = adapter.cookie_status()
            header_count = len(adapter.browser_headers())
        except Exception as exc:  # noqa: BLE001
            self.jm_auth_status_var.set(f"无法读取本机登录资料：{exc}")
            return
        header_text = f"浏览器请求头 {header_count} 项" if header_count else "未保存浏览器请求头"
        username = self.jm_favorite_username_var.get().strip()
        account_text = f"账号用户名 {username}" if username else "尚未填写账号用户名"
        self.jm_auth_status_var.set(f"{status['message']}；{header_text}；{account_text}")

    def _open_jmcomic_guide(self) -> None:
        messagebox.showinfo(
            "JMComic 使用与登录说明",
            "【登录步骤】\n"
            "1. 在设置页确认站点域名、代理和账号用户名。\n"
            "2. 点「打开普通 Chrome 登录」，在浏览器中完成 Cloudflare 验证和账号登录。\n"
            "3. 登录后打开漫画收藏页，确认内容可见。\n"
            "4. 关闭该 Chrome 的全部窗口，回到软件点「登录完成后获取」。\n"
            "5. 软件检测到非空 AVS 后才会更新 Cookie；未取得时保留原文件。\n"
            "\n"
            "【注意事项】\n"
            "- cf_clearance 只是 Cloudflare 通行凭证，不能证明账号已登录。\n"
            "- 账号功能（收藏/追更/观看记录）需要 AVS 登录会话。\n"
            "- 获取前必须关闭专用 Chrome 全部窗口，否则 Windows 会锁住 Cookie 数据库。\n"
            "- 出现 HTTP 403 时，应在相同代理下重新通过验证，再一起更新 Cookie、请求头和 User-Agent。\n"
            "- 也可使用「请求头 TXT → JSON」从浏览器抓包导入。\n"
            "\n"
            "Cookie 保存在 data/software_app/jmcomic/cookies.json，不会显示在日志中。",
        )

    def _open_plugins_folder(self) -> None:
        PLUGINS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(PLUGINS_DIR.resolve()))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开插件目录", str(exc))


    def _open_plugin_guide(self) -> None:
        guide_text = (
            "【插件目录结构】\n"
            "data/software_app/plugins/<plugin-id>/\n"
            "  plugin.json  （清单文件）\n"
            "  plugin.py    （适配器代码）\n"
            "\n"
            "【最小 plugin.json】\n"
            '{\n  "id": "sample-plugin",\n  "name": "示例平台",\n'
            '  "api_version": 1,\n  "enabled": true,\n'
            '  "module": "plugin.py",\n  "factory": "create_adapters"\n}'
            "\n\n"
            "【开发要求】\n"
            "- create_adapters() 返回一个 CrawlerAdapter 或 Adapter 列表。\n"
            "- 每个 Adapter 至少实现 preview_target() 和 download()。\n"
            "- 支持搜索时实现 search_targets() 并在 ModuleInfo.capabilities 中声明。\n"
            "- 只有 enabled=true 才会加载，模块 ID 重复会被拒绝。\n"
            '- 需替换内置模块时，清单须增加 "replaces": "pixiv"，这是显式接管。\n'
            "\n"
            "【管理方式】\n"
            "- 设置页「平台插件」可查看加载状态、启用/停用外部插件。\n"
            "- 修改清单或代码后需重启软件。\n"
            "- 命令行执行 python run_software.py plugins 可查看加载状态。"
        )
        messagebox.showinfo("平台插件开发说明", guide_text)


    def _refresh_plugin_tree(self) -> None:
        if not hasattr(self, "plugin_tree"):
            return
        for item in self.plugin_tree.get_children():
            self.plugin_tree.delete(item)
        self._plugin_report_rows = {}
        status_labels = {
            "loaded": "已加载",
            "disabled": "已停用",
            "error": "错误",
            "replaced": "已被接管",
        }
        for index, report in enumerate(self.context.plugin_reports):
            item_id = f"plugin-{index}"
            self._plugin_report_rows[item_id] = report
            kind = "内置" if report.source == "builtin" else "外部"
            self.plugin_tree.insert(
                "",
                "end",
                iid=item_id,
                values=(
                    f"{report.name} ({report.plugin_id})",
                    kind,
                    status_labels.get(report.status, report.status),
                    report.replaces or "-",
                    plugin_purpose(report.plugin_id) if report.source == "builtin" else (report.message or plugin_purpose(report.plugin_id)),
                ),
            )
        self._plugin_selection_changed()


    def _selected_plugin_report(self):
        selected = self.plugin_tree.selection() if hasattr(self, "plugin_tree") else ()
        if not selected:
            return None
        return getattr(self, "_plugin_report_rows", {}).get(selected[0])


    def _plugin_selection_changed(self) -> None:
        report = self._selected_plugin_report()
        self.plugin_toggle_button.state(["disabled"])
        self.plugin_toggle_button.configure(text="启用/停用")
        if report is None:
            self.plugin_detail_var.set(
                "这里显示平台 Adapter；PixivUtil2 参考功能迁移和公共压缩调试请看上方。外部插件修改后需重启软件。"
            )
            return
        if report.source == "builtin":
            detail = f"内置平台模块：{report.name}\n\n用途：\n{plugin_purpose(report.plugin_id)}"
            if report.status == "replaced":
                detail += f"\n\n当前状态：\n{report.message}"
            else:
                detail += "\n\n说明：\n随软件启动，不能直接停用；可信外部插件可以通过 replaces 显式接管。"
            self.plugin_detail_var.set(detail)
            return
        try:
            _manifest, payload = external_plugin_manifest(report.source, PLUGINS_DIR)
        except Exception as exc:  # noqa: BLE001
            self.plugin_detail_var.set(f"外部插件清单不可编辑：{exc}")
            return
        enabled = payload.get("enabled") is True
        self.plugin_toggle_button.configure(text="停用（重启后）" if enabled else "启用（重启后）")
        self.plugin_toggle_button.state(["!disabled"])
        replacement = str(payload.get("replaces") or "").strip()
        suffix = f"；重启后接管 {replacement}" if replacement else "；重启后作为新平台加载"
        description = str(payload.get("description") or plugin_purpose(report.plugin_id)).strip()
        self.plugin_detail_var.set(
            f"外部插件：{report.name}\n\n用途：\n{description}\n\n目录：\n{report.source}\n\n生效方式：{suffix.lstrip('；')}"
        )


    def _toggle_selected_plugin(self) -> None:
        report = self._selected_plugin_report()
        if report is None or report.source == "builtin":
            return
        try:
            _manifest, payload = external_plugin_manifest(report.source, PLUGINS_DIR)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("无法读取插件", str(exc))
            return
        currently_enabled = payload.get("enabled") is True
        enabling = not currently_enabled
        if enabling and not messagebox.askyesno(
            "启用外部插件",
            "外部插件是本机 Python 代码，启用后将获得当前用户权限。\n\n"
            "请只启用已经检查且来源可信的插件。是否继续？",
        ):
            return
        try:
            updated = set_external_plugin_enabled(report.source, enabling, PLUGINS_DIR)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("插件设置失败", str(exc))
            return
        state_text = "启用" if updated.get("enabled") is True else "停用"
        current = self.plugin_tree.selection()
        if current:
            values = list(self.plugin_tree.item(current[0], "values"))
            if len(values) >= 3:
                values[2] = f"待重启{state_text}"
                self.plugin_tree.item(current[0], values=values)
        self.plugin_toggle_button.configure(text="停用（重启后）" if enabling else "启用（重启后）")
        self.plugin_status_var.set(f"插件已设置为{state_text}；重启软件后生效")
        self.plugin_detail_var.set(f"已写入 {report.source}\\plugin.json；当前运行中的插件和任务不受影响。")


    def _cookie_capture_selection_changed(self, _event=None) -> None:
        hints = {
            "Twitter/X": "X 可能拒绝自动化登录。推荐先在普通 Chrome/Edge 登录，再导出 Cookie JSON、cookies.txt 或复制 Cookie 请求头后导入。",
            "Pixiv": "登录后还会调用 Pixiv 账号接口确认会话有效；保存时保留已有 FANBOX Cookie。",
            "FANBOX": "通过 Pixiv 账号登录，并以 FANBOX 支持方案接口确认会话；保存时保留已有 Pixiv Cookie。",
            "Instagram": "登录完成后检测 sessionid；可以在页面内完成验证码或双重验证。",
            "E-Hentai 表站": "表站账号独立保存，只用于 e-hentai.org 和表站收藏夹，不会发送给里站。",
            "ExHentai 里站": "先打开普通登录页完成表站/论坛登录，再点“登录后进入里站”；确认里站正常后关闭全部窗口并获取。尽量使用非亚洲代理/VPN。",
        }
        platform = self.cookie_capture_platform_var.get().strip()
        is_eh = platform in {"E-Hentai 表站", "ExHentai 里站"}
        if is_eh:
            self.cookie_capture_button.grid_remove()
            self.eh_plain_open_button.grid()
            self.eh_plain_capture_button.grid()
            self.eh_inner_open_button.grid() if platform == "ExHentai 里站" else self.eh_inner_open_button.grid_remove()
        else:
            self.eh_plain_open_button.grid_remove()
            self.eh_plain_capture_button.grid_remove()
            self.eh_inner_open_button.grid_remove()
            self.cookie_capture_button.grid()
        self.header_convert_button.grid()
        self.cookie_capture_status_var.set(
            hints.get(platform, "选择平台后打开独立登录窗口；软件不会保存账号密码。")
        )

    def _open_eh_plain_browser(self) -> None:
        platform = self.cookie_capture_platform_var.get().strip()
        if platform not in {"E-Hentai 表站", "ExHentai 里站"}:
            messagebox.showwarning("平台选择无效", "请先选择 E-Hentai 表站或 ExHentai 里站")
            return
        if platform == "ExHentai 里站" and not messagebox.askokcancel(
            "ExHentai 里站网络提示",
            "里站账号与表站账号使用不同的 Chrome 资料和 Cookie 文件。\n\n"
            "建议先连接非亚洲代理/VPN，并让登录、获取 Cookie 和后续访问保持同一出口。\n\n"
            "接下来只打开普通登录页；登录完成后请返回软件点击“登录后进入里站”。",
            parent=self,
        ):
            return
        self.eh_plain_open_button.state(["disabled"])
        self.cookie_capture_status_var.set(f"正在打开 {platform} 专用普通 Chrome…")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                status = open_ehentai_plain_chrome(platform, proxy_url=proxy_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("eh_plain_open_error", (platform, str(exc))))
                return
            self.ui_queue.put(("eh_plain_opened", status))

        threading.Thread(target=worker, name=f"eh-plain-open-{platform}", daemon=True).start()

    def _capture_eh_plain_browser(self) -> None:
        platform = self.cookie_capture_platform_var.get().strip()
        if platform not in {"E-Hentai 表站", "ExHentai 里站"}:
            messagebox.showwarning("平台选择无效", "请先选择 E-Hentai 表站或 ExHentai 里站")
            return
        self.eh_plain_capture_button.state(["disabled"])
        self.cookie_capture_status_var.set(
            f"正在读取已关闭的 {platform} 普通 Chrome 资料并在线验证；成功前不会覆盖旧 Cookie…"
        )
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                result = capture_ehentai_plain_chrome(platform, proxy_url=proxy_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("eh_plain_capture_error", (platform, str(exc))))
                return
            self.ui_queue.put(("eh_plain_capture_done", result))

        threading.Thread(target=worker, name=f"eh-plain-capture-{platform}", daemon=True).start()

    def _open_exhentai_after_login(self) -> None:
        if self.cookie_capture_platform_var.get().strip() != "ExHentai 里站":
            return
        self.eh_inner_open_button.state(["disabled"])
        self.cookie_capture_status_var.set("正在用同一个普通 Chrome 登录资料进入 ExHentai…")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                status = open_exhentai_after_login(proxy_url=proxy_url)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("eh_inner_open_error", str(exc)))
                return
            self.ui_queue.put(("eh_inner_opened", status))

        threading.Thread(target=worker, name="eh-inner-open-after-login", daemon=True).start()

    def _convert_jm_header_txt(self) -> None:
        self._convert_header_txt("JMComic")

    def _convert_header_txt(self, platform_override: str = "") -> None:
        platform = str(platform_override or self.cookie_capture_platform_var.get()).strip()
        try:
            spec = cookie_capture_spec(
                platform,
                jmcomic_domain=self.jm_domain_var.get().strip(),
                jmcomic_username=self.jm_favorite_username_var.get().strip(),
            )
        except ValueError as exc:
            messagebox.showerror("请求头转换失败", str(exc), parent=self)
            return
        source = filedialog.askopenfilename(
            parent=self,
            title=f"选择 {platform} 的 DevTools 请求头 TXT",
            filetypes=[("请求头文本", "*.txt"), ("所有文件", "*.*")],
        )
        if not source:
            return
        try:
            converted = convert_header_capture_to_cookie_json(spec, source)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("请求头转换失败", str(exc), parent=self)
            return
        warning = (
            f"已提取 {converted.cookie_count} 个 Cookie 并生成：\n{converted.destination}\n\n"
            "JSON 与原始 TXT 都包含明文登录 Cookie，请勿分享。\n\n"
            + (
                "是否现在使用当前软件代理在线验证，并导入该账号会话？"
                if platform in {"E-Hentai 表站", "ExHentai 里站"}
                else "是否现在导入该账号会话？导入后可使用平台连接检查验证。"
            )
        )
        if not messagebox.askyesno("Cookie JSON 已生成", warning, parent=self):
            self.cookie_capture_status_var.set(f"已生成 Cookie JSON：{converted.destination.name}；尚未导入")
            return
        self.header_convert_button.state(["disabled"])
        self.jm_header_convert_button.state(["disabled"])
        self.cookie_capture_status_var.set(f"正在导入 {platform} Cookie JSON…")
        if platform == "JMComic":
            self.jm_auth_status_var.set("正在从原始请求头导入 JMComic Cookie 与安全请求头…")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                if platform in {"E-Hentai 表站", "ExHentai 里站"}:
                    result = import_ehentai_cookie_file(
                        platform, converted.source, proxy_url=proxy_url
                    )
                    count = result.cookie_count
                    verified = True
                elif platform == "JMComic":
                    # Import the original capture so JM-specific safe request headers
                    # are retained alongside the cookie-only JSON export.
                    count = int(self.manager.get_adapter("jmcomic").import_cookie_file(converted.source))
                    verified = False
                else:
                    result = import_browser_cookie_file(spec, converted.destination)
                    count = result.cookie_count
                    verified = False
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("header_import_error", (platform, converted.destination, str(exc))))
                return
            self.ui_queue.put(("header_import_done", (platform, converted, count, verified)))

        threading.Thread(target=worker, name="header-json-import", daemon=True).start()

    def _finish_eh_plain_capture(self, payload, *, error: bool) -> None:
        self.eh_plain_capture_button.state(["!disabled"])
        if error:
            platform, message = payload if isinstance(payload, tuple) and len(payload) == 2 else ("EH", str(payload))
            self.cookie_capture_status_var.set(str(message))
            self.status_var.set(f"{platform} 登录资料未更新")
            self._append_log(f"{platform} 普通 Chrome 获取失败：{message}")
            return
        platform = str(getattr(payload, "platform", "EH") or "EH")
        count = int(getattr(payload, "cookie_count", 0) or 0)
        self.cookie_capture_status_var.set(
            f"{platform} 已从关闭的普通 Chrome 读取并验证 {count} 个 Cookie；与另一站账号互不覆盖。"
        )
        self.status_var.set(f"{platform} 登录资料已更新")
        self._append_log(f"{platform} 已从普通 Chrome 保存并验证 {count} 个 Cookie")
        self._refresh_platform_tabs()


    def _open_cookie_capture_guide(self) -> None:
        messagebox.showinfo(
            "浏览器登录与 Cookie 说明",
            "【使用步骤】\n"
            "1. 打开设置 → 浏览器登录与 Cookie。\n"
            "2. 选择平台：Twitter/X、Pixiv、FANBOX、Instagram、E-Hentai 表站或 ExHentai 里站。\n"
            "3. 确认代理 URL（登录窗口会使用当前值）。\n"
            "4. 普通平台点「打开登录浏览器并获取」；EH 点「打开普通 Chrome 登录」。\n"
            "5. 在官网窗口自行登录并完成验证，不要把密码输入软件。\n"
            "6. 普通平台检测到必需 Cookie 后自动保存并关闭窗口。\n"
            "   EH 需关闭全部专用窗口后点「登录完成后获取」。\n"
            "\n"
            "【各平台必需 Cookie】\n"
            "- Twitter/X: auth_token、ct0\n"
            "- Pixiv: PHPSESSID（账号接口验证成功）\n"
            "- FANBOX: FANBOXSESSID\n"
            "- Instagram: sessionid\n"
            "- E-Hentai 表站: ipb_member_id、ipb_pass_hash\n"
            "- ExHentai 里站: ipb_member_id、ipb_pass_hash、igneous\n"
            "\n"
            "【注意事项】\n"
            "- 软件不保存账号密码，Cookie 只保存在 data/software_app/。\n"
            "- 登录等待上限 10 分钟，可提前点「停止获取」。\n"
            "- 平台拒绝自动化登录时，可用「请求头 TXT → JSON」从普通浏览器导入。\n"
            "- Pixiv 和 FANBOX 需分别登录；EH 表站和里站也需分别配置。\n"
            "- Bluesky、Google 相似搜索、普通网页不需要 Cookie。",
        )


    def _start_cookie_capture(self) -> None:
        if self.cookie_capture_running:
            if self.cookie_capture_cancel_event is not None:
                self.cookie_capture_cancel_event.set()
            self.cookie_capture_button.state(["disabled"])
            self.cookie_capture_status_var.set("正在停止登录浏览器…")
            return

        platform = self.cookie_capture_platform_var.get().strip()
        try:
            spec = cookie_capture_spec(
                platform,
                jmcomic_domain=self.jm_domain_var.get().strip(),
                jmcomic_username=self.jm_favorite_username_var.get().strip(),
            )
        except ValueError as exc:
            messagebox.showwarning("无法打开登录浏览器", str(exc))
            return

        if platform == "ExHentai 里站" and not messagebox.askokcancel(
            "ExHentai 里站网络提示",
            "里站账号会与表站账号分开保存，不会互相复用。\n\n"
            "建议先连接非亚洲代理/VPN，并让本次登录与之后的里站访问保持同一出口。"
            "登录论坛后，软件会自动转到 ExHentai 检查页面与 igneous Cookie。\n\n"
            "确认当前网络环境后继续。",
            parent=self,
        ):
            return

        cancel_event = threading.Event()
        self.cookie_capture_cancel_event = cancel_event
        self.cookie_capture_running = True
        self.cookie_capture_combo.state(["disabled"])
        self.cookie_capture_button.configure(text="停止获取")
        self.cookie_capture_status_var.set(
            f"已打开 {platform} 登录窗口。请在网站内登录并完成验证；检测到所需 Cookie 后窗口会自动关闭。"
        )
        self.status_var.set(f"等待 {platform} 登录完成")
        self._append_log(f"已打开 {platform} 独立登录窗口；不会读取或保存账号密码")
        proxy_url = self.proxy_var.get().strip()

        def worker() -> None:
            try:
                result = capture_browser_cookies(
                    spec,
                    proxy_url=proxy_url,
                    cancel_event=cancel_event,
                    timeout_seconds=600,
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("cookie_capture_error", str(exc)))
                return
            self.ui_queue.put(("cookie_capture_done", result))

        threading.Thread(target=worker, name=f"cookie-capture-{spec.key}", daemon=True).start()


    def _import_selected_cookie_file(self) -> None:
        platform = self.cookie_capture_platform_var.get().strip()
        source = filedialog.askopenfilename(
            parent=self,
            title=f"导入 {platform} Cookie",
            filetypes=[
                ("Cookie 文件", "*.json *.txt *.cookies"),
                ("JSON", "*.json"),
                ("文本 / Netscape", "*.txt *.cookies"),
                ("所有文件", "*.*"),
            ],
        )
        if not source:
            return
        try:
            spec = cookie_capture_spec(
                platform,
                jmcomic_domain=self.jm_domain_var.get().strip(),
                jmcomic_username=self.jm_favorite_username_var.get().strip(),
            )
            if platform == "JMComic":
                adapter = self.manager.get_adapter("jmcomic")
                count = int(adapter.import_cookie_file(source))
                destination = adapter.runtime_data_dir / "cookies.json"
            elif platform in {"E-Hentai 表站", "ExHentai 里站"}:
                result = import_ehentai_cookie_file(
                    platform, source, proxy_url=self.proxy_var.get().strip()
                )
                count = result.cookie_count
                destination = result.destination
            else:
                result = import_browser_cookie_file(spec, source)
                count = result.cookie_count
                destination = result.destination
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Cookie 导入失败", str(exc), parent=self)
            return
        if platform == "JMComic":
            self._refresh_jm_auth_status()
        self.cookie_capture_status_var.set(
            f"{platform} 已从普通浏览器导出文件导入 {count} 个 Cookie；"
            + ("已完成目标站点在线验证。" if platform in {"E-Hentai 表站", "ExHentai 里站"}
               else "请使用平台连接检查或在线资料验证有效性。")
        )
        self.status_var.set(f"{platform} Cookie 已导入")
        self._append_log(f"{platform} Cookie 已从文件导入本机运行目录：{destination}")
        self._refresh_platform_tabs()


    def _finish_cookie_capture(self, payload, *, error: bool) -> None:
        self.cookie_capture_running = False
        self.cookie_capture_cancel_event = None
        self.cookie_capture_combo.state(["!disabled", "readonly"])
        self.cookie_capture_button.state(["!disabled"])
        self.cookie_capture_button.configure(text="打开登录浏览器并获取")
        if error:
            message = str(payload or "Cookie 获取未完成")
            self.cookie_capture_status_var.set(message)
            self.status_var.set("Cookie 获取未完成")
            self._append_log(f"Cookie 获取未完成：{message}")
            return

        platform = str(getattr(payload, "platform", "") or "所选平台")
        count = int(getattr(payload, "cookie_count", 0) or 0)
        destination = Path(getattr(payload, "destination", ""))
        user_agent = str(getattr(payload, "user_agent", "") or "").strip()
        if platform == "JMComic":
            domain = self.jm_domain_var.get().strip().rstrip("/")
            self.storage.set_setting("jmcomic_domain", domain)
            if user_agent:
                self.jm_user_agent_var.set(user_agent)
                self.storage.set_setting("jmcomic_user_agent", user_agent)
            self._refresh_jm_auth_status()
        self.cookie_capture_status_var.set(
            f"{platform} 登录资料已保存：{count} 个 Cookie。可以关闭网站会话后继续在软件中使用。"
        )
        self.status_var.set(f"{platform} Cookie 已更新")
        self._append_log(f"{platform} Cookie 已安全保存到本机运行目录：{destination}")
        self._refresh_platform_tabs()


    def _check_browser_driver(self, auto_download: bool) -> None:
        self.driver_check_button.state(["disabled"])
        self.driver_download_button.state(["disabled"])
        self.browser_driver_status_var.set("正在检测 Chrome 和 ChromeDriver…")

        def worker() -> None:
            try:
                from software_app.core.browser_driver import chromedriver_status

                status = chromedriver_status(auto_download=auto_download)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("browser_driver_error", str(exc)))
                return
            self.ui_queue.put(("browser_driver_loaded", status))

        threading.Thread(target=worker, name="browser-driver-check", daemon=True).start()

    def _check_aria2(self, auto_install: bool) -> None:
        if auto_install and not messagebox.askyesno(
            "安装 aria2",
            "将优先使用随软件提供或程序目录中的 aria2 官方便携包；没有本地包时再从官方 GitHub Release 下载。"
            "安装前会校验固定 SHA-256，并保留许可证文件。继续吗？",
        ):
            return
        self.aria2_check_button.state(["disabled"])
        self.aria2_install_button.state(["disabled"])
        self.aria2_status_var.set("正在安装并校验 aria2…" if auto_install else "正在检测 aria2…")

        def worker() -> None:
            try:
                from software_app.core.aria2_bt import aria2_status, install_aria2

                status = install_aria2() if auto_install else aria2_status()
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("aria2_error", str(exc)))
                return
            self.ui_queue.put(("aria2_loaded", status))

        threading.Thread(target=worker, name="aria2-check", daemon=True).start()


