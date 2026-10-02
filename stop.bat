@echo off
cd /d "%~dp0"
title AI-Card-Catcher - stop

echo Stopping AI-Card-Catcher...

if exist data\main.pid for /f %%p in (data\main.pid) do taskkill /PID %%p /F >nul 2>&1

taskkill /IM cpolar.exe /F >nul 2>&1
taskkill /IM new-api.exe /F >nul 2>&1

echo [OK] All AI-Card-Catcher services stopped.
timeout /t 2 >nul
