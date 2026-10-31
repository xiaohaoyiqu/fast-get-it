"""Paged media outcomes; database work stays off the Tk thread."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from software_app.core.security import redact_url_query, safe_error_message


STATES = {
    "全部": "",
    "失败": "failed",
    "待处理": "pending",
    "已保存": "saved",
    "已复用": "reused",
    "已补全": "repaired",
}
LABELS = {value: key for key, value in STATES.items()}
KINDS = {"image": "图片", "video": "视频", "animation": "动图", "audio": "音频"}
PAGE_SIZE = 100


class MediaResultsDialog(tk.Toplevel):
    def __init__(self, owner, task_id):
        super().__init__(owner)
        self.owner, self.task_id = owner, task_id
        self.title(f"逐媒体结果 · {task_id}")
        self.geometry("980x610")
        self.minsize(760, 470)
        self.transient(owner)
        self._events = queue.Queue()
        self._closed = False
        self._busy = False
        self._page = 0
        self._total = 0
        self._rows = {}
        self._poll_job = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        controls = ttk.Frame(self, padding=10)
        controls.grid(row=0, column=0, sticky="ew")
        self.state_var = tk.StringVar(value="全部")
        self.filter = ttk.Combobox(
            controls, textvariable=self.state_var, values=tuple(STATES), state="readonly", width=10
        )
        self.filter.pack(side="left")
        self.filter.bind("<<ComboboxSelected>>", lambda _event: self._load(0))
        self.refresh = ttk.Button(controls, text="刷新", command=lambda: self._load(self._page))
        self.refresh.pack(side="left", padx=5)
        self.previous = ttk.Button(controls, text="上一页", command=lambda: self._load(self._page - 1))
        self.previous.pack(side="left")
        self.next = ttk.Button(controls, text="下一页", command=lambda: self._load(self._page + 1))
        self.next.pack(side="left", padx=5)
        self.retry = ttk.Button(controls, text="仅补所选（当前页）", command=self._retry)
        self.retry.pack(side="left")
        self.open_page = ttk.Button(controls, text="打开原页面", command=self._open_page)
        self.open_page.pack(side="left", padx=5)
        self.status = tk.StringVar(value="正在读取媒体记录…")
        frame = ttk.Frame(self, padding=(10, 0))
        frame.grid(row=1, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(
            frame, columns=("index", "kind", "state", "target", "error"), show="headings", selectmode="extended"
        )
        for name, label, width in (
            ("index", "序号", 55),
            ("kind", "类型", 65),
            ("state", "状态", 75),
            ("target", "原页面", 330),
            ("error", "原因", 310),
        ):
            self.tree.heading(name, text=label)
            self.tree.column(name, width=width, minwidth=40, stretch=name in {"target", "error"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.bind("<<TreeviewSelect>>", self._select)
        self.detail = tk.Text(self, height=6, wrap="word", state="disabled")
        self.detail.grid(row=2, column=0, sticky="ew", padx=10, pady=(6, 0))
        ttk.Label(self, textvariable=self.status, wraplength=900).grid(
            row=3, column=0, sticky="w", padx=10, pady=(8, 3)
        )
        ttk.Label(
            self,
            text="已登记媒体：Bluesky／Instagram 静态下载、Pixiv 单作品图片、收藏、漫画系列、作品搜索及排行榜；动图按整组补下。原任务终态保留。",
        ).grid(row=4, column=0, sticky="w", padx=10, pady=(0, 10))
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._load(0)
        self._poll_job = self.after(100, self._poll)

    def _update_buttons(self):
        self.refresh.configure(state="disabled" if self._busy else "normal")
        self.filter.configure(state="disabled" if self._busy else "readonly")
        self.previous.configure(state="normal" if not self._busy and self._page > 0 else "disabled")
        self.next.configure(
            state="normal" if not self._busy and (self._page + 1) * PAGE_SIZE < self._total else "disabled"
        )
        selected = [self._rows[key] for key in self.tree.selection() if key in self._rows]
        self.retry.configure(
            state="normal"
            if not self._busy and selected and all(row["state"] in {"failed", "pending"} for row in selected)
            else "disabled"
        )
        self.open_page.configure(state="normal" if len(selected) == 1 else "disabled")

    def _load(self, page):
        if self._busy or self._closed:
            return
        page = max(0, page)
        state = STATES[self.state_var.get()]
        storage, events, task_id = self.owner.storage, self._events, self.task_id
        self._busy = True
        self.status.set("正在后台读取媒体记录…")
        self._update_buttons()

        def worker():
            try:
                total, rows = storage.media_page(task_id, state=state, offset=page * PAGE_SIZE, limit=PAGE_SIZE)
                events.put(("page", (page, total, rows)))
            except Exception as exc:  # noqa: BLE001
                events.put(("error", safe_error_message(exc)))

        try:
            threading.Thread(target=worker, name="media-results", daemon=True).start()
        except RuntimeError as exc:
            self._busy = False
            self.status.set(safe_error_message(exc))
            self._update_buttons()

    def _poll(self):
        self._poll_job = None
        if self._closed:
            return
        try:
            while True:
                kind, payload = self._events.get_nowait()
                self._busy = False
                if kind == "error":
                    self.status.set("读取失败：" + payload)
                else:
                    self._page, self._total, rows = payload
                    if self._page and not rows:
                        self._load(max(0, (self._total - 1) // PAGE_SIZE))
                        continue
                    self.tree.delete(*self.tree.get_children())
                    self._rows = {row["key"]: row for row in rows}
                    for row in rows:
                        self.tree.insert(
                            "",
                            "end",
                            iid=row["key"],
                            values=(
                                row["index"],
                                KINDS.get(row["kind"], row["kind"]),
                                LABELS.get(row["state"], row["state"]),
                                redact_url_query(row["target"]),
                                safe_error_message(row["error"]).replace("\n", " ")[:200],
                            ),
                        )
                    self.status.set(
                        f"第 {self._page + 1} / {max(1, (self._total + PAGE_SIZE - 1) // PAGE_SIZE)} 页 · 共 {self._total} 项"
                    )
                    self._select()
                self._update_buttons()
        except queue.Empty:
            pass
        finally:
            if not self._closed:
                self._poll_job = self.after(100, self._poll)

    def _select(self, _event=None):
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        for key in self.tree.selection():
            row = self._rows.get(key)
            if row:
                self.detail.insert(
                    "end",
                    f"{LABELS.get(row['state'], row['state'])} · 媒体 {row['index']}\n"
                    f"{redact_url_query(row['target'])}\n{safe_error_message(row['error']) or '无错误原因'}\n\n",
                )
        self.detail.configure(state="disabled")
        self._update_buttons()

    def _retry(self):
        keys = [key for key in self.tree.selection() if key in self._rows]
        try:
            self.owner._retry_media_subset(self.task_id, keys)
        except Exception as exc:  # noqa: BLE001
            messagebox.showwarning("补下未创建", safe_error_message(exc), parent=self)
            return
        self.status.set("所选媒体已创建独立补下任务；完成后刷新可查看原记录的已补全状态。")

    def _open_page(self):
        keys = self.tree.selection()
        if len(keys) == 1 and keys[0] in self._rows:
            url = self._rows[keys[0]]["target"]
            if urlsplit(url).scheme in {"http", "https"}:
                webbrowser.open(url)

    def _close(self):
        self._closed = True
        if self._poll_job is not None:
            self.after_cancel(self._poll_job)
        self.destroy()
