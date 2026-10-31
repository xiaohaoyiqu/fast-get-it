"""Exercise native integrity actions with generated files and an isolated index."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from unittest.mock import patch


def main():
    repo = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo))
    from software_app.core import library_integrity as integrity
    from software_app.core.models import FileRecord
    from software_app.core.storage import AppStorage
    from software_app.core.task_manager import TaskManager
    from software_app.ui.library_tools import LibraryToolsDialog

    root = repo / "data/software_app/verification" / f"library-integrity-ui-{time.time_ns()}"
    media = root / "media"
    media.mkdir(parents=True)
    first, second = media / "first.bin", media / "second.bin"
    first.write_bytes(b"generated-one")
    second.write_bytes(b"keep")
    storage = AppStorage(root / "index.sqlite")
    for path in (first, second):
        storage.upsert_file(FileRecord(path.resolve(), "pixiv", media_type="image"))
    manager = TaskManager(storage)
    app = tk.Tk()
    app.title("欲求达：隔离清单验收")
    app.geometry("900x650")
    app.manager, app.storage = manager, storage
    app.library_search_var = tk.StringVar(value="")
    app.output_dir_var = tk.StringVar(value=str(media))
    callbacks, checks = [], []
    app.report_callback_exception = lambda *_args: callbacks.append(str(_args[1]))
    dialog = LibraryToolsDialog(app)
    app.update()
    manifest, report = root / "baseline.json", root / "report.json"
    result = {"ok": False}

    def settle():
        deadline = time.monotonic() + 15
        while dialog._busy and time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        app.update()
        assert not dialog._busy, dialog.status.get()

    try:
        before = [tuple(row) for row in storage.list_files()]
        with (
            patch("software_app.ui.library_tools.filedialog.askdirectory", return_value=str(media)),
            patch("software_app.ui.library_tools.filedialog.asksaveasfilename", return_value=str(manifest)),
        ):
            dialog.manifest_button.invoke()
            assert dialog._busy and dialog.verify_button.instate(["disabled"])
            assert dialog.cancel_button.instate(["!disabled"])
            settle()
        baseline = manifest.read_bytes()
        assert len(json.loads(baseline)["entries"]) == 2
        checks.append("native export creates two local baselines and controls reflect worker activity")
        first.write_bytes(b"generated-two")
        second.unlink()
        with (
            patch("software_app.ui.library_tools.filedialog.askopenfilename", return_value=str(manifest)),
            patch("software_app.ui.library_tools.filedialog.askdirectory", return_value=str(media)),
            patch("software_app.ui.library_tools.filedialog.asksaveasfilename", return_value=str(report)),
        ):
            dialog.verify_button.invoke()
            settle()
        assert json.loads(report.read_text(encoding="utf-8"))["counts"] == {"changed": 1, "missing": 1}
        assert "变化：1" in dialog.details.get("1.0", "end")
        assert manifest.read_bytes() == baseline and first.read_bytes() == b"generated-two"
        assert [tuple(row) for row in storage.list_files()] == before
        checks.append("native verification reports same-size changes and missing files without changing index or media")
        with (
            patch("software_app.ui.library_tools.filedialog.askdirectory", return_value=str(media)),
            patch("software_app.ui.library_tools.filedialog.asksaveasfilename", return_value=str(manifest)),
            patch("software_app.ui.library_tools.messagebox.showerror") as error,
        ):
            dialog.manifest_button.invoke()
            settle()
            assert error.called
        assert manifest.read_bytes() == baseline
        checks.append("native existing-file error preserves previous manifest and restores buttons")
        entered = threading.Event()
        hash_file = integrity._hash
        cancelled = root / "cancelled.json"

        def delayed(path, event):
            entered.set()
            if not event.wait(5):
                raise TimeoutError("expected UI cancellation")
            return hash_file(path, event)

        with (
            patch.object(integrity, "_hash", side_effect=delayed),
            patch("software_app.ui.library_tools.filedialog.askdirectory", return_value=str(media)),
            patch("software_app.ui.library_tools.filedialog.asksaveasfilename", return_value=str(cancelled)),
        ):
            dialog.manifest_button.invoke()
            assert entered.wait(3)
            dialog.cancel_button.invoke()
            settle()
        assert not cancelled.exists() and not list(root.glob(".integrity-*.tmp"))
        assert dialog.manifest_button.instate(["!disabled"])
        checks.append("native stop cancels in-flight hashing without publishing a partial manifest")
        assert not callbacks, callbacks
        result.update(ok=True, baseline_sha256=hashlib.sha256(baseline).hexdigest())
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}")
    finally:
        dialog._close()
        manager.shutdown(3)
        app.destroy()
    result.update(checks=checks, callback_errors=callbacks)
    (root / "smoke-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(root / "smoke-report.json"), **result}, ensure_ascii=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
