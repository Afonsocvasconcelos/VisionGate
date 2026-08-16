@echo off
setlocal
cd /d "%~dp0"
title Install VisionGate

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Setup VisionGate.ps1" -Action Install
if errorlevel 1 goto :failed

echo.
echo Installation finished. Starting VisionGate...
call "%~dp0Launch VisionGate.bat"
exit /b %errorlevel%

:failed
echo.
echo Installation failed. Review the message above.
pause
exit /b 1
