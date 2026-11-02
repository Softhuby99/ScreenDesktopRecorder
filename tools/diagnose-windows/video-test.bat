@echo off
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ScreenRec Pro - Bildrate-Diagnose
rem ===========================================================================
rem  Befund aus dem Audiotest: die Tonspur ist einwandfrei, aber gdigrab
rem  liefert statt 30 nur ~3-4 Bilder/Sekunde. Daher die kleinen Dateien und
rem  das ruckelige Bild.
rem
rem  Dieses Skript nimmt mehrere Varianten zu je 10 Sekunden auf und zeigt
rem  fuer jede die TATSAECHLICH erreichte Bildrate. Damit laesst sich sehen,
rem  welches Verfahren auf dieser Hardware wirklich mithaelt.
rem
rem  Waehrend der Aufnahmen bitte etwas auf dem Bildschirm bewegen (Fenster
rem  schieben, scrollen) - bei voellig statischem Bild liefert gdigrab von
rem  sich aus weniger Bilder.
rem ===========================================================================

set "OUTDIR=%USERPROFILE%\Videos\ScreenRec-Videotest"
set "LOG=%OUTDIR%\video-test-log.txt"
if not exist "%OUTDIR%" mkdir "%OUTDIR%"

set "FFMPEG="
for /f "delims=" %%i in ('where ffmpeg.exe 2^>nul') do if not defined FFMPEG set "FFMPEG=%%i"
if not defined FFMPEG (
    for /f "delims=" %%d in ('dir /b /ad "%TEMP%\_MEI*" 2^>nul') do (
        if not defined FFMPEG if exist "%TEMP%\%%d\bin\ffmpeg.exe" set "FFMPEG=%TEMP%\%%d\bin\ffmpeg.exe"
    )
)
if not defined FFMPEG (
    for /f "delims=" %%d in ('dir /b /ad "C:\Temp\_MEI*" 2^>nul') do (
        if not defined FFMPEG if exist "C:\Temp\%%d\bin\ffmpeg.exe" set "FFMPEG=C:\Temp\%%d\bin\ffmpeg.exe"
    )
)
if not defined FFMPEG (
    echo FFmpeg nicht gefunden - bitte ScreenRec Pro starten und Skript erneut ausfuehren.
    pause
    exit /b 1
)

echo ScreenRec Pro - Bildrate-Diagnose > "%LOG%"
echo Zeitpunkt: %DATE% %TIME% >> "%LOG%"
echo FFmpeg:    !FFMPEG! >> "%LOG%"
echo. >> "%LOG%"

echo.
echo Bitte waehrend der Aufnahmen etwas auf dem Bildschirm bewegen!
echo.

rem --- Variante 1: gdigrab Vollbild, 30 fps (wie bisher) ----------------------
echo [1/5] gdigrab Vollbild 30 fps ...
echo ============ 1) gdigrab Vollbild 30 fps ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f gdigrab -framerate 30 -draw_mouse 1 -thread_queue_size 512 -i desktop -t 10 -c:v libx264 -preset faster -crf 23 -pix_fmt yuv420p "%OUTDIR%\v1_gdigrab_voll.mp4" >> "%LOG%" 2>&1

rem --- Variante 2: gdigrab Vollbild ohne zerolatency, groesseres probesize ----
echo [2/5] gdigrab Vollbild 30 fps mit groesserem probesize ...
echo ============ 2) gdigrab Vollbild 30 fps + probesize 100M ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -probesize 100M -f gdigrab -framerate 30 -draw_mouse 1 -thread_queue_size 1024 -i desktop -t 10 -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p "%OUTDIR%\v2_gdigrab_ultrafast.mp4" >> "%LOG%" 2>&1

rem --- Variante 3: gdigrab kleiner Ausschnitt --------------------------------
echo [3/5] gdigrab kleiner Ausschnitt 960x540 ...
echo ============ 3) gdigrab Ausschnitt 960x540 30 fps ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f gdigrab -framerate 30 -draw_mouse 1 -offset_x 0 -offset_y 0 -video_size 960x540 -i desktop -t 10 -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p "%OUTDIR%\v3_gdigrab_klein.mp4" >> "%LOG%" 2>&1

rem --- Variante 4: ddagrab (Desktop Duplication API, GPU-gestuetzt) ----------
echo [4/5] ddagrab (Desktop Duplication API, GPU) ...
echo ============ 4) ddagrab 30 fps (GPU-gestuetzt) ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f lavfi -i "ddagrab=output_idx=0:framerate=30" -t 10 -vf "hwdownload,format=bgra" -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p "%OUTDIR%\v4_ddagrab.mp4" >> "%LOG%" 2>&1

rem --- Variante 5: gdigrab mit nur 15 fps ------------------------------------
echo [5/5] gdigrab Vollbild 15 fps ...
echo ============ 5) gdigrab Vollbild 15 fps ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f gdigrab -framerate 15 -draw_mouse 1 -i desktop -t 10 -c:v libx264 -preset ultrafast -crf 23 -pix_fmt yuv420p "%OUTDIR%\v5_gdigrab_15fps.mp4" >> "%LOG%" 2>&1

rem --- Auswertung: tatsaechliche Bildrate je Datei ----------------------------
echo.
echo Auswertung ...
echo. >> "%LOG%"
echo ============ ERGEBNIS: TATSAECHLICH ERREICHTE BILDRATE ============ >> "%LOG%"

for %%f in (v1_gdigrab_voll v2_gdigrab_ultrafast v3_gdigrab_klein v4_ddagrab v5_gdigrab_15fps) do (
    echo. >> "%LOG%"
    echo ---- %%f ---- >> "%LOG%"
    if exist "%OUTDIR%\%%f.mp4" (
        "!FFMPEG!" -nostdin -hide_banner -i "%OUTDIR%\%%f.mp4" 2>> "%LOG%"
    ) else (
        echo DATEI NICHT ENTSTANDEN - Variante hat nicht funktioniert >> "%LOG%"
    )
)

echo. >> "%LOG%"
echo ============ DATEIGROESSEN ============ >> "%LOG%"
dir "%OUTDIR%\*.mp4" >> "%LOG%" 2>&1

echo.
echo ===========================================================
echo  Fertig. Bitte diese Datei schicken:
echo    %LOG%
echo.
echo  Interessant ist in jeder Stream-Zeile der Wert vor "fps"
echo  - das ist die tatsaechlich erreichte Bildrate.
echo ===========================================================
echo.
pause
