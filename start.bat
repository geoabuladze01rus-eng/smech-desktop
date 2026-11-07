@echo off
cd /d "%~dp0"
py -m uvicorn app:app --host 127.0.0.1 --port 8765
pause
