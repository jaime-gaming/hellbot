@echo off
REM ===================================================================
REM  Build WelcomeToHellBot.exe (single file, no console window).
REM  Run this on Windows; the result lands in dist\.
REM ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Welcome to Hell - building the executable

set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    echo Python was not found. Install it from https://www.python.org/downloads/
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment...
    %PYEXE% -m venv .venv || goto :fail
)

echo Installing build dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet || goto :fail
".venv\Scripts\python.exe" -m pip install -r requirements.txt pyinstaller --quiet || goto :fail

echo Building...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean hellbot.spec || goto :fail

if not exist "dist\.env" if exist ".env" copy /y ".env" "dist\.env" >nul
if not exist "dist\.env" copy /y ".env.example" "dist\.env.example" >nul

echo.
echo ============================================================
echo  Done.  dist\WelcomeToHellBot.exe
echo  Copy that exe anywhere; it keeps .env, data\ and logs\
echo  next to itself.
echo ============================================================
pause
exit /b 0

:fail
echo.
echo Build failed. Scroll up for the error.
pause
exit /b 1
