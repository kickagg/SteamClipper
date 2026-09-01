@echo off
title SteamClipper (navegador)
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

echo.
echo   SteamClipper - versao navegador
echo   O painel abre no navegador. O player abre em janela separada.
echo   Feche esta janela (ou rode Encerrar.bat) para desligar.
echo.

python run.py

if errorlevel 1 (
    echo.
    pause
)
