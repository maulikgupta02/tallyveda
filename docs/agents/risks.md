# Risks

## Do not touch without a human
- **`backend/app/analysis/indicators.py` thresholds** are explicit bank policy (green/amber/
  red cutoffs for every metric). The file's own docstring says "edit freely," but a change
  here changes what the bank considers a lending risk — treat any edit as a business decision,
  not a code-quality one, and have a human confirm the new thresholds are intended.
- **`backend/app/analysis/redflags.py` thresholds tied to Indian statute** — cash receipts
  ≥ ₹2 L (s.269ST) and cash payments > ₹10k (s.40A(3)) are legal thresholds, not tunable
  business policy like `indicators.py`. Don't adjust these without confirming the current
  legal figures with a human; they can change with Finance Act amendments.
- **`connector/internal/monitor/schedule_windows.go`** (Windows Task Scheduler registration,
  per-user, no admin rights) — per the README, this path is explicitly **not yet validated
  against real Windows 10/11** ("Validate the Windows scheduled task on Windows 10 and 11 as
  a standard (non-admin) user... This path can't be tested on macOS"). Any change here is
  unverifiable in this sandbox; treat it as higher-risk than the rest of the Go code.
- **Default credentials**: `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` default to `admin`/`admin`
  (`backend/app/config.py`) if the env vars aren't set. The README flags this under "Before a
  pilot" as something that must change, but the code has no safeguard (no startup warning, no
  refusal to run with defaults in a non-dev context). Don't treat a repo/install using the
  defaults as production-safe.
- **`connector/dist/`** — prebuilt `.exe` binaries are committed-but-gitignored (per the task
  brief and `.gitignore`). Never rebuild into this path casually; a verification build should
  go to a throwaway path outside the repo (this onboarding pass did that).

## Unverified against the real external system
- **Tally XML integration has never been run against a real TallyPrime/ERP 9 install** — only
  against `dev/mock_tally.py`. The README explicitly calls out the riskiest areas: the `Bills`
  collection (bill-wise ageing; falls back to FIFO + a warning if it fails), GSTIN/state field
  names on TallyPrime 4+, and Day Book performance on companies with >100k vouchers. Any
  change to `connector/internal/tally/` should be treated as unverified until tested against
  real Tally, regardless of how clean the mock-based tests look.
- **`dev/e2e.sh` could not be run to completion in this sandbox** (see `project.md`) — the
  individual pieces (backend tests, go vet/test, mock Tally, backend startup) were each
  verified in isolation, but the orchestrated script itself hit what looks like local network
  interception, not an app bug. Don't assume the script is broken from this result alone —
  re-verify on an unaffected machine before trusting either outcome.
- **Windows-specific connector behavior** (scheduled task creation/removal, `-H windowsgui`
  no-console build, `%AppData%` self-copy) is only buildable/cross-checked by reading code on
  this (macOS) sandbox — `schedule_other.go`'s non-Windows stub is what actually compiles and
  runs here. Treat all Windows-only code paths as unverified by this onboarding pass.

## Missing test coverage
- `backend/app/main.py` itself (routes, auth, throttling, upload-size/gzip-bomb guarding) has
  **no dedicated test file** — `test_analysis.py` and `test_monitoring.py` exercise it
  indirectly via `TestClient` for the flows they cover, but things like `_throttle`'s 20/hour
  limit, the gzip-bomb guard in `_gunzip_json`, and the various 404 paths don't appear to have
  direct tests.
- `backend/app/report/charts.py` and `report/builder.py` HTML/SVG output has no snapshot or
  visual test — only verified indirectly through `report.json`/`report.html` being produced
  without exceptions.
- Go: only `internal/extract` and `internal/tally` have test files. `internal/app` (the local
  UI/job runner), `internal/monitor` (scheduled-task logic, due-check), and `internal/upload`
  have none. `internal/monitor/schedule_windows.go` can't be tested at all outside Windows.
- No test runs the connector's actual Windows GUI-subsystem build or its browser-driven
  wizard end to end — only the headless code paths are exercised (by `dev/e2e.sh`, itself
  unverified here).

## Secrets / credentials handling
- No secrets files, `.env`, or credential store exist in this repo — all config is plain OS
  environment variables with defaults (see `architecture.md`). The only credential-like value
  is the bank Basic-auth password (`TC_ADMIN_PASSWORD`) and the per-application one-time code
  / monitoring token.
- Monitoring tokens are generated with `secrets.token_urlsafe(32)`, returned to the client
  exactly once, and stored only as a SHA-256 hash (`store._hash`) — the plaintext token is
  never persisted. One-time codes are stored in plaintext in SQLite but are short-lived
  (`TC_CODE_TTL_HOURS`, default 72h) and single-use (cleared on first successful upload).
- Uploaded Tally bundles (real financial data: ledgers, vouchers, balances) are stored
  unencrypted on disk under `DATA_DIR/applications/...` and in the `reports` table. The
  README's "Before a pilot" section flags this explicitly ("encrypt bundles at rest, set a
  retention and deletion policy, audit log") — none of that is implemented yet.

## Gaps against the product direction
The owner hosts the backend for many banks and MSMEs, with daily refreshes and an MSME-facing
dashboard (see `project.md`, "Product direction"). The code is still a single-bank, monthly,
bank-only tool. Hosting it as-is would put several customers' financial data in one
unpartitioned SQLite DB behind one shared admin login. Don't onboard a second bank until
tenancy and per-tenant access control exist.

## Known TODOs / explicitly-flagged gaps (from README "Before a pilot")
- Replace Basic auth with the bank's SSO.
- Encrypt bundles at rest; define retention/deletion policy; audit log of who viewed which report.
- Legal review of the consent text; Account Aggregator / DPDP Act applicability.
- Validate the Windows scheduled task on real Windows 10/11 as a non-admin user.
- Validate against a real TallyPrime/ERP 9 install (see above).

No other TODO/FIXME/XXX markers were found in the source during this pass (`grep` across
`backend/` and `connector/` for the common markers returned nothing beyond what's listed
above and in the README).
