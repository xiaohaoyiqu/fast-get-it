from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from software_app.core.adapter import TaskCancelled
from software_app.core.library_service import (
    LibraryFilter, export_library_csv, plan_library_cleanup, execute_library_cleanup,
)
from software_app.core.library_integrity import export_integrity_manifest, verify_integrity_manifest
from software_app.ui.desktop_support import send_path_to_recycle_bin


MEDIA_LABELS = {"图片": "image", "视频": "video", "动图": "animation", "音频": "audio",
                "文本": "text", "元数据": "metadata", "其他": "other"}


class LibraryToolsDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("下载库筛选、导出、核验与清理")
        self.geometry("850x590")
        self.minsize(700, 490)
        self.transient(master)
        self.owner = master
        self.manager = master.manager
        self.storage = master.storage
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._busy = False
        self._closing = False
        self._plan = None
        self._plan_filters = None
        self._poll_job = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        ttk.Label(self, text="范围：完整下载库索引，包含当前文件表未显示或已丢失的记录。", wraplength=760).grid(
            row=0, column=0, sticky="w", padx=12, pady=(12, 5))
        form = ttk.Frame(self, padding=12)
        form.grid(row=1, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        choices = {
            "module_id": ("全部", *sorted(self.manager.adapters)),
            "media_type": ("全部", *MEDIA_LABELS),
            "date_basis": ("入库日期", "发布日期"),
        }
        definitions = (("module_id", "平台"), ("media_type", "类型"), ("author", "作者 ID / 名称"),
                       ("tag", "标签（精确）"), ("title", "标题包含"), ("query", "综合关键词"),
                       ("date_basis", "日期依据"), ("start_date", "开始 YYYY-MM-DD"),
                       ("end_date", "结束 YYYY-MM-DD"))
        self.values = {}
        self.inputs = []
        for index, (key, label) in enumerate(definitions):
            row, column = divmod(index, 2)
            ttk.Label(form, text=label).grid(row=row, column=column * 2, sticky="w", padx=(0, 7), pady=4)
            default = "全部" if key in {"module_id", "media_type"} else "入库日期" if key == "date_basis" else ""
            if key == "query":
                default = master.library_search_var.get()
            variable = tk.StringVar(value=default)
            self.values[key] = variable
            widget = (ttk.Combobox(form, textvariable=variable, values=choices[key], state="readonly")
                      if key in choices else ttk.Entry(form, textvariable=variable))
            widget.grid(row=row, column=column * 2 + 1, sticky="ew", padx=(0, 12), pady=4)
            self.inputs.append(widget)
            variable.trace_add("write", self._invalidate_plan)
        actions = ttk.Frame(self, padding=(12, 0))
        actions.grid(row=2, column=0, sticky="ew")
        self.preview_button = ttk.Button(actions, text="预览匹配", command=self._preview)
        self.export_button = ttk.Button(actions, text="导出匹配 CSV", command=self._export)
        self.index_button = ttk.Button(actions, text="仅清索引", command=lambda: self._cleanup("index"))
        self.recycle_button = ttk.Button(actions, text="移到回收站并清索引", command=lambda: self._cleanup("recycle"))
        self.cancel_button = ttk.Button(actions, text="停止当前操作", command=self._stop.set)
        for index, button in enumerate((self.preview_button, self.export_button, self.index_button,
                                        self.recycle_button, self.cancel_button)):
            button.grid(row=0, column=index, padx=(0, 6), pady=5)
        self.manifest_button = ttk.Button(actions, text="导出成品清单", command=self._export_manifest)
        self.verify_button = ttk.Button(actions, text="读取清单核验", command=self._verify_manifest)
        self.manifest_button.grid(row=1, column=0, padx=(0, 6), pady=5)
        self.verify_button.grid(row=1, column=1, padx=(0, 6), pady=5)
        ttk.Label(actions, text="只读核验；本地摘要不代表来源可信。").grid(row=1, column=2, columnspan=3, sticky="w")
        self.details = tk.Text(self, wrap="word", height=12, state="disabled")
        self.details.grid(row=3, column=0, sticky="nsew", padx=12, pady=8)
        scrollbar = ttk.Scrollbar(self, command=self.details.yview)
        scrollbar.grid(row=3, column=1, sticky="ns", pady=8)
        self.details.configure(yscrollcommand=scrollbar.set)
        self.status = tk.StringVar(value="可直接导出或读取清单核验；清理前请先预览。")
        ttk.Label(self, textvariable=self.status, wraplength=800).grid(row=4, column=0, sticky="w", padx=12, pady=(0, 12))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._update_buttons()
        self._poll_job = self.after(80, self._poll)

    def _filters(self):
        values = {key: variable.get().strip() for key, variable in self.values.items()}
        for key in ("module_id", "media_type"):
            if values[key] == "全部":
                values[key] = ""
        values["media_type"] = MEDIA_LABELS.get(values["media_type"], values["media_type"])
        values["date_basis"] = "created_at" if values["date_basis"] == "入库日期" else "published_at"
        return LibraryFilter(**values)

    def _invalidate_plan(self, *_args):
        self._plan = None
        self._plan_filters = None
        if hasattr(self, "index_button"):
            self._update_buttons()
            self.status.set("筛选条件已变化，请重新预览。")

    def _update_buttons(self):
        for widget in (*self.inputs, self.preview_button, self.export_button, self.manifest_button, self.verify_button):
            widget.state(["disabled"] if self._busy else ["!disabled"])
        for button in (self.index_button, self.recycle_button):
            button.state(["!disabled"] if not self._busy and self._plan and self._plan.entries else ["disabled"])
        self.cancel_button.state(["!disabled"] if self._busy else ["disabled"])

    def _show_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", tk.END)
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def _start(self, kind, work):
        if self._busy:
            return
        self._stop.clear()
        self._busy = True
        self.status.set("正在后台处理…")
        self._update_buttons()

        def worker():
            try:
                self._events.put((kind, work()))
            except TaskCancelled as exc:
                self._events.put(("cancelled", str(exc)))
            except Exception as exc:
                self._events.put(("error", str(exc)))

        threading.Thread(target=worker, name=f"library-{kind}", daemon=True).start()

    def _preview(self):
        try:
            filters = self._filters()
        except ValueError as exc:
            messagebox.showerror("条件无效", str(exc), parent=self)
            return
        self._invalidate_plan()
        self._start("preview", lambda: (filters, plan_library_cleanup(self.storage, filters, cancel_event=self._stop)))

    def _export(self):
        try:
            filters = self._filters()
        except ValueError as exc:
            messagebox.showerror("条件无效", str(exc), parent=self)
            return
        name = filedialog.asksaveasfilename(parent=self, title="导出完整匹配索引", defaultextension=".csv",
                                            initialfile="下载库索引.csv", filetypes=[("CSV", "*.csv")])
        if name:
            self._start("export", lambda: (name, export_library_csv(self.storage, filters, name, cancel_event=self._stop)))

    def _cleanup(self, mode):
        if self._busy or self._plan is None:
            return
        if self._filters() != self._plan_filters:
            self._invalidate_plan()
            return
        plan = self._plan
        action = "仅删除下载库索引，本地文件保留；之后扫描会重新收录" if mode == "index" else "将匹配文件移到 Windows 回收站，并删除对应下载库索引"
        if not messagebox.askyesno("确认清理范围", f"匹配 {len(plan.entries)} 条，现有文件共 {plan.size:,} 字节。\n\n"
                                   f"{action}。\n任务、日志和平台下载历史保留。\n\n继续？",
                                   default="no", parent=self):
            return
        self._start("cleanup", lambda: execute_library_cleanup(
            self.manager, plan, mode=mode, recycle=send_path_to_recycle_bin, cancel_event=self._stop))

    def _export_manifest(self):
        if self._busy:
            return
        try:
            filters = self._filters()
        except ValueError as exc:
            messagebox.showerror("条件无效", str(exc), parent=self)
            return
        root = filedialog.askdirectory(parent=self, title="选择包含全部匹配文件的根目录", mustexist=True,
                                       initialdir=self.owner.output_dir_var.get())
        if not root:
            return
        name = filedialog.asksaveasfilename(parent=self, title="导出成品清单（请选择新文件名）", defaultextension=".json",
                                            initialfile="成品清单.json", filetypes=[("JSON", "*.json")])
        if name:
            self._start("integrity", lambda: (name, export_integrity_manifest(
                self.storage, filters, root, name, cancel_event=self._stop)))

    def _verify_manifest(self):
        if self._busy:
            return
        source = filedialog.askopenfilename(parent=self, title="选择成品清单", filetypes=[("JSON", "*.json")])
        if not source:
            return
        root = filedialog.askdirectory(parent=self, title="选择清单文件当前所在的根目录", mustexist=True,
                                       initialdir=self.owner.output_dir_var.get())
        if not root:
            return
        name = filedialog.asksaveasfilename(parent=self, title="保存核验报告（请选择新文件名）", defaultextension=".json",
                                            initialfile="成品核验报告.json", filetypes=[("JSON", "*.json")])
        if name:
            self._start("integrity", lambda: (name, verify_integrity_manifest(
                self.storage, source, root, name, cancel_event=self._stop)))

    def _poll(self):
        self._poll_job = None
        try:
            kind, value = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            if kind == "preview":
                self._plan_filters, self._plan = value
                plan = self._plan
                blocked = sum(bool(entry.error) for entry in plan.entries)
                self._show_details(f"匹配 {len(plan.entries)} 条；{plan.size:,} 字节；缺失 {plan.missing} 条；"
                                   f"不适合回收 {blocked} 条。\n以下最多展示 50 条，操作范围为全部匹配项：\n\n"
                                   + "\n".join(entry.path for entry in plan.entries[:50]))
                self.status.set("预览完成；执行时会重新检查文件和索引。")
            elif kind == "export":
                name, count = value
                self.status.set(f"已导出 {count} 条：{Path(name).name}")
            elif kind == "integrity":
                name, counts = value
                labels = {"recorded": "已建立基线", "matched": "匹配", "missing": "缺失", "changed": "变化",
                          "error": "无法读取", "busy": "正在使用", "no_baseline": "无有效基线"}
                self._show_details("\n".join(f"{labels.get(state, state)}：{count}" for state, count in counts.items())
                                   + f"\n\n已保存：{name}\n只记录当前本地文件；未改写媒体、索引或任务历史。"
                                   + "\n导出使用窗口筛选条件；核验使用所选清单全部条目。")
                self.status.set(f"清单操作完成：{Path(name).name}")
            elif kind == "cleanup":
                self._invalidate_plan()
                self._show_details(f"清除索引 {value.removed_indexes} 条；回收文件 {value.recycled} 个；"
                                   f"跳过 {value.skipped} 条。" + ("\n操作已停止。" if value.cancelled else "")
                                   + "\n" + "\n".join(value.errors[:10]))
                self.status.set("清理已结束；再次操作请重新预览。")
                self.owner._refresh_library(scan=False)
            else:
                self.status.set(str(value))
                if kind == "error" and not self._closing:
                    messagebox.showerror("下载库操作失败", str(value), parent=self)
            self._update_buttons()
            if self._closing:
                self.destroy()
                return
        self._poll_job = self.after(80, self._poll)

    def _close(self):
        if self._busy:
            self._closing = True
            self._stop.set()
            self.status.set("正在停止操作，结束后关闭…")
        else:
            self.destroy()

    def _destroyed(self, event):
        if event.widget is self:
            self._stop.set()
            if self._poll_job is not None:
                try:
                    self.after_cancel(self._poll_job)
                except tk.TclError:
                    pass
                self._poll_job = None
