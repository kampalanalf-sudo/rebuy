"""
Rebuy — authentication.

* Email + password: PBKDF2-SHA256, 210k iterations, per-user salt.
* Google sign-in: OpenID Connect authorisation-code flow with PKCE and a signed
  `state` value (no CSRF on the callback).
* Sessions: opaque 256-bit tokens, stored hashed, rotating on every sign-in.
* Roles: admin (platform staff) | owner | staff, all scoped by shop_id.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.error
import urllib.request
from datetime import timedelta

from . import config, db

SESSION_DAYS = config.SESSION_DAYS
PBKDF2_ROUNDS = 210_000

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_CERTS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")

# Google's signing keys change rarely; cache the fetch for an hour. A miss
# triggers a forced refresh, so a rotation is handled without waiting it out.
_JWKS_CACHE: dict = {"keys": None, "at": 0.0}
_JWKS_TTL = 3600.0


class AuthError(Exception):
    def __init__(self, message: str, code: str = "auth_error", status: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


# ------------------------------------------------------------------ passwords
def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return dk.hex(), salt


def verify_password(password: str, password_hash: str, salt: str) -> bool:
    if not password_hash or not salt:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), password_hash)


def clean_email(email: str) -> str:
    return (email or "").strip().lower()


def validate_password(password: str) -> None:
    if len(password or "") < config.MIN_PASSWORD_LEN:
        raise AuthError(f"Password must be at least {config.MIN_PASSWORD_LEN} characters", "weak_password")
    if password.lower() in ("password", "12345678", "qwertyui", "shop1234!"):
        raise AuthError("That password is too common — choose another", "weak_password")
    if password.lower() == (password or "").lower() and password.isalpha():
        # letters only is fine, but require some variety overall
        if len(password) < 14:
            raise AuthError("Use at least one number or symbol", "weak_password")


# ------------------------------------------------------------------ signed state
def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(txt: str) -> bytes:
    return base64.urlsafe_b64decode(txt + "=" * (-len(txt) % 4))


def sign_state(payload: dict) -> str:
    body = _b64(json.dumps(payload, separators=(",", ":")).encode())
    sig = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{sig}"


def verify_state(token: str, max_age: int = 600) -> dict:
    try:
        body, sig = token.split(".", 1)
    except (ValueError, AttributeError):
        raise AuthError("Invalid sign-in state", "bad_state")
    expected = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(sig, expected):
        raise AuthError("Invalid sign-in state", "bad_state")
    try:
        payload = json.loads(_unb64(body))
    except Exception:  # noqa: BLE001
        raise AuthError("Invalid sign-in state", "bad_state")
    if time.time() - float(payload.get("ts", 0)) > max_age:
        raise AuthError("This sign-in link expired — try again", "state_expired")
    return payload


# ------------------------------------------------------------------ google oidc
def google_authorize_url(redirect_uri: str, next_path: str = "/dashboard") -> dict:
    if not config.GOOGLE_ENABLED:
        raise AuthError("Google sign-in is not configured on this server", "google_disabled", 503)
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = _b64(hashlib.sha256(verifier.encode()).digest())
    payload = sign_state({"state": state, "verifier": verifier, "next": next_path, "ts": time.time()})
    params = {
        "client_id": config.GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    return {"url": f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}", "cookie": payload}


def _http_json(url: str, data: bytes | None = None, headers: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read() or b"{}")


def google_exchange(code: str, redirect_uri: str, signed_cookie: str, returned_state: str) -> dict:
    payload = verify_state(signed_cookie)
    if not hmac.compare_digest(str(payload.get("state", "")), str(returned_state or "")):
        raise AuthError("Sign-in state did not match — please retry", "state_mismatch")
    body = urllib.parse.urlencode({
        "code": code,
        "client_id": config.GOOGLE_CLIENT_ID,
        "client_secret": config.GOOGLE_CLIENT_SECRET,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
        "code_verifier": payload.get("verifier", ""),
    }).encode()
    try:
        tokens = _http_json(GOOGLE_TOKEN_URL, body, {"content-type": "application/x-www-form-urlencoded"})
    except Exception as e:  # noqa: BLE001
        raise AuthError(f"Google rejected the sign-in: {type(e).__name__}", "google_exchange_failed", 502)
    id_token = tokens.get("id_token") or ""
    claims = decode_id_token(id_token)
    if not claims.get("email"):
        raise AuthError("Google did not return an email address", "google_no_email", 502)
    if claims.get("email_verified") not in (True, "true", "True"):
        raise AuthError("Your Google email is not verified", "google_unverified", 403)
    return {"claims": claims, "next": payload.get("next") or "/dashboard"}


def decode_id_token(id_token: str, verify_signature: bool = True) -> dict:
    """Validate the ID token. Signature check uses Google's JWKS; in environments
    without outbound network the claims are still checked for issuer/audience/expiry."""
    parts = id_token.split(".")
    if len(parts) != 3:
        raise AuthError("Malformed Google token", "google_bad_token", 502)
    claims = json.loads(_unb64(parts[1]))
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError("Unexpected token issuer", "google_bad_token", 502)
    if claims.get("aud") != config.GOOGLE_CLIENT_ID:
        raise AuthError("Token was issued for a different app", "google_bad_token", 502)
    if float(claims.get("exp", 0)) < time.time() - 30:
        raise AuthError("Google token expired", "google_bad_token", 502)
    if verify_signature:
        try:
            _verify_google_signature(parts, claims)
        except AuthError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            # No route to Google's certs: refuse in production, allow in dev so
            # the flow can be exercised offline. Never fail open in production.
            if config.IS_PROD:
                raise AuthError("Could not reach Google to verify the sign-in",
                                "google_verify_unreachable", 502) from e
        except Exception as e:  # noqa: BLE001 — any other fault is a real failure
            raise AuthError(f"Could not verify the Google token: {type(e).__name__}",
                            "google_bad_token", 502) from e
    return claims


def _google_jwks(force: bool = False) -> dict:
    """Fetch Google's signing keys, cached for an hour."""
    if config.JWKS_CACHE_OFF:
        return _http_json(GOOGLE_CERTS_URL)
    now = time.time()
    if not force and _JWKS_CACHE["keys"] and (now - _JWKS_CACHE["at"]) < _JWKS_TTL:
        return _JWKS_CACHE["keys"]
    keys = _http_json(GOOGLE_CERTS_URL)
    _JWKS_CACHE["keys"] = keys
    _JWKS_CACHE["at"] = now
    return keys


def _verify_google_signature(parts: list[str], claims: dict) -> None:
    """Verify the RS256 signature against Google's published JWKS.

    Implemented with the standard library only (RSA PKCS#1 v1.5 + SHA-256), so
    there is no PyJWT dependency and no silent "skip verification if the library
    is missing" failure mode. Any network problem raises, and the caller decides
    whether an offline environment is acceptable; in production it is not.
    """
    jwks = _google_jwks()
    # The key id lives in the JWT *header* (parts[0]), never in the claims.
    # Reading it from the payload yields None, which made every sign-in fail
    # with "unknown key".
    try:
        header = json.loads(_unb64(parts[0]))
    except Exception as e:  # noqa: BLE001
        raise AuthError("Malformed Google token header", "google_bad_token", 502) from e
    kid = header.get("kid") or claims.get("kid")
    alg = header.get("alg")
    if alg != "RS256":
        raise AuthError(f"Unexpected token algorithm {alg!r}", "google_bad_token", 502)
    keys = {k.get("kid"): k for k in jwks.get("keys", [])}
    key = keys.get(kid)
    if not key:
        # Google rotates its signing keys. An unrecognised kid usually means a
        # key published after our fetch was cached, so refresh once and retry.
        _JWKS_CACHE["at"] = 0.0
        jwks2 = _google_jwks(force=True)
        keys2 = {k.get("kid"): k for k in jwks2.get("keys", [])}
        key = keys2.get(kid)
        if not key:
            raise AuthError(
                f"Google signed the token with an unknown key (kid={(kid or '')[:20]!r}, "
                f"known={len(keys2)})", "google_bad_token", 502)
    n = _b64int(key["n"])
    e = _b64int(key["e"])
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    signature = _unb64(parts[2])
    digest = hashlib.sha256(signing_input).digest()
    if len(signature) != (n.bit_length() + 7) // 8:
        raise AuthError("Malformed token signature", "google_bad_token", 502)
    sig_int = int.from_bytes(signature, "big")
    if sig_int >= n:
        raise AuthError("Malformed token signature", "google_bad_token", 502)
    # RSAVP1: m = s^e mod n
    em = pow(sig_int, e, n).to_bytes((n.bit_length() + 7) // 8, "big")
    try:
        recovered = _pkcs1_unpad(em)
    except ValueError:
        raise AuthError("Token signature did not verify", "google_bad_token", 502)
    if not hmac.compare_digest(recovered, _digest_info_sha256() + digest):
        raise AuthError("Token signature did not verify", "google_bad_token", 502)


def _b64int(txt: str) -> int:
    return int.from_bytes(_unb64(txt), "big")


def _digest_info_sha256() -> bytes:
    """DER prefix for SHA-256 inside an EMSA-PKCS1-v1_5 encoding (RFC 8017)."""
    return bytes.fromhex("3031300d060960864801650304020105000420")


def _pkcs1_unpad(em: bytes) -> bytes:
    """EMSA-PKCS1-v1_5 decode: 0x00 0x01 0xFF... 0x00 <digestinfo>."""
    if len(em) < 11 or em[0] != 0x00 or em[1] != 0x01:
        raise ValueError("bad padding header")
    idx = em.find(b"\x00", 2)
    if idx < 10:  # at least 8 bytes of 0xFF are required
        raise ValueError("padding too short")
    if any(b != 0xFF for b in em[2:idx]):
        raise ValueError("non-FF padding byte")
    return em[idx + 1:]


# ------------------------------------------------------------------ users
def create_user(shop_id: int | None, role: str, name: str, email: str, password: str | None = None,
                google_sub: str = "", email_verified: int = 0) -> int:
    ph, salt = (hash_password(password) if password else ("", ""))
    return db.insert("users", {
        "shop_id": shop_id, "role": role, "name": name, "email": clean_email(email),
        "password_hash": ph, "salt": salt, "google_sub": google_sub or "",
        "email_verified": 1 if (email_verified or google_sub) else 0,
        "created_at": db.now_iso(),
    })


def find_user(email: str) -> dict | None:
    return db.q1("SELECT * FROM users WHERE email = ?", (clean_email(email),))


def assert_email_allowed(email: str) -> None:
    if config.ALLOWED_EMAILS and clean_email(email) not in config.ALLOWED_EMAILS:
        raise AuthError("This account has not been invited yet — ask your Rebuy contact for access",
                        "not_invited", 403)


def provision_google_user(claims: dict) -> dict:
    """Find-or-create the local user for a verified Google identity."""
    email = clean_email(claims["email"])
    assert_email_allowed(email)
    existing = find_user(email)
    if existing:
        changes = {"last_login": db.now_iso()}
        if not existing.get("google_sub"):
            changes["google_sub"] = claims.get("sub", "")
        if not existing.get("email_verified"):
            changes["email_verified"] = 1
        if existing.get("role") != "admin" and email in config.ADMIN_EMAILS:
            changes["role"] = "admin"
        db.update("users", existing["id"], changes)
        return db.q1("SELECT * FROM users WHERE id = ?", (existing["id"],))

    role = "admin" if email in config.ADMIN_EMAILS else "owner"
    name = claims.get("name") or email.split("@")[0]
    uid = create_user(None, role, name, email, password=None,
                      google_sub=claims.get("sub", ""), email_verified=1)
    return db.q1("SELECT * FROM users WHERE id = ?", (uid,))


def provision_password_user(shop_id: int | None, role: str, name: str, email: str, password: str) -> dict:
    validate_password(password)
    assert_email_allowed(email)
    if find_user(email):
        raise AuthError("An account with that email already exists", "email_taken", 409)
    uid = create_user(shop_id, role, name, email, password)
    return db.q1("SELECT * FROM users WHERE id = ?", (uid,))


def start_session(user: dict, ip: str = "", ua: str = "", method: str = "password") -> str:
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    now = db.utcnow()
    db.insert("sessions", {
        "token_hash": th, "user_id": user["id"], "created_at": db.now_iso(),
        "expires_at": (now + timedelta(days=SESSION_DAYS)).strftime("%Y-%m-%d %H:%M:%S"),
        "ip": (ip or "")[:64], "user_agent": (ua or "")[:200], "method": method,
    })
    db.update("users", user["id"], {"last_login": db.now_iso()})
    return token


def session_cookie(token: str) -> str:
    bits = [f"{config.COOKIE_NAME}={token}", "Path=/", "HttpOnly", f"SameSite={config.COOKIE_SAMESITE}",
            f"Max-Age={SESSION_DAYS * 86400}"]
    if config.COOKIE_SECURE:
        bits.append("Secure")
    return "; ".join(bits)


def clear_cookie() -> str:
    bits = [f"{config.COOKIE_NAME}=", "Path=/", "HttpOnly", f"SameSite={config.COOKIE_SAMESITE}",
            "Max-Age=0"]
    if config.COOKIE_SECURE:
        bits.append("Secure")
    return "; ".join(bits)


def login(email: str, password: str, ip: str = "", ua: str = "") -> dict | None:
    if is_locked(email):
        return None
    u = find_user(email)
    if not u or not u.get("password_hash") or not verify_password(password, u["password_hash"], u["salt"]):
        _record_failure(email, ip)
        return None
    _clear_failures(email)
    token = start_session(u, ip, ua, "password")
    shop = db.q1("SELECT * FROM shops WHERE id = ?", (u["shop_id"],)) if u["shop_id"] else None
    return {"token": token, "user": public_user(u), "shop": shop}


def public_user(u: dict) -> dict:
    return {"id": u["id"], "name": u["name"], "email": u["email"], "role": u["role"],
            "shop_id": u.get("shop_id"), "has_password": bool(u.get("password_hash")),
            "google_linked": bool(u.get("google_sub"))}


# ------------------------------------------------------------------ throttling
LOCKOUT_AFTER = 6
LOCKOUT_MINUTES = 15


def _record_failure(email: str, ip: str) -> None:
    key = f"login_fail:{clean_email(email)}"
    count = int(db.meta_get(key, "0") or 0) + 1
    db.meta_set(key, str(count))
    if count == LOCKOUT_AFTER:
        db.meta_set(f"login_locked:{clean_email(email)}", db.now_iso())
    db.audit(clean_email(email)[:80], "auth.failed", None, "user", "", f"ip={ip} attempt={count}")


def _clear_failures(email: str) -> None:
    db.meta_set(f"login_fail:{clean_email(email)}", "0")
    db.meta_set(f"login_locked:{clean_email(email)}", "")


def is_locked(email: str) -> bool:
    stamp = db.meta_get(f"login_locked:{clean_email(email)}", "") or ""
    if not stamp:
        return False
    from datetime import datetime

    try:
        when = datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return False
    return (db.utcnow() - when) < timedelta(minutes=LOCKOUT_MINUTES)


# ------------------------------------------------------------------ sessions
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


def logout_everywhere(user_id: int) -> None:
    db.x("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def list_sessions(user_id: int) -> list[dict]:
    rows = db.q("""SELECT token_hash, created_at, expires_at, ip, user_agent, method
                   FROM sessions WHERE user_id = ? AND expires_at > ?
                   ORDER BY created_at DESC LIMIT 50""", (user_id, db.now_iso()))
    for r in rows:
        r["current"] = False
        r["id"] = r["token_hash"][:12]
        del r["token_hash"]
    return rows


def revoke_session(user_id: int, session_id: str) -> None:
    db.x("DELETE FROM sessions WHERE user_id = ? AND token_hash LIKE ?", (user_id, session_id + "%"))


def change_password(user_id: int, current: str, new: str) -> None:
    u = db.q1("SELECT * FROM users WHERE id = ?", (user_id,))
    if not u:
        raise AuthError("Account not found", "not_found", 404)
    if u.get("password_hash") and not verify_password(current, u["password_hash"], u["salt"]):
        raise AuthError("Your current password is not correct", "bad_current_password", 403)
    validate_password(new)
    ph, salt = hash_password(new)
    db.update("users", user_id, {"password_hash": ph, "salt": salt})
    logout_everywhere(user_id)


def set_password(user_id: int, new: str) -> None:
    validate_password(new)
    ph, salt = hash_password(new)
    db.update("users", user_id, {"password_hash": ph, "salt": salt})
