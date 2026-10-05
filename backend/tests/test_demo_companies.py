"""The demo companies (dev/demo_companies.py) land where they are meant to,
so the demo exes keep exercising the flows they were built for."""

from datetime import date, timedelta

import pytest

from test_analysis import ROOT  # noqa: F401  (puts dev/ on sys.path)

import demo_companies  # noqa: E402
import mock_tally  # noqa: E402

from app.analysis.book import build_book  # noqa: E402
from app.analysis.indicators import rate  # noqa: E402
from app.analysis.metrics import compute_all  # noqa: E402
from app.analysis.redflags import compute_flags  # noqa: E402

AS_OF = date(2026, 9, 30)


def analyse(key: str):
    co = demo_companies.DemoCompany(demo_companies.PROFILES[key], AS_OF)
    frm = AS_OF - timedelta(days=730)
    bundle = {
        "company": {"name": co.name}, "period": {"from": frm.isoformat(), "to": AS_OF.isoformat()},
        "groups": [{"name": n, "parent": p, "reserved_name": n if r else ""} for n, p, r in mock_tally.GROUPS],
        "voucher_types": [{"name": n, "parent": p or n, "reserved_name": n if r else ""} for n, p, r in mock_tally.VOUCHER_TYPES],
        "ledgers": [{"name": l["name"], "parent": l["parent"], "opening_balance": l["opening"],
                     "closing_balance": co.closing(l["name"], AS_OF), "gstin": l.get("gstin", ""),
                     "reserved_name": l.get("reserved", "")} for l in co.ledgers.values()],
        "vouchers": [{"guid": v.guid, "master_id": v.master_id, "date": v.date.isoformat(), "type": v.vtype,
                      "number": v.number, "party": v.party, "entries": [{"ledger": l, "amount": a} for l, a in v.entries]}
                     for v in co.index if frm <= v.date <= AS_OF],
    }
    book = build_book(bundle)
    m = compute_all(book)
    status = {i["key"]: i["status"] for i in rate(m)}
    flags = {f["title"] for f in compute_flags(book, m, AS_OF)}
    return status, flags


@pytest.mark.parametrize("key", list(demo_companies.PROFILES))
def test_demo_company(key):
    status, flags = analyse(key)
    counts = {s: sum(1 for v in status.values() if v == s) for s in ("green", "amber", "red")}
    if key == "healthy":
        assert counts["red"] == 0 and counts["amber"] == 0 and not flags
    elif key == "stressed":
        assert counts["red"] >= 8
        assert {"Stuck receivables: customers who have stopped paying", "Negative cash-in-hand balance"} <= flags
    elif key == "seasonal":
        assert counts["red"] == 0 and counts["amber"] >= 3
        assert "Cash receipts of ₹2 L or more (s.269ST)" in flags
    elif key == "redflags":
        assert counts["red"] == 0
        assert {"Circular trading risk: parties on both sides", "Window dressing: year-end sales spike",
                "Back-dated entries", "Suspense account balance",
                "Possible fund diversion: large loans and advances given"} <= flags
