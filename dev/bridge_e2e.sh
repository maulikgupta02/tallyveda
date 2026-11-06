#!/usr/bin/env bash
# MATTR AI bridge, end to end on this machine:
#   mock Tally -> connector (headless) -> backend -> outbox -> dev/bridge_receiver.py
#   (stand-in for the n8n workflow) -> apply_sync_batch() in a scratch Postgres
#   loaded with whatsapp_bot.sql, fix_01, access_v1.sql and n8n_bridge.sql.
# Then checks the bot's answers, reconciliation, idempotency, out-of-order
# parts and a deletion request.
#
# Usage: PGHOST=127.0.0.1 PGPORT=55432 dev/bridge_e2e.sh
#   needs: go, backend/.venv, a Postgres you can reach as user postgres
#   (trust auth). It creates and drops the databases tv_bridge_n8n and
#   tv_bridge_backend. Never point it at a real server.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$PWD
: "${PGHOST:=127.0.0.1}" "${PGPORT:=55432}"
PSQL=${PSQL:-psql}
N8N_DB=tv_bridge_n8n
BACK_DB=tv_bridge_backend
# The bot's own SQL (whatsapp_bot.sql, access_v1.sql, ...) is not in this repo:
# point BOT_SQL_DIR at a folder holding a copy. The bridge's SQL is in deploy/n8n.
BOT_SQL_DIR=${BOT_SQL_DIR:-audit/n8n-reference}
for f in whatsapp_bot.sql whatsapp_bot_memory_addon.sql fix_01_posting_vouchers.sql access_v1.sql; do
  [ -f "$BOT_SQL_DIR/$f" ] || { echo "Missing $BOT_SQL_DIR/$f: set BOT_SQL_DIR to the bot's SQL files"; exit 1; }
done
WORK=$(mktemp -d)
TALLY_PORT=${TALLY_PORT:-9101}
API_PORT=${API_PORT:-8766}
RECV_PORT=${RECV_PORT:-8770}
PHONE=919000000001

cleanup() { kill "${MOCK_PID:-}" "${API_PID:-}" "${RECV_PID:-}" 2>/dev/null || true; }
trap cleanup EXIT
q() { $PSQL -h "$PGHOST" -p "$PGPORT" -U postgres -d "$N8N_DB" -v ON_ERROR_STOP=1 -Atq "$@"; }

for db in "$N8N_DB" "$BACK_DB"; do
  $PSQL -h "$PGHOST" -p "$PGPORT" -U postgres -d postgres -qc "DROP DATABASE IF EXISTS $db" -c "CREATE DATABASE $db"
done
# The bot's database was a pg_dump of the backend: same tables, same columns.
export TC_DEV=1 TC_DATA_DIR="$WORK/data"
(cd backend && DATABASE_URL="postgresql://postgres@$PGHOST:$PGPORT/$N8N_DB" .venv/bin/python -c "from app import store; store._connect().close()")
# n8n_bridge.sql grants on database tally_data; this scratch one has another name.
for f in "$BOT_SQL_DIR"/whatsapp_bot.sql "$BOT_SQL_DIR"/whatsapp_bot_memory_addon.sql \
         "$BOT_SQL_DIR"/fix_01_posting_vouchers.sql "$BOT_SQL_DIR"/access_v1.sql deploy/n8n/n8n_bridge.sql; do
  sed "s/DATABASE tally_data/DATABASE $N8N_DB/" "$f" | q >/dev/null
done
echo "n8n database ready"

export DATABASE_URL="postgresql://postgres@$PGHOST:$PGPORT/$BACK_DB"   # the backend on Postgres, as on Render
export TC_N8N_WEBHOOK_URL="http://127.0.0.1:$RECV_PORT/webhook/mattr-bridge"
export TC_N8N_WEBHOOK_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
export BRIDGE_DB_URL="postgresql://n8n_ingest@$PGHOST:$PGPORT/$N8N_DB"   # the workflow's role
export BRIDGE_ADMIN_URL="postgresql://postgres@$PGHOST:$PGPORT/$N8N_DB"

python3 dev/mock_tally.py --port "$TALLY_PORT" >"$WORK/mock.log" 2>&1 & MOCK_PID=$!
backend/.venv/bin/python dev/bridge_receiver.py --port "$RECV_PORT" 2>"$WORK/recv.log" & RECV_PID=$!
(cd backend && exec .venv/bin/uvicorn app.main:app --port "$API_PORT" >"$WORK/api.log" 2>&1) & API_PID=$!
(cd connector && go build -o "$WORK/connector" .)
for _ in $(seq 1 100); do
  curl -sf "localhost:$API_PORT/healthz" >/dev/null && curl -sf "localhost:$TALLY_PORT" >/dev/null && break
  sleep 0.2
done
manage() { (cd backend && .venv/bin/python -m app.manage "$@"); }

APP=$(curl -sf -u admin:admin -H 'Content-Type: application/json' \
  -d '{"applicant_name":"Shree Ganesh Traders","reference":"LN-BRIDGE","months":24,"monitoring":true}' "localhost:$API_PORT/api/bank/applications")
CODE=$(python3 -c 'import sys,json;print(json.loads(sys.argv[1])["link_code"])' "$APP")
ID=$(python3 -c 'import sys,json;print(json.loads(sys.argv[1])["id"])' "$APP")
echo "application $ID"

export TC_HOME="$WORK/home"
"$WORK/connector" -tally "http://localhost:$TALLY_PORT" -server "http://localhost:$API_PORT" \
  -code "$CODE" -company "Shree Ganesh Traders Pvt Ltd" -consent "E2E Test" -monitor >"$WORK/connector.log" 2>&1

# The first share reads 3 months, then the connector extends history; wait for April 2026 to be in n8n.
for _ in $(seq 1 300); do
  FROM=$(q -c "SELECT state::jsonb #>> '{period,from}' FROM books WHERE application_id = '$ID'" || true)
  STATUS=$(q -c "SELECT status FROM applications WHERE id = '$ID'" || true)
  [ -n "$FROM" ] && [[ "$FROM" < "2026-04-02" ]] && [ "$STATUS" = ready ] && break
  sleep 0.5
done
echo "n8n holds books from $FROM, status $STATUS"
[ "$STATUS" = ready ] && [[ "$FROM" < "2026-04-02" ]] || { tail -20 "$WORK/api.log" "$WORK/recv.log"; exit 1; }

q -c "INSERT INTO wa_contacts (phone, display_name, verified_at) VALUES ('$PHONE', 'Test owner', now())" \
  -c "INSERT INTO wa_company_access (phone, application_id, granted_by, role) VALUES ('$PHONE', '$ID', 'admin', 'owner')"
q -c "SELECT bot_resolve('$PHONE')->>'active_company_name'"
SALES=$(q -c "SELECT bot_sales_summary('$PHONE', '2026-04-01', '2026-04-30')")
echo "April 2026: $SALES"
TAXABLE=$(python3 -c 'import sys,json;print(json.loads(sys.argv[1])["taxable_sales"])' "$SALES")
[ "$TAXABLE" = 4184571.51 ] || { echo "FAIL: taxable_sales $TAXABLE, expected 4184571.51"; exit 1; }

# The connector exits while the server still finishes the last backfill: wait for it to land.
for _ in $(seq 1 240); do
  PENDING=$(cd backend && .venv/bin/python -c "
import sys
from app import store
with store.db() as c:
    s = c.execute(\"SELECT COUNT(*) AS n FROM sync_sessions WHERE application_id = ? AND status IN ('open', 'finishing')\", (sys.argv[1],)).fetchone()['n']
    o = c.execute('SELECT COUNT(*) AS n FROM n8n_outbox WHERE sent_at IS NULL', ()).fetchone()['n']
print(s + o)" "$ID")
  [ "$PENDING" = 0 ] && break
  sleep 0.5
done
q -c "SELECT bridge_reconcile_all()" >"$WORK/n8n.json"
manage n8n-reconcile --compare "$WORK/n8n.json"

(cd backend && .venv/bin/python ../dev/bridge_checks.py "$ID")

# A heartbeat with Tally down reaches the bot.
TOKEN=$(python3 -c '
import json, pathlib, sys
f = next(pathlib.Path(sys.argv[1]).rglob("companies.json"))
print(json.loads(f.read_text())[0]["token"])' "$TC_HOME")
curl -sf -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"tally":"down","error":"Tally is not running"}' "localhost:$API_PORT/api/connector/monitor/status" >/dev/null
for _ in $(seq 1 20); do [ "$(q -c "SELECT bot_sync_info('$ID')->>'state'")" = down ] && break; sleep 0.25; done
[ "$(q -c "SELECT bot_sync_info('$ID')->>'state'")" = down ] || { echo "FAIL: Tally down not propagated"; exit 1; }
echo "heartbeat: Tally down reached the bot"

# A deletion request removes the company from the bot.
(cd backend && .venv/bin/python -c "import sys; from app import n8n, store; store.request_deletion(sys.argv[1], 'e2e'); print(n8n.deliver_pending())" "$ID")
RESOLVE=$(q -c "SELECT bot_resolve('$PHONE')")
echo "after deletion request: $RESOLVE"
[ "$(python3 -c 'import sys,json;print(json.loads(sys.argv[1]).get("reason"))' "$RESOLVE")" = unknown_phone ] \
  || [ "$(python3 -c 'import sys,json;print(json.loads(sys.argv[1]).get("reason"))' "$RESOLVE")" = no_companies ] \
  || { echo "FAIL: company still resolvable"; exit 1; }
[ "$(q -c "SELECT count(*) FROM book_vouchers WHERE application_id = '$ID'")" = 0 ] || { echo "FAIL: vouchers left"; exit 1; }
[ "$(q -c "SELECT count(*) FROM applications WHERE id = '$ID'")" = 0 ] || { echo "FAIL: application left"; exit 1; }
echo "OK"
