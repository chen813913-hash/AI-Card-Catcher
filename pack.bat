@echo off
cd /d "%~dp0"
title AI-Card-Catcher - pack for sharing

REM Create a CLEAN distribution zip: code + docs + scripts only.
REM NEVER includes: data/ (your tokens & chat logs), one-api.db (your API keys), bin/ (exes, users get them via get-binaries.bat)

REM remove python bytecode cache so it does not sneak into the zip
powershell -NoProfile -Command "Get-ChildItem -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force"

powershell -NoProfile -Command "$stamp = Get-Date -Format 'yyyyMMdd'; $out = 'AI-Card-Catcher-' + $stamp + '.zip'; $list = @('app','web','docs','start.bat','stop.bat','get-binaries.bat','README.md','.gitignore'); $missing = $list | Where-Object { -not (Test-Path $_) }; if ($missing) { Write-Host ('[FAIL] missing: ' + ($missing -join ', ')); exit 1 }; Compress-Archive -Path $list -DestinationPath $out -Force; $size = (Get-Item $out).Length / 1KB; Write-Host ('[OK] created ' + $out + ' (' + [math]::Round($size) + ' KB)'); Write-Host ''; Write-Host 'Package contains: source code, web UI, scripts, docs.'; Write-Host 'Package does NOT contain: data/, one-api.db, bin/*.exe'; Write-Host 'Recipients: run get-binaries.bat, then start.bat.'"

echo.
pause
