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

## Monthly monitoring (optional)

After the loan is given, the bank can keep receiving fresh books every month to spot early warning signs.

- **Bank:** tick *Offer monthly monitoring* when creating the request.
- **Applicant:** a second, unticked-by-default checkbox appears in the connector: *Also send an updated copy about once a month*. When it's ticked, the backend issues a long-lived monitoring token, stored hashed. The connector then copies itself to `%AppData%\TallyConnector\`, saves its settings there, and registers a per-user Windows scheduled task (*TallyConnector Monthly Update*; no admin rights needed).
- **Schedule:** the task runs every 2 hours from 09:00 to 21:00 while the user is logged in. It also runs after a missed start. Each run asks the bank `POST /api/connector/monitor/status` whether a refresh is due. If one is due and the company is open in Tally, it extracts and uploads. Otherwise it exits and tries again later.
- **Due rule:** set on the backend, so it can change without a new exe. A refresh is due 25+ days after the last report, on or after `TC_MONITOR_DAY` (default the 5th, so the previous month is booked). *Refresh now* on the bank's side makes it due at the next check-in. A client with no data for `TC_MONITOR_OVERDUE_DAYS` (default 40) is marked overdue.
- **Alerts:** every report is stored, and each new one is compared with the previous one. Alerts are raised when an indicator gets worse (e.g. Good → Concern), a new high-severity red flag appears, or a key number moves sharply: revenue −15%, receivables +30%, receivables over 90 days +50%, debt +25%, OD/CC use +30%, net worth −20%. Moves under ₹5 L are ignored. Alerts appear at the top of the report, on the history page (`/bank/applications/{id}`, which also has an indicator trend table) and as a badge on the dashboard.
- **Stopping:** the bank clicks *Stop monitoring*, or the client opens the connector and clicks *Stop monthly updates* (or runs `TallyConnector.exe -monitor-stop`). Either way, the scheduled task removes itself at its next run.

Logs go to `%AppData%\TallyConnector\connector.log`. On Windows the exe is a GUI-subsystem app with no console window. The interactive UI shuts down about 45 seconds after its browser tab is closed.

## Terminology

The report uses the standard credit terms (LTM, YoY, DSO, DPO, DIO, CCC, TNW, Debt/TNW, TOL/TNW, ICR, DSCR, HHI, EBITDA, COGS, NRR…). Each one shows its full name and a plain-English line, with the formula on hover, and there's a glossary at the end. All wording comes from `backend/app/report/glossary.py`.

## Repository layout

| Path | What it is |
|---|---|
| `connector/` | Go connector. Stdlib only, builds into a single ~8 MB `.exe` |
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
TallyConnector.exe -monitor-stop                                 # withdraw monthly consent
```

## Building for distribution

```bash
cd connector && SERVER=https://tally.yourbank.in VERSION=1.0.0 ./build.sh
```

This produces `dist/TallyConnector.exe` (64-bit) and a 32-bit build for older PCs. Before giving it to applicants, sign the exe with the bank's code-signing certificate (`signtool sign /fd sha256 /tr http://timestamp.digicert.com /td sha256 /f bank.pfx TallyConnector.exe`). Without a signature, Windows SmartScreen and antivirus tools will warn about an unsigned download.

## Backend configuration (environment variables)

| Variable | Default | |
|---|---|---|
| `TC_BANK_NAME` | Demo Bank | Shown to applicants in the connector |
| `TC_ADMIN_USER` / `TC_ADMIN_PASSWORD` | none | Required (backend won't start without them). Seed the first bank tenant + its first bank user on first startup of an empty database only; every login after that is a real per-tenant account in `store.py`'s `users` table — see "Accounts" below |
| `TC_DEV` | unset | `1` allows the `admin`/`admin` seed login for local runs. Never set it in production |
| `TC_DATA_DIR` | `backend/data` | SQLite database and uploaded bundles |
| `TC_CODE_TTL_HOURS` | 72 | How long a code stays valid |
| `TC_DEFAULT_MONTHS` | 24 | Months of data requested |
| `TC_MAX_UPLOAD_MB` | 300 | Maximum size of a compressed upload |
| `TC_CONNECTOR_EXE` | – | Path to the signed exe, served at `/download` |
| `TC_MONITOR_DAY` | 5 | Monthly refreshes are due on or after this day of the month |
| `TC_MONITOR_OVERDUE_DAYS` | 40 | Monitored clients with no data for longer are shown as overdue |

Run the backend behind TLS (nginx, a load balancer, or similar). The connector sends financial data, so the server URL must be `https://` in production.

## Accounts

One backend hosts several banks (tenants) and their applicants' MSME logins. `TC_ADMIN_USER`/
`TC_ADMIN_PASSWORD` only seed the first bank tenant and its first bank user, the first time the
backend runs against an empty database. After that:
- Onboard another bank with `python -m app.manage create-tenant "Bank name"`, then
  `python -m app.manage create-bank-user <tenant_id> <username>` (prints a one-time password).
  A bank user only ever sees applications created under their own tenant (`/bank`).
- An MSME login is tied to exactly one application: from that application's page, the bank
  clicks "Create login" (or "Reset password" to rotate it) under "MSME dashboard login". The
  applicant then signs in at `/msme` to see their own analysis — never another company's.

There's no admin web UI for tenant/user management yet; `python -m app.manage` is it.

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
