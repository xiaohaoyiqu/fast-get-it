"""Native preview, exact selection cleanup, and aria2 settings with generated data."""

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
    root = repo / "data/software_app/verification" / f"transfer-cache-ui-{time.time_ns()}"
    root.mkdir(parents=True)
    os.environ["YUQIUDA_HOME"] = str(root / "runtime")
    os.environ["YUQIUDA_OUTPUT_DIR"] = str(root / "media")
    from software_app.ui.tk_app import SoftwareDesktop
    from software_app.core.transfer_workspace import owned_workspace
    from software_app.core.aria2_bt import aria2_limits_from_options, Aria2Limits

    app = SoftwareDesktop()
    callbacks, checks = [], []
    app.report_callback_exception = lambda *args: callbacks.append(str(args[1]))
    result = {"ok": False}

    def settle(dialog):
        deadline = time.monotonic() + 8
        while dialog._busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()
        assert not dialog._busy, dialog.status.get()

    try:
        media = root / "media"
        original = media / "video.mp4"
        original.write_bytes(b"original complete video")
        with owned_workspace(original) as http:
            (http / "payload.part").write_bytes(b"partial")
        with owned_workspace(media / "other.mp4", kind="hls") as hls:
            (hls / "r000000.ts").write_bytes(b"generated segment")
        with owned_workspace(media / "unknown.mp4") as unknown:
            (unknown / "note.txt").write_bytes(b"user note")
        app.transfer_cache_button.invoke()
        app.update()
        dialog = app._transfer_cache_dialog
        dialog._preview()
        settle(dialog)
        assert len(dialog.tree.get_children()) == 3
        assert sum(not entry.error for entry in dialog._plan.entries) == 2
        assert http.exists() and hls.exists() and unknown.exists()
        checks.append("native preview lists owned caches and blocks unknown contents without writing")
        index = next(index for index, entry in enumerate(dialog._plan.entries) if entry.path == http)
        dialog.tree.selection_set(str(index))
        app.update()
        with patch("software_app.ui.transfer_cache.messagebox.askyesno", return_value=False):
            dialog.clean_button.invoke()
        assert http.exists()
        checks.append("declining confirmation preserves all cache files")
        with patch("software_app.ui.transfer_cache.messagebox.askyesno", return_value=True):
            dialog.clean_button.invoke()
            settle(dialog)
        assert not http.exists() and hls.exists() and unknown.exists()
        assert original.read_bytes() == b"original complete video"
        checks.append("confirmed native selection cleans only selected cache and preserves final media")
        assert dialog._plan is None
        assert dialog.clean_button.instate(["disabled"])
        checks.append("completed cleanup invalidates preview and disables repeated deletion")
        dialog._close()
        app.aria2_limit_enabled_var.set(True)
        app.aria2_download_kib_var.set("256")
        app.aria2_upload_kib_var.set("32")
        with patch("software_app.ui.settings_tab.messagebox"):
            app._save_settings()
        saved = app.storage.get_settings()
        assert aria2_limits_from_options(saved) == Aria2Limits(True, 256, 32)
        checks.append("native aria2 download and upload limits persist independently")
        app.aria2_upload_kib_var.set("0")
        with patch("software_app.ui.settings_tab.messagebox") as messages:
            app._save_settings()
        assert messages.showwarning.called
        assert app.storage.get_settings() == saved
        checks.append("invalid native aria2 value leaves all saved settings unchanged")
        assert not callbacks, callbacks
        result.update(ok=True)
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
    finally:
        app.manager.shutdown(timeout=2)
        app.destroy()
    result.update(checks=checks, callback_errors=callbacks)
    (root / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "report.json"), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
