# ScreenRec Pro

Ressourcenschonender Desktop-Screen-Recorder (Python + CustomTkinter + FFmpeg),
optimiert für flüssige Aufnahmen auch auf älteren/schwächeren Notebooks.

## Fertige Builds herunterladen (automatisch gebaut)

**Gilt nur im Ursprungs-Repo ScreenDesktopRecorder.** Die Workflow-Datei
liegt bewusst unter `projects/ScreenRecPro/.github/workflows/build.yml`:
Im Ursprungs-Repo ist dieser Projektordner das Checkout-Root, dort ist sie
eine gültige GitHub Action. Im Claudex-Repo liegt sie nicht im
Repo-Root-`.github/` und läuft deshalb absichtlich **nicht** - hier gibt es
keine automatischen Builds. Der folgende Ablauf beschreibt das
Ursprungs-Repo; im Windows-Build ist nur `bin\ffmpeg.exe` gebündelt (kein
ffprobe).

Bei jedem Push auf `main` (im Ursprungs-Repo) baut die GitHub Action
automatisch zwei eigenständige Programme auf echten Runnern der jeweiligen
Plattform:

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
| `diagnosis.py` | Abbrechbare Nachprüfung fertiger Aufnahmen (Dauer, Stille, Spur-Enden) |
| `recording_validation.py` | Pflichtspur- und Spur-Endeprüfung mit FFmpeg (ohne ffprobe) |
| `gui_logic.py` | GUI-freie Entscheidungslogik des Hauptfensters (Schließen, Quelltyp) |
| `tools/` | Historische Diagnose-Skripte (keine Tests), Windows-Diagnose, Sync-Werkzeuge |
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

**Ton/Bild-Zeitstempel:** Die Aufnahme verwendet die nativen Zeitstempel
(Basis der erfolgreichen Testvariante B02). Die fehlerhafte Wallclock-/copyts-
Kombination und die heuristische Verwerfung/Verschiebung von Audiobloecken
wurden entfernt. Ein verbleibender Start-/Geraeteversatz ist damit noch nicht
automatisch korrigiert. Im Audio-Tab steht dafuer **Ton-Versatz** (±500 ms)
zur Verfuegung: negativ = Ton frueher, positiv = Ton spaeter.

**Aufnahmepruefung:** Nach dem Stoppen prueft FFmpeg die angeforderten Spuren
und dekodiert den Anfang. Fehlende Spuren, unlesbare Dateien und bekannte
schwere Aufnahmefehler werden auch bei Exitcode 0 als Fehler gemeldet.
Diese kurze Pruefung garantiert weder eine fehlerfreie gesamte Datei noch
inhaltliche Synchronitaet. Danach prueft die Diagnose im Hintergrund, ob eine
erwartete Spur deutlich vor dem Ende der Erfassungsdauer endet (Toleranz 2 s
plus Ton-Versatz); das wird protokolliert und als Warnung gezeigt, macht die
Aufnahme aber nicht ungueltig.

**Schliessen waehrend einer Aufnahme:** Die App fragt "Aufnahme speichern?" -
Ja speichert (wartet die Finalisierung ab), Nein verwirft die Datei sofort,
Abbrechen laesst die Aufnahme weiterlaufen.

**Systemton unter Windows:** Der derzeitige DirectShow-Aufnahmeweg benoetigt
fuer Systemton eine Quelle wie „Stereomix“. Windows selbst unterstuetzt auch
WASAPI-Loopback ohne Stereomix; dieser zusaetzliche Aufnahmeweg ist noch nicht
integriert. Monitorlautsprecher ueber das Mikrofon aufzunehmen erfasst auch
die Wiedergabe-, Raum- und Mikrofonverzoegerung.

### Synchronitaet messen

Im Projektordner (dieselbe Python-Umgebung wie fuer die App):

```bash
python sync_check.py generate sync_reference.mp4 --duration 30
```

Das Video abspielen und dessen Bildbereich mit dem Recorder aufnehmen;
vor dem ersten Blitz starten und bis zum Ende aufnehmen. Fuer den bisherigen
Mikrofontest die Toene ueber die Monitorlautsprecher abspielen und andere
Geraeusche vermeiden. Danach:

```bash
python sync_check.py measure aufnahme.mp4
```

Bei Vollbildaufnahmen mit Player-Rahmen oder anderen hellen Flaechen den
Testbildbereich mit `--crop Breite:Hoehe:X:Y` angeben. `--threshold -25`
ist die Ton-Erkennungsschwelle in dB und bei leisen Aufnahmen anzupassen.
Das Werkzeug meldet pro Ereignis den Tonversatz, dessen Median und die
Aenderung zwischen erstem und letztem Ereignis. Positiv bedeutet Ton zu spaet.
Es bricht bei ungleicher Ereigniszahl oder unregelmaessigen Abstaenden ab.
Fehlen am Anfang/Ende ganze Ereignispaare, kann eine periodische Sequenz
mehrdeutig sein: deshalb immer die gesamte Sequenz aufnehmen.

Ein konstanter Versatz laesst sich mit dem Regler korrigieren; wechselnder
Versatz oder Drift nicht. Bei 30 FPS ist die Bildaufloesung etwa 33 ms.
Mehrere Starts und eine laengere Sequenz (z. B. `--duration 180`) vergleichen.
Pause/Resume verwendet weiterhin Prozess-Suspend und ist noch nicht fuer
synchronitaetskritische Aufnahmen abgesichert; fuer diese Tests nicht pausieren.

Die alten A/B-Skripte (frueher `test_audio_sync.py`/`test_audio_sync_b.py`,
jetzt `tools/audio_sync_variants_a.py` und `tools/audio_sync_variants_b.py`)
sind nur noch historisch: sie stammen aus der bisherigen Fehlersuche und bauen
Varianten aus dem jeweils aktuellen Builder. Nach dessen Aenderung
reproduzieren sie die historischen A-/B-Befehle nicht mehr. Ihre Ausgabe landet
in `tools/messungen/` (nicht versioniert, siehe `tools/README.md`). Fuer neue
Messungen die normale App und `sync_check.py` verwenden.

### Automatische Tests

Ohne Windows, Bildschirm und Audiohardware (Fake-Prozesse). Tests, die echtes
`ffmpeg` brauchen, und GUI-Tests ohne installiertes `customtkinter` werden
uebersprungen. Entwicklungsabhaengigkeit einmalig installieren:

```bash
pip install -r bin/requirements-dev.txt
```

Beide Aufrufe jeweils **im Projektordner `projects/ScreenRecPro/`**:

```bash
pytest
python -m unittest discover -s tests -v
```

`pytest.ini` beschraenkt die Sammlung auf `tests/`; die historischen Skripte in
`tools/` werden nicht als Tests gesammelt (geprueft durch
`tests/test_collect_guard.py`).

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
