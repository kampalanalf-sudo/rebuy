#!/usr/bin/env python3
"""
Provision a production Rebuy database.

    REBUY_DATABASE_URL=postgresql://... python3 scripts/provision.py --check
    REBUY_DATABASE_URL=postgresql://... python3 scripts/provision.py --apply --wipe-demo

Does three things, in order, and prints exactly what it did:

  1. schema   — create anything missing, add columns/tables a previous release
                did not have (see db._migrate_postgres)
  2. wipe     — optional. Deletes every shop, customer, sale, message and user,
                including demo accounts whose passwords were published in the
                repo. Templates and platform provider settings are kept.
  3. verify   — prints row counts and the remaining accounts

There is no demo seeding here. `--wipe-demo` is the only destructive flag and it
requires the environment variable REBUY_WIPE_CONFIRM=yes as a second key, so it
cannot fire from a stray shell history.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, seed  # noqa: E402

# Order matters for FK constraints. platform_meta and templates survive on
# purpose: they hold provider config and the default message copy.
WIPE_ORDER = (
    "messages", "reminders", "loyalty_events", "coupons", "campaigns", "receipts",
    "import_jobs", "sale_items", "sales", "products", "customers", "credentials",
    "audit_log", "webhook_events", "sessions", "password_resets", "invites",
    "users", "shops",
)

VERIFY = [
    ("shops", "SELECT COUNT(*) FROM shops"),
    ("users", "SELECT COUNT(*) FROM users"),
    ("customers", "SELECT COUNT(*) FROM customers"),
    ("sales", "SELECT COUNT(*) FROM sales"),
    ("messages", "SELECT COUNT(*) FROM messages"),
    ("templates", "SELECT COUNT(*) FROM templates"),
]


def _connect(url: str) -> None:
    if url:
        os.environ["REBUY_DATABASE_URL"] = url
        db.DATABASE_URL = url
        db.IS_PG = True
    if not db.IS_PG:
        print("Refusing to run: set REBUY_DATABASE_URL (this script is Postgres-only).")
        sys.exit(2)


def _report() -> None:
    print("\n  current state")
    for label, sql in VERIFY:
        try:
            print(f"    {label:<12} {db.scalar(sql, (), 0):>8,}")
        except Exception as e:  # noqa: BLE001
            print(f"    {label:<12} {'(missing)':>8}  {str(e)[:60]}")


def _accounts() -> None:
    try:
        rows = db.q("SELECT id, email, role, shop_id FROM users ORDER BY id LIMIT 40")
    except Exception:  # noqa: BLE001
        return
    if not rows:
        print("\n  accounts: none — the first Google sign-in becomes the admin")
        return
    print(f"\n  accounts ({len(rows)} shown)")
    for r in rows:
        print(f"    {r['id']:>4}  {r['email']:<40} {r['role']:<8} shop={r['shop_id']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--apply", action="store_true", help="actually run (default is a dry run)")
    ap.add_argument("--wipe-demo", action="store_true", help="delete all business data + demo accounts")
    args = ap.parse_args()

    _connect(args.url)
    host = db.DATABASE_URL.split("@")[-1]
    print(f"→ target {host}")
    print(f"→ mode   {'APPLY' if args.apply else 'DRY RUN (add --apply)'}")

    _report()

    if not args.apply:
        if args.wipe_demo:
            print("\n  would delete:", ", ".join(WIPE_ORDER))
        print("\n  dry run — nothing changed")
        return 0

    print("\n→ 1/3 schema")
    db.init_schema()
    print("   ok")

    if args.wipe_demo:
        if os.environ.get("REBUY_WIPE_CONFIRM") != "yes":
            print("\nRefusing to wipe: set REBUY_WIPE_CONFIRM=yes as well as --wipe-demo.")
            return 3
        print("\n→ 2/3 wipe")
        if db.IS_PG:
            existing = {r["tablename"] for r in
                        db.q("SELECT tablename FROM pg_tables WHERE schemaname='public'")}
            targets = [t for t in WIPE_ORDER if t in existing]
            db.x("TRUNCATE " + ", ".join(targets) + " RESTART IDENTITY CASCADE")
            print(f"   truncated {len(targets)} tables")
        else:
            for t in WIPE_ORDER:
                try:
                    db.x(f'DELETE FROM "{t}"')
                except Exception:  # noqa: BLE001
                    pass
        seed.engine.seed_default_templates()
        db.meta_set("seeded", "0")
        print("   templates restored; demo seed flag cleared")
    else:
        print("\n→ 2/3 wipe skipped")

    print("\n→ 3/3 verify")
    _report()
    _accounts()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
