"""Field-level encryption and access logging for sensitive client records.

Why this exists
---------------
A clinic's client list is medical data, not a sales list. Two rules follow, and
this module is the only place either is implemented:

1. **At rest.** Fields a hospital considers clinical are stored encrypted
   (Fernet = AES-128-CBC + HMAC-SHA256, from `cryptography`). A dump of the
   `clients` table without the key is unreadable. Encryption is *by column*,
   so ordinary queries still filter on plain columns (name, phone, branch)
   and only the sensitive ones are opened for a single record at a time.

2. **Every read.** Opening a client record writes an `access_log` row saying
   who looked, at what, from where, and why. That is the audit trail a health
   provider is expected to be able to produce on demand.

The key comes from REBUY_SECRET_KEY (or REBUY_PRIVACY_KEY if you want to
rotate it separately). If `cryptography` is unavailable we do NOT silently
store plaintext in production: `enabled` goes False, `mode` says "unavailable",
and the admin UI reports it, because a deployment that believes it is
encrypting and is not is worse than one that knows it isn't.
"""
from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from . import config, db

try:  # optional dependency; see module docstring
    from cryptography.fernet import Fernet, InvalidToken

    _HAVE_FERNET = True
except Exception:  # noqa: BLE001
    Fernet = None  # type: ignore[assignment]
    InvalidToken = Exception  # type: ignore[assignment,misc]
    _HAVE_FERNET = False

# Client columns that are encrypted at rest.
ENCRYPTED_FIELDS = ("notes", "condition", "identifier", "address", "dob")
# Columns the database may still reason about in the clear (searchable).
PLAIN_FIELDS = ("name", "phone", "whatsapp", "email", "area", "branch_id", "tags")


def _key() -> bytes:
    """Derive a stable Fernet key from the app secret."""
    raw = (getattr(config, "PRIVACY_KEY", "") or getattr(config, "SECRET_KEY", "") or "").encode()
    if not raw:
        return b""
    digest = hashlib.sha256(b"rebuy.privacy.v1:" + raw).digest()
    return base64.urlsafe_b64encode(digest)


def key_present() -> bool:
    return bool(_key())


def enabled() -> bool:
    """True when sensitive fields are actually being encrypted."""
    return bool(_HAVE_FERNET and key_present() and not getattr(config, "PRIVACY_PLAINTEXT", False))


def mode() -> str:
    if not _HAVE_FERNET:
        return "unavailable (cryptography not installed)"
    if not key_present():
        return "unavailable (no REBUY_SECRET_KEY)"
    if getattr(config, "PRIVACY_PLAINTEXT", False):
        return "plaintext (forced by REBUY_PRIVACY_PLAINTEXT=1)"
    return "encrypted (Fernet/AES-128-CBC + HMAC)"


def _fernet():
    return Fernet(_key())


def encrypt(value: str) -> str:
    """Encrypt one field. Empty values stay empty; already-sealed values are kept."""
    if not value:
        return ""
    value = str(value)
    if value.startswith("enc:v1:"):
        return value
    if not enabled():
        return value
    return "enc:v1:" + _fernet().encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    """Open one field. Values without the prefix are legacy plaintext."""
    if not value:
        return ""
    value = str(value)
    if not value.startswith("enc:v1:"):
        return value
    if not _HAVE_FERNET or not key_present():
        return "[encrypted — key unavailable on this deployment]"
    try:
        return _fernet().decrypt(value[7:].encode()).decode()
    except InvalidToken:
        return "[encrypted — could not be opened with this key]"
    except Exception:  # noqa: BLE001
        return "[encrypted — unreadable]"


def seal_record(rec: dict) -> dict:
    """Encrypt the sensitive fields of a client record before insert."""
    out = dict(rec)
    for f in ENCRYPTED_FIELDS:
        if f in out:
            out[f] = encrypt(out.get(f) or "")
    return out


def open_record(rec: dict | None) -> dict | None:
    """Decrypt the sensitive fields of a client record after read."""
    if not rec:
        return rec
    out = dict(rec)
    for f in ENCRYPTED_FIELDS:
        if f in out:
            out[f] = decrypt(out.get(f) or "")
    return out


# --------------------------------------------------------------------- audit
def log_access(actor: str, shop_id: int | None, client_id: int | None,
               action: str, detail: str = "", ip: str = "", ua: str = "") -> None:
    """Record that someone looked at (or changed) a client record.

    Never raises: an audit write failing must not break the request that
    triggered it, but it is deliberately written before the data is returned.
    """
    try:
        db.insert("access_log", {
            "shop_id": shop_id or 0,
            "actor": (actor or "system")[:120],
            "client_id": client_id or 0,
            "action": (action or "")[:40],
            "detail": (detail or "")[:500],
            "ip": (ip or "")[:64],
            "user_agent": (ua or "")[:200],
            "created_at": db.now_iso(),
        })
    except Exception:  # noqa: BLE001
        pass


def access_summary(shop_id: int, days: int = 30) -> dict:
    since = db.days_from_today(-days)
    return {
        "reads": db.scalar("SELECT COUNT(*) FROM access_log WHERE shop_id=? AND created_at>=?",
                           (shop_id, since), 0),
        "by_actor": db.q("""SELECT actor, COUNT(*) n FROM access_log
                            WHERE shop_id=? AND created_at>=? GROUP BY actor ORDER BY n DESC LIMIT 10""",
                         (shop_id, since)),
        "by_action": db.q("""SELECT action, COUNT(*) n FROM access_log
                             WHERE shop_id=? AND created_at>=? GROUP BY action ORDER BY n DESC LIMIT 10""",
                          (shop_id, since)),
        "recent": db.q("""SELECT actor, action, client_id, detail, ip, created_at FROM access_log
                          WHERE shop_id=? ORDER BY id DESC LIMIT 50""", (shop_id,)),
    }


# ------------------------------------------------------- outbound redaction
# Words that must never reach an outbound message body. A recall reminder says
# "you are due for a dental check-up"; it does not say why in clinical terms.
CLINICAL_TERMS = (
    "diagnosis", "diagnosed", "positive", "negative", "hiv", "tb test", "malaria test",
    "pregnant", "pregnancy test result", "cancer", "tumour", "tumor", "hepatitis",
    "syphilis", "gonorrhoea", "gonorrhea", "sti", "std", "mental", "psychiatric",
    "hiv+", "hiv positive", "viral load", "cd4",
)


def scrub(text: str) -> str:
    """Strip clinical terminology from a message body.

    Deliberately blunt: if a term appears, the message is not sent as-is. The
    caller sees the scrubbed text so a template author can spot the block.
    """
    if not text:
        return text
    out = text
    for term in CLINICAL_TERMS:
        low = out.lower()
        start = 0
        while True:
            i = low.find(term, start)
            if i < 0:
                break
            out = out[:i] + "[clinical detail withheld]" + out[i + len(term):]
            low = out.lower()
            start = i + len("[clinical detail withheld]")
    return out


def scrub_findings(text: str) -> list[str]:
    low = (text or "").lower()
    return [t for t in CLINICAL_TERMS if t in low]


def log_outbound(shop_id: int, client_id: int | None, channel: str, body: str,
                 scrubbed: bool, blocked: bool = False) -> None:
    try:
        db.insert("message_audit", {
            "shop_id": shop_id or 0,
            "client_id": client_id or 0,
            "channel": (channel or "")[:20],
            "excerpt": (body or "")[:300],
            "redacted": 1 if scrubbed else 0,
            "blocked": 1 if blocked else 0,
            "created_at": db.now_iso(),
        })
    except Exception:  # noqa: BLE001
        pass


def status() -> dict:
    """What the admin UI shows about the privacy layer."""
    return {"encryption": mode(), "enabled": enabled(),
            "key_present": key_present(),
            "sensitive_fields": list(ENCRYPTED_FIELDS),
            "clinical_terms_blocked": len(CLINICAL_TERMS)}
