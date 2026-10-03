"""Server-side copy of each company's books, kept current by incremental syncs.

The connector no longer re-uploads the whole window every day. It opens a
sync session and sends pieces:

- full: masters with balances, then vouchers one month at a time (newest first).
  Months already received are skipped when an interrupted full sync resumes.
- delta: masters (stored fields only), the vouchers on days where anything
  changed since `max_alter_id`, voucher-ID lists of recent months to detect
  deletions, and fresh balances only for ledgers those changes touched.

Every request that replaces vouchers answers with the ledgers it affected
(old and new versions), so the connector knows whose balances to re-read.
Balances of untouched ledgers are carried forward: the closing balance is
unchanged and the opening balance rolls forward over the days that left the
window. `finish` applies everything, prunes vouchers that left the window and
returns a complete bundle, which is stored and analysed exactly like a
one-shot upload, so every report stays reproducible.
"""

from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config, store
from .analysis.book import NON_ACCOUNTING_TYPES, _voucher_base_resolver

MASTER_KEYS = ("groups", "ledgers", "voucher_types")


def _now() -> str:
    return store.now().isoformat()


def get_book(app_id: str) -> dict | None:
    with store.db() as conn:
        row = conn.execute("SELECT * FROM books WHERE application_id = ?", (app_id,)).fetchone()
    if not row:
        return None
    book = dict(row)
    book["state"] = json.loads(book["state"])
    return book


def get_session(sync_id: str) -> dict | None:
    with store.db() as conn:
        row = conn.execute("SELECT * FROM sync_sessions WHERE id = ?", (sync_id,)).fetchone()
    return _session(row) if row else None


def open_session(app_id: str) -> dict | None:
    with store.db() as conn:
        row = conn.execute(
            "SELECT * FROM sync_sessions WHERE application_id = ? AND status = 'open' ORDER BY created_at DESC LIMIT 1",
            (app_id,),
        ).fetchone()
    return _session(row) if row else None


def _session(row) -> dict:
    s = dict(row)
    s["months_done"] = json.loads(s["months_done"])
    s["staged"] = json.loads(s["staged"])
    return s


def _set_session(sync_id: str, **cols) -> None:
    sets = ", ".join(f"{k} = ?" for k in cols)
    vals = [json.dumps(v) if isinstance(v, (dict, list)) else v for v in cols.values()]
    with store.db() as conn:
        conn.execute(f"UPDATE sync_sessions SET {sets}, updated_at = ? WHERE id = ?", (*vals, _now(), sync_id))


class SyncBusy(Exception):
    pass


def start(app: dict, period_from: str, period_to: str, want_full: bool = False, tally_alter_id: int | None = None,
          company_guid: str = "", company_name: str = "") -> dict:
    """Open (or resume) a sync session and tell the connector what to send."""
    app_id = app["id"]
    with store.db() as conn:
        busy = conn.execute(
            "SELECT 1 FROM sync_sessions WHERE application_id = ? AND status = 'finishing' AND updated_at > ?",
            (app_id, (store.now() - timedelta(minutes=15)).isoformat()),
        ).fetchone()
    if busy:
        raise SyncBusy("The previous update is still being processed. Try again in a few minutes.")
    with store.db() as conn:
        # A full sync whose finish never completed (the server restarted): reopen
        # it; every month is already in, so the connector only finishes it.
        conn.execute(
            "UPDATE sync_sessions SET status = 'open' WHERE application_id = ? AND mode = 'full' AND status = 'finishing'",
            (app_id,),
        )
        conn.execute(
            "UPDATE sync_sessions SET status = 'abandoned' WHERE application_id = ? AND mode = 'delta' AND status = 'finishing'",
            (app_id,),
        )
    current = open_session(app_id)
    if current and current["mode"] == "full" and current["created_at"] < (store.now() - timedelta(days=7)).isoformat():
        _set_session(current["id"], status="abandoned", staged={})  # too stale to finish; start over
        current = None
    if current and current["mode"] == "full":
        return _plan(current, None)
    if current:  # an unfinished delta: whatever it applied is redone by the next one
        _set_session(current["id"], status="abandoned")

    book = get_book(app_id)
    full = want_full or book is None
    if book and not full:
        state = book["state"]
        last_full = datetime.fromisoformat(book["last_full_at"]) if book["last_full_at"] else None
        full = (
            last_full is None
            or store.now() - last_full > timedelta(days=config.FULL_SYNC_DAYS)
            # A longer window needs history the book doesn't have.
            or period_from < state["period"]["from"]
            # Alter IDs going backwards means the company was restored from a backup.
            or (tally_alter_id is not None and 0 < tally_alter_id < book["max_alter_id"])
            or (company_guid and state.get("company", {}).get("guid") not in ("", None, company_guid))
            or (company_name and state.get("company", {}).get("name") not in ("", None, company_name))
            or state.get("needs_full", False)
        )
    session = {
        "id": uuid.uuid4().hex,
        "application_id": app_id,
        "mode": "full" if full else "delta",
        "status": "open",
        "period_from": period_from,
        "period_to": period_to,
        "months_done": [],
        "staged": {},
    }
    with store.db() as conn:
        conn.execute(
            "INSERT INTO sync_sessions (id, application_id, mode, status, period_from, period_to, created_at, updated_at)"
            " VALUES (?, ?, ?, 'open', ?, ?, ?, ?)",
            (session["id"], app_id, session["mode"], period_from, period_to, _now(), _now()),
        )
    return _plan(session, book)


def _plan(session: dict, book: dict | None) -> dict:
    plan = {
        "sync_id": session["id"],
        "mode": session["mode"],
        "period": {"from": session["period_from"], "to": session["period_to"]},
        "months_done": session["months_done"],
        "have_masters": "ledgers" in session["staged"],
        "since_alter_id": 0,
        "refresh_ledgers": [],
        "last_to": "",
        "stock_dates": [],
    }
    if session["mode"] == "delta" and book:
        plan["since_alter_id"] = book["max_alter_id"]
        plan["last_to"] = book["state"]["period"]["to"]
        # Vouchers dated after the last sync's end date that are now inside the
        # window (post-dated entries) change those ledgers' closing balances.
        plan["refresh_ledgers"] = sorted(
            _ledgers_between(session["application_id"], book["state"]["period"]["to"], session["period_to"], left_open=True)
        )
        # The stock snapshots a year and two years back drift as the window
        # moves; ask for any that is more than a few days off its date.
        have = [date.fromisoformat(s["as_of"]) for s in book["state"].get("stock_snapshots", [])]
        books_from = book["state"].get("company", {}).get("books_from") or ""
        end = date.fromisoformat(session["period_to"])
        for back in (365, 730):
            want = end - timedelta(days=back)
            if books_from and want.isoformat() < books_from:
                continue
            if have and not any(abs((d - want).days) <= 3 for d in have):
                plan["stock_dates"].append(want.isoformat())
    return plan


def stage(session: dict, payload: dict) -> list[str]:
    """Hold masters, balances, stock and bills until finish. Returns ledgers the
    connector should fetch balances for: in a delta, any ledger the book hasn't
    seen before."""
    staged = session["staged"]
    for key in (*MASTER_KEYS, "stock_snapshots", "bills", "company", "machine", "consent", "connector_version"):
        if key in payload:
            staged[key] = payload[key]
    if "balances" in payload:
        staged.setdefault("balances", {}).update({b["name"]: b for b in payload["balances"]})
    if payload.get("warnings"):
        staged.setdefault("warnings", []).extend(payload["warnings"])
    _set_session(session["id"], staged=staged)
    if session["mode"] != "delta" or "ledgers" not in payload:
        return []
    book = get_book(session["application_id"])
    known = {l["name"] for l in (book["state"].get("ledgers", []) if book else [])}
    return sorted(l["name"] for l in payload["ledgers"] if l["name"] not in known)


def _voucher_row(app_id: str, v: dict) -> tuple:
    guid = v.get("guid") or f"nog:{v.get('date')}:{v.get('type')}:{v.get('number')}:{v.get('master_id')}"
    ledgers = sorted({e["ledger"] for e in v.get("entries", [])})
    return (app_id, guid, v.get("date") or "", int(v.get("alter_id") or 0), json.dumps(ledgers), json.dumps(v))


def replace_vouchers(session: dict, date_from: str, date_to: str, vouchers: list[dict], month: str | None = None) -> list[str]:
    """Replace every stored voucher dated in [date_from, date_to] with these.
    Vouchers dated before the window are not kept, but their ledgers still
    count as affected. Returns the ledgers of old and new versions."""
    app_id = session["application_id"]
    rows = [_voucher_row(app_id, v) for v in vouchers]
    affected: set[str] = set()
    for r in rows:
        affected.update(json.loads(r[4]))
    keep = [r for r in rows if r[2] >= session["period_from"]]
    with store.db() as conn:
        old = conn.execute(
            "SELECT ledgers FROM book_vouchers WHERE application_id = ? AND vdate >= ? AND vdate <= ?",
            (app_id, date_from, date_to),
        ).fetchall()
        guids = [r[1] for r in keep]
        for i in range(0, len(guids), 500):  # a voucher whose date was edited lives outside the range
            part = guids[i : i + 500]
            old += conn.execute(
                f"SELECT ledgers FROM book_vouchers WHERE application_id = ? AND guid IN ({','.join('?' * len(part))})",
                (app_id, *part),
            ).fetchall()
        for r in old:
            affected.update(json.loads(r["ledgers"]))
        conn.execute(
            "DELETE FROM book_vouchers WHERE application_id = ? AND vdate >= ? AND vdate <= ?", (app_id, date_from, date_to)
        )
        if keep:
            conn.executemany(
                "INSERT INTO book_vouchers (application_id, guid, vdate, alter_id, ledgers, body) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (application_id, guid) DO UPDATE SET vdate = excluded.vdate, alter_id = excluded.alter_id,"
                " ledgers = excluded.ledgers, body = excluded.body",
                keep,
            )
    staged = session["staged"]
    staged["max_alter_id"] = max([staged.get("max_alter_id", 0), *(r[3] for r in rows)])
    staged["changed"] = staged.get("changed", False) or bool(rows or old)
    cols = {"staged": staged}
    if month and month not in session["months_done"]:
        session["months_done"].append(month)
        cols["months_done"] = session["months_done"]
    _set_session(session["id"], **cols)
    return sorted(affected)


def present(session: dict, date_from: str, date_to: str, guids: list[str]) -> list[str]:
    """Delete stored vouchers in [date_from, date_to] that Tally no longer has.
    Returns the ledgers they touched."""
    app_id = session["application_id"]
    keep = set(guids)
    with store.db() as conn:
        rows = conn.execute(
            "SELECT guid, ledgers FROM book_vouchers WHERE application_id = ? AND vdate >= ? AND vdate <= ?",
            (app_id, date_from, date_to),
        ).fetchall()
        gone = [r for r in rows if r["guid"] not in keep and not r["guid"].startswith("nog:")]
        if len(gone) > max(20, len(rows) // 3):
            # An empty or broken list from Tally must not wipe the book: re-read
            # everything on the next sync instead.
            session["staged"]["needs_full"] = True
            session["staged"].setdefault("warnings", []).append(
                f"Tally listed {len(rows) - len(gone)} of {len(rows)} vouchers from {date_from} to {date_to};"
                " a full re-read was scheduled instead of deleting them.")
            _set_session(session["id"], staged=session["staged"])
            return []
        for r in gone:
            conn.execute("DELETE FROM book_vouchers WHERE application_id = ? AND guid = ?", (app_id, r["guid"]))
    if gone:
        session["staged"]["changed"] = True
        _set_session(session["id"], staged=session["staged"])
    affected: set[str] = set()
    for r in gone:
        affected.update(json.loads(r["ledgers"]))
    return sorted(affected)


def _ledgers_between(app_id: str, after: str, upto: str, left_open: bool) -> set[str]:
    op = ">" if left_open else ">="
    with store.db() as conn:
        rows = conn.execute(
            f"SELECT ledgers FROM book_vouchers WHERE application_id = ? AND vdate {op} ? AND vdate <= ?",
            (app_id, after, upto),
        ).fetchall()
    out: set[str] = set()
    for r in rows:
        out.update(json.loads(r["ledgers"]))
    return out


def _roll_openings(app_id: str, voucher_types: list[dict], old_from: str, new_from: str) -> dict[str, float]:
    """Movement per ledger over [old_from, new_from): what moves the opening
    balance when the window's start date moves forward."""
    resolve = _voucher_base_resolver(voucher_types)
    movement: dict[str, float] = defaultdict(float)
    with store.db() as conn:
        rows = conn.execute(
            "SELECT body FROM book_vouchers WHERE application_id = ? AND vdate >= ? AND vdate < ?",
            (app_id, old_from, new_from),
        ).fetchall()
    for r in rows:
        v = json.loads(r["body"])
        if v.get("is_cancelled") or v.get("is_optional") or resolve(v.get("type", "")) in NON_ACCOUNTING_TYPES:
            continue
        for e in v.get("entries", []):
            movement[e["ledger"]] += float(e["amount"])
    return movement


def _renamed(before: dict, after: dict, vouchers: list[dict]) -> bool:
    """Tally renames a ledger or voucher type inside every voucher without
    changing their AlterIds, so stored vouchers would keep the old name. If a
    name that was in the masters has gone and stored vouchers still use it,
    re-read everything."""
    def names(state, key):
        return {x["name"] for x in state.get(key, [])}

    gone_ledgers = names(before, "ledgers") - names(after, "ledgers")
    gone_types = names(before, "voucher_types") - names(after, "voucher_types")
    if not gone_ledgers and not gone_types:
        return False
    return any(v.get("type") in gone_types or any(e["ledger"] in gone_ledgers for e in v.get("entries", []))
               for v in vouchers)


def finish(session: dict) -> dict:
    """Apply the session to the book and return the full bundle for the report."""
    app_id = session["application_id"]
    staged = session["staged"]
    book = get_book(app_id)
    pfrom, pto = session["period_from"], session["period_to"]
    if session["mode"] == "full" or book is None:
        state = {k: staged.get(k, []) for k in (*MASTER_KEYS, "stock_snapshots", "bills")}
        balances = staged.get("balances", {})
        for l in state["ledgers"]:
            if l["name"] in balances:
                l.update(opening_balance=balances[l["name"]]["opening_balance"],
                         closing_balance=balances[l["name"]]["closing_balance"])
        last_full = _now()
        max_alter = staged.get("max_alter_id", 0)
    else:
        state = dict(book["state"])
        old_from, old_to = state["period"]["from"], state["period"]["to"]
        for key in ("groups", "voucher_types"):
            if key in staged:
                state[key] = staged[key]
        roll = _roll_openings(app_id, state.get("voucher_types", []), old_from, pfrom) if pfrom > old_from else {}
        old = {l["name"]: l for l in state.get("ledgers", [])}
        balances = staged.get("balances", {})
        ledgers = []
        for m in staged.get("ledgers") or list(old.values()):
            l = dict(m)
            if l["name"] in balances:
                b = balances[l["name"]]
                l["opening_balance"], l["closing_balance"] = b["opening_balance"], b["closing_balance"]
            elif l["name"] in old:
                prev = old[l["name"]]
                l["opening_balance"] = round(prev.get("opening_balance", 0) + roll.get(l["name"], 0), 2)
                l["closing_balance"] = prev.get("closing_balance", 0)
            ledgers.append(l)
        state["ledgers"] = ledgers
        if "bills" in staged:
            state["bills"] = staged["bills"]
        snaps = {s["as_of"]: s for s in state.get("stock_snapshots", [])}
        if "stock_snapshots" in staged:
            snaps.update({s["as_of"]: s for s in staged["stock_snapshots"]})
        elif not staged.get("changed") and old_to in snaps and pto not in snaps:
            snaps[pto] = {**snaps.pop(old_to), "as_of": pto}  # nothing moved, so stock is unchanged
        oldest = (date.fromisoformat(pto) - timedelta(days=760)).isoformat()
        state["stock_snapshots"] = sorted((s for d, s in snaps.items() if d >= oldest), key=lambda s: s["as_of"])
        last_full = book["last_full_at"]
        max_alter = max(book["max_alter_id"], staged.get("max_alter_id", 0))

    for key in ("company", "machine", "consent", "connector_version"):
        if key in staged:
            state[key] = staged[key]
    state["period"] = {"from": pfrom, "to": pto}
    state["needs_full"] = bool(staged.get("needs_full")) or (session["mode"] == "delta" and state.get("needs_full", False))

    with store.db() as conn:
        conn.execute("DELETE FROM book_vouchers WHERE application_id = ? AND vdate < ?", (app_id, pfrom))
        rows = conn.execute(
            "SELECT body FROM book_vouchers WHERE application_id = ? AND vdate >= ? AND vdate <= ? ORDER BY vdate, guid",
            (app_id, pfrom, pto),
        ).fetchall()
    vouchers = [json.loads(r["body"]) for r in rows]
    del rows
    if session["mode"] == "delta" and book is not None and not state["needs_full"]:
        state["needs_full"] = _renamed(book["state"], state, vouchers)

    with store.db() as conn:
        conn.execute(
            "INSERT INTO books (application_id, state, max_alter_id, last_full_at, updated_at) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (application_id) DO UPDATE SET state = excluded.state, max_alter_id = excluded.max_alter_id,"
            " last_full_at = excluded.last_full_at, updated_at = excluded.updated_at",
            (app_id, json.dumps(state), max_alter, last_full, _now()),
        )
        # The staged masters can be megabytes; the book now holds them.
        conn.execute("UPDATE sync_sessions SET status = 'done', staged = '{}', updated_at = ? WHERE id = ?",
                     (_now(), session["id"]))

    return {
        "schema_version": 1,
        "connector_version": state.get("connector_version", ""),
        "extracted_at": _now(),
        "consent": state.get("consent", {}),
        "machine": state.get("machine", {}),
        "company": state.get("company", {}),
        "period": state["period"],
        "groups": state.get("groups", []),
        "ledgers": state.get("ledgers", []),
        "voucher_types": state.get("voucher_types", []),
        "stock_snapshots": state.get("stock_snapshots", []),
        "bills": state.get("bills", []),
        "vouchers": vouchers,
        "warnings": staged.get("warnings", []),
        "sync": {"mode": session["mode"], "session": session["id"], "needs_full": state["needs_full"]},
    }


def mark(session_id: str, status: str) -> None:
    _set_session(session_id, status=status)


def forget(app_id: str) -> None:
    """Drop the book after a one-shot upload from an older connector, so the
    next incremental sync starts with a full read instead of merging into a
    copy that no longer matches the latest report."""
    with store.db() as conn:
        conn.execute("DELETE FROM books WHERE application_id = ?", (app_id,))
        conn.execute("DELETE FROM book_vouchers WHERE application_id = ?", (app_id,))
        conn.execute("UPDATE sync_sessions SET status = 'abandoned', staged = '{}' WHERE application_id = ? AND status = 'open'",
                     (app_id,))


def prune_sessions(app_id: str, keep_days: int) -> None:
    """Old finished or abandoned sessions are only a log; drop them."""
    cutoff = (store.now() - timedelta(days=keep_days)).isoformat()
    with store.db() as conn:
        conn.execute(
            "DELETE FROM sync_sessions WHERE application_id = ? AND status <> 'open' AND updated_at < ?", (app_id, cutoff)
        )
        conn.execute(
            "UPDATE sync_sessions SET staged = '{}' WHERE application_id = ? AND status IN ('abandoned', 'failed')", (app_id,)
        )
