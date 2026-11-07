#!/usr/bin/env bash
# Full cycle for one scenario: baseline, simulator, controller, grade.
#   scripts/run_scenario.sh scenarios/public_02_incidents.yaml public_02 8702
set -euo pipefail
SC=${1:?scenario}; NAME=${2:?name}; PORT=${3:-8700}
cd "$(dirname "$0")/.."
mkdir -p reports logs
[ -f runs/base_$NAME/_truth/aggregates.json ] || python -m sim baseline --scenario "$SC" --run-dir runs/base_$NAME > logs/base_$NAME.log 2>&1
rm -rf runs/mine_$NAME state/$NAME
python -m sim serve --scenario "$SC" --run-dir runs/mine_$NAME --force --port "$PORT" > logs/sim_$NAME.log 2>&1 &
SIM=$!
trap 'kill $SIM 2>/dev/null || true' EXIT
for i in $(seq 1 120); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 1; done
START=$(date +%s)
CONTROLLER_DB=state/$NAME/controller.db CONTROLLER_STATUS=state/$NAME ./run.sh "http://127.0.0.1:$PORT" > logs/ctl_$NAME.log 2>&1
echo "controller wall time: $(( $(date +%s) - START ))s"
kill $SIM; wait $SIM 2>/dev/null || true
python -m sim grade --run runs/mine_$NAME --baseline runs/base_$NAME --out reports/$NAME.md
sed -n '5,12p' reports/$NAME.md
