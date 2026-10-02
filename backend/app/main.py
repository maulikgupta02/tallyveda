"""Bank-side API and dashboard.

Connector endpoints (called by TallyConnector.exe):
    POST /api/connector/verify           {"code"}              who is asking for the data
    POST /api/connector/upload           X-Link-Code + gzip JSON   first upload (one-time code)
    POST /api/connector/monitor/status   Bearer token           is a refresh due?
    POST /api/connector/monitor/upload   Bearer token + gzip JSON  daily refresh
    POST /api/connector/monitor/stop     Bearer token           client withdraws consent

Bank endpoints (HTTP Basic auth, per-tenant bank user — see store.py's `users` table):
    GET  /bank[?filter=attention|watch|alerts|overdue]   portfolio dashboard (this tenant only)
    GET  /bank/applications/{id}[?tab=<group key>]       company page: tabs, Manage panel
    GET  /bank/applications/{id}/report[.json]  latest report
    GET  /bank/reports/{report_id}[.json]       a specific report
    POST /bank/applications/{id}/msme-login     issue/reset this applicant's MSME login
    POST /api/bank/applications                 create request (JSON, for LOS integration)

MSME endpoints (HTTP Basic auth, a login tied to exactly one application):
    GET  /msme[?tab=home|sales|cust|money|dues] that company's own plain-language dashboard
    GET  /msme/report                           redirects to /msme (no bank report here)
    GET  /msme/report.json                      the MSME view-model, not the bank's report.json
"""

from __future__ import annotations

import gzip
import json
import logging
import time
import zlib
from collections import defaultdict, deque
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field

from . import config, store
from .analysis.alerts import compare, snapshot
from .report import dashboard as dashboard_views, dashboard_charts as dc
from .report import format as fmt
from .report.builder import build_report, render_html, report_json

log = logging.getLogger("tally_connector")
app = FastAPI(title="Tally Connector backend", docs_url="/api/docs")
security = HTTPBasic()
templates = Environment(
    loader=FileSystemLoader(Path(__file__).resolve().parent / "templates"),
    autoescape=select_autoescape(["html"]),
)
templates.filters["code"] = store.display_code
templates.filters["when"] = lambda s: s[:16].replace("T", " ") if s else ""
templates.filters["day"] = lambda s: s[:10] if s else ""
templates.filters["inr"] = fmt.inr
templates.filters["pct"] = fmt.pct
templates.filters["days"] = fmt.days
templates.filters["ratio"] = fmt.ratio
templates.globals.update(
    health_pill=dc.health_pill,
    spark=dc.sparkline,
    monthly_chart=dc.monthly_columns_responsive,
    stacked_bar=dc.stacked_bar,
    bullet_gauge=dc.bullet_gauge,
    hbars=dc.horizontal_bars,
    ageing_stack=dc.ageing_stack,
    emi_grid=dc.emi_grid,
    month_label=dc.month_label,
    STATUS_WORD=dc.STATUS_WORD,
    inr=fmt.inr,
)

MONITORING_LABELS = {
    "off": "Off",
    "active": "Daily",
    "stopped_by_bank": "Stopped by bank",
    "stopped_by_client": "Stopped by client",
}


def _ensure_seed_tenant() -> None:
    """On a brand-new database, turn TC_ADMIN_USER/TC_ADMIN_PASSWORD into the first
    real tenant + bank user, so a fresh deploy still gets a working login without a
    separate provisioning step. Once any tenant exists this is a no-op — onboarding
    a second bank is done via `python -m app.manage` (see docs/agents/architecture.md)."""
    if store.get_tenant(store.seed_tenant_id()) is not None:
        return
    tenant = store.create_tenant(config.BANK_NAME, tenant_id=store.seed_tenant_id())
    store.create_user(tenant["id"], config.ADMIN_USER, config.ADMIN_PASSWORD, "bank")


def bank_user(creds: HTTPBasicCredentials = Depends(security)) -> dict:
    _ensure_seed_tenant()
    user = store.authenticate(creds.username, creds.password, "bank")
    if not user:
        raise HTTPException(401, "Unauthorised", headers={"WWW-Authenticate": "Basic"})
    return user


def msme_user(creds: HTTPBasicCredentials = Depends(security)) -> dict:
    user = store.authenticate(creds.username, creds.password, "msme")
    if not user:
        raise HTTPException(401, "Unauthorised", headers={"WWW-Authenticate": "Basic"})
    return user


# ------------------------------------------------------------- connector API

_failed_attempts: dict[str, deque] = defaultdict(deque)
MAX_FAILED_PER_HOUR = 20


def _throttle(ip: str) -> deque:
    attempts = _failed_attempts[ip]
    while attempts and attempts[0] < time.time() - 3600:
        attempts.popleft()
    if len(attempts) >= MAX_FAILED_PER_HOUR:
        raise HTTPException(429, "Too many failed attempts. Try again in an hour.")
    return attempts


def _check_code(code: str, ip: str) -> dict:
    attempts = _throttle(ip)
    application = store.find_by_code(code or "")
    if not application:
        attempts.append(time.time())
        raise HTTPException(404, "This code is not valid or has expired. Ask your bank for a new code.")
    return application


def _check_token(request: Request) -> dict:
    attempts = _throttle(request.client.host)
    auth = request.headers.get("authorization", "")
    token = auth[7:] if auth.lower().startswith("bearer ") else ""
    application = store.find_by_monitor_token(token)
    if not application:
        attempts.append(time.time())
        raise HTTPException(401, "Monitoring token not recognised")
    return application


class VerifyIn(BaseModel):
    code: str


@app.post("/api/connector/verify")
def connector_verify(body: VerifyIn, request: Request):
    a = _check_code(body.code, request.client.host)
    return {
        "bank_name": config.BANK_NAME,
        "applicant_name": a["applicant_name"],
        "reference": a["reference"],
        "months": a["months"],
        "monitoring_offered": bool(a["monitoring_offered"]),
    }


async def _read_bundle(request: Request) -> tuple[bytes, dict]:
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            raise HTTPException(413, f"Upload larger than {config.MAX_UPLOAD_MB} MB")
    raw = bytes(raw)
    try:
        if raw[:2] == b"\x1f\x8b":
            bundle = _gunzip_json(raw)
        else:
            bundle = json.loads(raw)
            raw = gzip.compress(raw)
    except (OSError, zlib.error, ValueError) as e:
        raise HTTPException(400, f"Could not read upload: {e}")
    if not isinstance(bundle, dict) or "vouchers" not in bundle or "ledgers" not in bundle:
        raise HTTPException(400, "Upload is not a Tally connector bundle")
    return raw, bundle


def _gunzip_json(raw: bytes) -> dict:
    limit = config.MAX_UPLOAD_MB * 1024 * 1024 * 20  # guard against gzip bombs
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    out = bytearray()
    for i in range(0, len(raw), 1 << 20):
        out += d.decompress(raw[i : i + (1 << 20)], limit - len(out) + 1)
        if len(out) > limit:
            raise HTTPException(413, "Upload too large")
    out += d.flush()
    return json.loads(out)


def _store_upload(app_id: str, source: str, raw_gz: bytes, bundle: dict) -> str:
    company = (bundle.get("company") or {}).get("name", "")
    report_id = store.create_report(app_id, source, company)
    (store.report_dir(app_id, report_id) / "bundle.json.gz").write_bytes(raw_gz)
    return report_id


@app.post("/api/connector/upload")
async def connector_upload(request: Request, background: BackgroundTasks):
    a = _check_code(request.headers.get("x-link-code", ""), request.client.host)
    raw, bundle = await _read_bundle(request)
    report_id = _store_upload(a["id"], "initial", raw, bundle)
    consent = bundle.get("consent") or {}
    store.mark_uploaded(a["id"], (bundle.get("company") or {}).get("name", ""), consent.get("accepted_by", ""),
                        request.client.host)
    out = {"status": "received", "bank_name": config.BANK_NAME, "reference": a["reference"]}
    if a["monitoring_offered"] and consent.get("monitoring_opt_in"):
        out["monitor_token"] = store.start_monitoring(a["id"])
    background.add_task(process_report, report_id)
    return out


def _monitor_status(a: dict) -> dict:
    return {
        "active": a["monitoring_status"] == "active",
        "status": a["monitoring_status"],
        "due": store.monitoring_due(a),
        "bank_name": config.BANK_NAME,
        "applicant_name": a["applicant_name"],
        "reference": a["reference"],
        "months": a["months"],
        "last_report_at": a["last_report_at"],
    }


@app.post("/api/connector/monitor/status")
def monitor_status(request: Request):
    return _monitor_status(_check_token(request))


@app.post("/api/connector/monitor/upload")
async def monitor_upload(request: Request, background: BackgroundTasks):
    a = _check_token(request)
    if a["monitoring_status"] != "active":
        raise HTTPException(403, "Monitoring has been stopped")
    raw, bundle = await _read_bundle(request)
    report_id = _store_upload(a["id"], "refresh", raw, bundle)
    background.add_task(process_report, report_id)
    return {"status": "received", "report_id": report_id}


@app.post("/api/connector/monitor/stop")
def monitor_stop(request: Request):
    a = _check_token(request)
    store.stop_monitoring(a["id"], "client")
    return {"status": "stopped"}


def process_report(report_id: str) -> None:
    r = store.get_report(report_id)
    d = store.report_dir(r["application_id"], report_id)
    try:
        bundle = json.loads(gzip.decompress((d / "bundle.json.gz").read_bytes()))
        report = build_report(bundle)
        snap = snapshot(report)
        prev = store.previous_ready_report(r["application_id"], r["created_at"])
        alerts = compare(prev["indicators"], prev["snapshot"] or {}, report["indicators"], snap) if prev else []
        report["alerts"] = alerts
        report["report_id"] = report_id
        report["previous_report_at"] = prev["created_at"] if prev else None
        (d / "report.json").write_text(report_json(report))
        (d / "report.html").write_text(render_html(report, store.get_application(r["application_id"])))
        store.finish_report(report_id, report["period"]["to"].isoformat(), report["indicators"], snap,
                            report["summary"]["high_flags"], alerts)
    except Exception as e:  # keep the upload; the bank can recompute after a fix
        log.exception("report %s failed", report_id)
        store.fail_report(report_id, f"{type(e).__name__}: {e}")


# ------------------------------------------------------------------ bank UI


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse("/bank")


@app.get("/bank", response_class=HTMLResponse)
def dashboard(request: Request, filter: str = "", user: dict = Depends(bank_user)):
    pairs = store.applications_with_latest_report(user["tenant_id"])
    apps_by_id = {a["id"]: a for a, _ in pairs}
    entries = [
        {"application": a, "report": report, "overdue": store.monitoring_overdue(a),
         "new_alerts": report.get("alerts", []) if report else []}
        for a, report in pairs
    ]
    portfolio = dashboard_views.portfolio_view(entries)
    overdue_ids = {d["id"] for d in portfolio["data_overdue"]}
    rows = portfolio["rows"]
    if filter in ("attention", "watch"):
        rows = [r for r in rows if r["status"] == filter]
    elif filter == "alerts":
        rows = [r for r in rows if r["alert_count"] > 0]
    elif filter == "overdue":
        rows = [r for r in rows if r["id"] in overdue_ids]
    return templates.get_template("dashboard.html").render(
        portfolio=portfolio, rows=rows, active_filter=filter,
        apps_by_id=apps_by_id,
        bank_name=config.BANK_NAME,
        default_months=config.DEFAULT_MONTHS,
        monitoring_labels=MONITORING_LABELS,
        download_url=str(request.base_url) + "download" if config.CONNECTOR_EXE else None,
    )


@app.post("/bank/applications")
def create_application_form(
    applicant_name: str = Form(...), reference: str = Form(""), months: int = Form(config.DEFAULT_MONTHS),
    monitoring: str = Form(""), user: dict = Depends(bank_user),
):
    store.create_application(user["tenant_id"], applicant_name.strip(), reference.strip(),
                              max(6, min(months, 60)), monitoring == "on")
    return RedirectResponse("/bank", status_code=303)


COMPANY_TABS = ["overview", "sales", "profit", "recv", "pay", "wc", "debt", "bank", "tax", "data"]


@app.get("/bank/applications/{app_id}", response_class=HTMLResponse)
def application_detail(app_id: str, tab: str = "overview", user: dict = Depends(bank_user)):
    a = _get_or_404(app_id, user["tenant_id"])
    company = None
    if a["latest_report_id"]:
        report = store.load_report_json(app_id, a["latest_report_id"])
        if report:
            company = dashboard_views.company_view(report)
    return _render_application(a, "application.html", bank_name=config.BANK_NAME,
                                msme_login=store.msme_login_for(app_id),
                                company=company, active_tab=tab if tab in COMPANY_TABS else "overview")


@app.post("/bank/applications/{app_id}/code")
def regenerate(app_id: str, user: dict = Depends(bank_user)):
    _get_or_404(app_id, user["tenant_id"])
    store.regenerate_code(app_id)
    return RedirectResponse("/bank", status_code=303)


@app.post("/bank/applications/{app_id}/monitoring/refresh")
def monitoring_refresh(app_id: str, user: dict = Depends(bank_user)):
    _get_or_404(app_id, user["tenant_id"])
    store.request_refresh(app_id)
    return RedirectResponse(f"/bank/applications/{app_id}", status_code=303)


@app.post("/bank/applications/{app_id}/monitoring/stop")
def monitoring_stop_bank(app_id: str, user: dict = Depends(bank_user)):
    _get_or_404(app_id, user["tenant_id"])
    store.stop_monitoring(app_id, "bank")
    return RedirectResponse(f"/bank/applications/{app_id}", status_code=303)


@app.post("/bank/applications/{app_id}/msme-login", response_class=HTMLResponse)
def issue_msme_login(app_id: str, user: dict = Depends(bank_user)):
    """Issue (or rotate) the applicant's own login to their MSME dashboard. The
    password is only ever shown here, once — store.py keeps only its hash."""
    a = _get_or_404(app_id, user["tenant_id"])
    username, password = store.create_or_reset_msme_login(app_id, user["tenant_id"])
    return templates.get_template("msme_login.html").render(a=a, bank_name=config.BANK_NAME,
                                                              username=username, password=password)


@app.post("/bank/reports/{report_id}/recompute")
def recompute(report_id: str, user: dict = Depends(bank_user)):
    r = _report_or_404(report_id, user["tenant_id"])
    process_report(report_id)
    return RedirectResponse(f"/bank/applications/{r['application_id']}", status_code=303)


@app.get("/bank/applications/{app_id}/report", response_class=HTMLResponse)
def latest_report_html(app_id: str, user: dict = Depends(bank_user)):
    return _report_file(_latest_report_id(app_id, user["tenant_id"]), "report.html", "text/html; charset=utf-8")


@app.get("/bank/applications/{app_id}/report.json")
def latest_report_json(app_id: str, user: dict = Depends(bank_user)):
    return _report_file(_latest_report_id(app_id, user["tenant_id"]), "report.json", "application/json")


# Declared before the HTML route, which would otherwise also match "<id>.json".
@app.get("/bank/reports/{report_id}.json")
def report_json_view(report_id: str, user: dict = Depends(bank_user)):
    _report_or_404(report_id, user["tenant_id"])
    return _report_file(report_id, "report.json", "application/json")


@app.get("/bank/reports/{report_id}", response_class=HTMLResponse)
def report_html(report_id: str, user: dict = Depends(bank_user)):
    _report_or_404(report_id, user["tenant_id"])
    return _report_file(report_id, "report.html", "text/html; charset=utf-8")


@app.get("/bank/reports/{report_id}/bundle.json.gz")
def bundle_download(report_id: str, user: dict = Depends(bank_user)):
    _report_or_404(report_id, user["tenant_id"])
    return _report_file(report_id, "bundle.json.gz", "application/gzip")


# ------------------------------------------------------------------ MSME UI


MSME_TABS = ["home", "sales", "cust", "money", "dues"]


@app.get("/msme", response_class=HTMLResponse)
def msme_dashboard(tab: str = "home", user: dict = Depends(msme_user)):
    a = _get_or_404(user["application_id"])
    msme = None
    if a["latest_report_id"]:
        report = store.load_report_json(a["id"], a["latest_report_id"])
        if report:
            msme = dashboard_views.msme_view(report)
    return _render_application(a, "msme.html", bank_name=config.BANK_NAME,
                                msme=msme, active_tab=tab if tab in MSME_TABS else "home")


@app.get("/msme/report", include_in_schema=False)
def msme_latest_report_html(user: dict = Depends(msme_user)):
    """The MSME no longer gets the bank's full credit report — /msme already
    surfaces the analysis relevant to them (see design.md's msme_view)."""
    return RedirectResponse("/msme")


@app.get("/msme/report.json")
def msme_latest_report_json(user: dict = Depends(msme_user)):
    """MSME-safe JSON: the view-model (no ratings/thresholds/bank language),
    not the bank's full report.json."""
    report_id = _latest_report_id(user["application_id"])
    report = store.load_report_json(user["application_id"], report_id)
    return JSONResponse(dashboard_views.msme_view(report) if report else {})


def _render_application(a: dict, template_name: str, **extra) -> str:
    reports = store.list_reports(a["id"])
    ready = [r for r in reports if r["status"] == "ready"]
    # Indicator trend table: rows = indicators, columns = reports (oldest first, last 12).
    columns = list(reversed(ready[:12]))
    rows = []
    for ind in (columns[-1]["indicators"] if columns else []):
        rows.append({
            "label": ind["label"],
            "cells": [next((i for i in c["indicators"] if i["key"] == ind["key"]), None) for c in columns],
        })
    return templates.get_template(template_name).render(
        a=a, reports=reports, columns=columns, rows=rows,
        monitoring_label=MONITORING_LABELS[a["monitoring_status"]],
        due=store.monitoring_due(a), overdue=store.monitoring_overdue(a),
        overdue_days=config.MONITOR_OVERDUE_DAYS,
        **extra,
    )


def _get_or_404(app_id: str, tenant_id: str | None = None) -> dict:
    a = store.get_application(app_id, tenant_id)
    if not a:
        raise HTTPException(404, "No such application")
    return a


def _report_or_404(report_id: str, tenant_id: str | None = None) -> dict:
    """tenant_id=None skips the tenant check (used for MSME routes, which are
    already scoped to a single application_id by the msme_user dependency)."""
    r = store.get_report(report_id)
    if not r or not store.get_application(r["application_id"], tenant_id):
        raise HTTPException(404, "No such report")
    return r


def _latest_report_id(app_id: str, tenant_id: str | None = None) -> str:
    a = _get_or_404(app_id, tenant_id)
    if not a["latest_report_id"]:
        raise HTTPException(404, f"No report yet (status: {a['status']})")
    return a["latest_report_id"]


def _report_file(report_id: str, name: str, media_type: str):
    r = store.get_report(report_id)
    if not r:
        raise HTTPException(404, "No such report")
    path = store.report_dir(r["application_id"], report_id) / name
    if not path.exists():
        raise HTTPException(404, f"Report not available (status: {r['status']})")
    return Response(path.read_bytes(), media_type=media_type)


# --------------------------------------------------------------- bank JSON API


class ApplicationIn(BaseModel):
    applicant_name: str
    reference: str = ""
    months: int = Field(default=config.DEFAULT_MONTHS, ge=6, le=60)
    monitoring: bool = False


def _public(a: dict) -> dict:
    return {
        "id": a["id"],
        "applicant_name": a["applicant_name"],
        "reference": a["reference"],
        "status": a["status"],
        "link_code": store.display_code(a["code"]),
        "code_expires_at": a["code_expires_at"],
        "company_name": a["company_name"],
        "uploaded_at": a["uploaded_at"],
        "error": a["error"],
        "monitoring_offered": bool(a["monitoring_offered"]),
        "monitoring_status": a["monitoring_status"],
        "monitoring_due": store.monitoring_due(a),
        "monitoring_overdue": store.monitoring_overdue(a),
        "latest_report_id": a["latest_report_id"],
        "last_report_at": a["last_report_at"],
    }


@app.post("/api/bank/applications")
def api_create(body: ApplicationIn, user: dict = Depends(bank_user)):
    return _public(store.create_application(user["tenant_id"], body.applicant_name, body.reference,
                                             body.months, body.monitoring))


@app.get("/api/bank/applications/{app_id}")
def api_get(app_id: str, user: dict = Depends(bank_user)):
    return _public(_get_or_404(app_id, user["tenant_id"]))


@app.get("/api/bank/applications/{app_id}/reports")
def api_reports(app_id: str, user: dict = Depends(bank_user)):
    _get_or_404(app_id, user["tenant_id"])
    return [
        {k: r[k] for k in ("id", "source", "created_at", "status", "error", "period_to", "high_flags", "alerts", "indicators")}
        for r in store.list_reports(app_id)
    ]


@app.post("/api/bank/applications/{app_id}/monitoring/refresh")
def api_refresh(app_id: str, user: dict = Depends(bank_user)):
    _get_or_404(app_id, user["tenant_id"])
    store.request_refresh(app_id)
    return _public(store.get_application(app_id, user["tenant_id"]))


# ------------------------------------------------------------------ misc


@app.get("/download", include_in_schema=False)
def download():
    if not config.CONNECTOR_EXE or not Path(config.CONNECTOR_EXE).exists():
        raise HTTPException(404, "Connector download not configured")
    return FileResponse(config.CONNECTOR_EXE, filename="TallyConnector.exe")


@app.get("/healthz", include_in_schema=False)
def health():
    return JSONResponse({"ok": True})
