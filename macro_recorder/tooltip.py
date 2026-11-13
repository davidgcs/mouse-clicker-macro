"""A hint that appears when the pointer rests on a widget."""

from __future__ import annotations

import sys
import tkinter as tk

HOVER_MS = 500
# Light and dark on purpose: the hint reads the same whatever theme the system uses.
BACKGROUND = "#ffffe0"
FOREGROUND = "#000000"
GAP = 4  # pixels between the widget and its hint, so the hint never lands under the pointer


class Tooltip:
    def __init__(self, widget: tk.Misc, text: str):
        self.widget = widget
        self.text = text
        self._job: str | None = None
        self._window: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self.hide, add="+")
        widget.bind("<ButtonPress>", self.hide, add="+")
        widget.bind("<Destroy>", self.hide, add="+")

    def _schedule(self, _event=None) -> None:
        self.hide()
        self._job = self.widget.after(HOVER_MS, self._show)

    def hide(self, _event=None) -> None:
        job, window = self._job, self._window
        self._job = self._window = None
        try:
            if job is not None:
                self.widget.after_cancel(job)
            if window is not None:
                window.destroy()
        except tk.TclError:
            pass  # the whole window is being destroyed

    def _show(self) -> None:
        self._job = None
        if self._window is not None:
            return
        window = tk.Toplevel(self.widget)
        window.withdraw()
        window.wm_overrideredirect(True)
        if sys.platform == "darwin":
            try:  # a window that cannot take the focus away from the app
                window.tk.call("::tk::unsupported::MacWindowStyle", "style", window._w, "help", "noActivates")
            except tk.TclError:
                pass
        tk.Label(
            window,
            text=self.text,
            background=BACKGROUND,
            foreground=FOREGROUND,
            relief="solid",
            borderwidth=1,
            padx=4,
            pady=1,
        ).pack()
        window.update_idletasks()
        widget, width = self.widget, window.winfo_reqwidth()
        # No clamping to the screen: positions are on the whole desktop, which is wider than the primary screen.
        x = widget.winfo_rootx() + widget.winfo_width() // 2 - width // 2
        window.wm_geometry(f"+{x}+{widget.winfo_rooty() + widget.winfo_height() + GAP}")
        window.deiconify()
        self._window = window
