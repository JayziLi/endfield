@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" tools\kmbox_monitor_check.py settings.txt
pause
