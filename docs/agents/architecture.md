# Architecture

## Shape
```
Applicant's PC (Windows)                          Bank
┌───────────────┐  XML/HTTP   ┌────────────────┐  HTTPS   ┌──────────────────────────────┐
│ TallyPrime /  │◄───────────►│ TallyConnector │─────────►│ backend (FastAPI)            │
│ Tally.ERP 9   │ localhost   │ .exe (Go)      │  gzip    │ codes · storage · analytics  │
│ port 9000     │             │ local web UI   │  JSON    │ report · dashboard           │
└───────────────┘             └────────────────┘          └──────────────────────────────┘
```
Two independently deployed pieces talking one JSON contract (the "Bundle") over HTTP:
the Go connector (runs on the applicant's PC, never installed, reads Tally only) and the
Python backend (runs on the bank's infrastructure, has no knowledge of how to talk to Tally
itself). They never share code or a process.

## Backend (`backend/app/`) — who owns what
- **`main.py`** — single FastAPI app, owns all HTTP routing. Five route groups:
  - Connector API (`/api/connector/...`): `verify` (check a one-time code), `sync/start`
    (code in `X-Link-Code` for a first share, which issues a token, or a bearer token), then
    `sync/{id}/masters|vouchers|present|finish` (incremental sync, see `books.py` below),
    `monitor/status` (includes `resume` when a sync is unfinished), `monitor/stop`. The older
    one-shot `upload` and `monitor/upload` stay for 0.1.x connectors. The
    `bank_name` returned by `verify`/`monitor/status`/`upload` is the applicant's own tenant's
    name (`store.get_tenant(a["tenant_id"])`), not a single global setting — a direct (no-bank)
    client correctly sees the platform's own name (`TC_PLATFORM_NAME`) in the connector's
    consent text instead of some other bank's.
  - Bank UI (`/bank`, `/bank/applications/{id}`, report views) — HTTP Basic auth against a
    per-tenant bank account (`bank_user` dependency, backed by `store.authenticate`),
    server-rendered Jinja2. Every query is scoped to `user["tenant_id"]`, so one bank can
    never see another bank's applications (`_get_or_404`/`_report_or_404` check this).
  - MSME UI (`/msme`, `/msme/report[.json]`) — HTTP Basic auth against an MSME account
    (`msme_user` dependency), scoped to exactly the one `application_id` the account was
    issued for (a bank or platform admin creates/resets this login from an application's
    page). A trimmed, read-only view of that one company's own analysis (`msme.html`) — no
    monitoring controls, no other applicants' data.
  - Bank JSON API (`/api/bank/applications...`) — same bank Basic auth, for LOS integration.
  - Platform admin UI (`/admin/...`) — HTTP Basic auth against a `platform`-role account
    (`platform_user` dependency), scoped to nothing (sees every tenant). See "Platform admin
    panel" below.
  - Public marketing site (`/`, `robots.txt`, `sitemap.xml`, `llms.txt`, `/favicon.ico`,
    `/static/...`, `POST /api/leads`) — no auth, server-rendered from `templates/home.html` and
    `app/marketing.py` (static content/helpers; see `design.md`'s "Marketing home page"). A
    blanket middleware (`_noindex_private_routes`) tags every `/bank`, `/msme`, `/admin` and
    `/api` response `X-Robots-Tag: noindex, nofollow`, and a second branch sets a long
    `Cache-Control` on `/static/...`. `GZipMiddleware` and `StaticFiles` are both mounted here too.
  - Also owns upload-size/gzip-bomb guarding (`_read_bundle`, `_gunzip_json`), simple
    in-memory per-IP throttles (`_failed_attempts`, 20/hour, code/token checks; `_lead_attempts`,
    5/hour, `/api/leads`), and the CSRF guard (`_csrf_guard`) applied to every state-changing
    `/bank`/`/admin` form POST — see "CSRF" below.
- **`store.py`** — owns all persistence: SQLite (stdlib `sqlite3`, one file at
  `config.DATA_DIR/tally_connector.db`) by default, or Postgres (`psycopg[binary]`) when
  `DATABASE_URL` is set — see "Storage backend" below. No ORM; raw SQL, dict-like rows on
  both engines. Schema is created and migrated idempotently on every connection (`SCHEMA` +
  `MIGRATIONS` dict — new columns are added via `ALTER TABLE` if missing, never a separate
  migration runner/tool). Owns code generation/expiry, monitoring-token hashing (SHA-256,
  token itself is returned once and never stored), the due/overdue date math, password
  hashing (salted PBKDF2-SHA256, stdlib only, never stored or logged in the clear), and
  multi-tenancy/roles — see "Roles, tenants and the direct tenant" below. Every `applications`
  row carries a `tenant_id`. Also owns the `audit_log` table (see "Audit log" below).
- **`manage.py`** — scripted tenant/user provisioning, for first-time setup or automation
  (everyday management is `/admin` — see below): `python -m app.manage create-tenant
  "Bank name"` / `create-bank-user <tenant_id> <username>` / `create-platform-admin
  <username>`. `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` seed the very first bank tenant + bank
  user; `TC_PLATFORM_ADMIN_USER`/`TC_PLATFORM_ADMIN_PASSWORD` (optional) seed the first
  platform admin — both only on first startup of an empty database
  (`main._ensure_seed_tenant`/`main._ensure_seed_platform_admin`).
- **`analysis/`** — owns turning a raw Bundle into numbers and judgments. No I/O.
  - `book.py` — classifies ledgers into semantic categories by walking the Tally group tree
    (so custom voucher types/groups need no special-casing).
  - `metrics.py` (largest file, ~750 lines) — computes everything in the README's "What the
    report computes" section: revenue, customer concentration/HHI, receivables/payables
    ageing (bill-wise where available, FIFO fallback), working capital (DSO/DPO/DIO/CCC),
    balance sheet ratios, banking/cash behaviour. Also (added for ticket 7485bcfc, direction 6's
    dashboard groups): same-month-last-year sales, monthly average/range (extends
    `seasonality`), credit notes/returns ratio, purchases growth, NWC (`balance_sheet`'s
    `current_assets - current_liabilities`), a reconstructed month-end bank balance series
    with average/low(+month)/volatility (`bank_balance_series`), a monthly GST-collected
    series with a sales-consistency check (`gst_monthly`), a GST-vs-TDS split of the tax
    payable by ledger-name heuristic (`statutory_split`), per-loan EMI repayment regularity
    from loan-ledger voucher dates (`emi_regularity`), and a low-confidence related-party
    heuristic (`related_party_candidates` — name-overlap with the company's own name only;
    Tally's bundle has no address/director field, so this cannot be more than a name match).
  - `redflags.py` — stale books, back-dated entries, year-end sales spikes reversed in
    April, round-figure invoices, dual customer/supplier parties, cash-handling thresholds
    (s.269ST/s.40A(3)), suspense balances, negative net worth.
  - `indicators.py` — **the bank's editable policy**: green/amber/red thresholds per metric.
    Explicitly commented as "edit freely" — this is the one file meant to be tuned by a bank,
    not a bug surface. Each rule carries `lo`/`hi`/a derived `bands` list alongside the
    green/amber cutoffs (same numbers, no threshold changed) so `report/dashboard.py`'s gauge
    widgets and the red/amber/green status can never disagree about where a threshold sits.
  - `alerts.py` — compares two reports' indicators/snapshots to flag deterioration between
    monitoring cycles.
- **`report/`** — owns presentation of the computed numbers. `builder.py` assembles the
  final report dict + calls analysis; `charts.py` renders inline SVGs (no JS chart library);
  `glossary.py` is the single source of every credit term's name/plain-English line/formula;
  `format.py` has small formatters (days/pct/ratio); `dashboard.py` (added for ticket 7485bcfc,
  direction 6) is the view-model layer between a built report and the three dashboards —
  `company_view` (bank, one borrower, 10 tabbed groups: overview/sales/profitability/
  receivables/payables/working_capital/debt/banking/tax/data), `msme_view` (home/sales/
  customers/money/dues, plain language, no ratings or thresholds), and `portfolio_view`
  (bank portfolio: KPI counts, alerts-since-yesterday feed, data-overdue list, sales monitored
  with YoY, health by state, one row per borrower). Pure functions only — no I/O, no template
  rendering; callers pass in an already-built `report` dict (and, for the portfolio, a list of
  `{application, report, overdue, new_alerts}` per borrower, built from `store.applications_with_latest_report`/
  `store.load_report_json`). `main.py` wires these into `dashboard.html`/`application.html`/
  `msme.html` (ticket 7485bcfc phase 2); chart/widget rendering itself (SVG columns, sparklines,
  bullet gauges, stacked bars, ageing ramps, an EMI grid) lives in `report/dashboard_charts.py`,
  registered as Jinja globals in `main.py`, separate from `report/charts.py` (which still renders
  only `report.html`'s own charts).
  **Borrower health rule** (`dashboard.borrower_status`, one function, used everywhere a
  borrower's traffic-light status is needed): `attention` if any indicator is red, or a
  high-severity red flag is present, or the borrower's data is overdue; `watch` if 3 or more
  indicators are amber, or there's a new alert since the last refresh; else `healthy`
  (`nodata` is a separate state in `portfolio_view` for applications with no ready report yet).
- **`templates/`** — Jinja2 HTML, inline `<style>` per page, no shared CSS file or JS
  framework. See `design.md`.
- **`marketing.py`** — static content and small pure functions (no I/O) for the public home
  page: chart series, the "for banks" tab panels, the cash-cycle calculator's thresholds, the
  FAQ (also the single source for the FAQPage JSON-LD), title/description, and
  `json_ld()`. `main.py`'s `home()` route is the only caller.

## Connector (`connector/`) — who owns what
- **`main.go`** — CLI entry point and mode dispatch: interactive (opens a browser to a local
  127.0.0.1 UI), headless (`-code`/`-company`/`-consent`, or `-dump` to extract without
  uploading), `-monitor-run` (what the scheduled task invokes), `-monitor-stop`.
- **`internal/tally/`** — owns all Tally XML/HTTP protocol knowledge: UTF-16 request
  encoding (with a `-utf8` escape hatch), illegal-character sanitising, and the different
  response shapes Tally can return. Has its own tests (`tally_test.go`).
- **`internal/extract/`** — owns building the `Bundle` (see Data model below) from Tally
  responses: groups, ledgers, voucher types, stock snapshots, bills, and Day Book vouchers
  pulled month by month. Tally answers XML on the same thread that draws its screen, so a
  big request freezes Tally for the user. `pace.go` therefore gives every request a time
  limit and a pause afterwards (half the last request's duration, 0.3-5 s). After a timeout
  it waits for Tally to answer a ping before sending anything else, so requests never queue
  inside Tally. While the keyboard/mouse was used in the last minute (`internal/activity`,
  Windows `GetLastInputInfo`), each pause equals the last request's duration and batches aim
  at 3 s; when idle, pauses are a quarter and batches aim at 10 s. Ledger masters (with
  `MasterId`) are read without balances. Balances, the expensive computed part, are read in
  batches by `batch.go`. A cheap probe that asks only for stored fields picks the method:
  `MasterId` range filters (even batches that halve on timeout and grow when fast), else
  `CHILDOF` per group, else one whole request. Stock values work the same way. Nothing is
  escalated to a bigger request after a timeout. Stock and bills are best effort (warning,
  not failure). A slow voucher month is split in half at once. Tests: `extract_test.go`, and
  `run_test.go`, whose fake single-threaded Tally covers each filter mode.
- **`internal/app/`** — the local UI: serves `index.html` on 127.0.0.1 with a per-run token,
  owns the guided 4-step flow (detect Tally → enter code → choose company/consent →
  extract/upload) and idle-timeout (exits ~45s after the tab closes).
- **`internal/monitor/`** — owns the daily-monitoring lifecycle: `monitor.go` (due-check
    and upload loop invoked by `-monitor-run`/`-monitor-stop`), platform-specific scheduled-task
    registration split into `schedule_windows.go` (real Windows Task Scheduler XML, no admin
    rights needed) and `schedule_other.go` (non-Windows no-op — used for local dev builds on
    mac/Linux, and for Linux cloud installs, which instead run `-monitor-run` from cron or a
    systemd timer; see README.md "Linux / cloud installs"). Settings persist under
    `%AppData%\TallyConnector\` (`TC_HOME` env var overrides it, used by `dev/e2e.sh`, tests,
    and non-Windows hosts).
- **`internal/upload/`** — thin HTTP client to the bank backend's `/api/connector/...`
  endpoints; owns gzip-compressing the bundle before sending.

## Data flow
1. Bank creates an application (`POST /bank/applications` form, or `/api/bank/applications`
   JSON) → `store.create_application` generates a one-time 8-char code (`CODE_ALPHABET`
   excludes 0/O/1/I/L — meant to be read aloud), valid `TC_CODE_TTL_HOURS` (default 72).
2. Applicant runs `TallyConnector.exe`. It calls Tally's local XML/HTTP port (default 9000)
   to detect companies, calls the bank's `/api/connector/verify` with the code to confirm
   who's asking, then `internal/extract` pulls the books for the requested period into a
   `Bundle`.
3. Connector gzips the Bundle and `POST`s it to `/api/connector/upload` with the code in
   `X-Link-Code`. Backend validates size/gzip-bomb limits, stores the raw bytes
   (`report_dir/bundle.json.gz`), marks the code used (single-use — cleared immediately),
   and kicks off `process_report` as a FastAPI `BackgroundTask`.
4. `process_report` decompresses the bundle, runs `analysis` → `report.builder.build_report`,
   diffs against the previous ready report via `analysis.alerts`, writes `report.json` +
   `report.html` to disk, and updates the `reports`/`applications` rows (`store.finish_report`
   on success, `store.fail_report` on exception — the upload itself is always kept even if
   report generation fails, so the bank can recompute later via `POST
   /bank/reports/{id}/recompute`).
   **Since connector 0.2 the upload is incremental** (`books.py`; tables `books`,
   `book_vouchers`, `sync_sessions`). `sync/start` consumes the code and returns a token:
   monitoring if opted in, otherwise a one-off `sharing` token cleared by `store.end_share`
   once the share finishes. It also returns the plan: `full` (first share, every
   `TC_FULL_SYNC_DAYS`, a longer window, AlterIds going backwards, or a flagged deletion
   anomaly) or `delta`. A full sync sends masters with balances, then vouchers one month at a
   time. An open full session is always resumed, skipping `months_done`. A delta sends masters
   without balances, the Day Book for days with changed AlterIds plus the last few days, and
   recent voucher-ID lists for deletions (`present` refuses to delete more than a third of a
   range and flags a full re-read instead). It then sends balances only for affected ledgers;
   every voucher-changing request returns the ledgers it touched, old and new versions.
   `finish` (background task `main.finish_sync`) carries untouched balances forward, rolls
   opening balances over days that left the window, prunes, and writes a complete
   `bundle.json.gz`, which then goes through `process_report` exactly as in step 4.
5. If the applicant opted into monitoring, the upload response includes a monitoring token
   (returned once, stored only as a SHA-256 hash). The connector then self-copies to
   `%AppData%\TallyConnector\`, saves settings, and registers a Windows scheduled task that
   periodically asks `/api/connector/monitor/status` whether a refresh is due
   (`store.monitoring_due`, policy: due once a calendar day has passed since the last report),
   and if so runs a delta sync (0.2+; 0.1.x re-uploaded the whole window via
   `/api/connector/monitor/upload`). The connector keeps one entry per shared company in
   `companies.json` (`internal/monitor`). One scheduled task serves every daily entry, and a
   per-company OS file lock (`lock_windows.go`/`lock_other.go`; released when the process
   dies) stops the UI and the task syncing the same company at once.
   Server-side safety: a `finishing` full session left by a server restart is reopened, and the
   connector only finishes it. An open full session older than 7 days is restarted. A delta whose
   masters dropped a ledger or voucher-type name that stored vouchers still use sets
   `needs_full` and `force_refresh` (Tally renames inside vouchers without new AlterIds). One-shot
   uploads from 0.1.x call `books.forget`, so the next 0.2 sync is full. Retention
   (`store.prune_report_files`, `books.prune_sessions`, `TC_KEEP_RAW_DAYS`) runs after every
   report.
6. Bank views results via the dashboard (`/bank`) or a specific application's history
   (`/bank/applications/{id}`, including an indicator trend table and alerts).

## Data model
**Bundle** (the one JSON contract between connector and backend — `connector/internal/extract/bundle.go`,
schema versioned via `SchemaVersion` const, currently `1`):
`schema_version`, `connector_version`, `extracted_at`, `consent` (accepted_at/by, text,
monitoring_opt_in), `machine` (hostname, OS, tally_url, tally_banner), `company`, `period`
(from/to), `groups`, `ledgers` (with opening/closing balance, bill-wise flag, credit period,
GSTIN/state/country), `voucher_types`, `stock_snapshots`, `bills`, `vouchers` (each with
`entries`, each entry optionally with `bills` references), `warnings`. **Amount convention:
every amount is debit-positive** (Tally's XML is the opposite sign; the connector flips it —
see the doc comment at the top of `bundle.go`). The backend only ever receives this shape; it
never talks to Tally directly.

**Schema** (`backend/app/store.py`; SQLite at `backend/data/tally_connector.db` by default, or
Postgres when `DATABASE_URL` is set — see "Storage backend" below):
- `tenants` — one row per bank, plus one built-in row for the direct (no-bank) tenant: `id`,
  `name`, `created_at`, `kind` (`bank` | `direct`, added via `MIGRATIONS`, default `bank`),
  `status` (`active` | `suspended`, added via `MIGRATIONS`, default `active` — only meaningful
  for `kind = bank`; the direct tenant has no suspend concept of its own).
- `users` — one row per login: `tenant_id`, `username` (unique), `password_hash` (salted
  PBKDF2-SHA256), `role` (`bank` | `msme` | `platform`), `application_id` (set only for `msme`
  — the one application that login can see), `disabled` (added via `MIGRATIONS`, default 0 —
  a disabled user, or any user of a suspended bank tenant, can't log in anywhere;
  `store.authenticate` checks both). A `platform` user's `tenant_id` is the direct tenant's id
  (the only tenant a platform admin login is attached to, since the column is `NOT NULL`) —
  this does *not* make them a bank user of that tenant; they're never returned by a
  bank-scoped user query (`role = 'bank'`).
- `applications` — one row per borrower/loan request: `tenant_id` (which bank, or the direct
  tenant, owns it), identity, one-time code + expiry, `status` (awaiting_data | processing |
  ready | failed), monitoring fields added via `MIGRATIONS` (offered flag, status, hashed
  token, started/last-seen timestamps, a manual `force_refresh` flag, pointer to the latest
  report).
- `reports` — one row per upload (initial or refresh): status (processing | ready | failed),
  computed `indicators_json`/`snapshot_json`/`alerts_json`, `high_flags` count. Report
  artifacts themselves (the gzip bundle, `report.json`, `report.html`) are not in this table —
  see "Storage backend" below for where they live. `store.latest_ready_report`/
  `store.load_report_json`/`store.applications_with_latest_report` read the full report dict
  (metrics, indicators, flags, alerts) back for `report/dashboard.py`'s view-models — the DB
  columns alone aren't enough for those, they only carry the indicator/snapshot summary.
- `report_files` — only populated when `DATABASE_URL` is set: `application_id`, `report_id`,
  `name` (`bundle.json.gz` | `report.json` | `report.html`), `data` (bytea), `created_at`,
  primary key `(report_id, name)`. Table always exists (it's in `SCHEMA`, both engines) but is
  simply unused under SQLite, where these files go to disk instead.
- `audit_log` — one row per `/admin` write: `actor` (the platform admin's username), `action`
  (a short dotted string, e.g. `bank.suspend`, `msme.delete`, `user.disable`), `target_type`,
  `target_id`, `summary` (a human-readable line), `created_at`. Written by
  `store.record_audit`, read by `store.list_audit` (newest first) for `/admin` and `/admin/audit`.
  Unlike the Linear sync elsewhere in this project's sibling tooling, this is *not*
  best-effort — a failed audit write is allowed to fail the request, since every `/admin`
  action is expected to be attributable.
- `leads` — one row per demo/pilot request from the public home page's form (`POST
  /api/leads`): name, org, email, phone, kind, `created_at`, `user_agent`, `ip`. Unrelated to
  tenants/applications — a marketing lead, not a borrower. `store.create_lead`/`list_leads`;
  Read on `/admin/leads` or with `python -m app.manage list-leads`.

No other datastore, queue, or cache exists in this repo.

## Roles, tenants and the direct tenant
Three user roles now exist: `bank` (scoped to one tenant's applications, `/bank`), `msme`
(scoped to one application, `/msme`), and `platform` (scoped to nothing — sees every tenant,
`/admin`). A tenant has a `kind`: `bank` (a real bank customer, with its own `status` of
`active`/`suspended`) or `direct` — exactly one row, fixed id `store.direct_tenant_id()`,
created lazily by `store.ensure_direct_tenant`/`main._ensure_seed_platform_admin` the first
time it's needed, named by `TC_PLATFORM_NAME` (default "TallyVeda"). The direct tenant
holds two kinds of thing: MSME applications with no bank at all (created from `/admin/msmes`
with no bank chosen), and every `platform`-role user (since `users.tenant_id` is `NOT NULL` and
a platform admin isn't a bank user of anywhere). Direct-tenant applications are visible only to
platform admins (`/admin/msmes/...`) and to that application's own MSME login (`/msme`) — never
to any bank's `/bank` dashboard, which only ever queries its own `tenant_id`.

## Platform admin panel
`/admin/*` (HTTP Basic auth, role `platform`, `platform_user` dependency in `main.py`) is the
one place to manage the whole platform:
- `/admin` — overview: counts (banks, MSMEs linked/direct, users by role, uploads in the last
  24h, connectors overdue, failed reports) from `store.counts_overview`, plus the 20 most
  recent audit log entries.
- `/admin/banks[/{id}]` — list/create banks; per-bank detail (rename, suspend/reactivate, its
  bank users with create/reset-password/disable/enable/delete, its MSMEs with a create form).
- `/admin/msmes[/{id}]` — every MSME (bank-linked or direct) with filters (bank, direct/bank
  kind, status, overdue) and search (`store.list_all_applications`); create (under a chosen
  bank or direct); per-MSME detail (one-time code, monitoring refresh/stop, MSME login
  issue/reset, report history, a read-only `/admin/msmes/{id}/view` that reuses
  `application.html`'s company tabs exactly as the bank sees them — `_render_application(...,
  readonly=True)` hides the Manage panel and points its back-link at `/admin/msmes/{id}`
  instead of `/bank`), and delete (`store.delete_application`, behind typing the applicant's
  name back as confirmation — removes the application, its reports/report artifacts and its
  MSME login).
- `/admin/users` — every user across every tenant, filterable by role/bank; reset
  password/disable/enable/delete. `store.count_active_platform_admins` guards against
  disabling or deleting the last active `platform` user (checked at write time, not just in
  the UI) — the error is a plain 400, not a silent no-op.
- `/admin/audit` — the full audit log.
Every write above calls `store.record_audit` (see "Audit log" above) before responding.
Generated passwords (bank users, MSME logins, password resets) are shown exactly once via
`admin_credential.html`, the same one-time-reveal pattern `msme_login.html` already used for
bank-issued MSME logins — never stored or logged in the clear (`store.py` hashes them
immediately, same as every other password in this app).

## CSRF
Every state-changing form POST under `/admin` and `/bank` carries the `_csrf_guard`
dependency (`main.py`). Rule: if the request has an `Origin` header (or, failing that,
`Referer`), its host must match the request's own `Host`, or the request is rejected with 403.
If *neither* header is present, the request is allowed through. Rationale: both `/bank` and
`/admin` are HTTP Basic auth, which browsers resend automatically on every request to the same
origin (there's no session cookie to scope a CSRF token to) — so a page on another site could
still get a victim's browser to submit a state-changing form here. Every current browser sets
`Origin` on a cross-origin POST/fetch/form submission (and on most same-origin ones too), so
the only requests with neither header are non-browser clients (curl, a script, a LOS
integration, `TestClient` in this test suite) which were never exposed to the CSRF scenario in
the first place — rejecting them by default would break legitimate API usage for no real
security gain. Connector endpoints (`/api/connector/...`, authenticated by a one-time code or
bearer token) and the JSON `/api/bank/...` integration routes never carry this dependency —
they're not browser form submissions, and a non-browser caller may legitimately send neither
header.

## Storage backend
`store.IS_POSTGRES` (`DATABASE_URL` starts with `postgres://`/`postgresql://`) picks the
engine; every function in `store.py` runs the same SQL either way except two spots:
- `_connect()` opens a `sqlite3.Connection` or wraps a `psycopg.Connection` in `_PGConnection`,
  a small shim that translates `?` placeholders to `%s` and gives both engines dict-like rows
  (`sqlite3.Row` vs. psycopg's `dict_row`), so every other query in the file is backend-agnostic.
  `psycopg.connect(..., prepare_threshold=None)` disables server-side prepared statements —
  Neon's pooled endpoint is PgBouncer in transaction mode, which can hand a later statement on
  the same logical connection to a different backend process, so nothing here relies on
  session state (`SET`, temp tables, advisory locks, prepared statements) surviving between
  statements. Each `db()` call opens and closes its own connection (no pool) — simplest way to
  reconnect cleanly after Neon's free tier autosuspends on idle, and already how the SQLite
  path has always worked.
- `save_report_file`/`load_report_file` write/read one per-report artifact: a disk file under
  `DATA_DIR/applications/<app_id>/reports/<report_id>/<name>` for SQLite, a `report_files` row
  for Postgres. `process_report`/`main.py`'s upload and download routes call these instead of
  touching paths directly, so neither knows which backend is active.

`SCHEMA`/`MIGRATIONS` themselves are plain SQL (`CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ...
ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`) that Postgres accepts unchanged; the only
per-engine piece there is `_existing_columns` (`PRAGMA table_info` vs. a
`information_schema.columns` query) to decide which `ALTER TABLE`s are still needed.

## External services / SDKs
Postgres (optional, via `psycopg[binary]`, when `DATABASE_URL` is set — see above; tested
against a local Postgres cluster and against Neon's pooled endpoint). Otherwise none: no cloud
SDK, no third-party API, no payment/messaging integration. The only network calls are:
connector ↔ Tally's local XML/HTTP port, and connector ↔ bank backend over plain HTTP(S) (TLS
termination in front of the backend — Render's own TLS when hosted there, otherwise the
bank's responsibility). PyPI/Go-stdlib dependencies only (`fastapi`, `uvicorn`, `jinja2`,
`python-multipart`, `psycopg[binary]`, `pytest`, `httpx` for the backend; zero third-party Go
modules).

## Config (names only — see `backend/app/config.py` and `README.md` "Backend configuration")
`TC_BANK_NAME`, `TC_ADMIN_USER`, `TC_ADMIN_PASSWORD`, `TC_PLATFORM_ADMIN_USER`,
`TC_PLATFORM_ADMIN_PASSWORD`, `TC_PLATFORM_NAME`, `TC_DATA_DIR`, `DATABASE_URL`,
`TC_CODE_TTL_HOURS`, `TC_DEFAULT_MONTHS`, `TC_MAX_UPLOAD_MB`, `TC_CONNECTOR_EXE`,
`TC_CONNECTOR_URL`, `TC_MONITOR_OVERDUE_DAYS`, `TC_PUBLIC_URL` (canonical origin for the
marketing home page's SEO tags/sitemap/robots.txt — defaults to the live Render URL).
Connector-side env/flags (not server config, but worth knowing):
`TC_TALLY_URL`/`-tally`, `TC_SERVER`/`-server`, `TC_HOME` (test/dev override for where
monitoring settings are persisted, default `%AppData%\TallyConnector`). None are secrets files or `.env`-loaded — they're plain OS environment variables. All have
defaults except `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD`: `config.py` raises at import if either
is unset, unless `TC_DEV=1` (which falls back to `admin`/`admin` for local runs and tests).
These two only *seed* the first tenant/bank user on an empty database (`main._ensure_seed_tenant`)
— real accounts live in `store.py`'s `users` table from then on, provisioned via `manage.py` or
`/admin`. `TC_PLATFORM_ADMIN_USER`/`TC_PLATFORM_ADMIN_PASSWORD` are the same idea for the first
`/admin` login (`main._ensure_seed_platform_admin`), but genuinely optional — leaving them unset
just means `/admin` has no working login until one is created; `TC_DEV=1` falls back to
`platform`/`platform`. `TC_PLATFORM_NAME` names the one built-in direct tenant (see "Roles,
tenants and the direct tenant" above).

## Entry points
- Backend: `uvicorn app.main:app` (from `backend/`), app object is `app.main:app`.
- Connector: `go run .` / the built `TallyConnector.exe` (from `connector/`), entry
  function `main()` in `connector/main.go`; build via `connector/build.sh`.
- Dev: `dev/mock_tally.py` (standalone fake Tally server), `dev/e2e.sh` (orchestrates all
  three for a full local round trip — see `project.md` for its current verification status).

## Sync cadence, growing history and updates (2026-10-05, connector 0.5.0)
- **Schedule:** the Windows task runs at sign-in and every 15 minutes all day. A run first pings Tally,
  then asks `/api/connector/monitor/status`, sending a heartbeat (`{"tally": "ok"|"down", "error",
  "version"}`, 0.5.1+) even when Tally is closed. `store.record_heartbeat` keeps `tally_ok_at` and
  `tally_down_since`, and `main._tally_state` turns them into the data feed's "Tally" row and the green Live / red Asleep dot
  (`_shell.live_dot`, also on the portfolio and the admin MSME list): answering,
  not answering since X (computer on, Tally closed or port 9000 off), or no contact for 40+ minutes
  (computer off, asleep or signed out). A refresh is due on the
  first check-in of each Indian calendar day (`store.monitoring_due`), or while history is still short.
- **Growing history (both directions):** a first share reads only `books.FIRST_WINDOW_MONTHS` (3) so the
  bank gets a report within minutes. Each later sync is a delta (forward: what changed) plus a `backfill`
  span of up to `books.BACKFILL_MONTHS` (6) older months (backward), and the connector loops
  (`monitor.Continue`) while `/sync/{id}/finish` answers `more: true`, until the book reaches the bank's
  `target_from`. Opening balances roll back over the added months (`_roll_openings`). Reports whose window
  start moved earlier than the previous report's raise no alerts (`snapshot["window_from"]`). A one-off
  share keeps its token until the history is complete.
- **Updates:** `connector/release.sh` publishes `static/downloads/latest.json` (version, SHA-256, size,
  `auto`). `/api/connector/latest` serves it and `/download/connector.exe` the exe from the zip. Background
  runs install a newer release over the scheduled copy when `auto` is true (`monitor.AutoUpdate`); the
  page offers "Update now", which replaces the running exe and the scheduled copy and restarts. The
  installed copy is never downgraded by opening an older download (`installed-version`). Unsigned for
  now: a code-signing certificate is planned.


## Demo data seeder (test PCs only)
`connector/cmd/demoseed` builds `TallyVedaDemoSeed.exe` (`cmd/demoseed/build.sh`), a separate program
that imports `dev/demo_seed_data.py`'s synthetic books (the mock_tally company, 2024-04-01 to the build
date) into an open, empty company named "TallyVeda Demo" (any name starting with it also works). Tally can't create a company
over XML, so the user creates it first. It refuses a company holding vouchers it didn't write (fixed
uuid5 GUIDs), and resumes an interrupted run. The connector itself still never writes to Tally; never
ship the seeder in the connector zip.

## When Tally hangs (connector 0.5.2, 0.5.3)
A request that runs past its limit waits up to `idleWait` (20 min) for Tally to answer again. If it never
does (often a message box on Tally's screen), `pacer.do` returns `errStuck` and batch readers return it at
once instead of splitting and waiting again. Stock values are optional: once they make Tally slow or hang,
`readStock` stops and remembers `StockItem:ClosingValue` in tally-skip.json, so later syncs skip them
(seen on TallyPrime 1.1.7, 2026-10-05).
From 0.5.4 the connector no longer asks Tally for outstanding bills at all: `book.bills_from_vouchers`
matches the vouchers' New Ref / Agst Ref bill allocations on the server, using a ledger's bills only when
they add up to its closing balance (bills raised before the stored window can't be seen); other ledgers
are aged FIFO. Settlements of bills raised before the stored window are ignored; if older bills are still
open, the closing-balance check fails and that ledger goes FIFO.

From 0.5.5 Tally values stock only at the period end. Each voucher carries `inventory` lines
(`{item, qty, value}`, positive = in; always present, so older vouchers without the key are detectable),
and `Book.stock_value` works out an earlier date from the next per-item snapshot: quantity then = snapshot
quantity minus what moved since, valued at the item's average purchase rate over the year to that date
(else the snapshot's rate). It refuses (returns None, so margins show "approximate") past a voucher with no
stock lines or a Physical Stock voucher. Invoice lines carrying a delivery/receipt note's tracking number
are skipped by the connector (the note moved the stock). Existing books gain stock lines at their next
30-day full re-read. Tally still computes ledger closing balances (light) and today's stock value.
Only one sync talks to a Tally at a time (`monitor.LockTally`, an OS file lock per Tally URL): the page
waits for it, a scheduled run skips and retries next time. Two syncs at once (the page sharing one
company while the scheduled task updated another, 2026-10-05) left each waiting until Tally looked hung.
