"""Bridge checks against a local n8n database (run by dev/bridge_e2e.sh from
backend/, with the backend's environment):

- re-sending an applied batch changes nothing
- the parts of a new snapshot sent in reverse order still apply correctly

    python ../dev/bridge_checks.py <application_id>
"""

import gzip
import json
import os
import sys

import psycopg

sys.path.insert(0, os.getcwd())
from app import n8n, store  # noqa: E402

ADMIN = os.environ["BRIDGE_ADMIN_URL"]
INGEST = os.environ["BRIDGE_DB_URL"]
app_id = sys.argv[1]


def apply(part: dict) -> dict:
    key = f"{part['application_id']}:{part['batch_seq']}:{part['part']}"
    with psycopg.connect(INGEST, autocommit=True) as conn:
        return conn.execute("SELECT apply_sync_batch(true, %s::jsonb, %s::jsonb)",
                            (json.dumps({"x-idempotency-key": key}), json.dumps(part))).fetchone()[0]


def fingerprint() -> tuple:
    with psycopg.connect(ADMIN) as conn:
        return conn.execute(
            "SELECT bridge_reconcile(%(a)s),"
            " (SELECT md5(string_agg(guid || vdate || alter_id || ledgers || body, '|' ORDER BY guid))"
            "    FROM book_vouchers WHERE application_id = %(a)s),"
            " (SELECT md5(state || max_alter_id || coalesce(last_full_at, '')) FROM books WHERE application_id = %(a)s),"
            " (SELECT md5(row(company_name, status, tally_ok_at, tally_error)::text) FROM applications WHERE id = %(a)s),"
            " (SELECT max(batch_seq) FROM applied_batches WHERE application_id = %(a)s)",
            {"a": app_id}).fetchone()


def latest_snapshot() -> dict:
    with store.db() as conn:
        row = conn.execute("SELECT payload_gz FROM n8n_outbox WHERE application_id = ? AND kind = 'snapshot'"
                           " AND length(payload_gz) > 0 ORDER BY batch_seq DESC LIMIT 1", (app_id,)).fetchone()
    return json.loads(gzip.decompress(bytes(row["payload_gz"])))


before = fingerprint()
assert before[0]["vouchers"] > 0, before
old = latest_snapshot()
answers = [apply(p) for p in n8n.parts(old)]
assert all(a["status"] == 200 and a["result"] == "duplicate" for a in answers), answers
assert fingerprint() == before, "a re-sent batch changed something"
print(f"resend of batch {old['batch_seq']}: {len(answers)} part(s) answered duplicate, nothing changed")

with store.db() as conn:  # queued but not handed to the worker
    seq = n8n.queue_snapshot(conn, app_id)
    conn.execute("UPDATE n8n_outbox SET sent_at = ?, last_error = 'sent by bridge_checks' WHERE application_id = ?"
                 " AND batch_seq = ?", (store.now().isoformat(), app_id, seq))
n8n.PART_BYTES = 300_000
parts = n8n.parts(latest_snapshot())
assert len(parts) >= 3, len(parts)
answers = [apply(p) for p in reversed(parts)]
assert [a["status"] for a in answers] == [202] * (len(parts) - 1) + [200], answers
after = fingerprint()
assert after[4] == seq and after[:4] == before[:4], (before, after)
assert all(a["status"] == 200 and a["result"] == "duplicate" for a in map(apply, parts))
print(f"snapshot {seq} in {len(parts)} parts sent in reverse: applied once, books identical; resend = duplicate")
