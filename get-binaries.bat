@echo off
cd /d "%~dp0"
title AI-Card-Catcher - get binaries

REM Download order for new-api.exe: 1) this repo's GitHub Release (gh-proxy mirror) 2) upstream latest release 3) pinned fallback.
REM cpolar.exe: 1) this repo's Release 2) cpolar official site.

if not exist bin mkdir bin

echo ============================================
echo   AI-Card-Catcher - downloading components
echo   (new-api ~134MB, cpolar ~16MB)
echo   Tries this repo's Release first, then official sources (gh-proxy mirror).
echo ============================================
echo.

if not exist "bin\new-api.exe" (
    echo [1/2] new-api.exe: trying this repo's Release...
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://gh-proxy.com/https://github.com/chen813913-hash/AI-Card-Catcher/releases/download/v1.0.0/new-api.exe' -OutFile 'bin\new-api.exe' -TimeoutSec 900 } catch { Write-Host ('  Release fetch failed: ' + $_.Exception.Message) }"
)
if not exist "bin\new-api.exe" (
    echo [1/2] Downloading new-api.exe (latest upstream release)...
    powershell -NoProfile -Command "$ErrorActionPreference='Stop'; try { $r = Invoke-RestMethod -Uri 'https://api.github.com/repos/QuantumNous/new-api/releases/latest' -TimeoutSec 30; $a = $r.assets | Where-Object { $_.name -like 'new-api-*.exe' -and $_.name -notlike '*arm64*' } | Select-Object -First 1; $u = 'https://gh-proxy.com/' + $a.browser_download_url; Write-Host ('  version: ' + $r.tag_name); Invoke-WebRequest -Uri $u -OutFile 'bin\new-api.exe' -TimeoutSec 900 } catch { Write-Host ('[FAIL] ' + $_.Exception.Message); exit 1 }"
    if errorlevel 1 (
        echo [RETRY] Trying fallback version v1.0.0-rc.40 ...
        powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://gh-proxy.com/https://github.com/QuantumNous/new-api/releases/download/v1.0.0-rc.40/new-api-v1.0.0-rc.40.exe' -OutFile 'bin\new-api.exe' -TimeoutSec 900"
    )
)
if exist "bin\new-api.exe" ( echo [OK] new-api.exe ready. ) else ( echo [FAIL] new-api.exe download failed. )

if not exist "bin\cpolar.exe" (
    echo [2/2] cpolar.exe: trying this repo's Release...
    powershell -NoProfile -Command "try { Invoke-WebRequest -Uri 'https://gh-proxy.com/https://github.com/chen813913-hash/AI-Card-Catcher/releases/download/v1.0.0/cpolar.exe' -OutFile 'bin\cpolar.exe' -TimeoutSec 300 } catch { Write-Host ('  Release fetch failed: ' + $_.Exception.Message) }"
)
if not exist "bin\cpolar.exe" (
    echo [2/2] Downloading cpolar.exe from official site...
    powershell -NoProfile -Command "Invoke-WebRequest -Uri 'https://www.cpolar.com/static/downloads/cpolar-stable-windows-amd64.zip' -OutFile 'bin\cpolar.zip' -TimeoutSec 300; Expand-Archive -Path 'bin\cpolar.zip' -DestinationPath 'bin' -Force; Remove-Item 'bin\cpolar.zip' -ErrorAction SilentlyContinue"
)
if exist "bin\cpolar.exe" ( echo [OK] cpolar.exe ready. ) else ( echo [FAIL] cpolar.exe download failed. )

echo.
if exist "bin\new-api.exe" if exist "bin\cpolar.exe" (
    echo ============================================
    echo   All components ready. Run start.bat !
    echo ============================================
) else (
    echo Some downloads failed. Check your network and run this script again.
)
pause
