"""Store for tenants, users, loan applications, link codes, monitoring and
reports — SQLite by default, or Postgres when `DATABASE_URL` is set (the only
durable storage on a Render free web service, whose filesystem is ephemeral
and resets on every restart/sleep; see `docs/agents/architecture.md`).

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

Only two things differ by backend, both confined to this file: `_connect()`
(which engine, which placeholder/row shape) and `save_report_file`/
`load_report_file` (per-report artifacts — on disk under `DATA_DIR` for
SQLite, as a `bytea` row in Postgres, since Render's disk doesn't survive a
restart). Every other function here is plain SQL that runs unchanged on both.
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

DATABASE_URL = config.DATABASE_URL
IS_POSTGRES = DATABASE_URL.startswith(("postgres://", "postgresql://"))

if IS_POSTGRES:
    import psycopg
    from psycopg.rows import dict_row


class _PGConnection:
    """Makes a psycopg connection look enough like `sqlite3.Connection` that
    every query below — written once, with `?` placeholders and dict-style
    row access — runs unchanged on both engines. `DATABASE_URL` is typically
    Neon's pooled endpoint (PgBouncer transaction mode), which doesn't
    preserve session state across statements and can hand a later statement
    on the same logical connection to a different backend process — so this
    shim never relies on `SET`, temp tables or advisory locks, and disables
    server-side prepared statements (`prepare_threshold=None`). Each `db()`
    call runs its SCHEMA/ALTER TABLE/query statements inside one connection's
    implicit transaction, committed once at the end (see `db()` below)."""

    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql: str, params=()):
        return self._raw.execute(sql.replace("?", "%s"), params)

    def executemany(self, sql: str, seq) -> None:
        with self._raw.cursor() as cur:
            cur.executemany(sql.replace("?", "%s"), seq)

    def executescript(self, sql: str) -> None:
        for statement in sql.split(";"):
            statement = statement.strip()
            if statement:
                self._raw.execute(statement)

    def commit(self) -> None:
        self._raw.commit()

    def close(self) -> None:
        self._raw.close()


def _pg_connect():
    return _PGConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row, prepare_threshold=None))


def _existing_columns(conn, table: str) -> set[str]:
    if IS_POSTGRES:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", (table,)
        ).fetchall()
        return {r["column_name"] for r in rows}
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}

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
    role TEXT NOT NULL,            -- bank | msme | platform
    application_id TEXT,           -- set for role = msme: the one application this login sees
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target_id TEXT,
    summary TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_log_by_created ON audit_log(created_at);
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
CREATE TABLE IF NOT EXISTS report_files (
    application_id TEXT NOT NULL,
    report_id TEXT NOT NULL,
    name TEXT NOT NULL,
    data BYTEA NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (report_id, name)
);
CREATE TABLE IF NOT EXISTS leads (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    org TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    kind TEXT,
    created_at TEXT NOT NULL,
    user_agent TEXT,
    ip TEXT
);
CREATE INDEX IF NOT EXISTS leads_by_created ON leads(created_at);
CREATE TABLE IF NOT EXISTS books (
    application_id TEXT PRIMARY KEY,
    state TEXT NOT NULL,           -- JSON: company, period, groups, ledgers, voucher_types, stock, bills
    max_alter_id INTEGER NOT NULL DEFAULT 0,
    last_full_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS book_vouchers (
    application_id TEXT NOT NULL,
    guid TEXT NOT NULL,
    vdate TEXT NOT NULL,
    alter_id INTEGER NOT NULL DEFAULT 0,
    ledgers TEXT NOT NULL,         -- JSON list of ledger names in the entries
    body TEXT NOT NULL,            -- the voucher as uploaded
    PRIMARY KEY (application_id, guid)
);
CREATE INDEX IF NOT EXISTS book_vouchers_by_date ON book_vouchers(application_id, vdate);
CREATE TABLE IF NOT EXISTS sync_sessions (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    mode TEXT NOT NULL,            -- full | delta
    status TEXT NOT NULL,          -- open | finishing | done | abandoned | failed
    period_from TEXT NOT NULL,
    period_to TEXT NOT NULL,
    months_done TEXT NOT NULL DEFAULT '[]',
    staged TEXT NOT NULL DEFAULT '{}',  -- masters, balances, stock, bills: applied at finish
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS sync_sessions_by_app ON sync_sessions(application_id, created_at);
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
    },
    "tenants": {
        # Added for the platform admin panel. 'bank' is every pre-existing tenant;
        # 'direct' marks the single built-in platform tenant for MSMEs with no bank.
        "kind": "TEXT NOT NULL DEFAULT 'bank'",
        # Only meaningful for kind = 'bank' (suspend/reactivate from /admin); left
        # 'active' on the direct tenant, which has no suspend concept of its own.
        "status": "TEXT NOT NULL DEFAULT 'active'",
    },
    "users": {
        "disabled": "INTEGER NOT NULL DEFAULT 0",
    },
}


def now() -> datetime:
    return datetime.now(timezone.utc)


# Databases whose schema this process has already brought up to date. Keyed by
# target so tests that point DATA_DIR at a fresh directory still get a schema.
_schema_ready: set[str] = set()


def _connect():
    if IS_POSTGRES:
        conn, target = _pg_connect(), DATABASE_URL
    else:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        target = str(config.DATA_DIR / "tally_connector.db")
        conn = sqlite3.connect(target)
        conn.row_factory = sqlite3.Row
    if target in _schema_ready:
        return conn
    conn.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = _existing_columns(conn, table)
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
    # Created here, not in SCHEMA, because it's on a column added by MIGRATIONS above
    # (older databases don't have it until the ALTER TABLE just ran).
    conn.execute("CREATE INDEX IF NOT EXISTS applications_by_tenant ON applications(tenant_id, created_at)")
    conn.commit()
    _schema_ready.add(target)
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


def _report_dir(app_id: str, report_id: str):
    d = config.DATA_DIR / "applications" / app_id / "reports" / report_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_report_file(app_id: str, report_id: str, name: str, data: bytes) -> None:
    """Persist one per-report artifact (`bundle.json.gz`, `report.json`,
    `report.html`). On Postgres these live as a `bytea` row — Render's disk
    doesn't survive a restart/sleep, so nothing written to `DATA_DIR` would."""
    if IS_POSTGRES:
        with db() as conn:
            conn.execute(
                "INSERT INTO report_files (application_id, report_id, name, data, created_at) VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT (report_id, name) DO UPDATE SET data = excluded.data",
                (app_id, report_id, name, data, now().isoformat()),
            )
    else:
        (_report_dir(app_id, report_id) / name).write_bytes(data)


def load_report_file(app_id: str, report_id: str, name: str) -> bytes | None:
    if IS_POSTGRES:
        with db() as conn:
            row = conn.execute(
                "SELECT data FROM report_files WHERE report_id = ? AND name = ?", (report_id, name)
            ).fetchone()
        return bytes(row["data"]) if row else None
    path = _report_dir(app_id, report_id) / name
    return path.read_bytes() if path.exists() else None


# ----------------------------------------------------------- tenants & users


SEED_TENANT_ID = "seed-tenant"  # fixed id so _ensure_seed_tenant is idempotent across restarts
DIRECT_TENANT_ID = "direct-tenant"  # fixed id for the one built-in no-bank/platform tenant


def seed_tenant_id() -> str:
    return SEED_TENANT_ID


def direct_tenant_id() -> str:
    return DIRECT_TENANT_ID


def ensure_direct_tenant(name: str) -> dict:
    """The one built-in tenant for MSMEs with no bank (and the tenant platform
    admin users are attached to, since `users.tenant_id` is NOT NULL). Safe to
    call every startup/CLI invocation — a no-op once it exists."""
    existing = get_tenant(DIRECT_TENANT_ID)
    if existing:
        return existing
    return create_tenant(name, tenant_id=DIRECT_TENANT_ID, kind="direct")


def create_tenant(name: str, tenant_id: str | None = None, kind: str = "bank") -> dict:
    tenant_id = tenant_id or uuid.uuid4().hex
    with db() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, created_at, kind, status) VALUES (?, ?, ?, ?, 'active')",
            (tenant_id, name, now().isoformat(), kind),
        )
    return get_tenant(tenant_id)


def get_tenant(tenant_id: str) -> dict | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    return dict(row) if row else None


def list_tenants(kind: str | None = None) -> list[dict]:
    with db() as conn:
        if kind:
            rows = conn.execute("SELECT * FROM tenants WHERE kind = ? ORDER BY created_at", (kind,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM tenants ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def rename_tenant(tenant_id: str, name: str) -> None:
    with db() as conn:
        conn.execute("UPDATE tenants SET name = ? WHERE id = ?", (name, tenant_id))


def set_tenant_status(tenant_id: str, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE tenants SET status = ? WHERE id = ?", (status, tenant_id))


def create_user(tenant_id: str, username: str, password: str, role: str, application_id: str | None = None) -> dict:
    """role is 'bank' (sees every application for tenant_id), 'msme' (sees only
    application_id) or 'platform' (platform admin, sees every tenant)."""
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


def username_taken(username: str) -> bool:
    with db() as conn:
        row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    return row is not None


def list_users(tenant_id: str | None = None, role: str | None = None) -> list[dict]:
    clauses, params = [], []
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    if role:
        clauses.append("role = ?")
        params.append(role)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(f"SELECT * FROM users {where} ORDER BY created_at", params).fetchall()
    return [dict(r) for r in rows]


def count_active_platform_admins() -> int:
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role = 'platform' AND disabled = 0"
        ).fetchone()
    return row["n"]


def set_user_disabled(user_id: str, disabled: bool) -> None:
    with db() as conn:
        conn.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))


def delete_user(user_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))


def authenticate(username: str, password: str, role: str) -> dict | None:
    """Constant-shape lookup for Basic auth: always hashes, even on an unknown
    username, so a wrong username and a wrong password fail in the same time.
    A disabled user, or a user of a suspended bank tenant, never authenticates
    anywhere — even with the right password."""
    with db() as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ? AND role = ?", (username, role)).fetchone()
    user = dict(row) if row else None
    if not _verify_password(password, user["password_hash"] if user else _hash_password("")):
        return None
    if user["disabled"]:
        return None
    tenant = get_tenant(user["tenant_id"])
    if tenant and tenant["status"] == "suspended":
        return None
    return user


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


def list_all_applications(tenant_id: str | None = None, search: str = "") -> list[dict]:
    """Every application across tenants (for /admin/msmes), each with its
    tenant's name/kind joined in. `tenant_id` narrows to one bank or the
    direct tenant; `search` matches applicant name, reference or company name."""
    clauses, params = [], []
    if tenant_id:
        clauses.append("a.tenant_id = ?")
        params.append(tenant_id)
    if search:
        clauses.append("(a.applicant_name LIKE ? OR a.reference LIKE ? OR a.company_name LIKE ?)")
        like = f"%{search}%"
        params += [like, like, like]
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    with db() as conn:
        rows = conn.execute(
            f"SELECT a.*, t.name AS tenant_name, t.kind AS tenant_kind FROM applications a"
            f" JOIN tenants t ON t.id = a.tenant_id {where} ORDER BY a.created_at DESC",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def delete_application(app_id: str) -> None:
    """Deletes an application and everything that belongs only to it: its
    reports, report artifacts (disk files or `report_files` rows) and MSME
    login. Used by /admin/msmes/{id} delete, behind a typed confirmation."""
    import shutil

    with db() as conn:
        if IS_POSTGRES:
            conn.execute("DELETE FROM report_files WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM reports WHERE application_id = ?", (app_id,))
        for table in ("books", "book_vouchers", "sync_sessions"):
            conn.execute(f"DELETE FROM {table} WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM users WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM applications WHERE id = ?", (app_id,))
    if not IS_POSTGRES:
        app_dir = config.DATA_DIR / "applications" / app_id
        if app_dir.exists():
            shutil.rmtree(app_dir)


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


def start_share(app_id: str) -> str:
    """Token for a one-off share that arrives in several requests (no daily
    updates). It is cleared by end_share once the share has finished."""
    token = secrets.token_urlsafe(32)
    with db() as conn:
        conn.execute(
            "UPDATE applications SET monitoring_status = 'sharing', monitor_token_hash = ?, monitor_last_seen_at = ?"
            " WHERE id = ?",
            (_hash(token), now().isoformat(), app_id),
        )
    return token


def end_share(app_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE applications SET monitoring_status = 'off', monitor_token_hash = NULL"
            " WHERE id = ? AND monitoring_status = 'sharing'",
            (app_id,),
        )


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
    """The full report dict (metrics, indicators, flags, alerts) written by
    `report.builder.build_report`/`process_report` — not duplicated in the
    `reports` table/row (see `save_report_file`/`load_report_file`).
    Returns None if it isn't there yet (report still processing/failed)."""
    data = load_report_file(app_id, report_id, "report.json")
    return json.loads(data) if data else None


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


# -------------------------------------------------------------- audit log


def record_audit(actor: str, action: str, target_type: str, target_id: str | None, summary: str) -> None:
    """Best-effort is *not* appropriate here (unlike Linear sync elsewhere in
    this project) — every /admin write goes through this, so a failure should
    surface rather than be silently swallowed."""
    with db() as conn:
        conn.execute(
            "INSERT INTO audit_log (id, actor, action, target_type, target_id, summary, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, actor, action, target_type, target_id, summary, now().isoformat()),
        )


def list_audit(limit: int = 200) -> list[dict]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------- admin counts


def counts_overview() -> dict:
    """Plain counts for the /admin overview tiles."""
    since = (now() - timedelta(hours=24)).isoformat()
    with db() as conn:
        banks = conn.execute("SELECT COUNT(*) AS n FROM tenants WHERE kind = 'bank'").fetchone()["n"]
        msmes_linked = conn.execute(
            "SELECT COUNT(*) AS n FROM applications a JOIN tenants t ON t.id = a.tenant_id WHERE t.kind = 'bank'"
        ).fetchone()["n"]
        msmes_direct = conn.execute(
            "SELECT COUNT(*) AS n FROM applications a JOIN tenants t ON t.id = a.tenant_id WHERE t.kind = 'direct'"
        ).fetchone()["n"]
        users_by_role = {
            r["role"]: r["n"]
            for r in conn.execute("SELECT role, COUNT(*) AS n FROM users GROUP BY role").fetchall()
        }
        uploads_24h = conn.execute("SELECT COUNT(*) AS n FROM reports WHERE created_at >= ?", (since,)).fetchone()["n"]
        failed_reports = conn.execute("SELECT COUNT(*) AS n FROM reports WHERE status = 'failed'").fetchone()["n"]
        apps = conn.execute(
            "SELECT monitoring_status, force_refresh, last_report_at FROM applications WHERE monitoring_status = 'active'"
        ).fetchall()
    connectors_overdue = sum(1 for a in apps if monitoring_overdue(dict(a)))
    return {
        "banks": banks,
        "msmes_linked": msmes_linked,
        "msmes_direct": msmes_direct,
        "users_by_role": users_by_role,
        "uploads_24h": uploads_24h,
        "connectors_overdue": connectors_overdue,
        "failed_reports": failed_reports,
    }

# -------------------------------------------------------- marketing leads


def create_lead(name: str, org: str, email: str, phone: str, kind: str, user_agent: str, ip: str) -> dict:
    """A demo/pilot request from the public home page (see `main.py`'s `/api/leads`)."""
    lead_id = str(uuid.uuid4())
    with db() as conn:
        conn.execute(
            "INSERT INTO leads (id, name, org, email, phone, kind, created_at, user_agent, ip) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (lead_id, name, org, email, phone, kind, now().isoformat(), user_agent, ip),
        )
    return {"id": lead_id}


def list_leads(limit: int = 200) -> list[dict]:
    """Newest first — used by `/admin/leads` and `manage.py list-leads`."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM leads ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
