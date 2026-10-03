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

Platform admin endpoints (HTTP Basic auth, role `platform` — see store.py's `users` table):
    GET  /admin                                  overview: counts, recent activity, quick actions
    GET  /admin/banks[/{id}]                     list/create banks; per-bank detail, suspend/reactivate,
                                                  its bank users, its MSMEs
    GET  /admin/msmes[/{id}]                     every MSME (bank-linked or direct), filters/search;
                                                  per-MSME detail: code, monitoring, MSME login, report
                                                  history, read-only dashboard view, delete
    GET  /admin/users                            every user across tenants; reset/disable/enable/delete
    GET  /admin/audit                            the audit log
    GET  /admin/leads                            pilot requests from the home page
State-changing form POSTs under /admin and /bank are CSRF-guarded (`_csrf_guard`) — see its
docstring for the exact rule. Connector and JSON `/api/...` routes are exempt (not browser forms).

Public marketing site (no auth, server-rendered, see design.md's "Marketing home page"):
    GET  /                    the home page
    GET  /robots.txt, /sitemap.xml, /llms.txt
    GET  /favicon.ico, /static/...   icons, self-hosted fonts, the OG image
    POST /api/leads           demo/pilot request form (JSON or form-encoded)
Every route under /bank, /msme, /admin and /api carries `X-Robots-Tag: noindex, nofollow`
(see `_NOINDEX_PREFIXES` below) so only the home page is ever indexed.
"""

from __future__ import annotations

import gzip
import json
import logging
import secrets
import re
import time
import zlib
from collections import defaultdict, deque
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel, Field
from starlette.middleware.gzip import GZipMiddleware

from . import books, config, marketing, store
from .analysis.alerts import compare, snapshot
from .report import dashboard as dashboard_views, dashboard_charts as dc
from .report import format as fmt
from .report.builder import build_report, render_html, report_json

log = logging.getLogger("tally_connector")
app = FastAPI(title="Tally Connector backend", docs_url="/api/docs")
app.add_middleware(GZipMiddleware, minimum_size=500)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")

_NOINDEX_PREFIXES = ("/bank", "/msme", "/admin", "/api")


@app.middleware("http")
async def _noindex_private_routes(request: Request, call_next):
    """Everything except the public marketing site is a private, per-tenant
    tool — never meant to rank. Belt-and-braces alongside robots.txt, since a
    crawler that already has a stale link shouldn't need to consult it."""
    response = await call_next(request)
    if request.url.path.startswith(_NOINDEX_PREFIXES):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return response
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
    "sharing": "Receiving first share",
    "stopped_by_bank": "Stopped by bank",
    "stopped_by_client": "Stopped by client",
}


def _ensure_seed_tenant() -> None:
    """On a brand-new database, turn TC_ADMIN_USER/TC_ADMIN_PASSWORD into the first
    real tenant + bank user, so a fresh deploy still gets a working login without a
    separate provisioning step. Once any tenant exists this is a no-op — onboarding
    a second bank is done via `python -m app.manage` or /admin (see
    docs/agents/architecture.md)."""
    if store.get_tenant(store.seed_tenant_id()) is not None:
        return
    tenant = store.create_tenant(config.BANK_NAME, tenant_id=store.seed_tenant_id())
    store.create_user(tenant["id"], config.ADMIN_USER, config.ADMIN_PASSWORD, "bank")


def _ensure_seed_platform_admin() -> None:
    """Mirrors `_ensure_seed_tenant`, but for the platform's own /admin login and
    its built-in direct tenant. TC_PLATFORM_ADMIN_USER/PASSWORD are optional (unlike
    TC_ADMIN_USER/PASSWORD) — a deploy with no platform admin configured simply has
    no working /admin login until one is created via `python -m app.manage
    create-platform-admin` or TC_DEV's platform/platform default."""
    store.ensure_direct_tenant(config.PLATFORM_NAME)
    if not (config.PLATFORM_ADMIN_USER and config.PLATFORM_ADMIN_PASSWORD):
        return
    if store.list_users(tenant_id=store.direct_tenant_id(), role="platform"):
        return
    store.create_user(store.direct_tenant_id(), config.PLATFORM_ADMIN_USER, config.PLATFORM_ADMIN_PASSWORD, "platform")


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


def platform_user(creds: HTTPBasicCredentials = Depends(security)) -> dict:
    _ensure_seed_platform_admin()
    user = store.authenticate(creds.username, creds.password, "platform")
    if not user:
        raise HTTPException(401, "Unauthorised", headers={"WWW-Authenticate": "Basic"})
    return user


def _csrf_guard(request: Request) -> None:
    """Every state-changing form POST under /admin and /bank is Basic-auth protected,
    and Basic-auth browsers resend credentials on *any* request to the same origin
    automatically — there's no session cookie here, but the same ride-along risk
    exists: a page on another site could still submit a form/fetch to one of these
    URLs and the browser attaches the saved credentials itself.

    Rule: if the request carries an `Origin` header (or, failing that, `Referer`),
    its host must match this request's own `Host`, or the request is rejected with
    403. If *neither* header is present, the request is allowed through. Every
    current browser sets `Origin` on a cross-origin POST/fetch/form submission
    (and on same-origin ones too, in most cases) — so the only traffic with neither
    header is a non-browser client (curl, a script, a LOS integration, this test
    suite's TestClient) that was never exposed to the browser-based CSRF scenario
    this guards against in the first place. Connector endpoints (code/bearer-token
    authenticated) and the JSON `/api/bank/...` integration routes never carry this
    dependency — they're not browser form submissions and a non-browser caller may
    legitimately send neither header."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source:
        return
    # A present but host-less value (browsers send `Origin: null` from sandboxed iframes and
    # some redirects) is treated as cross-site, never as "no header".
    source_host = urlsplit(source).netloc.lower()
    if not source_host or source_host != request.url.netloc.lower():
        raise HTTPException(403, "Cross-site request blocked")


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
    # No per-IP limit on codes (owner's call, 2026-10-04): MSMEs retrying on a
    # shared office connection were locked out for an hour. Codes are 8
    # characters from a 31-letter alphabet and expire in TC_CODE_TTL_HOURS.
    application = store.find_by_code(code or "")
    if not application:
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
    tenant = store.get_tenant(a["tenant_id"])
    return {
        # The connector's consent screen shows this as who the applicant is sharing
        # books with — the applicant's own tenant's name, not a single global
        # setting, so a direct (no-bank) client correctly sees the platform's own
        # name (TC_PLATFORM_NAME) instead of some other bank's.
        "bank_name": tenant["name"] if tenant else config.BANK_NAME,
        "applicant_name": a["applicant_name"],
        "reference": a["reference"],
        "months": a["months"],
        "monitoring_offered": bool(a["monitoring_offered"]),
    }


async def _read_json(request: Request) -> tuple[bytes, dict]:
    """Read a JSON body, gzipped or not. Returns the gzipped bytes and the object."""
    max_bytes = config.MAX_UPLOAD_MB * 1024 * 1024
    raw = bytearray()
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > max_bytes:
            raise HTTPException(413, f"Upload larger than {config.MAX_UPLOAD_MB} MB")
    raw = bytes(raw)
    try:
        if raw[:2] == b"\x1f\x8b":
            data = _gunzip_json(raw)
        else:
            data = json.loads(raw)
            raw = gzip.compress(raw)
    except (OSError, zlib.error, ValueError) as e:
        raise HTTPException(400, f"Could not read upload: {e}")
    if not isinstance(data, dict):
        raise HTTPException(400, "Expected a JSON object")
    return raw, data


async def _read_bundle(request: Request) -> tuple[bytes, dict]:
    raw, bundle = await _read_json(request)
    if "vouchers" not in bundle or "ledgers" not in bundle:
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
    store.save_report_file(app_id, report_id, "bundle.json.gz", raw_gz)
    return report_id


@app.post("/api/connector/upload")
async def connector_upload(request: Request, background: BackgroundTasks):
    a = _check_code(request.headers.get("x-link-code", ""), request.client.host)
    raw, bundle = await _read_bundle(request)
    report_id = _store_upload(a["id"], "initial", raw, bundle)
    books.forget(a["id"])
    consent = bundle.get("consent") or {}
    store.mark_uploaded(a["id"], (bundle.get("company") or {}).get("name", ""), consent.get("accepted_by", ""),
                        request.client.host)
    tenant = store.get_tenant(a["tenant_id"])
    out = {"status": "received", "bank_name": tenant["name"] if tenant else config.BANK_NAME, "reference": a["reference"]}
    if a["monitoring_offered"] and consent.get("monitoring_opt_in"):
        out["monitor_token"] = store.start_monitoring(a["id"])
    background.add_task(process_report, report_id)
    return out


def _monitor_status(a: dict) -> dict:
    tenant = store.get_tenant(a["tenant_id"])
    return {
        "active": a["monitoring_status"] == "active",
        "status": a["monitoring_status"],
        "due": store.monitoring_due(a),
        "bank_name": tenant["name"] if tenant else config.BANK_NAME,
        "applicant_name": a["applicant_name"],
        "reference": a["reference"],
        "months": a["months"],
        "last_report_at": a["last_report_at"],
        "resume": (books.open_session(a["id"]) or {}).get("mode") == "full",
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
    books.forget(a["id"])
    background.add_task(process_report, report_id)
    return {"status": "received", "report_id": report_id}


# ------------------------------------------------------------ incremental sync
# The connector sends a company's books in pieces (see books.py): start, then
# masters / vouchers / present in any number of requests, then finish.


class SyncStartIn(BaseModel):
    company: dict
    period: dict
    want_full: bool = False
    tally_alter_id: int | None = None
    monitoring_opt_in: bool = False
    consent: dict = Field(default_factory=dict)
    machine: dict = Field(default_factory=dict)
    connector_version: str = ""


@app.post("/api/connector/sync/start")
async def sync_start(request: Request):
    _, data = await _read_json(request)
    try:
        body = SyncStartIn.model_validate(data)
    except ValueError as e:
        raise HTTPException(400, f"Invalid sync request: {e}")
    try:
        pfrom = date.fromisoformat(body.period["from"]).isoformat()
        pto = date.fromisoformat(body.period["to"]).isoformat()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "period.from and period.to must be YYYY-MM-DD")
    out = {}
    code = request.headers.get("x-link-code", "")
    if code:
        a = _check_code(code, request.client.host)
        store.mark_uploaded(a["id"], body.company.get("name", ""), body.consent.get("accepted_by", ""), request.client.host)
        if a["monitoring_offered"] and body.monitoring_opt_in:
            out["token"], out["monitoring"] = store.start_monitoring(a["id"]), True
        else:
            out["token"], out["monitoring"] = store.start_share(a["id"]), False
        tenant = store.get_tenant(a["tenant_id"])
        out.update(bank_name=tenant["name"] if tenant else config.BANK_NAME, reference=a["reference"])
        a = store.get_application(a["id"])
    else:
        a = _check_token(request)
        if a["monitoring_status"] not in ("active", "sharing"):
            raise HTTPException(403, "Monitoring has been stopped")
    try:
        plan = books.start(a, pfrom, pto, body.want_full, body.tally_alter_id, body.company.get("guid", ""),
                           body.company.get("name", ""))
    except books.SyncBusy as e:
        raise HTTPException(503, str(e))
    session = books.get_session(plan["sync_id"])
    books.stage(session, {"company": body.company, "machine": body.machine, "consent": body.consent,
                          "connector_version": body.connector_version})
    return {**out, **plan}


def _sync_session(sync_id: str, request: Request) -> dict:
    a = _check_token(request)
    s = books.get_session(sync_id)
    if not s or s["application_id"] != a["id"]:
        raise HTTPException(404, "Unknown sync")
    if s["status"] != "open":
        raise HTTPException(409, f"This sync is {s['status']}")
    return s


@app.post("/api/connector/sync/{sync_id}/masters")
async def sync_masters(sync_id: str, request: Request):
    s = _sync_session(sync_id, request)
    _, data = await _read_json(request)
    return {"affected": books.stage(s, data)}


@app.post("/api/connector/sync/{sync_id}/vouchers")
async def sync_vouchers(sync_id: str, request: Request):
    s = _sync_session(sync_id, request)
    _, data = await _read_json(request)
    try:
        dfrom, dto = date.fromisoformat(data["from"]).isoformat(), date.fromisoformat(data["to"]).isoformat()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "from and to must be YYYY-MM-DD")
    return {"affected": books.replace_vouchers(s, dfrom, dto, data.get("vouchers", []), data.get("month"))}


@app.post("/api/connector/sync/{sync_id}/present")
async def sync_present(sync_id: str, request: Request):
    s = _sync_session(sync_id, request)
    _, data = await _read_json(request)
    try:
        dfrom, dto = date.fromisoformat(data["from"]).isoformat(), date.fromisoformat(data["to"]).isoformat()
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "from and to must be YYYY-MM-DD")
    return {"affected": books.present(s, dfrom, dto, data.get("guids", []))}


def _months(pfrom: str, pto: str) -> list[str]:
    y, m = int(pfrom[:4]), int(pfrom[5:7])
    out = []
    while f"{y:04d}-{m:02d}" <= pto[:7]:
        out.append(f"{y:04d}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


@app.post("/api/connector/sync/{sync_id}/finish")
def sync_finish(sync_id: str, request: Request, background: BackgroundTasks):
    s = _sync_session(sync_id, request)
    if s["mode"] == "full":
        missing = [m for m in _months(s["period_from"], s["period_to"]) if m not in s["months_done"]]
        if missing:
            raise HTTPException(409, f"Months not received yet: {', '.join(missing)}")
    books.mark(sync_id, "finishing")
    background.add_task(finish_sync, sync_id)
    a = store.get_application(s["application_id"])
    return {"status": "received", "monitoring": a["monitoring_status"] == "active"}


def finish_sync(sync_id: str) -> None:
    s = books.get_session(sync_id)
    app_id = s["application_id"]
    try:
        bundle = books.finish(s)
    except Exception as e:
        log.exception("sync %s failed", sync_id)
        books.mark(sync_id, "failed")
        if not store.latest_ready_report(app_id):
            store.set_status(app_id, "failed", f"{type(e).__name__}: {e}")
        return
    source = "refresh" if store.list_reports(app_id) else "initial"
    report_id = _store_upload(app_id, source, gzip.compress(json.dumps(bundle).encode()), bundle)
    store.end_share(app_id)
    needs_full = bundle["sync"]["needs_full"]
    process_report(report_id, bundle)
    if needs_full:
        store.request_refresh(app_id)  # after the report, which clears the flag: full re-read at the next check-in


class ConnectorLogIn(BaseModel):
    kind: str = "log"
    text: str
    session: str = ""


@app.post("/api/connector/log")
async def connector_log(request: Request):
    """The connector's log, a Tally check, a system report (Tally release,
    add-ons, Windows) or a picture of the Tally window (base64 JPEG), for support. Accepts a
    connector token, or a live one-time code (which it does not use up), so a
    check can be sent before the first share."""
    code = request.headers.get("x-link-code", "")
    a = _check_code(code, request.client.host) if code else _check_token(request)
    _, data = await _read_json(request)
    try:
        body = ConnectorLogIn.model_validate(data)
    except ValueError as e:
        raise HTTPException(400, f"Invalid log: {e}")
    if body.kind not in ("log", "diagnose", "system", "screenshot"):
        raise HTTPException(400, "kind must be log, diagnose, system or screenshot")
    if body.kind == "screenshot":
        if len(body.text) > 3_000_000:
            raise HTTPException(413, "Picture too large")
        text = body.text
    else:
        text = body.text[-200_000:]
    store.save_connector_log(a["id"], body.kind, text, body.session[:64])
    return {"status": "saved"}


@app.post("/api/connector/monitor/stop")
def monitor_stop(request: Request):
    a = _check_token(request)
    store.stop_monitoring(a["id"], "client")
    return {"status": "stopped"}


def process_report(report_id: str, bundle: dict | None = None) -> None:
    r = store.get_report(report_id)
    app_id = r["application_id"]
    try:
        if bundle is None:
            raw = store.load_report_file(app_id, report_id, "bundle.json.gz")
            if raw is None:
                raise RuntimeError("the raw data for this report is no longer kept")
            bundle = json.loads(gzip.decompress(raw))
        report = build_report(bundle)
        del bundle
        snap = snapshot(report)
        prev = store.previous_ready_report(app_id, r["created_at"])
        alerts = compare(prev["indicators"], prev["snapshot"] or {}, report["indicators"], snap) if prev else []
        report["alerts"] = alerts
        report["report_id"] = report_id
        report["previous_report_at"] = prev["created_at"] if prev else None
        store.save_report_file(app_id, report_id, "report.json", report_json(report).encode())
        store.save_report_file(app_id, report_id, "report.html", render_html(report, store.get_application(app_id)).encode())
        store.finish_report(report_id, report["period"]["to"].isoformat(), report["indicators"], snap,
                            report["summary"]["high_flags"], alerts)
    except Exception as e:  # keep the upload; the bank can recompute after a fix
        log.exception("report %s failed", report_id)
        store.fail_report(report_id, f"{type(e).__name__}: {e}")
        return
    try:
        store.prune_report_files(app_id, config.KEEP_RAW_DAYS)
        books.prune_sessions(app_id, config.KEEP_RAW_DAYS)
    except Exception:
        log.exception("retention for %s failed", app_id)


# ------------------------------------------------------------- public marketing site


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_lead_attempts: dict[str, deque] = defaultdict(deque)
MAX_LEADS_PER_HOUR = 5


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def home(sent: str = ""):
    ccc = marketing.cash_cycle(**marketing.CASH_CYCLE_DEFAULTS)
    return templates.get_template("home.html").render(
        title=marketing.TITLE,
        description=marketing.DESCRIPTION,
        canonical=config.PUBLIC_URL + "/",
        public_url=config.PUBLIC_URL,
        ld_json=json.dumps(marketing.json_ld(config.PUBLIC_URL)),
        bars=marketing.hero_bars(),
        feed=marketing.ALERT_FEED_POOL[:3],
        feed_pool_json=json.dumps(marketing.ALERT_FEED_POOL),
        bank_tabs=marketing.BANK_TABS,
        overview_panel=marketing.bank_panel("overview"),
        bank_panels_json=json.dumps({k: marketing.bank_panel(k) for k in marketing.BANK_PANELS}),
        ccc=ccc,
        faq=marketing.FAQ,
        lead_kinds=marketing.LEAD_KINDS,
        sent=sent,
    )


def _lead_throttle(ip: str) -> deque:
    attempts = _lead_attempts[ip]
    cutoff = time.time() - 3600
    while attempts and attempts[0] < cutoff:
        attempts.popleft()
    return attempts


@app.post("/api/leads", include_in_schema=False)
async def create_lead(request: Request):
    """Demo/pilot request from the home page's form. Accepts JSON (the page's own
    fetch()) or a plain form POST (works without JS, then redirects back with
    `?sent=1#demo`). A filled honeypot or a bad email is rejected the same way
    either path reports errors, just without ever touching `store.create_lead`."""
    ip = request.client.host or "unknown"
    wants_json = "application/json" in request.headers.get("accept", "")

    if request.headers.get("content-type", "").startswith("application/json"):
        data = await request.json()
    else:
        data = dict(await request.form())

    def reject(status: int, message: str):
        if wants_json:
            raise HTTPException(status, message)
        return RedirectResponse("/?sent=0#demo", status_code=303)

    attempts = _lead_throttle(ip)
    if len(attempts) >= MAX_LEADS_PER_HOUR:
        return reject(429, "Too many requests. Please try again later.")
    attempts.append(time.time())

    if (data.get("website") or "").strip():
        return reject(400, "Could not submit the form.")  # honeypot tripped

    name = (data.get("name") or "").strip()
    org = (data.get("org") or "").strip()
    email = (data.get("email") or "").strip()
    phone = (data.get("phone") or "").strip()
    kind = (data.get("kind") or "").strip()
    if not name or not org or not EMAIL_RE.match(email):
        return reject(400, "Please fill in your name, organisation and a valid work email.")

    store.create_lead(name, org, email, phone, kind, request.headers.get("user-agent", ""), ip)
    if wants_json:
        return JSONResponse({"ok": True, "message": "Thanks, we will be in touch."})
    return RedirectResponse("/?sent=1#demo", status_code=303)


ROBOTS_TXT = """\
User-agent: *
Allow: /
Disallow: /bank
Disallow: /msme
Disallow: /admin
Disallow: /api
Disallow: /download

Sitemap: {public_url}/sitemap.xml
"""

LLMS_TXT = """\
# Tally Connector

Tally Connector lets a bank or NBFC pull an MSME borrower's Tally books (TallyPrime or \
Tally.ERP 9), with the borrower's consent, and turns them into a bank-grade credit report: \
revenue, customer concentration, receivables/payables ageing, working capital, balance \
sheet, leverage, banking/cash behaviour, GST, and red flags. Reports refresh daily once a \
borrower opts into monitoring. The MSME gets its own plain-language dashboard of the same \
data, with no ratings or lending language.

Home page: {public_url}/
"""


@app.get("/robots.txt", include_in_schema=False)
def robots_txt():
    return PlainTextResponse(ROBOTS_TXT.format(public_url=config.PUBLIC_URL))


@app.get("/llms.txt", include_in_schema=False)
def llms_txt():
    return PlainTextResponse(LLMS_TXT.format(public_url=config.PUBLIC_URL))


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap_xml():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        "  <url>\n"
        f"    <loc>{config.PUBLIC_URL}/</loc>\n"
        f"    <lastmod>{marketing.HOME_LASTMOD}</lastmod>\n"
        "  </url>\n"
        "</urlset>\n"
    )
    return Response(xml, media_type="application/xml")


@app.get("/favicon.ico", include_in_schema=False)
def favicon_ico():
    return FileResponse(_STATIC_DIR / "favicon.ico", media_type="image/x-icon")


# ------------------------------------------------------------------ bank UI


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
        download_url=str(request.base_url) + "download" if (config.CONNECTOR_EXE or config.CONNECTOR_URL) else None,
    )


@app.post("/bank/applications")
def create_application_form(
    applicant_name: str = Form(...), reference: str = Form(""), months: int = Form(config.DEFAULT_MONTHS),
    monitoring: str = Form(""), user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard),
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
def regenerate(app_id: str, user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id, user["tenant_id"])
    store.regenerate_code(app_id)
    return RedirectResponse("/bank", status_code=303)


@app.post("/bank/applications/{app_id}/monitoring/refresh")
def monitoring_refresh(app_id: str, user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id, user["tenant_id"])
    store.request_refresh(app_id)
    return RedirectResponse(f"/bank/applications/{app_id}", status_code=303)


@app.post("/bank/applications/{app_id}/monitoring/stop")
def monitoring_stop_bank(app_id: str, user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id, user["tenant_id"])
    store.stop_monitoring(app_id, "bank")
    return RedirectResponse(f"/bank/applications/{app_id}", status_code=303)


@app.post("/bank/applications/{app_id}/msme-login", response_class=HTMLResponse)
def issue_msme_login(app_id: str, user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard)):
    """Issue (or rotate) the applicant's own login to their MSME dashboard. The
    password is only ever shown here, once — store.py keeps only its hash."""
    a = _get_or_404(app_id, user["tenant_id"])
    username, password = store.create_or_reset_msme_login(app_id, user["tenant_id"])
    return templates.get_template("msme_login.html").render(a=a, bank_name=config.BANK_NAME,
                                                              username=username, password=password)


@app.post("/bank/reports/{report_id}/recompute")
def recompute(report_id: str, user: dict = Depends(bank_user), _csrf: None = Depends(_csrf_guard)):
    r = _report_or_404(report_id, user["tenant_id"])
    if not store.has_raw(r["application_id"], report_id):
        raise HTTPException(410, f"The raw data for this report is no longer kept (older than {config.KEEP_RAW_DAYS} days).")
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
    raw = store.raw_report_ids(a["id"])
    for r in reports:
        r["has_raw"] = r["id"] in raw
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
        a=a, reports=reports, columns=columns, rows=rows, sync=books.progress(a["id"]),
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
    data = store.load_report_file(r["application_id"], report_id, name)
    if data is None and name == "report.html":
        # Pages of older reports are dropped by the retention policy and
        # re-rendered from their figures, which are always kept.
        report = store.load_report_json(r["application_id"], report_id)
        if report is not None:
            data = render_html(report, store.get_application(r["application_id"])).encode()
    if data is None:
        raise HTTPException(404, f"Report not available (status: {r['status']})")
    return Response(data, media_type=media_type)


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
        "resume": books.open_session(a["id"]) is not None,
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


# --------------------------------------------------------------- platform admin


def _audit(user: dict, action: str, target_type: str, target_id: str | None, summary: str) -> None:
    store.record_audit(user["username"], action, target_type, target_id, summary)


@app.get("/admin", response_class=HTMLResponse)
def admin_overview(user: dict = Depends(platform_user)):
    return templates.get_template("admin_overview.html").render(
        user=user, counts=store.counts_overview(), audit=store.list_audit(limit=20),
        platform_name=config.PLATFORM_NAME,
    )


@app.get("/admin/banks", response_class=HTMLResponse)
def admin_banks(user: dict = Depends(platform_user)):
    return templates.get_template("admin_banks.html").render(user=user, banks=store.list_tenants(kind="bank"))


@app.post("/admin/banks")
def admin_create_bank(name: str = Form(...), user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    tenant = store.create_tenant(name.strip(), kind="bank")
    _audit(user, "bank.create", "tenant", tenant["id"], f"Created bank {tenant['name']}")
    return RedirectResponse(f"/admin/banks/{tenant['id']}", status_code=303)


def _bank_or_404(tenant_id: str) -> dict:
    t = store.get_tenant(tenant_id)
    if not t or t["kind"] != "bank":
        raise HTTPException(404, "No such bank")
    return t


@app.get("/admin/banks/{tenant_id}", response_class=HTMLResponse)
def admin_bank_detail(tenant_id: str, user: dict = Depends(platform_user)):
    tenant = _bank_or_404(tenant_id)
    return templates.get_template("admin_bank_detail.html").render(
        user=user, tenant=tenant, bank_users=store.list_users(tenant_id=tenant_id, role="bank"),
        msmes=store.list_all_applications(tenant_id=tenant_id), default_months=config.DEFAULT_MONTHS,
    )


@app.post("/admin/banks/{tenant_id}/rename")
def admin_bank_rename(tenant_id: str, name: str = Form(...), user: dict = Depends(platform_user),
                       _csrf: None = Depends(_csrf_guard)):
    _bank_or_404(tenant_id)
    store.rename_tenant(tenant_id, name.strip())
    _audit(user, "bank.rename", "tenant", tenant_id, f"Renamed bank to {name.strip()}")
    return RedirectResponse(f"/admin/banks/{tenant_id}", status_code=303)


@app.post("/admin/banks/{tenant_id}/suspend")
def admin_bank_suspend(tenant_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    _bank_or_404(tenant_id)
    store.set_tenant_status(tenant_id, "suspended")
    _audit(user, "bank.suspend", "tenant", tenant_id, "Suspended bank — its users can no longer log in")
    return RedirectResponse(f"/admin/banks/{tenant_id}", status_code=303)


@app.post("/admin/banks/{tenant_id}/reactivate")
def admin_bank_reactivate(tenant_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    _bank_or_404(tenant_id)
    store.set_tenant_status(tenant_id, "active")
    _audit(user, "bank.reactivate", "tenant", tenant_id, "Reactivated bank")
    return RedirectResponse(f"/admin/banks/{tenant_id}", status_code=303)


@app.post("/admin/banks/{tenant_id}/users", response_class=HTMLResponse)
def admin_bank_create_user(tenant_id: str, username: str = Form(...), user: dict = Depends(platform_user),
                            _csrf: None = Depends(_csrf_guard)):
    _bank_or_404(tenant_id)
    username = username.strip()
    if store.username_taken(username):
        raise HTTPException(400, "Username already taken")
    password = secrets.token_urlsafe(9)
    new_user = store.create_user(tenant_id, username, password, "bank")
    _audit(user, "user.create", "user", new_user["id"], f"Created bank user {username}")
    return templates.get_template("admin_credential.html").render(
        heading=f"Bank login for {username}", username=username, password=password,
        back_url=f"/admin/banks/{tenant_id}",
    )


@app.get("/admin/msmes", response_class=HTMLResponse)
def admin_msmes(bank: str = "", kind: str = "", status: str = "", overdue: str = "", q: str = "",
                 user: dict = Depends(platform_user)):
    apps = store.list_all_applications(tenant_id=bank or None, search=q)
    if kind:
        apps = [a for a in apps if a["tenant_kind"] == kind]
    if status:
        apps = [a for a in apps if a["status"] == status]
    if overdue:
        apps = [a for a in apps if store.monitoring_overdue(a)]
    return templates.get_template("admin_msmes.html").render(
        user=user, apps=apps, banks=store.list_tenants(kind="bank"), direct_id=store.direct_tenant_id(),
        filters={"bank": bank, "kind": kind, "status": status, "overdue": overdue, "q": q},
        default_months=config.DEFAULT_MONTHS,
    )


@app.post("/admin/msmes")
def admin_create_msme(applicant_name: str = Form(...), reference: str = Form(""),
                       months: int = Form(config.DEFAULT_MONTHS), monitoring: str = Form(""), bank_id: str = Form(""),
                       user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    tenant_id = bank_id or store.direct_tenant_id()
    if bank_id:
        _bank_or_404(bank_id)
    a = store.create_application(tenant_id, applicant_name.strip(), reference.strip(),
                                  max(6, min(months, 60)), monitoring == "on")
    _audit(user, "msme.create", "application", a["id"], f"Created MSME {a['applicant_name']}")
    return RedirectResponse(f"/admin/msmes/{a['id']}", status_code=303)


@app.get("/admin/msmes/{app_id}", response_class=HTMLResponse)
def admin_msme_detail(app_id: str, user: dict = Depends(platform_user)):
    a = _get_or_404(app_id)
    return templates.get_template("admin_msme_detail.html").render(
        user=user, a=a, tenant=store.get_tenant(a["tenant_id"]), reports=store.list_reports(app_id),
        msme_login=store.msme_login_for(app_id), due=store.monitoring_due(a), overdue=store.monitoring_overdue(a),
        overdue_days=config.MONITOR_OVERDUE_DAYS, monitoring_label=MONITORING_LABELS[a["monitoring_status"]],
        connector_logs=store.list_connector_logs(app_id), sync=books.progress(app_id),
    )


@app.post("/admin/msmes/{app_id}/code")
def admin_msme_code(app_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id)
    store.regenerate_code(app_id)
    _audit(user, "msme.code.regenerate", "application", app_id, "Issued a new one-time code")
    return RedirectResponse(f"/admin/msmes/{app_id}", status_code=303)


@app.post("/admin/msmes/{app_id}/monitoring/refresh")
def admin_msme_refresh(app_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id)
    store.request_refresh(app_id)
    _audit(user, "msme.monitoring.refresh", "application", app_id, "Requested an immediate refresh")
    return RedirectResponse(f"/admin/msmes/{app_id}", status_code=303)


@app.post("/admin/msmes/{app_id}/monitoring/stop")
def admin_msme_stop(app_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    _get_or_404(app_id)
    store.stop_monitoring(app_id, "bank")
    _audit(user, "msme.monitoring.stop", "application", app_id, "Stopped monitoring")
    return RedirectResponse(f"/admin/msmes/{app_id}", status_code=303)


@app.post("/admin/msmes/{app_id}/msme-login", response_class=HTMLResponse)
def admin_issue_msme_login(app_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    a = _get_or_404(app_id)
    username, password = store.create_or_reset_msme_login(app_id, a["tenant_id"])
    _audit(user, "msme.login.issue", "application", app_id, f"Issued/reset MSME login {username}")
    return templates.get_template("admin_credential.html").render(
        heading=f"MSME login for {a['applicant_name']}", username=username, password=password,
        back_url=f"/admin/msmes/{app_id}",
    )


@app.get("/admin/msmes/{app_id}/view", response_class=HTMLResponse)
def admin_view_msme(app_id: str, tab: str = "overview", user: dict = Depends(platform_user)):
    """Read-only reuse of the bank's own company page — a platform admin has no
    tenant-scoped bank account, so this bypasses `bank_user`/tenant_id entirely."""
    a = _get_or_404(app_id)
    company = None
    if a["latest_report_id"]:
        report = store.load_report_json(app_id, a["latest_report_id"])
        if report:
            company = dashboard_views.company_view(report)
    return _render_application(a, "application.html", bank_name=config.BANK_NAME,
                                msme_login=store.msme_login_for(app_id), company=company,
                                active_tab=tab if tab in COMPANY_TABS else "overview", readonly=True)


@app.post("/admin/msmes/{app_id}/delete")
def admin_delete_msme(app_id: str, confirm: str = Form(...), user: dict = Depends(platform_user),
                       _csrf: None = Depends(_csrf_guard)):
    a = _get_or_404(app_id)
    if confirm.strip() != a["applicant_name"]:
        raise HTTPException(400, "Typed name did not match this MSME's name — nothing was deleted")
    store.delete_application(app_id)
    _audit(user, "msme.delete", "application", app_id, f"Deleted MSME {a['applicant_name']} and all its data")
    return RedirectResponse("/admin/msmes", status_code=303)


@app.get("/admin/users", response_class=HTMLResponse)
def admin_users(role: str = "", bank: str = "", user: dict = Depends(platform_user)):
    tenants = {t["id"]: t for t in store.list_tenants()}
    return templates.get_template("admin_users.html").render(
        user=user, users=store.list_users(tenant_id=bank or None, role=role or None), tenants=tenants,
        banks=store.list_tenants(kind="bank"), filters={"role": role, "bank": bank},
    )


@app.post("/admin/users/{user_id}/reset-password", response_class=HTMLResponse)
def admin_reset_password(user_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "No such user")
    password = secrets.token_urlsafe(9)
    store.set_password(user_id, password)
    _audit(user, "user.reset_password", "user", user_id, f"Reset password for {target['username']}")
    return templates.get_template("admin_credential.html").render(
        heading=f"New password for {target['username']}", username=target["username"], password=password,
        back_url="/admin/users",
    )


@app.post("/admin/users/{user_id}/disable")
def admin_disable_user(user_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "No such user")
    if target["role"] == "platform" and not target["disabled"] and store.count_active_platform_admins() <= 1:
        raise HTTPException(400, "Cannot disable the last active platform admin")
    store.set_user_disabled(user_id, True)
    _audit(user, "user.disable", "user", user_id, f"Disabled {target['username']}")
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/enable")
def admin_enable_user(user_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "No such user")
    store.set_user_disabled(user_id, False)
    _audit(user, "user.enable", "user", user_id, f"Enabled {target['username']}")
    return RedirectResponse("/admin/users", status_code=303)


@app.post("/admin/users/{user_id}/delete")
def admin_delete_user(user_id: str, user: dict = Depends(platform_user), _csrf: None = Depends(_csrf_guard)):
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "No such user")
    if target["role"] == "platform" and not target["disabled"] and store.count_active_platform_admins() <= 1:
        raise HTTPException(400, "Cannot delete the last active platform admin")
    store.delete_user(user_id)
    _audit(user, "user.delete", "user", user_id, f"Deleted {target['username']}")
    return RedirectResponse("/admin/users", status_code=303)


@app.get("/admin/audit", response_class=HTMLResponse)
def admin_audit(user: dict = Depends(platform_user)):
    return templates.get_template("admin_audit.html").render(user=user, audit=store.list_audit(limit=200))


@app.get("/admin/leads", response_class=HTMLResponse)
def admin_leads(user: dict = Depends(platform_user)):
    return templates.get_template("admin_leads.html").render(user=user, leads=store.list_leads(limit=500))


# ------------------------------------------------------------------ misc


@app.get("/download", include_in_schema=False)
def download():
    if config.CONNECTOR_URL:
        return RedirectResponse(config.CONNECTOR_URL)
    if not config.CONNECTOR_EXE or not Path(config.CONNECTOR_EXE).exists():
        raise HTTPException(404, "Connector download not configured")
    return FileResponse(config.CONNECTOR_EXE, filename="TallyConnector.exe")


@app.get("/healthz", include_in_schema=False)
def health():
    return JSONResponse({"ok": True})
