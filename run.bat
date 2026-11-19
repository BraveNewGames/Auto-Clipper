@echo off
rem Starts the Auto-Clipper UI in your browser. Close this window (or Ctrl+C) to stop.
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python 3.10+ was not found on PATH. Install it from python.org and try again.
  pause
  exit /b 1
)
python -m autoclipper %*
if errorlevel 1 pause
