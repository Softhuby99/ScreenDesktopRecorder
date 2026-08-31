@echo off
setlocal enabledelayedexpansion
title ScreenRec Pro - Windows Build
echo ============================================================
echo   ScreenRec Pro - PyInstaller Build fuer Windows
echo ============================================================
echo.

REM ---- Ins Projekt-Wurzelverzeichnis wechseln -------------------------
REM (dieses Skript liegt in bin\, main.py eine Ebene darueber)
cd /d "%~dp0\.."

REM ---- Python im PATH? -------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo FEHLER: Python wurde nicht im PATH gefunden.
    echo Bitte Python 3.10 oder neuer von https://python.org installieren
    echo und beim Setup "Add python.exe to PATH" aktivieren.
    pause
    exit /b 1
)

REM ---- ffmpeg.exe vorhanden? --------------------------------------------
if not exist "bin\ffmpeg.exe" (
    echo FEHLER: bin\ffmpeg.exe wurde nicht gefunden.
    echo.
    echo Bitte einen offiziellen Windows-Build von FFmpeg herunterladen
    echo ^(z. B. "essentials"-Build von https://www.gyan.dev/ffmpeg/builds/^),
    echo die Datei ffmpeg.exe aus dem bin-Unterordner des Downloads
    echo entnehmen und hier unter bin\ffmpeg.exe ablegen.
    pause
    exit /b 1
)

REM ---- Isolierte Build-Umgebung anlegen ---------------------------------
if not exist "build_venv" (
    echo Erstelle virtuelle Umgebung "build_venv" ...
    python -m venv build_venv
    if errorlevel 1 (
        echo FEHLER: Virtuelle Umgebung konnte nicht erstellt werden.
        pause
        exit /b 1
    )
)
call build_venv\Scripts\activate.bat

echo.
echo Installiere Abhaengigkeiten ...
python -m pip install --upgrade pip >nul
pip install -r bin\requirements.txt
if errorlevel 1 (
    echo FEHLER: Abhaengigkeiten konnten nicht installiert werden.
    pause
    exit /b 1
)
pip install "pyinstaller>=6.3.0"
if errorlevel 1 (
    echo FEHLER: PyInstaller konnte nicht installiert werden.
    pause
    exit /b 1
)

echo.
echo Entferne alte Build-Ordner ...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "ScreenRecPro.spec" del /q "ScreenRecPro.spec"

REM ---- Icon optional: nur anhaengen, wenn vorhanden ---------------------
set ICON_ARG=
if exist "bin\icon.ico" set ICON_ARG=--icon "bin\icon.ico"

echo.
echo Baue eigenstaendige EXE (das kann einige Minuten dauern) ...
pyinstaller ^
    --name "ScreenRecPro" ^
    --onefile ^
    --windowed ^
    %ICON_ARG% ^
    --add-binary "bin\ffmpeg.exe;bin" ^
    --collect-all customtkinter ^
    --collect-all sounddevice ^
    --hidden-import "PIL._tkinter_finder" ^
    main.py

if errorlevel 1 (
    echo.
    echo Build fehlgeschlagen. Siehe Fehlermeldungen oben.
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Fertig! Die eigenstaendige EXE liegt unter:
echo   dist\ScreenRecPro.exe
echo ============================================================
pause
