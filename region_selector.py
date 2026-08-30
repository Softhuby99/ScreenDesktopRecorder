"""
region_selector.py
==================
Vollbild-Overlay zur freien Auswahl eines Bildschirmbereichs.

Zwei Darstellungs-Modi:

  1. SCREENSHOT (Standard unter Linux/X11):
     Tk/X11 stellt Fenster-Alpha ('-alpha') nur dann zuverlaessig dar,
     wenn ein Compositor laeuft UND dieser das Fenster tatsaechlich
     komponiert - je nach Fenstermanager (i3, Openbox, LXDE, aber auch
     kwin/picom mit 'unredirect-fullscreen'-Heuristiken) ist das nicht
     garantiert. Auf genau den alten/schwachen Notebooks, fuer die diese
     App optimiert ist, bleibt das Overlay dann komplett blickdicht -
     der Nutzer sieht nur einen schwarzen Bildschirm statt des Desktops.
     Deshalb wird unter Linux stattdessen IMMER EIN einzelnes Standbild
     des Bildschirms per FFmpeg gezogen und als Canvas-Hintergrund
     genutzt. Der Nutzer sieht optisch weiterhin "seinen Desktop" beim
     Aufziehen des Rahmens, das Verfahren funktioniert aber unabhaengig
     von Fenstermanager/Compositor. Ein einmaliges Standbild ist
     ressourcentechnisch vernachlaessigbar (kein Vergleich zu einer
     laufenden Aufnahme).

  2. TRANSPARENT (Windows/macOS, dort komponiert DWM/Quartz immer
     systemweit; unter Linux nur als Notnagel, falls der Screenshot
     ausnahmsweise fehlschlaegt):
     Ein randloses Toplevel-Fenster mit hoher Transparenz ('-alpha'),
     sodass der echte Desktop dahinter sichtbar bleibt.

Rueckgabe in beiden Modi identisch: (x, y, breite, hoehe) oder None.
"""

import os
import subprocess
import tempfile
import tkinter as tk
import uuid

from config import COLOR_DANGER
from platform_utils import IS_LINUX, get_subprocess_flags


class RegionSelector:
    """
    Blockierendes Overlay zur Bereichsauswahl.

    Verwendung:
        selector = RegionSelector(parent)
        region = selector.select()   # -> (x, y, w, h) oder None
    """

    MIN_SIZE = 20  # Mindestgroesse in Pixeln, um Fehlklicks zu ignorieren

    # Alpha-Wert des transparenten Overlays: 0.0 = unsichtbar, 1.0 = deckend.
    OVERLAY_ALPHA = 0.35

    # Abdunkelung ausserhalb der Auswahl im Screenshot-Modus (Stipple-Trick,
    # braucht keinen Compositor - funktioniert auf jedem Fenstermanager).
    DIM_COLOR = "#000000"
    DIM_STIPPLE = "gray50"

    def __init__(self, parent):
        self.parent = parent
        self.result: tuple | None = None

        self.start_x = 0
        self.start_y = 0
        self.rect_id = None
        self.info_id = None

        self._use_screenshot = False
        self._bg_photo = None  # Referenz muss gehalten werden (sonst GC!)

        # Abdunkelungs-Rechtecke im Screenshot-Modus
        self._dim_full = None
        self._dim_top = None
        self._dim_bottom = None
        self._dim_left = None
        self._dim_right = None

    # ------------------------------------------------------------------
    def select(self) -> tuple | None:
        """Oeffnet das Overlay und wartet, bis der Nutzer fertig ist."""
        self.window = tk.Toplevel(self.parent)

        # KRITISCH: sofort verstecken, BEVOR irgendeine Geometrie gesetzt
        # wird! Ein Toplevel wird in Tk beim Setzen von .geometry() bereits
        # auf dem Bildschirm eingeblendet. Wuerde man erst danach einen
        # Screenshot ziehen, faengt der Screenshot das eigene, noch
        # blickdichte Overlay-Fenster ein statt des echten Desktops - das
        # Ergebnis waere wieder ein (diesmal doppelt) schwarzes Bild.
        # Deshalb bleibt das Fenster bis GANZ zum Schluss (deiconify())
        # unsichtbar, waehrend Groesse, Hintergrund und ggf. Screenshot
        # vorbereitet werden.
        self.window.withdraw()

        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)

        bg_color = "#101010"
        self.window.configure(bg=bg_color, cursor="crosshair")

        # Multi-Monitor: Overlay ueber die gesamte virtuelle Desktopflaeche.
        # WICHTIG: bewusst KEIN '-fullscreen'-Attribut setzen! Viele
        # Compositor (kwin, picom mit 'unredirect-fullscreen') schalten
        # die Komposition fuer als 'fullscreen' erkannte Fenster ab, um
        # z. B. Spiele/Videos zu beschleunigen. Dann wird die Transparenz
        # ignoriert und das Fenster erscheint blickdicht schwarz - obwohl
        # ein Compositor laeuft. 'overrideredirect' + explizite Geometrie
        # erreicht optisch dasselbe, ohne diesen WM-Sonderfall auszuloesen.
        vw = self.window.winfo_vrootwidth() or self.window.winfo_screenwidth()
        vh = self.window.winfo_vrootheight() or self.window.winfo_screenheight()
        self.window.geometry(f"{vw}x{vh}+0+0")

        self.canvas = tk.Canvas(
            self.window, bg=bg_color, highlightthickness=0, cursor="crosshair"
        )
        self.canvas.pack(fill="both", expand=True)

        # ---- Modus bestimmen: transparent oder Screenshot-Fallback ----
        # Unter Linux/X11 wird IMMER der Screenshot-Weg genutzt: er ist
        # unabhaengig vom jeweiligen Fenstermanager/Compositor zuverlaessig,
        # waehrend '-alpha' je nach WM/Compositor-Kombination unterschiedlich
        # (oder gar nicht) funktioniert - genau das hat den schwarzen
        # Bildschirm verursacht. Das Fenster ist an dieser Stelle noch
        # unsichtbar (siehe withdraw() oben), der Screenshot zeigt also
        # garantiert den echten Desktop.
        if IS_LINUX:
            self.window.update_idletasks()
            self._bg_photo = self._grab_screenshot(vw, vh)
            self._use_screenshot = self._bg_photo is not None

        if self._use_screenshot:
            self.canvas.create_image(0, 0, anchor="nw", image=self._bg_photo)
            self._dim_full = self.canvas.create_rectangle(
                0, 0, vw, vh, fill=self.DIM_COLOR, outline="", stipple=self.DIM_STIPPLE
            )
        else:
            # Transparenter Modus (Windows/macOS immer; Linux nur als
            # Notnagel, falls der Screenshot ausnahmsweise fehlschlaegt,
            # z. B. weil FFmpeg nicht gefunden wird).
            self.window.attributes("-alpha", self.OVERLAY_ALPHA)

        # Bedienhinweis mittig einblenden (mit dunklem Hintergrund-Chip,
        # damit er auf jedem Untergrund lesbar bleibt)
        hint_text = "Bereich mit der Maus aufziehen  •  ESC = Abbrechen"
        self.canvas.create_rectangle(
            vw // 2 - 260, 18, vw // 2 + 260, 62,
            fill="#000000", outline="", stipple="gray50",
        )
        self.canvas.create_text(
            vw // 2, 40,
            text=hint_text,
            fill="#FFFFFF",
            font=("Segoe UI", 16, "bold"),
        )

        # Event-Bindings
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.window.bind("<Escape>", self._on_cancel)

        # Erst JETZT tatsaechlich anzeigen - Inhalt (Screenshot/Alpha)
        # steht zu diesem Zeitpunkt bereits vollstaendig fest.
        self.window.deiconify()
        self.window.focus_force()
        self.window.grab_set()
        self.parent.wait_window(self.window)

        return self.result

    # ------------------------------------------------------------------
    # Screenshot-Fallback (nur Linux ohne Compositor)
    # ------------------------------------------------------------------
    def _grab_screenshot(self, width: int, height: int):
        """
        Zieht ein einzelnes Vollbild-Standbild per FFmpeg und laedt es
        als tk.PhotoImage (PNG wird von Tk seit 8.6 nativ unterstuetzt -
        keine zusaetzliche Abhaengigkeit wie Pillow noetig).

        :return: tk.PhotoImage oder None bei jedem Fehler (dann faellt
                 select() auf den transparenten Modus zurueck).
        """
        from ffmpeg_utils import build_screenshot_command

        temp_path = os.path.join(
            tempfile.gettempdir(), f"screenrec_region_{uuid.uuid4().hex}.png"
        )
        try:
            cmd = build_screenshot_command(temp_path, width, height)
            subprocess.run(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                **get_subprocess_flags(),
            )
            if not os.path.isfile(temp_path):
                return None
            return tk.PhotoImage(file=temp_path)
        except Exception:
            return None
        finally:
            try:
                os.remove(temp_path)
            except Exception:
                pass

    def _update_dim_rects(self, x1: int, y1: int, x2: int, y2: int, vw: int, vh: int):
        """Passt die vier Abdunkelungs-Rechtecke rund um die Auswahl an."""
        x, y = min(x1, x2), min(y1, y2)
        x_end, y_end = max(x1, x2), max(y1, y2)

        for rect_id in (self._dim_top, self._dim_bottom, self._dim_left, self._dim_right):
            if rect_id:
                self.canvas.delete(rect_id)

        self._dim_top = self.canvas.create_rectangle(
            0, 0, vw, y, fill=self.DIM_COLOR, outline="", stipple=self.DIM_STIPPLE
        )
        self._dim_bottom = self.canvas.create_rectangle(
            0, y_end, vw, vh, fill=self.DIM_COLOR, outline="", stipple=self.DIM_STIPPLE
        )
        self._dim_left = self.canvas.create_rectangle(
            0, y, x, y_end, fill=self.DIM_COLOR, outline="", stipple=self.DIM_STIPPLE
        )
        self._dim_right = self.canvas.create_rectangle(
            x_end, y, vw, y_end, fill=self.DIM_COLOR, outline="", stipple=self.DIM_STIPPLE
        )

    # ------------------------------------------------------------------
    # Maus-Events
    # ------------------------------------------------------------------
    def _on_press(self, event):
        self.start_x, self.start_y = event.x, event.y

        if self._use_screenshot and self._dim_full:
            self.canvas.delete(self._dim_full)
            self._dim_full = None
            vw = self.window.winfo_width()
            vh = self.window.winfo_height()
            self._update_dim_rects(self.start_x, self.start_y, self.start_x, self.start_y, vw, vh)

        if self.rect_id:
            self.canvas.delete(self.rect_id)
        self.rect_id = self.canvas.create_rectangle(
            self.start_x, self.start_y, self.start_x, self.start_y,
            outline=COLOR_DANGER, width=3,
        )
        # Auswahlrechteck immer ueber den Abdunkelungs-Flaechen anzeigen
        self.canvas.tag_raise(self.rect_id)

    def _on_drag(self, event):
        if not self.rect_id:
            return
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, event.x, event.y)

        if self._use_screenshot:
            vw = self.window.winfo_width()
            vh = self.window.winfo_height()
            self._update_dim_rects(self.start_x, self.start_y, event.x, event.y, vw, vh)
            self.canvas.tag_raise(self.rect_id)

        # Live-Anzeige der Aufloesung neben dem Cursor
        w = abs(event.x - self.start_x)
        h = abs(event.y - self.start_y)
        label = f"{w} x {h} px"

        if self.info_id:
            self.canvas.delete(self.info_id)
        self.info_id = self.canvas.create_text(
            event.x + 55, event.y + 18,
            text=label, fill="#FFFFFF", font=("Segoe UI", 11, "bold"),
        )

    def _on_release(self, event):
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x, event.y

        x = min(x1, x2)
        y = min(y1, y2)
        w = abs(x2 - x1)
        h = abs(y2 - y1)

        # Zu kleine Auswahl verwerfen
        if w < self.MIN_SIZE or h < self.MIN_SIZE:
            self.result = None
        else:
            # Gerade Zahlen erzwingen (Pflicht fuer yuv420p)
            w -= w % 2
            h -= h % 2
            self.result = (x, y, w, h)

        self._close()

    def _on_cancel(self, _event=None):
        self.result = None
        self._close()

    def _close(self):
        try:
            self.window.grab_release()
        except Exception:
            pass
        try:
            self.window.destroy()
        except Exception:
            pass
