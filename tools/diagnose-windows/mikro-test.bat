@echo off
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ScreenRec Pro - Mikrofon-Pegel objektiv messen
rem ===========================================================================
rem  "Ton schlecht oder gar nicht zu hoeren" laesst sich nicht am Gehoer
rem  festmachen - dieses Skript misst den tatsaechlichen Pegel.
rem
rem  volumedetect liefert zwei Zahlen:
rem    mean_volume : Durchschnittspegel
rem    max_volume  : lauteste Stelle
rem  Einordnung fuer normales Sprechen aus ~50 cm Abstand:
rem    max_volume um -3 bis -12 dB   -> gut ausgesteuert
rem    max_volume um -20 bis -30 dB  -> leise, aber brauchbar
rem    max_volume unter -40 dB       -> viel zu leise
rem    max_volume genau 0 dB         -> uebersteuert (verzerrt!)
rem    mean_volume -91 dB            -> voellige Stille, es kommt NICHTS an
rem ===========================================================================

set "DEVICE=Microphone Array (Realtek(R) Audio)"
set "OUTDIR=%USERPROFILE%\Videos\ScreenRec-Mikrotest"
set "LOG=%OUTDIR%\mikro-test-log.txt"
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
    echo FFmpeg nicht gefunden - bitte ScreenRec Pro starten und erneut ausfuehren.
    pause
    exit /b 1
)

echo ScreenRec Pro - Mikrofon-Pegelmessung > "%LOG%"
echo Zeitpunkt: %DATE% %TIME% >> "%LOG%"
echo Geraet:    %DEVICE% >> "%LOG%"
echo. >> "%LOG%"

echo.
echo ===========================================================
echo  Bitte waehrend der naechsten 10 Sekunden NORMAL SPRECHEN
echo  (etwa im Abstand, den du auch bei einer Aufnahme haettest)
echo ===========================================================
echo.
pause

echo [1/3] Pegel wird gemessen (10 Sekunden, bitte sprechen) ...
echo ==== 1) PEGELMESSUNG (nur Mikrofon, keine Bearbeitung) ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -f dshow -i "audio=%DEVICE%" -t 10 -af volumedetect -f null - >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo [2/3] Hoerprobe wird aufgenommen (10 Sekunden, bitte sprechen) ...
echo ==== 2) HOERPROBE unbearbeitet ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -f dshow -i "audio=%DEVICE%" -t 10 -c:a aac -b:a 192k "%OUTDIR%\hoerprobe_original.m4a" >> "%LOG%" 2>&1

echo [3/3] Hoerprobe mit Anhebung (falls das Mikro zu leise ist) ...
echo ==== 3) HOERPROBE mit automatischer Anhebung ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -f dshow -i "audio=%DEVICE%" -t 10 -af "loudnorm=I=-16:TP=-1.5:LRA=11" -c:a aac -b:a 192k "%OUTDIR%\hoerprobe_angehoben.m4a" >> "%LOG%" 2>&1

echo. >> "%LOG%"
echo ==== DATEIEN ==== >> "%LOG%"
dir "%OUTDIR%\*.m4a" >> "%LOG%" 2>&1

echo.
echo ===========================================================
echo  Fertig. Bitte schicken:
echo    %LOG%
echo.
echo  Und bitte beide Hoerproben anhoeren:
echo    hoerprobe_original.m4a    (unbearbeitet)
echo    hoerprobe_angehoben.m4a   (automatisch ausgesteuert)
echo  Welche klingt brauchbar?
echo ===========================================================
echo.
pause
