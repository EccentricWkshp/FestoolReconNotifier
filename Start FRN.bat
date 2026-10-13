@echo off
setlocal

:: Run from this script's own folder, whatever directory it was launched from
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Could not find venv\Scripts\python.exe
    echo Create the virtual environment first:
    echo     python -m venv venv
    echo     venv\Scripts\python -m pip install -r requirements.txt
    pause
    exit /b 1
)

"venv\Scripts\python.exe" main.py

:: Keep the window open if the server exits so the error stays readable
echo.
echo Festool Recon Notifier has stopped.
pause
