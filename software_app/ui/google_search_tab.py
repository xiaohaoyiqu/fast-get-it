from __future__ import annotations

import re
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from software_app.core.blocklist import work_from_target
from software_app.core.exports import export_url_candidates
from software_app.core.models import IMAGE_EXTENSIONS
from software_app.crawlers.webpage import check_page_availability
from software_app.ui.desktop_support import (
    GOOGLE_BATCH_IMAGE_LIMIT,
    bounded_int as _bounded_int,
    collect_image_files as _collect_image_files,
    download_module_for_target,
    download_types_for_discovered_target,
)
from software_app.ui.scrolling import bind_canvas_mousewheel

try:
    from PIL import Image, ImageOps, ImageTk
except Exception:
    Image = None
    ImageOps = None
    ImageTk = None


LOCAL_IMAGE_LIBRARY = Path(r"D:\img_download")
GOOGLE_BATCH_RESULT_LIMIT = 1000


def google_search_failure_hint(message: object) -> str:
    """Short, actionable status for the two Lens pages that are not results."""
    content = str(message or "").casefold()
    if "图片已过期" in content or "视觉搜索内容已过期" in content or "图片未找到" in content:
        return "Google 图片已过期，请重新上传图片后重试"
    if "人机验证" in content or "unusual traffic" in content or "captcha" in content:
        return "Google 要求人机验证，请在浏览器完成验证后重试"
    return ""


class GoogleSearchTabMixin:
    def _build_google_search_tab(self) -> None:
        self.google_search_tab.columnconfigure(0, weight=1)
        self.google_search_tab.rowconfigure(1, weight=1)

        workflow = ttk.Frame(self.google_search_tab, style="Soft.TFrame", padding=(6, 1))
        workflow.grid(row=0, column=0, sticky="ew", pady=(0, 4))
        workflow.columnconfigure((0, 1, 2), weight=1)
        ttk.Label(workflow, text="1  选择图片", style="Small.TLabel").grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ttk.Label(workflow, text="2  查找页面", style="Small.TLabel").grid(row=0, column=1, sticky="ew", padx=3)
        ttk.Label(workflow, text="3  核对并下载", style="Small.TLabel").grid(row=0, column=2, sticky="ew", padx=(6, 0))

        workspace = ttk.Panedwindow(self.google_search_tab, orient="horizontal")
        workspace.grid(row=1, column=0, sticky="nsew")

        source_scroller = ttk.Frame(workspace, style="Panel.TFrame", width=220)
        source_scroller.columnconfigure(0, weight=1)
        source_scroller.rowconfigure(0, weight=1)
        source_canvas = tk.Canvas(source_scroller, background=self.palette["panel"], highlightthickness=0, width=205)
        source_scrollbar = ttk.Scrollbar(source_scroller, orient="vertical", command=source_canvas.yview)
        source_canvas.configure(yscrollcommand=source_scrollbar.set)
        source_canvas.grid(row=0, column=0, sticky="nsew")
        source_scrollbar.grid(row=0, column=1, sticky="ns")
        source_panel = ttk.Frame(source_canvas, style="Panel.TFrame", padding=12)
        source_window = source_canvas.create_window((0, 0), window=source_panel, anchor="nw")
        source_panel.bind("<Configure>", lambda _event: source_canvas.configure(scrollregion=source_canvas.bbox("all")))
        source_canvas.bind("<Configure>", lambda event: source_canvas.itemconfigure(source_window, width=event.width))
        source_panel.columnconfigure(0, weight=1)
        source_panel.rowconfigure(5, weight=1, minsize=72)
        workspace.add(source_scroller, weight=0)
        ttk.Label(source_panel, text="查询图片", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        library_hint = str(LOCAL_IMAGE_LIBRARY) if LOCAL_IMAGE_LIBRARY.is_dir() else "请选择本地图片"
        ttk.Label(source_panel, text=f"图片库：{library_hint}", style="Small.TLabel", wraplength=220).grid(row=1, column=0, sticky="ew", pady=(2, 8))
        preview_shell = tk.Frame(
            source_panel,
            background=self.palette["soft"],
            highlightthickness=1,
            highlightbackground=self.palette["border"],
        )
        preview_shell.grid(row=5, column=0, sticky="nsew", pady=(8, 10))
        preview_shell.columnconfigure(0, weight=1)
        preview_shell.rowconfigure(0, weight=1)
        self.google_source_preview = tk.Label(
            preview_shell,
            text="选择一张图片\n这里会显示查询预览",
            background=self.palette["soft"],
            foreground=self.palette["muted"],
            justify="center",
            font=("Microsoft YaHei UI", 10),
        )
        self.google_source_preview.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        self.google_source_preview.bind("<Configure>", self._handle_google_preview_resize)
        ttk.Label(source_panel, textvariable=self.google_source_name_var, style="Section.TLabel", wraplength=220).grid(row=6, column=0, sticky="ew")
        ttk.Label(source_panel, textvariable=self.google_source_info_var, style="Small.TLabel", wraplength=220).grid(row=7, column=0, sticky="ew", pady=(2, 8))
        self.google_choose_button = ttk.Button(source_panel, text="选择单张图片", command=self._choose_google_image)
        self.google_choose_button.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.google_folder_button = ttk.Button(source_panel, text="选择文件夹批量", command=self._choose_google_image_folder)
        self.google_folder_button.grid(row=3, column=0, sticky="ew", pady=(0, 8))
        search_options = ttk.Frame(source_panel, style="FlatPanel.TFrame")
        search_options.grid(row=9, column=0, sticky="ew", pady=(8, 0))
        search_options.columnconfigure(1, weight=1)
        ttk.Label(search_options, text="候选页面数", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Spinbox(search_options, from_=1, to=200, textvariable=self.google_limit_var, width=7).grid(row=0, column=1, sticky="e")
        ttk.Label(search_options, text="验证等待（秒）", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Spinbox(search_options, from_=0, to=300, textvariable=self.google_wait_var, width=7).grid(
            row=1, column=1, sticky="e", pady=(6, 0)
        )
        self.google_search_button = ttk.Button(
            source_panel,
            textvariable=self.google_search_button_var,
            style="Accent.TButton",
            command=self._search_google_image,
        )
        self.google_search_button.grid(row=4, column=0, sticky="ew")
        ttk.Label(
            source_panel,
            text="提示：AI 概览不代表图片匹配。图片过期请重新上传；人机验证请在浏览器完成。",
            style="Small.TLabel",
            wraplength=220,
            justify="left",
        ).grid(row=8, column=0, sticky="ew", pady=(8, 0))
        bind_canvas_mousewheel(source_canvas, source_panel)

        result_panel = ttk.Frame(workspace, style="Panel.TFrame", padding=12)
        result_panel.columnconfigure(0, weight=1)
        result_panel.rowconfigure(4, weight=1)
        workspace.add(result_panel, weight=1)
        result_header = ttk.Frame(result_panel, style="FlatPanel.TFrame")
        result_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        result_header.columnconfigure(1, weight=1)
        ttk.Label(result_header, text="候选页面", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(result_header, textvariable=self.google_result_summary_var, style="Small.TLabel").grid(row=0, column=1, sticky="e")
        ttk.Label(result_header, text="推荐搜索进度", style="Small.TLabel").grid(
            row=1, column=0, sticky="w", pady=(7, 0), padx=(0, 8)
        )
        self.google_search_progress = ttk.Progressbar(
            result_header,
            variable=self.google_search_progress_var,
            maximum=100,
            mode="determinate",
        )
        self.google_search_progress.grid(row=1, column=1, sticky="ew", pady=(7, 0))
        result_hint = ttk.Label(
            result_panel,
            text="这里只列出来源候选，AI 概览不代表图片匹配。请先打开网页核对；Pixiv、X 帖子可交给对应下载器。",
            style="Small.TLabel",
            wraplength=700,
            justify="left",
        )
        result_hint.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        result_panel.bind("<Configure>", lambda event: result_hint.configure(wraplength=max(280, event.width - 24)))

        columns = ("index", "source", "status", "site", "url")
        result_frame, self.google_result_tree = self._create_scrollable_tree(result_panel, columns)
        self.google_result_tree.configure(selectmode="extended")
        self.google_result_tree.heading("index", text="#")
        self.google_result_tree.heading("source", text="查询图片")
        self.google_result_tree.heading("status", text="状态")
        self.google_result_tree.heading("site", text="网站")
        self.google_result_tree.heading("url", text="候选页面 URL")
        self.google_result_tree.column("index", width=50, stretch=False, anchor="center")
        self.google_result_tree.column("source", width=130, stretch=False)
        self.google_result_tree.column("status", width=112, stretch=False, anchor="center")
        self.google_result_tree.column("site", width=140, stretch=False)
        self.google_result_tree.column("url", width=430)
        result_frame.grid(row=4, column=0, sticky="nsew")
        self.google_result_tree.tag_configure("even", background="#f8fafc")
        self.google_result_tree.tag_configure("odd", background=self.palette["panel"])
        self.google_result_tree.tag_configure("missing", background="#fff0f0", foreground="#a22929")
        self.google_result_tree.tag_configure("restricted", background="#fff8e6", foreground="#865d12")
        self.google_result_tree.tag_configure("blocked", background="#f2e9f3", foreground="#75417c")
        self.google_result_tree.bind("<<TreeviewSelect>>", lambda _event: self._update_google_result_summary())
        self.google_result_tree.bind("<Double-Button-1>", lambda _event: self._preview_selected_google_result())
        self.google_result_tree.bind("<Return>", lambda _event: self._preview_selected_google_result())
        self.google_result_tree.bind("<Control-a>", lambda _event: self._select_all_google_results())

        self.google_selection_detail_var = tk.StringVar(value="选中候选可查看跳转地址和黑名单状态。")
        self.google_detail_label = ttk.Label(result_panel, textvariable=self.google_selection_detail_var,
                                             style="Small.TLabel", wraplength=700, justify="left")
        self.google_detail_label.grid(row=3, column=0, sticky="ew", pady=(5, 0))
        self.google_detail_label.grid_remove()
        result_panel.bind("<Configure>", lambda event: self.google_detail_label.configure(wraplength=max(280, event.width - 24)), add="+")
        action_row = ttk.Frame(result_panel, style="FlatPanel.TFrame")
        action_row.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        action_row.columnconfigure(4, weight=1)
        self.google_select_all_button = ttk.Button(action_row, text="全选", style="Compact.TButton", command=self._select_all_google_results)
        self.google_select_all_button.grid(row=0, column=0, padx=(0, 6))
        self.google_clear_button = ttk.Button(action_row, text="取消选择", style="Compact.TButton", command=self._clear_google_selection)
        self.google_clear_button.grid(row=0, column=1, padx=(0, 6))
        self.google_preview_button = ttk.Button(action_row, text="打开网页", style="Compact.TButton", command=self._preview_selected_google_result)
        self.google_preview_button.grid(row=0, column=2, sticky="w")
        self.google_check_button = ttk.Button(action_row, text="检查链接", style="Compact.TButton", command=self._check_selected_google_results)
        self.google_check_button.grid(row=0, column=3, padx=(6, 0))
        self.google_mark_missing_button = ttk.Button(
            action_row, text="标记失效", style="Compact.TButton", command=self._toggle_selected_google_missing
        )
        self.google_mark_missing_button.grid(row=0, column=4, padx=(6, 0))
        self.google_export_button = ttk.Button(action_row, text="导出候选", style="Compact.TButton", command=self._export_google_results)
        self.google_export_button.grid(row=0, column=3, sticky="w", padx=(6, 0))
        self.google_more_menu = tk.Menu(action_row, tearoff=False)
        self.google_more_menu.add_command(label="检查链接", command=self._check_selected_google_results)
        self.google_more_menu.add_command(label="标记失效 / 恢复", command=self._toggle_selected_google_missing)
        self.google_more_menu.add_separator()
        self.google_more_menu.add_command(label="屏蔽选中页面/作品", command=self._block_selected_google_results)
        self.google_more_menu.add_command(label="核对 Pixiv 作者", command=self._inspect_selected_google_pixiv)
        self.google_more_menu.add_command(label="解除选中作品屏蔽", command=self._unblock_selected_google_results)
        self.google_more_button = ttk.Menubutton(
            action_row, text="更多操作", style="Compact.TButton", menu=self.google_more_menu
        )
        self.google_more_button.grid(row=0, column=4, sticky="e", padx=(6, 0))
        self.google_check_button.grid_remove()
        self.google_mark_missing_button.grid_remove()
        ttk.Label(action_row, text="每页最多文件", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(8, 0), padx=(0, 6))
        ttk.Spinbox(action_row, from_=1, to=200, textvariable=self.google_max_files_var, width=7).grid(row=1, column=1, sticky="w", pady=(8, 0))
        self.google_pixiv_button = ttk.Button(
            action_row, text="交给 Pixiv 下载", style="Compact.TButton", command=self._queue_selected_google_as_pixiv
        )
        self.google_pixiv_button.grid(row=1, column=2, sticky="w", padx=(6, 0), pady=(5, 0))
        self.google_twitter_button = ttk.Button(
            action_row, text="交给 X 下载", style="Compact.TButton", command=self._queue_selected_google_as_twitter
        )
        self.google_twitter_button.grid(row=1, column=3, sticky="w", padx=(6, 0), pady=(5, 0))
        self.google_crawl_button = ttk.Button(action_row, text="爬取选中页面", style="Compact.TButton", command=self._crawl_selected_google_results)
        self.google_crawl_button.grid(row=1, column=4, sticky="e", pady=(5, 0))
        self.google_block_button = ttk.Button(action_row, text="屏蔽选中页面/作品", style="Compact.TButton", command=self._block_selected_google_results)
        self.google_block_button.grid(row=2, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.google_pixiv_inspect_button = ttk.Button(action_row, text="核对 Pixiv 作者", style="Compact.TButton", command=self._inspect_selected_google_pixiv)
        self.google_pixiv_inspect_button.grid(row=2, column=2, columnspan=2, sticky="w", padx=(6, 0), pady=(8, 0))
        self.google_unblock_button = ttk.Button(action_row, text="解除选中作品屏蔽", style="Compact.TButton", command=self._unblock_selected_google_results)
        self.google_unblock_button.grid(row=2, column=4, columnspan=2, sticky="e", pady=(8, 0))
        self.google_block_button.grid_remove()
        self.google_pixiv_inspect_button.grid_remove()
        self.google_unblock_button.grid_remove()
        self.google_pixiv_inspecting = False
        self.google_results_generation = 0
        for button in (
            self.google_select_all_button,
            self.google_clear_button,
            self.google_preview_button,
            self.google_check_button,
            self.google_mark_missing_button,
            self.google_export_button,
            self.google_pixiv_button,
            self.google_twitter_button,
            self.google_crawl_button,
            self.google_block_button,
            self.google_pixiv_inspect_button,
            self.google_unblock_button,
        ):
            button.state(["disabled"])


    def _choose_google_image(self) -> None:
        current = Path(self.google_image_var.get().strip()).expanduser() if self.google_image_var.get().strip() else None
        initial_dir = current.parent if current and current.parent.is_dir() else LOCAL_IMAGE_LIBRARY
        if not initial_dir.is_dir():
            initial_dir = Path(self.output_dir_var.get()).expanduser()
        selected = filedialog.askopenfilename(
            title="选择用于相似搜索的图片",
            initialdir=str(initial_dir),
            filetypes=[
                ("图片", "*.jpg *.jpeg *.png *.webp *.gif *.bmp *.avif"),
                ("所有文件", "*.*"),
            ],
        )
        if selected:
            self.google_image_paths = [Path(selected)]
            self.google_image_var.set(selected)
            self._render_google_source_preview(Path(selected))
            if self.module_var.get() == "google_image":
                self._refresh_target_browser()


    def _choose_google_image_folder(self) -> None:
        current = Path(self.google_image_var.get().strip()).expanduser() if self.google_image_var.get().strip() else None
        initial_dir = current.parent if current and current.parent.is_dir() else LOCAL_IMAGE_LIBRARY
        if not initial_dir.is_dir():
            initial_dir = Path(self.output_dir_var.get()).expanduser()
        selected = filedialog.askdirectory(title="选择要批量搜索的图片文件夹", initialdir=str(initial_dir))
        if not selected:
            return
        folder = Path(selected)
        paths, truncated = _collect_image_files(folder)
        if not paths:
            messagebox.showwarning("没有图片", "所选文件夹及其子文件夹中没有支持的图片")
            return
        self.google_image_paths = paths
        self.google_image_var.set(str(paths[0]))
        self._render_google_source_preview(paths[0])
        self.google_source_name_var.set(f"批量文件夹：{folder.name}（{len(paths)} 张）")
        limit_note = f"；为控制浏览器负载，本批只取前 {GOOGLE_BATCH_IMAGE_LIMIT} 张" if truncated else ""
        self.google_source_info_var.set(f"递归读取 JPG、PNG、WEBP、GIF、BMP、AVIF{limit_note}")
        self.status_var.set(f"已选择批量图片 {len(paths)} 张")
        if truncated:
            self._append_log(f"批量图片超过上限，本轮按文件名顺序使用前 {GOOGLE_BATCH_IMAGE_LIMIT} 张")
        if self.module_var.get() == "google_image":
            self._refresh_target_browser()


    def _handle_google_preview_resize(self, _event=None) -> None:
        if self._google_preview_path is None or not self._google_preview_path.is_file():
            return
        if self._google_preview_resize_job is not None:
            try:
                self.after_cancel(self._google_preview_resize_job)
            except tk.TclError:
                pass
        self._google_preview_resize_job = self.after(100, lambda: self._render_google_source_preview(self._google_preview_path))


    def _render_google_source_preview(self, image_path: Path) -> None:
        self._google_preview_resize_job = None
        self._google_preview_path = image_path
        if not image_path.is_file():
            self.google_preview_image = None
            self.google_source_preview.configure(image="", text="找不到所选图片")
            self.google_source_name_var.set("图片不可用")
            self.google_source_info_var.set(f"文件不存在或已被移动：{image_path}")
            if self.google_search_cancel_event is None:
                self.google_search_button.state(["disabled"])
            return
        if self.google_search_cancel_event is None:
            self.google_search_button.state(["!disabled"])
        self.google_source_name_var.set(image_path.name)
        if Image is None or ImageTk is None:
            self.google_preview_image = None
            self.google_source_preview.configure(image="", text="已选择图片\n当前环境未安装 Pillow")
            self.google_source_info_var.set(self._format_size(image_path.stat().st_size))
            return
        try:
            with Image.open(image_path) as opened:
                opened.seek(0)
                original_size = opened.size
                preview = ImageOps.exif_transpose(opened).copy() if ImageOps is not None else opened.copy()
            max_width = max(150, self.google_source_preview.winfo_width() - 16)
            max_height = max(170, self.google_source_preview.winfo_height() - 16)
            preview.thumbnail((max_width, max_height))
            self.google_preview_image = ImageTk.PhotoImage(preview)
            self.google_source_preview.configure(image=self.google_preview_image, text="")
            self.google_source_info_var.set(
                f"{original_size[0]} × {original_size[1]}  ·  {self._format_size(image_path.stat().st_size)}"
            )
        except Exception as exc:  # noqa: BLE001
            self.google_preview_image = None
            self.google_source_preview.configure(image="", text="图片预览不可用")
            self.google_source_info_var.set(str(exc))


    def _search_google_image(self) -> None:
        image_value = self.google_image_var.get().strip()
        image_path = Path(image_value).expanduser()
        configured_paths = list(self.google_image_paths)
        if not configured_paths and image_value:
            configured_paths = [image_path]
        missing_paths = [path for path in configured_paths if not path.is_file()]
        paths = [path for path in configured_paths if path.is_file()]
        if not paths and image_path.is_file():
            paths = [image_path]
        if not paths:
            missing_text = f"\n\n原路径：{missing_paths[0]}" if missing_paths else ""
            messagebox.showwarning(
                "查询图片不存在",
                "原查询图片可能已被移动、改名或删除。旧候选仍可打开，但不能重新搜索；请重新选择图片。"
                + missing_text,
            )
            if configured_paths:
                self._render_google_source_preview(configured_paths[0])
            return
        if missing_paths:
            self._append_log(f"批量相似搜索跳过 {len(missing_paths)} 张已移动或删除的查询图片")
            self.status_var.set(f"已跳过 {len(missing_paths)} 张不存在的图片，继续搜索其余 {len(paths)} 张")
        self.google_image_paths = paths
        if any(path.suffix.lower() not in IMAGE_EXTENSIONS for path in paths):
            messagebox.showwarning("格式不支持", "请选择 JPG、PNG、WEBP、GIF、BMP 或 AVIF 图片")
            return
        if len(paths) == 1:
            self._render_google_source_preview(paths[0])
        try:
            limit = max(1, min(int(self.google_limit_var.get()), 200))
        except (TypeError, ValueError, tk.TclError):
            messagebox.showwarning("候选数无效", "候选数应为 1 到 200")
            return
        try:
            manual_wait_seconds = max(0, min(int(self.google_wait_var.get()), 300))
        except (TypeError, ValueError, tk.TclError):
            messagebox.showwarning("等待时间无效", "人机验证等待应为 0 到 300 秒")
            return
        self.google_search_cancel_event = threading.Event()
        cancel_event = self.google_search_cancel_event
        self.google_search_button.state(["disabled"])
        self.google_choose_button.state(["disabled"])
        self.google_folder_button.state(["disabled"])
        self.google_search_button_var.set("正在搜索，请稍候…")
        self.google_result_summary_var.set("正在连接 Google Lens")
        self.google_search_progress_var.set(3.0)
        self._show_google_results([])
        self.google_result_summary_var.set("正在连接 Google Lens")
        self.status_var.set("正在搜索相似页面")
        self._append_log(
            f"开始 Google 相似图片搜索: {paths[0].name}"
            if len(paths) == 1
            else f"开始 Google 文件夹批量相似搜索: {len(paths)} 张图片"
        )

        def worker() -> None:
            combined: list[dict] = []
            seen_urls: set[str] = set()
            processed = 0
            try:
                adapter = self.manager.get_adapter("google_image")
                for index, source in enumerate(paths, 1):
                    if cancel_event.is_set() or len(combined) >= GOOGLE_BATCH_RESULT_LIMIT:
                        break
                    self.ui_queue.put(
                        (
                            "google_search_progress",
                            {
                                "percent": 5.0 + (index - 1) / max(1, len(paths)) * 90.0,
                                "message": f"正在获取推荐 {index}/{len(paths)}：{source.name}",
                            },
                        )
                    )
                    self.ui_queue.put(
                        ("google_search_status", f"正在搜索 {index}/{len(paths)}：{source.name}")
                    )
                    rows = adapter.search_targets(
                        str(source),
                        limit=min(limit, GOOGLE_BATCH_RESULT_LIMIT - len(combined)),
                        options={
                            "headless": False,
                            "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
                            "task_id": "google-ui-search",
                            "cancel_event": cancel_event,
                            "manual_wait_seconds": manual_wait_seconds,
                            "on_status": lambda message, current=index, name=source.name: self.ui_queue.put(
                                ("google_search_status", f"[{current}/{len(paths)} {name}] {message}")
                            ),
                        },
                    )
                    processed = index
                    for row in rows:
                        url = str(row.get("url") or "").strip()
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        combined.append({**row, "source_image": source.name, "source_image_path": str(source.resolve())})
                        if len(combined) >= GOOGLE_BATCH_RESULT_LIMIT:
                            break
                    self.ui_queue.put(
                        (
                            "google_search_progress",
                            {
                                "percent": 5.0 + index / max(1, len(paths)) * 90.0,
                        "message": f"已搜索图片 {index}/{len(paths)}，累计 {len(combined)} 个来源候选",
                            },
                        )
                    )
            except Exception as exc:  # noqa: BLE001
                if combined:
                    self.ui_queue.put(
                        (
                            "google_search_loaded",
                            {
                                "rows": combined,
                                "processed": processed,
                                "total": len(paths),
                                "partial_reason": str(exc),
                            },
                        )
                    )
                    return
                self.ui_queue.put(("google_search_error", str(exc)))
                return
            self.ui_queue.put(
                (
                    "google_search_loaded",
                    {
                        "rows": combined,
                        "processed": processed,
                        "total": len(paths),
                        "cancelled": cancel_event.is_set(),
                        "result_limited": len(combined) >= GOOGLE_BATCH_RESULT_LIMIT,
                    },
                )
            )

        threading.Thread(target=worker, name="google-similarity-search", daemon=True).start()


    @staticmethod
    def _google_source_missing(row: dict) -> bool:
        source_path = str(row.get("source_image_path") or "").strip()
        return bool(source_path and not Path(source_path).expanduser().is_file())


    def _google_result_status(self, row: dict) -> str:
        if self._google_result_blocked(row):
            return "屏蔽（跳转）" if row.get("_blocked_via_redirect") else "黑名单屏蔽"
        availability = str(row.get("availability") or "unknown")
        label = {
            "available": "可访问",
            "missing": "链接已失效",
            "restricted": "登录/访问受限",
            "temporary": "网站暂时异常",
            "unknown": "待检查",
        }.get(availability, "待检查")
        if self._google_source_missing(row):
            return f"{label}；源图不存在"
        return label

    def _google_result_blocked(self, row: dict) -> bool:
        if "_blocked" in row:
            return bool(row["_blocked"])
        return GoogleSearchTabMixin._google_row_blocked_by_rules(self, row)

    def _google_row_blocked_by_rules(self, row: dict, blocked_accounts=None, blocked_works=None) -> bool:
        original = str(row.get("url") or "").strip()
        final = str(row.get("final_url") or "").strip()
        inspected_work_id = str(row.get("pixiv_inspected_work_id") or "")
        author_id = str(row.get("author_id") or "")
        original_author = author_id if not inspected_work_id or GoogleSearchTabMixin._pixiv_artwork_id(original) == inspected_work_id else ""
        final_author = author_id if not inspected_work_id or GoogleSearchTabMixin._pixiv_artwork_id(final) == inspected_work_id else ""
        if original and self.manager.blocklist.is_blocked(
            "google_image", original, author_id=original_author,
            blocked_accounts=blocked_accounts, blocked_works=blocked_works
        ):
            row["_blocked_via_redirect"] = False
            return True
        redirected = bool(final and final != original and self.manager.blocklist.is_blocked(
            "google_image", final, author_id=final_author,
            blocked_accounts=blocked_accounts, blocked_works=blocked_works
        ))
        row["_blocked_via_redirect"] = redirected
        return redirected

    def _refresh_google_blocklist(self) -> None:
        if not hasattr(self, "google_result_rows"):
            return
        accounts = self.manager.blocklist.blocked_accounts()
        works = self.manager.blocklist.blocked_works()
        for row in self.google_result_rows:
            row["_blocked"] = self._google_row_blocked_by_rules(row, accounts, works)
        for index in range(len(self.google_result_rows)):
            self._refresh_google_result_row(index)
        self._update_google_result_summary()


    def _refresh_google_result_row(self, index: int) -> None:
        if index < 0 or index >= len(self.google_result_rows):
            return
        item_id = str(index)
        if not self.google_result_tree.exists(item_id):
            return
        row = self.google_result_rows[index]
        url = str(row.get("url") or "").strip()
        host = urlparse(url).hostname or ""
        availability = str(row.get("availability") or "unknown")
        tag = "blocked" if self._google_result_blocked(row) else (availability if availability in {"missing", "restricted"} else ("even" if index % 2 == 0 else "odd"))
        self.google_result_tree.item(
            item_id,
            values=(index + 1, str(row.get("source_image") or ""), self._google_result_status(row), host, url),
            tags=(tag,),
        )


    def _show_google_results(self, rows: list[dict]) -> None:
        self.google_results_generation += 1
        self.google_result_rows = [dict(row) for row in rows]
        accounts = self.manager.blocklist.blocked_accounts()
        works = self.manager.blocklist.blocked_works()
        for row in self.google_result_rows:
            row["_blocked"] = self._google_row_blocked_by_rules(row, accounts, works)
        self.platform_candidate_rows["google_image"] = list(rows)
        for item in self.google_result_tree.get_children():
            self.google_result_tree.delete(item)
        for index, row in enumerate(self.google_result_rows):
            url = str(row.get("url") or "").strip()
            host = urlparse(url).hostname or ""
            source_image = str(row.get("source_image") or "")
            status = self._google_result_status(row)
            availability = str(row.get("availability") or "unknown")
            tag = "blocked" if self._google_result_blocked(row) else (availability if availability in {"missing", "restricted"} else ("even" if index % 2 == 0 else "odd"))
            self.google_result_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(index + 1, source_image, status, host, url),
                tags=(tag,),
            )
        self._update_google_result_summary()
        if self.module_var.get() == "google_image":
            self._refresh_platform_tabs()


    def _update_google_result_summary(self) -> None:
        total = len(self.google_result_rows)
        selected_rows = self._selected_google_rows()
        selected = len(selected_rows)
        if not total:
            self.google_result_summary_var.set("没有候选页面")
        else:
            source_count = len({str(row.get("source_image") or "") for row in self.google_result_rows})
            source_text = f" · 图片 {source_count} 张" if source_count else ""
            missing_count = sum(str(row.get("availability") or "") == "missing" for row in self.google_result_rows)
            blocked_count = sum(self._google_result_blocked(row) for row in self.google_result_rows)
            missing_sources = sum(self._google_source_missing(row) for row in self.google_result_rows)
            missing_text = f" · 失效链接 {missing_count}" if missing_count else ""
            blocked_text = f" · 黑名单 {blocked_count}" if blocked_count else ""
            source_missing_text = f" · 源图不存在 {missing_sources}" if missing_sources else ""
            self.google_result_summary_var.set(
                f"共 {total} 个 · 已选 {selected} 个{source_text}{missing_text}{blocked_text}{source_missing_text}"
            )
        if not selected_rows:
            self.google_selection_detail_var.set("选中候选可查看跳转地址和黑名单状态。")
            self.google_detail_label.grid_remove()
        elif len(selected_rows) == 1:
            self.google_detail_label.grid()
            row = selected_rows[0]
            original = str(row.get("url") or "").strip()
            final = str(row.get("final_url") or "").strip()
            detail = f"原地址：{original}"
            if final and final != original:
                detail += f"\n跳转地址：{final}"
            if row.get("author_id"):
                detail += f"\nPixiv 作者：{row.get('author_name') or '未知名称'}（ID {row['author_id']}）"
            elif row.get("pixiv_inspect_error"):
                detail += f"\nPixiv 作者读取失败：{row['pixiv_inspect_error']}"
            if self._google_result_blocked(row):
                detail += "\n已命中黑名单，下载时会跳过。"
            self.google_selection_detail_var.set(detail)
        else:
            self.google_detail_label.grid()
            selected_blocked = sum(self._google_result_blocked(row) for row in selected_rows)
            self.google_selection_detail_var.set(f"已选 {selected} 个候选，其中黑名单屏蔽 {selected_blocked} 个；下载时会自动跳过。")
        self.google_select_all_button.state(["!disabled"] if total else ["disabled"])
        self.google_clear_button.state(["!disabled"] if selected else ["disabled"])
        self.google_preview_button.state(["!disabled"] if selected else ["disabled"])
        checking = self.google_link_check_cancel_event is not None
        self.google_check_button.configure(text="停止检查" if checking else "检查链接")
        self.google_check_button.state(["!disabled"] if selected or checking else ["disabled"])
        self.google_mark_missing_button.state(["!disabled"] if selected and not checking else ["disabled"])
        all_missing = bool(selected_rows) and all(str(row.get("availability") or "") == "missing" for row in selected_rows)
        self.google_mark_missing_button.configure(text="恢复待检查" if all_missing else "标记失效")
        self.google_export_button.state(["!disabled"] if total else ["disabled"])
        self.google_block_button.state(["!disabled"] if any(not self._google_result_blocked(row) for row in selected_rows) else ["disabled"])
        work_keys = self.manager.blocklist.blocked_works()
        self.google_unblock_button.state(["!disabled"] if any(
            work_from_target("google_image", str(row.get(key) or "")) in work_keys
            for row in selected_rows for key in ("url", "final_url")
        ) else ["disabled"])
        downloadable = any(str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row) for row in selected_rows)
        self.google_crawl_button.state(["!disabled"] if downloadable else ["disabled"])
        pixiv_downloadable = any(
            str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row) and self._pixiv_result_target(row)
            for row in selected_rows
        )
        self.google_pixiv_button.state(["!disabled"] if pixiv_downloadable else ["disabled"])
        twitter_downloadable = any(
            str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row)
            and self._twitter_result_target(row) for row in selected_rows
        )
        self.google_twitter_button.state(["!disabled"] if twitter_downloadable else ["disabled"])
        inspectable = any(self._pixiv_result_target(row) for row in selected_rows)
        self.google_pixiv_inspect_button.state(["!disabled"] if inspectable and not self.google_pixiv_inspecting else ["disabled"])
        menu_states = {
            "检查链接": bool(selected or checking),
            "标记失效 / 恢复": bool(selected and not checking),
            "屏蔽选中页面/作品": any(not self._google_result_blocked(row) for row in selected_rows),
            "核对 Pixiv 作者": bool(inspectable and not self.google_pixiv_inspecting),
            "解除选中作品屏蔽": any(
                work_from_target("google_image", str(row.get(key) or "")) in work_keys
                for row in selected_rows for key in ("url", "final_url")
            ),
        }
        for label, enabled in menu_states.items():
            self.google_more_menu.entryconfigure(label, state="normal" if enabled else "disabled")


    def _select_all_google_results(self) -> None:
        children = self.google_result_tree.get_children()
        if children:
            self.google_result_tree.selection_set(children)
        self._update_google_result_summary()


    def _clear_google_selection(self) -> None:
        self.google_result_tree.selection_remove(self.google_result_tree.selection())
        self._update_google_result_summary()


    def _selected_google_rows(self) -> list[dict]:
        rows = []
        for item_id in self.google_result_tree.selection():
            try:
                rows.append(self.google_result_rows[int(item_id)])
            except (ValueError, IndexError):
                continue
        return rows

    def _block_selected_google_results(self) -> None:
        rows = [row for row in self._selected_google_rows() if not self._google_result_blocked(row)]
        if not rows:
            return
        if not messagebox.askyesno("屏蔽相似图片候选", f"屏蔽选中的 {len(rows)} 个具体页面或作品？作者账号不会因此被屏蔽。"):
            return
        try:
            for row in rows:
                self.manager.blocklist.add_work("google_image", str(row.get("url") or ""), label=str(row.get("title") or ""))
        except (OSError, ValueError) as exc:
            messagebox.showerror("屏蔽失败", str(exc))
            return
        self._refresh_google_blocklist()
        self._refresh_platform_tabs()

    def _unblock_selected_google_results(self) -> None:
        keys = {work_from_target("google_image", str(row.get(key) or ""))
                for row in self._selected_google_rows() for key in ("url", "final_url")}
        matches = [item for item in self.manager.blocklist.works()
                   if (item.get("platform"), item.get("work")) in keys]
        for item in matches:
            self.manager.blocklist.remove_work(str(item.get("id") or ""))
        self._refresh_google_blocklist()
        self._refresh_platform_tabs()


    def _check_selected_google_results(self) -> None:
        if self.google_link_check_cancel_event is not None:
            self.google_link_check_cancel_event.set()
            self.google_check_button.configure(text="正在停止…")
            self.status_var.set("正在停止候选链接检查")
            return
        entries: list[tuple[int, str]] = []
        for item_id in self.google_result_tree.selection():
            if str(item_id).isdigit() and int(item_id) < len(self.google_result_rows):
                index = int(item_id)
                url = str(self.google_result_rows[index].get("url") or "").strip()
                if url:
                    entries.append((index, url))
        if not entries:
            messagebox.showwarning("未选择候选", "请先选择要检查的候选链接")
            return
        self._start_google_link_check(entries)


    def _start_google_link_check(self, entries: list[tuple[int, str]]) -> None:
        if not entries or self.google_link_check_cancel_event is not None:
            return
        cancel_event = threading.Event()
        self.google_link_check_cancel_event = cancel_event
        self._update_google_result_summary()
        self.status_var.set(f"正在检查 {len(entries)} 个候选链接；404/410 会标记为失效")

        def worker() -> None:
            checked = 0
            missing = 0
            restricted = 0
            proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
            for index, url in entries:
                if cancel_event.is_set():
                    break
                try:
                    result = check_page_availability(url, proxy_url=proxy_url)
                except Exception as exc:  # noqa: BLE001
                    result = {
                        "availability": "unknown",
                        "status_code": None,
                        "final_url": url,
                        "message": str(exc).splitlines()[0],
                    }
                checked += 1
                missing += int(result.get("availability") == "missing")
                restricted += int(result.get("availability") == "restricted")
                self.ui_queue.put(("google_link_checked", (index, url, result)))
            self.ui_queue.put(
                (
                    "google_link_check_done",
                    {
                        "checked": checked,
                        "missing": missing,
                        "restricted": restricted,
                        "cancelled": cancel_event.is_set(),
                    },
                )
            )

        threading.Thread(target=worker, name="google-link-check", daemon=True).start()


    def _toggle_selected_google_missing(self) -> None:
        selections = [str(item_id) for item_id in self.google_result_tree.selection() if str(item_id).isdigit()]
        indices = [int(item_id) for item_id in selections if int(item_id) < len(self.google_result_rows)]
        if not indices:
            return
        restore = all(str(self.google_result_rows[index].get("availability") or "") == "missing" for index in indices)
        for index in indices:
            row = self.google_result_rows[index]
            if restore:
                row["availability"] = "unknown"
                row.pop("availability_source", None)
                row.pop("status_code", None)
            else:
                row["availability"] = "missing"
                row["availability_source"] = "manual"
                row["status_code"] = None
            self._refresh_google_result_row(index)
        self.platform_candidate_rows["google_image"] = list(self.google_result_rows)
        self._update_google_result_summary()
        self.status_var.set(
            f"已将 {len(indices)} 个候选恢复为待检查" if restore else f"已手动标记 {len(indices)} 个失效候选"
        )


    def _export_google_results(self) -> None:
        rows = self._selected_google_rows() or list(self.google_result_rows)
        if not rows:
            messagebox.showwarning("没有候选", "请先完成相似图片搜索")
            return
        selected = filedialog.asksaveasfilename(
            title="导出相似页面候选",
            defaultextension=".csv",
            initialfile="google-image-candidates.csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json"), ("纯 URL 文本", "*.txt")],
        )
        if not selected:
            return
        try:
            count = export_url_candidates([{**row, "blocked": self._google_result_blocked(row)} for row in rows], Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", self._friendly_error(exc, "候选导出"))
            return
        message = f"已导出 {count} 个候选页面：{selected}"
        self.status_var.set(message)
        self._append_log(message)


    def _preview_selected_google_result(self) -> None:
        rows = self._selected_google_rows()
        if not rows:
            messagebox.showwarning("未选择候选", "请先选择一个候选页面")
            return
        url = str(rows[0].get("url") or "").strip()
        if not url:
            return
        if str(rows[0].get("availability") or "") == "missing" and not messagebox.askyesno(
            "候选链接已失效",
            "这个候选已被 HTTP 404/410 或手动操作标记为失效。仍要在浏览器中打开吗？",
        ):
            return
        if webbrowser.open(url, new=2):
            self.status_var.set(f"已在浏览器打开候选网页：{url}")
            self._append_log(f"打开相似图片候选网页: {url}")
            if self.google_link_check_cancel_event is None and str(rows[0].get("availability_source") or "") != "manual":
                try:
                    index = self.google_result_rows.index(rows[0])
                except ValueError:
                    pass
                else:
                    self._start_google_link_check([(index, url)])
        else:
            messagebox.showerror("打开失败", url)


    @staticmethod
    def _download_module_for_target(module_id: str, target: str) -> str:
        return download_module_for_target(module_id, target)

    @staticmethod
    def _pixiv_artwork_id(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return ""
        host = parsed.hostname.casefold().removeprefix("www.")
        if host != "pixiv.net":
            return ""
        match = re.search(r"/artworks/(\d+)(?:/|$)", parsed.path)
        return match.group(1) if match else ""

    @staticmethod
    def _pixiv_result_target(row: dict) -> str:
        for key in ("final_url", "url"):
            url = str(row.get(key) or "").strip()
            if GoogleSearchTabMixin._pixiv_artwork_id(url):
                return url
        return ""

    @staticmethod
    def _twitter_post_target(url: str) -> str:
        parsed = urlparse(str(url or "").strip())
        host = str(parsed.hostname or "").casefold()
        if parsed.scheme not in {"http", "https"} or host not in {
            "x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"
        }:
            return ""
        match = re.match(r"^/([A-Za-z0-9_]{1,15})/status/(\d+)(?:/|$)", parsed.path)
        return f"https://x.com/{match.group(1)}/status/{match.group(2)}" if match else ""

    @staticmethod
    def _twitter_result_target(row: dict) -> str:
        for key in ("final_url", "url"):
            target = GoogleSearchTabMixin._twitter_post_target(str(row.get(key) or ""))
            if target:
                return target
        return ""

    def _inspect_selected_google_pixiv(self) -> None:
        if self.google_pixiv_inspecting:
            return
        entries: dict[str, dict] = {}
        for item_id in self.google_result_tree.selection():
            if not str(item_id).isdigit() or int(item_id) >= len(self.google_result_rows):
                continue
            index = int(item_id)
            target = self._pixiv_result_target(self.google_result_rows[index])
            work_id = self._pixiv_artwork_id(target)
            if work_id:
                entry = entries.setdefault(work_id, {"target": target, "indexes": []})
                entry["indexes"].append(index)
        if not entries:
            messagebox.showinfo("核对 Pixiv 作者", "请先选中 Pixiv 作品候选。")
            return
        if len(entries) > 30:
            messagebox.showwarning("核对 Pixiv 作者", "一次最多核对 30 件 Pixiv 作品，请缩小选择范围。")
            return
        generation = self.google_results_generation
        proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
        self.google_pixiv_inspecting = True
        self._update_google_result_summary()
        self.status_var.set(f"正在核对 {len(entries)} 件 Pixiv 作品的作者…")

        def worker() -> None:
            checked = 0
            failed = 0
            try:
                adapter = self.manager.get_adapter("pixiv")
                for work_id, entry in entries.items():
                    try:
                        metadata = adapter.inspect_artwork(entry["target"], proxy_url=proxy_url)
                    except Exception as exc:  # noqa: BLE001
                        failed += 1
                        metadata = {"error": str(exc).splitlines()[0]}
                    else:
                        checked += 1
                    self.ui_queue.put(("google_pixiv_inspected", {
                        "generation": generation, "work_id": work_id,
                        "indexes": entry["indexes"], "metadata": metadata,
                    }))
            except Exception as exc:  # noqa: BLE001
                failed += len(entries)
                self.ui_queue.put(("google_search_status", f"Pixiv 作者核对失败：{str(exc).splitlines()[0]}"))
            finally:
                self.ui_queue.put(("google_pixiv_inspect_done", {
                    "generation": generation, "checked": checked, "failed": failed,
                }))

        threading.Thread(target=worker, name="google-pixiv-author-check", daemon=True).start()

    def _queue_selected_google_as_pixiv(self) -> None:
        rows = [
            row for row in self._selected_google_rows()
            if str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row) and self._pixiv_result_target(row)
        ]
        if not rows:
            messagebox.showwarning("没有 Pixiv 作品", "选中项里没有可用的 Pixiv 作品详情页（/artworks/ID）。")
            return
        if not messagebox.askyesno(
            "确认使用 Pixiv 下载",
            f"将明确使用 Pixiv 插件处理 {len(rows)} 个选中作品；不会按网址自动切换平台。是否继续？",
        ):
            return
        callbacks = self._task_callbacks()
        created = 0
        for row in rows:
            url = self._pixiv_result_target(row)
            try:
                task = self.manager.start_task(
                    "pixiv",
                    url,
                    Path(self.output_dir_var.get()),
                    {
                        "input_kind": "work",
                        "content_scope": "作品 ID",
                        "search_mode": "作品 ID",
                        "max_works": 1,
                        "filter_ai": bool(self.storage.get_setting("pixiv_filter_ai", True)),
                        "pixiv_visibility": str(self.storage.get_setting("pixiv_visibility", "show") or "show"),
                        "age_mode": str(self.storage.get_setting("pixiv_age_mode", "all") or "all"),
                        "types": self._selected_types(),
                        "discovery_source": "google_image_explicit_pixiv",
                        "author_id": str(row.get("author_id") or ""),
                        "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
                        "retries": _bounded_int(self.storage.get_setting("task_retries", 1), 1, 0, 5),
                        **self._saved_format_options(),
                        **self._saved_archive_options(),
                    },
                    callbacks,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"Pixiv 作品加入队列失败：{url} | {exc}")
                continue
            created += 1
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
        self._refresh_tasks()
        self.status_var.set(f"已将 {created} 个 Pixiv 作品加入下载队列")

    def _queue_selected_google_as_twitter(self) -> None:
        targets = list(dict.fromkeys(
            self._twitter_result_target(row) for row in self._selected_google_rows()
            if str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row)
        ))
        targets = [target for target in targets if target]
        if not targets:
            messagebox.showwarning("没有 X 帖子", "请选中可用的 X 帖子详情页（/用户名/status/ID）。")
            return
        if not messagebox.askyesno(
            "确认使用 X 下载", f"将使用 X 原生下载器处理 {len(targets)} 个选中帖子。是否继续？"
        ):
            return
        callbacks = self._task_callbacks()
        created = 0
        for target in targets:
            try:
                task = self.manager.start_task(
                    "twitter", target, Path(self.output_dir_var.get()),
                    {
                        "types": self._selected_types(),
                        "discovery_source": "google_image_explicit_twitter",
                        "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
                        "retries": _bounded_int(self.storage.get_setting("task_retries", 1), 1, 0, 5),
                        **self._saved_format_options(),
                        **self._saved_archive_options(),
                    }, callbacks,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"X 帖子加入队列失败：{target} | {exc}")
                continue
            created += 1
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
        self._refresh_tasks()
        self.status_var.set(f"已将 {created} 个 X 帖子加入下载队列")


    def _crawl_selected_google_results(self) -> None:
        rows = self._selected_google_rows()
        if not rows:
            messagebox.showwarning("未选择候选", "请先选择要爬取的候选页面")
            return
        missing_rows = [row for row in rows if str(row.get("availability") or "") == "missing"]
        blocked_rows = [row for row in rows if self._google_result_blocked(row)]
        rows = [row for row in rows if str(row.get("availability") or "") != "missing" and not self._google_result_blocked(row)]
        if blocked_rows:
            self._append_log(f"相似图片候选中跳过黑名单 {len(blocked_rows)} 项")
        if not rows:
            messagebox.showwarning("没有可下载候选", "所选候选均已失效或命中黑名单，不会建立下载任务。")
            return
        if missing_rows:
            self._append_log(f"建立下载任务时跳过 {len(missing_rows)} 个已标记失效的候选")
        try:
            max_files = max(1, min(int(self.google_max_files_var.get()), 200))
        except (TypeError, ValueError, tk.TclError):
            messagebox.showwarning("文件数无效", "每页最多文件应为 1 到 200")
            return
        callbacks = self._task_callbacks()
        created = 0
        routed: dict[str, int] = {}
        for row in rows:
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            module_id = self._download_module_for_target("google_image", url)
            download_types = download_types_for_discovered_target(
                "google_image", module_id, self._selected_types()
            )
            try:
                task = self.manager.start_task(
                    module_id,
                    url,
                    Path(self.output_dir_var.get()),
                    {
                        "max_files": max_files,
                        "types": download_types,
                        "discovery_source": "google_image",
                        "author_id": str(row.get("author_id") or ""),
                        "pixiv_work_id": str(row.get("pixiv_inspected_work_id") or ""),
                        "browser_render": True,
                        "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
                        "retries": _bounded_int(self.storage.get_setting("task_retries", 1), 1, 0, 5),
                        "page_read_timeout": max(30, min(int(self.webpage_timeout_var.get()), 600)),
                        **self._saved_format_options(),
                        **self._saved_archive_options(),
                    },
                    callbacks,
                )
            except Exception as exc:  # noqa: BLE001
                self._append_log(f"候选页面加入失败: {url} | {exc}")
                continue
            created += 1
            routed[module_id] = routed.get(module_id, 0) + 1
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
            self._append_log(
                f"候选页面已加入 {self.manager.get_adapter(module_id).display_name}: "
                f"{task.task_id} | {url} | 浏览器提取页面图片"
            )
        if not created:
            messagebox.showerror("加入失败", "没有候选页面成功加入任务队列")
            return
        route_text = "、".join(f"{self.manager.get_adapter(key).display_name} {count}" for key, count in routed.items())
        self.status_var.set(f"已加入 {created} 个下载任务：{route_text}")
        self.notebook.select(self.tasks_tab)
        self._refresh_tasks()


