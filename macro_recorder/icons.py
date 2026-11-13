"""Small icons drawn with Tk itself, so the app needs no image files to ship."""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont

# The art is meant to sit next to text that is about this many pixels tall.
ART_LINE_HEIGHT = 16

# One character per pixel; anything not in the palette ("." here) stays transparent.
FOLDER = (
    ".oooooo.........",
    ".oyyyyo.........",
    ".oyyyyyoooooooo.",
    ".oyyyyyyyyyyyyo.",
    ".oooooooooooooo.",
    ".offffffffffffo.",
    ".offffffffffffo.",
    ".offffffffffffo.",
    ".offffffffffffo.",
    ".offffffffffffo.",
    ".offffffffffffo.",
    ".oooooooooooooo.",
)
# o is the outline, y the back of the folder and f its front.
FOLDER_COLORS = {"o": "#8a5a00", "y": "#e0a800", "f": "#ffcb3d"}
FOLDER_DISABLED_COLORS = {"o": "#8e8e8e", "y": "#bdbdbd", "f": "#d6d6d6"}


def scale_for(master: tk.Misc) -> int:
    """How many times the art must be enlarged to keep its size next to the text on a sharp screen."""
    font = tkfont.Font(root=master, name="TkDefaultFont", exists=True)
    return max(1, round(font.metrics("linespace") / ART_LINE_HEIGHT))


def draw(master: tk.Misc, rows: tuple[str, ...], colors: dict[str, str], scale: int = 1) -> tk.PhotoImage:
    """A PhotoImage of the art. The caller has to keep a reference to it, or Tk shows nothing."""
    image = tk.PhotoImage(master=master, width=len(rows[0]), height=len(rows))
    for y, row in enumerate(rows):
        x = 0
        while x < len(row):  # a run of the same colour is one rectangle
            end = x + 1
            while end < len(row) and row[end] == row[x]:
                end += 1
            if row[x] in colors:
                image.put(colors[row[x]], to=(x, y, end, y + 1))
            x = end
    return image.zoom(scale) if scale > 1 else image


def folder_icons(master: tk.Misc) -> tuple[tk.PhotoImage, tk.PhotoImage]:
    """The folder in colour, and in grey for a button that cannot be used."""
    scale = scale_for(master)
    return draw(master, FOLDER, FOLDER_COLORS, scale), draw(master, FOLDER, FOLDER_DISABLED_COLORS, scale)
