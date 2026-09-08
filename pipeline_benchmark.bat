@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m rhodes_fast --config settings.txt --pipeline-benchmark 1000
pause
