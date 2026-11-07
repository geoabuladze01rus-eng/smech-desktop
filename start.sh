#!/usr/bin/env sh
set -e
cd "$(dirname "$0")"
python -m uvicorn app:app --host 127.0.0.1 --port 8765
