@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-windows.ps1" -Demo
if errorlevel 1 pause
