"""Capture repeatable, privacy-safe screenshots of the desktop platform views."""

from __future__ import annotations

import json
import platform
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageGrab, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from software_app.ui.tk_app import SoftwareDesktop  # noqa: E402


OUTPUT_DIR = PROJECT_ROOT / "data" / "software_app" / "test_runs" / "ui_platform_validation"
PLATFORMS = ("twitter", "jmcomic", "pixiv", "google_image", "website", "bluesky", "instagram", "ehentai")
SIZES = ((1080, 680), (1240, 780), (1500, 860))


def capture_window(app: SoftwareDesktop, path: Path) -> tuple[int, int]:
    app.deiconify()
    app.attributes("-topmost", True)
    app.lift()
    app.update_idletasks()
    app.update()
    width = app.winfo_width()
    height = app.winfo_height()
    if platform.system() == "Windows":
        import ctypes

        class Rect(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        user32 = ctypes.windll.user32
        window = user32.GetParent(app.winfo_id()) or app.winfo_id()
        user32.BringWindowToTop(window)
        user32.SetForegroundWindow(window)
        app.update()
        rect = Rect()
        if user32.GetWindowRect(window, ctypes.byref(rect)):
            ImageGrab.grab((rect.left, rect.top, rect.right, rect.bottom), all_screens=True).save(path)
            app.attributes("-topmost", False)
            return width, height
    left = app.winfo_rootx()
    top = app.winfo_rooty()
    ImageGrab.grab((left, top, left + width, top + height), all_screens=True).save(path)
    app.attributes("-topmost", False)
    return width, height


def widget_inside_window(app: SoftwareDesktop, widget) -> bool:
    return (
        widget.winfo_ismapped()
        and widget.winfo_rootx() >= app.winfo_rootx()
        and widget.winfo_rooty() >= app.winfo_rooty()
        and widget.winfo_rootx() + widget.winfo_width() <= app.winfo_rootx() + app.winfo_width()
        and widget.winfo_rooty() + widget.winfo_height() <= app.winfo_rooty() + app.winfo_height()
    )


def contact_sheet(paths: list[Path], destination: Path, title: str) -> None:
    thumb_size = (560, 360)
    margin = 20
    label_height = 30
    columns = 2
    rows = (len(paths) + columns - 1) // columns
    sheet = Image.new("RGB", (
        margin + columns * (thumb_size[0] + margin),
        60 + rows * (thumb_size[1] + label_height + margin),
    ), "#eef2f6")
    draw = ImageDraw.Draw(sheet)
    draw.text((margin, 18), title, fill="#172033")
    for index, path in enumerate(paths):
        with Image.open(path) as opened:
            thumb = ImageOps.contain(opened.convert("RGB"), thumb_size)
        column = index % columns
        row = index // columns
        x = margin + column * (thumb_size[0] + margin)
        y = 55 + row * (thumb_size[1] + label_height + margin)
        sheet.paste(thumb, (x, y))
        draw.text((x, y + thumb_size[1] + 5), path.stem, fill="#172033")
    sheet.save(destination)


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    app = SoftwareDesktop()
    app.deiconify()
    app.lift()
    app.attributes("-topmost", True)
    app.after(300, lambda: app.attributes("-topmost", False))
    app.update()

    # Prevent cached personal rows from appearing in screenshots.
    app.following_rows = []
    app.pixiv_following_rows = []
    app.platform_candidate_rows = {
        "ehentai": [{
            "target": "https://e-hentai.org/g/1234567/abcdef1234/",
            "url": "https://e-hentai.org/g/1234567/abcdef1234/",
            "title": "界面验证画廊（不联网）",
            "input_kind": "gallery",
            "source": "离线界面验证",
            "downloadable": True,
        }],
    }
    app.current_candidate_rows = []
    app.target_browser_rows = []
    # Never expose locally saved account/session settings in visual-test artifacts.
    app.proxy_var.set("")
    app.jm_domain_var.set("https://example.invalid")
    app.jm_user_agent_var.set("Mozilla/5.0 (visual smoke test)")
    app.jm_favorite_username_var.set("")
    app.jm_favorite_folder_var.set("")
    app.jm_novel_favorite_folder_var.set("")
    app.eh_download_torrent_var.set(False)
    app.eh_bt_download_enabled_var.set(False)

    platform_paths: list[Path] = []
    similarity_paths: list[Path] = []
    settings_paths: list[Path] = []
    metrics: dict[str, object] = {
        "platforms": {}, "similarity": {}, "eh_login_settings": {}, "eh_download_settings": {},
        "jm_login_settings": {},
    }
    try:
        app.geometry("1240x780+20+20")
        app.update()
        for module_id in PLATFORMS:
            app._select_module(module_id)
            if module_id == "google_image":
                app.notebook.select(app.google_search_tab)
            else:
                app.notebook.select(app.following_tab)
                app._refresh_platform_tabs()
                if module_id == "ehentai" and app.following_tree.get_children():
                    app.following_tree.selection_set(app.following_tree.get_children()[0])
                    app._candidate_selection_changed()
            app.update()
            path = OUTPUT_DIR / f"platform_{module_id}_1240x780.png"
            actual = capture_window(app, path)
            platform_paths.append(path)
            metrics["platforms"][module_id] = {
                "actual_size": actual,
                "notebook_tab": app.notebook.tab(app.notebook.select(), "text"),
                "start_button_disabled": "disabled" in app.start_button.state(),
                "candidate_tree_height": app.following_tree.winfo_height(),
            }
            if module_id == "ehentai":
                metrics["platforms"][module_id].update({
                    "favorite_button_inside": widget_inside_window(app, app.eh_favorite_button),
                    "unfavorite_button_inside": widget_inside_window(app, app.eh_unfavorite_button),
                    "favorite_button_enabled": "disabled" not in app.eh_favorite_button.state(),
                    "unfavorite_button_enabled": "disabled" not in app.eh_unfavorite_button.state(),
                })

        app.notebook.select(app.settings_tab)
        app.geometry("1240x780+20+20")
        app.update()
        scroll_box = app.settings_canvas.bbox("all") or (0, 0, 1, 1)
        content_height = max(1, scroll_box[3] - scroll_box[1])
        target_y = (
            app.settings_canvas.canvasy(0)
            + app.cookie_settings_frame.winfo_rooty()
            - app.settings_canvas.winfo_rooty()
            - 20
        )
        app.settings_canvas.yview_moveto(max(0.0, min(1.0, (target_y - scroll_box[1]) / content_height)))
        for label, slug in (("E-Hentai 表站", "table"), ("ExHentai 里站", "inner")):
            app.cookie_capture_platform_var.set(label)
            app._cookie_capture_selection_changed()
            app.update()
            path = OUTPUT_DIR / f"settings_eh_{slug}_1240x780.png"
            actual = capture_window(app, path)
            settings_paths.append(path)
            metrics["eh_login_settings"][slug] = {
                "actual_size": actual,
                "plain_open_visible": bool(app.eh_plain_open_button.winfo_ismapped()),
                "plain_capture_visible": bool(app.eh_plain_capture_button.winfo_ismapped()),
                "automatic_button_visible": bool(app.cookie_capture_button.winfo_ismapped()),
                "plain_open_inside": widget_inside_window(app, app.eh_plain_open_button),
                "plain_capture_inside": widget_inside_window(app, app.eh_plain_capture_button),
                "header_convert_visible": bool(app.header_convert_button.winfo_ismapped()),
                "header_convert_inside": widget_inside_window(app, app.header_convert_button),
                "inner_step_visible": bool(app.eh_inner_open_button.winfo_ismapped()),
                "inner_step_inside": (
                    widget_inside_window(app, app.eh_inner_open_button)
                    if app.eh_inner_open_button.winfo_ismapped() else True
                ),
            }

        scroll_box = app.settings_canvas.bbox("all") or (0, 0, 1, 1)
        content_height = max(1, scroll_box[3] - scroll_box[1])
        target_y = (
            app.settings_canvas.canvasy(0)
            + app.eh_download_settings_frame.winfo_rooty()
            - app.settings_canvas.winfo_rooty()
            - 20
        )
        app.settings_canvas.yview_moveto(max(0.0, min(1.0, (target_y - scroll_box[1]) / content_height)))
        app.update()
        path = OUTPUT_DIR / "settings_eh_download_1240x780.png"
        actual = capture_window(app, path)
        settings_paths.append(path)
        metrics["eh_download_settings"] = {
            "actual_size": actual,
            "torrent_toggle_visible": bool(app.eh_torrent_checkbutton.winfo_ismapped()),
            "torrent_toggle_inside": widget_inside_window(app, app.eh_torrent_checkbutton),
            "torrent_toggle_default_off": not bool(app.eh_download_torrent_var.get()),
            "bt_toggle_visible": bool(app.eh_bt_checkbutton.winfo_ismapped()),
            "bt_toggle_inside": widget_inside_window(app, app.eh_bt_checkbutton),
            "bt_toggle_default_off": not bool(app.eh_bt_download_enabled_var.get()),
            "aria2_check_inside": widget_inside_window(app, app.aria2_check_button),
            "aria2_install_inside": widget_inside_window(app, app.aria2_install_button),
        }

        scroll_box = app.settings_canvas.bbox("all") or (0, 0, 1, 1)
        content_height = max(1, scroll_box[3] - scroll_box[1])
        target_y = (
            app.settings_canvas.canvasy(0)
            + app.jm_login_frame.winfo_rooty()
            - app.settings_canvas.winfo_rooty()
            - 20
        )
        app.settings_canvas.yview_moveto(max(0.0, min(1.0, (target_y - scroll_box[1]) / content_height)))
        app.update()
        path = OUTPUT_DIR / "settings_jm_1240x780.png"
        actual = capture_window(app, path)
        settings_paths.append(path)
        metrics["jm_login_settings"] = {
            "actual_size": actual,
            "header_convert_visible": bool(app.jm_header_convert_button.winfo_ismapped()),
            "header_convert_inside": widget_inside_window(app, app.jm_header_convert_button),
            "plain_open_inside": widget_inside_window(app, app.jm_plain_open_button),
            "plain_capture_inside": widget_inside_window(app, app.jm_plain_capture_button),
        }

        app._select_module("google_image")
        app.notebook.select(app.google_search_tab)
        source = PROJECT_ROOT / "图片" / "1.png"
        app.google_image_paths = [source]
        app.google_image_var.set(str(source))
        app._render_google_source_preview(source)
        app._show_google_results([
            {
                "source_image": source.name,
                "url": "https://commons.wikimedia.org/wiki/File:Example.jpg",
                "availability": "available",
            },
            {
                "source_image": source.name,
                "url": "https://www.pixiv.net/artworks/123456",
                "final_url": "https://www.pixiv.net/artworks/123456",
                "availability": "unknown",
            },
            {
                "source_image": source.name,
                "url": "https://x.com/example/status/1234567890",
                "availability": "restricted",
            },
            {
                "source_image": source.name,
                "url": "https://example.com/expired-image-source",
                "availability": "missing",
            },
        ])
        for width, height in SIZES:
            app.geometry(f"{width}x{height}+20+20")
            app.update()
            app._render_google_source_preview(source)
            app.update()
            path = OUTPUT_DIR / f"similarity_{width}x{height}.png"
            actual = capture_window(app, path)
            similarity_paths.append(path)
            metrics["similarity"][f"{width}x{height}"] = {
                "actual_size": actual,
                "preview_size": (app.google_source_preview.winfo_width(), app.google_source_preview.winfo_height()),
                "result_tree_size": (app.google_result_tree.winfo_width(), app.google_result_tree.winfo_height()),
                "result_rows": len(app.google_result_tree.get_children()),
                "search_button_visible": bool(app.google_search_button.winfo_ismapped()),
                "crawl_button_visible": bool(app.google_crawl_button.winfo_ismapped()),
                "more_button_visible": bool(app.google_more_button.winfo_ismapped()),
                "search_button_inside": widget_inside_window(app, app.google_search_button),
                "crawl_button_inside": widget_inside_window(app, app.google_crawl_button),
                "more_button_inside": widget_inside_window(app, app.google_more_button),
                "preview_inside": widget_inside_window(app, app.google_source_preview),
                "target_entry_inside": widget_inside_window(app, app.target_entry),
                "output_browse_inside": widget_inside_window(app, app.output_browse_button),
                "types_entry_inside": widget_inside_window(app, app.types_entry),
            }
    finally:
        app.destroy()

    (OUTPUT_DIR / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    contact_sheet(platform_paths, OUTPUT_DIR / "platforms_contact_sheet.png", "Platform views at 1240 x 780")
    contact_sheet(similarity_paths, OUTPUT_DIR / "similarity_contact_sheet.png", "Similarity search responsive views")
    contact_sheet(settings_paths, OUTPUT_DIR / "login_settings_contact_sheet.png", "Header import settings at 1240 x 780")
    print(json.dumps(metrics, ensure_ascii=False))
    print(OUTPUT_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
