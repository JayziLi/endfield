@echo off
setlocal
cd /d "%~dp0"

set "PROFILE=%~1"
if "%PROFILE%"=="" set "PROFILE=auto"
if /i not "%PROFILE%"=="auto" if /i not "%PROFILE%"=="cpu" if /i not "%PROFILE%"=="nvidia" (
  echo Usage: setup.bat [auto^|cpu^|nvidia]
  exit /b 2
)

if not exist ".venv\Scripts\python.exe" (
  echo [1/4] Creating a Python 3.11 environment...
  py -3.11 -m venv .venv 2>nul
  if errorlevel 1 python -m venv .venv 2>nul
  if errorlevel 1 (
    echo Python 3.11-3.13 was not found.
    echo Install Python from https://www.python.org/downloads/windows/ and run this file again.
    exit /b 1
  )
) else (
  echo [1/4] Reusing the existing environment.
)

if /i "%PROFILE%"=="auto" (
  where nvidia-smi.exe >nul 2>nul
  if errorlevel 1 (
    set "PROFILE=cpu"
  ) else (
    set "PROFILE=nvidia"
  )
)

echo [2/4] Installing the %PROFILE% runtime...
".venv\Scripts\python.exe" -m pip install --upgrade pip setuptools wheel
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -e ".[%PROFILE%]"
if errorlevel 1 exit /b 1

echo [3/4] Creating the local settings file...
if not exist "settings.txt" copy /y "settings.example.txt" "settings.txt" >nul

echo [4/4] Downloading the verified default YOLO model when needed...
".venv\Scripts\python.exe" -m rhodes_fast --config settings.txt --download-model
if errorlevel 1 exit /b 1

echo.
echo Endfield is ready. Runtime profile: %PROFILE%
echo Double-click start.bat to open the control panel.
exit /b 0
