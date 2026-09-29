@echo off
REM Double-click me. Sets everything up the first time, then opens the dashboard.
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found.
  echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH" on the first screen.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
  echo Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Could not create the virtual environment.
    pause
    exit /b 1
  )
)

call ".venv\Scripts\activate.bat"

where reviewer >nul 2>nul
if errorlevel 1 (
  echo Installing Reviewer - needs internet, takes about a minute...
  pip install -e . -q
  if errorlevel 1 (
    echo Install failed. Check your internet connection and try again.
    pause
    exit /b 1
  )
)

echo Starting the demo dashboard. Keep this window open. Press Ctrl+C to stop.
reviewer serve --demo --open
pause
