@echo off
cd /d "%~dp0"
set SMECH_ENABLE_WORKSPACE=1
if "%SMECH_DATABASE_URL%"=="" set "SMECH_DATABASE_URL=sqlite:///smech-workspace.db"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
pause
