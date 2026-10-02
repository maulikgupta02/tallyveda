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
- **`main.py`** — single FastAPI app, owns all HTTP routing. Three route groups:
  - Connector API (`/api/connector/...`): `verify` (check a one-time code), `upload` (first
    upload, authenticated by the one-time code in `X-Link-Code`), `monitor/status`,
    `monitor/upload`, `monitor/stop` (all authenticated by a bearer monitoring token).
  - Bank UI (`/bank`, `/bank/applications/{id}`, report views) — HTTP Basic auth
    (`bank_user` dependency), server-rendered Jinja2.
  - Bank JSON API (`/api/bank/applications...`) — same Basic auth, for LOS integration.
  - Also owns upload-size/gzip-bomb guarding (`_read_bundle`, `_gunzip_json`) and a simple
    in-memory per-IP throttle (`_failed_attempts`, 20/hour) on code/token checks.
- **`store.py`** — owns all persistence (SQLite, stdlib `sqlite3`, one file at
  `config.DATA_DIR/tally_connector.db`). No ORM; raw SQL with `conn.row_factory = sqlite3.Row`.
  Schema is created and migrated idempotently on every connection (`SCHEMA` + `MIGRATIONS`
  dict — new columns are added via `ALTER TABLE` if missing, never a separate migration
  runner/tool). Owns code generation/expiry, monitoring-token hashing (SHA-256, token itself
  is returned once and never stored), and the due/overdue date math.
- **`analysis/`** — owns turning a raw Bundle into numbers and judgments. No I/O.
  - `book.py` — classifies ledgers into semantic categories by walking the Tally group tree
    (so custom voucher types/groups need no special-casing).
  - `metrics.py` (largest file, ~600 lines) — computes everything in the README's "What the
    report computes" section: revenue, customer concentration/HHI, receivables/payables
    ageing (bill-wise where available, FIFO fallback), working capital (DSO/DPO/DIO/CCC),
    balance sheet ratios, banking/cash behaviour.
  - `redflags.py` — stale books, back-dated entries, year-end sales spikes reversed in
    April, round-figure invoices, dual customer/supplier parties, cash-handling thresholds
    (s.269ST/s.40A(3)), suspense balances, negative net worth.
  - `indicators.py` — **the bank's editable policy**: green/amber/red thresholds per metric.
    Explicitly commented as "edit freely" — this is the one file meant to be tuned by a bank,
    not a bug surface.
  - `alerts.py` — compares two reports' indicators/snapshots to flag deterioration between
    monitoring cycles.
- **`report/`** — owns presentation of the computed numbers. `builder.py` assembles the
  final report dict + calls analysis; `charts.py` renders inline SVGs (no JS chart library);
  `glossary.py` is the single source of every credit term's name/plain-English line/formula;
  `format.py` has small formatters (days/pct/ratio).
- **`templates/`** — Jinja2 HTML, inline `<style>` per page, no shared CSS file or JS
  framework. See `design.md`.

## Connector (`connector/`) — who owns what
- **`main.go`** — CLI entry point and mode dispatch: interactive (opens a browser to a local
  127.0.0.1 UI), headless (`-code`/`-company`/`-consent`, or `-dump` to extract without
  uploading), `-monitor-run` (what the scheduled task invokes), `-monitor-stop`.
- **`internal/tally/`** — owns all Tally XML/HTTP protocol knowledge: UTF-16 request
  encoding (with a `-utf8` escape hatch), illegal-character sanitising, and the different
  response shapes Tally can return. Has its own tests (`tally_test.go`).
- **`internal/extract/`** — owns building the `Bundle` (see Data model below) from Tally
  responses: groups, ledgers, voucher types, stock snapshots, bills, and Day Book vouchers
  pulled month by month. Has its own tests (`extract_test.go`).
- **`internal/app/`** — the local UI: serves `index.html` on 127.0.0.1 with a per-run token,
  owns the guided 4-step flow (detect Tally → enter code → choose company/consent →
  extract/upload) and idle-timeout (exits ~45s after the tab closes).
- **`internal/monitor/`** — owns the monthly-monitoring lifecycle: `monitor.go` (due-check
    and upload loop invoked by `-monitor-run`/`-monitor-stop`), platform-specific scheduled-task
    registration split into `schedule_windows.go` (real Windows Task Scheduler XML, no admin
    rights needed) and `schedule_other.go` (non-Windows no-op, used for local dev builds on
    mac/Linux). Settings persist under `%AppData%\TallyConnector\` (`TC_HOME` env var
    overrides it, used by `dev/e2e.sh` and tests).
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
5. If the applicant opted into monitoring, the upload response includes a monitoring token
   (returned once, stored only as a SHA-256 hash). The connector then self-copies to
   `%AppData%\TallyConnector\`, saves settings, and registers a Windows scheduled task that
   periodically asks `/api/connector/monitor/status` whether a refresh is due
   (`store.monitoring_due`, policy: 25+ days since last report AND on/after `TC_MONITOR_DAY`),
   and if so extracts and uploads again via `/api/connector/monitor/upload`.
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

**SQLite schema** (`backend/app/store.py`, `backend/data/tally_connector.db` by default):
- `applications` — one row per borrower/loan request: identity, one-time code + expiry,
  `status` (awaiting_data | processing | ready | failed), monitoring fields added via
  `MIGRATIONS` (offered flag, status, hashed token, started/last-seen timestamps, a manual
  `force_refresh` flag, pointer to the latest report).
- `reports` — one row per upload (initial or monthly): status (processing | ready | failed),
  computed `indicators_json`/`snapshot_json`/`alerts_json`, `high_flags` count. Report
  artifacts themselves (the gzip bundle, `report.json`, `report.html`) live on disk under
  `DATA_DIR/applications/<app_id>/reports/<report_id>/`, not in SQLite.

No other datastore, queue, or cache exists in this repo.

## External services / SDKs
None. No cloud SDK, no third-party API, no payment/messaging integration. The only network
calls are: connector ↔ Tally's local XML/HTTP port, and connector ↔ bank backend over
plain HTTP(S) (the backend's own TLS termination is the bank's responsibility, not code in
this repo). PyPI/Go-stdlib dependencies only (`fastapi`, `uvicorn`, `jinja2`,
`python-multipart`, `pytest`, `httpx` for the backend; zero third-party Go modules).

## Config (names only — see `backend/app/config.py` and `README.md` "Backend configuration")
`TC_BANK_NAME`, `TC_ADMIN_USER`, `TC_ADMIN_PASSWORD`, `TC_DATA_DIR`, `TC_CODE_TTL_HOURS`,
`TC_DEFAULT_MONTHS`, `TC_MAX_UPLOAD_MB`, `TC_CONNECTOR_EXE`, `TC_MONITOR_DAY`,
`TC_MONITOR_OVERDUE_DAYS`. Connector-side env/flags (not server config, but worth knowing):
`TC_TALLY_URL`/`-tally`, `TC_SERVER`/`-server`, `TC_HOME` (test/dev override for where
monitoring settings are persisted, default `%AppData%\TallyConnector`). All have defaults;
none are secrets files or `.env`-loaded — they're plain OS environment variables, and
`TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` default to `admin`/`admin` if unset (see risks.md).

## Entry points
- Backend: `uvicorn app.main:app` (from `backend/`), app object is `app.main:app`.
- Connector: `go run .` / the built `TallyConnector.exe` (from `connector/`), entry
  function `main()` in `connector/main.go`; build via `connector/build.sh`.
- Dev: `dev/mock_tally.py` (standalone fake Tally server), `dev/e2e.sh` (orchestrates all
  three for a full local round trip — see `project.md` for its current verification status).
