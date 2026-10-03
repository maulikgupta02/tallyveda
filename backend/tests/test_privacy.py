"""Consent OTP, privacy pages, MSME privacy controls and retention."""

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


def gz(obj):
    return gzip.compress(json.dumps(obj).encode())


def test_consent_needs_the_emailed_code(client, monkeypatch):
    from app import config, mailer, store

    monkeypatch.setattr(config, "REQUIRE_OTP", True)
    mailer.outbox.clear()
    assert client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "No email"}).status_code == 400
    a = client.post("/api/bank/applications", auth=AUTH,
                    json={"applicant_name": "Kamla", "contact_email": "Owner@Kamla.in"}).json()
    code = {"X-Link-Code": a["link_code"]}
    info = client.post("/api/connector/verify", json={"code": a["link_code"]}).json()
    assert info["otp_required"] and info["otp_sent_to"] == "ow***@kamla.in"

    b = bundle_from_model(mock_tally.Company(), date(2026, 4, 30), months=6)
    start = {"company": b["company"], "period": b["period"]}
    assert client.post("/api/connector/sync/start", headers=code, json=start).status_code == 403

    assert client.post("/api/connector/otp/send", headers=code).json()["sent_to"] == "ow***@kamla.in"
    assert client.post("/api/connector/otp/send", headers=code).status_code == 429  # resend too soon
    msg = mailer.outbox[-1]
    assert msg["To"] == "owner@kamla.in"
    otp = msg["Subject"].rsplit(" ", 1)[1]
    wrong = "000000" if otp != "000000" else "111111"
    assert client.post("/api/connector/otp/verify", headers=code, json={"otp": wrong}).status_code == 400
    assert client.post("/api/connector/otp/verify", headers=code, json={"otp": otp}).json()["verified"]

    plan = client.post("/api/connector/sync/start", headers=code,
                       json={**start, "consent": {"accepted_by": "R Agarwal"}}).json()
    assert plan["mode"] == "full"
    from app import books
    assert books.get_session(plan["sync_id"])["staged"]["consent"]["verified_email"] == "owner@kamla.in"
    app_row = store.get_application(a["id"])
    assert app_row["otp_verified_email"] == "owner@kamla.in"


def test_otp_locks_after_too_many_wrong_codes(client, monkeypatch):
    from app import config, mailer

    monkeypatch.setattr(config, "REQUIRE_OTP", True)
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "K", "contact_email": "k@k.in"}).json()
    code = {"X-Link-Code": a["link_code"]}
    client.post("/api/connector/otp/send", headers=code)
    otp = mailer.outbox[-1]["Subject"].rsplit(" ", 1)[1]
    wrong = "000000" if otp != "000000" else "111111"
    for _ in range(5):
        client.post("/api/connector/otp/verify", headers=code, json={"otp": wrong})
    r = client.post("/api/connector/otp/verify", headers=code, json={"otp": otp})
    assert r.status_code == 400 and "Too many" in r.json()["detail"]


def test_privacy_and_terms_pages(client):
    for path, text in (("/privacy", "Grievance officer"), ("/terms", "Terms of Use")):
        r = client.get(path)
        assert r.status_code == 200 and text.lower() in r.text.lower()
        assert "noindex" not in r.headers.get("x-robots-tag", "")
    assert "/privacy" in client.get("/sitemap.xml").text


def test_msme_can_withdraw_and_request_deletion(client):
    from app import store

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "M", "monitoring": True}).json()
    b = bundle_from_model(mock_tally.Company(), date(2026, 4, 30), months=6)
    b["consent"] = {"accepted_by": "M", "monitoring_opt_in": True}
    client.post("/api/connector/upload", content=gz(b), headers={"X-Link-Code": a["link_code"]})
    page = client.post(f"/bank/applications/{a['id']}/msme-login", auth=AUTH).text
    import re
    username = re.search(r'Username</div><div class="v">([^<]+)', page).group(1)
    password = re.search(r'Password</div><div class="v">([^<]+)', page).group(1)
    msme = (username, password)
    assert "Your data and privacy" in client.get("/msme", auth=msme).text
    assert client.post("/msme/monitoring/stop", auth=msme, follow_redirects=False).status_code == 303
    assert store.get_application(a["id"])["monitoring_status"] == "stopped_by_client"
    client.post("/msme/deletion", auth=msme, data={"reason": "Loan closed"}, follow_redirects=False)
    assert store.get_application(a["id"])["deletion_requested_at"]
    assert "Loan closed" in client.get("/admin", auth=("platform", "platform")).text


def test_retention_purges_raw_books_after_sharing_ends(client, monkeypatch):
    from app import config, store
    from app.main import housekeeping

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Old"}).json()
    b = bundle_from_model(mock_tally.Company(), date(2026, 4, 30), months=6)
    client.post("/api/connector/upload", content=gz(b), headers={"X-Link-Code": a["link_code"]})
    assert store.raw_report_ids(a["id"])
    assert housekeeping() == 0  # recent: kept
    old = (store.now() - timedelta(days=config.RETENTION_DAYS + 1)).isoformat()
    with store.db() as conn:
        conn.execute("UPDATE applications SET last_report_at = ? WHERE id = ?", (old, a["id"]))
    assert housekeeping() == 1
    assert not store.raw_report_ids(a["id"])
    r = store.list_reports(a["id"])[0]
    assert client.get(f"/bank/reports/{r['id']}", auth=AUTH).status_code == 200  # figures and page still there
