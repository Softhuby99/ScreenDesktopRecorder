"""
gui_widgets.py
==============
Wiederverwendbare kleine UI-Bausteine für gui_main.py.
"""

import tkinter

import customtkinter as ctk

from config import COLOR_BG_INPUT, COLOR_DANGER, COLOR_SUCCESS, COLOR_TEXT_MUTED, COLOR_WARNING


class LevelMeterBar(ctk.CTkFrame):
    """
    Bunter horizontaler Pegelbalken (grün -> gelb -> rot) mit kurz
    sichtbarer Peak-Markierung - für die Mikrofon-/Lautsprecher-Vorschau.
    Zeichnet direkt auf ein tkinter.Canvas, keine zusätzliche Abhängigkeit
    (z. B. PIL) nötig.
    """

    # (Start-Anteil, End-Anteil, Farbe) - deckt zusammen 0.0-1.0 ab
    SEGMENTS = (
        (0.0, 0.70, COLOR_SUCCESS),
        (0.70, 0.90, COLOR_WARNING),
        (0.90, 1.0, COLOR_DANGER),
    )

    def __init__(self, master, height: int = 22, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)

        self._canvas = tkinter.Canvas(
            self, height=height, highlightthickness=0, bg=COLOR_BG_INPUT,
        )
        self._canvas.pack(fill="x", expand=True)
        self._canvas.bind("<Configure>", lambda _e: self._redraw())

        self._level = 0.0
        self._peak = 0.0
        self._unavailable_text: str | None = "Vorschau wird geladen …"

    def set_level(self, rms: float, peak: float):
        self._unavailable_text = None
        self._level = max(0.0, min(1.0, rms))
        self._peak = max(0.0, min(1.0, peak))
        self._redraw()

    def set_unavailable(self, text: str = "Nicht verfügbar"):
        self._unavailable_text = text
        self._level = 0.0
        self._peak = 0.0
        self._redraw()

    def _redraw(self):
        c = self._canvas
        c.delete("all")
        w = c.winfo_width()
        h = c.winfo_height()
        if w <= 1 or h <= 1:
            return

        c.create_rectangle(0, 0, w, h, fill=COLOR_BG_INPUT, outline="")

        if self._unavailable_text:
            c.create_text(
                w / 2, h / 2, text=self._unavailable_text,
                fill=COLOR_TEXT_MUTED, font=("Segoe UI", 10),
            )
            return

        level_px = w * self._level
        for start, end, color in self.SEGMENTS:
            seg_start = w * start
            seg_end = w * end
            draw_end = min(seg_end, level_px)
            if draw_end > seg_start:
                c.create_rectangle(seg_start, 2, draw_end, h - 2, fill=color, outline="")

        # Peak-Marker: dünne helle Linie, die kurz an ihrer letzten Position stehen bleibt
        peak_px = w * self._peak
        if peak_px > 2:
            c.create_rectangle(peak_px - 2, 0, peak_px, h, fill="#FFFFFF", outline="")
