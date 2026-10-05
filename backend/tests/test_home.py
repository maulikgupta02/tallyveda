"""Public marketing home page: SEO surface, progressive-enhancement content,
and the lead form (`/api/leads`)."""

from __future__ import annotations

import json
import re

import pytest
from fastapi.testclient import TestClient

from app import marketing


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config, main as main_module

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    main_module._lead_attempts.clear()
    main_module._failed_attempts.clear()
    return TestClient(main_module.app)


def _jsonld(html: str) -> dict:
    m = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)
    assert m, "no JSON-LD block found"
    return json.loads(m.group(1))


def test_home_page_200_with_h1(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<h1>" in r.text
    assert "Credit decisions from live Tally books" in r.text


def test_json_ld_parses_and_faq_matches(client):
    data = _jsonld(client.get("/").text)
    types = {node["@type"] if isinstance(node["@type"], str) else tuple(node["@type"]) for node in data["@graph"]}
    assert "Organization" in types
    assert "WebSite" in types
    faq_node = next(n for n in data["@graph"] if n["@type"] == "FAQPage")
    got = [(q["name"], q["acceptedAnswer"]["text"]) for q in faq_node["mainEntity"]]
    assert got == marketing.FAQ


def test_robots_txt(client):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Disallow: /bank" in r.text
    assert "Disallow: /msme" in r.text
    assert "Disallow: /admin" in r.text
    assert "Disallow: /api" in r.text
    assert "Sitemap:" in r.text


def test_sitemap_xml(client):
    r = client.get("/sitemap.xml")
    assert r.status_code == 200
    assert "<loc>" in r.text
    assert "<lastmod>" in r.text


def test_noindex_header_on_private_routes(client):
    assert client.get("/bank").headers["x-robots-tag"] == "noindex, nofollow"
    assert client.get("/msme").headers["x-robots-tag"] == "noindex, nofollow"
    assert client.get("/api/bank/applications/does-not-exist").headers["x-robots-tag"] == "noindex, nofollow"


def test_home_page_has_no_noindex_header(client):
    assert "x-robots-tag" not in client.get("/").headers


def test_lead_post_json_stores_row(client):
    r = client.post("/api/leads", json={"name": "Asha", "org": "Acme Bank", "email": "asha@acme.test"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    from app import store
    assert "asha@acme.test" in {lead["email"] for lead in store.list_leads()}


def test_lead_post_form_redirects(client):
    r = client.post("/api/leads", data={"name": "Form User", "org": "Org", "email": "f@example.com"},
                     follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/?sent=1#demo"


def test_honeypot_rejected(client):
    r = client.post("/api/leads", json={"name": "Bot", "org": "X", "email": "bot@x.test", "website": "http://spam"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 400
    from app import store
    assert "bot@x.test" not in {lead["email"] for lead in store.list_leads()}


def test_invalid_email_rejected(client):
    r = client.post("/api/leads", json={"name": "Asha", "org": "Acme", "email": "not-an-email"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 400
    from app import store
    assert "not-an-email" not in {lead["email"] for lead in store.list_leads()}


def test_lead_rate_limit(client):
    for i in range(main_module_max_leads()):
        r = client.post("/api/leads", json={"name": f"T{i}", "org": "O", "email": f"t{i}@example.com"},
                         headers={"Accept": "application/json"})
        assert r.status_code == 200
    r = client.post("/api/leads", json={"name": "Over", "org": "O", "email": "over@example.com"},
                     headers={"Accept": "application/json"})
    assert r.status_code == 429


def main_module_max_leads() -> int:
    from app.main import MAX_LEADS_PER_HOUR
    return MAX_LEADS_PER_HOUR


def test_connector_update_endpoints_match_the_published_exe(client):
    import hashlib

    latest = client.get("/api/connector/latest").json()
    exe = client.get(latest["url"])
    assert exe.status_code == 200 and len(exe.content) == latest["size"]
    assert hashlib.sha256(exe.content).hexdigest() == latest["sha256"]
