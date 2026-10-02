"""Bundle -> report dict -> HTML."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..analysis.book import build_book
from ..analysis.indicators import rate
from ..analysis.metrics import compute_all
from ..analysis.redflags import compute_flags
from . import charts, format as fmt
from .glossary import TERMS, all_terms, term

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
_env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
_env.filters.update(inr=fmt.inr, pct=fmt.pct, days=fmt.days, ratio=fmt.ratio)
_env.globals["T"] = {k: term(k) for k in TERMS}
_env.globals["glossary"] = all_terms()
_env.filters["inr_full"] = lambda x: fmt.inr(x, precise=True)
_env.filters["dmy"] = lambda d: d.strftime("%d %b %Y") if isinstance(d, (date, datetime)) else (
    date.fromisoformat(d[:10]).strftime("%d %b %Y") if d else "n/a"
)

REPORT_VERSION = 1


def build_report(bundle: dict) -> dict:
    book = build_book(bundle)
    metrics = compute_all(book)
    extracted = bundle.get("extracted_at")
    extracted_on = date.fromisoformat(extracted[:10]) if extracted else None
    flags = compute_flags(book, metrics, extracted_on)
    indicators = rate(metrics)

    recv, pay = metrics["receivables"], metrics["payables"]
    total_recv = recv["total"] or 1
    top_customers = []
    for party, value in metrics["customers"]["ranked"][:15]:
        p = recv["per_party"].get(party, {})
        top_customers.append({
            "name": party,
            "sales": value,
            "share": value / metrics["customers"]["total"] if metrics["customers"]["total"] else None,
            "outstanding": p.get("outstanding", 0.0),
            "over_90": p.get("over_90", 0.0),
            "last_payment": p.get("last_settlement"),
            "state": book.ledgers[party].state if party in book.ledgers else "",
        })
    top_debtors = sorted(
        ({"name": n, **p, "share": p["outstanding"] / total_recv} for n, p in recv["per_party"].items()),
        key=lambda r: r["outstanding"], reverse=True,
    )[:10]
    top_creditors = sorted(
        ({"name": n, **p} for n, p in pay["per_party"].items()),
        key=lambda r: r["outstanding"], reverse=True,
    )[:10]
    top_suppliers = [
        {"name": n, "purchases": v, "share": v / metrics["suppliers"]["total"] if metrics["suppliers"]["total"] else None}
        for n, v in metrics["suppliers"]["ranked"][:10]
    ]

    counts = {s: sum(1 for i in indicators if i["status"] == s) for s in ("green", "amber", "red", "na")}
    high_flags = sum(1 for f in flags if f["severity"] == "high")

    vouchers = book.vouchers
    return {
        "report_version": REPORT_VERSION,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "company": book.company,
        "period": {"from": book.period_from, "to": book.period_to},
        "extracted_at": extracted,
        "connector_version": bundle.get("connector_version"),
        "summary": {
            "indicator_counts": counts,
            "high_flags": high_flags,
            "headline": [
                ("Revenue (last 12 months)", fmt.inr(metrics["pl"]["sales"])),
                ("Revenue growth", fmt.pct(metrics["revenue_growth"])),
                ("Net profit (last 12 months)", fmt.inr(metrics["pl"]["net_profit"])),
                ("Receivables", fmt.inr(recv["total"])),
                ("Net worth", fmt.inr(metrics["bs"]["net_worth"])),
                ("Total debt", fmt.inr(metrics["bs"]["total_debt"])),
            ],
        },
        "indicators": indicators,
        "flags": flags,
        "metrics": metrics,
        "top_customers": top_customers,
        "top_debtors": top_debtors,
        "top_creditors": top_creditors,
        "top_suppliers": top_suppliers,
        "data_quality": {
            "vouchers": len(vouchers),
            "first_voucher": vouchers[0].date if vouchers else None,
            "last_voucher": vouchers[-1].date if vouchers else None,
            "ledgers": len(book.ledgers),
            "customers": len(book.by_category("debtor")),
            "suppliers": len(book.by_category("creditor")),
            "cancelled": book.cancelled_vouchers,
            "excluded": book.excluded_vouchers,
            "unbalanced": book.unbalanced_vouchers,
            "stock_snapshots": sorted(book.stock_snapshots),
            "bills": len(book.bills),
            "warnings": book.warnings,
        },
    }


def render_html(report: dict, application: dict | None = None) -> str:
    m = report["metrics"]
    chart_sales = charts.monthly_columns(
        m["monthly"],
        [("sales", "Sales (net of GST)", "--series-1"), ("collections", "Collections from customers", "--series-2")],
    )
    chart_purchases = charts.monthly_columns(
        m["monthly"],
        [("purchases", "Purchases", "--series-1"), ("bank_credits", "All bank credits", "--series-2")],
        height=180,
    )
    return _env.get_template("report.html").render(
        r=report,
        m=m,
        app=application or {},
        chart_sales=chart_sales,
        chart_purchases=chart_purchases,
        chart_recv=charts.ageing_bar(m["receivables"]["buckets"]),
        chart_pay=charts.ageing_bar(m["payables"]["buckets"]),
    )


def _json_default(o):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(type(o).__name__)


def report_json(report: dict) -> str:
    # ltm_sales_by_party is a working table; ranked lists already carry it.
    slim = dict(report)
    slim["metrics"] = {k: v for k, v in report["metrics"].items() if k != "ltm_sales_by_party"}
    return json.dumps(slim, default=_json_default, ensure_ascii=False)
