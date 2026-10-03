"""/admin platform admin panel: roles/auth, the direct tenant, bank suspension,
MSME management, user management, the audit log and the CSRF guard."""

import re

import pytest
from fastapi.testclient import TestClient

PLATFORM = ("platform", "platform")
BANK = ("admin", "admin")  # the TC_DEV seed bank user (tenant seeded lazily by bank_user)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    from app.main import app

    return TestClient(app)


def _password_from(page_text: str) -> tuple[str, str]:
    username = re.search(r"Username</div><div class=\"v\">([^<]+)", page_text).group(1)
    password = re.search(r"Password</div><div class=\"v\">([^<]+)", page_text).group(1)
    return username, password


def test_platform_login_required_and_dev_default_works(client):
    assert client.get("/admin", auth=("nope", "nope")).status_code == 401
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=PLATFORM).status_code == 200


def test_bank_user_cannot_reach_admin_and_vice_versa(client):
    assert client.get("/admin", auth=BANK).status_code == 401
    assert client.get("/bank", auth=PLATFORM).status_code == 401


def test_direct_tenant_is_seeded_and_platform_admin_is_not_a_bank_user(client):
    from app import store

    client.get("/admin", auth=PLATFORM)  # triggers _ensure_seed_platform_admin
    direct = store.get_tenant(store.direct_tenant_id())
    assert direct and direct["kind"] == "direct" and direct["name"] == "TallyVeda"
    bank_users = store.list_users(tenant_id=store.direct_tenant_id(), role="bank")
    assert bank_users == []  # the platform admin never appears as a bank user


def test_create_bank_suspend_blocks_login_reactivate_restores(client):
    r = client.post("/admin/banks", data={"name": "Second Bank"}, auth=PLATFORM)
    bank_id = str(r.url).rstrip("/").split("/")[-1]
    assert client.get(f"/admin/banks/{bank_id}", auth=PLATFORM).status_code == 200

    r = client.post(f"/admin/banks/{bank_id}/users", data={"username": "ops@secondbank"}, auth=PLATFORM)
    username, password = _password_from(r.text)
    assert client.get("/bank", auth=(username, password)).status_code == 200

    client.post(f"/admin/banks/{bank_id}/suspend", auth=PLATFORM)
    assert client.get("/bank", auth=(username, password)).status_code == 401

    client.post(f"/admin/banks/{bank_id}/reactivate", auth=PLATFORM)
    assert client.get("/bank", auth=(username, password)).status_code == 200


def test_bank_rename(client):
    r = client.post("/admin/banks", data={"name": "Old Name"}, auth=PLATFORM)
    bank_id = str(r.url).rstrip("/").split("/")[-1]
    client.post(f"/admin/banks/{bank_id}/rename", data={"name": "New Name"}, auth=PLATFORM)
    page = client.get(f"/admin/banks/{bank_id}", auth=PLATFORM).text
    assert "New Name" in page


def test_create_direct_and_bank_msme_and_tenant_isolation(client):
    bank_resp = client.post("/admin/banks", data={"name": "Isolation Bank"}, auth=PLATFORM)
    bank_id = str(bank_resp.url).rstrip("/").split("/")[-1]
    bank_user_resp = client.post(f"/admin/banks/{bank_id}/users", data={"username": "iso_user"}, auth=PLATFORM)
    bank_username, bank_password = _password_from(bank_user_resp.text)

    direct_resp = client.post(
        "/admin/msmes", data={"applicant_name": "Direct MSME", "reference": "", "months": 24, "bank_id": ""},
        auth=PLATFORM,
    )
    direct_id = str(direct_resp.url).rstrip("/").split("/")[-1]

    bank_msme_resp = client.post(
        "/admin/msmes",
        data={"applicant_name": "Bank MSME", "reference": "", "months": 24, "bank_id": bank_id},
        auth=PLATFORM,
    )
    bank_msme_id = str(bank_msme_resp.url).rstrip("/").split("/")[-1]

    # The platform admin sees both.
    msmes_page = client.get("/admin/msmes", auth=PLATFORM).text
    assert "Direct MSME" in msmes_page and "Bank MSME" in msmes_page

    # The bank sees its own MSME, but never the direct-tenant one.
    bank_dashboard = client.get("/bank", auth=(bank_username, bank_password)).text
    assert "Direct MSME" not in bank_dashboard
    assert client.get(f"/bank/applications/{direct_id}", auth=(bank_username, bank_password)).status_code == 404
    assert client.get(f"/bank/applications/{bank_msme_id}", auth=(bank_username, bank_password)).status_code == 200


def test_msme_filters_and_search(client):
    client.post("/admin/msmes", data={"applicant_name": "Findable Co", "reference": "REF-1", "months": 24, "bank_id": ""},
                auth=PLATFORM)
    client.post("/admin/msmes", data={"applicant_name": "Other Co", "reference": "", "months": 24, "bank_id": ""},
                auth=PLATFORM)
    page = client.get("/admin/msmes", params={"q": "Findable"}, auth=PLATFORM).text
    assert "Findable Co" in page and "Other Co" not in page
    page = client.get("/admin/msmes", params={"kind": "direct"}, auth=PLATFORM).text
    assert "Findable Co" in page


def test_msme_detail_login_and_delete_requires_typed_confirmation(client):
    r = client.post("/admin/msmes", data={"applicant_name": "Delete Me Co", "reference": "", "months": 24, "bank_id": ""},
                    auth=PLATFORM)
    app_id = str(r.url).rstrip("/").split("/")[-1]

    login_resp = client.post(f"/admin/msmes/{app_id}/msme-login", auth=PLATFORM)
    username, password = _password_from(login_resp.text)
    assert client.get("/msme", auth=(username, password)).status_code == 200

    assert client.post(f"/admin/msmes/{app_id}/delete", data={"confirm": "wrong name"}, auth=PLATFORM).status_code == 400
    assert client.get(f"/admin/msmes/{app_id}", auth=PLATFORM).status_code == 200  # still there

    r = client.post(f"/admin/msmes/{app_id}/delete", data={"confirm": "Delete Me Co"}, auth=PLATFORM)
    assert r.status_code == 200 and str(r.url).endswith("/admin/msmes")
    assert client.get(f"/admin/msmes/{app_id}", auth=PLATFORM).status_code == 404
    assert client.get("/msme", auth=(username, password)).status_code == 401  # its MSME login is gone too


def test_admin_view_dashboard_is_read_only(client):
    import gzip
    import json
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from test_analysis import bundle_from_model, mock_tally

    r = client.post("/admin/msmes", data={"applicant_name": "Viewable Co", "reference": "", "months": 24, "bank_id": ""},
                     auth=PLATFORM)
    app_id = str(r.url).rstrip("/").split("/")[-1]
    page = client.get(f"/admin/msmes/{app_id}", auth=PLATFORM).text
    code = re.search(r'class="code">([A-Z0-9-]+)<', page).group(1).replace("-", "")
    bundle = bundle_from_model(mock_tally.Company())
    client.post("/api/connector/upload", content=gzip.compress(json.dumps(bundle).encode()),
                headers={"X-Link-Code": code})

    view = client.get(f"/admin/msmes/{app_id}/view", auth=PLATFORM)
    assert view.status_code == 200 and "Viewable Co" in view.text
    assert "Manage (code" not in view.text  # read-only: no code/monitoring/MSME-login controls


def test_users_list_reset_disable_enable_delete(client):
    bank_resp = client.post("/admin/banks", data={"name": "Users Bank"}, auth=PLATFORM)
    bank_id = str(bank_resp.url).rstrip("/").split("/")[-1]
    user_resp = client.post(f"/admin/banks/{bank_id}/users", data={"username": "users_test"}, auth=PLATFORM)
    username, password = _password_from(user_resp.text)
    from app import store

    user_id = store.authenticate(username, password, "bank")["id"]

    assert client.get("/admin/users", auth=PLATFORM).status_code == 200
    reset = client.post(f"/admin/users/{user_id}/reset-password", auth=PLATFORM)
    _, new_password = _password_from(reset.text)
    assert new_password != password
    assert client.get("/bank", auth=(username, password)).status_code == 401
    assert client.get("/bank", auth=(username, new_password)).status_code == 200

    client.post(f"/admin/users/{user_id}/disable", auth=PLATFORM)
    assert client.get("/bank", auth=(username, new_password)).status_code == 401
    client.post(f"/admin/users/{user_id}/enable", auth=PLATFORM)
    assert client.get("/bank", auth=(username, new_password)).status_code == 200

    client.post(f"/admin/users/{user_id}/delete", auth=PLATFORM)
    assert client.get("/bank", auth=(username, new_password)).status_code == 401


def test_cannot_disable_or_delete_last_active_platform_admin(client):
    from app import store

    client.get("/admin", auth=PLATFORM)  # ensure seeded
    admin_id = store.authenticate("platform", "platform", "platform")["id"]
    assert client.post(f"/admin/users/{admin_id}/disable", auth=PLATFORM).status_code == 400
    assert client.post(f"/admin/users/{admin_id}/delete", auth=PLATFORM).status_code == 400

    # With a second active admin, disabling *some* platform admin becomes
    # possible — the guard is about the count, not any particular identity.
    # Disable the new one (not the shared seed admin other tests rely on) so
    # this test doesn't leave the seed login broken for the rest of the suite.
    second = store.create_user(store.direct_tenant_id(), "second_admin_for_guard_test", "pw", "platform")
    assert client.post(f"/admin/users/{second['id']}/disable", auth=PLATFORM, follow_redirects=False).status_code == 303
    assert client.get("/admin", auth=PLATFORM).status_code == 200  # the seed admin is unaffected


def test_audit_log_records_actions(client):
    client.post("/admin/banks", data={"name": "Audited Bank"}, auth=PLATFORM)
    page = client.get("/admin/audit", auth=PLATFORM).text
    assert "bank.create" in page and "Audited Bank" in page


def test_csrf_blocks_mismatched_origin_allows_matching_or_absent(client):
    r = client.post("/admin/banks", data={"name": "Blocked Bank"}, auth=PLATFORM,
                     headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "Blocked Bank" not in client.get("/admin/banks", auth=PLATFORM).text

    # `Origin: null` (sandboxed iframes) carries no host and must count as cross-site.
    r = client.post("/admin/banks", data={"name": "Null Origin Bank"}, auth=PLATFORM,
                     headers={"Origin": "null"})
    assert r.status_code == 403
    assert "Null Origin Bank" not in client.get("/admin/banks", auth=PLATFORM).text

    # TestClient's base_url is http://testserver, so a matching Origin is accepted.
    r = client.post("/admin/banks", data={"name": "Allowed Bank"}, auth=PLATFORM,
                     headers={"Origin": "http://testserver"})
    assert r.status_code in (200, 303)
    assert "Allowed Bank" in client.get("/admin/banks", auth=PLATFORM).text

    # No Origin/Referer at all (a non-browser client, like every other test here) is allowed.
    r = client.post("/admin/banks", data={"name": "No Header Bank"}, auth=PLATFORM)
    assert r.status_code in (200, 303)
    assert "No Header Bank" in client.get("/admin/banks", auth=PLATFORM).text


def test_csrf_also_guards_bank_form_posts(client):
    r = client.post("/bank/applications", auth=BANK, data={"applicant_name": "CSRF Co"},
                     headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    assert "CSRF Co" not in client.get("/bank", auth=BANK).text


def test_admin_leads_lists_home_page_requests(client):
    r = client.post("/api/leads", json={"name": "Lead Person", "org": "Lead Bank", "email": "lead@bank.test", "kind": "A bank or NBFC"})
    assert r.status_code == 200
    page = client.get("/admin/leads", auth=PLATFORM)
    assert page.status_code == 200 and "Lead Bank" in page.text and "lead@bank.test" in page.text
    assert client.get("/admin/leads", auth=("admin", "admin")).status_code in (401, 403)
