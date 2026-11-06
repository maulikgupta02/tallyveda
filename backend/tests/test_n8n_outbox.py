"""MATTR AI bridge: outbox rows written with the change they describe, signed
and ordered delivery with backoff, purges, the consent gate and chunking."""

import gzip
import hashlib
import hmac
import json
import logging
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from test_analysis import bundle_from_model, mock_tally
from test_sync import AUTH, gz, months_newest_first, post_months

SECRET = "test-secret-not-real"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config, n8n

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "N8N_WEBHOOK_URL", "https://n8n.invalid/webhook/mattr-bridge")
    monkeypatch.setattr(config, "N8N_WEBHOOK_SECRET", SECRET)
    monkeypatch.setattr(n8n, "kick", lambda: None)  # tests deliver explicitly
    from app.main import app

    return TestClient(app)


@pytest.fixture(scope="module")
def model():
    return mock_tally.Company()


@pytest.fixture(scope="module")
def bundle(model):
    return bundle_from_model(model, months=6)


class Wire:
    """Stands in for n8n: records each request, answers with `answer(part)`."""

    def __init__(self, monkeypatch, answer=lambda part: 200):
        from app import n8n

        self.calls = []
        self.answer = answer
        monkeypatch.setattr(n8n, "_post", self.post)

    def post(self, url, body, headers):
        part = json.loads(gzip.decompress(body))
        self.calls.append({"url": url, "body": body, "headers": headers, "part": part})
        return self.answer(part)


def new_app(client, name="Shree Ganesh", forward=True):
    from app import store

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": name, "monitoring": True}).json()
    with store.db() as conn:
        conn.execute("UPDATE applications SET mattr_forward = ? WHERE id = ?", (int(forward), a["id"]))
    return a


def full_sync(client, a, bundle, bearer=None):
    period = bundle["period"]
    headers = bearer or {"X-Link-Code": a["link_code"]}
    plan = client.post("/api/connector/sync/start", headers=headers,
                       json={"company": bundle["company"], "period": period, "monitoring_opt_in": True,
                             "want_full": True, "consent": {"accepted_by": "R Agarwal", "monitoring_opt_in": True}}).json()
    bearer = bearer or {"Authorization": f"Bearer {plan['token']}"}
    masters = {k: bundle[k] for k in ("groups", "ledgers", "voucher_types", "stock_snapshots", "bills")}
    assert client.post(f"/api/connector/sync/{plan['sync_id']}/masters", headers=bearer, content=gz(masters)).status_code == 200
    post_months(client, bearer, plan["sync_id"], bundle, months_newest_first(period["from"], period["to"]))
    r = client.post(f"/api/connector/sync/{plan['sync_id']}/finish", headers=bearer)
    assert r.status_code == 200, r.text
    return bearer, plan["sync_id"]


def outbox(app_id=None):
    from app import store

    with store.db() as conn:
        rows = conn.execute("SELECT * FROM n8n_outbox ORDER BY application_id, batch_seq").fetchall()
    return [dict(r) for r in rows if app_id is None or r["application_id"] == app_id]


def payload(row):
    return json.loads(gzip.decompress(bytes(row["payload_gz"])))


def test_finish_writes_one_snapshot_with_the_book(client, bundle):
    from app import store

    a = new_app(client)
    full_sync(client, a, bundle)
    rows = outbox(a["id"])
    snaps = [r for r in rows if r["kind"] == "snapshot"]
    assert len(snaps) == 1 and snaps[0]["batch_seq"] == 1
    p = payload(snaps[0])
    with store.db() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM book_vouchers WHERE application_id = ?", (a["id"],)).fetchone()["n"]
        book = dict(conn.execute("SELECT * FROM books WHERE application_id = ?", (a["id"],)).fetchone())
    assert len(p["vouchers"]) == n > 0
    assert p["book"]["state"] == book["state"] and p["sync_session"]["status"] == "done"
    assert set(p["application"]) == set(__import__("app.n8n", fromlist=["x"]).APP_COLUMNS)
    assert "contact_email" not in p["application"]
    # The first report then marks the company ready, which the bot needs to know.
    assert [r["kind"] for r in rows] == ["snapshot", "status"]
    assert payload(rows[1])["application"]["status"] == "ready"


def test_failed_finish_leaves_no_outbox_row(client, bundle, monkeypatch):
    from app import books, n8n, store

    a = new_app(client)
    real = n8n.queue_snapshot

    def boom(conn, app_id):
        real(conn, app_id)
        raise RuntimeError("disk full")  # anything failing inside the book's transaction

    monkeypatch.setattr(books.n8n, "queue_snapshot", boom)
    full_sync(client, a, bundle)
    assert outbox(a["id"]) == []
    assert books.get_book(a["id"]) is None  # the book rolled back with it
    assert store.get_application(a["id"])["status"] == "failed"


def test_signature_and_headers_verify(client, bundle, monkeypatch):
    from app import n8n

    a = new_app(client)
    full_sync(client, a, bundle)
    wire = Wire(monkeypatch)
    assert n8n.deliver_pending()["sent"] == 2
    for call in wire.calls:
        h = call["headers"]
        expected = "sha256=" + hmac.new(SECRET.encode(), h["X-Timestamp"].encode() + b"." + call["body"],
                                        hashlib.sha256).hexdigest()
        assert hmac.compare_digest(h["X-Signature"], expected)
        part = call["part"]
        assert h["X-Idempotency-Key"] == f"{a['id']}:{part['batch_seq']}:{part['part']}"
        assert h["Content-Type"] == "application/gzip"
    assert [c["part"]["kind"] for c in wire.calls] == ["snapshot", "status"]
    assert all(r["sent_at"] for r in outbox(a["id"]))
    # A tampered body no longer matches.
    c = wire.calls[0]
    bad = hmac.new(SECRET.encode(), c["headers"]["X-Timestamp"].encode() + b"." + c["body"] + b"x", hashlib.sha256)
    assert "sha256=" + bad.hexdigest() != c["headers"]["X-Signature"]


def test_retries_back_off_then_give_up_and_alert(client, bundle, monkeypatch, caplog):
    from app import config, mailer, n8n, store

    a = new_app(client)
    full_sync(client, a, bundle)
    wire = Wire(monkeypatch, answer=lambda part: 503)
    t0 = store.now()
    clock = {"t": t0}
    monkeypatch.setattr(store, "now", lambda: clock["t"])

    assert n8n.deliver_pending()["failed"] == 1
    row = outbox(a["id"])[0]
    assert row["attempts"] == 1 and row["last_error"] == "HTTP 503" and not row["sent_at"]
    assert row["next_attempt_at"] == (t0 + timedelta(seconds=30)).isoformat()
    assert len(wire.calls) == 1  # the status row behind it waits: order per company

    n8n.deliver_pending()  # not due yet
    assert len(wire.calls) == 1
    clock["t"] = t0 + timedelta(seconds=31)
    n8n.deliver_pending()
    row = outbox(a["id"])[0]
    assert row["attempts"] == 2 and row["next_attempt_at"] == (clock["t"] + timedelta(seconds=60)).isoformat()

    sent = []
    monkeypatch.setattr(config, "ALERT_EMAIL", "ops@example.com")
    monkeypatch.setattr(config, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(mailer, "send", lambda to, subject, body: sent.append((to, subject, body)))
    clock["t"] = t0 + timedelta(hours=24, seconds=1)
    with caplog.at_level(logging.ERROR, logger="tally_connector"):
        assert n8n.deliver_pending()["dead"] == 1
    assert outbox(a["id"])[0]["dead_at"]
    assert sent and sent[0][0] == "ops@example.com" and "gave up" in sent[0][2]
    assert any("gave up" in r.getMessage() for r in caplog.records)

    # Later rows are no longer held back once n8n answers.
    wire.answer = lambda part: 200
    clock["t"] += timedelta(seconds=1)
    n8n.deliver_pending()
    assert outbox(a["id"])[1]["sent_at"]


def test_order_per_company_and_other_companies_unaffected(client, bundle, monkeypatch):
    from app import n8n

    a = new_app(client, "A Traders")
    b = new_app(client, "B Traders")
    full_sync(client, a, bundle)
    full_sync(client, b, bundle)
    wire = Wire(monkeypatch, answer=lambda part: 0 if part["application_id"] == a["id"] else 200)
    n8n.deliver_pending()
    sent = [(c["part"]["application_id"], c["part"]["batch_seq"]) for c in wire.calls]
    assert sent == [(a["id"], 1), (b["id"], 1), (b["id"], 2)] or sent == [(b["id"], 1), (b["id"], 2), (a["id"], 1)]
    assert not any(r["sent_at"] for r in outbox(a["id"]))
    assert all(r["sent_at"] for r in outbox(b["id"]))


def test_out_of_order_answer_queues_a_snapshot(client, bundle, monkeypatch):
    from app import n8n

    a = new_app(client)
    full_sync(client, a, bundle)
    seen = []

    def answer(part):
        seen.append((part["kind"], part["batch_seq"]))
        return 409 if part["kind"] == "status" else 200

    Wire(monkeypatch, answer=answer)
    stats = n8n.deliver_pending()
    assert stats["resynced"] == 1
    assert seen == [("snapshot", 1), ("status", 2), ("snapshot", 3)]
    assert all(r["sent_at"] for r in outbox(a["id"]))


def test_heartbeats_send_tally_state_changes_only(client, bundle, monkeypatch):
    from app import n8n, store

    a = new_app(client)
    bearer, _ = full_sync(client, a, bundle)
    Wire(monkeypatch)
    n8n.deliver_pending()

    def beat(**body):
        assert client.post("/api/connector/monitor/status", headers=bearer, json=body).status_code == 200
        return [r for r in outbox(a["id"]) if not r["sent_at"]]

    assert beat(tally="ok") == []  # nothing changed, and n8n's copy is fresh
    down = beat(tally="down", error="Tally not running")
    assert [r["kind"] for r in down] == ["status"]
    assert payload(down[0])["application"]["tally_error"] == "Tally not running"
    n8n.deliver_pending()
    assert beat(tally="down", error="Tally not running") == []
    assert [payload(r)["application"]["tally_down_since"] for r in beat(tally="ok")] == [None]
    n8n.deliver_pending()
    # n8n's tally_ok_at is refreshed hourly even while nothing else changes.
    later = store.now() + timedelta(hours=1, minutes=1)
    monkeypatch.setattr(store, "now", lambda: later)
    assert [r["kind"] for r in beat(tally="ok")] == ["status"]


def test_purge_on_deletion_request_retention_and_delete(client, bundle, monkeypatch):
    from app import main, n8n, store

    a = new_app(client, "Deleted Co")
    full_sync(client, a, bundle)
    Wire(monkeypatch)
    n8n.deliver_pending()

    store.request_deletion(a["id"], "closing the account")
    last = outbox(a["id"])[-1]
    assert last["kind"] == "purge" and payload(last)["reason"] == "deletion requested"

    b = new_app(client, "Retired Co")
    full_sync(client, b, bundle)
    store.stop_monitoring(b["id"], "client")
    with store.db() as conn:
        conn.execute("UPDATE applications SET last_report_at = '2020-01-01T00:00:00+00:00' WHERE id = ?", (b["id"],))
    assert main.housekeeping() >= 1
    assert payload(outbox(b["id"])[-1])["reason"] == "retention purge"
    # The earlier unsent snapshot is superseded: no financial data waits behind a purge.
    assert all(r["payload_gz"] in (b"", None) or r["kind"] == "purge" for r in outbox(b["id"]) if r["batch_seq"] < 3)

    r = client.post(f"/admin/msmes/{a['id']}/delete", auth=("platform", "platform"),
                    data={"confirm": "Deleted Co"}, headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert store.get_application(a["id"]) is None
    rows = outbox(a["id"])  # outlives the application
    assert [payload(r)["reason"] for r in rows if r["kind"] == "purge"] == ["deletion requested", "deleted"]
    wire = Wire(monkeypatch)
    n8n.deliver_pending()
    assert {c["part"]["kind"] for c in wire.calls} == {"purge"}


def test_nothing_queued_when_off_or_not_flagged(client, bundle, monkeypatch):
    from app import config, n8n, store

    a = new_app(client, "Not Flagged", forward=False)
    bearer, _ = full_sync(client, a, bundle)
    client.post("/api/connector/monitor/status", headers=bearer, json={"tally": "down", "error": "x"})
    assert outbox() == []

    monkeypatch.setattr(config, "N8N_WEBHOOK_URL", "")
    b = new_app(client, "Flagged, bridge off")
    full_sync(client, b, bundle)
    store.request_deletion(b["id"], "")
    assert outbox() == []
    assert n8n.deliver_pending() == {"sent": 0, "failed": 0, "dead": 0, "resynced": 0}


def test_forward_off_purges_and_on_sends_a_snapshot(client, bundle, monkeypatch):
    from app import n8n

    a = new_app(client, forward=False)
    full_sync(client, a, bundle)
    assert outbox() == []
    assert n8n.set_forward(a["id"], False) is None  # never sent: nothing to purge
    assert n8n.set_forward(a["id"], True) == 1
    assert n8n.set_forward(a["id"], False) == 2
    assert [r["kind"] for r in outbox(a["id"])] == ["snapshot", "purge"]
    assert outbox(a["id"])[0]["payload_gz"] == b""  # superseded by the purge


def test_large_company_is_chunked(client, model, monkeypatch):
    from app import n8n

    big = bundle_from_model(model, months=30)
    assert len(big["vouchers"]) > 8000
    a = new_app(client, "Big Co")
    full_sync(client, a, big)
    snap = [r for r in outbox(a["id"]) if r["kind"] == "snapshot"][0]

    # At the real 5 MB limit every gzipped part is far below it.
    for part in n8n.parts(payload(snap)):
        assert len(gzip.compress(json.dumps(part).encode())) <= n8n.PART_BYTES

    monkeypatch.setattr(n8n, "PART_BYTES", 400_000)
    wire = Wire(monkeypatch)
    n8n.deliver_pending()
    snap_parts = [c for c in wire.calls if c["part"]["kind"] == "snapshot"]
    of = snap_parts[0]["part"]["of"]
    assert of > 3 and [c["part"]["part"] for c in snap_parts] == list(range(1, of + 1))
    assert all(len(c["body"]) <= 400_000 and len(json.dumps(c["part"])) <= 400_000 for c in snap_parts)
    assert "book" in snap_parts[0]["part"] and all("book" not in c["part"] for c in snap_parts[1:])
    guids = [v["guid"] for c in snap_parts for v in c["part"]["vouchers"]]
    assert len(guids) == len(set(guids)) == snap_parts[0]["part"]["voucher_count"]
    stored = {v["guid"] for v in payload(snap)["vouchers"]}
    assert set(guids) == stored
    assert len({c["headers"]["X-Idempotency-Key"] for c in snap_parts}) == of
    # Deterministic: a retry sends byte-identical parts.
    again = [gzip.compress(json.dumps(p).encode(), mtime=0) for p in n8n.parts(payload(snap))]
    assert again == [c["body"] for c in snap_parts]
