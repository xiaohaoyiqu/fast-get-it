from __future__ import annotations

import tkinter as tk
from tkinter import ttk


APP_PALETTE = {
    "app": "#f3f5f8",
    "panel": "#ffffff",
    "sidebar": "#172033",
    "sidebar_muted": "#b6c0d2",
    "text": "#1d2939",
    "muted": "#667085",
    "border": "#d8dee8",
    "primary": "#4f46e5",
    "primary_hover": "#4338ca",
    "accent": "#ea580c",
    "accent_hover": "#c2410c",
    "soft": "#eef2ff",
    "selection": "#4338ca",
}


def apply_desktop_theme(root: tk.Misc) -> dict[str, str]:
    """Apply the visual layer without coupling page behavior to colors or fonts."""
    palette = dict(APP_PALETTE)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    font = ("Microsoft YaHei UI", 9)
    style.configure("TFrame", background=palette["app"])
    style.configure("App.TFrame", background=palette["app"])
    style.configure("Panel.TFrame", background=palette["panel"], relief="solid", borderwidth=1)
    style.configure("FlatPanel.TFrame", background=palette["panel"])
    style.configure("Panel.TLabelframe", background=palette["panel"], bordercolor=palette["border"], relief="solid")
    style.configure(
        "Panel.TLabelframe.Label",
        background=palette["panel"], foreground=palette["text"], font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.configure("Sidebar.TFrame", background=palette["sidebar"])
    style.configure("Soft.TFrame", background=palette["soft"])
    style.configure("TLabel", background=palette["app"], foreground=palette["text"], font=font)
    style.configure("Panel.TLabel", background=palette["panel"], foreground=palette["text"], font=font)
    style.configure("Title.TLabel", background=palette["panel"], foreground=palette["text"], font=("Microsoft YaHei UI", 12, "bold"))
    style.configure("Name.TLabel", background=palette["panel"], foreground=palette["text"], font=("Microsoft YaHei UI", 14, "bold"))
    style.configure("Small.TLabel", background=palette["panel"], foreground=palette["muted"], font=font)
    style.configure("Section.TLabel", background=palette["panel"], foreground=palette["text"], font=("Microsoft YaHei UI", 10, "bold"))
    style.configure("SidebarTitle.TLabel", background=palette["sidebar"], foreground="#ffffff", font=("Microsoft YaHei UI", 16, "bold"))
    style.configure("SidebarText.TLabel", background=palette["sidebar"], foreground=palette["sidebar_muted"], font=font)
    style.configure("HeaderTitle.TLabel", background=palette["app"], foreground=palette["text"], font=("Microsoft YaHei UI", 18, "bold"))
    style.configure("HeaderSub.TLabel", background=palette["app"], foreground=palette["muted"], font=font)
    style.configure(
        "Status.TLabel", background=palette["soft"], foreground=palette["primary"],
        padding=(11, 6), font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.configure(
        "Step.TLabel", background=palette["soft"], foreground=palette["primary"],
        padding=(10, 5), font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.configure(
        "TButton", padding=(11, 7), font=font, background="#f8fafc",
        foreground=palette["text"], bordercolor=palette["border"], relief="flat",
    )
    style.configure("Compact.TButton", padding=(7, 3), font=font)
    style.map(
        "TButton",
        background=[("active", "#eef2f6"), ("pressed", "#e4e8ee"), ("disabled", "#f2f4f7")],
        foreground=[("disabled", "#98a2b3")],
    )
    style.configure(
        "Accent.TButton", padding=(14, 8), background=palette["primary"], foreground="#ffffff",
        bordercolor=palette["primary"], relief="flat", font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", palette["primary_hover"]), ("pressed", "#3730a3"), ("disabled", "#a5b4fc")],
        foreground=[("disabled", "#eef2ff")],
    )
    style.configure(
        "Action.TButton", padding=(14, 8), background=palette["accent"], foreground="#ffffff",
        bordercolor=palette["accent"], relief="flat", font=("Microsoft YaHei UI", 9, "bold"),
    )
    style.map("Action.TButton", background=[("active", palette["accent_hover"]), ("pressed", "#9a3412")])
    style.configure(
        "TEntry", padding=(8, 6), fieldbackground="#ffffff", foreground=palette["text"],
        bordercolor=palette["border"], lightcolor=palette["border"], darkcolor=palette["border"], insertcolor=palette["text"],
    )
    style.configure("TCombobox", padding=(7, 5), fieldbackground="#ffffff", arrowsize=14)
    style.configure("TSpinbox", padding=(6, 5), fieldbackground="#ffffff", arrowsize=13)
    style.configure(
        "Treeview", rowheight=30, background="#ffffff", fieldbackground="#ffffff",
        foreground=palette["text"], bordercolor=palette["border"], font=font, relief="flat",
    )
    style.configure(
        "Treeview.Heading", background="#f2f4f7", foreground="#344054", padding=(9, 8),
        font=("Microsoft YaHei UI", 9, "bold"), relief="flat",
    )
    style.map("Treeview", background=[("selected", palette["selection"])], foreground=[("selected", "#ffffff")])
    style.map("Treeview.Heading", background=[("active", "#e4e7ec")])
    style.configure("TNotebook", background=palette["app"], borderwidth=0, tabmargins=(0, 0, 0, 0))
    style.configure(
        "TNotebook.Tab", padding=(15, 9), background="#e4e7ec", foreground=palette["muted"],
        font=("Microsoft YaHei UI", 9, "bold"), borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", "#ffffff"), ("active", "#eef2f6")],
        foreground=[("selected", palette["primary"])],
    )
    style.configure("Horizontal.TProgressbar", background=palette["primary"], troughcolor="#e4e7ec", borderwidth=0)
    return palette


__all__ = ["APP_PALETTE", "apply_desktop_theme"]
