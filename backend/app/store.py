"""SQLite store for loan applications, link codes, monitoring and reports.

An application is one borrower. Every upload (the first one via a one-time
code, later ones via the monitoring token) creates a row in `reports`, so the
bank keeps a history and can compare month to month.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import config

# No 0/O, 1/I/L: codes are read out over the phone.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    applicant_name TEXT NOT NULL,
    reference TEXT,
    months INTEGER NOT NULL,
    code TEXT UNIQUE,
    code_expires_at TEXT,
    status TEXT NOT NULL,          -- awaiting_data | processing | ready | failed
    created_at TEXT NOT NULL,
    uploaded_at TEXT,
    company_name TEXT,
    consent_name TEXT,
    uploader_ip TEXT,
    error TEXT
);
CREATE TABLE IF NOT EXISTS reports (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id),
    source TEXT NOT NULL,          -- initial | monthly
    created_at TEXT NOT NULL,
    status TEXT NOT NULL,          -- processing | ready | failed
    error TEXT,
    period_to TEXT,
    company_name TEXT,
    indicators_json TEXT,          -- [{key, label, status, display, value}]
    snapshot_json TEXT,           -- alerts.snapshot(): headline numbers for comparison
    high_flags INTEGER,
    alerts_json TEXT               -- changes vs the previous ready report
);
CREATE INDEX IF NOT EXISTS reports_by_app ON reports(application_id, created_at);
"""

# Columns added after the first release; applied to existing databases.
MIGRATIONS = {
    "applications": {
        "monitoring_offered": "INTEGER NOT NULL DEFAULT 0",
        "monitoring_status": "TEXT NOT NULL DEFAULT 'off'",  # off | active | stopped_by_bank | stopped_by_client
        "monitor_token_hash": "TEXT",
        "monitor_started_at": "TEXT",
        "monitor_last_seen_at": "TEXT",  # last time the client's scheduled task checked in
        "force_refresh": "INTEGER NOT NULL DEFAULT 0",
        "latest_report_id": "TEXT",
        "last_report_at": "TEXT",
    }
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def _connect():
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DATA_DIR / "tally_connector.db")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))


def normalise_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch.isalnum())


def display_code(code: str | None) -> str:
    return f"{code[:4]}-{code[4:]}" if code else ""


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def report_dir(app_id: str, report_id: str):
    d = config.DATA_DIR / "applications" / app_id / "reports" / report_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ------------------------------------------------------------ applications


def create_application(applicant_name: str, reference: str, months: int, monitoring_offered: bool = False) -> dict:
    app_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO applications (id, applicant_name, reference, months, code, code_expires_at, status, created_at,"
            " monitoring_offered) VALUES (?, ?, ?, ?, ?, ?, 'awaiting_data', ?, ?)",
            (app_id, applicant_name, reference, months, new_code(),
             (now() + timedelta(hours=config.CODE_TTL_HOURS)).isoformat(), now().isoformat(), int(monitoring_offered)),
        )
    return get_application(app_id)


def regenerate_code(app_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE applications SET code = ?, code_expires_at = ? WHERE id = ?",
            (new_code(), (now() + timedelta(hours=config.CODE_TTL_HOURS)).isoformat(), app_id),
        )


def get_application(app_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    return dict(row) if row else None


def list_applications() -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM applications ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


def find_by_code(code: str) -> dict | None:
    """Application for a live (unused, unexpired) code."""
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE code = ?", (normalise_code(code),)).fetchone()
    if not row:
        return None
    app = dict(row)
    if datetime.fromisoformat(app["code_expires_at"]) < now():
        return None
    return app


def mark_uploaded(app_id: str, company_name: str, consent_name: str, ip: str) -> None:
    # The code is single-use: clear it once data has been received.
    with db() as conn:
        conn.execute(
            "UPDATE applications SET code = NULL, code_expires_at = NULL, status = 'processing',"
            " uploaded_at = ?, company_name = ?, consent_name = ?, uploader_ip = ?, error = NULL WHERE id = ?",
            (now().isoformat(), company_name, consent_name, ip, app_id),
        )


def set_status(app_id: str, status: str, error: str | None = None) -> None:
    with db() as conn:
        conn.execute("UPDATE applications SET status = ?, error = ? WHERE id = ?", (status, error, app_id))


# -------------------------------------------------------------- monitoring


def start_monitoring(app_id: str) -> str:
    """Issue a new monitoring token (returned once, stored hashed)."""
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "UPDATE applications SET monitoring_status = 'active', monitor_token_hash = ?, monitor_started_at = ?,"
            " monitor_last_seen_at = ? WHERE id = ?",
            (_hash(token), now().isoformat(), now().isoformat(), app_id),
        )
    return token


def find_by_monitor_token(token: str) -> dict | None:
    if not token:
        return None
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE monitor_token_hash = ?", (_hash(token),)).fetchone()
        if row:
            conn.execute("UPDATE applications SET monitor_last_seen_at = ? WHERE id = ?", (now().isoformat(), row["id"]))
    return dict(row) if row else None


def stop_monitoring(app_id: str, by: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE applications SET monitoring_status = ?, force_refresh = 0 WHERE id = ? AND monitoring_status = 'active'",
            (f"stopped_by_{by}", app_id),
        )


def request_refresh(app_id: str) -> None:
    with db() as conn:
        conn.execute("UPDATE applications SET force_refresh = 1 WHERE id = ?", (app_id,))


def monitoring_due(app: dict, at: datetime | None = None) -> bool:
    """Monthly cadence: due once 25+ days have passed since the last report and
    the month has reached config.MONITOR_DAY (so last month is in the books)."""
    if app["monitoring_status"] != "active":
        return False
    if app["force_refresh"]:
        return True
    at = at or now()
    if not app["last_report_at"]:
        return True
    since = at - datetime.fromisoformat(app["last_report_at"])
    return since >= timedelta(days=25) and at.day >= config.MONITOR_DAY


def monitoring_overdue(app: dict, at: datetime | None = None) -> bool:
    if app["monitoring_status"] != "active" or not app["last_report_at"]:
        return False
    return (at or now()) - datetime.fromisoformat(app["last_report_at"]) > timedelta(days=config.MONITOR_OVERDUE_DAYS)


# ----------------------------------------------------------------- reports


def create_report(app_id: str, source: str, company_name: str) -> str:
    report_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO reports (id, application_id, source, created_at, status, company_name)"
            " VALUES (?, ?, ?, ?, 'processing', ?)",
            (report_id, app_id, source, now().isoformat(), company_name),
        )
    return report_id


def finish_report(report_id: str, period_to: str, indicators: list, snapshot: dict, high_flags: int, alerts: list) -> None:
    with db() as conn:
        r = conn.execute("SELECT application_id, created_at FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.execute(
            "UPDATE reports SET status = 'ready', error = NULL, period_to = ?, indicators_json = ?, snapshot_json = ?,"
            " high_flags = ?, alerts_json = ? WHERE id = ?",
            (period_to, json.dumps(indicators), json.dumps(snapshot), high_flags, json.dumps(alerts), report_id),
        )
        conn.execute(
            "UPDATE applications SET status = 'ready', error = NULL, latest_report_id = ?, last_report_at = ?,"
            " force_refresh = 0 WHERE id = ?",
            (report_id, r["created_at"], r["application_id"]),
        )


def fail_report(report_id: str, error: str) -> None:
    with db() as conn:
        r = conn.execute("SELECT application_id FROM reports WHERE id = ?", (report_id,)).fetchone()
        conn.execute("UPDATE reports SET status = 'failed', error = ? WHERE id = ?", (error, report_id))
        app = conn.execute("SELECT latest_report_id FROM applications WHERE id = ?", (r["application_id"],)).fetchone()
        if not app["latest_report_id"]:
            conn.execute("UPDATE applications SET status = 'failed', error = ? WHERE id = ?", (error, r["application_id"]))


def get_report(report_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
    return _report_row(row) if row else None


def list_reports(app_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM reports WHERE application_id = ? ORDER BY created_at DESC", (app_id,)).fetchall()
    return [_report_row(r) for r in rows]


def previous_ready_report(app_id: str, before: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE application_id = ? AND status = 'ready' AND created_at < ?"
            " ORDER BY created_at DESC LIMIT 1",
            (app_id, before),
        ).fetchone()
    return _report_row(row) if row else None


def _report_row(row) -> dict:
    r = dict(row)
    for k in ("indicators_json", "snapshot_json", "alerts_json"):
        r[k.removesuffix("_json")] = json.loads(r[k]) if r[k] else []
    return r
