# Design

There is no shared component library, CSS framework, or build step for styling — every
surface is a single server-rendered HTML file with its own inline `<style>` block using CSS
custom properties. Four such pages exist, and they should stay visually consistent with each
other even though nothing enforces that automatically:

- `backend/app/templates/dashboard.html` — bank's list of applications
- `backend/app/templates/application.html` — one application's history/alerts/monitoring controls
- `backend/app/templates/report.html` — the credit report itself (largest, most visual: charts, tables)
- `connector/internal/app/index.html` — the local 127.0.0.1 page the *applicant* sees on their
  own PC (different audience/tone from the other three, which are bank-internal)

**When adding or changing UI, reuse the tokens and patterns below rather than inventing new
ones.** There's no separate design-tokens file; the `:root` block at the top of each template
*is* the token set — all four files share **one identical `:root` block, copied verbatim**.
When adding a new page, copy that block exactly rather than retyping it; when adding a token
any page needs, add it to the block in all four files at once so they never diverge again.

## Colors
The single `:root` block, identical in `dashboard.html`, `application.html`, `report.html`
and `connector/internal/app/index.html`:
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

## Navigation
Flat, not a SPA: each bank page is its own server route (`/bank`, `/bank/applications/{id}`,
`/bank/reports/{id}`), navigated via plain `<a>` links and HTML form `POST`s with a redirect
back (`RedirectResponse(..., status_code=303)` in `main.py`). The connector's wizard is the
only multi-step flow, and it's steps within one page (`.card`/`.step` sections toggled via
`.hidden`/`.off` classes), not multiple routes.
