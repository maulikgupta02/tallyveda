"""Traffic-light indicators. Thresholds are the bank's policy: edit freely.

Each rule: (key, glossary term, section, getter, green_if, amber_if, fmt).
A value is green if it passes the green test, amber if it passes the amber
test, else red. `None` values are shown as "n/a" and not rated. Labels and
plain-English explanations come from report/glossary.py.
"""

from __future__ import annotations

from ..report.format import days, pct, ratio
from ..report.glossary import term


def _rules():
    return [
        ("revenue_growth", "yoy", "Revenue",
         lambda m: m["revenue_growth"], lambda v: v >= 0.05, lambda v: v >= -0.10, pct),
        ("net_margin", "net_margin", "Profitability",
         lambda m: m["pl"]["net_margin"], lambda v: v >= 0.05, lambda v: v > 0, pct),
        ("top1", "top1", "Concentration",
         lambda m: m["customers"]["top1_share"] if m["customers"]["count"] else None,
         lambda v: v <= 0.20, lambda v: v <= 0.40, pct),
        ("top5", "top5", "Concentration",
         lambda m: m["customers"]["top5_share"] if m["customers"]["count"] else None,
         lambda v: v <= 0.50, lambda v: v <= 0.75, pct),
        ("dso", "dso", "Receivables",
         lambda m: m["working_capital"]["dso"], lambda v: v <= 60, lambda v: v <= 120, days),
        ("recv_90", "recv_90", "Receivables",
         lambda m: m["receivables"]["over_90_share"], lambda v: v <= 0.10, lambda v: v <= 0.25, pct),
        ("dpo", "dpo", "Payables",
         lambda m: m["working_capital"]["dpo"], lambda v: v <= 90, lambda v: v <= 150, days),
        ("ccc", "ccc", "Working capital",
         lambda m: m["working_capital"]["ccc"], lambda v: v <= 90, lambda v: v <= 150, days),
        ("current_ratio", "current_ratio", "Liquidity",
         lambda m: m["bs"]["current_ratio"], lambda v: v >= 1.33, lambda v: v >= 1.0, ratio),
        ("debt_equity", "debt_tnw", "Leverage",
         lambda m: m["bs"]["debt_equity"] if m["bs"]["tangible_net_worth"] > 0 else float("inf"),
         lambda v: v <= 2.0, lambda v: v <= 3.0, ratio),
        ("tol_tnw", "tol_tnw", "Leverage",
         lambda m: m["bs"]["tol_tnw"] if m["bs"]["tangible_net_worth"] > 0 else float("inf"),
         lambda v: v <= 3.0, lambda v: v <= 4.0, ratio),
        ("interest_cover", "icr", "Debt service",
         lambda m: m["coverage"]["interest_coverage"], lambda v: v >= 3.0, lambda v: v >= 1.5, ratio),
        ("dscr", "dscr", "Debt service",
         lambda m: m["coverage"]["dscr"], lambda v: v >= 1.5, lambda v: v >= 1.2, ratio),
        ("cash_receipts", "cash_receipts", "Banking",
         lambda m: m["cash"]["cash_receipt_share"], lambda v: v <= 0.10, lambda v: v <= 0.30, pct),
    ]


def rate(metrics: dict) -> list[dict]:
    out = []
    for key, term_key, section, get, green, amber, fmt in _rules():
        try:
            value = get(metrics)
        except (KeyError, TypeError, ZeroDivisionError):
            value = None
        if value is None:
            status = "na"
        elif green(value):
            status = "green"
        elif amber(value):
            status = "amber"
        else:
            status = "red"
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
        })
    return out
