"""
Rebuy — the nudge brain.

One place decides *whether* to message a customer, *when*, *about what*, and
*with how much* of a discount. Everything is deterministic and explainable: each
decision writes a row to `nudge_decisions` with the score, the feature vector it
was computed from, and a human-readable reason. Nothing about it needs a model
endpoint to run, and every message it produces can be justified to the shop
owner after the fact.

The scoring model is a small logistic regression over features that are known
to matter for replenishment retail:

    urgency         how far past the predicted depletion date we are
    affinity        how often this customer buys this product
    margin_room     how much discount the item can absorb
    proximity       are they here, right now (only for proximity triggers)
    history         do they act on messages (reply/conversion rate)
    fatigue         how many nudges we already sent them recently
    timing          is this inside their usual shopping window

Weights are hand-set from domain reasoning, not fitted to data — with no
production history yet, a fitted model would just be noise with a confidence
interval. The structure means you *can* fit it later: features.goes_to_vector()
produces the design matrix, and every historical decision stores its own vector.

Consent is checked before scoring. Location purpose is separate from marketing
purpose, and proximity nudges require both.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta

from . import db, geo

# --------------------------------------------------------------------- model
# name: (weight, description shown in the cockpit)
WEIGHTS = {
    "urgency":    (2.60, "Product is at or past its predicted run-out date"),
    "affinity":   (1.10, "This customer buys this product often"),
    "history":    (0.95, "They usually reply or buy after a message"),
    "fatigue":    (-2.80, "They have been nudged recently"),
    "proximity":  (1.45, "They are near the shop right now"),
    "timing":     (0.60, "It is inside their usual shopping hours"),
    "margin_room": (0.40, "There is margin to discount if needed"),
    "basket":     (0.35, "Their basket value is above the shop average"),
}
# Calibrated so that, with the default threshold of 45:
#   overdue + nearby + habitual      -> ~85-95  (send)
#   due soon + nearby                -> ~65-75  (send)
#   nearby but not due for a while   -> ~35-45  (borderline, usually held)
#   recently nudged, anything        -> suppressed by the fatigue term
#   overdue but far away             -> ~55-65  (send on the normal time trigger)
BIAS = -3.45

FEATURE_LABELS = {k: v[1] for k, v in WEIGHTS.items()}

DEFAULT_SEND_HOURS = {  # sector -> (start, end) in local time
    "Supermarket": (17, 20),
    "Salon & Spa": (10, 18),
    "LPG & Water": (7, 11),
    "Auto Garage": (8, 17),
    "Pharmacy": (8, 20),
    "Hardware": (8, 17),
    "Bakery": (7, 11),
    "Restaurant": (11, 14),
}


def _squash(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-max(-40.0, min(40.0, x))))


def score(features: dict) -> float:
    """Probability this nudge is worth sending, 0-100."""
    z = BIAS + sum(w * float(features.get(k) or 0.0) for k, (w, _) in WEIGHTS.items())
    return round(100 * _squash(z), 1)


def contributions(features: dict) -> list[dict]:
    """Per-feature contribution to the score — what makes the cockpit explainable."""
    out = []
    for k, (w, label) in WEIGHTS.items():
        v = float(features.get(k) or 0.0)
        out.append({"feature": k, "label": label, "value": round(v, 3),
                    "weight": w, "points": round(w * v, 2)})
    out.sort(key=lambda r: -abs(r["points"]))
    return out


# ------------------------------------------------------------------ settings
DEFAULTS = {
    "enabled": 1, "proximity_enabled": 1, "ai_scoring": 1, "min_score": 45.0,
    "max_per_week": 2, "quiet_minutes": 240, "max_discount_pct": 15.0,
    "discount_aggressiveness": 0.5, "best_time_enabled": 1,
}


def settings_for(shop_id: int) -> dict:
    row = db.q1("SELECT * FROM nudge_settings WHERE shop_id=?", (shop_id,))
    if not row:
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    out.update({k: row[k] for k in DEFAULTS if k in row})
    return out


def save_settings(shop_id: int, values: dict) -> dict:
    current = settings_for(shop_id)
    allowed = set(DEFAULTS)
    merged = {**current}
    for k, v in (values or {}).items():
        if k not in allowed:
            continue
        if k in ("min_score", "max_discount_pct", "discount_aggressiveness"):
            try:
                merged[k] = max(0.0, min(100.0 if k == "min_score" else 1.0, float(v)))
            except (TypeError, ValueError):
                continue
        elif k in ("enabled", "proximity_enabled", "ai_scoring", "best_time_enabled"):
            merged[k] = 1 if str(v) in ("1", "true", "True", "on") else 0
        else:
            try:
                merged[k] = max(0, int(v))
            except (TypeError, ValueError):
                continue
    if db.q1("SELECT id FROM nudge_settings WHERE shop_id=?", (shop_id,)):
        db.update("nudge_settings", db.q1("SELECT id FROM nudge_settings WHERE shop_id=?",
                                          (shop_id,))["id"],
                  {**merged, "updated_at": db.now_iso()})
    else:
        db.insert("nudge_settings", {"shop_id": shop_id, **merged, "updated_at": db.now_iso()})
    return settings_for(shop_id)


# ------------------------------------------------------------------- consent
def has_consent(customer_id: int, purpose: str) -> bool:
    row = db.q1("SELECT granted FROM consents WHERE customer_id=? AND purpose=?",
                (customer_id, purpose))
    return bool(row and row["granted"])


def grant_consent(shop_id: int, customer_id: int, purpose: str, granted: bool,
                  source: str = "app") -> dict:
    now = db.now_iso()
    existing = db.q1("SELECT id FROM consents WHERE customer_id=? AND purpose=?",
                     (customer_id, purpose))
    values = {"granted": 1 if granted else 0, "source": source, "updated_at": now,
              "granted_at": now if granted else None, "revoked_at": None if granted else now}
    if existing:
        db.update("consents", existing["id"], values)
    else:
        db.insert("consents", {"shop_id": shop_id, "customer_id": customer_id,
                               "purpose": purpose, **values})
    db.audit(f"customer:{customer_id}", f"consent.{purpose}.{'granted' if granted else 'revoked'}",
             shop_id, "customer", customer_id, source)
    return {"purpose": purpose, "granted": bool(granted)}


def consent_state(customer_id: int) -> dict:
    rows = db.q("SELECT purpose, granted, updated_at FROM consents WHERE customer_id=?",
                (customer_id,))
    state = {p: False for p in ("location", "marketing", "analytics")}
    for r in rows:
        state[r["purpose"]] = bool(r["granted"])
    return state


# ------------------------------------------------------------------ features
def _customer_stats(shop_id: int, customer_id: int) -> dict:
    sent = db.scalar("""SELECT COUNT(*) FROM messages WHERE shop_id=? AND customer_id=? AND direction='out'""",
                     (shop_id, customer_id), 0)
    converted = db.scalar("""SELECT COUNT(*) FROM messages WHERE shop_id=? AND customer_id=? AND direction='out'
                             AND intent IS NOT NULL AND intent<>''""", (shop_id, customer_id), 0)
    replies = db.scalar("""SELECT COUNT(*) FROM messages WHERE shop_id=? AND customer_id=? AND direction='in'""",
                        (shop_id, customer_id), 0)
    return {"sent": sent, "converted": converted, "replies": replies}


def _product_affinity(customer_id: int, product_name: str) -> float:
    if not product_name:
        return 0.0
    n = db.scalar("""SELECT COUNT(*) FROM sale_items si JOIN sales s ON s.id=si.sale_id
                     WHERE s.customer_id=? AND si.product_name=?""", (customer_id, product_name), 0)
    return min(1.0, n / 4.0)          # 4+ repeat buys = fully habitual


def _recent_nudges(shop_id: int, customer_id: int, hours: int) -> int:
    cutoff = (db.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return db.scalar("""SELECT COUNT(*) FROM nudge_sends
                        WHERE shop_id=? AND customer_id=? AND created_at>=?""",
                     (shop_id, customer_id, cutoff), 0)


def _weekly_nudges(shop_id: int, customer_id: int) -> int:
    cutoff = (db.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    return db.scalar("""SELECT COUNT(*) FROM nudge_sends
                        WHERE shop_id=? AND customer_id=? AND created_at>=?""",
                     (shop_id, customer_id, cutoff), 0)


def build_features(shop: dict, customer: dict, item: dict | None, *,
                   distance_m: float | None = None, hour: int | None = None) -> dict:
    item = item or {}
    now = db.utcnow()
    today = now.strftime("%Y-%m-%d")

    # urgency: 0 at two weeks before run-out, 1 at the run-out date, >1 once overdue
    urgency = 0.0
    depletes = item.get("depletes_on") or ""
    if depletes:
        days_left = (datetime.strptime(depletes[:10], "%Y-%m-%d").date()
                     - datetime.strptime(today, "%Y-%m-%d").date()).days
        urgency = max(0.0, min(2.0, (14 - days_left) / 14.0))

    affinity = _product_affinity(customer["id"], item.get("product_name", ""))

    stats = _customer_stats(shop["id"], customer["id"])
    if stats["sent"] >= 3:
        history = min(1.0, (stats["replies"] + stats["converted"]) / stats["sent"])
    else:
        history = 0.45          # unknown: assume a mild positive prior

    recent = _recent_nudges(shop["id"], customer["id"], 72)
    week = _weekly_nudges(shop["id"], customer["id"])
    fatigue = min(1.0, recent / 2.0 + week / 4.0)

    proximity = 0.0
    if distance_m is not None:
        proximity = max(0.0, min(1.0, 1.0 - distance_m / 800.0))

    h = hour if hour is not None else now.hour
    start, end = DEFAULT_SEND_HOURS.get(shop.get("sector", ""), (9, 20))
    timing = 1.0 if start <= h <= end else 0.0

    ltv = float(customer.get("lifetime_value") or 0)
    basket = min(1.0, ltv / 250_000.0) if ltv else 0.0

    margin_room = 0.0
    if item.get("line_total") and item.get("qty"):
        # a cheap, frequently-bought line can absorb a percentage without hurting
        unit = float(item["line_total"]) / max(1.0, float(item["qty"]))
        margin_room = max(0.0, min(1.0, 1.0 - unit / 60_000.0))

    return {"urgency": round(urgency, 3), "affinity": round(affinity, 3),
            "history": round(history, 3), "fatigue": round(fatigue, 3),
            "proximity": round(proximity, 3), "timing": timing,
            "basket": round(basket, 3), "margin_room": round(margin_room, 3)}


# --------------------------------------------------------------- discounting
def suggest_discount(shop: dict, features: dict, score_value: float, settings: dict) -> float:
    """How much discount to offer.

    The logic: never discount a customer who is coming anyway, and never
    discount a message that is already highly likely to convert. Discount is
    spent where it changes behaviour — the hesitant middle — and it scales with
    how long the customer has been drifting.
    """
    if not settings.get("max_discount_pct"):
        return 0.0
    cap = float(settings["max_discount_pct"])
    aggressiveness = float(settings.get("discount_aggressiveness") or 0.5)
    urgency = float(features.get("urgency") or 0)
    proximity = float(features.get("proximity") or 0)

    if proximity > 0.55 and score_value > 70:
        return 0.0                      # they are outside; do not pay them to walk in
    if score_value > 82:
        return 0.0                      # already very likely; keep the margin
    # hesitancy peaks in the middle of the score range
    hesitancy = 1.0 - abs(score_value - 55.0) / 55.0
    drift = 0.0 if urgency < 0.7 else min(1.0, (urgency - 0.7) / 1.3)
    raw = cap * (0.35 * hesitancy + 0.45 * drift + 0.20 * aggressiveness)
    step = 5.0 if cap >= 10 else 2.5
    return float(max(0.0, min(cap, round(raw / step) * step)))


# --------------------------------------------------------------- best time
def best_send_hour(shop: dict, customer: dict) -> int:
    """The hour this shop's customers actually buy, learned from their own sales."""
    rows = db.q("""SELECT CAST(SUBSTR(s.sale_time,1,2) AS INTEGER) AS h, COUNT(*) AS n
                   FROM sales s
                   WHERE s.shop_id=? AND s.sale_time IS NOT NULL AND s.sale_time<>''
                   GROUP BY h ORDER BY n DESC""", (shop["id"],))
    own = db.q("""SELECT CAST(SUBSTR(s.sale_time,1,2) AS INTEGER) AS h, COUNT(*) AS n
                  FROM sales s WHERE s.shop_id=? AND s.customer_id=?
                  GROUP BY h ORDER BY n DESC""", (shop["id"], customer["id"]))
    if own and own[0]["n"] >= 2:
        return int(own[0]["h"])
    if rows:
        return int(rows[0]["h"])
    return DEFAULT_SEND_HOURS.get(shop.get("sector", ""), (10, 0))[0]


def in_quiet_window(shop: dict, when: datetime | None = None) -> bool:
    """Shop quiet hours are for marketing; a proximity nudge is requested by the
    customer walking in, but we still never buzz at 3am."""
    now = when or db.utcnow()
    if 22 <= now.hour or now.hour < 6:
        return True
    return False


# ------------------------------------------------------------------ decision
def decide(shop: dict, customer: dict, item: dict | None, *, trigger: str = "time",
           distance_m: float | None = None, hour: int | None = None,
           write: bool = True) -> dict:
    """Score one candidate nudge. Returns the decision dict (and logs it)."""
    settings = settings_for(shop["id"])
    features = build_features(shop, customer, item, distance_m=distance_m, hour=hour)
    value = score(features)
    consents = consent_state(customer["id"])

    blockers = []
    if not settings.get("enabled"):
        blockers.append("nudges are switched off for this shop")
    # Hard business gates, separate from the score. The model ranks candidates;
    # these decide whether a proximity nudge is appropriate at all, because a
    # high score should never override "they don't need this yet".
    if trigger == "proximity":
        if float(features.get("urgency") or 0) < 0.35:
            blockers.append("nothing due yet — product is not near its run-out date")
        if not (item or {}).get("product_name"):
            blockers.append("no recurring product on file for this customer")
    if not customer.get("opted_in"):
        blockers.append("customer opted out of marketing messages")
    elif not consents.get("marketing"):
        blockers.append("no marketing consent on file")
    if trigger == "proximity":
        if not settings.get("proximity_enabled"):
            blockers.append("proximity nudges are switched off")
        if not consents.get("location"):
            blockers.append("no location consent on file")
        if in_quiet_window(shop):
            blockers.append("inside the no-contact window (22:00-06:00)")
        if _recent_nudges(shop["id"], customer["id"], int(settings["quiet_minutes"])):
            blockers.append("nudged recently — inside the cooling-off window")
        if _weekly_nudges(shop["id"], customer["id"]) >= int(settings["max_per_week"]):
            blockers.append("weekly nudge cap reached")

    threshold = float(settings["min_score"]) if settings.get("ai_scoring") else 0.0
    passed = value >= threshold and not blockers
    discount = suggest_discount(shop, features, value, settings) if passed else 0.0
    expected = _expected_value(shop, customer, item, value, discount)

    top = contributions(features)[:3]
    if passed:
        reason = ", ".join(f"{c['label'].lower()} ({c['points']:+.1f})" for c in top if c["points"] > 0) \
                 or "routine replenishment timing"
        reason = f"score {value:.0f} ≥ {threshold:.0f}: {reason}"
    else:
        reason = "; ".join(blockers) if blockers else f"score {value:.0f} below threshold {threshold:.0f}"

    decision = {
        "shop_id": shop["id"], "customer_id": customer["id"], "trigger_kind": trigger,
        "product_name": (item or {}).get("product_name", ""),
        "score": value, "expected_value": expected, "send": 1 if passed else 0,
        "reason": reason[:400], "features": json.dumps(features),
        "discount_pct": discount, "channel": "push" if trigger == "proximity" else "whatsapp",
        "send_at": "", "outcome": "", "revenue": 0.0,
        "reminder_id": (item or {}).get("reminder_id"),
        "created_at": db.now_iso(),
    }
    if passed and settings.get("best_time_enabled") and trigger != "proximity":
        decision["send_at"] = f"{best_send_hour(shop, customer):02d}:00"
    if write:
        decision["id"] = db.insert("nudge_decisions", decision)
    return decision


def _expected_value(shop: dict, customer: dict, item: dict | None, value: float,
                    discount: float) -> float:
    """Rough UGX value of sending: P(convert) x basket x (1 - discount)."""
    ltv = float(customer.get("lifetime_value") or 0)
    basket = float((item or {}).get("line_total") or 0) or (ltv / max(1, int(customer.get("visit_count") or 1)))
    basket = max(3000.0, basket)
    return round(value / 100.0 * basket * (1 - discount / 100.0), 0)


def record_send(shop_id: int, customer_id: int, kind: str) -> None:
    db.insert("nudge_sends", {"shop_id": shop_id, "customer_id": customer_id, "kind": kind,
                              "created_at": db.now_iso()})


# ------------------------------------------------------------ proximity flow
def proximity_candidates(shop: dict, lat: float, lng: float, *, customer_id: int | None = None,
                         limit: int = 25) -> list[dict]:
    """Customers physically near the shop who have something due for replenishment.

    Runs a bounding-box pre-filter in SQL, then exact distance in Python. All
    geo math is done here rather than in the query so the same code path works
    on SQLite and Postgres.
    """
    lat0, lat1, lng0, lng1 = geo.bbox(lat, lng, 1500)
    # placeholders appear in SQL order: shop_id, lat0, lat1, lng0, lng1, [customer]
    params: list = [shop["id"], lat0, lat1, lng0, lng1]
    customer_clause = ""
    if customer_id:
        customer_clause = " AND c.id=?"
        params.append(customer_id)
    rows = db.q(f"""
        SELECT c.*,
               MAX(d.lat) AS dev_lat, MAX(d.lng) AS dev_lng,
               (SELECT si.product_name FROM sale_items si JOIN sales s ON s.id=si.sale_id
                WHERE s.customer_id=c.id AND si.depletes_on<>''
                ORDER BY si.depletes_on ASC LIMIT 1) AS next_product,
               (SELECT si.depletes_on FROM sale_items si JOIN sales s ON s.id=si.sale_id
                WHERE s.customer_id=c.id AND si.depletes_on<>''
                ORDER BY si.depletes_on ASC LIMIT 1) AS next_depletes,
               (SELECT MAX(s.sale_date) FROM sales s WHERE s.customer_id=c.id) AS last_purchase
        FROM customers c
        JOIN devices d ON d.customer_id = c.id
        WHERE c.shop_id=? AND c.opted_in=1
          AND d.lat BETWEEN ? AND ? AND d.lng BETWEEN ? AND ?
          {customer_clause}
        GROUP BY c.id
        LIMIT 200
    """, tuple(params))
    if not rows:
        return []

    places = db.q("SELECT * FROM shop_places WHERE shop_id=? AND active=1", (shop["id"],)) \
        or [{"id": None, "lat": lat, "lng": lng, "radius_m": 400, "label": shop.get("name", "")}]

    out = []
    for c in rows:
        # where this customer's own phone last reported, falling back to the
        # map centre only if the row somehow has no fix
        dlat = c.get("dev_lat")
        dlng = c.get("dev_lng")
        px = float(dlat) if dlat is not None else float(lat)
        py = float(dlng) if dlng is not None else float(lng)
        best = None
        for p in places:
            dist = geo.distance_m(px, py, float(p["lat"]), float(p["lng"]))
            if best is None or dist < best[0]:
                best = (dist, p)
        dist, place = best
        if dist > 1500:
            continue
        item = {"product_name": c.get("next_product") or "",
                "depletes_on": c.get("next_depletes") or "",
                "line_total": 0, "qty": 1}
        out.append({"customer": c, "distance_m": round(dist, 1), "place": place, "item": item})
        if len(out) >= limit:
            break
    return out


def run_proximity(shop: dict, lat: float, lng: float, *, customer_id: int | None = None,
                  dry_run: bool = False) -> dict:
    """The geofence pass: score everyone nearby and nudge the ones who qualify."""
    candidates = proximity_candidates(shop, lat, lng, customer_id=customer_id)
    decided, sent = [], []
    for cand in candidates:
        c, item = cand["customer"], cand["item"]
        if not item["product_name"]:
            continue
        decision = decide(shop, c, item, trigger="proximity", distance_m=cand["distance_m"],
                          write=not dry_run)
        decision["distance_m"] = cand["distance_m"]
        decision["customer_name"] = c.get("name", "")
        decision["place"] = (cand["place"] or {}).get("label", "")
        decided.append(decision)
        if decision["send"] and not dry_run:
            record_send(shop["id"], c["id"], "proximity")
            sent.append(decision)
    return {"evaluated": len(candidates), "decisions": decided, "sent": sent,
            "would_send": sum(1 for d in decided if d["send"]),
            "suppressed": sum(1 for d in decided if not d["send"])}


# -------------------------------------------------------------- attribution
def attribute_visit_sale(shop_id: int, customer_id: int, sale_id: int) -> int:
    """A purchase within 6h of a proximity nudge is credited to it."""
    cutoff = (db.utcnow() - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.q("""SELECT id, expected_value FROM nudge_decisions
                   WHERE shop_id=? AND customer_id=? AND trigger_kind='proximity'
                     AND send=1 AND created_at>=? AND outcome=''
                   ORDER BY created_at DESC LIMIT 1""", (shop_id, customer_id, cutoff))
    if not rows:
        return 0
    d = rows[0]
    db.update("nudge_decisions", d["id"], {
        "outcome": "converted", "revenue": float(d["expected_value"] or 0)})
    return 1


# ------------------------------------------------------------------ best time
def best_time_slots(shop: dict) -> dict:
    """Heatmap of the shop's own purchase hours — what the send-time model reads."""
    rows = db.q("""SELECT CAST(SUBSTR(sale_time,1,2) AS INTEGER) AS h, COUNT(*) AS n
                   FROM sales WHERE shop_id=? AND sale_time IS NOT NULL AND sale_time<>''
                   GROUP BY h ORDER BY h""", (shop["id"],))
    by_hour = {int(r["h"]): int(r["n"]) for r in rows if r["h"] is not None}
    peak = max(by_hour.values()) if by_hour else 1
    return {"by_hour": [{"hour": h, "count": by_hour.get(h, 0),
                         "share": round(by_hour.get(h, 0) / peak, 3)} for h in range(24)]}


# ------------------------------------------------------------------ insights
def lift_report(shop_id: int, days: int = 30) -> dict:
    """Compare outcomes of nudges the model sent vs the ones it suppressed.

    This is the honest scoreboard: if the AI is not beating the threshold, the
    report says so rather than claiming a win.
    """
    since = (db.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = db.q("""SELECT send, COUNT(*) AS n,
                          SUM(CASE WHEN outcome='converted' THEN 1 ELSE 0 END) AS converted,
                          SUM(revenue) AS revenue,
                          AVG(score) AS avg_score
                   FROM nudge_decisions WHERE shop_id=? AND created_at>=?
                   GROUP BY send""", (shop_id, since))
    out = {"sent": {"n": 0, "converted": 0, "revenue": 0, "rate": 0, "avg_score": 0},
           "suppressed": {"n": 0, "converted": 0, "revenue": 0, "rate": 0, "avg_score": 0}}
    for r in rows:
        key = "sent" if r["send"] else "suppressed"
        n = int(r["n"] or 0)
        out[key] = {"n": n, "converted": int(r["converted"] or 0),
                    "revenue": float(r["revenue"] or 0),
                    "rate": round(100 * int(r["converted"] or 0) / n, 1) if n else 0,
                    "avg_score": round(float(r["avg_score"] or 0), 1)}
    diff = out["sent"]["rate"] - out["suppressed"]["rate"]
    out["lift_points"] = round(diff, 1)
    # A rate difference on a handful of sends is noise. Wilson lower bounds keep
    # the verdict from congratulating itself on small samples.
    sent_lo, sent_hi = _wilson(out["sent"]["converted"], out["sent"]["n"])
    supp_lo, supp_hi = _wilson(out["suppressed"]["converted"], out["suppressed"]["n"])
    out["sent"]["ci"] = [round(100 * sent_lo, 1), round(100 * sent_hi, 1)]
    out["suppressed"]["ci"] = [round(100 * supp_lo, 1), round(100 * supp_hi, 1)]
    enough = out["sent"]["n"] >= 30 and out["suppressed"]["n"] >= 30
    if not enough:
        out["verdict"] = (f"too little data to call — {out['sent']['n']} sent, "
                          f"{out['suppressed']['n']} held back (need 30 of each)")
    elif supp_hi < sent_lo:
        out["verdict"] = "model is beating the baseline with confidence"
    elif supp_lo > sent_hi:
        out["verdict"] = "model is worse than holding everything back — review weights"
    else:
        out["verdict"] = "no measurable difference yet"
    out["comparable"] = enough
    return out


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion — honest error bars on small n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)
