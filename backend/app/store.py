"""SQLite store for tenants, users, loan applications, link codes, monitoring
and reports.

We host one backend for many banks and MSMEs. A `tenant` is a bank (the
customer who pays for and uses the dashboard); every application belongs to
exactly one tenant, and all bank-facing queries are scoped by `tenant_id` so
one bank never sees another's applicants. A `user` is either a bank login
(role `bank`, sees every application for its tenant) or an MSME login (role
`msme`, tied to exactly one `application_id` — the MSME only ever sees its
own company's data).

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
CREATE TABLE IF NOT EXISTS tenants (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES tenants(id),
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,            -- bank | msme
    application_id TEXT,           -- set for role = msme: the one application this login sees
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
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
    source TEXT NOT NULL,          -- initial | refresh
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
        # Added for multi-tenancy. Pre-existing rows get '' (no tenant) — a hosted
        # deploy with real tenants is expected to start from a fresh database.
        "tenant_id": "TEXT NOT NULL DEFAULT ''",
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
    # Created here, not in SCHEMA, because it's on a column added by MIGRATIONS above
    # (older databases don't have it until the ALTER TABLE just ran).
    conn.execute("CREATE INDEX IF NOT EXISTS applications_by_tenant ON applications(tenant_id, created_at)")
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


# A per-user random salt plus PBKDF2 is enough here (no third-party deps like
# bcrypt/argon2 — see project.md's "stdlib only" constraint) since logins are
# low-volume, human-typed passwords, not a high-throughput public API.
_PBKDF2_ITERATIONS = 200_000


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _PBKDF2_ITERATIONS)
    return f"{salt.hex()}:{dk.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, hash_hex = stored.split(":")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), _PBKDF2_ITERATIONS)
    return secrets.compare_digest(dk.hex(), hash_hex)


def report_dir(app_id: str, report_id: str):
    d = config.DATA_DIR / "applications" / app_id / "reports" / report_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# ----------------------------------------------------------- tenants & users


SEED_TENANT_ID = "seed-tenant"  # fixed id so _ensure_seed_tenant is idempotent across restarts


def seed_tenant_id() -> str:
    return SEED_TENANT_ID


def create_tenant(name: str, tenant_id: str | None = None) -> dict:
    tenant_id = tenant_id or uuid.uuid4().hex
    with db() as conn:
        conn.execute("INSERT INTO tenants (id, name, created_at) VALUES (?, ?, ?)", (tenant_id, name, now().isoformat()))
    return get_tenant(tenant_id)


def get_tenant(tenant_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def create_user(tenant_id: str, username: str, password: str, role: str, application_id: str | None = None) -> dict:
    """role is 'bank' (sees every application for tenant_id) or 'msme' (sees only application_id)."""
    user_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO users (id, tenant_id, username, password_hash, role, application_id, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user_id, tenant_id, username, _hash_password(password), role, application_id, now().isoformat()),
        )
    return get_user(user_id)


def set_password(user_id: str, password: str) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (_hash_password(password), user_id))


def get_user(user_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def authenticate(username: str, password: str, role: str) -> dict | None:
    """Constant-shape lookup for Basic auth: always hashes, even on an unknown
    username, so a wrong username and a wrong password fail in the same time."""
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ? AND role = ?", (username, role)).fetchone()
    user = dict(row) if row else None
    if _verify_password(password, user["password_hash"] if user else _hash_password("")):
        return user
    return None


def msme_login_for(app_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE application_id = ? AND role = 'msme'", (app_id,)).fetchone()
    return dict(row) if row else None


def create_or_reset_msme_login(app_id: str, tenant_id: str) -> tuple[str, str]:
    """Issue (or rotate the password of) the MSME login for one application.
    Returns (username, password) — the password is shown to the bank once."""
    password = secrets.token_urlsafe(9)
    existing = msme_login_for(app_id)
    if existing:
        set_password(existing["id"], password)
        return existing["username"], password
    username = f"msme-{app_id[:10]}"
    user = create_user(tenant_id, username, password, "msme", application_id=app_id)
    return user["username"], password


# ------------------------------------------------------------ applications


def create_application(tenant_id: str, applicant_name: str, reference: str, months: int,
                        monitoring_offered: bool = False) -> dict:
    app_id = uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO applications (id, tenant_id, applicant_name, reference, months, code, code_expires_at, status,"
            " created_at, monitoring_offered) VALUES (?, ?, ?, ?, ?, ?, ?, 'awaiting_data', ?, ?)",
            (app_id, tenant_id, applicant_name, reference, months, new_code(),
             (now() + timedelta(hours=config.CODE_TTL_HOURS)).isoformat(), now().isoformat(), int(monitoring_offered)),
        )
    return get_application(app_id)


def regenerate_code(app_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE applications SET code = ?, code_expires_at = ? WHERE id = ?",
            (new_code(), (now() + timedelta(hours=config.CODE_TTL_HOURS)).isoformat(), app_id),
        )


def get_application(app_id: str, tenant_id: str | None = None) -> dict | None:
    """tenant_id is None for connector-side lookups (already scoped by a code/token
    that uniquely identifies the application); bank/MSME routes must always pass it
    so one tenant can never fetch another tenant's application by guessing an id."""
    with db() as conn:
        row = conn.execute("SELECT * FROM applications WHERE id = ?", (app_id,)).fetchone()
    app = dict(row) if row else None
    if app and tenant_id is not None and app["tenant_id"] != tenant_id:
        return None
    return app


def list_applications(tenant_id: str) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM applications WHERE tenant_id = ? ORDER BY created_at DESC", (tenant_id,)
        ).fetchall()
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
    """Daily cadence: due once a calendar day has passed since the last report."""
    if app["monitoring_status"] != "active":
        return False
    if app["force_refresh"]:
        return True
    at = at or now()
    if not app["last_report_at"]:
        return True
    since = at - datetime.fromisoformat(app["last_report_at"])
    return since >= timedelta(days=1)


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


def latest_ready_report(app_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM reports WHERE application_id = ? AND status = 'ready' ORDER BY created_at DESC LIMIT 1",
            (app_id,),
        ).fetchone()
    return _report_row(row) if row else None


def load_report_json(app_id: str, report_id: str) -> dict | None:
    """The full report dict (metrics, indicators, flags, alerts) written to disk
    by `report.builder.build_report`/`process_report` — not duplicated in SQLite.
    Returns None if the file isn't there yet (report still processing/failed)."""
    path = report_dir(app_id, report_id) / "report.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def applications_with_latest_report(tenant_id: str) -> list[tuple[dict, dict | None]]:
    """One (application, latest ready report's full dict-or-None) pair per
    application for a tenant's portfolio view (`report.dashboard.portfolio_view`)."""
    out = []
    for app in list_applications(tenant_id):
        latest = latest_ready_report(app["id"])
        report = load_report_json(app["id"], latest["id"]) if latest else None
        out.append((app, report))
    return out


def _report_row(row) -> dict:
    r = dict(row)
    for k in ("indicators_json", "snapshot_json", "alerts_json"):
        r[k.removesuffix("_json")] = json.loads(r[k]) if r[k] else []
    return r
