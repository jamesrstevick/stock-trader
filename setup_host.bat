@echo off
REM Double-click this on the Dell. It finds this folder, sets up Python/venv, then starts the host.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_host.ps1" %*
if errorlevel 1 (
  echo.
  echo Setup did not finish. Read the message above.
  pause
)
