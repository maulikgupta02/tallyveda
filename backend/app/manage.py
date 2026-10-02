"""Tenant/user provisioning CLI.

There's no admin web UI yet (see docs/agents/architecture.md) — onboarding a
second bank, or adding another login for an existing bank, is done from here:

    python -m app.manage create-tenant "Second Bank"
    python -m app.manage create-bank-user <tenant_id> ops@secondbank.in
    python -m app.manage create-platform-admin ops@platform.example

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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
