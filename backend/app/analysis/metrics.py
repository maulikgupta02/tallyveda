"""Credit metrics computed from a `Book`.

Everything here is derived from the books alone. Where a number is an
approximation (FIFO ageing, DSCR, interest detection by ledger name) the result
carries a note so the credit officer knows how it was produced.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .book import LIQUID_CATEGORIES, Book, Voucher, month_key, month_range, windows

AGE_BUCKETS = [(30, "0-30"), (60, "31-60"), (90, "61-90"), (180, "91-180"), (365, "181-365")]
OLDEST_BUCKET = ">365"
CASH_SALES = "(Cash / counter sales)"
UNATTRIBUTED = "(Not linked to a party)"


def _ratio(num: float, den: float) -> float | None:
    return num / den if den else None


def _bucket(age_days: int) -> str:
    for limit, label in AGE_BUCKETS:
        if age_days <= limit:
            return label
    return OLDEST_BUCKET


def _empty_buckets() -> dict[str, float]:
    return {label: 0.0 for _, label in AGE_BUCKETS} | {OLDEST_BUCKET: 0.0}


def _is_contra(v: Voucher) -> bool:
    return all(e.ledger.category in LIQUID_CATEGORIES for e in v.entries)


# ---------------------------------------------------------------- sales flows


def party_flows(book: Book):
    """Per-voucher attribution of sales/purchases to customers/suppliers.

    Works off ledger groups rather than voucher types, so custom voucher types
    ("GST Sales", "Export Invoice") and credit/debit notes are handled without
    configuration.
    """
    sales, purchases = [], []  # (date, party, net value, gross billed)
    for v in book.vouchers:
        sale = -v.total("sales")
        if abs(sale) > 0.005:
            parties = [e for e in v.entries if e.ledger.category == "debtor"]
            weight = sum(abs(e.amount) for e in parties)
            if weight:
                for e in parties:
                    share = abs(e.amount) / weight
                    sales.append((v.date, e.ledger.name, sale * share, e.amount))
            else:
                label = CASH_SALES if v.has(*LIQUID_CATEGORIES) else UNATTRIBUTED
                sales.append((v.date, label, sale, 0.0))
        buy = v.total("purchase")
        if abs(buy) > 0.005:
            parties = [e for e in v.entries if e.ledger.category == "creditor"]
            weight = sum(abs(e.amount) for e in parties)
            if weight:
                for e in parties:
                    share = abs(e.amount) / weight
                    purchases.append((v.date, e.ledger.name, buy * share, -e.amount))
            else:
                label = "(Cash purchases)" if v.has(*LIQUID_CATEGORIES) else UNATTRIBUTED
                purchases.append((v.date, label, buy, 0.0))
    return sales, purchases


def _sum_by_party(rows, start: date, end: date):
    out = defaultdict(float)
    for d, party, value, _ in rows:
        if start <= d <= end:
            out[party] += value
    return out


def _gross_billed(rows, start: date, end: date) -> float:
    return sum(g for d, _, _, g in rows if start <= d <= end)


def concentration(values: dict[str, float], real_party=lambda p: True) -> dict:
    positive = {p: v for p, v in values.items() if v > 0}
    total = sum(positive.values())
    ranked = sorted(positive.items(), key=lambda kv: kv[1], reverse=True)
    named = [(p, v) for p, v in ranked if real_party(p)]
    shares = [v / total for _, v in named] if total else []
    cumulative, parties_for_80 = 0.0, 0
    for s in shares:
        cumulative += s
        parties_for_80 += 1
        if cumulative >= 0.8:
            break
    return {
        "total": total,
        "count": len(named),
        "top1_share": sum(shares[:1]),
        "top5_share": sum(shares[:5]),
        "top10_share": sum(shares[:10]),
        "hhi": round(sum((s * 100) ** 2 for s in shares)),
        "parties_for_80pct": parties_for_80,
        "ranked": ranked,
    }


# ------------------------------------------------------------------- ageing


def ageing(book: Book, category: str) -> dict:
    """Outstanding ageing for debtors (dr balances) or creditors (cr balances).

    Uses Tally's bill-wise outstanding for ledgers that have it, otherwise
    FIFO: the balance is assumed to be made up of the most recent invoices.
    """
    sign = 1 if category == "debtor" else -1
    as_of = book.as_of
    bills_by_ledger = defaultdict(list)
    for b in book.bills:
        bills_by_ledger[b["ledger"]].append(b)

    invoices = defaultdict(list)  # ledger -> [(date, amount in balance direction)]
    last_settlement = {}
    for v in book.vouchers:
        for e in v.entries:
            if e.ledger.category != category:
                continue
            signed = e.amount * sign
            if signed > 0:
                invoices[e.ledger.name].append((v.date, signed))
            elif signed < 0 and v.has(*LIQUID_CATEGORIES):
                last_settlement[e.ledger.name] = v.date

    totals = _empty_buckets()
    per_party = {}
    advances = 0.0
    bill_wise_ledgers = 0
    for ledger in book.by_category(category):
        balance = ledger.closing * sign
        if balance < -0.5:
            advances += -balance
            continue
        if balance <= 0.5:
            continue
        buckets = _empty_buckets()
        bills = bills_by_ledger.get(ledger.name)
        if bills:
            bill_wise_ledgers += 1
            for b in bills:
                amount = float(b.get("closing_balance") or 0) * sign
                bill_date = date.fromisoformat(b["date"][:10]) if b.get("date") else book.period_from
                buckets[_bucket((as_of - bill_date).days)] += amount
        else:
            remaining = balance
            for d, amount in sorted(invoices.get(ledger.name, []), reverse=True):
                take = min(remaining, amount)
                buckets[_bucket((as_of - d).days)] += take
                remaining -= take
                if remaining <= 0.005:
                    break
            if remaining > 0.005:
                # Older than the extracted period: came from the opening balance.
                buckets[OLDEST_BUCKET] += remaining
        for k, v in buckets.items():
            totals[k] += v
        per_party[ledger.name] = {
            "outstanding": sum(buckets.values()),
            "buckets": buckets,
            "over_90": sum(v for k, v in buckets.items() if k in ("91-180", "181-365", OLDEST_BUCKET)),
            "last_settlement": last_settlement.get(ledger.name),
        }

    total = sum(totals.values())
    over_90 = totals["91-180"] + totals["181-365"] + totals[OLDEST_BUCKET]
    stuck = [
        name
        for name, p in per_party.items()
        if p["outstanding"] > 0
        and (p["last_settlement"] is None or (as_of - p["last_settlement"]).days > 90)
    ]
    return {
        "total": total,
        "buckets": totals,
        "over_90": over_90,
        "over_90_share": _ratio(over_90, total),
        "per_party": per_party,
        "advances": advances,
        "no_settlement_90d": stuck,
        "method": "bill-wise" if bill_wise_ledgers and bill_wise_ledgers == len(per_party)
        else "mixed (bill-wise + FIFO)" if bill_wise_ledgers
        else "FIFO on invoices",
    }


# ---------------------------------------------------------------- P&L


INTEREST_WORDS = ("interest",)
DEPRECIATION_WORDS = ("depreciation", "amortisation", "amortization")


def profit_and_loss(book: Book, start: date, end: date) -> dict:
    t = defaultdict(float)
    interest = depreciation = 0.0
    for v in book.entries_between(start, end):
        for e in v.entries:
            cat = e.ledger.category
            if cat in ("sales", "direct_income", "indirect_income"):
                t[cat] += -e.amount
            elif cat in ("purchase", "direct_expense", "indirect_expense"):
                t[cat] += e.amount
                name = e.ledger.name.lower()
                if any(w in name for w in INTEREST_WORDS):
                    interest += e.amount
                elif any(w in name for w in DEPRECIATION_WORDS):
                    depreciation += e.amount
    opening_stock = book.stock_value(start - timedelta(days=1))
    closing_stock = book.stock_value(end)
    stock_known = opening_stock is not None and closing_stock is not None
    stock_change = (closing_stock - opening_stock) if stock_known else 0.0
    cogs = t["purchase"] + t["direct_expense"] - stock_change
    gross = t["sales"] + t["direct_income"] - cogs
    net = gross + t["indirect_income"] - t["indirect_expense"]
    ebitda = net + interest + depreciation
    return {
        "sales": t["sales"],
        "direct_income": t["direct_income"],
        "indirect_income": t["indirect_income"],
        "purchases": t["purchase"],
        "direct_expense": t["direct_expense"],
        "indirect_expense": t["indirect_expense"],
        "opening_stock": opening_stock,
        "closing_stock": closing_stock,
        "stock_adjusted": stock_known,
        "cogs": cogs,
        "gross_profit": gross,
        "net_profit": net,
        "interest": interest,
        "depreciation": depreciation,
        "ebitda": ebitda,
        "gross_margin": _ratio(gross, t["sales"]),
        "net_margin": _ratio(net, t["sales"]),
        "ebitda_margin": _ratio(ebitda, t["sales"]),
    }


# ------------------------------------------------------------ balance sheet


def balance_sheet(book: Book) -> dict:
    """Position at the period end from ledger closing balances (debit-positive)."""
    b = defaultdict(float)
    for l in book.ledgers.values():
        c, bal = l.category, l.closing
        if c in ("sales", "purchase", "direct_expense", "indirect_expense", "direct_income", "indirect_income"):
            continue
        if c in ("cash", "bank"):
            if bal >= 0:
                b["cash_bank"] += bal
            else:
                b["bank_od"] += -bal  # overdrawn current account
        elif c == "bank_od":
            b["bank_od"] += max(-bal, 0)
            b["cash_bank"] += max(bal, 0)
        elif c == "debtor":
            b["debtors"] += max(bal, 0)
            b["customer_advances"] += max(-bal, 0)
        elif c == "creditor":
            b["creditors"] += max(-bal, 0)
            b["supplier_advances"] += max(bal, 0)
        elif c == "stock":
            b["stock_ledgers"] += bal
        elif c in ("advance", "deposit"):
            b["loans_advances_given"] += bal
        elif c == "current_asset":
            b["other_current_assets"] += bal
        elif c == "fixed_asset":
            b["fixed_assets"] += bal
        elif c == "investment":
            b["investments"] += bal
        elif c == "misc_asset":
            b["misc_assets"] += bal
        elif c == "tax":
            b["tax_payable"] += -bal
        elif c == "provision":
            b["provisions"] += -bal
        elif c == "current_liability":
            b["other_current_liabilities"] += -bal
        elif c == "secured_loan":
            b["secured_loans"] += -bal
        elif c in ("unsecured_loan", "loan"):
            b["unsecured_loans"] += -bal
        elif c in ("capital", "reserves", "pl_account"):
            b["book_equity"] += -bal
        elif c == "suspense":
            b["suspense"] += bal
        else:
            b["other_net"] += bal

    inventory_stock = book.stock_value(book.as_of)
    b["stock"] = inventory_stock if inventory_stock else max(b["stock_ledgers"], 0)

    current_assets = (
        b["cash_bank"] + b["debtors"] + b["stock"] + b["supplier_advances"]
        + b["loans_advances_given"] + b["other_current_assets"]
    )
    current_liabilities = (
        b["creditors"] + b["customer_advances"] + b["tax_payable"] + b["provisions"]
        + b["other_current_liabilities"] + b["bank_od"]
    )
    total_assets = current_assets + b["fixed_assets"] + b["investments"] + b["misc_assets"] + b["suspense"] + max(b["other_net"], 0)
    debt = b["secured_loans"] + b["unsecured_loans"] + b["bank_od"]
    outside_liabilities = current_liabilities + b["secured_loans"] + b["unsecured_loans"] + max(-b["other_net"], 0)
    # Net worth as assets less outside liabilities: this includes the current
    # year's profit, which Tally does not post to any ledger until year end.
    net_worth = total_assets - outside_liabilities
    tangible_net_worth = net_worth - b["misc_assets"]
    return dict(b) | {
        "current_assets": current_assets,
        "current_liabilities": current_liabilities,
        "total_assets": total_assets,
        "total_debt": debt,
        "outside_liabilities": outside_liabilities,
        "net_worth": net_worth,
        "tangible_net_worth": tangible_net_worth,
        "current_ratio": _ratio(current_assets, current_liabilities),
        "quick_ratio": _ratio(current_assets - b["stock"], current_liabilities),
        "debt_equity": _ratio(debt, tangible_net_worth) if tangible_net_worth > 0 else None,
        "tol_tnw": _ratio(outside_liabilities, tangible_net_worth) if tangible_net_worth > 0 else None,
    }


# ------------------------------------------------------------ cash & banking


def cash_and_banking(book: Book, start: date, end: date) -> dict:
    monthly = defaultdict(lambda: defaultdict(float))
    receipts_bank = receipts_cash = 0.0
    big_cash_receipts, big_cash_payments = [], []
    for v in book.vouchers:
        if _is_contra(v):
            continue
        in_window = start <= v.date <= end
        key = month_key(v.date)
        bank_in = sum(e.amount for e in v.entries if e.ledger.category in ("bank", "bank_od") and e.amount > 0)
        bank_out = -sum(e.amount for e in v.entries if e.ledger.category in ("bank", "bank_od") and e.amount < 0)
        monthly[key]["bank_credits"] += bank_in
        monthly[key]["bank_debits"] += bank_out
        from_debtors = -sum(e.amount for e in v.entries if e.ledger.category == "debtor" and e.amount < 0)
        cash = v.total("cash")
        if from_debtors > 0:
            monthly[key]["collections"] += from_debtors
            if in_window:
                if cash > 0:
                    receipts_cash += min(cash, from_debtors)
                    if cash >= 200000:
                        big_cash_receipts.append((v.date, v.party or v.number, cash))
                if bank_in > 0:
                    receipts_bank += min(bank_in, from_debtors)
        to_parties = sum(e.amount for e in v.entries if e.ledger.category in ("creditor", "indirect_expense", "direct_expense") and e.amount > 0)
        if in_window and cash < -10000 and to_parties > 0:
            big_cash_payments.append((v.date, v.party or v.number, -cash))

    # Running cash-in-hand balance: a cash ledger going credit means cash was
    # paid out that the books say didn't exist.
    negative_cash_days = []
    for ledger in book.by_category("cash"):
        movement = sum(e.amount for v in book.vouchers for e in v.entries if e.ledger is ledger)
        running = ledger.closing - movement
        daily = defaultdict(float)
        for v in book.vouchers:
            for e in v.entries:
                if e.ledger is ledger:
                    daily[v.date] += e.amount
        lowest = None
        for d in sorted(daily):
            running += daily[d]
            if running < -1:
                negative_cash_days.append((d, ledger.name, running))
                lowest = min(lowest or running, running)

    received = receipts_bank + receipts_cash
    return {
        "monthly": monthly,
        "cash_receipt_share": _ratio(receipts_cash, received),
        "receipts_bank": receipts_bank,
        "receipts_cash": receipts_cash,
        "big_cash_receipts": big_cash_receipts,
        "big_cash_payments": big_cash_payments,
        "negative_cash_days": negative_cash_days,
    }


# ------------------------------------------------------------------ loans


def loans(book: Book, start: date, end: date) -> dict:
    rows = []
    principal_repaid = 0.0
    for l in book.by_category("secured_loan", "unsecured_loan", "loan", "bank_od"):
        repaid = borrowed = 0.0
        for v in book.entries_between(start, end):
            for e in v.entries:
                if e.ledger is l:
                    if e.amount > 0:
                        repaid += e.amount
                    else:
                        borrowed += -e.amount
        if l.category != "bank_od":
            principal_repaid += repaid
        if abs(l.closing) > 0.5 or repaid or borrowed:
            rows.append({
                "name": l.name,
                "type": {"secured_loan": "Secured", "unsecured_loan": "Unsecured", "bank_od": "OD / CC"}.get(l.category, "Loan"),
                "outstanding": -l.closing,
                "repaid": repaid,
                "drawn": borrowed,
            })
    rows.sort(key=lambda r: r["outstanding"], reverse=True)
    return {"rows": rows, "principal_repaid": principal_repaid}


# ---------------------------------------------------------------- GST / tax


def taxes(book: Book, start: date, end: date) -> dict:
    output_tax = paid = 0.0
    for v in book.entries_between(start, end):
        tax = v.total("tax")
        if v.total("sales") < 0:
            output_tax += -tax
        if v.has(*LIQUID_CATEGORIES):
            paid += sum(e.amount for e in v.entries if e.ledger.category == "tax" and e.amount > 0)
    payable = sum(max(-l.closing, 0) for l in book.by_category("tax"))
    return {"output_tax": output_tax, "tax_paid": paid, "tax_payable": payable}


# -------------------------------------------------------------- spread


def customer_spread(book: Book, ltm_sales: dict[str, float]) -> dict:
    by_state = defaultdict(float)
    for party, value in ltm_sales.items():
        ledger = book.ledgers.get(party)
        if party.startswith("("):
            label = party  # cash / unattributed sales have no state
        else:
            label = ledger.state if ledger and ledger.state else "Not recorded"
        by_state[label] += value
    total = sum(v for v in by_state.values() if v > 0)
    states = sorted(((s, v, _ratio(v, total)) for s, v in by_state.items() if v > 0), key=lambda r: r[1], reverse=True)

    bands = [(100000, "< ₹1 L"), (1000000, "₹1-10 L"), (5000000, "₹10-50 L"), (10000000, "₹50 L-1 Cr")]
    band_rows = defaultdict(lambda: [0, 0.0])
    for party, value in ltm_sales.items():
        if value <= 0 or party.startswith("("):
            continue
        label = next((lbl for lim, lbl in bands if value < lim), "> ₹1 Cr")
        band_rows[label][0] += 1
        band_rows[label][1] += value
    order = [lbl for _, lbl in bands] + ["> ₹1 Cr"]
    return {
        "states": states,
        "size_bands": [(lbl, *band_rows[lbl]) for lbl in order if lbl in band_rows],
    }


def retention(ltm: dict[str, float], prior: dict[str, float]) -> dict:
    real = lambda p: not p.startswith("(")
    now = {p for p, v in ltm.items() if v > 0 and real(p)}
    before = {p for p, v in prior.items() if v > 0 and real(p)}
    lost = before - now
    new = now - before
    retained_rev = sum(ltm[p] for p in now & before)
    return {
        "active": len(now),
        "prior_active": len(before),
        "new": len(new),
        "lost": len(lost),
        "lost_prior_revenue_share": _ratio(sum(prior[p] for p in lost), sum(v for p, v in prior.items() if v > 0)),
        "new_revenue_share": _ratio(sum(ltm[p] for p in new), sum(v for v in ltm.values() if v > 0)),
        "net_revenue_retention": _ratio(retained_rev, sum(prior[p] for p in before)),
        "lost_parties": sorted(lost, key=lambda p: prior[p], reverse=True)[:10],
    }


# ------------------------------------------------------------------ monthly


def monthly_series(book: Book, sales_rows, purchase_rows, cash) -> list[dict]:
    months = month_range(book.period_from, book.period_to)
    sales = defaultdict(float)
    purchases = defaultdict(float)
    for d, _, v, _ in sales_rows:
        sales[month_key(d)] += v
    for d, _, v, _ in purchase_rows:
        purchases[month_key(d)] += v
    return [
        {
            "month": m,
            "sales": sales[m],
            "purchases": purchases[m],
            "collections": cash["monthly"][m]["collections"],
            "bank_credits": cash["monthly"][m]["bank_credits"],
        }
        for m in months
    ]


def seasonality(series: list[dict], start_key: str) -> dict:
    values = [r["sales"] for r in series if r["month"] >= start_key]
    if len(values) < 3:
        return {"cv": None, "peak": None, "trough": None}
    mean = sum(values) / len(values)
    var = sum((x - mean) ** 2 for x in values) / len(values)
    ltm_rows = [r for r in series if r["month"] >= start_key]
    peak = max(ltm_rows, key=lambda r: r["sales"])
    trough = min(ltm_rows, key=lambda r: r["sales"])
    return {"cv": _ratio(var ** 0.5, mean), "peak": peak["month"], "trough": trough["month"]}


def compute_all(book: Book) -> dict:
    w = windows(book.as_of)
    ltm_start, ltm_end = w["ltm"]
    prior_start, prior_end = w["prior"]
    has_prior = book.period_from <= prior_start + timedelta(days=31)

    sales_rows, purchase_rows = party_flows(book)
    ltm_sales = _sum_by_party(sales_rows, ltm_start, ltm_end)
    prior_sales = _sum_by_party(sales_rows, prior_start, prior_end)
    ltm_purch = _sum_by_party(purchase_rows, ltm_start, ltm_end)

    real = lambda p: not p.startswith("(")
    customers = concentration(ltm_sales, real)
    suppliers = concentration(ltm_purch, real)
    pl = profit_and_loss(book, ltm_start, ltm_end)
    pl_prior = profit_and_loss(book, prior_start, prior_end) if has_prior else None
    bs = balance_sheet(book)
    recv = ageing(book, "debtor")
    pay = ageing(book, "creditor")
    cash = cash_and_banking(book, ltm_start, ltm_end)
    loan = loans(book, ltm_start, ltm_end)
    tax = taxes(book, ltm_start, ltm_end)
    series = monthly_series(book, sales_rows, purchase_rows, cash)

    credit_billing = _gross_billed(sales_rows, ltm_start, ltm_end)
    credit_purchases = _gross_billed(purchase_rows, ltm_start, ltm_end)
    dso = _ratio(recv["total"] * 365, credit_billing)
    dpo = _ratio(pay["total"] * 365, credit_purchases)
    dio = _ratio(bs["stock"] * 365, pl["cogs"]) if pl["cogs"] > 0 else None
    ccc = (dso or 0) + (dio or 0) - (dpo or 0) if dso is not None else None

    ltm_collections = sum(r["collections"] for r in series if r["month"] >= month_key(ltm_start))
    debt_service = pl["interest"] + loan["principal_repaid"]
    cash_sales = ltm_sales.get(CASH_SALES, 0.0)

    return {
        "windows": {"ltm": (ltm_start, ltm_end), "prior": (prior_start, prior_end), "has_prior": has_prior},
        "pl": pl,
        "pl_prior": pl_prior,
        "revenue_growth": _ratio(pl["sales"] - pl_prior["sales"], pl_prior["sales"]) if pl_prior else None,
        "monthly": series,
        "seasonality": seasonality(series, month_key(ltm_start)),
        "customers": customers,
        "cash_sales_share": _ratio(cash_sales, customers["total"]),
        "retention": retention(ltm_sales, prior_sales) if has_prior else None,
        "spread": customer_spread(book, ltm_sales),
        "suppliers": suppliers,
        "receivables": recv,
        "payables": pay,
        "working_capital": {
            "dso": dso,
            "dpo": dpo,
            "dio": dio,
            "ccc": ccc,
            "credit_billing": credit_billing,
            "credit_purchases": credit_purchases,
            "collection_ratio": _ratio(ltm_collections, credit_billing),
        },
        "bs": bs,
        "cash": cash,
        "loans": loan,
        "tax": tax,
        "coverage": {
            "interest_coverage": _ratio(pl["ebitda"], pl["interest"]) if pl["interest"] > 0 else None,
            "dscr": _ratio(pl["ebitda"], debt_service) if debt_service > 0 else None,
            "debt_service": debt_service,
        },
        "ltm_sales_by_party": ltm_sales,
    }
