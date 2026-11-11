"""Authentication: salted PBKDF2 passwords + opaque session cookies."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from . import db

SESSION_DAYS = 14


def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return dk.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 120_000)
    return hmac.compare_digest(dk.hex(), password_hash)


def create_user(shop_id: int | None, role: str, name: str, email: str, password: str) -> int:
    ph, salt = hash_password(password)
    return db.insert("users", {
        "shop_id": shop_id, "role": role, "name": name, "email": email.lower().strip(),
        "password_hash": ph, "salt": salt, "created_at": db.now_iso(),
    })


def login(email: str, password: str) -> dict | None:
    u = db.q1("SELECT * FROM users WHERE email = ?", (email.lower().strip(),))
    if not u or not verify_password(password, u["password_hash"], u["salt"]):
        return None
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    now = db.utcnow()
    db.insert("sessions", {
        "token_hash": th, "user_id": u["id"], "created_at": db.now_iso(),
        "expires_at": (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
    })
    db.update("users", u["id"], {"last_login": db.now_iso()})
    shop = db.q1("SELECT * FROM shops WHERE id = ?", (u["shop_id"],)) if u["shop_id"] else None
    return {
        "token": token,
        "user": {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"], "shop_id": u["shop_id"]},
        "shop": shop,
    }


def session_user(token: str | None) -> dict | None:
    if not token:
        return None
    th = hashlib.sha256(token.encode()).hexdigest()
    row = db.q1("""
        SELECT u.id, u.name, u.email, u.role, u.shop_id, s.expires_at
        FROM sessions s JOIN users u ON u.id = s.user_id
        WHERE s.token_hash = ?
    """, (th,))
    if not row:
        return None
    if row["expires_at"] < db.now_iso():
        db.x("DELETE FROM sessions WHERE token_hash = ?", (th,))
        return None
    return row


def logout(token: str | None) -> None:
    if token:
        db.x("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
