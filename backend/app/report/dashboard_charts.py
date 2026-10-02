"""Inline SVG/HTML widgets for the direction-6 dashboards (bank portfolio,
bank company page, MSME page).

These mirror `docs/agents/mockups/7485bcfc/gen_d6_base.py` and `d6_screens.py`
(the approved reference implementation) but render real report data, so every
piece of free text (party/customer names, labels) is escaped. Pure string
builders only — no I/O. Callers (Jinja templates, via `main.py`'s
`templates.globals`) must mark the result `|safe`; everything untrusted
inside is already escaped with `html.escape`.

Chart rules (see design.md "Direction 5/6"): columns at most 24px wide with a
4px rounded top, the latest period highlighted in `--series-1` (others
`--age-1`); an optional last-year line in 2px `--series-2` with ringed dots;
hairline gridlines; a legend only when there are two series; a value label
only on the latest bar; hover via SVG `<title>`.
"""

from __future__ import annotations

from datetime import date
from html import escape as e

from .charts import _nice_max
from .format import inr

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def month_label(key: str) -> str:
    """"2026-09" -> "Sep 2026" (same convention as dashboard._month_label)."""
    if not key:
        return key
    y, m = key.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


STATUS_TOKEN = {"green": "good", "amber": "warning", "red": "critical", "na": "muted"}
STATUS_WORD = {"green": "Green", "amber": "Amber", "red": "Red", "na": "n/a"}
HEALTH = {
    "attention": ("critical", "Attention"),
    "watch": ("warning", "Watch"),
    "healthy": ("good", "Healthy"),
    "nodata": ("muted", "No data"),
}


def health_pill(status: str) -> str:
    tok, label = HEALTH.get(status, ("muted", status.title()))
    return f'<span class="pill p-{e(status)}"><i style="background:var(--{tok})"></i>{e(label)}</span>'


def sparkline(values: list[float], *, width: int = 96, height: int = 28, color: str = "var(--series-1)") -> str:
    values = [v for v in values if v is not None]
    if len(values) < 2:
        return ""
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1
    pad = 4
    n = len(values)
    xs = [pad + i * (width - 2 * pad) / (n - 1) for i in range(n)]
    ys = [height - pad - (v - lo) / rng * (height - 2 * pad) for v in values]
    pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in zip(xs, ys))
    out = [f'<svg class="spark" viewBox="0 0 {width} {height}" width="{width}" height="{height}" aria-hidden="true">']
    out.append(f'<polygon points="{xs[0]:.1f},{height} {pts} {xs[-1]:.1f},{height}" fill="{color}" opacity=".1"/>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" '
               f'stroke-linejoin="round" stroke-linecap="round"/>')
    out.append(f'<circle cx="{xs[-1]:.1f}" cy="{ys[-1]:.1f}" r="3.5" fill="{color}" stroke="var(--card)" stroke-width="2"/>')
    out.append("</svg>")
    return "".join(out)


def _signed_bar_path(x: float, y_zero: float, y_val: float, w: float, r: float = 4) -> str:
    """Rounds the corner farthest from the zero line — the bar's "tip" —
    whichever side of zero it falls on (bank balances can go negative)."""
    top_y, bottom_y = min(y_zero, y_val), max(y_zero, y_val)
    r = min(r, w / 2, bottom_y - top_y)
    if r <= 0:
        return f"M{x:.1f},{top_y:.1f} H{x + w:.1f} V{bottom_y:.1f} H{x:.1f} Z"
    if y_val < y_zero:  # positive: round the top
        return (f"M{x:.1f},{bottom_y:.1f} V{top_y + r:.1f} Q{x:.1f},{top_y:.1f} {x + r:.1f},{top_y:.1f} "
                f"H{x + w - r:.1f} Q{x + w:.1f},{top_y:.1f} {x + w:.1f},{top_y + r:.1f} V{bottom_y:.1f} Z")
    # negative: round the bottom
    return (f"M{x:.1f},{top_y:.1f} V{bottom_y - r:.1f} Q{x:.1f},{bottom_y:.1f} {x + r:.1f},{bottom_y:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{bottom_y:.1f} {x + w:.1f},{bottom_y - r:.1f} V{top_y:.1f} Z")


def _columns(values: list[float], labels: list[str], *, last_year: list[float] | None, width: int, height: int,
             unit_fmt, tips: list[str] | None) -> str:
    left, right, top, bottom = 48, 8, 12, 26
    plot_w, plot_h = width - left - right, height - top - bottom
    n = len(values)
    if n == 0:
        return ""
    all_values = [v for v in values if v is not None] + [v for v in (last_year or []) if v is not None]
    highest = max(all_values + [0])
    lowest = min(all_values + [0])
    vmax = _nice_max(highest) if highest > 0 else 0.0
    vmin = -_nice_max(-lowest) if lowest < 0 else 0.0
    vspan = (vmax - vmin) or 1
    band = plot_w / n
    bar_w = min(24, band * 0.56)

    def y(v: float) -> float:
        return top + plot_h - (v - vmin) / vspan * plot_h

    out = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img">']
    for i in range(5):
        v = vmin + (vmax - vmin) * i / 4
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" class="grid"/>')
        out.append(f'<text x="{left - 6}" y="{y(v) + 4:.1f}" class="axis tick" text-anchor="end">{e(unit_fmt(v))}</text>')
    zero_y = y(0)
    for i, v in enumerate(values):
        if v is None:
            continue
        cx = left + band * i + band / 2
        x0 = cx - bar_w / 2
        last = i == n - 1
        fill = "var(--series-1)" if last else "var(--age-1)"
        label = tips[i] if tips else f"{labels[i]}: {unit_fmt(v)}"
        out.append(f'<g class="hit"><rect x="{cx - band / 2:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" fill="transparent"/>'
                   f'<path d="{_signed_bar_path(x0, zero_y, y(v), bar_w)}" style="fill:{fill}"/><title>{e(label)}</title></g>')
        if i % 2 == (1 if n > 12 else 0) or n <= 12:
            out.append(f'<text x="{cx:.1f}" y="{height - 8}" class="axis tick" text-anchor="middle">{e(labels[i])}</text>')
        if last:
            label_y = y(v) - 6 if v >= 0 else y(v) + 14
            out.append(f'<text x="{cx:.1f}" y="{label_y:.1f}" class="val" text-anchor="middle">{e(unit_fmt(v))}</text>')
    if last_year:
        pts_xy = [(left + band * i + band / 2, y(v)) for i, v in enumerate(last_year) if v is not None]
        if len(pts_xy) >= 2:
            pts = " ".join(f"{x:.1f},{yy:.1f}" for x, yy in pts_xy)
            out.append(f'<polyline points="{pts}" fill="none" stroke="var(--series-2)" stroke-width="2" stroke-linejoin="round"/>')
        for i, v in enumerate(last_year):
            if v is None:
                continue
            cx = left + band * i + band / 2
            out.append(f'<circle cx="{cx:.1f}" cy="{y(v):.1f}" r="4" fill="var(--series-2)" stroke="var(--card)" stroke-width="2">'
                       f'<title>{e(labels[i])} last year: {e(unit_fmt(v))}</title></circle>')
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{top + plot_h:.1f}" y2="{top + plot_h:.1f}" class="baseline"/>')
    out.append("</svg>")
    return "".join(out)


def monthly_columns(values: list[float], labels: list[str], *, last_year: list[float] | None = None,
                     width: int = 640, height: int = 220, unit_fmt=inr, tips: list[str] | None = None) -> str:
    """One column per period (desktop size). `last_year`, when given, overlays a
    2px line (same length as `values`; use None entries for months with no
    year-earlier data)."""
    return _columns(values, labels, last_year=last_year, width=width, height=height, unit_fmt=unit_fmt, tips=tips)


def monthly_columns_responsive(values: list[float], labels: list[str], *, last_year: list[float] | None = None,
                                width: int = 640, height: int = 220, unit_fmt=inr, tips: list[str] | None = None) -> str:
    """Desktop chart plus a 360-wide phone chart (design.md: axis text must
    never shrink below ~11px on a phone, so the phone chart is drawn at its
    own narrower viewBox rather than scaled down)."""
    desktop = _columns(values, labels, last_year=last_year, width=width, height=height, unit_fmt=unit_fmt, tips=tips)
    mobile_labels = [l[:1] for l in labels]
    mobile = _columns(values, mobile_labels, last_year=last_year, width=360, height=round(height * 0.9),
                       unit_fmt=unit_fmt, tips=tips)
    return f'<div class="ch-d">{desktop}</div><div class="ch-m">{mobile}</div>'


def stacked_bar(parts: list[tuple[float, str, str]], *, height: int = 12, gap: int = 2) -> str:
    """parts: (value, css colour, title). `title` is shown verbatim as the
    segment's hover text, so callers pre-format the value (count, ₹, %...).
    Rendered as flex so gaps are real surface gaps, not overlapping borders."""
    segs = "".join(
        f'<span style="flex:{v} 1 0;background:{c}" title="{e(title)}"></span>'
        for v, c, title in parts if v)
    return f'<div class="stack" style="height:{height}px;gap:{gap}px">{segs}</div>'


def bullet_gauge(value: float, lo: float, hi: float, bands: list[tuple[float, float, str]], *, label: str = "") -> str:
    """bands: list of (start, end, "green"|"amber"|"red") in data units."""
    def pct(v: float) -> float:
        return (min(max(v, lo), hi) - lo) / (hi - lo) * 100 if hi > lo else 0

    segs = "".join(
        f'<span class="band b-{STATUS_TOKEN.get(st, "muted")}" style="left:{pct(a):.1f}%;width:{max(pct(b) - pct(a), 0):.1f}%"></span>'
        for a, b, st in bands)
    return (f'<div class="bullet" title="{e(label)}">{segs}'
            f'<span class="mark" style="left:{pct(value):.1f}%"></span></div>')


def horizontal_bars(rows: list[tuple[str, float]], *, unit: str = "%", color: str = "var(--series-1)",
                     vmax: float | None = None, value_fmt=None) -> str:
    """rows: (name, value). `value_fmt`, when given, formats the displayed
    value (e.g. `format.inr`); otherwise the raw number plus `unit` is shown."""
    rows = [r for r in rows if r[1] is not None]
    if not rows:
        return ""
    vmax = vmax or max(v for _, v in rows) or 1
    out = []
    for name, v in rows:
        display = value_fmt(v) if value_fmt else f"{v:g}{unit}"
        out.append(f'<div class="hb"><span class="hb-name">{e(name)}</span>'
                   f'<span class="hb-track"><span class="hb-fill" style="width:{min(v / vmax * 100, 100):.1f}%;background:{color}"></span></span>'
                   f'<span class="hb-val num">{e(display)}</span></div>')
    return "".join(out)


def ageing_stack(buckets: dict[str, float]) -> tuple[str, str]:
    """Returns (bar_html, legend_html) for a receivables/payables ageing
    breakdown on the `--age-*` ramp (design.md's ordinal young->old ramp)."""
    items = list(buckets.items())
    bar = stacked_bar([(v, f"var(--age-{i + 1})", f"{k}: {inr(v)}") for i, (k, v) in enumerate(items)], height=18)
    legend = "".join(
        f'<span><i style="background:var(--age-{i + 1})"></i>{e(k)} <b class="num">{e(inr(v))}</b></span>'
        for i, (k, v) in enumerate(items))
    return bar, legend


def _as_date(d):
    """`emi_regularity`'s dates are `date` objects fresh off `build_report`,
    but ISO strings once a report has round-tripped through `report.json`
    (see `report/builder.py`'s JSON encoding) — accept either."""
    return d if isinstance(d, date) else date.fromisoformat(str(d)[:10])


def emi_grid(loans: list[dict], months: list[str]) -> str:
    """One square per calendar month: red if any loan had a late repayment
    that month (a gap of more than 35 days since the previous instalment),
    green if any loan had an on-time repayment, grey otherwise."""
    late_months, paid_months = set(), set()
    for loan in loans:
        dates = sorted(_as_date(d) for d in (loan.get("dates") or []))
        for i, d in enumerate(dates):
            key = f"{d.year:04d}-{d.month:02d}"
            late = i > 0 and (d - dates[i - 1]).days > 35
            (late_months if late else paid_months).add(key)
    out = []
    for m in months:
        cls = "late" if m in late_months else ("" if m in paid_months else "na")
        title = "Late repayment" if m in late_months else ("On time" if m in paid_months else "No repayment")
        out.append(f'<span class="emi {cls}" title="{e(m)}: {e(title)}"></span>')
    return "".join(out)
