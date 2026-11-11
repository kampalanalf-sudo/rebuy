"""Platform admin API — the owner's cockpit across all shops."""
from __future__ import annotations

import json
import os
import re
import time

from . import auth, db, engine, imports, providers
from .httpd import ApiError, route

_SECRET_KEYS = ("token", "password", "secret", "auth_token")

PLANS = {"starter": {"price": 49000, "shops_customers": 500, "messages": 2000},
         "growth": {"price": 129000, "shops_customers": 3000, "messages": 15000},
         "scale": {"price": 299000, "shops_customers": 20000, "messages": 100000}}


@route("GET", "/api/admin/overview", "admin")
def overview(ctx):
    days = ctx.qi("days", 30)
    since = db.days_from_today(-days) + " 00:00:00"
    k = {
        "shops_total": db.scalar("SELECT COUNT(*) FROM shops", (), 0),
        "shops_active": db.scalar("SELECT COUNT(*) FROM shops WHERE status='active'", (), 0),
        "shops_trial": db.scalar("SELECT COUNT(*) FROM shops WHERE status='trial'", (), 0),
        "shops_paused": db.scalar("SELECT COUNT(*) FROM shops WHERE status IN ('paused','churned')", (), 0),
        "shops_new_30d": db.scalar("SELECT COUNT(*) FROM shops WHERE created_at>=?", (since,), 0),
        "customers": db.scalar("SELECT COUNT(*) FROM customers", (), 0),
        "sales": db.scalar("SELECT COUNT(*) FROM sales", (), 0),
        "gmv": db.scalar("SELECT IFNULL(SUM(total),0) FROM sales WHERE sale_date>=?", (db.days_from_today(-days),), 0),
        "gmv_all": db.scalar("SELECT IFNULL(SUM(total),0) FROM sales", (), 0),
        "messages_out": db.scalar("SELECT COUNT(*) FROM messages WHERE direction='out' AND created_at>=?", (since,), 0),
        "messages_in": db.scalar("SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at>=?", (since,), 0),
        "messages_failed": db.scalar("SELECT COUNT(*) FROM messages WHERE status='failed' AND created_at>=?", (since,), 0),
        "reminders_sent": db.scalar("SELECT COUNT(*) FROM reminders WHERE sent_at>=? AND status<>'cancelled'", (since,), 0),
        "reminders_converted": db.scalar("SELECT COUNT(*) FROM reminders WHERE status='converted' AND sent_at>=?", (since,), 0),
        "revenue_recovered": db.scalar("SELECT IFNULL(SUM(revenue),0) FROM reminders WHERE status='converted' AND sent_at>=?", (since,), 0),
        "mrr": db.scalar("SELECT IFNULL(SUM(monthly_price),0) FROM shops WHERE status IN ('active','trial')", (), 0),
        "mrr_active": db.scalar("SELECT IFNULL(SUM(monthly_price),0) FROM shops WHERE status='active'", (), 0),
        "arr_projected": db.scalar("SELECT IFNULL(SUM(monthly_price),0) FROM shops WHERE status='active'", (), 0) * 12,
        "queued_now": db.scalar("SELECT COUNT(*) FROM messages WHERE status='queued'", (), 0),
        "due_now": db.scalar("SELECT COUNT(*) FROM reminders WHERE status='scheduled' AND due_on<=?", (db.today(),), 0),
        "live_channels": db.scalar("SELECT COUNT(*) FROM credentials WHERE enabled=1 AND driver<>'sandbox'", (), 0),
    }
    k["reply_rate"] = round(100.0 * k["messages_in"] / max(k["messages_out"], 1), 1)
    k["conversion_rate"] = round(100.0 * k["reminders_converted"] / max(k["reminders_sent"], 1), 1)
    k["delivery_rate"] = round(100.0 * (k["messages_out"] - k["messages_failed"]) / max(k["messages_out"], 1), 1)
    k["arpu"] = round(k["mrr"] / max(k["shops_total"], 1), 0)
    k["activation_rate"] = round(100.0 * k["shops_active"] / max(k["shops_total"], 1), 1)
    k["retention_score_avg"] = round(db.scalar(
        """SELECT AVG(100.0 * (SELECT COUNT(*) FROM customers c2 WHERE c2.shop_id=s.id
                              AND c2.risk_band IN ('healthy','due','new'))
                     / MAX(1,(SELECT COUNT(*) FROM customers c3 WHERE c3.shop_id=s.id)))
           FROM shops s WHERE (SELECT COUNT(*) FROM customers c WHERE c.shop_id=s.id) > 0""", (), 0) or 0, 1)

    return {
        "kpis": k,
        "shop_growth": db.q("""SELECT substr(created_at,1,10) day, COUNT(*) n FROM shops
                               WHERE created_at>=? GROUP BY day ORDER BY day""", (db.days_from_today(-120) + " 00:00:00",)),
        "messages_series": engine.daily_series(None, days, "messages"),
        "gmv_series": engine.daily_series(None, days, "sales"),
        "plan_mix": db.q("SELECT plan, COUNT(*) n, IFNULL(SUM(monthly_price),0) mrr FROM shops GROUP BY plan ORDER BY n DESC"),
        "status_mix": db.q("SELECT status, COUNT(*) n FROM shops GROUP BY status"),
        "sector_mix": db.q("SELECT sector, COUNT(*) n FROM shops GROUP BY sector ORDER BY n DESC LIMIT 12"),
        "channel_mix": db.q("""SELECT channel, COUNT(*) n, SUM(status='failed') failed FROM messages
                               WHERE direction='out' AND created_at>=? GROUP BY channel""", (since,)),
        "top_shops": db.q("""SELECT s.id, s.name, s.sector, s.plan, s.status, s.monthly_price,
                                    (SELECT COUNT(*) FROM customers c WHERE c.shop_id=s.id) customers,
                                    (SELECT IFNULL(SUM(total),0) FROM sales sa WHERE sa.shop_id=s.id AND sa.sale_date>=?) gmv,
                                    (SELECT COUNT(*) FROM messages m WHERE m.shop_id=s.id AND m.direction='out' AND m.created_at>=?) messages,
                                    (SELECT IFNULL(SUM(revenue),0) FROM reminders r WHERE r.shop_id=s.id AND r.status='converted' AND r.sent_at>=?) recovered,
                                    (SELECT COUNT(*) FROM reminders r2 WHERE r2.shop_id=s.id AND r2.status='converted' AND r2.sent_at>=?) conversions
                             FROM shops s ORDER BY gmv DESC LIMIT 12""",
                          (db.days_from_today(-days), since, since, since)),
        "recent_signups": db.q("""SELECT id, name, sector, city, plan, status, created_at FROM shops
                                  ORDER BY id DESC LIMIT 10"""),
        "needs_attention": db.q("""SELECT s.id, s.name, s.status, s.plan,
                                          (SELECT COUNT(*) FROM messages m WHERE m.shop_id=s.id AND m.status='failed' AND m.created_at>=?) failures,
                                          (SELECT COUNT(*) FROM customers c WHERE c.shop_id=s.id) customers,
                                          (SELECT MAX(created_at) FROM sales sa WHERE sa.shop_id=s.id) last_sale
                                   FROM shops s WHERE s.status IN ('active','trial')
                                   ORDER BY failures DESC LIMIT 8""", (since,)),
        "provider_health": providers.provider_health(),
        "worker": {"last_tick": db.meta_get("last_tick", "never"),
                   "last_tick_stats": json.loads(db.meta_get("last_tick_stats", "{}") or "{}"),
                   "ticks": int(db.meta_get("tick_count", "0") or 0)},
    }


@route("GET", "/api/admin/shops", "admin")
def admin_shops(ctx):
    page, per = max(1, ctx.qi("page", 1)), min(200, max(10, ctx.qi("per", 25)))
    where, params = ["1=1"], []
    if ctx.q("search"):
        where.append("(s.name LIKE ? OR s.owner_email LIKE ? OR s.city LIKE ? OR s.slug LIKE ?)")
        params += [f"%{ctx.q('search')}%"] * 4
    if ctx.q("status"):
        where.append("s.status=?")
        params.append(ctx.q("status"))
    if ctx.q("plan"):
        where.append("s.plan=?")
        params.append(ctx.q("plan"))
    if ctx.q("sector"):
        where.append("s.sector=?")
        params.append(ctx.q("sector"))
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM shops s WHERE {w}", params, 0)
    order = {"gmv": "gmv DESC", "customers": "customers DESC", "new": "s.id DESC",
             "recovered": "recovered DESC", "name": "s.name ASC", "mrr": "s.monthly_price DESC"}.get(
        ctx.q("sort") or "gmv", "gmv DESC")
    since = db.days_from_today(-30)
    rows = db.q(f"""SELECT s.id, s.name, s.slug, s.sector, s.city, s.plan, s.status, s.monthly_price, s.currency,
                           s.owner_name, s.owner_email, s.phone, s.created_at, s.onboarded_at, s.trial_ends_at,
                           (SELECT COUNT(*) FROM customers c WHERE c.shop_id=s.id) customers,
                           (SELECT COUNT(*) FROM sales sa WHERE sa.shop_id=s.id) sales,
                           (SELECT IFNULL(SUM(sa.total),0) FROM sales sa WHERE sa.shop_id=s.id) gmv,
                           (SELECT IFNULL(SUM(sa.total),0) FROM sales sa WHERE sa.shop_id=s.id AND sa.sale_date>=?) gmv_30d,
                           (SELECT COUNT(*) FROM messages m WHERE m.shop_id=s.id AND m.direction='out') messages,
                           (SELECT COUNT(*) FROM messages m WHERE m.shop_id=s.id AND m.status='failed') failed,
                           (SELECT COUNT(*) FROM reminders r WHERE r.shop_id=s.id AND r.status='converted') conversions,
                           (SELECT IFNULL(SUM(r.revenue),0) FROM reminders r WHERE r.shop_id=s.id AND r.status='converted') recovered,
                           (SELECT COUNT(*) FROM credentials cr WHERE cr.shop_id=s.id AND cr.enabled=1 AND cr.driver<>'sandbox') live_channels,
                           (SELECT MAX(sa.created_at) FROM sales sa WHERE sa.shop_id=s.id) last_activity
                    FROM shops s WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?""",
                [since] + params + [per, (page - 1) * per])
    for r in rows:
        r["retention_score"] = round(100.0 * db.scalar(
            "SELECT COUNT(*) FROM customers WHERE shop_id=? AND risk_band IN ('healthy','due','new')", (r["id"],), 0)
            / max(r["customers"], 1), 1)
        r["health"] = "healthy" if r["customers"] > 5 and (r["last_activity"] or "") >= db.days_from_today(-14) else (
            "quiet" if r["customers"] else "empty")
    return {"rows": rows, "total": total, "page": page, "per": per, "pages": max(1, (total + per - 1) // per),
            "sectors": db.q("SELECT sector, COUNT(*) n FROM shops GROUP BY sector ORDER BY n DESC")}


@route("GET", "/api/admin/shops/:sid", "admin")
def admin_shop_detail(ctx):
    sid = int(ctx.params["sid"])
    shop = db.q1("SELECT * FROM shops WHERE id=?", (sid,))
    if not shop:
        raise ApiError("shop not found", 404)
    days = ctx.qi("days", 30)
    since = db.days_from_today(-days) + " 00:00:00"
    return {
        "shop": shop,
        "owner": db.q1("SELECT id, name, email, role, last_login, created_at FROM users WHERE shop_id=? LIMIT 1", (sid,)),
        "kpis": engine.shop_kpis(sid, days),
        "revenue_series": engine.daily_series(sid, days, "sales"),
        "message_series": engine.daily_series(sid, days, "messages"),
        "risk_mix": db.q("SELECT risk_band band, COUNT(*) n FROM customers WHERE shop_id=? GROUP BY risk_band", (sid,)),
        "funnel": {
            "scheduled": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND created_at>=?", (sid, since), 0),
            "sent": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND sent_at>=?", (sid, since), 0),
            "replied": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status IN ('replied','converted') AND sent_at>=?", (sid, since), 0),
            "converted": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?", (sid, since), 0),
            "revenue": db.scalar("SELECT IFNULL(SUM(revenue),0) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?", (sid, since), 0),
        },
        "channels": db.q("""SELECT channel, provider, COUNT(*) n, SUM(status='failed') failed FROM messages
                            WHERE shop_id=? AND direction='out' AND created_at>=? GROUP BY channel, provider""", (sid, since)),
        "credentials": db.q("""SELECT provider, driver, enabled, config, updated_at FROM credentials
                               WHERE scope='shop' AND shop_id=?""", (sid,)),
        "top_customers": db.q("""SELECT id, name, phone, visit_count, total_spent, risk_band FROM customers
                                 WHERE shop_id=? ORDER BY total_spent DESC LIMIT 10""", (sid,)),
        "top_products": db.q("""SELECT si.product_name name, COUNT(*) n, IFNULL(SUM(si.line_total),0) revenue
                                FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.shop_id=?
                                GROUP BY si.product_name ORDER BY revenue DESC LIMIT 10""", (sid,)),
        "recent_messages": db.q("""SELECT m.id, m.channel, m.direction, m.status, m.provider, m.body, m.created_at,
                                          c.name customer
                                   FROM messages m LEFT JOIN customers c ON c.id=m.customer_id
                                   WHERE m.shop_id=? ORDER BY m.id DESC LIMIT 25""", (sid,)),
        "import_jobs": db.q("SELECT * FROM import_jobs WHERE shop_id=? ORDER BY id DESC LIMIT 10", (sid,)),
        "audit": db.q("SELECT * FROM audit_log WHERE shop_id=? ORDER BY id DESC LIMIT 30", (sid,)),
        "usage_vs_plan": {
            "plan": shop["plan"], "limits": PLANS.get(shop["plan"], PLANS["starter"]),
            "customers": db.scalar("SELECT COUNT(*) FROM customers WHERE shop_id=?", (sid,), 0),
            "messages_30d": db.scalar("SELECT COUNT(*) FROM messages WHERE shop_id=? AND direction='out' AND created_at>=?", (sid, since), 0),
        },
    }


@route("PATCH", "/api/admin/shops/:sid", "admin")
def admin_patch_shop(ctx):
    sid = int(ctx.params["sid"])
    if not db.q1("SELECT id FROM shops WHERE id=?", (sid,)):
        raise ApiError("shop not found", 404)
    allowed = ["name", "status", "plan", "monthly_price", "sector", "city", "owner_name", "owner_email",
               "auto_reminders", "max_msgs_per_customer_month", "trial_ends_at", "currency", "whatsapp_number"]
    patch = {k: ctx.b(k) for k in allowed if ctx.b(k) is not None}
    if "plan" in patch and "monthly_price" not in patch:
        patch["monthly_price"] = PLANS.get(patch["plan"], PLANS["starter"])["price"]
    if not patch:
        raise ApiError("nothing to update")
    db.update("shops", sid, patch)
    db.audit(ctx.user["email"], "admin.shop.updated", sid, "shop", sid, json.dumps(patch)[:300])
    return {"ok": True, "shop": db.q1("SELECT * FROM shops WHERE id=?", (sid,))}


@route("POST", "/api/admin/shops", "admin")
def admin_create_shop(ctx):
    """Manually onboard a shop (sales-assisted onboarding)."""
    name = (ctx.b("name") or "").strip()
    email = (ctx.b("owner_email") or "").strip().lower()
    if not name or not email:
        raise ApiError("shop name and owner email are required")
    if db.q1("SELECT id FROM users WHERE email=?", (email,)):
        raise ApiError("that email already has an account", 409)
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or f"shop-{int(time.time())}"
    base, n = slug, 2
    while db.q1("SELECT id FROM shops WHERE slug=?", (slug,)):
        slug, n = f"{base}-{n}", n + 1
    plan = ctx.b("plan") or "starter"
    sid = db.insert("shops", {
        "name": name[:120], "slug": slug, "owner_name": ctx.b("owner_name", "")[:80], "owner_email": email,
        "phone": ctx.b("phone", ""), "whatsapp_number": ctx.b("phone", ""), "sender_name": name[:40],
        "city": ctx.b("city") or "Kampala", "country": ctx.b("country") or "Uganda",
        "sector": ctx.b("sector") or "Retail", "plan": plan, "status": ctx.b("status") or "trial",
        "monthly_price": PLANS.get(plan, PLANS["starter"])["price"], "currency": ctx.b("currency") or "UGX",
        "onboarded_at": db.now_iso(), "trial_ends_at": db.days_from_today(14), "created_at": db.now_iso(),
    })
    password = ctx.b("password") or f"Rebuy!{sid}{db.utcnow():%m}"
    auth.create_user(sid, "owner", ctx.b("owner_name") or name, email, password)
    db.audit(ctx.user["email"], "admin.shop.created", sid, "shop", sid, f"{name} / {email}")
    return {"ok": True, "shop_id": sid, "temporary_password": password,
            "login_url": f"/#/login?email={email}"}


@route("POST", "/api/admin/shops/:sid/impersonate", "admin")
def impersonate(ctx):
    """Open a shop-owner session as this shop (read/write as them) for support."""
    sid = int(ctx.params["sid"])
    owner = db.q1("SELECT * FROM users WHERE shop_id=? AND role='owner' LIMIT 1", (sid,))
    shop = db.q1("SELECT * FROM shops WHERE id=?", (sid,))
    if not shop:
        raise ApiError("shop not found", 404)
    import hashlib
    import secrets
    from datetime import timedelta
    token = secrets.token_urlsafe(32)
    db.insert("sessions", {"token_hash": hashlib.sha256(token.encode()).hexdigest(),
                           "user_id": (owner or ctx.user)["id"] if owner else ctx.user["id"],
                           "created_at": db.now_iso(),
                           "expires_at": (db.utcnow() + timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")})
    if not owner:  # no owner user yet: borrow the admin user but scope it to the shop
        db.x("UPDATE sessions SET user_id=? WHERE token_hash=?",
             (ctx.user["id"], hashlib.sha256(token.encode()).hexdigest()))
    db.audit(ctx.user["email"], "admin.impersonate", sid, "shop", sid, shop["name"])
    body = json.dumps({"ok": True, "shop": shop, "note": "Open this in a new tab; the session lasts 2 hours."},
                      default=str).encode()
    return 200, {"content-type": "application/json",
                 "set-cookie": f"rebuy_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=7200",
                 "content-length": str(len(body))}, body


@route("GET", "/api/admin/messaging", "admin")
def admin_messaging(ctx):
    days = ctx.qi("days", 30)
    since = db.days_from_today(-days) + " 00:00:00"
    by_provider = db.q("""SELECT provider, channel, COUNT(*) n, SUM(status='failed') failed,
                                 SUM(status IN ('delivered','read','replied')) delivered
                          FROM messages WHERE direction='out' AND created_at>=?
                          GROUP BY provider, channel ORDER BY n DESC""", (since,))
    return {
        "totals": db.q1("""SELECT COUNT(*) all_messages, SUM(direction='out') outbound, SUM(direction='in') inbound,
                                  SUM(status='failed') failed, SUM(status='queued') queued,
                                  SUM(status='delivered') delivered
                           FROM messages WHERE created_at>=?""", (since,)) or {},
        "by_provider": by_provider,
        "by_channel_day": db.q("""SELECT substr(created_at,1,10) day, channel, COUNT(*) n FROM messages
                                  WHERE direction='out' AND created_at>=? GROUP BY day, channel ORDER BY day""", (since,)),
        "failures": db.q("""SELECT m.id, m.shop_id, s.name shop, m.channel, m.provider, m.to_addr, m.error, m.created_at
                            FROM messages m JOIN shops s ON s.id=m.shop_id
                            WHERE m.status='failed' ORDER BY m.id DESC LIMIT 40"""),
        "queue": db.q1("""SELECT SUM(status='queued') queued, SUM(status='sent') sent_awaiting_receipt,
                                 COUNT(*) total FROM messages""") or {},
        "templates": db.q("""SELECT template_key, channel, COUNT(*) n FROM messages
                             WHERE direction='out' AND created_at>=? GROUP BY template_key, channel
                             ORDER BY n DESC LIMIT 20""", (since,)),
        "reply_rates": db.q("""SELECT s.id, s.name,
                                     SUM(m.direction='out') outb, SUM(m.direction='in') inb
                              FROM messages m JOIN shops s ON s.id=m.shop_id
                              WHERE m.created_at>=? GROUP BY s.id ORDER BY outb DESC LIMIT 10""", (since,)),
        "cost_estimate": estimate_cost(by_provider),
        "providers": providers.provider_health(),
    }


COST_PER_MESSAGE = {"meta": {"whatsapp": 0.0318}, "twilio": {"sms": 0.0079}, "smtp": {"email": 0.0002},
                    "sandbox": {"whatsapp": 0, "sms": 0, "email": 0}}


def estimate_cost(by_provider: list[dict]) -> dict:
    total = 0.0
    for r in by_provider:
        total += (r["n"] or 0) * COST_PER_MESSAGE.get(r["provider"], {}).get(r["channel"], 0.0)
    return {"usd_30d": round(total, 2), "per_message_avg": round(total / max(sum(r["n"] or 0 for r in by_provider), 1), 5),
            "note": "Indicative: WhatsApp conversation ≈ $0.0318, Twilio SMS ≈ $0.0079, email ≈ $0.0002."}


@route("GET", "/api/admin/revenue", "admin")
def admin_revenue(ctx):
    rows = db.q("SELECT plan, status, monthly_price FROM shops")
    mrr_by_plan = {}
    for r in rows:
        mrr_by_plan.setdefault(r["plan"], {"shops": 0, "mrr": 0, "active": 0})
        mrr_by_plan[r["plan"]]["shops"] += 1
        mrr_by_plan[r["plan"]]["mrr"] += r["monthly_price"] or 0
        if r["status"] == "active":
            mrr_by_plan[r["plan"]]["active"] += 1
    mrr = db.scalar("SELECT IFNULL(SUM(monthly_price),0) FROM shops WHERE status IN ('active','trial')", (), 0)
    active_mrr = db.scalar("SELECT IFNULL(SUM(monthly_price),0) FROM shops WHERE status='active'", (), 0)
    return {
        "mrr": mrr, "active_mrr": active_mrr, "arr": active_mrr * 12,
        "arpu": round(mrr / max(db.scalar("SELECT COUNT(*) FROM shops", (), 1), 1), 0),
        "by_plan": mrr_by_plan,
        "plan_limits": PLANS,
        "growth": db.q("""SELECT substr(created_at,1,7) month, COUNT(*) shops, IFNULL(SUM(monthly_price),0) mrr
                          FROM shops GROUP BY month ORDER BY month"""),
        "status_mix": db.q("SELECT status, COUNT(*) n, IFNULL(SUM(monthly_price),0) mrr FROM shops GROUP BY status"),
        "trial_conversions": {
            "in_trial": db.scalar("SELECT COUNT(*) FROM shops WHERE status='trial'", (), 0),
            "converted": db.scalar("SELECT COUNT(*) FROM shops WHERE status='active'", (), 0),
            "expiring_7d": db.scalar("SELECT COUNT(*) FROM shops WHERE status='trial' AND trial_ends_at<=?",
                                     (db.days_from_today(7),), 0),
        },
        "top_paying": db.q("""SELECT id, name, plan, monthly_price, status, sector FROM shops
                              WHERE status IN ('active','trial') ORDER BY monthly_price DESC LIMIT 10"""),
        "gmv_processed": db.scalar("SELECT IFNULL(SUM(total),0) FROM sales", (), 0),
        "take_rate_note": "Rebuy earns subscription MRR; GMV processed is the sales value flowing through shops.",
    }


@route("GET", "/api/admin/retention", "admin")
def admin_retention(ctx):
    days = ctx.qi("days", 30)
    since = db.days_from_today(-days) + " 00:00:00"
    funnel = {
        "scheduled": db.scalar("SELECT COUNT(*) FROM reminders WHERE created_at>=?", (since,), 0),
        "sent": db.scalar("SELECT COUNT(*) FROM reminders WHERE sent_at>=? AND status<>'cancelled'", (since,), 0),
        "delivered": db.scalar("SELECT COUNT(*) FROM messages WHERE direction='out' AND status IN ('delivered','read','replied') AND created_at>=?", (since,), 0),
        "replied": db.scalar("SELECT COUNT(*) FROM messages WHERE direction='in' AND created_at>=?", (since,), 0),
        "converted": db.scalar("SELECT COUNT(*) FROM reminders WHERE status='converted' AND sent_at>=?", (since,), 0),
        "revenue": db.scalar("SELECT IFNULL(SUM(revenue),0) FROM reminders WHERE status='converted' AND sent_at>=?", (since,), 0),
        "cancelled": db.scalar("SELECT COUNT(*) FROM reminders WHERE status='cancelled' AND created_at>=?", (since,), 0),
    }
    return {
        "funnel": funnel,
        "by_kind": db.q("""SELECT kind, COUNT(*) sent, SUM(status='converted') converted,
                                  IFNULL(SUM(revenue),0) revenue
                           FROM reminders WHERE sent_at>=? GROUP BY kind ORDER BY sent DESC""", (since,)),
        "by_sector": db.q("""SELECT s.sector, COUNT(r.id) reminders, SUM(r.status='converted') converted,
                                    IFNULL(SUM(r.revenue),0) revenue
                             FROM reminders r JOIN shops s ON s.id=r.shop_id
                             WHERE r.sent_at>=? GROUP BY s.sector ORDER BY revenue DESC""", (since,)),
        "top_shops": db.q("""SELECT s.id, s.name, s.sector, COUNT(r.id) sent,
                                    SUM(r.status='converted') converted, IFNULL(SUM(r.revenue),0) revenue
                             FROM reminders r JOIN shops s ON s.id=r.shop_id
                             WHERE r.sent_at>=? GROUP BY s.id ORDER BY revenue DESC LIMIT 15""", (since,)),
        "worst_shops": db.q("""SELECT s.id, s.name, COUNT(r.id) sent, SUM(r.status='failed') failed
                               FROM reminders r JOIN shops s ON s.id=r.shop_id
                               WHERE r.created_at>=? GROUP BY s.id, s.name HAVING COUNT(r.id)>5 ORDER BY failed DESC LIMIT 10""", (since,)),
        "customer_risk": db.q("SELECT risk_band band, COUNT(*) n FROM customers GROUP BY risk_band"),
        "repeat_rate": db.q1("""SELECT COUNT(*) customers, SUM(visit_count>=2) repeat,
                                       AVG(visit_count) avg_visits, AVG(total_spent) avg_spend
                                FROM customers""") or {},
        "opt_outs": db.scalar("SELECT COUNT(*) FROM customers WHERE opted_in=0", (), 0),
        "reply_intent_mix": db.q("SELECT intent, COUNT(*) n FROM messages WHERE direction='in' AND intent<>'' GROUP BY intent ORDER BY n DESC"),
        "series": engine.daily_series(None, days, "messages"),
    }


@route("GET", "/api/admin/queue", "admin")
def admin_queue(ctx):
    return {
        "worker": {"last_tick": db.meta_get("last_tick", "never"),
                   "tick_count": int(db.meta_get("tick_count", "0") or 0),
                   "last_stats": json.loads(db.meta_get("last_tick_stats", "{}") or "{}"),
                   "interval_seconds": int(os.environ.get("REBUY_TICK_SECONDS", "20")),
                   "mode": "background thread (local/Docker) + /api/cron/tick (serverless)"},
        "reminders_by_status": db.q("SELECT status, COUNT(*) n FROM reminders GROUP BY status ORDER BY n DESC"),
        "due_now": db.scalar("SELECT COUNT(*) FROM reminders WHERE status='scheduled' AND due_on<=?", (db.today(),), 0),
        "due_by_day": db.q("""SELECT due_on day, COUNT(*) n FROM reminders WHERE status='scheduled'
                              GROUP BY due_on ORDER BY due_on LIMIT 21"""),
        "messages_by_status": db.q("SELECT status, COUNT(*) n FROM messages GROUP BY status ORDER BY n DESC"),
        "stuck": db.q("""SELECT m.id, m.shop_id, s.name shop, m.channel, m.status, m.error, m.created_at
                         FROM messages m JOIN shops s ON s.id=m.shop_id
                         WHERE m.status IN ('queued','failed') ORDER BY m.id DESC LIMIT 25"""),
        "next_7_days": db.q("""SELECT r.due_on, COUNT(*) n, s.name FROM reminders r JOIN shops s ON s.id=r.shop_id
                               WHERE r.status='scheduled' AND r.due_on<=? GROUP BY r.due_on, s.name
                               ORDER BY r.due_on LIMIT 40""", (db.days_from_today(7),)),
    }


@route("POST", "/api/admin/tick", "admin")
def admin_tick(ctx):
    from . import worker
    stats = worker.run_tick()
    db.audit(ctx.user["email"], "admin.tick", None, detail=json.dumps(stats)[:300])
    return {"ok": True, "stats": stats}


@route("GET", "/api/admin/credentials", "admin")
def admin_credentials(ctx):
    out = []
    for ch in ("whatsapp", "sms", "email"):
        row = db.q1("SELECT * FROM credentials WHERE scope='platform' AND provider=?", (ch,))
        cfg = json.loads(row["config"]) if row else {}
        driver, live = providers.credentials_for(None, ch)
        out.append({
            "channel": ch, "driver": row["driver"] if row else "sandbox",
            "enabled": bool(row and row["enabled"]), "config": providers.mask(cfg) if hasattr(providers, "mask") else _mask(cfg),
            "active_driver": driver,
            "shops_overriding": db.scalar("SELECT COUNT(*) FROM credentials WHERE scope='shop' AND provider=? AND enabled=1", (ch,), 0),
            "env_available": bool(providers._env_driver(ch)),
            "schema": {
                "whatsapp": {"token": "Meta permanent access token", "phone_number_id": "Phone Number ID",
                             "api_version": "graph API version (default v21.0)", "from_number": "display number",
                             "verify_token": "webhook verification token", "app_secret": "signature verification"},
                "sms": {"sid": "Twilio Account SID", "token": "Twilio Auth Token", "from_number": "Twilio sender"},
                "email": {"host": "SMTP host", "port": "587", "user": "username", "password": "password/app password",
                          "from_address": "from header", "security": "starttls | ssl"},
            }[ch],
        })
    return {"channels": out, "webhooks": {
        "whatsapp_inbound": "/api/webhooks/whatsapp",
        "twilio_inbound": "/api/webhooks/twilio",
        "twilio_status": "/api/webhooks/twilio-status",
        "email_inbound": "/api/webhooks/email",
        "cron": "/api/cron/tick",
    }}


def _mask(cfg: dict) -> dict:
    return {k: (v[:3] + "•" * 10 + v[-3:] if k in ("token", "password", "secret") and isinstance(v, str) and len(v) > 8
                else "•" * 8 if k in ("token", "password", "secret") and v else v) for k, v in cfg.items()}


@route("POST", "/api/admin/credentials", "admin")
def admin_save_credentials(ctx):
    ch = ctx.b("channel")
    if ch not in ("whatsapp", "sms", "email"):
        raise ApiError("unknown channel")
    driver = ctx.b("driver") or "sandbox"
    cfg = ctx.b("config") or {}
    row = db.q1("SELECT * FROM credentials WHERE scope='platform' AND provider=?", (ch,))
    if row:
        merged = json.loads(row["config"] or "{}")
        if driver == "sandbox":
            merged = {}
        for k, v in cfg.items():
            if isinstance(v, str) and "•" in v:
                continue
            if k in _SECRET_KEYS and not str(v or "").strip():
                continue
            merged[k] = v
        db.update("credentials", row["id"], {"driver": driver, "enabled": 1 if ctx.b("enabled", True) else 0,
                                             "config": json.dumps(merged), "updated_at": db.now_iso()})
    else:
        db.insert("credentials", {"scope": "platform", "shop_id": None, "provider": ch, "driver": driver,
                                  "enabled": 1 if ctx.b("enabled", True) else 0, "config": json.dumps(cfg),
                                  "updated_at": db.now_iso()})
    db.audit(ctx.user["email"], "admin.credentials.saved", None, "credential", ch, f"{ch}->{driver}")
    providers.clear_credential_cache()
    return {"ok": True, "channels": admin_credentials(ctx)["channels"]}


@route("POST", "/api/admin/credentials/test", "admin")
def admin_test_credentials(ctx):
    ch = ctx.b("channel")
    to = ctx.b("to") or ""
    driver = ctx.b("driver") or "sandbox"
    cfg = ctx.b("config") or {}
    if not to:
        raise ApiError("enter a destination to test against")
    if "•" in json.dumps(cfg):  # user left the stored secret untouched — load it
        row = db.q1("SELECT config FROM credentials WHERE scope='platform' AND provider=?", (ch,))
        stored = json.loads(row["config"]) if row else {}
        cfg = {**stored, **{k: v for k, v in cfg.items() if isinstance(v, str) and "•" not in v}}
    res = providers.test_provider(ch, driver, cfg, to, ctx.b("body") or f"Rebuy platform test on {ch} via {driver}.")
    db.audit(ctx.user["email"], "admin.credentials.test", None, "credential", ch, f"{driver} -> {to} ok={res.get('ok')}")
    return {"ok": bool(res.get("ok")), "driver": driver, "result": {k: v for k, v in res.items() if k != "body"},
            "detail": str(res.get("body", ""))[:800]}


@route("GET", "/api/admin/audit", "admin")
def admin_audit(ctx):
    limit = min(500, max(20, ctx.qi("limit", 100)))
    where, params = ["1=1"], []
    if ctx.q("shop"):
        where.append("shop_id=?")
        params.append(int(ctx.q("shop")))
    if ctx.q("action"):
        where.append("action LIKE ?")
        params.append(f"%{ctx.q('action')}%")
    w = " AND ".join(where)
    return {"rows": db.q(f"""SELECT a.*, s.name shop FROM audit_log a LEFT JOIN shops s ON s.id=a.shop_id
                             WHERE {w} ORDER BY a.id DESC LIMIT ?""", params + [limit]),
            "actions": db.q("SELECT action, COUNT(*) n FROM audit_log GROUP BY action ORDER BY n DESC LIMIT 25")}


@route("GET", "/api/admin/capacity", "admin")
def admin_capacity(ctx):
    """Proof the platform can carry 100+ shops: row counts, index list, timed queries."""
    size = db.size_bytes()
    if db.IS_PG:
        tables = db.q("SELECT table_name AS name FROM information_schema.tables "
                      "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")
        indexes = db.q("SELECT indexname AS name, tablename AS tbl_name FROM pg_indexes "
                       "WHERE schemaname='public' ORDER BY tablename, indexname")
    else:
        tables = db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
        indexes = db.q("SELECT name, tbl_name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY tbl_name")
    counts = {t["name"]: db.scalar(f"SELECT COUNT(*) FROM {t['name']}", (), 0) for t in tables}
    timings = {}
    probes = {
        "admin_shop_list": "SELECT s.id, (SELECT COUNT(*) FROM customers c WHERE c.shop_id=s.id) n FROM shops s ORDER BY s.id DESC LIMIT 25",
        "shop_dashboard_kpis": "SELECT COUNT(*) FROM messages WHERE shop_id=1 AND direction='out'",
        "due_reminders": "SELECT COUNT(*) FROM reminders WHERE status='scheduled' AND due_on<=date('now')",
        "customer_search": "SELECT COUNT(*) FROM customers WHERE shop_id=1 AND name LIKE '%a%'",
        "revenue_30d": "SELECT IFNULL(SUM(total),0) FROM sales WHERE sale_date>=date('now','-30 day')",
        "inbox_join": "SELECT COUNT(*) FROM messages m JOIN customers c ON c.id=m.customer_id WHERE m.shop_id=1",
    }
    for k, sql in probes.items():
        t0 = time.perf_counter()
        try:
            db.scalar(sql, (), 0)
            timings[k] = round((time.perf_counter() - t0) * 1000, 2)
        except Exception as e:  # noqa: BLE001
            timings[k] = f"error: {e}"
    shops = counts.get("shops", 0) or 1
    return {
        "db_path": db.DB_PATH if not db.IS_PG else "supabase://postgres (managed Postgres)",
        "db_backend": db.backend(), "db_size_mb": round(size / 1024 / 1024, 2),
        "row_counts": counts, "total_rows": sum(counts.values()),
        "indexes": indexes,
        "query_timings_ms": timings,
        "per_shop_avg": {k: round(v / shops, 1) for k, v in counts.items() if k in ("customers", "sales", "sale_items", "messages", "reminders")},
        "projection_1000_shops": {
            "customers": round(counts.get("customers", 0) / shops * 1000),
            "messages_per_month": round(counts.get("messages", 0) / shops * 1000),
            "estimated_db_mb": round(size / 1024 / 1024 / shops * 1000, 1),
            "recommendation": ("Running on managed Postgres (Supabase): connections are pooled, writes are concurrent, "
                               "and read replicas/point-in-time recovery are available. Scale by raising the pool size "
                               "and adding replicas rather than a single write lock."
                               if db.IS_PG else
                               "SQLite handles this comfortably with the current indexes (WAL mode, one writer). "
                               "Beyond ~5k shops or multi-region, set REBUY_DATABASE_URL to a Postgres/Supabase "
                               "connection string — the storage layer switches engine automatically."),
        },
        "limits": PLANS,
        "worker": {"interval_seconds": int(os.environ.get("REBUY_TICK_SECONDS", "20")),
                   "last_tick": db.meta_get("last_tick", "never"),
                   "ticks": int(db.meta_get("tick_count", "0") or 0)},
    }


@route("POST", "/api/admin/seed", "admin")
def admin_seed(ctx):
    from . import seed
    n = ctx.bi("shops", 120)
    res = seed.generate_demo_fleet(shops=n, reset=bool(ctx.b("reset", False)))
    db.audit(ctx.user["email"], "admin.seed", None, detail=json.dumps({k: v for k, v in res.items() if k != "shops"})[:300])
    return {"ok": True, **res}
