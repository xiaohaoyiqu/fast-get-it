from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from software_app.core.person_config import PersonConfigs, person_key, FORMAT_VALUES, validated_options, settings_source
from software_app.core.preflight import inspect_environment
from software_app.core.security import safe_error_message


class PersonConfigMixin:
    def _refresh_person_config_hint(self):
        if not hasattr(self, "person_config_hint_var"):
            return
        try:
            targets = self._current_input_targets()
            if len(targets) != 1:
                text = "人物配置：多目标逐个精确匹配" if targets else "人物配置：未选择目标"
            else:
                options = self._current_target_options(targets[0])
                key = person_key(self.module_var.get(), targets[0], str(options.get("input_kind") or ""))
                profile = PersonConfigs(self.storage).get(key)
                text = (
                    f"人物配置：{profile['label']} · {key[0]}/{key[1]}"
                    if profile
                    else "人物配置：使用当前设置；仅明确的作者／账号目标自动匹配"
                )
            self.person_config_hint_var.set(text)
        except Exception as exc:
            self.person_config_hint_var.set("人物配置无法读取：" + safe_error_message(exc))

    def _open_person_config(self):
        target = self._single_target_for_action("人物配置")
        if not target:
            return
        module_id = self.module_var.get()
        options = self._current_target_options(target)
        key = person_key(module_id, target, str(options.get("input_kind") or ""))
        if key is None:
            messagebox.showinfo(
                "请选择明确账号",
                "人物配置支持 Twitter、Pixiv、Bluesky 和 Instagram 的作者主页或账号。\n"
                "Pixiv 数字 ID 请先选择作者范围；单帖、作品、搜索词不自动推断作者。",
            )
            return
        self._edit_person_config(key, options)

    def _edit_person_config(self, key, options=None, on_saved=None):
        module_id = key[0]
        store = PersonConfigs(self.storage)
        try:
            profile = store.get(key)
        except (ValueError, TypeError) as exc:
            messagebox.showerror("人物配置损坏", safe_error_message(exc), parent=self)
            try:
                profile = store.metadata(key)
            except Exception as metadata_error:
                messagebox.showerror("配置无法读取", safe_error_message(metadata_error), parent=self)
                return
            if profile is None or not messagebox.askyesno(
                "重设损坏配置",
                f"为 {module_id}/{key[1]} 打开重设窗口？\n保存后才替换损坏参数，已有任务不受影响。",
                parent=self,
            ):
                return
            profile["options"] = {}
        except Exception as exc:
            messagebox.showerror("配置无法读取", safe_error_message(exc), parent=self)
            return
        expected_revision = profile["revision"] if profile else ""
        options = options or {}
        values = profile["options"] if profile else options
        dialog = tk.Toplevel(self)
        dialog.title("人物配置 · " + key[1])
        dialog.transient(self)
        dialog.columnconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text=f"固定绑定：{module_id} / {key[1]}\n只作用于这个账号目标；不会修改全局设置或 Cookie。",
            wraplength=520,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=10)
        label = tk.StringVar(dialog, value=profile["label"] if profile else key[1])
        directory = tk.StringVar(dialog, value=profile["output_dir"] if profile else "")
        variables = {
            name: tk.StringVar(dialog, value=str(values.get(name, defaults)))
            for name, defaults in (
                ("types", "1,2,3,4"),
                ("image_format", "original"),
                ("video_format", "original"),
                ("animation_format", "gif"),
                ("audio_format", "mp3"),
                ("max_works", 20),
            )
        }
        skip_ai = tk.BooleanVar(dialog, value=bool(values.get("filter_ai", True)))
        fields = [("配置名称", label, None), ("下载类型（1图 2视频 3动图 4音频）", variables["types"], None)]
        fields += [
            (title, variables[name], sorted(FORMAT_VALUES[name]))
            for title, name in (
                ("图片格式", "image_format"),
                ("视频格式", "video_format"),
                ("动图格式", "animation_format"),
                ("音频格式", "audio_format"),
            )
        ]
        if module_id == "pixiv":
            fields.append(("Pixiv 最多作品", variables["max_works"], None))
        fields.append(("保存目录（空白沿用当前目录）", directory, None))
        for row, (title, variable, choices) in enumerate(fields, 1):
            ttk.Label(dialog, text=title).grid(row=row, column=0, sticky="w", padx=12, pady=5)
            widget = (
                ttk.Combobox(dialog, textvariable=variable, values=choices, state="readonly")
                if choices
                else ttk.Entry(dialog, textvariable=variable, width=36)
            )
            widget.grid(row=row, column=1, sticky="ew", padx=12, pady=5)

        def browse():
            selected = filedialog.askdirectory(parent=dialog, initialdir=directory.get() or self.output_dir_var.get())
            if selected:
                directory.set(selected)

        ttk.Button(dialog, text="浏览", command=browse).grid(row=len(fields), column=2, padx=8)
        if module_id == "pixiv":
            ttk.Checkbutton(dialog, text="Pixiv 跳过 AI 作品", variable=skip_ai).grid(
                row=len(fields) + 1, column=0, columnspan=2, sticky="w", padx=12
            )
        ttk.Label(
            dialog,
            text="用户名会改名或被重新注册；请核对固定绑定。Pixiv 使用数字 ID，Bluesky 可使用 DID。\n"
            "混合批次保留批次子目录；已有任务和重试沿用创建时的配置。",
            wraplength=520,
        ).grid(row=len(fields) + 2, column=0, columnspan=3, sticky="w", padx=12, pady=10)

        def save():
            try:
                saved = {name: variable.get().strip() for name, variable in variables.items()}
                maximum = saved.pop("max_works")
                if module_id == "pixiv":
                    saved.update(max_works=int(maximum), filter_ai=skip_ai.get())
                store.save(key, label.get(), saved, directory.get(), expected_revision=expected_revision)
            except Exception as exc:
                messagebox.showerror("配置未保存", safe_error_message(exc), parent=dialog)
                return
            self._refresh_person_config_hint()
            dialog.destroy()
            if on_saved:
                on_saved()

        def remove():
            if messagebox.askyesno(
                "删除人物配置", f"删除 {module_id}/{key[1]} 的配置？已有任务不受影响。", parent=dialog
            ):
                try:
                    store.delete(key, expected_revision=expected_revision)
                except Exception as exc:
                    messagebox.showerror("配置未删除", safe_error_message(exc), parent=dialog)
                    return
                self._refresh_person_config_hint()
                dialog.destroy()
                if on_saved:
                    on_saved()

        buttons = ttk.Frame(dialog)
        buttons.grid(row=len(fields) + 3, column=0, columnspan=3, sticky="e", padx=12, pady=12)
        ttk.Button(buttons, text="删除此配置", command=remove).pack(side="left", padx=5)
        ttk.Button(buttons, text="保存此人物配置", command=save).pack(side="left", padx=5)
        dialog.grab_set()

    def _open_person_config_overview(self):
        dialog = tk.Toplevel(self)
        dialog.title("人物配置总览")
        dialog.geometry("1000x560")
        dialog.transient(self)
        bar = ttk.Frame(dialog, padding=10)
        bar.pack(fill="x")
        query = tk.StringVar(dialog)
        platform = tk.StringVar(dialog, value="全部")
        ttk.Label(bar, text="账号或名称").pack(side="left")
        search = ttk.Entry(bar, textvariable=query, width=28)
        search.pack(side="left", padx=6)
        ttk.Combobox(
            bar,
            textvariable=platform,
            values=("全部", "twitter", "pixiv", "bluesky", "instagram"),
            state="readonly",
            width=14,
        ).pack(side="left", padx=6)
        columns = ("platform", "account", "label", "directory", "modified", "state")
        body = ttk.Frame(dialog)
        body.pack(fill="both", expand=True, padx=10)
        tree = ttk.Treeview(body, columns=columns, show="headings", selectmode="browse")
        for name, title, width in zip(
            columns,
            ("平台", "账号", "名称", "目录", "修改时间（UTC）", "状态"),
            (95, 150, 140, 220, 150, 170),
            strict=True,
        ):
            tree.heading(name, text=title)
            tree.column(name, width=width)
        scroll = ttk.Scrollbar(body, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        tree.pack(fill="both", expand=True)
        footer = ttk.Frame(dialog, padding=10)
        footer.pack(fill="x")
        status = tk.StringVar(dialog)
        ttk.Label(footer, textvariable=status).pack(side="left")
        state = {"offset": 0, "total": 0, "rows": {}, "loading": False, "timer": None}
        events = queue.Queue()
        store = PersonConfigs(self.storage)

        def load(reset=False):
            if not dialog.winfo_exists() or state["loading"]:
                return
            if reset:
                state["offset"] = 0
            state["loading"] = True
            status.set("正在读取…")
            search_value, platform_value, offset = query.get(), platform.get(), state["offset"]

            def worker():
                try:
                    events.put(
                        (
                            store.list_page(search_value, "" if platform_value == "全部" else platform_value, offset),
                            None,
                        )
                    )
                except Exception as exc:
                    events.put((None, safe_error_message(exc)))

            threading.Thread(target=worker, name="person-config-list", daemon=True).start()
            state["timer"] = dialog.after(80, poll)

        def poll():
            state["timer"] = None
            try:
                result, error = events.get_nowait()
            except queue.Empty:
                state["timer"] = dialog.after(80, poll)
                return
            state["loading"] = False
            if error:
                status.set("读取失败")
                messagebox.showerror("配置总览无法读取", error, parent=dialog)
                return
            rows, total = result
            tree.delete(*tree.get_children())
            state["rows"] = {str(index): item for index, item in enumerate(rows)}
            state["total"] = total
            for iid, item in state["rows"].items():
                tree.insert(
                    "",
                    "end",
                    iid=iid,
                    values=(
                        item["module_id"],
                        item["account_key"],
                        item["label"],
                        item["output_dir"] or "沿用当前目录",
                        item["updated_at"] or "旧记录未记时",
                        item["state"],
                    ),
                )
            status.set(f"共 {total} 项 · 第 {state['offset'] // 100 + 1} 页 · 每页最多 100 项")

        def selected():
            if state["loading"]:
                return None
            selection = tree.selection()
            return state["rows"].get(selection[0]) if selection else None

        def edit():
            item = selected()
            if item:
                self._edit_person_config((item["module_id"], item["account_key"]), on_saved=load)

        def remove():
            item = selected()
            if not item or not messagebox.askyesno(
                "删除人物配置",
                f"删除 {item['module_id']}/{item['account_key']} 的配置？已有任务不受影响。",
                parent=dialog,
            ):
                return
            try:
                store.delete((item["module_id"], item["account_key"]), expected_revision=item["revision"])
            except Exception as exc:
                messagebox.showerror("配置未删除", safe_error_message(exc), parent=dialog)
                return
            self._refresh_person_config_hint()
            load(reset=True)

        def page(delta):
            offset = state["offset"] + delta * 100
            if not state["loading"] and 0 <= offset < state["total"]:
                state["offset"] = offset
                load()

        def close(event):
            if event.widget is dialog and state["timer"] is not None:
                dialog.after_cancel(state["timer"])
                state["timer"] = None

        dialog.bind("<Destroy>", close, add="+")
        search.bind("<Return>", lambda _event: load(reset=True))
        tree.bind("<Double-1>", lambda _event: edit())
        ttk.Button(bar, text="搜索 / 刷新", command=lambda: load(reset=True)).pack(side="left", padx=6)
        for label, command in (
            ("上一页", lambda: page(-1)),
            ("下一页", lambda: page(1)),
            ("修改 / 重设", edit),
            ("删除", remove),
        ):
            ttk.Button(footer, text=label, command=command).pack(side="right", padx=4)
        load()

    def _edit_task_overrides(self, prepared):
        """Return task-only values; never save into UI defaults or person profiles."""
        dialog = tk.Toplevel(self)
        dialog.title("本次任务设置")
        dialog.transient(self)
        dialog.columnconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text="以下修改仅用于本批新任务，优先于人物配置；留空表示沿用。\n不会保存到人物默认值或全局设置。",
            wraplength=680,
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=12, pady=10)
        preview = tk.Text(dialog, height=7, width=88, wrap="word")
        preview.grid(row=1, column=0, columnspan=3, sticky="ew", padx=12)
        preview.insert(
            "end",
            "\n".join(
                f"{target} · {settings_source(options)} → {directory}\n"
                f"类型 {options.get('types', '1,2,3,4')}，图片 {options.get('image_format', 'original')}，"
                f"视频 {options.get('video_format', 'original')}，动图 {options.get('animation_format', 'gif')}，"
                f"音频 {options.get('audio_format', 'mp3')}"
                for target, directory, options in prepared
            ),
        )
        preview.configure(state="disabled")
        variables = {name: tk.StringVar(dialog) for name in ("types", *FORMAT_VALUES)}
        for row, (name, title) in enumerate(
            (
                ("types", "下载类型（1图 2视频 3动图 4音频）"),
                ("image_format", "图片格式"),
                ("video_format", "视频格式"),
                ("animation_format", "动图格式"),
                ("audio_format", "音频格式"),
            ),
            2,
        ):
            ttk.Label(dialog, text=title).grid(row=row, column=0, sticky="w", padx=12, pady=5)
            if name in FORMAT_VALUES:
                widget = ttk.Combobox(
                    dialog, textvariable=variables[name], state="readonly", values=("", *sorted(FORMAT_VALUES[name]))
                )
            else:
                widget = ttk.Entry(dialog, textvariable=variables[name])
            widget.grid(row=row, column=1, sticky="ew", padx=12, pady=5)
        directory = tk.StringVar(dialog)
        ttk.Label(dialog, text="本次保存目录（空白沿用）").grid(row=7, column=0, padx=12, pady=5)
        ttk.Entry(dialog, textvariable=directory).grid(row=7, column=1, sticky="ew", padx=12, pady=5)

        def browse():
            path = filedialog.askdirectory(parent=dialog, initialdir=self.output_dir_var.get())
            if path:
                directory.set(path)

        result = []

        def apply():
            try:
                values = validated_options(
                    {name: variable.get().strip() for name, variable in variables.items() if variable.get().strip()}
                )
                path = Path(directory.get().strip()).expanduser().resolve() if directory.get().strip() else None
                if path and path.exists() and not path.is_dir():
                    raise ValueError("本次保存目录指向文件")
                for target, base_directory, options in prepared:
                    updated = dict(options)
                    if values or path:
                        updated["_task_override"] = {"options": values.copy(), "output_dir": str(path) if path else ""}
                        updated.update(values)
                    result.append((target, path or base_directory, updated))
            except Exception as exc:
                result.clear()
                messagebox.showerror("本次设置无效", safe_error_message(exc), parent=dialog)
                return
            dialog.destroy()

        ttk.Button(dialog, text="浏览", command=browse).grid(row=7, column=2, padx=8)
        buttons = ttk.Frame(dialog)
        buttons.grid(row=8, column=0, columnspan=3, sticky="e", padx=12, pady=12)
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=6)
        ttk.Button(buttons, text="使用这些设置并继续确认", command=apply).pack(side="left", padx=6)
        dialog.grab_set()
        self.wait_window(dialog)
        return result or None

    def _open_preflight(self):
        dialog = tk.Toplevel(self)
        dialog.title("执行前检查")
        dialog.geometry("760x520")
        dialog.transient(self)
        controls = ttk.Frame(dialog, padding=8)
        controls.pack(fill="x")
        probe_write = tk.BooleanVar(dialog, value=False)
        ttk.Checkbutton(controls, text="试写现有目录（创建并清理临时文件）", variable=probe_write).pack(side="left")
        text = tk.Text(dialog, wrap="word", padx=12, pady=12)
        text.pack(fill="both", expand=True)
        text.insert("end", "正在检查本机环境…")
        text.configure(state="disabled")
        events = queue.Queue()
        timer = [None]
        cancel_event = threading.Event()
        running = [False]

        def closed(event):
            if event.widget is dialog:
                cancel_event.set()
                if timer[0] is not None:
                    dialog.after_cancel(timer[0])
                    timer[0] = None

        dialog.bind("<Destroy>", closed, add="+")

        def worker(directory, entries, prefix, probe):
            try:
                report, driver = inspect_environment(
                    directory, tasks=entries, probe_write=probe, cancel_event=cancel_event
                )
                events.put(("\n".join([*prefix, report]), driver))
            except Exception as exc:
                events.put(("检查未完成：" + safe_error_message(exc), ""))

        def poll():
            timer[0] = None
            if not dialog.winfo_exists():
                return
            try:
                report, driver = events.get_nowait()
            except queue.Empty:
                timer[0] = dialog.after(80, poll)
                return
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("end", report)
            text.configure(state="disabled")
            if driver:
                self.browser_driver_status_var.set(driver)
            running[0] = False
            run_button.state(["!disabled"])

        def run():
            if running[0]:
                return
            directory = Path(self.output_dir_var.get())
            module_id = self.module_var.get()
            targets = self._current_input_targets()
            if len(targets) > 100:
                messagebox.showerror("目标过多", "执行前检查一次最多 100 个目标", parent=dialog)
                return
            entries, prefix = [], []
            for target in targets:
                try:
                    actual, options = self.manager.prepare_task_settings(
                        module_id, target, directory, self._current_target_options(target)
                    )
                    entries.append((module_id, target, actual, options))
                except Exception as exc:
                    prefix.append(f"{target}：配置读取失败，此目标未检查。" + safe_error_message(exc))
            if targets and not entries:
                messagebox.showerror("配置检查失败", "\n".join(prefix), parent=dialog)
                return
            running[0] = True
            run_button.state(["disabled"])
            threading.Thread(
                target=worker,
                args=(directory, entries if targets else None, prefix, probe_write.get()),
                name="local-preflight",
                daemon=True,
            ).start()
            timer[0] = dialog.after(80, poll)

        run_button = ttk.Button(controls, text="重新检查", command=run)
        run_button.pack(side="right")
        run()
