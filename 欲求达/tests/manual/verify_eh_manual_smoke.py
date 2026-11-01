"""Exercise the EH manual form with generated credentials in an isolated runtime."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import patch


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    root = repo / "data/software_app/verification" / f"eh-manual-ui-{time.time_ns()}"
    root.mkdir(parents=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    import tkinter as tk
    from tkinter import ttk
    from software_app.ui.tk_app import SoftwareDesktop
    from software_app.core.browser_cookie_capture import cookie_capture_spec
    from software_app.core.cookie_store import load_cookie_records

    app = SoftwareDesktop()
    checks, errors = [], []
    app.report_callback_exception = lambda *args: errors.append(str(args[1]))

    def children(widget):
        for child in widget.winfo_children():
            yield child
            yield from children(child)

    try:
        for platform in ("E-Hentai 表站", "ExHentai 里站"):
            app.cookie_capture_platform_var.set(platform)
            app._cookie_capture_selection_changed()
            app.update()
            assert not app.cookie_capture_button.winfo_manager()
            assert app.eh_manual_button.winfo_manager() == "grid"
            assert not any(
                hasattr(app, name)
                for name in ("eh_plain_open_button", "eh_plain_capture_button", "eh_inner_open_button")
            )
            checks.append(platform + " manual controls only")
            app._edit_eh_cookie()
            app.update()
            dialog = next(w for w in app.winfo_children() if isinstance(w, tk.Toplevel))
            entries = [w for w in children(dialog) if isinstance(w, ttk.Entry)]
            assert len(entries) == len(cookie_capture_spec(platform).required_names)
            for entry in entries:
                assert entry.cget("show")
                entry.insert(0, "generated-test-value")
            with patch(
                "software_app.core.ehentai_cookie._validate_eh_session",
                side_effect=AssertionError("unexpected network"),
            ):
                next(w for w in children(dialog) if isinstance(w, ttk.Button)).invoke()
            app.update()
            records = load_cookie_records(cookie_capture_spec(platform).destination, strict=True)
            assert len(records) == len(entries)
            checks.append(platform + " masked fields and offline save")
            before = cookie_capture_spec(platform).destination.read_bytes()
            app._finish_eh_validation((platform, "simulated timeout"), error=True)
            assert cookie_capture_spec(platform).destination.read_bytes() == before
            checks.append(platform + " failed check preserves save")
        app.cookie_capture_platform_var.set("Pixiv")
        app._cookie_capture_selection_changed()
        assert app.cookie_capture_button.winfo_manager() == "grid"
        assert not app.eh_manual_button.winfo_manager()
        checks.append("other platform capture retained")
        assert not errors, errors
    finally:
        app.manager.shutdown(timeout=2)
        app.destroy()
    result = {"ok": True, "checks": checks, "callback_errors": errors}
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))


if __name__ == "__main__":
    main()
