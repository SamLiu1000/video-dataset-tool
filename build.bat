@echo off
rem =====================================================================
rem  Video Crop Tool - one-click portable build (PyInstaller onedir)
rem
rem  Usage:
rem    build.bat            rebuild into dist\video-dataset-tool\
rem    build.bat zip        rebuild, then also make dist\video-dataset-tool.zip
rem
rem  Requires an existing virtual env (.venv or .venv1); PyInstaller is
rem  installed automatically if missing.
rem =====================================================================
title Video Crop Tool - Build
cd /d "%~dp0" >nul

rem ---- locate venv python (prefer .venv, else .venv1) ----
set "PY="
for %%V in (.venv .venv1) do (
    if not defined PY if exist "%%V\Scripts\python.exe" set "PY=%%V\Scripts\python.exe"
)
if not defined PY (
    echo [ERROR] Virtual env not found. Run setup.bat first.
    pause
    exit /b 1
)
echo [OK] Using virtual env: %PY%

rem ---- ensure PyInstaller is installed ----
"%PY%" -c "import PyInstaller" >nul 2>nul
if errorlevel 1 (
    echo [INFO] PyInstaller not found, installing...
    "%PY%" -m pip install --disable-pip-version-check -q pyinstaller
    if errorlevel 1 (
        echo [ERROR] Failed to install PyInstaller. Check your network and retry.
        pause
        exit /b 1
    )
)

rem ---- clean previous output so every build is fresh ----
echo [INFO] Removing build\ and dist\video-dataset-tool\ ...
if exist "build" rmdir /s /q "build" >nul 2>nul
if exist "dist\video-dataset-tool" rmdir /s /q "dist\video-dataset-tool" >nul 2>nul

rem ---- run PyInstaller ----
echo [INFO] Building (Qt + libmpv + ffmpeg, about 3-5 minutes)...
"%PY%" -m PyInstaller --noconfirm --clean VCT.spec
if errorlevel 1 (
    echo [ERROR] Build failed. See the log above.
    pause
    exit /b 1
)

rem ---- verify output ----
if not exist "dist\video-dataset-tool\video-dataset-tool.exe" (
    echo [ERROR] video-dataset-tool.exe was not generated.
    pause
    exit /b 1
)
echo [OK] Build complete: dist\video-dataset-tool\video-dataset-tool.exe

rem ---- optional: create distributable zip ----
if /i "%~1"=="zip" (
    echo [INFO] Creating distributable zip - large folder, may take a minute...
    powershell -NoProfile -Command "Compress-Archive -Path 'dist\video-dataset-tool' -DestinationPath 'dist\video-dataset-tool.zip' -Force" >nul 2>nul
    if exist "dist\video-dataset-tool.zip" (
        echo [OK] Archive: dist\video-dataset-tool.zip
    ) else (
        echo [WARN] Zip failed, but dist\video-dataset-tool\ folder is ready.
    )
)

echo.
echo Done! The portable build is under dist\video-dataset-tool\ - run video-dataset-tool.exe.
echo To distribute, copy the dist\video-dataset-tool folder (or the zip).
