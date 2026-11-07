#!/usr/bin/env bash
set -eu
cd "$(dirname "$0")"
# Single-computer test mode. Do not expose this HTTP port to the internet.
export SMECH_ENABLE_WORKSPACE=1
export SMECH_DATABASE_URL="${SMECH_DATABASE_URL:-sqlite:///$(pwd)/smech-workspace.db}"
python -m uvicorn app:app --host 127.0.0.1 --port 8000
