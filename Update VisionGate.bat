@echo off
setlocal
cd /d "%~dp0"
title Update VisionGate

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup VisionGate.ps1" -Action Backup
if errorlevel 1 goto :failed

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup VisionGate.ps1" -Action SourceUpdate
if errorlevel 1 goto :failed

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup VisionGate.ps1" -Action Update
if errorlevel 1 goto :failed

echo.
echo VisionGate is up to date. Your login, cameras, identities, automations, devices, and settings were preserved.
pause
exit /b 0

:failed
echo.
echo Update failed. No VisionGate data was removed.
pause
exit /b 1
