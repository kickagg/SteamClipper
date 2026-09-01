@echo off
title SteamClipper (desktop)
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
    echo.
    echo   Python nao encontrado no PATH.
    echo   Instale com:  winget install Python.Python.3.13
    echo.
    pause
    exit /b 1
)

where ffmpeg >nul 2>&1
if errorlevel 1 (
    echo.
    echo   ffmpeg nao encontrado no PATH.
    echo   Instale com:  winget install Gyan.FFmpeg
    echo.
    pause
    exit /b 1
)

REM pythonw: abre so a janela do app, sem console atras
start "" pythonw run.py
