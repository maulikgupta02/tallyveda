"""Analytics checked against the mock company's ground truth.

The bundle is built straight from dev/mock_tally.py's model (no Go, no HTTP),
so these tests pin the backend maths. The full XML path is covered by
dev/e2e.sh.
"""

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dev"))

import mock_tally  # noqa: E402

from app.analysis.book import build_book  # noqa: E402
from app.analysis.metrics import ageing, compute_all  # noqa: E402
from app.report.builder import build_report, render_html, report_json  # noqa: E402

AS_OF = date(2026, 9, 27)


def bundle_from_model(co: mock_tally.Company, as_of: date = AS_OF, months: int = 24) -> dict:
    frm = as_of - timedelta(days=round(months * 30.44) - 1)
    groups = [{"name": n, "parent": p, "reserved_name": n if r else ""} for n, p, r in mock_tally.GROUPS]
    vtypes = [{"name": n, "parent": p or n, "reserved_name": n if r else ""} for n, p, r in mock_tally.VOUCHER_TYPES]
    ledgers = [
        {"name": l["name"], "parent": l["parent"], "opening_balance": l["opening"],
         "closing_balance": co.closing(l["name"], as_of), "state": l.get("state", ""), "gstin": l.get("gstin", "")}
        for l in co.ledgers.values()
    ]
    snaps = [{"as_of": d.isoformat(), "items": [{"name": "all", "closing_value": co.stock_value(d)}]}
             for d in (as_of, as_of - timedelta(days=365), as_of - timedelta(days=730))]
    vouchers = [
        {"guid": v.guid, "master_id": v.master_id, "date": v.date.isoformat(), "type": v.vtype, "number": v.number,
         "party": v.party, "is_cancelled": v.cancelled, "entries": [{"ledger": l, "amount": a} for l, a in v.entries]}
        for v in co.index if frm <= v.date <= as_of
    ]
    return {
        "schema_version": 1, "extracted_at": as_of.isoformat(), "company": {"name": mock_tally.COMPANY},
        "period": {"from": frm.isoformat(), "to": as_of.isoformat()},
        "groups": groups, "voucher_types": vtypes, "ledgers": ledgers, "stock_snapshots": snaps,
        "bills": [], "vouchers": vouchers, "warnings": [],
    }


@pytest.fixture(scope="module")
def model():
    return mock_tally.Company()


@pytest.fixture(scope="module")
def bundle(model):
    return bundle_from_model(model)


@pytest.fixture(scope="module")
def metrics(bundle):
    return compute_all(build_book(bundle))


def test_ledger_classification(bundle):
    book = build_book(bundle)
    assert book.ledgers["Metro Wholesale Mart"].category == "debtor"
    gujarat = next(l for l in book.ledgers.values() if l.state == "Gujarat")
    assert gujarat.category == "debtor" and gujarat.parent == "Outstation Debtors"
    assert book.ledgers["SBI Cash Credit A/c"].category == "bank_od"
    assert book.ledgers["ICICI Term Loan"].category == "secured_loan"
    assert book.ledgers["Profit & Loss A/c"].category == "pl_account"


def test_ltm_sales_match_ledgers(model, metrics):
    start = AS_OF - timedelta(days=364)
    expected = -sum(a for v in model.index if start <= v.date <= AS_OF and not v.cancelled
                    for l, a in v.entries if l.startswith("Sales @"))
    assert metrics["pl"]["sales"] == pytest.approx(expected, rel=1e-9)


def test_receivables_equal_debtor_balances(model, metrics):
    debtors = sum(max(model.closing(c["name"], AS_OF), 0) for c in model.customers)
    assert metrics["receivables"]["total"] == pytest.approx(debtors, abs=1)
    assert metrics["bs"]["debtors"] == pytest.approx(debtors, abs=1)
    assert sum(metrics["receivables"]["buckets"].values()) == pytest.approx(debtors, abs=1)


def test_balance_sheet_balances(model, metrics):
    # Every voucher balances, so net worth (assets - outside liabilities) must
    # equal book equity + profit to date + the change in stock since opening.
    pl_parents = {"Sales Accounts", "Purchase Accounts", "Direct Expenses", "Indirect Expenses",
                  "Direct Incomes", "Indirect Incomes"}
    profit = -sum(model.closing(n, AS_OF) for n, l in model.ledgers.items() if l["parent"] in pl_parents)
    stock_change = model.stock_value(AS_OF) - model.stock_value(model.start - timedelta(days=1))
    bs = metrics["bs"]
    assert bs["net_worth"] == pytest.approx(bs["book_equity"] + profit + stock_change, abs=5)


def test_customer_concentration(metrics):
    c = metrics["customers"]
    assert c["ranked"][0][0] == "Sunrise Retail Pvt Ltd"
    assert 0.15 < c["top1_share"] < 0.45
    assert c["top1_share"] <= c["top5_share"] <= c["top10_share"] <= 1


def test_stopped_payer_is_flagged(bundle, metrics):
    recv = metrics["receivables"]
    assert "Sunrise Retail Pvt Ltd" in recv["no_settlement_90d"]
    # Customers that pay into the CC (bank OD) account are still paying.
    assert "Metro Wholesale Mart" not in recv["no_settlement_90d"]


def test_lost_customers(metrics):
    assert set(metrics["retention"]["lost_parties"]) >= {"Jain Trading Co", "Deccan Supplies"}


def test_fifo_ageing_uses_latest_invoices():
    bundle = {
        "period": {"from": "2026-01-01", "to": "2026-06-30"},
        "groups": [{"name": "Sundry Debtors", "parent": "Current Assets", "reserved_name": "Sundry Debtors"},
                   {"name": "Sales Accounts", "parent": "", "reserved_name": "Sales Accounts"}],
        "ledgers": [{"name": "A", "parent": "Sundry Debtors", "closing_balance": 150},
                    {"name": "Sales", "parent": "Sales Accounts", "closing_balance": -300}],
        "voucher_types": [],
        "vouchers": [
            {"date": d, "type": "Sales", "entries": [{"ledger": "A", "amount": 100}, {"ledger": "Sales", "amount": -100}]}
            for d in ("2026-01-10", "2026-04-15", "2026-06-20")
        ],
    }
    a = ageing(build_book(bundle), "debtor")
    assert a["buckets"]["0-30"] == 100  # 20 Jun invoice
    assert a["buckets"]["61-90"] == 50  # half of 15 Apr invoice
    assert a["buckets"]["91-180"] == 0


def test_flags(bundle, metrics):
    report = build_report(bundle)
    titles = {f["title"] for f in report["flags"]}
    assert "Circular trading risk: parties on both sides" in titles
    assert "Cash receipts of ₹2 L or more (s.269ST)" in titles
    assert "Back-dated entries" in titles
    assert "Window dressing: year-end sales spike" in titles
    assert "Possible fund diversion: large loans and advances given" in titles
    spike = next(f for f in report["flags"] if f["title"] == "Window dressing: year-end sales spike")
    assert "reversed in April" in spike["detail"]


def test_report_renders(bundle):
    report = build_report(bundle)
    html = render_html(report, {"id": "x", "applicant_name": "Test", "reference": "R1"})
    assert "Sunrise Retail Pvt Ltd" in html and "<svg" in html
    assert report_json(report).startswith("{")


def test_empty_bundle_does_not_crash():
    report = build_report({"period": {"from": "2026-01-01", "to": "2026-03-31"}, "groups": [], "ledgers": [],
                           "voucher_types": [], "vouchers": []})
    assert any(f["title"] == "No vouchers found" for f in report["flags"])
    render_html(report)
