@echo off
setlocal
cd /d "%~dp0"
title Configure VisionGate Login

if not exist ".venv\Scripts\python.exe" (
    echo VisionGate is not installed yet. Run Install VisionGate.bat first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" auth.py
if errorlevel 1 (
    echo.
    echo Login configuration failed. Review the message above.
) else (
    echo.
    echo Restart VisionGate to use the new login.
)
pause
