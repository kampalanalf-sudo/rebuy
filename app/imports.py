"""
Data import: spreadsheets, CSV exports and receipt text/photos.

Shop owners arrive with whatever they have — an Excel sales book, a POS CSV
export, a photo of a handwritten receipt, or a WhatsApp order message. This
module normalises all of it into (customer, sale, sale_items) and hands the
result to the retention engine.

Design notes
  * Column mapping is fuzzy: a header only has to look like one of the synonyms.
  * Rows are treated as line items and grouped by receipt number when present,
    otherwise by (customer, date) — this covers both "one row per sale" and
    "one row per item" exports without asking the user to pick.
  * XLSX is read with openpyxl when installed and with a small stdlib
    zip+XML reader otherwise, so the dependency is optional.
  * Receipt images go through `ocr_image()`, which shells out to tesseract if it
    is installed. When no OCR engine is available the receipt is parked in
    "needs review" and the UI shows the photo next to a fast-entry form.
"""
from __future__ import annotations

import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from datetime import datetime
from xml.etree import ElementTree

from . import db, engine

UPLOAD_DIR = os.environ.get("REBUY_UPLOADS", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "uploads"))

HEADER_SYNONYMS = {
    "date": ["date", "sale_date", "day", "timestamp", "when", "sold_on", "time", "transaction_date", "saledate"],
    "receipt_no": ["receipt", "receipt_no", "receiptno", "invoice", "invoice_no", "txn", "reference", "ref", "bill", "orderno", "order_no", "ticket"],
    "name": ["customer", "customer_name", "name", "client", "buyer", "customername", "served", "patron", "guest"],
    "phone": ["phone", "mobile", "tel", "telephone", "contact", "msisdn", "whatsapp", "number", "cell", "phone_number"],
    "email": ["email", "mail", "e_mail", "email_address"],
    "product": ["product", "item", "item_name", "description", "service", "productname", "goods", "sku_name", "desc"],
    "sku": ["sku", "code", "item_code", "product_code", "barcode"],
    "category": ["category", "type", "group", "department", "class"],
    "qty": ["qty", "quantity", "units", "count", "qnty", "amount_bought", "no_of_items"],
    "unit": ["unit", "uom", "pack", "size"],
    "unit_price": ["price", "unit_price", "rate", "unitprice", "amount_each", "cost"],
    "line_total": ["total", "amount", "line_total", "subtotal", "value", "sum", "paid", "grand_total", "net"],
    "payment_method": ["payment", "method", "payment_method", "paid_via", "mode"],
    "area": ["area", "address", "location", "city", "town", "region"],
    "staff": ["staff", "cashier", "served_by", "agent", "salesperson"],
    "notes": ["notes", "note", "comment", "remarks"],
}

DATE_PATTERNS = [
    "%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%d/%m/%y", "%m/%d/%Y", "%d-%m-%Y", "%Y-%m-%d %H:%M:%S",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%d.%m.%Y", "%Y%m%d",
]


# --------------------------------------------------------------------- parsing
def clean_money(v) -> float:
    if v is None or v == "":
        return 0.0
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    neg = s.startswith("(") and s.endswith(")")
    s = re.sub(r"[^\d\.\-]", "", s)
    s = s.rstrip("-")
    try:
        f = float(s or 0)
    except ValueError:
        return 0.0
    return -f if neg else f


def clean_date(v) -> str:
    if v is None or v == "":
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    s = str(v).strip()
    if not s:
        return ""
    for f in DATE_PATTERNS:
        try:
            return datetime.strptime(s[:len(datetime.now().strftime(f)) + 4], f).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    m = re.search(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})", s)
    if m:
        y = m.group(3)
        y = f"20{y}" if len(y) == 2 else y
        return f"{y}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return ""


def map_headers(headers: list[str]) -> dict[str, int]:
    """Return {canonical_field: column_index} using fuzzy synonym matching."""
    out: dict[str, int] = {}
    norm = [(h or "").strip().lower().replace(" ", "_") for h in headers]
    for field, syns in HEADER_SYNONYMS.items():
        for i, h in enumerate(norm):
            hh = h.strip("_")
            if hh in syns:
                out[field] = i
                break
        if field in out:
            continue
        for i, h in enumerate(norm):  # second pass: substring match
            if any(s in h for s in syns) and len(h) > 2:
                out.setdefault(field, i)
                break
    return out


def read_csv(text: str) -> list[list[str]]:
    text = text.lstrip("\ufeff")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return [r for r in csv.reader(io.StringIO(text), dialect) if any((c or "").strip() for c in r)]


def read_xlsx(data: bytes) -> list[list[str]]:
    try:
        import openpyxl  # type: ignore
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = [[("" if c is None else str(c)) for c in row] for row in ws.iter_rows(values_only=True)]
        wb.close()
        return [r for r in rows if any((c or "").strip() for c in r)]
    except ImportError:
        return _read_xlsx_stdlib(data)


def _read_xlsx_stdlib(data: bytes) -> list[list[str]]:
    """Minimal .xlsx reader (zip + sheet XML) for environments without openpyxl."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    shared: list[str] = []
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        if "xl/sharedStrings.xml" in z.namelist():
            root = ElementTree.fromstring(z.read("xl/sharedStrings.xml"))
            for si in root.findall(f"{ns}si"):
                shared.append("".join(t.text or "" for t in si.iter(f"{ns}t")))
        sheet_name = next((n for n in z.namelist() if re.match(r"xl/worksheets/sheet1\.xml", n)), None)
        if not sheet_name:
            sheet_name = next((n for n in z.namelist() if n.startswith("xl/worksheets/sheet")), None)
        if not sheet_name:
            return []
        root = ElementTree.fromstring(z.read(sheet_name))
    rows: list[list[str]] = []
    for row in root.iter(f"{ns}row"):
        vals = []
        for c in row.findall(f"{ns}c"):
            t = c.get("t")
            v = c.find(f"{ns}v")
            is_node = c.find(f"{ns}is")
            if t == "s" and v is not None:
                vals.append(shared[int(v.text)] if v.text and int(v.text) < len(shared) else "")
            elif t == "inlineStr" and is_node is not None:
                vals.append("".join(x.text or "" for x in is_node.iter(f"{ns}t")))
            else:
                vals.append(v.text if v is not None and v.text else "")
        rows.append(vals)
    return [r for r in rows if any((c or "").strip() for c in r)]


def rows_to_records(rows: list[list[str]]) -> tuple[dict[str, int], list[dict]]:
    if not rows:
        return {}, []
    header_row = 0
    for i, r in enumerate(rows[:5]):
        joined = " ".join(str(c).lower() for c in r)
        if any(s in joined for s in ("date", "customer", "item", "product", "phone", "amount", "total")):
            header_row = i
            break
    mapping = map_headers([str(c) for c in rows[header_row]])
    if not mapping or ("name" not in mapping and "phone" not in mapping):
        return mapping, []
    recs = []
    for r in rows[header_row + 1:]:
        def g(field):
            i = mapping.get(field)
            return (str(r[i]).strip() if i is not None and i < len(r) else "")
        rec = {k: g(k) for k in mapping}
        if not any(rec.values()):
            continue
        recs.append(rec)
    return mapping, recs


def group_records(records: list[dict]) -> list[dict]:
    """Group line items into sales: by receipt number, else by customer+date."""
    groups: dict[str, dict] = {}
    order: list[str] = []
    for i, r in enumerate(records):
        key = r.get("receipt_no") or ""
        if not key:
            who = r.get("phone") or r.get("name") or "walk-in"
            key = f"{who}|{r.get('date') or db.today()}"
        if key not in groups:
            groups[key] = {"key": key, "lines": [], **{k: r.get(k, "") for k in
                                                       ("date", "name", "phone", "email", "receipt_no",
                                                        "payment_method", "area", "staff", "notes")}}
            order.append(key)
        for k in ("date", "name", "phone", "email", "receipt_no", "payment_method", "area", "staff", "notes"):
            if not groups[key].get(k) and r.get(k):
                groups[key][k] = r[k]
        groups[key]["lines"].append(r)
    return [groups[k] for k in order]


RECEIPT_LINE = re.compile(
    r"^\s*(?P<name>[A-Za-z][A-Za-z0-9 &'\.\-/]{1,48}?)\s+"
    r"(?:(?P<qty>\d{1,3}(?:\.\d+)?)\s*[xX@]?\s*)?"
    r"(?P<price>\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?)\s*"
    r"(?:\|?\s*(?P<total>\d{1,3}(?:[,\s]\d{3})*(?:\.\d+)?))?\s*$")
TOTAL_RE = re.compile(r"(?:grand\s*total|total|amount\s*due|net|balance|sum)\s*[:\-]?\s*([A-Za-z]{0,4}\s*)?([\d][\d,\s\.]*)", re.I)
DATE_RE = re.compile(r"\b(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}\s+[A-Za-z]{3,9}\s+\d{2,4})\b")
PHONE_RE = re.compile(r"(\+?\d[\d\s\-()]{7,15}\d)")
RECEIPT_NO_RE = re.compile(r"(?:receipt|invoice|txn|transaction|bill|ref|no\.?)\s*(?:no\.?|#|:)?\s*[:\-]?\s*([A-Za-z0-9\-/]{3,20})", re.I)
CUSTOMER_RE = re.compile(r"(?:customer|client|served|name|buyer|member)\s*[:\-]\s*(.{2,40})", re.I)


def parse_receipt_text(text: str) -> dict:
    """Best-effort parse of a receipt (OCR output, pasted text, or WhatsApp order)."""
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    out = {"date": "", "receipt_no": "", "name": "", "phone": "", "total": 0.0, "lines": [], "confidence": 0}
    for ln in lines[:12]:
        if not out["date"]:
            m = DATE_RE.search(ln)
            if m:
                out["date"] = clean_date(m.group(1))
        if not out["receipt_no"]:
            m = RECEIPT_NO_RE.search(ln)
            if m and not m.group(1).lower() in ("total", "cash", "paid"):
                out["receipt_no"] = m.group(1)
        if not out["name"]:
            m = CUSTOMER_RE.search(ln)
            if m:
                out["name"] = m.group(1).strip(" .")
        if not out["phone"]:
            m = PHONE_RE.search(ln)
            if m and len(re.sub(r"\D", "", m.group(1))) >= 9:
                out["phone"] = m.group(1)
    totals = [clean_money(m.group(2)) for m in TOTAL_RE.finditer(text)]
    out["total"] = max(totals) if totals else 0.0

    skip = re.compile(r"(total|subtotal|cash|change|balance|discount|tax|vat|received|receipt|invoice|thank|welcome|"
                      r"date|time|served|customer|paid|mpesa|mobile money|visa|card|amount due|grand)", re.I)
    for ln in lines:
        if skip.search(ln) or len(ln) < 3:
            continue
        m = RECEIPT_LINE.match(ln)
        if not m:
            continue
        price = clean_money(m.group("price"))
        total = clean_money(m.group("total")) if m.group("total") else 0.0
        qty = float(m.group("qty")) if m.group("qty") else (round(total / price, 2) if price and total else 1.0)
        if price <= 0:
            continue
        name = m.group("name").strip(" .-")
        if not re.search(r"[A-Za-z]{3}", name):
            continue
        out["lines"].append({"product": name[:60], "qty": qty or 1.0, "unit_price": price,
                             "line_total": total or round(price * (qty or 1.0), 2)})
    if out["lines"] and not out["total"]:
        out["total"] = round(sum(l["line_total"] for l in out["lines"]), 2)
    score = 0
    score += 25 if out["lines"] else 0
    score += 20 if out["total"] else 0
    score += 20 if out["name"] or out["phone"] else 0
    score += 15 if out["date"] else 0
    score += 10 if out["receipt_no"] else 0
    out["confidence"] = min(100, score)
    return out


def ocr_image(path: str) -> str:
    """Run tesseract when available; otherwise return '' and let the UI ask for a quick entry."""
    if not shutil.which("tesseract"):
        return ""
    try:
        r = subprocess.run(["tesseract", path, "stdout", "--psm", "6"], capture_output=True, text=True, timeout=90)
        return r.stdout or ""
    except Exception:  # noqa: BLE001
        return ""


def save_upload(filename: str, data: bytes) -> str:
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9\.\-_]", "_", os.path.basename(filename or "upload"))[:60]
    stored = os.path.join(UPLOAD_DIR, f"{datetime.now():%Y%m%d%H%M%S%f}_{safe}")
    with open(stored, "wb") as f:
        f.write(data)
    return stored


# ---------------------------------------------------------------------- import
def upsert_customer(shop_id: int, name: str, phone: str, email: str = "", area: str = "",
                    source: str = "import", consent: bool = True) -> dict | None:
    name = (name or "").strip()
    phone = (phone or "").strip()
    email = (email or "").strip().lower()
    if not name and not phone and not email:
        return None
    if not name:
        name = phone or email.split("@")[0] or "Walk-in customer"
    existing = None
    if phone:
        norm = _norm_phone(phone)
        existing = db.q1("SELECT * FROM customers WHERE shop_id=? AND (phone=? OR phone=? OR whatsapp=?)",
                         (shop_id, phone, norm, norm))
    if not existing and email:
        existing = db.q1("SELECT * FROM customers WHERE shop_id=? AND email=?", (shop_id, email))
    now = db.now_iso()
    if existing:
        patch = {"updated_at": now}
        if email and not existing.get("email"):
            patch["email"] = email
        if area and not existing.get("area"):
            patch["area"] = area
        if name and (not existing.get("name") or existing["name"].startswith("Walk-in")):
            patch["name"] = name
        db.update("customers", existing["id"], patch)
        return db.q1("SELECT * FROM customers WHERE id=?", (existing["id"],))
    cid = db.insert("customers", {
        "shop_id": shop_id, "name": name[:120], "phone": phone[:32], "whatsapp": phone[:32],
        "email": email[:120], "area": area[:80], "opted_in": 1 if consent and phone else (1 if consent and email else 0),
        "consent_source": source, "first_seen": db.today(), "risk_band": "new",
        "created_at": now, "updated_at": now,
    })
    return db.q1("SELECT * FROM customers WHERE id=?", (cid,))


def _norm_phone(phone: str) -> str:
    d = re.sub(r"\D", "", phone or "")
    if d.startswith("0"):
        return "256" + d[1:]
    if d.startswith("7") or d.startswith("3"):
        return "256" + d
    return d


def upsert_product(shop_id: int, name: str, category: str = "General", unit: str = "pc",
                   price: float = 0.0, sku: str = "", kind: str = "goods", cycle_days: int | None = None) -> int:
    name = (name or "").strip()[:120]
    if not name:
        return 0
    row = db.q1("SELECT * FROM products WHERE shop_id=? AND lower(name)=lower(?)", (shop_id, name))
    cat = category or _guess_category(name)
    cyc = cycle_days or (row["cycle_days"] if row else engine.CATEGORY_DEFAULT_CYCLES.get(cat, 30))
    if row:
        patch = {}
        if price and abs(row["unit_price"] - price) > 0.01:
            patch["unit_price"] = price
        if unit and row["unit"] != unit:
            patch["unit"] = unit
        if kind and row["kind"] != kind:
            patch["kind"] = kind
        if category and row["category"] != cat:
            patch["category"] = cat
        if cycle_days and int(row["cycle_days"] or 0) != int(cycle_days):
            patch["cycle_days"] = int(cycle_days)
        if patch:
            db.update("products", row["id"], patch)
        return row["id"]
    return db.insert("products", {
        "shop_id": shop_id, "name": name, "sku": sku, "category": cat, "kind": kind, "unit": unit or "pc",
        "unit_price": price, "cycle_days": cyc, "baseline_qty": 1, "active": 1, "created_at": db.now_iso(),
    })


CATEGORY_KEYWORDS = {
    "Grains & Flour": ["flour", "maize", "rice", "posho", "millet", "beans", "cassava", "wheat", "grain", "semolina"],
    "Cooking Oil": ["oil", "ghee", "fat"],
    "Sugar": ["sugar"],
    "Salt": ["salt"],
    "Beverages": ["soda", "juice", "water", "beer", "wine", "spirit", "tea", "coffee", "coca", "pepsi", "fanta", "sprite", "energy"],
    "Fuel & Gas": ["gas", "lpg", "cylinder", "paraffin", "kerosene", "petrol", "diesel", "refill"],
    "Charcoal": ["charcoal", "briquette"],
    "Personal Care": ["soap", "shampoo", "tooth", "paste", "cream", "lotion", "roll-on", "rollon", "deodorant", "sanitary", "towel", "razor", "vaseline"],
    "Cosmetics": ["makeup", "lipstick", "foundation", "wig", "hair", "braids", "nail", "perfume", "salon"],
    "Cleaning": ["detergent", "bleach", "jik", "soap", "mop", "broom", "cleaner", "air freshener", "oomo", "sunlight", "vix"],
    "Baby Care": ["diaper", "nappy", "baby", "formula", "cerelac", "milk"],
    "Dairy": ["milk", "yoghurt", "yogurt", "cheese", "butter", "cream"],
    "Bakery": ["bread", "cake", "bun", "pastry", "doughnut", "mandazi", "donut", "croissant"],
    "Meat & Fish": ["meat", "beef", "goat", "chicken", "fish", "tilapia", "pork", "mutton", "liver", "sausage", "eggs", "egg"],
    "Produce": ["tomato", "onion", "potato", "matooke", "banana", "cabbage", "carrot", "pepper", "greens", "dodo", "fruit", "mango", "pineapple", "avocado", "lemon", "garlic", "ginger"],
    "Pharmacy": ["tablet", "capsule", "syrup", "medicine", "drug", "panadol", "antibiotic", "vitamin", "dewormer", "ors", "injection", "malaria", "artem", "paracetamol"],
    "Stationery": ["pen", "book", "exercise", "paper", "stapler", "ink", "toner", "marker", "chalk", "notebook", "pencil", "file", "printing"],
    "Hardware": ["cement", "nail", "paint", "rod", "pipe", "tap", "wire", "screw", "bolt", "tile", "timber", "plywood", "glue", "tools", "hammer", "spade"],
    "Electronics": ["phone", "charger", "cable", "battery", "bulb", "tv", "radio", "fridge", "speaker", "airtime", "data", "sim", "laptop", "solar", "panel"],
    "Pet Care": ["dog", "cat", "pet", "feed"],
    "Agrovet": ["seed", "fertilizer", "dapp", "urea", "pesticide", "herbicide", "spray", "chick", "poultry", "feeds", "animal"],
    "Hair & Beauty": ["haircut", "salon", "braiding", "manicure", "pedicure", "shave", "facial", "massage", "weave", "dreads"],
    "Auto Service": ["service", "oil change", "tyre", "tire", "brake", "wash", "repair", "mechanic", "alignment", "battery change"],
    "Laundry": ["laundry", "wash", "iron", "dry clean", "cleaning"],
    "Fitness": ["gym", "workout", "session", "yoga", "swim", "membership", "class"],
}


def _guess_category(name: str) -> str:
    n = (name or "").lower()
    for cat, words in CATEGORY_KEYWORDS.items():
        if any(w in n for w in words):
            return cat
    return "General"


def guess_kind(name: str, category: str = "") -> str:
    n = f"{name} {category}".lower()
    service_words = ["service", "haircut", "salon", "braiding", "manicure", "pedicure", "massage", "laundry", "wash",
                     "repair", "install", "delivery fee", "membership", "class", "session", "consult", "checkup",
                     "grooming", "facial", "shave", "dread", "weave", "tint", "alignment", "training", "rental",
                     "cleaning", "gym", "yoga", "swim"]
    return "service" if any(w in n for w in service_words) else "goods"


def import_records(shop_id: int, records: list[dict], source: str = "csv",
                   default_date: str | None = None, dry_run: bool = False) -> dict:
    """Import parsed records (line items) into the shop. Returns an import report."""
    report = {"rows_total": len(records), "rows_ok": 0, "rows_failed": 0, "sales": 0,
              "customers": 0, "products": 0, "reminders": 0, "errors": [], "preview": []}
    if not records:
        report["errors"].append("No rows matched a customer column. Expected headers like: date, customer, phone, item, qty, total")
        return report
    groups = group_records(records)
    for g in groups:
        try:
            sale_date = clean_date(g.get("date")) or default_date or db.today()
            if sale_date > db.today():
                sale_date = db.today()
            cust = upsert_customer(shop_id, g.get("name", ""), g.get("phone", ""), g.get("email", ""),
                                   g.get("area", ""), source)
            if not cust:
                cust = upsert_customer(shop_id, "Walk-in customer", "", "", "", source)
            lines, total = [], 0.0
            for ln in g["lines"]:
                pname = (ln.get("product") or "").strip()
                if not pname:
                    continue
                qty = float(re.sub(r"[^\d\.]", "", ln.get("qty", "") or "1") or 1)
                price = clean_money(ln.get("unit_price"))
                lt = clean_money(ln.get("line_total"))
                if not lt:
                    lt = round(price * qty, 2)
                if not price and qty:
                    price = round(lt / qty, 2)
                cat = ln.get("category") or _guess_category(pname)
                kind = guess_kind(pname, cat)
                pid = upsert_product(shop_id, pname, cat, ln.get("unit") or ("session" if kind == "service" else "pc"),
                                     price, ln.get("sku", ""), kind)
                prod = db.q1("SELECT * FROM products WHERE id=?", (pid,)) if pid else None
                cycle, rkind = engine.effective_cycle(prod or {"category": cat, "kind": kind, "cycle_days": 30}, qty)
                lines.append({
                    "product_id": pid or None, "product_name": pname[:120], "category": cat, "kind": rkind,
                    "qty": qty, "unit": (ln.get("unit") or "pc")[:12], "unit_price": price,
                    "line_total": lt, "cycle_days": cycle,
                    "depletes_on": engine.add_days(sale_date, cycle),
                })
                total += lt
                report["products"] += 1
            if not lines:
                report["rows_failed"] += len(g["lines"])
                report["errors"].append(f"{g.get('receipt_no') or g.get('name') or 'row'}: no item lines")
                continue
            if total <= 0:
                total = round(sum(l["line_total"] for l in lines), 2)
            if dry_run:
                report["preview"].append({"date": sale_date, "customer": cust["name"], "phone": cust["phone"],
                                          "items": lines, "total": total})
                report["rows_ok"] += len(g["lines"])
                continue
            sale_id = db.insert("sales", {
                "shop_id": shop_id, "receipt_no": (g.get("receipt_no") or "")[:40], "customer_id": cust["id"],
                "sale_date": sale_date, "total": total, "currency": "UGX",
                "payment_method": (g.get("payment_method") or "cash")[:24], "channel": "in_store",
                "staff_name": (g.get("staff") or "")[:40], "source": source, "notes": (g.get("notes") or "")[:200],
                "created_at": db.now_iso(),
            })
            for l in lines:
                l = dict(l, sale_id=sale_id, shop_id=shop_id)
                db.insert("sale_items", l)
            res = engine.on_sale_created(sale_id)
            report["sales"] += 1
            report["rows_ok"] += len(lines)
            report["reminders"] += res.get("reminders", 0)
            report["customers"] += 1
        except Exception as e:  # noqa: BLE001
            report["rows_failed"] += 1
            report["errors"].append(f"{(g.get('receipt_no') or g.get('name') or '?')}: {type(e).__name__}: {e}"[:220])
    return report


def import_file(shop_id: int, filename: str, data: bytes, source: str = "csv") -> dict:
    ext = os.path.splitext(filename or "")[1].lower()
    if ext in (".xlsx", ".xlsm"):
        rows = read_xlsx(data)
        mapping, records = rows_to_records(rows)
    else:
        text = data.decode("utf-8", errors="replace")
        rows = read_csv(text)
        mapping, records = rows_to_records(rows)
    if not records and rows:
        # maybe it's a plain receipt / text dump
        parsed = parse_receipt_text("\n".join(" ".join(r) for r in rows[:200]))
        if parsed["lines"]:
            records = [dict(parsed, product=l["product"], qty=str(l["qty"]), unit_price=str(l["unit_price"]),
                            line_total=str(l["line_total"]), name=parsed["name"], phone=parsed["phone"],
                            date=parsed["date"], receipt_no=parsed["receipt_no"]) for l in parsed["lines"]]
    rep = import_records(shop_id, records, source)
    rep["mapping"] = {k: rows[0][v] if rows and v < len(rows[0]) else "" for k, v in mapping.items()}
    job_id = db.insert("import_jobs", {
        "shop_id": shop_id, "kind": source, "filename": filename, "rows_total": rep["rows_total"],
        "rows_ok": rep["rows_ok"], "rows_failed": rep["rows_failed"], "status": "done",
        "report": json.dumps({"sales": rep["sales"], "customers": rep["customers"],
                              "reminders": rep["reminders"], "errors": rep["errors"][:20]}),
        "created_at": db.now_iso(),
    })
    rep["job_id"] = job_id
    db.audit("import", "file.imported", shop_id, "import_job", job_id,
             f"{filename}: {rep['rows_ok']}/{rep['rows_total']} rows, {rep['sales']} sales")
    return rep


def import_receipt_text(shop_id: int, text: str, source: str = "receipt_ocr") -> dict:
    parsed = parse_receipt_text(text)
    if not parsed["lines"]:
        return {"ok": False, "parsed": parsed, "error": "Could not find any item lines in that text. "
                                                       "Try a format like:  Maize flour 2kg  2  4,000  8,000"}
    records = [dict(parsed, product=l["product"], qty=str(l["qty"]), unit_price=str(l["unit_price"]),
                    line_total=str(l["line_total"])) for l in parsed["lines"]]
    rep = import_records(shop_id, records, source)
    return {"ok": True, "parsed": parsed, "report": rep}


def import_base64_file(shop_id: int, filename: str, data_url: str) -> dict:
    """Handle a browser upload sent as a data URL (avoids multipart parsing)."""
    if "," in data_url:
        header, b64 = data_url.split(",", 1)
    else:
        header, b64 = "", data_url
    data = base64.b64decode(b64)
    is_image = "image" in header or os.path.splitext(filename)[1].lower() in (".jpg", ".jpeg", ".png", ".webp", ".heic", ".gif")
    if is_image:
        stored = save_upload(filename, data)
        text = ocr_image(stored)
        parsed = parse_receipt_text(text) if text else {"confidence": 0, "lines": []}
        rid = db.insert("receipts", {
            "shop_id": shop_id, "filename": filename, "stored_path": stored, "mime": header.split(";")[0].replace("data:", "") or "image/jpeg",
            "size": len(data), "raw_text": text[:8000], "parsed_json": json.dumps(parsed),
            "parse_status": "parsed" if parsed.get("lines") else "needs_review", "created_at": db.now_iso(),
        })
        db.audit("import", "receipt.uploaded", shop_id, "receipt", rid, f"{filename} ({len(data)//1024} KB)")
        return {"kind": "receipt", "receipt_id": rid, "stored_path": f"/api/receipts/{rid}/file",
                "ocr_available": bool(text), "parsed": parsed,
                "message": ("OCR text extracted — review and import." if text else
                            "No OCR engine on this server. The photo is stored; confirm the items below "
                            "(or paste the receipt text) and we'll import it.")}
    return {"kind": "file", "report": import_file(shop_id, filename, data, "csv")}


def confirm_receipt(shop_id: int, receipt_id: int, payload: dict) -> dict:
    """Shop owner confirms/edits a parsed receipt, then we import it."""
    rec = db.q1("SELECT * FROM receipts WHERE id=? AND shop_id=?", (receipt_id, shop_id))
    if not rec:
        return {"ok": False, "error": "receipt not found"}
    records = [dict(payload, product=l.get("product", ""), qty=str(l.get("qty", 1)),
                    unit_price=str(l.get("unit_price", 0)), line_total=str(l.get("line_total", 0)),
                    unit=l.get("unit", "pc"), category=l.get("category", "")) for l in payload.get("lines", [])]
    rep = import_records(shop_id, records, "receipt_ocr", default_date=payload.get("date"))
    db.update("receipts", receipt_id, {"parse_status": "imported" if rep["sales"] else "failed",
                                       "parsed_json": json.dumps(payload)[:20000]})
    return {"ok": bool(rep["sales"]), "report": rep}
