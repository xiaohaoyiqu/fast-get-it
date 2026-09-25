from __future__ import annotations

import json
import threading
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from urllib.parse import urlparse

from software_app.core.blocklist import account_from_target, work_from_target
from software_app.core.bluesky_account import BlueskyAccountManager
from software_app.core.candidate_lists import export_candidate_list, import_candidate_list
from software_app.crawlers.pixiv import parse_pixiv_target
from software_app.crawlers.pixiv.catalog import filter_bookmark_candidates
from software_app.ui.desktop_support import (
    bounded_int as _bounded_int,
    select_treeview_row_at_event,
)
from software_app.ui.platform_config import PLATFORM_CONTENT_SCOPES, TASK_STATUS_LABELS
from software_app.ui.scrolling import bind_canvas_mousewheel


class FollowingTabMixin:
    @staticmethod
    def _candidate_author_for(row: dict, target: str, module_id: str) -> str:
        author_id = str(row.get("author_id") or "")
        inspected_work = str(row.get("pixiv_inspected_work_id") or "")
        if module_id == "google_image" and inspected_work:
            return author_id if work_from_target(module_id, target) == ("pixiv", inspected_work) else ""
        return author_id

    def _build_following_tab(self) -> None:
        self.following_tab.columnconfigure(0, weight=1)
        self.following_tab.rowconfigure(1, weight=1)

        controls_view = ttk.Frame(self.following_tab, style="Panel.TFrame")
        controls_view.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        controls_view.columnconfigure(0, weight=1)
        controls_canvas = tk.Canvas(controls_view, height=155, background=self.palette["panel"], highlightthickness=0)
        controls_scrollbar = ttk.Scrollbar(controls_view, orient="vertical", command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=controls_scrollbar.set)
        controls_canvas.grid(row=0, column=0, sticky="ew")
        controls_scrollbar.grid(row=0, column=1, sticky="ns")
        button_row = ttk.Frame(controls_canvas, style="Panel.TFrame")
        controls_window = controls_canvas.create_window((0, 0), window=button_row, anchor="nw")
        button_row.bind("<Configure>", lambda _event: controls_canvas.configure(scrollregion=controls_canvas.bbox("all")), add="+")
        controls_canvas.bind("<Configure>", lambda event: controls_canvas.itemconfigure(controls_window, width=event.width))
        button_row.columnconfigure(6, weight=1)
        self.candidate_refresh_var = tk.StringVar(value="更新新的")
        self.candidate_refresh_button = ttk.Button(
            button_row, textvariable=self.candidate_refresh_var, command=self._refresh_platform_candidates
        )
        self.candidate_refresh_button.grid(row=0, column=0, padx=(0, 6))
        self.following_full_refresh_button = ttk.Button(
            button_row, text="更新全部", command=self._refresh_all_platform_following
        )
        self.following_full_refresh_button.grid(row=0, column=1, padx=(0, 6))
        self.candidate_local_button = ttk.Button(
            button_row, text="读取本地", command=self._load_platform_candidates
        )
        self.candidate_local_button.grid(row=0, column=2, padx=(0, 6))
        self.candidate_use_button = ttk.Button(
            button_row, text="填入目标", command=self._use_selected_platform_candidate
        )
        self.candidate_use_button.grid(row=1, column=0, padx=(0, 6), pady=(7, 0))
        self.candidate_preview_button = ttk.Button(
            button_row, text="获取资料", command=self._preview_selected_platform_candidate
        )
        self.candidate_preview_button.grid(row=1, column=1, padx=(0, 6), pady=(7, 0))
        self.candidate_download_button = ttk.Button(
            button_row, text="加入队列", command=self._download_selected_platform_candidate
        )
        self.candidate_download_button.grid(row=1, column=2, pady=(7, 0))
        self.candidate_fill_batch_button = ttk.Button(
            button_row, text="批量填入工作台", command=self._fill_selected_candidates
        )
        self.candidate_fill_batch_button.grid(row=7, column=0, padx=(0, 6), pady=(7, 0), sticky="w")
        self.candidate_batch_info_button = ttk.Button(
            button_row, text="批量获取资料", command=self._fetch_selected_candidate_info
        )
        self.candidate_batch_info_button.grid(row=7, column=1, padx=(0, 6), pady=(7, 0), sticky="w")
        self.candidate_fill_batch_button.state(["disabled"])
        self.candidate_batch_info_button.state(["disabled"])
        self.candidate_block_author_button = ttk.Button(button_row, text="屏蔽选中作者", command=self._block_selected_candidate_authors)
        self.candidate_block_author_button.grid(row=1, column=3, padx=(6, 0), pady=(7, 0))
        self.candidate_block_work_button = ttk.Button(button_row, text="屏蔽选中作品", command=self._block_selected_candidate_works)
        self.candidate_block_work_button.grid(row=1, column=4, padx=(6, 0), pady=(7, 0))
        ttk.Label(button_row, text="搜索种类", style="Panel.TLabel").grid(row=2, column=0, sticky="w", pady=(8, 0))
        self.content_scope_combo = ttk.Combobox(
            button_row,
            textvariable=self.content_scope_var,
            state="readonly",
            values=PLATFORM_CONTENT_SCOPES["twitter"],
            width=23,
        )
        self.content_scope_combo.grid(row=2, column=1, columnspan=2, sticky="w", pady=(8, 0))
        self.content_scope_combo.bind("<<ComboboxSelected>>", self._content_scope_changed)
        self.content_scope_help_label = ttk.Label(
            button_row, textvariable=self.content_scope_help_var, style="Small.TLabel", wraplength=620
        )
        self.content_scope_help_label.grid(row=3, column=0, columnspan=7, sticky="ew", pady=(5, 0))
        ttk.Label(button_row, textvariable=self.candidate_selection_var, style="Section.TLabel").grid(
            row=4, column=0, columnspan=7, sticky="ew", pady=(7, 0)
        )
        self.candidate_select_all_button = ttk.Button(button_row, text="全选当前页", command=self._select_all_platform_candidates)
        self.candidate_select_all_button.grid(row=5, column=0, padx=(0, 6), pady=(7, 0))
        self.candidate_clear_button = ttk.Button(button_row, text="取消选择", command=self._clear_platform_candidate_selection)
        self.candidate_clear_button.grid(row=5, column=1, padx=(0, 6), pady=(7, 0))
        self.following_import_button = ttk.Button(button_row, text="从文件更新", command=self._import_following_accounts)
        self.following_import_button.grid(row=5, column=2, padx=(0, 6), pady=(7, 0))
        self.following_export_button = ttk.Button(button_row, text="导出关注", command=self._export_following_accounts)
        self.following_export_button.grid(row=5, column=3, pady=(7, 0))
        self.candidate_list_import_button = ttk.Button(button_row, text="导入名单", command=self._import_candidate_list)
        self.candidate_list_import_button.grid(row=5, column=4, padx=(6, 6), pady=(7, 0))
        self.candidate_list_export_button = ttk.Button(button_row, text="导出候选", command=self._export_candidate_list)
        self.candidate_list_export_button.grid(row=5, column=5, pady=(7, 0))
        self.jm_candidate_tags_button = ttk.Button(button_row, text="编辑 JM 标签", command=self._edit_selected_jm_candidate_tags)
        self.jm_candidate_tags_button.grid(row=6, column=0, columnspan=2, sticky="w", pady=(7, 0))
        self.jm_candidate_remove_button = ttk.Button(button_row, text="移除 JM 候选", command=self._remove_selected_jm_candidates)
        self.jm_candidate_remove_button.grid(row=6, column=2, columnspan=2, sticky="w", pady=(7, 0))
        self.bluesky_follow_button = ttk.Button(
            button_row, text="关注 Bluesky 账号", command=lambda: self._manage_bluesky_following(True)
        )
        self.bluesky_follow_button.grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.bluesky_follow_button.grid_remove()
        self.bluesky_unfollow_button = ttk.Button(
            button_row, text="取消关注", command=lambda: self._manage_bluesky_following(False)
        )
        self.bluesky_unfollow_button.grid(row=0, column=4, sticky="w")
        self.bluesky_unfollow_button.grid_remove()
        self.eh_favorite_button = ttk.Button(
            button_row, text="加入 EH 收藏", command=lambda: self._manage_eh_favorite(True)
        )
        self.eh_favorite_button.grid(row=0, column=3, sticky="w", padx=(0, 6))
        self.eh_favorite_button.grid_remove()
        self.eh_unfavorite_button = ttk.Button(
            button_row, text="移出 EH 收藏", command=lambda: self._manage_eh_favorite(False)
        )
        self.eh_unfavorite_button.grid(row=0, column=4, sticky="w")
        self.eh_unfavorite_button.grid_remove()
        self.following_progress_label = ttk.Label(
            button_row,
            textvariable=self.following_progress_status_var,
            style="Small.TLabel",
            wraplength=620,
        )
        self.following_progress_label.grid(row=7, column=0, columnspan=7, sticky="ew", pady=(7, 0))
        self.jm_candidate_auth_label = ttk.Label(
            button_row,
            textvariable=self.jm_auth_status_var,
            style="Small.TLabel",
            wraplength=620,
        )
        self.jm_candidate_auth_label.grid(row=7, column=0, columnspan=7, sticky="ew", pady=(7, 0))
        self.jm_candidate_auth_label.grid_remove()
        self.following_progress = ttk.Progressbar(
            button_row, variable=self.following_progress_var, maximum=100.0, mode="determinate"
        )
        self.following_progress.grid(row=8, column=0, columnspan=7, sticky="ew", pady=(4, 0))
        self.bookmark_filter_query_var = tk.StringVar()
        self.bookmark_filter_field_var = tk.StringVar(value="全部")
        self.bookmark_date_basis_var = tk.StringVar(value="收藏时间")
        self.bookmark_filter_controls = ttk.Frame(button_row, style="Panel.TFrame")
        self.bookmark_filter_controls.grid(row=9, column=0, columnspan=7, sticky="ew", pady=(8, 0))
        ttk.Label(self.bookmark_filter_controls, text="本地收藏筛选", style="Panel.TLabel").pack(side="left")
        ttk.Combobox(
            self.bookmark_filter_controls, textvariable=self.bookmark_filter_field_var,
            values=("全部", "作品 ID", "作者", "标签", "标题"), state="readonly", width=10,
        ).pack(side="left", padx=(6, 4))
        bookmark_query = ttk.Entry(self.bookmark_filter_controls, textvariable=self.bookmark_filter_query_var, width=18)
        bookmark_query.pack(side="left", padx=(0, 6))
        bookmark_query.bind("<Return>", lambda _event: self._apply_bookmark_filters())
        ttk.Combobox(
            self.bookmark_filter_controls, textvariable=self.bookmark_date_basis_var,
            values=("收藏时间", "发布时间"), state="readonly", width=10,
        ).pack(side="left", padx=(0, 6))
        ttk.Button(self.bookmark_filter_controls, text="筛选", command=self._apply_bookmark_filters).pack(side="left")
        self.bookmark_filter_controls.grid_remove()
        self.bookmark_filter_start_var = tk.StringVar(value=str(self.storage.get_setting("pixiv_start_date", "") or ""))
        self.bookmark_filter_end_var = tk.StringVar(value=str(self.storage.get_setting("pixiv_end_date", "") or ""))
        self.bookmark_filter_dates = ttk.Frame(button_row, style="Panel.TFrame")
        self.bookmark_filter_dates.grid(row=10, column=0, columnspan=7, sticky="ew", pady=(5, 0))
        ttk.Label(self.bookmark_filter_dates, text="日期范围", style="Panel.TLabel").pack(side="left")
        ttk.Entry(self.bookmark_filter_dates, textvariable=self.bookmark_filter_start_var, width=12).pack(side="left", padx=(6, 4))
        ttk.Label(self.bookmark_filter_dates, text="至", style="Panel.TLabel").pack(side="left")
        ttk.Entry(self.bookmark_filter_dates, textvariable=self.bookmark_filter_end_var, width=12).pack(side="left", padx=(4, 8))
        ttk.Label(self.bookmark_filter_dates, text="YYYY-MM-DD；留空不限", style="Small.TLabel").pack(side="left")
        self.bookmark_filter_dates.grid_remove()
        self.bookmark_result_var = tk.StringVar(value="")
        self.bookmark_result_label = ttk.Label(
            button_row, textvariable=self.bookmark_result_var, style="Small.TLabel"
        )
        self.bookmark_result_label.grid(row=11, column=0, columnspan=7, sticky="w", pady=(4, 0))
        self.bookmark_result_label.grid_remove()
        button_row.bind(
            "<Configure>",
            lambda event: (
                self.content_scope_help_label.configure(wraplength=max(240, event.width - 12)),
                self.following_progress_label.configure(wraplength=max(240, event.width - 12)),
                self.jm_candidate_auth_label.configure(wraplength=max(240, event.width - 12)),
            ),
            add="+",
        )
        for button in (self.candidate_use_button, self.candidate_preview_button, self.candidate_download_button):
            button.state(["disabled"])
        bind_canvas_mousewheel(controls_canvas, button_row)

        columns = ("handle", "name", "bio")
        following_tree_frame, self.following_tree = self._create_scrollable_tree(self.following_tab, columns)
        self.following_tree.configure(selectmode="extended")
        self.following_tree.heading("handle", text="账号")
        self.following_tree.heading("name", text="名称")
        self.following_tree.heading("bio", text="简介")
        self.following_tree.column("handle", width=150, stretch=False)
        self.following_tree.column("name", width=180, stretch=False)
        self.following_tree.column("bio", width=520)
        following_tree_frame.grid(row=1, column=0, sticky="nsew")
        self.following_tree.bind("<<TreeviewSelect>>", self._candidate_selection_changed)
        self.following_tree.bind("<Double-Button-1>", self._use_double_clicked_platform_candidate)

    def _build_twitter_history_tab(self) -> None:
        self.twitter_history_tab.columnconfigure(0, weight=1)
        self.twitter_history_tab.rowconfigure(1, weight=1)
        button_row = ttk.Frame(self.twitter_history_tab, style="Panel.TFrame")
        button_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        button_row.columnconfigure(6, weight=1)
        ttk.Button(button_row, text="刷新", command=self._load_platform_history).grid(row=0, column=0, padx=(0, 6))
        self.history_use_button = ttk.Button(button_row, text="填入目标", command=self._use_selected_platform_history)
        self.history_use_button.grid(row=0, column=1, padx=(0, 6))
        self.history_preview_button = ttk.Button(button_row, text="在线获取资料", command=self._preview_selected_platform_history)
        self.history_preview_button.grid(row=0, column=2, padx=(0, 6))
        self.history_clear_selected_button = ttk.Button(button_row, text="清理选中", command=self._clear_selected_platform_history)
        self.history_clear_selected_button.grid(row=0, column=3, padx=(0, 6))
        self.history_fill_batch_button = ttk.Button(button_row, text="批量填入工作台", command=self._fill_selected_histories)
        self.history_fill_batch_button.grid(row=0, column=5, padx=(6, 0))
        self.history_fill_batch_button.state(["disabled"])
        self.clear_history_var = tk.StringVar(value="清理本平台全部")
        self.history_clear_all_button = ttk.Button(
            button_row, textvariable=self.clear_history_var, command=self._clear_platform_history
        )
        self.history_clear_all_button.grid(row=0, column=4)
        ttk.Label(button_row, textvariable=self.history_selection_var, style="Small.TLabel").grid(
            row=1, column=0, columnspan=7, sticky="w", pady=(6, 0)
        )
        for button in (self.history_use_button, self.history_preview_button, self.history_clear_selected_button):
            button.state(["disabled"])

        columns = ("name", "handle", "count", "latest", "source")
        history_tree_frame, self.twitter_history_tree = self._create_scrollable_tree(self.twitter_history_tab, columns)
        self.twitter_history_tree.configure(selectmode="extended")
        self.twitter_history_tree.heading("name", text="作者/文件夹")
        self.twitter_history_tree.heading("handle", text="@名")
        self.twitter_history_tree.heading("count", text="记录")
        self.twitter_history_tree.heading("latest", text="最近时间")
        self.twitter_history_tree.heading("source", text="来源")
        self.twitter_history_tree.column("name", width=240)
        self.twitter_history_tree.column("handle", width=150, stretch=False)
        self.twitter_history_tree.column("count", width=80, stretch=False)
        self.twitter_history_tree.column("latest", width=160, stretch=False)
        self.twitter_history_tree.column("source", width=220)
        history_tree_frame.grid(row=1, column=0, sticky="nsew")
        self.twitter_history_tree.bind("<<TreeviewSelect>>", self._history_selection_changed)
        self.twitter_history_tree.bind("<Double-Button-1>", self._use_double_clicked_platform_history)

    def _refresh_platform_tabs(self) -> None:
        if not hasattr(self, "following_tree"):
            return
        adapter = self._selected_adapter()
        scope = self.content_scope_var.get()
        for button in (self.jm_candidate_tags_button, self.jm_candidate_remove_button):
            button.grid() if adapter.module_id == "jmcomic" else button.grid_remove()
        for button in (self.bluesky_follow_button, self.bluesky_unfollow_button):
            button.grid() if adapter.module_id == "bluesky" else button.grid_remove()
        for button in (self.eh_favorite_button, self.eh_unfavorite_button):
            button.grid() if adapter.module_id == "ehentai" else button.grid_remove()
        candidate_names = {
            "twitter": "Twitter 关注 / 用户",
            "pixiv": "Pixiv 作者 / 作品候选",
            "jmcomic": {
                "漫画收藏夹": "JMComic 漫画收藏",
                "小说收藏夹": "JMComic 小说收藏",
                "追更连载": "JMComic 追更连载",
                "漫画观看记录": "JMComic 漫画记录",
                "小说观看记录": "JMComic 小说记录",
            }.get(scope, "JMComic 搜索候选"),
            "bluesky": "Bluesky 公开关注" if scope == "关注账号" else "Bluesky 用户候选" if scope == "用户搜索" else "Bluesky 帖子候选",
            "instagram": "Instagram 账号 / 帖子",
            "google_image": "Google 页面候选",
            "website": "网页候选",
        }
        history_names = {
            "twitter": "推特历史",
            "pixiv": "Pixiv 历史",
            "jmcomic": "JMComic 历史",
            "bluesky": "Bluesky 历史",
            "instagram": "Instagram 历史",
            "google_image": "Google 搜索历史",
            "website": "网页爬取历史",
        }
        self.notebook.tab(self.following_tab, text=candidate_names.get(adapter.module_id, f"{adapter.display_name} 候选"))
        self.notebook.tab(self.twitter_history_tab, text=history_names.get(adapter.module_id, f"{adapter.display_name} 历史"))
        self.clear_history_var.set("清理推特全部" if adapter.module_id == "twitter" else "清理本平台全部")
        self.candidate_local_button.configure(text="读取本地")
        show_following_progress = (
            (adapter.module_id == "twitter" and scope == "关注账号")
            or (adapter.module_id == "pixiv" and scope == "关注画师")
        )
        if show_following_progress:
            self.following_progress_label.grid()
            self.following_progress.grid()
        else:
            self.following_progress_label.grid_remove()
            self.following_progress.grid_remove()
        if adapter.module_id == "jmcomic":
            self._refresh_jm_auth_status()
            self.jm_candidate_auth_label.grid()
        else:
            self.jm_candidate_auth_label.grid_remove()
        if adapter.module_id == "pixiv" and scope in {"作品收藏", "小说收藏"}:
            self.bookmark_filter_controls.grid()
            self.bookmark_filter_dates.grid()
            self.bookmark_result_label.grid()
        else:
            self.bookmark_filter_controls.grid_remove()
            self.bookmark_filter_dates.grid_remove()
            self.bookmark_result_label.grid_remove()

        if adapter.module_id == "twitter":
            self.following_import_button.configure(text="从文件更新", command=self._import_following_accounts)
            self.following_export_button.configure(text="导出关注", command=self._export_following_accounts)
            self.following_import_button.state(["!disabled"])
            self.following_export_button.state(["!disabled"] if self.following_rows else ["disabled"])
            self.candidate_refresh_var.set(
                "停止更新" if self.following_refreshing else ("更新新的" if scope == "关注账号" else "搜索用户")
            )
            self.candidate_refresh_button.state(["!disabled"] if scope != "帖子 / 媒体 URL" else ["disabled"])
            self.following_full_refresh_button.state(
                ["!disabled"] if scope == "关注账号" and not self.following_refreshing else ["disabled"]
            )
            self.following_tree.heading("handle", text="账号")
            self.following_tree.heading("name", text="名称")
            self.following_tree.heading("bio", text="简介 / 来源")
            rows = []
            if scope == "关注账号":
                rows = [
                    {
                        "module_id": "twitter",
                        "target": f"@{str(row.get('handle') or '').strip().lstrip('@')}",
                        "handle": str(row.get("handle") or "").strip().lstrip("@"),
                        "name": row.get("display_name") or "",
                        "detail": (
                            ("[文件导入] " if row.get("import_source") or row.get("source") == "file_import" else "")
                            + str(row.get("bio") or "").replace("\n", " / ")
                        ),
                        "source": row.get("source") or "关注列表",
                        "import_source": row.get("import_source") or "",
                    }
                    for row in self.following_rows
                    if str(row.get("handle") or "").strip()
                ]
            seen_targets = {str(row.get("target") or "").lower() for row in rows}
            search_rows = self.platform_candidate_rows.get("twitter", []) if scope == "用户 / @用户名" else []
            for search_row in search_rows:
                handle = str(search_row.get("handle") or "").strip().lstrip("@")
                target = f"@{handle}" if handle else str(search_row.get("url") or search_row.get("target") or "").strip()
                if not target or target.lower() in seen_targets:
                    continue
                seen_targets.add(target.lower())
                rows.append(
                    {
                        "module_id": "twitter",
                        "target": target,
                        "handle": handle,
                        "name": search_row.get("title") or search_row.get("name") or target,
                        "detail": search_row.get("source") or "搜索候选",
                    }
                )
        elif adapter.module_id == "pixiv" and scope == "关注画师":
            self.candidate_local_button.configure(text="读取本地关注")
            self.following_import_button.configure(text="导入 Cookie", command=self._import_pixiv_cookie)
            self.following_export_button.configure(text="导出关注", command=self._export_pixiv_following_accounts)
            self.following_import_button.state(["!disabled"])
            self.following_export_button.state(["!disabled"] if self.pixiv_following_rows else ["disabled"])
            self.candidate_refresh_var.set("停止更新" if self.following_refreshing else "更新新的")
            self.candidate_refresh_button.state(["!disabled"])
            self.following_full_refresh_button.state(["!disabled"] if not self.following_refreshing else ["disabled"])
            self.following_tree.heading("handle", text="画师 ID")
            self.following_tree.heading("name", text="名称")
            self.following_tree.heading("bio", text="简介")
            rows = [
                {
                    "module_id": "pixiv",
                    "target": str(row.get("profile_url") or f"https://www.pixiv.net/users/{row.get('user_id') or ''}"),
                    "handle": str(row.get("user_id") or ""),
                    "name": row.get("display_name") or str(row.get("user_id") or ""),
                    "detail": str(row.get("bio") or "").replace("\n", " / "),
                    "avatar_url": row.get("avatar_url") or "",
                    "profile_url": row.get("profile_url") or "",
                    "author_id": str(row.get("user_id") or ""),
                    "source": "Pixiv 关注画师",
                }
                for row in self.pixiv_following_rows
                if str(row.get("user_id") or "").strip()
            ]
        else:
            if adapter.module_id == "pixiv":
                self.following_import_button.configure(text="导入 Cookie", command=self._import_pixiv_cookie)
            elif adapter.module_id == "jmcomic":
                self.following_import_button.configure(text="导入登录资料", command=self._import_jmcomic_cookie)
            elif adapter.module_id == "instagram":
                self.following_import_button.configure(text="导入 Cookie", command=self._import_instagram_cookie)
            else:
                self.following_import_button.configure(text="导入关注", command=self._import_following_accounts)
            self.following_export_button.configure(text="导出关注", command=self._export_following_accounts)
            self.following_import_button.state(["!disabled"] if adapter.module_id in {"pixiv", "jmcomic", "instagram"} else ["disabled"])
            self.following_export_button.state(["disabled"])
            self.following_full_refresh_button.state(["disabled"])
            is_search_scope = (
                adapter.module_id == "google_image"
                or (
                    adapter.module_id == "pixiv"
                    and scope not in {"作品 ID", "作者 ID", "关注画师", "小说 ID", "作者小说"}
                )
                or (adapter.module_id == "jmcomic" and scope not in {"漫画 ID / 链接", "章节 ID / 链接"})
                or (adapter.module_id == "bluesky" and scope != "帖子 / 媒体")
            )
            if adapter.module_id == "pixiv" and scope in {"作品收藏", "小说收藏"}:
                self.candidate_refresh_var.set("更新我的作品收藏" if scope == "作品收藏" else "更新我的小说收藏")
                self.candidate_local_button.configure(text="读取本地收藏")
            elif adapter.module_id == "jmcomic":
                jm_actions = {
                    "综合搜索": "执行综合搜索",
                    "作品搜索": "搜索作品",
                    "作者搜索": "搜索作者作品",
                    "标签搜索": "搜索标签",
                    "角色搜索": "搜索角色",
                    "分类 / 排行": "读取分类 / 排行",
                    "漫画收藏夹": "读取漫画收藏",
                    "小说收藏夹": "读取小说收藏",
                    "追更连载": "读取追更连载",
                    "漫画观看记录": "读取漫画记录",
                    "小说观看记录": "读取小说记录",
                }
                self.candidate_refresh_var.set(jm_actions.get(scope, "执行此类搜索"))
                self.candidate_local_button.configure(text="显示本次结果")
            elif adapter.module_id == "bluesky":
                self.candidate_refresh_var.set({
                    "关注账号": "读取公开关注",
                    "用户搜索": "搜索用户",
                    "帖子搜索": "搜索帖子",
                }.get(scope, "使用帖子链接"))
                self.candidate_local_button.configure(text="显示本次结果")
            else:
                self.candidate_refresh_var.set("打开相似搜索" if adapter.module_id == "google_image" else "执行此类搜索")
            can_refresh = is_search_scope and ("search" in adapter.info.capabilities or adapter.module_id == "google_image")
            self.candidate_refresh_button.state(["!disabled"] if can_refresh else ["disabled"])
            self.following_tree.heading("handle", text="目标")
            self.following_tree.heading("name", text="标题")
            self.following_tree.heading("bio", text="种类 / 作者")
            if adapter.module_id == "jmcomic":
                self.following_tree.heading("handle", text="作品 / 章节 ID")
                self.following_tree.heading("name", text="标题")
                self.following_tree.heading("bio", text="来源 / 作者 / 标签")
            rows = []
            candidate_rows = (
                []
                if adapter.module_id in {"pixiv", "jmcomic", "bluesky", "instagram"}
                and self.platform_candidate_scope.get(adapter.module_id) != scope
                else self.platform_candidate_rows.get(adapter.module_id, [])
            )
            if adapter.module_id == "pixiv" and scope in {"作品收藏", "小说收藏"}:
                candidate_rows = self._filter_pixiv_bookmark_rows(candidate_rows)
                self.bookmark_result_var.set(
                    f"匹配 {len(candidate_rows)} 项；显示前 {min(len(candidate_rows), 1000)} 项，缩小条件可查看其余结果"
                )
                candidate_rows = candidate_rows[:1000]
            for row in candidate_rows:
                target = str(row.get("url") or row.get("target") or row.get("id") or "").strip()
                if target:
                    kind_text = str(row.get("type") or row.get("search_mode") or scope).strip()
                    author_text = str(row.get("author_name") or "").strip()
                    tags = row.get("tags") or []
                    tag_text = "标签: " + ", ".join(map(str, tags[:5])) if isinstance(tags, (tuple, list)) and tags else ""
                    rows.append(
                        {
                            "module_id": adapter.module_id,
                            "target": target,
                            "handle": str(row.get("id") or row.get("author_id") or ""),
                            "name": row.get("title") or row.get("name") or target,
                            "detail": " · ".join(part for part in (kind_text, author_text, tag_text) if part),
                            "input_kind": row.get("input_kind") or "",
                            "source": row.get("source") or "搜索候选",
                            "bio": row.get("bio") or "",
                            "avatar_url": row.get("avatar_url") or row.get("thumbnail_url") or "",
                            "profile_url": row.get("url") or "",
                            "final_url": row.get("final_url") or "",
                            "author_id": row.get("author_id") or "",
                            "pixiv_inspected_work_id": row.get("pixiv_inspected_work_id") or "",
                            "author_name": row.get("author_name") or "",
                            "published_at": row.get("published_at") or "",
                            "tags": row.get("tags") or [],
                            "downloadable": bool(row.get("downloadable", True)),
                            "read_only_reason": row.get("read_only_reason") or "",
                        }
                    )
        blocked_accounts = self.manager.blocklist.blocked_accounts()
        blocked_works = getattr(self.manager.blocklist, "blocked_works", lambda: set())()
        unblocked_rows = [
            row for row in rows
            if not self.manager.blocklist.is_blocked(
                adapter.module_id, str(row.get("target") or ""),
                input_kind=str(row.get("input_kind") or ""),
                author_id=FollowingTabMixin._candidate_author_for(row, str(row.get("target") or ""), adapter.module_id),
                blocked_accounts=blocked_accounts,
                blocked_works=blocked_works,
            )
            and not (adapter.module_id == "google_image" and row.get("final_url") and self.manager.blocklist.is_blocked(
                "google_image", str(row["final_url"]),
                author_id=FollowingTabMixin._candidate_author_for(row, str(row["final_url"]), adapter.module_id),
                blocked_accounts=blocked_accounts,
                blocked_works=blocked_works,
            ))
        ]
        self._blocked_candidate_count = len(rows) - len(unblocked_rows)
        rows = unblocked_rows
        selected_targets = {
            str(self.current_candidate_rows[int(item_id)].get("target") or "").strip().lower()
            for item_id in self.following_tree.selection()
            if str(item_id).isdigit() and int(item_id) < len(getattr(self, "current_candidate_rows", []))
        }
        current_view = self.following_tree.yview()
        scroll_fraction = float(current_view[0]) if current_view else 0.0
        self.current_candidate_rows = rows
        for item in self.following_tree.get_children():
            self.following_tree.delete(item)
        for index, row in enumerate(rows):
            self.following_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(row.get("handle") or row.get("target", ""), row.get("name", ""), row.get("detail", "")),
            )
        restored = [
            str(index)
            for index, row in enumerate(rows)
            if str(row.get("target") or "").strip().lower() in selected_targets
        ]
        if restored:
            self.following_tree.selection_set(*restored)
            self.following_tree.focus(restored[0])
        if rows:
            self.following_tree.yview_moveto(max(0.0, min(scroll_fraction, 1.0)))
        self._candidate_selection_changed()
        self._populate_platform_history(adapter.module_id)
        self.candidate_list_import_button.state(
            ["!disabled"] if "download" in adapter.info.capabilities and adapter.info.stage != "planned" else ["disabled"]
        )
        self.candidate_list_export_button.state(["!disabled"] if self._exportable_candidate_rows() else ["disabled"])

    def _exportable_candidate_rows(self) -> list[dict]:
        module_id = self.module_var.get()
        scope = self.content_scope_var.get()
        if self.platform_candidate_scope.get(module_id) == scope and scope not in {"关注账号", "关注画师"}:
            rows = list(self.platform_candidate_rows.get(module_id, []))
            if module_id == "pixiv" and scope in {"作品收藏", "小说收藏"}:
                rows = self._filter_pixiv_bookmark_rows(rows)
        else:
            rows = list(self.current_candidate_rows)
        blocked_accounts = self.manager.blocklist.blocked_accounts()
        blocked_works = getattr(self.manager.blocklist, "blocked_works", lambda: set())()
        return [
            row for row in rows
            if not self.manager.blocklist.is_blocked(
                module_id, str(row.get("url") or row.get("target") or ""),
                input_kind=str(row.get("input_kind") or ""),
                author_id=FollowingTabMixin._candidate_author_for(
                    row, str(row.get("url") or row.get("target") or ""), module_id
                ),
                blocked_accounts=blocked_accounts,
                blocked_works=blocked_works,
            )
            and not (module_id == "google_image" and row.get("final_url") and self.manager.blocklist.is_blocked(
                "google_image", str(row["final_url"]),
                author_id=FollowingTabMixin._candidate_author_for(row, str(row["final_url"]), module_id),
                blocked_accounts=blocked_accounts,
                blocked_works=blocked_works,
            ))
        ]

    def _import_candidate_list(self) -> None:
        module_id = self.module_var.get()
        path = filedialog.askopenfilename(
            title="导入当前平台候选名单",
            filetypes=[("候选名单", "*.txt *.csv *.json"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            rows = import_candidate_list(path, module_id)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("名单导入失败", str(exc))
            return
        if not rows:
            messagebox.showinfo("名单为空", "文件中没有可用目标")
            return
        if module_id == "twitter" and self.content_scope_var.get() == "关注账号":
            self.content_scope_var.set("用户 / @用户名")
            self._content_scope_changed()
        elif module_id == "pixiv" and self.content_scope_var.get() in {"关注画师", "作品收藏", "小说收藏"}:
            self.content_scope_var.set("作品 ID")
            self._content_scope_changed()
        self._show_platform_search_results(module_id, Path(path).name, rows)
        self.notebook.select(self.following_tab)
        self.status_var.set(f"已导入 {len(rows)} 个候选；选择后可预览或加入队列")

    def _export_candidate_list(self) -> None:
        rows = self._exportable_candidate_rows()
        if not rows:
            messagebox.showinfo("没有候选", "当前列表没有可导出的目标")
            return
        module_id = self.module_var.get()
        if module_id == "pixiv" and self.content_scope_var.get() in {"作品收藏", "小说收藏"}:
            rows = [{**row, "bookmarked": True} for row in rows]
        path = filedialog.asksaveasfilename(
            title="导出当前候选",
            defaultextension=".csv",
            initialfile=f"{module_id}_candidates.csv",
            filetypes=[("CSV 表格", "*.csv"), ("JSON 数据", "*.json"), ("文本目标名单", "*.txt")],
        )
        if not path:
            return
        try:
            count = export_candidate_list(rows, path, module_id)
        except (OSError, ValueError) as exc:
            messagebox.showerror("候选导出失败", str(exc))
            return
        self.status_var.set(f"已导出 {count} 个候选：{path}")

    def _populate_platform_history(self, module_id: str) -> None:
        if module_id == "twitter":
            rows = list(self.twitter_history_rows)
            self.twitter_history_tree.heading("name", text="作者/文件夹")
            self.twitter_history_tree.heading("handle", text="@名")
            self.twitter_history_tree.heading("count", text="记录")
            self.twitter_history_tree.heading("latest", text="最近时间")
            self.twitter_history_tree.heading("source", text="来源")
        else:
            file_counts: dict[str, int] = {}
            file_summaries: dict[str, dict] = {}
            for file_row in self.storage.list_files(module_id=module_id, limit=10000):
                task_id = str(file_row["task_id"] or "")
                if task_id:
                    file_counts[task_id] = file_counts.get(task_id, 0) + 1
                    summary = file_summaries.setdefault(task_id, {})
                    for key in ("title", "author_name", "author_id", "published_at"):
                        if not summary.get(key) and file_row[key]:
                            summary[key] = str(file_row[key])
                    if not summary.get("tags"):
                        try:
                            tags = json.loads(str(file_row["tags_json"] or "[]"))
                        except (TypeError, ValueError):
                            tags = []
                        if isinstance(tags, list):
                            summary["tags"] = [str(tag) for tag in tags if str(tag).strip()]
            rows = []
            seen_targets: set[str] = set()
            if module_id == "pixiv":
                try:
                    account_rows = self.manager.get_adapter("pixiv").load_account_history()
                except Exception as exc:  # Cache damage must not hide software task history.
                    account_rows = []
                    self._append_log(f"读取 Pixiv 账号浏览历史缓存失败：{exc}")
                for item in account_rows:
                    target = str(item.get("url") or "").strip()
                    if not target or target.casefold() in seen_targets:
                        continue
                    seen_targets.add(target.casefold())
                    rows.append(
                        {
                            "module_id": "pixiv",
                            "target": target,
                            "name": str(item.get("title") or target),
                            "handle": "浏览历史（Premium）",
                            "count": 0,
                            "latest": str(item.get("viewed_at") or "-") or "-",
                            "source": "账号浏览历史",
                            "tags": item.get("tags") or [],
                        }
                    )
            for task_row in self.storage.list_tasks(limit=500):
                row = dict(task_row)
                if str(row.get("module_id") or "") != module_id:
                    continue
                target = str(row.get("normalized_target") or row.get("target") or "").strip()
                target_key = target.casefold()
                if target_key in seen_targets:
                    rows = [item for item in rows if str(item.get("target") or "").casefold() != target_key]
                else:
                    seen_targets.add(target_key)
                scope = self._history_content_scope(module_id, target, row.get("options_json"))
                task_id = str(row.get("task_id") or "")
                summary = file_summaries.get(task_id, {})
                title = str(summary.get("title") or "").strip()
                author_name = str(summary.get("author_name") or "").strip()
                display_name = f"{title} — {author_name}" if title and author_name else title or target
                rows.append(
                    {
                        "module_id": module_id,
                        "task_id": task_id,
                        "target": target,
                        "name": display_name,
                        "handle": scope,
                        "count": file_counts.get(task_id, 0),
                        "latest": row.get("updated_at") or "-",
                        "source": TASK_STATUS_LABELS.get(str(row.get("status") or ""), row.get("status") or "任务记录"),
                        "tags": summary.get("tags") or [],
                    }
                )
            name_headings = {
                "pixiv": "作品 / 作者目标",
                "jmcomic": "漫画 / 章节目标",
                "bluesky": "账号 / 帖子目标",
                "instagram": "账号 / 帖子目标",
                "google_image": "查询图片",
                "website": "网页目标",
            }
            self.twitter_history_tree.heading("name", text=name_headings.get(module_id, "目标"))
            self.twitter_history_tree.heading("handle", text="搜索 / 内容种类")
            self.twitter_history_tree.heading("count", text="文件")
            self.twitter_history_tree.heading("latest", text="最近时间")
            self.twitter_history_tree.heading("source", text="状态")
        self.twitter_history_tree.column("name", width=245, minwidth=150, stretch=True, anchor="w")
        self.twitter_history_tree.column("handle", width=120, minwidth=95, stretch=False, anchor="w")
        self.twitter_history_tree.column("count", width=60, minwidth=55, stretch=False, anchor="center")
        self.twitter_history_tree.column("latest", width=135, minwidth=120, stretch=False, anchor="center")
        self.twitter_history_tree.column("source", width=100, minwidth=85, stretch=False, anchor="center")
        self.platform_history_rows = rows
        for item in self.twitter_history_tree.get_children():
            self.twitter_history_tree.delete(item)
        for index, row in enumerate(rows):
            handle = str(row.get("handle") or "").strip()
            self.twitter_history_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    row.get("name") or row.get("target") or "-",
                    f"@{handle}" if module_id == "twitter" and handle and handle != "-" else handle or "-",
                    row.get("count") or row.get("local_files") or 0,
                    row.get("latest") or "-",
                    row.get("source") or "-",
                ),
            )
        self._history_selection_changed()

    def _history_content_scope(self, module_id: str, target: str, options_json: object) -> str:
        options = {}
        if isinstance(options_json, str) and options_json.strip():
            try:
                loaded = json.loads(options_json)
                options = loaded if isinstance(loaded, dict) else {}
            except (TypeError, ValueError):
                pass
        value = target.lower()
        if module_id == "pixiv":
            kind_scopes = {
                "work": "作品 ID", "user": "作者 ID", "user_novels": "作者小说",
                "manga_series": "漫画系列", "novel": "小说 ID", "novel_series": "小说系列",
                "bookmark": "作品收藏", "novel_bookmark": "小说收藏", "history": "浏览历史（Premium）",
                "fanbox": "FANBOX", "sketch": "Sketch",
            }
            if target.startswith(("http://", "https://")):
                try:
                    parsed_kind = parse_pixiv_target(target).kind
                except ValueError:
                    parsed_kind = ""
                if parsed_kind in kind_scopes:
                    return kind_scopes[parsed_kind]
            if "/tags/" in value:
                return "标签（全部）"
        scope = str(options.get("content_scope") or options.get("search_mode") or "").strip()
        if scope:
            return scope
        if module_id == "pixiv":
            return "作品 ID"
        if module_id == "jmcomic":
            return "章节 ID / 链接" if "/photo/" in value else "漫画 ID / 链接"
        return PLATFORM_CONTENT_SCOPES.get(module_id, ("目标",))[0]

    def _load_platform_candidates(self) -> None:
        if self.module_var.get() == "twitter" and self.content_scope_var.get() == "关注账号":
            self._load_following_accounts()
            self._show_following_cache_info(self.manager.get_adapter("twitter").following_cache_info())
        elif self.module_var.get() == "pixiv" and self.content_scope_var.get() == "关注画师":
            self._load_pixiv_following_accounts()
            self._show_following_cache_info(self.manager.get_adapter("pixiv").following_cache_info())
        elif self.module_var.get() == "pixiv" and self.content_scope_var.get() in {"作品收藏", "小说收藏"}:
            adapter = self.manager.get_adapter("pixiv")
            kind = "novel" if self.content_scope_var.get() == "小说收藏" else "work"
            rows = adapter.load_bookmark_candidates(kind)
            self.platform_candidate_rows["pixiv"] = rows
            self.platform_candidate_scope["pixiv"] = self.content_scope_var.get()
            self._refresh_platform_tabs()
            payload = adapter.load_bookmark_payload(kind)
            updated_at = str(payload.get("updated_at") or "尚未在线更新")
            cache_state = "完整" if payload.get("complete") else "部分或旧版"
            if payload.get("account") and adapter.cookie_account_id() and payload.get("account") != adapter.cookie_account_id():
                cache_state = "其他账号的缓存，未展示"
            self.status_var.set(f"已读取本地 Pixiv 收藏 {len(rows)} 个（{cache_state}缓存）")
            messagebox.showinfo(
                "Pixiv 本地收藏",
                f"已读取当前账号的{'小说' if kind == 'novel' else '作品'}收藏 {len(rows)} 个。\n"
                f"缓存状态：{cache_state}；更新时间：{updated_at}\n\n"
                "这里只读取本地缓存，不访问网站，也不会自动下载。",
            )
        else:
            self._refresh_platform_tabs()
            self.status_var.set(f"已读取 {len(self.current_candidate_rows)} 个本地候选")
            messagebox.showinfo(
                "读取本地候选",
                f"已从软件本地记录读取 {len(self.current_candidate_rows)} 个候选。\n"
                "这里只读取缓存和历史，不会访问网站，也不会自动开始下载。",
            )

    def _show_following_cache_info(self, info: dict) -> None:
        platform = str(info.get("platform") or "当前平台")
        path = str(info.get("path") or "未知")
        if not info.get("valid"):
            message = f"{info.get('error') or '本地关注缓存无效'}\n\n文件：{path}\n\n没有使用该文件中的账号。"
            messagebox.showwarning(f"{platform} 本地关注", message)
            return
        partial_text = "部分缓存（不能据此判断取关）" if info.get("partial") else "完整缓存"
        mode_names = {"new": "更新新的", "all": "更新全部", "file_merge": "从文件合并"}
        update_mode = mode_names.get(str(info.get("update_mode") or ""), str(info.get("update_mode") or "未记录"))
        details = [
            f"已读取 {int(info.get('count') or 0)} 个有效账号。",
            f"状态：{partial_text}",
            f"最近方式：{update_mode}",
        ]
        if info.get("account"):
            details.append(f"所属账号：{info['account']}")
        if info.get("collected_at"):
            details.append(f"在线采集时间：{info['collected_at']}")
        if info.get("imported_at"):
            details.append(f"文件导入时间：{info['imported_at']}")
        details.extend(
            [
                f"文件：{path}",
                "",
                "本次只读取本地文件，不访问网站，也不会自动下载。",
            ]
        )
        messagebox.showinfo(f"{platform} 本地关注", "\n".join(details))

    def _refresh_platform_candidates(self) -> None:
        module_id = self.module_var.get()
        if module_id in {"twitter", "pixiv"} and self.following_refreshing:
            if self.following_cancel_event is not None:
                self.following_cancel_event.set()
                self.candidate_refresh_var.set("正在停止…")
                self.status_var.set("正在停止刷新；已获取的关注账号会保留")
        elif module_id == "twitter" and self.content_scope_var.get() == "关注账号":
            self._refresh_following_accounts("new")
        elif module_id == "pixiv" and self.content_scope_var.get() == "关注画师":
            self._refresh_pixiv_following_accounts("new")
        elif module_id == "google_image":
            self.notebook.select(self.google_search_tab)
        else:
            self._search_platform_targets()

    def _selected_platform_candidate(self) -> dict | None:
        rows = self._selected_platform_candidates()
        return rows[0] if rows else None

    def _selected_platform_candidates(self) -> list[dict]:
        rows: list[dict] = []
        for item_id in self.following_tree.selection():
            try:
                index = int(item_id)
            except ValueError:
                continue
            if index < len(self.current_candidate_rows):
                rows.append(self.current_candidate_rows[index])
        return rows

    def _select_all_platform_candidates(self) -> None:
        children = self.following_tree.get_children()
        if children:
            self.following_tree.selection_set(*children)
        self._candidate_selection_changed()

    def _clear_platform_candidate_selection(self) -> None:
        self.following_tree.selection_remove(*self.following_tree.selection())
        self._candidate_selection_changed()

    @staticmethod
    def _jm_candidate_key(row: dict) -> str:
        return str(row.get("target") or row.get("url") or row.get("id") or "").strip().casefold()

    def _edit_selected_jm_candidate_tags(self) -> None:
        if self.module_var.get() != "jmcomic":
            return
        selected = self._selected_platform_candidates()
        if not selected:
            return
        original = selected[0].get("tags") or []
        initial = ", ".join(map(str, original)) if isinstance(original, (list, tuple)) else str(original)
        value = simpledialog.askstring("编辑 JMComic 标签", "输入最多 5 个标签，用逗号分隔；留空可清除标签。", initialvalue=initial)
        if value is None:
            return
        tags = list(dict.fromkeys(part.strip() for part in value.replace("，", ",").split(",") if part.strip()))
        if len(tags) > 5:
            messagebox.showwarning("标签过多", "每个候选最多保存 5 个标签")
            return
        keys = {self._jm_candidate_key(row) for row in selected}
        for row in self.platform_candidate_rows.get("jmcomic", []):
            if self._jm_candidate_key(row) in keys:
                row["tags"] = tags
        self._refresh_platform_tabs()
        self.status_var.set(f"已更新 {len(keys)} 个 JMComic 候选的标签；可导出 CSV/JSON 保存")

    def _remove_selected_jm_candidates(self) -> None:
        if self.module_var.get() != "jmcomic":
            return
        keys = {self._jm_candidate_key(row) for row in self._selected_platform_candidates()}
        if not keys:
            return
        previous = self.platform_candidate_rows.get("jmcomic", [])
        self.platform_candidate_rows["jmcomic"] = [row for row in previous if self._jm_candidate_key(row) not in keys]
        self._refresh_platform_tabs()
        self.status_var.set(f"已从当前列表移除 {len(keys)} 个 JMComic 候选；重新搜索可恢复")

    def _manage_bluesky_following(self, desired: bool) -> None:
        if self.module_var.get() != "bluesky":
            return
        row = self._selected_platform_candidate()
        if not row:
            messagebox.showwarning("未选择账号", "请先选择一个 Bluesky 用户候选")
            return
        target = str(row.get("author_id") or row.get("handle") or row.get("target") or "").strip()
        input_kind = str(row.get("input_kind") or "")
        if input_kind != "profile" or "/post/" in str(row.get("target") or ""):
            messagebox.showwarning("请选择用户", "关注管理只对 Bluesky 用户候选开放，不直接操作帖子候选")
            return
        action = "关注" if desired else "取消关注"
        label = str(row.get("name") or row.get("author_name") or target)
        if not messagebox.askyesno(
            f"{action} Bluesky 账号",
            f"确认使用你的 Bluesky 账号{action}：\n{label}\n{target}\n\n"
            "该操作会立即修改站内关注关系。应用密码只用于本次操作，不会保存。",
            parent=self,
        ):
            return
        initial = str(self.storage.get_setting("bluesky_account_identifier", "") or "")
        identifier = simpledialog.askstring(
            "Bluesky 账号", "输入你的 Bluesky handle：", initialvalue=initial, parent=self
        )
        if not identifier:
            return
        app_password = simpledialog.askstring(
            "Bluesky 应用密码", "输入应用密码（仅本次使用，不保存）：", show="*", parent=self
        )
        if not app_password:
            return
        self.storage.set_setting("bluesky_account_identifier", identifier.strip())
        for button in (self.bluesky_follow_button, self.bluesky_unfollow_button):
            button.state(["disabled"])
        self.status_var.set(f"正在{action} Bluesky 账号：{label}")
        proxy_url = str(self.storage.get_setting("proxy_url", "") or "")

        def worker() -> None:
            try:
                result = BlueskyAccountManager(
                    identifier, app_password, proxy_url=proxy_url
                ).set_following(target, desired)
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("bluesky_follow_error", (desired, label, str(exc))))
                return
            self.ui_queue.put(("bluesky_follow_done", (desired, label, result)))

        threading.Thread(target=worker, name="bluesky-follow-management", daemon=True).start()

    def _finish_bluesky_following(self, payload, *, error: bool) -> None:
        if error:
            desired, label, message = payload
            action = "关注" if desired else "取消关注"
            self.status_var.set(f"Bluesky {action}失败")
            self._append_log(f"Bluesky {action} {label} 失败：{message}")
            messagebox.showerror(f"Bluesky {action}失败", str(message), parent=self)
        else:
            desired, label, result = payload
            action = "关注" if desired else "取消关注"
            changed = bool(result.get("changed")) if isinstance(result, dict) else False
            message = f"已{action} {label}" if changed else f"{label} 已经处于目标状态，无需重复操作"
            self.status_var.set(message)
            self._append_log(message)
        self._candidate_selection_changed()

    def _manage_eh_favorite(self, desired: bool) -> None:
        if self.module_var.get() != "ehentai":
            return
        row = self._selected_platform_candidate()
        if not row:
            messagebox.showwarning("未选择画廊", "请先选择一个 EH 画廊候选", parent=self)
            return
        target = str(row.get("target") or "").strip()
        try:
            parsed = self.manager.get_adapter("ehentai").can_handle(target)
        except Exception:  # noqa: BLE001
            parsed = False
        if not parsed or str(row.get("input_kind") or "") != "gallery":
            messagebox.showwarning("请选择画廊", "收藏管理只对具体 EH 画廊候选开放", parent=self)
            return
        label = str(row.get("title") or row.get("name") or target)
        if desired:
            for button in (self.eh_favorite_button, self.eh_unfavorite_button):
                button.state(["disabled"])
            self.status_var.set("正在读取 EH 收藏分类与当前状态…")
            proxy_url = str(self.storage.get_setting("proxy_url", "") or "")

            def read_state() -> None:
                try:
                    state = self.manager.get_adapter("ehentai").favorite_state(
                        target, {"proxy_url": proxy_url}
                    )
                except Exception as exc:  # noqa: BLE001
                    self.ui_queue.put(("eh_favorite_error", (True, label, str(exc))))
                    return
                self.ui_queue.put(("eh_favorite_prompt", (label, target, state)))

            threading.Thread(target=read_state, name="eh-favorite-state", daemon=True).start()
            return
        self._start_eh_favorite_write(False, label, target, None, "", "")

    def _prompt_eh_favorite(self, payload) -> None:
        label, target, state = payload
        categories = list(state.get("categories") or []) if isinstance(state, dict) else []
        categories = [str(categories[index]) if index < len(categories) else f"分类 {index}" for index in range(10)]
        current = state.get("category") if isinstance(state, dict) else None
        current_category = current if isinstance(current, int) and not isinstance(current, bool) else 0
        lines = "\n".join(f"{index}: {name}" for index, name in enumerate(categories))
        category = simpledialog.askinteger(
            "EH 收藏分类",
            f"选择账号当前的收藏分类：\n\n{lines}",
            initialvalue=current_category, minvalue=0, maxvalue=9, parent=self,
        )
        if category is None:
            self._candidate_selection_changed()
            return
        initial_note = str(state.get("note") or "") if isinstance(state, dict) and state.get("favorited") else ""
        note_value = simpledialog.askstring(
            "EH 收藏注释", "可选注释；按 UTF-8 编码最多 200 字节：",
            initialvalue=initial_note, parent=self,
        )
        if note_value is None:
            self._candidate_selection_changed()
            return
        note = note_value.strip()
        if len(note.encode("utf-8")) > 200:
            messagebox.showwarning("注释过长", "EH 收藏注释按 UTF-8 编码后不能超过 200 字节", parent=self)
            self._candidate_selection_changed()
            return
        self._start_eh_favorite_write(True, label, target, category, note, categories[category])

    def _start_eh_favorite_write(self, desired: bool, label: str, target: str,
                                 category: int | None, note: str, category_name: str) -> None:
        action = "加入收藏" if desired else "移出收藏"
        detail = f"\n分类：{category} · {category_name}" if desired else ""
        if not messagebox.askyesno(
            f"EH {action}",
            f"确认在该画廊所属的表站/里站账号中{action}？\n{label}\n{target}{detail}\n\n"
            "该操作会立即修改站内收藏，并在写入后重新读取状态核验。",
            parent=self,
        ):
            self._candidate_selection_changed()
            return
        for button in (self.eh_favorite_button, self.eh_unfavorite_button):
            button.state(["disabled"])
        self.status_var.set(f"正在将 EH 画廊{action}：{label}")
        proxy_url = str(self.storage.get_setting("proxy_url", "") or "")

        def worker() -> None:
            try:
                result = self.manager.get_adapter("ehentai").set_favorite(
                    target, category if desired else None, note,
                    {"proxy_url": proxy_url},
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("eh_favorite_error", (desired, label, str(exc))))
                return
            self.ui_queue.put(("eh_favorite_done", (desired, label, result)))

        threading.Thread(target=worker, name="eh-favorite-management", daemon=True).start()

    def _finish_eh_favorite(self, payload, *, error: bool) -> None:
        desired, label, detail = payload
        action = "加入收藏" if desired else "移出收藏"
        if error:
            self.status_var.set(f"EH {action}失败")
            self._append_log(f"EH {action} {label} 失败：{detail}")
            messagebox.showerror(f"EH {action}失败", str(detail), parent=self)
        else:
            changed = bool(detail.get("changed")) if isinstance(detail, dict) else False
            message = f"已将 {label} {action}" if changed else f"{label} 已经处于目标收藏状态，无需重复操作"
            self.status_var.set(message)
            self._append_log(message)
        self._candidate_selection_changed()

    def _candidate_selection_changed(self, _event=None) -> None:
        rows = self._selected_platform_candidates()
        row = rows[0] if rows else None
        enabled = bool(rows)
        single = len(rows) == 1
        for button in (self.candidate_use_button, self.candidate_preview_button):
            button.state(["!disabled"] if single else ["disabled"])
        batch_targets = self._candidate_workbench_targets(rows)
        for button in (self.candidate_fill_batch_button, self.candidate_batch_info_button):
            button.state(["!disabled"] if batch_targets else ["disabled"])
        adapter = self._selected_adapter()
        can_download = (
            enabled and all(bool(item.get("downloadable", True)) for item in rows)
            and "download" in adapter.info.capabilities and adapter.info.stage != "planned"
        )
        self.candidate_download_button.state(["!disabled"] if can_download else ["disabled"])
        jm_selected = self.module_var.get() == "jmcomic" and enabled
        self.jm_candidate_tags_button.state(["!disabled"] if jm_selected else ["disabled"])
        self.jm_candidate_remove_button.state(["!disabled"] if jm_selected else ["disabled"])
        bluesky_profile = (
            self.module_var.get() == "bluesky"
            and single
            and str(row.get("input_kind") or "") == "profile"
            and "/post/" not in str(row.get("target") or "")
        ) if row else False
        for button in (self.bluesky_follow_button, self.bluesky_unfollow_button):
            button.state(["!disabled"] if bluesky_profile else ["disabled"])
        eh_gallery = (
            self.module_var.get() == "ehentai"
            and single
            and str(row.get("input_kind") or "") == "gallery"
        ) if row else False
        for button in (self.eh_favorite_button, self.eh_unfavorite_button):
            button.state(["!disabled"] if eh_gallery else ["disabled"])
        author_keys = [self._candidate_author_key(item) for item in rows]
        self.candidate_block_author_button.state(["!disabled"] if any(author_keys) else ["disabled"])
        self.candidate_block_work_button.state(["!disabled"] if any(
            work_from_target(str(item.get("module_id") or self.module_var.get()), str(item.get("target") or ""),
                             str(item.get("input_kind") or "")) for item in rows) else ["disabled"])
        if not row:
            suffix = f"；黑名单已排除 {self._blocked_candidate_count} 项" if getattr(self, "_blocked_candidate_count", 0) else ""
            self.candidate_selection_var.set("尚未选择候选；可用 Ctrl/Shift 多选" + suffix)
            return
        target = str(row.get("target") or "").strip()
        name = str(row.get("name") or "").strip()
        if len(rows) == 1:
            read_only = str(row.get("read_only_reason") or "").strip()
            self.candidate_selection_var.set(
                f"已选：{name or target}  →  {target}" + (f"；只读：{read_only}" if read_only else "")
            )
        else:
            self.candidate_selection_var.set(f"已选 {len(rows)} 项；可批量获取资料、填入工作台或加入队列")

    def _candidate_author_key(self, row: dict):
        module_id = str(row.get("module_id") or self.module_var.get())
        author_id = str(row.get("author_id") or "").strip()
        if module_id == "pixiv" and author_id:
            return "pixiv", author_id
        return account_from_target(module_id, str(row.get("target") or ""), str(row.get("input_kind") or ""))

    def _candidate_workbench_targets(self, rows: list[dict]) -> tuple[str, list[str]] | None:
        if not rows:
            return None
        module_ids = set()
        targets = []
        for row in rows:
            module_id = str(row.get("module_id") or self.module_var.get())
            target = str(row.get("target") or "").strip()
            if module_id == "google_image" and target.startswith(("http://", "https://")):
                module_id = "website"
            if not target:
                continue
            module_ids.add(module_id)
            targets.append(target)
        if len(module_ids) != 1 or not targets:
            return None
        return next(iter(module_ids)), list(dict.fromkeys(targets))

    def _fill_targets_into_workbench(self, module_id: str, targets: list[str]) -> None:
        if module_id != self.module_var.get():
            self._select_module(module_id)
        display_value = " / ".join(targets)
        self.target_var.set(display_value)
        self._prefilled_task_targets = (module_id, display_value, list(targets))
        self.target_entry.focus_set()
        self.target_entry.selection_range(0, tk.END)
        self.status_var.set(f"已将 {len(targets)} 个目标填入工作台；可批量获取资料或加入下载队列")

    def _fill_selected_candidates(self) -> None:
        selected = self._candidate_workbench_targets(self._selected_platform_candidates())
        if not selected:
            messagebox.showwarning("无法批量填入", "请选中同一平台的有效目标")
            return
        self._fill_targets_into_workbench(*selected)

    def _fetch_selected_candidate_info(self) -> None:
        selected = self._candidate_workbench_targets(self._selected_platform_candidates())
        if not selected:
            messagebox.showwarning("无法批量获取资料", "请选中同一平台的有效目标")
            return
        self._fill_targets_into_workbench(*selected)
        self._batch_fetch_target_info()

    def _block_selected_candidate_authors(self) -> None:
        keys = list(dict.fromkeys(key for row in self._selected_platform_candidates() if (key := self._candidate_author_key(row))))
        if not keys or not messagebox.askyesno("屏蔽作者", f"屏蔽选中的 {len(keys)} 个作者账号？"):
            return
        for key in keys:
            self.manager.blocklist.add_group(key)
        self._refresh_platform_tabs()
        self._refresh_google_blocklist()
        self._update_blocklist_status()

    def _block_selected_candidate_works(self) -> None:
        rows = [row for row in self._selected_platform_candidates() if work_from_target(
            str(row.get("module_id") or self.module_var.get()), str(row.get("target") or ""),
            str(row.get("input_kind") or ""))]
        if not rows or not messagebox.askyesno("屏蔽作品", f"屏蔽选中的 {len(rows)} 个具体作品或页面？"):
            return
        for row in rows:
            self.manager.blocklist.add_work(str(row.get("module_id") or self.module_var.get()),
                                            str(row.get("target") or ""), input_kind=str(row.get("input_kind") or ""))
        self._refresh_platform_tabs()
        self._refresh_google_blocklist()
        self._update_blocklist_status()

    def _use_selected_platform_candidate(self) -> None:
        row = self._selected_platform_candidate()
        if not row:
            messagebox.showwarning("未选择候选", "请先选择一个作者、作品或页面")
            return
        module_id = str(row.get("module_id") or self.module_var.get())
        target = str(row.get("target") or "").strip()
        if module_id == "google_image" and target.startswith(("http://", "https://")):
            module_id = "website"
        if module_id != self.module_var.get():
            self._select_module(module_id)
        self.target_var.set(target)
        self.target_entry.focus_set()
        self.target_entry.selection_range(0, tk.END)
        self.status_var.set(f"已填入目标：{target}")

    def _use_double_clicked_platform_candidate(self, event) -> str:
        if select_treeview_row_at_event(self.following_tree, event):
            self._use_selected_platform_candidate()
        return "break"

    def _preview_selected_platform_candidate(self) -> None:
        row = self._selected_platform_candidate()
        if not row:
            messagebox.showwarning("未选择候选", "请先选择一个作者、作品或页面")
            return
        module_id = str(row.get("module_id") or self.module_var.get())
        target = str(row.get("target") or "").strip()
        if module_id == "google_image" and target.startswith(("http://", "https://")):
            module_id = "website"
        if module_id != self.module_var.get():
            self._select_module(module_id)
        self.target_var.set(target)
        self._preview_online()

    def _download_selected_platform_candidate(self) -> None:
        rows = self._selected_platform_candidates()
        if not rows:
            messagebox.showwarning("未选择候选", "请先选择一个作者、作品或页面")
            return
        self._queue_download_rows(rows)

    def _queue_download_rows(self, rows: list[dict]) -> None:
        """Queue one or many concrete targets from either discovery list."""
        imported_rows = [row for row in rows if row.get("import_source") or row.get("source") == "file_import"]
        if imported_rows and not messagebox.askyesno(
            "确认下载文件导入的账号",
            f"选中项中有 {len(imported_rows)} 个账号来自本地文件，软件尚未联网核实这些用户名。\n\n"
            "不存在的用户名会导致任务失败；如果误写成另一个真实用户名，则可能下载那个错误账号的内容。\n"
            "建议先用“在线获取资料”核对。仍要加入下载队列吗？",
        ):
            self.status_var.set("已取消加入队列；文件导入账号尚未下载")
            return
        callbacks = self._task_callbacks()
        created = 0
        created_task_ids: list[str] = []
        failures: list[str] = []
        module_ids: set[str] = set()
        for row in rows:
            module_id = str(row.get("module_id") or self.module_var.get())
            target = str(row.get("target") or "").strip()
            if not target:
                continue
            if not bool(row.get("downloadable", True)):
                failures.append(f"{target}: {row.get('read_only_reason') or '该候选当前只读'}")
                continue
            module_id = self._download_module_for_target(module_id, target)
            try:
                adapter = self.manager.get_adapter(module_id)
                if "download" not in adapter.info.capabilities or adapter.info.stage == "planned":
                    raise ValueError(f"{adapter.display_name} 当前还不能下载")
                task = self.manager.start_task(
                    module_id,
                    target,
                    Path(self.output_dir_var.get()),
                    {
                        **self._current_target_options(target),
                        **({"input_kind": row.get("input_kind")} if row.get("input_kind") else {}),
                        **({"author_id": row.get("author_id")} if row.get("author_id") else {}),
                        **({"jm_candidate_tags": row.get("tags") or []} if module_id == "jmcomic" else {}),
                    },
                    callbacks,
                )
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{target}: {exc}")
                continue
            created += 1
            created_task_ids.append(task.task_id)
            module_ids.add(module_id)
            self.current_task_id = task.task_id
            self.current_task_ids.add(task.task_id)
            self._append_log(f"预选项已加入下载队列: {task.task_id} | {target}")
        if not created:
            messagebox.showerror("加入失败", failures[0] if failures else "没有候选成功加入下载队列")
            return
        concurrency = ", ".join(
            f"{self.manager.get_adapter(module_id).display_name} {self.manager.get_concurrency_limits(module_id)[0]} 路"
            for module_id in sorted(module_ids)
        )
        self.status_var.set(f"已加入 {created} 个下载任务；并发上限：{concurrency}")
        if failures:
            self._append_log(f"另有 {len(failures)} 个候选加入失败：{failures[0]}")
        self.notebook.select(self.tasks_tab)
        self._refresh_tasks()
        self._follow_task_batch(created_task_ids)

    def _load_platform_history(self) -> None:
        if self.module_var.get() == "twitter":
            self._load_twitter_history()
        else:
            self._populate_platform_history(self.module_var.get())
            self.status_var.set(f"已读取 {self._selected_adapter().display_name} 历史 {len(self.platform_history_rows)} 条")

    def _selected_platform_history(self) -> dict | None:
        rows = self._selected_platform_histories()
        return rows[0] if rows else None

    def _selected_platform_histories(self) -> list[dict]:
        rows: list[dict] = []
        history_rows = self.twitter_history_rows if self.module_var.get() == "twitter" else self.platform_history_rows
        for item_id in self.twitter_history_tree.selection():
            try:
                index = int(item_id)
            except ValueError:
                continue
            if index < len(history_rows):
                rows.append(history_rows[index])
        return rows

    def _use_double_clicked_platform_history(self, event) -> str:
        if select_treeview_row_at_event(self.twitter_history_tree, event):
            self._use_selected_platform_history()
        return "break"

    def _history_selection_changed(self, _event=None) -> None:
        rows = self._selected_platform_histories()
        single = len(rows) == 1
        self.history_use_button.state(["!disabled"] if single else ["disabled"])
        self.history_preview_button.state(["!disabled"] if single else ["disabled"])
        self.history_clear_selected_button.state(["!disabled"] if rows else ["disabled"])
        self.history_fill_batch_button.state(["!disabled"] if self._history_workbench_targets(rows) else ["disabled"])
        if not rows:
            self.history_selection_var.set("尚未选择历史记录；可用 Ctrl/Shift 多选")
        elif single:
            row = rows[0]
            self.history_selection_var.set(f"已选：{row.get('name') or row.get('target') or '-'}")
        else:
            self.history_selection_var.set(f"已选 {len(rows)} 条历史记录，可批量清理")

    def _use_selected_platform_history(self) -> None:
        row = self._selected_platform_history()
        if not row:
            messagebox.showwarning("未选择历史", "请先选择一条本平台历史记录")
            return
        self._apply_history_scope(row)
        target = self._history_row_target(row)
        if not target:
            return
        self.target_var.set(target)
        self._preview_target()

    def _history_workbench_targets(self, rows: list[dict]) -> tuple[str, list[str]] | None:
        if not rows:
            return None
        module_id = self.module_var.get()
        if module_id != "twitter":
            scopes = {str(row.get("handle") or "").strip() for row in rows if str(row.get("handle") or "").strip()}
            if len(scopes) > 1:
                return None
        targets = [self._history_row_target(row) for row in rows]
        targets = list(dict.fromkeys(target.strip() for target in targets if target and target.strip()))
        return (module_id, targets) if targets else None

    def _fill_selected_histories(self) -> None:
        rows = self._selected_platform_histories()
        selected = self._history_workbench_targets(rows)
        if not selected:
            messagebox.showwarning("无法批量填入", "请选中一条或多条有效历史记录；非 Twitter 历史需属于同一内容种类")
            return
        if self.module_var.get() != "twitter" and rows:
            self._apply_history_scope(rows[0])
        self._fill_targets_into_workbench(*selected)

    def _preview_selected_platform_history(self) -> None:
        row = self._selected_platform_history()
        if not row:
            messagebox.showwarning("未选择历史", "请先选择一条本平台历史记录")
            return
        self._apply_history_scope(row)
        target = self._history_row_target(row)
        if target:
            self.target_var.set(target)
            self._preview_online()

    def _apply_history_scope(self, row: dict) -> None:
        if self.module_var.get() == "twitter":
            return
        scope = str(row.get("handle") or "").strip()
        valid = PLATFORM_CONTENT_SCOPES.get(self.module_var.get(), ())
        if scope in valid:
            self.content_scope_var.set(scope)
            self.platform_scope_selection[self.module_var.get()] = scope

    def _clear_platform_history(self) -> None:
        if self.module_var.get() == "twitter":
            self._clear_twitter_download_records()
        else:
            self._clear_current_module_history()
            self._populate_platform_history(self.module_var.get())

    def _clear_selected_platform_history(self) -> None:
        rows = self._selected_platform_histories()
        if not rows:
            messagebox.showwarning("未选择历史", "请先选择一条或多条历史记录")
            return
        module_id = self.module_var.get()
        if not messagebox.askyesno(
            "清理选中历史",
            f"清理选中的 {len(rows)} 条历史及对应任务/文件索引？\n\n本地已下载文件不会删除。",
        ):
            return

        json_counts: dict[str, int] = {}
        if module_id == "twitter":
            json_counts = self.manager.get_adapter("twitter").clear_selected_download_records(rows)
        elif module_id == "pixiv":
            account_targets = {
                str(row.get("target") or "") for row in rows if row.get("source") == "账号浏览历史"
            }
            if account_targets:
                json_counts["account_history"] = self.manager.get_adapter("pixiv").clear_account_history(account_targets)

        task_ids = {str(row.get("task_id") or "") for row in rows if str(row.get("task_id") or "")}
        if module_id == "twitter":
            selected_handles = {
                str(row.get("handle") or "").strip().lstrip("@").casefold()
                for row in rows
                if str(row.get("handle") or "").strip()
            }
            selected_targets = {str(row.get("target") or "").strip().casefold() for row in rows}
            for task_row in self.storage.list_tasks(limit=10000):
                item = dict(task_row)
                if str(item.get("module_id") or "") != "twitter":
                    continue
                target = str(item.get("normalized_target") or item.get("target") or "").strip()
                handle = self._target_to_handle(target).casefold()
                if (handle and handle in selected_handles) or target.casefold() in selected_targets:
                    task_ids.add(str(item.get("task_id") or ""))
        task_ids.discard("")

        active = set(self.manager.active_task_ids()) & task_ids
        inactive = task_ids - active
        db_counts = self.storage.delete_task_records(inactive)
        for task_id in active:
            self.pending_task_deletions.add(task_id)
            self.manager.cancel(task_id)

        self._refresh_tasks()
        self._refresh_library(scan=False)
        self._load_platform_history()
        self.status_var.set(
            f"已清理 {len(rows)} 条选中历史"
            + (f"；{len(active)} 个运行中任务停止后移除" if active else "")
        )
        self._append_log(f"清理选中历史: module={module_id}, json={json_counts}, db={db_counts}")

    def _import_following_accounts(self) -> None:
        selected = filedialog.askopenfilename(
            title="导入 Twitter 关注列表",
            filetypes=[
                ("关注列表", "*.json *.csv *.txt *.list"),
                ("JSON", "*.json"),
                ("CSV", "*.csv"),
                ("文本账号列表", "*.txt *.list"),
                ("所有文件", "*.*"),
            ],
        )
        if not selected:
            return
        adapter = self.manager.get_adapter("twitter")
        try:
            preview = adapter.preview_following_import(Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "文件未使用",
                self._friendly_error(exc, "关注列表预检") + "\n\n原有本地关注缓存没有修改。",
            )
            return
        invalid = int(preview["invalid_count"])
        samples = preview.get("invalid_samples") or []
        invalid_text = ""
        if invalid:
            invalid_text = f"\n无效并跳过：{invalid} 条"
            if samples:
                invalid_text += "\n示例：" + "；".join(str(item) for item in samples)
        prompt = (
            f"文件：{preview['path']}\n\n"
            f"读取：{preview['source_count']} 条\n"
            f"有效去重：{preview['valid_count']} 条\n"
            f"将新增：{preview['added']} 条\n"
            f"将更新同名账号资料：{preview['updated']} 条"
            f"{invalid_text}\n\n"
            "这是合并更新：不会删除原有账号；写入前会自动备份当前缓存。\n"
            "文件导入无法联网核实用户名。拼写合法但错误的账号会出现在候选中；若直接加入下载队列，"
            "不存在的账号会失败，恰好存在的错误账号则可能下载到错误内容。\n\n"
            "确认使用这个文件更新本地关注列表吗？"
        )
        if not messagebox.askyesno("确认从文件更新 Twitter 关注", prompt):
            self.status_var.set("已取消从文件更新；本地关注缓存未修改")
            return
        try:
            summary = adapter.import_following_accounts(Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导入失败", self._friendly_error(exc, "关注列表导入"))
            return
        self._load_following_accounts()
        message = (
            f"文件更新完成：新增 {summary['added']}，更新 {summary['updated']}，"
            f"跳过 {summary['skipped']}，当前共 {summary['total']} 个"
        )
        self.status_var.set(message)
        self._append_log(message)
        backup = str(summary.get("backup") or "")
        messagebox.showinfo(
            "Twitter 关注已更新",
            message
            + (f"\n\n更新前备份：{backup}" if backup else "\n\n此前没有本地缓存，因此未生成备份。")
            + "\n\n导入结果标记为“部分缓存”；需要核对取关时请执行一次成功的“更新全部”。",
        )

    def _import_pixiv_cookie(self) -> None:
        selected = filedialog.askopenfilename(
            title="导入 Pixiv Cookie JSON",
            filetypes=[("JSON Cookie", "*.json"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            count = self.manager.get_adapter("pixiv").import_cookie_file(Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Pixiv Cookie 导入失败", str(exc))
            return
        message = f"Pixiv/FANBOX Cookie 已安全保存（{count} 项）；登录限定功能将在访问时验证"
        self.status_var.set(message)
        self._append_log(message)

    def _import_jmcomic_cookie(self) -> None:
        selected = filedialog.askopenfilename(
            title="导入 JMComic Cookie 或请求标头",
            filetypes=[("Cookie / 请求标头", "*.json *.txt"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            adapter = self.manager.get_adapter("jmcomic")
            count = adapter.import_cookie_file(Path(selected))
            cookie_status = adapter.cookie_status()
            header_count = len(adapter.browser_headers())
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("JMComic Cookie 导入失败", str(exc))
            return
        profile = f"；已保存 {header_count} 个浏览器请求头" if header_count else ""
        message = f"JMComic Cookie 已安全保存（{count} 项）{profile}；{cookie_status['message']}"
        self._refresh_jm_auth_status()
        self.status_var.set(message)
        self._append_log(message)

    def _import_instagram_cookie(self) -> None:
        selected = filedialog.askopenfilename(
            title="导入 Instagram Cookie",
            filetypes=[("Cookie 文件", "*.json *.txt"), ("所有文件", "*.*")],
        )
        if not selected:
            return
        try:
            adapter = self.manager.get_adapter("instagram")
            count = adapter.import_cookie_file(Path(selected))
            status = adapter.cookie_status()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Instagram Cookie 导入失败", str(exc))
            return
        message = f"Instagram Cookie 已安全保存（{count} 项）；{status['message']}"
        self.status_var.set(message)
        self._append_log(message)

    def _export_pixiv_following_accounts(self) -> None:
        if not self.pixiv_following_rows:
            messagebox.showwarning("没有关注画师", "请先读取本地缓存或更新 Pixiv 关注画师")
            return
        selected = filedialog.asksaveasfilename(
            title="导出 Pixiv 关注画师",
            defaultextension=".csv",
            initialfile="pixiv-following.csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
        )
        if not selected:
            return
        try:
            count = self.manager.get_adapter("pixiv").export_following_accounts(Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", self._friendly_error(exc, "Pixiv 关注导出"))
            return
        self.status_var.set(f"已导出 Pixiv 关注画师 {count} 个")
        self._append_log(f"Pixiv 关注画师已导出：{selected}（{count} 个）")

    def _export_following_accounts(self) -> None:
        if not self.following_rows:
            messagebox.showwarning("没有关注列表", "请先读取、刷新或导入关注账号")
            return
        selected = filedialog.asksaveasfilename(
            title="导出 Twitter 关注列表",
            defaultextension=".csv",
            initialfile="twitter-following.csv",
            filetypes=[("CSV", "*.csv"), ("JSON", "*.json")],
        )
        if not selected:
            return
        try:
            count = self.manager.get_adapter("twitter").export_following_accounts(Path(selected))
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", self._friendly_error(exc, "关注列表导出"))
            return
        message = f"已导出 {count} 个关注账号：{selected}"
        self.status_var.set(message)
        self._append_log(message)

    def _load_following_accounts(self) -> None:
        if not hasattr(self, "following_tree"):
            return
        adapter = self.manager.get_adapter("twitter")
        self.following_rows = adapter.load_following_accounts()
        for item in self.following_tree.get_children():
            self.following_tree.delete(item)
        for index, row in enumerate(self.following_rows):
            handle = row.get("handle", "")
            display_name = row.get("display_name", "")
            bio = str(row.get("bio", "")).replace("\n", " / ")
            self.following_tree.insert("", tk.END, iid=str(index), values=(f"@{handle}", display_name, bio))
        if self.following_rows:
            self.status_var.set(f"已加载关注作者 {len(self.following_rows)} 个")
        else:
            self.status_var.set("本地关注缓存中没有可用的 Twitter 账号")
        self._refresh_target_browser()
        if self.module_var.get() == "twitter":
            self._refresh_platform_tabs()

    def _selected_following_handle(self) -> str:
        selection = self.following_tree.selection()
        if not selection:
            return ""
        index = int(selection[0])
        if index >= len(self.following_rows):
            return ""
        return str(self.following_rows[index].get("handle", "")).strip().lstrip("@")

    def _select_twitter_module(self) -> None:
        self._select_module("twitter")

    def _use_selected_following(self) -> None:
        handle = self._selected_following_handle()
        if not handle:
            messagebox.showwarning("未选择作者", "请先选择一个关注作者")
            return
        self._select_twitter_module()
        self.target_var.set(f"@{handle}")
        self.status_var.set(f"已选择 @{handle}")
        self._preview_target()

    def _download_selected_following(self) -> None:
        handle = self._selected_following_handle()
        if not handle:
            messagebox.showwarning("未选择作者", "请先选择一个关注作者")
            return
        self._select_twitter_module()
        self.target_var.set(f"@{handle}")
        self._start_download()


    def _preview_selected_following(self) -> None:
        self._use_selected_following()

    def _refresh_all_platform_following(self) -> None:
        if self.module_var.get() == "pixiv" and self.content_scope_var.get() == "关注画师":
            self._refresh_pixiv_following_accounts("all")
        else:
            self._refresh_following_accounts("all")

    def _refresh_all_following_accounts(self) -> None:
        self._refresh_all_platform_following()

    def _refresh_following_accounts(self, update_mode: str = "new") -> None:
        if self.following_refreshing and self.following_cancel_event is not None:
            self.following_cancel_event.set()
            self.candidate_refresh_var.set("正在停止…")
            self.status_var.set("正在停止刷新；已抓到的关注账号会保留")
            return
        self._select_twitter_module()
        self.following_refreshing = True
        self.following_progress_var.set(0.0)
        self.following_refresh_mode = "all" if update_mode == "all" else "new"
        self.following_cancel_event = threading.Event()
        cancel_event = self.following_cancel_event
        self.candidate_refresh_var.set("停止更新")
        self.following_full_refresh_button.state(["disabled"])
        if self.following_refresh_mode == "new":
            self.status_var.set("正在更新新的关注；连续匹配旧账号后会自动完成")
            self._append_log("开始更新新的 Twitter 关注账号（按 handle 匹配，不依赖列表位置）")
        else:
            self.status_var.set("正在更新全部关注；滚动到底后会自动完成")
            self._append_log("开始更新全部 Twitter 关注账号（完成后同步移除已取关账号）")

        def worker() -> None:
            try:
                payload = self.manager.get_adapter("twitter").fetch_following(
                    timeout_seconds=0,
                    update_mode=self.following_refresh_mode,
                    known_overlap=30,
                    cancel_event=cancel_event,
                    on_status=lambda message: self.ui_queue.put(("following_status", message)),
                    on_accounts=lambda payload: self.ui_queue.put(("following_partial", payload)),
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("following_error", str(exc)))
                return
            self.ui_queue.put(("following_loaded", payload))

        threading.Thread(target=worker, name="twitter-following-refresh", daemon=True).start()

    def _pixiv_following_owner_id(self) -> str:
        value = self.target_var.get().strip()
        owner_id = ""
        if value.isdigit():
            owner_id = value
        elif value.startswith(("http://", "https://")):
            parts = [part for part in urlparse(value).path.split("/") if part]
            if "following" in parts and "users" in parts:
                user_index = parts.index("users")
                if user_index + 1 < len(parts) and parts[user_index + 1].isdigit():
                    owner_id = parts[user_index + 1]
        if not owner_id:
            saved = str(self.storage.get_setting("pixiv_following_user_id", "") or "").strip()
            owner_id = saved if saved.isdigit() else ""
        if not owner_id:
            owner_id = str(self.manager.get_adapter("pixiv").cookie_account_id() or "").strip()
        if not owner_id:
            raise ValueError("请输入 Pixiv 关注页 URL 或当前账号用户 ID；现有 Cookie 也无法识别账号 ID")
        self.storage.set_setting("pixiv_following_user_id", owner_id)
        return owner_id

    def _load_pixiv_following_accounts(self) -> None:
        if not hasattr(self, "following_tree"):
            return
        adapter = self.manager.get_adapter("pixiv")
        self.pixiv_following_rows = adapter.load_following_accounts()
        self._refresh_target_browser()
        if self.module_var.get() == "pixiv":
            self._refresh_platform_tabs()
        info = adapter.following_cache_info()
        state = "部分缓存" if info.get("partial") else "完整缓存"
        self.following_progress_status_var.set(f"本地 {state} · {len(self.pixiv_following_rows)} 个")
        self.status_var.set(f"已从本地读取 Pixiv 关注画师 {len(self.pixiv_following_rows)} 个（{state}）")

    def _refresh_pixiv_following_accounts(self, update_mode: str = "new") -> None:
        if self.following_refreshing and self.following_cancel_event is not None:
            self.following_cancel_event.set()
            self.candidate_refresh_var.set("正在停止…")
            self.status_var.set("正在停止 Pixiv 关注刷新；已有结果会保留")
            return
        try:
            owner_id = self._pixiv_following_owner_id()
        except ValueError as exc:
            messagebox.showwarning("缺少 Pixiv 账号", str(exc))
            self.status_var.set("请先填写 Pixiv 关注页 URL 或账号用户 ID")
            return
        self.following_refreshing = True
        cached_count = len(self.manager.get_adapter("pixiv").load_following_accounts())
        self.following_progress_var.set(2.0)
        self.following_refresh_mode = "all" if update_mode == "all" else "new"
        self.following_cancel_event = threading.Event()
        cancel_event = self.following_cancel_event
        self.candidate_refresh_var.set("停止更新")
        self.following_full_refresh_button.state(["disabled"])
        action = "更新全部" if self.following_refresh_mode == "all" else "更新新的"
        pixiv_proxy_url = str(self.storage.get_setting("proxy_url", "") or "")
        self.following_progress_status_var.set(f"{action} · 等待第 1 页 · 本地已有 {cached_count} 个")
        self.status_var.set(f"正在{action} Pixiv 关注画师：用户 {owner_id}")
        self._append_log(
            f"开始{action} Pixiv 关注画师（分页完整读取；用户 ID {owner_id}；"
            + ("完整成功后同步取关" if self.following_refresh_mode == "all" else "合并新增且不删除旧缓存")
            + "）"
        )
        self._append_log("Pixiv 网络方式：使用设置中的代理" if pixiv_proxy_url else "Pixiv 网络方式：直接连接")

        def worker() -> None:
            try:
                payload = self.manager.get_adapter("pixiv").fetch_following(
                    owner_id,
                    update_mode=self.following_refresh_mode,
                    visibility=str(self.storage.get_setting("pixiv_visibility", "show") or "show"),
                    cancel_event=cancel_event,
                    on_status=lambda message: self.ui_queue.put(("pixiv_following_status", message)),
                    on_accounts=lambda result: self.ui_queue.put(("pixiv_following_partial", result)),
                    options={"proxy_url": pixiv_proxy_url},
                )
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("pixiv_following_error", str(exc)))
                return
            self.ui_queue.put(("pixiv_following_loaded", payload))

        threading.Thread(target=worker, name="pixiv-following-refresh", daemon=True).start()


    def _load_twitter_history(self) -> None:
        if not hasattr(self, "twitter_history_tree"):
            return
        output_dir = self.output_dir_var.get()

        def worker() -> None:
            try:
                adapter = self.manager.get_adapter("twitter")
                rows = adapter.downloaded_users(Path(output_dir))
                self.ui_queue.put(("twitter_history_loaded", rows))
            except Exception as exc:  # noqa: BLE001
                self.ui_queue.put(("twitter_history_error", str(exc)))

        threading.Thread(target=worker, name="twitter-history-load", daemon=True).start()

    def _apply_twitter_history(self, rows: list[dict]) -> None:
        if not hasattr(self, "twitter_history_tree"):
            return
        self.twitter_history_rows = rows
        for item in self.twitter_history_tree.get_children():
            self.twitter_history_tree.delete(item)
        for index, row in enumerate(self.twitter_history_rows):
            handle = str(row.get("handle") or "").strip()
            self.twitter_history_tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    row.get("name") or handle or row.get("target") or "-",
                    f"@{handle}" if handle else "-",
                    row.get("count") or row.get("local_files") or 0,
                    row.get("latest") or "-",
                    row.get("source") or "-",
                ),
            )
        self._refresh_target_browser()
        if self.module_var.get() == "twitter":
            self._refresh_platform_tabs()

    def _selected_history_row(self) -> dict | None:
        selection = self.twitter_history_tree.selection()
        if not selection:
            return None
        index = int(selection[0])
        rows = self.twitter_history_rows if self.module_var.get() == "twitter" else self.platform_history_rows
        if index >= len(rows):
            return None
        return rows[index]

    def _history_row_target(self, row: dict) -> str:
        handle = str(row.get("handle") or "").strip().lstrip("@")
        if str(row.get("module_id") or "") == "twitter" and handle:
            return f"@{handle}"
        target = str(row.get("target") or "").strip()
        if target:
            return target
        return str(row.get("name") or "").strip()

    def _use_selected_history(self) -> None:
        row = self._selected_history_row()
        if not row:
            messagebox.showwarning("未选择作者", "请先选择一个下载历史作者")
            return
        target = self._history_row_target(row)
        if not target:
            return
        module_id = str(row.get("module_id") or self.module_var.get() or "twitter")
        self._select_module(module_id)
        self.target_var.set(target)
        self.status_var.set(f"已选择 {target}")
        self._preview_target()

    def _preview_selected_history(self) -> None:
        self._use_selected_history()
    def _clear_current_module_history(self) -> None:
        module_id = self.module_var.get()
        if not messagebox.askyesno("清理记录", "只清理任务、日志和文件索引，不删除已下载文件。"):
            return
        if module_id == "pixiv":
            self.manager.get_adapter("pixiv").clear_account_history()
        task_ids = {
            str(row["task_id"])
            for row in self.storage.list_tasks(limit=10000)
            if str(row["module_id"]) == module_id
        }
        active = task_ids & set(self.manager.active_task_ids())
        counts = self.storage.delete_task_records(task_ids - active)
        for task_id in active:
            self.pending_task_deletions.add(task_id)
            self.manager.cancel(task_id)
        self._refresh_tasks()
        self._refresh_library(scan=False)
        self._append_log(f"已清理 {module_id} 记录: {counts}")
        self.status_var.set("记录已清理" + (f"；{len(active)} 个运行中任务停止后移除" if active else ""))

    def _filter_pixiv_bookmark_rows(self, rows: list[dict]) -> list[dict]:
        return filter_bookmark_candidates(
            rows,
            query=self.bookmark_filter_query_var.get(),
            field=self.bookmark_filter_field_var.get(),
            date_basis=self.bookmark_date_basis_var.get(),
            start_date=self.bookmark_filter_start_var.get().strip(),
            end_date=self.bookmark_filter_end_var.get().strip(),
        )

    def _apply_bookmark_filters(self) -> None:
        if self.module_var.get() != "pixiv" or self.content_scope_var.get() not in {"作品收藏", "小说收藏"}:
            return
        for label, value in (("开始日期", self.bookmark_filter_start_var.get().strip()),
                             ("结束日期", self.bookmark_filter_end_var.get().strip())):
            if value:
                try:
                    date.fromisoformat(value)
                except ValueError:
                    messagebox.showwarning("Pixiv 日期无效", f"{label}请使用 YYYY-MM-DD")
                    return
        start = self.bookmark_filter_start_var.get().strip()
        end = self.bookmark_filter_end_var.get().strip()
        if start and end and start > end:
            messagebox.showwarning("Pixiv 日期无效", "开始日期不能晚于结束日期")
            return
        self._refresh_platform_tabs()
        self.status_var.set(f"本地收藏筛选后显示 {len(self.current_candidate_rows)} 个")

    def _current_target_options(self, target: str = "") -> dict:
        module_id = self.module_var.get()
        scope = self.content_scope_var.get()
        options = {
            "types": self._selected_types(),
            **self._saved_format_options(),
            **self._saved_archive_options(),
            "content_scope": scope,
            "search_mode": scope,
            "proxy_url": str(self.storage.get_setting("proxy_url", "") or ""),
            "retries": _bounded_int(self.storage.get_setting("task_retries", 1), 1, 0, 5),
            "page_read_timeout": _bounded_int(self.storage.get_setting("webpage_read_timeout", 60), 60, 30, 600),
        }
        if target and module_id == "pixiv":
            options["max_works"] = _bounded_int(
                self.storage.get_setting("pixiv_max_works", 20), 20, 1, 100
            )

            options["filter_ai"] = bool(self.storage.get_setting("pixiv_filter_ai", True))
            if scope in {"作者 ID", "关注画师"}:
                options["input_kind"] = "user"
            elif scope == "作品 ID":
                options["input_kind"] = "work"
            elif scope == "漫画系列":
                options["input_kind"] = "manga_series"
            elif scope == "小说 ID":
                options["input_kind"] = "novel"
            elif scope == "作者小说":
                options["input_kind"] = "user_novels"
            elif scope == "小说系列":
                options["input_kind"] = "novel_series"
            elif scope == "排行榜":
                options["input_kind"] = "ranking"
            elif scope == "新作":
                options["input_kind"] = "new"
            elif scope == "作品收藏":
                options["input_kind"] = "bookmark"
                options["account_id"] = str(self.manager.get_adapter("pixiv").cookie_account_id() or "")
            elif scope == "小说收藏":
                options["input_kind"] = "novel_bookmark"
                options["account_id"] = str(self.manager.get_adapter("pixiv").cookie_account_id() or "")
            elif scope == "浏览历史（Premium）":
                options["input_kind"] = "history"
                options["account_id"] = str(self.manager.get_adapter("pixiv").cookie_account_id() or "")
            elif scope == "作者 + 标签":
                options["input_kind"] = "author_tag"
            elif scope == "FANBOX":
                options["input_kind"] = "fanbox"
            elif scope == "Sketch":
                options["input_kind"] = "sketch"
            options["pixiv_visibility"] = str(self.storage.get_setting("pixiv_visibility", "show") or "show")
            options["start_date"] = str(self.storage.get_setting("pixiv_start_date", "") or "")
            options["end_date"] = str(self.storage.get_setting("pixiv_end_date", "") or "")
            if scope in {"作品收藏", "小说收藏"} and self.__dict__.get("bookmark_filter_start_var") is not None:
                options["start_date"] = self.bookmark_filter_start_var.get().strip()
                options["end_date"] = self.bookmark_filter_end_var.get().strip()
            options["minimum_bookmarks"] = _bounded_int(
                self.storage.get_setting("pixiv_minimum_bookmarks", 0), 0, 0, 100000000
            )
            options["age_mode"] = str(self.storage.get_setting("pixiv_age_mode", "all") or "all")
            options["bookmark_tag"] = str(self.storage.get_setting("pixiv_bookmark_tag", "") or "")
            options["bookmark_date_basis"] = (
                "published"
                if self.__dict__.get("bookmark_date_basis_var") is not None
                and self.bookmark_date_basis_var.get() == "发布时间"
                else "bookmarked"
            )
            options["author_match_mode"] = str(self.storage.get_setting("pixiv_author_match_mode", "partial") or "partial")
            options["history_limit"] = _bounded_int(
                self.storage.get_setting("pixiv_history_limit", 100), 100, 1, 10000
            )
            if target.startswith(("http://", "https://")):
                kind_scopes = {
                    "work": "作品 ID", "user": "作者 ID", "user_novels": "作者小说",
                    "manga_series": "漫画系列", "novel": "小说 ID", "novel_series": "小说系列",
                    "bookmark": "作品收藏", "novel_bookmark": "小说收藏", "history": "浏览历史（Premium）",
                    "fanbox": "FANBOX", "sketch": "Sketch",
                }
                try:
                    parsed_kind = parse_pixiv_target(target).kind
                except ValueError:
                    parsed_kind = ""
                actual_scope = kind_scopes.get(parsed_kind)
                if actual_scope:
                    if actual_scope != scope:
                        options["requested_content_scope"] = scope
                    options["content_scope"] = actual_scope
                    options["search_mode"] = actual_scope
                    options["input_kind"] = parsed_kind
        elif target and module_id == "jmcomic":
            options["domain"] = str(self.storage.get_setting("jmcomic_domain", "https://18comic.vip") or "https://18comic.vip")
            options["user_agent"] = str(self.storage.get_setting("jmcomic_user_agent", "") or "")
            options["order_by"] = str(self.storage.get_setting("jmcomic_order_by", "mr") or "mr")
            options["time_range"] = str(self.storage.get_setting("jmcomic_time_range", "a") or "a")
            options["category"] = str(self.storage.get_setting("jmcomic_category", "0") or "0")
            options["favorite_username"] = str(self.storage.get_setting("jmcomic_favorite_username", "") or "")
            options["favorite_folder_id"] = str(self.storage.get_setting("jmcomic_favorite_folder_id", "0") or "0")
            options["novel_favorite_folder_id"] = str(self.storage.get_setting("jmcomic_novel_favorite_folder_id", "0") or "0")
            options["match_mode"] = str(self.storage.get_setting("jmcomic_match_mode", "fuzzy") or "fuzzy")
            options["jm_postprocess"] = str(self.storage.get_setting("jmcomic_postprocess", "none") or "none")
            options["jm_download_cover"] = bool(self.storage.get_setting("jmcomic_download_cover", True))
            if scope == "章节 ID / 链接":
                options["input_kind"] = "photo"
            elif scope == "漫画 ID / 链接":
                options["input_kind"] = "album"
        elif target and module_id == "ehentai":
            options["filter_ai"] = True
            options["eh_download_method"] = "images"
            options["eh_download_images"] = True
            options["eh_download_torrent"] = bool(self.storage.get_setting("eh_download_torrent", False))
            options["eh_bt_download_enabled"] = bool(self.storage.get_setting("eh_bt_download_enabled", False))
            options["max_torrents"] = 1
        return options

    def _clear_twitter_download_records(self) -> None:
        if not messagebox.askyesno("清理推特下载记录", "会清空推特 downloaded/failed JSON 和软件任务记录，不删除本地媒体文件。"):
            return
        adapter = self.manager.get_adapter("twitter")
        adapter.clear_selected_download_records(list(self.twitter_history_rows))
        record_counts = adapter.clear_download_records()
        task_ids = {
            str(row["task_id"])
            for row in self.storage.list_tasks(limit=10000)
            if str(row["module_id"]) == "twitter"
        }
        active = task_ids & set(self.manager.active_task_ids())
        db_counts = self.storage.delete_task_records(task_ids - active)
        for task_id in active:
            self.pending_task_deletions.add(task_id)
            self.manager.cancel(task_id)
        self._refresh_tasks()
        self._refresh_library(scan=False)
        self._load_twitter_history()
        self._append_log(f"已清理推特下载记录: json={record_counts}, db={db_counts}")
        self.status_var.set("推特下载记录已清理" + (f"；{len(active)} 个运行中任务停止后移除" if active else ""))
