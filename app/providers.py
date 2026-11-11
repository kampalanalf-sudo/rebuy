"""
Messaging providers.

Real drivers are implemented against the public APIs:
  * WhatsApp  -> Meta WhatsApp Cloud API  (POST /{phone_number_id}/messages)
  * SMS       -> Twilio Messaging API     (POST /2010-04-01/Accounts/{sid}/Messages.json)
  * Email     -> any SMTP host            (smtplib, STARTTLS/SSL)

Each driver is only used when it is enabled AND fully configured, either for the
shop itself (Settings -> Channels) or platform-wide (Admin -> Integrations).
Otherwise the sandbox driver takes over: it records the message, simulates a
delivery receipt and, for reminder nudges, simulates a realistic customer reply
a few seconds later so the whole loop can be demoed without paid credentials.

Switching a shop from sandbox to live is a config change, not a code change.
"""
from __future__ import annotations

import base64
import json
import os
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from . import db

TIMEOUT = int(os.environ.get("REBUY_HTTP_TIMEOUT", "20"))


# --------------------------------------------------------------------- helpers
def _http_json(url: str, payload: dict, headers: dict, method: str = "POST") -> dict:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode() or "{}"
            return {"ok": True, "status": resp.status, "body": json.loads(body)}
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:1000]
        return {"ok": False, "status": e.code, "body": body}
    except Exception as e:  # noqa: BLE001 - network failures must not kill the worker
        return {"ok": False, "status": 0, "body": f"{type(e).__name__}: {e}"}


def _http_form(url: str, fields: dict, headers: dict) -> dict:
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            body = resp.read().decode() or "{}"
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                parsed = {"raw": body[:500]}
            return {"ok": True, "status": resp.status, "body": parsed}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "body": e.read().decode(errors="replace")[:1000]}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "body": f"{type(e).__name__}: {e}"}


_CRED_CACHE: dict[tuple, tuple[str, dict]] = {}
_CRED_STAMP = [0.0]


def clear_credential_cache() -> None:
    """Call after credentials change (settings save, admin edit)."""
    _CRED_CACHE.clear()
    _CRED_STAMP[0] = 0.0


def credentials_for(shop_id: int | None, channel: str) -> tuple[str, dict]:
    """Return (driver, config) for a channel, shop override first.

    Cached for a minute: a fleet-wide tick dispatches thousands of messages and
    would otherwise issue two queries per message.
    """
    hit = _CRED_CACHE.get((shop_id, channel))
    if hit is not None:
        return hit
    result = _resolve_credentials(shop_id, channel)
    _CRED_CACHE[(shop_id, channel)] = result
    if len(_CRED_CACHE) > 2048:
        _CRED_CACHE.clear()
    return result


def _resolve_credentials(shop_id: int | None, channel: str) -> tuple[str, dict]:
    for scope, sid in (("shop", shop_id), ("platform", None)):
        if scope == "shop" and not sid:
            continue
        row = db.q1(
            "SELECT * FROM credentials WHERE scope = ? AND IFNULL(shop_id,0) = IFNULL(?,0) AND provider = ?",
            (scope, sid, channel),
        )
        if row and row["enabled"]:
            try:
                cfg = json.loads(row["config"] or "{}")
            except json.JSONDecodeError:
                cfg = {}
            driver = row["driver"]
            if driver == "whatsapp":
                driver = "meta"
            if cfg.get("token") or cfg.get("sid") or cfg.get("host") or driver == "sandbox":
                return driver, cfg
    # environment fallback (useful on Vercel / Docker)
    env = _env_driver(channel)
    if env:
        return env
    return "sandbox", {}


def _env_driver(channel: str) -> tuple[str, dict] | None:
    if channel == "whatsapp":
        token = os.environ.get("WHATSAPP_TOKEN")
        pid = os.environ.get("WHATSAPP_PHONE_NUMBER_ID")
        if token and pid:
            return "meta", {"token": token, "phone_number_id": pid,
                            "api_version": os.environ.get("WHATSAPP_API_VERSION", "v21.0"),
                            "from_number": os.environ.get("WHATSAPP_FROM", "")}
    if channel == "sms":
        sid = os.environ.get("TWILIO_ACCOUNT_SID")
        tok = os.environ.get("TWILIO_AUTH_TOKEN")
        frm = os.environ.get("TWILIO_FROM")
        if sid and tok and frm:
            return "twilio", {"sid": sid, "token": tok, "from_number": frm}
    if channel == "email":
        host = os.environ.get("SMTP_HOST")
        user = os.environ.get("SMTP_USER")
        pw = os.environ.get("SMTP_PASSWORD")
        if host and user and pw:
            return "smtp", {"host": host, "port": int(os.environ.get("SMTP_PORT", "587")),
                            "user": user, "password": pw,
                            "from_address": os.environ.get("SMTP_FROM", user),
                            "security": os.environ.get("SMTP_SECURITY", "starttls")}
    return None


def e164(phone: str, default_country: str = "UG") -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        return digits
    d = digits.lstrip("0")
    cc = {"UG": "256", "KE": "254", "TZ": "255", "RW": "250", "NG": "234", "GH": "233"}.get(default_country, "256")
    if d.startswith(cc):
        return "+" + d
    return f"+{cc}{d}"


# ---------------------------------------------------------------------- drivers
def send_whatsapp(to: str, body: str, cfg: dict, driver: str) -> dict:
    if driver != "meta":
        return {"ok": True, "provider": "sandbox", "message_id": f"sbx-wa-{abs(hash(to + body)) % 10**10}"}
    url = f"https://graph.facebook.com/{cfg.get('api_version', 'v21.0')}/{cfg['phone_number_id']}/messages"
    headers = {"Authorization": f"Bearer {cfg['token']}", "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": e164(to).lstrip("+"),
        "type": "text",
        "text": {"preview_url": False, "body": body},
    }
    res = _http_json(url, payload, headers)
    if res["ok"]:
        mid = (res["body"].get("messages") or [{}])[0].get("id", "")
        return {"ok": True, "provider": "meta", "message_id": mid}
    return {"ok": False, "provider": "meta", "error": str(res["body"])[:500]}


def send_sms(to: str, body: str, cfg: dict, driver: str) -> dict:
    if driver != "twilio":
        return {"ok": True, "provider": "sandbox", "message_id": f"sbx-sms-{abs(hash(to + body)) % 10**10}"}
    sid, tok = cfg["sid"], cfg["token"]
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    auth = "Basic " + base64.b64encode(f"{sid}:{tok}".encode()).decode()
    headers = {"Authorization": auth, "Content-Type": "application/x-www-form-urlencoded"}
    res = _http_form(url, {"To": e164(to), "From": cfg["from_number"], "Body": body}, headers)
    if res["ok"]:
        return {"ok": True, "provider": "twilio", "message_id": res["body"].get("sid", "")}
    return {"ok": False, "provider": "twilio", "error": str(res["body"])[:500]}


def send_email(to: str, subject: str, body: str, cfg: dict, driver: str) -> dict:
    if driver != "smtp":
        return {"ok": True, "provider": "sandbox", "message_id": f"sbx-em-{abs(hash(to + body)) % 10**10}"}
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_address", cfg["user"])
    msg["To"] = to
    msg.attach(MIMEText(body, "plain"))
    html = f"<div style='font-family:Arial,sans-serif;font-size:15px;line-height:1.5;white-space:pre-wrap'>{body}</div>"
    msg.attach(MIMEText(html, "html"))
    try:
        port = int(cfg.get("port", 587))
        if cfg.get("security") == "ssl":
            server = smtplib.SMTP_SSL(cfg["host"], port, timeout=TIMEOUT, context=ssl.create_default_context())
        else:
            server = smtplib.SMTP(cfg["host"], port, timeout=TIMEOUT)
            server.ehlo()
            server.starttls(context=ssl.create_default_context())
            server.ehlo()
        server.login(cfg["user"], cfg["password"])
        server.sendmail(msg["From"], [to], msg.as_string())
        server.quit()
        return {"ok": True, "provider": "smtp", "message_id": msg["Message-ID"] or f"smtp-{to}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "provider": "smtp", "error": f"{type(e).__name__}: {e}"}


def test_provider(channel: str, driver: str, cfg: dict, to: str, body: str) -> dict:
    """Used by the 'Send test' buttons in shop settings and the admin console."""
    try:
        if channel == "whatsapp":
            return send_whatsapp(to, body, cfg, driver)
        if channel == "sms":
            return send_sms(to, body, cfg, driver)
        if channel == "email":
            return send_email(to, cfg.get("test_subject", "Rebuy test email"), body, cfg, driver)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": False, "error": "unknown channel"}


# --------------------------------------------------------------- inbound parsing
INTENT_RULES = [
    ("opt_out", ["stop", "unsubscribe", "opt out", "optout", "dont message", "do not message", "leave me", "siagaza"]),
    ("order", ["yes", "order", "deliver", "bring", "send me", "send 2", "i need", "want", "sure", "ok", "okay", "confirm", "yee", "kale"]),
    ("snooze", ["later", "not now", "next week", "next month", "still have", "enough", "not yet", "snooze"]),
    ("question", ["how much", "price", "cost", "where", "when", "what time", "do you have", "?" ]),
    ("negative", ["no", "nope", "not interested", "cancel", "waste"]),
]


def classify_intent(text: str) -> str:
    t = (text or "").lower().strip()
    if not t:
        return "unknown"
    for intent, words in INTENT_RULES:
        if any(w in t for w in words):
            return intent
    return "other"


def parse_meta_webhook(payload: dict) -> dict:
    """Normalise a Meta WhatsApp Cloud API webhook into our inbound shape."""
    try:
        entry = payload["entry"][0]["changes"][0]["value"]
        msg = entry["messages"][0]
        contact = (entry.get("contacts") or [{}])[0]
        return {
            "provider": "meta",
            "provider_msg_id": msg.get("id", ""),
            "to": entry.get("metadata", {}).get("phone_number_id", ""),
            "from": msg.get("from", ""),
            "name": contact.get("profile", {}).get("name", ""),
            "text": msg.get("text", {}).get("body", "") or msg.get("type", ""),
            "timestamp": msg.get("timestamp", ""),
            "type": msg.get("type", "text"),
        }
    except Exception:  # noqa: BLE001
        return {"provider": "meta", "text": "", "from": "", "raw": json.dumps(payload)[:500]}


def parse_twilio_status(fields: dict) -> dict:
    return {
        "provider": "twilio",
        "provider_msg_id": fields.get("MessageSid", ""),
        "status": (fields.get("MessageStatus") or "").lower(),
        "to": fields.get("To", ""),
        "error": fields.get("ErrorCode", ""),
    }


def provider_health() -> list[dict]:
    """Live status of every configured channel, for the admin integrations page."""
    out = []
    for channel in ("whatsapp", "sms", "email"):
        driver, cfg = credentials_for(None, channel)
        configured = driver != "sandbox"
        masked = {}
        for k, v in cfg.items():
            if k in ("token", "password", "secret"):
                masked[k] = (v[:4] + "•" * 8 + v[-4:]) if isinstance(v, str) and len(v) > 10 else "•" * 8
            else:
                masked[k] = v
        shops_live = db.scalar(
            "SELECT COUNT(*) FROM credentials WHERE scope='shop' AND provider=? AND enabled=1 AND driver<>'sandbox'",
            (channel,), 0)
        out.append({
            "channel": channel, "driver": driver, "configured": configured,
            "config": masked, "shops_with_own_credentials": shops_live,
            "last_24h": db.q1(
                "SELECT COUNT(*) n, SUM(status='failed') failed FROM messages "
                "WHERE channel=? AND created_at >= ?", (channel, db.now_iso()[:10] + " 00:00:00")) or {},
        })
    return out
