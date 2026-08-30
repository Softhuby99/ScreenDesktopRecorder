"""
gui_main.py
===========
Hauptfenster mit allen Einstellungen im Dark-Mode-Fluent-Design.

Zwei Tabs statt einer langen Liste:
  - "Video": Bildschirmaufnahme (Modus, Framerate, Video-Encoder, Preset,
    optional eine Audiospur dazu).
  - "Audio": reine Tonaufnahme - Mikrofon/Systemton auswählen (mit
    Live-Pegelanzeige), Verstärkung/Rauschunterdrückung einstellen.

Der Start-Button ist bewusst EIN gemeinsamer Button unten (nicht zwei
verschiedene) - was er tut, hängt vom gerade aktiven Tab ab. Das hält die
Bedienung konsistent: "welcher Tab ist offen" entscheidet "was wird
aufgenommen".

Thread-Sicherheit: Worker-Threads (Benchmark, Recorder, Level-Meter) rufen
NIEMALS direkt Tkinter-Methoden auf. Sie nutzen ausschließlich
self.after(0, ...), wodurch die Aktion in die Event-Loop des GUI-Threads
eingereiht wird.
"""

import os
import threading
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from audio_devices import (
    get_audio_sources, guess_microphone_default, guess_speaker_monitor_default,
    list_meter_devices,
)
from audio_meter import LevelMeter
from benchmark import BenchmarkThread
from config import (
    APP_NAME, APP_VERSION, AUDIO_ONLY_EXTENSION,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG_CARD, COLOR_BG_HOVER,
    COLOR_BG_INPUT, COLOR_BG_MAIN, COLOR_DANGER, COLOR_SUCCESS,
    COLOR_TEXT_MUTED, COLOR_TEXT_PRIMARY, COLOR_WARNING,
    DEFAULT_ENCODER, DEFAULT_FPS, DEFAULT_PRESET,
    ENCODER_OPTIONS, FPS_OPTIONS,
    METER_REFRESH_MS, MIC_GAIN_DEFAULT, MIC_GAIN_MAX, MIC_GAIN_MIN, MIC_GAIN_STEPS,
    MODE_FULLSCREEN, MODE_OPTIONS, MODE_REGION,
    NO_AUDIO_LABEL, PRESET_OPTIONS, QSV_ENCODERS,
    RADIUS_LG, RADIUS_MD, RADIUS_SM,
)
from ffmpeg_utils import check_encoder_available, check_qsv_available, get_ffmpeg_path
from gui_mini import MiniPanel
from gui_widgets import LevelMeterBar
from platform_utils import (
    get_default_videos_dir, get_platform_warning,
    open_file_manager,
)
from recorder import RecorderThread
from region_selector import RegionSelector

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

TAB_VIDEO = "🎥  Video"
TAB_AUDIO = "🎙️  Audio"


class MainWindow(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP_NAME} v{APP_VERSION}")
        self.geometry("560x840")
        # Deutlich kleinere Mindestgroesse als frueher: das Fenster ist per
        # Maus frei in der Groesse veraenderbar (Tk-Standardverhalten, hier
        # nicht eingeschraenkt), und der Inhalt wird beim Verkleinern
        # automatisch scrollbar (siehe CTkScrollableFrame in _build_ui),
        # statt einfach abgeschnitten zu werden.
        self.minsize(340, 380)
        self.configure(fg_color=COLOR_BG_MAIN)

        # ---- Zustand ----
        self.region: tuple | None = None
        self.audio_map: dict[str, str] = {}
        self.recorder: RecorderThread | None = None
        self.mini_panel: MiniPanel | None = None
        self.benchmark_thread: BenchmarkThread | None = None
        self._timer_job = None
        self._blink_state = True

        # Mikrofon-/Lautsprecher-Vorschau (Audio-Tab)
        self._meter_devices: list[tuple[str, int]] = []
        self._mic_level_meter: LevelMeter | None = None
        self._speaker_level_meter: LevelMeter | None = None
        self._meter_poll_job = None

        self._build_ui()
        self._check_environment()
        self._load_audio_devices_async()
        self._load_meter_devices_async()
        self._poll_meters()

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ==================================================================
    # UI-AUFBAU
    # ==================================================================
    def _build_ui(self):
        # ---------------- Fixe Bereiche zuerst reservieren -------------------
        # Werden mit side='bottom' direkt auf dem Hauptfenster gepackt (NICHT
        # in einen der scrollbaren Tab-Bereiche), damit sie unabhängig vom
        # aktiven Tab und auch beim Verkleinern des Fensters immer sichtbar
        # bleiben. Reihenfolge der pack(side='bottom', ...)-Aufrufe ist
        # wichtig: jeder weitere Aufruf legt sich direkt ÜBER die vorherigen.
        self.status_label = ctk.CTkLabel(
            self, text="Bereit.", font=("Segoe UI", 11),
            text_color=COLOR_TEXT_MUTED, wraplength=500,
        )
        self.status_label.pack(side="bottom", fill="x", padx=24, pady=(0, 16))

        self.start_btn = ctk.CTkButton(
            self, text="●   Aufnahme starten",
            height=54, corner_radius=RADIUS_LG,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            font=("Segoe UI", 16, "bold"),
            command=self._start_recording,
        )
        self.start_btn.pack(side="bottom", fill="x", padx=24, pady=(8, 6))

        # Speicherort ist für beide Tabs gemeinsam gültig - deshalb geteilt
        # und fix (nicht Teil des scrollbaren Tab-Inhalts).
        save_card = ctk.CTkFrame(self, fg_color=COLOR_BG_CARD, corner_radius=RADIUS_LG)
        save_card.pack(side="bottom", fill="x", padx=24, pady=8)
        self._card_title(save_card, "Speicherort")

        row = ctk.CTkFrame(save_card, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=(0, 14))

        self.path_var = ctk.StringVar(value=get_default_videos_dir())
        ctk.CTkEntry(
            row, textvariable=self.path_var, height=34,
            corner_radius=RADIUS_SM, fg_color=COLOR_BG_INPUT,
            border_width=0, font=("Segoe UI", 12),
        ).pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            row, text="Durchsuchen", width=110, height=34,
            corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, hover_color=COLOR_BG_HOVER,
            font=("Segoe UI", 12), command=self._browse_folder,
        ).pack(side="left", padx=(8, 0))

        # ---------------- Kopfbereich (fix, oberhalb der Tabs) ----------------
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(side="top", fill="x", padx=24, pady=(22, 8))

        ctk.CTkLabel(
            header, text=APP_NAME,
            font=("Segoe UI", 26, "bold"), text_color=COLOR_TEXT_PRIMARY,
        ).pack(anchor="w")

        ctk.CTkLabel(
            header, text="Ressourcenschonende Bildschirmaufnahme",
            font=("Segoe UI", 12), text_color=COLOR_TEXT_MUTED,
        ).pack(anchor="w")

        # ---------------- Tabs: Video / Audio ----------------
        self.tabview = ctk.CTkTabview(
            self, fg_color="transparent",
            segmented_button_fg_color=COLOR_BG_CARD,
            segmented_button_selected_color=COLOR_ACCENT,
            segmented_button_selected_hover_color=COLOR_ACCENT_HOVER,
            segmented_button_unselected_color=COLOR_BG_CARD,
            segmented_button_unselected_hover_color=COLOR_BG_HOVER,
            text_color=COLOR_TEXT_PRIMARY,
            command=self._on_tab_change,
        )
        self.tabview.pack(side="top", fill="both", expand=True, padx=16, pady=(0, 4))

        video_tab = self.tabview.add(TAB_VIDEO)
        audio_tab = self.tabview.add(TAB_AUDIO)

        self._build_video_tab(video_tab)
        self._build_audio_tab(audio_tab)

    def _build_video_tab(self, parent):
        scroll = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=COLOR_BG_INPUT,
            scrollbar_button_hover_color=COLOR_BG_HOVER,
        )
        scroll.pack(side="top", fill="both", expand=True)
        self._put_scrollbar_on_left(scroll)

        # ---------------- Benchmark-Karte ----------------
        bench_card = self._make_card(scroll)

        self.bench_btn = ctk.CTkButton(
            bench_card, text="⚡  System testen & optimieren",
            height=42, corner_radius=RADIUS_MD,
            fg_color=COLOR_BG_INPUT, hover_color=COLOR_BG_HOVER,
            font=("Segoe UI", 13, "bold"),
            command=self._start_benchmark,
        )
        self.bench_btn.pack(fill="x", padx=14, pady=(14, 8))

        self.bench_status = ctk.CTkLabel(
            bench_card,
            text="Noch nicht getestet – Standardeinstellungen aktiv.",
            font=("Segoe UI", 11), text_color=COLOR_TEXT_MUTED,
            wraplength=460, justify="left",
        )
        self.bench_status.pack(fill="x", padx=14, pady=(0, 14))

        # ---------------- Aufnahme-Einstellungen ----------------
        settings_card = self._make_card(scroll)
        self._card_title(settings_card, "Aufnahme")

        # Modus
        self.mode_var = ctk.StringVar(value=MODE_FULLSCREEN)
        row = self._labeled_row(settings_card, "Modus")
        ctk.CTkOptionMenu(
            row, values=MODE_OPTIONS, variable=self.mode_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
            command=self._on_mode_change,
        ).pack(side="right", fill="x", expand=True)

        self.region_label = ctk.CTkLabel(
            settings_card, text="", font=("Segoe UI", 11),
            text_color=COLOR_ACCENT, anchor="w",
        )
        self.region_label.pack(fill="x", padx=14)

        # Framerate
        self.fps_var = ctk.StringVar(value=DEFAULT_FPS)
        row = self._labeled_row(settings_card, "Framerate")
        self.fps_menu = ctk.CTkOptionMenu(
            row, values=FPS_OPTIONS, variable=self.fps_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
        )
        self.fps_menu.pack(side="right", fill="x", expand=True)

        # Encoder
        self.encoder_var = ctk.StringVar(value=DEFAULT_ENCODER)
        row = self._labeled_row(settings_card, "Video-Encoder")
        self.encoder_menu = ctk.CTkOptionMenu(
            row, values=ENCODER_OPTIONS, variable=self.encoder_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
            command=self._on_encoder_change,
        )
        self.encoder_menu.pack(side="right", fill="x", expand=True)

        self.encoder_warning = ctk.CTkLabel(
            settings_card, text="", font=("Segoe UI", 11, "bold"),
            text_color=COLOR_WARNING, anchor="w",
        )
        self.encoder_warning.pack(fill="x", padx=14)

        # Preset
        self.preset_var = ctk.StringVar(value=DEFAULT_PRESET)
        row = self._labeled_row(settings_card, "Encoder-Preset")
        self.preset_menu = ctk.CTkOptionMenu(
            row, values=PRESET_OPTIONS, variable=self.preset_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
        )
        self.preset_menu.pack(side="right", fill="x", expand=True)

        # Audio (dieselbe Quelle wie im Audio-Tab - gemeinsame StringVar,
        # siehe self.audio_var / _on_audio_source_change)
        self.audio_var = ctk.StringVar(value=NO_AUDIO_LABEL)
        row = self._labeled_row(settings_card, "Audio-Quelle")
        self.audio_menu = ctk.CTkOptionMenu(
            row, values=[NO_AUDIO_LABEL], variable=self.audio_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
        )
        self.audio_menu.pack(side="right", fill="x", expand=True)

        ctk.CTkLabel(settings_card, text="").pack(pady=2)  # Abstand

        # Reagiert auf JEDE Änderung von audio_var - egal ob über dieses
        # Dropdown oder das im Audio-Tab ausgelöst - und hält die
        # Mikrofon-Vorschau synchron (siehe _on_audio_source_change).
        self.audio_var.trace_add("write", self._on_audio_source_change)

    def _build_audio_tab(self, parent):
        scroll = ctk.CTkScrollableFrame(
            parent, fg_color="transparent",
            scrollbar_button_color=COLOR_BG_INPUT,
            scrollbar_button_hover_color=COLOR_BG_HOVER,
        )
        scroll.pack(side="top", fill="both", expand=True)
        self._put_scrollbar_on_left(scroll)

        # ---------------- Mikrofon-Karte ----------------
        mic_card = self._make_card(scroll)
        self._card_title(mic_card, "Mikrofon")

        row = self._labeled_row(mic_card, "Aufnahmequelle")
        # Dieselbe StringVar wie das "Audio-Quelle"-Dropdown im Video-Tab -
        # eine Auswahl gilt für beide Tabs, kein doppelter Zustand.
        ctk.CTkOptionMenu(
            row, values=[NO_AUDIO_LABEL], variable=self.audio_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
        ).pack(side="right", fill="x", expand=True)

        self.mic_meter = LevelMeterBar(mic_card, height=22)
        self.mic_meter.pack(fill="x", padx=14, pady=(6, 12))

        row = self._labeled_row(mic_card, "Verstärkung")
        self.mic_gain_label = ctk.CTkLabel(
            row, text=f"{MIC_GAIN_DEFAULT:.1f}×", font=("Segoe UI", 12),
            text_color=COLOR_TEXT_MUTED, width=48,
        )
        self.mic_gain_label.pack(side="right")
        self.mic_gain_var = ctk.DoubleVar(value=MIC_GAIN_DEFAULT)
        ctk.CTkSlider(
            row, from_=MIC_GAIN_MIN, to=MIC_GAIN_MAX,
            number_of_steps=MIC_GAIN_STEPS, variable=self.mic_gain_var,
            command=self._on_gain_change,
            fg_color=COLOR_BG_INPUT, progress_color=COLOR_ACCENT,
            button_color=COLOR_ACCENT, button_hover_color=COLOR_ACCENT_HOVER,
        ).pack(side="right", fill="x", expand=True, padx=(0, 10))

        ctk.CTkLabel(
            mic_card,
            text="Wirkt nur auf die Aufnahme (die Vorschau oben zeigt den "
                 "unveränderten Rohpegel).",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
            anchor="w", wraplength=460, justify="left",
        ).pack(fill="x", padx=14)

        self.denoise_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            mic_card, text="Rauschunterdrückung (Aufnahme, experimentell)",
            variable=self.denoise_var, font=("Segoe UI", 12),
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
        ).pack(fill="x", padx=14, pady=(10, 14), anchor="w")

        # ---------------- Lautsprecher-Karte (nur Vorschau, keine Aufnahme) --
        speaker_card = self._make_card(scroll)
        self._card_title(speaker_card, "Lautsprecher (Systemton-Vorschau)")

        row = self._labeled_row(speaker_card, "Gerät")
        self.speaker_var = ctk.StringVar(value="")
        self.speaker_menu = ctk.CTkOptionMenu(
            row, values=["Wird geladen …"], variable=self.speaker_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
            command=self._on_speaker_device_change,
        )
        self.speaker_menu.pack(side="right", fill="x", expand=True)

        self.speaker_meter = LevelMeterBar(speaker_card, height=22)
        self.speaker_meter.pack(fill="x", padx=14, pady=(6, 8))

        self.speaker_hint = ctk.CTkLabel(
            speaker_card, text="",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
            anchor="w", wraplength=460, justify="left",
        )
        self.speaker_hint.pack(fill="x", padx=14, pady=(0, 14))

        # ---------------- Hinweis ----------------
        ctk.CTkLabel(
            scroll,
            text=f"ℹ  Wird ohne Bild als AAC/.{AUDIO_ONLY_EXTENSION}-Datei gespeichert.",
            font=("Segoe UI", 11), text_color=COLOR_TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=24, pady=(4, 16))

    # ---- UI-Hilfsfunktionen ----
    def _make_card(self, parent) -> ctk.CTkFrame:
        card = ctk.CTkFrame(parent, fg_color=COLOR_BG_CARD, corner_radius=RADIUS_LG)
        card.pack(fill="x", padx=24, pady=8)
        return card

    def _put_scrollbar_on_left(self, scrollable_frame: ctk.CTkScrollableFrame):
        """
        CustomTkinter platziert die Scrollleiste eines CTkScrollableFrame
        standardmaessig rechts - dafuer gibt es keine oeffentliche Option.
        Auf ausdruecklichen Wunsch wird sie hier stattdessen links platziert,
        indem Canvas und Scrollbar nach der Konstruktion in der internen
        Grid-Anordnung vertauscht werden (Nachbau von CTkScrollableFrame.
        _create_grid() fuer orientation='vertical', nur mit getauschten
        Spalten).

        Nutzt bewusst private Attribute (_parent_frame/_parent_canvas/
        _scrollbar) der customtkinter-Bibliothek, da es keinen anderen Weg
        gibt. Falls eine kuenftige customtkinter-Version diese internen
        Namen aendert, faengt der try/except das ab - im schlimmsten Fall
        bleibt die Scrollleiste dann einfach an der Standardposition
        rechts, die App bleibt aber voll funktionsfaehig.
        """
        try:
            parent_frame = scrollable_frame._parent_frame
            canvas = scrollable_frame._parent_canvas
            scrollbar = scrollable_frame._scrollbar

            border_spacing = scrollable_frame._apply_widget_scaling(
                parent_frame.cget("corner_radius") + parent_frame.cget("border_width")
            )
            border_padding = (0, scrollable_frame._border_width + 1)

            canvas.grid_forget()
            scrollbar.grid_forget()

            # WICHTIG: _create_grid() der Basisklasse setzt "weight=1" auf
            # Spalte 0 (dort, wo urspruenglich der Canvas sass). Wird nur der
            # Canvas in Spalte 1 verschoben, OHNE die Spaltengewichtung
            # mitzuziehen, bekommt die (schmale) Scrollbar-Spalte den
            # gesamten zusaetzlichen Platz und der eigentliche Inhalt wird
            # auf einen schmalen Streifen zusammengequetscht. Deshalb hier
            # explizit zuruecksetzen: Spalte 0 (Scrollbar) bleibt bei ihrer
            # natuerlichen (schmalen) Breite, Spalte 1 (Canvas/Inhalt)
            # bekommt das gesamte Stretch-Gewicht.
            parent_frame.grid_columnconfigure(0, weight=0)
            parent_frame.grid_columnconfigure(1, weight=1)

            scrollbar.grid(row=1, column=0, sticky="nsew", padx=border_padding, pady=border_spacing)
            canvas.grid(row=1, column=1, sticky="nsew", padx=(0, border_spacing), pady=border_spacing)
        except Exception:
            pass

    def _card_title(self, parent, text: str):
        ctk.CTkLabel(
            parent, text=text, font=("Segoe UI", 13, "bold"),
            text_color=COLOR_TEXT_PRIMARY, anchor="w",
        ).pack(fill="x", padx=14, pady=(14, 8))

    def _labeled_row(self, parent, label: str) -> ctk.CTkFrame:
        """
        Erzeugt eine Zeile mit Label links und gibt das Row-Frame zurück.
        Das eigentliche Steuerelement (Dropdown etc.) muss ANSCHLIESSEND
        MIT diesem zurückgegebenen 'row' als Parent erzeugt und
        mit .pack(side='right', ...) eingefügt werden.
        """
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=14, pady=5)
        ctk.CTkLabel(
            row, text=label, font=("Segoe UI", 12),
            text_color=COLOR_TEXT_MUTED, width=130, anchor="w",
        ).pack(side="left")
        return row

    def _set_status(self, text: str, color: str = COLOR_TEXT_MUTED):
        self.status_label.configure(text=text, text_color=color)

    def _on_tab_change(self):
        active = self.tabview.get()
        if active == TAB_AUDIO:
            self.start_btn.configure(text="●   Audio aufnehmen")
        else:
            self.start_btn.configure(text="●   Aufnahme starten")

    # ==================================================================
    # UMGEBUNGSPRÜFUNG
    # ==================================================================
    def _check_environment(self):
        """Prüft FFmpeg-Verfügbarkeit und Display-Server beim Start."""
        try:
            path = get_ffmpeg_path()
            self._set_status(f"FFmpeg gefunden: {os.path.basename(path)}", COLOR_SUCCESS)
        except FileNotFoundError as exc:
            self._set_status(str(exc).split("\n")[0], COLOR_DANGER)
            self.start_btn.configure(state="disabled")
            self.bench_btn.configure(state="disabled")
            messagebox.showerror("FFmpeg fehlt", str(exc))
            return

        warning = get_platform_warning()
        if warning:
            self._set_status(warning, COLOR_WARNING)

    def _load_audio_devices_async(self):
        """Geräteerkennung (für die Aufnahme selbst) im Thread – blockiert den Start nicht."""
        def worker():
            try:
                sources = get_audio_sources()
            except Exception:
                sources = []
            self.after(0, lambda: self._apply_audio_devices(sources))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_audio_devices(self, sources: list):
        self.audio_map = {display: ident for display, ident in sources}
        values = [NO_AUDIO_LABEL] + list(self.audio_map.keys())
        self.audio_menu.configure(values=values)
        self.audio_var.set(NO_AUDIO_LABEL)

    # ==================================================================
    # MIKROFON-/LAUTSPRECHER-VORSCHAU (Audio-Tab)
    # ==================================================================
    def _load_meter_devices_async(self):
        """
        Geräteerkennung für die LIVE-VORSCHAU (getrennt von
        _load_audio_devices_async oben - siehe audio_devices.list_meter_devices
        für den Unterschied: PortAudio-Index statt FFmpeg-Bezeichner).
        """
        def worker():
            try:
                devices = list_meter_devices()
            except Exception:
                devices = []
            self.after(0, lambda: self._apply_meter_devices(devices))

        threading.Thread(target=worker, daemon=True).start()

    def _apply_meter_devices(self, devices: list[tuple[str, int]]):
        self._meter_devices = devices

        # Mikrofon-Vorschau anhand der aktuell gewählten Audioquelle starten
        # (funktioniert auch, wenn noch "Kein Audio" gewählt ist - zeigt
        # dann einfach das Standard-Mikrofon zur Kontrolle an).
        self._on_audio_source_change()

        # Lautsprecher-Dropdown befüllen
        names = [name for name, _idx in devices]
        if names:
            self.speaker_menu.configure(values=names)
        else:
            self.speaker_menu.configure(values=["Kein Gerät gefunden"])
            self.speaker_var.set("Kein Gerät gefunden")

        speaker_idx = guess_speaker_monitor_default(devices)
        if speaker_idx is not None:
            speaker_name = next((n for n, i in devices if i == speaker_idx), None)
            if speaker_name:
                self.speaker_var.set(speaker_name)
            self._start_speaker_meter(speaker_idx)
            self.speaker_hint.configure(text="")
        else:
            self.speaker_meter.set_unavailable(
                "Kein Systemton-Gerät gefunden" if names else "Kein Eingabegerät gefunden"
            )
            self.speaker_hint.configure(
                text="ℹ  Unter Windows meist nur mit aktiviertem 'Stereo Mix' "
                     "verfügbar (Sound-Systemsteuerung → Aufnahme → rechte "
                     "Maustaste → Deaktivierte Geräte anzeigen). Unter Linux "
                     "hängt es von PulseAudio/PipeWire ab."
            )

    def _index_for_meter_name(self, name: str) -> int | None:
        for n, i in self._meter_devices:
            if n == name:
                return i
        return None

    def _on_audio_source_change(self, *_trace_args):
        """
        Reagiert auf jede Änderung von self.audio_var (Video- ODER
        Audio-Tab-Dropdown - beide teilen sich dieselbe Variable) und hält
        die Mikrofon-Vorschau synchron. Läuft über trace_add, nicht über
        das 'command'-Argument der Dropdowns, damit es unabhängig davon
        funktioniert, über welches der beiden Dropdowns die Auswahl
        geändert wurde.
        """
        label = self.audio_var.get()
        is_system_audio = label.startswith("🔊")
        idx = (
            guess_speaker_monitor_default(self._meter_devices) if is_system_audio
            else guess_microphone_default(self._meter_devices)
        )
        self._start_mic_meter(idx)

    def _on_speaker_device_change(self, value: str):
        idx = self._index_for_meter_name(value)
        if idx is not None:
            self._start_speaker_meter(idx)
            self.speaker_hint.configure(text="")

    def _on_gain_change(self, value):
        self.mic_gain_label.configure(text=f"{float(value):.1f}×")

    def _start_mic_meter(self, index: int | None):
        if self._mic_level_meter:
            self._mic_level_meter.stop()
            self._mic_level_meter = None
        if index is None:
            self.mic_meter.set_unavailable("Kein Mikrofon gefunden")
            return
        meter = LevelMeter(index)
        if meter.start():
            self._mic_level_meter = meter
        else:
            self.mic_meter.set_unavailable("Gerät konnte nicht geöffnet werden")

    def _start_speaker_meter(self, index: int | None):
        if self._speaker_level_meter:
            self._speaker_level_meter.stop()
            self._speaker_level_meter = None
        if index is None:
            self.speaker_meter.set_unavailable("Kein Systemton-Gerät gefunden")
            return
        meter = LevelMeter(index)
        if meter.start():
            self._speaker_level_meter = meter
        else:
            self.speaker_meter.set_unavailable("Gerät konnte nicht geöffnet werden")

    def _pause_meters(self):
        """
        Gibt beide Vorschau-Geräte frei. WICHTIG vor dem Start einer echten
        Aufnahme: manche Systeme (v. a. Windows/DirectShow) erlauben nur
        EINEM Prozess gleichzeitig Zugriff auf ein Aufnahmegerät - ohne
        das hier würde FFmpeg das Mikrofon u. U. nicht öffnen können,
        solange die Live-Vorschau es noch offen hält.
        """
        if self._mic_level_meter:
            self._mic_level_meter.stop()
            self._mic_level_meter = None
        if self._speaker_level_meter:
            self._speaker_level_meter.stop()
            self._speaker_level_meter = None

    def _resume_meters(self):
        self._on_audio_source_change()
        speaker_idx = self._index_for_meter_name(self.speaker_var.get())
        self._start_speaker_meter(speaker_idx)

    def _poll_meters(self):
        if self._mic_level_meter:
            rms, peak = self._mic_level_meter.get_level()
            self.mic_meter.set_level(rms, peak)
        if self._speaker_level_meter:
            rms, peak = self._speaker_level_meter.get_level()
            self.speaker_meter.set_level(rms, peak)
        self._meter_poll_job = self.after(METER_REFRESH_MS, self._poll_meters)

    # ==================================================================
    # EVENT-HANDLER
    # ==================================================================
    def _on_mode_change(self, value: str):
        if value == MODE_REGION:
            self.region_label.configure(text="")
            self.withdraw()
            self.after(220, self._open_region_selector)
        else:
            self.region = None
            self.region_label.configure(text="")

    def _open_region_selector(self):
        try:
            result = RegionSelector(self).select()
        finally:
            self.deiconify()
            self.lift()

        if result:
            x, y, w, h = result
            self.region = result
            self.region_label.configure(text=f"✓ Bereich: {w} × {h} px  (bei {x}, {y})")
        else:
            self.region = None
            self.mode_var.set(MODE_FULLSCREEN)
            self.region_label.configure(text="")

    def _on_encoder_change(self, value: str):
        # WICHTIG: sowohl der libx265-Verfügbarkeitscheck als auch der neue
        # QSV-Hardwarecheck laufen NIE mehr synchron auf dem GUI-Thread -
        # beide starten echte FFmpeg-Testkodierungen, die je nach Hardware
        # spürbar dauern können und sonst die GUI kurz einfrieren würden.
        if value in QSV_ENCODERS:
            self.encoder_warning.configure(
                text="⏳  Prüfe Hardware-Unterstützung (Intel Quick Sync) ...",
                text_color=COLOR_WARNING,
            )
            self._check_encoder_async(
                value, check_qsv_available,
                fallback_msg=(
                    f"{value} konnte auf dieser Hardware nicht genutzt werden.\n\n"
                    "Intel Quick Sync setzt eine passende Intel-Grafikeinheit "
                    "(iGPU) mit aktuellem Treiber voraus. Es wird wieder auf "
                    "libx264 (Software-Encoder) umgeschaltet.",
                ),
                success_text="⚙  Intel Quick Sync (Hardware) aktiv.",
            )
        elif value == "libx265":
            self.encoder_warning.configure(
                text="⏳  Prüfe Verfügbarkeit ...", text_color=COLOR_WARNING,
            )
            self._check_encoder_async(
                value, check_encoder_available,
                fallback_msg=(
                    "libx265 ist in dieser FFmpeg-Version nicht enthalten.\n"
                    "Bitte installiere eine Full-Build oder nutze libx264."
                ),
                success_text="⚠  Erfordert hohe CPU-Leistung!",
            )
        else:
            self.encoder_warning.configure(text="")

    def _check_encoder_async(self, encoder: str, check_fn, fallback_msg: str, success_text: str):
        """
        Führt eine (potenziell langsame) Encoder-Verfügbarkeitsprüfung in
        einem Hintergrund-Thread aus und wendet das Ergebnis anschließend
        über self.after(0, ...) sicher im GUI-Thread an.
        """
        def worker():
            try:
                available = check_fn(encoder)
            except Exception:
                available = False
            self.after(0, self._apply_encoder_check, encoder, available, fallback_msg, success_text)

        threading.Thread(target=worker, daemon=True).start()

    def _apply_encoder_check(self, encoder: str, available: bool, fallback_msg: str, success_text: str):
        # Falls der Nutzer in der Zwischenzeit (während die Prüfung im
        # Hintergrund lief) bereits einen anderen Encoder gewählt hat, darf
        # dieses veraltete Ergebnis nichts mehr überschreiben.
        if self.encoder_var.get() != encoder:
            return

        if available:
            self.encoder_warning.configure(text=success_text, text_color=COLOR_WARNING)
        else:
            messagebox.showwarning("Encoder nicht verfügbar", fallback_msg)
            self.encoder_var.set(DEFAULT_ENCODER)
            self.encoder_warning.configure(text="")

    def _browse_folder(self):
        folder = filedialog.askdirectory(
            title="Speicherort wählen", initialdir=self.path_var.get()
        )
        if folder:
            self.path_var.set(folder)

    # ==================================================================
    # BENCHMARK
    # ==================================================================
    def _start_benchmark(self):
        self.bench_btn.configure(state="disabled", text="⏳  Test läuft ...")
        self.start_btn.configure(state="disabled")
        self.bench_status.configure(
            text="Testaufnahme läuft ...", text_color=COLOR_TEXT_MUTED
        )

        self.benchmark_thread = BenchmarkThread(
            on_progress=lambda t: self.after(0, self._bench_progress, t),
            on_finish=lambda r: self.after(0, self._bench_finish, r),
            on_error=lambda m: self.after(0, self._bench_error, m),
        )
        self.benchmark_thread.start()

    def _bench_progress(self, text: str):
        self.bench_status.configure(text=text, text_color=COLOR_TEXT_MUTED)

    def _bench_finish(self, result: dict):
        # Entscheidungs-Matrix anwenden
        self.fps_var.set(result["fps"])
        self.encoder_var.set(result["encoder"])
        self.preset_var.set(result["preset"])
        self._on_encoder_change(result["encoder"])

        self.bench_status.configure(
            text=(f"{result['message']}\n"
                  f"Ø CPU: {result['avg_cpu']} %  •  Spitze: {result['peak_cpu']} %  •  "
                  f"Preset: {result['preset']}"),
            text_color=result["color"],
        )
        self._reset_bench_buttons()
        messagebox.showinfo(result["title"], result["message"])

    def _bench_error(self, message: str):
        self.bench_status.configure(text=message, text_color=COLOR_DANGER)
        self._reset_bench_buttons()
        messagebox.showerror("Benchmark fehlgeschlagen", message)

    def _reset_bench_buttons(self):
        self.bench_btn.configure(state="normal", text="⚡  System testen & optimieren")
        self.start_btn.configure(state="normal")

    # ==================================================================
    # AUFNAHME
    # ==================================================================
    def _start_recording(self):
        folder = self.path_var.get().strip()
        if not os.path.isdir(folder):
            messagebox.showerror("Ungültiger Pfad", "Der Speicherort existiert nicht.")
            return

        audio_only = self.tabview.get() == TAB_AUDIO

        audio_label = self.audio_var.get()
        audio_device = self.audio_map.get(audio_label) if audio_label != NO_AUDIO_LABEL else None

        if audio_only:
            mode_region = False
            region = None
            if not audio_device:
                messagebox.showwarning(
                    "Keine Audioquelle",
                    "Bitte im Audio-Tab zuerst eine Aufnahmequelle "
                    "(Mikrofon oder Systemton) auswählen.",
                )
                return
        else:
            mode_region = self.mode_var.get() == MODE_REGION
            region = self.region
            if mode_region and not region:
                messagebox.showwarning("Kein Bereich", "Bitte zuerst einen Bereich wählen.")
                return

        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        extension = AUDIO_ONLY_EXTENSION if audio_only else "mp4"
        output_path = os.path.join(folder, f"Aufnahme_{timestamp}.{extension}")

        settings = {
            "output_path": output_path,
            "fps": self.fps_var.get(),
            "encoder": self.encoder_var.get(),
            "preset": self.preset_var.get(),
            "mode_region": mode_region,
            "region": region,
            "audio_device": audio_device,
            "audio_only": audio_only,
            "gain": self.mic_gain_var.get(),
            "denoise": self.denoise_var.get(),
        }

        # Vorschau-Geräte freigeben, BEVOR FFmpeg versucht, dieselbe
        # Audioquelle zu öffnen (siehe _pause_meters).
        self._pause_meters()

        self.recorder = RecorderThread(
            settings,
            on_started=lambda: self.after(0, self._on_recording_started),
            on_stopped=lambda p, s: self.after(0, self._on_recording_stopped, p, s),
            on_error=lambda m: self.after(0, self._on_recording_error, m),
        )
        self.recorder.start()

        self._set_status("Aufnahme wird gestartet ...", COLOR_ACCENT)

    def _on_recording_started(self):
        # Hauptfenster ausblenden -> spart RAM/CPU auf schwacher Hardware
        self.withdraw()

        audio_only = self.tabview.get() == TAB_AUDIO

        self.mini_panel = MiniPanel(
            self, fps=self.fps_var.get(),
            on_pause=self._toggle_pause, on_stop=self._stop_recording,
        )
        self.mini_panel.set_fps_text("Audio" if audio_only else f"{self.fps_var.get()} FPS")
        self._tick_timer()

    def _tick_timer(self):
        """Aktualisiert Timer + Blinkpunkt im Sekundentakt."""
        if not (self.recorder and self.mini_panel):
            return

        try:
            self.mini_panel.update_timer(self.recorder.get_elapsed())
            if not self.recorder.is_paused:
                self._blink_state = not self._blink_state
                self.mini_panel.blink_dot(self._blink_state)
        except Exception:
            return

        self._timer_job = self.after(500, self._tick_timer)

    def _toggle_pause(self) -> bool:
        return self.recorder.toggle_pause() if self.recorder else False

    def _stop_recording(self):
        if self._timer_job:
            self.after_cancel(self._timer_job)
            self._timer_job = None
        if self.recorder:
            self.recorder.stop()

    def _on_recording_stopped(self, path: str, success: bool):
        if self.mini_panel:
            try:
                self.mini_panel.destroy()
            except Exception:
                pass
            self.mini_panel = None

        self.deiconify()
        self.lift()
        self.recorder = None
        self._resume_meters()

        if success:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            self._set_status(
                f"✓ Gespeichert: {os.path.basename(path)}  ({size_mb:.1f} MB)",
                COLOR_SUCCESS,
            )
            if messagebox.askyesno(
                "Aufnahme fertig",
                f"Datei gespeichert:\n{os.path.basename(path)}  ({size_mb:.1f} MB)\n\n"
                "Ordner jetzt öffnen?",
            ):
                open_file_manager(path)
        else:
            self._set_status("Aufnahme fehlgeschlagen – keine Datei erzeugt.", COLOR_DANGER)

    def _on_recording_error(self, message: str):
        short = message.split("\n")[0][:200]
        self._set_status(short, COLOR_DANGER)
        messagebox.showerror("Aufnahmefehler", message[:600])

    # ==================================================================
    def _on_close(self):
        if self.recorder and self.recorder.is_active:
            if not messagebox.askyesno(
                "Aufnahme läuft",
                "Es läuft noch eine Aufnahme. Wirklich beenden?"
            ):
                return
            self.recorder.stop()
            self.recorder.join(timeout=8)

        if self._meter_poll_job:
            self.after_cancel(self._meter_poll_job)
            self._meter_poll_job = None
        self._pause_meters()

        if self.benchmark_thread and self.benchmark_thread.is_alive():
            self.benchmark_thread.cancel()

        self.destroy()
