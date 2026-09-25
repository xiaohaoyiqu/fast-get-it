from __future__ import annotations

import os
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

from software_app.core.models import IMAGE_EXTENSIONS
from software_app.crawlers.common import normalized_path_component, path_component_error
from software_app.ui.desktop_support import (
    LIBRARY_RENDER_LIMIT,
    library_query_tokens,
    library_text_matches,
    normalized_search_text,
    select_treeview_row_at_event,
    send_path_to_recycle_bin as _send_path_to_recycle_bin,
)

try:
    from PIL import Image, ImageOps, ImageTk
except Exception:
    Image = None
    ImageOps = None
    ImageTk = None


class LibraryTabMixin:
    def _build_library_tab(self) -> None:
        self.library_tab.columnconfigure(0, weight=1)
        self.library_tab.rowconfigure(0, weight=0)
        self.library_tab.rowconfigure(1, weight=3)
        self.library_tab.rowconfigure(2, weight=1)

        top_frame = ttk.Frame(self.library_tab, style="Panel.TFrame", padding=4)
        top_frame.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        top_frame.columnconfigure(0, weight=1)
        top_frame.rowconfigure(1, weight=1)

        columns = ("module", "folder", "files", "images", "size")
        folder_header = ttk.Frame(top_frame, style="Panel.TFrame")
        folder_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        folder_header.columnconfigure(2, weight=1)
        ttk.Label(folder_header, text="文件夹汇总", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(folder_header, text="搜索", style="Panel.TLabel").grid(row=0, column=1, sticky="e", padx=(12, 4))
        self.library_search_entry = ttk.Entry(folder_header, textvariable=self.library_search_var)
        self.library_search_entry.grid(row=0, column=2, sticky="ew", padx=(0, 4))
        ttk.Button(folder_header, text="清除搜索", command=lambda: self.library_search_var.set("")).grid(
            row=0, column=3, padx=(2, 6)
        )
        ttk.Button(folder_header, text="刷新 / 扫描", command=self._scan_output_dir).grid(row=0, column=4, padx=(6, 0))
        ttk.Button(folder_header, text="打开所选文件夹", command=self._open_selected_folder).grid(row=0, column=5, padx=(6, 0))
        self.library_status_label = ttk.Label(folder_header, textvariable=self.library_status_var, style="Small.TLabel")
        self.library_status_label.grid(row=1, column=0, columnspan=6, sticky="ew", pady=(5, 0))
        self.library_search_var.trace_add("write", self._schedule_library_filter)
        folder_tree_frame, self.folder_tree = self._create_scrollable_tree(top_frame, columns, height=1)
        self.folder_tree.heading("module", text="平台")
        self.folder_tree.heading("folder", text="作者/文件夹")
        self.folder_tree.heading("files", text="文件")
        self.folder_tree.heading("images", text="图片")
        self.folder_tree.heading("size", text="大小")
        self.folder_tree.column("module", width=90, stretch=False)
        self.folder_tree.column("folder", width=320)
        self.folder_tree.column("files", width=70, stretch=False)
        self.folder_tree.column("images", width=70, stretch=False)
        self.folder_tree.column("size", width=90, stretch=False)
        folder_tree_frame.grid(row=1, column=0, sticky="nsew")
        self.folder_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_selected_folder())

        file_frame = ttk.Frame(self.library_tab, style="Panel.TFrame", padding=8)
        file_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 8))
        file_frame.columnconfigure(0, weight=1)
        file_frame.rowconfigure(1, weight=1)
        file_header = ttk.Frame(file_frame, style="Panel.TFrame")
        file_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        file_header.columnconfigure(1, weight=1)
        ttk.Label(file_header, text="文件列表", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(file_header, textvariable=self.library_file_status_var, style="Small.TLabel").grid(
            row=0, column=1, sticky="e", padx=(8, 4)
        )
        ttk.Button(file_header, text="打开", command=self._open_selected_file).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(file_header, text="复制路径", command=self._copy_selected_file_paths).grid(row=0, column=3, padx=(6, 0))
        ttk.Button(file_header, text="重命名", command=self._rename_selected_file).grid(row=0, column=4, padx=(6, 0))
        ttk.Button(file_header, text="移到回收站", command=self._recycle_selected_files).grid(row=0, column=5, padx=(6, 0))
        self._library_preview_override: bool | None = None
        self.library_preview_toggle = ttk.Button(file_header, text="显示预览", command=self._toggle_library_preview)
        self.library_preview_toggle.grid(row=0, column=6, padx=(6, 0))
        file_columns = ("type", "name", "size")
        file_tree_frame, self.folder_file_tree = self._create_scrollable_tree(file_frame, file_columns, height=2)
        self.folder_file_tree.configure(selectmode="extended")
        self.folder_file_tree.heading("type", text="类型")
        self.folder_file_tree.heading("name", text="名字")
        self.folder_file_tree.heading("size", text="大小")
        self.folder_file_tree.column("type", width=90, stretch=False)
        self.folder_file_tree.column("name", width=620)
        self.folder_file_tree.column("size", width=100, stretch=False)
        file_tree_frame.grid(row=1, column=0, sticky="nsew")
        self.folder_file_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_selected_library_file())
        self.folder_file_tree.bind("<Double-Button-1>", self._open_double_clicked_file)

        display_frame = ttk.Frame(self.library_tab, style="Panel.TFrame", padding=10)
        display_frame.grid(row=2, column=0, sticky="nsew")
        display_frame.columnconfigure(0, weight=1)
        display_frame.rowconfigure(1, weight=1)
        self.library_preview_frame = display_frame
        preview_header = ttk.Frame(display_frame, style="Panel.TFrame")
        preview_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        preview_header.columnconfigure(1, weight=1)
        ttk.Label(preview_header, text="图片预览", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(preview_header, textvariable=self.library_preview_status_var, style="Small.TLabel").grid(
            row=0, column=1, sticky="e", padx=(12, 8)
        )
        self.library_preview_progress = ttk.Progressbar(
            preview_header,
            variable=self.library_preview_progress_var,
            maximum=100,
            mode="determinate",
            length=180,
        )
        self.library_preview_progress.grid(row=0, column=2, sticky="e")
        self.file_preview = ttk.Label(
            display_frame,
            text="暂无图片",
            anchor="center",
            justify="center",
            wraplength=620,
            style="Panel.TLabel",
        )
        self.file_preview.grid(row=1, column=0, sticky="nsew")
        self.library_preview_frame.bind("<Configure>", self._handle_preview_resize)
        self.library_tab.bind("<Configure>", self._adapt_library_height, add="+")

    def _adapt_library_height(self, event: tk.Event | None = None) -> None:
        """Give the file list priority; preview can be opened on demand."""
        height = event.height if event is not None else self.library_tab.winfo_height()
        show_preview = self._library_preview_override
        if show_preview is None:
            show_preview = height >= 500
        if height < 390:
            self.library_status_label.grid_remove()
        elif not self.library_status_label.winfo_manager():
            self.library_status_label.grid()
        self.library_tab.rowconfigure(2, weight=1 if show_preview else 0)
        if show_preview and not self.library_preview_frame.winfo_manager():
            self.library_preview_frame.grid()
        elif not show_preview and self.library_preview_frame.winfo_manager():
            self.library_preview_frame.grid_remove()
        self.library_preview_toggle.configure(text="收起预览" if show_preview else "显示预览")

    def _toggle_library_preview(self) -> None:
        self._library_preview_override = not bool(self.library_preview_frame.winfo_manager())
        self._adapt_library_height()


    def _handle_preview_resize(self, _event=None) -> None:
        if not hasattr(self, "file_preview"):
            return
        try:
            if not self.winfo_exists() or not self.file_preview.winfo_exists():
                return
        except tk.TclError:
            return
        if self._preview_resize_job is not None:
            try:
                self.after_cancel(self._preview_resize_job)
            except tk.TclError:
                pass
        self._preview_resize_job = self.after(180, self._rerender_current_preview)


    def _rerender_current_preview(self) -> None:
        self._preview_resize_job = None
        try:
            if not self.winfo_exists() or not self.file_preview.winfo_exists():
                return
        except tk.TclError:
            return
        if self._preview_path and self._preview_path.exists():
            self._render_image_preview(self._preview_path, fallback=self._preview_fallback)


    def _scan_output_dir(self) -> None:
        self._refresh_library(scan=True)
        self.status_var.set("下载库正在后台扫描；可以继续使用其他页面")


    def _refresh_library(self, scan: bool = True) -> None:
        if not hasattr(self, "folder_tree"):
            return
        self._library_pending_refresh = True
        self._library_pending_scan = self._library_pending_scan or bool(scan)
        if self._library_worker_running:
            return
        if self._library_refresh_job is not None:
            try:
                self.after_cancel(self._library_refresh_job)
            except tk.TclError:
                pass
        delay = 10 if scan else 180
        self._library_refresh_job = self.after(delay, self._start_library_refresh)


    def _start_library_refresh(self) -> None:
        self._library_refresh_job = None
        if self._library_worker_running:
            return
        self._library_pending_refresh = False
        scan = self._library_pending_scan
        self._library_pending_scan = False
        output_dir = self.output_dir_var.get()
        module_id = self.module_var.get() or "twitter"
        self._library_worker_running = True
        started = time.monotonic()
        self.library_status_var.set("正在后台扫描并建立搜索索引…" if scan else "正在后台更新下载库索引…")

        def worker() -> None:
            scanned_count: int | None = None
            try:
                if scan:
                    scanned_count = int(self.storage.scan_media_files(
                        output_dir,
                        module_id=module_id,
                        task_id=None,
                        on_progress=lambda count: self.ui_queue.put(("library_scan_progress", count)),
                        count_only=True,
                    ))
                rows, indexed_count, truncated = self._collect_library_folders(output_dir)
                self.ui_queue.put(
                    (
                        "library_loaded",
                        {
                            "rows": rows,
                            "indexed_count": indexed_count,
                            "truncated": truncated,
                            "scanned_count": scanned_count,
                            "elapsed": time.monotonic() - started,
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("library_error", str(exc)))

        threading.Thread(target=worker, name="library-index-refresh", daemon=True).start()


    def _schedule_library_filter(self, *_args) -> None:
        if not hasattr(self, "folder_tree"):
            return
        if self._library_filter_job is not None:
            try:
                self.after_cancel(self._library_filter_job)
            except tk.TclError:
                pass
        self._library_filter_job = self.after(250, self._apply_library_filter)


    def _apply_library_filter(self) -> None:
        self._library_filter_job = None
        query = self.library_search_var.get().strip()
        query_tokens = library_query_tokens(query)
        selected = self._selected_folder_row()
        selected_path = str(selected.get("path") or "") if selected else ""
        filtered: list[dict] = []
        for group in self.library_all_folder_rows:
            group_matches = not query_tokens or library_text_matches(
                str(group.get("_search_text") or ""), query_tokens
            )
            source_files = group.get("files") or []
            files = source_files if group_matches else [
                row
                for row in source_files
                if library_text_matches(str(row.get("_search_text") or ""), query_tokens)
            ]
            if not files and not group_matches:
                continue
            if query:
                sample = next((row["path"] for row in files if Path(row["path"]).suffix.lower() in IMAGE_EXTENSIONS), None)
                filtered.append(
                    {
                        **group,
                        "files": files,
                        "file_count": len(files),
                        "image_count": sum(1 for row in files if Path(row["path"]).suffix.lower() in IMAGE_EXTENSIONS),
                        "size": sum(int(row.get("size") or 0) for row in files),
                        "sample_image": sample,
                    }
                )
            else:
                filtered.append(group)
        self.folder_rows = filtered
        for item in self.folder_tree.get_children():
            self.folder_tree.delete(item)
        rendered_folders = self.folder_rows[:2000]
        for index, row in enumerate(rendered_folders):
            self.folder_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    row.get("module_id", ""),
                    row.get("name", ""),
                    row.get("file_count", 0),
                    row.get("image_count", 0),
                    self._format_size(int(row.get("size", 0) or 0)),
                ),
            )
        match_files = sum(int(row.get("file_count") or 0) for row in self.folder_rows)
        suffix = "；索引达到 100000 条上限" if self._library_index_truncated else ""
        prefix = f"{self._library_scan_feedback}；" if self._library_scan_feedback else ""
        if query:
            self.library_status_var.set(
                f"{prefix}搜索“{query}”：{len(self.folder_rows)} 个文件夹、{match_files} 个文件；关键词按空格组合匹配{suffix}"
            )
        else:
            self.library_status_var.set(
                f"{prefix}已索引 {self._library_index_count} 个文件、{len(self.folder_rows)} 个文件夹{suffix}"
            )
        if rendered_folders:
            target_index = next(
                (index for index, row in enumerate(rendered_folders) if str(row.get("path") or "") == selected_path),
                0,
            )
            self.folder_tree.selection_set(str(target_index))
            self.folder_tree.see(str(target_index))
            self._render_selected_folder()
        else:
            self._set_empty_local_preview()


    def _collect_library_folders(self, output_dir: str, limit: int = 100000) -> tuple[list[dict], int, bool]:
        groups: dict[str, dict] = {}
        stored_rows = self.storage.list_library_files(limit=limit + 1)
        truncated = len(stored_rows) > limit
        for row in stored_rows[:limit]:
            path = Path(row["path"])
            if not path.exists():
                continue
            folder_path, folder_name = self._folder_for_file(path, output_dir)
            key = os.path.normcase(str(folder_path))
            group = groups.setdefault(
                key,
                {
                    "name": folder_name,
                    "path": folder_path,
                    "module_id": row["module_id"],
                    "file_count": 0,
                    "image_count": 0,
                    "size": 0,
                    "latest": 0.0,
                    "files": [],
                    "sample_image": None,
                    "_search_text": normalized_search_text(row["module_id"], folder_name, folder_path),
                },
            )
            try:
                stat = path.stat()
            except OSError:
                continue
            group["file_count"] += 1
            group["image_count"] += 1 if path.suffix.lower() in IMAGE_EXTENSIONS else 0
            group["size"] += stat.st_size
            group["latest"] = max(float(group["latest"]), stat.st_mtime)
            file_row = {
                "path": path,
                "media_type": row["media_type"],
                "size": stat.st_size,
                "mtime": stat.st_mtime,
                "name": path.name,
                "_search_text": normalized_search_text(
                    path.name,
                    path,
                    row["media_type"],
                    row["module_id"],
                    row["title"],
                    row["author_name"],
                    row["author_id"],
                    row["source_id"],
                    row["chapter"],
                    row["tags_json"],
                ),
            }
            group["files"].append(file_row)
            if group["sample_image"] is None and path.suffix.lower() in IMAGE_EXTENSIONS:
                group["sample_image"] = path
        result = list(groups.values())
        for group in result:
            group["files"].sort(key=lambda item: float(item.get("mtime") or 0), reverse=True)
        result.sort(key=lambda item: float(item.get("latest", 0)), reverse=True)
        return result, sum(int(group["file_count"]) for group in result), truncated


    def _folder_for_file(self, path: Path, output_dir: str | None = None) -> tuple[Path, str]:
        try:
            base = Path(output_dir if output_dir is not None else self.output_dir_var.get()).expanduser().resolve()
            resolved = path.resolve()
            rel = resolved.relative_to(base)
            if len(rel.parts) >= 2:
                folder = base / rel.parts[0]
                return folder, rel.parts[0]
        except Exception:
            pass
        folder = path.parent
        if folder.name.lower() in {"video & gif", "audio"} and folder.parent != folder:
            folder = folder.parent
        return folder, folder.name


    def _set_empty_local_preview(self) -> None:
        self.folder_file_rows = []
        if hasattr(self, "library_file_status_var"):
            self.library_file_status_var.set("没有匹配文件")
        self._preview_path = None
        self._preview_fallback = {}
        self._library_preview_generation += 1
        self._library_preview_request_key = None
        self._library_preview_loading = False
        self.library_preview_status_var.set("没有可预览图片")
        self.library_preview_progress_var.set(0.0)
        if self._library_preview_job is not None:
            try:
                self.after_cancel(self._library_preview_job)
            except tk.TclError:
                pass
            self._library_preview_job = None
        if hasattr(self, "folder_file_tree"):
            for item in self.folder_file_tree.get_children():
                self.folder_file_tree.delete(item)
        if hasattr(self, "file_preview"):
            self.file_preview.configure(
                image="",
                text="暂无图片",
                wraplength=max(160, self.file_preview.winfo_width() - 20),
            )
        self.preview_image = None


    def _selected_folder_row(self) -> dict | None:
        selection = self.folder_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        if index >= len(self.folder_rows):
            return None
        return self.folder_rows[index]


    def _render_selected_folder(self) -> None:
        row = self._selected_folder_row()
        if not row:
            self._set_empty_local_preview()
            return
        all_files = list(row.get("files", []))
        render_limit = LIBRARY_RENDER_LIMIT
        self.folder_file_rows = all_files[:render_limit]
        if len(all_files) > render_limit:
            self.library_file_status_var.set(f"共 {len(all_files)} 个，仅显示前 {render_limit} 个；可继续搜索缩小范围")
        else:
            self.library_file_status_var.set(f"显示 {len(all_files)} 个文件")
        for item in self.folder_file_tree.get_children():
            self.folder_file_tree.delete(item)
        for index, file_row in enumerate(self.folder_file_rows):
            self.folder_file_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(file_row["media_type"], file_row["name"], self._format_size(int(file_row["size"] or 0))),
            )
        sample = row.get("sample_image")
        if sample:
            self._render_image_preview(Path(sample), fallback=row)
        else:
            self._library_preview_generation += 1
            self._preview_path = None
            self._preview_fallback = {}
            self._library_preview_request_key = None
            self._library_preview_loading = False
            self.library_preview_status_var.set("所选文件夹没有图片")
            self.library_preview_progress_var.set(0.0)
            self.file_preview.configure(
                image="",
                text=f"暂无图片\n{row.get('name', '')}\n文件: {row.get('file_count', 0)}",
                wraplength=max(160, self.file_preview.winfo_width() - 20),
            )
            self.preview_image = None


    def _selected_library_file_path(self) -> Path | None:
        paths = self._selected_library_file_paths()
        return paths[0] if paths else None


    def _selected_library_file_paths(self) -> list[Path]:
        paths: list[Path] = []
        for item_id in self.folder_file_tree.selection():
            try:
                index = int(item_id)
            except ValueError:
                continue
            if index < len(self.folder_file_rows):
                paths.append(Path(self.folder_file_rows[index]["path"]))
        return paths


    def _render_selected_library_file(self) -> None:
        path = self._selected_library_file_path()
        if not path:
            return
        row = self._selected_folder_row() or {}
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            self._render_image_preview(path, fallback=row)
        else:
            self._library_preview_generation += 1
            self._preview_path = None
            self._preview_fallback = {}
            self._library_preview_request_key = None
            self._library_preview_loading = False
            self.library_preview_status_var.set("所选项目不是图片")
            self.library_preview_progress_var.set(0.0)
            self.file_preview.configure(
                image="",
                text=f"{path.suffix.lower() or '文件'}\n{path.name}",
                wraplength=max(160, self.file_preview.winfo_width() - 20),
            )
            self.preview_image = None


    @staticmethod
    def _library_image_request_key(path: Path, max_width: int, max_height: int) -> tuple[str, int, int, int, int]:
        try:
            stat = path.stat()
            modified = int(stat.st_mtime_ns)
            size = int(stat.st_size)
        except OSError:
            modified = -1
            size = -1
        width_bucket = max(160, (max_width // 32) * 32)
        height_bucket = max(120, (max_height // 32) * 32)
        return os.path.normcase(str(path.resolve(strict=False))), width_bucket, height_bucket, modified, size


    def _render_image_preview(self, path: Path, fallback: dict | None = None) -> None:
        fallback = fallback or {}
        self._preview_path = path
        self._preview_fallback = fallback
        max_width = max(160, self.file_preview.winfo_width() - 20)
        max_height = max(120, self.file_preview.winfo_height() - 20)
        request_key = self._library_image_request_key(path, max_width, max_height)
        if request_key == self._library_preview_request_key:
            return
        self._library_preview_request_key = request_key
        max_width = request_key[1]
        max_height = request_key[2]
        self.file_preview.configure(image="", text=f"正在后台生成预览…\n{path.name}", wraplength=max_width)
        self.preview_image = None
        self._library_preview_loading = True
        self.library_preview_status_var.set(f"正在加载：{path.name}")
        self.library_preview_progress_var.set(5.0)
        if Image is None or ImageTk is None:
            self._library_preview_loading = False
            self.library_preview_status_var.set("当前环境未安装 Pillow")
            self.library_preview_progress_var.set(0.0)
            self.file_preview.configure(image="", text=f"无法显示图片\n{path.name}")
            return
        self._library_preview_generation += 1
        generation = self._library_preview_generation
        if self._library_preview_job is not None:
            try:
                self.after_cancel(self._library_preview_job)
            except tk.TclError:
                pass
        self._library_preview_job = self.after(
            120,
            lambda: self._start_library_image_preview(path, max_width, max_height, generation),
        )


    def _start_library_image_preview(self, path: Path, max_width: int, max_height: int, generation: int) -> None:
        self._library_preview_job = None

        def worker() -> None:
            try:
                self.ui_queue.put(("library_preview_progress", (generation, str(path), 15, "正在读取图片文件")))
                with Image.open(path) as source:
                    width, height = source.size
                    if width * height > 40_000_000:
                        raise ValueError(f"图片为 {width}×{height}，超过 4000 万像素预览保护上限")
                    self.ui_queue.put(("library_preview_progress", (generation, str(path), 35, "正在读取图片信息")))
                    try:
                        source.draft("RGB", (max_width * 2, max_height * 2))
                    except Exception:
                        pass
                    if ImageOps is not None:
                        source = ImageOps.exif_transpose(source)
                    self.ui_queue.put(("library_preview_progress", (generation, str(path), 60, "正在校正图片方向")))
                    source.thumbnail((max_width, max_height))
                    self.ui_queue.put(("library_preview_progress", (generation, str(path), 85, "正在生成缩略图")))
                    preview = source.convert("RGBA" if "A" in source.getbands() else "RGB").copy()
                self.ui_queue.put(("library_preview_progress", (generation, str(path), 95, "正在显示预览")))
                self.ui_queue.put(("library_preview", (generation, str(path), preview, "")))
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("library_preview", (generation, str(path), None, str(exc))))

        threading.Thread(target=worker, name="library-image-preview", daemon=True).start()


    def _open_selected_folder(self) -> None:
        row = self._selected_folder_row()
        if not row:
            return
        folder = Path(row["path"])
        if not folder.exists():
            messagebox.showerror("文件夹不存在", str(folder))
            return
        os.startfile(str(folder))  # type: ignore[attr-defined]


    def _open_selected_file(self) -> None:
        path = self._selected_library_file_path()
        if not path:
            return
        if not path.exists():
            messagebox.showerror("文件不存在", str(path))
            return
        os.startfile(str(path))  # type: ignore[attr-defined]

    def _open_double_clicked_file(self, event) -> str:
        if select_treeview_row_at_event(self.folder_file_tree, event):
            self._open_selected_file()
        return "break"


    def _copy_selected_file_paths(self) -> None:
        paths = self._selected_library_file_paths()
        if not paths:
            messagebox.showwarning("未选择文件", "请先在文件列表中选择一个或多个文件")
            return
        text = "\n".join(str(path.resolve()) for path in paths)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status_var.set(f"已复制 {len(paths)} 个文件路径")


    def _rename_selected_file(self) -> None:
        paths = self._selected_library_file_paths()
        if len(paths) != 1:
            messagebox.showwarning("请选择一个文件", "重命名一次只能操作一个文件")
            return
        source = paths[0]
        if not source.is_file():
            messagebox.showerror("文件不存在", str(source))
            return
        new_name = simpledialog.askstring("重命名文件", "输入新文件名（包含扩展名）：", initialvalue=source.name)
        if new_name is None:
            return
        new_name = normalized_path_component(new_name.strip())
        name_error = path_component_error(new_name, max_length=180)
        if name_error:
            messagebox.showerror("文件名无效", name_error)
            return
        destination = source.with_name(new_name)
        if destination.exists() and destination.resolve() != source.resolve():
            messagebox.showerror("文件已存在", str(destination))
            return
        try:
            source.rename(destination)
            try:
                self.storage.rename_file_record(source, destination)
            except Exception:
                destination.rename(source)
                raise
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("重命名失败", str(exc))
            return
        self._refresh_library(scan=False)
        self.status_var.set(f"已重命名为 {destination.name}")


    def _recycle_selected_files(self) -> None:
        paths = self._selected_library_file_paths()
        if not paths:
            messagebox.showwarning("未选择文件", "请先选择要移到回收站的文件")
            return
        existing = [path for path in paths if path.is_file()]
        if not existing:
            messagebox.showerror("文件不存在", "所选文件已经不存在")
            return
        preview_names = "\n".join(f"• {path.name}" for path in existing[:8])
        if len(existing) > 8:
            preview_names += f"\n……另有 {len(existing) - 8} 个文件"
        if not messagebox.askyesno(
            "移到回收站",
            f"将以下 {len(existing)} 个文件移到 Windows 回收站？\n\n{preview_names}\n\n下载任务和历史记录不会被清空。",
        ):
            return
        moved = 0
        errors: list[str] = []
        for path in existing:
            try:
                _send_path_to_recycle_bin(path)
                self.storage.delete_file_record(path)
                moved += 1
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{path.name}: {exc}")
        self._refresh_library(scan=False)
        self.status_var.set(f"已将 {moved} 个文件移到回收站")
        if errors:
            messagebox.showerror("部分文件处理失败", "\n".join(errors[:10]))


