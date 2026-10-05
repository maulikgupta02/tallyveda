#!/usr/bin/env python3
"""Writes the demo books the seeder exe imports into an empty Tally company.

    python3 dev/demo_seed_data.py connector/cmd/demoseed/data

Uses the same synthetic company as dev/mock_tally.py (2024-04-01 to today), in
Tally's import format. Files are imported in name order: masters first, so
every voucher finds its ledgers. Each line of a file is one TALLYMESSAGE.
Vouchers are written in creation order, so the back-dated entries the report
should find are keyed in last here too. Cancelled vouchers are left out.
"""

from __future__ import annotations

import gzip
import sys
import uuid
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

sys.path.insert(0, str(Path(__file__).parent))
from mock_tally import GROUPS, Company, tally_amount, tdate  # noqa: E402

# Fixed namespace: the same voucher always gets the same GUID, so a re-run can
# tell its own vouchers from anyone else's.
NS = uuid.UUID("5b1f0c2e-7d4a-4c1e-9a53-74a11e7da0e5")


def msg(xml: str) -> str:
    return f'<TALLYMESSAGE xmlns:UDF="TallyUDF">{xml}</TALLYMESSAGE>'


def groups() -> list[str]:
    return [msg(f'<GROUP NAME={quoteattr(n)} ACTION="Create"><NAME.LIST><NAME>{escape(n)}</NAME></NAME.LIST>'
                f'<PARENT>{escape(p)}</PARENT></GROUP>')
            for n, p, reserved in GROUPS if not reserved]


def voucher_types() -> list[str]:
    return [msg('<VOUCHERTYPE NAME="GST Sales" ACTION="Create"><NAME.LIST><NAME>GST Sales</NAME></NAME.LIST>'
                '<PARENT>Sales</PARENT><NUMBERINGMETHOD>Manual</NUMBERINGMETHOD><ISACTIVE>Yes</ISACTIVE></VOUCHERTYPE>')]


def ledgers(co: Company) -> list[str]:
    # No stock items (valuing them hung TallyPrime 1.1.7 on a test PC, 2026-10-05),
    # so the capital account no longer carries the opening stock.
    opening_stock = co.stock_value(date.fromordinal(co.start.toordinal() - 1))
    out = []
    for l in co.ledgers.values():
        if l["name"] == "Capital - R. Agarwal":
            l = {**l, "opening": l["opening"] + opening_stock}
        if l.get("reserved"):
            continue  # Profit & Loss A/c exists in every company
        # "Cash" is created with every new company too.
        action = "Alter" if l["name"] == "Cash" else "Create"
        extra = ""
        if l.get("credit_period"):
            extra += f'<ISBILLWISEON>Yes</ISBILLWISEON><BILLCREDITPERIOD>{l["credit_period"]} Days</BILLCREDITPERIOD>'
        if l.get("state"):
            extra += f'<COUNTRYNAME>India</COUNTRYNAME><LEDSTATENAME>{escape(l["state"])}</LEDSTATENAME>'
        if l.get("gstin"):
            extra += f'<PARTYGSTIN>{l["gstin"]}</PARTYGSTIN>'
        out.append(msg(f'<LEDGER NAME={quoteattr(l["name"])} ACTION="{action}"><NAME.LIST><NAME>{escape(l["name"])}</NAME></NAME.LIST>'
                       f'<PARENT>{escape(l["parent"])}</PARENT><OPENINGBALANCE>{tally_amount(l["opening"])}</OPENINGBALANCE>'
                       f'{extra}</LEDGER>'))
    return out


def voucher(v, guid: str) -> str:
    def entry(ledger, dr):
        s = (f"<ALLLEDGERENTRIES.LIST><LEDGERNAME>{escape(ledger)}</LEDGERNAME>"
             f"<ISDEEMEDPOSITIVE>{'Yes' if dr > 0 else 'No'}</ISDEEMEDPOSITIVE><AMOUNT>{tally_amount(dr)}</AMOUNT>")
        if ledger == v.party:
            new = v.vtype in ("Sales", "GST Sales", "Purchase")
            ref = escape(f"{v.vtype} {v.number}") if new else ""
            s += (f"<BILLALLOCATIONS.LIST><NAME>{ref}</NAME><BILLTYPE>{'New Ref' if new else 'On Account'}</BILLTYPE>"
                  f"<AMOUNT>{tally_amount(dr)}</AMOUNT></BILLALLOCATIONS.LIST>")
        return s + "</ALLLEDGERENTRIES.LIST>"

    head = (f'<VOUCHER REMOTEID="{guid}" VCHTYPE={quoteattr(v.vtype)} ACTION="Create" OBJVIEW="Accounting Voucher View">'
            f"<DATE>{tdate(v.date)}</DATE><EFFECTIVEDATE>{tdate(v.date)}</EFFECTIVEDATE><GUID>{guid}</GUID>"
            f"<VOUCHERTYPENAME>{escape(v.vtype)}</VOUCHERTYPENAME><VOUCHERNUMBER>{v.number}</VOUCHERNUMBER>"
            f"<PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>")
    if v.party:
        head += f"<PARTYLEDGERNAME>{escape(v.party)}</PARTYLEDGERNAME>"
    if v.narration:
        head += f"<NARRATION>{escape(v.narration)}</NARRATION>"
    return msg(head + "".join(entry(l, a) for l, a in v.entries) + "</VOUCHER>")


def main(out_dir: str) -> None:
    co = Company(end=date.today())
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for old in out.glob("*.xml.gz"):
        old.unlink()
    vouchers = [voucher(v, str(uuid.uuid5(NS, str(v.master_id))))
                for v in sorted(co.vouchers, key=lambda v: v.master_id) if not v.cancelled]
    files = {
        "1-groups": groups(), "3-vouchertypes": voucher_types(), "4-ledgers": ledgers(co), "6-vouchers": vouchers,
    }
    for name, lines in files.items():
        with gzip.open(out / f"{name}.xml.gz", "wt", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    print(f"wrote {len(vouchers)} vouchers, {co.start} to {co.end}, to {out}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "connector/cmd/demoseed/data")
