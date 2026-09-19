from __future__ import annotations

import tkinter as tk


def bind_canvas_mousewheel(canvas: tk.Canvas, content: tk.Widget) -> None:
    """Scroll a canvas when the pointer is over its current child controls."""

    visited: set[str] = set()

    def scroll_units(units: int) -> str:
        bounds = canvas.bbox("all")
        if bounds and bounds[3] > canvas.winfo_height():
            canvas.yview_scroll(units, "units")
        return "break"

    def on_wheel(event: tk.Event) -> str:
        delta = int(getattr(event, "delta", 0) or 0)
        if delta == 0:
            return "break"
        return scroll_units(-max(1, abs(delta) // 120) if delta > 0 else max(1, abs(delta) // 120))

    def bind_tree(widget: tk.Widget) -> None:
        widget_path = str(widget)
        if widget_path in visited:
            return
        visited.add(widget_path)
        widget.bind("<MouseWheel>", on_wheel, add="+")
        widget.bind("<Button-4>", lambda _event: scroll_units(-1), add="+")
        widget.bind("<Button-5>", lambda _event: scroll_units(1), add="+")
        for child in widget.winfo_children():
            bind_tree(child)

    bind_tree(canvas)
    bind_tree(content)
