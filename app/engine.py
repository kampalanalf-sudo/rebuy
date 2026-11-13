"""
Rebuy — the retention engine.

Responsibilities
  1. Turn every sale line into a predicted run-out date (goods) or next-due date
     (services), scaled by the quantity bought.
  2. Schedule a reminder a few days *before* the customer runs out.
  3. Dispatch reminders over WhatsApp / SMS / email through the configured
     provider, honouring consent, quiet hours and per-customer frequency caps.
  4. Read replies, classify intent (order / snooze / opt-out / question), and
     react: create a ready-to-serve task, attach a coupon, or unsubscribe.
  5. Attribute revenue back to reminders so the shop (and the platform admin)
     can see what retention is actually earning.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import datetime, timedelta

from . import db, geo, privacy, providers

FMT = "%Y-%m-%d %H:%M:%S"
DFMT = "%Y-%m-%d"

# --------------------------------------------------------------------- templates
DEFAULT_TEMPLATES = [
    ("welcome", "whatsapp",
     "Hi {first_name}, welcome to {shop_name}! 🎉 We've saved your purchase so we can "
     "remind you before you run out of the things you buy often. Reply STOP at any time "
     "to opt out."),
    ("replenish", "whatsapp",
     "Hi {first_name} 👋 Your {product} ({qty} {unit}, bought {purchase_date}) should be "
     "running low around {depletes_on}. Shall we set aside {suggest_qty} {unit} for you? "
     "Reply YES and we'll keep it ready.{coupon_line}"),
    ("service_due", "whatsapp",
     "Hi {first_name} 👋 It's been {days_since} days since your {product} at {shop_name}. "
     "You're due again around {depletes_on}. Reply YES and we'll book you a slot this week."
     "{coupon_line}"),
    ("followup", "whatsapp",
     "Just checking in, {first_name} — we haven't heard back about your {product}. "
     "Want us to hold {suggest_qty} {unit}?{coupon_line}"),
    ("winback", "whatsapp",
     "{first_name}, we've missed you at {shop_name}! It's been {days_since} days since your "
     "last visit. Come back this week and use code {coupon_code} for {discount_text}."
     "{coupon_line}"),
    ("loyalty_reward", "whatsapp",
     "🎁 {first_name}, you've earned it! {points} points = {reward_text} at {shop_name}. "
     "Show this message or quote {coupon_code} at the counter."),
    ("loyalty_progress", "whatsapp",
     "{first_name}, you're {points_to_go} purchase(s) away from {reward_text} at {shop_name}. "
     "Keep going! 💪"),
    ("birthday", "whatsapp",
     "Happy birthday {first_name}! 🎂🎉 Everyone at {shop_name} wishes you a great day. "
     "Here's {discount_text} on us — code {coupon_code}, valid 7 days."),
    ("proximity", "whatsapp",
     "Hi {first_name} 👋 You're {distance_text} {shop_name} and your {product} should be "
     "running low. {offer_line} Reply YES and we'll keep it at the counter for you."),
    ("proximity_push", "push",
     "{product} is nearly out — you're {distance_text} {shop_name}. {offer_line}"),
    ("replenish_smart", "whatsapp",
     "Hi {first_name} — based on how often you buy {product}, it should run out around "
     "{depletes_on}. {best_time_line}Want us to set aside {suggest_qty} {unit}?{coupon_line}"),
    ("thank_you", "whatsapp",
     "Thank you for your purchase today, {first_name}! 🧾 {item_summary}. Total {total_text}. "
     "We'll let you know when it's time to restock."),
    ("broadcast", "whatsapp",
     "Hi {first_name}, {shop_name} here: {body_extra}{coupon_line}"),
    ("replenish", "sms",
     "{shop_name}: Hi {first_name}, your {product} is likely running low ({depletes_on}). "
     "Reply YES to reorder. STOP to opt out."),
    ("service_due", "sms",
     "{shop_name}: {first_name}, your {product} is due around {depletes_on}. Reply YES to book."),
    ("followup", "sms", "{shop_name}: following up on your {product}. Reply YES to reorder."),
    ("winback", "sms",
     "{shop_name}: we miss you {first_name}! Use {coupon_code} for {discount_text}. Reply STOP to opt out."),
    ("broadcast", "sms", "{shop_name}: {body_extra}{coupon_line}"),
    ("loyalty_reward", "sms", "{shop_name}: {points} pts earned = {reward_text}. Quote {coupon_code}."),
    ("birthday", "sms", "{shop_name}: Happy birthday {first_name}! {discount_text} with code {coupon_code}."),
    ("welcome", "sms", "Welcome to {shop_name}, {first_name}! We'll remind you before you run out. STOP to opt out."),
    ("thank_you", "sms", "{shop_name}: thanks {first_name}! {item_summary}. Total {total_text}."),
    ("replenish", "email",
     "Hi {first_name},\n\nBased on your purchase on {purchase_date}, your {product} "
     "({qty} {unit}) should be about finished around {depletes_on}.\n\n"
     "Would you like us to prepare {suggest_qty} {unit} for collection or delivery?\n"
     "Reply to this email or WhatsApp us on {shop_phone}.\n{coupon_line}\n"
     "— {shop_name}, {shop_address}"),
    ("service_due", "email",
     "Hi {first_name},\n\nIt has been {days_since} days since your {product}. Your next "
     "appointment is due around {depletes_on}.\n\nReply to book a slot.\n\n— {shop_name}"),
    ("winback", "email",
     "Hi {first_name},\n\nIt's been {days_since} days since we last saw you at {shop_name}. "
     "Here is {discount_text} with code {coupon_code} to welcome you back.\n\n— {shop_name}"),
    ("broadcast", "email", "Hi {first_name},\n\n{headline}\n\n{body_extra}\n\n— {shop_name}"),
    ("welcome", "email", "Hi {first_name},\n\nWelcome to {shop_name}. We'll help you never run out.\n\n— {shop_name}"),
    ("loyalty_reward", "email", "Hi {first_name},\n\nYou've earned {reward_text} ({points} points). Code {coupon_code}.\n\n— {shop_name}"),
    ("birthday", "email", "Happy birthday {first_name}!\n\n{discount_text} with code {coupon_code}.\n\n— {shop_name}"),
    ("thank_you", "email",
     "Hi {first_name},\n\nThank you for shopping with {shop_name}.\n\n{item_summary}\nTotal: {total_text}\n"
     "Receipt: {receipt_no}\n\n— {shop_name}"),
]

EMAIL_SUBJECTS = {
    "replenish": "Time to restock your {product}?",
    "service_due": "Your {product} is due",
    "winback": "We miss you, {first_name}",
    "loyalty_reward": "You've earned a reward at {shop_name}",
    "birthday": "Happy birthday, {first_name}! 🎂",
    "thank_you": "Your receipt from {shop_name}",
    "welcome": "Welcome to {shop_name}",
    "broadcast": "{headline}",
    "followup": "Following up — {product}",
}

CATEGORY_DEFAULT_CYCLES = {
    "Grains & Flour": 30, "Cooking Oil": 45, "Sugar": 30, "Salt": 60, "Beverages": 14,
    "Water": 7, "Airtime & Data": 14, "Fuel & Gas": 30, "Charcoal": 21,
    "Personal Care": 45, "Cosmetics": 60, "Cleaning": 30, "Baby Care": 21,
    "Dairy": 5, "Bakery": 3, "Meat & Fish": 5, "Produce": 4, "Pharmacy": 30,
    "Stationery": 60, "Hardware": 180, "Electronics": 365, "Pet Care": 30,
    "Agrovet": 30, "Hair & Beauty": 21, "Auto Service": 90, "Laundry": 7,
    "Fitness": 30, "General": 30,
}

RISK_BANDS = ["new", "healthy", "due", "overdue", "at_risk", "lost"]


# ------------------------------------------------------------------ date helpers
def pdate(s: str) -> datetime:
    s = (s or "").strip()
    for f in (DFMT, FMT, "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s[:19] if len(s) > 10 and f in (FMT,) else s[:len(f) + 2], f)
        except ValueError:
            continue
    try:
        return datetime.strptime(s[:10], DFMT)
    except ValueError:
        return db.utcnow()


def d(dt: datetime) -> str:
    return dt.strftime(DFMT)


def add_days(date_str: str, n: float) -> str:
    return d(pdate(date_str) + timedelta(days=n))


def diff_days(a: str, b: str) -> int:
    return (pdate(a) - pdate(b)).days


def money(v: float, currency: str = "UGX") -> str:
    sym = {"UGX": "UGX ", "KES": "KSh ", "USD": "$ ", "TZS": "TZS "}.get(currency, currency + " ")
    return f"{sym}{v:,.0f}"


# ------------------------------------------------------------------- cycle maths
def effective_cycle(product: dict, qty: float) -> tuple[int, str]:
    """How many days this purchase should last, scaled by quantity bought."""
    kind = product.get("kind", "goods")
    base = product.get("cycle_days") or CATEGORY_DEFAULT_CYCLES.get(product.get("category", ""), 30)
    bq = float(product.get("baseline_qty") or 1)
    if kind == "service":
        return max(3, int(base)), "service_due"
    ratio = max(0.25, (float(qty or 1) / bq))
    cycle = int(round(base * ratio))
    return max(3, min(cycle, 540)), "replenish"


def suggest_qty(qty: float) -> str:
    if qty == int(qty):
        return str(int(qty))
    return f"{qty:.1f}"


# -------------------------------------------------------------------- templates
# templates whose shipped default text changed — existing platforms get refreshed
_LEGACY_DEFAULTS = {
    ("broadcast", "whatsapp"): "Hi {first_name}, {shop_name} here: {headline}. {body_extra}{coupon_line}",
    ("broadcast", "sms"): "{shop_name}: {headline}. {body_extra}",
}


def seed_default_templates() -> None:
    if db.scalar("SELECT COUNT(*) FROM templates WHERE shop_id IS NULL", (), 0):
        defaults = {(k, ch): b for k, ch, b in DEFAULT_TEMPLATES}
        for (key, ch), legacy in _LEGACY_DEFAULTS.items():
            db.x("UPDATE templates SET body=? WHERE shop_id IS NULL AND key=? AND channel=? AND body=?",
                 (defaults.get((key, ch), legacy), key, ch, legacy))
        return
    rows = [(None, k, ch, "en", EMAIL_SUBJECTS.get(k, ""), b, 1, db.now_iso()) for k, ch, b in DEFAULT_TEMPLATES]
    db.xm("INSERT INTO templates (shop_id, key, channel, language, subject, body, active, created_at) "
          "VALUES (?,?,?,?,?,?,?,?)", rows)
    _TEMPLATE_CACHE.clear()


_TEMPLATE_CACHE: dict[tuple, dict] = {}


def clear_template_cache() -> None:
    _TEMPLATE_CACHE.clear()


def get_template(shop_id: int | None, key: str, channel: str) -> dict:
    ck = (shop_id or 0, key, channel)
    hit = _TEMPLATE_CACHE.get(ck)
    if hit is not None:
        return hit
    row = db.q1("SELECT * FROM templates WHERE IFNULL(shop_id,0)=? AND key=? AND channel=? AND active=1",
                (shop_id or 0, key, channel))
    if not row:
        row = db.q1("SELECT * FROM templates WHERE shop_id IS NULL AND key=? AND channel=? AND active=1",
                    (key, channel))
    if not row:
        row = db.q1("SELECT * FROM templates WHERE shop_id IS NULL AND key=? AND active=1 LIMIT 1", (key,))
    row = row or {"body": "Hi {first_name}, it's {shop_name}.", "subject": "", "key": key, "channel": channel}
    _TEMPLATE_CACHE[ck] = row
    return row


def render(shop_id: int | None, key: str, channel: str, ctx: dict) -> tuple[str, str]:
    tpl = get_template(shop_id, key, channel)
    # two passes: campaign/broadcast copy is injected via {body_extra} and carries its own placeholders
    body = _fill(_fill(tpl["body"], ctx), ctx)
    subject = _fill(tpl.get("subject") or EMAIL_SUBJECTS.get(key, ""), ctx) if channel == "email" else ""
    subject = _fill(subject, ctx)
    return subject, body


def _fill(text: str, ctx: dict) -> str:
    def rep(m):
        k = m.group(1)
        v = ctx.get(k, "")
        return "" if v is None else str(v)
    out = re.sub(r"\{(\w+)\}", rep, text or "")
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ------------------------------------------------------------------ context
def build_context(shop: dict, customer: dict, item: dict | None = None,
                  reminder: dict | None = None, coupon: dict | None = None, extra: dict | None = None) -> dict:
    first_name = (customer.get("name") or "there").strip().split(" ")[0]
    ctx = {
        "first_name": first_name,
        "customer_name": customer.get("name", ""),
        "shop_name": shop.get("sender_name") or shop.get("name", ""),
        "shop_phone": shop.get("whatsapp_number") or shop.get("phone", ""),
        "shop_address": ", ".join(x for x in [shop.get("city", ""), shop.get("country", "")] if x),
        "product": (item or {}).get("product_name", "your order"),
        "qty": suggest_qty((item or {}).get("qty", 1)),
        "unit": (item or {}).get("unit", "pc"),
        "suggest_qty": suggest_qty((item or {}).get("qty", 1)),
        "purchase_date": (item or {}).get("sale_date", ""),
        "depletes_on": (item or {}).get("depletes_on", ""),
        "days_since": (item or {}).get("days_since", ""),
        "receipt_no": (item or {}).get("receipt_no", ""),
        "total_text": money((item or {}).get("total", 0), shop.get("currency", "UGX")),
        "item_summary": (item or {}).get("item_summary", ""),
        "points": customer.get("loyalty_points", 0),
        "points_to_go": "",
        "reward_text": "a free item",
        "headline": (extra or {}).get("headline", "a quick update"),
        "body_extra": (extra or {}).get("body", ""),
        "distance_text": "",
        "offer_line": "",
        "best_time_line": "",
        "coupon_line": "",
        "coupon_code": "",
        "coupon": "",
        "discount_text": "",
    }
    if coupon:
        ctx["coupon_code"] = coupon["code"]
        ctx["coupon"] = coupon["code"]
        if coupon["discount_type"] == "percent":
            ctx["discount_text"] = f"{coupon['discount_value']:.0f}% off"
        elif coupon["discount_type"] == "amount":
            ctx["discount_text"] = money(coupon["discount_value"], shop.get("currency", "UGX")) + " off"
        else:
            ctx["discount_text"] = coupon.get("description") or "a free item"
        ctx["coupon_line"] = f" Use code {coupon['code']} for {ctx['discount_text']}."
    if ctx["depletes_on"]:
        ctx["days_since"] = max(0, diff_days(db.today(), ctx["purchase_date"])) if ctx["purchase_date"] else ""
    ctx.update({k: v for k, v in (extra or {}).items() if v is not None})
    return ctx


# ----------------------------------------------------------------- sale handling
def on_sale_created(sale_id: int) -> dict:
    """Plan reminders for a freshly recorded sale. Returns a small summary."""
    sale = db.q1("SELECT * FROM sales WHERE id=?", (sale_id,))
    if not sale:
        return {"reminders": 0}
    shop = db.q1("SELECT * FROM shops WHERE id=?", (sale["shop_id"],))
    customer = db.q1("SELECT * FROM customers WHERE id=?", (sale["customer_id"],)) if sale["customer_id"] else None
    items = db.q("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,))
    created = 0

    # Instant messages only make sense for sales captured in real time — an import
    # of last year's ledger must not text 4,000 people "thanks for your purchase today".
    fresh = diff_days(db.today(), sale["sale_date"]) <= 1
    if customer and fresh:
        summary = ", ".join(f"{i['qty']:g} {i['unit']} {i['product_name']}" for i in items[:4])
        if customer.get("visit_count", 0) <= 1:
            _schedule(shop, customer, None, "welcome", sale["sale_date"], channel=_channel(shop, customer))
        else:
            _schedule(shop, customer, None, "thank_you", sale["sale_date"], channel=_channel(shop, customer),
                      extra={"item_summary": summary, "receipt_no": sale["receipt_no"], "total": sale["total"]})

    for it in items:
        prod = db.q1("SELECT * FROM products WHERE id=?", (it["product_id"],)) if it["product_id"] else None
        prod = prod or {"kind": it["kind"], "cycle_days": it["cycle_days"],
                        "baseline_qty": 1, "category": it["category"]}
        cycle, kind = effective_cycle(prod, it["qty"])
        depletes = add_days(sale["sale_date"], cycle)
        db.update("sale_items", it["id"], {"cycle_days": cycle, "depletes_on": depletes, "kind": kind})
        lead = int(shop.get("reminder_lead_days") or 3)
        due = add_days(depletes, -lead)
        if due < sale["sale_date"]:
            due = sale["sale_date"]
        # backdated imports: if the run-out has already passed, remind today.
        if due < db.today():
            due = db.today()
        exists = db.q1("SELECT id FROM reminders WHERE sale_item_id=? AND kind IN ('replenish','service_due')", (it["id"],))
        if not exists:
            _schedule(shop, customer, it, kind, due, channel=_channel(shop, customer))
            created += 1
    recompute_customer(sale["shop_id"], sale["customer_id"]) if customer else None
    attribute_conversions(sale)
    return {"reminders": created, "items": len(items)}


def _channel(shop: dict, customer: dict | None) -> str:
    if customer and customer.get("consent_channel") in ("whatsapp", "sms", "email"):
        return customer["consent_channel"]
    if customer and customer.get("whatsapp"):
        return "whatsapp"
    if customer and not customer.get("phone") and customer.get("email"):
        return "email"
    return "whatsapp"


def _schedule(shop: dict, customer: dict | None, item: dict | None, kind: str, due_on: str,
              channel: str = "whatsapp", extra: dict | None = None, coupon_id: int | None = None,
              attempt: int = 1, campaign_id: int | None = None) -> int | None:
    if not customer or not customer.get("opted_in"):
        return None
    rid = db.insert("reminders", {
        "shop_id": shop["id"], "customer_id": customer["id"],
        "sale_item_id": (item or {}).get("id"), "product_name": (item or {}).get("product_name", ""),
        "kind": kind, "attempt": attempt, "due_on": due_on, "status": "scheduled",
        "channel": channel, "coupon_id": coupon_id, "created_at": db.now_iso(),
    })
    if extra or campaign_id:
        # reuse the snooze_until column as a small json side-channel for context
        payload = {"_campaign": campaign_id} if campaign_id else {}
        payload.update({k: v for k, v in (extra or {}).items() if k != "_campaign"})
        db.x("UPDATE reminders SET snooze_until=? WHERE id=?", (json.dumps(payload), rid))
    return rid


# ------------------------------------------------------------------- dispatch
def in_quiet_hours(shop: dict, when: datetime | None = None) -> bool:
    now = when or db.utcnow()
    try:
        qs = datetime.strptime(shop.get("quiet_start") or "21:00", "%H:%M").time()
        qe = datetime.strptime(shop.get("quiet_end") or "07:00", "%H:%M").time()
    except ValueError:
        return False
    t = now.time()
    if qs == qe:
        return False
    if qs < qe:
        return qs <= t < qe
    return t >= qs or t < qe


def monthly_message_count(shop_id: int, customer_id: int) -> int:
    since = (db.utcnow() - timedelta(days=30)).strftime(FMT)
    return db.scalar(
        "SELECT COUNT(*) FROM messages WHERE shop_id=? AND customer_id=? AND direction='out' AND created_at>=?",
        (shop_id, customer_id, since), 0)


_AUTOMATION_LAST: dict[int, datetime] = {}
AUTOMATION_INTERVAL_SECONDS = int(os.environ.get("REBUY_AUTOMATION_INTERVAL", "900"))


def _automation_due(shop_id: int, now: datetime) -> bool:
    last = _AUTOMATION_LAST.get(shop_id)
    if last is None or (now - last).total_seconds() >= AUTOMATION_INTERVAL_SECONDS:
        _AUTOMATION_LAST[shop_id] = now
        return True
    return False


def tick(limit: int = 400, now: datetime | None = None) -> dict:
    """One pass of the worker. Called by the background thread (local/Docker)
    and by the /api/cron/tick endpoint (Vercel cron)."""
    now = now or db.utcnow()
    stats = {"dispatched": 0, "deferred": 0, "cancelled": 0, "replies": 0, "deliveries": 0,
             "followups": 0, "winbacks": 0, "loyalty": 0, "birthdays": 0, "errors": 0}
    today = d(now)

    # Only shops with something to do: one query instead of one per shop.
    shops = db.q("""SELECT s.* FROM shops s
                    WHERE s.status IN ('active','trial')
                      AND (EXISTS (SELECT 1 FROM reminders r WHERE r.shop_id=s.id AND r.status='scheduled' AND r.due_on<=?)
                           OR EXISTS (SELECT 1 FROM messages m WHERE m.shop_id=s.id AND m.status='queued'))
                    ORDER BY s.id""", (today,))
    for shop in shops:
        quiet = in_quiet_hours(shop, now)
        # one lookup for the whole shop's monthly cap checks
        _cap_cache: dict[int, int] = {}

        def _msgs_this_month(cid: int) -> int:
            if cid not in _cap_cache:
                _cap_cache[cid] = monthly_message_count(shop["id"], cid)
            return _cap_cache[cid]

        # 1. scheduled reminders that are due
        due = db.q("""SELECT r.* FROM reminders r JOIN customers c ON c.id=r.customer_id
                      WHERE r.shop_id=? AND r.status='scheduled' AND r.due_on<=? AND r.kind<>'silent'
                      ORDER BY r.due_on LIMIT ?""", (shop["id"], today, limit))
        for r in due:
            c = db.q1("SELECT * FROM customers WHERE id=?", (r["customer_id"],))
            if not c:
                db.update("reminders", r["id"], {"status": "cancelled"})
                continue
            if not c.get("opted_in"):
                db.update("reminders", r["id"], {"status": "cancelled"})
                stats["cancelled"] += 1
                continue
            cap = int(shop.get("max_msgs_per_customer_month") or 6)
            if _msgs_this_month(c["id"]) >= cap:
                db.update("reminders", r["id"], {"status": "cancelled"})
                stats["cancelled"] += 1
                db.audit("engine", "reminder.cap_reached", shop["id"], "reminder", r["id"], c["name"])
                continue
            if quiet:
                stats["deferred"] += 1
                continue
            _dispatch(shop, c, r, stats)

        # 2. follow-ups for reminders that got no answer.
        #    These are "nice to have" and unbounded in a big fleet, so they run on
        #    a per-shop timer instead of on every pass (keeps each tick cheap).
        if shop.get("auto_reminders") and _automation_due(shop["id"], now):
            _followups(shop, today, stats)
            _winbacks(shop, today, stats)
            _loyalty(shop, today, stats)
            _birthdays(shop, today, stats)

    # 3. push queued messages out through providers
    _deliver_queued(stats, limit)
    # 4. sandbox: simulated delivery receipts + customer replies
    _simulate_inbound(stats, now)
    return stats


def _dispatch(shop: dict, c: dict, r: dict, stats: dict) -> None:
    item = None
    if r.get("sale_item_id"):
        item = db.q1("""SELECT si.*, s.sale_date, s.receipt_no, s.total FROM sale_items si
                        JOIN sales s ON s.id=si.sale_id WHERE si.id=?""", (r["sale_item_id"],))
    coupon = db.q1("SELECT * FROM coupons WHERE id=?", (r["coupon_id"],)) if r.get("coupon_id") else None
    extra = None
    campaign_id = None
    if r.get("snooze_until") and str(r["snooze_until"]).startswith("{"):
        try:
            extra = json.loads(r["snooze_until"])
            campaign_id = extra.pop("_campaign", None)
        except json.JSONDecodeError:
            extra = None
    ctx = build_context(shop, c, item, r, coupon, extra)
    subject, body = render(shop["id"], r["kind"], r["channel"], ctx)
    to = {"whatsapp": c.get("whatsapp") or c.get("phone"), "sms": c.get("phone"),
          "email": c.get("email")}[r["channel"]]
    if not to:
        fallback = "whatsapp" if (c.get("whatsapp") or c.get("phone")) else ("email" if c.get("email") else None)
        if not fallback:
            db.update("reminders", r["id"], {"status": "failed"})
            stats["errors"] += 1
            return
        r = dict(r, channel=fallback)
        to = c.get("whatsapp") or c.get("phone") or c.get("email")
        subject, body = render(shop["id"], r["kind"], r["channel"], ctx)

    driver, cfg = providers.credentials_for(shop["id"], r["channel"])
    mid = db.insert("messages", {
        "shop_id": shop["id"], "customer_id": c["id"], "reminder_id": r["id"],
        "direction": "out", "channel": r["channel"], "provider": driver,
        "to_addr": to, "from_addr": shop.get("whatsapp_number") or shop.get("phone") or cfg.get("from_address", ""),
        "subject": subject, "body": body, "template_key": r["kind"], "status": "queued",
        "campaign_id": campaign_id, "created_at": db.now_iso(),
    })
    db.update("reminders", r["id"], {"status": "queued", "message_id": mid, "channel": r["channel"]})
    stats["dispatched"] += 1


def queue_nudge_message(shop: dict, decision: dict) -> int | None:
    """Turn an approved nudge decision into a queued WhatsApp/push message.

    Kept separate from `_dispatch` because the input is a model decision rather
    than a scheduled reminder row, and the copy is location-aware.
    """
    customer = db.q1("SELECT * FROM customers WHERE id=?", (decision.get("customer_id"),))
    if not customer:
        return None
    channel = "whatsapp"
    distance = float(decision.get("distance_m") or 0)
    extra = {
        "distance_text": geo.describe_distance(distance) if distance else "",
        "offer_line": (f"{decision['discount_pct']:.0f}% off if you come in the next 45 minutes."
                       if decision.get("discount_pct") else ""),
        "headline": "Nearby right now",
    }
    key = "proximity" if distance else "replenish_smart"
    subject, body = render(shop["id"], key, channel, build_context(shop, customer, None, None, None, extra))
    msg_id = db.insert("messages", {
        "shop_id": shop["id"], "customer_id": customer["id"], "reminder_id": None,
        "campaign_id": None, "direction": "out", "channel": channel, "provider": "sandbox",
        "to_addr": customer.get("whatsapp") or customer.get("phone") or "",
        "from_addr": shop.get("whatsapp_number") or "", "subject": subject, "body": body,
        "template_key": key, "status": "queued", "provider_msg_id": "", "error": "",
        "cost": 0.0, "intent": "", "created_at": db.now_iso(),
    })
    db.audit("nudges", "nudge.queued", shop["id"], "message", msg_id,
             f"score={decision.get('score')} {decision.get('reason', '')[:120]}")
    return msg_id


def _deliver_queued(stats: dict, limit: int) -> None:
    queued = db.q("SELECT * FROM messages WHERE status='queued' ORDER BY id LIMIT ?", (limit * 3,))
    for m in queued:
        shop = db.q1("SELECT * FROM shops WHERE id=?", (m["shop_id"],))
        # Clinical-vocabulary guard. A recall reminder may say "you are due for
        # a dental check-up"; it may never carry a diagnosis, a test result or
        # an HIV/TB status, because those messages are read on shared phones and
        # can do real harm. Anything flagged is held, not sent, and logged.
        findings = privacy.scrub_findings(m["body"] or "")
        if findings:
            db.update("messages", m["id"], {
                "status": "blocked",
                "error": "clinical terms withheld: " + ", ".join(findings),
            })
            privacy.log_outbound(m["shop_id"], m.get("customer_id"), m["channel"],
                                 m["body"] or "", scrubbed=True, blocked=True)
            continue
        driver, cfg = providers.credentials_for(m["shop_id"], m["channel"])
        try:
            if m["channel"] == "whatsapp":
                res = providers.send_whatsapp(m["to_addr"], m["body"], cfg, driver)
            elif m["channel"] == "sms":
                res = providers.send_sms(m["to_addr"], m["body"], cfg, driver)
            else:
                res = providers.send_email(m["to_addr"], m["subject"] or f"Message from {shop['name']}",
                                           m["body"], cfg, driver)
        except Exception as e:  # noqa: BLE001
            res = {"ok": False, "error": f"{type(e).__name__}: {e}"}
        if res.get("ok"):
            try:
                privacy.log_outbound(m["shop_id"], m.get("customer_id"), m["channel"],
                                     m["body"] or "", scrubbed=False, blocked=False)
            except Exception:  # noqa: BLE001
                pass
            db.update("messages", m["id"], {
                "status": "sent", "provider": res.get("provider", driver),
                "provider_msg_id": res.get("message_id", ""), "sent_at": db.now_iso(),
            })
            if m.get("reminder_id"):
                db.update("reminders", m["reminder_id"], {"status": "sent", "sent_at": db.now_iso()})
            # sandbox delivery receipts arrive fast
            if res.get("provider") == "sandbox":
                db.update("messages", m["id"], {"status": "delivered", "delivered_at": db.now_iso()})
                if m.get("reminder_id"):
                    db.update("reminders", m["reminder_id"], {"status": "delivered"})
            stats["deliveries"] += 1
        else:
            db.update("messages", m["id"], {"status": "failed", "error": str(res.get("error", ""))[:400],
                                            "provider": res.get("provider", driver)})
            if m.get("reminder_id"):
                db.update("reminders", m["reminder_id"], {"status": "failed"})
            stats["errors"] += 1
            db.audit("engine", "message.failed", m["shop_id"], "message", m["id"], str(res.get("error", ""))[:200])


REPLY_WEIGHTS = [("order", 44), ("question", 12), ("snooze", 11), ("negative", 7), ("opt_out", 4), ("other", 22)]
REPLY_SAMPLES = {
    "order": ["Yes please, set it aside for me", "Yes, deliver 2 in the evening", "Sure, I'll pick it up tomorrow morning",
              "Okay confirm my order", "Yes I need it, thanks"],
    "question": ["How much is it now?", "Do you have it in stock?", "What time do you close today?"],
    "snooze": ["Not yet, I still have some. Next week please", "Later, I'll come end of month"],
    "negative": ["No thanks, not interested", "No, I bought elsewhere"],
    "opt_out": ["STOP", "Please stop messaging me"],
    "other": ["Received, thanks", "👍", "Let me check and get back to you"],
}


def _simulate_inbound(stats: dict, now: datetime) -> None:
    """Sandbox provider: create delivery receipts + plausible customer replies."""
    driver, _ = providers.credentials_for(None, "whatsapp")
    candidates = db.q("""SELECT m.* FROM messages m WHERE m.provider='sandbox' AND m.direction='out'
                         AND m.status IN ('sent','delivered') AND m.sent_at IS NOT NULL
                         AND m.sent_at <= ? AND m.template_key IN ('replenish','service_due','followup','winback','broadcast','loyalty_reward')
                         AND NOT EXISTS (SELECT 1 FROM messages r WHERE r.reminder_id=m.reminder_id AND r.direction='in')
                         LIMIT 60""", ((now - timedelta(seconds=25)).strftime(FMT),))
    for m in candidates:
        if m["status"] == "sent":
            db.update("messages", m["id"], {"status": "delivered", "delivered_at": db.now_iso()})
            if m.get("reminder_id"):
                db.update("reminders", m["reminder_id"], {"status": "delivered"})
            stats["deliveries"] += 1
        if random.randint(1, 100) <= 70:  # ~70% of customers eventually answer
            intent = random.choices([i for i, _ in REPLY_WEIGHTS], weights=[w for _, w in REPLY_WEIGHTS])[0]
            text = random.choice(REPLY_SAMPLES[intent])
            handle_inbound(m["shop_id"], m["customer_id"], "whatsapp", m["to_addr"], text,
                           provider="sandbox", reminder_id=m.get("reminder_id"), reply_to=m["id"])
            stats["replies"] += 1


def handle_inbound(shop_id: int, customer_id: int | None, channel: str, from_addr: str, text: str,
                   provider: str = "", reminder_id: int | None = None, reply_to: int | None = None) -> dict:
    """One place for every inbound message: webhooks, sandbox replies, manual logging."""
    c = None
    if customer_id:
        c = db.q1("SELECT * FROM customers WHERE id=?", (customer_id,))
    if not c:
        norm = providers.e164(from_addr)
        c = db.q1("SELECT * FROM customers WHERE shop_id=? AND (phone=? OR whatsapp=?)",
                  (shop_id, from_addr, from_addr)) or db.q1(
            "SELECT * FROM customers WHERE shop_id=? AND (phone=? OR whatsapp=?)", (shop_id, norm, norm))
    if not c:
        c = {"id": None, "name": from_addr, "opted_in": 1}
    intent = providers.classify_intent(text)
    shop = db.q1("SELECT * FROM shops WHERE id=?", (shop_id,))
    mid = db.insert("messages", {
        "shop_id": shop_id, "customer_id": c.get("id"), "reminder_id": reminder_id,
        "direction": "in", "channel": channel, "provider": provider or "sandbox",
        "from_addr": from_addr, "to_addr": shop.get("whatsapp_number", "") if shop else "",
        "body": text, "status": "replied", "intent": intent, "created_at": db.now_iso(),
    })
    if reminder_id:
        db.update("reminders", reminder_id, {"status": "replied", "replied_at": db.now_iso(), "message_id": mid})
    if c.get("id"):
        if intent == "opt_out":
            db.update("customers", c["id"], {"opted_in": 0, "opt_out_at": db.now_iso()})
            db.x("UPDATE reminders SET status='cancelled' WHERE customer_id=? AND status IN ('scheduled','queued')",
                 (c["id"],))
            db.audit("customer", "opt_out", shop_id, "customer", c["id"], text[:120])
        elif intent == "snooze":
            db.x("UPDATE reminders SET status='snoozed', snooze_until=?, due_on=? WHERE id=?",
                 (db.now_iso(), db.days_from_today(14), reminder_id or -1))
        db.x("UPDATE customers SET updated_at=? WHERE id=?", (db.now_iso(), c["id"]))
    return {"message_id": mid, "intent": intent, "customer": c.get("name")}


# ---------------------------------------------------------------- automation
def _followups(shop: dict, today: str, stats: dict) -> None:
    maxf = int(shop.get("max_followups") or 2)
    rows = db.q("""SELECT r.* FROM reminders r
                   WHERE r.shop_id=? AND r.status IN ('sent','delivered') AND r.kind IN ('replenish','service_due')
                   AND r.sent_at IS NOT NULL AND date(r.sent_at) <= date(?, '-3 day')
                   AND date(r.sent_at) >= date(?, '-30 day') AND r.attempt < ?
                   AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.reminder_id=r.id AND m.direction='in')
                   AND NOT EXISTS (SELECT 1 FROM reminders r2 WHERE r2.customer_id=r.customer_id
                                   AND r2.kind='followup' AND r2.attempt>r.attempt)
                   LIMIT 12""", (shop["id"], today, today, maxf))
    for r in rows:
        c = db.q1("SELECT * FROM customers WHERE id=?", (r["customer_id"],))
        item = db.q1("""SELECT si.*, s.sale_date, s.receipt_no, s.total FROM sale_items si
                        JOIN sales s ON s.id=si.sale_id WHERE si.id=?""", (r["sale_item_id"],)) if r["sale_item_id"] else None
        coupon_id = None
        if r["attempt"] + 1 >= maxf and random.random() < 0.6:
            coupon_id = _make_coupon(shop["id"], c["id"], "followup")
        _schedule(shop, c, item, "followup", today, channel=r["channel"], coupon_id=coupon_id,
                  attempt=r["attempt"] + 1)
        db.update("reminders", r["id"], {"status": "superseded"})
        stats["followups"] += 1


def _winbacks(shop: dict, today: str, stats: dict) -> None:
    rows = db.q("""SELECT c.* FROM customers c WHERE c.shop_id=? AND c.opted_in=1
                   AND c.risk_band IN ('at_risk','lost') AND c.visit_count >= 2
                   AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.customer_id=c.id AND r.kind='winback'
                                   AND r.created_at >= date(?, '-30 day'))
                   LIMIT 25""", (shop["id"], today))
    for c in rows:
        coupon_id = _make_coupon(shop["id"], c["id"], "winback", value=10)
        _schedule(shop, c, None, "winback", today, channel=_channel(shop, c), coupon_id=coupon_id)
        stats["winbacks"] += 1


def _loyalty(shop: dict, today: str, stats: dict) -> None:
    if not shop.get("loyalty_enabled"):
        return
    rows = db.q("""SELECT c.* FROM customers c WHERE c.shop_id=? AND c.opted_in=1 AND c.loyalty_points >= 5
                   AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.customer_id=c.id AND r.kind='loyalty_reward'
                                   AND r.created_at >= date(?, '-45 day')) LIMIT 20""", (shop["id"], today))
    for c in rows:
        coupon_id = _make_coupon(shop["id"], c["id"], "loyalty", kind="free_item")
        db.update("customers", c["id"], {"loyalty_points": max(0, c["loyalty_points"] - 5)})
        db.insert("loyalty_events", {"shop_id": shop["id"], "customer_id": c["id"], "points": -5,
                                     "reason": "reward_redeemed", "ref": str(coupon_id), "created_at": db.now_iso()})
        _schedule(shop, c, None, "loyalty_reward", today, channel=_channel(shop, c), coupon_id=coupon_id,
                  extra={"reward_text": "a free item"})
        stats["loyalty"] += 1


def _birthdays(shop: dict, today: str, stats: dict) -> None:
    md = today[5:]
    rows = db.q("""SELECT c.* FROM customers c WHERE c.shop_id=? AND c.opted_in=1 AND substr(c.birthday,6,5)=?
                   AND NOT EXISTS (SELECT 1 FROM reminders r WHERE r.customer_id=c.id AND r.kind='birthday'
                                   AND r.due_on=?) LIMIT 20""", (shop["id"], md, today))
    for c in rows:
        coupon_id = _make_coupon(shop["id"], c["id"], "birthday", value=15)
        _schedule(shop, c, None, "birthday", today, channel=_channel(shop, c), coupon_id=coupon_id)
        stats["birthdays"] += 1


def _make_coupon(shop_id: int, customer_id: int, reason: str, value: float = 5,
                 kind: str = "percent") -> int:
    code = f"{reason[:3].upper()}{random.randint(1000, 9999)}"
    existing = db.q1("SELECT id FROM coupons WHERE shop_id=? AND code=?", (shop_id, code))
    if existing:
        return existing["id"]
    desc = {"winback": "Welcome back discount", "followup": "Reorder nudge", "loyalty": "Loyalty reward",
            "birthday": "Birthday treat"}.get(reason, "Discount")
    return db.insert("coupons", {
        "shop_id": shop_id, "code": code, "description": desc,
        "discount_type": kind, "discount_value": value, "expires_on": db.days_from_today(14),
        "max_uses": 1, "customer_id": customer_id, "status": "active", "created_at": db.now_iso(),
    })


# ------------------------------------------------------------------- scoring
def recompute_customer(shop_id: int, customer_id: int | None) -> None:
    if not customer_id:
        return
    agg = db.q1("""SELECT COUNT(DISTINCT s.id) visits, IFNULL(SUM(s.total),0) spent,
                          MAX(s.sale_date) last_purchase, MIN(s.sale_date) first_seen
                   FROM sales s WHERE s.customer_id=?""", (customer_id,))
    cycle = db.scalar("""SELECT AVG(si.cycle_days) FROM sale_items si JOIN sales s ON s.id=si.sale_id
                         WHERE s.customer_id=?""", (customer_id,), None)
    cycle = float(cycle or 30)
    last = agg["last_purchase"] or db.today()
    days_since = max(0, diff_days(db.today(), last))
    ratio = days_since / max(cycle, 1)
    if agg["visits"] <= 1 and days_since <= cycle:
        band = "new"
    elif ratio < 0.8:
        band = "healthy"
    elif ratio < 1.15:
        band = "due"
    elif ratio < 1.7:
        band = "overdue"
    elif ratio < 3.2:
        band = "at_risk"
    else:
        band = "lost"
    ltv = (agg["spent"] / max(agg["visits"], 1)) * min(agg["visits"], 12) * 1.0
    db.update("customers", customer_id, {
        "visit_count": agg["visits"], "total_spent": agg["spent"], "last_purchase_at": last,
        "first_seen": agg["first_seen"] or last, "risk_band": band, "lifetime_value": round(ltv, 2),
        "updated_at": db.now_iso(),
    })


def recompute_shop(shop_id: int) -> None:
    for row in db.q("SELECT id FROM customers WHERE shop_id=?", (shop_id,)):
        recompute_customer(shop_id, row["id"])


def attribute_conversions(sale: dict) -> None:
    """If a customer buys again within 21 days of a reminder, credit that reminder."""
    if not sale.get("customer_id"):
        return
    open_reminders = db.q("""SELECT * FROM reminders WHERE customer_id=? AND status IN ('sent','delivered','replied')
                             AND sent_at IS NOT NULL AND date(sent_at) >= date(?, '-21 day')""",
                          (sale["customer_id"], sale["sale_date"]))
    items = db.q("SELECT * FROM sale_items WHERE sale_id=?", (sale["id"],))
    names = {(i["product_name"] or "").lower() for i in items}
    for r in open_reminders:
        match = (r["kind"] in ("welcome", "winback", "birthday", "loyalty_reward", "broadcast", "thank_you")
                 or (r.get("product_name") or "").lower() in names)
        if not match:
            continue
        rev = sale["total"] if r["kind"] != "replenish" else sum(
            i["line_total"] for i in items if (i["product_name"] or "").lower() == (r.get("product_name") or "").lower())
        db.update("reminders", r["id"], {"status": "converted", "converted_at": db.now_iso(), "revenue": rev or sale["total"]})
        if r.get("coupon_id"):
            db.x("UPDATE coupons SET used_count=used_count+1, status='used' WHERE id=?", (r["coupon_id"],))
        db.audit("engine", "reminder.converted", sale["shop_id"], "reminder", r["id"], f"revenue={rev}")


# ------------------------------------------------------------------- analytics
def shop_kpis(shop_id: int, days: int = 30) -> dict:
    since = db.days_from_today(-days)
    k = {}
    k["customers"] = db.scalar("SELECT COUNT(*) FROM customers WHERE shop_id=?", (shop_id,), 0)
    k["active_customers"] = db.scalar(
        "SELECT COUNT(*) FROM customers WHERE shop_id=? AND risk_band IN ('healthy','due','new')", (shop_id,), 0)
    k["at_risk"] = db.scalar(
        "SELECT COUNT(*) FROM customers WHERE shop_id=? AND risk_band IN ('at_risk','lost')", (shop_id,), 0)
    k["revenue_at_risk"] = db.scalar(
        "SELECT IFNULL(SUM(total_spent/ (CASE WHEN visit_count=0 THEN 1 ELSE visit_count END)),0) "
        "FROM customers WHERE shop_id=? AND risk_band IN ('at_risk','lost')", (shop_id,), 0)
    k["sales_total"] = db.scalar("SELECT IFNULL(SUM(total),0) FROM sales WHERE shop_id=? AND sale_date>=?",
                                 (shop_id, since), 0)
    k["repeat_rate"] = round(100.0 * db.scalar(
        "SELECT COUNT(*) FROM customers WHERE shop_id=? AND visit_count>=2", (shop_id,), 0) / max(k["customers"], 1), 1)
    k["reminders_sent"] = db.scalar(
        "SELECT COUNT(*) FROM messages WHERE shop_id=? AND direction='out' AND created_at>=?", (shop_id, since + " 00:00:00"), 0)
    k["replies"] = db.scalar(
        "SELECT COUNT(*) FROM messages WHERE shop_id=? AND direction='in' AND created_at>=?", (shop_id, since + " 00:00:00"), 0)
    k["conversions"] = db.scalar(
        "SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?", (shop_id, since + " 00:00:00"), 0)
    k["revenue_recovered"] = db.scalar(
        "SELECT IFNULL(SUM(revenue),0) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?",
        (shop_id, since + " 00:00:00"), 0)
    # Reply rate is per OUTBOUND REMINDER, not per message. A single reminder
    # often produces a multi-message WhatsApp thread, so a raw inbound/outbound
    # message ratio can exceed 100% — three messages back and forth over one
    # reminder read as a "169% reply rate", which is nonsense on a dashboard.
    # Numerator is also capped at the denominator: a customer may write in about
    # something unrelated to the reminder we counted, and no reply rate is >100%.
    # Both numbers must come from the same population: reminders dispatched in
    # the window. Filtering the numerator by replied_at and the denominator by
    # sent_at mixes two different sets (a reminder sent in September and
    # answered in October lands in the numerator but not the denominator), which
    # is how the ratio came out as "264 of 187".
    k["reminders_dispatched"] = db.scalar(
        "SELECT COUNT(*) FROM reminders WHERE shop_id=? AND sent_at>=? AND status<>'cancelled'",
        (shop_id, since + " 00:00:00"), 0)
    k["reminders_replied"] = db.scalar(
        "SELECT COUNT(*) FROM reminders WHERE shop_id=? AND sent_at>=? "
        "AND status<>'cancelled' AND replied_at IS NOT NULL",
        (shop_id, since + " 00:00:00"), 0)
    k["reply_rate"] = round(100.0 * min(k["reminders_replied"], k["reminders_dispatched"])
                            / max(k["reminders_dispatched"], 1), 1)
    k["conversion_rate"] = round(100.0 * k["conversions"] / max(k["reminders_sent"], 1), 1)
    k["retention_score"] = round(100.0 * k["active_customers"] / max(k["customers"], 1), 1)
    k["due_today"] = db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status='scheduled' AND due_on<=?",
                               (shop_id, db.today()), 0)
    k["loyalty_points_outstanding"] = db.scalar(
        "SELECT IFNULL(SUM(loyalty_points),0) FROM customers WHERE shop_id=?", (shop_id,), 0)
    k["avg_cycle_days"] = round(db.scalar(
        "SELECT AVG(cycle_days) FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.shop_id=?", (shop_id,), 30) or 30, 1)
    return k


def daily_series(shop_id: int | None, days: int = 30, table: str = "sales") -> list[dict]:
    since = db.days_from_today(-days)
    if table == "sales":
        sql = "SELECT sale_date day, IFNULL(SUM(total),0) value, COUNT(*) n FROM sales WHERE sale_date>=?"
        params: tuple = (since,)
        if shop_id:
            sql = "SELECT sale_date day, IFNULL(SUM(total),0) value, COUNT(*) n FROM sales " \
                  "WHERE shop_id=? AND sale_date>=? GROUP BY sale_date ORDER BY day"
            params = (shop_id, since)
        else:
            sql += " GROUP BY sale_date ORDER BY day"
    else:
        sql = "SELECT substr(created_at,1,10) day, COUNT(*) value, COUNT(*) n FROM messages " \
              "WHERE created_at>=? AND direction='out'"
        params = (since + " 00:00:00",)
        if shop_id:
            sql = "SELECT substr(created_at,1,10) day, COUNT(*) value, COUNT(*) n FROM messages " \
                  "WHERE shop_id=? AND created_at>=? AND direction='out' GROUP BY day ORDER BY day"
            params = (shop_id, since + " 00:00:00")
        else:
            sql += " GROUP BY day ORDER BY day"
    rows = db.q(sql, params)
    idx = {r["day"]: r for r in rows}
    base = pdate(since)
    return [{"day": d(base + timedelta(days=i)),
             "value": float(idx[d(base + timedelta(days=i))]["value"]) if d(base + timedelta(days=i)) in idx else 0.0,
             "n": int(idx[d(base + timedelta(days=i))]["n"]) if d(base + timedelta(days=i)) in idx else 0}
            for i in range(days + 1)]
