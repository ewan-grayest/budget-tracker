#!/usr/bin/env sh
set -eu
mkdir -p "${DATA_DIR:-./data}"
export DB_PATH="${DB_PATH:-${DATA_DIR:-./data}/budget.db}"
export PORT="${PORT:-8080}"
export SEED_DEMO="${SEED_DEMO:-1}"
exec python3 app.py
