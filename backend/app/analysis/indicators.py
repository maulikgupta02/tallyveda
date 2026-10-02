"""Traffic-light indicators. Thresholds are the bank's policy: edit freely.

Each rule: (key, glossary term, section, getter, direction, green_at,
amber_at, lo, hi, fmt). `direction` is +1 when a higher value is better
(green if value >= green_at, amber if >= amber_at, else red) or -1 when
lower is better (green if value <= green_at, amber if <= amber_at, else
red). `lo`/`hi` only bound the gauge widget the dashboard view-model draws
(see report/dashboard.py) — they don't affect the red/amber/green call.
`None` is shown as "n/a" and not rated. Labels and plain-English
explanations come from report/glossary.py.
"""

from __future__ import annotations

from ..report.format import days, pct, ratio
from ..report.glossary import term


def _rules():
    return [
        ("revenue_growth", "yoy", "Revenue",
         lambda m: m["revenue_growth"], 1, 0.05, -0.10, -0.30, 0.50, pct),
        ("net_margin", "net_margin", "Profitability",
         lambda m: m["pl"]["net_margin"], 1, 0.05, 0, -0.10, 0.20, pct),
        ("top1", "top1", "Concentration",
         lambda m: m["customers"]["top1_share"] if m["customers"]["count"] else None,
         -1, 0.20, 0.40, 0, 0.60, pct),
        ("top5", "top5", "Concentration",
         lambda m: m["customers"]["top5_share"] if m["customers"]["count"] else None,
         -1, 0.50, 0.75, 0, 1.0, pct),
        ("dso", "dso", "Receivables",
         lambda m: m["working_capital"]["dso"], -1, 60, 120, 0, 180, days),
        ("recv_90", "recv_90", "Receivables",
         lambda m: m["receivables"]["over_90_share"], -1, 0.10, 0.25, 0, 0.50, pct),
        ("dpo", "dpo", "Payables",
         lambda m: m["working_capital"]["dpo"], -1, 90, 150, 0, 200, days),
        ("ccc", "ccc", "Working capital",
         lambda m: m["working_capital"]["ccc"], -1, 90, 150, 0, 200, days),
        ("current_ratio", "current_ratio", "Liquidity",
         lambda m: m["bs"]["current_ratio"], 1, 1.33, 1.0, 0.3, 2.5, ratio),
        ("debt_equity", "debt_tnw", "Leverage",
         lambda m: m["bs"]["debt_equity"] if m["bs"]["tangible_net_worth"] > 0 else float("inf"),
         -1, 2.0, 3.0, 0, 5.0, ratio),
        ("tol_tnw", "tol_tnw", "Leverage",
         lambda m: m["bs"]["tol_tnw"] if m["bs"]["tangible_net_worth"] > 0 else float("inf"),
         -1, 3.0, 4.0, 0, 6.0, ratio),
        ("interest_cover", "icr", "Debt service",
         lambda m: m["coverage"]["interest_coverage"], 1, 3.0, 1.5, 0, 5.0, ratio),
        ("dscr", "dscr", "Debt service",
         lambda m: m["coverage"]["dscr"], 1, 1.5, 1.2, 0.5, 2.5, ratio),
        ("cash_receipts", "cash_receipts", "Banking",
         lambda m: m["cash"]["cash_receipt_share"], -1, 0.10, 0.30, 0, 0.60, pct),
    ]


# Rules whose amber test is strict (value must exceed amber_at): a net margin of exactly zero is red.
_STRICT_AMBER = {"net_margin"}


def _bands(direction: int, green_at: float, amber_at: float, lo: float, hi: float) -> list[tuple[float, float, str]]:
    if direction == 1:
        return [(lo, amber_at, "red"), (amber_at, green_at, "amber"), (green_at, hi, "green")]
    return [(lo, green_at, "green"), (green_at, amber_at, "amber"), (amber_at, hi, "red")]


def rate(metrics: dict) -> list[dict]:
    out = []
    for key, term_key, section, get, direction, green_at, amber_at, lo, hi, fmt in _rules():
        try:
            value = get(metrics)
        except (KeyError, TypeError, ZeroDivisionError):
            value = None
        if value is None:
            status = "na"
        elif value == float("inf"):
            status = "red"
        elif direction == 1:
            amber_ok = value > amber_at if key in _STRICT_AMBER else value >= amber_at
            status = "green" if value >= green_at else "amber" if amber_ok else "red"
        else:
            status = "green" if value <= green_at else "amber" if value <= amber_at else "red"
        t = term(term_key)
        out.append({
            "key": key,
            "label": f"{t['abbr']} · {t['name']}",
            "plain": t["plain"],
            "formula": t["formula"],
            "section": section,
            "value": None if value in (None, float("inf")) else value,
            "display": "negative net worth" if value == float("inf") else fmt(value),
            "status": status,
            "lo": lo,
            "hi": hi,
            "bands": _bands(direction, green_at, amber_at, lo, hi),
        })
    return out
