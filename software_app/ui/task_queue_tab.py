from __future__ import annotations

import json
import os
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from software_app.core.security import redact_sensitive_data
from software_app.ui.desktop_support import retry_route_for_task
from software_app.ui.platform_config import PLATFORM_CONTENT_SCOPES, TASK_STATUS_LABELS


class TaskQueueTabMixin:
    def _build_tasks_tab(self) -> None:
        self.tasks_tab.columnconfigure(0, weight=1)
        self.tasks_tab.rowconfigure(1, weight=1)
        task_header = ttk.Frame(self.tasks_tab, style="FlatPanel.TFrame")
        task_header.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        task_header.columnconfigure(0, weight=1)
        ttk.Label(task_header, text="下载任务", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(task_header, textvariable=self.task_summary_var, style="Small.TLabel").grid(row=0, column=1, sticky="e", padx=(8, 8))
        ttk.Button(task_header, text="刷新", command=self._refresh_tasks).grid(row=0, column=2, sticky="e")
        ttk.Progressbar(task_header, variable=self.task_progress_var, maximum=100, mode="determinate").grid(row=1, column=0, columnspan=3, sticky="ew", pady=(7, 0))

        task_panes = ttk.Panedwindow(self.tasks_tab, orient=tk.VERTICAL)
        task_panes.grid(row=1, column=0, sticky="nsew")
        task_list_panel = ttk.Frame(task_panes, style="Panel.TFrame")
        task_list_panel.columnconfigure(0, weight=1)
        task_list_panel.rowconfigure(0, weight=1)
        columns = ("status", "module", "target", "updated")
        task_tree_frame, self.task_tree = self._create_scrollable_tree(task_list_panel, columns)
        self.task_tree.configure(selectmode="extended")
        self.task_tree.heading("status", text="状态")
        self.task_tree.heading("module", text="模块")
        self.task_tree.heading("target", text="目标")
        self.task_tree.heading("updated", text="更新时间")
        self.task_tree.column("status", width=110, stretch=False)
        self.task_tree.column("module", width=120, stretch=False)
        self.task_tree.column("target", width=420)
        self.task_tree.column("updated", width=150, stretch=False)
        self.task_tree.tag_configure("running", background=self.palette["soft"])
        self.task_tree.tag_configure("cancelling", background="#fff7ed")
        self.task_tree.tag_configure("failed", background="#fef2f2")
        self.task_tree.tag_configure("cancelled", background="#f5f5f4")
        self.task_tree.tag_configure("completed", background="#f0fdf4")
        self.task_tree.bind("<<TreeviewSelect>>", lambda _event: self._render_selected_task())
        self.task_tree.bind("<Double-Button-1>", lambda _event: self._use_selected_task())
        task_tree_frame.grid(row=0, column=0, sticky="nsew")

        task_detail_panel = ttk.Frame(task_panes, style="Panel.TFrame", padding=(0, 8, 0, 0))
        task_detail_panel.columnconfigure(0, weight=1)
        task_detail_panel.rowconfigure(1, weight=1)
        detail_header = ttk.Frame(task_detail_panel, style="FlatPanel.TFrame")
        detail_header.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        detail_header.columnconfigure(0, weight=1)
        ttk.Label(detail_header, textvariable=self.task_detail_var, style="Small.TLabel").grid(row=0, column=0, sticky="w")
        self.task_retry_button = ttk.Button(detail_header, text="批量重试", style="Compact.TButton", command=self._retry_selected_task)
        self.task_retry_button.grid(row=0, column=1, padx=(6, 0))
        self.task_cancel_button = ttk.Button(detail_header, text="批量取消", style="Compact.TButton", command=self._cancel_selected_task)
        self.task_cancel_button.grid(row=0, column=2, padx=(6, 0))
        self.task_delete_button = ttk.Button(detail_header, text="删除任务记录", style="Compact.TButton", command=self._delete_selected_tasks)
        self.task_delete_button.grid(row=0, column=3, padx=(6, 0))
        self.task_use_button = ttk.Button(detail_header, text="填入工作台", style="Compact.TButton", command=self._use_selected_task)
        self.task_use_button.grid(row=0, column=4, padx=(6, 0))
        self.task_folder_button = ttk.Button(detail_header, text="打开输出目录", style="Compact.TButton", command=self._open_selected_task_folder)
        self.task_folder_button.grid(row=0, column=5, padx=(6, 0))
        for button in (
            self.task_retry_button,
            self.task_cancel_button,
            self.task_delete_button,
            self.task_use_button,
            self.task_folder_button,
        ):
            button.state(["disabled"])

        detail_text_frame = ttk.Frame(task_detail_panel, style="Panel.TFrame")
        detail_text_frame.grid(row=1, column=0, sticky="nsew")
        detail_text_frame.columnconfigure(0, weight=1)
        detail_text_frame.rowconfigure(0, weight=1)
        self.task_detail_text = tk.Text(
            detail_text_frame,
            height=8,
            wrap="none",
            borderwidth=0,
            highlightthickness=0,
            padx=9,
            pady=7,
            background="#f8fafc",
            foreground=self.palette["text"],
            font=("Consolas", 9),
        )
        self.task_detail_text.grid(row=0, column=0, sticky="nsew")
        detail_y = ttk.Scrollbar(detail_text_frame, orient="vertical", command=self.task_detail_text.yview)
        detail_y.grid(row=0, column=1, sticky="ns")
        detail_x = ttk.Scrollbar(detail_text_frame, orient="horizontal", command=self.task_detail_text.xview)
        detail_x.grid(row=1, column=0, sticky="ew")
        self.task_detail_text.configure(yscrollcommand=detail_y.set, xscrollcommand=detail_x.set, state="disabled")
        self._task_detail_scroll_grabbed = False
        detail_y.bind("<ButtonPress-1>", lambda _e: setattr(self, "_task_detail_scroll_grabbed", True))
        detail_y.bind("<ButtonRelease-1>", lambda _e: setattr(self, "_task_detail_scroll_grabbed", False))
        self.task_detail_text.bind("<ButtonPress-1>", lambda _e: setattr(self, "_task_detail_scroll_grabbed", False))
        self._write_text(self.task_detail_text, "选择上方任务后，这里会显示完整参数、错误、日志和本任务文件。")

        task_panes.add(task_list_panel, weight=3)
        task_panes.add(task_detail_panel, weight=2)
        task_panes.bind("<Configure>", lambda _event: self.after_idle(
            lambda: self._keep_task_detail_visible(task_panes)
        ))

    @staticmethod
    def _keep_task_detail_visible(task_panes: ttk.Panedwindow) -> None:
        """Keep the log readable when the main window becomes short."""
        try:
            height = task_panes.winfo_height()
            if height < 120:
                return
            sash = task_panes.sashpos(0)
            minimum_list = 65
            minimum_detail = min(205, max(125, height - minimum_list))
            maximum_list = max(minimum_list, height - minimum_detail)
            if sash > maximum_list:
                task_panes.sashpos(0, maximum_list)
            elif sash < minimum_list:
                task_panes.sashpos(0, minimum_list)
        except tk.TclError:
            pass

    def _refresh_tasks(self) -> None:
        selected_task_ids = {str(task_id) for task_id in self.task_tree.selection()}
        current_view = self.task_tree.yview()
        scroll_fraction = float(current_view[0]) if current_view else 0.0
        for item in self.task_tree.get_children():
            self.task_tree.delete(item)
        rows = self.storage.list_tasks(limit=200)
        self.task_rows = {}
        counts: dict[str, int] = {}
        for row in rows:
            row_data = dict(row)
            task_id = str(row_data["task_id"])
            self.task_rows[task_id] = row_data
            status = str(row["status"])
            counts[status] = counts.get(status, 0) + 1
            target = row["normalized_target"] or row["target"]
            self.task_tree.insert(
                "",
                tk.END,
                iid=task_id,
                values=(TASK_STATUS_LABELS.get(status, status), row["module_id"], target, row["updated_at"]),
                tags=(status,),
            )
        active = counts.get("queued", 0) + counts.get("running", 0) + counts.get("cancelling", 0)
        if not rows:
            self.task_summary_var.set("暂无任务")
        else:
            self.task_summary_var.set(
                f"共 {len(rows)} · 进行中 {active} · 完成 {counts.get('completed', 0)} · "
                f"失败 {counts.get('failed', 0)} · 已取消 {counts.get('cancelled', 0)}"
            )
        if rows:
            restored = [task_id for task_id in selected_task_ids if task_id in self.task_rows]
            if not restored:
                restored = [str(rows[0]["task_id"])]
            self.task_tree.selection_set(*restored)
            self.task_tree.focus(restored[0])
            self.task_tree.yview_moveto(max(0.0, min(scroll_fraction, 1.0)))
        self._render_selected_task()

    def _selected_task_row(self) -> dict | None:
        rows = self._selected_task_rows()
        return rows[0] if rows else None

    def _selected_task_rows(self) -> list[dict]:
        return [
            self.task_rows[str(task_id)]
            for task_id in self.task_tree.selection()
            if str(task_id) in self.task_rows
        ]

    @staticmethod
    def _decode_task_options(value: object) -> dict:
        try:
            options = json.loads(str(value or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return options if isinstance(options, dict) else {}

    def _task_can_retry(self, row: dict) -> bool:
        status = str(row.get("status") or "")
        if status in {"failed", "cancelled"}:
            return True
        if status != "completed" or str(row.get("module_id") or "") != "website":
            return False
        options = self._decode_task_options(row.get("options_json"))
        if options.get("discovery_source") != "google_image":
            return False
        return not self.storage.list_files(task_id=str(row.get("task_id") or ""), limit=1)

    def _render_selected_task(self) -> None:
        rows = self._selected_task_rows()
        if not rows:
            self.task_detail_var.set("选择一个任务查看参数、日志和已下载文件")
            self._write_text(self.task_detail_text, "任务队列为空，或尚未选择任务。")
            for button in (
                self.task_retry_button,
                self.task_cancel_button,
                self.task_delete_button,
                self.task_use_button,
                self.task_folder_button,
            ):
                button.state(["disabled"])
            return

        retryable = [row for row in rows if self._task_can_retry(row)]
        cancellable = [row for row in rows if str(row.get("status")) in {"queued", "running", "cancelling"}]
        self.task_retry_button.state(["!disabled"] if retryable else ["disabled"])
        self.task_cancel_button.state(["!disabled"] if cancellable else ["disabled"])
        self.task_delete_button.state(["!disabled"])
        if len(rows) > 1:
            self.task_use_button.state(["disabled"])
            self.task_folder_button.state(["disabled"])
            self.task_detail_var.set(
                f"已选 {len(rows)} 项 · 可重试 {len(retryable)} · 可取消 {len(cancellable)} · 可删除 {len(rows)}"
            )
            self._write_text(
                self.task_detail_text,
                "\n".join(
                    f"{TASK_STATUS_LABELS.get(str(row.get('status')), row.get('status'))}  "
                    f"{row.get('module_id') or '-'}  {row.get('target') or '-'}  [{row.get('task_id')}]"
                    for row in rows
                ),
            )
            return

        row = rows[0]

        task_id = str(row["task_id"])
        status = str(row["status"])
        options = self._decode_task_options(row.get("options_json"))
        logs = list(reversed(self.storage.list_logs(task_id, limit=500)))
        files = self.storage.list_files(task_id=task_id, limit=500)
        target = str(row.get("normalized_target") or row.get("target") or "-")
        self.task_detail_var.set(
            f"{TASK_STATUS_LABELS.get(status, status)} · {row.get('module_id', '-')} · 日志 {len(logs)} · 文件 {len(files)}"
        )

        lines = [
            f"任务 ID: {task_id}",
            f"状态: {TASK_STATUS_LABELS.get(status, status)}",
            f"模块: {row.get('module_id', '-')}",
            f"目标: {target}",
            f"原始输入: {row.get('target') or '-'}",
            f"输出目录: {row.get('output_dir') or '-'}",
            f"创建: {row.get('created_at') or '-'}",
            f"开始: {row.get('started_at') or '-'}",
            f"结束: {row.get('finished_at') or '-'}",
        ]
        error = str(row.get("error") or "").strip()
        if error:
            lines.extend(("", "错误:", error))
        lines.extend(("", "参数:", json.dumps(redact_sensitive_data(options), ensure_ascii=False, indent=2)))
        lines.extend(("", f"日志（{len(logs)}）:"))
        if logs:
            lines.extend(
                f"[{log['created_at']}] [{str(log['level']).upper()}] {log['message']}"
                for log in logs
            )
        else:
            lines.append("（暂无日志）")
        lines.extend(("", f"本任务文件（{len(files)}）:"))
        if files:
            lines.extend(f"[{file['media_type']}] {file['path']}" for file in files)
        else:
            lines.append("（暂无已入库文件）")
        self._write_text(self.task_detail_text, "\n".join(lines))

        self.task_use_button.state(["!disabled"])
        output_dir = Path(str(row.get("output_dir") or "")).expanduser()
        if str(row.get("output_dir") or "").strip() and output_dir.is_dir():
            self.task_folder_button.state(["!disabled"])
        else:
            self.task_folder_button.state(["disabled"])

    def _retry_selected_task(self) -> None:
        rows = [row for row in self._selected_task_rows() if self._task_can_retry(row)]
        if not rows:
            messagebox.showwarning("未选择任务", "请先选择失败、已取消，或旧版零文件完成的相似图片任务")
            return
        created: list[str] = []
        failures: list[str] = []
        callbacks = self._task_callbacks()
        seen: set[tuple[str, str, str]] = set()
        for row in rows:
            try:
                original_module = str(row.get("module_id") or "")
                target = str(row.get("target") or row.get("normalized_target") or "")
                original_options = self._decode_task_options(row.get("options_json"))
                routed_module, options = retry_route_for_task(original_module, target, original_options)
                format_options = self._saved_format_options()
                common_postprocess_options = {**format_options, **self._saved_archive_options()}
                options.update(common_postprocess_options)
                retry_overrides = dict(common_postprocess_options)
                if routed_module == "pixiv":
                    try:
                        max_works = max(1, min(int(self.storage.get_setting("pixiv_max_works", 20)), 100))
                        minimum_bookmarks = max(
                            0, min(int(self.storage.get_setting("pixiv_minimum_bookmarks", 0)), 100000000)
                        )
                    except (TypeError, ValueError):
                        max_works, minimum_bookmarks = 20, 0
                    retry_overrides.update(
                        {
                            "max_works": max_works,
                            "filter_ai": bool(self.storage.get_setting("pixiv_filter_ai", True)),
                            "pixiv_visibility": str(self.storage.get_setting("pixiv_visibility", "show") or "show"),
                            "start_date": str(self.storage.get_setting("pixiv_start_date", "") or ""),
                            "end_date": str(self.storage.get_setting("pixiv_end_date", "") or ""),
                            "minimum_bookmarks": minimum_bookmarks,
                            "age_mode": str(self.storage.get_setting("pixiv_age_mode", "all") or "all"),
                        }
                    )
                    options.update(retry_overrides)
                elif routed_module == "jmcomic":
                    retry_overrides.update(
                        {
                            "domain": str(self.storage.get_setting("jmcomic_domain", "https://18comic.vip") or "https://18comic.vip"),
                            "user_agent": str(self.storage.get_setting("jmcomic_user_agent", "") or ""),
                            "order_by": str(self.storage.get_setting("jmcomic_order_by", "mr") or "mr"),
                            "time_range": str(self.storage.get_setting("jmcomic_time_range", "a") or "a"),
                            "category": str(self.storage.get_setting("jmcomic_category", "0") or "0"),
                            "favorite_username": str(self.storage.get_setting("jmcomic_favorite_username", "") or ""),
                            "favorite_folder_id": str(self.storage.get_setting("jmcomic_favorite_folder_id", "0") or "0"),
                            "novel_favorite_folder_id": str(self.storage.get_setting("jmcomic_novel_favorite_folder_id", "0") or "0"),
                            "match_mode": str(self.storage.get_setting("jmcomic_match_mode", "fuzzy") or "fuzzy"),
                            "jm_postprocess": str(self.storage.get_setting("jmcomic_postprocess", "none") or "none"),
                            "jm_download_cover": bool(self.storage.get_setting("jmcomic_download_cover", True)),
                        }
                    )
                    options.update(retry_overrides)
                elif routed_module == "ehentai":
                    retry_overrides.update(
                        {
                            "eh_download_method": "images",
                            "eh_download_images": True,
                            "eh_download_torrent": bool(self.storage.get_setting("eh_download_torrent", False)),
                            "eh_bt_download_enabled": bool(self.storage.get_setting("eh_bt_download_enabled", False)),
                            "max_torrents": 1,
                        }
                    )
                    options.update(retry_overrides)
                output_dir = Path(str(row.get("output_dir") or self.output_dir_var.get()))
                retry_key = (routed_module, target.strip().casefold(), str(output_dir).casefold())
                if retry_key in seen or self.manager.has_active_task(*retry_key):
                    failures.append(f"{row['task_id']}: 相同目标已有运行中或本批重试任务")
                    continue
                seen.add(retry_key)
                if routed_module != original_module or options != original_options:
                    options.setdefault("types", self._selected_types())
                    task = self.manager.start_task(
                        routed_module,
                        target,
                        output_dir,
                        options,
                        callbacks,
                    )
                else:
                    task = self.manager.retry_task(
                        str(row["task_id"]),
                        callbacks,
                        allow_empty_completed=str(row.get("status")) == "completed",
                        option_overrides=retry_overrides,
                    )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{row['task_id']}: {exc}")
                continue
            created.append(task.task_id)
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
            self._append_log(f"重试任务 {row['task_id']} -> {task.task_id}")
        if not created:
            messagebox.showerror("重试失败", failures[0] if failures else "没有任务可以重试")
            return
        self.task_progress_var.set(3.0)
        self.status_var.set(f"已重新创建 {len(created)} 个任务")
        self._refresh_tasks()
        selected = [task_id for task_id in created if self.task_tree.exists(task_id)]
        if selected:
            self.task_tree.selection_set(*selected)
            self.task_tree.focus(selected[0])
            self._render_selected_task()
        if failures:
            self._append_log(f"另有 {len(failures)} 个任务重试失败：{failures[0]}")

    def _cancel_selected_task(self) -> None:
        rows = [row for row in self._selected_task_rows() if str(row.get("status")) in {"queued", "running"}]
        if not rows:
            messagebox.showwarning("未选择任务", "请先选择一个排队中或运行中的任务")
            return
        for row in rows:
            self.manager.cancel(str(row["task_id"]))
        self.status_var.set(f"正在取消 {len(rows)} 个任务")
        self._append_log(f"已请求批量取消 {len(rows)} 个所选任务")
        self._refresh_tasks()

    def _delete_selected_tasks(self) -> None:
        rows = self._selected_task_rows()
        if not rows:
            messagebox.showwarning("未选择任务", "请先选择一个或多个任务")
            return
        if not messagebox.askyesno(
            "删除任务记录",
            f"删除选中的 {len(rows)} 个任务及对应日志/文件索引？\n\n"
            "运行中的任务会先正常停止；本地已下载文件不会删除。",
        ):
            return
        task_ids = {str(row["task_id"]) for row in rows}
        active = task_ids & set(self.manager.active_task_ids())
        inactive = task_ids - active
        counts = self.storage.delete_task_records(inactive)
        for task_id in active:
            self.pending_task_deletions.add(task_id)
            self.manager.cancel(task_id)
        self._refresh_tasks()
        self._refresh_library(scan=False)
        self.status_var.set(
            f"已删除 {len(inactive)} 个任务记录"
            + (f"；{len(active)} 个运行中任务停止后自动移除" if active else "")
        )
        self._append_log(f"删除任务记录: tasks={len(inactive)}, pending={len(active)}, db={counts}")

    def _use_selected_task(self) -> None:
        row = self._selected_task_row()
        if not row:
            messagebox.showwarning("未选择任务", "请先选择一个任务")
            return
        module_id = str(row.get("module_id") or "")
        try:
            self.manager.get_adapter(module_id)
        except KeyError:
            messagebox.showerror("模块不可用", f"当前版本没有注册任务模块: {module_id}")
            return
        self._select_module(module_id)
        self.target_var.set(str(row.get("target") or row.get("normalized_target") or ""))
        output_dir = str(row.get("output_dir") or "").strip()
        if output_dir:
            self.output_dir_var.set(output_dir)
        options = self._decode_task_options(row.get("options_json"))
        types = str(options.get("types") or "").strip()
        if types:
            self.types_var.set(types)
        scope = str(options.get("content_scope") or options.get("search_mode") or "").strip()
        if scope in PLATFORM_CONTENT_SCOPES.get(module_id, ()):
            self.content_scope_var.set(scope)
            self._content_scope_changed()
        self.status_var.set(f"已把任务 {row['task_id']} 填入工作台")

    def _open_selected_task_folder(self) -> None:
        row = self._selected_task_row()
        if not row:
            return
        folder = Path(str(row.get("output_dir") or "")).expanduser()
        if not folder.is_dir():
            messagebox.showerror("输出目录不存在", str(folder))
            return
        os.startfile(str(folder.resolve()))  # type: ignore[attr-defined]
