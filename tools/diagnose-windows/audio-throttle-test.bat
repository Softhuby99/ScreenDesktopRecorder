@echo off
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ScreenRec Pro - warum bremst die Audioquelle das Bild?
rem ===========================================================================
rem  Befund aus 11 Messungen: sobald eine Audioquelle mitlaeuft, faellt die
rem  Bildrate auf 3-9 fps; ohne Audio liegt sie bei 15-30 fps. Preset,
rem  Aufloesung und Aufnahmeverfahren spielen dabei keine Rolle.
rem
rem  Zwei Verdaechtige:
rem    1) -audio_buffer_size 500 (von mir frueher von 80 auf 500 erhoeht):
rem       dshow liefert den Ton dann nur alle 500 ms in grossen Bloecken,
rem       was das Einlesen des Bildes ausbremsen kann.
rem    2) fehlendes -thread_queue_size an der VIDEO-Eingabe: ohne eigene
rem       Warteschlange lesen Bild und Ton in derselben Schleife, und ein
rem       blockierender Audio-Lesevorgang haelt das Bild mit an.
rem
rem  Jede Variante nimmt 15 Sekunden auf; am Ende steht die tatsaechlich
rem  erreichte Bildrate. Bitte waehrend der Aufnahmen etwas auf dem
rem  Bildschirm bewegen (Fenster schieben, scrollen).
rem ===========================================================================

set "DEVICE=Microphone Array (Realtek(R) Audio)"
set "OUTDIR=%USERPROFILE%\Videos\ScreenRec-Throttle"
set "LOG=%OUTDIR%\throttle-test-log.txt"
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

set "VID=-f lavfi -i ddagrab=output_idx=0:framerate=30:draw_mouse=1"
set "ENC=-t 15 -vf hwdownload,format=bgra -c:v libx264 -preset faster -crf 23 -tune zerolatency -pix_fmt yuv420p -c:a aac -b:a 192k -ar 48000 -ac 2"

echo ScreenRec Pro - Throttle-Diagnose > "%LOG%"
echo Zeitpunkt: %DATE% %TIME% >> "%LOG%"
echo FFmpeg:    !FFMPEG! >> "%LOG%"
echo. >> "%LOG%"

echo.
echo Bitte waehrend der Aufnahmen etwas auf dem Bildschirm bewegen!
echo Jede Variante dauert 15 Sekunden.
echo.

echo [1/7] A - genau wie die App jetzt (Audiopuffer 500 ms)
echo ==== A: wie die App jetzt (audio_buffer_size 500) ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y %VID% -f dshow -thread_queue_size 1024 -audio_buffer_size 500 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\A.mp4" >> "%LOG%" 2>&1

echo [2/7] B - zusaetzlich Warteschlange an der Video-Eingabe
echo ==== B: + thread_queue_size an der Video-Eingabe ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -thread_queue_size 1024 %VID% -f dshow -thread_queue_size 1024 -audio_buffer_size 500 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\B.mp4" >> "%LOG%" 2>&1

echo [3/7] C - Audiopuffer 100 ms statt 500
echo ==== C: audio_buffer_size 100 ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y %VID% -f dshow -thread_queue_size 1024 -audio_buffer_size 100 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\C.mp4" >> "%LOG%" 2>&1

echo [4/7] D - Audiopuffer 50 ms
echo ==== D: audio_buffer_size 50 ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y %VID% -f dshow -thread_queue_size 1024 -audio_buffer_size 50 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\D.mp4" >> "%LOG%" 2>&1

echo [5/7] E - GAR KEIN audio_buffer_size (Geraete-Standard)
echo ==== E: ohne audio_buffer_size ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y %VID% -f dshow -thread_queue_size 1024 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\E.mp4" >> "%LOG%" 2>&1

echo [6/7] F - Kombination: Warteschlange + 100 ms Puffer
echo ==== F: thread_queue_size + audio_buffer_size 100 ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -thread_queue_size 1024 %VID% -f dshow -thread_queue_size 1024 -audio_buffer_size 100 -rtbufsize 256M -i "audio=%DEVICE%" %ENC% "%OUTDIR%\F.mp4" >> "%LOG%" 2>&1

echo [7/7] G - Audio zuerst, Bild danach (andere Reihenfolge)
echo ==== G: Audio-Eingabe zuerst ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f dshow -thread_queue_size 1024 -audio_buffer_size 100 -rtbufsize 256M -i "audio=%DEVICE%" -thread_queue_size 1024 %VID% %ENC% "%OUTDIR%\G.mp4" >> "%LOG%" 2>&1

echo.
echo Auswertung ...
echo. >> "%LOG%"
echo ==================== ERGEBNIS ==================== >> "%LOG%"
for %%f in (A B C D E F G) do (
    echo. >> "%LOG%"
    echo ---- Variante %%f ---- >> "%LOG%"
    if exist "%OUTDIR%\%%f.mp4" (
        "!FFMPEG!" -nostdin -hide_banner -i "%OUTDIR%\%%f.mp4" 2>> "%LOG%"
    ) else (
        echo DATEI NICHT ENTSTANDEN >> "%LOG%"
    )
)

echo.
echo ===========================================================
echo  Fertig. Bitte diese Datei schicken:
echo    %LOG%
echo  Entscheidend ist je Variante der Wert vor "fps".
echo ===========================================================
echo.
pause
