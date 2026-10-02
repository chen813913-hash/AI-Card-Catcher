@echo off
cd /d "%~dp0"
title AI-Card-Catcher

REM ---- locate python: py launcher -> python -> python3 ----
REM 注意：PY 必须是单个词（不含空格参数），否则 "%PY%" 展开会把 "py -3" 当成程序名
set PY=
py --version >nul 2>&1 && set PY=py
if not defined PY (
    python --version >nul 2>&1 && set PY=python
)
if not defined PY (
    python3 --version >nul 2>&1 && set PY=python3
)
if not defined PY (
    echo [MISSING] Python 3.10+ not found.
    echo Install it from https://www.python.org/downloads/
    echo IMPORTANT: check "Add Python to PATH" during install.
    pause
    exit /b 1
)

if not exist "bin\new-api.exe" (
    echo [MISSING] bin\new-api.exe not found.
    echo Run get-binaries.bat first, then start.bat again.
    pause
    exit /b 1
)
if not exist "app\main.py" (
    echo [MISSING] app\main.py not found.
    pause
    exit /b 1
)

echo ============================================
echo   AI-Card-Catcher starting...
echo   A browser window will open shortly.
echo   Keep this window open while using.
echo   Run stop.bat to shut everything down.
echo ============================================
echo.

"%PY%" app\main.py

echo.
echo Main control stopped.
pause
