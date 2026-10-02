#!/usr/bin/env python3
"""Mock TallyPrime XML/HTTP server for developing the connector without Tally.

    python3 dev/mock_tally.py            # serves on http://localhost:9000

Generates a synthetic trading company (~10k vouchers over 2.5 years) with
deliberate problems for the report to find: a large customer who stops paying,
a March sales spike reversed in April, back-dated entries, cash receipts of
₹2 L+, and a party that is both customer and supplier.

It answers the same request shapes the connector sends (TDL collections for
masters, "Day Book" export for vouchers) using Tally's XML layout and sign
convention (debit = negative), including Tally quirks such as the "&#4; Primary"
parent and empty LEDGERENTRIES.LIST placeholders.
"""

from __future__ import annotations

import argparse
import math
import random
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

COMPANY = "Shree Ganesh Traders Pvt Ltd"
HOME_STATE = "Maharashtra"

# (name, parent, reserved) — the predefined Tally groups plus two custom ones.
GROUPS = [
    ("Capital Account", "", True), ("Reserves & Surplus", "Capital Account", True),
    ("Current Assets", "", True), ("Bank Accounts", "Current Assets", True), ("Cash-in-Hand", "Current Assets", True),
    ("Deposits (Asset)", "Current Assets", True), ("Loans & Advances (Asset)", "Current Assets", True),
    ("Stock-in-Hand", "Current Assets", True), ("Sundry Debtors", "Current Assets", True),
    ("Current Liabilities", "", True), ("Duties & Taxes", "Current Liabilities", True),
    ("Provisions", "Current Liabilities", True), ("Sundry Creditors", "Current Liabilities", True),
    ("Direct Expenses", "", True), ("Direct Incomes", "", True), ("Fixed Assets", "", True),
    ("Indirect Expenses", "", True), ("Indirect Incomes", "", True), ("Investments", "", True),
    ("Loans (Liability)", "", True), ("Bank OD A/c", "Loans (Liability)", True),
    ("Secured Loans", "Loans (Liability)", True), ("Unsecured Loans", "Loans (Liability)", True),
    ("Misc. Expenses (ASSET)", "", True), ("Purchase Accounts", "", True), ("Sales Accounts", "", True),
    ("Suspense A/c", "", True), ("Branch / Divisions", "", True),
    ("Outstation Debtors", "Sundry Debtors", False), ("Local Suppliers", "Sundry Creditors", False),
]

VOUCHER_TYPES = [
    ("Sales", "", True), ("Purchase", "", True), ("Receipt", "", True), ("Payment", "", True),
    ("Journal", "", True), ("Contra", "", True), ("Credit Note", "", True), ("Debit Note", "", True),
    ("Sales Order", "", True), ("GST Sales", "Sales", False),
]

CUSTOMER_NAMES = [
    "Sunrise Retail Pvt Ltd", "Metro Wholesale Mart", "Balaji Distributors", "Patel Brothers", "Krishna Enterprises",
    "Om Sai Agencies", "Vardhman Stores", "Royal Traders", "Mahalaxmi Super Market", "Sai Krupa Traders",
    "Jain Trading Co", "Deccan Supplies", "Ganga Sales Corporation", "Shiv Shakti Stores", "Anand Agencies",
    "Classic Distributors", "New India Traders", "Bharat Retail", "Kaveri Marketing", "Lakshmi General Stores",
    "Venkatesh Traders", "Modern Bazaar", "Hind Supply Co", "Pioneer Agencies", "Star Mart", "Everest Traders",
    "Sagar Enterprises", "Unity Distributors",
]
STATES = [HOME_STATE] * 6 + ["Gujarat", "Karnataka", "Delhi", "Tamil Nadu", "Madhya Pradesh", "Goa"]
SUPPLIER_NAMES = [
    "Hindustan Foods Ltd", "Agro Mills Pvt Ltd", "Balaji Distributors", "Western Packaging", "Nirmal Oils",
    "Supreme Spices", "Coastal Commodities", "Prime Grains", "Shakti Pulses", "Fresh Farm Products",
]
ITEMS = [("Toor Dal 30kg", 3600), ("Sunflower Oil 15L", 2100), ("Basmati Rice 25kg", 2600), ("Sugar 50kg", 2150), ("Spice Mix Carton", 1800)]


@dataclass
class Voucher:
    date: date
    vtype: str
    number: str
    party: str
    entries: list  # [(ledger, dr_amount)] debit-positive
    master_id: int = 0
    cancelled: bool = False
    invoice: bool = False
    narration: str = ""
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))


class Company:
    def __init__(self, seed: int = 7, start: date = date(2024, 4, 1), end: date = date(2026, 9, 20)):
        self.rng = random.Random(seed)
        self.start, self.end = start, end
        self.ledgers: dict[str, dict] = {}
        self.vouchers: list[Voucher] = []
        self.counters: dict[str, int] = {}
        self._build_masters()
        self._simulate()
        self._finalise()

    # ---------------------------------------------------------------- masters
    def ledger(self, name, parent, opening_dr=0.0, **extra):
        self.ledgers[name] = {"name": name, "parent": parent, "opening": opening_dr, **extra}

    def _build_masters(self):
        r = self.rng
        self.customers = []
        for i, name in enumerate(CUSTOMER_NAMES):
            state = STATES[i % len(STATES)] if i else HOME_STATE
            parent = "Sundry Debtors" if state == HOME_STATE else "Outstation Debtors"
            gstin = f"27AAB{'C' if i % 2 else 'F'}{1000 + i}K1Z{i % 10}" if state == HOME_STATE else f"24AAC{1200 + i}Q1Z{i % 9}"
            self.ledger(name, parent, state=state, gstin=gstin, credit_period=30)
            self.customers.append({
                "name": name, "state": state,
                "weight": 1 / (i + 1) ** 1.05,
                "delay": r.randint(20, 70),
                "stop_paying": date(2026, 5, 1) if name == "Sunrise Retail Pvt Ltd" else None,
                "lost_after": date(2025, 7, 1) if name in ("Jain Trading Co", "Deccan Supplies") else None,
                "joins": date(2025, 10, 1) if name in ("Everest Traders", "Sagar Enterprises", "Unity Distributors") else None,
            })
        self.suppliers = []
        for i, name in enumerate(SUPPLIER_NAMES):
            if name == "Balaji Distributors":
                gstin = self.ledgers[name]["gstin"]
                name = "Balaji Distributors (Purchase)"
            else:
                gstin = f"27AAD{2000 + i}M1Z{i}"
            self.ledger(name, "Local Suppliers" if i % 2 else "Sundry Creditors", state=HOME_STATE, gstin=gstin, credit_period=45)
            self.suppliers.append({"name": name, "weight": 1 / (i + 1) ** 0.9, "terms": r.randint(35, 60)})

        L = self.ledger
        L("Sales @ 5%", "Sales Accounts"); L("Sales @ 18%", "Sales Accounts")
        L("Purchase @ 5%", "Purchase Accounts"); L("Purchase @ 18%", "Purchase Accounts")
        L("CGST Output", "Duties & Taxes"); L("SGST Output", "Duties & Taxes"); L("IGST Output", "Duties & Taxes")
        L("CGST Input", "Duties & Taxes"); L("SGST Input", "Duties & Taxes")
        L("Cash", "Cash-in-Hand", 180000)
        L("HDFC Bank Current A/c", "Bank Accounts", 1200000)
        L("SBI Cash Credit A/c", "Bank OD A/c", -6500000)
        L("ICICI Term Loan", "Secured Loans", -6000000)
        L("Loan from Director - R. Agarwal", "Unsecured Loans", -2500000)
        L("Plant & Machinery", "Fixed Assets", 4200000); L("Furniture & Fixtures", "Fixed Assets", 650000)
        L("Vehicle", "Fixed Assets", 1400000)
        L("Security Deposit - Godown", "Deposits (Asset)", 500000)
        L("Advance to Ganesh Infra (Sister Concern)", "Loans & Advances (Asset)", 1500000)
        L("Salary", "Indirect Expenses"); L("Godown Rent", "Indirect Expenses"); L("Electricity", "Indirect Expenses")
        L("Office Expenses", "Indirect Expenses"); L("Interest on Term Loan", "Indirect Expenses")
        L("Interest on Cash Credit", "Indirect Expenses"); L("Bank Charges", "Indirect Expenses")
        L("Depreciation", "Indirect Expenses"); L("Freight Inward", "Direct Expenses"); L("Loading & Unloading", "Direct Expenses")
        L("Discount Received", "Indirect Incomes"); L("Salary Payable", "Provisions")
        L("Capital - R. Agarwal", "Capital Account", 0.0)
        L("Profit & Loss A/c", "", 0.0, reserved="Profit & Loss A/c")
        # Opening stock is carried by inventory (Tally integrates it into the
        # balance sheet); the capital account balances everything.
        opening_stock = self.stock_value(self.start - timedelta(days=1))
        diff = sum(l["opening"] for l in self.ledgers.values()) + opening_stock
        self.ledgers["Capital - R. Agarwal"]["opening"] = -diff

    def stock_value(self, d: date) -> float:
        t = (d - date(2024, 4, 1)).days / 365
        return 8500000 * (1 + 0.1 * t) + 1500000 * math.sin(2 * math.pi * (d.month - 7) / 12)

    # ------------------------------------------------------------- simulation
    def next_no(self, vtype):
        self.counters[vtype] = self.counters.get(vtype, 0) + 1
        return f"{self.counters[vtype]}"

    def add(self, d, vtype, entries, party="", invoice=False, narration=""):
        # An amount of -1 is a placeholder for "the balancing credit".
        if any(a == -1 for _, a in entries):
            other = sum(a for _, a in entries if a != -1)
            entries = [(l, -other if a == -1 else a) for l, a in entries]
        entries = [(l, round(a, 2)) for l, a in entries if abs(round(a, 2)) >= 0.01]
        diff = round(sum(a for _, a in entries), 2)
        if diff:
            l, a = entries[0]
            entries[0] = (l, round(a - diff, 2))
        v = Voucher(d, vtype, self.next_no(vtype), party, entries, invoice=invoice, narration=narration)
        self.vouchers.append(v)
        return v

    def monthly_target(self, d: date) -> float:
        t = (d - self.start).days / 365
        season = {10: 1.25, 11: 1.3, 3: 1.1, 7: 0.88, 8: 0.9}.get(d.month, 1.0)
        return 4500000 * (1.12 ** t) * season

    def _simulate(self):
        r = self.rng
        receivable_queue = []  # (due date, customer, amount)
        payable_queue = []
        d = self.start
        while d <= self.end:
            if d.weekday() < 6:
                self._day_sales(d, receivable_queue)
                self._day_purchases(d, payable_queue)
            self._settle(d, receivable_queue, payable_queue)
            if d.day == 1:
                self._monthly(d)
            if d.month == 3 and d.day == 31:
                self.add(d, "Journal", [("Depreciation", 620000), ("Plant & Machinery", -420000),
                                        ("Furniture & Fixtures", -65000), ("Vehicle", -135000)], narration="Depreciation for the year")
            d += timedelta(days=1)
        self._year_end_window_dressing()
        self._cash_receipts_over_limit()

    def _customers_on(self, d):
        return [c for c in self.customers
                if not (c["lost_after"] and d > c["lost_after"]) and not (c["joins"] and d < c["joins"])]

    def _day_sales(self, d, queue):
        r = self.rng
        custs = self._customers_on(d)
        per_day = self.monthly_target(d) / 26
        if d.month == 3 and d.year == 2026 and d.day >= 20:
            per_day *= 2.6  # year-end push
        n = max(1, int(r.gauss(5, 1.5)))
        for _ in range(n):
            c = r.choices(custs, weights=[x["weight"] for x in custs])[0]
            taxable = max(5000, r.lognormvariate(0, 0.5) * per_day / n)
            taxable = round(taxable, 2)
            self._sale(d, c, taxable, queue)
        if r.random() < 0.3:
            amt = round(r.uniform(8000, 45000), 2)
            self.add(d, "Sales", [("Cash", amt * 1.05), ("Sales @ 5%", -amt), ("CGST Output", -amt * 0.025),
                                  ("SGST Output", -amt * 0.025)], narration="Counter sale")

    def _sale(self, d, c, taxable, queue, vtype=None):
        rate18 = self.rng.random() < 0.4
        sales_ledger = "Sales @ 18%" if rate18 else "Sales @ 5%"
        tax = taxable * (0.18 if rate18 else 0.05)
        if c["state"] == HOME_STATE:
            taxes = [("CGST Output", -tax / 2), ("SGST Output", -tax / 2)]
        else:
            taxes = [("IGST Output", -tax)]
        gross = taxable + tax
        v = self.add(d, vtype or self.rng.choice(["Sales", "GST Sales"]),
                     [(c["name"], gross), (sales_ledger, -taxable)] + taxes, party=c["name"], invoice=True)
        if c["stop_paying"] and d >= c["stop_paying"] - timedelta(days=c["delay"]):
            return v
        delay = max(0, int(self.rng.gauss(c["delay"], 10)))
        queue.append((d + timedelta(days=delay), c["name"], round(gross, 2)))
        return v

    def _day_purchases(self, d, queue):
        r = self.rng
        if r.random() < 0.55:
            s = r.choices(self.suppliers, weights=[x["weight"] for x in self.suppliers])[0]
            taxable = round(self.monthly_target(d) * 0.80 / 14 * r.lognormvariate(0, 0.35), 2)
            rate18 = r.random() < 0.4
            tax = taxable * (0.18 if rate18 else 0.05)
            self.add(d, "Purchase", [(s["name"], -(taxable + tax)), ("Purchase @ 18%" if rate18 else "Purchase @ 5%", taxable),
                                     ("CGST Input", tax / 2), ("SGST Input", tax / 2)], party=s["name"], invoice=True)
            freight = round(taxable * 0.012, 2)
            self.add(d, "Payment", [("Freight Inward", freight), ("HDFC Bank Current A/c", -freight)])
            queue.append((d + timedelta(days=int(r.gauss(s["terms"], 8))), s["name"], round(taxable + tax, 2)))

    def _settle(self, d, recv, pay):
        r = self.rng
        for item in [x for x in recv if x[0] <= d]:
            recv.remove(item)
            _, name, amt = item
            bank = "Cash" if r.random() < 0.03 else "SBI Cash Credit A/c"
            self.add(d, "Receipt", [(bank, amt), (name, -amt)], party=name)
        for item in [x for x in pay if x[0] <= d]:
            pay.remove(item)
            _, name, amt = item
            self.add(d, "Payment", [(name, amt), ("SBI Cash Credit A/c", -amt)], party=name)

    def _monthly(self, d):
        r = self.rng
        month_end = d - timedelta(days=1)
        if month_end < self.start:
            return
        self.add(d, "Payment", [("Salary", 520000 * (1.08 ** ((d - self.start).days / 365))), ("HDFC Bank Current A/c", -1)])
        self.add(d, "Payment", [("Godown Rent", 120000), ("HDFC Bank Current A/c", -120000)])
        self.add(d, "Payment", [("Electricity", r.uniform(30000, 55000)), ("HDFC Bank Current A/c", -1)])
        self.add(d, "Payment", [("Office Expenses", r.uniform(12000, 30000)), ("Cash", -1)])
        self.add(d, "Payment", [("Loading & Unloading", r.uniform(11000, 18000)), ("Cash", -1)])
        self.add(d, "Payment", [("Bank Charges", r.uniform(1500, 4000)), ("SBI Cash Credit A/c", -1)])
        self.add(d, "Journal", [("Interest on Cash Credit", 62000 + r.uniform(-8000, 8000)), ("SBI Cash Credit A/c", -1)])
        outstanding = -self.balance("ICICI Term Loan", d)
        interest = outstanding * 0.0085
        self.add(d, "Payment", [("ICICI Term Loan", 125000), ("Interest on Term Loan", interest), ("SBI Cash Credit A/c", -1)])
        self.add(d, "Contra", [("HDFC Bank Current A/c", 900000), ("SBI Cash Credit A/c", -900000)])
        self.add(d, "Payment", [("Advance to Ganesh Infra (Sister Concern)", 400000), ("SBI Cash Credit A/c", -400000)],
                 narration="Advance to sister concern")
        # Sweep surplus cash and current-account money into the CC account.
        for ledger, keep in (("Cash", 300000), ("HDFC Bank Current A/c", 1500000)):
            surplus = self.balance(ledger, d) - keep
            if surplus > 0:
                self.add(d, "Contra", [("SBI Cash Credit A/c", surplus), (ledger, -surplus)])
        # GST settlement for the previous month
        tax = sum(-a for v in self.vouchers if v.date.month == month_end.month and v.date.year == month_end.year
                  for l, a in v.entries if l.endswith("Output"))
        itc = sum(a for v in self.vouchers if v.date.month == month_end.month and v.date.year == month_end.year
                  for l, a in v.entries if l.endswith("Input"))
        if tax > itc:
            net = tax - itc
            self.add(d + timedelta(days=19), "Payment", [("CGST Output", net / 2), ("SGST Output", net / 2), ("SBI Cash Credit A/c", -net)],
                     narration="GST paid")

    def _year_end_window_dressing(self):
        march = [v for v in self.vouchers if v.date.year == 2026 and v.date.month == 3 and v.date.day >= 20
                 and v.vtype in ("Sales", "GST Sales") and v.party]
        for v in march[: len(march) // 3]:
            taxable = -sum(a for l, a in v.entries if l.startswith("Sales"))
            reversed_entries = [(l, -a) for l, a in v.entries]
            self.add(date(2026, 4, self.rng.randint(3, 20)), "Credit Note", reversed_entries, party=v.party,
                     narration=f"Goods returned against {v.number} ({taxable:.0f})")

    def _cash_receipts_over_limit(self):
        for d, name, amt in [(date(2026, 1, 12), "Patel Brothers", 250000), (date(2026, 2, 3), "Royal Traders", 300000),
                             (date(2026, 6, 18), "Om Sai Agencies", 220000)]:
            self.add(d, "Receipt", [("Cash", amt), (name, -amt)], party=name)
            self.add(d + timedelta(days=1), "Contra", [("SBI Cash Credit A/c", amt), ("Cash", -amt)])

    def balance(self, ledger: str, upto: date) -> float:
        return self.ledgers[ledger]["opening"] + sum(a for v in self.vouchers if v.date <= upto and not v.cancelled
                                                     for l, a in v.entries if l == ledger)

    def _finalise(self):
        r = self.rng
        self.vouchers.sort(key=lambda v: v.date)
        # Creation order = date order, except a batch of back-dated entries
        # keyed in at the end of the period.
        backdated = r.sample([v for v in self.vouchers if date(2025, 10, 1) <= v.date <= date(2026, 3, 31)
                              and v.vtype in ("Sales", "GST Sales")], 14)
        ordered = [v for v in self.vouchers if v not in backdated] + backdated
        for i, v in enumerate(ordered, start=1000):
            v.master_id = i
        for v in r.sample(self.vouchers, 12):
            if v.vtype in ("Sales", "Payment"):
                v.cancelled = True
        self.index = sorted(self.vouchers, key=lambda v: v.date)
        self.movements: dict[str, list] = {}
        for v in self.index:
            if v.cancelled:
                continue
            for l, a in v.entries:
                self.movements.setdefault(l, []).append((v.date, a))

    def closing(self, ledger: str, upto: date) -> float:
        return self.ledgers[ledger]["opening"] + sum(a for d, a in self.movements.get(ledger, []) if d <= upto)


# -------------------------------------------------------------------- XML out

def tally_amount(dr: float) -> str:
    return f"{-dr:.2f}"


def tdate(d: date) -> str:
    return d.strftime("%Y%m%d")


def parent_xml(parent: str) -> str:
    return "&#4; Primary" if not parent else escape(parent)


def collection_response(objects: list[str]) -> str:
    return ("<ENVELOPE>\n <HEADER>\n  <VERSION>1</VERSION>\n  <STATUS>1</STATUS>\n </HEADER>\n <BODY>\n  <DESC>\n  </DESC>\n"
            "  <DATA>\n   <COLLECTION>\n" + "\n".join(objects) + "\n   </COLLECTION>\n  </DATA>\n </BODY>\n</ENVELOPE>\n")


def render_companies(co: Company) -> list[str]:
    return [
        f'<COMPANY NAME="{escape(COMPANY)}" RESERVEDNAME=""><NAME TYPE="String">{escape(COMPANY)}</NAME>'
        f'<GUID TYPE="String">a1b2c3d4-0000-4000-8000-{1:012d}</GUID><STARTINGFROM TYPE="Date">{tdate(co.start)}</STARTINGFROM>'
        f'<BOOKSFROM TYPE="Date">{tdate(co.start)}</BOOKSFROM><STATENAME TYPE="String">{HOME_STATE}</STATENAME>'
        f'<INCOMETAXNUMBER TYPE="String">AAKCS1234F</INCOMETAXNUMBER></COMPANY>',
        '<COMPANY NAME="Demo Company (Old)" RESERVEDNAME=""><NAME TYPE="String">Demo Company (Old)</NAME>'
        '<STARTINGFROM TYPE="Date">20190401</STARTINGFROM><BOOKSFROM TYPE="Date">20190401</BOOKSFROM></COMPANY>',
    ]


def render_groups() -> list[str]:
    out = []
    for name, parent, reserved in GROUPS:
        out.append(f'<GROUP NAME="{escape(name)}" RESERVEDNAME="{escape(name) if reserved else ""}">'
                   f'<PARENT TYPE="String">{parent_xml(parent)}</PARENT></GROUP>')
    return out


def render_voucher_types() -> list[str]:
    return [f'<VOUCHERTYPE NAME="{escape(n)}" RESERVEDNAME="{escape(n) if res else ""}">'
            f'<PARENT TYPE="String">{escape(p or n)}</PARENT></VOUCHERTYPE>' for n, p, res in VOUCHER_TYPES]


def render_ledgers(co: Company, to: date) -> list[str]:
    out = []
    for l in co.ledgers.values():
        extra = ""
        if l.get("gstin"):
            extra += f'<PARTYGSTIN TYPE="String">{l["gstin"]}</PARTYGSTIN>'
        if l.get("state"):
            extra += f'<LEDSTATENAME TYPE="String">{escape(l["state"])}</LEDSTATENAME>'
        if l.get("credit_period"):
            extra += f'<ISBILLWISEON TYPE="Logical">Yes</ISBILLWISEON><BILLCREDITPERIOD TYPE="Due Date">{l["credit_period"]} Days</BILLCREDITPERIOD>'
        out.append(
            f'<LEDGER NAME="{escape(l["name"])}" RESERVEDNAME="{escape(l.get("reserved", ""))}">'
            f'<PARENT TYPE="String">{parent_xml(l["parent"])}</PARENT>'
            f'<OPENINGBALANCE TYPE="Amount">{tally_amount(l["opening"])}</OPENINGBALANCE>'
            f'<CLOSINGBALANCE TYPE="Amount">{tally_amount(co.closing(l["name"], to))}</CLOSINGBALANCE>{extra}</LEDGER>'
        )
    return out


def render_stock(co: Company, to: date) -> list[str]:
    total = co.stock_value(to)
    shares = [0.3, 0.22, 0.2, 0.18, 0.1]
    out = []
    for (name, rate), share in zip(ITEMS, shares):
        value = total * share
        out.append(f'<STOCKITEM NAME="{escape(name)}" RESERVEDNAME=""><PARENT TYPE="String">&#4; Primary</PARENT>'
                   f'<BASEUNITS TYPE="String">Bag</BASEUNITS><CLOSINGBALANCE TYPE="Quantity"> {value / rate:.0f} Bag</CLOSINGBALANCE>'
                   f'<CLOSINGVALUE TYPE="Amount">{tally_amount(value)}</CLOSINGVALUE></STOCKITEM>')
    return out


def render_voucher(v: Voucher) -> str:
    parts = [
        f'<VOUCHER REMOTEID="{v.guid}" VCHKEY="{v.guid}:00000008" VCHTYPE="{escape(v.vtype)}" ACTION="Create" '
        f'OBJVIEW="{"Invoice Voucher View" if v.invoice else "Accounting Voucher View"}">',
        f"<DATE>{tdate(v.date)}</DATE><GUID>{v.guid}</GUID><NARRATION>{escape(v.narration)}</NARRATION>",
        f"<VOUCHERTYPENAME>{escape(v.vtype)}</VOUCHERTYPENAME><VOUCHERNUMBER>{v.number}</VOUCHERNUMBER>",
        f"<PARTYLEDGERNAME>{escape(v.party)}</PARTYLEDGERNAME>",
        f"<ISCANCELLED>{'Yes' if v.cancelled else 'No'}</ISCANCELLED><ISOPTIONAL>No</ISOPTIONAL>",
        f"<ISINVOICE>{'Yes' if v.invoice else 'No'}</ISINVOICE>",
        f"<MASTERID> {v.master_id}</MASTERID><ALTERID> {v.master_id + 50}</ALTERID>",
    ]

    def entry(tag, ledger, dr, bills=False):
        s = (f"<{tag}><LEDGERNAME>{escape(ledger)}</LEDGERNAME><ISDEEMEDPOSITIVE>{'Yes' if dr > 0 else 'No'}</ISDEEMEDPOSITIVE>"
             f"<AMOUNT>{tally_amount(dr)}</AMOUNT>")
        if bills:
            s += (f"<BILLALLOCATIONS.LIST><NAME>{escape(v.number)}</NAME><BILLTYPE>New Ref</BILLTYPE>"
                  f"<AMOUNT>{tally_amount(dr)}</AMOUNT></BILLALLOCATIONS.LIST>")
        return s + f"</{tag}>"

    sales_like = [(l, a) for l, a in v.entries if l.startswith("Sales @") or l.startswith("Purchase @")]
    if v.invoice and sales_like:
        # Item invoice: sales/purchase ledger lives under the inventory entry.
        for l, a in v.entries:
            if (l, a) not in sales_like:
                parts.append(entry("LEDGERENTRIES.LIST", l, a, bills=l == v.party))
        for (l, a), (item, rate) in zip(sales_like, ITEMS):
            qty = max(1, round(abs(a) / rate))
            parts.append(
                f"<ALLINVENTORYENTRIES.LIST><STOCKITEMNAME>{escape(item)}</STOCKITEMNAME>"
                f"<ISDEEMEDPOSITIVE>{'Yes' if a > 0 else 'No'}</ISDEEMEDPOSITIVE><RATE>{rate:.2f}/Bag</RATE>"
                f"<AMOUNT>{tally_amount(a)}</AMOUNT><ACTUALQTY> {qty} Bag</ACTUALQTY><BILLEDQTY> {qty} Bag</BILLEDQTY>"
                f"{entry('ACCOUNTINGALLOCATIONS.LIST', l, a)}</ALLINVENTORYENTRIES.LIST>"
            )
    else:
        for l, a in v.entries:
            parts.append(entry("ALLLEDGERENTRIES.LIST", l, a, bills=l == v.party))
        parts.append("<LEDGERENTRIES.LIST>      </LEDGERENTRIES.LIST>")  # Tally emits empty placeholders
    parts.append("</VOUCHER>")
    return "".join(parts)


def daybook_response(co: Company, frm: date, to: date) -> str:
    vs = [v for v in co.index if frm <= v.date <= to]
    body = "\n".join(f'<TALLYMESSAGE xmlns:UDF="TallyUDF">{render_voucher(v)}</TALLYMESSAGE>' for v in vs)
    return ("<ENVELOPE>\n <HEADER>\n  <TALLYREQUEST>Import Data</TALLYREQUEST>\n </HEADER>\n <BODY>\n  <IMPORTDATA>\n"
            f"   <REQUESTDESC>\n    <REPORTNAME>Vouchers</REPORTNAME>\n    <STATICVARIABLES>\n     <SVCURRENTCOMPANY>{escape(COMPANY)}</SVCURRENTCOMPANY>\n"
            "    </STATICVARIABLES>\n   </REQUESTDESC>\n   <REQUESTDATA>\n" + body + "\n   </REQUESTDATA>\n  </IMPORTDATA>\n </BODY>\n</ENVELOPE>\n")


def error_response(msg: str) -> str:
    return f"<ENVELOPE><HEADER><VERSION>1</VERSION><STATUS>0</STATUS></HEADER><BODY><DATA><LINEERROR>{escape(msg)}</LINEERROR></DATA></BODY></ENVELOPE>"


def parse_tdate(s: str | None, default: date) -> date:
    if not s:
        return default
    s = s.strip()
    if re.fullmatch(r"\d{8}", s):
        return date(int(s[:4]), int(s[4:6]), int(s[6:]))
    from datetime import datetime
    return datetime.strptime(s, "%d-%b-%Y").date()


def decode_body(raw: bytes) -> tuple[str, str]:
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff") or (len(raw) > 1 and raw[1] == 0):
        return raw.decode("utf-16"), "utf-16le"
    return raw.decode("utf-8"), "utf-8"


def handle_request(co: Company, xml: str) -> str:
    root = ET.fromstring(xml)
    sv = {el.tag.upper(): (el.text or "") for el in root.iter() if el.tag.upper().startswith("SV")}
    company = sv.get("SVCURRENTCOMPANY", COMPANY)
    if company and company not in (COMPANY,) and company != "Demo Company (Old)":
        return error_response(f"Could not set 'SVCurrentCompany' to '{company}'")
    frm = parse_tdate(sv.get("SVFROMDATE"), co.start)
    to = parse_tdate(sv.get("SVTODATE"), co.end)
    report = root.findtext(".//REPORTNAME") or root.findtext(".//HEADER/ID") or ""
    if company == "Demo Company (Old)":
        return collection_response([]) if "Day Book" not in report else daybook_response(co, date(1900, 1, 1), date(1900, 1, 1))
    coll = root.find(".//TDL//COLLECTION")
    if coll is not None:
        kind = (coll.findtext("TYPE") or "").strip().lower()
        if kind == "company":
            return collection_response(render_companies(co))
        if kind == "group":
            return collection_response(render_groups())
        if kind == "ledger":
            return collection_response(render_ledgers(co, to))
        if kind == "vouchertype":
            return collection_response(render_voucher_types())
        if kind == "stockitem":
            return collection_response(render_stock(co, to))
        if kind == "bills":
            return collection_response([])
        return error_response(f"Unknown collection type {kind}")
    if report.strip().lower() == "day book":
        return daybook_response(co, frm, to)
    return error_response(f"Could not find Report '{report}'!")


def serve(port: int, seed: int):
    co = Company(seed)
    print(f"mock Tally: {COMPANY}: {len(co.vouchers)} vouchers {co.start}..{co.end}, listening on :{port}")

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self._send("<RESPONSE>TallyPrime Server is Running</RESPONSE>", "utf-8")

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            text, enc = decode_body(raw)
            try:
                out = handle_request(co, text)
            except ET.ParseError as e:
                out = error_response(f"XML parse error: {e}")
            self._send(out, enc)

        def _send(self, text, enc):
            data = text.encode("utf-16-le") if enc == "utf-16le" else text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", f"text/xml; charset={'utf-16' if enc == 'utf-16le' else 'utf-8'}")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            pass

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    serve(a.port, a.seed)
