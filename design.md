# Design

There is no shared component library, CSS framework, or build step for styling — every
surface is a single server-rendered HTML file with its own inline `<style>` block using CSS
custom properties. The pages below should stay visually consistent with each other even though
nothing enforces that automatically:

- `backend/app/templates/dashboard.html` — bank's list of applications
- `backend/app/templates/application.html` — one application's history/alerts/monitoring controls
  (bank-only; also where a bank issues/resets an applicant's MSME login; `readonly=True` —
  passed by the platform admin's `/admin/msmes/{id}/view` — hides the Manage panel and points
  the back-link at `/admin/msmes/{id}` instead of `/bank`, reusing the exact same tabs/markup)
- `backend/app/templates/msme.html` — an MSME's own read-only view of the same application's
  history/trend (same visual language as `application.html`, minus any bank-only controls)
- `backend/app/templates/msme_login.html` — the one-time "here is the username/password"
  confirmation shown to the bank right after creating/resetting an MSME login
- `backend/app/templates/report.html` — the credit report itself (largest, most visual: charts, tables)
- `backend/app/templates/admin_overview.html`, `admin_banks.html`, `admin_bank_detail.html`,
  `admin_msmes.html`, `admin_msme_detail.html`, `admin_users.html`, `admin_audit.html` — the
  platform admin panel (2026-10-02, ticket 2d48bed7), one page per `/admin` route. Same shared
  `:root` block, plus a smaller, admin-specific set of components (dark `.appbar` with a top
  `.tabs-top` nav across all seven pages — Overview/Banks/MSMEs/Users/Audit — a flat-table
  `.pill`/`.tbl` list-and-detail pattern, `details.manage`/`.mpanel` reused verbatim from
  `application.html`'s Manage panel, and `.new-panel` reused from `dashboard.html`'s "+ New
  request" toggle pattern for "+ New bank"/"+ New MSME"). No new design tokens were needed.
- `backend/app/templates/admin_credential.html` — the one-time credential-reveal page for
  anything `/admin` creates or resets a password for (bank users, MSME logins), visually
  identical to `msme_login.html`'s existing pattern, just reusable for any username/password.
- `connector/internal/app/index.html` — the local 127.0.0.1 page the *applicant* sees on their
  own PC (different audience/tone from the other bank/MSME-facing pages)

**Admin-page layout note**: any table whose rows carry more than one or two action buttons
(e.g. "Reset password" / "Disable" / "Delete") needs either a full-width (`s12`) card or a
`.scrollx` wrapper — a half-width (`s6`) card clips action buttons with no way to reach them.
`.scrollx`'s `overflow-x: auto` also needs `min-width: 0` wherever it sits inside a CSS grid
(`.manage-body`'s `display: grid` children default to `min-width: auto`, which lets a wide table
blow out the grid item and overflow the whole page horizontally on narrow viewports otherwise —
the existing `application.html`/`dashboard.html` pages happened not to hit this because their
`.scrollx` tables weren't nested inside a `display: grid` container).

A seventh surface, `backend/app/templates/home.html` (the public marketing home page at
`GET /`), shares the same `:root` color tokens but is otherwise its own thing — see "Marketing
home page" below for why.

**When adding or changing UI, reuse the tokens and patterns below rather than inventing new
ones.** There's no separate design-tokens file; the `:root` block at the top of each template
*is* the token set — all six app templates share **one identical `:root` block, copied
verbatim**. When adding a new page, copy that block exactly rather than retyping it; when
adding a token any page needs, add it to the block in all six files at once so they never
diverge again.

## App shell (2026-10-04, current; supersedes Colors/Typography/Spacing/Components below)
Approved on the design canvas https://claude.ai/artifact/Vdb98GXVhSSTwmSiqRqrZh (five artboards:
connector, bank portfolio, borrower, MSME dashboard, admin MSMEs). Product name: **TallyVeda**,
mark "TV".
- **Shared CSS:** the bank, MSME and admin pages link `backend/app/static/app.css?v=N` and keep
  only page-specific CSS (chart internals) inline. Bump `?v=` when editing it. `home.html`,
  `legal.html` and `report.html` keep their own styles.
- **Shell:** `.shell` = dark navy sidebar `.side` (`--side #14162e`; `.who` with `.mark`, nav
  links with `a.on` and `.cnt`/`.cnt.hot` counts, `.foot`) + `.work` area. Below 900px the
  sidebar turns into a top bar with a scrolling nav row. The MSME dashboard uses `.topbar` +
  `.pills-nav` + `.page` (1280 max) instead, since it has no sidebar.
- **Sidebars:** one source, `templates/_shell.html` (`bank_side`, `admin_side`, icons). Admin
  counts come from `store.nav_counts()` (a Jinja global); bank counts from `main._bank_nav()`.
  Both end with "Sign out" (`/signout`), and the bank one with "Download connector".
- **Page anatomy:** `.head` (h1, `.sub`/`.meta`, `.actions`), then a `.strip` of headline
  numbers in one card, then `.split` (`.main-col` + `.rail` of 320px+) or `.cols`.
- **Palette:** ground `#f6f7fb`, cards white with `--line #e3e6ee`, text `#0f172a`/`#475569`/
  `#64748b`, accent indigo `--accent #4338ca` (solid, no gradients), chart series `#4f46e5` bars
  (`#c7d2fe` for past months) and teal `#0d9488` for last year. Status colours are reserved and
  always carry a word: `.pill.crit/.warn/.good` (with an `<i>` dot) and `.sev` badges
  ("Critical", "Watch").
- **Type:** IBM Plex Sans body, Bricolage Grotesque for h1 and `.stat-v` numbers, self-hosted.
  Table headers and `.sev` are 11–12px uppercase.
- **Buttons:** one `.btn` primary per area; `.btn.ghost` for the rest; `.btn.warn` for
  destructive-but-common (stop updates, suspend); `.btn.danger` for irreversible (delete).
- **Tables:** `.tbl` inside `.scrollx`; numbers right-aligned `.num`; first column `.name`
  (bold name + small ref). Raw enum values are humanised.
- **Connector page** (`connector/internal/app/index.html`): two panes, navy left (mark,
  headline, stepper, trust points, footer) and the current step only on the right. System fonts
  only (it is served offline by the exe).

## Colors
The single `:root` block, identical across all six files above:
```
--surface: #fcfcfb      page background
--surface-2: #f3f2ef    panel/form background (e.g. the "new request" bar)
--card: #ffffff         card background (connector wizard steps; unused elsewhere)
--line: #e2e1dc         borders, table rules, dividers
--text: #0b0b0b         primary text
--text-2: #52514e       secondary/muted text
--muted: #7a7974        tertiary text (report tile captions, chart axis labels)
--accent: #2a78d6       primary action color (buttons, links, chart series)
--accent-ink: #ffffff   text/icon color on a solid --accent fill
--good: #0ca30c         status: ready / green indicator
--warning: #fab219      status: processing / amber indicator
--serious: #ec835a      status: a fourth level between warning and critical
--critical: #d03b3b     status: failed / red indicator
--series-1 / --series-2: #2a78d6 / #eb6834   chart line colors (report.html)
--age-1..--age-6: #86b6ef -> #0d366b          6-step ageing-bucket ramp, light to dark blue (report.html)
```
Most tokens are only *used* by one or two pages (e.g. `--age-*`/`--series-*` only by
`report.html`'s charts, `--card`/`--accent-ink` only by the connector wizard), but every page
carries the full set so the block stays copy-paste identical. Don't trim a page's `:root` down
to just what it uses.

## Typography
One system font stack everywhere, no webfonts: `-apple-system, "Segoe UI", Roboto, Arial,
sans-serif`. Body text is `14px/1.45` on every page. Headings are plain `h1`/`h2` with manual
`font-size` — `h1` is `22px`, `h2` (where used) is `16px` — no heading scale/mixin.

## Spacing / layout
- Single-column, centered `main` with a `max-width` cap and `0 auto` margin: `1040px`
  (dashboard), `960px` (report — narrower, print-oriented), `560px` (connector wizard —
  narrower still, single form flow).
- No grid system; flex is used ad hoc (`display: flex` on forms/status pills).
- Borders/radii: `1px solid var(--line)` for inputs/cards, `border-radius: 6px` on buttons
  and inputs, `8–10px` on panels/cards.
- `box-sizing: border-box` set globally via `* { box-sizing: border-box; }` in every file.

## Components (reuse these patterns, don't invent new ones)
- **Status pill**: `.status` + a colored `<i>` dot — `.status.ready/.processing/.failed` in
  `dashboard.html`. The connector's wizard uses the same step-indicator idea with `.step .num`
  (a numbered circle that turns green with `.ok .num` once a step completes) — same visual
  language (colored circle = state), different markup; keep that pairing if adding a new
  stepped flow.
- **Buttons**: plain `<button>`/`a.btn` with the shared border/radius above; `button.primary`
  (bank pages) / unqualified `button` (connector page, since there's only ever one primary
  action per screen) gets the solid `--accent` fill; `button.secondary` is outline-only.
- **Badges**: `.badge.alert` / `.badge.overdue` (small colored pill, dashboard.html) for
  inline counts/warnings next to a row.
- **Code display**: `.code` — monospace (`ui-monospace, Menlo, Consolas, monospace`),
  letter-spacing, used for the one-time link code in both `dashboard.html` and the
  connector's `input.code`.
- **Charts**: inline SVG built server-side in `backend/app/report/charts.py` — no charting
  library, no client JS. Reuse the `--series-*`/`--age-*` tokens for any new chart so colors
  stay consistent with the rest of the report.

## Styling approach
Inline `<style>` per Jinja2 template, scoped implicitly by being single-page documents (no
CSS modules/scoping tool, no Tailwind/Bootstrap/etc., no CSS-in-JS). All four HTML surfaces
are server-rendered strings (Jinja2 for the backend, a Go `html/template`-free raw file for
the connector's static page) — there is no frontend build step, bundler, or JS framework
anywhere in this repo. The small amount of interactivity on the connector's wizard page is
plain inline/vanilla JS polling the local backend (not shown above the first 60 lines, but
consistent with "no framework" throughout).

## Dashboard directions (ticket 7485bcfc, pending pick)

Three static HTML directions for the bank's two dashboard screens (portfolio overview,
one-company view) and the MSME's own dashboard were produced at
`docs/agents/mockups/7485bcfc/direction-{1,2,3}.html` (gitignored, local only), with
`-desktop.png`/`-mobile.png` screenshots alongside each. All three use the exact shared
`:root` token block unchanged — no new tokens were needed; differences are purely layout,
density and component choice. None is picked yet.

**Direction 1 — Dense Grid Console.** A spreadsheet-density direction for the bank screens:
screen A is a single sortable/filterable table (filter chips above it) with a compact
per-row "indicator health" strip — 14 small coloured squares, one per bank indicator,
giving an at-a-glance shape without taking table width. Screen B is a two-column dense
layout: a wide left column with a full indicator table (value + a tiny inline SVG
sparkline + rating per row) and the red-flags list, and a narrower right rail with data
freshness, a "what changed" timeline and the reports list. Typography and row height stay
close to the existing `dashboard.html`/`application.html` tables — a credit officer scanning
many rows a day. Screen C breaks sharply from that density: large rounded cards, one big
hero number with a trend line, and short plain-language notes — deliberately not using the
dense table style at all, so the MSME owner never sees anything that reads like a ledger.

**Direction 2 — Priority Feed.** Card/feed-based, organised around triage rather than an
alphabetical or table listing. Screen A groups applicant cards into three sections —
"Needs attention now", "Watch", "Up to date" — each card carrying an inline sparkline,
a couple of key metrics, and badges for new alerts/overdue data; a KPI strip above gives the
portfolio-level counts. Screen B keeps a horizontal strip of headline tiles (closest of the
three to the current `report.html` tile pattern) then splits into a "what changed" feed of
icon cards on the left and an indicator list rendered as individual rated cards (not a table)
on the right — every indicator is its own small card with a coloured pill. Screen C mirrors
the same card/pill language in a warmer tone: a centered hero card, customer-share bars
instead of a table, and "watch" cards using the same visual shape as the bank's alert cards
but without any colour grading or bank language — just plain-text notes.

**Direction 3 — Split-Pane Console.** An information-architecture-led direction. Screen A is
a master-detail split: a persistent left rail lists every applicant (name, health dot, new/
overdue badges) with a search box, and the right pane shows portfolio KPIs plus a quick-read
summary of whichever applicant is selected, before linking into the full screen B. Screen B is
a long-form single document with a sticky left-hand section nav (Headline / Indicators / Red
flags / What changed / Data freshness / Reports) — closest to a static version of a filed
credit report, good for a reviewer who wants to read top-to-bottom rather than scan a grid; on
narrow widths the side nav collapses into a horizontal scrollable tab strip. Screen C is a
full-bleed narrative, mobile-first: large section headings ("How you're doing", "Your
customers", "Money coming in", "Your cash", "Things to keep an eye on"), a bottom quick-link
bar for jumping between sections (intended as `position: sticky; bottom: 0` once embedded in
a real scrolling page — screenshotting it that way produced a full-page-capture artifact, so
the mockup renders it as a plain static bar instead; the sticky behaviour itself is a safe
CSS-only interaction that fits the no-JS-framework constraint).

Directions 1-3 were screenshotted at 1440×900 (desktop) and 390×844 (mobile) and visually
reviewed; two markup bugs (a missing space producing run-together text in direction 2's
MSME notes, and un-blocked `<b>` tags in direction 3's timeline causing the same issue) were
found and fixed before finishing.

**Direction 4 — Merged Split-Pane Feed (owner-requested merge of 2 + 3, 2026-10-02).** Per
owner feedback, this direction is not a fourth independent option but a deliberate merge:
screen A takes direction 3's master-detail split pane and groups the left rail using
direction 2's urgency buckets (Needs attention now / Watch / Up to date / No data), each row
carrying new-alert/overdue badges, plus a search box and a sort control; the right pane opens
with direction 2's portfolio KPI strip and closes with the selected company's quick-read as
direction-2-style cards with sparklines. Two new portfolio-level panels were added per the
brief: an exposure-by-region breakdown and an alerts-since-yesterday feed across all
borrowers, plus a data-freshness summary panel. Screen B keeps direction 3's long-form page
with a sticky section nav (collapsing to a horizontal tab strip on mobile) but now opens with
direction 2's "what changed since last refresh" feed before the headline tiles, and every
indicator is rendered as a direction-2-style rated card with a sparkline, grouped into named
sections (Revenue & growth, Profitability & concentration, Receivables, Payables & suppliers,
Working capital & liquidity, Banking & debt service, Statutory & data integrity) instead of
one flat table. Screen C keeps direction 3's mobile-first narrative flow and bottom quick-link
bar, with direction 2's bar charts and warm card tone throughout; still no ratings, traffic
lights, bank thresholds or lending language anywhere on screen C.

*Visual polish applied beyond 1-3*: a shared `.card` component (consistent border, radius,
and a subtle `box-shadow: 0 1px 2px rgba(11,11,11,0.04)` — a literal CSS value, not a new
token, since it is only ever used at that one opacity) for every panel; a consistent
`.section-head` pattern; indicator and sparkline charts now draw a baseline axis and mark the
latest data point with a filled circle in the status colour; numbers use
`font-variant-numeric: tabular-nums` globally for alignment; explicit empty/stale states
("No data yet" rail row, "9 days overdue" badge, stale freshness panel). **No new design
tokens were added** — the existing `:root` block (copied verbatim, unchanged) covered
everything needed; `--accent`/status colours already provided a "highlighted latest point"
colour and card backgrounds needed no new surface tint.

*New parameters shown on screens B/C, split by what the backend already computes vs. what
would need new work* (checked against `backend/app/analysis/{metrics,indicators,redflags,alerts}.py`,
`report/glossary.py` and the connector's `internal/extract/extract.go` /
`internal/tally/requests.go`, which is the ceiling on what Tally data is actually available —
nothing below was invented beyond what the extracted Ledgers/Vouchers/Bills/StockSnapshots can
support):

- **Computed today, just not surfaced in any existing template:** DPO and payables ageing
  (`metrics.ageing(book, "creditor")`), supplier concentration (`metrics.concentration` on
  purchase flows, already returned as `suppliers` from `compute_all`), DIO/inventory days and
  CCC (`working_capital`), current ratio, debt/TNW, TOL/TNW, interest coverage, DSCR, cash
  receipts share, aggregate statutory dues payable (`bs["tax_payable"]`), books-lag red flag.
- **Computed now (backend, ticket 7485bcfc phase 1 — 2026-10-02):** same-month-last-year sales
  comparison and monthly average/range (`metrics.same_month_last_year`, extended
  `metrics.seasonality`); credit notes/returns ratio (`metrics.credit_notes`, by the reserved
  "Credit Note" voucher type); purchases growth (`compute_all`'s `purchases_growth`); customers
  billed/new/lost (already `customers`/`retention`), customers owing count
  (`compute_all`'s `customers_owing`); current assets/liabilities and NWC (`balance_sheet`'s
  `nwc`); TOL/TNW and net worth (already in `bs`); a reconstructed month-end bank balance series
  with average/low(+month)/volatility (`metrics.bank_balance_series`); a monthly GST-collected
  series with a sales-consistency check (`metrics.gst_monthly`); a GST-vs-TDS split of statutory
  dues by ledger-name heuristic (`metrics.statutory_split`); EMI/loan repayment regularity from
  loan-ledger voucher dates (`metrics.emi_regularity` — repayment count and a "late" count per
  loan, on a >35-day-gap heuristic); voucher count (already in `report["data_quality"]`). All of
  these are assembled into the three dashboards' tile/chart shape by the new
  `report/dashboard.py` (`company_view`/`msme_view`/`portfolio_view` — see `architecture.md`).
  Gross margin trend over time and per-customer payment-behaviour trend (best/slowest payers
  over several quarters) were **not** built in this pass — still real candidates for a future
  ticket, not ruled out, just not in phase 1's scope (phase 1 covers the parameters the direction
  6 mockup's tiles/charts actually show; neither of those two is on a d6 screen).
- **New but low-confidence, heuristic only:** related-party transactions
  (`metrics.related_party_candidates`, built 2026-10-02) — Tally's bundle has **no address or
  director field at all** (only ledger name, GSTIN, state), so this is narrower than the
  mockup's "name/address match" framing: it is a name-overlap match against the company's own
  name only, explicitly weaker than the GSTIN+name circular-trading check `redflags.py` already
  does between debtors and creditors. Shown as low confidence, not a red flag.
- **Portfolio-wide rollups (backend, 2026-10-02):** region/health-by-state breakdown from
  `Company.State` (via `report["company"]["state"]`, which the connector already extracts in
  `tally.Companies()`) and the alerts-since-yesterday feed are built in `report/dashboard.py`'s
  `portfolio_view` from each application's latest report plus a caller-supplied `new_alerts`
  list (the diff against the previous report, already computed by `analysis.alerts.compare` at
  upload time) — no new per-company computation, a cross-application rollup only.
- **Not computable from Tally data, excluded from scope:** GST filing regularity/GSTR status.
  Tally's books have no record of whether or when a GST return was filed — that would require
  a separate GSTN integration, which is out of scope for a Tally-only connector. Shown in the
  mockup as "Not available" with that reasoning, not invented.
- **Sector/industry breakdown** (asked for alongside region): Tally has no industry
  classification field either. The mockup includes a region breakdown (by `Company.State`,
  which *is* real) but explicitly notes sector would need a bank-entered tag at onboarding,
  not something derivable from the books.

Screenshotted at 1440×900 and 390×844 and reviewed; the mobile nav/tab strip was double-
checked by cropping the full-page screenshot (the downscaled thumbnail made the horizontal
scroll strip look like it had wrapped onto multiple lines — at full resolution it's a single
scrollable row, as intended).

**Open questions carried into direction 4:**
- "Combined LTM sales monitored" (₹412.6 Cr) is a portfolio KPI invented for the mockup since
  there is no stored "loan exposure/sanctioned amount" field anywhere in the current data model
  (`dashboard.html`'s new-request form has no amount field) — worth deciding whether portfolio
  KPIs should be framed around monitored sales (derivable today) or whether a loan-amount field
  should be added to applications so exposure can be reported in lending terms.
  - Gross margin trend, EMI regularity and bank-balance volatility all read as fairly
  substantial new analysis modules, not small additions — worth scoping each as its own
  backend ticket rather than one big "more indicators" ticket.
- The related-party heuristic's low-confidence framing needs a product decision: should it be
  a red flag (like circular trading), a separate "unverified" section, or dropped until a
  better signal exists?

### Direction 5: Visual dashboards (owner feedback on 1–4: too much text, poor use of space)
`direction-5.html`, generated by `gen_d5.py` in the same folder, so every chart is real SVG
built from one dataset, the way `report/charts.py` will render it server-side. It merges 2 and 3,
replaces sentences with charts, and uses direction 4's parameter scoping (above) unchanged.
- **Layout system:** 12-column CSS grid with 16px gaps (`.s3/.s4/.s5/.s6/.s7/.s8/.s12` spans).
  Cards are white (`--card`) on `--surface` with a 1px `--line` border, 10px radius, 18px
  padding and a header row (`h2` + optional legend/link). Breakpoints are 1180px (KPIs 2-up,
  side panels full width), 900px (single column, tables become cards, bottom nav appears) and
  640px (compact type, phone-sized charts). Each column chart is rendered twice (`.ch-d` and a
  360-wide `.ch-m`), so axis text never shrinks below about 11px on a phone.
- **Charts** follow the dataviz rules: columns at most 24px wide with a 4px rounded top and the
  latest period in `--series-1` (others `--age-1`); last year as a 2px `--series-2` line with
  ringed dots; hairline solid gridlines; one axis; a legend for two series and none for one; a
  label only on the latest value; hover via SVG `<title>`. Stacked bars use 2px surface gaps.
  Status colours appear only with a label or icon. `--series-1`/`--series-2` pass the palette
  validator (normal ΔE 33.6, CVD ΔE 24.7).
- **A, portfolio:** a KPI row of four tiles, each a number plus one visual (health mix stacked bar,
  7-day alert columns, an overdue list, sales sparkline). Then today's alerts (severity icon,
  company, one-line change, time) next to health by state (stacked bar per state). Then a
  full-width borrower table: urgency stripe, health pill, 12-month sparkline, LTM + YoY,
  meters for debtor days and owed > 90 d, alert count and data freshness. On phones the table
  becomes one card per borrower.
- **B, one borrower:** header (name, health pill, freshness, full report), tabs (Overview, Sales,
  Receivables, Payables, Cash & debt, Statutory, History; only Overview is mocked), and a
  "since yesterday" row of change chips. Then the cards:
  - monthly sales: this year's columns against last year's line, plus a facts strip
  - indicators: a green/amber/red mix bar, then one bullet gauge per indicator showing the
    bank's threshold bands with a value marker
  - receivables: an ageing stacked bar on the `--age-*` ramp, plus the largest overdue debtors
  - cash cycle: a stock / collect / pay timeline that resolves to the cycle length, plus
    supplier concentration
  - customer concentration
  - month-end bank balance
  - red flags, EMI regularity (12 squares) and statutory dues
- **C, MSME:** phone-first. Sales this month as the hero number over the same this-year/last-year
  chart; four tiles (to collect, in the bank, stock, due in 30 days); a "to do this week" list
  (call the stuck debtor, TDS/EMI/GST dates and amounts); who buys from you; how fast they pay
  (days bars); busy months as a 12-cell heat strip on the `--age-*` ramp. Bottom nav on phones.
  It shows no ratings, thresholds or lending language.
- **Tokens:** the shared `:root` block, unchanged. Nothing new was added.

### Direction 6: direction 5 + new theme + grouped detailed metrics (owner's pick)
`direction-6.html`, generated by `gen_d6.py` (with `gen_d6_base.py` and `d6_screens.py`).
- **Theme: navy and indigo.** The `:root` block changes in every page at once:
  - surfaces: `--surface #f5f6f8`, `--surface-2 #eceef3`, `--line #e2e5eb`
  - text: `--text #101828`, `--text-2 #475467`, `--muted #667085`
  - accent: `--accent #3b5bdb`
  - one new token, `--brand #111c3a`: the app bar background and the "cash tied up" bar
  - status: `--good #12a150`, `--warning #f2a900`, `--serious #ea6b2d`, `--critical #d92d20`
  - charts: `--series-1 #3b5bdb`, `--series-2 #0fa3a3`
  - ramp: `--age-1..6` indigo, `#c5cff8 → #1b2c75`
  
  Series 2 moved from orange to teal so chart colours never look like the amber or orange status
  colours. The series pair passes the dataviz palette validator (normal ΔE 23.2, CVD ΔE 20.8,
  contrast ≥ 3:1). Health pills are tinted by status (10–16% fill, dark text).
- **Bank company page, grouped into tabs** (radio-input tabs in the mockup; real routes or
  `?tab=` in the build). Every metric is a `.mt` tile: label, value, a one-line context, and
  optionally the bank rating (dot + word), a threshold bullet gauge or a sparkline. Each tab
  has 4–8 tiles plus one or two charts.
  - **Overview:** changes since yesterday, four headline tiles, monthly sales, indicator
    gauges, red flags, reports.
  - **Sales:** LTM, growth, latest month vs the same month last year, monthly average and
    range, seasonality, credit notes / returns, cash sales, customers billed; sales chart and
    customer retention (new / lost).
  - **Profitability:** gross margin (approximate), EBITDA, net margin, interest, depreciation,
    overheads, other income, COGS; margins last year vs this year, and "where each ₹100 goes".
  - **Customers & receivables:** receivables, DSO, > 90 d, collection ratio, top customer,
    top 5, customers owing, stuck debtors; ageing, largest balances, customer shares, days to
    pay by customer.
  - **Suppliers & payables:** payables, DPO, > 90 d, purchases, top supplier, top 5; payables
    ageing and supplier shares.
  - **Working capital:** CCC, DIO, current ratio, NWC, current assets and liabilities; the
    cash-cycle timeline.
  - **Debt:** debt/TNW, TOL/TNW, interest cover, DSCR, debt service, net worth; loans table and
    EMI record.
  - **Banking & cash:** month-end balance, average, lowest, swings, cash receipts share, large
    cash receipts and payments, negative cash days; balance chart and bank vs cash receipts.
  - **Tax:** GST collected, paid and payable, TDS payable, GST vs sales consistency, filing
    status ("not available"); monthly GST chart.
  - **Data & flags:** books lag, last upload, voucher count, Tally company; red flags, checks
    with no findings, low-confidence related party.
- **MSME page, tabs Home / Sales / Customers / Money / Dues.** On phones the tab bar is the
  bottom nav. Home has the hero chart, four tiles, "to do this week" and "areas to watch".
  Each other tab has plain-language tiles and charts. There are still no ratings or lending
  terms.

## Navigation
Flat, not a SPA: each bank page is its own server route (`/bank`, `/bank/applications/{id}`,
`/bank/reports/{id}`), navigated via plain `<a>` links and HTML form `POST`s with a redirect
back (`RedirectResponse(..., status_code=303)` in `main.py`). The MSME side is the same
pattern at `/msme`, `/msme/report` — just a different (narrower) HTTP Basic-auth account and
no write actions, so no forms/redirects there, only links. The connector's wizard is the
only multi-step flow, and it's steps within one page (`.card`/`.step` sections toggled via
`.hidden`/`.off` classes), not multiple routes.


## Marketing home page (ticket 90b7d084)

`backend/app/templates/home.html` (`GET /`) is the public, logged-out marketing site — a
different audience (a bank's credit team or an MSME owner deciding whether to try the
product at all, not someone already using the dashboard) and the only page search engines
are allowed to index (see "SEO" below). It follows an owner-approved mockup rather than
growing out of the six app pages above, so it deliberately diverges from them in two ways,
documented here so a future change doesn't "fix" them back to the app's pattern by mistake:

- **Typography.** The six app pages use one system font stack and no webfonts (see
  "Typography" above). The home page instead self-hosts two Google Fonts as variable woff2
  files under `/static/fonts/` (`bricolage-grotesque.woff2` for headings/display numbers —
  weights 500–700, `ibm-plex-sans.woff2` for body text — weights 400–600), both declared with
  `font-display: swap` and `font-optical-sizing: auto`, with the display font preloaded. This
  matches the approved mockup's look; the app pages' system-font convention is unchanged.
- **Markup style.** The app pages' CSS is class-based (`:root` tokens + named classes, see
  `dashboard.html`). The approved mockup used heavy inline `style="..."` attributes; `home.html`
  keeps the same visuals but converts them to named classes in a `<style>` block (the app
  pages' convention), since that's easier to maintain and is what the rest of this repo does —
  a faithful reproduction of the design, not a literal copy of the mockup's markup.

Colors are **not** a divergence: `home.html` carries the identical direction-6 navy/indigo
`:root` block (including the `--brand` token), so the marketing site and the dashboards read
as the same product.

**Components specific to this page** (all progressive enhancement — every one has a
server-rendered default so the page is complete without JS; a small inline `<script>` at the
end of the template takes over from there):
- **Sample dashboard card** (hero): a 12-bar chart (`.bars`/`.bar`) that plays a one-time
  "rise" animation on load, and a 3-item "Today's alerts" feed (`.feed-list`) that rotates
  through a 5-item pool every ~3.2s. Both skip their animation/rotation under
  `prefers-reduced-motion: reduce` (a blanket `* { animation: none !important }` override in
  that media query, plus the JS simply never starts the rotation interval).
- **"For banks" tab/panel** (`.tablist`/`.panel`): 5 tabs, 6 metric tiles (`.mt`) each. Full
  ARIA tablist pattern (`role="tab"`/`"tabpanel"`, `aria-selected`, roving `tabindex`,
  arrow-key/Home/End navigation) with all 5 panels' data inlined as JSON for the JS to switch
  without a round trip; the server renders the `overview` tab's panel directly in HTML so a
  no-JS visitor still sees real content, just can't switch tabs.
- **Cash-cycle calculator** (`.field` sliders + `.result-card`): three range inputs
  (stock/debtor/creditor days) feeding `ccc = stock + debtor − creditor`, three proportional
  meter bars, and verdict text at the same thresholds as the mockup (≤60d healthy, ≤120d
  stretched, else strained). `app/marketing.py`'s `cash_cycle()` computes the initial
  server-rendered state; the inline JS duplicates the same small formula for instant slider
  feedback (deliberately not a server round trip per keystroke).
- **Lead form** (`#demo`): posts to `POST /api/leads`, JSON via `fetch()` when JS is available
  (shows an inline "Thanks, we will be in touch" message) or a plain form POST otherwise
  (redirects to `/?sent=1#demo`, which renders the same thank-you copy server-side). A hidden
  `website` honeypot field and a per-IP rate limit (`app/main.py`'s `_lead_attempts`, 5/hour)
  guard it; both reject the same way on both paths without revealing which check fired.

Static content specific to the page — chart series, the five tab panels' tiles, the
cash-cycle thresholds, and the FAQ copy (also the source for the FAQPage JSON-LD) — lives in
`backend/app/marketing.py`, not in `home.html` or `main.py`, following the same "small pure
functions, no I/O" convention as `report/dashboard.py`.

### SEO

`config.PUBLIC_URL` (env `TC_PUBLIC_URL`, defaults to the live Render URL) is the one source
for the canonical origin used in `<link rel="canonical">`, Open Graph/Twitter tags, the
JSON-LD `url` fields, `robots.txt`'s `Sitemap:` line and `sitemap.xml` itself — change it in
one place once a real domain is bought (see `project.md`).

- Only `/` is indexable. Every route under `/bank`, `/msme`, `/admin` and `/api` gets
  `X-Robots-Tag: noindex, nofollow` from a blanket middleware in `main.py`
  (`_noindex_private_routes`), and `robots.txt` disallows the same prefixes plus `/download`
  (the connector `.exe`/download redirect — not meant to be crawled either, but not a path a
  stale indexed link would be useless without the home page's context, so it's a robots.txt
  entry only, not a response header).
- JSON-LD (`marketing.json_ld`): `Organization` + `WebSite` + `SoftwareApplication`
  (`BusinessApplication`/`FinanceApplication`, `operatingSystem: Windows`, no invented ratings
  or prices) + `FAQPage`, built directly from the same `marketing.FAQ` list the visible FAQ
  section renders from, so they can't drift apart.
- Icons: `/favicon.ico` (16/32/48px, one file with all three sizes), `/static/icon-192.png`
  (`rel="icon"`, ≥48px per Google's favicon guidance), `/static/favicon.svg` (`rel="icon"`,
  vector), and `/static/apple-touch-icon.png` (180×180, fully opaque, no transparency — iOS
  applies its own mask). All under `/static/`, cache-control is set long
  (`public, max-age=31536000, immutable`, from the same middleware above) except
  `/favicon.ico` itself, served by its own root-level route since browsers probe that exact
  path. **If the icon's design ever changes, publish it under a new filename and update these
  links rather than overwriting the existing files** — Google caches a given icon URL for
  weeks regardless of what's served there now.
- Open Graph image: `/static/og-image.png`, 1200×630, generated once from a small HTML card
  (navy background, the brand mark, the H1) via `npx playwright screenshot` — not hand-drawn,
  not a screenshot of the live page. Regenerate the same way if the hero headline ever changes.
- `robots.txt`, `sitemap.xml` (one `<url>`, the home page, with a `marketing.HOME_LASTMOD`
  constant — bump it when the page's visible content changes meaningfully) and `llms.txt` (a
  plain-text summary for LLM crawlers) are all generated by `main.py`, not static files, so
  they can reuse `config.PUBLIC_URL` and the FAQ data.

## Approved direction (ticket 7485bcfc)
Direction 6 chosen. 6 Owner approved direction 6 (owner said no further approval needed). Build: new theme in all six pages' :root, bank portfolio + company page with grouped metric tabs, MSME tabs. Build it on this device.

**Phase 1 (backend, 2026-10-02): done.** Every metric the d6 tiles/charts need is now computed
(see the parameter list above and `architecture.md`), and `report/dashboard.py` shapes it into
the three view-models (`company_view`/`msme_view`/`portfolio_view`), including the borrower
health rule. No routes or templates were touched — phase 2 wires these view-models into the new
navy/indigo theme and the actual `dashboard.html`/`application.html`/`msme.html` markup.

**Phase 2 (frontend, 2026-10-02): done.** All six pages' `:root` block is now direction 6's navy/
indigo token set, copied verbatim (including the new `--brand` token). `report/dashboard_charts.py`
holds the server-rendered SVG/HTML chart helpers used by the bank/MSME dashboards (sparkline,
column chart with an optional last-year line and a narrower phone variant, stacked bar, bullet
gauge, horizontal bars, an ageing-ramp stack, a health pill and an EMI repayment grid) — all pure
functions, escaping every piece of free text, registered as Jinja globals in `main.py`. They are
distinct from (and reuse the `_nice_max`/scale helpers of) `report/charts.py`, which still renders
`report.html`'s own charts unchanged.
- `/bank` (`dashboard.html`) is now the portfolio view built from `dashboard.portfolio_view` —
  KPI tiles, today's alerts feed, health-by-state, and the borrower table/cards with server-side
  `?filter=attention|watch|alerts|overdue` chips. The old "new request" form is unchanged, now
  behind a "+ New request" toggle.
- `/bank/applications/{id}` (`application.html`) is `dashboard.company_view`'s ten tabs, navigated
  by real `?tab=<key>` links (no JS). Every existing bank control (code issue/reset, monitoring
  refresh/stop, MSME login issue/reset, report history/recompute/view, bundle download) lives in a
  collapsible "Manage" panel above the tabs, unchanged in behaviour. An application with no ready
  report yet shows an empty state instead of the tabs.
- `/msme` (`msme.html`) is `dashboard.msme_view`'s five tabs (Home/Sales/Customers/Money/Dues),
  same `?tab=` pattern, bottom tab bar on phones. `/msme/report` now redirects to `/msme`;
  `/msme/report.json` returns the MSME view-model instead of the bank's report.json.
- **Deviations from the mockup**, all low-risk simplifications given real (not fabricated) data
  constraints:
  - The portfolio's "new alerts, last 7 days" mini bar chart and the sales-monitored sparkline
    were dropped — there's no stored day-by-day alert-count or portfolio-sales history to chart
    from, only the current snapshot. Shown as a plain number instead of inventing a trend.
  - The mockup's per-alert icon set (flag/up/down/clock...) was replaced by the existing plain
    colored-dot status language (`.dot`/`.pill`) already used elsewhere in this app, rather than
    introducing a new icon set outside this doc's component list.
  - Overview's full per-indicator `.ind` list (name + gauge + value for all ten indicators) was
    simplified to the green/amber/red mix bar plus four headline tiles; the other nine tabs each
    surface their own relevant indicators as gauges on their own tiles, so nothing is lost, just
    not duplicated on Overview too.
  - Receivables/Payables tabs show ageing + largest balances only (matching what
    `report/dashboard.py`'s view-model carries) — the mockup's extra "who they sell/buy from" and
    "days to pay by customer" panels on this tab were left for a future ticket rather than
    threading more fields through the view-model for this pass.
  - The cash-cycle timeline's day-0/day-N axis captions were dropped as a minor cosmetic
    simplification; the per-stage day counts are already labelled on each bar.
  - Column charts gained a proper negative-value axis (`vmin`/`vmax` both computed, bars rounding
    whichever end is away from zero) beyond what the mockup needed, since a real borrower's
    reconstructed bank balance can go negative (overdraft) — the mockup's all-positive sample data
    never exercised this path.
  - Wide tables (the loans table, legacy report-history table) scroll horizontally on phones via
    the same `.scrollx` (`overflow-x: auto`) pattern this repo already used pre-direction-6, rather
    than card-ifying every table.
  - Screenshotted and visually reviewed at 1440×900 and 390×844 for the portfolio, every company
    tab and every MSME tab, plus the empty states (no report yet, MSME with no data) and an
    application with a negative bank balance.
