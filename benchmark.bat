@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m rhodes_fast --config settings.txt --benchmark 100
pause
