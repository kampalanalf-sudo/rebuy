"""Shop-owner API: everything a shop sees and does."""
from __future__ import annotations

import hashlib
import json
from datetime import timedelta
import re
import secrets
import urllib.parse

from . import auth, config, db, engine, imports, providers
from .httpd import ApiError, route

CHANNELS = ("whatsapp", "sms", "email")
_SECRET_KEYS = ("token", "password", "secret", "auth_token")


def shop_of(ctx) -> dict:
    shop = db.q1("SELECT * FROM shops WHERE id=?", (ctx.shop_id,))
    if not shop:
        raise ApiError("shop not found", 404)
    return shop


def public_shop(shop: dict) -> dict:
    s = dict(shop)
    return s


def mask(cfg: dict) -> dict:
    out = {}
    for k, v in (cfg or {}).items():
        if k in ("token", "password", "secret", "auth_token") and isinstance(v, str) and v:
            out[k] = v[:3] + "•" * 10 + (v[-3:] if len(v) > 8 else "")
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------- session
def _client_ip(ctx) -> str:
    fwd = ctx.headers.get("x-forwarded-for") or ctx.headers.get("X-Forwarded-For") or ""
    return (fwd.split(",")[0].strip() if fwd else (ctx.headers.get("x-real-ip") or "")).strip()


def _ua(ctx) -> str:
    return (ctx.headers.get("user-agent") or ctx.headers.get("User-Agent") or "")[:200]


def _session_response(payload: dict, token: str, status: int = 200, extra: dict | None = None):
    body = json.dumps(payload, default=str).encode()
    headers = {"content-type": "application/json", "set-cookie": auth.session_cookie(token),
               "content-length": str(len(body))}
    headers.update(extra or {})
    return status, headers, body


@route("POST", "/api/auth/login", "none")
def login(ctx):
    email, password = auth.clean_email(ctx.b("email", "")), ctx.b("password", "")
    if not email or not password:
        raise ApiError("Email and password are required", 400, "missing_fields")
    if auth.is_locked(email):
        raise ApiError("Too many attempts — try again in 15 minutes", 429, "locked")
    res = auth.login(email, password, ip=_client_ip(ctx), ua=_ua(ctx))
    if not res:
        raise ApiError("That email and password do not match", 401, "bad_credentials")
    db.audit(res["user"]["email"], "auth.login", res["user"].get("shop_id"))
    return _session_response({"ok": True, "user": res["user"], "shop": res["shop"]}, res["token"])


@route("POST", "/api/auth/logout", "none")
def logout(ctx):
    m = re.search(r"rebuy_session=([^;]+)", ctx.headers.get("cookie", ""))
    auth.logout(m.group(1) if m else None)
    body = json.dumps({"ok": True}).encode()
    return 200, {"content-type": "application/json", "set-cookie": auth.clear_cookie(),
                 "content-length": str(len(body))}, body


@route("POST", "/api/auth/logout-all", "user")
def logout_all(ctx):
    auth.logout_everywhere(ctx.user["id"])
    db.audit(ctx.user["email"], "auth.logout_all", ctx.user.get("shop_id"))
    body = json.dumps({"ok": True}).encode()
    return 200, {"content-type": "application/json", "set-cookie": auth.clear_cookie(),
                 "content-length": str(len(body))}, body


@route("GET", "/api/auth/me", "none")
def me(ctx):
    if not ctx.user:
        return {"authenticated": False, "google_enabled": config.GOOGLE_ENABLED,
                "signup_open": config.ALLOW_SIGNUP}
    u = db.q1("SELECT id, name, email, role, shop_id, password_hash, google_sub, created_at, last_login "
              "FROM users WHERE id=?", (ctx.user["id"],))
    shop = db.q1("SELECT * FROM shops WHERE id=?", (ctx.user["shop_id"],)) if ctx.user["shop_id"] else None
    user = auth.public_user(u)
    user["created_at"] = u.get("created_at")
    user["last_login"] = u.get("last_login")
    return {"authenticated": True, "user": user, "shop": shop,
            "google_enabled": config.GOOGLE_ENABLED, "signup_open": config.ALLOW_SIGNUP}


@route("GET", "/api/auth/sessions", "user")
def sessions_list(ctx):
    return {"sessions": auth.list_sessions(ctx.user["id"])}


@route("POST", "/api/auth/sessions/revoke", "user")
def session_revoke(ctx):
    sid = (ctx.b("id") or "").strip()
    if not sid:
        raise ApiError("session id is required")
    auth.revoke_session(ctx.user["id"], sid)
    return {"ok": True, "sessions": auth.list_sessions(ctx.user["id"])}


@route("POST", "/api/auth/password", "user")
def change_password(ctx):
    auth.change_password(ctx.user["id"], ctx.b("current_password", ""), ctx.b("new_password", ""))
    db.audit(ctx.user["email"], "auth.password_changed", ctx.user.get("shop_id"))
    # every session was revoked, so sign this browser back in
    u = db.q1("SELECT * FROM users WHERE id=?", (ctx.user["id"],))
    token = auth.start_session(u, _client_ip(ctx), _ua(ctx), "password")
    return _session_response({"ok": True, "user": auth.public_user(u)}, token)


# ------------------------------------------------------------ password reset
@route("POST", "/api/auth/forgot", "none")
def forgot_password(ctx):
    """Always answers the same way, whether or not the address exists — otherwise
    the endpoint becomes a free account-enumeration oracle."""
    email = auth.clean_email(ctx.b("email", ""))
    u = auth.find_user(email) if email else None
    token = ""
    if u and u.get("password_hash"):
        token = secrets.token_urlsafe(32)
        th = hashlib.sha256(token.encode()).hexdigest()
        db.x("DELETE FROM password_resets WHERE user_id=?", (u["id"],))
        db.insert("password_resets", {"token_hash": th, "user_id": u["id"],
                                      "expires_at": (db.utcnow() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
                                      "created_at": db.now_iso()})
        link = f"{config.PUBLIC_URL or ''}/#/reset?token={token}"
        driver, cfg = providers.credentials_for(None, "email")
        try:
            providers.send_email(email, "Reset your Rebuy password",
                                 f"Someone asked to reset the password for {email} on Rebuy.\n\n"
                                 f"Open this link within the hour to choose a new one:\n{link}\n\n"
                                 f"If it was not you, ignore this email — your password stays as it is.",
                                 cfg, driver)
        except Exception:  # noqa: BLE001 — never reveal delivery problems to the caller
            pass
        db.audit(email, "auth.reset_requested", u.get("shop_id"))
    # deliberately identical for known and unknown addresses
    return {"ok": True, "message": "If that email has an account, a reset link is on its way."}


@route("POST", "/api/auth/reset", "none")
def reset_password(ctx):
    token = (ctx.b("token") or "").strip()
    new = ctx.b("password", "")
    if not token:
        raise ApiError("This reset link is incomplete", 400, "bad_token")
    th = hashlib.sha256(token.encode()).hexdigest()
    row = db.q1("SELECT * FROM password_resets WHERE token_hash=?", (th,))
    if not row or row.get("used_at") or row["expires_at"] < db.now_iso():
        raise ApiError("This reset link has expired — request a new one", 400, "expired_token")
    try:
        auth.set_password(row["user_id"], new)
    except auth.AuthError as e:
        raise ApiError(e.message, e.status, e.code)
    db.x("UPDATE password_resets SET used_at=? WHERE token_hash=?", (db.now_iso(), th))
    auth.logout_everywhere(row["user_id"])
    u = db.q1("SELECT * FROM users WHERE id=?", (row["user_id"],))
    db.audit(u["email"], "auth.reset_completed", u.get("shop_id"))
    token2 = auth.start_session(u, _client_ip(ctx), _ua(ctx), "password")
    shop = db.q1("SELECT * FROM shops WHERE id=?", (u["shop_id"],)) if u["shop_id"] else None
    return _session_response({"ok": True, "user": auth.public_user(u), "shop": shop}, token2)


# ------------------------------------------------------------------ google oauth
@route("GET", "/api/auth/google/start", "none")
def google_start(ctx):
    base = config.PUBLIC_URL or ctx.b("origin", "")
    want = ctx.q("next", "/dashboard")
    if not want.startswith("/"):
        want = "/dashboard"
    try:
        out = auth.google_authorize_url(auth_redirect_uri(ctx), want)
    except auth.AuthError as e:
        raise ApiError(e.message, e.status, e.code)
    body = json.dumps({"url": out["url"]}).encode()
    cookie = (f"rebuy_oauth={out['cookie']}; Path=/api/auth/google; HttpOnly; SameSite=Lax; "
              f"Max-Age=600" + ("; Secure" if config.COOKIE_SECURE else ""))
    _trace_call("start", f"next={want} redirect_uri={auth_redirect_uri(ctx)} "
                         f"secure={config.COOKIE_SECURE}")
    return 200, {"content-type": "application/json", "set-cookie": cookie,
                 "content-length": str(len(body))}, body


def auth_redirect_uri(ctx) -> str:
    if config.PUBLIC_URL:
        return config.google_redirect_uri()
    proto = (ctx.headers.get("x-forwarded-proto") or "http").split(",")[0].strip()
    host = ctx.headers.get("x-forwarded-host") or ctx.headers.get("host") or "localhost:8000"
    return f"{proto}://{host}/api/auth/google/callback"


def _trace_call(step: str, detail: str = "") -> None:
    """Record what the OAuth callback saw. Best-effort: never breaks the flow."""
    try:
        db.audit("oauth", "google.callback", None, "oauth", step, detail)
    except Exception:  # noqa: BLE001
        pass


@route("GET", "/api/auth/google/callback", "none")
def google_callback(ctx):
    """Google redirects the browser here, so answer with a redirect rather than JSON."""
    code = ctx.q("code", "")
    state = ctx.q("state", "")
    err = ctx.q("error", "")
    cookie = ctx.headers.get("cookie", "") or ""
    has_sess = "rebuy_session=" in cookie
    has_oauth = "rebuy_oauth=" in cookie
    _trace_call("enter", f"code={len(code)} state={len(state)} err={err[:40]!r} "
                         f"cookie_oauth={has_oauth} cookie_session={has_sess} "
                         f"ua={(ctx.headers.get('user-agent') or '')[:80]!r}")
    if err:
        _trace_call("google_error", err[:200])
        return _redirect("/#/login?error=" + urllib.parse.quote(err), clear_oauth=True)
    m = re.search(r"rebuy_oauth=([^;]+)", cookie)
    if not code:
        # The user pressed Cancel/Deny on Google's screen.
        _trace_call("no_code", "google returned no authorization code")
        return _redirect("/#/login?error=google_cancelled", clear_oauth=True)
    if not m:
        # A code arrived but our state cookie did not. That is not a cancellation:
        # it happens when the browser drops the cookie on the way back (cross-site
        # privacy modes, a different host than the one that started the flow, or an
        # expired 10-minute window). Say so, because "cancelled" sends the user
        # looking for a button they never pressed.
        _trace_call("state_lost", "code present but no rebuy_oauth cookie")
        return _redirect("/#/login?error=google_state_lost", clear_oauth=True)
    try:
        out = auth.google_exchange(code, auth_redirect_uri(ctx), m.group(1), state)
        user = auth.provision_google_user(out["claims"])
        token = auth.start_session(user, _client_ip(ctx), _ua(ctx), "google")
    except auth.AuthError as e:
        _trace_call("auth_error", f"{e.code}: {str(e)[:200]}")
        return _redirect("/#/login?error=" + urllib.parse.quote(e.code), clear_oauth=True)
    except Exception as e:  # noqa: BLE001
        _trace_call("crash", f"{type(e).__name__}: {str(e)[:250]}")
        raise
    nxt = out["next"] if out["next"].startswith("/") else "/dashboard"
    _trace_call("success", f"user={user['email']} role={user.get('role')} "
                           f"cookie_set=yes next={nxt}")
    db.audit(user["email"], "auth.login.google", user.get("shop_id"))
    return _redirect("/#" + nxt.lstrip("/"), token=token)


def _redirect(path: str, token: str = "", clear_oauth: bool = False):
    """302 with AT MOST ONE Set-Cookie header.

    Vercel's proxy collapses repeated Set-Cookie headers into a single one, and
    only one cookie survives the trip (a well-known platform behaviour). Since
    losing the session cookie means the user is silently bounced back to the
    login page, every response here sets exactly one cookie and no more. The
    short-lived rebuy_oauth cookie therefore is NOT cleared here; it is cleared
    when the flow next starts, and it expires on its own after 10 minutes.
    """
    headers = {"location": path, "cache-control": "no-store"}
    if token:
        headers["set-cookie"] = auth.session_cookie(token)
    elif clear_oauth:
        headers["set-cookie"] = "rebuy_oauth=; Path=/api/auth/google; Max-Age=0"
    return 302, headers, b""


@route("POST", "/api/signup", "none")
def signup(ctx):
    """Self-serve onboarding. Gated by REBUY_ALLOW_SIGNUP in production."""
    if not config.ALLOW_SIGNUP and not ctx.user:
        raise ApiError("Self-serve signup is closed — contact sales to get set up", 403, "signup_closed")
    name = (ctx.b("shop_name") or "").strip()
    owner = (ctx.b("owner_name") or "").strip()
    email = auth.clean_email(ctx.b("email", ""))
    password = ctx.b("password") or ""
    phone = (ctx.b("phone") or "").strip()
    if not name or not email or not password:
        raise ApiError("Shop name, your name, email and password are required", 400, "missing_fields")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "shop"
    base, n = slug, 2
    while db.q1("SELECT id FROM shops WHERE slug=?", (slug,)):
        slug = f"{base}-{n}"
        n += 1
    plan = ctx.b("plan") or "starter"
    price = {"starter": 49000, "growth": 129000, "scale": 299000}.get(plan, 49000)
    try:
        auth.validate_password(password)
        auth.assert_email_allowed(email)
    except auth.AuthError as e:
        raise ApiError(e.message, e.status, e.code)
    if auth.find_user(email):
        raise ApiError("An account with that email already exists", 409, "email_taken")
    shop_id = db.insert("shops", {
        "name": name[:120], "slug": slug, "owner_name": owner[:80], "owner_email": email,
        "phone": phone, "whatsapp_number": phone, "sender_name": name[:40],
        "city": ctx.b("city") or "Kampala", "country": ctx.b("country") or "Uganda",
        "sector": ctx.b("sector") or "Retail", "plan": plan, "status": "trial",
        "monthly_price": price, "currency": ctx.b("currency") or "UGX",
        "onboarded_at": db.now_iso(), "trial_ends_at": db.days_from_today(14), "created_at": db.now_iso(),
    })
    user = auth.provision_password_user(shop_id, "owner", owner or name, email, password)
    engine.seed_default_templates()
    db.audit(email, "shop.signup", shop_id, "shop", shop_id, f"{name} ({plan})")
    token = auth.start_session(user, _client_ip(ctx), _ua(ctx), "password")
    shop = db.q1("SELECT * FROM shops WHERE id=?", (shop_id,))
    return _session_response({"ok": True, "shop_id": shop_id, "user": auth.public_user(user), "shop": shop,
                              "next": "/#/onboarding"}, token)


# --------------------------------------------------------------------- profile
# --------------------------------------------------------------------- profile
@route("GET", "/api/shop", "shop")
def get_shop(ctx):
    return {"shop": public_shop(shop_of(ctx)), "counts": {
        "customers": db.scalar("SELECT COUNT(*) FROM customers WHERE shop_id=?", (ctx.shop_id,), 0),
        "products": db.scalar("SELECT COUNT(*) FROM products WHERE shop_id=?", (ctx.shop_id,), 0),
        "sales": db.scalar("SELECT COUNT(*) FROM sales WHERE shop_id=?", (ctx.shop_id,), 0),
        "reminders_due": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status='scheduled' AND due_on<=?",
                                   (ctx.shop_id, db.today()), 0),
    }}


@route("PATCH", "/api/shop", "shop")
def patch_shop(ctx):
    allowed = ["name", "owner_name", "owner_email", "phone", "city", "country", "sector", "currency",
               "sender_name", "whatsapp_number", "auto_reminders", "reminder_lead_days", "max_followups",
               "max_msgs_per_customer_month", "quiet_start", "quiet_end", "loyalty_enabled", "loyalty_rule", "language"]
    patch = {k: ctx.b(k) for k in allowed if ctx.b(k) is not None}
    if not patch:
        raise ApiError("nothing to update")
    db.update("shops", ctx.shop_id, patch)
    db.audit(ctx.user["email"], "shop.updated", ctx.shop_id, "shop", ctx.shop_id, json.dumps(patch)[:300])
    return {"ok": True, "shop": public_shop(shop_of(ctx))}


# -------------------------------------------------------------------- dashboard
@route("GET", "/api/dashboard", "shop")
def dashboard(ctx):
    sid = ctx.shop_id
    days = ctx.qi("days", 30)
    k = engine.shop_kpis(sid, days)
    shop = shop_of(ctx)
    return {
        "kpis": k,
        "shop": {"name": shop["name"], "plan": shop["plan"], "status": shop["status"],
                 "currency": shop["currency"], "auto_reminders": shop["auto_reminders"],
                 "whatsapp_number": shop["whatsapp_number"]},
        "revenue_series": engine.daily_series(sid, days, "sales"),
        "message_series": engine.daily_series(sid, days, "messages"),
        "risk_mix": db.q("SELECT risk_band band, COUNT(*) n FROM customers WHERE shop_id=? GROUP BY risk_band", (sid,)),
        "channel_mix": db.q("""SELECT channel, COUNT(*) n, SUM(status='failed') failed
                               FROM messages WHERE shop_id=? AND direction='out'
                                 AND created_at>=? GROUP BY channel""", (sid, db.days_from_today(-days) + " 00:00:00")),
        "top_products": db.q("""SELECT si.product_name name, COUNT(*) purchases, IFNULL(SUM(si.line_total),0) revenue,
                                       AVG(si.cycle_days) cycle
                                FROM sale_items si JOIN sales s ON s.id=si.sale_id
                                WHERE s.shop_id=? AND s.sale_date>=?
                                GROUP BY si.product_name ORDER BY revenue DESC LIMIT 8""",
                             (sid, db.days_from_today(-90))),
        "upcoming": db.q("""SELECT r.id, r.due_on, r.kind, r.channel, r.status, r.product_name,
                                   c.name customer, c.phone, c.risk_band
                            FROM reminders r JOIN customers c ON c.id=r.customer_id
                            WHERE r.shop_id=? AND r.status='scheduled'
                            ORDER BY r.due_on LIMIT 12""", (sid,)),
        "needs_action": db.q("""SELECT r.id, r.kind, r.due_on, r.product_name, r.status, c.name customer, c.phone,
                                       m.body last_reply, m.intent, m.created_at replied_at
                                FROM reminders r JOIN customers c ON c.id=r.customer_id
                                LEFT JOIN messages m ON m.reminder_id=r.id AND m.direction='in'
                                WHERE r.shop_id=? AND r.status IN ('replied','delivered','sent')
                                ORDER BY r.sent_at DESC LIMIT 12""", (sid,)),
        "at_risk": db.q("""SELECT id, name, phone, risk_band, last_purchase_at, total_spent, visit_count
                           FROM customers WHERE shop_id=? AND risk_band IN ('at_risk','lost','overdue')
                           ORDER BY total_spent DESC LIMIT 10""", (sid,)),
        "recent_sales": db.q("""SELECT s.id, s.receipt_no, s.sale_date, s.total, c.name customer, s.source
                                FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                                WHERE s.shop_id=? ORDER BY s.sale_date DESC, s.id DESC LIMIT 8""", (sid,)),
        "onboarding": onboarding_state(sid),
    }


def onboarding_state(sid: int) -> list[dict]:
    steps = [
        ("add_products", "Add your products & services with restock cycles",
         db.scalar("SELECT COUNT(*) FROM products WHERE shop_id=?", (sid,), 0) > 0),
        ("add_customers", "Add customers (or import your sales book)",
         db.scalar("SELECT COUNT(*) FROM customers WHERE shop_id=?", (sid,), 0) > 0),
        ("record_sale", "Record your first sale",
         db.scalar("SELECT COUNT(*) FROM sales WHERE shop_id=?", (sid,), 0) > 0),
        ("connect_channel", "Connect WhatsApp / SMS / email",
         db.scalar("SELECT COUNT(*) FROM credentials WHERE shop_id=? AND enabled=1", (sid,), 0) > 0),
        ("first_reminder", "Send your first automatic reminder",
         db.scalar("SELECT COUNT(*) FROM messages WHERE shop_id=? AND direction='out' AND status IN ('sent','delivered')",
                   (sid,), 0) > 0),
        ("loyalty", "Turn on loyalty rewards",
         bool(db.scalar("SELECT loyalty_enabled FROM shops WHERE id=?", (sid,), 0))),
    ]
    return [{"key": k, "label": label, "done": bool(done)} for k, label, done in steps]


@route("GET", "/api/onboarding", "shop")
def onboarding(ctx):
    return {"steps": onboarding_state(ctx.shop_id)}


# --------------------------------------------------------------------- customers
@route("GET", "/api/customers", "shop")
def list_customers(ctx):
    sid, page, per = ctx.shop_id, max(1, ctx.qi("page", 1)), min(200, max(10, ctx.qi("per", 25)))
    search = (ctx.q("search") or "").strip()
    risk = ctx.q("risk") or ""
    tag = ctx.q("tag") or ""
    where, params = ["c.shop_id=?"], [sid]
    if search:
        where.append("(c.name LIKE ? OR c.phone LIKE ? OR c.email LIKE ? OR c.area LIKE ?)")
        params += [f"%{search}%"] * 4
    if risk:
        where.append("c.risk_band=?")
        params.append(risk)
    if tag:
        where.append("c.tags LIKE ?")
        params.append(f"%{tag}%")
    sort = {"recent": "c.last_purchase_at DESC", "spend": "c.total_spent DESC",
            "visits": "c.visit_count DESC", "name": "c.name ASC", "risk": "c.risk_band ASC"}.get(ctx.q("sort") or "recent", "c.last_purchase_at DESC")
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM customers c WHERE {w}", params, 0)
    rows = db.q(f"""SELECT c.*, (SELECT COUNT(*) FROM reminders r WHERE r.customer_id=c.id AND r.status='scheduled') pending_reminders,
                           (SELECT MAX(m.created_at) FROM messages m WHERE m.customer_id=c.id) last_message
                    FROM customers c WHERE {w} ORDER BY {sort} LIMIT ? OFFSET ?""",
                params + [per, (page - 1) * per])
    return {"rows": rows, "total": total, "page": page, "per": per,
            "pages": max(1, (total + per - 1) // per),
            "bands": db.q("SELECT risk_band band, COUNT(*) n FROM customers WHERE shop_id=? GROUP BY risk_band", (sid,))}


@route("POST", "/api/customers", "shop")
def create_customer(ctx):
    c = imports.upsert_customer(ctx.shop_id, ctx.b("name", ""), ctx.b("phone", ""), ctx.b("email", ""),
                                ctx.b("area", ""), "manual")
    if not c:
        raise ApiError("a name, phone or email is required")
    patch = {}
    for k in ("tags", "notes", "birthday", "language"):
        if ctx.b(k) is not None:
            patch[k] = ctx.b(k)
    if ctx.b("opted_in") is not None:
        patch["opted_in"] = 1 if ctx.b("opted_in") else 0
    if patch:
        db.update("customers", c["id"], dict(patch, updated_at=db.now_iso()))
    db.audit(ctx.user["email"], "customer.created", ctx.shop_id, "customer", c["id"], c["name"])
    return {"ok": True, "customer": db.q1("SELECT * FROM customers WHERE id=?", (c["id"],))}


@route("GET", "/api/customers/:cid", "shop")
def get_customer(ctx):
    cid = int(ctx.params["cid"])
    c = db.q1("SELECT * FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    if not c:
        raise ApiError("customer not found", 404)
    sales = db.q("""SELECT s.*, (SELECT COUNT(*) FROM sale_items si WHERE si.sale_id=s.id) items
                    FROM sales s WHERE s.customer_id=? ORDER BY s.sale_date DESC LIMIT 25""", (cid,))
    for s in sales:
        s["lines"] = db.q("SELECT * FROM sale_items WHERE sale_id=?", (s["id"],))
    msgs = db.q("SELECT * FROM messages WHERE customer_id=? ORDER BY created_at DESC LIMIT 60", (cid,))
    rems = db.q("SELECT * FROM reminders WHERE customer_id=? ORDER BY due_on DESC LIMIT 30", (cid,))
    coupons = db.q("SELECT * FROM coupons WHERE customer_id=? ORDER BY created_at DESC LIMIT 10", (cid,))
    cycle = db.scalar("SELECT AVG(si.cycle_days) FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.customer_id=?",
                      (cid,), None)
    intervals = db.q("""SELECT sale_date FROM sales WHERE customer_id=? ORDER BY sale_date""", (cid,))
    gaps = [engine.diff_days(intervals[i + 1]["sale_date"], intervals[i]["sale_date"]) for i in range(len(intervals) - 1)]
    return {"customer": c, "sales": sales, "messages": msgs, "reminders": rems, "coupons": coupons,
            "stats": {"avg_cycle_days": round(float(cycle or 30), 1),
                      "avg_gap_days": round(sum(gaps) / len(gaps), 1) if gaps else None,
                      "avg_basket": round(c["total_spent"] / c["visit_count"], 2) if c["visit_count"] else 0,
                      "days_since_last": engine.diff_days(db.today(), c["last_purchase_at"] or db.today()) if c["last_purchase_at"] else None,
                      "next_best_action": next_best_action(c, cycle)},
            "loyalty": db.q("SELECT * FROM loyalty_events WHERE customer_id=? ORDER BY created_at DESC LIMIT 20", (cid,))}


def next_best_action(c: dict, cycle) -> str:
    band = c.get("risk_band")
    cycle = float(cycle or 30)
    if band == "lost":
        return "Win-back campaign with a strong coupon — they have not bought in months."
    if band == "at_risk":
        return "Send a win-back nudge this week; they are past their normal buying cycle."
    if band == "overdue":
        return "Reminder is late — send a follow-up with a small coupon."
    if band == "due":
        return f"Due to reorder around now (cycle ≈ {cycle:.0f} days). Send the replenishment reminder."
    if band == "new":
        return "Welcome them and set expectations; ask for their WhatsApp to enable reminders."
    return f"Healthy. Next natural reorder in ~{max(1, cycle - (engine.diff_days(db.today(), c['last_purchase_at'] or db.today()))):.0f} days."


@route("PATCH", "/api/customers/:cid", "shop")
def patch_customer(ctx):
    cid = int(ctx.params["cid"])
    if not db.q1("SELECT id FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id)):
        raise ApiError("customer not found", 404)
    allowed = ["name", "phone", "whatsapp", "email", "area", "tags", "notes", "birthday", "language", "opted_in"]
    patch = {k: ctx.b(k) for k in allowed if ctx.b(k) is not None}
    if "opted_in" in patch:
        patch["opted_in"] = 1 if patch["opted_in"] else 0
        patch["opt_out_at"] = None if patch["opted_in"] else db.now_iso()
    patch["updated_at"] = db.now_iso()
    db.update("customers", cid, patch)
    db.audit(ctx.user["email"], "customer.updated", ctx.shop_id, "customer", cid, json.dumps(patch)[:200])
    return {"ok": True, "customer": db.q1("SELECT * FROM customers WHERE id=?", (cid,))}


@route("DELETE", "/api/customers/:cid", "shop")
def delete_customer(ctx):
    cid = int(ctx.params["cid"])
    db.x("DELETE FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    db.audit(ctx.user["email"], "customer.deleted", ctx.shop_id, "customer", cid)
    return {"ok": True}


@route("POST", "/api/customers/:cid/message", "shop")
def message_customer(ctx):
    """Ad-hoc message to one customer (used by the Inbox reply box)."""
    cid = int(ctx.params["cid"])
    c = db.q1("SELECT * FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    if not c:
        raise ApiError("customer not found", 404)
    body = (ctx.b("body") or "").strip()
    channel = ctx.b("channel") or "whatsapp"
    if not body:
        raise ApiError("message body is required")
    if not c.get("opted_in") and channel != "email":
        raise ApiError("this customer has opted out of messages", 409)
    to = {"whatsapp": c.get("whatsapp") or c.get("phone"), "sms": c.get("phone"), "email": c.get("email")}[channel]
    if not to:
        raise ApiError(f"no {channel} address on file for this customer", 409)
    shop = shop_of(ctx)
    driver, cfg = providers.credentials_for(ctx.shop_id, channel)
    mid = db.insert("messages", {
        "shop_id": ctx.shop_id, "customer_id": cid, "direction": "out", "channel": channel, "provider": driver,
        "to_addr": to, "from_addr": shop.get("whatsapp_number") or cfg.get("from_address", ""),
        "subject": ctx.b("subject") or f"Message from {shop['name']}", "body": body,
        "template_key": "manual", "status": "queued", "created_at": db.now_iso(),
    })
    stats = engine.tick(limit=5)
    m = db.q1("SELECT * FROM messages WHERE id=?", (mid,))
    db.audit(ctx.user["email"], "message.sent_manual", ctx.shop_id, "customer", cid, body[:120])
    return {"ok": True, "message": m, "worker": stats}


# ---------------------------------------------------------------------- products
@route("GET", "/api/products", "shop")
def list_products(ctx):
    kind = ctx.q("kind") or ""
    search = ctx.q("search") or ""
    where, params = ["shop_id=?"], [ctx.shop_id]
    if kind in ("goods", "service"):
        where.append("kind=?")
        params.append(kind)
    if search:
        where.append("(name LIKE ? OR category LIKE ? OR sku LIKE ?)")
        params += [f"%{search}%"] * 3
    w = " AND ".join(where)
    rows = db.q(f"""SELECT p.*, (SELECT COUNT(*) FROM sale_items si WHERE si.product_id=p.id) sold_count,
                           (SELECT IFNULL(SUM(si.line_total),0) FROM sale_items si WHERE si.product_id=p.id) revenue
                    FROM products p WHERE {w} ORDER BY p.category, p.name LIMIT 500""", params)
    return {"rows": rows, "categories": db.q(
        "SELECT category, COUNT(*) n FROM products WHERE shop_id=? GROUP BY category ORDER BY n DESC", (ctx.shop_id,))}


@route("POST", "/api/products", "shop")
def create_product(ctx):
    name = (ctx.b("name") or "").strip()
    if not name:
        raise ApiError("product name is required")
    kind = ctx.b("kind") or imports.guess_kind(name, ctx.b("category", ""))
    cat = ctx.b("category") or imports._guess_category(name)
    cycle = ctx.bi("cycle_days", 0) or engine.CATEGORY_DEFAULT_CYCLES.get(cat, 90 if kind == "service" else 30)
    pid = db.insert("products", {
        "shop_id": ctx.shop_id, "name": name[:120], "sku": ctx.b("sku", "")[:40], "category": cat[:60],
        "kind": kind, "unit": (ctx.b("unit") or ("session" if kind == "service" else "pc"))[:12],
        "unit_price": float(ctx.b("unit_price", 0) or 0), "cycle_days": int(cycle),
        "baseline_qty": float(ctx.b("baseline_qty", 1) or 1), "active": 1, "created_at": db.now_iso(),
    })
    db.audit(ctx.user["email"], "product.created", ctx.shop_id, "product", pid, f"{name} ({cycle}d)")
    return {"ok": True, "product": db.q1("SELECT * FROM products WHERE id=?", (pid,))}


@route("PATCH", "/api/products/:pid", "shop")
def patch_product(ctx):
    pid = int(ctx.params["pid"])
    if not db.q1("SELECT id FROM products WHERE id=? AND shop_id=?", (pid, ctx.shop_id)):
        raise ApiError("product not found", 404)
    allowed = ["name", "sku", "category", "kind", "unit", "unit_price", "cycle_days", "baseline_qty", "active"]
    patch = {k: ctx.b(k) for k in allowed if ctx.b(k) is not None}
    db.update("products", pid, patch)
    return {"ok": True, "product": db.q1("SELECT * FROM products WHERE id=?", (pid,))}


@route("DELETE", "/api/products/:pid", "shop")
def delete_product(ctx):
    db.x("DELETE FROM products WHERE id=? AND shop_id=?", (int(ctx.params["pid"]), ctx.shop_id))
    return {"ok": True}


# ------------------------------------------------------------------------- sales
@route("GET", "/api/sales", "shop")
def list_sales(ctx):
    sid = ctx.shop_id
    page, per = max(1, ctx.qi("page", 1)), min(100, max(10, ctx.qi("per", 25)))
    where, params = ["s.shop_id=?"], [sid]
    if ctx.q("from"):
        where.append("s.sale_date>=?")
        params.append(ctx.q("from"))
    if ctx.q("to"):
        where.append("s.sale_date<=?")
        params.append(ctx.q("to"))
    if ctx.q("customer"):
        where.append("s.customer_id=?")
        params.append(int(ctx.q("customer")))
    if ctx.q("source"):
        where.append("s.source=?")
        params.append(ctx.q("source"))
    if ctx.q("search"):
        where.append("(s.receipt_no LIKE ? OR c.name LIKE ? OR c.phone LIKE ?)")
        params += [f"%{ctx.q('search')}%"] * 3
    w = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE {w}", params, 0)
    rows = db.q(f"""SELECT s.*, c.name customer_name, c.phone customer_phone,
                           (SELECT COUNT(*) FROM sale_items si WHERE si.sale_id=s.id) item_count,
                           (SELECT group_concat(si.product_name, ', ') FROM sale_items si WHERE si.sale_id=s.id) items
                    FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                    WHERE {w} ORDER BY s.sale_date DESC, s.id DESC LIMIT ? OFFSET ?""",
                params + [per, (page - 1) * per])
    summary = db.q1(f"""SELECT COUNT(*) n, IFNULL(SUM(s.total),0) revenue, IFNULL(AVG(s.total),0) basket
                        FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE {w}""", params)
    return {"rows": rows, "total": total, "page": page, "per": per,
            "pages": max(1, (total + per - 1) // per), "summary": summary}


@route("POST", "/api/sales", "shop")
def create_sale(ctx):
    """Record a new sale. Accepts a customer object (or id) plus an items array."""
    items = ctx.b("items") or []
    if not items:
        raise ApiError("add at least one item")
    cid = ctx.bi("customer_id", 0)
    if not cid:
        c = imports.upsert_customer(ctx.shop_id, ctx.b("customer_name", ""), ctx.b("customer_phone", ""),
                                    ctx.b("customer_email", ""), ctx.b("customer_area", ""), "manual")
        if not c:
            raise ApiError("select a customer or enter a name/phone")
        cid = c["id"]
    else:
        if not db.q1("SELECT id FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id)):
            raise ApiError("customer not found", 404)
    sale_date = str(ctx.b("date") or db.today())[:10]
    total = 0.0
    sale_id = db.insert("sales", {
        "shop_id": ctx.shop_id, "receipt_no": (ctx.b("receipt_no") or f"R{db.utcnow():%Y%m%d%H%M%S}")[:40],
        "customer_id": cid, "sale_date": sale_date, "sale_time": db.utcnow().strftime("%H:%M"),
        "total": 0, "currency": shop_of(ctx)["currency"],
        "payment_method": ctx.b("payment_method") or "cash", "channel": ctx.b("channel") or "in_store",
        "staff_name": ctx.b("staff_name") or ctx.user.get("name", ""), "source": ctx.b("source") or "manual",
        "notes": ctx.b("notes", "")[:300], "created_at": db.now_iso(),
    })
    for it in items:
        name = (it.get("product_name") or it.get("name") or "").strip()
        if not name:
            continue
        qty = float(it.get("qty", 1) or 1)
        price = float(it.get("unit_price", 0) or 0)
        unit = (it.get("unit") or "pc")[:12]
        cat = it.get("category") or imports._guess_category(name)
        kind = it.get("kind") or imports.guess_kind(name, cat)
        pid = imports.upsert_product(ctx.shop_id, name, cat, unit, price, it.get("sku", ""), kind,
                                     int(it["cycle_days"]) if it.get("cycle_days") else None)
        prod = db.q1("SELECT * FROM products WHERE id=?", (pid,))
        cycle, rkind = engine.effective_cycle(prod, qty)
        lt = round(price * qty, 2)
        total += lt
        db.insert("sale_items", {
            "sale_id": sale_id, "shop_id": ctx.shop_id, "product_id": pid, "product_name": name[:120],
            "category": cat, "kind": rkind, "qty": qty, "unit": unit, "unit_price": price, "line_total": lt,
            "cycle_days": cycle, "depletes_on": engine.add_days(sale_date, cycle),
        })
    db.update("sales", sale_id, {"total": round(total, 2)})
    # loyalty: 1 point per visit
    shop = shop_of(ctx)
    if shop.get("loyalty_enabled"):
        pts = int(ctx.b("loyalty_points", 1) or 1)
        db.x("UPDATE customers SET loyalty_points = loyalty_points + ? WHERE id=?", (pts, cid))
        db.insert("loyalty_events", {"shop_id": ctx.shop_id, "customer_id": cid, "points": pts,
                                     "reason": "purchase", "ref": str(sale_id), "created_at": db.now_iso()})
    res = engine.on_sale_created(sale_id)
    engine.recompute_customer(ctx.shop_id, cid)
    db.audit(ctx.user["email"], "sale.created", ctx.shop_id, "sale", sale_id,
             f"{total:,.0f} / {len(items)} items / {res['reminders']} reminders planned")
    return {"ok": True, "sale_id": sale_id, "total": round(total, 2), "reminders_planned": res["reminders"],
            "sale": db.q1("SELECT * FROM sales WHERE id=?", (sale_id,)),
            "items": db.q("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,)),
            "customer": db.q1("SELECT * FROM customers WHERE id=?", (cid,))}


@route("GET", "/api/sales/:sid", "shop")
def get_sale(ctx):
    sid = int(ctx.params["sid"])
    s = db.q1("SELECT * FROM sales WHERE id=? AND shop_id=?", (sid, ctx.shop_id))
    if not s:
        raise ApiError("sale not found", 404)
    s["items"] = db.q("SELECT * FROM sale_items WHERE sale_id=?", (sid,))
    s["customer"] = db.q1("SELECT * FROM customers WHERE id=?", (s["customer_id"],)) if s["customer_id"] else None
    s["reminders"] = db.q("SELECT * FROM reminders WHERE sale_item_id IN (SELECT id FROM sale_items WHERE sale_id=?)", (sid,))
    return s


@route("DELETE", "/api/sales/:sid", "shop")
def delete_sale(ctx):
    sid = int(ctx.params["sid"])
    db.x("DELETE FROM sales WHERE id=? AND shop_id=?", (sid, ctx.shop_id))
    db.audit(ctx.user["email"], "sale.deleted", ctx.shop_id, "sale", sid)
    return {"ok": True}


# ----------------------------------------------------------------------- imports
@route("POST", "/api/import/file", "shop")
def import_file(ctx):
    filename = ctx.b("filename") or "upload.csv"
    data = ctx.b("data") or ctx.b("data_url") or ""
    if not data:
        raise ApiError("no file data received")
    res = imports.import_base64_file(ctx.shop_id, filename, data)
    return res


@route("POST", "/api/import/text", "shop")
def import_text(ctx):
    text = ctx.b("text") or ""
    if not text.strip():
        raise ApiError("paste a receipt or an order message first")
    return imports.import_receipt_text(ctx.shop_id, text)


@route("POST", "/api/import/preview", "shop")
def import_preview(ctx):
    """Dry-run so the owner can see the column mapping before committing."""
    data = ctx.b("data") or ""
    filename = ctx.b("filename") or "upload.csv"
    if "," in data and data.split(",")[0].startswith("data:"):
        import base64
        raw = base64.b64decode(data.split(",", 1)[1])
    else:
        raw = data.encode()
    if filename.lower().endswith((".xlsx", ".xlsm")):
        rows = imports.read_xlsx(raw)
    else:
        rows = imports.read_csv(raw.decode("utf-8", errors="replace"))
    mapping, records = imports.rows_to_records(rows)
    return {"mapping": mapping, "headers": rows[0] if rows else [], "rows_found": len(records),
            "sample": records[:5], "groups": len(imports.group_records(records[:200]))}


@route("POST", "/api/receipts/:rid/confirm", "shop")
def confirm_receipt(ctx):
    res = imports.confirm_receipt(ctx.shop_id, int(ctx.params["rid"]), ctx.body if isinstance(ctx.body, dict) else {})
    if not res.get("ok"):
        raise ApiError(res.get("error") or "could not import that receipt")
    return res


@route("GET", "/api/receipts", "shop")
def list_receipts(ctx):
    return {"rows": db.q("""SELECT id, filename, mime, size, parse_status, created_at, raw_text,
                                   substr(parsed_json,1,2000) parsed
                            FROM receipts WHERE shop_id=? ORDER BY id DESC LIMIT 100""", (ctx.shop_id,)),
            "jobs": db.q("SELECT * FROM import_jobs WHERE shop_id=? ORDER BY id DESC LIMIT 25", (ctx.shop_id,))}


@route("GET", "/api/import/jobs", "shop")
def import_jobs(ctx):
    return {"rows": db.q("SELECT * FROM import_jobs WHERE shop_id=? ORDER BY id DESC LIMIT 50", (ctx.shop_id,))}


# --------------------------------------------------------------------- reminders
@route("GET", "/api/reminders", "shop")
def list_reminders(ctx):
    sid = ctx.shop_id
    status = ctx.q("status") or ""
    where, params = ["r.shop_id=?"], [sid]
    if status:
        where.append("r.status IN (%s)" % ",".join("?" * len(status.split(","))))
        params += status.split(",")
    if ctx.q("kind"):
        where.append("r.kind=?")
        params.append(ctx.q("kind"))
    if ctx.q("due") == "today":
        where.append("r.due_on<=?")
        params.append(db.today())
    w = " AND ".join(where)
    rows = db.q(f"""SELECT r.*, c.name customer, c.phone, c.risk_band, c.whatsapp, c.email,
                           cp.code coupon_code, cp.discount_value, cp.discount_type
                    FROM reminders r JOIN customers c ON c.id=r.customer_id
                    LEFT JOIN coupons cp ON cp.id=r.coupon_id
                    WHERE {w} ORDER BY (r.status='scheduled') DESC, r.due_on DESC, r.id DESC LIMIT 200""", params)
    counts = {r["status"]: r["n"] for r in db.q(
        "SELECT status, COUNT(*) n FROM reminders WHERE shop_id=? GROUP BY status", (sid,))}
    return {"rows": rows, "counts": counts,
            "by_kind": db.q("SELECT kind, COUNT(*) n FROM reminders WHERE shop_id=? GROUP BY kind", (sid,))}


@route("POST", "/api/reminders/:rid/send", "shop")
def send_reminder_now(ctx):
    rid = int(ctx.params["rid"])
    r = db.q1("SELECT * FROM reminders WHERE id=? AND shop_id=?", (rid, ctx.shop_id))
    if not r:
        raise ApiError("reminder not found", 404)
    db.update("reminders", rid, {"status": "scheduled", "due_on": db.today()})
    stats = engine.tick(limit=50)
    return {"ok": True, "worker": stats, "reminder": db.q1("SELECT * FROM reminders WHERE id=?", (rid,))}


@route("POST", "/api/reminders/:rid/skip", "shop")
def skip_reminder(ctx):
    rid = int(ctx.params["rid"])
    db.x("UPDATE reminders SET status='cancelled' WHERE id=? AND shop_id=?", (rid, ctx.shop_id))
    db.audit(ctx.user["email"], "reminder.skipped", ctx.shop_id, "reminder", rid)
    return {"ok": True}


@route("POST", "/api/reminders/:rid/snooze", "shop")
def snooze_reminder(ctx):
    rid = int(ctx.params["rid"])
    days = ctx.bi("days", 7)
    db.x("UPDATE reminders SET due_on=?, status='scheduled' WHERE id=? AND shop_id=?",
         (db.days_from_today(days), rid, ctx.shop_id))
    return {"ok": True, "due_on": db.days_from_today(days)}


@route("POST", "/api/reminders/run", "shop")
def run_worker(ctx):
    stats = engine.tick(limit=200)
    db.audit(ctx.user["email"], "worker.manual_tick", ctx.shop_id, detail=json.dumps(stats))
    return {"ok": True, "stats": stats}


# ------------------------------------------------------------------------- inbox
@route("GET", "/api/inbox", "shop")
def inbox(ctx):
    sid = ctx.shop_id
    convo = db.q("""SELECT c.id customer_id, c.name, c.phone, c.risk_band, c.opted_in,
                           MAX(m.created_at) last_at,
                           SUM(m.direction='out') out_count, SUM(m.direction='in') in_count,
                           SUM(m.direction='in' AND m.status='replied' AND m.id > (
                               SELECT IFNULL(MAX(m2.id),0) FROM messages m2 WHERE m2.customer_id=c.id AND m2.direction='out')) unanswered
                    FROM messages m JOIN customers c ON c.id=m.customer_id
                    WHERE m.shop_id=? GROUP BY c.id ORDER BY last_at DESC LIMIT 100""", (sid,))
    unread = db.scalar("""SELECT COUNT(*) FROM messages m WHERE m.shop_id=? AND m.direction='in'
                          AND m.id > (SELECT IFNULL(MAX(m2.id),0) FROM messages m2
                                      WHERE m2.customer_id=m.customer_id AND m2.direction='out')""", (sid,), 0)
    return {"conversations": convo, "unread": unread,
            "channel_counts": db.q("SELECT channel, COUNT(*) n FROM messages WHERE shop_id=? AND direction='in' GROUP BY channel", (sid,))}


@route("GET", "/api/inbox/:cid", "shop")
def conversation(ctx):
    cid = int(ctx.params["cid"])
    c = db.q1("SELECT * FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    if not c:
        raise ApiError("customer not found", 404)
    return {"customer": c, "messages": db.q(
        "SELECT * FROM messages WHERE customer_id=? ORDER BY created_at ASC, id ASC LIMIT 300", (cid,))}


@route("POST", "/api/inbox/simulate-reply", "shop")
def simulate_reply(ctx):
    """Demo helper: pretend the customer answered on WhatsApp."""
    cid = ctx.bi("customer_id", 0)
    text = ctx.b("text") or "Yes please"
    channel = ctx.b("channel") or "whatsapp"
    c = db.q1("SELECT * FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    if not c:
        raise ApiError("customer not found", 404)
    last = db.q1("SELECT * FROM reminders WHERE customer_id=? ORDER BY id DESC LIMIT 1", (cid,))
    res = engine.handle_inbound(ctx.shop_id, cid, channel, c.get("whatsapp") or c.get("phone") or c.get("email", ""),
                                text, provider="demo", reminder_id=last["id"] if last else None)
    return {"ok": True, **res}


@route("GET", "/api/messages", "shop")
def list_messages(ctx):
    sid = ctx.shop_id
    where, params = ["m.shop_id=?"], [sid]
    if ctx.q("channel"):
        where.append("m.channel=?")
        params.append(ctx.q("channel"))
    if ctx.q("direction"):
        where.append("m.direction=?")
        params.append(ctx.q("direction"))
    if ctx.q("status"):
        where.append("m.status=?")
        params.append(ctx.q("status"))
    w = " AND ".join(where)
    limit = min(300, max(20, ctx.qi("limit", 100)))
    rows = db.q(f"""SELECT m.*, c.name customer FROM messages m LEFT JOIN customers c ON c.id=m.customer_id
                    WHERE {w} ORDER BY m.id DESC LIMIT ?""", params + [limit])
    stats = db.q1(f"""SELECT COUNT(*) total, SUM(m.direction='out') out, SUM(m.direction='in') inbound,
                             SUM(m.status='failed') failed, SUM(m.status='queued') queued
                      FROM messages m WHERE {w}""", params)
    return {"rows": rows, "stats": stats}


# ---------------------------------------------------------------------- templates
@route("GET", "/api/templates", "shop")
def list_templates(ctx):
    rows = db.q("""SELECT * FROM templates WHERE shop_id IS NULL OR shop_id=? ORDER BY shop_id DESC, key, channel""",
                (ctx.shop_id,))
    seen, out = set(), []
    for r in rows:  # shop override wins over platform default
        k = (r["key"], r["channel"])
        if k in seen:
            continue
        seen.add(k)
        r["is_custom"] = bool(r["shop_id"])
        out.append(r)
    return {"rows": out, "variables": ["first_name", "product", "qty", "unit", "suggest_qty", "purchase_date",
                                       "depletes_on", "days_since", "shop_name", "shop_phone", "coupon_code",
                                       "discount_text", "coupon_line", "points", "reward_text", "total_text",
                                       "item_summary", "receipt_no", "headline", "body_extra"]}


@route("POST", "/api/templates", "shop")
def save_template(ctx):
    key, channel, body = ctx.b("key"), ctx.b("channel") or "whatsapp", ctx.b("body") or ""
    if not key or not body.strip():
        raise ApiError("key and body are required")
    existing = db.q1("SELECT * FROM templates WHERE IFNULL(shop_id,0)=? AND key=? AND channel=?",
                     (ctx.shop_id, key, channel))
    if existing:
        db.update("templates", existing["id"], {"body": body, "subject": ctx.b("subject") or "",
                                                "active": 1 if ctx.b("active", True) else 0})
        tid = existing["id"]
    else:
        tid = db.insert("templates", {"shop_id": ctx.shop_id, "key": key, "channel": channel, "language": "en",
                                      "subject": ctx.b("subject") or "", "body": body, "active": 1,
                                      "created_at": db.now_iso()})
    db.audit(ctx.user["email"], "template.saved", ctx.shop_id, "template", tid, key)
    return {"ok": True, "template": db.q1("SELECT * FROM templates WHERE id=?", (tid,))}


@route("POST", "/api/templates/preview", "shop")
def preview_template(ctx):
    shop = shop_of(ctx)
    cid = ctx.bi("customer_id", 0)
    c = db.q1("SELECT * FROM customers WHERE id=? AND shop_id=?", (cid, ctx.shop_id)) if cid else None
    if not c:
        c = {"name": "Sarah Nakato", "phone": "+256700000001", "email": "sarah@example.com",
             "loyalty_points": 4, "id": None, "risk_band": "due"}
    item = None
    if ctx.bi("sale_item_id", 0):
        item = db.q1("""SELECT si.*, s.sale_date, s.receipt_no, s.total FROM sale_items si
                        JOIN sales s ON s.id=si.sale_id WHERE si.id=?""", (ctx.bi("sale_item_id"),))
    ctxv = engine.build_context(shop, c, item, None,
                                {"code": "DEMO10", "discount_type": "percent", "discount_value": 10,
                                 "description": "10% off"} if ctx.b("with_coupon") else None,
                                {"headline": ctx.b("headline", "New stock has landed"), "body": ctx.b("body_extra", "")})
    subject, body = engine.render(shop["id"], ctx.b("key", "replenish"), ctx.b("channel", "whatsapp"), ctxv)
    return {"subject": subject, "body": body, "context": ctxv}


@route("DELETE", "/api/templates/:tid", "shop")
def delete_template(ctx):
    db.x("DELETE FROM templates WHERE id=? AND shop_id=?", (int(ctx.params["tid"]), ctx.shop_id))
    return {"ok": True}


# ----------------------------------------------------------------------- coupons
@route("GET", "/api/coupons", "shop")
def list_coupons(ctx):
    return {"rows": db.q("""SELECT cp.*, c.name customer FROM coupons cp LEFT JOIN customers c ON c.id=cp.customer_id
                            WHERE cp.shop_id=? ORDER BY cp.id DESC LIMIT 200""", (ctx.shop_id,)),
            "stats": db.q1("""SELECT COUNT(*) total, SUM(status='used') used, SUM(status='active') active,
                                     IFNULL(SUM(discount_value),0) value_given
                              FROM coupons WHERE shop_id=?""", (ctx.shop_id,))}


@route("POST", "/api/coupons", "shop")
def create_coupon(ctx):
    code = (ctx.b("code") or "").strip().upper() or f"SAVE{db.utcnow():%H%M}"
    if db.q1("SELECT id FROM coupons WHERE shop_id=? AND code=?", (ctx.shop_id, code)):
        raise ApiError("that code already exists", 409)
    cid = db.insert("coupons", {
        "shop_id": ctx.shop_id, "code": code[:24], "description": ctx.b("description", "")[:120],
        "discount_type": ctx.b("discount_type") or "percent", "discount_value": float(ctx.b("discount_value", 10) or 0),
        "min_spend": float(ctx.b("min_spend", 0) or 0), "expires_on": ctx.b("expires_on") or db.days_from_today(30),
        "max_uses": ctx.bi("max_uses", 100), "customer_id": ctx.bi("customer_id", 0) or None,
        "status": "active", "created_at": db.now_iso(),
    })
    db.audit(ctx.user["email"], "coupon.created", ctx.shop_id, "coupon", cid, code)
    return {"ok": True, "coupon": db.q1("SELECT * FROM coupons WHERE id=?", (cid,))}


@route("POST", "/api/coupons/redeem", "shop")
def redeem_coupon(ctx):
    code = (ctx.b("code") or "").strip().upper()
    row = db.q1("SELECT * FROM coupons WHERE shop_id=? AND code=?", (ctx.shop_id, code))
    if not row:
        raise ApiError("unknown coupon code", 404)
    if row["status"] != "active":
        raise ApiError("that coupon is no longer active", 409)
    if row["expires_on"] and row["expires_on"] < db.today():
        raise ApiError("that coupon has expired", 409)
    db.x("UPDATE coupons SET used_count=used_count+1, status=CASE WHEN used_count+1>=max_uses THEN 'used' ELSE status END WHERE id=?",
         (row["id"],))
    return {"ok": True, "coupon": db.q1("SELECT * FROM coupons WHERE id=?", (row["id"],))}


# ---------------------------------------------------------------------- campaigns
SEGMENTS = [
    {"key": "at_risk", "label": "At-risk customers", "icon": "⚠",
     "description": "Bought before but overdue for a reorder — the highest-value group to win back.",
     "segment": {"risk": ["at_risk", "overdue"]}},
    {"key": "lost", "label": "Lost customers", "icon": "✕",
     "description": "No purchase in a long time. A coupon usually restarts the relationship.",
     "segment": {"risk": ["lost"]}},
    {"key": "due", "label": "Due for a reorder", "icon": "⏰",
     "description": "Right at their natural restock point — the easiest yes.",
     "segment": {"risk": ["due"]}},
    {"key": "vip", "label": "VIPs (spend 300k+)", "icon": "★",
     "description": "Your biggest spenders. Keep them feeling special.",
     "segment": {"min_spend": 300000}},
    {"key": "lapsed_60", "label": "No purchase in 60 days", "icon": "◔",
     "description": "Silent for two months but not yet lost.",
     "segment": {"inactive_days": 60}},
    {"key": "new", "label": "New customers", "icon": "✚",
     "description": "First purchase recently — welcome them and set the habit.",
     "segment": {"risk": ["new"]}},
    {"key": "services", "label": "Service customers", "icon": "✂",
     "description": "Everyone who has bought a service — good for re-booking offers.",
     "segment": {"product": ""}},
]


def segment_counts(shop_id: int) -> list[dict]:
    out = []
    for seg in SEGMENTS:
        rule = dict(seg["segment"])
        if seg["key"] == "services":  # services = anyone with a service line item
            w = ("c.opted_in=1 AND EXISTS (SELECT 1 FROM sales s JOIN sale_items si ON si.sale_id=s.id "
                 "WHERE s.customer_id=c.id AND si.kind='service')")
            params: list = []
        else:
            w, params = build_segment_sql(rule)
        n = db.scalar(f"SELECT COUNT(*) FROM customers c WHERE c.shop_id=? AND {w}", [shop_id] + params, 0)
        out.append({**seg, "segment": rule, "count": n})
    return out


@route("GET", "/api/campaigns", "shop")
def list_campaigns(ctx):
    return {"rows": db.q("""SELECT c.*, cp.code coupon_code,
                              (SELECT COUNT(*) FROM messages m WHERE m.campaign_id=c.id) sent
                       FROM campaigns c LEFT JOIN coupons cp ON cp.id=c.coupon_id
                       WHERE c.shop_id=? ORDER BY c.id DESC LIMIT 50""", (ctx.shop_id,)),
            "campaigns": db.q("SELECT * FROM campaigns WHERE shop_id=? ORDER BY id DESC LIMIT 12", (ctx.shop_id,)),
            "segments": segment_counts(ctx.shop_id)}


def build_segment_sql(seg: dict) -> tuple[str, list]:
    where, params = ["c.opted_in=1"], []
    if seg.get("risk"):
        bands = seg["risk"] if isinstance(seg["risk"], list) else [seg["risk"]]
        where.append("c.risk_band IN (%s)" % ",".join("?" * len(bands)))
        params += bands
    if seg.get("category"):
        where.append("""EXISTS (SELECT 1 FROM sales s JOIN sale_items si ON si.sale_id=s.id
                                WHERE s.customer_id=c.id AND si.category=?)""")
        params.append(seg["category"])
    if seg.get("product"):
        where.append("""EXISTS (SELECT 1 FROM sales s JOIN sale_items si ON si.sale_id=s.id
                                WHERE s.customer_id=c.id AND si.product_name LIKE ?)""")
        params.append(f"%{seg['product']}%")
    if seg.get("min_spend"):
        where.append("c.total_spent>=?")
        params.append(float(seg["min_spend"]))
    if seg.get("inactive_days"):
        where.append("c.last_purchase_at<=?")
        params.append(db.days_from_today(-int(seg["inactive_days"])))
    if seg.get("tag"):
        where.append("c.tags LIKE ?")
        params.append(f"%{seg['tag']}%")
    if seg.get("area"):
        where.append("c.area LIKE ?")
        params.append(f"%{seg['area']}%")
    return " AND ".join(where), params


@route("POST", "/api/campaigns/preview", "shop")
def campaign_preview(ctx):
    seg = ctx.b("segment") or {}
    w, params = build_segment_sql(seg)
    n = db.scalar(f"SELECT COUNT(*) FROM customers c WHERE c.shop_id=? AND {w}", [ctx.shop_id] + params, 0)
    sample = db.q(f"SELECT c.id, c.name, c.phone, c.risk_band, c.total_spent FROM customers c "
                  f"WHERE c.shop_id=? AND {w} LIMIT 5", [ctx.shop_id] + params)
    return {"count": n, "sample": sample}


@route("POST", "/api/campaigns", "shop")
def create_campaign(ctx):
    name = (ctx.b("name") or "").strip()
    if not name:
        raise ApiError("give the campaign a name")
    seg = ctx.b("segment") or {}
    channel = ctx.b("channel") or "whatsapp"
    template_key = ctx.b("template_key") or "broadcast"
    cid = db.insert("campaigns", {
        "shop_id": ctx.shop_id, "name": name[:120], "segment": json.dumps(seg),
        "template_key": template_key, "channel": channel, "status": ctx.b("status") or "draft",
        "scheduled_at": ctx.b("scheduled_at"), "created_at": db.now_iso(),
    })
    db.audit(ctx.user["email"], "campaign.created", ctx.shop_id, "campaign", cid, name)
    return {"ok": True, "campaign": db.q1("SELECT * FROM campaigns WHERE id=?", (cid,))}


@route("POST", "/api/campaigns/:cid/send", "shop")
def send_campaign(ctx):
    cid = int(ctx.params["cid"])
    camp = db.q1("SELECT * FROM campaigns WHERE id=? AND shop_id=?", (cid, ctx.shop_id))
    if not camp:
        raise ApiError("campaign not found", 404)
    shop = shop_of(ctx)
    seg = json.loads(camp["segment"] or "{}")
    w, params = build_segment_sql(seg)
    customers = db.q(f"SELECT c.* FROM customers c WHERE c.shop_id=? AND {w} LIMIT 5000", [ctx.shop_id] + params)
    raw_body = ctx.b("body") or ""
    # the editor writes {name}/{shop}/{product} style placeholders; templates use {first_name}…
    raw_body = re.sub(r"\{\{(\w+)\}\}", r"{\1}", raw_body)
    raw_body = raw_body.replace("{name}", "{customer_name}").replace("{shop}", "{shop_name}")
    extra = {"headline": ctx.b("headline") or shop["name"] + " update", "body": raw_body}
    coupon_id = camp.get("coupon_id")
    pct_off = float(ctx.b("coupon_percent", 0) or 0)
    if pct_off and not coupon_id:
        base = f"{shop['slug'][:6].upper().replace('-', '')}{db.utcnow():%m%d%H%M}"
        code, n = base[:22], 2
        while db.q1("SELECT id FROM coupons WHERE shop_id=? AND code=?", (ctx.shop_id, code)):
            code = f"{base[:20]}{n}"
            n += 1
        coupon_id = db.insert("coupons", {
            "shop_id": ctx.shop_id, "code": code[:24], "description": f"{pct_off:.0f}% campaign discount",
            "discount_type": "percent", "discount_value": pct_off, "min_spend": 0,
            "expires_on": db.days_from_today(14), "max_uses": 5000, "customer_id": None,
            "reminder_id": None, "status": "active", "created_at": db.now_iso(),
        })
        db.update("campaigns", cid, {"coupon_id": coupon_id})
    queued = 0
    for c in customers:
        rid = engine._schedule(shop, c, None,
                               camp["template_key"] if camp["template_key"] != "broadcast" else "broadcast",
                               db.today(), channel=camp["channel"], extra=extra, coupon_id=coupon_id,
                               campaign_id=cid)
        if rid:
            queued += 1
    db.update("campaigns", cid, {"status": "running", "sent_count": queued})
    stats = engine.tick(limit=400)
    db.x("""UPDATE campaigns SET sent_count=(SELECT COUNT(*) FROM messages WHERE campaign_id=?),
                                status=CASE WHEN (SELECT COUNT(*) FROM messages WHERE campaign_id=?)>0 THEN 'sent' ELSE status END
             WHERE id=?""", (cid, cid, cid))
    db.audit(ctx.user["email"], "campaign.sent", ctx.shop_id, "campaign", cid, f"{queued} recipients")
    return {"ok": True, "queued": queued, "recipients": len(customers), "worker": stats}


# ------------------------------------------------------------------------ loyalty
@route("GET", "/api/loyalty", "shop")
def loyalty(ctx):
    sid = ctx.shop_id
    return {
        "enabled": bool(shop_of(ctx)["loyalty_enabled"]),
        "rule": shop_of(ctx)["loyalty_rule"],
        "leaderboard": db.q("""SELECT id, name, phone, loyalty_points, visit_count, total_spent
                               FROM customers WHERE shop_id=? ORDER BY loyalty_points DESC, total_spent DESC LIMIT 20""", (sid,)),
        "near_reward": db.q("""SELECT id, name, phone, loyalty_points FROM customers
                               WHERE shop_id=? AND loyalty_points BETWEEN 3 AND 4 ORDER BY loyalty_points DESC LIMIT 20""", (sid,)),
        "events": db.q("""SELECT le.*, c.name customer FROM loyalty_events le JOIN customers c ON c.id=le.customer_id
                          WHERE le.shop_id=? ORDER BY le.id DESC LIMIT 50""", (sid,)),
        "stats": db.q1("""SELECT COUNT(*) members, IFNULL(SUM(loyalty_points),0) points_outstanding,
                                 SUM(loyalty_points>=5) ready_to_redeem
                          FROM customers WHERE shop_id=?""", (sid,)),
    }


# ------------------------------------------------------------------------ reports
@route("GET", "/api/reports", "shop")
def reports(ctx):
    sid = ctx.shop_id
    days = ctx.qi("days", 30)
    since = db.days_from_today(-days) + " 00:00:00"
    funnel = {
        "scheduled": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND created_at>=?", (sid, since), 0),
        "sent": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND sent_at>=? AND status<>'cancelled'", (sid, since), 0),
        "replied": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status IN ('replied','converted') AND sent_at>=?", (sid, since), 0),
        "converted": db.scalar("SELECT COUNT(*) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?", (sid, since), 0),
        "revenue": db.scalar("SELECT IFNULL(SUM(revenue),0) FROM reminders WHERE shop_id=? AND status='converted' AND sent_at>=?", (sid, since), 0),
    }
    return {
        "funnel": funnel,
        "kpis": engine.shop_kpis(sid, days),
        "by_kind": db.q("""SELECT kind, COUNT(*) sent, SUM(status='converted') converted,
                                  IFNULL(SUM(revenue),0) revenue
                           FROM reminders WHERE shop_id=? AND sent_at>=? GROUP BY kind ORDER BY sent DESC""", (sid, since)),
        "by_channel": db.q("""SELECT channel, COUNT(*) sent, SUM(status IN ('delivered','read','replied')) delivered,
                                     SUM(status='failed') failed
                              FROM messages WHERE shop_id=? AND direction='out' AND created_at>=? GROUP BY channel""", (sid, since)),
        "by_product": db.q("""SELECT r.product_name, COUNT(*) reminders, SUM(r.status='converted') converted,
                                     IFNULL(SUM(r.revenue),0) revenue
                              FROM reminders r WHERE r.shop_id=? AND r.product_name<>'' AND r.sent_at>=?
                              GROUP BY r.product_name ORDER BY revenue DESC LIMIT 12""", (sid, since)),
        "revenue_series": engine.daily_series(sid, days, "sales"),
        "message_series": engine.daily_series(sid, days, "messages"),
        "cohorts": db.q("""SELECT substr(s.sale_date,1,7) month, COUNT(DISTINCT s.customer_id) customers,
                                  IFNULL(SUM(s.total),0) revenue
                           FROM sales s WHERE s.shop_id=? AND s.sale_date>=?
                           GROUP BY month ORDER BY month""", (sid, db.days_from_today(-180))),
        "rebuy_matrix": db.q("""SELECT visit_count, COUNT(*) customers FROM customers WHERE shop_id=?
                                GROUP BY visit_count ORDER BY visit_count LIMIT 12""", (sid,)),
        "coupons": db.q1("""SELECT COUNT(*) issued, SUM(status='used') redeemed,
                                   IFNULL(SUM(CASE WHEN status='used' THEN discount_value ELSE 0 END),0) discount_given
                            FROM coupons WHERE shop_id=?""", (sid,)),
        "top_customers": db.q("""SELECT name, phone, visit_count, total_spent, risk_band FROM customers
                                 WHERE shop_id=? ORDER BY total_spent DESC LIMIT 10""", (sid,)),
    }


# ---------------------------------------------------------------------- team
@route("GET", "/api/team", "shop")
def team_list(ctx):
    rows = db.q("""SELECT id, name, email, role, google_sub, last_login, created_at
                   FROM users WHERE shop_id=? ORDER BY id""", (ctx.shop_id,))
    for r in rows:
        r["is_you"] = r["id"] == ctx.user["id"]
        r["sign_in"] = "Google" if r["google_sub"] else ("Password" if True else "")
        r["google_linked"] = bool(r["google_sub"])
        r.pop("google_sub", None)
    invites = db.q("""SELECT id, email, role, invited_by, expires_at, accepted_at, created_at
                      FROM invites WHERE shop_id=? AND accepted_at IS NULL ORDER BY id DESC""", (ctx.shop_id,))
    return {"members": rows, "invites": invites,
            "roles": [{"key": "owner", "label": "Owner", "blurb": "Full access, billing, team"},
                      {"key": "staff", "label": "Staff", "blurb": "Sales, customers, reminders"}]}


@route("POST", "/api/team/invite", "shop")
def team_invite(ctx):
    if ctx.user.get("role") not in ("owner", "admin"):
        raise ApiError("Only the shop owner can invite team members", 403, "forbidden")
    email = auth.clean_email(ctx.b("email", ""))
    role = ctx.b("role") or "staff"
    if role not in ("owner", "staff"):
        raise ApiError("Role must be owner or staff")
    if not re.match(r"^[^@\s]+@[^@\s]+\.[a-z]{2,}$", email):
        raise ApiError("That email address does not look right")
    if auth.find_user(email):
        raise ApiError("That person already has an account — they can sign in with Google directly", 409)
    token = secrets.token_urlsafe(24)
    th = hashlib.sha256(token.encode()).hexdigest()
    db.x("DELETE FROM invites WHERE shop_id=? AND email=?", (ctx.shop_id, email))
    db.insert("invites", {"shop_id": ctx.shop_id, "email": email, "role": role, "token_hash": th,
                          "invited_by": ctx.user["email"], "expires_at": db.days_from_today(7),
                          "created_at": db.now_iso()})
    shop = shop_of(ctx)
    link = f"{config.PUBLIC_URL or ''}/#/login?invite={token}"
    driver, cfg = providers.credentials_for(None, "email")
    sent = providers.send_email(
        email, f"You're invited to {shop['name']} on Rebuy",
        f"{ctx.user['name']} invited you to {shop['name']} on Rebuy.\n\n"
        f"Sign in with this Google address ({email}) here:\n{link}\n\n"
        f"The link expires in 7 days.", cfg, driver)
    db.audit(ctx.user["email"], "team.invited", ctx.shop_id, "invite", email, role)
    return {"ok": True, "invite": {"email": email, "role": role, "link": link,
                                   "email_sent": bool(sent and sent.get("ok"))},
            "members": team_list(ctx)["members"], "invites": team_list(ctx)["invites"]}


@route("POST", "/api/team/role", "shop")
def team_role(ctx):
    if ctx.user.get("role") != "owner":
        raise ApiError("Only the shop owner can change roles", 403, "forbidden")
    uid = ctx.bi("user_id", 0)
    role = ctx.b("role") or ""
    if role not in ("owner", "staff"):
        raise ApiError("Role must be owner or staff")
    target = db.q1("SELECT * FROM users WHERE id=? AND shop_id=?", (uid, ctx.shop_id))
    if not target:
        raise ApiError("Team member not found", 404)
    if target["id"] == ctx.user["id"]:
        raise ApiError("You cannot change your own role")
    db.update("users", uid, {"role": role})
    db.audit(ctx.user["email"], "team.role_changed", ctx.shop_id, "user", uid, role)
    return {"ok": True, "members": team_list(ctx)["members"]}


@route("POST", "/api/team/remove", "shop")
def team_remove(ctx):
    if ctx.user.get("role") != "owner":
        raise ApiError("Only the shop owner can remove team members", 403, "forbidden")
    uid = ctx.bi("user_id", 0)
    if uid == ctx.user["id"]:
        raise ApiError("You cannot remove yourself")
    target = db.q1("SELECT * FROM users WHERE id=? AND shop_id=?", (uid, ctx.shop_id))
    if not target:
        raise ApiError("Team member not found", 404)
    auth.logout_everywhere(uid)
    db.x("DELETE FROM users WHERE id=? AND shop_id=?", (uid, ctx.shop_id))
    db.audit(ctx.user["email"], "team.removed", ctx.shop_id, "user", uid, target["email"])
    return {"ok": True, "members": team_list(ctx)["members"]}


@route("POST", "/api/team/cancel-invite", "shop")
def team_cancel_invite(ctx):
    iid = ctx.bi("invite_id", 0)
    db.x("DELETE FROM invites WHERE id=? AND shop_id=?", (iid, ctx.shop_id))
    return {"ok": True, "invites": team_list(ctx)["invites"]}


# ------------------------------------------------------------------- channel setup
@route("GET", "/api/settings/channels", "shop")
def channels(ctx):
    out = []
    for ch in CHANNELS:
        row = db.q1("SELECT * FROM credentials WHERE scope='shop' AND shop_id=? AND provider=?", (ctx.shop_id, ch))
        plat = db.q1("SELECT * FROM credentials WHERE scope='platform' AND provider=?", (ch,))
        driver, cfg = providers.credentials_for(ctx.shop_id, ch)
        out.append({
            "channel": ch, "driver": driver,
            "shop_config": mask(json.loads(row["config"])) if row else {},
            "shop_enabled": bool(row and row["enabled"]),
            "shop_driver": row["driver"] if row else None,
            "platform_driver": plat["driver"] if plat else "sandbox",
            "platform_enabled": bool(plat and plat["enabled"]),
            "inherits_platform": driver == (plat["driver"] if plat and plat["enabled"] else "sandbox") and not (row and row["enabled"]),
            "sent_7d": db.scalar("""SELECT COUNT(*) FROM messages WHERE shop_id=? AND channel=? AND direction='out'
                                    AND created_at>=?""", (ctx.shop_id, ch, db.days_from_today(-7) + " 00:00:00"), 0),
            "failed_7d": db.scalar("""SELECT COUNT(*) FROM messages WHERE shop_id=? AND channel=? AND direction='out'
                                      AND status='failed' AND created_at>=?""",
                                   (ctx.shop_id, ch, db.days_from_today(-7) + " 00:00:00"), 0),
            "help": {
                "whatsapp": "Meta WhatsApp Cloud API: a permanent access token plus your Phone Number ID. "
                            "Set your webhook to POST /api/webhooks/whatsapp.",
                "sms": "Twilio: Account SID, Auth Token and a Twilio sender (phone or short code). "
                       "Status callback: POST /api/webhooks/twilio-status.",
                "email": "Any SMTP host (Gmail app password, SendGrid, Amazon SES, Mailgun…).",
            }[ch],
        })
    return {"channels": out, "sandbox_note": "While a channel has no live credentials, Rebuy uses the sandbox "
                                             "driver: messages are logged and simulated replies arrive so the "
                                             "full loop can be tested."}


@route("POST", "/api/settings/channels", "shop")
def save_channel(ctx):
    ch = ctx.b("channel")
    driver = ctx.b("driver") or "sandbox"
    if ch not in CHANNELS:
        raise ApiError("unknown channel")
    cfg = ctx.b("config") or {}
    existing = db.q1("SELECT * FROM credentials WHERE scope='shop' AND shop_id=? AND provider=?", (ctx.shop_id, ch))
    if existing:
        merged = json.loads(existing["config"] or "{}")
        if driver == "sandbox":
            merged = {}  # switching back to the sandbox clears the provider config
        for k, v in cfg.items():  # ignore masked placeholders so re-saving doesn't wipe secrets
            if isinstance(v, str) and "•" in v:
                continue
            if k in _SECRET_KEYS:
                v = (v or "").strip()
                if not v:  # blank field means "keep what is stored", not "erase it"
                    continue
            merged[k] = v
        db.update("credentials", existing["id"], {"driver": driver, "enabled": 1 if ctx.b("enabled", True) else 0,
                                                  "config": json.dumps(merged), "updated_at": db.now_iso()})
    else:
        cfg = {k: v for k, v in (cfg or {}).items() if not (k in _SECRET_KEYS and not str(v or "").strip())}
        db.insert("credentials", {"scope": "shop", "shop_id": ctx.shop_id, "provider": ch, "driver": driver,
                                  "enabled": 1 if ctx.b("enabled", True) else 0,
                                  "config": json.dumps(cfg), "updated_at": db.now_iso()})
    db.audit(ctx.user["email"], "channel.configured", ctx.shop_id, "credential", ch, f"{ch}->{driver}")
    providers.clear_credential_cache()
    return {"ok": True, "channels": channels(ctx)["channels"]}


@route("POST", "/api/settings/test-message", "shop")
def test_message(ctx):
    ch = ctx.b("channel") or "whatsapp"
    to = ctx.b("to") or ""
    if ch not in CHANNELS:
        raise ApiError("unknown channel")
    driver, cfg = providers.credentials_for(ctx.shop_id, ch)
    if not to:
        c = db.q1("SELECT * FROM customers WHERE shop_id=? AND opted_in=1 ORDER BY id LIMIT 1", (ctx.shop_id,))
        if c:
            to = {"whatsapp": c.get("whatsapp") or c.get("phone"), "sms": c.get("phone"), "email": c.get("email")}[ch]
    if not to:
        raise ApiError("no destination address — enter one or add a customer first")
    shop = shop_of(ctx)
    body = ctx.b("body") or f"Test from {shop['name']} via Rebuy ({ch}, driver={driver})."
    res = providers.test_provider(ch, driver, cfg, to, body)
    mid = db.insert("messages", {
        "shop_id": ctx.shop_id, "direction": "out", "channel": ch, "provider": driver, "to_addr": to,
        "from_addr": shop.get("whatsapp_number") or cfg.get("from_address", ""),
        "subject": "Rebuy test", "body": body, "template_key": "test",
        "status": "delivered" if res.get("ok") else "failed", "error": res.get("error", ""),
        "provider_msg_id": res.get("message_id", ""), "created_at": db.now_iso(),
        "sent_at": db.now_iso() if res.get("ok") else None,
    })
    db.audit(ctx.user["email"], "channel.test", ctx.shop_id, "message", mid, f"{ch}/{driver} -> {to}")
    return {"ok": bool(res.get("ok")), "driver": driver, "result": {k: v for k, v in res.items() if k != "body"},
            "detail": str(res.get("body", ""))[:600], "message_id": mid}


# -------------------------------------------------------------------------- misc
@route("GET", "/api/meta/sectors", "none")
def sectors(ctx):
    return {"sectors": sorted(set(list(imports.CATEGORY_KEYWORDS.keys()) + [
        "Supermarket", "Pharmacy", "Salon & Spa", "Auto Garage", "Hardware", "Agrovet", "Bakery",
        "LPG & Water", "Boutique", "Laundry", "Gym & Fitness", "Pet Shop", "Electronics", "Restaurant",
        "Stationery & Print", "Mobile Money & Airtime", "Clinic", "Other"]))}


@route("GET", "/api/meta/cycles", "none")
def cycles(ctx):
    return {"categories": engine.CATEGORY_DEFAULT_CYCLES, "risk_bands": engine.RISK_BANDS}


@route("GET", "/api/health", "none")
def health(ctx):
    """Liveness + a non-secret view of how this instance is configured."""
    cfgstatus = config.status()
    checks = {
        "database": True,
        "secret_key": cfgstatus["secret_key_set"] or not cfgstatus["production"],
        "google_oauth": cfgstatus["google_oauth"],
        "public_url": bool(cfgstatus["public_url"]),
    }
    if cfgstatus["production"]:
        warnings = []
        if not checks["secret_key"]:
            warnings.append("REBUY_SECRET_KEY is not set — sign-in state cannot be signed")
        if not checks["google_oauth"]:
            warnings.append("Google sign-in is not configured")
        if not checks["public_url"]:
            warnings.append("REBUY_PUBLIC_URL is not set — OAuth redirects will be inferred from Host")
        if not cfgstatus["admin_emails"]:
            warnings.append("REBUY_ADMIN_EMAILS is empty — no platform admin can sign in")
    else:
        warnings = []
    return {"ok": True, "time": db.now_iso(),
            "shops": db.scalar("SELECT COUNT(*) FROM shops", (), 0),
            "db": "supabase/postgres" if db.IS_PG else "sqlite", "backend": db.backend(),
            "config": cfgstatus, "warnings": warnings}
