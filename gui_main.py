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
import time
from datetime import datetime
from tkinter import filedialog, messagebox

import customtkinter as ctk

from audio_devices import (
    get_audio_sources, guess_microphone_default, guess_speaker_monitor_default,
    list_meter_devices, looks_like_system_audio,
)
from audio_meter import LevelMeter
from benchmark import BenchmarkThread
from config import (
    APP_NAME, APP_VERSION, AUDIO_ONLY_EXTENSION,
    COLOR_ACCENT, COLOR_ACCENT_HOVER, COLOR_BG_CARD, COLOR_BG_HOVER,
    COLOR_BG_INPUT, COLOR_BG_MAIN, COLOR_DANGER, COLOR_DANGER_HOVER, COLOR_SUCCESS,
    COLOR_TEXT_MUTED, COLOR_TEXT_PRIMARY, COLOR_WARNING,
    DEFAULT_ENCODER, DEFAULT_FPS, DEFAULT_PRESET,
    ENCODER_OPTIONS, FPS_OPTIONS,
    METER_REFRESH_MS, MIC_GAIN_DEFAULT, MIC_GAIN_MAX, MIC_GAIN_MIN, MIC_GAIN_STEPS,
    MODE_FULLSCREEN, MODE_OPTIONS, MODE_REGION,
    NO_AUDIO_LABEL, PRESET_OPTIONS, QSV_ENCODERS,
    RADIUS_LG, RADIUS_MD, RADIUS_SM,
)

try:
    # Wird von re-sync-to-repro.sh bei jedem Sync mit dem aktuellen
    # Zeitstempel ueberschrieben und ganz normal mitcommittet (siehe
    # build_info.py) - so laesst sich einer heruntergeladenen .exe
    # zweifelsfrei ansehen, welcher Stand tatsaechlich drin steckt,
    # unabhaengig von der von Hand gepflegten (und leicht mal
    # vergessenen) APP_VERSION.
    from build_info import BUILD_STAMP
except ImportError:
    BUILD_STAMP = "dev"

from ffmpeg_utils import check_encoder_available, check_qsv_available, get_ffmpeg_path
from gui_mini import MiniPanel
from optimizer import (
    OPTIMIZE_PROFILES, OptimizeThread, get_profile, probe_duration_seconds,
    probe_media_info, suggest_output_path,
)
from gui_widgets import LevelMeterBar
from platform_utils import (
    IS_WINDOWS, even_dimensions, get_default_videos_dir, get_platform_warning,
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
        self.title(f"{APP_NAME} v{APP_VERSION} [{BUILD_STAMP}]")
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

        # Video nachträglich verkleinern (siehe optimizer.py/_build_optimize_card)
        self._optimize_input_path: str | None = None
        self._optimize_thread: OptimizeThread | None = None

        # Mikrofon-/Lautsprecher-Vorschau (Audio-Tab)
        self._meter_devices: list[tuple[str, int]] = []
        self._active_audio_only = False  # siehe _start_recording/_on_recording_started
        self._recording_had_audio = False  # war eine Tonquelle gewaehlt?
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
            wraplength=410, justify="left",
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

        if IS_WINDOWS:
            # Windows' gdigrab nimmt im Vollbild-Modus IMMER die gesamte
            # virtuelle Anzeige auf - bei mehreren Monitoren also alle
            # zusammen als ein (entsprechend breites/hohes) Video, nicht
            # nur den Hauptbildschirm. Das ist für die meisten Nutzer nicht
            # offensichtlich, deshalb hier einmalig als Hinweis sichtbar
            # (nicht erst nach dem Start als Überraschung im fertigen Video).
            ctk.CTkLabel(
                settings_card,
                text="ℹ  \"Vollbild\" nimmt bei mehreren Monitoren alle "
                     "zusammen auf. Nur einen Bildschirm? \"Bereich\" wählen "
                     "und den gewünschten Monitor aufziehen.",
                font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
                anchor="w", wraplength=410, justify="left",
            ).pack(fill="x", padx=14, pady=(0, 4))

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

        self._build_optimize_card(scroll)

    def _build_optimize_card(self, parent):
        """
        Nachträgliche Verkleinerung EINER bereits fertigen Videodatei -
        unabhängig von der eigentlichen Aufnahme (siehe optimizer.py).
        Läuft komplett separat vom Aufnahme-Start/Stop-Mechanismus, hat
        also einen eigenen Button/Fortschrittsbalken/Re-Entrancy-Schutz.
        """
        card = self._make_card(parent)
        self._card_title(card, "Video verkleinern (nachträglich)")

        ctk.CTkLabel(
            card,
            text="Kodiert eine bereits aufgenommene Datei mit einem "
                 "langsameren, effizienteren Verfahren neu - deutlich "
                 "kleinere Datei, ohne die Bildqualität sichtbar zu "
                 "verschlechtern. Die Originaldatei bleibt unverändert "
                 "erhalten, es entsteht eine neue Datei daneben.",
            font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
            anchor="w", wraplength=410, justify="left",
        ).pack(fill="x", padx=14, pady=(0, 10))

        row = self._labeled_row(card, "Datei")
        self.optimize_file_label = ctk.CTkLabel(
            row, text="Keine Datei gewählt", font=("Segoe UI", 12),
            text_color=COLOR_TEXT_MUTED, anchor="w",
        )
        self.optimize_file_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            row, text="Wählen …", width=90, height=30,
            corner_radius=RADIUS_SM, fg_color=COLOR_BG_INPUT,
            hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
            command=self._choose_optimize_file,
        ).pack(side="right")

        row = self._labeled_row(card, "Methode")
        self.optimize_profile_var = ctk.StringVar(value=OPTIMIZE_PROFILES[0]["label"])
        ctk.CTkOptionMenu(
            row, values=[p["label"] for p in OPTIMIZE_PROFILES],
            variable=self.optimize_profile_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
            command=self._on_optimize_profile_change,
        ).pack(side="right", fill="x", expand=True)

        self.optimize_profile_desc = ctk.CTkLabel(
            card, text=OPTIMIZE_PROFILES[0]["description"],
            font=("Segoe UI", 10), text_color=COLOR_TEXT_MUTED,
            anchor="w", wraplength=410, justify="left",
        )
        self.optimize_profile_desc.pack(fill="x", padx=14, pady=(0, 10))

        self.optimize_progress = ctk.CTkProgressBar(
            card, height=10, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, progress_color=COLOR_ACCENT,
        )
        self.optimize_progress.set(0)
        self.optimize_progress.pack(fill="x", padx=14, pady=(0, 6))

        self.optimize_status = ctk.CTkLabel(
            card, text="", font=("Segoe UI", 11),
            text_color=COLOR_TEXT_MUTED, anchor="w",
        )
        self.optimize_status.pack(fill="x", padx=14, pady=(0, 10))

        self.optimize_btn = ctk.CTkButton(
            card, text="🗜  Video verkleinern", height=36,
            corner_radius=RADIUS_SM, fg_color=COLOR_ACCENT,
            hover_color=COLOR_ACCENT_HOVER, font=("Segoe UI", 12, "bold"),
            command=self._start_optimize,
        )
        self.optimize_btn.pack(fill="x", padx=14, pady=(0, 14))

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
        # eine Auswahl gilt für beide Tabs, kein doppelter Zustand. Die
        # Werteliste (values=) wird von jedem CTkOptionMenu SEPARAT
        # verwaltet, auch wenn beide dieselbe Variable teilen - deshalb
        # als self.audio_menu_audio_tab merken, damit _apply_audio_devices()
        # auch dieses zweite Dropdown befüllen kann (siehe dort).
        self.audio_menu_audio_tab = ctk.CTkOptionMenu(
            row, values=[NO_AUDIO_LABEL], variable=self.audio_var,
            width=250, height=34, corner_radius=RADIUS_SM,
            fg_color=COLOR_BG_INPUT, button_color=COLOR_BG_INPUT,
            button_hover_color=COLOR_BG_HOVER, font=("Segoe UI", 12),
        )
        self.audio_menu_audio_tab.pack(side="right", fill="x", expand=True)

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
            anchor="w", wraplength=410, justify="left",
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
            anchor="w", wraplength=410, justify="left",
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

    def _current_screen_size(self) -> tuple[int, int]:
        """
        Ermittelt die Bildschirmauflösung ÜBER DAS BEREITS VORHANDENE
        Hauptfenster (self) statt ein zweites Tk-Root zu öffnen wie
        platform_utils.get_screen_size() es täte. WICHTIG: nur vom
        GUI-Thread aus aufrufen (self.winfo_...() ist Tkinter) - das
        Ergebnis wird dann an RecorderThread/BenchmarkThread als fertiger
        Wert übergeben, damit diese Worker-Threads selbst NIE Tkinter
        anfassen müssen (siehe platform_utils.get_screen_size-Docstring).
        """
        return even_dimensions(self.winfo_screenwidth(), self.winfo_screenheight())

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
        # WICHTIG: NICHT direkt {display: ident for display, ident in sources}
        # - zwei Geräte mit identischem Anzeigenamen (z. B. zwei baugleiche
        # USB-Mikrofone, oder zwei DirectShow-Geräte mit generischem Namen
        # wie "Mikrofon (USB Audio Device)") würden sich sonst im Dict
        # gegenseitig überschreiben: das Dropdown zeigt dann nur EINEN
        # Eintrag, und das erste der beiden echten Geräte wäre dauerhaft
        # nicht auswählbar. Deshalb werden doppelte Anzeigenamen hier mit
        # " (2)", " (3)", ... eindeutig gemacht, bevor sie in die Map
        # wandern - die zugrunde liegende FFmpeg-ID bleibt dabei pro Gerät
        # exakt erhalten.
        self.audio_map = {}
        seen: dict[str, int] = {}
        display_values = []
        for display, ident in sources:
            seen[display] = seen.get(display, 0) + 1
            unique_display = display if seen[display] == 1 else f"{display} ({seen[display]})"
            self.audio_map[unique_display] = ident
            display_values.append(unique_display)
        values = [NO_AUDIO_LABEL] + display_values
        # Beide Dropdowns (Video-Tab UND Audio-Tab) teilen zwar dieselbe
        # StringVar, aber .configure(values=...) muss auf JEDEM der beiden
        # Widgets einzeln aufgerufen werden - sonst bleibt das zweite in
        # seiner Popup-Liste beim Startwert hängen, obwohl der angezeigte
        # Text (über die gemeinsame Variable) korrekt aktualisiert wirkt.
        self.audio_menu.configure(values=values)
        self.audio_menu_audio_tab.configure(values=values)
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
        # looks_like_system_audio() prüft Stichworte im Klartext-Namen,
        # nicht nur ein Emoji-Präfix - auf Windows/Fallback beginnen sonst
        # ALLE Geräte (auch Stereo Mix/Loopback) mit "🎤", wodurch die
        # Vorschau dort fälschlich immer die Mikrofon-Kategorie annehmen
        # würde (siehe Modul-Kommentar in audio_devices.looks_like_system_audio).
        is_system_audio = looks_like_system_audio(label)
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
            # Falls die App inzwischen geschlossen wurde (self.destroy()
            # bereits gelaufen), waere self.after(...) hier ein Zugriff auf
            # ein bereits zerstoertes Tk-Fenster - dieser (daemon-)Thread
            # wird dadurch nicht selbst zum Problem (der zugrunde liegende
            # FFmpeg-Testlauf ist ueber sein eigenes subprocess.run(timeout=...)
            # ohnehin spaetestens nach ~10s garantiert beendet), aber ohne
            # dieses try/except wuerde eine Exception im Hintergrund-Thread
            # als Traceback auf stderr landen.
            try:
                self.after(0, self._apply_encoder_check, encoder, available, fallback_msg, success_text)
            except Exception:
                pass

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
    # VIDEO NACHTRÄGLICH VERKLEINERN (siehe optimizer.py)
    # ==================================================================
    def _choose_optimize_file(self):
        path = filedialog.askopenfilename(
            title="Videodatei wählen",
            filetypes=[
                ("Videodateien", "*.mp4 *.mov *.mkv *.avi"),
                ("Alle Dateien", "*.*"),
            ],
        )
        if path:
            self._set_optimize_file(path)

    def _set_optimize_file(self, path: str):
        """
        Setzt die Zieldatei fürs Verkleinern - sowohl per manueller Auswahl
        (_choose_optimize_file) als auch automatisch nach einer fertigen
        Video-Aufnahme (siehe _on_recording_stopped), damit der naheliegende
        Ablauf "gerade aufgenommen -> gleich verkleinern" keinen Umweg über
        den Dateidialog braucht.
        """
        self._optimize_input_path = path
        self.optimize_file_label.configure(
            text=os.path.basename(path), text_color=COLOR_TEXT_PRIMARY,
        )

    def _on_optimize_profile_change(self, label: str):
        profile = next((p for p in OPTIMIZE_PROFILES if p["label"] == label), None)
        if profile:
            self.optimize_profile_desc.configure(text=profile["description"])

    def _start_optimize(self):
        # Re-Entrancy-Schutz, gleiches Prinzip wie bei _start_recording:
        # ein zweiter Klick waehrend eine Optimierung schon laeuft wuerde
        # sonst einen zweiten, konkurrierenden FFmpeg-Prozess auf dieselbe
        # Ausgabedatei starten.
        if self._optimize_thread is not None and self._optimize_thread.is_alive():
            return

        if not self._optimize_input_path or not os.path.isfile(self._optimize_input_path):
            messagebox.showwarning(
                "Keine Datei", "Bitte zuerst eine Videodatei auswählen.",
            )
            return

        profile = get_profile(self._profile_id_for_label(self.optimize_profile_var.get()))
        output_path = suggest_output_path(self._optimize_input_path)

        self.optimize_btn.configure(
            text="✕  Abbrechen", fg_color=COLOR_DANGER, hover_color=COLOR_DANGER_HOVER,
            command=self._cancel_optimize,
        )
        self.optimize_progress.set(0)
        self.optimize_status.configure(
            text="Starte Optimierung …", text_color=COLOR_TEXT_MUTED,
        )

        self._optimize_thread = OptimizeThread(
            self._optimize_input_path, output_path, profile,
            on_progress=lambda p: self.after(0, self._on_optimize_progress, p),
            on_finish=lambda out, orig, new: self.after(0, self._on_optimize_finish, out, orig, new),
            on_error=lambda m: self.after(0, self._on_optimize_error, m),
        )
        self._optimize_thread.start()

    def _profile_id_for_label(self, label: str) -> str:
        for profile in OPTIMIZE_PROFILES:
            if profile["label"] == label:
                return profile["id"]
        return OPTIMIZE_PROFILES[0]["id"]

    def _cancel_optimize(self):
        if self._optimize_thread:
            self._optimize_thread.cancel()
        self.optimize_status.configure(text="Wird abgebrochen …", text_color=COLOR_TEXT_MUTED)
        self.optimize_btn.configure(state="disabled")
        self.after(200, self._poll_optimize_cancelled)

    def _poll_optimize_cancelled(self):
        if self._optimize_thread and self._optimize_thread.is_alive():
            self.after(200, self._poll_optimize_cancelled)
            return
        self.optimize_status.configure(text="Abgebrochen.", text_color=COLOR_TEXT_MUTED)
        self.optimize_progress.set(0)
        self._reset_optimize_button()

    def _on_optimize_progress(self, percent: float | None):
        if percent is None:
            self.optimize_status.configure(text="Wird verkleinert …")
            return
        self.optimize_progress.set(percent / 100)
        self.optimize_status.configure(text=f"Wird verkleinert … {percent:.0f} %")

    def _on_optimize_finish(self, output_path: str, original_bytes: int, new_bytes: int):
        self.optimize_progress.set(1.0)
        self._reset_optimize_button()

        orig_mb = original_bytes / (1024 * 1024)
        new_mb = new_bytes / (1024 * 1024)
        saved_pct = (1 - new_bytes / original_bytes) * 100 if original_bytes else 0.0
        self.optimize_status.configure(
            text=f"✓ {orig_mb:.1f} MB → {new_mb:.1f} MB  ({saved_pct:.0f} % kleiner)",
            text_color=COLOR_SUCCESS,
        )
        if messagebox.askyesno(
            "Verkleinerung fertig",
            f"Neue Datei:\n{os.path.basename(output_path)}\n\n"
            f"{orig_mb:.1f} MB → {new_mb:.1f} MB  ({saved_pct:.0f} % kleiner)\n\n"
            "Ordner jetzt öffnen?",
        ):
            open_file_manager(output_path)

    def _on_optimize_error(self, message: str):
        self._reset_optimize_button()
        self.optimize_progress.set(0)
        self.optimize_status.configure(text="Fehlgeschlagen.", text_color=COLOR_DANGER)
        messagebox.showerror("Optimierung fehlgeschlagen", message[:600])

    def _reset_optimize_button(self):
        self.optimize_btn.configure(
            state="normal", text="🗜  Video verkleinern",
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            command=self._start_optimize,
        )

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
            screen_size=self._current_screen_size(),
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
        # Re-Entrancy-Schutz: ohne dies würde ein Doppelklick innerhalb des
        # kurzen Fensters, bis FFmpeg tatsächlich gestartet ist (_on_recording_started
        # kommt asynchron erst nach dem Prozessstart), einen zweiten,
        # verwaisten RecorderThread/FFmpeg-Prozess samt zweitem MiniPanel
        # erzeugen. self.recorder ist nur zwischen Start und Stop gesetzt.
        if self.recorder is not None:
            return

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
            # Auf dem GUI-Thread ermittelt - siehe _current_screen_size().
            "screen_size": self._current_screen_size(),
        }

        # Für _on_recording_started() (siehe dort): merken, WELCHER Modus
        # tatsächlich in diesem FFmpeg-Kommando gelandet ist - der Nutzer
        # kann den Tab ja theoretisch noch wechseln, bevor _on_recording_started
        # (kommt asynchron ~0.8s später) feuert. Dort erneut self.tabview.get()
        # abzufragen würde dann fälschlich den NEUEN Tab statt des tatsächlich
        # aufgenommenen Modus anzeigen (nur die MiniPanel-Beschriftung wäre
        # betroffen, nicht die Aufnahme selbst - aber trotzdem irreführend).
        self._active_audio_only = audio_only
        # Merken, ob ueberhaupt eine Tonquelle im Spiel war - nur dann ist
        # eine fehlende Audiospur in der fertigen Datei ein Befund und
        # keine voellig normale Bild-ohne-Ton-Aufnahme.
        self._recording_had_audio = bool(settings.get("audio_device")) or audio_only

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

        # Den tatsächlich aufgenommenen Modus verwenden (siehe _start_recording),
        # NICHT erneut self.tabview.get() - der Tab könnte inzwischen
        # gewechselt worden sein, während FFmpeg noch startete.
        audio_only = self._active_audio_only

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
        # Vor dem Zuruecksetzen sichern - wird gebraucht, um eine evtl.
        # vorzeitig abgeschnittene Aufnahme zu erkennen (siehe
        # _check_recording_truncated) UND um im Fehlerfall die rohe
        # FFmpeg-Ausgabe in eine Log-Datei schreiben zu koennen, statt
        # sie nur (gekuerzt) in einer Dialogbox zu zeigen, die leicht
        # weggeklickt wird, bevor jemand den Text kopieren kann.
        elapsed = self.recorder.get_elapsed() if self.recorder else 0.0
        stderr_lines = list(self.recorder._stderr_buffer) if self.recorder else []
        command = list(self.recorder.command) if self.recorder else []

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

        # IMMER ein Log schreiben - unabhaengig von Erfolg oder Fehler.
        # Frueher entstand nur im Verdachtsfall eines, wodurch ausgerechnet
        # die schlimmsten Faelle (Datei unbrauchbar oder gar nicht erst
        # angelegt) voellig spurlos blieben. Ein kleines Textfile pro
        # Aufnahme ist der Preis dafuer, dass ein Problem immer
        # nachvollziehbar ist.
        log_path = self._write_recording_log(path, stderr_lines, command, elapsed, success)

        if success:
            size_mb = os.path.getsize(path) / (1024 * 1024)
            self._set_status(
                f"✓ Gespeichert: {os.path.basename(path)}  ({size_mb:.1f} MB)",
                COLOR_SUCCESS,
            )
            # Nur bei echten VIDEO-Aufnahmen als Vorauswahl für "Video
            # verkleinern" übernehmen - eine reine .m4a-Tonaufnahme hat
            # keinen Videostream, den die Optimierungsprofile (allesamt
            # Video-Encoder) verarbeiten könnten.
            if not self._active_audio_only:
                self._set_optimize_file(path)
            threading.Thread(
                target=self._check_recording_truncated,
                args=(path, elapsed, log_path),
                daemon=True,
            ).start()
            if messagebox.askyesno(
                "Aufnahme fertig",
                f"Datei gespeichert:\n{os.path.basename(path)}  ({size_mb:.1f} MB)\n\n"
                "Ordner jetzt öffnen?",
            ):
                open_file_manager(path)
        else:
            hint = f"  (Details: {os.path.basename(log_path)})" if log_path else ""
            self._set_status(
                f"Aufnahme fehlgeschlagen – keine verwertbare Datei.{hint}",
                COLOR_DANGER,
            )

    def _check_recording_truncated(self, path: str, elapsed_seconds: float, log_path: str | None):
        """
        Läuft im Hintergrund (Dateidauer-Sondierung kann kurz dauern) und
        vergleicht die tatsächlich vergangene Aufnahmezeit mit der Dauer
        der fertigen Datei.

        Hintergrund: ein Aussetzer der Audioaufnahme (z. B. Puffer-
        Überlauf beim Mikrofon) kann FFmpeg dazu bringen, den Audio-Input
        vorzeitig zu beenden - das passiert mit sauberem Exit-Code, wird
        also von RecorderThread NICHT als Fehler erkannt und bislang auch
        nicht dem Nutzer gemeldet. Ergebnis: eine "erfolgreich" gemeldete,
        aber nur wenige Sekunden/KB große Datei. Diese Prüfung holt das
        nachträglich sichtbar nach.
        """
        if elapsed_seconds < 4:
            return  # zu kurz fuer eine verlaessliche Aussage
        try:
            duration = probe_duration_seconds(path)
        except Exception:
            duration = None

        if duration is None:
            # Die Dauer liess sich nicht einmal auslesen - genau der Fall
            # "Datei laesst sich gar nicht abspielen".
            self._append_recording_log(
                log_path,
                f"BEFUND: Aufnahme lief {elapsed_seconds:.0f}s, aber die Dauer der "
                "Datei ist nicht auslesbar - Datei vermutlich beschaedigt/unvollstaendig.",
            )
            self.after(0, self._warn_unreadable_file, log_path)
            return

        self._append_recording_log(
            log_path, f"Dauer laut Datei: {duration:.1f}s (Aufnahmezeit: {elapsed_seconds:.1f}s)"
        )

        # Streams der fertigen Datei protokollieren. Die Containerdauer
        # allein verraet NICHT, ob die Tonspur vorhanden ist oder
        # vorzeitig endet - "Datei volle Laenge, aber Ton nur kurz" sieht
        # an der Gesamtdauer voellig unauffaellig aus.
        info = probe_media_info(path)
        self._append_recording_log(log_path, "Inhalt der Datei laut FFmpeg:\n" + info)

        if "Audio:" not in info:
            # Es wurde eine Tonquelle gewaehlt, aber es gibt gar keinen
            # Audiostream - das ist ein eindeutiger Befund, kein Verdacht.
            if self._recording_had_audio:
                self._append_recording_log(
                    log_path,
                    "BEFUND: Es war eine Tonquelle ausgewaehlt, die Datei enthaelt "
                    "aber ueberhaupt keinen Audiostream.",
                )
                self.after(0, self._warn_no_audio_stream, log_path)

        if duration < elapsed_seconds * 0.7 - 1.0:
            self._append_recording_log(
                log_path,
                f"BEFUND: Datei deutlich kuerzer als die Aufnahmezeit "
                f"({duration:.0f}s statt {elapsed_seconds:.0f}s) - vorzeitig abgeschnitten.",
            )
            self.after(0, self._warn_possible_truncation, elapsed_seconds, duration, log_path)

    def _write_recording_log(self, video_path: str, stderr_lines: list, command: list,
                             elapsed_seconds: float, success: bool) -> str | None:
        """
        Schreibt nach JEDER Aufnahme ein Protokoll neben die Datei -
        auch bei Erfolg.

        Zweck: eine Dialogbox zeigt nur gekürzten Text und wird leicht
        weggeklickt, bevor jemand ihn kopieren kann; und die frühere
        Variante, nur im Verdachtsfall zu protokollieren, ließ
        ausgerechnet die schlimmsten Fälle (Datei unbrauchbar oder gar
        nicht erst angelegt) spurlos verschwinden.

        Neben der FFmpeg-Ausgabe wird bewusst auch das vollständige
        FFmpeg-Kommando festgehalten: ohne die tatsächlich verwendeten
        Parameter lässt sich ein Fehlerbild kaum nachvollziehen.
        """
        try:
            log_path = os.path.splitext(video_path)[0] + "_aufnahme-log.txt"
            try:
                size_info = f"{os.path.getsize(video_path)} Bytes"
            except OSError:
                size_info = "Datei nicht vorhanden"

            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"ScreenRec Pro v{APP_VERSION} [{BUILD_STAMP}]\n")
                f.write(f"Zeitpunkt:      {datetime.now().isoformat(timespec='seconds')}\n")
                f.write(f"Ergebnis:       {'OK' if success else 'FEHLGESCHLAGEN'}\n")
                f.write(f"Aufnahmedauer:  {elapsed_seconds:.1f}s\n")
                f.write(f"Datei:          {os.path.basename(video_path)} ({size_info})\n")
                f.write("\nFFmpeg-Kommando:\n")
                f.write("-" * 60 + "\n")
                f.write(" ".join(command) if command else "(nicht erfasst)")
                f.write("\n\nFFmpeg-Ausgabe (stderr, letzte Zeilen):\n")
                f.write("-" * 60 + "\n")
                f.write("\n".join(stderr_lines) if stderr_lines else "(keine Ausgabe erfasst)")
                f.write("\n")
            return log_path
        except Exception:
            return None

    def _append_recording_log(self, log_path: str | None, text: str):
        """
        Haengt einen nachtraeglich ermittelten Befund an ein bereits
        geschriebenes Protokoll an (die Dauerpruefung laeuft erst nach
        dem Schreiben des Logs im Hintergrund).
        """
        if not log_path:
            return
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{text}\n")
        except Exception:
            pass

    def _warn_no_audio_stream(self, log_path: str | None):
        self._set_status("⚠ Aufnahme enthält keine Tonspur", COLOR_WARNING)
        log_hint = (
            f"\n\nDetails im Protokoll:\n{os.path.basename(log_path)}"
            if log_path else ""
        )
        messagebox.showwarning(
            "Keine Tonspur in der Aufnahme",
            "Es war eine Tonquelle ausgewählt, die fertige Datei enthält "
            "aber keinen Audiostream.\n\n"
            "Meist liegt das daran, dass das Aufnahmegerät von einem anderen "
            "Programm belegt ist oder Windows der App den Mikrofonzugriff "
            "verweigert (Einstellungen → Datenschutz → Mikrofon)."
            f"{log_hint}",
        )

    def _warn_unreadable_file(self, log_path: str | None):
        self._set_status("⚠ Datei beschädigt – Dauer nicht auslesbar", COLOR_DANGER)
        log_hint = (
            f"\n\nDetails wurden gespeichert in:\n{os.path.basename(log_path)}"
            if log_path else ""
        )
        messagebox.showwarning(
            "Aufnahme vermutlich beschädigt",
            "Die Datei wurde zwar angelegt, lässt sich aber nicht auslesen – "
            "sie ist vermutlich unvollständig und nicht abspielbar.\n\n"
            "Das passiert, wenn FFmpeg die Aufnahme nicht sauber abschließen "
            "konnte (die Datei bekommt dann keinen gültigen Abschluss-Block)."
            f"{log_hint}",
        )

    def _warn_possible_truncation(self, elapsed_seconds: float, duration: float, log_path: str | None):
        self._set_status(
            f"⚠ Datei evtl. unvollständig: {duration:.0f}s statt {elapsed_seconds:.0f}s",
            COLOR_WARNING,
        )
        log_hint = (
            f"\n\nDetails wurden gespeichert in:\n{os.path.basename(log_path)}"
            if log_path else ""
        )
        messagebox.showwarning(
            "Aufnahme möglicherweise unvollständig",
            f"Die Aufnahme lief {elapsed_seconds:.0f} s, die gespeicherte Datei "
            f"enthält aber nur {duration:.0f} s.\n\n"
            "Das deutet auf einen Aussetzer der Audioaufnahme während der "
            "Aufnahme hin (z. B. ein kurzzeitiger Mikrofon-Puffer-Überlauf), "
            "durch den Video und Audio vorzeitig beendet wurden.\n\n"
            "Falls das öfter passiert: anderes Mikrofon/Audiogerät probieren "
            "oder testweise ganz ohne Audiospur aufnehmen."
            f"{log_hint}",
        )

    def _on_recording_error(self, message: str):
        # Das Protokoll schreibt _on_recording_stopped, das unmittelbar
        # nach diesem Callback ohnehin immer laeuft (auch im Fehlerfall) -
        # hier also bewusst KEIN eigenes Log, sonst wuerde es gleich
        # darauf ueberschrieben.
        short = message.split("\n")[0][:200]
        self._set_status(short, COLOR_DANGER)
        messagebox.showerror(
            "Aufnahmefehler",
            message[:600]
            + "\n\nEin Protokoll mit dem vollständigen FFmpeg-Aufruf und "
              "seiner Ausgabe liegt neben der Aufnahmedatei "
              "(…_aufnahme-log.txt).",
        )

    def _wait_for_recorder_shutdown(self):
        """
        Wartet auf das Ende von RecorderThread, OHNE dabei die komplette
        Tk-Ereignisschleife zu blockieren wie ein einzelnes langes join()
        es taete.

        RecorderThread._graceful_stop() hat selbst ein Eskalations-Budget
        von bis zu ~18s (wait 10s -> terminate+wait 5s -> kill+wait 3s),
        um FFmpeg das MOOV-Atom sauber finalisieren zu lassen. Ein reines
        `self.recorder.join(timeout=20)` wuerde in dieser Zeit die
        Ereignisschleife komplett anhalten - unter Windows fuehrt das
        typischerweise dazu, dass das Fenster im Titel "(Keine Rückmeldung)"
        anzeigt, obwohl im Hintergrund alles wie vorgesehen laeuft. Deshalb
        hier stattdessen in kleinen Schritten warten und zwischendurch
        self.update() aufrufen, WAEHREND eine Statuszeile anzeigt, dass
        bewusst noch (kurz) gewartet wird.
        """
        self._set_status("Aufnahme wird finalisiert – bitte warten ...", COLOR_ACCENT)
        try:
            self.update()
        except Exception:
            pass

        deadline = time.time() + 20
        while self.recorder.is_alive() and time.time() < deadline:
            self.recorder.join(timeout=0.2)
            try:
                self.update()
            except Exception:
                # Fenster wurde zwischenzeitlich zerstoert - nichts mehr zu tun.
                break

        if self.recorder.is_alive():
            # Letzte Instanz: sollte in ~20s wirklich nichts geklappt haben
            # (haengender Thread), FFmpeg hart beenden statt die App ewig
            # offenzuhalten oder als Waisenprozess weiterlaufen zu lassen.
            self.recorder.force_kill()
            self.recorder.join(timeout=3)

    # ==================================================================
    def _on_close(self):
        if self.recorder and self.recorder.is_active:
            if not messagebox.askyesno(
                "Aufnahme läuft",
                "Es läuft noch eine Aufnahme. Wirklich beenden?"
            ):
                return
            self.recorder.stop()
            self._wait_for_recorder_shutdown()

        if self._meter_poll_job:
            self.after_cancel(self._meter_poll_job)
            self._meter_poll_job = None
        self._pause_meters()

        if self.benchmark_thread and self.benchmark_thread.is_alive():
            self.benchmark_thread.cancel()

        if self._optimize_thread and self._optimize_thread.is_alive():
            # Kein Datenverlust-Risiko wie bei einer laufenden Aufnahme (die
            # Originaldatei bleibt so oder so unangetastet) - deshalb ohne
            # Rückfrage einfach abbrechen; die unfertige Ausgabedatei räumt
            # OptimizeThread.cancel() selbst auf.
            self._optimize_thread.cancel()

        self.destroy()
