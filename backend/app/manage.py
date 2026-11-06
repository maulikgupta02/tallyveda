"""Tenant/user provisioning CLI.

There's no admin web UI yet (see docs/agents/architecture.md) — onboarding a
second bank, or adding another login for an existing bank, is done from here:

    python -m app.manage create-tenant "Second Bank"
    python -m app.manage create-bank-user <tenant_id> ops@secondbank.in
    python -m app.manage create-platform-admin ops@platform.example

MATTR AI bridge (app/n8n.py; needs TC_N8N_WEBHOOK_URL and TC_N8N_WEBHOOK_SECRET):

    python -m app.manage mattr-forward <application_id> on|off
    python -m app.manage n8n-resync <application_id> | --all-flagged
    python -m app.manage n8n-deliver
    python -m app.manage n8n-reconcile [<application_id>] [--compare n8n.json]

All print the generated password once; it is never stored or logged in the
clear (store.py hashes it immediately). There is now also an /admin web UI
(see docs/agents/architecture.md) for everyday bank/MSME/user management —
these commands remain for first-time provisioning and scripted setups.
"""

from __future__ import annotations

import argparse
import secrets

from . import store


def create_tenant(args: argparse.Namespace) -> None:
    tenant = store.create_tenant(args.name)
    print(f"tenant_id: {tenant['id']}")


def create_bank_user(args: argparse.Namespace) -> None:
    if not store.get_tenant(args.tenant_id):
        raise SystemExit(f"No such tenant: {args.tenant_id}")
    password = args.password or secrets.token_urlsafe(9)
    store.create_user(args.tenant_id, args.username, password, "bank")
    print(f"username: {args.username}")
    print(f"password: {password}")


def create_platform_admin(args: argparse.Namespace) -> None:
    from . import config

    if store.username_taken(args.username):
        raise SystemExit(f"Username already taken: {args.username}")
    store.ensure_direct_tenant(config.PLATFORM_NAME)
    password = args.password or secrets.token_urlsafe(9)
    store.create_user(store.direct_tenant_id(), args.username, password, "platform")
    print(f"username: {args.username}")
    print(f"password: {password}")

def list_leads(args: argparse.Namespace) -> None:
    """Demo/pilot requests from the public home page (see `main.py`'s `/api/leads`).
    Also shown on /admin/leads."""
    for lead in store.list_leads(args.limit):
        print(f"{lead['created_at']}  {lead['name']} <{lead['email']}>  {lead['org']}  "
              f"{lead['kind']}  {lead['phone'] or '-'}")


def _need_bridge() -> None:
    from . import n8n

    if not n8n.enabled():
        raise SystemExit("The MATTR AI bridge is off: set TC_N8N_WEBHOOK_URL and TC_N8N_WEBHOOK_SECRET.")


def mattr_forward(args: argparse.Namespace) -> None:
    from . import n8n

    _need_bridge()
    a = store.get_application(args.application_id)
    if not a:
        raise SystemExit(f"No such application: {args.application_id}")
    on = args.state == "on"
    seq = n8n.set_forward(a["id"], on)
    store.record_audit("cli", "mattr.forward_on" if on else "mattr.forward_off", "application", a["id"],
                       f"MATTR AI forwarding {'on' if on else 'off'} for {a['applicant_name']}")
    what = "snapshot" if on else "purge"
    print(f"{a['applicant_name']}: forwarding {args.state}" + (f", {what} queued as batch {seq}" if seq else
          ("" if on is False else " (no books yet: the next sync sends them)")))
    print(n8n.deliver_pending())


def n8n_resync(args: argparse.Namespace) -> None:
    from . import n8n

    _need_bridge()
    if args.all_flagged:
        with store.db() as conn:
            ids = [r["id"] for r in conn.execute("SELECT id FROM applications WHERE mattr_forward = 1").fetchall()]
    elif args.application_id:
        ids = [args.application_id]
    else:
        raise SystemExit("Give an application id or --all-flagged")
    for app_id in ids:
        seq = n8n.resync(app_id)
        print(f"{app_id}: " + (f"snapshot queued as batch {seq}" if seq else "nothing queued (not flagged, or no books)"))
    print(n8n.deliver_pending())


def n8n_deliver(args: argparse.Namespace) -> None:
    from . import n8n

    _need_bridge()
    print(n8n.deliver_pending())
    with store.db() as conn:
        rows = conn.execute(
            "SELECT application_id, kind, batch_seq, attempts, last_error, next_attempt_at, dead_at FROM n8n_outbox"
            " WHERE sent_at IS NULL ORDER BY application_id, batch_seq"
        ).fetchall()
    for r in rows:
        print(f"waiting: {r['application_id']} {r['kind']} #{r['batch_seq']} attempts={r['attempts']}"
              f" error={r['last_error'] or '-'} next={r['next_attempt_at']}" + (" DEAD" if r["dead_at"] else ""))


def n8n_reconcile(args: argparse.Namespace) -> None:
    """Prints the backend's figures as JSON. Run `SELECT bridge_reconcile_all();`
    on the n8n database, save its output, and pass it with --compare."""
    import json

    from . import n8n

    if args.application_id:
        ids = [args.application_id]
    else:
        with store.db() as conn:
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM applications WHERE mattr_forward = 1 ORDER BY id").fetchall()]
    ours = {i: n8n.reconcile_facts(i) for i in ids}
    if not args.compare:
        print(json.dumps(list(ours.values()), indent=2))
        return
    with open(args.compare) as f:
        theirs = {r["application_id"]: r for r in json.load(f)}
    drift = 0
    for app_id, mine in ours.items():
        other = theirs.get(app_id)
        if other is None:
            print(f"DRIFT {app_id}: missing in n8n")
            drift += 1
            continue
        for key in ("vouchers", "max_alter_id", "closing_sha256"):
            if mine[key] != other.get(key):
                print(f"DRIFT {app_id}: {key} backend={mine[key]} n8n={other.get(key)}")
                drift += 1
        for month in sorted(set(mine["months"]) | set(other.get("months", {}))):
            a, b = mine["months"].get(month), other.get("months", {}).get(month)
            if a != b:
                print(f"DRIFT {app_id}: {month} backend={a} n8n={b}")
                drift += 1
    print(f"{len(ours)} companies compared, {drift} differences")
    if drift:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("create-tenant", help="Register a new bank")
    p.add_argument("name")
    p.set_defaults(func=create_tenant)

    p = sub.add_parser("create-bank-user", help="Add a dashboard login for an existing bank")
    p.add_argument("tenant_id")
    p.add_argument("username")
    p.add_argument("--password", help="Defaults to a random one, printed once")
    p.set_defaults(func=create_bank_user)

    p = sub.add_parser("create-platform-admin", help="Add a platform admin login (/admin)")
    p.add_argument("username")
    p.add_argument("--password", help="Defaults to a random one, printed once")
    p.set_defaults(func=create_platform_admin)

    p = sub.add_parser("list-leads", help="Show demo/pilot requests from the home page")
    p.add_argument("--limit", type=int, default=200)
    p.set_defaults(func=list_leads)

    p = sub.add_parser("mattr-forward", help="Flag a company for MATTR AI (written agreement on file), or unflag it")
    p.add_argument("application_id")
    p.add_argument("state", choices=("on", "off"))
    p.set_defaults(func=mattr_forward)

    p = sub.add_parser("n8n-resync", help="Queue a fresh snapshot of a company's books for MATTR AI")
    p.add_argument("application_id", nargs="?")
    p.add_argument("--all-flagged", action="store_true")
    p.set_defaults(func=n8n_resync)

    p = sub.add_parser("n8n-deliver", help="Send what is waiting for MATTR AI now and list what is stuck")
    p.set_defaults(func=n8n_deliver)

    p = sub.add_parser("n8n-reconcile", help="Backend figures to compare with bridge_reconcile_all() in n8n")
    p.add_argument("application_id", nargs="?")
    p.add_argument("--compare", help="JSON saved from SELECT bridge_reconcile_all() on the n8n database")
    p.set_defaults(func=n8n_reconcile)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
