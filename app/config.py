"""
Rebuy — configuration.

Everything that differs between a demo and a paid deployment lives here, is
read from the environment exactly once, and fails loudly if it is missing in
production. No credentials are ever hard-coded in the source tree.

Key variables
-------------
REBUY_ENV                 production | staging | development   (default: development)
REBUY_SECRET_KEY          signs the OAuth state JWT and session pepper. REQUIRED in production.
REBUY_PUBLIC_URL          e.g. https://app.rebuy.ug  — used to build OAuth redirect URIs
REBUY_DATABASE_URL        Postgres/Supabase connection string (omit for SQLite)
REBUY_GOOGLE_CLIENT_ID    Google OAuth client id
REBUY_GOOGLE_CLIENT_SECRET
REBUY_ALLOWED_EMAILS      optional comma list of emails allowed to sign in during private beta
REBUY_ADMIN_EMAILS        comma list promoted to platform admin on first sign-in
REBUY_SEED_DEMO           "1" to allow the demo fleet to exist in this environment
REBUY_COOKIE_SECURE       "1" forces the Secure flag on cookies (auto-on in production)
"""
from __future__ import annotations

import os
import secrets

ENV = (os.environ.get("REBUY_ENV") or "development").strip().lower()
IS_PROD = ENV == "production"


def _bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [x.strip().lower() for x in raw.split(",") if x.strip()]


class MissingConfig(RuntimeError):
    pass


# ------------------------------------------------------------------ core secrets
SECRET_KEY = os.environ.get("REBUY_SECRET_KEY", "").strip()

if IS_PROD and len(SECRET_KEY) < 32:
    raise MissingConfig(
        "REBUY_SECRET_KEY must be set to at least 32 characters in production "
        "(generate one with: python3 -c \"import secrets;print(secrets.token_urlsafe(48))\")"
    )

# development default is ephemeral on purpose: sessions reset when the process does
SECRET_KEY = SECRET_KEY or secrets.token_urlsafe(48)

PUBLIC_URL = (os.environ.get("REBUY_PUBLIC_URL") or "").rstrip("/")
GOOGLE_CLIENT_ID = os.environ.get("REBUY_GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("REBUY_GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_ENABLED = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)

ALLOWED_EMAILS = _list("REBUY_ALLOWED_EMAILS")
ADMIN_EMAILS = _list("REBUY_ADMIN_EMAILS")
SEED_DEMO = _bool("REBUY_SEED_DEMO", default=not IS_PROD)

COOKIE_SECURE = _bool("REBUY_COOKIE_SECURE", default=IS_PROD)
COOKIE_NAME = "rebuy_session"
COOKIE_SAMESITE = "Lax"
SESSION_DAYS = 14

# signup policy: self-serve is on by default outside production, off in production
# until you deliberately open it
ALLOW_SIGNUP = _bool("REBUY_ALLOW_SIGNUP", default=not IS_PROD)

# password policy
MIN_PASSWORD_LEN = 10


def google_redirect_uri(base_url: str = "") -> str:
    base = (base_url or PUBLIC_URL).rstrip("/")
    return f"{base}/api/auth/google/callback"


def status() -> dict:
    """Non-secret summary, used by /api/health and the admin cockpit."""
    return {
        "env": ENV,
        "production": IS_PROD,
        "public_url": PUBLIC_URL or None,
        "google_oauth": GOOGLE_ENABLED,
        "signup_open": ALLOW_SIGNUP,
        "demo_seed_allowed": SEED_DEMO,
        "cookie_secure": COOKIE_SECURE,
        "restricted_emails": len(ALLOWED_EMAILS),
        "admin_emails": len(ADMIN_EMAILS),
        "secret_key_set": bool(os.environ.get("REBUY_SECRET_KEY")),
        "database": "postgres" if (os.environ.get("REBUY_DATABASE_URL") or os.environ.get("DATABASE_URL")) else "sqlite",
    }
