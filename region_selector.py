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
        self._vroot_x = 0
        self._vroot_y = 0
        self._vw = 0
        self._vh = 0
        # Aktueller "zweiter Punkt" der Auswahl (Maus ODER Tastatur-Nudge) -
        # siehe _on_drag()/_nudge_selection()/_confirm_via_keyboard().
        self._cur_x = 0
        self._cur_y = 0

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
        # WICHTIG fuer Multi-Monitor-Layouts, bei denen ein Bildschirm LINKS
        # von oder OBERHALB des Hauptbildschirms liegt (unter Windows sehr
        # gebraeuchlich): der virtuelle Desktop-Ursprung liegt dann NICHT
        # bei (0,0), sondern ist negativ (z. B. -1920,0). Ein fest verdrahtetes
        # "+0+0" wuerde das Overlay nur auf den Hauptbildschirm legen und
        # den negativ liegenden Bildschirm komplett aussen vor lassen - der
        # Nutzer koennte dort gar keinen Bereich auswaehlen. winfo_vrootx()/
        # winfo_vrooty() liefern den echten (ggf. negativen) Ursprung; unter
        # X11 ist das ueblicherweise 0.
        self._vroot_x = self.window.winfo_vrootx()
        self._vroot_y = self.window.winfo_vrooty()
        self._vw, self._vh = vw, vh
        self.window.geometry(f"{vw}x{vh}+{self._vroot_x}+{self._vroot_y}")

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
        hint_text = (
            "Bereich mit der Maus aufziehen  •  danach Pfeiltasten = verschieben, "
            "Enter = übernehmen  •  ESC = Abbrechen"
        )
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

        # Tastatur-Alternative (Barrierefreiheit): sobald per Maus EINMAL
        # eine Auswahl aufgezogen wurde, kann sie mit den Pfeiltasten
        # nachjustiert und mit Enter bestaetigt werden, ganz ohne die Maus
        # ruhig halten und exakt loslassen zu muessen.
        self.window.bind("<Return>", self._confirm_via_keyboard)
        self.window.bind("<KP_Enter>", self._confirm_via_keyboard)
        for key, dx, dy in (
            ("<Left>", -1, 0), ("<Right>", 1, 0), ("<Up>", 0, -1), ("<Down>", 0, 1),
        ):
            self.window.bind(key, lambda _e, dx=dx, dy=dy: self._nudge_selection(dx, dy))
        for key, dx, dy in (
            ("<Shift-Left>", -10, 0), ("<Shift-Right>", 10, 0),
            ("<Shift-Up>", 0, -10), ("<Shift-Down>", 0, 10),
        ):
            self.window.bind(key, lambda _e, dx=dx, dy=dy: self._nudge_selection(dx, dy))
        self.window.focus_force()

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
        self._cur_x, self._cur_y = event.x, event.y

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
        self._cur_x, self._cur_y = event.x, event.y
        self._redraw_selection(show_info_near_cursor=True)

    def _redraw_selection(self, show_info_near_cursor: bool = False):
        """
        Zeichnet Auswahlrechteck, Abdunkelung und Größenanzeige anhand von
        (start_x, start_y) -> (_cur_x, _cur_y) neu. Gemeinsam genutzt von
        Maus-Drag UND Tastatur-Nudge (_nudge_selection), damit beide Wege
        optisch exakt gleich reagieren.
        """
        x2, y2 = self._cur_x, self._cur_y
        self.canvas.coords(self.rect_id, self.start_x, self.start_y, x2, y2)

        if self._use_screenshot:
            vw = self.window.winfo_width()
            vh = self.window.winfo_height()
            self._update_dim_rects(self.start_x, self.start_y, x2, y2, vw, vh)
            self.canvas.tag_raise(self.rect_id)

        w = abs(x2 - self.start_x)
        h = abs(y2 - self.start_y)
        label = f"{w} x {h} px"

        if self.info_id:
            self.canvas.delete(self.info_id)
        info_x = x2 + 55 if show_info_near_cursor else min(self.start_x, x2)
        info_y = y2 + 18 if show_info_near_cursor else max(self.start_y, y2) + 18
        self.info_id = self.canvas.create_text(
            info_x, info_y,
            text=label, fill="#FFFFFF", font=("Segoe UI", 11, "bold"),
        )

    def _nudge_selection(self, dx: int, dy: int):
        """
        Tastatur-Alternative zum Nachziehen mit der Maus: verschiebt die
        GESAMTE bereits aufgezogene Auswahl (nicht nur eine Ecke) um
        (dx, dy) Pixel, begrenzt auf die sichtbare Overlay-Fläche. Ohne
        vorher per Maus gestartete Auswahl (kein rect_id) passiert nichts -
        das Nudging ist eine Verfeinerung, kein Ersatz für den ersten Zug.
        """
        if not self.rect_id:
            return
        w = self._cur_x - self.start_x
        h = self._cur_y - self.start_y

        new_start_x = max(0, min(self.start_x + dx, self._vw - 1))
        new_start_y = max(0, min(self.start_y + dy, self._vh - 1))
        new_cur_x = max(0, min(new_start_x + w, self._vw))
        new_cur_y = max(0, min(new_start_y + h, self._vh))
        # Falls das Verschieben am Rand die Breite/Höhe gekappt hätte,
        # lieber den Startpunkt nachjustieren statt die Auswahlgröße
        # ungewollt zu verändern.
        new_start_x = new_cur_x - w
        new_start_y = new_cur_y - h

        self.start_x, self.start_y = new_start_x, new_start_y
        self._cur_x, self._cur_y = new_cur_x, new_cur_y
        self._redraw_selection(show_info_near_cursor=False)

    def _finish_selection(self, x1: int, y1: int, x2: int, y2: int):
        """Gemeinsame Abschlusslogik für Maus-Loslassen UND Enter-Taste."""
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
            # x/y sind bisher relativ zur Overlay-Fensterecke (Canvas-lokale
            # Koordinaten) - in ABSOLUTE Bildschirmkoordinaten umrechnen,
            # indem der (ggf. negative) virtuelle Desktop-Ursprung wieder
            # aufaddiert wird (siehe select()/winfo_vrootx()-Kommentar oben).
            # Ohne dies waere die Auswahl auf einem links/oberhalb des
            # Hauptbildschirms liegenden Monitor um genau diesen Betrag
            # verschoben.
            self.result = (x + self._vroot_x, y + self._vroot_y, w, h)

        self._close()

    def _on_release(self, event):
        self._finish_selection(self.start_x, self.start_y, event.x, event.y)

    def _confirm_via_keyboard(self, _event=None):
        """Enter/Kp_Enter: übernimmt die aktuelle (ggf. per Pfeiltasten
        nachjustierte) Auswahl, ohne dass die Maus losgelassen werden muss."""
        if not self.rect_id:
            return
        self._finish_selection(self.start_x, self.start_y, self._cur_x, self._cur_y)

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
