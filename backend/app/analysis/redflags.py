"""Data-integrity and behavioural red flags.

Each flag is a dict: {"severity": "high"|"medium"|"low", "title", "detail"}.
They are prompts for the credit officer to ask questions, not verdicts.
"""

from __future__ import annotations

import re
from datetime import date

from .book import Book, month_key

LEGAL_SUFFIXES = re.compile(
    r"\b(private|pvt|limited|ltd|llp|co|company|and|&|m/s|ms|the|enterprises?|traders?)\b"
)


def _norm_party(name: str) -> str:
    n = LEGAL_SUFFIXES.sub(" ", name.lower())
    return re.sub(r"[^a-z0-9]", "", n)


def _inr(x: float) -> str:
    from ..report.format import inr

    return inr(x)


def compute_flags(book: Book, m: dict, extracted_at: date | None) -> list[dict]:
    flags: list[dict] = []
    add = lambda sev, title, detail: flags.append({"severity": sev, "title": title, "detail": detail})
    as_of = book.as_of
    sales_total = m["pl"]["sales"]

    # Books freshness
    if book.vouchers:
        last = book.vouchers[-1].date
        lag = (as_of - last).days
        if lag > 45:
            add("high", "Books not up to date", f"Last voucher is dated {last:%d %b %Y}, {lag} days before the report date.")
        elif lag > 15:
            add("medium", "Books may be lagging", f"Last voucher is dated {last:%d %b %Y}, {lag} days before the report date.")
    else:
        add("high", "No vouchers found", "No accounting vouchers were found in the extracted period.")

    # Back-dated entries: vouchers created (MasterID order) after vouchers
    # dated more than 30 days later.
    ordered = sorted((v for v in book.vouchers if v.master_id), key=lambda v: v.master_id)
    running_max = None
    backdated_count, backdated_value, worst = 0, 0.0, 0
    for v in ordered:
        if running_max and (running_max - v.date).days > 30:
            backdated_count += 1
            backdated_value += sum(e.amount for e in v.entries if e.amount > 0)
            worst = max(worst, (running_max - v.date).days)
        running_max = max(running_max or v.date, v.date)
    if backdated_count:
        total_value = sum(e.amount for v in book.vouchers for e in v.entries if e.amount > 0) or 1
        share = backdated_value / total_value
        sev = "high" if share > 0.05 else "medium" if share > 0.01 else "low"
        add(sev, "Back-dated entries",
            f"{backdated_count} vouchers ({share:.1%} of transaction value) were entered after later-dated vouchers; "
            f"the largest gap is {worst} days. Common around audits, year end or loan applications.")

    if extracted_at:
        future = [v for v in book.vouchers if v.date > extracted_at]
        if future:
            add("medium", "Future-dated vouchers", f"{len(future)} vouchers are dated after the extraction date.")

    # Year-end sales spike and reversal in the following month (Indian FY: March close).
    series = {r["month"]: r["sales"] for r in m["monthly"]}
    for year in sorted({int(k[:4]) for k in series}):
        march = series.get(f"{year}-03")
        others = [series[k] for k in series if f"{year - 1}-04" <= k <= f"{year}-02"]
        if march and len(others) >= 6:
            avg = sum(others) / len(others)
            if avg > 0 and march > 1.6 * avg:
                april_returns = sum(
                    v.total("sales") for v in book.vouchers
                    if month_key(v.date) == f"{year}-04" and v.total("sales") > 0
                )
                detail = f"March {year} sales were {march / avg:.1f}× the average month of that year."
                if april_returns > 0.1 * march:
                    detail += f" {_inr(april_returns)} of sales were reversed in April {year}."
                add("high" if april_returns > 0.1 * march else "medium", "Window dressing: year-end sales spike", detail)

    # Round-figure invoices
    big_invoices = [
        e.amount for v in book.vouchers if v.total("sales") < 0
        for e in v.entries if e.ledger.category == "debtor" and e.amount >= 100000
    ]
    if len(big_invoices) >= 20:
        round_share = sum(1 for a in big_invoices if abs(a % 10000) < 0.5) / len(big_invoices)
        if round_share > 0.15:
            add("medium", "Round-figure invoices (possible accommodation entries)",
                f"{round_share:.0%} of invoices above ₹1 L are exact multiples of ₹10,000. "
                "Tax-inclusive invoices are rarely round; this can indicate estimated or accommodation entries.")

    # Same party on both sides: possible circular trading.
    debtor_names = {}
    for l in book.by_category("debtor"):
        debtor_names[_norm_party(l.name)] = l
        if l.gstin:
            debtor_names[l.gstin.upper()] = l
    ltm_sales = m["ltm_sales_by_party"]
    both = []
    for l in book.by_category("creditor"):
        match = debtor_names.get(l.gstin.upper()) if l.gstin else None
        match = match or debtor_names.get(_norm_party(l.name))
        if match and ltm_sales.get(match.name, 0) > 0:
            both.append((match.name, ltm_sales[match.name]))
    if both:
        value = sum(v for _, v in both)
        share = value / sales_total if sales_total else 0
        add("high" if share > 0.1 else "medium", "Circular trading risk: parties on both sides",
            f"{len(both)} {'party' if len(both) == 1 else 'parties'} ({share:.1%} of sales) also {'appears' if len(both) == 1 else 'appear'} as suppliers: "
            + ", ".join(n for n, _ in sorted(both, key=lambda x: -x[1])[:5])
            + ". Check for circular or related-party trading.")

    # Cash
    cash = m["cash"]
    if cash["negative_cash_days"]:
        first = cash["negative_cash_days"][0]
        add("high", "Negative cash-in-hand balance",
            f"Cash-in-hand went negative on {len(cash['negative_cash_days'])} days (first {first[0]:%d %b %Y}, {first[1]}). "
            "Physically impossible; indicates missing or fabricated entries.")
    if cash["big_cash_receipts"]:
        total = sum(x[2] for x in cash["big_cash_receipts"])
        add("medium", "Cash receipts of ₹2 L or more (s.269ST)",
            f"{len(cash['big_cash_receipts'])} receipts totalling {_inr(total)} in the last 12 months "
            "(Income Tax Act s.269ST prohibits cash receipts of ₹2 L or more).")
    if len(cash["big_cash_payments"]) >= 5:
        total = sum(x[2] for x in cash["big_cash_payments"])
        add("low", "Cash payments above ₹10,000 (s.40A(3))",
            f"{len(cash['big_cash_payments'])} cash payments totalling {_inr(total)} (disallowable under s.40A(3)).")

    # Receivables stuck
    recv = m["receivables"]
    if recv["total"] > 0 and recv["no_settlement_90d"]:
        stuck_value = sum(recv["per_party"][p]["outstanding"] for p in recv["no_settlement_90d"])
        share = stuck_value / recv["total"]
        if share > 0.1:
            add("high" if share > 0.25 else "medium", "Stuck receivables: customers who have stopped paying",
                f"{len(recv['no_settlement_90d'])} customer{'s' if len(recv['no_settlement_90d']) != 1 else ''} owing {_inr(stuck_value)} ({share:.0%} of receivables) "
                "have made no payment in 90 days.")

    bs = m["bs"]
    if abs(bs.get("suspense", 0)) > 0.005 * max(bs["total_assets"], 1):
        add("medium", "Suspense account balance", f"{_inr(bs['suspense'])} is parked in suspense.")
    if bs["net_worth"] > 0 and bs["loans_advances_given"] > 0.25 * bs["net_worth"]:
        add("medium", "Possible fund diversion: large loans and advances given",
            f"{_inr(bs['loans_advances_given'])} given as loans/advances ({bs['loans_advances_given'] / bs['net_worth']:.0%} of net worth). "
            "Check for diversion of funds to group or related parties.")
    if bs["net_worth"] <= 0:
        add("high", "Negative net worth (TNW < 0)", f"Liabilities exceed assets by {_inr(-bs['net_worth'])}.")

    if book.unbalanced_vouchers:
        add("low", "Unbalanced vouchers", f"{book.unbalanced_vouchers} vouchers have debits ≠ credits in the extract.")
    if book.cancelled_vouchers > max(20, 0.03 * (len(book.vouchers) or 1)):
        add("low", "High number of cancelled vouchers", f"{book.cancelled_vouchers} vouchers were cancelled.")

    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(flags, key=lambda f: order[f["severity"]])
