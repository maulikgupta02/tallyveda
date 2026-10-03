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
- **Dashboard login**: `TC_ADMIN_USER`/`TC_ADMIN_PASSWORD` are required. `config.py` refuses
  to start without them, and only `TC_DEV=1` (tests, `dev/e2e.sh`, local runs) allows
  `admin`/`admin` (and, for `/admin`, `platform`/`platform`). Never set `TC_DEV` on a hosted
  deployment, and don't reintroduce a fallback. These two env vars now only *seed* the first
  tenant + bank user on an empty database (`main._ensure_seed_tenant`) — real logins are
  per-tenant rows in `store.py`'s `users` table (bank users scoped to their tenant's
  applications, MSME users scoped to one application, platform admins scoped to nothing),
  provisioned via `python -m app.manage` or `/admin` (2026-10-02, ticket 2d48bed7 — see
  `architecture.md`'s "Platform admin panel"). There's still no password-reset
  *self-service* flow (an admin/platform admin resets it for you, there's no "forgot
  password" email), and no account lockout beyond the existing per-IP throttle.
- **`/admin` is a high blast-radius surface** — a platform admin can suspend any bank
  (logging out every one of its users immediately), delete any MSME's data permanently (typed
  confirmation is the only guard), and disable/delete any user. The only built-in safety net
  is `store.count_active_platform_admins`, which blocks disabling or deleting the *last*
  active platform admin — there is nothing stopping a platform admin from, say, suspending
  every bank in sequence. Treat `/admin` credentials with the same care as root/infra access,
  not as a regular login.
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
- **First real install (2026-10-03) froze Tally at "Reading ledgers"**: the old single ledger
  request asked Tally to compute every ledger's balances at once. Fixed with paced, per-group
  requests (see architecture.md, `internal/extract/`). Still unverified on real Tally: that
  `CHILDOF` with a plain group name is honoured (if not, the fallback reads everything in
  one request, as before), and whether one huge group (thousands of debtors) is still slow
  enough to freeze Tally for a while. `connector.log` now records how long every request took.
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
- `backend/app/main.py`'s connector/bank routes (throttling, upload-size/gzip-bomb guarding)
  still have **no dedicated test file** — `test_analysis.py` and `test_monitoring.py` exercise
  them indirectly via `TestClient` for the flows they cover, but things like `_throttle`'s
  20/hour limit, the gzip-bomb guard in `_gunzip_json`, and the various 404 paths don't appear
  to have direct tests. (`/admin` itself now has dedicated coverage — `test_admin.py`, added
  2026-10-02 — including the CSRF guard, role isolation, the direct tenant, bank suspension,
  and the last-active-platform-admin guard.)
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
  unencrypted at rest — on disk under `DATA_DIR/applications/...` (SQLite) or in the
  `report_files` table (Postgres) — and in the `reports` table either way. The README's
  "Before a pilot" section flags this explicitly ("encrypt bundles at rest, set a retention
  and deletion policy, audit log") — none of that is implemented yet, on either backend.

## Free-tier hosting (Render + Neon)
- **Render's free web service disk is ephemeral** — wiped on every restart, redeploy or
  wake-from-sleep. `DATABASE_URL` (Postgres) must be set there; running it with the default
  SQLite/`TC_DATA_DIR` setup would silently lose every application, report and uploaded
  bundle the first time the instance restarts. `render.yaml` doesn't set `DATABASE_URL` for
  you (`sync: false`) precisely so this can't be deployed by accident without it.
- **Render's free instance sleeps after ~15 minutes idle**, and the first request afterwards
  can take about a minute to wake it. The connector's HTTP client (`connector/internal/upload`)
  already budgets 15 minutes per request and retries a connection failure (not just a slow
  response), so this is expected to self-recover rather than fail outright — but it's only
  been exercised against a local mock of a cold connection refusal, never a real Render sleep
  cycle, so treat it as logically-sound-but-unverified against the real platform.
- **Neon's free tier caps total storage at 0.5 GB** across all of a project's branches. Tally
  bundles can be large (gzip helps, but a company with years of vouchers still adds up); there
  is no pruning/retention policy here (see above), so a free Neon project could fill up with
  no warning from this code — worth monitoring manually before relying on it past a pilot.
- **The Postgres path has been run once against Neon's real pooled endpoint** (a throwaway
  schema, dropped afterward — never against `public`), confirming `prepare_threshold=None`
  and the no-session-state query style work through PgBouncer transaction pooling. It has
  **not** been run against Neon over any real length of time (autosuspend/resume under a
  realistic request pattern, the 0.5 GB cap actually being hit) — treat that as unverified
  until it's been live for a while.

## Gaps against the product direction
The owner hosts the backend for many banks and MSMEs, with daily refreshes and an MSME-facing
dashboard (see `project.md`, "Product direction"). Tenancy, per-bank accounts and a per-application
MSME login now exist (`store.py`'s `tenants`/`users` tables, `/bank` scoped to `tenant_id`, `/msme`
scoped to `application_id` — see `architecture.md`). Refreshes are now due daily (`store.monitoring_due`),
not monthly, and Linux cloud installs have a documented cron/systemd setup (README.md "Linux / cloud
installs") since `schedule_other.go` still has no built-in scheduler. Each refresh still re-extracts
the full requested window rather than only new vouchers — true incremental extraction (only
pulling what changed since the last report) would need the connector/backend contract to change
(partial bundles, merging with the previous snapshot) and was judged too large/risky to bundle into
this pass given `internal/tally/`'s unverified-against-real-Tally status (see below); a human should
decide whether to pursue it, especially for the Day Book performance concern on very large companies.
Still open:
- **Admin web UI resolved** (2026-10-02, ticket 2d48bed7): `/admin` now covers tenant/MSME/user
  management with an audit trail (`audit_log` table) of every action — see
  `architecture.md`'s "Platform admin panel". `python -m app.manage` remains for scripted/
  first-time provisioning.
- No password-reset self-service for bank or MSME users, and no account lockout beyond the
  existing per-IP throttle (20 failed attempts/hour) shared with the connector endpoints. A
  platform admin can reset any user's password from `/admin/users`, but there's still no
  "forgot password" flow a bank/MSME user can trigger themselves.
- This pass has **not been run against a real second tenant in a shared production database** —
  only against SQLite in tests and a local dev run. Treat the isolation as test-verified, not
  field-verified, before onboarding a second paying bank.

## Known TODOs / explicitly-flagged gaps (from README "Before a pilot")
- Replace Basic auth with the bank's SSO.
- Encrypt bundles at rest; define retention/deletion policy; audit log of who viewed which report.
- Legal review of the consent text; Account Aggregator / DPDP Act applicability.
- Validate the Windows scheduled task on real Windows 10/11 as a non-admin user.
- Validate against a real TallyPrime/ERP 9 install (see above).

No other TODO/FIXME/XXX markers were found in the source during this pass (`grep` across
`backend/` and `connector/` for the common markers returned nothing beyond what's listed
above and in the README).
