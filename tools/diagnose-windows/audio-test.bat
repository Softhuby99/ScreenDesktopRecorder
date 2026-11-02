@echo off
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ScreenRec Pro - Audio-Diagnose (laeuft OHNE neuen Build)
rem ===========================================================================
rem  Nimmt zweimal 20 Sekunden auf - einmal mit den Parametern der aktuellen
rem  .exe, einmal mit den vorgeschlagenen neuen - und schreibt alles inklusive
rem  FFmpeg-Ausgabe und Stream-Uebersicht in eine Logdatei.
rem
rem  Damit laesst sich auf DEINER Hardware direkt vergleichen, ob die Tonspur
rem  mit den neuen Parametern voll durchlaeuft - ohne den Umweg ueber
rem  Sync -> GitHub-Build -> Download.
rem
rem  Hinweis zu "-nostdin": ohne diese Option liest FFmpeg von der Standard-
rem  eingabe und verschluckt dabei die restlichen Zeilen dieser Batch-Datei -
rem  das Skript bricht dann mittendrin scheinbar grundlos ab.
rem ===========================================================================

set "DEVICE=Microphone Array (Realtek(R) Audio)"
set "OUTDIR=%USERPROFILE%\Videos\ScreenRec-Audiotest"
set "LOG=%OUTDIR%\audio-test-log.txt"

if not exist "%OUTDIR%" mkdir "%OUTDIR%"

rem --- FFmpeg suchen: erst im PATH, dann im Entpackordner der laufenden .exe --
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
    echo.
    echo FFmpeg wurde nicht gefunden.
    echo.
    echo Einfachste Loesung: ScreenRec Pro STARTEN und laufen lassen - dann
    echo entpackt es sein eigenes ffmpeg.exe nach C:\Temp\_MEI... und dieses
    echo Skript findet es. Danach dieses Skript erneut ausfuehren.
    echo.
    pause
    exit /b 1
)

echo FFmpeg gefunden: !FFMPEG!
echo.

rem --- Log-Kopf ---------------------------------------------------------------
echo ScreenRec Pro - Audio-Diagnose > "%LOG%"
echo Zeitpunkt: %DATE% %TIME% >> "%LOG%"
echo FFmpeg:    !FFMPEG! >> "%LOG%"
echo Geraet:    %DEVICE% >> "%LOG%"
echo. >> "%LOG%"

rem --- Schritt 1: welche Audiogeraete sieht FFmpeg ueberhaupt? -----------------
echo [1/4] Verfuegbare Audiogeraete werden aufgelistet ...
echo ============ 1) VERFUEGBARE DSHOW-GERAETE ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -list_devices true -f dshow -i dummy >> "%LOG%" 2>&1
echo. >> "%LOG%"

rem --- Schritt 2: Aufnahme A - Parameter der AKTUELLEN .exe -------------------
echo [2/4] Test A laeuft (aktuelle Parameter, 20 Sekunden) - bitte sprechen ...
echo ============ 2) TEST A - AKTUELLE PARAMETER ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f gdigrab -framerate 30 -draw_mouse 1 -thread_queue_size 512 -i desktop -f dshow -thread_queue_size 1024 -audio_buffer_size 500 -i "audio=%DEVICE%" -t 20 -c:v libx264 -preset faster -crf 23 -tune zerolatency -pix_fmt yuv420p -g 60 -movflags +faststart -c:a aac -b:a 192k -ar 48000 -ac 2 "%OUTDIR%\test_A_aktuell.mp4" >> "%LOG%" 2>&1
echo. >> "%LOG%"

rem --- Schritt 3: Aufnahme B - mit rtbufsize + aresample ----------------------
echo [3/4] Test B laeuft (neue Parameter, 20 Sekunden) - bitte wieder sprechen ...
echo ============ 3) TEST B - NEUE PARAMETER (rtbufsize + aresample) ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f gdigrab -framerate 30 -draw_mouse 1 -thread_queue_size 512 -i desktop -f dshow -thread_queue_size 1024 -audio_buffer_size 500 -rtbufsize 256M -i "audio=%DEVICE%" -t 20 -c:v libx264 -preset faster -crf 23 -tune zerolatency -pix_fmt yuv420p -g 60 -movflags +faststart -c:a aac -b:a 192k -ar 48000 -ac 2 -af aresample=async=1 "%OUTDIR%\test_B_neu.mp4" >> "%LOG%" 2>&1
echo. >> "%LOG%"

rem --- Schritt 4: Was steckt in den beiden Dateien? ----------------------------
echo [4/4] Ergebnisse werden ausgewertet ...
echo ============ 4) INHALT TEST A ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -i "%OUTDIR%\test_A_aktuell.mp4" >> "%LOG%" 2>&1
echo. >> "%LOG%"
echo ============ 5) INHALT TEST B ============ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -i "%OUTDIR%\test_B_neu.mp4" >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo ============ 6) DATEIGROESSEN ============ >> "%LOG%"
dir "%OUTDIR%\test_A_aktuell.mp4" "%OUTDIR%\test_B_neu.mp4" >> "%LOG%" 2>&1

echo.
echo ===========================================================
echo  Fertig. Bitte diese Datei schicken:
echo    %LOG%
echo.
echo  Und kurz reinhoeren: laeuft der Ton in test_B_neu.mp4
echo  ueber die vollen 20 Sekunden?
echo ===========================================================
echo.
pause
