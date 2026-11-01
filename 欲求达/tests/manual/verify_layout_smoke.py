"""Check real control reachability across desktop pages without platform requests."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", default="twitter")
    parser.add_argument("--scope", default="")
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    root = repo / "data/software_app/verification" / f"layout-{time.time_ns()}"
    root.mkdir(parents=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    sys.path.insert(0, str(repo))
    sys.path.insert(0, str(Path(__file__).parent))
    import tkinter as tk
    from tkinter import ttk
    from verify_desktop_smoke import capture_window
    from software_app.ui.tk_app import SoftwareDesktop

    app = SoftwareDesktop()
    app._select_module(args.platform)
    if args.scope:
        app.content_scope_var.set(args.scope)
        app._content_scope_changed()
    errors = []
    app.report_callback_exception = lambda _typ, value, _tb: errors.append(str(value))
    checks = []
    screenshots = []
    original_scale = float(app.tk.call("tk", "scaling"))

    def pump():
        for _ in range(4):
            app.update()
            time.sleep(0.015)

    def widgets(parent):
        pending = [parent]
        seen = set()
        while pending:
            child = pending.pop()
            if str(child) in seen:
                continue
            seen.add(str(child))
            pending.extend(child.winfo_children())
            yield child

    def reveal(widget):
        ancestor = widget.master
        canvases = []
        while ancestor is not None:
            if isinstance(ancestor, tk.Canvas):
                canvases.append(ancestor)
            ancestor = ancestor.master
        for canvas in reversed(canvases):
            bounds = canvas.bbox("all")
            if not bounds:
                continue
            x = canvas.canvasx(0) + widget.winfo_rootx() - canvas.winfo_rootx()
            y = canvas.canvasy(0) + widget.winfo_rooty() - canvas.winfo_rooty()
            canvas.xview_moveto(max(0, x - 6) / max(1, bounds[2]))
            canvas.yview_moveto(max(0, y - 6) / max(1, bounds[3]))
            pump()

    def check_button(button, context):
        reveal(button)
        x, y = button.winfo_rootx(), button.winfo_rooty()
        width, height = button.winfo_width(), button.winfo_height()
        ancestor = button.master
        while ancestor is not None:
            px, py = ancestor.winfo_rootx(), ancestor.winfo_rooty()
            assert (
                x >= px - 1
                and y >= py - 1
                and x + width <= px + ancestor.winfo_width() + 1
                and y + height <= py + ancestor.winfo_height() + 1
            ), (
                context,
                button.cget("text"),
                str(ancestor),
                [x, y, width, height],
                {"bbox": ancestor.bbox("all"), "view": ancestor.yview(), "height": ancestor.winfo_height()}
                if isinstance(ancestor, tk.Canvas)
                else {},
            )
            ancestor = ancestor.master
        center_x, center_y = x + width // 2, y + height // 2
        for sibling in button.master.winfo_children():
            if sibling is button or not sibling.winfo_viewable():
                continue
            sx, sy = sibling.winfo_rootx(), sibling.winfo_rooty()
            assert not (sx <= center_x < sx + sibling.winfo_width() and sy <= center_y < sy + sibling.winfo_height()), (
                context,
                "overlapping control",
                button.cget("text"),
                str(sibling),
            )
        checks.append(context + ": " + str(button.cget("text")))

    try:
        for scale in (original_scale, original_scale * 1.25):
            app.tk.call("tk", "scaling", scale)
            for width, height in ((1240, 780), (1080, 680), (1000, 640)):
                app.geometry(f"{width}x{height}+0+0")
                app.lift()
                pump()
                for index, tab in enumerate(app.notebook.tabs()):
                    app.notebook.select(tab)
                    pump()
                    context = f"{width}x{height} scale={scale:.2f} tab={index}"
                    buttons = [
                        widget
                        for widget in widgets(app)
                        if isinstance(widget, (ttk.Button, ttk.Menubutton)) and widget.winfo_viewable()
                    ]
                    for button in buttons:
                        check_button(button, context)
                    if tab == str(app.tasks_tab):
                        assert app.task_detail_text.winfo_height() >= 55, (
                            context,
                            "task log squeezed out",
                            app.task_detail_text.winfo_height(),
                            [
                                (str(widget), widget.winfo_height(), widget.winfo_reqheight())
                                for widget in (app.task_detail_text.master, app.task_detail_text.master.master)
                            ],
                        )
                        checks.append(context + ": task log visible")
                    if tab == str(app.google_search_tab):
                        assert app.google_result_tree.winfo_height() >= 70, (context, "search results squeezed out")
                        checks.append(context + ": search table visible")
                    for canvas in widgets(app):
                        if isinstance(canvas, tk.Canvas):
                            canvas.yview_moveto(0)
                            canvas.xview_moveto(0)
                    pump()
                    if scale == original_scale:
                        filename = f"{width}x{height}-tab{index}.png"
                        capture_window(app, root / filename)
                        screenshots.append(filename)
        assert not errors, errors
        checks.append("no Tk callback errors")
        report = {"ok": True, "checks": checks, "screenshots": screenshots, "callback_errors": errors}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    "ok": True,
                    "report": str(root / "report.json"),
                    "checks": len(checks),
                    "screenshots": len(screenshots),
                },
                ensure_ascii=True,
            ),
            flush=True,
        )
        return 0
    except Exception as exc:
        capture_window(app, root / "failure.png")
        report = {"ok": False, "checks": checks, "error": repr(exc), "callback_errors": errors}
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"ok": False, "report": str(root / "report.json"), "checks": len(checks), "error": repr(exc)},
                ensure_ascii=True,
            ),
            flush=True,
        )
        raise
    finally:
        app.destroy()


if __name__ == "__main__":
    raise SystemExit(main())
