@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Требуется Python 3.12 с python.org (установить с python launcher).
  pause
  exit /b 1
)
if not exist ".venv-desktop\Scripts\python.exe" (
  py -3.12 -m venv .venv-desktop
  if errorlevel 1 (
    echo Не удалось создать Python 3.12 окружение.
    pause
    exit /b 1
  )
)
if not exist ".venv-desktop\.installed" (
  .venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop-run.txt
  if errorlevel 1 (
    echo Не удалось установить зависимости. Проверьте интернет.
    pause
    exit /b 1
  )
  type nul > ".venv-desktop\.installed"
)
.venv-desktop\Scripts\python.exe desktop_launcher.py
if errorlevel 1 pause
