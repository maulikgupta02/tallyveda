import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("TC_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
# When set (postgres://... or postgresql://...), store.py stores everything in Postgres
# instead of SQLite + DATA_DIR — the only durable option on a Render free web service,
# whose disk is ephemeral. Unset (the default) keeps today's SQLite/DATA_DIR behaviour
# exactly, for local dev, tests and dev/e2e.sh. See docs/agents/architecture.md.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
BANK_NAME = os.environ.get("TC_BANK_NAME", "Demo Bank")
# Bank/MSME logins are now real per-tenant accounts in store.py's `users` table, not a
# single shared Basic-auth password. TC_ADMIN_USER/TC_ADMIN_PASSWORD only *seed* the
# first bank tenant and its first bank user, on first startup of an empty database
# (see main.py's _ensure_seed_tenant) — once any tenant exists they're ignored. This
# keeps the old safety property: a deploy that forgets to set them fails at startup
# instead of serving every applicant's books behind admin/admin.
DEV = os.environ.get("TC_DEV") == "1"
ADMIN_USER = os.environ.get("TC_ADMIN_USER", "admin" if DEV else "")
ADMIN_PASSWORD = os.environ.get("TC_ADMIN_PASSWORD", "admin" if DEV else "")
if not (ADMIN_USER and ADMIN_PASSWORD):
    raise RuntimeError("Set TC_ADMIN_USER and TC_ADMIN_PASSWORD, or TC_DEV=1 for a local admin/admin login")
# Platform admin (the operator's own /admin login) — same seed-once pattern as
# ADMIN_USER/ADMIN_PASSWORD above, but for the `platform` role instead of the
# first bank tenant. Also only required outside TC_DEV.
PLATFORM_ADMIN_USER = os.environ.get("TC_PLATFORM_ADMIN_USER", "platform" if DEV else "")
PLATFORM_ADMIN_PASSWORD = os.environ.get("TC_PLATFORM_ADMIN_PASSWORD", "platform" if DEV else "")
# Name of the operator's own built-in "direct" tenant (MSMEs with no bank),
# shown as the counterparty name in the connector's consent text for them.
PLATFORM_NAME = os.environ.get("TC_PLATFORM_NAME", "TallyVeda")
CODE_TTL_HOURS = int(os.environ.get("TC_CODE_TTL_HOURS", "72"))
MAX_UPLOAD_MB = int(os.environ.get("TC_MAX_UPLOAD_MB", "300"))
DEFAULT_MONTHS = int(os.environ.get("TC_DEFAULT_MONTHS", "24"))
# Daily monitoring: a refresh is due once a day has passed since the last
# report, and flagged overdue after N days with no data at all.
MONITOR_OVERDUE_DAYS = int(os.environ.get("TC_MONITOR_OVERDUE_DAYS", "3"))
# Incremental syncs send only changes; every N days the connector re-reads the
# whole window instead, as a safety net for anything change tracking can miss.
FULL_SYNC_DAYS = int(os.environ.get("TC_FULL_SYNC_DAYS", "30"))
# Retention: every report's figures (report.json) are kept for good, since the
# trend charts and alerts read them. The raw upload (bundle.json.gz, the big
# file) and the rendered page are kept for this many days, plus the first
# report and the last one of each month. Older pages are re-rendered on demand.
KEEP_RAW_DAYS = int(os.environ.get("TC_KEEP_RAW_DAYS", "30"))
# Once a company is no longer sharing (daily updates off or stopped) and nothing
# has arrived for this many days, its raw books are deleted; report figures stay
# as the record of the assessment until a deletion request is carried out.
RETENTION_DAYS = int(os.environ.get("TC_RETENTION_DAYS", "180"))

# Consent: each request carries the MSME's email, and the connector only accepts
# consent after the one-time password sent there is entered (RBI digital lending
# rules expect explicit, auditable consent). Needs the TC_SMTP_* settings; with
# TC_REQUIRE_OTP=0 the step is skipped entirely. Off by default in TC_DEV.
REQUIRE_OTP = os.environ.get("TC_REQUIRE_OTP", "0" if DEV else "1") == "1"
OTP_TTL_MINUTES = 10
SMTP_HOST = os.environ.get("TC_SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("TC_SMTP_PORT", "465"))
SMTP_USER = os.environ.get("TC_SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("TC_SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("TC_SMTP_FROM", "") or SMTP_USER

# Privacy notice and terms (/privacy, /terms).
OPERATOR_NAME = os.environ.get("TC_OPERATOR_NAME", PLATFORM_NAME)
OPERATOR_ADDRESS = os.environ.get("TC_OPERATOR_ADDRESS", "")
GRIEVANCE_EMAIL = os.environ.get("TC_GRIEVANCE_EMAIL", "")
DATA_LOCATION = os.environ.get("TC_DATA_LOCATION", "Singapore")
# Optional: path to the built TallyConnector.exe, served at /download. Useful for a
# local/VM deploy with the exe on disk, but a Render free web service's filesystem is
# ephemeral and has no build step for it — set TC_CONNECTOR_URL instead (e.g. a GitHub
# Releases asset) and /download redirects there. TC_CONNECTOR_URL takes priority.
CONNECTOR_EXE = os.environ.get("TC_CONNECTOR_EXE", "")
CONNECTOR_URL = os.environ.get("TC_CONNECTOR_URL", "")
# Canonical origin of the public marketing home page (SEO tags, sitemap, robots.txt,
# JSON-LD). No trailing slash. Defaults to the live Render URL — change it once a real
# domain is bought (see project.md).
PUBLIC_URL = os.environ.get("TC_PUBLIC_URL", "https://tally-connector-1lir.onrender.com").rstrip("/")
