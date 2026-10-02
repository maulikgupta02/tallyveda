"""Monthly monitoring: opt-in, due logic, refresh, alerts and stopping."""

import gzip
import json
import os
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


def test_monthly_monitoring_flow(client, model):
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
    assert "Indicator trend" in page and "customers who have stopped paying" in page
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
    app = {"monitoring_status": "active", "force_refresh": 0, "last_report_at": utc(2026, 8, 20).isoformat()}
    assert not store.monitoring_due(app, utc(2026, 9, 10))  # only 21 days since the last report
    assert store.monitoring_due(app, utc(2026, 9, 15))      # 26 days, and past the 5th
    early = app | {"last_report_at": utc(2026, 8, 1).isoformat()}
    assert not store.monitoring_due(early, utc(2026, 9, 3))  # 33 days, but before the 5th
    assert store.monitoring_due(early | {"force_refresh": 1}, utc(2026, 8, 2))
    assert not store.monitoring_due(app | {"monitoring_status": "stopped_by_bank"}, utc(2026, 12, 1))


def test_login_required_outside_dev():
    backend = Path(__file__).resolve().parent.parent
    env = {k: v for k, v in os.environ.items() if not k.startswith("TC_")}
    run = lambda extra: subprocess.run([sys.executable, "-c", "import app.config"], cwd=backend,
                                       env=env | extra, capture_output=True, text=True)
    assert "TC_ADMIN_PASSWORD" in run({}).stderr
    assert run({"TC_ADMIN_USER": "bank", "TC_ADMIN_PASSWORD": "s3cret"}).returncode == 0
    assert run({"TC_DEV": "1"}).returncode == 0
