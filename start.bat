@echo off
setlocal
cd /d "%~dp0"

if not exist "settings.txt" copy /y "settings.example.txt" "settings.txt" >nul
if not exist ".venv\Scripts\pythonw.exe" (
  echo First run: preparing the Endfield environment...
  call setup.bat auto
  if errorlevel 1 (
    echo.
    echo Setup failed. See the message above for details.
    pause
    exit /b 1
  )
)

start "" ".venv\Scripts\pythonw.exe" -m rhodes_fast --gui --config settings.txt
