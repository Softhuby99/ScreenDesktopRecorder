@echo off
setlocal enabledelayedexpansion
rem ===========================================================================
rem  ScreenRec Pro - welches Audioformat liefert dein Mikrofon wirklich?
rem ===========================================================================
rem  Befund: In JEDEM Log steht "Guessed Channel Layout: stereo". FFmpeg
rem  bekommt vom Geraet keine Formatangabe und raet 2 Kanaele. Liefert das
rem  "Microphone Array" in Wahrheit 1 Kanal (oder 4), werden die Samples
rem  falsch zusammengesetzt - das klingt blechern, halbiert oder gar nicht.
rem
rem  Schritt 1 fragt das Geraet, welche Formate es unterstuetzt.
rem  Schritt 2 nimmt mit mehreren festen Formaten je 8 Sekunden auf und
rem  misst den Pegel jeder Aufnahme.
rem
rem  Bitte waehrend JEDER Aufnahme gleichmaessig sprechen (zaehlen hilft:
rem  "einundzwanzig, zweiundzwanzig, ..."), damit die Pegel vergleichbar sind.
rem ===========================================================================

set "DEVICE=Microphone Array (Realtek(R) Audio)"
set "OUTDIR=%USERPROFILE%\Videos\ScreenRec-Mikroformat"
set "LOG=%OUTDIR%\mikro-format-log.txt"
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

echo ScreenRec Pro - Mikrofon-Formatpruefung > "%LOG%"
echo Zeitpunkt: %DATE% %TIME% >> "%LOG%"
echo Geraet:    %DEVICE% >> "%LOG%"
echo. >> "%LOG%"

rem --- Schritt 1: Was kann das Geraet ueberhaupt? -----------------------------
echo [1/8] Unterstuetzte Formate werden abgefragt ...
echo ================ 1) UNTERSTUETZTE FORMATE DES GERAETS ================ >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -f dshow -list_options true -i "audio=%DEVICE%" >> "%LOG%" 2>&1
echo. >> "%LOG%"

echo.
echo ===========================================================
echo  Es folgen 6 Aufnahmen zu je 8 Sekunden.
echo  Bitte bei JEDER gleichmaessig sprechen oder zaehlen,
echo  damit die Pegel vergleichbar sind.
echo ===========================================================
echo.
pause

rem --- Schritt 2: verschiedene feste Formate ausprobieren ---------------------
call :rec 2 "Standard - wie bisher, ohne Formatangabe" ""
call :rec 3 "1 Kanal (mono), 44100 Hz" "-sample_rate 44100 -channels 1"
call :rec 4 "1 Kanal (mono), 48000 Hz" "-sample_rate 48000 -channels 1"
call :rec 5 "1 Kanal (mono), 16000 Hz" "-sample_rate 16000 -channels 1"
call :rec 6 "2 Kanaele (stereo), 44100 Hz" "-sample_rate 44100 -channels 2"
call :rec 7 "2 Kanaele (stereo), 48000 Hz" "-sample_rate 48000 -channels 2"

rem --- Schritt 3: Pegel jeder Aufnahme messen ---------------------------------
echo [8/8] Pegel werden gemessen ...
echo. >> "%LOG%"
echo ================ PEGEL JE VARIANTE ================ >> "%LOG%"
for %%f in (2 3 4 5 6 7) do (
    echo. >> "%LOG%"
    echo ---- Variante %%f ---- >> "%LOG%"
    if exist "%OUTDIR%\v%%f.m4a" (
        "!FFMPEG!" -nostdin -hide_banner -i "%OUTDIR%\v%%f.m4a" -af volumedetect -f null - >> "%LOG%" 2>&1
    ) else (
        echo FORMAT NICHT UNTERSTUETZT - keine Datei entstanden >> "%LOG%"
    )
)

echo.
echo ===========================================================
echo  Fertig. Bitte schicken:
echo    %LOG%
echo.
echo  Und die Dateien v2..v7.m4a anhoeren - welche klingt sauber?
echo    %OUTDIR%
echo ===========================================================
echo.
pause
exit /b 0

rem ---------------------------------------------------------------------------
:rec
echo [%~1/8] %~2
echo ==== Variante %~1: %~2 ==== >> "%LOG%"
"!FFMPEG!" -nostdin -hide_banner -loglevel warning -y -f dshow %~3 -i "audio=%DEVICE%" -t 8 -c:a aac -b:a 192k "%OUTDIR%\v%~1.m4a" >> "%LOG%" 2>&1
exit /b 0
