#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -m venv .venv-desktop
. .venv-desktop/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements-desktop.txt -r requirements-dev.txt
python -m pytest -q
python scripts/build_native.py
echo 'Готово: dist/Smech.app'
