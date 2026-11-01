"""HentaiPaw category navigation, local favorites and access checks."""

from __future__ import annotations

import threading
import time
from tkinter import filedialog, messagebox
from urllib.parse import parse_qs, urlencode, urlsplit

from software_app.core.security import safe_error_message
from software_app.crawlers.hentaipaw.catalog import BASE_URL, CATEGORY_SCOPES


class HentaiPawUiMixin:
    def _hentaipaw_show_favorites(self):
        self.content_scope_var.set("软件收藏")
        self._content_scope_changed()
        rows = self.manager.get_adapter("hentaipaw").favorites.rows(limit=100)
        self._show_platform_search_results("hentaipaw", "我的收藏", rows)
        self.status_var.set(f"已读取软件收藏 {len(rows)} 项")
        self._record_settings_operation("HentaiPaw", self.status_var.get())

    def _hentaipaw_import_favorites(self):
        messagebox.showinfo(
            "导入浏览器收藏",
            "在收藏所在的浏览器打开 HentaiPaw，进入开发者工具 → Application → Local Storage → 本站。\n"
            "复制 favorites 的完整值，保存成 JSON 文件后导入。Cookie 文件不包含这份收藏。\n"
            "导入会合并到软件收藏，不改写浏览器中的原收藏。",
            parent=self,
        )
        source = filedialog.askopenfilename(
            parent=self, title="导入 HentaiPaw favorites JSON", filetypes=[("JSON", "*.json")]
        )
        if not source:
            return
        try:
            count = self.manager.get_adapter("hentaipaw").favorites.import_file(source)
            self._hentaipaw_show_favorites()
            self.status_var.set(f"已合并 {count} 项浏览器收藏")
            self._record_settings_operation("HentaiPaw", f"HentaiPaw {self.status_var.get()}")
        except Exception as exc:
            self._record_settings_operation(
                "HentaiPaw", f"HentaiPaw 收藏导入失败：{safe_error_message(exc)}", error=True
            )
            messagebox.showerror("收藏未导入", safe_error_message(exc), parent=self)

    def _hentaipaw_export_favorites(self):
        destination = filedialog.asksaveasfilename(
            parent=self,
            title="导出 HentaiPaw 收藏",
            defaultextension=".json",
            initialfile="hentaipaw-favorites.json",
            filetypes=[("JSON", "*.json")],
        )
        if not destination:
            return
        try:
            count = self.manager.get_adapter("hentaipaw").favorites.export_file(destination)
            self.status_var.set(f"已导出 {count} 项软件收藏，可备份或手动合并到浏览器 favorites")
            self._record_settings_operation("HentaiPaw", f"HentaiPaw {self.status_var.get()}")
        except Exception as exc:
            self._record_settings_operation(
                "HentaiPaw", f"HentaiPaw 收藏导出失败：{safe_error_message(exc)}", error=True
            )
            messagebox.showerror("收藏未导出", safe_error_message(exc), parent=self)

    def _hentaipaw_set_favorite(self, desired):
        row = self._selected_platform_candidate()
        if not row or not row.get("downloadable", True):
            messagebox.showinfo("请选择作品", "先选择一部具体作品，再加入或移出软件收藏。", parent=self)
            return
        try:
            self.manager.get_adapter("hentaipaw").favorites.set(
                row["target"], row.get("name") or row["target"], desired
            )
            if self.content_scope_var.get() == "软件收藏":
                self._hentaipaw_show_favorites()
            self.status_var.set("已加入软件收藏" if desired else "已移出软件收藏")
            self._record_settings_operation("HentaiPaw", f"HentaiPaw {self.status_var.get()}：{row['target']}")
        except Exception as exc:
            self._record_settings_operation(
                "HentaiPaw", f"HentaiPaw 收藏修改失败：{safe_error_message(exc)}", error=True
            )
            messagebox.showerror("收藏未修改", safe_error_message(exc), parent=self)

    def _hentaipaw_open_category(self):
        row = self._selected_platform_candidate()
        if not row or row.get("input_kind") != "category":
            return
        group = urlsplit(row["target"]).path.split("/")[1]
        scope = next((name for name, value in CATEGORY_SCOPES.items() if value == group), "标签分类")
        self.content_scope_var.set(scope)
        self._content_scope_changed()
        self.platform_search_var.set(row["target"])
        self._search_platform_targets()

    def _hentaipaw_index_page(self, delta):
        group = CATEGORY_SCOPES.get(self.content_scope_var.get())
        if not group:
            return
        raw = urlsplit(self.platform_search_var.get())
        try:
            current = int(parse_qs(raw.query).get("page", ["1"])[0])
        except ValueError:
            current = 1
        page = max(1, min(9999, current + delta))
        self.platform_search_var.set(f"{BASE_URL}/{group}?" + urlencode({"page": page}))
        self._search_platform_targets()

    def _check_hentaipaw_access(self):
        request_id = getattr(self, "_hentaipaw_access_request_id", 0) + 1
        self._hentaipaw_access_request_id = request_id
        options = {"proxy_url": self.proxy_var.get().strip()}
        adapter = self.manager.get_adapter("hentaipaw")
        note = self._settings_check_note("HentaiPaw")
        self.hentaipaw_access_button.state(["disabled"])
        self.cookie_capture_status_var.set(f"正在检查站点访问与会话配置，不验证账号身份… {note}")
        self._record_settings_operation("HentaiPaw", f"开始检查 HentaiPaw 站点访问；{note}")
        started = time.monotonic()

        def worker():
            try:
                result = dict(adapter.access_status(options))
            except Exception as exc:
                result = {"accessible": False, "message": self._friendly_error(exc, "HentaiPaw 访问检查")}
            result["message"] = (
                f"{result.get('message') or '站点访问检查未完成'} · 耗时 {time.monotonic() - started:.1f} 秒。{note}"
            )
            self.ui_queue.put(("hentaipaw_access_result", (request_id, result)))

        try:
            threading.Thread(target=worker, daemon=True, name="hentaipaw-access-check").start()
        except RuntimeError as exc:
            self._finish_hentaipaw_access((request_id, {"message": safe_error_message(exc)}))

    def _finish_hentaipaw_access(self, payload):
        request_id, result = payload
        if request_id != getattr(self, "_hentaipaw_access_request_id", 0):
            return
        self.hentaipaw_access_button.state(["!disabled"])
        message = safe_error_message(str(result.get("message") or "站点访问检查未完成"))
        self._record_settings_operation("HentaiPaw", f"HentaiPaw {message}", error=not result.get("accessible", False))
        if self.cookie_capture_platform_var.get() == "HentaiPaw":
            self.cookie_capture_status_var.set(message)
