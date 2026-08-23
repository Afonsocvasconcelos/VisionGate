@echo off
setlocal
cd /d "%~dp0"
title VisionGate

set "DATA_DIR=%~dp0data"
set "VISIONGATE_PYTHON=.venv\Scripts\python.exe"
set "VISIONGATE_SETUP=%~dp0scripts\Setup VisionGate.ps1"

if /i "%~1"=="--check" (
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Check
    exit /b %errorlevel%
)

if /i "%~1"=="--firewall" (
    netsh advfirewall firewall delete rule name="VisionGate" >nul 2>&1
    netsh advfirewall firewall add rule name="VisionGate" dir=in action=allow protocol=TCP localport=83 profile=private remoteip=localsubnet >nul
    exit /b %errorlevel%
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Check >nul 2>&1
if errorlevel 1 (
    echo VisionGate needs to install or repair its dependencies...
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%VISIONGATE_SETUP%" -Action Install || goto :failed
)

set "VISIONGATE_URL=http://127.0.0.1:83"
for /f "delims=" %%I in ('%VISIONGATE_PYTHON% -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('VISIONGATE_PUBLIC_HOST', ''))"') do set "VISIONGATE_PUBLIC_HOST=%%I"
if defined VISIONGATE_PUBLIC_HOST (
    set "VISIONGATE_URL=http://%VISIONGATE_PUBLIC_HOST%:83"
)

set "VISIONGATE_SOURCE_VERSION="
for /f "delims=" %%I in ('%VISIONGATE_PYTHON% -c "from pathlib import Path; print(max(p.stat().st_mtime_ns for p in Path('.').glob('*.py')))"') do set "VISIONGATE_SOURCE_VERSION=%%I"
if not defined VISIONGATE_SOURCE_VERSION goto :failed

powershell.exe -NoProfile -Command "$ProgressPreference='SilentlyContinue'; try { $health=Invoke-RestMethod -Uri 'http://127.0.0.1:83/health' -TimeoutSec 2; if($health.ok -and [string]$health.source_version -eq '%VISIONGATE_SOURCE_VERSION%' -and $health.data_location -eq 'local'){exit 0}; if($health.ok){exit 2} } catch {}; exit 1" >nul 2>&1
if errorlevel 2 goto :restart_updated
if not errorlevel 1 (
    echo.
    echo VisionGate is already running at %VISIONGATE_URL%
    start "VisionGate Browser" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Process '%VISIONGATE_URL%'"
    exit /b 0
)
goto :check_port

:restart_updated
echo Reloading updated VisionGate files...
powershell.exe -NoProfile -Command "$connection=Get-NetTCPConnection -LocalPort 83 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if(-not $connection){exit 0}; $process=Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $connection.OwningProcess); if($process.CommandLine -notmatch '(?i)-m\s+uvicorn\s+app:app'){exit 1}; Stop-Process -Id $connection.OwningProcess -Force; for($i=0;$i -lt 30;$i++){if(-not (Get-NetTCPConnection -LocalPort 83 -State Listen -ErrorAction SilentlyContinue)){exit 0}; Start-Sleep -Milliseconds 100}; exit 1" >nul 2>&1
if errorlevel 1 (
    echo Could not safely stop the outdated VisionGate process.
    echo Close its old window, then launch VisionGate again.
    pause
    exit /b 1
)

:check_port
powershell.exe -NoProfile -Command "if(Get-NetTCPConnection -LocalPort 83 -State Listen -ErrorAction SilentlyContinue){exit 0}else{exit 1}" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo Port 83 is already used by another application.
    echo Close that application, then launch VisionGate again.
    pause
    exit /b 1
)

"%VISIONGATE_PYTHON%" auth.py --ensure || goto :failed

powershell.exe -NoProfile -Command "$rule=Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -in @('VisionGate','VisionGate HTTP 83') -and $_.Enabled -eq 'True' } | Get-NetFirewallPortFilter | Where-Object LocalPort -eq '83'; if($rule){exit 0}else{exit 1}" >nul 2>&1
if errorlevel 1 (
    echo Windows will ask once for permission to allow VisionGate on your private network.
    powershell.exe -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '--firewall' -Verb RunAs -Wait" >nul 2>&1
    if errorlevel 1 echo WARNING: Firewall access was not granted; this PC will work, but other devices may not connect.
)

set "VISIONGATE_LAN="
for /f "delims=" %%I in ('%VISIONGATE_PYTHON% -c "from core import local_ipv4_addresses; a=local_ipv4_addresses(); print(a[0] if a else '')"') do set "VISIONGATE_LAN=%%I"

echo.
echo VisionGate is starting at http://127.0.0.1:83
if defined VISIONGATE_LAN echo Other devices can open http://%VISIONGATE_LAN%:83
if defined VISIONGATE_PUBLIC_HOST echo Public access: %VISIONGATE_URL%
echo Close this window to stop the server.
echo.

start "VisionGate Browser" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 4; Start-Process '%VISIONGATE_URL%'"
"%VISIONGATE_PYTHON%" -m uvicorn app:app --host 0.0.0.0 --port 83 --no-proxy-headers --no-use-colors

echo.
echo VisionGate stopped.
pause
exit /b 0

:failed
echo.
echo Installation or startup failed. Review the message above.
pause
exit /b 1
