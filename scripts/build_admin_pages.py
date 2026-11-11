#!/usr/bin/env python3
"""
Build the admin half of the GitHub Pages demo.

The shop side is computed in the browser from docs/demo.json. The admin cockpit
has ~15 aggregate endpoints, so instead of re-implementing them in JavaScript
this calls the real handler functions and saves their JSON output. Whatever the
cockpit shows on GitHub Pages is byte-for-byte what the server returns.

    python3 scripts/build_admin_pages.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app import api_admin  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


class Ctx:
    """Stand-in for the request context: the handlers only read query params."""

    def __init__(self, **params):
        self.params = params
        self.query = params
        self.user = {"id": 0, "name": "Demo Admin", "email": "admin@rebuy.ug", "role": "admin",
                     "shop_id": None}
        self.shop_id = None

    def qi(self, key, default=None):
        try:
            return int(self.query.get(key, default))
        except (TypeError, ValueError):
            return default

    def qs(self, key, default=""):
        return str(self.query.get(key, default) or default)

    def q(self, key, default=""):
        """String query param (the handlers use this for filters)."""
        val = self.query.get(key, default)
        return str(val) if val not in (None, "") else default

    def b(self, key, default=None):
        return self.query.get(key, default)

    def bi(self, key, default=0):
        return self.qi(key, default)


def _plain(obj):
    """sqlite3.Row / dict / list -> JSON-safe plain structures."""
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "keys"):
        return {k: _plain(obj[k]) for k in obj.keys()}
    return obj


def call(fn, **params):
    try:
        out = _plain(fn(Ctx(**params)))
        return out if isinstance(out, (dict, list)) else {"data": out}
    except Exception as e:  # noqa: BLE001
        print(f"  ! {fn.__name__}: {type(e).__name__}: {e}")
        return {"error": f"{type(e).__name__}: {e}"}


def main() -> int:
    db.init_schema()
    data = {
        "generated_at": db.now_iso(),
        "today": db.today(),
        "mode": "github-pages-demo",
        "overview": call(api_admin.overview, days=30),
        "shops": call(api_admin.admin_shops, per=100, page=1),
        "shop_detail_1": call(api_admin.admin_shop_detail, sid=1, days=30),
        "messaging": call(api_admin.admin_messaging, days=30),
        "retention": call(api_admin.admin_retention, days=30),
        "queue": call(api_admin.admin_queue),
        "capacity": call(api_admin.admin_capacity),
        "credentials": call(api_admin.admin_credentials),
        "revenue": call(api_admin.admin_revenue),
        "audit": call(api_admin.admin_audit, limit=200),
    }
    path = os.path.join(OUT, "admin.json")
    with open(path, "w") as f:
        json.dump(data, f, separators=(",", ":"), default=str)
    size = os.path.getsize(path)
    k = (data["overview"] or {}).get("kpis", {})
    print(f"wrote {path} ({size / 1024:.0f} KB)")
    print(f"  shops      {k.get('shops_total')} ({k.get('shops_active')} active)")
    print(f"  customers  {k.get('customers'):,}" if k.get("customers") else "  customers  ?")
    print(f"  MRR        {k.get('mrr', 0):,.0f}")
    print(f"  recovered  {k.get('revenue_recovered', 0):,.0f}")
    failed = [name for name, v in data.items() if isinstance(v, dict) and "error" in v]
    if failed:
        print("  endpoints that failed:", ", ".join(failed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
