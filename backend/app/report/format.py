"""Indian-style number formatting for reports."""

from __future__ import annotations


def inr(x: float | None, precise: bool = False) -> str:
    """₹ in lakh / crore: 12,34,567 -> ₹12.35 L; 3,45,00,000 -> ₹3.45 Cr."""
    if x is None:
        return "n/a"
    sign = "-" if x < 0 else ""
    a = abs(x)
    if not precise:
        if a >= 1e7:
            return f"{sign}₹{a / 1e7:,.2f} Cr"
        if a >= 1e5:
            return f"{sign}₹{a / 1e5:,.2f} L"
    return f"{sign}₹{group_indian(round(a))}"


def group_indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{x * 100:.{digits}f}%"


def days(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.0f} days"


def ratio(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}×"
