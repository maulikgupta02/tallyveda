# Tally Connector

Lets a bank pull a loan applicant's books straight from Tally and get an automatic credit report covering revenue, customer concentration and spread, receivables and payables ageing, working capital, the balance sheet, leverage, banking and cash behaviour, GST, and red flags.

```
Applicant's PC (Windows)                          Bank
┌───────────────┐  XML/HTTP   ┌────────────────┐  HTTPS   ┌──────────────────────────────┐
│ TallyPrime /  │◄───────────►│ TallyConnector │─────────►│ backend (FastAPI)            │
│ Tally.ERP 9   │ localhost   │ .exe (Go)      │  gzip    │ codes · storage · analytics  │
│ port 9000     │             │ local web UI   │  JSON    │ report · dashboard           │
└───────────────┘             └────────────────┘          └──────────────────────────────┘
```

The backend's `/` is a public marketing home page (no auth) — see `design.md`'s "Marketing
home page" for its content/SEO/lead-form details. `/bank` and `/msme` below are the two
logged-in dashboards.

## How it works

1. **The bank** creates a request on the dashboard (`/bank`) and gets a one-time code, for example `7K2Q-XM9P`. The code is valid for 72 hours and can be used once.
2. **The applicant** downloads `TallyConnector.exe` (served at `/download` if configured) and double-clicks it on the computer where Tally is open. Nothing gets installed, and it needs no admin rights or runtime.
3. A browser page opens and walks them through four steps:
   - **Tally detected.** If Tally isn't found, the page shows how to turn on its server (F1 → Settings → Connectivity → *acts as Both*, port 9000).
   - **Enter the code.** The page then shows which bank is asking and for which application.
   - **Choose the company and consent.** The applicant types their name, and the consent text is stored with the upload.
   - **Extract and upload.** This takes about 1–5 minutes, depending on data volume.
4. **The bank** sees the status change to *Report ready* and opens the report. It can print to PDF or download JSON for its loan origination system (LOS).

The connector only reads from Tally and never writes to it.

## Daily monitoring (optional)

After the loan is given, the bank can keep receiving fresh books every day to spot early warning signs.

- **Bank:** tick *Offer daily monitoring* when creating the request.
- **Applicant:** a second, unticked-by-default checkbox appears in the connector: *Also send an updated copy daily*. When it's ticked, the backend issues a long-lived monitoring token, stored hashed. The connector then copies itself to `%AppData%\TallyConnector\`, saves its settings there, and registers a per-user Windows scheduled task (*TallyConnector Daily Update*; no admin rights needed).
- **Schedule:** the task runs every 2 hours from 09:00 to 21:00 while the user is logged in. It also runs after a missed start. Each run asks the bank `POST /api/connector/monitor/status` whether a refresh is due. If one is due and the company is open in Tally, it extracts and uploads. Otherwise it exits and tries again later.
- **Due rule:** set on the backend, so it can change without a new exe. A refresh is due once a calendar day has passed since the last report. *Refresh now* on the bank's side makes it due at the next check-in. A client with no data for `TC_MONITOR_OVERDUE_DAYS` (default 3) is marked overdue.
- **Alerts:** every report is stored, and each new one is compared with the previous one. Alerts are raised when an indicator gets worse (e.g. Good → Concern), a new high-severity red flag appears, or a key number moves sharply: revenue −15%, receivables +30%, receivables over 90 days +50%, debt +25%, OD/CC use +30%, net worth −20%. Moves under ₹5 L are ignored. Alerts appear at the top of the report, on the history page (`/bank/applications/{id}`, which also has an indicator trend table) and as a badge on the dashboard.
- **Stopping:** the bank clicks *Stop monitoring*, or the client opens the connector and clicks *Stop daily updates* (or runs `TallyConnector.exe -monitor-stop`). Either way, the scheduled task removes itself at its next run.
- **Incremental sync (connector 0.2+):** the backend keeps its own copy of each company's books (`app/books.py`), and the connector sends only what changed. The first share uploads one month at a time, newest first. If it's interrupted, the next run continues from the months still missing. A daily update sends the vouchers on days where Tally's change number (`AlterId`) moved, plus the last few days. It compares recent voucher IDs (92 days) to catch deletions, and re-reads balances only for ledgers those changes touched. The backend carries the other balances forward and rolls opening balances as the window moves. Every `TC_FULL_SYNC_DAYS` (default 30), the update is a full re-read instead, as a safety net. A full re-read also happens at the next check-in when a ledger or voucher type that stored vouchers use was renamed, since Tally renames inside vouchers without changing their AlterIds. Each update still produces a complete bundle and report, so history stays reproducible. Connectors 0.1.x keep using the one-shot `/api/connector/upload` and `monitor/upload`, which still work.
- **Several companies:** every company shared from one computer is a separate entry (with its own bank token) in `%AppData%\TallyConnector\companies.json`. A 0.1.x `monitor.json` is converted on first run. One scheduled task serves them all, and each is synced only while Tally has it loaded.

- **Retention:** every report's figures (`report.json`, about 40 KB) are kept for good, because trend charts and alerts read them. The raw upload (`bundle.json.gz`) and the rendered page are kept for `TC_KEEP_RAW_DAYS` (default 30), plus the first report and the last report of each month. An older page is re-rendered from its figures when opened. *Recompute* is only offered while the raw upload is kept. Pruning runs after each report, so no cron is needed.

Logs go to `%AppData%\TallyConnector\connector.log`. On Windows the exe is a GUI-subsystem app with no console window. The interactive UI shuts down about 45 seconds after its browser tab is closed.

### Linux / cloud installs (no scheduler)

`schedule_other.go` is a no-op on anything but Windows (there is no portable "Task Scheduler" API), so a connector running on a Linux cloud host (e.g. an AWS instance reaching Tally over the network via `-tally`/`TC_TALLY_URL`) needs its own scheduler for `-monitor-run`. `build.sh` produces `dist/tallyconnector-linux-amd64` for this alongside the Windows exe. Two ready-to-use options:

**cron** — add a line like this to the service account's crontab (`crontab -e`), running a few times a day to match the Windows cadence:
```
0 9,11,13,15,17,19,21 * * * /opt/tallyconnector/tallyconnector-linux-amd64 -monitor-run >> /var/log/tallyconnector.log 2>&1
```

**systemd timer** — `/etc/systemd/system/tallyconnector.service`:
```ini
[Unit]
Description=TallyConnector monitoring check

[Service]
Type=oneshot
ExecStart=/opt/tallyconnector/tallyconnector-linux-amd64 -monitor-run
```
and `/etc/systemd/system/tallyconnector.timer`:
```ini
[Unit]
Description=Run TallyConnector's monitoring check every 2 hours

[Timer]
OnCalendar=*-*-* 9,11,13,15,17,19,21:00:00
Persistent=true

[Install]
WantedBy=timers.target
```
Enable with `systemctl enable --now tallyconnector.timer`. `TC_HOME` still controls where settings/logs are kept (default `$XDG_CONFIG_HOME` or `~/.config/TallyConnector` via Go's `os.UserConfigDir()` on Linux), so point it at a directory the service account can write to if it isn't running as a normal login user.

## Terminology

The report uses the standard credit terms (LTM, YoY, DSO, DPO, DIO, CCC, TNW, Debt/TNW, TOL/TNW, ICR, DSCR, HHI, EBITDA, COGS, NRR…). Each one shows its full name and a plain-English line, with the formula on hover, and there's a glossary at the end. All wording comes from `backend/app/report/glossary.py`.

## Repository layout

| Path | What it is |
|---|---|
| `connector/` | Go connector. Stdlib only, builds into a single ~8 MB `.exe` (plus a Linux binary for cloud installs, see `build.sh`) |
| `connector/internal/tally` | Tally XML client: UTF-16, sanitises illegal characters, handles the different response layouts |
| `connector/internal/extract` | Builds the bundle: groups, ledgers, voucher types, stock snapshots, bills, and Day Book vouchers month by month |
| `connector/internal/app` | Local UI, served on 127.0.0.1 with a per-run token |
| `backend/app/analysis` | `book.py` classifies ledgers, `metrics.py` computes metrics, `redflags.py` checks for red flags, `indicators.py` holds the bank's traffic-light policy |
| `backend/app/report` | Builds the report and inline SVG charts |
| `dev/mock_tally.py` | Mock Tally server with a synthetic company that has deliberate problems |
| `dev/e2e.sh` | Full end-to-end run |

## Running locally

```bash
# backend
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
TC_DEV=1 .venv/bin/uvicorn app.main:app --port 8000   # dashboard: http://localhost:8000/bank (admin/admin)

# mock Tally (instead of real Tally)
python3 dev/mock_tally.py --port 9000

# connector (guided UI)
cd connector && go run . -server http://localhost:8000

# tests
cd backend && .venv/bin/python -m pytest -q
cd connector && go test ./...
dev/e2e.sh
```

Headless connector commands, useful for support and debugging:

```
TallyConnector.exe -code 7K2Q-XM9P -company "My Co Pvt Ltd" -consent "A. Kumar"
TallyConnector.exe -company "My Co Pvt Ltd" -dump books.json     # extract only, no upload
TallyConnector.exe -tally http://192.168.1.20:9000 ...           # Tally on another machine
TallyConnector.exe -monitor-run                                  # what the scheduled task runs
TallyConnector.exe -monitor-stop                                 # withdraw monitoring consent
```

## Building for distribution

```bash
cd connector && SERVER=https://tally.yourbank.in VERSION=1.0.0 ./build.sh
```

This produces one `dist/TallyConnector.exe` that runs on every Windows PC (a 32-bit build, so it works on 32-bit, 64-bit and ARM Windows alike), plus `dist/tallyconnector-linux-amd64` for Linux cloud installs (see "Linux / cloud installs" above). Before giving the Windows exe to applicants, sign it with the bank's code-signing certificate (`signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f bank.pfx TallyConnector.exe`). Without a signature, Windows SmartScreen and antivirus tools will warn about an unsigned download.

## Backend configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `TC_BANK_NAME` | Demo Bank | Shown to applicants in the connector |
| `TC_ADMIN_USER` / `TC_ADMIN_PASSWORD` | none | Required (backend won't start without them). Seed the first bank tenant + its first bank user on first startup of an empty database only; every login after that is a real per-tenant account in `store.py`'s `users` table — see "Accounts" below |
| `TC_DEV` | unset | `1` allows the `admin`/`admin` seed login for local runs. Never set it in production |
| `TC_DATA_DIR` | `backend/data` | SQLite database and uploaded bundles — ignored when `DATABASE_URL` is set |
| `DATABASE_URL` | unset | `postgres://...`/`postgresql://...` — when set, everything (including per-report files) is stored in Postgres instead of SQLite/`TC_DATA_DIR`. See "Deploying (Render + Neon)" below |
| `TC_CODE_TTL_HOURS` | 72 | How long a code stays valid |
| `TC_DEFAULT_MONTHS` | 24 | Months of data requested |
| `TC_MAX_UPLOAD_MB` | 300 | Maximum size of a compressed upload |
| `TC_CONNECTOR_EXE` | – | Path to the signed exe on local disk, served at `/download` |
| `TC_CONNECTOR_URL` | – | If set, `/download` redirects here instead (e.g. a GitHub Releases asset) — takes priority over `TC_CONNECTOR_EXE`, and is the only option that works on Render's ephemeral filesystem |
| `TC_MONITOR_OVERDUE_DAYS` | 3 | Monitored clients with no data for longer are shown as overdue |
| `TC_PLATFORM_ADMIN_USER` / `TC_PLATFORM_ADMIN_PASSWORD` | none | Optional. Seed the first `/admin` (platform admin) login on first startup of an empty database, if both are set. `TC_DEV=1` falls back to `platform`/`platform` for local runs. Unlike `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD`, leaving these unset is fine — `/admin` just has no working login until one is created (`python -m app.manage create-platform-admin`) |
| `TC_PLATFORM_NAME` | Tally Connector | Name of the operator's own built-in "direct" tenant (MSMEs with no bank) — shown as the counterparty name in the connector's consent text for a direct client |
| `TC_PUBLIC_URL` | `https://tally-connector-1lir.onrender.com` | Canonical origin for the public marketing home page (`/`) — its `<link rel="canonical">`, Open Graph/Twitter tags, JSON-LD, `robots.txt`'s `Sitemap:` line and `sitemap.xml`. Change it once a real domain is bought |

Run the backend behind TLS (nginx, a load balancer, or similar). The connector sends financial data, so the server URL must be `https://` in production.

## Deploying (Render + Neon)

The backend can run on Render's **free** web service plan, with a Neon **free** Postgres
database as the only durable storage (Render's own disk is ephemeral — it's wiped on every
restart/redeploy/sleep, so `DATABASE_URL` must be set or every application, report and
uploaded bundle is lost the first time the instance restarts).

1. **Create a Neon project** (free tier) and copy its pooled connection string — it already
   includes `sslmode=require&channel_binding=require` and looks like
   `postgresql://user:pass@ep-xxxx-pooler.<region>.aws.neon.tech/dbname?sslmode=require...`.
   Don't strip the `-pooler` or the query string.
2. **Create a Render Blueprint** from this repo (`render.yaml` at the repo root, `rootDir:
   backend`) — one free web service, `uvicorn app.main:app --host 0.0.0.0 --port $PORT`,
   health check `/healthz`, region `singapore`, Python pinned via `PYTHON_VERSION`.
3. **Set the env vars Render asks for** (the blueprint declares them but leaves the values to
   you, since they're secrets):
   - `DATABASE_URL` — Neon's pooled connection string from step 1.
   - `TC_ADMIN_USER` — the first bank's admin username.
   - `TC_ADMIN_PASSWORD` — Render can generate this one for you (`generateValue: true` in the
     blueprint); copy it from the Render dashboard after the first deploy.
   - `TC_BANK_NAME` — defaults to "Demo Bank" in the blueprint; change it to the real bank name.
   - Never set `TC_DEV` here — it would allow an `admin`/`admin` login in production.
4. **Deploy.** The backend creates its schema (tables, including the `report_files` table
   used only when `DATABASE_URL` is set) on first startup, same as SQLite's `MIGRATIONS`
   locally — no separate migration step to run.
5. **The connector download** (`/download`) has nothing to serve from disk on Render (the
   `.exe` isn't in the image and nothing written at runtime survives a restart). Set
   `TC_CONNECTOR_URL` to a GitHub Releases asset URL for the built exe instead, or leave both
   `TC_CONNECTOR_EXE`/`TC_CONNECTOR_URL` unset and distribute the exe to applicants directly.

**Free-tier caveats** (see `docs/agents/risks.md` for the full list): Render's free instance
sleeps after 15 minutes idle and the next request can take about a minute to wake it — the
connector's HTTP client already allows up to 15 minutes per request and retries a connection
failure, so this doesn't normally surface as an error, just a slow first upload/monitoring
check after a gap. Neon's free tier caps storage at 0.5 GB total across all projects — bundles
are real financial data (ledgers, vouchers, balances) and are stored unencrypted at rest, same
as the local SQLite/disk setup; see "Before a pilot" above before using either for real
customer data.

## Accounts

One backend hosts several banks (tenants), their applicants' MSME logins, and direct MSME
clients with no bank at all (the platform's own built-in tenant, named by `TC_PLATFORM_NAME`).
`TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` only seed the first bank tenant and its first bank user,
the first time the backend runs against an empty database; `TC_PLATFORM_ADMIN_USER`/
`TC_PLATFORM_ADMIN_PASSWORD` do the same for the first `/admin` login. After that:
- **`/admin`** (role `platform`, HTTP Basic auth) is the everyday way to run the platform: create/
  suspend/reactivate banks, create/manage bank users and MSMEs (either under a bank or direct),
  issue/reset MSME dashboard logins, delete an MSME and all its data, manage every user across
  tenants, and read the audit log of every admin action. See `docs/agents/architecture.md` for
  the full route list.
- `python -m app.manage` still works for scripted/first-time provisioning: `create-tenant
  "Bank name"`, `create-bank-user <tenant_id> <username>`, `create-platform-admin <username>`
  (each prints a one-time password).
- A bank user only ever sees applications created under their own tenant (`/bank`); a suspended
  bank's users (and any disabled user, of any role) can't log in anywhere.
- An MSME login is tied to exactly one application: from that application's page (`/bank/...`
  or `/admin/msmes/...`), issue/reset it under "MSME dashboard login". The applicant then signs
  in at `/msme` to see their own analysis — never another company's.

State-changing form submissions under `/admin` and `/bank` are CSRF-guarded: a request whose
`Origin` (or, failing that, `Referer`) header names a different host is rejected; a request with
neither header is allowed (see `main.py`'s `_csrf_guard` for the full reasoning). Connector and
JSON `/api/...` endpoints are exempt — they're not browser form submissions.

## What the report computes

The last-twelve-months (LTM) window ends on the extraction date and is compared with the 12 months before it.

- **Revenue:** monthly sales and collections, YoY growth, seasonality, and gross/net/EBITDA margin (COGS adjusted with stock values at the window boundaries).
- **Customers:** top 1/5/10 share, HHI, how many customers make up 80% of sales, new vs lost customers, net revenue retention, sales by state, and customers by size band. Sales are attributed through the ledger group tree, so custom voucher types and credit notes need no setup.
- **Receivables and payables:** ageing buckets (bill-wise where Tally provides it, otherwise FIFO against invoices), DSO/DPO, collection ratio, largest debtors and creditors, and customers who haven't paid in 90 days.
- **Working capital:** DSO, DIO, DPO and the cash conversion cycle.
- **Balance sheet:** current and quick ratio, net worth (assets minus outside liabilities, so it includes the current year's profit that hasn't been posted yet), Debt/TNW, TOL/TNW, existing loans with repayments, interest cover and approximate DSCR.
- **Banking and cash:** bank credits by month (to reconcile with the bank statement), share of customer receipts in cash, and days with a negative cash balance.
- **Red flags:** stale books, back-dated entries (MasterID order vs voucher date), a year-end sales spike followed by credit notes in April, round-figure invoices, parties that are both customer and supplier (matched by GSTIN or name), cash receipts of ₹2 L or more (s.269ST), cash payments over ₹10k (s.40A(3)), large advances to related parties, suspense balances, and negative net worth.

Traffic-light thresholds are bank policy and live in `backend/app/analysis/indicators.py`. The report supports a credit officer's judgement; it doesn't make the lending decision.

## Before a pilot

- **Validate the Windows scheduled task** on Windows 10 and 11 as a standard (non-admin) user: creating it from the task XML, a run after a missed start, and removal. This path can't be tested on macOS.
- **Validate against real Tally.** The request and response formats follow TallyPrime's documented XML interface and are exercised through the mock, but they haven't been run against a live TallyPrime or ERP 9 install yet. The ones to check first are the `Bills` collection (bill-wise outstanding; if it fails, ageing falls back to FIFO and a warning is added), GSTIN and state field names on TallyPrime 4+, and Day Book performance on companies with more than 100k vouchers.
- **Security:** replace Basic auth with the bank's SSO, encrypt bundles at rest, set a retention and deletion policy, and keep an audit log of who viewed which report.
- **Compliance:** have legal review the consent text and consider the Account Aggregator / DPDP Act requirements that apply to the bank.
