"""Daily monitoring: opt-in, due logic, refresh, alerts and stopping."""

import gzip
import json
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

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


def gz(bundle: dict) -> bytes:
    return gzip.compress(json.dumps(bundle).encode())


def test_daily_monitoring_flow(client, model):
    a = client.post("/api/bank/applications", auth=AUTH,
                    json={"applicant_name": "Shree Ganesh", "monitoring": True}).json()
    assert client.post("/api/connector/verify", json={"code": a["link_code"]}).json()["monitoring_offered"]

    # First upload in April 2026, before the largest customer stopped paying.
    first = bundle_from_model(model, date(2026, 4, 30))
    first["consent"] = {"accepted_by": "R Agarwal", "monitoring_opt_in": True}
    r = client.post("/api/connector/upload", content=gz(first), headers={"X-Link-Code": a["link_code"]})
    token = r.json()["monitor_token"]
    bearer = {"Authorization": f"Bearer {token}"}

    status = client.post("/api/connector/monitor/status", headers=bearer).json()
    assert status["active"] and not status["due"]  # just received a report

    client.post(f"/bank/applications/{a['id']}/monitoring/refresh", auth=AUTH)
    assert client.post("/api/connector/monitor/status", headers=bearer).json()["due"]

    second = bundle_from_model(model, date(2026, 9, 27))
    assert client.post("/api/connector/monitor/upload", content=gz(second), headers=bearer).status_code == 200

    reports = client.get(f"/api/bank/applications/{a['id']}/reports", auth=AUTH).json()
    assert [r["status"] for r in reports] == ["ready", "ready"]
    titles = {x["title"] for x in reports[0]["alerts"]}
    assert "New red flag: Stuck receivables: customers who have stopped paying" in titles
    assert any(t.startswith("Receivables older than 90 days") for t in titles)
    assert reports[1]["alerts"] == []  # first report has nothing to compare with

    # Refresh flag cleared by the new report.
    assert not client.post("/api/connector/monitor/status", headers=bearer).json()["due"]
    page = client.get(f"/bank/applications/{a['id']}", auth=AUTH).text
    assert "Since yesterday" in page and "customers who have stopped paying" in page
    html = client.get(f"/bank/reports/{reports[0]['id']}", auth=AUTH).text
    assert "Changes since previous report" in html
    assert client.get(f"/bank/reports/{reports[0]['id']}.json", auth=AUTH).json()["alerts"]

    # Client withdraws consent: token stops working for uploads.
    client.post("/api/connector/monitor/stop", headers=bearer)
    assert not client.post("/api/connector/monitor/status", headers=bearer).json()["active"]
    assert client.post("/api/connector/monitor/upload", content=gz(second), headers=bearer).status_code == 403
    assert client.post("/api/connector/monitor/status", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_no_token_without_opt_in(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "X", "monitoring": True}).json()
    r = client.post("/api/connector/upload", content=gz(bundle_from_model(model)), headers={"X-Link-Code": a["link_code"]})
    assert "monitor_token" not in r.json()


def test_due_schedule():
    from app import store

    utc = lambda *a: datetime(*a, tzinfo=timezone.utc)
    app = {"monitoring_status": "active", "force_refresh": 0, "last_report_at": utc(2026, 9, 10, 8, 0).isoformat()}
    assert not store.monitoring_due(app, utc(2026, 9, 10, 20, 0))  # under a day since the last report
    assert store.monitoring_due(app, utc(2026, 9, 11, 9, 0))       # a day has passed
    assert store.monitoring_due({**app, "last_report_at": None}, utc(2026, 9, 10, 8, 1))  # never reported yet
    assert store.monitoring_due(app | {"force_refresh": 1}, utc(2026, 9, 10, 8, 1))
    assert not store.monitoring_due(app | {"monitoring_status": "stopped_by_bank"}, utc(2026, 12, 1))


def test_msme_login_is_tenant_isolated(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Isolated Co"}).json()
    page = client.post(f"/bank/applications/{a['id']}/msme-login", auth=AUTH).text
    username = re.search(r"Username</div><div class=\"v\">([^<]+)", page).group(1)
    password = re.search(r"Password</div><div class=\"v\">([^<]+)", page).group(1)
    MSME = (username, password)

    r = client.get("/msme", auth=MSME)
    assert r.status_code == 200 and "Isolated Co" in r.text

    # The MSME login never sees the bank dashboard or another applicant's data.
    other = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Other Co"}).json()
    assert client.get("/bank", auth=MSME).status_code == 401
    assert client.get(f"/bank/applications/{other['id']}", auth=MSME).status_code == 401
    assert client.get(f"/bank/applications/{a['id']}", auth=MSME).status_code == 401

    # Resetting rotates the password; the old one stops working.
    page2 = client.post(f"/bank/applications/{a['id']}/msme-login", auth=AUTH).text
    password2 = re.search(r"Password</div><div class=\"v\">([^<]+)", page2).group(1)
    assert password2 != password
    assert client.get("/msme", auth=MSME).status_code == 401
    assert client.get("/msme", auth=(username, password2)).status_code == 200


COMPANY_TABS = ["overview", "sales", "profit", "recv", "pay", "wc", "debt", "bank", "tax", "data"]
MSME_TABS = ["home", "sales", "cust", "money", "dues"]


def test_portfolio_page_renders(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Portfolio Co"}).json()
    client.post("/api/connector/upload", content=gz(bundle_from_model(model)), headers={"X-Link-Code": a["link_code"]})
    page = client.get("/bank", auth=AUTH)
    assert page.status_code == 200 and "Portfolio Co" in page.text
    assert client.get("/bank?filter=attention", auth=AUTH).status_code == 200
    assert client.get("/bank?filter=overdue", auth=AUTH).status_code == 200


def test_company_page_tabs_render_and_are_tenant_isolated(client, model):
    from app import store

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Tabbed Co"}).json()
    client.post("/api/connector/upload", content=gz(bundle_from_model(model)), headers={"X-Link-Code": a["link_code"]})
    for tab in COMPANY_TABS:
        r = client.get(f"/bank/applications/{a['id']}?tab={tab}", auth=AUTH)
        assert r.status_code == 200, tab

    other_tenant = store.create_tenant("Another Bank")
    store.create_user(other_tenant["id"], "tabs_admin", "tabs_pw", "bank")
    assert client.get(f"/bank/applications/{a['id']}", auth=("tabs_admin", "tabs_pw")).status_code == 404


def test_msme_page_has_no_bank_language(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Plain Language Co"}).json()
    client.post("/api/connector/upload", content=gz(bundle_from_model(model)), headers={"X-Link-Code": a["link_code"]})
    page = client.post(f"/bank/applications/{a['id']}/msme-login", auth=AUTH).text
    username = re.search(r"Username</div><div class=\"v\">([^<]+)", page).group(1)
    password = re.search(r"Password</div><div class=\"v\">([^<]+)", page).group(1)
    MSME = (username, password)

    for tab in MSME_TABS:
        r = client.get(f"/msme?tab={tab}", auth=MSME)
        assert r.status_code == 200
        lowered = r.text.lower()
        assert "amber" not in lowered and "red flag" not in lowered and "dscr" not in lowered


def test_msme_report_no_longer_exposes_bank_report(client, model):
    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Redirect Co"}).json()
    client.post("/api/connector/upload", content=gz(bundle_from_model(model)), headers={"X-Link-Code": a["link_code"]})
    page = client.post(f"/bank/applications/{a['id']}/msme-login", auth=AUTH).text
    username = re.search(r"Username</div><div class=\"v\">([^<]+)", page).group(1)
    password = re.search(r"Password</div><div class=\"v\">([^<]+)", page).group(1)
    MSME = (username, password)

    r = client.get("/msme/report", auth=MSME, follow_redirects=False)
    assert r.status_code in (302, 303, 307, 308)
    assert r.headers["location"] == "/msme"
    assert client.get("/msme/report", auth=None).status_code == 401

    body = client.get("/msme/report.json", auth=MSME).json()
    assert body.get("groups") and [g["key"] for g in body["groups"]] == MSME_TABS
    assert "indicators" not in body and "flags" not in body


def test_bank_tenants_are_isolated(client, model):
    from app import store

    other_tenant = store.create_tenant("Other Bank")
    store.create_user(other_tenant["id"], "other_admin", "other_pw", "bank")
    OTHER_AUTH = ("other_admin", "other_pw")

    a = client.post("/api/bank/applications", auth=AUTH, json={"applicant_name": "Tenant A Co"}).json()
    assert client.get(f"/bank/applications/{a['id']}", auth=OTHER_AUTH).status_code == 404
    assert client.get(f"/api/bank/applications/{a['id']}", auth=OTHER_AUTH).status_code == 404
    dash = client.get("/bank", auth=OTHER_AUTH).text
    assert "Tenant A Co" not in dash


def test_login_required_outside_dev():
    backend = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("TC_")}
    run = lambda extra: subprocess.run([sys.executable, "-c", "import app.config"], cwd=backend,
                                       env=env | extra, capture_output=True, text=True)
    assert "TC_ADMIN_PASSWORD" in run({}).stderr
    assert run({"TC_ADMIN_USER": "bank", "TC_ADMIN_PASSWORD": "s3cret"}).returncode == 0
    assert run({"TC_DEV": "1"}).returncode == 0
