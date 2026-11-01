"""Offline desktop state checks for platform changes and late search responses."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    args.root = args.root.resolve()
    args.root.mkdir(parents=True, exist_ok=True)
    os.environ["YUQIUDA_HOME"] = str(args.root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(args.root / "media")
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    import tkinter as tk
    from tkinter import messagebox
    from software_app.ui.tk_app import SoftwareDesktop

    app = None
    errors, dialogs, checks = [], [], []

    def check(label, condition):
        if not condition:
            raise AssertionError(label)
        checks.append(label)

    def pump():
        app.after(250, app.quit)
        app.mainloop()

    try:
        with (
            patch.object(
                tk.Tk, "report_callback_exception", side_effect=lambda _kind, value, _trace: errors.append(str(value))
            ),
            patch(
                "requests.sessions.Session.request",
                side_effect=RuntimeError("offline UI check"),
            ),
            patch.multiple(
                messagebox,
                showerror=lambda *a, **k: dialogs.append(str(a)),
                showwarning=lambda *a, **k: dialogs.append(str(a)),
                showinfo=lambda *a, **k: None,
            ),
        ):
            app = SoftwareDesktop()
            app.withdraw()
            pump()
            for adapter in app.adapters:
                app._select_module(adapter.module_id)
                enabled = (
                    "download" in adapter.info.capabilities
                    and adapter.info.stage != "planned"
                    and adapter.module_id != "google_image"
                )
                check(adapter.module_id + "_download_button", app.start_button.instate(["!disabled"]) == enabled)

            app._select_module("instagram")
            app.content_scope_var.set("关注账号（需登录，规划）")
            app._content_scope_changed()
            check("planned_scope_disabled", app.start_button.instate(["disabled"]))
            app.content_scope_var.set("账号 / 用户名")
            app._content_scope_changed()
            check("available_scope_enabled_again", app.start_button.instate(["!disabled"]))

            app._select_module("ehentai")
            app.content_scope_var.set("表站关键词")
            app._content_scope_changed()
            check("eh_search_refresh_enabled", app.candidate_refresh_button.instate(["!disabled"]))
            app._select_module("hentaipaw")
            app.content_scope_var.set("关键词搜索")
            app._content_scope_changed()
            check("hentaipaw_search_refresh_enabled", app.candidate_refresh_button.instate(["!disabled"]))
            adapter = app.manager.get_adapter("hentaipaw")
            jobs = []

            def deferred(*, target, name, daemon):
                return SimpleNamespace(start=lambda: jobs.append(target))

            def search(query, **kwargs):
                if query == "fail":
                    raise ValueError("delayed old failure")
                return [{"id": "123", "title": query, "url": "https://zh.hentaipaw.com/articles/123"}]

            with (
                patch("software_app.ui.tk_app.threading.Thread", side_effect=deferred),
                patch.object(adapter, "search_targets", side_effect=search),
            ):
                app.platform_search_var.set("first")
                app._search_platform_targets()
                app._refresh_target_browser()
                app._refresh_platform_tabs()
                check("search_button_stays_disabled_while_busy", app.platform_search_button.instate(["disabled"]))
                check("candidate_refresh_stays_disabled_while_busy", app.candidate_refresh_button.instate(["disabled"]))
                app.platform_search_var.set("second")
                app._search_platform_targets()
                jobs[1]()
                pump()
                check("latest_result_displayed", app.target_browser_rows[0]["name"] == "second")
                status = app.status_var.get()
                jobs[0]()
                pump()
                check(
                    "older_result_ignored",
                    app.target_browser_rows[0]["name"] == "second" and app.status_var.get() == status,
                )
                check(
                    "search_controls_restored",
                    app.platform_search_button.instate(["!disabled"])
                    and app.candidate_refresh_button.instate(["!disabled"]),
                )
                app.platform_search_var.set("fail")
                app._search_platform_targets()
                app._select_module("pixiv")
                status = app.status_var.get()
                jobs[2]()
                pump()
                check("old_platform_error_ignored", app.status_var.get() == status and app.module_var.get() == "pixiv")
            check("no_error_dialogs", not dialogs)
            check("no_tk_callback_errors", not errors)
    finally:
        if app is not None:
            app.destroy()
    result = {"checks": checks, "callback_errors": errors, "dialogs": dialogs}
    (args.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
