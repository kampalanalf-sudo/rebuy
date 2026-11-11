"""
Inbound webhooks + serverless cron.

  GET  /api/webhooks/whatsapp        Meta verification handshake
  POST /api/webhooks/whatsapp        inbound WhatsApp messages + delivery statuses
  POST /api/webhooks/twilio          inbound SMS
  POST /api/webhooks/twilio-status   SMS delivery status callbacks
  POST /api/webhooks/email           inbound email (SendGrid/Mailgun parse-to-webhook)
  POST /api/cron/tick                worker heartbeat for serverless deployments

Point your provider consoles at these paths and replies flow straight into the
shop's Inbox and update reminder statuses.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.parse

from . import db, engine, providers
from .httpd import ApiError, route


def _verify_meta_signature(raw: bytes, headers: dict) -> bool:
    secret = os.environ.get("WHATSAPP_APP_SECRET")
    if not secret:
        row = db.q1("SELECT config FROM credentials WHERE scope='platform' AND provider='whatsapp'")
        if row:
            secret = json.loads(row["config"] or "{}").get("app_secret")
    if not secret:
        return True  # verification disabled until an app secret is configured
    sig = headers.get("x-hub-signature-256", "")
    if not sig.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig[7:])


def _shop_from_number_id(number_id: str) -> int | None:
    for row in db.q("SELECT shop_id, config FROM credentials WHERE provider='whatsapp' AND enabled=1"):
        try:
            cfg = json.loads(row["config"] or "{}")
        except json.JSONDecodeError:
            continue
        if str(cfg.get("phone_number_id", "")) == str(number_id):
            return row["shop_id"] or None
    shop = db.q1("SELECT id FROM shops WHERE status IN ('active','trial') ORDER BY id LIMIT 1")
    return shop["id"] if shop else None


@route("GET", "/api/webhooks/whatsapp", "none")
def wa_verify(ctx):
    mode = ctx.q("hub.mode")
    token = ctx.q("hub.verify_token")
    challenge = ctx.q("hub.challenge")
    expected = os.environ.get("WHATSAPP_VERIFY_TOKEN")
    if not expected:
        row = db.q1("SELECT config FROM credentials WHERE scope='platform' AND provider='whatsapp'")
        if row:
            expected = json.loads(row["config"] or "{}").get("verify_token")
    if mode == "subscribe" and (not expected or token == expected):
        body = (challenge or "ok").encode()
        return 200, {"content-type": "text/plain", "content-length": str(len(body))}, body
    raise ApiError("verification failed", 403)


@route("POST", "/api/webhooks/whatsapp", "none")
def wa_inbound(ctx):
    if not _verify_meta_signature(ctx.raw, ctx.headers):
        raise ApiError("invalid signature", 403)
    payload = ctx.body if isinstance(ctx.body, dict) else {}
    db.insert("webhook_events", {"provider": "meta", "event_type": "whatsapp",
                                 "payload": json.dumps(payload)[:8000], "created_at": db.now_iso()})
    handled = {"inbound": 0, "statuses": 0}
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            number_id = value.get("metadata", {}).get("phone_number_id", "")
            shop_id = _shop_from_number_id(number_id)
            for msg in value.get("messages", []) or []:
                parsed = providers.parse_meta_webhook({"entry": [entry]})
                if not shop_id:
                    continue
                norm = "+" + str(msg.get("from", "")).lstrip("+")
                c = db.q1("SELECT * FROM customers WHERE shop_id=? AND (phone=? OR whatsapp=? OR phone=? OR whatsapp=?)",
                          (shop_id, norm, norm, msg.get("from", ""), msg.get("from", "")))
                text = msg.get("text", {}).get("body", "") if msg.get("type") == "text" else f"[{msg.get('type')}]"
                last = None
                if c:
                    last = db.q1("""SELECT * FROM reminders WHERE customer_id=? AND status IN ('sent','delivered','replied')
                                    ORDER BY id DESC LIMIT 1""", (c["id"],))
                engine.handle_inbound(shop_id, c["id"] if c else None, "whatsapp", norm, text,
                                      provider="meta", reminder_id=last["id"] if last else None)
                handled["inbound"] += 1
            for st in value.get("statuses", []) or []:
                status = (st.get("status") or "").lower()
                pmid = st.get("id", "")
                m = db.q1("SELECT * FROM messages WHERE provider_msg_id=?", (pmid,))
                if m:
                    new = {"sent": "sent", "delivered": "delivered", "read": "read", "failed": "failed"}.get(status, m["status"])
                    patch = {"status": new}
                    if new == "delivered":
                        patch["delivered_at"] = db.now_iso()
                    if new == "failed":
                        patch["error"] = json.dumps(st.get("errors", []))[:400]
                    db.update("messages", m["id"], patch)
                    if m.get("reminder_id") and new in ("delivered", "failed"):
                        db.update("reminders", m["reminder_id"], {"status": new if new != "failed" else "failed"})
                handled["statuses"] += 1
    return {"ok": True, **handled}


@route("POST", "/api/webhooks/twilio", "none")
def twilio_inbound(ctx):
    fields = ctx.body if isinstance(ctx.body, dict) else {}
    db.insert("webhook_events", {"provider": "twilio", "event_type": "sms.inbound",
                                 "payload": json.dumps(fields)[:8000], "created_at": db.now_iso()})
    frm = fields.get("From", "")
    text = fields.get("Body", "")
    shop_id = None
    c = db.q1("SELECT shop_id, id FROM customers WHERE phone=? OR whatsapp=? LIMIT 1", (frm, frm))
    if c:
        shop_id = c["shop_id"]
    if not shop_id:
        row = db.q1("SELECT shop_id FROM credentials WHERE provider='sms' AND enabled=1 ORDER BY (shop_id IS NULL) ASC LIMIT 1")
        if row:
            shop_id = row["shop_id"]
    if not shop_id:
        return {"ok": False, "error": "could not resolve a shop for this sender"}
    c = db.q1("SELECT * FROM customers WHERE shop_id=? AND (phone=? OR whatsapp=?)", (shop_id, frm, frm))
    last = db.q1("""SELECT * FROM reminders WHERE customer_id=? AND status IN ('sent','delivered','replied')
                    ORDER BY id DESC LIMIT 1""", (c["id"],)) if c else None
    res = engine.handle_inbound(shop_id, c["id"] if c else None, "sms", frm, text, provider="twilio",
                               reminder_id=last["id"] if last else None)
    body = "<?xml version='1.0' encoding='UTF-8'?><Response></Response>".encode()
    return 200, {"content-type": "text/xml", "content-length": str(len(body))}, body


@route("POST", "/api/webhooks/twilio-status", "none")
def twilio_status(ctx):
    fields = ctx.body if isinstance(ctx.body, dict) else {}
    parsed = providers.parse_twilio_status(fields)
    db.insert("webhook_events", {"provider": "twilio", "event_type": f"sms.{parsed.get('status')}",
                                 "payload": json.dumps(fields)[:4000], "created_at": db.now_iso()})
    m = db.q1("SELECT * FROM messages WHERE provider_msg_id=?", (parsed["provider_msg_id"],))
    if m:
        mapping = {"sent": "sent", "delivered": "delivered", "undelivered": "failed", "failed": "failed",
                   "queued": "queued", "receiving": "sent"}
        new = mapping.get(parsed["status"], m["status"])
        patch = {"status": new}
        if new == "delivered":
            patch["delivered_at"] = db.now_iso()
        if new == "failed":
            patch["error"] = f"twilio {parsed.get('error') or parsed['status']}"
        db.update("messages", m["id"], patch)
        if m.get("reminder_id") and new == "failed":
            db.update("reminders", m["reminder_id"], {"status": "failed"})
    return {"ok": True, "matched": bool(m)}


@route("POST", "/api/webhooks/email", "none")
def email_inbound(ctx):
    """Inbound email (reply to a reminder). Accepts SendGrid/Mailgun style fields."""
    fields = ctx.body if isinstance(ctx.body, dict) else {}
    frm = fields.get("from") or fields.get("From") or ""
    text = fields.get("text") or fields.get("stripped-text") or fields.get("body") or ""
    db.insert("webhook_events", {"provider": "email", "event_type": "email.inbound",
                                 "payload": json.dumps(fields)[:8000], "created_at": db.now_iso()})
    c = db.q1("SELECT * FROM customers WHERE email=?", (frm.lower(),)) if frm else None
    if not c:
        return {"ok": False, "error": "unknown sender"}
    last = db.q1("""SELECT * FROM reminders WHERE customer_id=? AND status IN ('sent','delivered','replied')
                    ORDER BY id DESC LIMIT 1""", (c["id"],))
    res = engine.handle_inbound(c["shop_id"], c["id"], "email", frm, text, provider="smtp",
                               reminder_id=last["id"] if last else None)
    return {"ok": True, **res}


@route("POST", "/api/cron/tick", "none")
def cron_tick(ctx):
    """Vercel cron (or any scheduler) calls this to advance the queue serverlessly."""
    secret = os.environ.get("CRON_SECRET")
    if secret:
        provided = ctx.headers.get("authorization", "").replace("Bearer ", "") or ctx.q("secret") or ""
        if not hmac.compare_digest(provided, secret):
            raise ApiError("invalid cron secret", 403)
    from . import worker
    return {"ok": True, "stats": worker.run_tick(), "time": db.now_iso()}


@route("GET", "/api/cron/tick", "none")
def cron_tick_get(ctx):
    if os.environ.get("CRON_SECRET"):
        raise ApiError("use POST with the cron secret", 403)
    from . import worker
    return {"ok": True, "stats": worker.run_tick(), "time": db.now_iso()}
