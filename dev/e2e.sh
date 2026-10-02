#!/usr/bin/env bash
# End-to-end run: mock Tally -> connector (headless) -> backend -> report.
# Usage: dev/e2e.sh            (needs: go, backend/.venv)
set -euo pipefail
cd "$(dirname "$0")/.."

WORK=$(mktemp -d)
TALLY_PORT=${TALLY_PORT:-9100}
API_PORT=${API_PORT:-8765}
cleanup() { kill "${MOCK_PID:-}" "${API_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT

python3 dev/mock_tally.py --port "$TALLY_PORT" >"$WORK/mock.log" 2>&1 & MOCK_PID=$!
(cd backend && TC_DATA_DIR="$WORK/data" exec .venv/bin/uvicorn app.main:app --port "$API_PORT" >"$WORK/api.log" 2>&1) & API_PID=$!
(cd connector && go build -o "$WORK/connector" .)

for _ in $(seq 1 50); do
  curl -sf "localhost:$API_PORT/healthz" >/dev/null && curl -sf "localhost:$TALLY_PORT" >/dev/null && break
  sleep 0.2
done

APP=$(curl -sf -u admin:admin -H 'Content-Type: application/json' \
  -d '{"applicant_name":"Shree Ganesh Traders","reference":"LN-E2E","months":24,"monitoring":true}' "localhost:$API_PORT/api/bank/applications")
CODE=$(python3 -c 'import sys,json;print(json.loads(sys.argv[1])["link_code"])' "$APP")
ID=$(python3 -c 'import sys,json;print(json.loads(sys.argv[1])["id"])' "$APP")
echo "application $ID, code $CODE"

export TC_HOME="$WORK/home"   # where the connector keeps its monitoring settings
"$WORK/connector" -tally "http://localhost:$TALLY_PORT" -server "http://localhost:$API_PORT" \
  -code "$CODE" -company "Shree Ganesh Traders Pvt Ltd" -consent "E2E Test" -monitor

for _ in $(seq 1 50); do
  STATUS=$(curl -sf -u admin:admin "localhost:$API_PORT/api/bank/applications/$ID" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$STATUS" != processing ] && break
  sleep 0.2
done
echo "status: $STATUS"
[ "$STATUS" = ready ] || { cat "$WORK/api.log"; exit 1; }

curl -sf -u admin:admin "localhost:$API_PORT/bank/applications/$ID/report" -o "$WORK/report.html"
echo "report: $WORK/report.html"
# The code is single-use.
if curl -sf -H 'Content-Type: application/json' -d "{\"code\":\"$CODE\"}" "localhost:$API_PORT/api/connector/verify" >/dev/null; then
  echo "FAIL: code still valid after upload"; exit 1
fi

# --- monthly monitoring ---------------------------------------------------
api() { curl -sf -u admin:admin "localhost:$API_PORT$1" "${@:2}"; }
field() { python3 -c "import sys,json;print(json.load(sys.stdin)$1)"; }
[ -f "$TC_HOME/monitor.json" ] || { echo "FAIL: monitoring not set up"; exit 1; }
"$WORK/connector" -monitor-run
[ "$(api "/api/bank/applications/$ID/reports" | field '.__len__()')" = 1 ] || { echo "FAIL: uploaded when not due"; exit 1; }
api "/api/bank/applications/$ID/monitoring/refresh" -X POST >/dev/null
"$WORK/connector" -monitor-run
sleep 1
[ "$(api "/api/bank/applications/$ID/reports" | field '.__len__()')" = 2 ] || { echo "FAIL: refresh not uploaded"; exit 1; }
"$WORK/connector" -monitor-stop
[ "$(api "/api/bank/applications/$ID" | field '["monitoring_status"]')" = stopped_by_client ] || { echo "FAIL: stop"; exit 1; }
[ ! -f "$TC_HOME/monitor.json" ] || { echo "FAIL: settings left behind"; exit 1; }
echo "monitoring: opt-in, not-due skip, refresh, stop OK"
echo "OK"
