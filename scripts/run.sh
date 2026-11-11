#!/usr/bin/env bash
# Start the simulator on 127.0.0.1:8600 (set OPTLAB_API_PORT for another port).
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
[ -x "$PY" ] || PY=python3
PYTHONPATH=src exec "$PY" -m optlab.api
