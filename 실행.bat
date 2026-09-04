@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist .env (
  echo [!] .env not found. Copy .env.example to .env and fill in the values.
  pause
  exit /b 1
)
if not exist .venv (
  echo [1/3] creating venv...
  python -m venv .venv
)
echo [2/3] installing packages...
.venv\Scripts\python -m pip install -q -r requirements.txt
echo [3/3] starting bot (Ctrl+C to stop)
.venv\Scripts\python bot.py
pause
