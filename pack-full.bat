@echo off
cd /d "%~dp0"
title AI-Card-Catcher - full package (for sharing with friends)

REM Create a FULL package including the downloaded exes (bin/).
REM Good for sending via netdisk / chat apps - recipients just unzip & start.
REM STILL NEVER includes: data/ (your tokens & chat logs), one-api.db (your API keys).

REM remove python bytecode cache so it does not sneak into the zip
powershell -NoProfile -Command "Get-ChildItem -Recurse -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force"

if not exist "bin\new-api.exe" (
    echo [WARN] bin\new-api.exe not found - run get-binaries.bat first.
    echo        Falling back to code-only package.
)

powershell -NoProfile -Command "$stamp = Get-Date -Format 'yyyyMMdd'; $out = 'AI-Card-Catcher-FULL-' + $stamp + '.zip'; $list = @('app','web','docs','bin','start.bat','stop.bat','start-recorder.bat','stop-recorder.bat','start-tunnel.bat','使用说明.md','README.md','.gitignore'); $existing = $list | Where-Object { Test-Path $_ }; Compress-Archive -Path $existing -DestinationPath $out -Force; $size = (Get-Item $out).Length / 1MB; Write-Host ('[OK] created ' + $out + ' (' + [math]::Round($size,1) + ' MB)'); Write-Host 'Included: code + UI + docs + bin exes'; Write-Host 'Excluded: data/ (privacy), one-api.db (your API keys)'"

echo.
pause
