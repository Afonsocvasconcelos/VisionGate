@echo off
setlocal
cd /d "%~dp0"
title Configure VisionGate Online Access

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Public Access.ps1" -Action Configure
if errorlevel 1 goto :failed

echo.
echo Online access is configured. Follow the router steps shown above,
echo then close any running VisionGate window and launch it again.
pause
exit /b 0

:failed
echo.
echo Online access setup failed. Nothing was exposed by the router automatically.
pause
exit /b 1
