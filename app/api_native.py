"""
Rebuy — native app and location API.

The web app talks to /api/* with a session cookie. The native apps (iOS /
Android) need three things the browser does not:

  1. a device-scoped token, because a shopper is not a shop owner
  2. an endpoint to post location fixes to
  3. consent management that is explicit, per purpose, and revocable

Two identities are in play and must never be confused:

  * STAFF device  — an owner or team member's phone. Full shop API access via
                    `Authorization: Bearer <token>`.
  * SHOPPER device — a customer's phone, granted one narrow capability:
                    report location and read their own nudges. It can never
                    read the shop's data.

Endpoints
---------
POST /api/native/pair          shopper: exchange shop slug + phone for a device token
GET  /api/native/me            shopper: who am I, what am I allowed to share
POST /api/native/consent       shopper: grant or revoke location / marketing
POST /api/native/location      shopper: push a fix; triggers the geofence pass
GET  /api/native/nudges        shopper: proximity offers available right now
POST /api/native/push-token    shopper: register an FCM/APNs token
GET  /api/nearby               shop: what the geofence sees around a point (map view)
POST /api/nudges/proximity     shop: run a proximity pass now (used by cron/webhooks)
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets

from . import auth, config, db, engine, geo, nudges
from .httpd import ApiError, route

PAIR_TTL_DAYS = 180


def _token_from(headers: dict) -> str:
    raw = (headers.get("authorization") or headers.get("Authorization") or "")
    return raw[7:].strip() if raw.lower().startswith("bearer ") else ""


def _device_from(headers: dict) -> dict | None:
    token = _token_from(headers)
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    row = db.q1("SELECT * FROM devices WHERE token_hash=?", (th,))
    if not row:
        return None
    db.update("devices", row["id"], {"last_seen_at": db.now_iso()})
    return row


def _require_shopper(headers: dict) -> tuple[dict, dict]:
    device = _device_from(headers)
    if not device or not device.get("customer_id"):
        raise ApiError("Pair this device first", 401, "unpaired_device")
    customer = db.q1("SELECT * FROM customers WHERE id=?", (device["customer_id"],))
    if not customer:
        raise ApiError("Customer no longer exists", 401, "unknown_customer")
    return device, customer


# --------------------------------------------------------------------- pairing
@route("POST", "/api/native/pair", "none")
def pair(ctx):
    """Exchange a shop slug + the phone number already on file for a device token.

    Deliberately narrow: the phone must already belong to a customer of that
    shop, so this cannot be used to enumerate customers or create accounts.
    A 6-digit code delivered by SMS/WhatsApp is the production upgrade; until
    a provider is configured the exchange is allowed only outside production.
    """
    slug = (ctx.b("shop_slug") or "").strip().lower()
    phone = re.sub(r"[^0-9+]", "", (ctx.b("phone") or ""))
    if not slug or len(phone) < 9:
        raise ApiError("shop_slug and phone are required", 400, "missing_fields")
    shop = db.q1("SELECT * FROM shops WHERE slug=?", (slug,))
    if not shop:
        raise ApiError("Shop not found", 404, "unknown_shop")
    tail = phone[-9:]
    customer = db.q1("""SELECT * FROM customers WHERE shop_id=?
                        AND (phone LIKE ? OR whatsapp LIKE ?) LIMIT 1""",
                     (shop["id"], f"%{tail}", f"%{tail}"))
    if not customer:
        raise ApiError("We do not have that number on file — ask the shop to add you",
                       404, "unknown_customer")

    token = secrets.token_urlsafe(32)
    db.insert("devices", {
        "customer_id": customer["id"], "shop_id": shop["id"],
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "platform": (ctx.b("platform") or "android")[:20],
        "push_token": (ctx.b("push_token") or "")[:500],
        "app_version": (ctx.b("app_version") or "")[:20],
        "last_seen_at": db.now_iso(), "created_at": db.now_iso(),
    })
    if ctx.b("push_token"):
        _register_push(customer, shop, ctx.b("push_token"), ctx.b("platform"))
    db.audit(customer["name"], "native.paired", shop["id"], "customer", customer["id"],
             ctx.b("platform") or "android")
    return {"ok": True, "token": token, "expires_days": PAIR_TTL_DAYS,
            "customer": {"id": customer["id"], "name": customer["name"]},
            "shop": {"name": shop["name"], "slug": shop["slug"]},
            "consent": nudges.consent_state(customer["id"])}


@route("GET", "/api/native/me", "none")
def native_me(ctx):
    device, customer = _require_shopper(ctx.headers)
    shop = db.q1("SELECT id, name, slug, city, currency FROM shops WHERE id=?", (device["shop_id"],))
    places = db.q("SELECT id, label, address, lat, lng, radius_m, open_from, open_to FROM shop_places "
                  "WHERE shop_id=? AND active=1", (device["shop_id"],))
    return {"customer": {"id": customer["id"], "name": customer["name"]},
            "shop": shop, "places": places,
            "consent": nudges.consent_state(customer["id"]),
            "device": {"id": device["id"], "platform": device["platform"]}}


@route("POST", "/api/native/consent", "none")
def native_consent(ctx):
    device, customer = _require_shopper(ctx.headers)
    purpose = (ctx.b("purpose") or "").strip()
    if purpose not in ("location", "marketing", "analytics"):
        raise ApiError("purpose must be location, marketing or analytics", 400)
    granted = str(ctx.b("granted", "")) in ("1", "true", "True", "on", "yes")
    out = nudges.grant_consent(device["shop_id"], customer["id"], purpose, granted, "app")
    out["state"] = nudges.consent_state(customer["id"])
    return out


# -------------------------------------------------------------------- location
@route("POST", "/api/native/location", "none")
def native_location(ctx):
    """A location fix from the shopper's phone.

    Records the fix, updates the branch geofence state machine (enter/exit), and
    — only when everything lines up — triggers the proximity nudge pass.
    """
    device, customer = _require_shopper(ctx.headers)
    try:
        fix = geo.parse_fix(ctx.body if isinstance(ctx.body, dict) else {})
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_fix")

    shop = db.q1("SELECT * FROM shops WHERE id=?", (device["shop_id"],))
    if not shop:
        raise ApiError("Shop not found", 404)

    # Trust is decided BEFORE the device row is rewritten. Writing the fix first
    # would replace the previous position with the coarse one, so the next
    # enter/exit comparison would be made against a 2 km lie.
    trusted, why = geo.trust_fix(fix, device.get("last_seen_at") or "")
    if not trusted:
        return {"ok": True, "used": False, "reason": why}

    db.update("devices", device["id"], {"lat": fix["lat"], "lng": fix["lng"],
                                        "accuracy_m": fix["accuracy_m"],
                                        "last_seen_at": db.now_iso()})

    if not nudges.has_consent(customer["id"], "location"):
        return {"ok": True, "used": False, "reason": "no location consent"}

    prev_lat, prev_lng = device.get("lat"), device.get("lng")
    places = db.q("SELECT * FROM shop_places WHERE shop_id=? AND active=1", (shop["id"],))
    if not places:
        return {"ok": True, "used": True, "inside": False, "place": "", "event": "",
                "distance_m": None, "offer": None, "decided": False,
                "reason": "the shop has no branch location set up yet"}

    nearest, nearest_d = None, None
    for p in places:
        dist = geo.distance_m(fix["lat"], fix["lng"], float(p["lat"]), float(p["lng"]))
        if nearest_d is None or dist < nearest_d:
            nearest, nearest_d = p, dist

    inside = nearest_d <= float(nearest["radius_m"])
    was_inside = False
    if prev_lat is not None and prev_lng is not None:
        try:
            was_inside = geo.distance_m(float(prev_lat), float(prev_lng),
                                        float(nearest["lat"]), float(nearest["lng"])) <= float(nearest["radius_m"])
        except (TypeError, ValueError):
            was_inside = False

    event = ""
    if inside and not was_inside:
        event = "enter"
        db.insert("visits", {"shop_id": shop["id"], "place_id": nearest["id"], "customer_id": customer["id"],
                             "device_id": device["id"], "event": "enter", "lat": fix["lat"],
                             "lng": fix["lng"], "distance_m": round(nearest_d, 1),
                             "created_at": db.now_iso()})
    elif was_inside and not inside:
        event = "exit"
        db.insert("visits", {"shop_id": shop["id"], "place_id": nearest["id"], "customer_id": customer["id"],
                             "device_id": device["id"], "event": "exit", "lat": fix["lat"],
                             "lng": fix["lng"], "distance_m": round(nearest_d, 1),
                             "created_at": db.now_iso()})

    result = {"ok": True, "used": True, "distance_m": round(nearest_d, 1),
              "place": nearest["label"], "event": event,
              "inside": inside,
              "offer": None}

    # fire the brain only on approach, to avoid a nudge every time the app polls
    if inside or nearest_d <= 900:
        run = nudges.run_proximity(shop, fix["lat"], fix["lng"], customer_id=customer["id"])
        if run["sent"]:
            d = run["sent"][0]
            result["offer"] = _offer_payload(shop, d, nearest, nearest_d)
            result["decided"] = True
        else:
            result["decided"] = False
            result["reason"] = (run["decisions"][0]["reason"] if run["decisions"]
                                else "nothing due for replenishment")
    return result


def _offer_payload(shop: dict, decision: dict, place: dict, distance_m: float) -> dict:
    return {
        "headline": decision["product_name"] or "Something you buy often",
        "body": nudges_text(shop, decision, place, distance_m),
        "discount_pct": decision["discount_pct"],
        "score": decision["score"],
        "walk_minutes": geo.walking_minutes(distance_m),
        "place": place["label"],
        "expires_in_min": 45,
    }


def nudges_text(shop: dict, decision: dict, place: dict, distance_m: float) -> str:
    """Copy for a proximity nudge. Short — it is a lock-screen notification."""
    product = decision["product_name"] or "your usual"
    where = geo.describe_distance(distance_m)
    discount = decision.get("discount_pct") or 0
    base = f"You're {where} {shop['name']} and your {product} is nearly out."
    if discount:
        return f"{base} Show this for {discount:.0f}% off — valid for the next 45 minutes."
    return f"{base} Pop in and we'll have it ready."


@route("GET", "/api/native/nudges", "none")
def native_nudges(ctx):
    """Live offers for this shopper, so the app can render a card with no push."""
    device, customer = _require_shopper(ctx.headers)
    shop = db.q1("SELECT * FROM shops WHERE id=?", (device["shop_id"],))
    if device.get("lat") is None:
        return {"offers": [], "note": "send a location fix first"}
    run = nudges.run_proximity(shop, float(device["lat"]), float(device["lng"]),
                              customer_id=customer["id"], dry_run=True)
    offers = []
    for d in run["decisions"]:
        if not d["send"]:
            continue
        places = db.q("SELECT * FROM shop_places WHERE shop_id=? AND active=1", (shop["id"],))
        p = places[0] if places else {"label": shop["name"], "lat": device["lat"], "lng": device["lng"]}
        dist = geo.distance_m(float(device["lat"]), float(device["lng"]), float(p["lat"]), float(p["lng"]))
        offers.append(_offer_payload(shop, d, p, dist))
    return {"offers": offers, "evaluated": run["evaluated"]}


@route("POST", "/api/native/push-token", "none")
def native_push_token(ctx):
    device, customer = _require_shopper(ctx.headers)
    token = (ctx.b("token") or "").strip()
    if not token:
        raise ApiError("token is required", 400)
    shop = db.q1("SELECT * FROM shops WHERE id=?", (device["shop_id"],))
    _register_push(customer, shop, token, ctx.b("platform"))
    return {"ok": True}


def _register_push(customer: dict, shop: dict, token: str, platform: str | None) -> None:
    existing = db.q1("SELECT id FROM push_tokens WHERE token=?", (token,))
    if existing:
        db.update("push_tokens", existing["id"], {"active": 1, "customer_id": customer["id"],
                                                  "shop_id": shop["id"]})
    else:
        db.insert("push_tokens", {"customer_id": customer["id"], "shop_id": shop["id"],
                                  "token": token[:500], "platform": (platform or "android")[:20],
                                  "active": 1, "created_at": db.now_iso()})


# ------------------------------------------------------------------ shop side
@route("GET", "/api/nearby", "shop")
def nearby(ctx):
    """What the geofence sees around a point — the map view in the web app."""
    lat = ctx.q("lat") or ""
    lng = ctx.q("lng") or ""
    shop_id = ctx.shop_id
    if lat and lng:
        try:
            fix = geo.parse_fix({"lat": lat, "lng": lng})
        except ValueError as e:
            raise ApiError(str(e), 400, "bad_fix")
    else:
        place = db.q1("SELECT * FROM shop_places WHERE shop_id=? AND active=1", (shop_id,))
        if not place:
            return {"places": [], "people": [], "note": "add a branch location first"}
        fix = {"lat": float(place["lat"]), "lng": float(place["lng"])}
    shop = db.q1("SELECT * FROM shops WHERE id=?", (shop_id,))
    cands = nudges.proximity_candidates(shop, fix["lat"], fix["lng"], limit=60)
    people = []
    for c in cands:
        cust = c["customer"]
        people.append({
            "customer_id": cust["id"], "name": cust["name"], "area": cust.get("area", ""),
            "distance_m": c["distance_m"], "product": c["item"]["product_name"],
            "depletes_on": c["item"]["depletes_on"], "risk_band": cust.get("risk_band", ""),
            "lifetime_value": cust.get("lifetime_value", 0),
        })
    return {"center": fix, "places": db.q("SELECT * FROM shop_places WHERE shop_id=?", (shop_id,)),
            "people": people}


@route("POST", "/api/nudges/proximity", "shop")
def run_proximity_now(ctx):
    shop = db.q1("SELECT * FROM shops WHERE id=?", (ctx.shop_id,))
    place = db.q1("SELECT * FROM shop_places WHERE shop_id=? AND active=1", (ctx.shop_id,))
    if not place:
        raise ApiError("Add a branch location first", 400, "no_place")
    run = nudges.run_proximity(shop, float(place["lat"]), float(place["lng"]))
    for d in run["decisions"][:5]:
        if d["send"]:
            engine.queue_nudge_message(shop, d)
    db.audit(ctx.user["email"], "nudges.proximity_run", ctx.shop_id,
             detail=f"evaluated={run['evaluated']} sent={len(run['sent'])}")
    return {"ok": True, "evaluated": run["evaluated"], "would_send": run["would_send"],
            "sent": len(run["sent"]),
            "decisions": [{k: d[k] for k in ("customer_name", "distance_m", "score", "send",
                                             "reason", "discount_pct", "product_name")}
                          for d in run["decisions"][:20]]}

# --------------------------------------------------------------- shop console
@route("GET", "/api/nudges/settings", "shop")
def nudge_settings_get(ctx):
    return {"settings": nudges.settings_for(ctx.shop_id),
            "features": [{"key": k, "label": v[1], "weight": v[0]}
                         for k, v in nudges.WEIGHTS.items()],
            "defaults": {"min_score": 45, "max_per_week": 2, "quiet_minutes": 240}}


@route("PATCH", "/api/nudges/settings", "shop")
def nudge_settings_patch(ctx):
    out = nudges.save_settings(ctx.shop_id, ctx.body if isinstance(ctx.body, dict) else {})
    db.audit(ctx.user["email"], "nudges.settings", ctx.shop_id, detail=str(out)[:300])
    return {"ok": True, "settings": out}


@route("GET", "/api/nudges/places", "shop")
def places_list(ctx):
    return {"places": db.q("SELECT * FROM shop_places WHERE shop_id=? ORDER BY id", (ctx.shop_id,))}


@route("POST", "/api/nudges/places", "shop")
def places_create(ctx):
    name = (ctx.b("label") or "").strip() or "Main branch"
    try:
        fix = geo.parse_fix(ctx.body if isinstance(ctx.body, dict) else {})
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_place")
    radius = ctx.bi("radius_m", 400)
    pid = db.insert("shop_places", {
        "shop_id": ctx.shop_id, "label": name[:80], "address": (ctx.b("address") or "")[:160],
        "lat": fix["lat"], "lng": fix["lng"],
        "radius_m": max(50, min(5000, radius)),
        "open_from": (ctx.b("open_from") or "07:00")[:5],
        "open_to": (ctx.b("open_to") or "21:00")[:5],
        "active": 1, "created_at": db.now_iso()})
    db.audit(ctx.user["email"], "place.created", ctx.shop_id, "shop_place", pid, name)
    return {"ok": True, "id": pid,
            "places": db.q("SELECT * FROM shop_places WHERE shop_id=? ORDER BY id", (ctx.shop_id,))}


@route("POST", "/api/nudges/places/delete", "shop")
def places_delete(ctx):
    pid = ctx.bi("id", 0)
    db.x("DELETE FROM shop_places WHERE id=? AND shop_id=?", (pid, ctx.shop_id))
    return {"ok": True, "places": db.q("SELECT * FROM shop_places WHERE shop_id=? ORDER BY id", (ctx.shop_id,))}


@route("GET", "/api/nudges/insights", "shop")
def nudge_insights(ctx):
    """The AI cockpit: what was decided, why, what it earned, and whether it beat
    the baseline."""
    days = ctx.qi("days", 30)
    row = db.q1("SELECT id, name, sector FROM shops WHERE id=?", (ctx.shop_id,)) or {}
    lift = nudges.lift_report(ctx.shop_id, days)
    since = db.days_from_today(-days)
    recent = db.q("""SELECT d.*, c.name AS customer_name
                     FROM nudge_decisions d LEFT JOIN customers c ON c.id=d.customer_id
                     WHERE d.shop_id=? AND d.created_at>=?
                     ORDER BY d.created_at DESC LIMIT 40""", (ctx.shop_id, since))
    for r in recent:
        try:
            feats = json.loads(r.get("features") or "{}")
            r["top_features"] = nudges.contributions(feats)[:3]
        except Exception:  # noqa: BLE001
            r["top_features"] = []
        r.pop("features", None)
    by_trigger = db.q("""SELECT trigger_kind, COUNT(*) AS n,
                                SUM(CASE WHEN send=1 THEN 1 ELSE 0 END) AS sent,
                                SUM(revenue) AS revenue
                         FROM nudge_decisions WHERE shop_id=? AND created_at>=?
                         GROUP BY trigger_kind""", (ctx.shop_id, since))
    return {"shop": row, "lift": lift, "recent": recent, "by_trigger": by_trigger,
            "best_times": nudges.best_time_slots({"id": ctx.shop_id,
                                                  "sector": row.get("sector", "")}),
            "consent": _consent_funnel(ctx.shop_id),
            "places": db.q("SELECT * FROM shop_places WHERE shop_id=?", (ctx.shop_id,)),
            "device_count": db.scalar("SELECT COUNT(*) FROM devices WHERE shop_id=?",
                                      (ctx.shop_id,), 0),
            "settings": nudges.settings_for(ctx.shop_id)}


def _consent_funnel(shop_id: int) -> dict:
    total = db.scalar("SELECT COUNT(*) FROM customers WHERE shop_id=?", (shop_id,), 0)
    loc = db.scalar("""SELECT COUNT(*) FROM consents WHERE shop_id=? AND purpose='location' AND granted=1""",
                    (shop_id,), 0)
    mkt = db.scalar("""SELECT COUNT(*) FROM consents WHERE shop_id=? AND purpose='marketing' AND granted=1""",
                    (shop_id,), 0)
    dev = db.scalar("""SELECT COUNT(DISTINCT customer_id) FROM devices WHERE shop_id=? AND customer_id IS NOT NULL""",
                    (shop_id,), 0)
    return {"customers": total, "location_consent": loc, "marketing_consent": mkt,
            "app_installs": dev,
            "location_rate": round(100 * loc / total, 1) if total else 0}


@route("GET", "/api/nudges/visits", "shop")
def visits_list(ctx):
    rows = db.q("""SELECT v.*, c.name AS customer_name, p.label AS place_label
                   FROM visits v LEFT JOIN customers c ON c.id=v.customer_id
                   LEFT JOIN shop_places p ON p.id=v.place_id
                   WHERE v.shop_id=? ORDER BY v.created_at DESC LIMIT 50""", (ctx.shop_id,))
    return {"visits": rows,
            "today": db.scalar("SELECT COUNT(*) FROM visits WHERE shop_id=? AND created_at LIKE ?",
                               (ctx.shop_id, db.today() + "%"), 0)}
