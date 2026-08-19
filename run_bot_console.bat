@echo off
REM ===================================================================
REM  Welcome to Hell - console mode (for debugging).
REM  Runs the bot directly with log output in this window.
REM ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Welcome to Hell - console

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment found. Run run_bot.bat once first.
    pause
    exit /b 1
)

".venv\Scripts\python.exe" bot.py
echo.
echo The bot has stopped. Press any key to close this window.
pause >nul
