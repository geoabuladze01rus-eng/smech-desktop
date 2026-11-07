#!/bin/bash
set -e
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Требуется Python 3.12 или 3.13 с python.org'
  read -r -p 'Нажмите Enter для завершения...'
  exit 1
fi
if ! python3 -c 'import sys; assert sys.version_info >= (3, 11)' 2>/dev/null; then
  echo 'Установите Python версии 3.11 или новее (рекомендуется 3.12) с python.org'
  read -r -p 'Нажмите Enter для завершения...'
  exit 1
fi
if [ ! -x .venv-desktop/bin/python ]; then
  python3 -m venv .venv-desktop
fi
if [ ! -f .venv-desktop/.installed ]; then
  .venv-desktop/bin/python -m pip install -r requirements-desktop-run.txt
  touch .venv-desktop/.installed
fi
.venv-desktop/bin/python desktop_launcher.py
