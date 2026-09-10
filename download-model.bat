@echo off
setlocal
cd /d "%~dp0"
if not exist "settings.txt" copy /y "settings.example.txt" "settings.txt" >nul
if not exist ".venv\Scripts\python.exe" (
  echo Run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m rhodes_fast --config settings.txt --download-model
pause
