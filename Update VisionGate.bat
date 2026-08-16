@echo off
setlocal
cd /d "%~dp0"
title Update VisionGate

if not exist ".git\" goto :dependencies
where git >nul 2>&1 || goto :git_missing
echo Downloading VisionGate updates...
git pull --ff-only || goto :failed
goto :dependencies

:git_missing
echo Git is unavailable, so only dependencies will be updated.

:dependencies

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup VisionGate.ps1" -Action Update
if errorlevel 1 goto :failed

echo.
echo VisionGate is up to date. Your cameras, whitelist, events, and settings were preserved.
pause
exit /b 0

:failed
echo.
echo Update failed. No VisionGate data was removed.
pause
exit /b 1
