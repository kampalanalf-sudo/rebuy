"""Rebuy auth test suite — no network, no external services.

    python3 scripts/test_auth.py

Covers: state signing, Google ID-token validation, account provisioning,
email allow-listing, password policy, session lifecycle, and login throttling.
"""
import base64
import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DB = os.path.join(tempfile.gettempdir(), f"rebuy_auth_test_{os.getpid()}.db")
os.environ.update(
    REBUY_ENV="development",
    REBUY_DB=DB,
    REBUY_WORKER="0",
    REBUY_SECRET_KEY="t" * 40,
    REBUY_GOOGLE_CLIENT_ID="cid.apps.googleusercontent.com",
    REBUY_GOOGLE_CLIENT_SECRET="csecret",
    REBUY_ADMIN_EMAILS="boss@rebuy.ug",
    REBUY_SEED_DEMO="0",
)

from app import auth, config, db  # noqa: E402

db.init_schema()

PASSED = []
FAILED = []


def check(name, cond, extra=""):
    if cond:
        PASSED.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAILED.append(name)
        print(f"  \033[31mFAIL\033[0m  {name} {extra}")


def raises(fn, code=None):
    try:
        fn()
        return False
    except auth.AuthError as e:
        return code is None or e.code == code
    except Exception:  # noqa: BLE001
        return False


def b64(obj) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def make_id_token(**claims) -> str:
    now = int(time.time())
    base = {"iss": "https://accounts.google.com", "aud": config.GOOGLE_CLIENT_ID,
            "exp": now + 600, "email": "boss@rebuy.ug", "email_verified": True,
            "sub": "g-1", "name": "Boss"}
    base.update(claims)
    return f"{b64({'alg': 'RS256', 'typ': 'JWT'})}.{b64(base)}.sig"


print("\n\033[1mstate signing\033[0m")
tok = auth.sign_state({"a": 1, "ts": time.time()})
check("state round-trips", auth.verify_state(tok)["a"] == 1)
check("bit-flipped state rejected", raises(lambda: auth.verify_state(tok[:-1] + ("a" if tok[-1] != "a" else "b"))))
check("expired state rejected", raises(lambda: auth.verify_state(auth.sign_state({"ts": time.time() - 999}))))
check("garbage state rejected", raises(lambda: auth.verify_state("nonsense")))

print("\n\033[1mGoogle ID token\033[0m")
claims = auth.decode_id_token(make_id_token(), verify_signature=False)
check("valid token decodes", claims["email"] == "boss@rebuy.ug")
check("wrong audience rejected", raises(lambda: auth.decode_id_token(make_id_token(aud="other"), verify_signature=False)))
check("wrong issuer rejected", raises(lambda: auth.decode_id_token(make_id_token(iss="https://evil.example"), verify_signature=False)))
check("expired token rejected", raises(lambda: auth.decode_id_token(make_id_token(exp=int(time.time()) - 100), verify_signature=False)))
check("malformed token rejected", raises(lambda: auth.decode_id_token("a.b", verify_signature=False)))

print("\n\033[1maccount provisioning\033[0m")
u = auth.provision_google_user({"email": "boss@rebuy.ug", "sub": "g-1", "name": "Boss", "email_verified": True})
check("REBUY_ADMIN_EMAILS promotes to admin", u["role"] == "admin")
u_again = auth.provision_google_user({"email": "boss@rebuy.ug", "sub": "g-1", "name": "Boss", "email_verified": True})
check("repeat sign-in reuses the account", u_again["id"] == u["id"])
check("google user has no password", not u_again["password_hash"])
check("google_sub stored", u_again["google_sub"] == "g-1")

staff = auth.provision_google_user({"email": "staffer@shop.ug", "sub": "g-2", "name": "Staff", "email_verified": True})
check("new google user defaults to owner", staff["role"] == "owner")
check("google user is email-verified", staff["email_verified"] == 1)

config.ALLOWED_EMAILS = ["only@rebuy.ug"]
check("allow-list blocks outsiders",
      raises(lambda: auth.provision_google_user({"email": "nope@x.ug", "sub": "g-3", "email_verified": True}), "not_invited"))
config.ALLOWED_EMAILS = []

print("\n\033[1mpassword policy\033[0m")
for pw, want in [("short", False), ("onlyletters", False), ("password", False),
                 ("Long-Enough-Pass9", True), ("1234567890a", True)]:
    check(f"{pw!r} -> {'accepted' if want else 'rejected'}",
          raises(lambda p=pw: auth.validate_password(p)) is not want)

print("\n\033[1mpassword hashing\033[0m")
h, salt = auth.hash_password("Correct-Horse-9")
check("verify accepts the right password", auth.verify_password("Correct-Horse-9", h, salt))
check("verify rejects the wrong password", not auth.verify_password("Correct-Horse-8", h, salt))
check("unique salt per hash", auth.hash_password("Correct-Horse-9")[1] != salt)

print("\n\033[1msessions\033[0m")
sess = auth.start_session(staff)
check("session resolves", (auth.session_user(sess) or {}).get("email") == "staffer@shop.ug")
check("token is not stored in the clear",
      db.q1("SELECT token_hash FROM sessions WHERE user_id=?", (staff["id"],))["token_hash"] != sess)
auth.logout(sess)
check("logout invalidates the session", auth.session_user(sess) is None)
s2, s3 = auth.start_session(staff), auth.start_session(staff)
check("two live sessions", len(auth.list_sessions(staff["id"])) == 2)
auth.logout_everywhere(staff["id"])
check("logout-all invalidates both", auth.session_user(s2) is None and auth.session_user(s3) is None)

print("\n\033[1mlogin throttling\033[0m")
auth.provision_password_user(None, "owner", "Throttle Test", "throttle@shop.ug", "Good-Password-1")
check("6 bad attempts lock the account",
      (lambda: ([auth.login("throttle@shop.ug", "wrong") for _ in range(6)], auth.is_locked("throttle@shop.ug"))[1])())
check("correct password still refused while locked", auth.login("throttle@shop.ug", "Good-Password-1") is None)
db.meta_set("login_locked:throttle@shop.ug", "")
check("clearing the lock restores login", auth.login("throttle@shop.ug", "Good-Password-1") is not None)

print("\n\033[1mpassword change\033[0m")
auth.provision_password_user(None, "owner", "Change Test", "change@shop.ug", "First-Password-1")
cu = auth.find_user("change@shop.ug")
live = auth.start_session(cu)
check("wrong current password refused", raises(lambda: auth.change_password(cu["id"], "nope", "Second-Password-2"), "bad_current_password"))
auth.change_password(cu["id"], "First-Password-1", "Second-Password-2")
check("existing sessions revoked on change", auth.session_user(live) is None)
check("new password works", auth.login("change@shop.ug", "Second-Password-2") is not None)
check("old password dead", auth.login("change@shop.ug", "First-Password-1") is None)

print("\n\033[1mpassword reset\033[0m")
import hashlib  # noqa: E402
import secrets as _secrets  # noqa: E402

auth.provision_password_user(None, "owner", "Reset Test", "resetme@shop.ug", "Original-Pass-1")
ru = auth.find_user("resetme@shop.ug")


def issue_reset(user_id, ttl_hours=1):
    from datetime import timedelta
    tok = _secrets.token_urlsafe(32)
    db.insert("password_resets", {"token_hash": hashlib.sha256(tok.encode()).hexdigest(),
                                 "user_id": user_id,
                                 "expires_at": (db.utcnow() + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S"),
                                 "created_at": db.now_iso()})
    return tok


good_tok = issue_reset(ru["id"])
okrow = db.q1("SELECT * FROM password_resets WHERE token_hash=?", (hashlib.sha256(good_tok.encode()).hexdigest(),))
check("reset token stored hashed", okrow is not None and okrow["token_hash"] != good_tok)
check("reset token starts unused", okrow["used_at"] is None)
auth.set_password(ru["id"], "Replaced-Pass-2")
db.x("UPDATE password_resets SET used_at=? WHERE token_hash=?", (db.now_iso(), okrow["token_hash"]))
check("reset password takes effect", auth.login("resetme@shop.ug", "Replaced-Pass-2") is not None)
check("old password dead after reset", auth.login("resetme@shop.ug", "Original-Pass-1") is None)
check("used token marked", db.q1("SELECT used_at FROM password_resets WHERE token_hash=?",
                                (okrow["token_hash"],))["used_at"] is not None)

print("\n\033[1mRS256 signature verification\033[0m")
# Build a throwaway RSA keypair and sign a token exactly the way Google does,
# then check the stdlib verifier accepts the real signature and rejects the rest.
import random as _random  # noqa: E402

_rng = _random.Random(1234)
_primes = []


def _is_probable_prime(n, k=24):
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(k):
        a = _rng.randrange(2, n - 1)
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _gen_prime(bits):
    while True:
        c = _rng.getrandbits(bits) | (1 << (bits - 1)) | 1
        if _is_probable_prime(c):
            return c


_p, _q = _gen_prime(512), _gen_prime(512)
_N, _E = _p * _q, 65537
_D = pow(_E, -1, (_p - 1) * (_q - 1))
_KID = "test-kid-1"


def _sign_rs256(signing_input: bytes) -> bytes:
    digest_info = bytes.fromhex("3031300d060960864801650304020105000420") + hashlib.sha256(signing_input).digest()
    k = (_N.bit_length() + 7) // 8
    ps = b"\xff" * (k - len(digest_info) - 3)
    em = b"\x00\x01" + ps + b"\x00" + digest_info
    return pow(int.from_bytes(em, "big"), _D, _N).to_bytes(k, "big")


import app.auth as _authmod  # noqa: E402


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _signed_token(**overrides):
    now = int(time.time())
    payload = {"iss": "https://accounts.google.com", "aud": config.GOOGLE_CLIENT_ID,
               "exp": now + 600, "email": "boss@rebuy.ug", "email_verified": True,
               "sub": "g-1", "kid": _KID}
    payload.update(overrides)
    head = _b64u(json.dumps({"alg": "RS256", "kid": _KID, "typ": "JWT"}).encode())
    body = _b64u(json.dumps(payload).encode())
    sig = _sign_rs256(f"{head}.{body}".encode())
    return f"{head}.{body}.{_b64u(sig)}"


# point the JWKS fetch at our own key
_authmod._http_json = lambda url, data=None, headers=None: {
    "keys": [{"kid": _KID, "kty": "RSA", "alg": "RS256", "use": "sig",
              "n": _b64u(_N.to_bytes((_N.bit_length() + 7) // 8, "big")), "e": _b64u(_E.to_bytes(3, "big"))}]}

real = _signed_token()
check("valid RS256 signature accepted", _authmod.decode_id_token(real)["email"] == "boss@rebuy.ug")

tampered_head, tampered_body, tampered_sig = real.split(".")
flipped = bytearray(_unb64_sig := base64.urlsafe_b64decode(tampered_sig + "=" * (-len(tampered_sig) % 4)))
flipped[10] ^= 0x01
bad = f"{tampered_head}.{tampered_body}.{_b64u(bytes(flipped))}"
check("corrupted signature rejected", raises(lambda: _authmod.decode_id_token(bad), "google_bad_token"))

other_body = _b64u(json.dumps({"iss": "https://accounts.google.com", "aud": config.GOOGLE_CLIENT_ID,
                               "exp": int(time.time()) + 600, "email": "attacker@evil.ug",
                               "email_verified": True, "sub": "g-9", "kid": _KID}).encode())
swapped = f"{tampered_head}.{other_body}.{tampered_sig}"
check("swapped payload rejected", raises(lambda: _authmod.decode_id_token(swapped), "google_bad_token"))

_authmod._http_json = lambda url, data=None, headers=None: {"keys": []}
check("unknown signing key rejected", raises(lambda: _authmod.decode_id_token(real), "google_bad_token"))

print("\n\033[1mproduction gates\033[0m")
import importlib  # noqa: E402

_prev_env = os.environ.get("REBUY_ENV")
os.environ["REBUY_ENV"] = "production"
os.environ["REBUY_SEED_DEMO"] = "1"
os.environ["REBUY_SECRET_KEY"] = "p" * 48
import app.config as _cfg  # noqa: E402
importlib.reload(_cfg)
check("REBUY_SEED_DEMO=1 cannot enable seeding in production", _cfg.SEED_DEMO is False)
check("self-serve signup is closed in production", _cfg.ALLOW_SIGNUP is False)
check("cookies are Secure in production", _cfg.COOKIE_SECURE is True)
os.environ["REBUY_ENV"] = _prev_env or "development"
os.environ.pop("REBUY_SEED_DEMO", None)
importlib.reload(_cfg)

# ------------------------------------------------------------------ static page
# Every browser sends Accept-Encoding: gzip, so a stale .gz twin serves the old
# page to every visitor while a plain curl shows the new one. That exact bug
# shipped once. These checks make it impossible to ship silently again.
def test_static_gzip_twins():
    import gzip
    import os
    from app import httpd

    root = os.path.dirname(ROOT)
    for name in ("index.html", "admin.html"):
        raw = os.path.join(root, "static", name)
        gz = raw + ".gz"
        if not os.path.exists(raw):
            continue
        check(f"{name}: gzip twin exists", os.path.exists(gz),
              "run python3 scripts/resync_static.py")
        if not os.path.exists(gz):
            continue
        # the twin must decompress to exactly the current file
        with open(gz, "rb") as f:
            same = gzip.decompress(f.read()) == open(raw, "rb").read()
        check(f"{name}: gzip twin matches source", same,
              "stale twin — run scripts/resync_static.py")
        check(f"{name}: twin not reported stale", not httpd._gz_is_stale(gz, raw))

    # a deliberately stale twin must be refused rather than served
    bad = os.path.join(ROOT, "_stale_test.html.gz")
    with gzip.open(bad, "wb") as f:
        f.write(b"<html>old</html>")
    check("a stale twin is detected", httpd._gz_is_stale(bad, os.path.join(root, "static", "index.html")))
    os.unlink(bad)


test_static_gzip_twins()

print(f"\n\033[1m{len(PASSED)} passed, {len(FAILED)} failed\033[0m")
if FAILED:
    print("failed:", ", ".join(FAILED))
try:
    os.remove(DB)
except OSError:
    pass
sys.exit(1 if FAILED else 0)
