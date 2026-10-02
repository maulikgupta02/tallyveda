"""Inline SVG charts for the report (no JS, prints cleanly).

Colours are CSS custom properties defined in report.html so light/dark and
print share one definition. Hover tooltips use SVG <title>.
"""

from __future__ import annotations

from html import escape

from .format import inr

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _nice_max(v: float) -> float:
    if v <= 0:
        return 1.0
    import math

    exp = 10 ** math.floor(math.log10(v))
    for m in (1, 2, 2.5, 5, 10):
        if v <= m * exp:
            return m * exp
    return 10 * exp


def _bar_path(x: float, y: float, w: float, h: float, r: float = 4) -> str:
    """Column with rounded top (data end), square at the baseline."""
    r = min(r, w / 2, h)
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} V{y + h:.1f} Z"
    )


def monthly_columns(rows: list[dict], series: list[tuple[str, str, str]], height: int = 220) -> str:
    """Grouped columns per month. series = [(key, label, css var)]."""
    if not rows:
        return ""
    width = 760
    left, right, top, bottom = 56, 8, 12, 28
    plot_w, plot_h = width - left - right, height - top - bottom
    vmax = _nice_max(max((r[k] for r in rows for k, _, _ in series), default=0))
    band = plot_w / len(rows)
    bar_w = min(12, (band - 6) / len(series) - 2)
    out = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    for i in range(5):
        v = vmax * i / 4
        y = top + plot_h - plot_h * i / 4
        out.append(f'<line x1="{left}" x2="{width - right}" y1="{y:.1f}" y2="{y:.1f}" class="grid"/>')
        out.append(f'<text x="{left - 6}" y="{y + 4:.1f}" class="axis" text-anchor="end">{escape(inr(v))}</text>')
    group_w = len(series) * bar_w + (len(series) - 1) * 2
    for idx, r in enumerate(rows):
        x0 = left + band * idx + (band - group_w) / 2
        y_, m_ = r["month"].split("-")
        label = f"{MONTHS[int(m_) - 1]} {y_}"
        tip = label + "\n" + "\n".join(f"{lbl}: {inr(r[k])}" for k, lbl, _ in series)
        out.append(f"<g><title>{escape(tip)}</title>")
        out.append(f'<rect x="{left + band * idx:.1f}" y="{top}" width="{band:.1f}" height="{plot_h}" class="hit"/>')
        for s_idx, (k, _, var) in enumerate(series):
            h = plot_h * max(r[k], 0) / vmax
            if h > 0.5:
                x = x0 + s_idx * (bar_w + 2)
                out.append(f'<path d="{_bar_path(x, top + plot_h - h, bar_w, h)}" style="fill:var({var})"/>')
        out.append("</g>")
        if m_ in ("01", "04", "07", "10") or len(rows) <= 12:
            out.append(
                f'<text x="{left + band * idx + band / 2:.1f}" y="{height - 8}" class="axis" text-anchor="middle">'
                f"{MONTHS[int(m_) - 1]} {y_[2:]}</text>"
            )
    out.append(f'<line x1="{left}" x2="{width - right}" y1="{top + plot_h}" y2="{top + plot_h}" class="baseline"/>')
    out.append("</svg>")
    legend = "".join(
        f'<span class="key"><i style="background:var({var})"></i>{escape(lbl)}</span>' for _, lbl, var in series
    )
    return f'<figure class="viz"><div class="legend">{legend}</div>{"".join(out)}</figure>'


AGE_VARS = ["--age-1", "--age-2", "--age-3", "--age-4", "--age-5", "--age-6"]


def ageing_bar(buckets: dict[str, float]) -> str:
    """One horizontal stacked bar of ageing buckets (ordinal ramp, young -> old)."""
    total = sum(v for v in buckets.values() if v > 0)
    if total <= 0:
        return ""
    width, height = 760, 30
    x = 0.0
    parts = [f'<svg viewBox="0 0 {width} {height}" role="img" class="chart">']
    items = [(k, v) for k, v in buckets.items()]
    for i, (label, value) in enumerate(items):
        if value <= 0:
            continue
        w = width * value / total
        gap = 2 if x + w < width - 0.5 else 0
        parts.append(
            f'<rect x="{x:.1f}" y="4" width="{max(w - gap, 0.5):.1f}" height="22" rx="0" '
            f'style="fill:var({AGE_VARS[i]})"><title>{escape(label)} days: {escape(inr(value))} '
            f"({value / total:.0%})</title></rect>"
        )
        x += w
    parts.append("</svg>")
    legend = "".join(
        f'<span class="key"><i style="background:var({AGE_VARS[i]})"></i>{escape(k)} d</span>'
        for i, (k, _) in enumerate(items)
    )
    return f'<figure class="viz"><div class="legend">{legend}</div>{"".join(parts)}</figure>'
