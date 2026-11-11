"""HTTP layer: a tiny router + a stdlib server (no external dependencies).

The same `dispatch()` entry point is reused by the Vercel serverless adapter in
api/index.py, so local, Docker and Vercel all run identical code.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import auth, db

ROUTES: list[tuple[str, re.Pattern, object, str]] = []
STATIC_DIR = os.environ.get("REBUY_STATIC_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")


class Ctx:
    def __init__(self, method, path, query, headers, body, user, params, raw):
        self.method = method
        self.path = path
        self.query = query
        self.headers = headers
        self.body = body
        self.user = user
        self.params = params
        self.raw = raw
        self.shop_id = user["shop_id"] if user else None

    def q(self, key, default=None):
        v = self.query.get(key)
        return v[0] if v else default

    def qi(self, key, default=0) -> int:
        try:
            return int(self.q(key, default))
        except (TypeError, ValueError):
            return default

    def b(self, key, default=None):
        if not isinstance(self.body, dict):
            return default
        v = self.body.get(key, default)
        return default if v is None else v

    def bi(self, key, default=0) -> int:
        try:
            return int(self.b(key, default))
        except (TypeError, ValueError):
            return default


def route(method: str, pattern: str, need: str = "user"):
    """need: none | user | shop | staff | admin"""
    rx = re.compile("^" + re.sub(r":(\w+)", r"(?P<\1>[^/]+)", pattern) + "$")

    def deco(fn):
        ROUTES.append((method.upper(), rx, fn, need))
        return fn
    return deco


class ApiError(Exception):
    def __init__(self, message, status=400, code="error"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _parse_body(raw: bytes, content_type: str):
    if not raw:
        return {}
    ct = (content_type or "").lower()
    try:
        if "json" in ct:
            return json.loads(raw.decode("utf-8", errors="replace"))
        if "form-urlencoded" in ct:
            return {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return {}


def dispatch(method: str, path: str, headers: dict, raw: bytes) -> tuple[int, dict, bytes]:
    """Returns (status, headers, body_bytes)."""
    parsed = urllib.parse.urlparse(path)
    query = urllib.parse.parse_qs(parsed.query)
    route_path = parsed.path.rstrip("/") or "/"
    cookie_header = headers.get("cookie") or headers.get("Cookie") or ""
    token = None
    if cookie_header:
        c = SimpleCookie(cookie_header)
        if "rebuy_session" in c:
            token = c["rebuy_session"].value
    bearer = (headers.get("authorization") or headers.get("Authorization") or "")
    if bearer.lower().startswith("bearer "):
        token = bearer[7:].strip() or token
    if method.upper() in ("GET", "HEAD") and not parsed.path.startswith("/api/"):
        static = serve_file(parsed.path, headers.get("accept-encoding") or headers.get("Accept-Encoding") or "")
        if static:
            return static

    user = auth.session_user(token)
    body = _parse_body(raw, headers.get("content-type") or headers.get("Content-Type") or "")

    for m, rx, fn, need in ROUTES:
        if m != method.upper():
            continue
        mt = rx.match(route_path)
        if not mt:
            continue
        params = mt.groupdict()
        if need != "none" and not user:
            return _json({"error": "authentication required"}, 401)
        if need == "admin" and (not user or user["role"] != "admin"):
            return _json({"error": "platform admin access required"}, 403)
        if need in ("shop", "staff") and (not user or not user["shop_id"]):
            return _json({"error": "shop account required"}, 403)
        ctx = Ctx(method.upper(), route_path, query, headers, body, user, params, raw)
        try:
            out = fn(ctx)
        except ApiError as e:
            return _json({"error": e.message, "code": e.code}, e.status)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            return _json({"error": f"{type(e).__name__}: {e}"}, 500)
        if isinstance(out, tuple) and len(out) == 3 and isinstance(out[1], dict) and isinstance(out[2], bytes):
            return out
        if isinstance(out, tuple) and len(out) == 2:
            status, payload = out
            if isinstance(payload, (dict, list)):
                return _json(payload, status)
            return status, {"content-type": "text/plain"}, str(payload).encode()
        return _json(out if out is not None else {"ok": True})
    return _json({"error": f"no route for {method} {route_path}"}, 404)


def _json(payload, status: int = 200, extra_headers: dict | None = None) -> tuple[int, dict, bytes]:
    body = json.dumps(payload, default=str).encode()
    hdrs = {"content-type": "application/json; charset=utf-8", "content-length": str(len(body)),
            "cache-control": "no-store"}
    if extra_headers:
        hdrs.update(extra_headers)
    return status, hdrs, body


# Static pages are also kept compressed (brotli-quality gzip) so a serverless
# bundle can ship them as ~50 KB of base64 instead of 200 KB. serve_file() uses
# the .gz twin when the client accepts gzip.
STATIC_GZ_DIR = os.environ.get("REBUY_STATIC_GZ", STATIC_DIR)


def _send_gzip(rel: str, ctype: str) -> tuple[int, dict, bytes] | None:
    gz_path = os.path.join(STATIC_GZ_DIR, rel + ".gz")
    if not os.path.isfile(gz_path):
        return None
    with open(gz_path, "rb") as f:
        data = f.read()
    return 200, {"content-type": ctype, "content-encoding": "gzip", "content-length": str(len(data)),
                 "cache-control": "no-cache" if rel.endswith(".html") else "public, max-age=300"}, data


def serve_file(path: str, accept_encoding: str = "") -> tuple[int, dict, bytes] | None:
    rel = path.lstrip("/")
    if rel in ("", "index.html"):
        rel = "index.html"
    if rel == "admin":
        rel = "admin.html"
    ext0 = os.path.splitext(rel)[1].lower()
    if ext0 in (".html", ".css", ".js") and "gzip" in (accept_encoding or ""):
        ctype0 = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                  ".js": "application/javascript; charset=utf-8"}[ext0]
        gz = _send_gzip(rel, ctype0)
        if gz:
            return gz
    full = os.path.normpath(os.path.join(STATIC_DIR, rel))
    if not full.startswith(os.path.normpath(STATIC_DIR)) or not os.path.isfile(full):
        return None
    ext = os.path.splitext(full)[1].lower()
    ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
             ".js": "application/javascript; charset=utf-8", ".json": "application/json",
             ".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg",
             ".ico": "image/x-icon", ".woff2": "font/woff2", ".csv": "text/csv"}.get(ext, "application/octet-stream")
    with open(full, "rb") as f:
        data = f.read()
    return 200, {"content-type": ctype, "content-length": str(len(data)),
                 "cache-control": "no-cache" if ext == ".html" else "public, max-age=300"}, data


MAX_BODY_BYTES = int(os.environ.get("REBUY_MAX_BODY", str(8 * 1024 * 1024)))
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("REBUY_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _cross_origin(headers: dict) -> bool:
    """Reject state-changing requests that a browser marked as coming from
    another site. Same-origin form posts and fetch() calls send neither header
    or send a matching Origin."""
    site = (headers.get("sec-fetch-site") or "").lower()
    if site and site not in ("same-origin", "none"):
        return True
    origin = headers.get("origin") or ""
    if not origin:
        return False
    allowed = set(ALLOWED_HOSTS)
    try:
        host = urllib.parse.urlparse(origin).netloc.lower()
    except ValueError:
        return True
    if allowed:
        return host not in allowed
    server_host = (headers.get("host") or "").lower()
    return bool(server_host) and host != server_host


CSP = os.environ.get("REBUY_CSP") or (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'self'; "
    "base-uri 'self'; form-action 'self'"
)

SECURITY_HEADERS = {
    "x-content-type-options": "nosniff",
    "x-frame-options": "SAMEORIGIN",
    "referrer-policy": "strict-origin-when-cross-origin",
    "permissions-policy": "geolocation=(), microphone=(), camera=()",
    "x-xss-protection": "0",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "Rebuy"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def _headers_dict(self) -> dict:
        return {k.lower(): v for k, v in self.headers.items()}

    def _run(self, method: str):
        length = int(self.headers.get("content-length") or 0)
        if length > MAX_BODY_BYTES:
            self._respond(413, {"content-type": "application/json"}, b'{"error":"payload too large"}')
            return
        raw = self.rfile.read(length) if length else b""
        if method == "OPTIONS":
            # same-origin only: there is no legitimate cross-site API consumer
            self._respond(204, {"allow": "GET,POST,PATCH,PUT,DELETE,OPTIONS", "content-length": "0"}, b"")
            return
        hdrs_in = self._headers_dict()
        if method not in ("GET", "HEAD") and _cross_origin(hdrs_in):
            self._respond(403, {"content-type": "application/json"},
                          b'{"error":"cross-site request blocked","code":"csrf"}')
            return
        status, hdrs, body = dispatch(method, self.path, hdrs_in, raw)
        self._respond(status, hdrs, body)

    def _respond(self, status: int, hdrs: dict, body: bytes):
        self.send_response(status)
        merged = dict(SECURITY_HEADERS)
        merged.update({k.lower(): v for k, v in hdrs.items()})
        if merged.get("content-type", "").startswith("text/html"):
            merged.setdefault("content-security-policy", CSP)
        for k, v in merged.items():
            self.send_header(k, str(v))
        if "content-length" not in {k.lower() for k in merged}:
            self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            try:
                self.wfile.write(body)
            except BrokenPipeError:
                pass

    def do_GET(self):
        static = serve_file(self.path.split("?")[0], self.headers.get("accept-encoding", ""))
        if static and not self.path.startswith("/api/"):
            self._respond(*static)
            return
        self._run("GET")

    def do_POST(self):
        self._run("POST")

    def do_PATCH(self):
        self._run("PATCH")

    def do_PUT(self):
        self._run("PUT")

    def do_DELETE(self):
        self._run("DELETE")

    def do_OPTIONS(self):
        self._run("OPTIONS")

    def do_HEAD(self):
        self._run("GET")

    def log_message(self, fmt, *args):
        if os.environ.get("REBUY_QUIET") != "1":
            print(f"[{self.log_date_time_string()}] {fmt % args}")


def run(host: str = "0.0.0.0", port: int = 8000):
    db.init_schema()
    from . import api  # noqa: F401  (registers routes)
    from . import api_admin  # noqa: F401
    from . import webhooks  # noqa: F401  (registers webhook + cron routes)
    from . import seed
    seed.ensure_bootstrapped()
    from . import worker
    worker.start()
    srv = ThreadingHTTPServer((host, port), Handler)
    srv.daemon_threads = True
    print(f"Rebuy running on http://{host}:{port}  (db: {db.DB_PATH})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("shutting down")
