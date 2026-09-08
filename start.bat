@echo off
cd /d "%~dp0"
start "" ".venv\Scripts\pythonw.exe" -m rhodes_fast --gui --config settings.txt
