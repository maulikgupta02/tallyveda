"""Stand-in for the n8n "Tally bridge (MATTR AI)" workflow
(audit/n8n-reference/Tally_bridge.json), for local runs of dev/bridge_e2e.sh:
the same checks as its Code node (HMAC over timestamp + "." + raw body,
5-minute window, gunzip), then the same SQL call its Postgres node makes.

    BRIDGE_DB_URL=postgresql://n8n_ingest@127.0.0.1:55432/tally_data \
    TC_N8N_WEBHOOK_SECRET=... python dev/bridge_receiver.py --port 8770

Never logs payloads.
"""

import argparse
import gzip
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

SECRET = os.environ["TC_N8N_WEBHOOK_SECRET"].encode()
DB = os.environ["BRIDGE_DB_URL"]
KEEP = ("x-idempotency-key", "x-application-id", "x-batch-seq", "x-kind", "x-part", "x-of", "x-timestamp")


def apply(sig_ok: bool, headers: dict, body: str) -> dict:
    with psycopg.connect(DB, autocommit=True) as conn:
        return conn.execute("SELECT apply_sync_batch(%s, %s::jsonb, %s::jsonb)",
                            (sig_ok, json.dumps(headers), body)).fetchone()[0]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        h = {k.lower(): v for k, v in self.headers.items()}
        kept = {k: h[k] for k in KEEP if k in h}
        ts, sig = h.get("x-timestamp", ""), h.get("x-signature", "")
        expected = "sha256=" + hmac.new(SECRET, ts.encode() + b"." + raw, hashlib.sha256).hexdigest()
        ok = ts.isdigit() and abs(time.time() - int(ts)) <= 300 and hmac.compare_digest(sig, expected)
        body = "{}"
        if ok:
            try:
                body = gzip.decompress(raw).decode()
                json.loads(body)
            except (OSError, ValueError):
                return self.reply(400, {"ok": False, "error": "not_gzipped_json"})
        try:
            r = apply(ok, kept, body)
        except psycopg.Error:
            return self.reply(503, {"ok": False, "error": "temporarily_unavailable", "retry": True})
        self.reply(int(r.pop("status")), r)

    def reply(self, status: int, obj: dict):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)
        sys.stderr.write(f"bridge {self.headers.get('X-Idempotency-Key')} {self.headers.get('X-Kind')} -> {status}\n")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--port", type=int, default=8770)
    ThreadingHTTPServer(("127.0.0.1", p.parse_args().port), Handler).serve_forever()
