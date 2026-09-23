@echo off
setlocal
cd /d "%~dp0"

if not exist "settings.txt" copy /y "settings.example.txt" "settings.txt" >nul
if not exist ".venv\Scripts\python.exe" goto setup
if not exist ".venv\Scripts\pythonw.exe" goto setup

".venv\Scripts\python.exe" -c "import importlib.metadata as m; import rhodes_fast, webview; assert m.version('endfield') == rhodes_fast.__version__" >nul 2>nul
if errorlevel 1 (
  echo Updating Endfield and its interface in the existing environment...
  ".venv\Scripts\python.exe" -m pip install -e "."
  if errorlevel 1 goto setup_failed
  ".venv\Scripts\python.exe" -c "import importlib.metadata as m; import rhodes_fast, webview; assert m.version('endfield') == rhodes_fast.__version__" >nul 2>nul
  if errorlevel 1 goto setup_failed
)
goto launch

:setup
echo First run: preparing the Endfield environment...
call setup.bat auto
if errorlevel 1 goto setup_failed

:launch
start "" ".venv\Scripts\pythonw.exe" -m rhodes_fast --gui --config settings.txt
exit /b 0

:setup_failed
echo.
echo Setup failed. See the message above for details.
pause
exit /b 1
