"""Native Windows smoke checks. Uses a new isolated runtime and generated files only."""
from __future__ import annotations

import json
import ctypes
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path
from unittest.mock import patch


def capture_window(window, path):
    """Capture this Tk client only; overlapping desktop windows must never be captured."""
    from ctypes import wintypes
    from PIL import Image

    class BitmapInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG), ("height", wintypes.LONG),
                    ("planes", wintypes.WORD), ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                    ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG), ("yppm", wintypes.LONG),
                    ("colors", wintypes.DWORD), ("important", wintypes.DWORD)]

    user32, gdi = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetDC.argtypes, user32.GetDC.restype = [wintypes.HWND], wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.GetAncestor.argtypes, user32.GetAncestor.restype = [wintypes.HWND, wintypes.UINT], wintypes.HWND
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    gdi.CreateCompatibleDC.argtypes, gdi.CreateCompatibleDC.restype = [wintypes.HDC], wintypes.HDC
    gdi.CreateCompatibleBitmap.argtypes, gdi.CreateCompatibleBitmap.restype = [wintypes.HDC, ctypes.c_int, ctypes.c_int], wintypes.HBITMAP
    gdi.SelectObject.argtypes, gdi.SelectObject.restype = [wintypes.HDC, wintypes.HANDLE], wintypes.HANDLE
    gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi.DeleteObject.argtypes, gdi.DeleteDC.argtypes = [wintypes.HANDLE], [wintypes.HDC]
    window.update_idletasks()
    width, height = window.winfo_width(), window.winfo_height()
    hwnd = user32.GetAncestor(window.winfo_id(), 2)
    source_dc = user32.GetDC(hwnd)
    memory_dc = gdi.CreateCompatibleDC(source_dc)
    bitmap = gdi.CreateCompatibleBitmap(source_dc, width, height)
    previous = gdi.SelectObject(memory_dc, bitmap)
    try:
        if not user32.PrintWindow(hwnd, memory_dc, 3):
            raise RuntimeError("PrintWindow failed")
        info = BitmapInfo(ctypes.sizeof(BitmapInfo), width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        gdi.SelectObject(memory_dc, previous)
        if not gdi.GetDIBits(memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0):
            raise RuntimeError("GetDIBits failed")
        captured = Image.frombuffer("RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1)
        if all(low == high for low, high in captured.getextrema()):
            raise RuntimeError("window capture produced an empty image")
        captured.save(path)
    finally:
        gdi.SelectObject(memory_dc, previous)
        gdi.DeleteObject(bitmap)
        gdi.DeleteDC(memory_dc)
        user32.ReleaseDC(hwnd, source_dc)


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    root = repo / "data" / "software_app" / "verification" / f"native-{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=False)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")

    import tkinter as tk
    from tkinter import messagebox, ttk
    from PIL import Image
    from software_app.core.adapter import CrawlerAdapter, TaskCancelled
    from software_app.core.models import ModuleInfo, TargetPreview, FileRecord
    from software_app.core.task_control import task_cancelled
    from software_app.core.bandwidth import MEDIA_BANDWIDTH
    from software_app.core.browser_pool import BROWSER_POOL
    from software_app.core.media_results import media_descriptor, retry_folder, run_media_jobs
    from software_app.ui.media_results import MediaResultsDialog
    from software_app.ui.tk_app import SoftwareDesktop

    checks = []
    callback_errors = []
    unraisable_errors = []
    dialogs = []
    app = None

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        (root / "progress.json").write_text(json.dumps({"checks": checks}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"PASS {name}", flush=True)

    def pump_until(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            app.update()
            if predicate():
                return
            time.sleep(0.02)
        raise AssertionError("Tk operation timed out")

    def button(widget, text):
        pending, seen = deque([widget]), set()
        while pending:
            current = pending.popleft()
            key = str(current)
            if key in seen:
                continue
            seen.add(key)
            # Some composite widgets delegate child discovery to an inner
            # widget. Visit each Tk pathname once and query only real buttons.
            if isinstance(current, (ttk.Button, tk.Button)) and str(current.cget("text")) == text:
                return current
            pending.extend(current.winfo_children())
        return None

    class SmokeAdapter(CrawlerAdapter):
        info = ModuleInfo("smoke", "Local smoke")

        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.finished = threading.Event()

        def preview_target(self, raw_target, options=None):
            return TargetPreview("smoke", raw_target, raw_target)

        def download(self, task, callbacks, cancel_event):
            self.entered.set()
            if not self.release.wait(8):
                raise RuntimeError("test adapter not released")
            if task_cancelled(cancel_event):
                raise TaskCancelled("cancelled")
            self.finished.set()

    class BatchSmokeAdapter(CrawlerAdapter):
        def __init__(self, module_id):
            self.info = ModuleInfo(module_id, f"Local {module_id}")

        def preview_target(self, raw_target, options=None):
            return TargetPreview(self.module_id, raw_target, raw_target)

        def download(self, task, callbacks, cancel_event):
            path = task.output_dir / "batch-sample.png"
            Image.new("RGB", (24, 24), "#68b0ab").save(path)
            callbacks.on_file(FileRecord(path, task.module_id, task.task_id, "image", path.stat().st_size))

    class PostprocessSmokeAdapter(CrawlerAdapter):
        info = ModuleInfo("postprocess-smoke", "Local postprocess smoke")

        def preview_target(self, raw_target, options=None):
            return TargetPreview(self.module_id, raw_target, raw_target, title="合成样本")

        def download(self, task, callbacks, cancel_event):
            from software_app.core.image_postprocess import build_image_artifacts
            task.output_dir.mkdir(parents=True, exist_ok=True)
            source = task.output_dir / "source.png"
            Image.new("RGB", (24, 32), "#68b0ab").save(source)
            (task.output_dir / "compose.pdf").write_bytes(b"previous pdf")
            (task.output_dir / "compose_long.jpg").write_bytes(b"previous long")
            for path, kind in build_image_artifacts(
                [source], task.output_dir, "compose", "both", file_conflict=task.options["file_conflict"],
                cancel_event=cancel_event,
            ):
                callbacks.on_file(FileRecord(path, task.module_id, task.task_id, kind, path.stat().st_size))

    class ErrorPolicySmokeAdapter(CrawlerAdapter):
        info = ModuleInfo("error-policy-smoke", "Local error policy smoke")
        calls = 0

        def preview_target(self, raw_target, options=None):
            return TargetPreview(self.module_id, raw_target, raw_target)

        def download(self, task, callbacks, cancel_event):
            import requests
            self.calls += 1
            response = requests.Response()
            response.status_code = 404
            raise requests.HTTPError("generated missing resource", response=response)

    class MediaSmokeAdapter(CrawlerAdapter):
        info = ModuleInfo("media-smoke", "Local media outcomes")
        supports_media_retry = True
        reports_files = True

        def __init__(self):
            self.fail = True
            self.indices = []

        def preview_target(self, raw_target, options=None):
            return TargetPreview(self.module_id, raw_target, raw_target)

        def download(self, task, callbacks, cancel_event):
            items = task.options.get("_retry_media")
            if items is None:
                items = [media_descriptor(task, task.target, index, "image",
                                          f"https://cdn.example/{index}.jpg?sig=generated-secret",
                                          task.output_dir / "pages") for index in (1, 2)]

            def save(item):
                self.indices.append(item["index"])
                if self.fail and item["index"] == 2:
                    raise ValueError("generated media failure")
                folder = retry_folder(task, item)
                folder.mkdir(parents=True, exist_ok=True)
                path = folder / f"{item['index']}.png"
                Image.new("RGB", (24, 24), "#68b0ab").save(path)
                return FileRecord(path, task.module_id, task.task_id, "image", path.stat().st_size)

            run_media_jobs(task, callbacks, cancel_event, [(item, lambda item=item: save(item)) for item in items])

    try:
        with patch.object(tk.Tk, "report_callback_exception", lambda _self, kind, value, trace: callback_errors.append(str(value))), patch.multiple(
            messagebox, showinfo=lambda *a, **k: dialogs.append("info"),
            showwarning=lambda *a, **k: dialogs.append("warning"),
            showerror=lambda *a, **k: dialogs.append("error"), askyesno=lambda *a, **k: True,
        ), patch("requests.sessions.Session.request", side_effect=RuntimeError("offline smoke test")), patch.object(
            sys, "unraisablehook", lambda error: unraisable_errors.append(f"{error.exc_type.__name__}: {error.exc_value}")
        ):
            app = SoftwareDesktop()
            app.update()
            check("main_window_created", app.winfo_exists())
            app.file_conflict_var.set("遇到重名时报错")
            app._save_settings()
            check("conflict_setting_saved", app.storage.get_setting("file_conflict") == "error")
            app.file_conflict_var.set("自动改名保留两者")
            app._save_settings()
            check("auto_rename_setting_saved", app.storage.get_setting("file_conflict") == "rename")
            app.file_conflict_var.set("复用已有文件")
            app.speed_limit_enabled_var.set(True)
            app.speed_limit_kib_var.set("64")
            app._save_settings()
            check("speed_setting_applies_immediately", MEDIA_BANDWIDTH.settings == (True, 64 * 1024))
            app.speed_limit_enabled_var.set(False)
            app._save_settings()
            check("speed_disable_keeps_value", MEDIA_BANDWIDTH.settings == (False, 64 * 1024)
                  and app.storage.get_setting("speed_limit_kib") == 64)
            app.browser_session_limit_var.set("2")
            app._save_settings()
            check("browser_limit_saved_and_applied", app.storage.get_setting("browser_session_limit") == 2
                  and BROWSER_POOL.snapshot()[1] == 2)
            adapter = SmokeAdapter()
            app.manager.register_adapter(adapter)
            task = app.manager.start_task("smoke", "local", root / "task-output", callbacks=app._task_callbacks())
            pump_until(adapter.entered.is_set)
            app._refresh_tasks()
            app.task_tree.selection_set(task.task_id)
            app._render_selected_task()
            app.task_pause_button.invoke()
            check("pause_button", app.storage.get_task(task.task_id)["status"] == "paused")
            adapter.release.set()
            for _ in range(5):
                app.update()
                time.sleep(0.02)
            check("paused_worker_stays_at_checkpoint", not adapter.finished.is_set())
            app.task_resume_button.invoke()
            pump_until(lambda: app.storage.get_task(task.task_id)["status"] == "completed")
            check("resume_button", adapter.finished.is_set())

            app.storage.create_task("restart-smoke", "smoke", "local", str(root / "restart-output"), {})
            app.storage.update_task_status("restart-smoke", "paused")
            app.storage.recover_interrupted_tasks()
            restore_button = button(app.tasks_tab, "恢复中断任务")
            check("restart_button_exists", restore_button is not None)
            with patch.object(messagebox, "askyesno", return_value=False):
                restore_button.invoke()
            check("declined_restart_keeps_pending", len(app.storage.interrupted_tasks()) == 1)
            restore_button.invoke()
            check("restart_preserves_paused_task", app.storage.get_task("restart-smoke")["status"] == "paused")
            app.manager.resume("restart-smoke")
            pump_until(lambda: app.storage.get_task("restart-smoke")["status"] == "completed")
            check("restarted_task_completes", not app.storage.interrupted_tasks())

            app.manager.register_adapter(PostprocessSmokeAdapter())
            composed = app.manager.start_task("postprocess-smoke", "sample", root / "postprocess",
                                               {"file_conflict": "rename", "archive_mode": "task"},
                                               app._task_callbacks())
            pump_until(lambda: app.storage.get_task(composed.task_id)["status"] == "completed")
            composed_rows = app.storage.list_files(task_id=composed.task_id)
            names = {Path(row["path"]).name for row in composed_rows}
            check("postprocess_actual_paths", {"compose (2).pdf", "compose (2)_long.jpg"}.issubset(names))
            check("postprocess_archive_index", sum(row["media_type"] == "archive" for row in composed_rows) == 1)
            check("postprocess_old_files_preserved", (root / "postprocess" / "compose.pdf").read_bytes() == b"previous pdf"
                  and (root / "postprocess" / "compose_long.jpg").read_bytes() == b"previous long")

            policy_adapter = ErrorPolicySmokeAdapter()
            app.manager.register_adapter(policy_adapter)
            policy_task = app.manager.start_task(policy_adapter.module_id, "sample", root / "error-policy",
                                                  {"retries": 2}, app._task_callbacks())
            pump_until(lambda: app.storage.get_task(policy_task.task_id)["status"] in {"failed", "completed"})
            check("permanent_error_no_retry", policy_adapter.calls == 1
                  and app.storage.get_task(policy_task.task_id)["status"] == "failed")
            app._refresh_tasks()
            app.task_tree.selection_set(policy_task.task_id)
            app._render_selected_task()
            check("classified_error_visible", "资源不存在" in app.task_detail_text.get("1.0", "end"))

            media = root / "media"
            media.mkdir(exist_ok=True)
            first, second = media / "rename-sample.png", media / "recycle-sample.png"
            for path in (first, second):
                Image.new("RGB", (32, 32), "#68b0ab").save(path)
                app.storage.upsert_file(FileRecord(path, "website", None, "image", path.stat().st_size, path.name))
            app._refresh_library(scan=False)
            pump_until(lambda: not app._library_worker_running and bool(app.library_all_folder_rows))
            app._selected_library_file_paths = lambda: [first]
            with patch("software_app.ui.library_tab.simpledialog.askstring", return_value="renamed.png"):
                button(app.library_tab, "重命名").invoke()
            renamed = first.with_name("renamed.png")
            check("original_rename_button", renamed.is_file() and not first.exists())
            app._selected_library_file_paths = lambda: [second]
            button(app.library_tab, "移到回收站").invoke()
            check("native_recycle_button", not second.exists())
            check("recycle_removed_exact_index", not any(row["path"] == str(second) for row in app.storage.list_files()))

            button(app.library_tab, "筛选 / 导出 / 清理").invoke()
            dialog = app._library_tools_dialog
            dialog.preview_button.invoke()
            pump_until(lambda: not dialog._busy and dialog._plan is not None)
            expected_preview_paths = {str(renamed.resolve()), *(row["path"] for row in composed_rows)}
            check("tools_preview", {entry.path for entry in dialog._plan.entries} == expected_preview_paths)
            exported = root / "library.csv"
            with patch("software_app.ui.library_tools.filedialog.asksaveasfilename", return_value=str(exported)):
                dialog.export_button.invoke()
            pump_until(lambda: not dialog._busy)
            check("tools_export", exported.is_file())
            dialog.index_button.invoke()
            pump_until(lambda: not dialog._busy)
            check("tools_index_cleanup", not app.storage.list_files() and renamed.is_file())
            dialog.destroy()

            media_adapter = MediaSmokeAdapter()
            app.manager.register_adapter(media_adapter)
            partial = app.manager.start_task("media-smoke", "https://example.com/generated-post",
                                             root / "partial-output", {"archive_mode": "task", "retries": 0},
                                             callbacks=app._task_callbacks())
            pump_until(lambda: not app.manager.active_task_ids())
            check("media_partial_task", app.storage.get_task(partial.task_id)["status"] == "partial"
                  and app.storage.media_summary(partial.task_id) == {"saved": 1, "failed": 1})
            app._refresh_tasks()
            app.task_tree.selection_set(partial.task_id)
            app._render_selected_task()
            button(app, "查看逐媒体结果").invoke()
            viewer = next(child for child in app.winfo_children() if isinstance(child, MediaResultsDialog))
            pump_until(lambda: not viewer._busy and viewer._total == 2)
            check("media_dialog_lists_results", len(viewer.tree.get_children()) == 2)
            viewer.state_var.set("失败")
            viewer.filter.event_generate("<<ComboboxSelected>>")
            pump_until(lambda: not viewer._busy and viewer._total == 1)
            check("media_dialog_filters_failures", len(viewer.tree.get_children()) == 1
                  and next(iter(viewer._rows.values()))["index"] == 2)
            viewer.tree.selection_set(viewer.tree.get_children()[0])
            viewer.tree.event_generate("<<TreeviewSelect>>")
            app.update()
            check("media_dialog_shows_reason", "generated media failure" in viewer.detail.get("1.0", "end")
                  and viewer.retry.instate(["!disabled"]))
            failures_csv = root / "failed-media.csv"
            # A Mock retains call arguments (including the Tk typevariable) in
            # cycles which a worker's GC may finalize. A plain stub owns none.
            with patch("software_app.ui.task_queue_tab.filedialog.asksaveasfilename",
                       new=lambda **_options: str(failures_csv)):
                app.media_export_button.invoke()
            pump_until(lambda: app._media_export_cancel is None)
            import csv
            with failures_csv.open(encoding="utf-8-sig", newline="") as stream:
                exported_media = list(csv.reader(stream))
            check("media_failure_csv_export", len(exported_media) == 2
                  and "generated media failure" in failures_csv.read_text(encoding="utf-8-sig")
                  and "generated-secret" not in failures_csv.read_text(encoding="utf-8-sig"))
            media_adapter.fail = False
            viewer.retry.invoke()
            pump_until(lambda: not app.manager.active_task_ids()
                       and app.storage.media_summary(partial.task_id).get("repaired") == 1)
            check("media_dialog_selected_retry_only", media_adapter.indices == [1, 2, 2]
                  and app.storage.get_task(partial.task_id)["status"] == "partial")
            viewer.state_var.set("已补全")
            viewer.filter.event_generate("<<ComboboxSelected>>")
            pump_until(lambda: not viewer._busy and viewer._total == 1)
            viewer.tree.selection_set(viewer.tree.get_children()[0])
            viewer.tree.event_generate("<<TreeviewSelect>>")
            app.update()
            check("media_dialog_repaired_cannot_retry", viewer.retry.instate(["disabled"])
                  and next(iter(viewer._rows.values()))["state"] == "repaired")
            viewer.lift()
            app.update()
            capture_window(viewer, root / "media-results.png")
            viewer._close()

            # Native mixed-batch actions use generated local samples, never platform accounts.
            for module_id in ("twitter", "website"):
                app.manager.register_adapter(BatchSmokeAdapter(module_id))
            mixed_button = button(app, "混合平台批量任务")
            check("mixed_batch_entry", mixed_button is not None)
            mixed_button.invoke()
            mixed = app._mixed_batch_dialog
            mixed.input.insert("1.0", "https://x.com/example/status/123\nhttps://example.com/a\n"
                               "https://twitter.com/another/status/123\n@example\nhttps://pixiv.net/tags/example")
            app.update()
            before_tasks = len(app.storage.list_tasks())
            mixed.preview_button.invoke()
            check("mixed_offline_preview_and_dedup", tuple(mixed.tree.selection()) == ("1", "2")
                  and mixed.tree.set("3", "state") == "重复" and mixed.tree.set("4", "state") == "无效"
                  and len(app.storage.list_tasks()) == before_tasks)
            mixed.input.insert("end", "\n")
            app.update()
            check("mixed_input_change_invalidates_preview", mixed._preview_text is None and mixed.submit_button.instate(["disabled"]))
            mixed.preview_button.invoke()
            with patch.object(messagebox, "askyesno", return_value=False):
                mixed.submit_button.invoke()
            check("mixed_declined_confirmation", not mixed._busy and len(app.storage.list_tasks()) == before_tasks)
            mixed.tree.selection_set("2")
            mixed.submit_button.invoke()
            pump_until(lambda: not mixed._busy and not app.manager.active_task_ids())
            _, subset = app.storage.batch_snapshot(mixed._batch_id)
            check("mixed_selected_subset_only", len(subset) == 1 and subset[0]["module_id"] == "website")
            mixed.preview_button.invoke()
            mixed.submit_button.invoke()
            pump_until(lambda: not mixed._busy and not app.manager.active_task_ids())
            batch, batch_items = app.storage.batch_snapshot(mixed._batch_id)
            check("mixed_two_platform_enqueue", {item["module_id"] for item in batch_items} == {"twitter", "website"}
                  and all(item["status"] == "completed" for item in batch_items))
            summary = Path(batch["output_dir"]) / "批次汇总.txt"
            check("mixed_updated_summary", "已完成 2" in summary.read_text(encoding="utf-8-sig")
                  and (Path(batch["output_dir"]) / "001_twitter/batch-sample.png").is_file()
                  and (Path(batch["output_dir"]) / "002_website/batch-sample.png").is_file())
            check("mixed_same_preview_cannot_enqueue_twice", mixed.submit_button.instate(["disabled"]))
            mixed.lift()
            app.update()
            capture_window(mixed, root / "mixed-batch.png")
            mixed.destroy()
            app.lift()
            for _ in range(8):
                app.update()
                time.sleep(0.03)
            screenshot = root / "desktop.png"
            capture_window(app, screenshot)
            check("no_tk_callback_errors", not callback_errors)
            check("no_unraisable_errors", not unraisable_errors)
            check("no_error_dialogs", "error" not in dialogs)
        result = {"ok": True, "checks": checks, "callback_errors": callback_errors,
                  "unraisable_errors": unraisable_errors, "screenshot": str(screenshot)}
    except Exception as exc:
        result = {"ok": False, "checks": checks, "error": f"{type(exc).__name__}: {exc}",
                  "traceback": traceback.format_exc(), "callback_errors": callback_errors,
                  "unraisable_errors": unraisable_errors}
    finally:
        # Preserve evidence even if native window teardown aborts the process.
        (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        if app is not None:
            app.destroy()
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
