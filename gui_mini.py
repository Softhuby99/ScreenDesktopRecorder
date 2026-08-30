"""
gui_mini.py
===========
Rahmenloses Mini-Bedienfeld, das während der Aufnahme über allen
Fenstern schwebt. Bewusst minimal gehalten (geringer RAM-Bedarf).

Features: Pause/Weiter, Stopp, Laufzeit-Timer (MM:SS), FPS-Anzeige,
verschiebbar per Drag auf der Leiste.
"""

import customtkinter as ctk

from config import (
    COLOR_BG_CARD, COLOR_BG_HOVER, COLOR_BG_INPUT,
    COLOR_DANGER, COLOR_DANGER_HOVER,
    COLOR_TEXT_MUTED, COLOR_TEXT_PRIMARY,
    RADIUS_MD, RADIUS_SM,
)


class MiniPanel(ctk.CTkToplevel):
    """
    :param master: Hauptfenster
    :param fps: anzuzeigende Framerate
    :param on_pause: Callback() -> bool (neuer Pausenzustand)
    :param on_stop: Callback()
    """

    def __init__(self, master, fps: str, on_pause, on_stop):
        super().__init__(master)

        self.on_pause = on_pause
        self.on_stop = on_stop
        self._paused = False
        self._drag_x = 0
        self._drag_y = 0

        # ---- Fenstereigenschaften ----
        self.overrideredirect(True)                 # rahmenlos
        self.attributes("-topmost", True)           # always on top
        try:
            self.attributes("-alpha", 0.94)         # leicht transparent
        except Exception:
            pass
        self.configure(fg_color=COLOR_BG_CARD)
        self.resizable(False, False)

        # ---- Positionierung: oben mittig ----
        width, height = 330, 60
        screen_w = self.winfo_screenwidth()
        self.geometry(f"{width}x{height}+{(screen_w - width) // 2}+24")

        self._build_ui()
        self._bind_drag()

    # ------------------------------------------------------------------
    def _build_ui(self):
        container = ctk.CTkFrame(
            self, fg_color=COLOR_BG_CARD, corner_radius=RADIUS_MD
        )
        container.pack(fill="both", expand=True, padx=2, pady=2)

        # --- Roter Aufnahme-Punkt (blinkt) ---
        self.dot = ctk.CTkLabel(
            container, text="●", font=("Segoe UI", 18),
            text_color=COLOR_DANGER, width=20,
        )
        self.dot.pack(side="left", padx=(12, 4))

        # --- Timer + FPS ---
        info = ctk.CTkFrame(container, fg_color="transparent")
        info.pack(side="left", padx=(0, 10))

        self.timer_label = ctk.CTkLabel(
            info, text="00:00",
            font=("Segoe UI", 19, "bold"), text_color=COLOR_TEXT_PRIMARY,
        )
        self.timer_label.pack(anchor="w")

        self.fps_label = ctk.CTkLabel(
            info, text="30 FPS",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
        )
        self.fps_label.pack(anchor="w")

        # --- Stopp-Button ---
        self.stop_btn = ctk.CTkButton(
            container, text="■  Stopp", width=88, height=34,
            corner_radius=RADIUS_SM,
            fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
            font=("Segoe UI", 12, "bold"),
            command=self._handle_stop,
        )
        self.stop_btn.pack(side="right", padx=(4, 12))

        # --- Pause-Button ---
        self.pause_btn = ctk.CTkButton(
            container, text="⏸  Pause", width=92, height=34,
            corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, hover_color=COLOR_BG_HOVER,
            font=("Segoe UI", 12),
            command=self._handle_pause,
        )
        self.pause_btn.pack(side="right", padx=4)

    def _bind_drag(self):
        """Ermöglicht das Verschieben des rahmenlosen Fensters."""
        for widget in (self, self.timer_label, self.fps_label, self.dot):
            widget.bind("<Button-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _drag_move(self, event):
        self.geometry(f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    # ------------------------------------------------------------------
    # ÖFFENTLICHE UPDATE-METHODEN (vom GUI-Thread aufgerufen)
    # ------------------------------------------------------------------
    def update_timer(self, seconds: float):
        mins, secs = divmod(int(seconds), 60)
        self.timer_label.configure(text=f"{mins:02d}:{secs:02d}")

    def set_fps_text(self, text: str):
        self.fps_label.configure(text=text)

    def blink_dot(self, visible: bool):
        self.dot.configure(text_color=COLOR_DANGER if visible else COLOR_BG_CARD)

    # ------------------------------------------------------------------
    def _handle_pause(self):
        if not self.on_pause:
            return
        self._paused = bool(self.on_pause())
        if self._paused:
            self.pause_btn.configure(text="▶  Weiter")
            self.fps_label.configure(text="PAUSIERT")
        else:
            self.pause_btn.configure(text="⏸  Pause")

    def _handle_stop(self):
        self.stop_btn.configure(state="disabled", text="...")
        self.pause_btn.configure(state="disabled")
        if self.on_stop:
            self.on_stop()