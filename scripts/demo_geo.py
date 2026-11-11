#!/usr/bin/env python3
"""
Populate a development database with geofence demo data.

    REBUY_DB=./data/rebuy.db REBUY_SEED_DEMO=1 python3 scripts/demo_geo.py

Adds one Kampala branch to shop 1, plants a handful of app installs around it,
grants consent to most of them, runs the proximity pass, and marks one nudge as
converted so the lift report has something to show. Development only.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config, db, nudges  # noqa: E402

if config.IS_PROD:
    print("Refusing to run: this script fabricates demo data and REBUY_ENV=production.")
    sys.exit(2)

KLA_LAT, KLA_LNG = 0.3476, 32.5825   # Kampala CBD, near Ntinda


def main() -> int:
    db.init_schema()
    shop = db.q1("SELECT * FROM shops WHERE id=1")
    if not shop:
        print("No shop 1 — seed a demo fleet first (REBUY_SEED_DEMO=1 python3 serve.py).")
        return 1

    if not db.q1("SELECT id FROM shop_places WHERE shop_id=1"):
        db.insert("shop_places", {"shop_id": 1, "label": "Ntinda branch", "address": "Ntinda Road",
                                  "lat": KLA_LAT, "lng": KLA_LNG, "radius_m": 400,
                                  "open_from": "07:00", "open_to": "21:00",
                                  "created_at": db.now_iso()})
        print("added branch: Ntinda (400 m geofence)")

    # plant installs at a spread of distances
    offsets = [(0.0002, 0.0003, 12), (0.0010, 0.0010, 18), (0.0025, 0.0030, 25),
               (0.0060, 0.0050, 30), (0.0140, 0.0120, 45), (0.0300, 0.0250, 60)]
    customers = db.q("SELECT id, name FROM customers WHERE shop_id=1 ORDER BY id LIMIT 40")
    placed = 0
    for c, (dlat, dlng, acc) in zip(customers, offsets):
        if db.q1("SELECT id FROM devices WHERE customer_id=?", (c["id"],)):
            continue
        db.insert("devices", {"customer_id": c["id"], "shop_id": 1,
                              "token_hash": f"demo-{c['id']}", "platform": "android",
                              "lat": KLA_LAT + dlat, "lng": KLA_LNG + dlng, "accuracy_m": acc,
                              "last_seen_at": db.now_iso(), "created_at": db.now_iso()})
        nudges.grant_consent(1, c["id"], "location", True, "app")
        nudges.grant_consent(1, c["id"], "marketing", True, "app")
        placed += 1
    print(f"planted {placed} app installs with consent")

    run = nudges.run_proximity(shop, KLA_LAT, KLA_LNG)
    print(f"proximity pass: evaluated {run['evaluated']}, sent {len(run['sent'])}")

    row = db.q1("SELECT id, expected_value FROM nudge_decisions WHERE shop_id=1 AND send=1 LIMIT 1")
    if row:
        db.update("nudge_decisions", row["id"], {"outcome": "converted",
                                                 "revenue": row["expected_value"]})
        print("marked one nudge converted (so the lift report is not empty)")
    print("decisions logged:", db.scalar("SELECT COUNT(*) FROM nudge_decisions", (), 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
