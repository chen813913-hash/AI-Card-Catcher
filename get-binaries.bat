@echo off
cd /d "%~dp0"
title AI-Card-Catcher - get binaries

if not exist bin mkdir bin

echo ============================================
echo   AI-Card-Catcher - downloading components
echo   (new-api ~134MB, cpolar ~16MB)
echo   Uses gh-proxy.com mirror for GitHub.
echo ============================================
echo.

if exist "bin\new-api.exe" (
    echo [OK] bin\new-api.exe already exists - skip.
) else (
    echo [1/2] Downloading new-api.exe (latest release)...
    powershell -NoProfile -Command "$ErrorActionPreference='Stop'; try { $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/QuantumNous/new-api/releases/latest' -TimeoutSec 30; $a = $r.assets | Where-Object { $_.name -like 'new-api-*.exe' -and $_.name -notlike '*arm64*' } | Select-Object -First 1; $u = 'https://gh-proxy.com/' + $a.browser_download_url; Write-Host ('  version: ' + $r.tag_name); Invoke-WebRequest -Uri $u -OutFile 'bin\new-api.exe' -TimeoutSec 900 } catch { Write-Host ('[FAIL] ' + $_.Exception.Message); exit 1 }"
    if errorlevel 1 (
        echo [RETRY] Trying fallback version v1.0.0-rc.40 ...
        powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://gh-proxy.com/https://github.com/QuantumNous/new-api/releases/download/v1.0.0-rc.40/new-api-v1.0.0-rc.40.exe' -OutFile 'bin\new-api.exe' -TimeoutSec 900"
    )
    if exist "bin\new-api.exe" ( echo [OK] new-api.exe downloaded. ) else ( echo [FAIL] new-api.exe download failed. )
)

if exist "bin\cpolar.exe" (
    echo [OK] bin\cpolar.exe already exists - skip.
) else (
    echo [2/2] Downloading cpolar.exe ...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.cpolar.com/static/downloads/cpolar-stable-windows-amd64.zip' -OutFile 'bin\cpolar.zip' -TimeoutSec 300; Expand-Archive -Path 'bin\cpolar.zip' -DestinationPath 'bin' -Force; Remove-Item 'bin\cpolar.zip' -ErrorAction SilentlyContinue"
    if exist "bin\cpolar.exe" ( echo [OK] cpolar.exe downloaded. ) else ( echo [FAIL] cpolar.exe download failed. )
)

echo.
if exist "bin\new-api.exe" if exist "bin\cpolar.exe" (
    echo ============================================
    echo   All components ready. Run start.bat !
    echo ============================================
) else (
    echo Some downloads failed. Check your network and run this script again.
)
pause
