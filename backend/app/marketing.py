"""Static content and small pure helpers for the public marketing home page
(`GET /`, `templates/home.html`). Mirrors the approved design's `renderVals()` —
sample dashboard bars/alerts, the "for banks" tab panels, and the cash-cycle
calculator's thresholds — as server-rendered defaults; `static/home.js`
(inline in the template) takes over the interactive bits client-side. No
real customer data: the sample borrower and sample numbers below are the
same placeholder figures the approved design used.

Copy (headings, body text, FAQ) lives in `templates/home.html` itself, not
here — this module only holds the pieces that are actually data: chart
series, tab panel tiles, and numeric thresholds.
"""

from __future__ import annotations

MONTHS = ["Oct", "Nov", "Dec", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep"]
SALES_LAKH = [140, 145, 158, 166, 165, 188, 124, 128, 146, 152, 160, 168]


def hero_bars() -> list[dict]:
    """The sample dashboard's 12-month bar chart. Rendered at full height
    server-side (no layout shift); `home.js` replays the "rise" animation on
    load unless the visitor has prefers-reduced-motion set."""
    peak = max(SALES_LAKH)
    bars = []
    for i, (month, value) in enumerate(zip(MONTHS, SALES_LAKH)):
        bars.append({
            "pct": round(value / peak * 100),
            "latest": i == len(SALES_LAKH) - 1,
            "title": f"{month}: ₹{value} L",
        })
    return bars


# Today's-alerts feed pool. home.js rotates the visible 3 every ~3s
# (prefers-reduced-motion: static, shows the first 3 and never rotates).
ALERT_FEED_POOL = [
    {"who": "Shree Ganesh Traders", "what": "New red flag: stuck receivables", "color": "#d92d20", "time": "06:14"},
    {"who": "Om Steel Fabricators", "what": "Sales down 12% on last month", "color": "#ea6b2d", "time": "06:05"},
    {"who": "Patel Brothers Textiles", "what": "Receivables up 19%", "color": "#f2a900", "time": "05:58"},
    {"who": "Kaveri Agro Foods", "what": "Top customer now 21% of sales", "color": "#f2a900", "time": "05:40"},
    {"who": "Balaji Distributors", "what": "Connector checked in, books current", "color": "#12a150", "time": "05:31"},
]

# "For banks" tabbed dashboard preview: 5 tabs, 6 tiles each.
BANK_TABS = [
    ("overview", "Overview"),
    ("recv", "Receivables"),
    ("wc", "Working capital"),
    ("debt", "Debt"),
    ("bank", "Banking & tax"),
]

BANK_PANELS = {
    "overview": {
        "note": "Changes since yesterday, then the headline numbers",
        "tiles": [
            ("Sales, 12 months", "₹18.40 Cr", "+13.6% on last year", "Green", "#12a150"),
            ("EBITDA margin", "7.9%", "+0.6 pt on last year", "Amber", "#f2a900"),
            ("Debtor days", "72 d", "was 64 d a year ago", "Amber", "#f2a900"),
            ("DSCR", "1.35x", "was 1.60x yesterday", "Amber", "#f2a900"),
            ("Debt / net worth", "1.80x", "₹2.35 Cr of debt", "Green", "#12a150"),
            ("Red flags", "3", "1 high, 2 medium", "Red", "#d92d20"),
        ],
    },
    "recv": {
        "note": "Who owes what, and for how long",
        "tiles": [
            ("Receivables", "₹1.37 Cr", "+22% since last refresh", "Amber", "#f2a900"),
            ("Owed over 90 days", "18%", "₹24.6 L", "Amber", "#f2a900"),
            ("Collection ratio", "94%", "collected vs billed", "Green", "#12a150"),
            ("Top customer", "24%", "of sales", "Amber", "#f2a900"),
            ("Top 5 customers", "58%", "of sales", "Amber", "#f2a900"),
            ("Stuck debtors", "1", "₹38.6 L, no payment in 90 d", "Red", "#d92d20"),
        ],
    },
    "wc": {
        "note": "How long cash stays tied up",
        "tiles": [
            ("Cash cycle", "95 d", "stock + collect − supplier credit", "Amber", "#f2a900"),
            ("Inventory days", "77 d", "₹1.46 Cr of stock", "Amber", "#f2a900"),
            ("Current ratio", "1.18x", "assets / liabilities", "Amber", "#f2a900"),
            ("Net working capital", "₹38 L", "current assets − liabilities", "Green", "#12a150"),
            ("Creditor days", "54 d", "was 51 d", "Green", "#12a150"),
            ("Payables over 90 days", "12%", "₹11 L", "Green", "#12a150"),
        ],
    },
    "debt": {
        "note": "Can the business carry what it owes",
        "tiles": [
            ("DSCR", "1.35x", "was 1.60x yesterday", "Amber", "#f2a900"),
            ("Interest cover", "2.1x", "EBITDA / interest", "Amber", "#f2a900"),
            ("Debt / net worth", "1.80x", "TOL / TNW 2.60x", "Green", "#12a150"),
            ("Debt service, 12 mo", "₹1.08 Cr", "interest + principal", "Green", "#12a150"),
            ("EMIs on time", "11 of 12", "1 paid 14 days late", "Amber", "#f2a900"),
            ("Loans", "4", "₹2.35 Cr outstanding", "Green", "#12a150"),
        ],
    },
    "bank": {
        "note": "What the bank account says",
        "tiles": [
            ("Month-end balance", "₹62 L", "September", "Green", "#12a150"),
            ("Lowest balance", "₹6.2 L", "on 26 Aug", "Amber", "#f2a900"),
            ("Cash receipts", "14%", "of all money received", "Amber", "#f2a900"),
            ("Large cash receipts", "3", "≥ ₹2 L each, ₹7.2 L", "Red", "#d92d20"),
            ("GST collected", "₹2.96 Cr", "16.1% of sales", "Green", "#12a150"),
            ("Statutory dues now", "₹18.4 L", "GST + TDS", "Amber", "#f2a900"),
        ],
    },
}


def bank_panel(key: str) -> dict:
    tiles = BANK_PANELS[key]["tiles"]
    return {
        "note": BANK_PANELS[key]["note"],
        "tiles": [
            {"label": label, "value": value, "ctx": ctx, "rating": rating, "dot": dot}
            for label, value, ctx, rating, dot in tiles
        ],
    }


# Cash-cycle calculator defaults and verdict thresholds (see design.md).
CASH_CYCLE_DEFAULTS = {"stock": 77, "debtor": 72, "creditor": 54}


def cash_cycle(stock: int, debtor: int, creditor: int) -> dict:
    ccc = stock + debtor - creditor
    pct = lambda v: round(v / 180 * 100)
    if ccc <= 60:
        verdict = "Healthy: cash comes back quickly enough to fund growth without much borrowing."
    elif ccc <= 120:
        verdict = f"Stretched: the business needs working-capital credit to bridge about {ccc} days."
    else:
        verdict = "Strained: over four months of cash is tied up, so a delay from one big customer hurts."
    return {
        "stock": stock, "debtor": debtor, "creditor": creditor,
        "ccc": max(ccc, 0),
        "w_stock": pct(stock), "w_debtor": pct(debtor), "w_creditor": pct(creditor),
        "verdict": verdict,
    }


FAQ = [
    ("Does it change anything in Tally?",
     "No. It reads ledgers, vouchers, outstanding bills and stock values through Tally's own "
     "data connection and writes nothing back."),
    ("Which versions of Tally work?",
     "TallyPrime and Tally.ERP 9, on a desktop or on a cloud server, as long as Tally's "
     "connectivity setting is switched on."),
    ("Does the MSME need to install anything?",
     "No. It is one Windows file that runs on 32- and 64-bit PCs and needs no admin rights."),
    ("How fresh is the data?",
     "The first share covers 24 months. With daily updates on, the connector sends a fresh "
     "copy every day that Tally is open."),
    ("Who can see an MSME's data?",
     "Only the bank that requested it and the MSME itself. Each bank sees its own borrowers "
     "and nobody else's."),
    ("Can an MSME use it without a bank?",
     "Yes. We can set an MSME up directly with its own dashboard, with no lender involved."),
]

LEAD_KINDS = ["A bank or NBFC", "An MSME", "A CA or advisor"]

TITLE = "Tally Connector — Bank-Grade MSME Credit Analysis from Tally"
DESCRIPTION = (
    "Turn an MSME's TallyPrime or Tally.ERP 9 books into a bank-grade credit report in "
    "minutes, refreshed daily, for faster MSME credit assessment and monitoring."
)
# Bump when the home page's visible content changes meaningfully (sitemap.xml's <lastmod>).
HOME_LASTMOD = "2026-10-02"


def json_ld(public_url: str) -> dict:
    """Organization + WebSite + SoftwareApplication + FAQPage, matching the visible
    FAQ exactly. No ratings or prices — none exist for this product."""
    return {
        "@context": "https://schema.org",
        "@graph": [
            {
                "@type": "Organization",
                "@id": f"{public_url}/#org",
                "name": "Tally Connector",
                "url": public_url,
                "logo": f"{public_url}/static/og-image.png",
            },
            {
                "@type": "WebSite",
                "@id": f"{public_url}/#website",
                "name": "Tally Connector",
                "url": public_url,
                "publisher": {"@id": f"{public_url}/#org"},
            },
            {
                "@type": "SoftwareApplication",
                "name": "Tally Connector",
                "url": public_url,
                "applicationCategory": ["BusinessApplication", "FinanceApplication"],
                "operatingSystem": "Windows",
            },
            {
                "@type": "FAQPage",
                "mainEntity": [
                    {
                        "@type": "Question",
                        "name": q,
                        "acceptedAnswer": {"@type": "Answer", "text": a},
                    }
                    for q, a in FAQ
                ],
            },
        ],
    }
