"""Normalised view of a Tally data bundle.

The connector uploads a bundle (see connector/internal/extract/bundle.go). All
amounts in the bundle are *debit-positive*: a debit balance / debit entry is
positive, a credit is negative. Tally itself uses the opposite sign in XML; the
connector flips it.

`Book` classifies every ledger by walking the group tree up to the first
predefined Tally group, and filters vouchers down to the ones that hit the books
(no cancelled, optional or order/memo vouchers).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

# Predefined Tally groups -> category. The first match walking up from a
# ledger's parent wins, so "Bank OD A/c" (child of "Loans (Liability)") maps to
# bank_od, and a custom "Export Debtors" group under "Sundry Debtors" maps to
# debtor.
RESERVED_CATEGORIES = {
    "Sundry Debtors": "debtor",
    "Sundry Creditors": "creditor",
    "Sales Accounts": "sales",
    "Purchase Accounts": "purchase",
    "Bank Accounts": "bank",
    "Bank OD A/c": "bank_od",
    "Bank OCC A/c": "bank_od",
    "Cash-in-Hand": "cash",
    "Secured Loans": "secured_loan",
    "Unsecured Loans": "unsecured_loan",
    "Loans (Liability)": "loan",
    "Duties & Taxes": "tax",
    "Provisions": "provision",
    "Stock-in-Hand": "stock",
    "Direct Expenses": "direct_expense",
    "Indirect Expenses": "indirect_expense",
    "Direct Incomes": "direct_income",
    "Indirect Incomes": "indirect_income",
    "Loans & Advances (Asset)": "advance",
    "Deposits (Asset)": "deposit",
    "Investments": "investment",
    "Fixed Assets": "fixed_asset",
    "Capital Account": "capital",
    "Reserves & Surplus": "reserves",
    "Current Assets": "current_asset",
    "Current Liabilities": "current_liability",
    "Suspense A/c": "suspense",
    "Misc. Expenses (ASSET)": "misc_asset",
    "Branch / Divisions": "branch",
}

PL_CATEGORIES = {
    "sales",
    "purchase",
    "direct_expense",
    "indirect_expense",
    "direct_income",
    "indirect_income",
}
LIQUID_CATEGORIES = {"bank", "bank_od", "cash"}

# Voucher base types that never post to the books.
NON_ACCOUNTING_TYPES = {
    "Sales Order",
    "Purchase Order",
    "Delivery Note",
    "Receipt Note",
    "Rejections In",
    "Rejections Out",
    "Memorandum",
    "Reversing Journal",
    "Physical Stock",
    "Stock Journal",
    "Material In",
    "Material Out",
    "Job Work In Order",
    "Job Work Out Order",
    "Attendance",
}

PRIMARY_MARKERS = {"", "primary"}


def parse_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value[:10])


@dataclass
class Ledger:
    name: str
    parent: str
    category: str
    primary: str
    closing: float  # debit-positive, as at the bundle's period end
    opening: float
    state: str = ""
    gstin: str = ""
    credit_period_days: int | None = None


@dataclass
class Entry:
    ledger: Ledger
    amount: float  # debit-positive


@dataclass
class Voucher:
    date: date
    type: str
    base_type: str
    number: str
    party: str
    master_id: int | None
    entries: list[Entry]

    def total(self, category: str) -> float:
        return sum(e.amount for e in self.entries if e.ledger.category == category)

    def has(self, *categories: str) -> bool:
        return any(e.ledger.category in categories for e in self.entries)


@dataclass
class Book:
    company: dict
    period_from: date
    period_to: date
    ledgers: dict[str, Ledger]
    vouchers: list[Voucher]
    stock_snapshots: dict[date, float]
    bills: list[dict]
    # Per-item snapshots and the vouchers' stock lines (date, item, qty, value;
    # positive = in), for valuing stock on dates Tally wasn't asked about.
    stock_items: dict[date, list[dict]] = field(default_factory=dict)
    stock_moves: list[tuple[date, str, float, float]] = field(default_factory=list)
    # Vouchers on or before this date came from connectors that didn't send
    # stock lines, so stock can't be rolled back past it.
    stock_lines_from: date | None = None
    warnings: list[str] = field(default_factory=list)
    excluded_vouchers: int = 0
    cancelled_vouchers: int = 0
    unbalanced_vouchers: int = 0

    @property
    def as_of(self) -> date:
        return self.period_to

    def by_category(self, *categories: str) -> list[Ledger]:
        return [l for l in self.ledgers.values() if l.category in categories]

    def stock_value(self, on: date) -> float | None:
        """Closing stock on a date: a snapshot from Tally within 7 days, else
        worked out from the next snapshot and the stock moved in between."""
        best = None
        for d, v in self.stock_snapshots.items():
            gap = abs((d - on).days)
            if gap <= 7 and (best is None or gap < best[0]):
                best = (gap, v)
        if best:
            return best[1]
        return self._rolled_back_stock(on)

    def _rolled_back_stock(self, on: date) -> float | None:
        """Each item's quantity on `on` is the snapshot quantity less what came
        in (plus what went out) since; it is valued at the item's average
        purchase rate over the year to `on`, else at the snapshot's rate."""
        later = sorted(d for d in self.stock_items if d > on)
        if not later or (self.stock_lines_from and on < self.stock_lines_from):
            return None
        anchor = later[0]
        moved: dict[str, float] = defaultdict(float)
        bought: dict[str, list[float]] = defaultdict(lambda: [0.0, 0.0])
        for d, item, qty, value in self.stock_moves:
            if on < d <= anchor:
                moved[item] += qty
            if on - timedelta(days=365) < d <= on and qty > 0 and value > 0:
                bought[item][0] += qty
                bought[item][1] += value
        total = 0.0
        for it in self.stock_items[anchor]:
            qty_then = float(it.get("closing_qty") or 0) - moved.get(it["name"], 0.0)
            if qty_then <= 0:
                continue
            q, v = bought.get(it["name"], (0.0, 0.0))
            anchor_qty, anchor_value = float(it.get("closing_qty") or 0), abs(float(it.get("closing_value") or 0))
            rate = v / q if q else (anchor_value / anchor_qty if anchor_qty else None)
            if rate is None:
                return None  # can't value this item: better no figure than a low one
            total += qty_then * rate
        return total

    def entries_between(self, start: date, end: date):
        for v in self.vouchers:
            if start <= v.date <= end:
                yield v


def _group_resolver(groups: list[dict]):
    by_name = {g["name"]: g for g in groups}

    def resolve(group_name: str) -> tuple[str, str]:
        """Return (category, primary group name) for a group."""
        category = None
        primary = group_name
        seen = set()
        name = group_name
        while name and name.lower() not in PRIMARY_MARKERS and name not in seen:
            seen.add(name)
            g = by_name.get(name, {"name": name, "parent": ""})
            key = g.get("reserved_name") or g["name"]
            if category is None and key in RESERVED_CATEGORIES:
                category = RESERVED_CATEGORIES[key]
            primary = key
            name = g.get("parent", "")
        return category or "other", primary

    return resolve


def _voucher_base_resolver(voucher_types: list[dict]):
    by_name = {v["name"]: v for v in voucher_types}

    def resolve(type_name: str) -> str:
        name, seen = type_name, set()
        while name in by_name and name not in seen:
            seen.add(name)
            vt = by_name[name]
            if vt.get("reserved_name"):
                return vt["reserved_name"]
            parent = vt.get("parent", "")
            if not parent or parent.lower() in PRIMARY_MARKERS:
                return name
            name = parent
        return name

    return resolve


def build_book(bundle: dict) -> Book:
    resolve_group = _group_resolver(bundle.get("groups", []))
    resolve_vtype = _voucher_base_resolver(bundle.get("voucher_types", []))

    ledgers: dict[str, Ledger] = {}
    for raw in bundle.get("ledgers", []):
        category, primary = resolve_group(raw.get("parent", ""))
        if raw["name"] == "Profit & Loss A/c" or raw.get("reserved_name") == "Profit & Loss A/c":
            category, primary = "pl_account", "Profit & Loss A/c"
        ledgers[raw["name"]] = Ledger(
            name=raw["name"],
            parent=raw.get("parent", ""),
            category=category,
            primary=primary,
            closing=float(raw.get("closing_balance") or 0),
            opening=float(raw.get("opening_balance") or 0),
            state=raw.get("state") or "",
            gstin=raw.get("gstin") or "",
            credit_period_days=raw.get("credit_period_days"),
        )

    warnings = list(bundle.get("warnings", []))
    vouchers: list[Voucher] = []
    excluded = cancelled = unbalanced = 0
    unknown_ledgers: set[str] = set()
    for raw in bundle.get("vouchers", []):
        if raw.get("is_cancelled"):
            cancelled += 1
            continue
        base = resolve_vtype(raw.get("type", ""))
        if raw.get("is_optional") or base in NON_ACCOUNTING_TYPES:
            excluded += 1
            continue
        entries = []
        for e in raw.get("entries", []):
            ledger = ledgers.get(e["ledger"])
            if ledger is None:
                unknown_ledgers.add(e["ledger"])
                ledger = Ledger(e["ledger"], "", "other", "", 0.0, 0.0)
                ledgers[e["ledger"]] = ledger
            entries.append(Entry(ledger, float(e["amount"])))
        if not entries:
            excluded += 1
            continue
        if abs(sum(e.amount for e in entries)) > 1.0:
            unbalanced += 1
        vouchers.append(
            Voucher(
                date=parse_date(raw["date"]),
                type=raw.get("type", ""),
                base_type=base,
                number=raw.get("number", ""),
                party=raw.get("party", ""),
                master_id=raw.get("master_id"),
                entries=entries,
            )
        )
    vouchers.sort(key=lambda v: v.date)

    if unknown_ledgers:
        warnings.append(
            f"{len(unknown_ledgers)} ledgers used in vouchers were missing from the ledger master list."
        )

    snapshots, stock_items = {}, {}
    for snap in bundle.get("stock_snapshots", []):
        d = parse_date(snap["as_of"])
        snapshots[d] = abs(sum(float(i.get("closing_value") or 0) for i in snap.get("items", [])))
        if all("closing_qty" in i for i in snap.get("items", [])):
            stock_items[d] = snap.get("items", [])
    stock_moves, lines_from = [], None
    for raw in bundle.get("vouchers", []):
        d = parse_date(raw.get("date"))
        if d is None:
            continue
        # Older vouchers carry no stock lines, and a physical stock count sets
        # quantities instead of moving them: stock can't be rolled back past either.
        if "inventory" not in raw or (resolve_vtype(raw.get("type", "")) == "Physical Stock" and raw["inventory"]):
            lines_from = d if lines_from is None or d > lines_from else lines_from
            continue
        if raw.get("is_cancelled") or raw.get("is_optional"):
            continue
        for line in raw["inventory"] or []:
            stock_moves.append((d, line["item"], float(line.get("qty") or 0), float(line.get("value") or 0)))

    period = bundle.get("period", {})
    bills = bills_from_vouchers(bundle.get("vouchers", []), ledgers)  # never Tally's Bills collection
    return Book(
        company=bundle.get("company", {}),
        period_from=parse_date(period.get("from")) or (vouchers[0].date if vouchers else date.today()),
        period_to=parse_date(period.get("to")) or (vouchers[-1].date if vouchers else date.today()),
        ledgers=ledgers,
        vouchers=vouchers,
        stock_snapshots=snapshots,
        bills=bills,
        stock_items=stock_items,
        stock_moves=stock_moves,
        stock_lines_from=lines_from,
        warnings=warnings,
        excluded_vouchers=excluded,
        cancelled_vouchers=cancelled,
        unbalanced_vouchers=unbalanced,
    )


def bills_from_vouchers(raw_vouchers: list[dict], ledgers: dict[str, Ledger]) -> list[dict]:
    """Bill-wise outstanding worked out from the bill allocations on vouchers,
    instead of asking Tally (its Bills collection froze Tally, 2026-10-05).

    A bill is opened by a "New Ref" and settled by "Agst Ref" entries with the
    same name. Bills raised before the vouchers we hold can't be seen, so a
    ledger's bills are used only when they add up to its closing balance; any
    other ledger is aged FIFO on invoices instead."""
    totals: dict[tuple[str, str], float] = defaultdict(float)
    opened: dict[tuple[str, str], str] = {}
    for v in raw_vouchers:
        if v.get("is_cancelled") or v.get("is_optional"):
            continue
        for e in v.get("entries", []):
            for b in e.get("bills") or []:
                kind = (b.get("type") or "").strip().lower()
                if not b.get("name") or kind not in ("new ref", "agst ref"):
                    continue
                key = (e["ledger"], b["name"])
                totals[key] += float(b.get("amount") or 0)
                if kind == "new ref" and (key not in opened or v["date"] < opened[key]):
                    opened[key] = v["date"]
    by_ledger: dict[str, list[dict]] = defaultdict(list)
    for (ledger, name), amount in totals.items():
        # A settlement of a bill raised before our vouchers is not an open bill;
        # whatever it leaves open shows up in the closing-balance check below.
        if abs(amount) > 0.5 and (ledger, name) in opened:
            by_ledger[ledger].append({"ledger": ledger, "name": name, "date": opened.get((ledger, name), ""),
                                      "closing_balance": round(amount, 2)})
    out = []
    for name, bills in by_ledger.items():
        led = ledgers.get(name)
        total = sum(b["closing_balance"] for b in bills)
        if led and abs(total - led.closing) <= max(1.0, abs(led.closing) * 0.005):
            out.extend(bills)
    return out


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def month_range(start: date, end: date) -> list[str]:
    keys, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return keys


def windows(as_of: date) -> dict[str, tuple[date, date]]:
    """Last-twelve-months and the twelve months before it."""
    return {
        "ltm": (as_of - timedelta(days=364), as_of),
        "prior": (as_of - timedelta(days=729), as_of - timedelta(days=365)),
    }


def zero_dict():
    return defaultdict(float)
