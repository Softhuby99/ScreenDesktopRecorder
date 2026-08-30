# ScreenRec Pro

Ressourcenschonender Desktop-Screen-Recorder (Python + CustomTkinter + FFmpeg),
optimiert für flüssige Aufnahmen auch auf älteren/schwächeren Notebooks.

## Fertige Builds herunterladen (automatisch gebaut)

Bei jedem Push auf `main` baut eine GitHub Action automatisch zwei
eigenständige Programme auf echten Runnern der jeweiligen Plattform:

- **Windows:** `ScreenRecPro.exe` (inkl. eingebettetem FFmpeg, läuft ohne
  weitere Installation)
- **Linux:** `ScreenRecPro` (nutzt das systemweite FFmpeg - vorher einmalig
  `sudo apt install ffmpeg libportaudio2`; ohne `libportaudio2` startet die
  App trotzdem, aber die Mikrofon-/Lautsprecher-Vorschau im Audio-Tab
  bleibt auf "nicht verfügbar")

So kommst du an die Dateien:

1. Im Reiter **Actions** dieses Repos den neuesten erfolgreichen Lauf von
   "Builds (Windows + Linux)" öffnen.
2. Ganz unten bei **Artifacts** das gewünschte Paket herunterladen (ZIP):
   `ScreenRecPro-windows` oder `ScreenRecPro-linux`, und entpacken.

Der Workflow lässt sich auch manuell über "Run workflow" (Tab *Actions*)
anstoßen, ganz ohne neuen Commit.

## Lokal unter Linux starten (Entwicklung/Test)

```bash
python -m venv venv
source venv/bin/activate
pip install -r bin/requirements.txt
python main.py
```

Benötigt zusätzlich systemweit installiertes `ffmpeg` sowie `libportaudio2`
(für die Mikrofon-/Lautsprecher-Vorschau im Audio-Tab), z. B.:
`sudo apt install ffmpeg libportaudio2`.

## Lokal unter Windows bauen (Alternative zu GitHub Actions)

Siehe `bin/build_windows.bat` - erstellt eine isolierte virtuelle Umgebung,
installiert alle Abhängigkeiten sowie PyInstaller und baut
`dist\ScreenRecPro.exe`. Dafür muss vorher eine `bin\ffmpeg.exe`
(Windows-Build von FFmpeg) manuell abgelegt werden.

## Projektstruktur

| Datei | Zweck |
|---|---|
| `main.py` | Einstiegspunkt, Abhängigkeits-Check, Plattform-Vorbereitung |
| `gui_main.py` | Hauptfenster (CustomTkinter, Dark Mode) - Tabs "Video" und "Audio" |
| `gui_mini.py` | Schwebendes Mini-Bedienfeld während der Aufnahme |
| `gui_widgets.py` | Wiederverwendbare UI-Bausteine (z. B. der bunte Pegelbalken) |
| `benchmark.py` | Automatischer Performance-Test (Thread) |
| `recorder.py` | FFmpeg-Aufnahmesteuerung (Thread) |
| `ffmpeg_utils.py` | FFmpeg-Pfadauflösung und Kommando-Builder |
| `audio_devices.py` | Cross-platform Audiogeräte-Erkennung (Aufnahme + Live-Vorschau) |
| `audio_meter.py` | Echtzeit-Pegelmesser (RMS/Peak) für Mikrofon-/Lautsprecher-Vorschau |
| `region_selector.py` | Bereichsauswahl-Overlay |
| `platform_utils.py` | Betriebssystem-Abstraktion (Windows/Linux/macOS) |
| `config.py` | Zentrale Konstanten (Farben, Encoding-Presets, Benchmark-Matrix) |

## Video- und Audio-Tab

Das Hauptfenster ist in zwei Tabs aufgeteilt:

- **Video**: klassische Bildschirmaufnahme (Vollbild oder Bereich), mit
  Framerate, Video-Encoder (inkl. Intel Quick Sync H.264/HEVC/AV1) und
  optional einer zusätzlichen Audiospur.
- **Audio**: reine Tonaufnahme ohne Bild (Ausgabe als `.m4a`). Mikrofon
  und - sofern vom System bereitgestellt (Linux: PulseAudio-Monitor,
  Windows: "Stereo Mix") - der Systemton lassen sich mit einer live
  ausschlagenden Pegelanzeige auswählen; zusätzlich einstellbar sind eine
  digitale Verstärkung sowie eine einfache Rauschunterdrückung.

Welcher der beiden Tabs gerade offen ist, bestimmt, was der
"Start"-Button unten aufnimmt.
