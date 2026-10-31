from __future__ import annotations

import queue
import threading
import tkinter as tk
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import messagebox, ttk

from software_app.core.mixed_batch import BatchTaskPlan, REPORT_NAME, route_batch, start_batch
from software_app.core.security import redact_url_query, safe_error_message


class MixedBatchDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.owner = master
        self.title("混合平台批量任务")
        self.geometry("1050x740")
        self.minsize(760, 620)
        self.transient(master)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        self._plans = {}
        self._preview_text = None
        self._output_dir = None
        self._batch_id = None
        self._busy = False
        self._closing = False
        self._stop = threading.Event()
        self._events = queue.Queue()
        self._poll_job = None
        ttk.Label(self, text="每行一个完整 HTTP(S) 链接，最多 100 行。识别只解析链接；确认入队后才会联网。\n"
                  "支持 Twitter/X、Pixiv、FANBOX、Sketch、JM、Bluesky、Instagram、EH；其他公网链接按普通网页处理。",
                  wraplength=980).grid(row=0, column=0, sticky="w", padx=12, pady=10)
        input_frame = ttk.Frame(self)
        input_frame.grid(row=1, column=0, sticky="ew", padx=12)
        input_frame.columnconfigure(0, weight=1)
        self.input = tk.Text(input_frame, height=7, wrap="none", undo=True)
        self.input.grid(row=0, column=0, sticky="ew")
        scrollbar = ttk.Scrollbar(input_frame, command=self.input.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.input.configure(yscrollcommand=scrollbar.set)
        self.input.bind("<<Modified>>", self._changed)
        actions = ttk.Frame(self)
        actions.grid(row=2, column=0, sticky="ew", padx=12, pady=8)
        self.preview_button = ttk.Button(actions, text="识别并预览", command=self._preview)
        self.preview_button.pack(side="left")
        self.select_button = ttk.Button(actions, text="选中全部可用项", command=self._select_ready)
        self.select_button.pack(side="left", padx=8)
        self.submit_button = ttk.Button(actions, text="确认选中项入队", command=self._submit, state="disabled")
        self.submit_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止后续入队", command=self._stop.set, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        frame = ttk.Frame(self)
        frame.grid(row=3, column=0, sticky="nsew", padx=12)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        self.tree = ttk.Treeview(frame, columns=("line", "state", "platform", "scope", "target", "note"), show="headings", selectmode="extended")
        for name, title, width in (("line", "行", 35), ("state", "识别", 60), ("platform", "平台", 90),
                                   ("scope", "下载范围", 130), ("target", "目标", 320), ("note", "说明", 270)):
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, stretch=name in {"target", "note"})
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, command=self.tree.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=scroll.set)
        xscroll = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        xscroll.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=xscroll.set)
        self.details = tk.StringVar(value="先输入链接并识别，再选择需要入队的条目（Ctrl / Shift 多选）。")
        ttk.Label(self, textvariable=self.details, wraplength=980, justify="left").grid(row=4, column=0, sticky="w", padx=12, pady=10)
        self.status = tk.StringVar(value="未预览")
        ttk.Label(self, textvariable=self.status, wraplength=980).grid(row=5, column=0, sticky="w", padx=12, pady=(0, 10))
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _changed(self, _event=None):
        if not self.input.edit_modified():
            return
        self.input.edit_modified(False)
        if not self._busy:
            self._preview_text = None
            self._plans.clear()
            self.submit_button.configure(state="disabled")
            self.status.set("输入已变化，请重新识别")

    def _select_ready(self):
        self.tree.selection_set(tuple(self._plans))

    def _preview(self):
        self._plans.clear()
        self._preview_text = None
        self.submit_button.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        try:
            text = self.input.get("1.0", "end-1c")
            settings = self.owner.storage.get_settings()
            routes = route_batch(text, enabled_modules=set(self.owner.manager.adapters),
                                 jm_domain=str(settings.get("jmcomic_domain", "https://18comic.vip")))
            output_root = self.owner.output_dir_var.get().strip()
            if not output_root:
                raise ValueError("请先在主窗口设置保存目录")
            self._batch_id = uuid.uuid4().hex
            self._output_dir = Path(output_root).expanduser().resolve() / f"批次_{datetime.now():%Y%m%d_%H%M%S}_{self._batch_id[:8]}"
            failures = 0
            for route in routes:
                state = {"ready": "可入队", "error": "无效", "duplicate": "重复"}[route.state]
                note = route.note
                if route.state == "ready":
                    try:
                        options = self.owner._current_target_options(route.target, module_id=route.module_id,
                            scope=route.scope, settings=settings, use_view_filters=False)
                        options["discovery_source"] = "mixed_batch"
                        if route.module_id in {"pixiv", "jmcomic"}:
                            options["input_kind"] = route.kind
                        if route.module_id == "website":
                            options["max_files"] = max(1, min(200, int(settings.get("webpage_max_files", 50))))
                        self._plans[str(route.line)] = BatchTaskPlan(route, dict(options))
                    except Exception as exc:
                        state, note = "参数错误", safe_error_message(exc)
                        failures += 1
                self.tree.insert("", "end", iid=str(route.line), values=(route.line, state, route.module_id, route.scope,
                    redact_url_query(route.target or route.raw), note))
            self._preview_text = text
            self._select_ready()
            self.submit_button.configure(state="normal" if self._plans else "disabled")
            counts = Counter(route.state for route in routes)
            self.status.set(f"共 {len(routes)} 项；可用 {len(self._plans)}，无效 {counts['error'] + failures}，重复 {counts['duplicate']}。选中项才会入队。")
            self.details.set(self._settings_summary(list(self._plans.values())))
        except Exception as exc:
            self.status.set("识别失败：" + safe_error_message(exc))

    def _settings_summary(self, plans):
        if not plans:
            return "没有可用项。请按表格说明调整链接，再识别。"
        options = plans[0].options
        text = (f"独立目录：{self._output_dir}\n类型：{options.get('types')}；格式 图片={options.get('image_format', 'original')}，"
                f"视频={options.get('video_format', 'original')}，动图={options.get('animation_format', 'original')}，"
                f"音频={options.get('audio_format', 'original')}；重试 {options.get('retries', 0)} 次。\n"
                f"归档={options.get('archive_mode', 'none')}；解压ZIP={bool(options.get('extract_archives'))}；"
                f"归档后清源={bool(options.get('archive_cleanup_sources'))}。使用识别时已保存的平台设置；各条目放入独立子目录。")
        if any(plan.route.module_id == "ehentai" for plan in plans):
            eh = next(plan.options for plan in plans if plan.route.module_id == "ehentai")
            text += f"\nEH：下载图片；种子={bool(eh.get('eh_download_torrent'))}；BT 自动下载={bool(eh.get('eh_bt_download_enabled'))}。"
        if any(plan.route.module_id == "website" for plan in plans):
            web = next(plan.options for plan in plans if plan.route.module_id == "website")
            text += f"\n普通网页：静态 HTTP 提取，每页最多 {web['max_files']} 个文件。"
        return text

    def _submit(self):
        if self._busy:
            return
        if self._preview_text is None or self.input.get("1.0", "end-1c") != self._preview_text:
            self.status.set("输入已变化，请重新识别")
            return
        plans = [self._plans[item] for item in self.tree.get_children() if item in self.tree.selection() and item in self._plans]
        if not plans:
            self.status.set("请选择至少一个可用项")
            return
        if not messagebox.askyesno("确认混合平台批次", f"将创建 {len(plans)} 个独立任务。\n\n"
                + self._settings_summary(plans) + f"\n\n汇总文件：{REPORT_NAME}\n是否开始入队？", parent=self):
            return
        # Capture all Tk-owned state here. Worker only calls thread-safe core services.
        manager, batch_id, output_dir = self.owner.manager, self._batch_id, self._output_dir
        callbacks = self.owner._task_callbacks()
        self._busy = True
        self._stop.clear()
        self.input.configure(state="disabled")
        for widget in (self.preview_button, self.select_button, self.submit_button):
            widget.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status.set("正在逐项入队；已入队的任务会按各平台并发限制执行")

        def work():
            try:
                result = start_batch(manager, plans, output_dir, batch_id, callbacks, self._stop)
                self._events.put(("done", result))
            except Exception as exc:
                self._events.put(("error", safe_error_message(exc)))

        threading.Thread(target=work, name="mixed-batch-enqueue", daemon=True).start()
        self._poll_job = self.after(80, self._poll)

    def _poll(self):
        self._poll_job = None
        try:
            kind, value = self._events.get_nowait()
        except queue.Empty:
            self._poll_job = self.after(80, self._poll)
            return
        self._busy = False
        self.input.configure(state="normal")
        self.preview_button.configure(state="normal")
        self.select_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self._preview_text = None  # Never enqueue the same frozen batch twice.
        if kind == "done":
            self.owner.current_task_ids.update(set(value.task_ids) & set(self.owner.manager.active_task_ids()))
            self.owner._refresh_tasks()
            if value.task_ids:
                self.owner._follow_task_batch(value.task_ids)
            self.status.set(f"已加入 {len(value.task_ids)} 项，未能入队 {value.failures} 项。汇总：{value.report}"
                            + ("；" + "；".join(value.warnings) if value.warnings else ""))
            self.owner.status_var.set(f"混合批次已加入 {len(value.task_ids)} 个任务")
        else:
            self.status.set("批次入队失败：" + value)
        if self._closing:
            self.destroy()

    def _close(self):
        if self._busy:
            self._closing = True
            self._stop.set()
            self.status.set("正在停止后续入队；已创建任务继续由主窗口管理")
        else:
            self.destroy()
