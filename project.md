# tally_connector

Lets a bank pull a loan applicant's Tally books (via a small Windows connector the
applicant runs) and turns them into an automatic credit report — revenue, customer
concentration, receivables/payables ageing, working capital, balance sheet, leverage,
banking/cash behaviour, GST, and red flags — plus optional daily monitoring after the loan
is disbursed.

## Stack
- **Backend**: Python 3.12, FastAPI + Jinja2 (server-rendered HTML, no JS framework),
  SQLite (stdlib `sqlite3`, no ORM) by default, or Postgres (via `psycopg[binary]`, no ORM)
  when `DATABASE_URL` is set — the only durable storage on a Render free web service, whose
  disk is ephemeral. `uvicorn`. Lives in `backend/`. See "Deploying" in `README.md` and
  `docs/agents/architecture.md`'s "Storage backend" section.
- **Connector**: Go 1.22, standard library only (no third-party deps — `connector/go.mod`
  has no `require` block). Builds to a single static Windows `.exe`. Lives in `connector/`.
- **Dev tooling**: `dev/mock_tally.py` (fake TallyPrime XML/HTTP server with a synthetic
  company and deliberate red flags), `dev/e2e.sh` (full local round trip).
- No external services, cloud SDKs, or package registries beyond PyPI/Go stdlib — this is
  a fully self-contained, deployable-anywhere system (bank runs the backend behind its own
  TLS; nothing calls out to a third party).

## Install / run / build / test
All commands below were run from the repo root in this sandbox and verified working,
except where noted.

```bash
# backend — install (verified: venv already present at backend/.venv, same result)
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt

# backend — run (verified: starts, /healthz returns {"ok": true}, / renders the marketing home page)
TC_DEV=1 .venv/bin/uvicorn app.main:app --port 8000   # home page at /, /bank admin/admin, /admin platform/platform (dev only)

# backend — tests (verified: 66 passed, 2026-10-02)
.venv/bin/python -m pytest -q

# backend — tests against Postgres instead of SQLite (verified: 66/66 passed against a
# throwaway local cluster; same suite, no code path is SQLite-specific)
DATABASE_URL=postgresql://user@host:port/dbname .venv/bin/python -m pytest -q

# connector — vet/tests (verified: go vet clean, `go test ./...` passed — extract and tally packages have tests)
cd connector && go vet ./... && go test ./...

# connector — build (verified compiles; built to a throwaway path outside the repo, not
# connector/dist, to avoid touching the committed-but-gitignored prebuilt binaries there)
go build -o /tmp/throwaway-binary .

# connector — real distributable build (NOT run in this sandbox — produces the actual
# dist/ binaries; only run this intentionally, see connector/build.sh)
SERVER=https://tally.yourbank.in VERSION=1.0.0 ./build.sh

# mock Tally, for local dev instead of a real Tally install (verified: serves on :9000,
# answers a ping with "TallyPrime Server is Running")
python3 dev/mock_tally.py --port 9000

# end-to-end script (mock Tally -> connector headless run -> backend -> report)
dev/e2e.sh
```

`dev/e2e.sh` **could not be verified end-to-end in this sandbox**: each piece works in
isolation (pytest suite passes, the backend answers correctly when queried directly with
`curl 127.0.0.1:<port>`, mock Tally answers directly), but the script's own `curl
localhost:<port>/...` calls intermittently got a `400 Bad Request` with headers that look
like a corporate network proxy/VPN intercepting the connection (`Server: 127.0.0.1`,
`Access-Control-Allow-Headers: X-Okta-XsrfToken, ...`), not an application error. Treat the
script itself as unverified here; re-run it on a machine without that interception before
relying on it.

No database migrations beyond `backend/app/store.py`'s own `SCHEMA`/`MIGRATIONS` (applied
automatically on every connection, against SQLite or Postgres — see `architecture.md`). No
separate build step for the backend. **Deploy tooling now exists**: `render.yaml` at the repo
root (Render Blueprint, free web service) plus a Neon free Postgres database — see README's
"Deploying (Render + Neon)". The README still says the bank runs the backend behind its own
TLS; that's out of date for the hosted path (Render terminates TLS) but still applies if a
bank runs the backend itself — see "Product direction" below.

## Product direction (owner, 2026-10-02)
- **We host the backend** (not the bank). Banks and MSMEs both use it as a hosted service.
- **The MSME installs the connector** next to their Tally, either on their own machine or on
  their cloud (e.g. a Windows VM on AWS).
- **Banks get a dashboard** with a detailed, near-real-time feed of each company's analysis,
  **refreshed daily**.
- **MSMEs get their own dashboard** showing the analysis relevant to them: sales, growth and
  potential areas of concern.

Gaps between that and the code today (checked 2026-10-02):
- **MSME dashboard resolved** (2026-10-02, ticket 7485bcfc phase 2): `/msme` is now its own
  plain-language, tabbed dashboard (Home/Sales/Customers/Money/Dues, `report/dashboard.py`'s
  `msme_view`) — sales trend, what's due, who's slow to pay, busy months — with no ratings,
  thresholds or lending language anywhere. `/msme/report` no longer serves the bank's credit
  report (redirects to `/msme`); `/msme/report.json` returns the MSME view-model instead.
- **Multi-tenant accounts exist now** (2026-10-02): `store.py` has `tenants` and `users`
  tables, every `applications` row carries a `tenant_id`, bank logins are per-tenant accounts
  (`/bank`, scoped by `tenant_id`), and each application can have its own MSME login
  (`/msme`, scoped to one `application_id`) issued from the bank's application page.
  `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` now only seed the first tenant + bank user; onboarding
  another bank can still be done via `python -m app.manage create-tenant`/`create-bank-user`,
  or from `/admin` (see below).
- **Platform admin panel built** (2026-10-02, ticket 2d48bed7): a new user role `platform`
  (seeded from `TC_PLATFORM_ADMIN_USER`/`TC_PLATFORM_ADMIN_PASSWORD`, or `python -m app.manage
  create-platform-admin`) can manage the whole platform at `/admin` — banks (create, rename,
  suspend/reactivate), every MSME (bank-linked or direct, with filters/search, create, issue/
  reset its MSME login, a read-only dashboard view, delete with a typed confirmation), every
  user across tenants (reset/disable/enable/delete, with a guard against disabling/deleting the
  last active platform admin), and an audit log of every admin action. Tenants gained a `kind`
  (`bank` | `direct`) and `status` (`active` | `suspended`, bank tenants only); one built-in
  direct tenant (named by `TC_PLATFORM_NAME`, default "Tally Connector") holds MSMEs with no
  bank, visible only to platform admins and to the MSME's own login. A disabled user, or any
  user of a suspended bank, can't log in anywhere (`/bank`, `/msme` or `/admin`). State-changing
  form POSTs under `/admin` and `/bank` are now CSRF-guarded (`main.py`'s `_csrf_guard`) — see
  `docs/agents/architecture.md` for the exact rule. See `docs/agents/risks.md` for what's still
  open.
- **Daily refreshes (2026-10-02, resolved).** Refreshes are now due once a calendar day has
  passed since the last report (`store.monitoring_due`), not monthly; labels/copy in the
  connector and bank/MSME UI say "Daily"/"Refresh" accordingly.
- **Incremental, resumable sync (2026-10-03, connector 0.2).** The backend keeps a copy of each
  company's books (`app/books.py`); the connector sends only changes and resumes an interrupted
  first share month by month. One connector serves several companies (`companies.json`). See
  `docs/agents/architecture.md`, upload flow step 3.
- **Cloud installs (2026-10-02, partially resolved).** Automatic scheduling still only exists
  on Windows (`schedule_windows.go`); there is no portable non-Windows scheduler API. A Linux
  cloud host now has a documented, ready-to-use cron line and systemd timer for running
  `-monitor-run` (README.md "Linux / cloud installs", `schedule_other.go`'s doc comment) instead
  of being left to figure it out by hand.
- **Hosting.** No deploy config, TLS, backups or production storage are in the repo. SQLite plus
  unencrypted bundles on local disk (see `docs/agents/risks.md`) need revisiting for a hosted
  multi-customer service.
- **Public marketing home page (resolved, 2026-10-02, ticket 90b7d084).** `GET /` is now a
  server-rendered marketing page (was a bare redirect to `/bank`) — see `design.md`'s
  "Marketing home page" for the full breakdown. Verified in this pass: all four Lighthouse
  categories (performance/accessibility/best-practices/SEO) score 100 against a local run;
  `backend/tests/test_home.py` passed against both SQLite and a throwaway local Postgres.

## Folder map
```
backend/app/
  main.py          FastAPI app: connector API, bank dashboard, MSME dashboard, bank JSON API,
                   platform admin panel (/admin), public marketing home page (/, robots.txt,
                   sitemap.xml, /api/leads)
  config.py        env-var config (all with safe defaults)
  store.py         SQLite/Postgres access: tenants, users, applications, codes, monitoring,
                   reports, audit log, leads
  manage.py        CLI to provision tenants/bank users/platform admins and list leads (also doable from /admin)
  marketing.py     static content + small pure helpers for the home page (chart data, FAQ,
                   cash-cycle calculator, JSON-LD) — see design.md's "Marketing home page"
  analysis/        book.py (ledger classification), metrics.py (the actual numbers),
                   redflags.py, indicators.py (bank's traffic-light policy), alerts.py
                   (report-to-report comparison)
  report/          builder.py, charts.py (inline SVG), format.py, glossary.py (all report wording)
  templates/       dashboard.html, application.html, msme.html, msme_login.html, report.html
                   (server-rendered Jinja2), admin_overview/banks/bank_detail/msmes/
                   msme_detail/users/audit/leads.html, admin_credential.html,
                   home.html (public marketing page)
  static/          favicon.ico/.svg, icon-192.png, apple-touch-icon.png, og-image.png,
                   fonts/ (self-hosted woff2 for home.html only) — served at /static,
                   long-cached; favicon.ico also served at the root
backend/tests/     test_analysis.py, test_monitoring.py, test_dashboard.py, test_admin.py, test_home.py
                   (pytest, all passing)
connector/
  main.go          CLI entry: interactive UI mode, headless mode, -monitor-run/-monitor-stop
  internal/tally/   Tally XML/HTTP client (UTF-16, quirky response handling)
  internal/extract/ builds the upload Bundle from Tally data (has its own tests)
  internal/app/     local 127.0.0.1 UI (index.html) + the Period()/RunJob() job logic
  internal/monitor/ daily scheduled-task logic (schedule_windows.go / schedule_other.go)
  internal/upload/  HTTP client to the bank backend
  dist/            prebuilt .exe binaries — gitignored, never rebuild/overwrite casually
dev/
  mock_tally.py    fake Tally server for local dev/testing
  e2e.sh           full local round trip (see caveat above)
```

## Conventions actually observed
- Python: `from __future__ import annotations`, type hints throughout, small pure functions
  in `analysis/`, no classes where a dict/function will do. Comments explain *why* (e.g. the
  debit-positive sign convention, why DATA_DIR migrations exist) not *what*.
- Go: stdlib-only, no third-party deps; platform-specific code split into `_windows.go` /
  `_other.go` build-tagged files rather than runtime branching.
- Config is always `os.environ.get("TC_...", <sane default>)` — see `config.py` and
  `architecture.md` for the full list.
- SQLite schema evolves via an explicit `MIGRATIONS` dict in `store.py`, applied idempotently
  on every connection — no separate migration tool/files.
- Security-sensitive defaults (72h codes, 300MB upload cap) are documented
  inline in `README.md` under "Before a pilot" — read that section before any production use.

## Docs
- `docs/agents/seo-backlog.md` — deferred SEO and trust fixes for the home page (audit 2026-10-02).
- `docs/agents/architecture.md` — modules, data flow, data model, config, entry points.
- `docs/agents/risks.md` — fragile areas, missing coverage, what not to touch without a human.
- `design.md` — this repo has a UI (bank dashboard + report + connector's local setup page);
  colors/typography/components are documented there.

## Organization context
This is a sole repo (no `org.md` — not part of a registered multi-repo organization).
