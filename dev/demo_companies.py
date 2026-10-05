#!/usr/bin/env python3
"""Demo companies with different financial pictures, for testing TallyVeda end
to end on a PC whose Tally has no data (see connector/cmd/demoseed).

Each profile simulates about three and a half years of accounting vouchers
(from 1 April 2023 to the build date) for one made-up business, with the
conditions it is meant to show:

  healthy   growing distributor: quick collections, low debt, clean books
  stressed  shrinking manufacturer: slow and stuck debtors, stretched
            suppliers, losses, maxed-out cash credit, unpaid GST, negative cash
  seasonal  agri-input dealer: two sales seasons a year, one customer near
            half of sales, heavy cash trade (s.269ST / s.40A(3))
  redflags  trader whose ratios look fine but whose books show circular
            trading, year-end window dressing, back-dated entries, round
            invoices, money parked in suspense and advances to a sister concern

The model has the same shape as dev/mock_tally.Company (ledgers, vouchers,
closing()), so dev/demo_seed_data.py writes it in Tally's import format and
the backend tests can analyse it directly.
"""

from __future__ import annotations

import math
import random
import string
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from mock_tally import GROUPS, VOUCHER_TYPES, Voucher  # noqa: F401  (re-exported for the seed writer)

START = date(2023, 4, 1)

STATE_CODES = {"Maharashtra": "27", "Gujarat": "24", "Karnataka": "29", "Delhi": "07", "Tamil Nadu": "33",
               "Rajasthan": "08", "Uttar Pradesh": "09", "Madhya Pradesh": "23", "Telangana": "36", "Haryana": "06"}

FIRST = ["Shree", "Jai", "Om", "Sai", "New", "Royal", "Star", "Prime", "Metro", "Sunrise", "Classic", "Ganga",
         "Krishna", "Laxmi", "Balaji", "Ambika", "Vijay", "Sagar", "Unity", "Everest", "Pioneer", "Bharat", "Deccan",
         "Kaveri", "Narmada", "Shiv", "Durga", "Hari", "Mahavir", "Navkar", "Siddhi", "Riddhi", "Patel", "Jain",
         "Agarwal", "Gupta", "Reddy", "Nair", "Iyer", "Shah"]
SECOND = ["Traders", "Enterprises", "Agencies", "Distributors", "Stores", "Marketing", "Sales Corporation",
          "Supply Co", "Industries", "Brothers", "& Sons", "Retail Pvt Ltd", "Wholesale", "Trading Co", "Mart"]


@dataclass
class Profile:
    key: str
    company: str
    business: str
    home: str
    other_states: list[str]
    owner: str
    monthly_sales: float           # credit + cash sales per month at the start
    growth: float                  # per year
    season: list[float]            # Jan..Dec multipliers
    gross_margin: float
    rate18_share: float            # share of sales at 18% GST (rest 5%)
    customers: int
    concentration: float           # weight exponent for customers after the anchor
    anchor_share: float = 0.0      # one customer forced to about this share of credit sales
    collect_days: float = 35
    collect_drift: float = 0.0     # extra days per year
    supplier_days: float = 45
    supplier_drift: float = 0.0
    cash_sales: float = 0.03
    overheads: float = 0.06        # monthly fixed costs as a share of starting monthly sales
    overhead_growth: float = 0.08
    term_loan: float = 0.0
    cc_opening: float = 0.0        # cash credit drawn at the start
    director_loan: float = 0.0
    fixed_assets: float = 0.0
    stop_paying: list[tuple[int, date]] = field(default_factory=list)  # customer index, from
    lost: list[tuple[int, date]] = field(default_factory=list)         # customer index, no sales after
    big_cash_receipts: int = 0     # ₹2 L+ cash receipts per year
    big_cash_payments: int = 0     # ₹10,000+ cash payments per month
    negative_cash: bool = False
    gst_unpaid_from: date | None = None
    window_dressing: bool = False
    backdated: int = 0
    circular: int = 0
    round_invoices: bool = False
    suspense: float = 0.0
    sister_advance: float = 0.0    # per month
    reserves: float = 0.0          # opening fixed deposits (the owner's own money)
    seed: int = 1


PROFILES = {
    "healthy": Profile(
        key="healthy", company="TallyVeda Demo Healthy", business="FMCG distributor", home="Maharashtra",
        other_states=["Gujarat", "Karnataka", "Madhya Pradesh"], owner="A. Kulkarni",
        monthly_sales=2_80_00_000, growth=0.18, season=[1, 1, 1.08, 0.95, 0.95, 0.92, 0.9, 0.95, 1.0, 1.15, 1.2, 1.05],
        gross_margin=0.115, rate18_share=0.3, customers=42, concentration=0.75,
        collect_days=28, supplier_days=40, cash_sales=0.02, overheads=0.055, term_loan=1_20_00_000,
        cc_opening=1_50_00_000, fixed_assets=2_40_00_000, seed=11),
    "stressed": Profile(
        key="stressed", company="TallyVeda Demo Stressed", business="engineering components manufacturer",
        home="Gujarat", other_states=["Maharashtra", "Rajasthan", "Tamil Nadu"], owner="K. Mehta",
        monthly_sales=1_40_00_000, growth=-0.16, season=[1.05, 1.0, 1.2, 0.85, 0.95, 0.95, 0.9, 0.95, 1.0, 1.0, 1.05, 1.1],
        gross_margin=0.17, rate18_share=1.0, customers=18, concentration=1.1,
        collect_days=75, collect_drift=28, supplier_days=70, supplier_drift=35, cash_sales=0.0,
        overheads=0.12, overhead_growth=0.06, term_loan=3_60_00_000, cc_opening=2_40_00_000,
        director_loan=60_00_000, fixed_assets=5_20_00_000,
        stop_paying=[(0, date(2025, 11, 1)), (2, date(2026, 3, 1)), (5, date(2026, 6, 1))],
        lost=[(1, date(2025, 8, 1)), (7, date(2026, 1, 1))],
        negative_cash=True, gst_unpaid_from=date(2026, 2, 1), seed=22),
    "seasonal": Profile(
        key="seasonal", company="TallyVeda Demo Seasonal", business="agri-input dealer (seeds, fertiliser, pesticide)",
        home="Karnataka", other_states=["Telangana", "Tamil Nadu"], owner="R. Gowda",
        monthly_sales=70_00_000, growth=0.03, season=[0.45, 0.4, 0.5, 0.6, 1.3, 2.3, 2.1, 1.1, 0.9, 1.8, 1.6, 0.65],
        gross_margin=0.085, rate18_share=0.1, customers=26, concentration=1.0, anchor_share=0.52,
        collect_days=45, supplier_days=30, cash_sales=0.32, overheads=0.05, term_loan=40_00_000,
        cc_opening=55_00_000, fixed_assets=60_00_000, big_cash_receipts=9, big_cash_payments=6, reserves=1_80_00_000, seed=33),
    "redflags": Profile(
        key="redflags", company="TallyVeda Demo Red Flags", business="electronics trader", home="Delhi",
        other_states=["Haryana", "Uttar Pradesh", "Rajasthan"], owner="S. Malhotra",
        monthly_sales=1_60_00_000, growth=0.22, season=[1, 1, 1, 1, 1, 1, 1, 1, 1, 1.1, 1.15, 1],
        gross_margin=0.07, rate18_share=1.0, customers=24, concentration=0.9,
        collect_days=55, supplier_days=50, cash_sales=0.04, overheads=0.035, term_loan=80_00_000,
        cc_opening=1_20_00_000, director_loan=1_50_00_000, fixed_assets=90_00_000,
        window_dressing=True, backdated=150, circular=3, round_invoices=True, suspense=38_00_000,
        sister_advance=9_00_000, big_cash_receipts=3, seed=44),
}


def _gstin(rng: random.Random, state: str) -> str:
    pan = "".join(rng.choice(string.ascii_uppercase) for _ in range(3)) + rng.choice("CFP") + \
        rng.choice(string.ascii_uppercase) + f"{rng.randint(1000, 9999)}" + rng.choice(string.ascii_uppercase)
    return f"{STATE_CODES.get(state, '27')}{pan}1Z{rng.choice(string.digits + string.ascii_uppercase)}"


class DemoCompany:
    """One simulated business. Amounts are debit-positive, as in mock_tally."""

    def __init__(self, p: Profile, end: date | None = None):
        self.p, self.start, self.end = p, START, end or date.today()
        self.rng = random.Random(p.seed)
        self.ledgers: dict[str, dict] = {}
        self.vouchers: list[Voucher] = []
        self.counters: dict[str, int] = {}
        self.bal: dict[str, float] = {}
        self._masters()
        self._simulate()
        self._finalise()

    # ----------------------------------------------------------- masters
    def ledger(self, name, parent, opening=0.0, **extra):
        self.ledgers[name] = {"name": name, "parent": parent, "opening": opening, **extra}
        self.bal[name] = opening

    def _party_names(self, n: int, used: set[str]) -> list[str]:
        out = []
        while len(out) < n:
            name = f"{self.rng.choice(FIRST)} {self.rng.choice(SECOND)}"
            if name not in used:
                used.add(name)
                out.append(name)
        return out

    def _masters(self):
        p, r = self.p, self.rng
        used: set[str] = set()
        states = [p.home] * 3 + p.other_states
        self.customers = []
        for i, name in enumerate(self._party_names(p.customers, used)):
            state = p.home if i < 2 else r.choice(states)
            self.ledger(name, "Sundry Debtors" if state == p.home else "Outstation Debtors",
                        state=state, gstin=_gstin(r, state), credit_period=30)
            stop = next((d for k, d in p.stop_paying if k == i), None)
            lost = next((d for k, d in p.lost if k == i), None)
            self.customers.append({"name": name, "state": state, "weight": 1 / (i + 1) ** p.concentration,
                                   "delay": r.gauss(0, 8), "stop": stop, "lost": lost})
        if p.anchor_share:
            rest = sum(c["weight"] for c in self.customers[1:])
            self.customers[0]["weight"] = rest * p.anchor_share / (1 - p.anchor_share)

        self.suppliers = []
        for i, name in enumerate(self._party_names(max(6, p.customers // 3), used)):
            state = p.home if i % 3 else r.choice(states)
            self.ledger(name, "Sundry Creditors" if i % 2 else "Local Suppliers", state=state,
                        gstin=_gstin(r, state), credit_period=45)
            self.suppliers.append({"name": name, "state": state, "weight": 1 / (i + 1) ** 0.8})
        # Circular trading: the same businesses buy from and sell to us, under
        # a second ledger name but the same GSTIN.
        for c in self.customers[3:3 + p.circular]:
            name = f"{c['name']} (Purchase)"
            self.ledger(name, "Sundry Creditors", state=c["state"], gstin=self.ledgers[c["name"]]["gstin"], credit_period=30)
            self.suppliers.append({"name": name, "state": c["state"], "weight": 1.5, "circular": True})

        L = self.ledger
        L("Sales @ 5%", "Sales Accounts"); L("Sales @ 18%", "Sales Accounts")
        L("Purchase @ 5%", "Purchase Accounts"); L("Purchase @ 18%", "Purchase Accounts")
        for t in ("CGST", "SGST", "IGST"):
            L(f"{t} Output", "Duties & Taxes"); L(f"{t} Input", "Duties & Taxes")
        L("Cash", "Cash-in-Hand", 2_50_000 if not p.negative_cash else 40_000)
        L("HDFC Bank Current A/c", "Bank Accounts", p.monthly_sales * 0.06)
        L("SBI Cash Credit A/c", "Bank OD A/c", -p.cc_opening)
        if p.term_loan:
            L("Term Loan - SBI", "Secured Loans", -p.term_loan)
        if p.director_loan:
            L(f"Loan from Director - {p.owner}", "Unsecured Loans", -p.director_loan)
        L("Plant & Machinery", "Fixed Assets", p.fixed_assets * 0.6)
        L("Furniture & Fixtures", "Fixed Assets", p.fixed_assets * 0.1)
        L("Vehicles", "Fixed Assets", p.fixed_assets * 0.3)
        L("Security Deposit - Premises", "Deposits (Asset)", p.monthly_sales * 0.05)
        L("Fixed Deposit - HDFC", "Bank Accounts", p.reserves)
        if p.sister_advance:
            L(f"Advance to {p.owner.split()[-1]} Infra (Sister Concern)", "Loans & Advances (Asset)", p.sister_advance * 6)
        for name in ("Salary & Wages", "Rent", "Electricity", "Office Expenses", "Transport & Freight",
                     "Interest on Term Loan", "Interest on Cash Credit", "Bank Charges", "Depreciation"):
            L(name, "Indirect Expenses")
        L("Freight Inward", "Direct Expenses")
        L("Discount Received", "Indirect Incomes")
        if p.suspense:
            L("Suspense A/c", "Suspense A/c")
        L(f"Capital - {p.owner}", "Capital Account")
        L("Profit & Loss A/c", "", 0.0, reserved="Profit & Loss A/c")
        # Opening debtors and creditors so the books don't start from nothing.
        for c in self.customers[: p.customers // 2]:
            amt = round(p.monthly_sales * c["weight"] / sum(x["weight"] for x in self.customers) * p.collect_days / 30, 2)
            self.ledgers[c["name"]]["opening"] = self.bal[c["name"]] = amt
        for s in self.suppliers[:4]:
            amt = -round(p.monthly_sales * (1 - p.gross_margin) * 0.2, 2)
            self.ledgers[s["name"]]["opening"] = self.bal[s["name"]] = amt
        capital = -sum(l["opening"] for l in self.ledgers.values())
        self.ledgers[f"Capital - {p.owner}"]["opening"] = self.bal[f"Capital - {p.owner}"] = capital
        self.recv = [(self.start + timedelta(days=self.rng.randint(5, int(p.collect_days) + 10)), c["name"], self.bal[c["name"]])
                     for c in self.customers if self.bal[c["name"]] > 0]
        self.pay = [(self.start + timedelta(days=self.rng.randint(10, int(p.supplier_days))), s["name"], -self.bal[s["name"]])
                    for s in self.suppliers if self.bal[s["name"]] < 0]

    # -------------------------------------------------------- simulation
    def add(self, d, vtype, entries, party="", narration=""):
        if any(a is None for _, a in entries):  # None = the balancing amount
            other = sum(a for _, a in entries if a is not None)
            entries = [(l, -other if a is None else a) for l, a in entries]
        entries = [(l, round(a, 2)) for l, a in entries if abs(round(a, 2)) >= 0.01]
        diff = round(sum(a for _, a in entries), 2)
        if diff:
            l, a = entries[0]
            entries[0] = (l, round(a - diff, 2))
        self.counters[vtype] = self.counters.get(vtype, 0) + 1
        v = Voucher(d, vtype, str(self.counters[vtype]), party, entries, narration=narration,
                    guid=str(uuid.UUID(int=self.rng.getrandbits(128))))
        for l, a in entries:
            self.bal[l] = self.bal.get(l, 0.0) + a
        self.vouchers.append(v)
        return v

    def years(self, d: date) -> float:
        return (d - self.start).days / 365

    def target(self, d: date) -> float:
        p = self.p
        return p.monthly_sales * (1 + p.growth) ** self.years(d) * p.season[d.month - 1]

    def _tax(self, party_state, taxable, rate, side):
        if party_state == self.p.home:
            return [(f"CGST {side}", taxable * rate / 2), (f"SGST {side}", taxable * rate / 2)]
        return [(f"IGST {side}", taxable * rate)]

    def _simulate(self):
        d = self.start
        while d <= self.end:
            if d.weekday() < 6:
                self._sales(d)
                self._purchases(d)
            self._settle(d)
            if d.day == 1 and d > self.start:
                self._monthly(d)
            if d.day == 20 and d > self.start:
                self._gst(d)
            if d.month == 3 and d.day == 31:
                self.add(d, "Journal", [("Depreciation", self.p.fixed_assets * 0.12), ("Plant & Machinery", -self.p.fixed_assets * 0.07),
                                        ("Furniture & Fixtures", -self.p.fixed_assets * 0.01), ("Vehicles", None)],
                         narration="Depreciation for the year")
            self._treasury(d)
            d += timedelta(days=1)
        self._special()

    def _active_customers(self, d):
        return [c for c in self.customers if not (c["lost"] and d >= c["lost"])]

    def _sales(self, d):
        p, r = self.p, self.rng
        per_day = self.target(d) / 26
        if p.window_dressing and d.month == 3 and d.day >= 20:
            per_day *= 2.8
        cash_part = per_day * p.cash_sales
        credit = per_day - cash_part
        custs = self._active_customers(d)
        n = max(1, int(r.gauss(4, 1.2)))
        for _ in range(n):
            c = r.choices(custs, weights=[x["weight"] for x in custs])[0]
            taxable = max(3000.0, r.lognormvariate(0, 0.45) * credit / n)
            self._sale(d, c, taxable)
        if cash_part > 0:
            taxable = cash_part * r.uniform(0.7, 1.3)
            rate = 0.18 if r.random() < p.rate18_share else 0.05
            self.add(d, "Sales", [("Cash", taxable * (1 + rate)), (f"Sales @ {int(rate * 100)}%", -taxable)]
                     + [(l, -a) for l, a in self._tax(p.home, taxable, rate, "Output")], narration="Counter sale")

    def _sale(self, d, c, taxable, vtype="Sales"):
        p, r = self.p, self.rng
        rate = 0.18 if r.random() < p.rate18_share else 0.05
        gross = taxable * (1 + rate)
        if p.round_invoices and gross >= 1_00_000 and r.random() < 0.5:
            gross = round(gross / 10_000) * 10_000
            taxable = gross / (1 + rate)
        v = self.add(d, vtype, [(c["name"], gross), (f"Sales @ {int(rate * 100)}%", -taxable)]
                     + [(l, -a) for l, a in self._tax(c["state"], taxable, rate, "Output")], party=c["name"])
        if not (c["stop"] and d + timedelta(days=p.collect_days) >= c["stop"]):
            delay = p.collect_days + p.collect_drift * self.years(d) + c["delay"] + r.gauss(0, 9)
            self.recv.append((d + timedelta(days=max(0, int(delay))), c["name"], round(gross, 2)))
        return v

    def _purchases(self, d):
        p, r = self.p, self.rng
        if r.random() > 0.6:
            return
        s = r.choices(self.suppliers, weights=[x["weight"] for x in self.suppliers])[0]
        taxable = self.target(d) * (1 - p.gross_margin) / 15.6 * r.lognormvariate(0, 0.3)
        rate = 0.18 if r.random() < p.rate18_share else 0.05
        gross = taxable * (1 + rate)
        self.add(d, "Purchase", [(s["name"], -gross), (f"Purchase @ {int(rate * 100)}%", taxable)]
                 + self._tax(s["state"], taxable, rate, "Input"), party=s["name"])
        delay = p.supplier_days + p.supplier_drift * self.years(d) + r.gauss(0, 8)
        self.pay.append((d + timedelta(days=max(0, int(delay))), s["name"], round(gross, 2)))
        if r.random() < 0.3:
            freight = taxable * 0.01
            self.add(d, "Payment", [("Freight Inward", freight), ("HDFC Bank Current A/c", -freight)])

    def _settle(self, d):
        for item in [x for x in self.recv if x[0] <= d]:
            self.recv.remove(item)
            _, name, amt = item
            self.add(d, "Receipt", [("SBI Cash Credit A/c", amt), (name, -amt)], party=name)
        for item in [x for x in self.pay if x[0] <= d]:
            self.pay.remove(item)
            _, name, amt = item
            self.add(d, "Payment", [(name, amt), ("SBI Cash Credit A/c", -amt)], party=name)

    def _monthly(self, d):
        p, r = self.p, self.rng
        base = p.monthly_sales * p.overheads * (1 + p.overhead_growth) ** self.years(d)
        bank = "HDFC Bank Current A/c"
        self.add(d, "Payment", [("Salary & Wages", base * 0.5), (bank, None)])
        self.add(d, "Payment", [("Rent", base * 0.18), (bank, None)])
        self.add(d, "Payment", [("Electricity", base * 0.12 * r.uniform(0.85, 1.15)), (bank, None)])
        self.add(d, "Payment", [("Transport & Freight", base * 0.1 * r.uniform(0.8, 1.2)), (bank, None)])
        office = base * 0.1 * r.uniform(0.8, 1.2)
        if p.negative_cash:
            self.add(d, "Payment", [("Office Expenses", office), ("Cash", None)])  # no cash in hand to pay it
        else:
            self.add(d, "Payment", [("Office Expenses", office), (bank, None)])
        self.add(d, "Payment", [("Bank Charges", r.uniform(1500, 6000)), ("SBI Cash Credit A/c", None)])
        cc = -self.bal["SBI Cash Credit A/c"]
        if cc > 0:
            self.add(d, "Journal", [("Interest on Cash Credit", cc * 0.105 / 12), ("SBI Cash Credit A/c", None)])
        if p.term_loan and -self.bal["Term Loan - SBI"] > 1:
            outstanding = -self.bal["Term Loan - SBI"]
            principal = min(outstanding, p.term_loan / 60)
            self.add(d, "Payment", [("Term Loan - SBI", principal), ("Interest on Term Loan", outstanding * 0.11 / 12),
                                    ("SBI Cash Credit A/c", None)], narration="EMI")
        for _ in range(p.big_cash_payments):
            amt = round(r.uniform(12_000, 48_000), -2)
            self._ensure_cash(d, amt)
            self.add(d + timedelta(days=r.randint(0, 25)), "Payment", [("Transport & Freight", amt), ("Cash", None)],
                     narration="Labour and transport paid in cash")
        if p.sister_advance:
            name = next(n for n in self.ledgers if n.startswith("Advance to"))
            self.add(d, "Payment", [(name, p.sister_advance * r.uniform(0.7, 1.3)), ("SBI Cash Credit A/c", None)],
                     narration="Advance to group concern")

    def _gst(self, d):
        """Monthly GST: input credit is set off against output tax, the rest paid."""
        p = self.p
        prev = d.replace(day=1) - timedelta(days=1)
        outs: dict[str, float] = {}
        ins: dict[str, float] = {}
        for v in self.vouchers[-6000:]:
            if v.date.year == prev.year and v.date.month == prev.month:
                for l, a in v.entries:
                    if l.endswith("Output") and a < 0:
                        outs[l] = outs.get(l, 0.0) - a
                    elif l.endswith("Input") and a > 0:
                        ins[l] = ins.get(l, 0.0) + a
        out, itc = sum(outs.values()), sum(ins.values())
        if out <= 0:
            return
        used = min(itc, out)
        if used > 0:
            self.add(d, "Journal", [(l, a / out * used) for l, a in outs.items()] + [(l, -a / itc * used) for l, a in ins.items()],
                     narration="GST input credit set off")
        net = out - used
        if p.gst_unpaid_from and d >= p.gst_unpaid_from:
            net *= 0.25  # only part paid; the rest piles up as GST payable
        if net > 1:
            self.add(d, "Payment", [(l, a / out * net) for l, a in outs.items()] + [("SBI Cash Credit A/c", None)],
                     narration="GST paid")

    def _ensure_cash(self, d, amt):
        if self.bal["Cash"] < amt + 20_000:
            self.add(d, "Contra", [("Cash", amt + 50_000), ("HDFC Bank Current A/c", None)], narration="Cash withdrawn")

    def _treasury(self, d):
        bank, cc, cash = "HDFC Bank Current A/c", "SBI Cash Credit A/c", "Cash"
        keep_cash = 3_00_000
        if not self.p.negative_cash and self.bal[cash] > keep_cash + 1_00_000:
            amt = round(self.bal[cash] - keep_cash, -3)
            self.add(d, "Contra", [(bank, amt), (cash, -amt)], narration="Cash deposited")
        if self.p.negative_cash and d.day == 15 and d.month % 3 == 1 and self.bal[cash] < 1_00_000:
            amt = round(1_50_000 - self.bal[cash], -3)  # topped up only once a quarter
            self.add(d, "Contra", [(cash, amt), (bank, None)], narration="Cash withdrawn")
        if d.day == 28 and self.bal[cc] > self.p.monthly_sales * 0.5:
            amt = round(self.bal[cc] - self.p.monthly_sales * 0.2, -5)  # surplus parked in a deposit
            self.add(d, "Payment", [("Fixed Deposit - HDFC", amt), (cc, -amt)], narration="FD booked")
        floor, ceiling = self.p.monthly_sales * 0.04, self.p.monthly_sales * 0.12
        if self.bal[bank] < floor:
            amt = round(floor * 2 - self.bal[bank], -3)
            self.add(d, "Contra", [(bank, amt), (cc, -amt)], narration="Transfer from CC")
        elif self.bal[bank] > ceiling:
            amt = round(self.bal[bank] - floor * 2, -3)
            self.add(d, "Contra", [(cc, amt), (bank, -amt)], narration="Surplus to CC")

    def _special(self):
        p, r = self.p, self.rng
        # Large cash receipts from customers (s.269ST).
        days = sorted(self.start + timedelta(days=r.randint(30, (self.end - self.start).days - 5))
                      for _ in range(int(p.big_cash_receipts * self.years(self.end))))
        for d in days:
            c = r.choice(self.customers[:8])
            amt = round(r.uniform(2_05_000, 4_50_000), -3)
            self.add(d, "Receipt", [("Cash", amt), (c["name"], -amt)], party=c["name"])
            self.add(d + timedelta(days=1), "Contra", [("HDFC Bank Current A/c", amt), ("Cash", -amt)], narration="Cash deposited")
        # Window dressing: a third of each March's late sales come back as credit notes in April.
        if p.window_dressing:
            for year in range(self.start.year + 1, self.end.year + 1):
                march = [v for v in self.vouchers if v.date.year == year and v.date.month == 3 and v.date.day >= 20
                         and v.vtype == "Sales" and v.party]
                for v in march[: len(march) // 3]:
                    when = date(year, 4, r.randint(4, 25))
                    if when <= self.end:
                        self.add(when, "Credit Note", [(l, -a) for l, a in v.entries], party=v.party,
                                 narration=f"Goods returned against invoice {v.number}")
        if p.suspense:
            for i in range(4):
                d = date(2025, 9 + i, 12)
                self.add(d, "Journal", [("Suspense A/c", p.suspense / 4), ("SBI Cash Credit A/c", None)],
                         narration="Pending identification")

    def _finalise(self):
        r = self.rng
        self.vouchers.sort(key=lambda v: v.date)
        backdated = []
        if self.p.backdated:
            pool = [v for v in self.vouchers if v.vtype == "Sales" and date(2025, 10, 1) <= v.date <= date(2026, 3, 31)]
            backdated = r.sample(pool, min(self.p.backdated, len(pool)))
        ids = {id(v) for v in backdated}
        ordered = [v for v in self.vouchers if id(v) not in ids] + backdated
        for i, v in enumerate(ordered, start=1000):
            v.master_id = i
        self.index = list(self.vouchers)
        self.movements: dict[str, list] = {}
        for v in self.index:
            for l, a in v.entries:
                self.movements.setdefault(l, []).append((v.date, a))

    def closing(self, ledger: str, upto: date) -> float:
        return self.ledgers[ledger]["opening"] + sum(a for d, a in self.movements.get(ledger, []) if d <= upto)

    def stock_value(self, d: date) -> float:
        return 0.0

    @property
    def name(self) -> str:
        return self.p.company


if __name__ == "__main__":
    for key, prof in PROFILES.items():
        co = DemoCompany(prof)
        sales = -sum(a for v in co.vouchers for l, a in v.entries if l.startswith("Sales @") and v.date.year == 2025)
        print(f"{key:9s} {len(co.vouchers):6d} vouchers, 2025 sales ₹{sales / 1e7:.2f} Cr, "
              f"CC {co.bal['SBI Cash Credit A/c'] / 1e7:.2f} Cr, cash {co.bal['Cash'] / 1e5:.1f} L")
