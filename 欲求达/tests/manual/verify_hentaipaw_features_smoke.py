"""Offline native UI checks for HentaiPaw categories, favorites and access status."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
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
    from tkinter import messagebox
    from software_app.ui.tk_app import SoftwareDesktop

    app = None
    errors, checks = [], []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)

    def wait_for(predicate):
        deadline = time.monotonic() + 8

        def tick():
            if predicate() or time.monotonic() >= deadline:
                app.quit()
            else:
                app.after(30, tick)

        app.after(30, tick)
        app.mainloop()
        assert predicate(), "UI operation did not complete"

    try:
        with (
            patch.multiple(
                messagebox,
                showinfo=lambda *a, **k: None,
                showerror=lambda *a, **k: errors.append(str(a)),
                showwarning=lambda *a, **k: errors.append(str(a)),
            ),
            patch(
                "requests.sessions.Session.request",
                side_effect=RuntimeError("offline features check"),
            ),
        ):
            app = SoftwareDesktop()
            app.withdraw()
            app.report_callback_exception = lambda _kind, value, _trace: errors.append(str(value))
            app._select_module("hentaipaw")
            adapter = app.manager.get_adapter("hentaipaw")
            category = {
                "id": "12",
                "url": "https://zh.hentaipaw.com/tags/12",
                "title": "Example category",
                "input_kind": "category",
                "downloadable": False,
                "read_only_reason": "展开分类查看作品",
            }
            work = {
                "id": "123",
                "url": "https://zh.hentaipaw.com/articles/123",
                "title": "Example work",
                "input_kind": "article",
            }
            app.content_scope_var.set("标签分类")
            app._content_scope_changed()
            app.platform_search_var.set("")
            with patch.object(adapter, "search_targets", side_effect=[[category], [work]]) as search:
                app._search_platform_targets()
                wait_for(lambda: bool(app.current_candidate_rows))
                app.following_tree.selection_set("0")
                app._candidate_selection_changed()
                check("category_download_disabled", app.candidate_download_button.instate(["disabled"]))
                check("category_expand_action", app.candidate_preview_button.cget("text") == "展开分类")
                check("category_page_actions", bool(app.hentaipaw_next_button.winfo_manager()))
                app._preview_selected_platform_candidate()
                wait_for(
                    lambda: (
                        bool(app.current_candidate_rows) and app.current_candidate_rows[0].get("target") == work["url"]
                    )
                )
                check("category_expands_into_works", search.call_args.args[0] == category["url"])
            app.following_tree.selection_set("0")
            app._candidate_selection_changed()
            check("work_download_enabled", app.candidate_download_button.instate(["!disabled"]))
            app.eh_favorite_button.invoke()
            check("add_software_favorite", len(adapter.favorites.rows()) == 1)
            export = args.root / "favorites-export.json"
            with patch("software_app.ui.hentaipaw.filedialog.asksaveasfilename", return_value=str(export)):
                app._hentaipaw_export_favorites()
            check(
                "export_browser_compatible_shape", "attributes" in json.loads(export.read_text(encoding="utf-8"))["123"]
            )
            source = args.root / "browser-favorites.json"
            source.write_text(
                json.dumps({"456": {"attributes": {"title": "Imported work", "language_id": 3}}}), encoding="utf-8"
            )
            with patch("software_app.ui.hentaipaw.filedialog.askopenfilename", return_value=str(source)):
                app._hentaipaw_import_favorites()
            check("import_merges_browser_favorites", len(adapter.favorites.rows()) == 2)
            check("software_favorites_scope", app.content_scope_var.get() == "软件收藏")
            app.following_tree.selection_set("0")
            app._candidate_selection_changed()
            app.eh_unfavorite_button.invoke()
            check("remove_software_favorite", len(adapter.favorites.rows()) == 1)
            app.content_scope_var.set("周榜")
            app._content_scope_changed()
            app.platform_search_var.set("")
            with patch.object(adapter, "search_targets", return_value=[work]) as search:
                app._search_platform_targets()
                wait_for(lambda: app._platform_search_context is None)
                check("ranking_empty_query_works", search.call_args.kwargs["options"]["search_mode"] == "周榜")
            app.cookie_capture_platform_var.set("HentaiPaw")
            app._cookie_capture_selection_changed()
            check("access_check_button_visible", bool(app.hentaipaw_access_button.winfo_manager()))
            with patch.object(
                adapter,
                "access_status",
                return_value={
                    "accessible": True,
                    "login_verified": False,
                    "message": "公共页面可访问；账号登录状态未验证",
                },
            ):
                app.hentaipaw_access_button.invoke()
                wait_for(lambda: "账号登录状态未验证" in app.cookie_capture_status_var.get())
            check("public_access_not_claimed_as_login", "账号登录状态未验证" in app.cookie_capture_status_var.get())
            check("no_tk_or_dialog_errors", not errors)
    finally:
        if app is not None:
            app.destroy()
    result = {"checks": checks, "errors": errors}
    (args.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
