"""Anonymous connector telemetry (`/api/connector/telemetry`) and its admin page."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

PLATFORM = ("platform", "platform")
INSTALL = "0123456789abcdef0123456789abcdef"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config, main as main_module

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    main_module._telemetry_hits.clear()
    return TestClient(main_module.app)


def test_event_is_saved_without_auth_and_shown_to_platform_admin(client):
    r = client.post("/api/connector/telemetry",
                    json={"install_id": INSTALL, "version": "0.5.9", "event": "tally", "body": "port 9000 held by tally.exe"})
    assert r.status_code == 200
    page = client.get(f"/admin/telemetry?install={INSTALL}", auth=PLATFORM)
    assert page.status_code == 200
    assert "port 9000 held by tally.exe" in page.text
    assert client.get("/admin/telemetry").status_code == 401


@pytest.mark.parametrize("payload", [
    {"install_id": "not-hex", "event": "tally", "body": "x"},
    {"install_id": INSTALL, "event": "upload_books", "body": "x"},
])
def test_bad_events_are_rejected(client, payload):
    assert client.post("/api/connector/telemetry", json=payload).status_code == 400


def test_oversized_event_is_rejected(client):
    r = client.post("/api/connector/telemetry",
                    json={"install_id": INSTALL, "event": "probe", "body": "x" * 70_000})
    assert r.status_code == 413


def test_rate_limited_per_ip(client, monkeypatch):
    from app import main as main_module

    monkeypatch.setattr(main_module, "MAX_TELEMETRY_PER_HOUR", 2)
    ev = {"install_id": INSTALL, "event": "start", "body": "x"}
    assert client.post("/api/connector/telemetry", json=ev).status_code == 200
    assert client.post("/api/connector/telemetry", json=ev).status_code == 200
    assert client.post("/api/connector/telemetry", json=ev).status_code == 429
