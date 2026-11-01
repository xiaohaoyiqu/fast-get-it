"""Small layout helpers shared by desktop pages."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from software_app.ui.scrolling import bind_canvas_mousewheel


class ResponsiveToolbar(ttk.Frame):
    """Keep action buttons in equal columns and wrap them when space is tight."""

    def __init__(self, parent, *, max_columns: int = 8, gap: int = 6, **kwargs):
        super().__init__(parent, **kwargs)
        self.max_columns = max_columns
        self.gap = gap
        self._layout_job = None
        self._bound_children: set[str] = set()
        self._previous_columns = 0
        self.bind("<Configure>", self._queue_layout, add="+")
        self.bind("<Destroy>", self._destroy_layout, add="+")
        self._queue_layout()

    def _queue_layout(self, _event=None):
        if self._layout_job is None:
            self._layout_job = self.after_idle(self._layout_buttons)

    def _layout_buttons(self):
        self._layout_job = None
        if not self.winfo_exists():
            return
        children = self.winfo_children()
        for child in children:
            if str(child) not in self._bound_children:
                child.bind("<Map>", self._queue_layout, add="+")
                child.bind("<Unmap>", self._queue_layout, add="+")
                self._bound_children.add(str(child))
        buttons = [child for child in children if child.winfo_manager() == "grid"]
        if not buttons:
            return
        width = max(1, self.winfo_width())
        requested = max(child.winfo_reqwidth() for child in buttons)
        columns = max(1, min(self.max_columns, len(buttons), (width + self.gap) // (requested + self.gap)))
        for column in range(max(columns, self._previous_columns)):
            self.columnconfigure(
                column, weight=1 if column < columns else 0, uniform="actions" if column < columns else ""
            )
        self._previous_columns = columns
        for index, child in enumerate(buttons):
            row, column = divmod(index, columns)
            child.grid(
                row=row,
                column=column,
                columnspan=1,
                rowspan=1,
                sticky="ew",
                padx=(0, self.gap if column < columns - 1 else 0),
                pady=(0 if row == 0 else self.gap, 0),
            )

    def _destroy_layout(self, event):
        if event.widget is self and self._layout_job is not None:
            self.after_cancel(self._layout_job)
            self._layout_job = None


def wrap_label(label: ttk.Label, parent: tk.Widget, *, padding: int = 0, minimum: int = 80) -> None:
    """Let status text wrap without setting a minimum width on its grid column."""
    label.configure(wraplength=minimum, justify="left")
    parent.bind("<Configure>", lambda event: label.configure(wraplength=max(minimum, event.width - padding)), add="+")


class ScrollableControls(ttk.Frame):
    """Bound a page's controls so its results remain usable in short windows."""

    def __init__(self, parent, *, height: int = 160, **kwargs):
        super().__init__(parent, **kwargs)
        self._height_limit = height
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, background="#ffffff", highlightthickness=0, width=1, height=height)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.content = ttk.Frame(self.canvas, style="Panel.TFrame")
        self.content.columnconfigure(0, weight=1)
        self._window = self.canvas.create_window((0, 0), window=self.content, anchor="nw")
        self.canvas.bind(
            "<Configure>", lambda event: self.canvas.itemconfigure(self._window, width=max(1, event.width))
        )
        self.content.bind("<Configure>", self._update_content)
        self._wheel_job = self.after_idle(self._bind_wheel)
        self.bind("<Destroy>", self._cancel_wheel, add="+")

    def set_height_limit(self, height: int):
        self._height_limit = max(50, height)
        self._update_content()

    def _update_content(self, _event=None):
        self.canvas.configure(
            scrollregion=self.canvas.bbox("all"), height=min(self._height_limit, max(1, self.content.winfo_reqheight()))
        )

    def _bind_wheel(self):
        self._wheel_job = None
        bind_canvas_mousewheel(self.canvas, self.content)

    def _cancel_wheel(self, event):
        if event.widget is self and self._wheel_job is not None:
            self.after_cancel(self._wheel_job)
            self._wheel_job = None
