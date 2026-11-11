#!/usr/bin/env python3
"""Tests for the clinical/care layer (app/care.py, app/privacy.py, app/imports.py).

Run:  python3 scripts/test_care.py
"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False).name
os.environ.update({
    "REBUY_DB": _tmp,
    "REBUY_QUIET": "1",
    "REBUY_WORKER": "0",
    "REBUY_ENV": "development",
    "REBUY_SECRET_KEY": "test-only-secret-key-with-enough-length-for-kdf",
})

from app import care, db, imports, privacy  # noqa: E402

PASSED = FAILED = 0


def check(label: str, ok: bool, extra: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAILED += 1
        print(f"  \033[31mFAIL\033[0m  {label}" + (f" — {extra}" if extra else ""))


db.init_schema()
priv = privacy.enabled()
print(f"\n\033[1mprivacy layer\033[0m   ({privacy.mode()})")
check("encryption is active", priv)

if priv:
    sealed = privacy.seal_record({"name": "Grace", "dob": "1988-04-12", "identifier": "FM-1"})
    check("dob is encrypted at rest", sealed["dob"].startswith("enc:v1:"))
    check("identifier is encrypted at rest", sealed["identifier"].startswith("enc:v1:"))
    check("name stays searchable in the clear", sealed["name"] == "Grace")
    check("round-trip returns the original", privacy.open_record(sealed)["dob"] == "1988-04-12")
    check("empty values stay empty", privacy.encrypt("") == "")

check("clinical terms are detected", privacy.scrub_findings("your HIV test is positive") != [])
check("neutral recall copy passes", privacy.scrub_findings("due for your dental check-up") == [])
check("scrub removes the term", "hiv" not in privacy.scrub("Your HIV result").lower())

print("\n\033[1mclient records\033[0m")
shop_id = db.insert("shops", {"name": "C-Care", "slug": "cc", "sector": "Healthcare",
                              "status": "active", "currency": "UGX", "created_at": db.now_iso()})
care.seed_clinic_services(shop_id)
check("services seeded", len(care.list_services(shop_id)) >= 8)

cid = care.create_client(shop_id, {"name": "Grace Nakato", "phone": "0772123456",
                                   "dob": "1988-04-12", "identifier": "FM-1",
                                   "branch_label": "Kampala Main"}, "tester")
check("client created", cid > 0)
row = care.get_client(shop_id, cid, "tester")
check("client reads back decrypted", row["dob"] == "1988-04-12" and row["identifier"] == "FM-1")
if priv:
    raw = db.q1("SELECT dob, identifier FROM clients WHERE id=?", (cid,))
    check("raw row is ciphertext", raw["dob"].startswith("enc:v1:"))
check("access log recorded the read",
      db.scalar("SELECT COUNT(*) FROM access_log WHERE client_id=? AND action='view'", (cid,), 0) >= 1)

print("\n\033[1mphone normalisation\033[0m")
for given, want in [("0772123456", "+256772123456"), ("+256701987654", "+256701987654"),
                    ("0700111222", "+256700111222"), ("256782555111", "+256782555111"),
                    ("772123456", "+256772123456"), ("123", ""), ("", "")]:
    got = imports.normalise_phone(given)
    check(f"{given or '(empty)'!r} -> {want or 'rejected'!r}", got == want, f"got {got!r}")

print("\n\033[1mimport mapping\033[0m")
headers = ["File No", "Patient Name", "Phone", "Branch", "Service", "Next Appointment", "Insurance"]
mapping = imports.map_client_headers(headers)
check("maps Patient Name", headers[mapping.get("name", -1)] == "Patient Name")
check("maps File No to identifier", headers[mapping.get("identifier", -1)] == "File No")
check("maps Branch", headers[mapping.get("branch_label", -1)] == "Branch")
check("maps Next Appointment to next_due_on",
      headers[mapping.get("next_due_on", -1)] == "Next Appointment")

csv_text = "\n".join([
    "File No,Patient Name,Phone,Service,Last Visit,Next Appointment",
    "FM-101,Alice Auma,0772000101,Dental check-up & scaling,2026-01-10,2026-07-10",
    "FM-102,Bob Kato,+256770002022,Antenatal review,2026-02-01,2026-03-01",
    "FM-101,Alice Auma,0772000101,Dental check-up & scaling,2026-01-10,2026-07-10",
    ",,0772000303,Antenatal review,2026-02-01,2026-03-01",
    "FM-104,Bad Phone,123,Dental check-up & scaling,2026-01-01,2026-07-01",
])
hdrs, rows = imports.read_client_rows("t.csv", csv_text.encode())
res = imports.import_clients(shop_id, rows, imports.map_client_headers(hdrs), {"filename": "t.csv"}, "tester")
check("valid rows imported", res["created"] == 2, f"created {res['created']}")
check("in-file duplicate skipped", res["skipped"] >= 1)
check("no-name row rejected", any("no name" in (r.get("why") or "") for r in res["report"]))
check("bad phone rejected", any("not a valid number" in (r.get("why") or "") for r in res["report"]))
check("recalls scheduled from Next Appointment", res["recalls_scheduled"] == 2,
      f"got {res['recalls_scheduled']}")

# re-import must not duplicate
res2 = imports.import_clients(shop_id, rows, imports.map_client_headers(hdrs), {"filename": "t.csv"}, "tester")
check("re-import updates instead of duplicating", res2["created"] == 0,
      f"created {res2['created']}")

print("\n\033[1mvisits, recalls and retention\033[0m")
svc = {s["name"]: s for s in care.list_services(shop_id)}
alice = db.q1("SELECT id FROM clients WHERE name='Alice Auma' AND shop_id=?", (shop_id,))["id"]
out = care.record_visit(shop_id, alice, {"service_id": svc["Dental check-up & scaling"]["id"],
                                         "visited_on": "2026-03-01"}, "tester")
check("visit schedules the next recall", bool(out["next_due_on"]))
check("recall is 180 days after the visit", out["next_due_on"] == "2026-08-28", out["next_due_on"])

recall_id = db.q1("""SELECT id FROM recalls WHERE client_id=? AND status='due'
                     ORDER BY due_on DESC LIMIT 1""", (alice,))["id"]
care.mark_recall(shop_id, recall_id, "completed", "tester")
check("completing a recall chains the next one",
      db.scalar("SELECT COUNT(*) FROM recalls WHERE client_id=? AND status='due'", (alice,), 0) >= 1)

ret = care.retention(shop_id, 3650)
check("retention reports a rate", isinstance(ret["retention_rate"], float))
check("retention splits by service", len(ret["by_service"]) >= 1)
check("retention splits by branch", len(ret["by_branch"]) >= 1)

print("\n\033[1mopt-out\033[0m")
care.set_optout(shop_id, alice, False, "tester")
check("opt-out is stored", db.scalar("SELECT opted_in FROM clients WHERE id=?", (alice,), 1) == 0)
check("opted-out clients are excluded from due recalls",
      all(r["client_id"] != alice for r in care.due_recalls(shop_id, "2099-01-01")))
care.set_optout(shop_id, alice, True, "tester")
check("opt back in works", db.scalar("SELECT opted_in FROM clients WHERE id=?", (alice,), 0) == 1)

print("\n\033[1mtenant isolation\033[0m")
other = db.insert("shops", {"name": "Other", "slug": "other", "sector": "Healthcare",
                            "status": "active", "created_at": db.now_iso()})
check("cannot read another shop's client", care.get_client(other, alice, "tester") is None)
check("cannot update another shop's client",
      care.update_client(other, alice, {"name": "Hacked"}, "tester") is False)
check("list is scoped to the shop", care.list_clients(other)["total"] == 0)

print(f"\n\033[1m{PASSED} passed, {FAILED} failed\033[0m\n")
try:
    os.unlink(_tmp)
except OSError:
    pass
sys.exit(1 if FAILED else 0)
