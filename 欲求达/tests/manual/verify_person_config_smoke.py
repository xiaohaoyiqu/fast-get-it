"""Isolated Tk checks for driver visibility, person binding and independent tasks."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    root = repo / "data/software_app/verification" / f"person-ui-{time.time_ns()}"
    root.mkdir(parents=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    import tkinter as tk
    from tkinter import ttk
    from software_app.core.adapter import CrawlerAdapter
    from software_app.core.browser_driver import BrowserDriverStatus
    from software_app.core.models import ModuleInfo, TargetPreview
    from software_app.core.person_config import PersonConfigs
    from software_app.core.preflight import inspect_environment
    from software_app.ui.tk_app import SoftwareDesktop
    from verify_desktop_smoke import capture_window

    checks, errors = [], []
    app = SoftwareDesktop()
    notifications = []

    def notice(title, message, **_kwargs):
        notifications.append(str(title))
        if title != "下载完成":
            errors.append(f"Unexpected info dialog: {title}: {message}")

    def unexpected(title, message, **_kwargs):
        errors.append(f"Unexpected warning/error dialog: {title}: {message}")

    dialog_patches = [
        patch("tkinter.messagebox.showinfo", side_effect=notice),
        patch("tkinter.messagebox.showwarning", side_effect=unexpected),
        patch("tkinter.messagebox.showerror", side_effect=unexpected),
    ]
    for dialog_patch in dialog_patches:
        dialog_patch.start()
    app.geometry("1280x900")
    app.report_callback_exception = lambda *args: errors.append(str(args))

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def pump_until(condition, timeout=6):
        deadline = time.monotonic() + timeout
        while not condition():
            app.update()
            if time.monotonic() >= deadline:
                raise TimeoutError("UI action timed out")
            time.sleep(0.02)
        app.update()

    def button(parent, title):
        pending = [parent]
        seen = set()
        while pending:
            widget = pending.pop()
            if str(widget) in seen:
                continue
            seen.add(str(widget))
            if isinstance(widget, ttk.Button) and widget.cget("text") == title:
                return widget
            pending.extend(widget.winfo_children())
        raise AssertionError("Missing button: " + title)

    def dialog_named(title):
        return next(
            widget
            for widget in app.winfo_children()
            if isinstance(widget, tk.Toplevel) and widget.title().startswith(title)
        )

    class LocalAccount(CrawlerAdapter):
        info = ModuleInfo("twitter", "Local account test")
        reports_files = True

        def preview_target(self, target, options=None):
            return TargetPreview("twitter", target, target)

        def download(self, task, callbacks, cancel_event):
            return None

    try:
        app.update()
        app.notebook.select(app.settings_tab)
        app.update()
        status = BrowserDriverStatus(root / "driver.exe", "123.1", "123.2", "test", "本机测试：版本匹配")
        with patch("software_app.core.browser_driver.chromedriver_status", return_value=status):
            app._check_browser_driver(False)
            pump_until(lambda: "123.2" in app.browser_driver_status_var.get())
        app.settings_canvas.yview_moveto(1.0)
        app.update()
        check(
            "driver_result_has_separate_grid_row",
            int(app.driver_status_label.grid_info()["row"]) == 12
            and int(app.driver_check_button.master.grid_info()["row"]) == 11,
        )
        check(
            "driver_result_is_visible",
            app.driver_status_label.winfo_ismapped()
            and app.driver_status_label.winfo_rooty() < app.winfo_rooty() + app.winfo_height(),
        )
        check(
            "driver_result_contains_versions_and_path",
            all(text in app.browser_driver_status_var.get() for text in ("123.1", "123.2", str(root / "driver.exe"))),
        )
        capture_window(app, root / "driver-settings.png")

        app._select_module("twitter")
        app.target_var.set("alice")
        app.update()
        app._open_person_config()
        app.update()
        dialog = dialog_named("人物配置")
        entries = {
            int(widget.grid_info()["row"]): widget
            for widget in dialog.winfo_children()
            if isinstance(widget, ttk.Entry) and not isinstance(widget, ttk.Combobox)
        }
        for row, value in ((1, "Alice pictures"), (2, "1"), (7, str(root / "alice-output"))):
            entries[row].delete(0, "end")
            entries[row].insert(0, value)
        capture_window(dialog, root / "person-editor.png")
        button(dialog, "保存此人物配置").invoke()
        app.update()
        store = PersonConfigs(app.storage)
        check("editor_saves_exact_account", store.get(("twitter", "alice"))["label"] == "Alice pictures")
        check("hint_displays_matching_profile", "Alice pictures" in app.person_config_hint_var.get())
        app.target_var.set("bob")
        app.update()
        check("switching_account_clears_old_profile_hint", "Alice pictures" not in app.person_config_hint_var.get())
        store.save(("twitter", "bob"), "Bob videos", {"types": "2", "image_format": "original"})
        app.target_var.set("alice")
        app.update()

        with patch("software_app.ui.person_config.inspect_environment", wraps=inspect_environment):
            app._open_preflight()
            report_dialog = dialog_named("执行前检查")
            output = next(widget for widget in report_dialog.winfo_children() if isinstance(widget, tk.Text))
            pump_until(lambda: "设置来源：" in output.get("1.0", "end"))
            check("preflight_shows_applied_person", "Alice pictures" in output.get("1.0", "end"))
            capture_window(report_dialog, root / "preflight.png")
            report_dialog.destroy()

        app.manager.register_adapter(LocalAccount())
        first = app.manager.start_task("twitter", "alice", root / "media", {"types": "4"})
        second = app.manager.start_task("twitter", "bob", root / "media", {"types": "4"})
        app.manager.wait(first.task_id, timeout=6)
        app.manager.wait(second.task_id, timeout=6)
        check(
            "two_tasks_have_different_media_settings", (first.options["types"], second.options["types"]) == ("1", "2")
        )
        check("person_directory_applied_to_matching_task", first.output_dir == root / "alice-output")
        check("other_person_keeps_own_directory", second.output_dir == root / "media")
        store.save(("twitter", "alice"), "Alice changed", {"types": "3"})
        app.storage.update_task_status(first.task_id, "failed", "generated test failure")

        def retry_from_ui(current_settings=False):
            before = {row["task_id"] for row in app.storage.list_tasks()}
            app._refresh_tasks()
            app.task_tree.selection_set(first.task_id)
            with patch("software_app.ui.task_queue_tab.messagebox.askyesno", return_value=True):
                app._retry_selected_task(use_current_settings=current_settings)
            (new_id,) = {row["task_id"] for row in app.storage.list_tasks()} - before
            app.manager.wait(new_id, timeout=6)
            return json.loads(app.storage.get_task(new_id)["options_json"])

        retried = retry_from_ui()
        check(
            "retry_keeps_original_snapshot",
            retried["types"] == "1" and retried["_person_config"]["label"] == "Alice pictures",
        )
        current = retry_from_ui(True)
        check("explicit_current_retry_uses_updated_person", current["types"] == "3")
        app._refresh_tasks()
        app.notebook.select(app.tasks_tab)
        app.task_tree.selection_set(first.task_id)
        app._render_selected_task()
        app.update()
        check(
            "task_detail_contains_original_person_snapshot", "Alice pictures" in app.task_detail_text.get("1.0", "end")
        )
        capture_window(app, root / "task-settings.png")
        check("no_tk_callback_errors", not errors)
    finally:
        app.destroy()
        for dialog_patch in reversed(dialog_patches):
            dialog_patch.stop()
        (root / "report.json").write_text(
            json.dumps(
                {
                    "ok": len(checks) == 14 and not errors,
                    "checks": checks,
                    "errors": errors,
                    "notifications": notifications,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    print(
        json.dumps(
            {
                "ok": len(checks) == 14 and not errors,
                "checks": checks,
                "passed": len(checks),
                "errors": errors,
                "report": str(root / "report.json"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
