#!/usr/bin/env sh
# Run without Docker. Only DATA_DIR differs from the container defaults — a
# relative path so nothing tries to write /data on a developer machine.
# Everything else comes from the application's own environment handling, so
# the defaults live in exactly one place.
set -eu
export DATA_DIR="${DATA_DIR:-./data}"
exec python3 app.py
