@echo off
rem Video Crop Tool - quick launcher (drag a video folder onto this file to load it)
title Video Crop Tool
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
    echo [ERROR] Virtual env not found. Run setup.bat first.
    pause
    exit /b 1
)

if not exist "ffmpeg\ffmpeg.exe" echo [WARN] ffmpeg\ffmpeg.exe not found - export may be unavailable (preview/stepping still works)

if "%~1"=="" (
    echo Starting Video Crop Tool ...
    start "" ".venv\Scripts\pythonw.exe" -m video_crop_tool
) else (
    echo Starting Video Crop Tool, loading folder: %~1
    start "" ".venv\Scripts\pythonw.exe" -m video_crop_tool "%~1"
)
