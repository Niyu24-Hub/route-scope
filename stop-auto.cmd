@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Route Scope Python environment was not found.
  exit /b 1
)
".venv\Scripts\python.exe" -m route_scope watch-stop
