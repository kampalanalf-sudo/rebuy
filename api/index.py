"""Vercel serverless adapter for Rebuy.

Every request is rewritten to this function by `vercel.json`:

    /(.*)  ->  /api/index?__path=$1

so the original path arrives in the `__path` query parameter. The adapter then
hands the request to the exact same `httpd.dispatch()` used by the local
server, which means the API, webhooks, admin routes and static files all behave
identically on Vercel and locally.

Serverless notes
----------------
* The background worker thread is disabled (REBUY_WORKER=0). Vercel Cron hits
  /api/cron/tick every 5 minutes to advance the reminder queue instead.
* Set REBUY_DATABASE_URL (Supabase/Postgres session-pooler string) and every
  request reads/writes the managed database — no local state at all. Without
  it, SQLite falls back to /tmp/rebuy.db, which is EPHEMERAL and per-instance.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("REBUY_WORKER", "0")
if not os.environ.get("REBUY_DB"):
    # /var/task is read-only on Vercel; only /tmp is writable
    os.environ["REBUY_DB"] = "/tmp/rebuy.db" if os.environ.get("VERCEL") else str(ROOT / "data" / "rebuy.db")

from app import httpd  # noqa: E402

_BOOTED = False


def _bootstrap() -> None:
    """Import the route modules and make sure the schema + demo fleet exist."""
    global _BOOTED
    if _BOOTED:
        return
    from app import db

    db.init_schema()
    from app import api, api_admin, webhooks  # noqa: F401,E402  (register routes)
    try:
        from app import seed

        seed.ensure_bootstrapped()
    except Exception as exc:  # pragma: no cover - never block the request on seeding
        print("rebuy seed skipped:", exc)
    _BOOTED = True


class handler(httpd.BaseHTTPRequestHandler):  # noqa: N801  (Vercel expects this name)
    server_version = "Rebuy-Vercel/1.0"
    protocol_version = "HTTP/1.1"

    def _headers(self) -> dict:
        return {k.lower(): v for k, v in self.headers.items()}

    def _run(self, method: str) -> None:
        try:
            _bootstrap()
        except Exception as exc:  # pragma: no cover
            return self._send(500, {"content-type": "text/plain"}, f"bootstrap failed: {exc}".encode())

        raw_path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query, keep_blank_values=True)
        if "__path" in params:
            path = params.pop("__path")[0]
        else:
            path = raw_path
        path = path or "/"
        if not path.startswith("/"):
            path = "/" + path
        rest = urllib.parse.urlencode(params, doseq=True)
        full_path = path + (("?" + rest) if rest else "")

        if method in ("GET", "HEAD") and not path.startswith("/api/"):
            # Vercel strips originals in favour of pre-compressed .gz twins
            static = httpd.serve_file(path, self.headers.get("accept-encoding", "gzip"))
            if static:
                return self._send(*static)

        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        status, headers, body = httpd.dispatch(method, full_path, self._headers(), raw)
        self._send(status, headers, body)

    def _send(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, str(value))
        if "content-length" not in {k.lower() for k in headers}:
            self.send_header("content-length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        self._run("GET")

    def do_HEAD(self):  # noqa: N802
        self._run("GET")

    def do_POST(self):  # noqa: N802
        self._run("POST")

    def do_PATCH(self):  # noqa: N802
        self._run("PATCH")

    def do_PUT(self):  # noqa: N802
        self._run("PUT")

    def do_DELETE(self):  # noqa: N802
        self._run("DELETE")

    def do_OPTIONS(self):  # noqa: N802
        self._send(204, {"access-control-allow-origin": "*", "access-control-allow-headers": "*",
                         "access-control-allow-methods": "GET,POST,PATCH,PUT,DELETE,OPTIONS",
                         "content-length": "0"}, b"")

    def log_message(self, fmt, *args):  # keep Vercel logs readable
        print("[rebuy]", fmt % args)
