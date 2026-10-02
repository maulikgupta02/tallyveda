"""Month-on-month early-warning alerts for monitored borrowers.

Compares a new report's snapshot with the previous one. A snapshot is the
small dict stored with every report (see `snapshot`), so comparisons never
need to reload old bundles.
"""

from __future__ import annotations

from ..report.format import inr, pct

RANK = {"green": 0, "amber": 1, "red": 2}
LABEL = {"green": "Good", "amber": "Watch", "red": "Concern"}

# (key, label, threshold, direction): alert when the relative change beats the
# threshold in the bad direction (+1 = an increase is bad).
MOVES = [
    ("sales_ltm", "Revenue (last 12 months)", 0.15, -1),
    ("receivables", "Receivables", 0.30, +1),
    ("receivables_over_90", "Receivables older than 90 days", 0.50, +1),
    ("total_debt", "Total debt", 0.25, +1),
    ("bank_od", "Bank OD / CC utilised", 0.30, +1),
    ("net_worth", "Net worth", 0.20, -1),
]
MIN_ABS_MOVE = 500000  # ignore swings below ₹5 L


def snapshot(report: dict) -> dict:
    m = report["metrics"]
    return {
        "tiles": report["summary"]["headline"],
        "numbers": {
            "sales_ltm": m["pl"]["sales"],
            "net_profit_ltm": m["pl"]["net_profit"],
            "receivables": m["receivables"]["total"],
            "receivables_over_90": m["receivables"]["over_90"],
            "total_debt": m["bs"]["total_debt"],
            "bank_od": m["bs"]["bank_od"],
            "net_worth": m["bs"]["net_worth"],
        },
        "flag_titles": [f["title"] for f in report["flags"] if f["severity"] == "high"],
    }


def compare(prev_indicators: list, prev_snap: dict, indicators: list, snap: dict) -> list[dict]:
    alerts = []
    before = {i["key"]: i for i in prev_indicators}
    for i in indicators:
        p = before.get(i["key"])
        if not p or p["status"] not in RANK or i["status"] not in RANK:
            continue
        if RANK[i["status"]] > RANK[p["status"]]:
            alerts.append({
                "severity": "high" if i["status"] == "red" else "medium",
                "title": f"{i['label']} worsened",
                "detail": f"{LABEL[p['status']]} → {LABEL[i['status']]} ({p['display']} → {i['display']}).",
            })

    old_flags = set(prev_snap.get("flag_titles", []))
    for title in snap.get("flag_titles", []):
        if title not in old_flags:
            alerts.append({"severity": "high", "title": f"New red flag: {title}", "detail": "Not present in the previous report."})

    old, new = prev_snap.get("numbers", {}), snap.get("numbers", {})
    for key, label, threshold, direction in MOVES:
        a, b = old.get(key), new.get(key)
        if a is None or b is None or abs(b - a) < MIN_ABS_MOVE:
            continue
        near_zero = abs(a) < MIN_ABS_MOVE  # a % change off a tiny base is meaningless
        change = float("inf") if near_zero else (b - a) / abs(a)
        if change * direction > threshold or (near_zero and direction * (b - a) > 0):
            alerts.append({
                "severity": "medium",
                "title": f"{label} {'up' if b > a else 'down'} " + ("from near zero" if near_zero else pct(abs(change), 0)),
                "detail": f"{inr(a)} → {inr(b)} since the previous report.",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(alerts, key=lambda x: order[x["severity"]])
