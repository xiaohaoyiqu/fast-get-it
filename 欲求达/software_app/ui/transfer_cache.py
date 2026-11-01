from __future__ import annotations

from dataclasses import replace
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from software_app.core.adapter import TaskCancelled
from software_app.core.transfer_cleanup import preview_transfer_caches, execute_transfer_cleanup


class TransferCacheDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("续传缓存预览与清理")
        self.geometry("960x540")
        self.minsize(760, 420)
        self.transient(master)
        self.owner = master
        self._events = queue.Queue()
        self._stop = threading.Event()
        self._busy = self._closing = False
        self._plan = None
        self._poll_job = None
        self.root_var = tk.StringVar(value=master.output_dir_var.get())
        self.status = tk.StringVar(value="选择下载目录后预览，再选择要清理的缓存；不会自动清理。")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        form = ttk.Frame(self, padding=12)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="下载目录").grid(row=0, column=0, padx=(0, 8))
        self.root_entry = ttk.Entry(form, textvariable=self.root_var)
        self.root_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(form, text="浏览", command=self._browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))
        ttk.Label(
            self,
            text="只识别有归属记录的 HTTP／HLS 缓存。占用、链接、未知内容或预览后有变化的目录会跳过。"
            "清理会失去所选缓存的续传进度，成品、Cookie 和任务记录保留。",
            wraplength=900,
        ).grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        listing = ttk.Frame(self)
        listing.grid(row=2, column=0, sticky="nsew", padx=12)
        listing.columnconfigure(0, weight=1)
        listing.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            listing, columns=("kind", "bytes", "files", "path", "state"), show="headings", selectmode="extended"
        )
        for key, title, width in (
            ("kind", "类型", 65),
            ("bytes", "字节", 100),
            ("files", "文件数", 65),
            ("path", "缓存目录", 390),
            ("state", "检查结果", 240),
        ):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key in {"path", "state"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(listing, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._update_buttons())
        actions = ttk.Frame(self, padding=12)
        actions.grid(row=3, column=0, sticky="ew")
        self.preview_button = ttk.Button(actions, text="预览缓存", command=self._preview)
        self.select_button = ttk.Button(actions, text="选择全部可清理项", command=self._select_all)
        self.clean_button = ttk.Button(actions, text="清理所选缓存", command=self._cleanup)
        self.stop_button = ttk.Button(actions, text="停止当前操作", command=self._stop.set)
        for column, button in enumerate((self.preview_button, self.select_button, self.clean_button, self.stop_button)):
            button.grid(row=0, column=column, padx=(0, 8))
        ttk.Label(self, textvariable=self.status, wraplength=900).grid(
            row=4, column=0, sticky="w", padx=12, pady=(0, 12)
        )
        self.root_var.trace_add("write", self._invalidate)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.bind("<Destroy>", self._destroyed, add="+")
        self._update_buttons()
        self._poll_job = self.after(80, self._poll)

    def _browse(self):
        directory = filedialog.askdirectory(parent=self, title="选择需要预览缓存的下载目录")
        if directory:
            self.root_var.set(directory)

    def _invalidate(self, *_args):
        self._plan = None
        self.tree.delete(*self.tree.get_children())
        self.status.set("目录已变化，请重新预览。")
        self._update_buttons()

    def _selected_entries(self):
        if self._plan is None:
            return []
        return [
            self._plan.entries[int(index)]
            for index in self.tree.selection()
            if index.isdigit() and int(index) < len(self._plan.entries) and not self._plan.entries[int(index)].error
        ]

    def _update_buttons(self):
        for widget in (self.root_entry, self.browse_button, self.preview_button):
            widget.state(["disabled"] if self._busy else ["!disabled"])
        self.select_button.state(
            ["!disabled"]
            if not self._busy and self._plan and any(not entry.error for entry in self._plan.entries)
            else ["disabled"]
        )
        self.clean_button.state(["!disabled"] if not self._busy and self._selected_entries() else ["disabled"])
        self.stop_button.state(["!disabled"] if self._busy else ["disabled"])

    def _select_all(self):
        if self._plan and not self._busy:
            self.tree.selection_set(*(str(index) for index, entry in enumerate(self._plan.entries) if not entry.error))

    def _start(self, kind, work):
        if self._busy:
            return
        self._busy = True
        self._stop.clear()
        self.status.set("正在扫描…" if kind == "preview" else "正在清理所选缓存…")
        self._update_buttons()

        def worker():
            try:
                self._events.put((kind, work()))
            except TaskCancelled as exc:
                self._events.put(("cancelled", str(exc)))
            except Exception as exc:
                self._events.put(("error", str(exc)))

        threading.Thread(target=worker, name=f"transfer-cache-{kind}", daemon=True).start()

    def _preview(self):
        root = self.root_var.get().strip()
        self._invalidate()
        if not root:
            self.status.set("请先选择下载目录。")
            return
        self._start("preview", lambda: preview_transfer_caches(root, cancel_event=self._stop))

    def _cleanup(self):
        entries = self._selected_entries()
        if self._busy or not entries:
            return
        size = sum(entry.size for entry in entries)
        if not messagebox.askyesno(
            "确认清理缓存",
            f"清理所选 {len(entries)} 个缓存目录，共 {size:,} 字节。\n\n"
            "所选缓存的续传进度将丢失，重试时可能需要重新下载。成品、Cookie、任务和下载库记录保留。\n\n继续？",
            default="no",
            parent=self,
        ):
            return
        # Freeze exactly the selected preview entries, independent of later UI
        # selection changes while the worker runs.
        plan = replace(self._plan, entries=tuple(entries))
        selected = tuple(entry.path for entry in entries)
        self._start(
            "cleanup", lambda: execute_transfer_cleanup(self.owner.manager, plan, selected, cancel_event=self._stop)
        )

    def _poll(self):
        self._poll_job = None
        try:
            kind, value = self._events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy = False
            if kind == "preview":
                self._plan = value
                for index, entry in enumerate(value.entries):
                    self.tree.insert(
                        "",
                        "end",
                        iid=str(index),
                        values=(
                            entry.kind.upper(),
                            f"{entry.size:,}",
                            len(entry.files),
                            str(entry.path.relative_to(value.root)),
                            entry.error or "可清理",
                        ),
                    )
                ready = [entry for entry in value.entries if not entry.error]
                self.status.set(
                    f"找到 {len(value.entries)} 项，可清理 {len(ready)} 项、{sum(entry.size for entry in ready):,} 字节；请选择范围。"
                )
            elif kind == "cleanup":
                self._invalidate()
                self.status.set(
                    f"清理目录 {value.removed_folders} 个、文件 {value.removed_files} 个、{value.removed_bytes:,} 字节；"
                    f"跳过 {value.skipped} 项。" + ("操作已停止。" if value.cancelled else "请重新预览。")
                )
                if value.errors and not self._closing:
                    messagebox.showwarning("部分缓存未清理", "\n".join(value.errors[:10]), parent=self)
            else:
                self._invalidate()
                self.status.set(str(value))
                if kind == "error" and not self._closing:
                    messagebox.showerror("缓存操作失败", str(value), parent=self)
            self._update_buttons()
            if self._closing:
                self.destroy()
                return
        self._poll_job = self.after(80, self._poll)

    def _close(self):
        if self._busy:
            self._closing = True
            self._stop.set()
            self.status.set("正在停止，结束后关闭…")
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
