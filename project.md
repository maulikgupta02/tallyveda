# tally_connector

Lets a bank pull a loan applicant's Tally books (via a small Windows connector the
applicant runs) and turns them into an automatic credit report — revenue, customer
concentration, receivables/payables ageing, working capital, balance sheet, leverage,
banking/cash behaviour, GST, and red flags — plus optional daily monitoring after the loan
is disbursed.

## Stack
- **Backend**: Python 3.12, FastAPI + Jinja2 (server-rendered HTML, no JS framework),
  SQLite (stdlib `sqlite3`, no ORM), `uvicorn`. Lives in `backend/`.
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

# backend — run (verified: starts, /healthz returns {"ok": true})
TC_DEV=1 .venv/bin/uvicorn app.main:app --port 8000   # dashboard at /bank, admin/admin (dev only)

# backend — tests (verified: 17 passed)
.venv/bin/python -m pytest -q

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
automatically on every connection — see `architecture.md`). No separate build step for the
backend. No deploy tooling is checked in yet. The README still says the bank runs the
backend behind its own TLS, but that's out of date — see "Product direction" below.

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
  another bank is `python -m app.manage create-tenant`/`create-bank-user` (no admin web UI
  yet — see `docs/agents/risks.md`).
- **Daily refreshes (2026-10-02, resolved).** Refreshes are now due once a calendar day has
  passed since the last report (`store.monitoring_due`), not monthly; labels/copy in the
  connector and bank/MSME UI say "Daily"/"Refresh" accordingly. Each refresh still re-extracts
  the full requested window rather than only new data — see `docs/agents/risks.md` for why
  true incremental extraction wasn't attempted in this pass.
- **Cloud installs (2026-10-02, partially resolved).** Automatic scheduling still only exists
  on Windows (`schedule_windows.go`); there is no portable non-Windows scheduler API. A Linux
  cloud host now has a documented, ready-to-use cron line and systemd timer for running
  `-monitor-run` (README.md "Linux / cloud installs", `schedule_other.go`'s doc comment) instead
  of being left to figure it out by hand.
- **Hosting.** No deploy config, TLS, backups or production storage are in the repo. SQLite plus
  unencrypted bundles on local disk (see `docs/agents/risks.md`) need revisiting for a hosted
  multi-customer service.

## Folder map
```
backend/app/
  main.py          FastAPI app: connector API, bank dashboard, MSME dashboard, bank JSON API
  config.py        env-var config (all with safe defaults)
  store.py         SQLite access: tenants, users, applications, codes, monitoring, reports
  manage.py        CLI to provision tenants/bank users (no admin web UI yet)
  analysis/        book.py (ledger classification), metrics.py (the actual numbers),
                   redflags.py, indicators.py (bank's traffic-light policy), alerts.py
                   (report-to-report comparison)
  report/          builder.py, charts.py (inline SVG), format.py, glossary.py (all report wording)
  templates/       dashboard.html, application.html, msme.html, msme_login.html, report.html
                   (server-rendered Jinja2)
  tests/           test_analysis.py, test_monitoring.py (pytest, all passing)
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
- `docs/agents/architecture.md` — modules, data flow, data model, config, entry points.
- `docs/agents/risks.md` — fragile areas, missing coverage, what not to touch without a human.
- `design.md` — this repo has a UI (bank dashboard + report + connector's local setup page);
  colors/typography/components are documented there.

## Organization context
This is a sole repo (no `org.md` — not part of a registered multi-repo organization).
