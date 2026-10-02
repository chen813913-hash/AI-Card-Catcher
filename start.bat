@echo off
cd /d "%~dp0"
title AI-Card-Catcher

REM ---- locate python (managed first, then system) ----
set PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe
if not exist "%PY%" set PY=python

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
