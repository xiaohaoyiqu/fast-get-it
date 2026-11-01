"""Offline HentaiPaw desktop checks using isolated runtime data."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time
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
    from PIL import Image
    from software_app.core.models import TargetPreview
    from software_app.ui.tk_app import SoftwareDesktop

    errors = []
    checks = []
    image = io.BytesIO()
    Image.new("RGB", (24, 32), "#5d99aa").save(image, format="PNG")
    app = None
    try:
        with (
            patch.object(
                tk.Tk, "report_callback_exception", side_effect=lambda _kind, value, _trace: errors.append(str(value))
            ),
            patch(
                "requests.sessions.Session.request",
                side_effect=RuntimeError("offline desktop check"),
            ),
        ):
            app = SoftwareDesktop()
            app.withdraw()
            app._select_module("hentaipaw")
            app.types_var.set("2 视频")
            options = app._current_target_options("123")
            assert options["types"] == "1", options
            checks.append("image_type_not_inherited_from_other_platform")
            app.target_var.set("https://zh.hentaipaw.com/viewer?articleId=123&page=2")
            app._preview_target()
            assert app.profile_url_var.get() == "https://zh.hentaipaw.com/articles/123"
            checks.append("reader_url_local_preview")
            preview = TargetPreview(
                "hentaipaw",
                "123",
                "https://zh.hentaipaw.com/articles/123",
                title="Example",
                metadata={
                    "target_id": "123",
                    "reader_verified": True,
                    "page_count": 3,
                    "thumbnail_url": "https://cdn.imagedeliveries.com/123/thumbnails/cover.webp",
                },
            )
            adapter = app.manager.get_adapter("hentaipaw")
            with patch.object(adapter, "fetch_preview_image_bytes", return_value=image.getvalue()):
                app._render_generic_preview(preview)
                app._apply_twitter_history([])
                assert "3 页" in app.profile_bio_var.get(), app.profile_bio_var.get()
                deadline = time.monotonic() + 8

                def wait_for_cover():
                    if app.profile_avatar_image is not None or time.monotonic() >= deadline:
                        app.quit()
                    else:
                        app.after(30, wait_for_cover)

                app.after(30, wait_for_cover)
                app.mainloop()
                assert app.profile_avatar_image is not None, "Cover not rendered"
            assert "3 页" in app.profile_bio_var.get(), app.profile_bio_var.get()
            checks.append("cover_and_verified_page_count")
            checks.append("twitter_history_does_not_replace_other_platform_preview")
            row = {"module_id": "hentaipaw", "id": "123", "title": "Example", "target": preview.normalized_target}
            with (
                patch.object(app.manager, "start_task", return_value=SimpleNamespace(task_id="fixture")) as start,
                patch.object(
                    app,
                    "_refresh_tasks",
                ),
                patch.object(app, "_follow_task_batch"),
            ):
                app._queue_download_rows([row])
                assert start.call_args.args[0:2] == ("hentaipaw", preview.normalized_target)
                assert start.call_args.args[3]["types"] == "1"
            checks.append("search_candidate_queue_options")
            app.cookie_capture_platform_var.set("HentaiPaw")
            app._cookie_capture_selection_changed()
            assert not app.cookie_capture_button.winfo_manager()
            assert app.cookie_import_button.winfo_manager()
            checks.append("cookie_import_without_unverified_auto_login")
            assert not errors, errors
            checks.append("no_tk_callback_errors")
    finally:
        if app is not None:
            app.destroy()
    result = {"checks": checks, "callback_errors": errors}
    (args.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
