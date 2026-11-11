"""Care routes: client records, services, recalls, retention.

Mounted as its own module so the retail API stays untouched. Every route here
is shop-scoped and every read of a client record writes an access-log entry
(see app/privacy.py). Import and export are logged as such, because bulk
movement of medical data is exactly what an audit trail exists to capture.
"""
from __future__ import annotations

import base64
import json

from . import care, db, imports, privacy
from .httpd import ApiError, route


def _ctx_ids(ctx):
    return ctx.shop_id, getattr(ctx, "user", None) or {}


def _actor(ctx) -> str:
    u = getattr(ctx, "user", None) or {}
    return u.get("email") or "unknown"


def _ip(ctx) -> str:
    try:
        fwd = (ctx.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        return fwd or (ctx.headers.get("x-real-ip") or "")
    except Exception:  # noqa: BLE001
        return ""


def _ua(ctx) -> str:
    try:
        return (ctx.headers.get("user-agent") or "")[:200]
    except Exception:  # noqa: BLE001
        return ""


# ------------------------------------------------------------------ overview
@route("GET", "/api/care/overview", "shop")
def care_overview(ctx):
    return care.overview(ctx.shop_id)


@route("GET", "/api/care/privacy", "shop")
def care_privacy(ctx):
    """What the privacy layer is doing on this deployment, and the audit trail."""
    return {"status": privacy.status(), "audit": privacy.access_summary(ctx.shop_id, 30)}


# ------------------------------------------------------------------ services
@route("GET", "/api/care/services", "shop")
def care_services(ctx):
    return {"rows": care.list_services(ctx.shop_id, active_only=ctx.q("all") != "1")}


@route("POST", "/api/care/services", "shop")
def care_service_create(ctx):
    try:
        sid = care.upsert_service(ctx.shop_id, ctx.body if isinstance(ctx.body, dict) else {})
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_service")
    privacy.log_access(_actor(ctx), ctx.shop_id, 0, "service_create", f"service #{sid}", _ip(ctx), _ua(ctx))
    return {"ok": True, "id": sid}


@route("PATCH", "/api/care/services/:sid", "shop")
def care_service_update(ctx):
    data = dict(ctx.body if isinstance(ctx.body, dict) else {})
    data["id"] = int(ctx.params["sid"])
    try:
        sid = care.upsert_service(ctx.shop_id, data)
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_service")
    return {"ok": True, "id": sid}


@route("POST", "/api/care/services/seed", "shop")
def care_service_seed(ctx):
    n = care.seed_clinic_services(ctx.shop_id)
    return {"ok": True, "created": n}


# ------------------------------------------------------------------- clients
@route("GET", "/api/care/clients", "shop")
def care_clients(ctx):
    """Directory view. Sensitive columns stay sealed and are not returned."""
    return care.list_clients(ctx.shop_id, q=ctx.q("q", ""),
                             branch_id=int(ctx.q("branch_id")) if ctx.q("branch_id") else None,
                             due_only=ctx.q("due") == "1",
                             limit=min(int(ctx.q("limit", 100) or 100), 500),
                             offset=int(ctx.q("offset", 0) or 0))


@route("GET", "/api/care/clients/:cid", "shop")
def care_client_get(ctx):
    row = care.get_client(ctx.shop_id, int(ctx.params["cid"]), _actor(ctx), _ip(ctx), _ua(ctx))
    if not row:
        raise ApiError("no such client", 404, "not_found")
    visits = db.q("""SELECT id, visited_on, service_name, clinician, amount, branch_id
                     FROM client_visits WHERE shop_id=? AND client_id=?
                     ORDER BY visited_on DESC LIMIT 50""", (ctx.shop_id, row["id"]))
    recalls = db.q("""SELECT id, service_name, due_on, status, completed_on
                      FROM recalls WHERE shop_id=? AND client_id=?
                      ORDER BY due_on DESC LIMIT 50""", (ctx.shop_id, row["id"]))
    return {"client": row, "visits": visits, "recalls": recalls}


@route("POST", "/api/care/clients", "shop")
def care_client_create(ctx):
    try:
        cid = care.create_client(ctx.shop_id, ctx.body if isinstance(ctx.body, dict) else {},
                                 _actor(ctx), _ip(ctx), _ua(ctx))
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_client")
    return {"ok": True, "id": cid}


@route("PATCH", "/api/care/clients/:cid", "shop")
def care_client_update(ctx):
    ok = care.update_client(ctx.shop_id, int(ctx.params["cid"]),
                            ctx.body if isinstance(ctx.body, dict) else {},
                            _actor(ctx), _ip(ctx), _ua(ctx))
    if not ok:
        raise ApiError("no such client", 404, "not_found")
    return {"ok": True}


@route("POST", "/api/care/clients/:cid/optout", "shop")
def care_client_optout(ctx):
    wanted = not (isinstance(ctx.body, dict) and ctx.body.get("opted_in") is False)
    ok = care.set_optout(ctx.shop_id, int(ctx.params["cid"]), wanted, _actor(ctx))
    if not ok:
        raise ApiError("no such client", 404, "not_found")
    return {"ok": True, "opted_in": wanted}


@route("POST", "/api/care/clients/:cid/visits", "shop")
def care_client_visit(ctx):
    try:
        out = care.record_visit(ctx.shop_id, int(ctx.params["cid"]),
                                ctx.body if isinstance(ctx.body, dict) else {},
                                _actor(ctx), _ip(ctx), _ua(ctx))
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_visit")
    return {"ok": True, **out}


# ------------------------------------------------------------------- recalls
@route("GET", "/api/care/recalls", "shop")
def care_recalls(ctx):
    status = ctx.q("status", "due")
    limit = min(int(ctx.q("limit", 200) or 200), 500)
    if status == "due":
        return {"rows": care.due_recalls(ctx.shop_id, ctx.q("on_or_before") or None, limit)}
    return {"rows": db.q("""SELECT r.*, c.name AS client_name, c.phone, c.opted_in
                            FROM recalls r JOIN clients c ON c.id=r.client_id
                            WHERE r.shop_id=? AND r.status=?
                            ORDER BY r.due_on DESC LIMIT ?""", (ctx.shop_id, status, limit))}


@route("POST", "/api/care/recalls/:rid", "shop")
def care_recall_mark(ctx):
    body = ctx.body if isinstance(ctx.body, dict) else {}
    try:
        ok = care.mark_recall(ctx.shop_id, int(ctx.params["rid"]),
                              (body.get("status") or "completed"), _actor(ctx),
                              body.get("when"))
    except ValueError as e:
        raise ApiError(str(e), 400, "bad_status")
    if not ok:
        raise ApiError("no such recall", 404, "not_found")
    return {"ok": True}


@route("GET", "/api/care/retention", "shop")
def care_retention(ctx):
    days = min(int(ctx.q("days", 365) or 365), 1095)
    branch = int(ctx.q("branch_id")) if ctx.q("branch_id") else None
    return care.retention(ctx.shop_id, days, branch)


# -------------------------------------------------------------------- import
@route("POST", "/api/care/import/preview", "shop")
def care_import_preview(ctx):
    """Dry run: show the column mapping and the first rejects before importing."""
    body = ctx.body if isinstance(ctx.body, dict) else {}
    filename = body.get("filename") or "clients.csv"
    data = body.get("data") or body.get("data_url") or ""
    if not data:
        raise ApiError("no file data received", 400, "no_data")
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        raw = base64.b64decode(data.split(",", 1)[1])
    else:
        raw = data.encode()
    headers, rows = imports.read_client_rows(filename, raw)
    if not headers:
        raise ApiError("that file has no readable rows", 400, "empty_file")
    mapping = imports.map_client_headers(headers)
    records = []
    for r in rows[:500]:
        rec = {}
        for field, idx in mapping.items():
            rec[field] = str(r[idx]).strip() if idx < len(r) else ""
        if any(rec.values()):
            records.append(rec)
    checked = imports.validate_client_records(records)
    rejected = [c for c in checked if not c["ok"]][:10]
    return {
        "mapping": {k: headers[v] if v < len(headers) else "" for k, v in mapping.items()},
        "headers": headers,
        "rows_found": len(rows),
        "sample": checked[:5],
        "would_reject": len([c for c in checked if not c["ok"]]),
        "reject_examples": [{"row": c["_row"], "name": c["name"],
                             "why": "; ".join(c["problems"])} for c in rejected],
        "unmapped_columns": [h for i, h in enumerate(headers) if i not in set(mapping.values())],
        "will_encrypt": list(privacy.ENCRYPTED_FIELDS),
    }


@route("POST", "/api/care/import", "shop")
def care_import(ctx):
    """Commit an uploaded client file."""
    body = ctx.body if isinstance(ctx.body, dict) else {}
    filename = body.get("filename") or "clients.csv"
    data = body.get("data") or body.get("data_url") or ""
    if not data:
        raise ApiError("no file data received", 400, "no_data")
    if "," in data and data.split(",", 1)[0].startswith("data:"):
        raw = base64.b64decode(data.split(",", 1)[1])
    else:
        raw = data.encode()
    if len(raw) > 40 * 1024 * 1024:
        raise ApiError("that file is larger than 40 MB — split it and import in parts", 413, "too_large")
    headers, rows = imports.read_client_rows(filename, raw)
    if not headers:
        raise ApiError("that file has no readable rows", 400, "empty_file")
    mapping = imports.map_client_headers(headers)
    if "name" not in mapping and "phone" not in mapping and "identifier" not in mapping:
        raise ApiError(
            "could not find a name, phone or file-number column in that file. "
            "Expected something like Patient Name / Phone / File No.", 400, "no_identity_columns")
    options = {
        "update_existing": body.get("update_existing", True),
        "schedule_recalls": body.get("schedule_recalls", True),
        "branch_id": body.get("branch_id") or None,
        "branch_label": body.get("branch_label") or "",
        "filename": filename,
    }
    return imports.import_clients(ctx.shop_id, rows, mapping, options, _actor(ctx))


# -------------------------------------------------------------------- export
@route("GET", "/api/care/export", "shop")
def care_export(ctx):
    """Export the client list as CSV. Logged as a bulk disclosure."""
    import csv
    import io

    rows = db.q("""SELECT id, name, phone, whatsapp, email, area, branch_label, tags,
                          opted_in, last_visit_at, last_service, next_due_on, visits_count
                   FROM clients WHERE shop_id=? ORDER BY name""", (ctx.shop_id,))
    privacy.log_access(_actor(ctx), ctx.shop_id, 0, "export",
                       f"exported {len(rows)} client records", _ip(ctx), _ua(ctx))
    buf = io.StringIO()
    w = csv.writer(buf)
    cols = ["id", "name", "phone", "whatsapp", "email", "area", "branch_label", "tags",
            "opted_in", "last_visit_at", "last_service", "next_due_on", "visits_count"]
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return 200, {"content-type": "text/csv; charset=utf-8",
                 "content-disposition": 'attachment; filename="clients.csv"',
                 "cache-control": "no-store"}, buf.getvalue().encode()
