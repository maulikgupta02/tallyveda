"""The dashboard view-models (ticket 7485bcfc, phase 1): pure functions over
an already-built report, checked against the same mock company as
test_analysis.py."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "dev"))

from test_analysis import AS_OF, bundle_from_model, mock_tally  # noqa: E402

from app.report import dashboard  # noqa: E402
from app.report.builder import build_report  # noqa: E402

EXPECTED_GROUPS = ["overview", "sales", "profit", "recv", "pay", "wc", "debt", "bank", "tax", "data"]


def _report():
    return build_report(bundle_from_model(mock_tally.Company()))


def test_company_view_has_all_groups_in_order():
    cv = dashboard.company_view(_report())
    assert [g["key"] for g in cv["groups"]] == EXPECTED_GROUPS
    assert cv["status"] in ("attention", "watch", "healthy")


def test_company_view_gauges_match_indicator_status():
    report = _report()
    cv = dashboard.company_view(report)
    ind = {i["key"]: i for i in report["indicators"]}
    for g in cv["groups"]:
        for t in g["tiles"]:
            if t["gauge"] is not None:
                assert t["status"] in ("green", "amber", "red", None)


def _flatten(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k, v
            yield from _flatten(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _flatten(v)


def test_msme_view_has_no_bank_language():
    mv = dashboard.msme_view(_report())
    assert [g["key"] for g in mv["groups"]] == ["home", "sales", "cust", "money", "dues"]
    for key, value in _flatten(mv):
        assert key != "status" or value is None, "MSME view must never carry a bank rating/status"
        assert key != "gauge" or value is None, "MSME view must never carry a threshold gauge"
        if isinstance(value, str):
            assert "amber" not in value.lower() and "red flag" not in value.lower()


def test_msme_to_do_has_no_invented_due_dates():
    report = _report()
    todo = dashboard._msme_to_do(report["metrics"])
    # GST/TDS amounts are shown without a date (Tally has no due-date field).
    for item in todo:
        if "GST" in item["title"] or "TDS" in item["title"]:
            assert "date not in Tally" in item["detail"]


def test_portfolio_view_health_counts_and_nodata():
    report = _report()
    apps = [
        {"application": {"id": "a1", "applicant_name": "Shree Ganesh Traders", "last_report_at": "2026-09-27"},
         "report": report, "overdue": False, "new_alerts": []},
        {"application": {"id": "a2", "applicant_name": "No Data Yet", "last_report_at": None},
         "report": None, "overdue": False},
    ]
    pv = dashboard.portfolio_view(apps)
    assert pv["kpi"]["nodata"] == 1
    assert sum(pv["kpi"].values()) == 2
    assert len(pv["rows"]) == 2
    nodata_row = next(r for r in pv["rows"] if r["id"] == "a2")
    assert nodata_row["status"] == "nodata" and nodata_row["sales_series"] is None
    assert pv["sales_monitored"]["ltm"] == report["metrics"]["pl"]["sales"]


def test_borrower_status_health_rule():
    healthy_counts = {"green": 14, "amber": 0, "red": 0, "na": 0}
    watch_counts = {"green": 10, "amber": 3, "red": 0, "na": 1}
    attention_counts = {"green": 10, "amber": 2, "red": 1, "na": 1}
    assert dashboard.borrower_status(healthy_counts, [], overdue=False) == "healthy"
    assert dashboard.borrower_status(watch_counts, [], overdue=False) == "watch"
    assert dashboard.borrower_status(attention_counts, [], overdue=False) == "attention"
    assert dashboard.borrower_status(healthy_counts, [], overdue=True) == "attention"
    assert dashboard.borrower_status(healthy_counts, [{"severity": "high"}], overdue=False) == "attention"
    assert dashboard.borrower_status(healthy_counts, [], overdue=False, has_alert=True) == "watch"
