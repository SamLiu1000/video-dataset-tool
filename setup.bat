@echo off
rem Video Crop Tool - recreate virtual env and install dependencies
title Video Crop Tool - Setup
cd /d "%~dp0"

echo Creating virtual env .venv ...
python -m venv .venv
if errorlevel 1 (
    echo [ERROR] Failed to create venv. Install Python 3.10+ and add it to PATH.
    pause
    exit /b 1
)

echo Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo Installing dependencies (PySide6 / PyAV / numpy) ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] Dependency install failed
    pause
    exit /b 1
)

echo.
echo Checking libmpv-2.dll (required by python-mpv preview engine) ...
if exist ".venv\Lib\site-packages\libmpv-2.dll" (
    echo   [OK] libmpv-2.dll found in site-packages
) else if exist "libmpv\libmpv-2.dll" (
    echo   [OK] libmpv-2.dll found in project libmpv\ directory
) else (
    echo   [WARN] libmpv-2.dll NOT found. Preview engine will not start.
    echo   Download it from: https://sourceforge.net/projects/mpv-player-windows/files/libmpv/
    echo   then place libmpv-2.dll into .venv\Lib\site-packages\ or libmpv\ folder.
)
echo.
echo Done! Run run.bat to launch the tool, test.bat to run tests.
pause
