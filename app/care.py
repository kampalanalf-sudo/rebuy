"""Care-specific logic: client records, service recalls and retention.

A clinic does not "sell products that run out". It delivers a service and the
client becomes due again later — a dental scaling in 6 months, an antenatal
review in 4 weeks, an annual screening. This module turns that model into
scheduled recalls, and turns recall outcomes into the retention numbers a
hospital actually reports on.

Three ideas carry the whole thing:

* **Service** — has a `recall_days`. Recording a visit against a service
  schedules the next recall automatically. `recall_days = 0` means one-off.
* **Recall** — one row per client per due date. It moves due -> booked ->
  completed / missed. Completing it schedules the following one.
* **Retention** — computed from recalls, not from purchases: of the clients
  who came due, how many came back. Split by branch and by service so an
  operator can see which department is losing people.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from . import db, privacy

# ------------------------------------------------------------------ services
def list_services(shop_id: int, active_only: bool = True) -> list[dict]:
    where = "shop_id=?" + (" AND active=1" if active_only else "")
    return db.q(f"SELECT * FROM services WHERE {where} ORDER BY department, name", (shop_id,))


def upsert_service(shop_id: int, data: dict) -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("a service needs a name")
    try:
        recall_days = int(data.get("recall_days") or 0)
    except (TypeError, ValueError):
        recall_days = 0
    if recall_days < 0 or recall_days > 3650:
        raise ValueError("recall interval must be between 0 and 3650 days")
    fields = {
        "shop_id": shop_id,
        "name": name[:120],
        "department": (data.get("department") or "")[:80],
        "recall_days": recall_days,
        "price": float(data.get("price") or 0),
        "currency": (data.get("currency") or "UGX")[:8],
        "active": 1 if data.get("active", True) else 0,
    }
    sid = data.get("id")
    if sid:
        row = db.q1("SELECT id FROM services WHERE id=? AND shop_id=?", (sid, shop_id))
        if not row:
            raise ValueError("no such service")
        db.update("services", sid, fields)
        return int(sid)
    fields["created_at"] = db.now_iso()
    return int(db.insert("services", fields))


def seed_clinic_services(shop_id: int) -> int:
    """Starter service catalogue for a health provider."""
    defaults = [
        ("General consultation", "Outpatient", 0, 0),
        ("Dental check-up & scaling", "Dental", 180, 0),
        ("Antenatal review", "Maternity", 28, 0),
        ("Postnatal review", "Maternity", 42, 0),
        ("Immunisation - child", "Paediatrics", 30, 0),
        ("Annual health screening", "Wellness", 365, 0),
        ("Optometry review", "Eye care", 365, 0),
        ("Physiotherapy session", "Rehab", 14, 0),
        ("Chronic care review", "Chronic care", 90, 0),
    ]
    have = {s["name"] for s in db.q("SELECT name FROM services WHERE shop_id=?", (shop_id,))}
    n = 0
    for name, dept, days, price in defaults:
        if name in have:
            continue
        db.insert("services", {"shop_id": shop_id, "name": name, "department": dept,
                               "recall_days": days, "price": price, "currency": "UGX",
                               "active": 1, "created_at": db.now_iso()})
        n += 1
    return n


# ------------------------------------------------------------------- clients
def _blank_to_none(v):
    return None if v == "" else v


def create_client(shop_id: int, data: dict, actor: str = "", ip: str = "", ua: str = "") -> int:
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("a client record needs a name")
    rec = {
        "shop_id": shop_id,
        "name": name[:160],
        "phone": (data.get("phone") or "")[:40],
        "whatsapp": (data.get("whatsapp") or data.get("phone") or "")[:40],
        "email": (data.get("email") or "")[:160],
        "area": (data.get("area") or "")[:80],
        "branch_id": data.get("branch_id") or None,
        "branch_label": (data.get("branch_label") or "")[:80],
        "dob": (data.get("dob") or "")[:20],
        "identifier": (data.get("identifier") or "")[:80],
        "condition": (data.get("condition") or "")[:400],
        "address": (data.get("address") or "")[:300],
        "notes": (data.get("notes") or "")[:1000],
        "tags": (data.get("tags") or "")[:200],
        "language": (data.get("language") or "en")[:8],
        "opted_in": 1 if data.get("opted_in", True) else 0,
        "consent_source": (data.get("consent_source") or "import")[:40],
        "visits_count": 0,
        "created_at": db.now_iso(),
        "updated_at": db.now_iso(),
    }
    cid = int(db.insert("clients", privacy.seal_record(rec)))
    privacy.log_access(actor or "system", shop_id, cid, "create",
                       f"client created: {rec['name'][:60]}", ip, ua)
    return cid


def get_client(shop_id: int, client_id: int, actor: str = "", ip: str = "",
               ua: str = "", purpose: str = "view") -> dict | None:
    """Fetch one client and log the read.

    The access log is written before the caller gets the data, so a failure to
    log is a failure to disclose.
    """
    row = db.q1("SELECT * FROM clients WHERE id=? AND shop_id=?", (client_id, shop_id))
    if not row:
        return None
    privacy.log_access(actor or "system", shop_id, client_id, purpose,
                       f"opened client record: {row.get('name', '')[:60]}", ip, ua)
    return privacy.open_record(row)


def list_clients(shop_id: int, q: str = "", branch_id: int | None = None,
                 due_only: bool = False, limit: int = 100, offset: int = 0) -> dict:
    """List clients. Sensitive columns stay sealed — this is a directory view."""
    where = ["shop_id=?"]
    params: list = [shop_id]
    if q:
        where.append("(LOWER(name) LIKE ? OR phone LIKE ? OR LOWER(email) LIKE ? OR LOWER(identifier) LIKE ?)")
        like = f"%{q.strip().lower()}%"
        params += [like, f"%{q.strip()}%", like, like]
    if branch_id:
        where.append("branch_id=?")
        params.append(branch_id)
    if due_only:
        where.append("next_due_on IS NOT NULL AND next_due_on<=?")
        params.append(db.today())
    clause = " AND ".join(where)
    total = db.scalar(f"SELECT COUNT(*) FROM clients WHERE {clause}", tuple(params), 0)
    rows = db.q(f"""SELECT id, name, phone, whatsapp, email, area, branch_id, branch_label,
                           tags, opted_in, last_visit_at, last_service, next_due_on,
                           visits_count, created_at
                    FROM clients WHERE {clause}
                    ORDER BY COALESCE(next_due_on,'9999') ASC, name ASC
                    LIMIT ? OFFSET ?""", tuple(params + [limit, offset]))
    return {"rows": rows, "total": total, "limit": limit, "offset": offset}


def update_client(shop_id: int, client_id: int, data: dict, actor: str = "",
                  ip: str = "", ua: str = "") -> bool:
    row = db.q1("SELECT * FROM clients WHERE id=? AND shop_id=?", (client_id, shop_id))
    if not row:
        return False
    allowed = ("name", "phone", "whatsapp", "email", "area", "branch_id", "branch_label",
               "dob", "identifier", "condition", "address", "notes", "tags", "language",
               "opted_in")
    patch = {k: data[k] for k in allowed if k in data}
    if not patch:
        return True
    patch["updated_at"] = db.now_iso()
    db.update("clients", client_id, privacy.seal_record(patch))
    changed = ", ".join(sorted(k for k in patch if k != "updated_at"))
    privacy.log_access(actor or "system", shop_id, client_id, "update",
                       f"fields changed: {changed}", ip, ua)
    return True


def set_optout(shop_id: int, client_id: int, opted_in: bool, actor: str = "") -> bool:
    row = db.q1("SELECT id FROM clients WHERE id=? AND shop_id=?", (client_id, shop_id))
    if not row:
        return False
    db.update("clients", client_id, {
        "opted_in": 1 if opted_in else 0,
        "opted_out_at": None if opted_in else db.now_iso(),
        "updated_at": db.now_iso(),
    })
    privacy.log_access(actor or "system", shop_id, client_id,
                       "optin" if opted_in else "optout",
                       "consent changed", "", "")
    return True


# ------------------------------------------------------------------- recalls
def record_visit(shop_id: int, client_id: int, data: dict, actor: str = "",
                 ip: str = "", ua: str = "") -> dict:
    """Record a visit and schedule the next recall for that service.

    This is the heart of the model: one call both writes history and creates
    the future reminder, so retention is a consequence of normal operation
    rather than a separate chore.
    """
    client = db.q1("SELECT * FROM clients WHERE id=? AND shop_id=?", (client_id, shop_id))
    if not client:
        raise ValueError("no such client")
    service_id = data.get("service_id")
    service = None
    if service_id:
        service = db.q1("SELECT * FROM services WHERE id=? AND shop_id=?", (service_id, shop_id))
    visited_on = (data.get("visited_on") or db.today())[:10]
    vid = db.insert("client_visits", {
        "shop_id": shop_id, "client_id": client_id,
        "branch_id": data.get("branch_id") or client.get("branch_id"),
        "service_id": service_id or None,
        "service_name": (service or {}).get("name", data.get("service_name") or "")[:120],
        "visited_on": visited_on,
        "clinician": (data.get("clinician") or "")[:80],
        "amount": float(data.get("amount") or 0),
        "notes": privacy.encrypt((data.get("notes") or "")[:1000]),
        "created_at": db.now_iso(),
    })
    # close any open recall for this service
    if service_id:
        db.x("""UPDATE recalls SET status='completed', completed_on=?, updated_at=?
                WHERE shop_id=? AND client_id=? AND service_id=?
                  AND status IN ('due','booked','missed')""",
             (visited_on, db.now_iso(), shop_id, client_id, service_id))
    next_due = schedule_recall(shop_id, client_id, service, visited_on, actor)
    db.x("""UPDATE clients SET last_visit_at=?, last_service=?, visits_count=visits_count+1,
                   next_due_on=?, updated_at=? WHERE id=?""",
         (visited_on, (service or {}).get("name", "")[:120], next_due,
          db.now_iso(), client_id))
    privacy.log_access(actor or "system", shop_id, client_id, "visit",
                       f"visit recorded{(', service=' + service['name']) if service else ''}", ip, ua)
    return {"visit_id": vid, "next_due_on": next_due}


def schedule_recall(shop_id: int, client_id: int, service: dict | None,
                    from_date: str, actor: str = "") -> str | None:
    """Create the next recall for a service. Returns the due date, or None."""
    if not service or not service.get("recall_days"):
        return None
    try:
        base = datetime.strptime(from_date, "%Y-%m-%d")
    except ValueError:
        base = db.utcnow()
    due = (base + timedelta(days=int(service["recall_days"]))).strftime("%Y-%m-%d")
    client = db.q1("SELECT branch_id FROM clients WHERE id=?", (client_id,))
    rid = db.insert("recalls", {
        "shop_id": shop_id, "client_id": client_id,
        "service_id": service.get("id"), "service_name": (service.get("name") or "")[:120],
        "branch_id": (client or {}).get("branch_id"),
        "due_on": due, "status": "due",
        "created_at": db.now_iso(), "updated_at": db.now_iso(),
    })
    privacy.log_access(actor or "system", shop_id, client_id, "recall",
                       f"recall scheduled for {due} ({(service.get('name') or '')[:50]})", "", "")
    return due


def due_recalls(shop_id: int, on_or_before: str | None = None, limit: int = 500) -> list[dict]:
    day = on_or_before or db.today()
    return db.q("""SELECT r.*, c.name AS client_name, c.phone, c.whatsapp, c.opted_in,
                          c.branch_label
                   FROM recalls r JOIN clients c ON c.id=r.client_id
                   WHERE r.shop_id=? AND r.status='due' AND r.due_on<=?
                     AND c.opted_in=1
                   ORDER BY r.due_on LIMIT ?""", (shop_id, day, limit))


def mark_recall(shop_id: int, recall_id: int, status: str, actor: str = "",
                when: str | None = None) -> bool:
    if status not in ("due", "booked", "completed", "missed", "cancelled"):
        raise ValueError("unknown status")
    row = db.q1("SELECT * FROM recalls WHERE id=? AND shop_id=?", (recall_id, shop_id))
    if not row:
        return False
    patch = {"status": status, "updated_at": db.now_iso()}
    if status == "completed":
        patch["completed_on"] = when or db.today()
    db.update("recalls", recall_id, patch)
    if status == "completed" and row.get("service_id"):
        svc = db.q1("SELECT * FROM services WHERE id=?", (row["service_id"],))
        due = schedule_recall(shop_id, row["client_id"], svc, patch["completed_on"], actor)
        if due:
            db.x("UPDATE clients SET next_due_on=? WHERE id=?", (due, row["client_id"]))
    privacy.log_access(actor or "system", shop_id, row["client_id"], f"recall_{status}",
                       f"recall #{recall_id} -> {status}", "", "")
    return True


# ----------------------------------------------------------------- retention
def retention(shop_id: int, days: int = 365, branch_id: int | None = None) -> dict:
    """Retention from recalls: of those who came due, how many came back.

    Deliberately not "did they purchase again" — for a clinic the meaningful
    event is returning for the service they were recalled for.
    """
    since = db.days_from_today(-days)
    branch_clause = " AND r.branch_id=?" if branch_id else ""
    params: tuple = (shop_id, since) + ((branch_id,) if branch_id else ())

    totals = db.q1(f"""SELECT
            COUNT(*) AS recalls_due,
            SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS completed,
            SUM(CASE WHEN r.status='booked' THEN 1 ELSE 0 END) AS booked,
            SUM(CASE WHEN r.status='missed' THEN 1 ELSE 0 END) AS missed,
            SUM(CASE WHEN r.status IN ('due','booked') THEN 1 ELSE 0 END) AS still_open
        FROM recalls r
        WHERE r.shop_id=? AND r.due_on>=?{branch_clause}""", params) or {}

    due_n = int(totals.get("recalls_due") or 0)
    done = int(totals.get("completed") or 0)
    rate = round(100.0 * done / due_n, 1) if due_n else 0.0

    by_service = db.q(f"""SELECT r.service_name,
                COUNT(*) AS due, SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS came_back
            FROM recalls r
            WHERE r.shop_id=? AND r.due_on>=?{branch_clause}
            GROUP BY r.service_name ORDER BY due DESC""", params)

    by_branch = db.q(f"""SELECT COALESCE(c.branch_label, 'Unassigned') AS branch,
                COUNT(*) AS due, SUM(CASE WHEN r.status='completed' THEN 1 ELSE 0 END) AS came_back
            FROM recalls r JOIN clients c ON c.id=r.client_id
            WHERE r.shop_id=? AND r.due_on>=?{branch_clause}
            GROUP BY COALESCE(c.branch_label,'Unassigned') ORDER BY due DESC""", params)

    for row in by_service + by_branch:
        d = int(row.get("due") or 0)
        cb = int(row.get("came_back") or 0)
        row["rate"] = round(100.0 * cb / d, 1) if d else 0.0

    # clients who have gone quiet: no visit since their last recall came due
    lapsed = db.scalar(f"""SELECT COUNT(*) FROM clients
            WHERE shop_id=? AND (last_visit_at IS NULL OR last_visit_at < ?)""",
                       (shop_id, db.days_from_today(-180)), 0)

    return {
        "window_days": days,
        "recalls_due": due_n,
        "completed": done,
        "booked": int(totals.get("booked") or 0),
        "missed": int(totals.get("missed") or 0),
        "still_open": int(totals.get("still_open") or 0),
        "retention_rate": rate,
        "by_service": by_service,
        "by_branch": by_branch,
        "lapsed_clients": lapsed,
        "clients_total": db.scalar("SELECT COUNT(*) FROM clients WHERE shop_id=?", (shop_id,), 0),
        "opted_out": db.scalar("SELECT COUNT(*) FROM clients WHERE shop_id=? AND opted_in=0",
                               (shop_id,), 0),
    }


def overview(shop_id: int) -> dict:
    today = db.today()
    return {
        "clients": db.scalar("SELECT COUNT(*) FROM clients WHERE shop_id=?", (shop_id,), 0),
        "services": db.scalar("SELECT COUNT(*) FROM services WHERE shop_id=? AND active=1",
                              (shop_id,), 0),
        "branches": db.scalar("SELECT COUNT(*) FROM shop_places WHERE shop_id=? AND active=1",
                              (shop_id,), 0),
        "due_today": db.scalar("""SELECT COUNT(*) FROM recalls WHERE shop_id=? AND status='due'
                                  AND due_on<=?""", (shop_id, today), 0),
        "due_7d": db.scalar("""SELECT COUNT(*) FROM recalls WHERE shop_id=? AND status='due'
                               AND due_on<=?""", (shop_id, db.days_from_today(7)), 0),
        "due_30d": db.scalar("""SELECT COUNT(*) FROM recalls WHERE shop_id=? AND status='due'
                                AND due_on<=?""", (shop_id, db.days_from_today(30)), 0),
        "overdue": db.scalar("""SELECT COUNT(*) FROM recalls WHERE shop_id=? AND status='due'
                                AND due_on<?""", (shop_id, today), 0),
        "privacy": privacy.status(),
    }
