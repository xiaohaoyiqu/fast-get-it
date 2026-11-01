"""Exercise platform settings and feedback using real Tk widgets and isolated data."""

from __future__ import annotations

import argparse
import json
import os
import sys
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

    import requests

    from software_app.ui.tk_app import SoftwareDesktop

    checks, errors = [], []
    app = None

    def check(name, ok):
        if not ok:
            raise AssertionError(name)
        checks.append(name)

    def pump():
        app.after(250, app.quit)
        app.mainloop()

    try:
        with (
            patch("requests.sessions.Session.request", side_effect=RuntimeError("offline UI verification")),
            patch.multiple(
                messagebox,
                showerror=lambda *a, **k: errors.append(str(a)),
                showwarning=lambda *a, **k: errors.append(str(a)),
                showinfo=lambda *a, **k: None,
            ),
        ):
            app = SoftwareDesktop()
            app.withdraw()
            app.report_callback_exception = lambda _kind, value, _trace: errors.append(str(value))
            pump()
            for adapter in app.adapters:
                app._select_module(adapter.module_id)
                for scope in app.content_scope_combo.cget("values"):
                    app.content_scope_var.set(scope)
                    app._content_scope_changed()
                    check(f"{adapter.module_id}:{scope}", app.module_var.get() == adapter.module_id)
            app.proxy_var.set("http://127.0.0.1:12345")
            for platform, module_id, method, button, variable in (
                ("Pixiv", "pixiv", "_check_pixiv_connection", "pixiv_connection_button", "pixiv_connection_status_var"),
                ("FANBOX", "pixiv", "_check_fanbox_status", "fanbox_check_button", "fanbox_status_var"),
                ("JMComic", "jmcomic", "_check_jm_connection", "jm_connection_button", "jm_connection_status_var"),
            ):
                app._select_module(module_id)
                adapter = app.manager.get_adapter(module_id)
                probe = "fanbox_supporting_status" if platform == "FANBOX" else "connection_status"
                for failed in (False, True):
                    with patch.object(
                        adapter,
                        probe,
                        return_value={"message": f"{platform} 可读取"},
                        side_effect=requests.Timeout("Cookie request timeout") if failed else None,
                    ):
                        getattr(app, method)()
                        for _ in range(6):
                            pump()
                            if getattr(app, button).instate(["!disabled"]):
                                break
                    message = getattr(app, variable).get()
                    check(f"{platform}:restored:{failed}", getattr(app, button).instate(["!disabled"]))
                    check(f"{platform}:unsaved_warning:{failed}", "尚未保存" in message)
                    check(f"{platform}:duration:{failed}", "耗时" in message)
                    if failed:
                        check(f"{platform}:network_not_login", "超时" in message and "失效" not in message)
            app.cookie_capture_platform_var.set("Pixiv")
            app._cookie_capture_selection_changed()
            current = app.cookie_capture_status_var.get()
            app._finish_eh_validation(("ExHentai 里站", "模拟验证失败"), error=True)
            app._finish_cookie_file_import(("Instagram", "模拟格式错误"), error=True)
            app._finish_header_import(("HentaiPaw", args.root / "export.json", "模拟格式错误"), error=True)
            check("late_cookie_results_keep_current_platform", app.cookie_capture_status_var.get() == current)
            logs = app.storage.recent_logs()
            check("operations_persisted", len(logs) >= 15)
            check(
                "platforms_in_logs",
                {"pixiv", "jmcomic", "ehentai", "instagram", "hentaipaw"}.issubset({r["module_id"] for r in logs}),
            )
            check("no_callback_errors_or_dialogs", not errors)
    finally:
        if app is not None:
            app.destroy()
        result = {"checks": checks, "errors": errors}
        (args.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": len(checks), "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
