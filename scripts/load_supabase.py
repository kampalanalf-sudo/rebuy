#!/usr/bin/env python3
"""
Load the Rebuy schema + demo fleet into Supabase (or any Postgres).

    REBUY_DATABASE_URL=postgresql://... python3 scripts/load_supabase.py [--shops 120] [--wipe]

Reads the connection string from REBUY_DATABASE_URL / DATABASE_URL (or --url).
Creates every table/index/helper function, seeds the demo fleet, then repairs
the BIGSERIAL sequences so new rows do not collide with seeded ids.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, seed  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="")
    ap.add_argument("--shops", type=int, default=int(os.environ.get("REBUY_SEED_SHOPS", "120")))
    ap.add_argument("--wipe", action="store_true", help="truncate existing tables first")
    args = ap.parse_args()

    if args.url:
        os.environ["REBUY_DATABASE_URL"] = args.url
        db.DATABASE_URL = args.url
        db.IS_PG = True

    if not db.IS_PG:
        print("No REBUY_DATABASE_URL set — refusing to run against local SQLite.")
        return 2

    t0 = time.time()
    print(f"→ schema on {db.DATABASE_URL.split('@')[-1]}")
    db.init_schema()

    if args.wipe:
        print("→ truncating existing tables")
        seed._clear_all()
        seed.engine.seed_default_templates()

    print(f"→ seeding {args.shops} shops")
    stats = seed.generate_demo_fleet(shops=args.shops, reset=args.wipe)
    db.fix_sequences()

    print(f"→ done in {time.time() - t0:.1f}s: {stats}")
    print("  shops      ", db.scalar("SELECT COUNT(*) FROM shops", (), 0))
    print("  customers  ", db.scalar("SELECT COUNT(*) FROM customers", (), 0))
    print("  sales      ", db.scalar("SELECT COUNT(*) FROM sales", (), 0))
    print("  reminders  ", db.scalar("SELECT COUNT(*) FROM reminders", (), 0))
    print("  messages   ", db.scalar("SELECT COUNT(*) FROM messages", (), 0))
    print("  db size MB ", round(db.size_bytes() / 1024 / 1024, 2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
