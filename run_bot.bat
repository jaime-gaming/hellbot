@echo off
REM ===================================================================
REM  Welcome to Hell - Discord event bot
REM  Double-click this file to open the control panel (no console).
REM  First run: creates a virtual environment and installs dependencies.
REM ===================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title Welcome to Hell - starting

set "PYEXE="
where py >nul 2>&1 && set "PYEXE=py -3"
if not defined PYEXE (
    where python >nul 2>&1 && set "PYEXE=python"
)
if not defined PYEXE (
    powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show('Python 3.10 or newer was not found.' + [char]10 + [char]10 + 'Install it from https://www.python.org/downloads/ and tick ''Add python.exe to PATH'' during setup, then run this file again.','Welcome to Hell')" >nul
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment ^(first run only^)...
    %PYEXE% -m venv .venv
    if errorlevel 1 goto :venvfail
)

if not exist ".venv\.deps-ok" (
    echo Installing dependencies ^(first run only, this can take a minute^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
    if errorlevel 1 goto :depsfail
    echo ok> ".venv\.deps-ok"
)

REM pythonw = no console window; the control panel window is the UI.
if exist ".venv\Scripts\pythonw.exe" (
    start "" ".venv\Scripts\pythonw.exe" "launcher_main.py"
) else (
    start "" ".venv\Scripts\python.exe" "launcher_main.py"
)
exit /b 0

:venvfail
powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show('Could not create the Python virtual environment. Try running this file as administrator, or install Python from python.org.','Welcome to Hell')" >nul
exit /b 1

:depsfail
powershell -NoProfile -Command "Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show('Dependency installation failed. Check the internet connection and run this file again.','Welcome to Hell')" >nul
exit /b 1
