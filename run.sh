#!/usr/bin/env bash
# Starts the controller against a running simulator and runs until the scenario finishes.
# No Docker; needs Python 3.10+ with httpx (in requirements.txt). State lives in ./state/ (SQLite).
#   ./run.sh http://127.0.0.1:8700
# Restarting with the same URL resumes from ./state/controller.db (crash-safe).
set -euo pipefail
SIM_URL="${1:?usage: ./run.sh <SIM_URL>}"
cd "$(dirname "$0")"
python -c "import httpx" 2>/dev/null || pip install -q httpx 2>/dev/null || pip install -q --break-system-packages httpx
exec python -m controller --url "$SIM_URL" --db "${CONTROLLER_DB:-state/controller.db}" --status-dir "${CONTROLLER_STATUS:-state}"
