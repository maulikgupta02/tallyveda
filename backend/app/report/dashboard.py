"""View-models for the three direction-6 dashboards (bank portfolio, bank
one-borrower, MSME). Pure functions only — no I/O, no template rendering;
`main.py` (phase 2) is expected to load the reports via `store.py` and pass
the resulting dicts in here. See `docs/agents/mockups/7485bcfc/d6_screens.py`
for the screens this feeds and `design.md`'s "Direction 6" section for the
tab/group layout.

A "tile" is the one repeated UI unit across all three dashboards:
{label, value, context, status, gauge, sparkline}. `status` is
green/amber/red/None (bank only — never set on the MSME view). `gauge`, when
present, is {"value", "lo", "hi", "bands"} straight from
`analysis.indicators.rate()` so the gauge widget and the traffic-light status
can never disagree about a threshold.
"""

from __future__ import annotations

from datetime import date

from . import format as fmt


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _month_label(key: str | None) -> str | None:
    """"2026-09" -> "Sep 2026" (same month names report/charts.py uses)."""
    if not key:
        return key
    y, m = key.split("-")
    return f"{_MONTHS[int(m) - 1]} {y}"


def _as_date(value):
    """Reports built fresh carry `date` objects; reports round-tripped through
    `store.load_report_json` carry ISO strings (see report/builder.py's JSON
    encoding) — accept either."""
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value[:10])

# ---------------------------------------------------------------- tiles


def tile(label: str, value: str, context: str = "", *, status: str | None = None,
         gauge: dict | None = None, sparkline: list[float] | None = None) -> dict:
    return {"label": label, "value": value, "context": context, "status": status,
            "gauge": gauge, "sparkline": sparkline}


def _gauge(indicators: dict[str, dict], key: str) -> dict | None:
    i = indicators.get(key)
    if not i or i["value"] is None:
        return None
    return {"value": i["value"], "lo": i["lo"], "hi": i["hi"], "bands": i["bands"]}


def _series(m: dict, key: str) -> list[float]:
    return [r[key] for r in m["monthly"][-12:]]


def _month_labels(m: dict, n: int = 12) -> list[str]:
    return [_month_label(r["month"]) for r in m["monthly"][-n:]]


def _month_keys(m: dict, n: int = 12) -> list[str]:
    return [r["month"] for r in m["monthly"][-n:]]


def _last_year_series(m: dict, key: str, n: int = 12) -> list[float] | None:
    """Values for the n months immediately before the current window, aligned
    1:1 with `_series(m, key)` — only available when the report covers at
    least 2*n months of books; otherwise there is nothing to overlay."""
    monthly = m["monthly"]
    if len(monthly) < 2 * n:
        return None
    return [r[key] for r in monthly[-2 * n:-n]]


def _group(key: str, label: str, tiles: list[dict], **charts) -> dict:
    return {"key": key, "label": label, "tiles": tiles, "charts": charts}


# ---------------------------------------------------------------- bank: one borrower


def company_view(report: dict, previous: dict | None = None) -> dict:
    """Ten tabbed groups for the bank's one-borrower page (direction 6, screen B)."""
    m = report["metrics"]
    ind = {i["key"]: i for i in report["indicators"]}
    flags = report["flags"]
    alerts = report.get("alerts", [])
    counts = report["summary"]["indicator_counts"]
    pl, bs, recv, pay, wc, cash, cov = (
        m["pl"], m["bs"], m["receivables"], m["payables"], m["working_capital"], m["cash"], m["coverage"],
    )
    sales_series = _series(m, "sales")
    sales_months = _month_labels(m)
    sales_ly = _last_year_series(m, "sales")
    same_month = m.get("same_month_last_year")

    overview = _group(
        "overview", "Overview",
        [
            tile("Sales, 12 mo", fmt.inr(pl["sales"]), fmt.pct(m["revenue_growth"]) + " vs last year" if m["revenue_growth"] is not None else "",
                 sparkline=sales_series),
            tile("EBITDA margin", fmt.pct(pl["ebitda_margin"])),
            tile("Debtor days", fmt.days(wc["dso"]), status=ind.get("dso", {}).get("status"), gauge=_gauge(ind, "dso")),
            tile("DSCR", fmt.ratio(cov["dscr"]), status=ind.get("dscr", {}).get("status"), gauge=_gauge(ind, "dscr")),
        ],
        since_yesterday=alerts,
        indicator_mix=counts,
        monthly_sales=sales_series, monthly_sales_months=sales_months, monthly_sales_ly=sales_ly,
        red_flags=flags[:5],
    )

    sales = _group(
        "sales", "Sales",
        [
            tile("Sales, 12 mo", fmt.inr(pl["sales"]), sparkline=sales_series),
            tile("Revenue growth", fmt.pct(m["revenue_growth"]), "year on year",
                 status=ind.get("revenue_growth", {}).get("status"), gauge=_gauge(ind, "revenue_growth")),
            tile(_month_label(same_month["month"]) if same_month else "This month", fmt.inr(same_month["value"]) if same_month else "n/a",
                 fmt.pct(same_month["growth"]) + " vs same month last year" if same_month and same_month["growth"] is not None else ""),
            tile("Monthly average", fmt.inr(m["seasonality"]["average"]),
                 f"range {fmt.inr(m['seasonality']['low'])}–{fmt.inr(m['seasonality']['high'])}" if m["seasonality"]["low"] is not None else ""),
            tile("Seasonality", f"Peak {_month_label(m['seasonality']['peak'])}" if m["seasonality"]["peak"] else "n/a",
                 f"slowest {_month_label(m['seasonality']['trough'])} · variation {fmt.pct(m['seasonality']['cv'], 0)}" if m["seasonality"]["cv"] is not None else ""),
            tile("Credit notes / returns", fmt.pct(m["credit_notes_ratio"]), "of gross billing"),
            tile("Cash sales", fmt.pct(m["cash_sales_share"]), "of sales"),
            tile("Customers billed", str(m["customers"]["count"]),
                 f"+{m['retention']['new']} vs last year" if m["retention"] else ""),
        ],
        monthly_sales=sales_series, monthly_sales_months=sales_months, monthly_sales_ly=sales_ly,
        retention=m["retention"],
    )

    profit = _group(
        "profit", "Profitability",
        [
            tile("Gross margin", fmt.pct(pl["gross_margin"]), "approximate" if not pl["stock_adjusted"] else ""),
            tile("EBITDA", fmt.inr(pl["ebitda"]), fmt.pct(pl["ebitda_margin"]) + " of sales"),
            tile("Net margin", fmt.pct(pl["net_margin"]), fmt.inr(pl["net_profit"]) + " net profit",
                 status=ind.get("net_margin", {}).get("status"), gauge=_gauge(ind, "net_margin")),
            tile("Interest cost", fmt.inr(pl["interest"])),
            tile("Depreciation", fmt.inr(pl["depreciation"])),
            tile("Overheads", fmt.pct((pl["indirect_expense"] / pl["sales"]) if pl["sales"] else None),
                 "indirect expenses / sales"),
            tile("Other income", fmt.inr(pl["indirect_income"])),
            tile("Cost of goods", fmt.inr(pl["cogs"])),
        ],
    )

    receivables = _group(
        "recv", "Customers & receivables",
        [
            tile("Receivables", fmt.inr(recv["total"])),
            tile("Debtor days", fmt.days(wc["dso"]), status=ind.get("dso", {}).get("status"), gauge=_gauge(ind, "dso")),
            tile("Owed > 90 days", fmt.pct(recv["over_90_share"]), fmt.inr(recv["over_90"]),
                 status=ind.get("recv_90", {}).get("status"), gauge=_gauge(ind, "recv_90")),
            tile("Collection ratio", fmt.pct(wc["collection_ratio"])),
            tile("Top customer", fmt.pct(m["customers"]["top1_share"] if m["customers"]["count"] else None),
                 status=ind.get("top1", {}).get("status"), gauge=_gauge(ind, "top1")),
            tile("Top 5 customers", fmt.pct(m["customers"]["top5_share"] if m["customers"]["count"] else None)),
            tile("Customers owing", str(m["customers_owing"])),
            tile("Stuck debtors", str(len(recv["no_settlement_90d"]))),
        ],
        ageing=recv["buckets"],
        largest=[(n, p["outstanding"]) for n, p in
                 sorted(recv["per_party"].items(), key=lambda kv: kv[1]["outstanding"], reverse=True)[:10]],
    )

    payables = _group(
        "pay", "Suppliers & payables",
        [
            tile("Payables", fmt.inr(pay["total"])),
            tile("Creditor days", fmt.days(wc["dpo"]), status=ind.get("dpo", {}).get("status"), gauge=_gauge(ind, "dpo")),
            tile("Owed > 90 days", fmt.pct(pay["over_90_share"]), fmt.inr(pay["over_90"])),
            tile("Purchases, 12 mo", fmt.inr(pl["purchases"]), fmt.pct(m["purchases_growth"]) + " vs last year" if m["purchases_growth"] is not None else ""),
            tile("Top supplier", fmt.pct(m["suppliers"]["top1_share"] if m["suppliers"]["count"] else None)),
            tile("Top 5 suppliers", fmt.pct(m["suppliers"]["top5_share"] if m["suppliers"]["count"] else None)),
        ],
        ageing=pay["buckets"],
        largest=[(n, p["outstanding"]) for n, p in
                 sorted(pay["per_party"].items(), key=lambda kv: kv[1]["outstanding"], reverse=True)[:10]],
    )

    working_capital = _group(
        "wc", "Working capital",
        [
            tile("Cash cycle", fmt.days(wc["ccc"]), status=ind.get("ccc", {}).get("status"), gauge=_gauge(ind, "ccc")),
            tile("Inventory days", fmt.days(wc["dio"]), fmt.inr(bs["stock"]) + " of stock"),
            tile("Current ratio", fmt.ratio(bs["current_ratio"]), status=ind.get("current_ratio", {}).get("status"),
                 gauge=_gauge(ind, "current_ratio")),
            tile("Net working capital", fmt.inr(bs["nwc"])),
            tile("Current assets", fmt.inr(bs["current_assets"])),
            tile("Current liabilities", fmt.inr(bs["current_liabilities"])),
        ],
        cycle={"dio": wc["dio"], "dso": wc["dso"], "dpo": wc["dpo"], "ccc": wc["ccc"]},
    )

    debt = _group(
        "debt", "Debt",
        [
            tile("Debt / net worth", fmt.ratio(bs["debt_equity"]), status=ind.get("debt_equity", {}).get("status"),
                 gauge=_gauge(ind, "debt_equity")),
            tile("Total liabilities / net worth", fmt.ratio(bs["tol_tnw"]), status=ind.get("tol_tnw", {}).get("status"),
                 gauge=_gauge(ind, "tol_tnw")),
            tile("Interest cover", fmt.ratio(cov["interest_coverage"]), status=ind.get("interest_cover", {}).get("status"),
                 gauge=_gauge(ind, "interest_cover")),
            tile("DSCR", fmt.ratio(cov["dscr"]), status=ind.get("dscr", {}).get("status"), gauge=_gauge(ind, "dscr")),
            tile("Debt service, 12 mo", fmt.inr(cov["debt_service"])),
            tile("Net worth", fmt.inr(bs["net_worth"])),
        ],
        loans=m["loans"]["rows"],
        emi=m["emi_regularity"], emi_months=_month_keys(m),
    )

    banking = _group(
        "bank", "Banking & cash",
        [
            tile("Bank balance", fmt.inr(m["bank_balance"]["series"][-1][1]) if m["bank_balance"]["series"] else "n/a",
                 sparkline=[v for _, v in m["bank_balance"]["series"]]),
            tile("Average balance", fmt.inr(m["bank_balance"]["average"])),
            tile("Lowest balance", fmt.inr(m["bank_balance"]["low"]), _month_label(m["bank_balance"]["low_month"]) or ""),
            tile("Balance swings", fmt.pct(m["bank_balance"]["volatility"])),
            tile("Cash receipts", fmt.pct(cash["cash_receipt_share"]), status=ind.get("cash_receipts", {}).get("status"),
                 gauge=_gauge(ind, "cash_receipts")),
            tile("Large cash receipts", str(len(cash["big_cash_receipts"]))),
            tile("Large cash payments", str(len(cash["big_cash_payments"]))),
            tile("Days cash went negative", str(len(cash["negative_cash_days"]))),
        ],
        balance_months=[_month_label(k) for k, _ in m["bank_balance"]["series"][-12:]],
        balance_values=[v for _, v in m["bank_balance"]["series"][-12:]],
    )

    tax = _group(
        "tax", "Tax",
        [
            tile("GST collected, 12 mo", fmt.inr(m["tax"]["output_tax"])),
            tile("GST paid, 12 mo", fmt.inr(m["tax"]["tax_paid"])),
            tile("GST payable now", fmt.inr(m["statutory_split"]["gst_payable"])),
            tile("TDS payable now", fmt.inr(m["statutory_split"]["tds_payable"])),
            tile("GST vs sales", "Consistent" if m["gst_monthly"]["rate_range"] and m["gst_monthly"]["rate_range"]["consistent"]
                 else "Review" if m["gst_monthly"]["rate_range"] else "n/a"),
            tile("GST filing status", "Not available", "needs a GSTN link, not in Tally"),
        ],
        gst_months=[_month_label(k) for k, _ in m["gst_monthly"]["series"][-12:]],
        gst_values=[v for _, v in m["gst_monthly"]["series"][-12:]],
    )

    data = _group(
        "data", "Data & flags",
        [
            tile("Books lag", f"{(_as_date(report['period']['to']) - _as_date(report['data_quality']['last_voucher'])).days} d"
                 if report["data_quality"]["last_voucher"] else "n/a"),
            tile("Vouchers in books", str(report["data_quality"]["vouchers"])),
            tile("Tally company", report["company"].get("name", "n/a")),
        ],
        red_flags=flags,
        related_party=m["related_party"],
    )

    return {
        "status": borrower_status(counts, flags, overdue=False, has_alert=bool(alerts)),
        "groups": [overview, sales, profit, receivables, payables, working_capital, debt, banking, tax, data],
    }


# ---------------------------------------------------------------- MSME


def msme_view(report: dict) -> dict:
    """Home/Sales/Customers/Money/Dues groups — plain language, no ratings,
    thresholds or lending terms anywhere."""
    m = report["metrics"]
    pl, bs, recv, pay, wc, cash = m["pl"], m["bs"], m["receivables"], m["payables"], m["working_capital"], m["cash"]
    sales_series = _series(m, "sales")
    sales_months = _month_labels(m)
    sales_ly = _last_year_series(m, "sales")
    same_month = m.get("same_month_last_year")

    home = _group(
        "home", "Home",
        [
            tile("To collect", fmt.inr(recv["total"]), f"paid in {fmt.days(wc['dso'])} on average" if wc["dso"] else ""),
            tile("In the bank", fmt.inr(m["bank_balance"]["series"][-1][1]) if m["bank_balance"]["series"] else "n/a",
                 sparkline=[v for _, v in m["bank_balance"]["series"]]),
            tile("Stock", fmt.inr(bs["stock"]), f"{fmt.days(wc['dio'])} to sell" if wc["dio"] else ""),
            tile("Due in 30 days", "see to-do list", "GST, TDS, EMI"),
        ],
        hero={"label": f"Sales in {_month_label(same_month['month'])}" if same_month else "Sales", "value": fmt.inr(same_month["value"]) if same_month else fmt.inr(pl["sales"]),
              "delta": fmt.pct(same_month["growth"]) if same_month and same_month["growth"] is not None else None},
        monthly_sales=sales_series, monthly_sales_months=sales_months, monthly_sales_ly=sales_ly,
        to_do=_msme_to_do(m),
        watch=_msme_watch(m),
    )

    sales = _group(
        "sales", "Sales",
        [
            tile("Sales, 12 months", fmt.inr(pl["sales"]), fmt.pct(m["revenue_growth"]) + " vs last year" if m["revenue_growth"] is not None else "",
                 sparkline=sales_series),
            tile("Profit, 12 months", fmt.inr(pl["net_profit"])),
            tile("Gross margin", fmt.pct(pl["gross_margin"])),
            tile("Customers billed", str(m["customers"]["count"]), f"{m['retention']['new']} new this year" if m["retention"] else ""),
        ],
        monthly_sales=sales_series, monthly_sales_months=sales_months, monthly_sales_ly=sales_ly,
        revenue_growth=m["revenue_growth"],
    )

    customers = _group(
        "cust", "Customers",
        [
            tile("Kept customers", fmt.pct(m["retention"]["net_revenue_retention"]) if m["retention"] else "n/a"),
            tile("New customers", str(m["retention"]["new"]) if m["retention"] else "n/a"),
            tile("Lost customers", str(m["retention"]["lost"]) if m["retention"] else "n/a"),
            tile("Returns", fmt.pct(m["credit_notes_ratio"])),
        ],
        who_buys=m["customers"]["ranked"][:10],
    )

    money = _group(
        "money", "Money",
        [
            tile("Stock", fmt.inr(bs["stock"])),
            tile("You owe suppliers", fmt.inr(pay["total"])),
            tile("Cash tied up", fmt.days(wc["ccc"])),
            tile("Loans", fmt.inr(bs["total_debt"]), f"{len(m['loans']['rows'])} loans"),
        ],
        receivables_ageing=recv["buckets"],
        balance_months=[_month_label(k) for k, _ in m["bank_balance"]["series"][-12:]],
        balance_values=[v for _, v in m["bank_balance"]["series"][-12:]],
    )

    dues = _group(
        "dues", "Dues",
        [],
        to_do=_msme_to_do(m),
        emi=m["emi_regularity"], emi_months=_month_keys(m),
    )

    return {"groups": [home, sales, customers, money, dues]}


def _msme_to_do(m: dict) -> list[dict]:
    """Stuck debtors to call, plus GST/TDS/EMI amounts due — without inventing
    a due date where one isn't derivable from the books."""
    items = []
    for name in m["receivables"]["no_settlement_90d"][:5]:
        p = m["receivables"]["per_party"][name]
        items.append({"title": f"Call {name}", "detail": f"{fmt.inr(p['outstanding'])}, unpaid 90+ days",
                      "kind": "call", "amount": fmt.inr(p["outstanding"]), "reason": "Unpaid 90+ days"})
    gst_due = m["statutory_split"]["gst_payable"]
    if gst_due:
        items.append({"title": "Pay GST", "detail": f"{fmt.inr(gst_due)} due (date not in Tally)",
                      "kind": "gst", "amount": fmt.inr(gst_due), "reason": "Due date not in Tally"})
    tds_due = m["statutory_split"]["tds_payable"]
    if tds_due:
        items.append({"title": "Pay TDS", "detail": f"{fmt.inr(tds_due)} due (date not in Tally)",
                      "kind": "tds", "amount": fmt.inr(tds_due), "reason": "Due date not in Tally"})
    for loan in m["emi_regularity"]:
        if loan["late"]:
            items.append({"title": f"Watch {loan['name']} EMI", "detail": f"{loan['late']} late payment(s) in the last 12 months",
                          "kind": "emi", "amount": "", "reason": f"{loan['late']} late payment(s) in the last 12 months"})
    return items


def _msme_watch(m: dict) -> list[dict]:
    areas = []
    if m["working_capital"]["dso"] and m["receivables"]["no_settlement_90d"]:
        areas.append({"title": "Collections slowed" if m["working_capital"]["dso"] > 60 else "Some slow payers",
                      "detail": f"Customers take {fmt.days(m['working_capital']['dso'])} to pay on average"})
    if m["customers"]["count"] and m["customers"]["top1_share"] and m["customers"]["top1_share"] > 0.20:
        top = m["customers"]["ranked"][0][0]
        areas.append({"title": "One big customer", "detail": f"{top} is {fmt.pct(m['customers']['top1_share'])} of your sales"})
    if m["bank_balance"]["low"] is not None and m["bank_balance"]["average"] and m["bank_balance"]["low"] < 0.25 * m["bank_balance"]["average"]:
        areas.append({"title": "Thin cash", "detail": f"Bank balance fell to {fmt.inr(m['bank_balance']['low'])} in {_month_label(m['bank_balance']['low_month'])}"})
    return areas


# ---------------------------------------------------------------- bank: portfolio


def borrower_status(indicator_counts: dict, flags: list[dict], overdue: bool, has_alert: bool = False) -> str:
    """Health rule (documented in docs/agents/architecture.md):
    attention = any red indicator, or a high-severity red flag, or overdue data;
    watch     = 3 or more amber indicators, or a new alert since the last refresh;
    healthy   = anything else that has data at all.
    """
    if overdue or indicator_counts.get("red", 0) > 0 or any(f["severity"] == "high" for f in flags):
        return "attention"
    if indicator_counts.get("amber", 0) >= 3 or has_alert:
        return "watch"
    return "healthy"


def portfolio_view(entries: list[dict]) -> dict:
    """entries: one dict per application —
    {"application": <applications row>, "report": <full report dict or None>,
     "overdue": bool, "new_alerts": list[dict] (since the previous report)}.
    "report" is None when there's no ready report yet (status = nodata).
    """
    rows, by_state = [], {}
    kpi = {"attention": 0, "watch": 0, "healthy": 0, "nodata": 0}
    alerts_feed = []
    data_overdue = []
    total_ltm = total_prior = 0.0

    for e in entries:
        app, report, overdue = e["application"], e.get("report"), e.get("overdue", False)
        new_alerts = e.get("new_alerts", [])
        if not report:
            kpi["nodata"] += 1
            rows.append({"name": app["applicant_name"], "id": app["id"], "status": "nodata",
                         "sales_series": None, "ltm": None, "yoy": None, "dso": None, "recv_90": None,
                         "alert_count": 0, "freshness": "Never" if not app["last_report_at"] else app["last_report_at"]})
            if overdue:
                data_overdue.append({"name": app["applicant_name"], "id": app["id"], "since": app["last_report_at"]})
            continue

        m = report["metrics"]
        counts = report["summary"]["indicator_counts"]
        status = borrower_status(counts, report["flags"], overdue, has_alert=bool(new_alerts))
        kpi[status] += 1
        if overdue:
            data_overdue.append({"name": app["applicant_name"], "id": app["id"], "since": app["last_report_at"]})

        state = report["company"].get("state") or "Not recorded"
        bucket = by_state.setdefault(state, {"attention": 0, "watch": 0, "healthy": 0, "nodata": 0})
        bucket[status] += 1

        ltm = m["pl"]["sales"]
        prior = m["pl_prior"]["sales"] if m["pl_prior"] else None
        total_ltm += ltm
        if prior is not None:
            total_prior += prior

        for a in new_alerts:
            alerts_feed.append({**a, "company": app["applicant_name"], "id": app["id"]})

        rows.append({
            "name": app["applicant_name"], "id": app["id"], "status": status,
            "sales_series": _series(m, "sales"),
            "ltm": ltm, "yoy": m["revenue_growth"],
            "dso": m["working_capital"]["dso"],
            "recv_90": m["receivables"]["over_90_share"],
            "alert_count": len(new_alerts),
            "freshness": app["last_report_at"],
        })

    health_by_state = [{"state": s, **counts} for s, counts in sorted(by_state.items())]
    return {
        "kpi": kpi,
        "alerts_since_yesterday": alerts_feed,
        "data_overdue": data_overdue,
        "sales_monitored": {"ltm": total_ltm, "yoy": (total_ltm - total_prior) / total_prior if total_prior else None},
        "health_by_state": health_by_state,
        "rows": rows,
    }
