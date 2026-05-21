@echo off
REM ==========================================================================
REM  Captain's Sidestick — Windows Build Script
REM  Compiles main.py into a single self-contained .exe using PyInstaller.
REM
REM  Usage:
REM    1.  pip install -r requirements.txt
REM    2.  Double-click build.bat  (or run from a Developer Command Prompt)
REM    3.  Output:  dist\CaptainsSidestick.exe
REM ==========================================================================

setlocal EnableDelayedExpansion

echo.
echo  ================================================
echo   Captain's Sidestick  ^|  Build Script
echo  ================================================
echo.

REM ── Verify Python is available ─────────────────────────────────────────────
where python >nul 2>&1
if %errorlevel% neq 0 (
    echo  [ERROR] Python not found in PATH.
    echo          Install Python 3.12+ from https://python.org
    pause
    exit /b 1
)

python --version
echo.

REM ── Install / upgrade dependencies ────────────────────────────────────────
echo  [1/3] Installing dependencies...
pip install -r requirements.txt --upgrade --quiet
if %errorlevel% neq 0 (
    echo  [ERROR] pip install failed. Check your internet connection.
    pause
    exit /b 1
)
echo  Dependencies OK.
echo.

REM ── Clean previous build artefacts ────────────────────────────────────────
echo  [2/3] Cleaning previous build...
if exist "build"  rmdir /s /q "build"
if exist "dist"   rmdir /s /q "dist"
if exist "CaptainsSidestick.spec" del /q "CaptainsSidestick.spec"
echo  Clean OK.
echo.

REM ── Run PyInstaller ────────────────────────────────────────────────────────
echo  [3/3] Running PyInstaller...
echo.

REM Check whether icon.ico is present; include it only if it exists
if exist "icon.ico" (
    set ICON_FLAG=--icon=icon.ico
) else (
    set ICON_FLAG=
    echo  [INFO] icon.ico not found — building without custom icon.
)

pyinstaller ^
    --onefile ^
    --windowed ^
    --name "CaptainsSidestick" ^
    --add-data "." ^
    --hidden-import vgamepad ^
    --hidden-import websockets ^
    --hidden-import qrcode ^
    --hidden-import PIL ^
    --hidden-import PIL.ImageTk ^
    --hidden-import pystray ^
    --hidden-import asyncio ^
    --collect-all vgamepad ^
    --collect-all websockets ^
    %ICON_FLAG% ^
    main.py

if %errorlevel% neq 0 (
    echo.
    echo  [ERROR] PyInstaller failed. See output above for details.
    pause
    exit /b 1
)

echo.
echo  ================================================
echo   BUILD SUCCESSFUL
echo   Output: dist\CaptainsSidestick.exe
echo  ================================================
echo.

REM ── Optional: open the dist folder ────────────────────────────────────────
if exist "dist\CaptainsSidestick.exe" (
    echo  Opening output folder...
    explorer dist
)

pause
endlocal
