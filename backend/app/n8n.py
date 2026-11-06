"""MATTR AI bridge: push each company's applied books to the WhatsApp bot's
database through an n8n webhook.

The backend is the system of record; the bot's database (`tally_data`, next
to n8n) is a projection that never computes books itself. Data flows one way:

- `snapshot`: written in the same transaction as `books.finish()` (and by
  `manage.py n8n-resync`). It holds the bot's columns of `applications`, the
  `books` row, every `book_vouchers` row and the latest `sync_sessions` row.
  n8n replaces that company's rows with it, so a snapshot is self-contained.
- `status`: the Tally connection columns, when a monitoring check-in changes
  them (or the copy in n8n is over an hour old), so "is Tally connected?"
  stays right between syncs.
- `purge`: retention purge, deletion request, deletion, or forwarding turned
  off. n8n deletes the company's rows and its chat memory.

Rows go into `n8n_outbox` with a per-company `batch_seq` (from `n8n_seq`,
whose row lock keeps two writers from taking the same number). A worker
thread sends them right after commit, in order per company, and retries from
ordinary traffic (`maybe_deliver`, called by the HTTP middleware), because a
Render free service sleeps and has no scheduler. Failures back off
exponentially; after 24 hours a row is given up, logged and emailed to
TC_ALERT_EMAIL. When n8n answers 409 (it missed a batch), a fresh snapshot is
queued so it heals itself.

Each request is one part of a batch, gzipped JSON of at most PART_BYTES before
compression, signed with HMAC-SHA256 over `timestamp + "." + raw body`.
Payloads are financial data: they are never logged, and sent rows are
deleted after SENT_KEEP_DAYS.

Off unless TC_N8N_WEBHOOK_URL and TC_N8N_WEBHOOK_SECRET are both set, and
only for companies with `applications.mattr_forward = 1`.
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import json
import logging
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from . import config, store

log = logging.getLogger("tally_connector")

# The applications columns the bot reads, plus the NOT NULL ones a first
# insert into its copy needs. Nothing else (contact email, tokens, OTPs) leaves.
APP_COLUMNS = ("id", "tenant_id", "company_name", "applicant_name", "status", "months", "created_at",
               "deletion_requested_at", "raw_purged_at", "connector_version",
               "tally_ok_at", "tally_down_since", "tally_error")
STATUS_COLUMNS = ("tally_ok_at", "tally_down_since", "tally_error", "connector_version")

PART_BYTES = 5 * 1024 * 1024       # JSON size of one part before gzip, so the gzipped part is smaller still
GIVE_UP_AFTER = timedelta(hours=24)
FIRST_RETRY_SECONDS = 30
MAX_RETRY_SECONDS = 3600
STATUS_REFRESH = timedelta(hours=1)
SENT_KEEP_DAYS = 2
POLL_SECONDS = 60
TIMEOUT_SECONDS = 30


def enabled() -> bool:
    return bool(config.N8N_WEBHOOK_URL and config.N8N_WEBHOOK_SECRET)


def _now() -> str:
    return store.now().isoformat()


# ------------------------------------------------------------- queueing
# Every queue_* function takes the caller's open connection, so the outbox row
# commits (or rolls back) with the change it describes.


def _forwarded(conn, app_id: str) -> bool:
    row = conn.execute("SELECT mattr_forward FROM applications WHERE id = ?", (app_id,)).fetchone()
    return bool(row and row["mattr_forward"])


def _was_sent(conn, app_id: str) -> bool:
    return conn.execute("SELECT 1 FROM n8n_seq WHERE application_id = ?", (app_id,)).fetchone() is not None


def _app_row(conn, app_id: str) -> dict | None:
    row = conn.execute(f"SELECT {', '.join(APP_COLUMNS)} FROM applications WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def _enqueue(conn, app_id: str, kind: str, body: dict, status_ok_at: str | None = None) -> int:
    row = conn.execute(
        "INSERT INTO n8n_seq (application_id, last_seq, status_ok_at) VALUES (?, 1, ?)"
        " ON CONFLICT (application_id) DO UPDATE SET last_seq = n8n_seq.last_seq + 1,"
        " status_ok_at = COALESCE(excluded.status_ok_at, n8n_seq.status_ok_at) RETURNING last_seq",
        (app_id, status_ok_at),
    ).fetchone()
    seq = row["last_seq"]
    now = _now()
    if kind in ("snapshot", "purge"):
        # Self-contained: earlier unsent snapshot/status rows are superseded, and
        # their payloads (financial data) need not wait around.
        conn.execute(
            "UPDATE n8n_outbox SET sent_at = ?, payload_gz = ?, last_error = ? WHERE application_id = ?"
            " AND sent_at IS NULL AND dead_at IS NULL AND kind IN ('snapshot', 'status') AND batch_seq < ?",
            (now, b"", f"superseded by {kind} {seq}", app_id, seq),
        )
    payload = {"kind": kind, "application_id": app_id, "batch_seq": seq, "created_at": now, **body}
    conn.execute(
        "INSERT INTO n8n_outbox (id, application_id, kind, batch_seq, payload_gz, created_at, next_attempt_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, app_id, kind, seq, gzip.compress(json.dumps(payload).encode()), now, now),
    )
    return seq


def queue_snapshot(conn, app_id: str) -> int | None:
    """Queue the company's applied state, read through `conn` so it is exactly
    what this transaction commits. None when off, not flagged or no books."""
    if not enabled() or not _forwarded(conn, app_id):
        return None
    book = conn.execute(
        "SELECT application_id, state, max_alter_id, last_full_at, updated_at FROM books WHERE application_id = ?",
        (app_id,),
    ).fetchone()
    if not book:
        return None
    app = _app_row(conn, app_id)
    session = conn.execute(
        "SELECT id, application_id, mode, status, period_from, period_to, months_done, created_at, updated_at"
        " FROM sync_sessions WHERE application_id = ? AND status = 'done' ORDER BY updated_at DESC LIMIT 1",
        (app_id,),
    ).fetchone()
    vouchers = [dict(r) for r in conn.execute(
        "SELECT guid, vdate, alter_id, ledgers, body FROM book_vouchers WHERE application_id = ? ORDER BY vdate, guid",
        (app_id,),
    ).fetchall()]
    return _enqueue(conn, app_id, "snapshot", {
        "application": app,
        "book": dict(book),
        "sync_session": dict(session) if session else None,
        "vouchers": vouchers,
    }, status_ok_at=app["tally_ok_at"] or "")


def queue_status(conn, app_id: str, before: dict | None) -> bool:
    """After a heartbeat or a finished report updated `applications`: queue its
    columns when the Tally state flipped, the error, connector version or
    status changed (the bot only lists 'ready' companies), or n8n's copy of
    tally_ok_at is over an hour old. `before` holds the columns read earlier."""
    if not enabled() or not _forwarded(conn, app_id):
        return False
    seq = conn.execute("SELECT status_ok_at FROM n8n_seq WHERE application_id = ?", (app_id,)).fetchone()
    if not seq:
        return False  # n8n has no copy of this company yet; its first snapshot carries the state
    app = _app_row(conn, app_id)
    before = before or {}
    changed = bool(before.get("tally_down_since")) != bool(app["tally_down_since"]) or any(
        (before[k] or "") != (app[k] or "") for k in ("tally_error", "connector_version", "status") if k in before)
    sent_ok = seq["status_ok_at"]
    stale = bool(app["tally_ok_at"]) and (
        not sent_ok or datetime.fromisoformat(app["tally_ok_at"]) - datetime.fromisoformat(sent_ok) >= STATUS_REFRESH)
    if not (changed or stale):
        return False
    _enqueue(conn, app_id, "status", {"application": app}, status_ok_at=app["tally_ok_at"] or "")
    return True


def queue_purge(conn, app_id: str, reason: str) -> int | None:
    """Queue deletion of the company from n8n, if it was ever sent there. Sent
    even after forwarding was switched off: that is when it matters."""
    if not enabled() or not _was_sent(conn, app_id):
        return None
    return _enqueue(conn, app_id, "purge", {"reason": reason})


def set_forward(app_id: str, on: bool) -> int | None:
    """Flag a company for MATTR AI (and queue its books) or unflag it (and
    queue a purge). Returns the queued batch_seq, if any."""
    with store.db() as conn:
        conn.execute("UPDATE applications SET mattr_forward = ? WHERE id = ?", (int(on), app_id))
        seq = queue_snapshot(conn, app_id) if on else queue_purge(conn, app_id, "forwarding switched off")
    kick()
    return seq


def resync(app_id: str) -> int | None:
    with store.db() as conn:
        seq = queue_snapshot(conn, app_id)
    kick()
    return seq


# ------------------------------------------------------------- the wire


def parts(payload: dict) -> list[dict]:
    """Split a batch into self-describing parts. Part 1 carries everything but
    the vouchers; vouchers are spread over parts of at most PART_BYTES of JSON.
    Deterministic, so a retry resends identical parts."""
    head = {k: payload[k] for k in ("kind", "application_id", "batch_seq", "created_at")}
    vouchers = payload.get("vouchers")
    if vouchers is None:
        return [{**head, "part": 1, "of": 1, **{k: v for k, v in payload.items() if k not in head}}]
    first = {k: v for k, v in payload.items() if k not in head and k != "vouchers"}
    budget = PART_BYTES - len(json.dumps(first)) - 1024
    chunks: list[list[dict]] = [[]]
    size = 0
    for v in vouchers:
        n = len(json.dumps(v)) + 2
        if chunks[-1] and size + n > budget:
            chunks.append([])
            size = 0
            budget = PART_BYTES - 1024
        chunks[-1].append(v)
        size += n
    of = len(chunks)
    out = []
    for i, chunk in enumerate(chunks, 1):
        part = {**head, "part": i, "of": of, "voucher_count": len(vouchers), "vouchers": chunk}
        if i == 1:
            part.update(first)
        out.append(part)
    return out


def sign(secret: str, timestamp: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()


def _post(url: str, body: bytes, headers: dict) -> int:
    """POST one part; returns the HTTP status (0 when the server is unreachable).
    Tests replace this."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def _send_part(part: dict) -> int:
    body = gzip.compress(json.dumps(part).encode(), mtime=0)
    ts = str(int(time.time()))
    headers = {
        "Content-Type": "application/gzip",
        "X-Timestamp": ts,
        "X-Signature": sign(config.N8N_WEBHOOK_SECRET, ts, body),
        "X-Idempotency-Key": f"{part['application_id']}:{part['batch_seq']}:{part['part']}",
        "X-Application-Id": part["application_id"],
        "X-Batch-Seq": str(part["batch_seq"]),
        "X-Kind": part["kind"],
        "X-Part": str(part["part"]),
        "X-Of": str(part["of"]),
    }
    return _post(config.N8N_WEBHOOK_URL, body, headers)


# ------------------------------------------------------------- delivery

_lock = threading.Lock()
_again = threading.Event()
_last_poll = 0.0


def kick() -> None:
    """Deliver in the background, after the caller's transaction committed."""
    if not enabled():
        return
    if not _lock.acquire(blocking=False):
        _again.set()  # the running worker makes one more pass
        return

    def run():
        try:
            while True:
                _again.clear()
                _deliver_locked()
                if not _again.is_set():
                    break
        except Exception:
            log.exception("n8n delivery failed")
        finally:
            _lock.release()

    threading.Thread(target=run, daemon=True).start()


def maybe_deliver() -> None:
    """Retries, driven by ordinary traffic: at most once a minute, and only
    when something is waiting."""
    global _last_poll
    if not enabled() or time.time() - _last_poll < POLL_SECONDS:
        return
    _last_poll = time.time()
    with store.db() as conn:
        due = conn.execute(
            "SELECT 1 FROM n8n_outbox WHERE sent_at IS NULL AND dead_at IS NULL AND next_attempt_at <= ? LIMIT 1",
            (_now(),),
        ).fetchone()
    if due:
        kick()


def deliver_pending() -> dict:
    """Send what is due, synchronously (manage.py and tests)."""
    with _lock:
        return _deliver_locked()


def _backoff(attempts: int) -> timedelta:
    return timedelta(seconds=min(FIRST_RETRY_SECONDS * 2 ** (attempts - 1), MAX_RETRY_SECONDS))


def _deliver_locked() -> dict:
    stats = {"sent": 0, "failed": 0, "dead": 0, "resynced": 0}
    if not enabled():
        return stats
    for _ in range(3):  # a 409 queues a snapshot; send it in the same run
        resynced = stats["resynced"]
        _deliver_pass(stats)
        if stats["resynced"] == resynced:
            break
    with store.db() as conn:
        cutoff = (store.now() - timedelta(days=SENT_KEEP_DAYS)).isoformat()
        conn.execute("DELETE FROM n8n_outbox WHERE sent_at IS NOT NULL AND sent_at < ?", (cutoff,))
    return stats


def _deliver_pass(stats: dict) -> None:
    with store.db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, application_id, kind, batch_seq, created_at, attempts, next_attempt_at FROM n8n_outbox"
            " WHERE sent_at IS NULL AND dead_at IS NULL ORDER BY application_id, batch_seq"
        ).fetchall()]
    by_app: dict[str, list[dict]] = {}
    for r in rows:
        by_app.setdefault(r["application_id"], []).append(r)
    for app_id, queue in by_app.items():
        for r in queue:
            now = store.now()
            if r["next_attempt_at"] > now.isoformat():
                break  # in order: nothing later for this company goes first
            with store.db() as conn:
                blob = conn.execute("SELECT payload_gz FROM n8n_outbox WHERE id = ?", (r["id"],)).fetchone()
            payload = json.loads(gzip.decompress(bytes(blob["payload_gz"])))
            code = 0
            for part in parts(payload):
                code = _send_part(part)
                if code not in (200, 202):
                    break
            del payload
            if code in (200, 202):
                with store.db() as conn:
                    conn.execute("UPDATE n8n_outbox SET sent_at = ?, attempts = attempts + 1, last_error = NULL"
                                 " WHERE id = ?", (now.isoformat(), r["id"]))
                stats["sent"] += 1
                continue
            if code == 409:
                # n8n is missing an earlier batch: a snapshot makes it whole again.
                with store.db() as conn:
                    conn.execute("UPDATE n8n_outbox SET sent_at = ?, attempts = attempts + 1, last_error = ?"
                                 " WHERE id = ?", (now.isoformat(), "409 from n8n; snapshot queued", r["id"]))
                    queue_snapshot(conn, app_id)
                log.warning("n8n: %s batch %s out of order; queued a snapshot", app_id, r["batch_seq"])
                stats["resynced"] += 1
                break
            attempts = r["attempts"] + 1
            error = f"HTTP {code}" if code else "unreachable"
            dead = now - datetime.fromisoformat(r["created_at"]) >= GIVE_UP_AFTER
            with store.db() as conn:
                conn.execute(
                    "UPDATE n8n_outbox SET attempts = ?, last_error = ?, next_attempt_at = ?, dead_at = ? WHERE id = ?",
                    (attempts, error, (now + _backoff(attempts)).isoformat(), now.isoformat() if dead else None, r["id"]),
                )
            if dead:
                stats["dead"] += 1
                _alert(app_id, r, error)
            else:
                stats["failed"] += 1
                log.warning("n8n: %s batch %s not delivered (%s), attempt %s", app_id, r["batch_seq"], error, attempts)
            break


def _alert(app_id: str, row: dict, error: str) -> None:
    msg = (f"MATTR AI bridge gave up on {row['kind']} batch {row['batch_seq']} of application {app_id}"
           f" after {GIVE_UP_AFTER} ({error}). Later batches still go out; run"
           f" `python -m app.manage n8n-resync {app_id}` once n8n is reachable.")
    log.error("n8n: %s", msg)
    if config.ALERT_EMAIL and config.SMTP_HOST:
        from . import mailer

        try:
            mailer.send(config.ALERT_EMAIL, "MATTR AI bridge: delivery failed", msg)
        except mailer.MailError:
            log.exception("n8n: alert email failed")


# ------------------------------------------------------------- reconciliation
# The same figures are computed by bridge_reconcile() in
# audit/n8n-reference/n8n_bridge.sql, so the two outputs compare as JSON.


def _money(x) -> str:
    q = Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return str(q if q != 0 else Decimal("0.00"))


def reconcile_facts(app_id: str) -> dict:
    with store.db() as conn:
        book = conn.execute("SELECT state, max_alter_id FROM books WHERE application_id = ?", (app_id,)).fetchone()
        rows = conn.execute("SELECT vdate, alter_id, body FROM book_vouchers WHERE application_id = ?",
                            (app_id,)).fetchall()
    months: dict[str, list[Decimal]] = {}
    max_alter = 0
    for r in rows:
        max_alter = max(max_alter, r["alter_id"])
        m = months.setdefault(r["vdate"][:7], [Decimal(0), Decimal(0)])
        for e in json.loads(r["body"]).get("entries", []):
            a = Decimal(str(e.get("amount") or 0))
            if a > 0:
                m[0] += a
            elif a < 0:
                m[1] -= a
    ledgers = json.loads(book["state"]).get("ledgers", []) if book else []
    pairs = sorted((l["name"], _money(l.get("closing_balance") or 0)) for l in ledgers)
    closing = hashlib.sha256("".join(f"{n}\t{c}\n" for n, c in pairs).encode()).hexdigest()
    return {
        "application_id": app_id,
        "vouchers": len(rows),
        "max_alter_id": max(max_alter, book["max_alter_id"] if book else 0),
        "months": {k: {"dr": _money(v[0]), "cr": _money(v[1])} for k, v in sorted(months.items())},
        "closing_sha256": closing,
    }
