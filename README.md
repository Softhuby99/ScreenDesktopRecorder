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
| `optimizer.py` | Nachträgliche Verkleinerung fertiger Videodateien (Thread) |
| `settings_store.py` | Merkt sich Tonquelle, Speicherort, FPS, Encoder und Preset zwischen zwei Starts (`%APPDATA%\ScreenRecPro\settings.json` bzw. `~/.config/ScreenRecPro/settings.json`) |

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

Die gewählte Tonquelle (und Speicherort, FPS, Encoder, Preset) bleibt
über einen Neustart hinweg erhalten. Die Pegelanzeige zeigt genau das
Gerät, das auch aufgenommen wird. Ist die Tonspur einer fertigen
Aufnahme komplett stumm (z. B. weil Windows den Mikrofonzugriff für
Desktop-Apps sperrt), warnt die App mit den möglichen Ursachen.

**Ton und Bild synchron:** Unter Windows bekommen Ton (DirectShow) und
Bild (ddagrab) eine gemeinsame Uhr. Früher setzte FFmpeg jede Quelle für
sich auf 0 - da die Bildquelle einige hundert ms später startet als die
Tonquelle, kam der Ton um genau diese (schwankende) Startverzögerung zu
spät. Für Geräte mit eigener Verzögerung (z. B. Bluetooth-Headsets) gibt
es im Audio-Tab zusätzlich den Regler **Ton-Versatz** (±500 ms).

**Ton vom PC selbst (Systemton) unter Windows:** FFmpeg kann unter
Windows nur Aufnahmegeräte (DirectShow) öffnen. Der Ton, der aus den
Lautsprechern kommt, ist nur aufnehmbar, wenn ein Gerät wie "Stereomix"
vorhanden und aktiviert ist - viele neuere Notebooks haben das nicht.

## Leistungstest ("System testen & optimieren")

1. **CPU-Messung (5 s):** Testbilder werden im Echtzeit-Takt mit der
   anspruchsvollsten Einstellung (60 FPS, Preset `medium`) kodiert, die
   CPU-Auslastung wird im Sekundentakt gemessen -> Entscheidungsmatrix
   (< 60 % / 60-85 % / > 85 %).
2. **Durchsatz-Kontrolle (je ca. 3 s):** Die gewählte Einstellung wird so
   schnell wie möglich kodiert - mit exakt denselben Encoder-Parametern
   wie die echte Aufnahme. Schafft der Encoder die Bildrate nicht mit
   40 % Reserve, wird schrittweise auf ein schnelleres Preset bzw. eine
   niedrigere Bildrate (bis 24 FPS) ausgewichen. So wird nichts
   empfohlen, bei dem die Aufnahme später Bilder verliert (Ruckeln).

## Video nachträglich verkleinern

Im Video-Tab gibt es unterhalb der eigentlichen Aufnahmeeinstellungen die
Karte "Video verkleinern (nachträglich)". Damit lässt sich eine bereits
aufgenommene Videodatei erneut durch FFmpeg schicken und deutlich
kleiner machen, ohne die Originaldatei zu verändern (die verkleinerte
Version landet als `<name>_optimiert.mp4` daneben).

Der Grund, warum das überhaupt etwas bringt: Während der Live-Aufnahme
muss FFmpeg mit einem sehr schnellen Preset (`ultrafast` bis `faster`)
kodieren, damit die Aufnahme in Echtzeit mithält - das kostet spürbar
Dateigröße. Bei der nachträglichen Verarbeitung gibt es diesen
Zeitdruck nicht mehr, sodass ein viel langsameres, effizienteres
Preset (und optional ein moderneren Codec) bei gleicher CRF-Qualität
eine deutlich kleinere Datei erzeugt.

Drei auswählbare Methoden ("Ausgewogen", "Kleiner (H.265)", "Maximale
Einsparung (H.265, langsam)") - Details siehe Antwort im Chat bzw. die
Beschreibungstexte direkt in der App. Der Fortschritt wird prozentual
angezeigt, ein laufender Vorgang lässt sich jederzeit abbrechen.
