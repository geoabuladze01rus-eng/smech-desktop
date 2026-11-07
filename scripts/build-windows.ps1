$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')
if (-not (Get-Command py -ErrorAction SilentlyContinue)) { throw 'Установите Python 3.11–3.13 с python.org' }
py -3.12 -m venv .venv-desktop
& .\.venv-desktop\Scripts\python.exe -m pip install --upgrade pip
& .\.venv-desktop\Scripts\python.exe -m pip install -r requirements-desktop.txt -r requirements-dev.txt
& .\.venv-desktop\Scripts\python.exe -m pytest -q
& .\.venv-desktop\Scripts\python.exe scripts\build_native.py
Write-Host 'Готово: dist\Smech\Smech.exe (все файлы папки Smech нужны для запуска)'
