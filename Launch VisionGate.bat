@echo off
setlocal
cd /d "%~dp0"
title VisionGate

set "VISIONGATE_PYTHON=.venv\Scripts\python.exe"
set "VISIONGATE_SETUP=%~dp0scripts\Setup VisionGate.ps1"

if /i "%~1"=="--check" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Check
    exit /b %errorlevel%
)

if /i "%~1"=="--firewall" (
    netsh advfirewall firewall delete rule name="VisionGate" >nul 2>&1
    netsh advfirewall firewall add rule name="VisionGate" dir=in action=allow protocol=TCP localport=8000 profile=private remoteip=localsubnet >nul
    exit /b %errorlevel%
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Check >nul 2>&1
if errorlevel 1 (
    echo VisionGate needs to install or repair its dependencies...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Install || goto :failed
)

powershell.exe -NoProfile -Command "$rule=Get-NetFirewallRule -DisplayName 'VisionGate' -ErrorAction SilentlyContinue | Where-Object Enabled -eq 'True'; if($rule){exit 0}else{exit 1}" >nul 2>&1
if errorlevel 1 (
    echo Windows will ask once for permission to allow VisionGate on your private network.
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--firewall' -Verb RunAs -Wait" >nul 2>&1
    if errorlevel 1 echo WARNING: Firewall access was not granted; this PC will work, but other devices may not connect.
)

set "VISIONGATE_LAN="
for /f "delims=" %%I in ('"%VISIONGATE_PYTHON%" -c "from core import local_ipv4_addresses; a=local_ipv4_addresses(); print(a[0] if a else '')"') do set "VISIONGATE_LAN=%%I"

echo.
echo VisionGate is starting at http://127.0.0.1:8000
if defined VISIONGATE_LAN echo Other devices can open http://%VISIONGATE_LAN%:8000
echo Close this window to stop the server.
echo.

start "VisionGate Browser" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process 'http://127.0.0.1:8000'"
"%VISIONGATE_PYTHON%" -m uvicorn app:app --host 0.0.0.0 --port 8000 --no-use-colors

echo.
echo VisionGate stopped.
pause
exit /b 0

:failed
echo.
echo Installation or startup failed. Review the message above.
pause
exit /b 1
