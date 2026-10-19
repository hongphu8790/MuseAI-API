@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [MuseAI-API] .venv not found. Run: py -3.12 -m venv .venv
  exit /b 1
)
call .venv\Scripts\activate.bat
muse-web --host 127.0.0.1 --port 8788
