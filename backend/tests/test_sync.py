"""Incremental sync: resumable full read, then a delta that matches a fresh read."""

import gzip
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from test_analysis import bundle_from_model, mock_tally

AUTH = ("admin", "admin")


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def model():
    return mock_tally.Company()


def gz(obj: dict) -> bytes:
    return gzip.compress(json.dumps(obj).encode())


def months_newest_first(pfrom: str, pto: str) -> list[tuple[str, str, str]]:
    out = []
    d = date.fromisoformat(pfrom).replace(day=1)
    end = date.fromisoformat(pto)
    while d <= end:
        nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
        out.append((d.strftime("%Y-%m"), max(d.isoformat(), pfrom), min((nxt - timedelta(days=1)).isoformat(), pto)))
        d = nxt
    return out[::-1]


def post_months(client, bearer, sync_id, bundle, months):
    for month, frm, to in months:
        vs = [v for v in bundle["vouchers"] if frm <= v["date"] <= to]
        r = client.post(f"/api/connector/sync/{sync_id}/vouchers", headers=bearer,
                        content=gz({"from": frm, "to": to, "month": month, "vouchers": vs}))
        assert r.status_code == 200, r.text


def test_full_sync_resumes_and_delta_matches_a_fresh_read(client, model):
    a = client.post("/api/bank/applications", auth=AUTH,
                    json={"applicant_name": "Shree Ganesh", "monitoring": True}).json()
    d1 = date(2026, 4, 30)
    first = bundle_from_model(model, d1)
    for i, v in enumerate(first["vouchers"]):
        v["alter_id"] = i + 1
    period = first["period"]

    # Full read: masters, then three months, then the connector is interrupted.
    r = client.post("/api/connector/sync/start", headers={"X-Link-Code": a["link_code"]},
                    json={"company": first["company"], "period": period, "monitoring_opt_in": True,
                          "consent": {"accepted_by": "R Agarwal", "monitoring_opt_in": True}})
    plan = r.json()
    assert plan["mode"] == "full" and plan["monitoring"] and plan["token"]
    bearer = {"Authorization": f"Bearer {plan['token']}"}
    masters = {k: first[k] for k in ("groups", "ledgers", "voucher_types", "stock_snapshots", "bills")}
    assert client.post(f"/api/connector/sync/{plan['sync_id']}/masters", headers=bearer, content=gz(masters)).status_code == 200
    months = months_newest_first(period["from"], period["to"])
    post_months(client, bearer, plan["sync_id"], first, months[:3])
    assert client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer).status_code == 409

    # The next run resumes the same session and skips what was received.
    again = client.post("/api/connector/sync/start", headers=bearer,
                        json={"company": first["company"], "period": {"from": "2024-05-15", "to": "2026-05-15"}}).json()
    assert again["sync_id"] == plan["sync_id"] and again["have_masters"]
    assert again["period"] == period and len(again["months_done"]) == 3
    post_months(client, bearer, plan["sync_id"], first, [m for m in months if m[0] not in again["months_done"]])
    assert client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer).json()["monitoring"]

    reports = client.get(f"/api/bank/applications/{a['id']}/reports", auth=AUTH).json()
    assert [r["status"] for r in reports] == ["ready"]

    # Five days later: a delta with the new days, one edited and one deleted voucher.
    d2 = d1 + timedelta(days=5)
    second = bundle_from_model(model, d2)
    plan = client.post("/api/connector/sync/start", headers=bearer,
                       json={"company": first["company"], "period": second["period"]}).json()
    assert plan["mode"] == "delta" and plan["since_alter_id"] == len(first["vouchers"])
    sid = plan["sync_id"]

    affected = set(client.post(f"/api/connector/sync/{sid}/masters", headers=bearer,
                               content=gz({k: second[k] for k in ("groups", "voucher_types")} |
                                          {"ledgers": [{k: v for k, v in l.items() if "balance" not in k}
                                                       for l in second["ledgers"]]})).json()["affected"])
    new_days = [v for v in second["vouchers"] if v["date"] > d1.isoformat()]
    r = client.post(f"/api/connector/sync/{sid}/vouchers", headers=bearer,
                    content=gz({"from": (d1 + timedelta(days=1)).isoformat(), "to": d2.isoformat(), "vouchers": new_days}))
    affected |= set(r.json()["affected"])

    # Re-post an old day with one voucher moved to a different ledger.
    old_day = first["vouchers"][len(first["vouchers"]) // 2]["date"]
    day = [json.loads(json.dumps(v)) for v in first["vouchers"] if v["date"] == old_day]
    edited = day[0]
    moved_from = edited["entries"][0]["ledger"]
    moved_to = next(l["name"] for l in first["ledgers"] if l["name"] not in {e["ledger"] for e in edited["entries"]})
    edited["entries"][0]["ledger"] = moved_to
    r = client.post(f"/api/connector/sync/{sid}/vouchers", headers=bearer,
                    content=gz({"from": old_day, "to": old_day, "vouchers": day}))
    affected |= set(r.json()["affected"])
    assert {moved_from, moved_to} <= affected

    # A recent voucher was deleted in Tally.
    recent = months_newest_first(second["period"]["from"], second["period"]["to"])[1]
    in_month = [v for v in first["vouchers"] if recent[1] <= v["date"] <= recent[2]]
    gone = in_month[0]
    r = client.post(f"/api/connector/sync/{sid}/present", headers=bearer,
                    content=gz({"from": recent[1], "to": recent[2], "guids": [v["guid"] for v in in_month[1:]]}))
    affected |= set(r.json()["affected"])
    assert {e["ledger"] for e in gone["entries"]} <= affected

    fresh = {l["name"]: l for l in second["ledgers"]}
    balances = [{"name": n, "opening_balance": 1.0, "closing_balance": fresh[n]["closing_balance"]} for n in affected]
    client.post(f"/api/connector/sync/{sid}/masters", headers=bearer, content=gz({"balances": balances}))
    assert client.post(f"/api/connector/sync/{sid}/finish", headers=bearer).status_code == 200

    reports = client.get(f"/api/bank/applications/{a['id']}/reports", auth=AUTH).json()
    assert [r["status"] for r in reports] == ["ready", "ready"]

    from app import books, store

    sess = books.get_session(sid)
    bundle = json.loads(gzip.decompress(store.load_report_file(a["id"], reports[0]["id"], "bundle.json.gz")))
    assert bundle["sync"]["session"] == sess["id"] and bundle["period"] == second["period"]
    expected = {v["guid"] for v in second["vouchers"]} - {gone["guid"]}
    assert {v["guid"] for v in bundle["vouchers"]} == expected
    got = {l["name"]: l for l in bundle["ledgers"]}
    for name, l in got.items():
        if name in affected:
            assert l["opening_balance"] == 1.0
        else:
            # Untouched: closing carried forward, opening rolled over the days that left the window.
            assert l["closing_balance"] == pytest.approx(fresh[name]["closing_balance"], abs=0.01), name
    dropped = [v for v in first["vouchers"] if v["date"] < second["period"]["from"] and not v["is_cancelled"]]
    movement = {}
    for v in dropped:
        for e in v["entries"]:
            movement[e["ledger"]] = movement.get(e["ledger"], 0) + e["amount"]
    before = {l["name"]: l for l in first["ledgers"]}
    for name, m in movement.items():
        if name not in affected:
            assert got[name]["opening_balance"] == pytest.approx(before[name]["opening_balance"] + m, abs=0.01), name


def test_one_off_share_token_ends_with_the_share(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "One-off"}).json()
    b = bundle_from_model(model, date(2026, 4, 30), months=2)
    plan = client.post("/api/connector/sync/start", headers={"X-Link-Code": a["link_code"]},
                       json={"company": b["company"], "period": b["period"]}).json()
    assert plan["mode"] == "full" and not plan["monitoring"]
    bearer = {"Authorization": f"Bearer {plan['token']}"}
    client.post(f"/api/connector/sync/{plan['sync_id']}/masters", headers=bearer,
                content=gz({k: b[k] for k in ("groups", "ledgers", "voucher_types")}))
    post_months(client, bearer, plan["sync_id"], b, months_newest_first(b["period"]["from"], b["period"]["to"]))
    assert client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer).status_code == 200
    # The code is used up and the share token no longer works.
    assert client.post("/api/connector/verify", json={"code": a["link_code"]}).status_code == 404
    assert client.post("/api/connector/sync/start", headers=bearer,
                       json={"company": b["company"], "period": b["period"]}).status_code == 401


def test_retention_keeps_recent_first_and_month_end_raw_data(client, model):
    from app import config, store

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Retention", "monitoring": True}).json()
    b = bundle_from_model(model, date(2026, 4, 30), months=3)
    b["consent"] = {"accepted_by": "R Agarwal", "monitoring_opt_in": True}
    r = client.post("/api/connector/upload", content=gz(b), headers={"X-Link-Code": a["link_code"]})
    bearer = {"Authorization": f"Bearer {r.json()['monitor_token']}"}
    for _ in range(4):
        client.post("/api/connector/monitor/upload", content=gz(b), headers=bearer)
    reports = sorted(store.list_reports(a["id"]), key=lambda r: r["created_at"])
    assert len(reports) == 5 and all(r["status"] == "ready" for r in reports)

    # Backdate: first report in January, two in February, one in March, one today.
    now = store.now()
    days = [100, 75, 70, 40, 0]
    with store.db() as conn:
        for r, ago in zip(reports, days):
            conn.execute("UPDATE reports SET created_at = ? WHERE id = ?", ((now - timedelta(days=ago)).isoformat(), r["id"]))
    reports = sorted(store.list_reports(a["id"]), key=lambda r: r["created_at"])
    store.prune_report_files(a["id"], config.KEEP_RAW_DAYS)
    kept = store.raw_report_ids(a["id"])
    by_month = {}
    for r in reports:
        by_month[r["created_at"][:7]] = r["id"]
    expected = {reports[0]["id"], reports[-1]["id"], *by_month.values()}
    assert kept == expected and len(kept) < len(reports)

    pruned = next(r for r in reports if r["id"] not in kept)
    page = client.get(f"/bank/reports/{pruned['id']}", auth=AUTH)
    assert page.status_code == 200 and "<html" in page.text.lower()  # re-rendered from report.json
    assert client.post(f"/bank/reports/{pruned['id']}/recompute", auth=AUTH).status_code == 410
    assert client.get(f"/bank/reports/{pruned['id']}.json", auth=AUTH).status_code == 200


def test_renamed_ledger_schedules_a_full_read(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Rename", "monitoring": True}).json()
    b = bundle_from_model(model, date(2026, 4, 30), months=2)
    plan = client.post("/api/connector/sync/start", headers={"X-Link-Code": a["link_code"]},
                       json={"company": b["company"], "period": b["period"], "monitoring_opt_in": True}).json()
    bearer = {"Authorization": f"Bearer {plan['token']}"}
    client.post(f"/api/connector/sync/{plan['sync_id']}/masters", headers=bearer,
                content=gz({k: b[k] for k in ("groups", "ledgers", "voucher_types")}))
    post_months(client, bearer, plan["sync_id"], b, months_newest_first(b["period"]["from"], b["period"]["to"]))
    client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer)

    used = b["vouchers"][0]["entries"][0]["ledger"]
    renamed = [dict(l, name=l["name"] + " (renamed)") if l["name"] == used else l for l in b["ledgers"]]
    plan = client.post("/api/connector/sync/start", headers=bearer, json={"company": b["company"], "period": b["period"]}).json()
    assert plan["mode"] == "delta"
    client.post(f"/api/connector/sync/{plan['sync_id']}/masters", headers=bearer,
                content=gz({"groups": b["groups"], "voucher_types": b["voucher_types"], "ledgers": renamed}))
    client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer)
    assert client.post("/api/connector/monitor/status", headers=bearer).json()["due"]
    plan = client.post("/api/connector/sync/start", headers=bearer, json={"company": b["company"], "period": b["period"]}).json()
    assert plan["mode"] == "full"
