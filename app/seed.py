"""
Demo data generator.

Boots the platform with a realistic fleet of 120 Ugandan shops, their product
catalogues (goods *and* services), customers, 6 months of sales, the reminders
those sales imply, and the WhatsApp/SMS/email traffic those reminders produced.
Deterministic (fixed RNG seed) so every install looks the same.
"""
from __future__ import annotations

import json
import os
import random
import re
from datetime import timedelta

from . import auth, db, engine, imports, providers

RNG_SEED = 20260401
HISTORY_DAYS = 180

FIRST_NAMES = ["Sarah", "Joseph", "Mariam", "David", "Grace", "Peter", "Aisha", "John", "Florence", "Ronald",
               "Sharon", "Ibrahim", "Doreen", "Moses", "Prossy", "Samuel", "Nancy", "Patrick", "Resty", "Brian",
               "Catherine", "Emmanuel", "Harriet", "Kenneth", "Lillian", "Michael", "Naomi", "Oscar", "Peace",
               "Robert", "Stella", "Tom", "Violet", "William", "Zawadi", "Alice", "Ben", "Charity", "Denis",
               "Esther", "Fred", "Goretti", "Henry", "Immaculate", "James", "Kevin", "Leticia", "Martin",
               "Norah", "Paul", "Rachel", "Steven", "Trinity", "Umar", "Veronica", "Wycliffe", "Yvonne", "Godfrey",
               "Juliet", "Mathias", "Agnes", "Baker", "Diana", "Edward", "Faith", "George", "Hawa", "Ivan",
               "Joyce", "Kato", "Lydia", "Mugisha", "Nicholas", "Olivia", "Pius", "Quin", "Ruth", "Simon",
               "Teopista", "Vincent", "Winnie", "Yusuf", "Zabulon"]

LAST_NAMES = ["Nakato", "Mugisha", "Okello", "Nambi", "Kato", "Achieng", "Ssebugwawo", "Namutebi", "Ojok",
              "Kabanda", "Nalubega", "Wasswa", "Auma", "Kibuuka", "Nansubuga", "Owino", "Babirye", "Sekandi",
              "Adhiambo", "Lubwama", "Chebet", "Mugabi", "Nakyewa", "Opio", "Ssentongo", "Tumwine", "Apio",
              "Baguma", "Nakamya", "Otim", "Kyomuhangi", "Musoke", "Nalwoga", "Byaruhanga", "Isabirye",
              "Tumusiime", "Nakabuye", "Ogwal", "Katende", "Mbabazi", "Ssekitoleko", "Nyiramana", "Ddamulira",
              "Akello", "Muwonge", "Nankunda", "Sserunkuma", "Aol", "Kirunda", "Mukasa"]

AREAS = ["Ntinda", "Nansana", "Kira", "Wandegeya", "Ndeeba", "Bugolobi", "Kololo", "Kawempe", "Najjanakumbi",
         "Bukoto", "Kyanja", "Bunga", "Munyonyo", "Makerere", "Balintuma", "Kisenyi", "Katwe", "Lubaga",
         "Nakasero", "Kyebando", "Bweyogerere", "Namugongo", "Gayaza", "Ssentema", "Zana", "Kajjansi"]

CITIES = ["Kampala", "Wakiso", "Mukono", "Entebbe", "Jinja", "Mbarara", "Gulu", "Mbale", "Fort Portal",
          "Masaka", "Lira", "Kampala", "Kampala", "Wakiso", "Kampala"]

CATALOGUES = {
    "Supermarket": [
        ("Maize flour 2kg", "Grains & Flour", "pack", 4500, 21), ("Sugar 1kg", "Sugar", "kg", 4200, 30),
        ("Cooking oil 1L", "Cooking Oil", "bottle", 9500, 45), ("Rice 5kg", "Grains & Flour", "bag", 22000, 45),
        ("Beans 2kg", "Grains & Flour", "kg", 6000, 30), ("Salt 500g", "Salt", "pack", 1200, 90),
        ("Bathing soap", "Personal Care", "bar", 2500, 45), ("Detergent 1kg", "Cleaning", "pack", 7500, 45),
        ("Toothpaste 100ml", "Personal Care", "tube", 4500, 60), ("Milk 500ml", "Dairy", "pack", 2800, 5),
        ("Bread loaf", "Bakery", "loaf", 3000, 3), ("Eggs tray", "Meat & Fish", "tray", 14000, 7),
        ("Charcoal 10kg", "Charcoal", "bag", 18000, 21), ("Tomato sauce 400g", "Beverages", "bottle", 5500, 45),
        ("Tea leaves 250g", "Beverages", "pack", 5000, 60), ("Spaghetti 500g", "Grains & Flour", "pack", 3200, 30),
        ("Sanitary pads", "Personal Care", "pack", 8000, 30), ("Baby diapers pack", "Baby Care", "pack", 25000, 21),
        ("Water 18.9L refill", "Water", "bottle", 6000, 7), ("Airtime top-up", "Electronics", "credit", 5000, 10),
        ("Vaseline 250ml", "Personal Care", "jar", 9000, 60), ("Matchbox pack", "General", "pack", 1000, 60),
    ],
    "LPG & Water": [
        ("LPG refill 6kg", "Fuel & Gas", "cylinder", 45000, 25), ("LPG refill 12kg", "Fuel & Gas", "cylinder", 85000, 45),
        ("LPG refill 3kg", "Fuel & Gas", "cylinder", 25000, 12), ("Gas cylinder deposit", "Fuel & Gas", "cylinder", 90000, 720),
        ("Gas regulator", "Fuel & Gas", "pc", 35000, 720), ("Gas hose 2m", "Fuel & Gas", "pc", 15000, 365),
        ("Water 18.9L refill", "Water", "bottle", 6000, 7), ("Water dispenser", "Water", "pc", 120000, 720),
        ("Charcoal 10kg", "Charcoal", "bag", 18000, 21), ("Delivery fee", "General", "trip", 5000, 30),
    ],
    "Salon & Spa": [
        ("Haircut", "Hair & Beauty", "session", 10000, 21), ("Braids installation", "Hair & Beauty", "session", 80000, 45),
        ("Manicure", "Hair & Beauty", "session", 20000, 21), ("Pedicure", "Hair & Beauty", "session", 25000, 21),
        ("Hair wash & treatment", "Hair & Beauty", "session", 8000, 14), ("Dreadlock retwist", "Hair & Beauty", "session", 50000, 30),
        ("Weave installation", "Hair & Beauty", "session", 150000, 60), ("Facial treatment", "Hair & Beauty", "session", 40000, 30),
        ("Beard trim", "Hair & Beauty", "session", 8000, 14), ("Relaxer application", "Hair & Beauty", "session", 30000, 45),
        ("Massage 1hr", "Hair & Beauty", "session", 60000, 30), ("Hair colouring", "Hair & Beauty", "session", 70000, 60),
    ],
    "Pharmacy": [
        ("Panadol 24 tabs", "Pharmacy", "pack", 3000, 21), ("Antibiotic course", "Pharmacy", "course", 15000, 45),
        ("Multivitamins 60 tabs", "Pharmacy", "bottle", 28000, 60), ("Cough syrup 100ml", "Pharmacy", "bottle", 9000, 21),
        ("BP medication 30 tabs", "Pharmacy", "pack", 22000, 30), ("Diabetes medication", "Pharmacy", "pack", 35000, 30),
        ("ORS sachets", "Pharmacy", "pack", 2000, 30), ("Dewormer", "Pharmacy", "dose", 5000, 90),
        ("Malaria rapid test", "Pharmacy", "test", 8000, 45), ("Artemether 6 tabs", "Pharmacy", "pack", 12000, 45),
        ("Sanitary pads", "Personal Care", "pack", 8000, 30), ("Hand sanitiser 250ml", "Cleaning", "bottle", 7000, 45),
    ],
    "Auto Garage": [
        ("Engine oil change", "Auto Service", "service", 120000, 90), ("Car wash", "Auto Service", "wash", 15000, 7),
        ("Full service", "Auto Service", "service", 250000, 90), ("Wheel alignment", "Auto Service", "service", 80000, 180),
        ("Brake pads replacement", "Auto Service", "set", 180000, 365), ("Battery replacement", "Auto Service", "pc", 250000, 540),
        ("Tyre rotation", "Auto Service", "service", 40000, 90), ("AC regas", "Auto Service", "service", 150000, 180),
        ("Air filter", "Auto Service", "pc", 45000, 120), ("Detailing & polish", "Auto Service", "service", 90000, 60),
    ],
    "Bakery": [
        ("Bread loaf", "Bakery", "loaf", 3000, 3), ("Mandazi 6pcs", "Bakery", "pack", 2000, 2),
        ("Birthday cake 2kg", "Bakery", "cake", 95000, 90), ("Cupcakes dozen", "Bakery", "box", 36000, 30),
        ("Scones 4pcs", "Bakery", "pack", 4000, 3), ("Doughnuts 6pcs", "Bakery", "pack", 9000, 7),
        ("Queen cake", "Bakery", "pc", 2500, 4), ("Croissant", "Bakery", "pc", 5000, 7),
    ],
    "Hardware": [
        ("Cement 50kg", "Hardware", "bag", 38000, 30), ("Paint 5L", "Hardware", "tin", 145000, 365),
        ("Nails 1kg", "Hardware", "kg", 6500, 90), ("Y-bar rod 10mm", "Hardware", "rod", 42000, 180),
        ("Tiles box", "Hardware", "box", 68000, 180), ("Timber 2x4", "Hardware", "piece", 18000, 180),
        ("PVC pipe 4in", "Hardware", "length", 25000, 365), ("Wood glue", "Hardware", "bottle", 12000, 120),
        ("Roofing nails 5kg", "Hardware", "box", 32000, 180), ("Wheelbarrow", "Hardware", "pc", 165000, 720),
    ],
    "Agrovet": [
        ("DAP fertiliser 50kg", "Agrovet", "bag", 185000, 120), ("Urea 50kg", "Agrovet", "bag", 165000, 120),
        ("Maize seed 2kg", "Agrovet", "pack", 24000, 180), ("Poultry feeds 50kg", "Agrovet", "bag", 118000, 21),
        ("Dairy meal 50kg", "Agrovet", "bag", 95000, 30), ("Livestock dewormer", "Agrovet", "dose", 12000, 90),
        ("Pesticide 1L", "Agrovet", "bottle", 32000, 45), ("Day-old chicks 50", "Agrovet", "batch", 280000, 120),
        ("Pig feed 50kg", "Agrovet", "bag", 105000, 21), ("Sprayer knapsack", "Agrovet", "pc", 85000, 720),
    ],
    "Boutique": [
        ("Men's shirt", "General", "pc", 45000, 180), ("Ladies dress", "General", "pc", 85000, 120),
        ("Kids school uniform", "General", "set", 55000, 90), ("Jeans", "General", "pc", 75000, 240),
        ("Shoes", "General", "pair", 120000, 240), ("Socks 3-pack", "General", "pack", 15000, 90),
        ("Jacket", "General", "pc", 145000, 365), ("Handbag", "General", "pc", 95000, 365), ("Belt", "General", "pc", 35000, 365),
    ],
    "Laundry": [
        ("Wash & fold per kg", "Laundry", "kg", 3000, 7), ("Dry clean suit", "Laundry", "set", 25000, 45),
        ("Iron only", "Laundry", "load", 5000, 7), ("Bedsheet set", "Laundry", "set", 12000, 14),
        ("Curtains per pair", "Laundry", "pair", 20000, 90), ("Carpet cleaning", "Laundry", "pc", 45000, 120),
        ("Duvet cleaning", "Laundry", "pc", 30000, 90),
    ],
    "Gym & Fitness": [
        ("Monthly membership", "Fitness", "month", 100000, 30), ("Personal training 4 sessions", "Fitness", "pack", 200000, 30),
        ("Yoga class pack", "Fitness", "pack", 80000, 21), ("Swimming pass", "Fitness", "pass", 30000, 14),
        ("Annual membership", "Fitness", "year", 950000, 365), ("Day pass", "Fitness", "pass", 15000, 7),
        ("Protein supplement 1kg", "Fitness", "tub", 180000, 30),
    ],
    "Electronics": [
        ("Data bundle 5GB", "Electronics", "bundle", 25000, 30), ("Airtime top-up", "Electronics", "credit", 5000, 7),
        ("Phone charger", "Electronics", "pc", 25000, 365), ("USB cable", "Electronics", "pc", 12000, 240),
        ("Power bank", "Electronics", "pc", 85000, 540), ("LED bulb", "Electronics", "pc", 9000, 365),
        ("Solar panel 100W", "Electronics", "pc", 380000, 1095), ("Battery AA 4-pack", "Electronics", "pack", 6000, 90),
        ("Bluetooth speaker", "Electronics", "pc", 145000, 720), ("Screen protector", "Electronics", "pc", 10000, 180),
    ],
    "Pet Shop": [
        ("Dog food 10kg", "Pet Care", "bag", 78000, 30), ("Cat litter 5kg", "Pet Care", "bag", 32000, 21),
        ("Flea treatment", "Pet Care", "dose", 25000, 30), ("Dog wash", "Pet Care", "session", 30000, 30),
        ("Dewormer for pets", "Pet Care", "dose", 15000, 90), ("Dog treats", "Pet Care", "pack", 18000, 21),
    ],
    "Restaurant": [
        ("Lunch plate", "General", "plate", 15000, 5), ("Breakfast set", "General", "set", 12000, 5),
        ("Catering 50 pax", "General", "event", 950000, 60), ("Coffee 250g", "Beverages", "pack", 22000, 21),
        ("Fresh juice", "Beverages", "glass", 8000, 7), ("Takeaway pack", "General", "pack", 18000, 7),
    ],
    "Clinic": [
        ("General consultation", "Pharmacy", "visit", 30000, 45), ("Lab test package", "Pharmacy", "test", 65000, 90),
        ("Antenatal visit", "Pharmacy", "visit", 40000, 30), ("Child vaccination", "Pharmacy", "dose", 25000, 60),
        ("Dental cleaning", "Pharmacy", "session", 90000, 180), ("Family planning review", "Pharmacy", "visit", 35000, 90),
    ],
    "Stationery & Print": [
        ("Printing ream A4", "Stationery", "ream", 22000, 45), ("Toner cartridge", "Stationery", "pc", 145000, 120),
        ("Exercise books 48pg", "Stationery", "pc", 1800, 90), ("Pens box", "Stationery", "box", 12000, 90),
        ("Photocopy bundle 200", "Stationery", "bundle", 20000, 21), ("File folders", "Stationery", "pack", 15000, 120),
    ],
}

SHOP_PREFIX = ["Nalubowa", "Kato's", "Zaina", "Mugisha", "Blessed", "Golden", "Ntinda", "Kampala", "Sunrise",
               "Silver", "Nakato", "Prime", "Royal", "Victory", "Good", "City", "Green", "Neighbours", "Mukasa",
               "Excel", "Trust", "Family", "Quick", "Bulungi", "Mama", "Unity", "Pearl", "Highland", "Bright", "Amka"]
SHOP_SUFFIX = {
    "Supermarket": ["Supermarket", "Fresh Mart", "Grocers", "Mini Mart", "Foods"],
    "LPG & Water": ["Gas & Water", "LPG Refill", "Gas Point", "Energy"],
    "Salon & Spa": ["Hair Lounge", "Salon & Spa", "Beauty Studio", "Barbers"],
    "Pharmacy": ["Pharmacy", "Drug Shop", "Pharma Care"],
    "Auto Garage": ["Auto Care", "Garage", "Motor Works", "Auto Spa"],
    "Bakery": ["Bakery", "Bakes", "Confectionery"],
    "Hardware": ["Hardware", "Building Supplies", "Hardwares"],
    "Agrovet": ["Agrovet", "Farm Supplies", "Agro Inputs"],
    "Boutique": ["Boutique", "Fashions", "Apparel", "Collection"],
    "Laundry": ["Laundry", "Cleaners", "Dry Clean"],
    "Gym & Fitness": ["Fitness Centre", "Gym", "Wellness"],
    "Electronics": ["Electronics", "Tech Hub", "Gadgets", "Telecom"],
    "Pet Shop": ["Pet Shop", "Vet Supplies"],
    "Restaurant": ["Restaurant", "Cafe", "Diner", "Kitchen"],
    "Clinic": ["Clinic", "Health Centre", "Medical"],
    "Stationery & Print": ["Stationers", "Print Shop", "Office Supplies"],
}

SERVICE_SECTORS = {"Salon & Spa", "Auto Garage", "Laundry", "Gym & Fitness",
                 "Clinic", "Restaurant"}

SECTOR_WEIGHTS = [("Supermarket", 22), ("LPG & Water", 10), ("Salon & Spa", 12), ("Pharmacy", 10),
                  ("Auto Garage", 8), ("Bakery", 6), ("Hardware", 6), ("Agrovet", 6), ("Boutique", 7),
                  ("Laundry", 4), ("Gym & Fitness", 4), ("Electronics", 7), ("Pet Shop", 2), ("Restaurant", 5),
                  ("Clinic", 5), ("Stationery & Print", 4)]

PLAN_MIX = [("starter", 62), ("growth", 30), ("scale", 8)]
STATUS_MIX = [("active", 74), ("trial", 18), ("paused", 5), ("churned", 3)]

CHANNELS = [("whatsapp", 78), ("sms", 14), ("email", 8)]


def pick(weighted):
    items, weights = zip(*weighted)
    return random.choices(items, weights=weights)[0]


_USED_PHONES: set[str] = set()


def phone(i: int) -> str:
    """Deterministic-ish Ugandan MSISDN, guaranteed unique inside the dataset."""
    for _ in range(50):
        cand = f"+2567{random.randint(0, 9)}{random.randint(0, 999999):06d}"
        if cand not in _USED_PHONES:
            _USED_PHONES.add(cand)
            return cand
    cand = f"+2567{1000000 + i:07d}"
    _USED_PHONES.add(cand)
    return cand


# ------------------------------------------------------------------ bootstrapping
def ensure_bootstrapped(force: bool = False) -> dict:
    db.init_schema()
    engine.seed_default_templates()
    if db.scalar("SELECT COUNT(*) FROM users", (), 0) == 0 or force:
        return generate_demo_fleet(shops=int(os.environ.get("REBUY_SEED_SHOPS", "120")), reset=force)
    return {"skipped": True}


def _clear_all() -> None:
    order = ("messages", "reminders", "loyalty_events", "coupons", "campaigns", "receipts", "import_jobs",
             "sale_items", "sales", "products", "customers", "credentials", "audit_log", "webhook_events",
             "sessions", "users", "shops", "templates", "platform_meta")
    if db.IS_PG:
        db.x("TRUNCATE " + ", ".join(order) + " RESTART IDENTITY CASCADE")
        return
    for t in order:
        db.x(f"DELETE FROM {t}")
    for t in ("messages", "reminders", "customers", "sales", "sale_items", "products", "shops", "users", "coupons"):
        db.x(f"DELETE FROM sqlite_sequence WHERE name=?", (t,))


def generate_demo_fleet(shops: int = 120, reset: bool = False) -> dict:
    random.seed(RNG_SEED)
    if reset:
        _clear_all()
        engine.seed_default_templates()
    today = db.utcnow()
    stats = {"shops": 0, "users": 0, "customers": 0, "products": 0, "sales": 0, "items": 0,
             "reminders": 0, "messages": 0, "coupons": 0}

    # ---------------------------------------------------------------- shops
    sectors = [pick(SECTOR_WEIGHTS) for _ in range(max(4, shops))]
    sectors[0], sectors[1], sectors[2], sectors[3] = "Supermarket", "Salon & Spa", "LPG & Water", "Auto Garage"
    # the first four shops are the ones we demo with, so they get fixed identities
    FIXED = {
        1: ("Kampala Fresh Mart", "kampala-fresh-mart", "Sarah Nalubowa", "owner@kampalafresh.ug", "Kampala"),
        2: ("Nakato Hair Lounge", "nakato-hair-lounge", "Doreen Nakato", "owner@nakatohair.ug", "Kampala"),
        3: ("Gas & Go LPG", "gas-and-go-lpg", "Peter Mugisha", "owner@gasandgo.ug", "Wakiso"),
        4: ("Kato Auto Care", "kato-auto-care", "Joseph Kato", "owner@katoauto.ug", "Kampala"),
    }
    shop_rows, used_names = [], {v[0] for v in FIXED.values()}
    for i in range(shops):
        sector = sectors[i]
        fixed = FIXED.get(i + 1)
        if fixed:
            name, slug = fixed[0], fixed[1]
        else:
            while True:
                name = f"{random.choice(SHOP_PREFIX)} {random.choice(SHOP_SUFFIX[sector])}"
                if name not in used_names:
                    used_names.add(name)
                    break
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        plan = pick(PLAN_MIX)
        status = "active" if i < 4 else pick(STATUS_MIX)
        created = today - timedelta(days=random.randint(3, 330), hours=random.randint(0, 23))
        price = {"starter": 49000, "growth": 129000, "scale": 299000}[plan]
        owner = fixed[2] if fixed else f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
        email = fixed[3] if fixed else f"{slug.replace('-', '.')}@{random.choice(['gmail.com', 'outlook.com', 'yahoo.com', 'rebuy.ug'])}"
        ph = phone(i)
        city = fixed[4] if fixed else random.choice(CITIES)
        shop_rows.append((i + 1, name, slug, owner, email, ph, city, "Uganda", sector, plan,
                          status, price, "UGX", "en", "Africa/Kampala", name[:32], ph, 1,
                          random.choice([2, 3, 4]), random.choice([1, 2]), random.choice([4, 6, 8]),
                          "21:00", "07:00", 1 if random.random() < 0.75 else 0, "buy_5_get_1",
                          created.strftime("%Y-%m-%d %H:%M:%S"),
                          (created + timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"),
                          created.strftime("%Y-%m-%d %H:%M:%S")))
    db.xm("""INSERT INTO shops (id,name,slug,owner_name,owner_email,phone,city,country,sector,plan,status,
             monthly_price,currency,language,timezone,sender_name,whatsapp_number,auto_reminders,reminder_lead_days,
             max_followups,max_msgs_per_customer_month,quiet_start,quiet_end,loyalty_enabled,loyalty_rule,
             onboarded_at,trial_ends_at,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", shop_rows)
    stats["shops"] = len(shop_rows)

    # ---------------------------------------------------------------- users
    admin_id = auth.create_user(None, "admin", "Platform Admin", "admin@rebuy.app", "Admin1234!")
    stats["users"] += 1
    uid = auth.create_user(1, "owner", "Sarah Nalubowa", "owner@kampalafresh.ug", "Shop1234!")
    stats["users"] += 1
    for i, r in enumerate(shop_rows[1:24], start=2):
        auth.create_user(i, "owner", r[3], f"{r[2].replace('-', '.')}@rebuy.ug", "Shop1234!")
        stats["users"] += 1


    # ------------------------------------------------------- catalogue + data
    cust_rows, prod_rows, sale_rows, item_rows = [], [], [], []
    rem_rows, msg_rows, coupon_rows, job_rows = [], [], [], []
    cust_id = prod_id = sale_id = item_id = rem_id = msg_id = cp_id = 0
    now_str = db.now_iso()
    recency = [(2, 9), (5, 11), (9, 12), (14, 10), (20, 9), (28, 9), (40, 8), (60, 7), (85, 6),
               (120, 5), (170, 4), (240, 3)]
    service_units = {"session", "service", "visit", "test", "wash", "month", "year", "pass", "course",
                     "event", "load", "trip", "dose", "set"}

    for shop in shop_rows:
        sid, sector, status = shop[0], shop[8], shop[10]
        rich = sid <= 4
        catalogue = CATALOGUES.get(sector, CATALOGUES["Supermarket"])
        chosen = catalogue[:len(catalogue) if rich else max(6, int(len(catalogue) * random.uniform(0.5, 0.9)))]
        prod_map = {}
        for (pname, cat, unit, price, cycle) in chosen:
            prod_id += 1
            kind = "service" if (sector in SERVICE_SECTORS or unit in service_units) else "goods"
            if kind == "goods" and unit in ("session", "service", "visit", "wash"):
                kind = "service"
            prod_rows.append((prod_id, sid, pname, f"SKU-{sid}-{prod_id % 997:03d}", cat, kind, unit,
                              round(price * random.uniform(0.9, 1.15)), cycle, 1, 1, now_str))
            prod_map[pname] = {"id": prod_id, "name": pname, "unit": unit, "price": price,
                               "cycle": cycle, "cat": cat, "kind": kind}
        avg_cycle = sum(p["cycle"] for p in prod_map.values()) / max(1, len(prod_map))

        # ---- customers -------------------------------------------------------
        n_customers = 220 if sid == 1 else (120 if rich else random.randint(18, 90))
        if status == "churned":
            n_customers = max(6, int(n_customers * 0.4))
        shop_customers = []
        for _ in range(n_customers):
            cust_id += 1
            nm = f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"
            ph = phone(cust_id)
            opted = 1 if random.random() < 0.94 else 0
            created = today - timedelta(days=random.randint(5, HISTORY_DAYS + 20))
            email = (f"{nm.split()[0].lower()}.{random.randint(1, 99)}@"
                     f"{random.choice(['gmail.com', 'yahoo.com', 'outlook.com'])}") if random.random() < 0.45 else ""
            cust_rows.append((cust_id, sid, nm, ph, ph if random.random() < 0.9 else "", email,
                              random.choice(AREAS), random.choice(["regular", "vip", "regular", "walk-in", "corporate", ""]),
                              "", "en", opted, None if opted else now_str, "sale",
                              f"{random.randint(1970, 2010)}-{random.randint(1, 12):02d}-{random.randint(1, 28):02d}"
                              if random.random() < 0.5 else "",
                              created.strftime("%Y-%m-%d %H:%M:%S"), "", 0.0, 0, 0, 0.0, "new",
                              created.strftime("%Y-%m-%d %H:%M:%S"), now_str))
            shop_customers.append(cust_id)

        # ---- sales, generated per customer so recency (and therefore risk) is realistic
        for c_id in shop_customers:
            n_visits = pick([(1, 16), (2, 20), (3, 20), (4, 16), (5, 12), (6, 8), (8, 5), (11, 3)])
            days_ago = pick(recency)
            if status == "churned":
                days_ago = min(420, days_ago + random.randint(60, 220))
            if rich and random.random() < 0.25:
                n_visits += random.randint(1, 4)
            for _ in range(n_visits):
                if days_ago > 430:
                    break
                sdate = today - timedelta(days=days_ago, hours=random.randint(8, 19), minutes=random.randint(0, 59))
                sale_id += 1
                n_items = random.choices([1, 2, 3, 4, 5], weights=[36, 28, 20, 11, 5])[0]
                lines = random.sample(list(prod_map.values()), min(n_items, len(prod_map)))
                total, receipt = 0.0, f"R{sale_id:06d}"
                sale_rows.append((sale_id, sid, receipt, c_id, sdate.strftime("%Y-%m-%d"), sdate.strftime("%H:%M"),
                                  0.0, "UGX", pick([("cash", 44), ("mobile_money", 42), ("card", 9), ("credit", 5)]),
                                  pick([("in_store", 74), ("delivery", 16), ("phone", 10)]), "", "seed", "",
                                  sdate.strftime("%Y-%m-%d %H:%M:%S")))
                for p in lines:
                    item_id += 1
                    qty = (random.choices([1, 2, 3, 5], weights=[72, 19, 6, 3])[0] if p["kind"] == "goods" else 1)
                    price = p["price"]
                    lt = round(price * qty, 2)
                    total += lt
                    cycle = p["cycle"] if p["kind"] == "service" else min(540, max(3, int(round(p["cycle"] * qty))))
                    item_rows.append((item_id, sale_id, sid, p["id"], p["name"], p["cat"], p["kind"], qty,
                                      p["unit"], price, lt, cycle,
                                      (sdate + timedelta(days=cycle)).strftime("%Y-%m-%d")))
                sale_rows[-1] = tuple(list(sale_rows[-1][:6]) + [round(total, 2)] + list(sale_rows[-1][7:]))
                days_ago += max(3, int(avg_cycle * random.uniform(0.55, 1.7)))

    db.xm("INSERT INTO products (id,shop_id,name,sku,category,kind,unit,unit_price,cycle_days,baseline_qty,active,created_at) "
          "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", prod_rows)
    db.xm("""INSERT INTO customers (id,shop_id,name,phone,whatsapp,email,area,tags,notes,language,opted_in,opt_out_at,
             consent_source,birthday,first_seen,last_purchase_at,total_spent,visit_count,loyalty_points,lifetime_value,
             risk_band,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", cust_rows)
    db.xm("""INSERT INTO sales (id,shop_id,receipt_no,customer_id,sale_date,sale_time,total,currency,payment_method,
             channel,staff_name,source,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", sale_rows)
    db.xm("""INSERT INTO sale_items (id,sale_id,shop_id,product_id,product_name,category,kind,qty,unit,unit_price,
             line_total,cycle_days,depletes_on) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""", item_rows)
    stats.update(customers=len(cust_rows), products=len(prod_rows), sales=len(sale_rows), items=len(item_rows))

    # --------------------------------------------------- reminders + messages
    sale_index = {s[0]: s for s in sale_rows}
    cust_index = {c[0]: c for c in cust_rows}
    shop_index = {s[0]: s for s in shop_rows}
    outcome_weights = [("converted", 20), ("replied", 12), ("delivered", 18), ("sent", 20),
                       ("failed", 6), ("cancelled", 6), ("superseded", 8)]
    status_map = {"converted": "delivered", "replied": "replied", "sent": "sent", "delivered": "delivered",
                  "failed": "failed", "superseded": "delivered"}
    reply_texts = ["Yes please, keep it for me", "Sure, I will come tomorrow", "How much is it now?",
                   "Not yet, I still have some", "Please deliver 2 in the evening", "No thanks, I bought elsewhere",
                   "Okay confirmed", "STOP", "Let me check and get back to you", "👍", "What time do you close?",
                   "Yes, and add 1kg of sugar"]
    for it in item_rows:
        item_id, sale_id_, sid, pid, pname, cat, kind, qty, unit, price, lt, cycle, depletes = it
        sale = sale_index[sale_id_]
        cust = cust_index[sale[3]]
        shop = shop_index[sid]
        if not cust[10]:
            continue
        lead = shop[18]
        due_dt = engine.pdate(depletes) - timedelta(days=lead)
        rkind = "service_due" if kind == "service" else "replenish"
        channel = "whatsapp" if cust[4] else ("email" if cust[5] else "sms")
        created = sale[13]

        if due_dt > today:
            rem_id += 1
            due_str = due_dt.strftime("%Y-%m-%d")
            # keep a live queue: a slice of future reminders is due right now
            if random.random() < 0.06:
                due_str = (today - timedelta(days=random.randint(0, 2))).strftime("%Y-%m-%d")
            rem_rows.append((rem_id, sid, cust[0], item_id, pname, rkind, 1, due_str, "scheduled", channel,
                             None, None, None, None, None, 0.0, None, created))
            continue

        outcome = pick(outcome_weights)
        sent_dt = min(due_dt.replace(hour=random.randint(8, 18), minute=random.randint(0, 59)),
                      today - timedelta(hours=random.randint(1, 20)))
        sent = sent_dt.strftime("%Y-%m-%d %H:%M:%S")
        coupon_id, code = None, ""
        if outcome in ("converted", "replied", "superseded") and random.random() < 0.22:
            cp_id += 1
            code = f"{rkind[:3].upper()}{cp_id:05d}"
            rem_id += 1
            coupon_rows.append((cp_id, sid, code, "Auto-issued by Rebuy", "percent",
                                random.choice([5, 10, 10, 15]), 0.0,
                                (sent_dt + timedelta(days=14)).strftime("%Y-%m-%d"), 1,
                                1 if outcome == "converted" else 0, cust[0], rem_id,
                                "used" if outcome == "converted" else "active", sent))
            coupon_id = cp_id
        else:
            rem_id += 1
        # Old "sent, never answered" reminders would otherwise look like live
        # follow-up candidates forever; age them out so the queue stays realistic.
        if outcome in ("sent", "delivered") and sent_dt < today - timedelta(days=21):
            outcome = "cancelled"
        rem_rows.append((rem_id, sid, cust[0], item_id, pname, rkind, 1, due_dt.strftime("%Y-%m-%d"), outcome,
                         channel, coupon_id, None,
                         sent if outcome != "cancelled" else None,
                         sent_dt.strftime("%Y-%m-%d %H:%M:%S") if outcome in ("replied", "superseded") else None,
                         (sent_dt + timedelta(days=random.randint(1, 6))).strftime("%Y-%m-%d %H:%M:%S") if outcome == "converted" else None,
                         lt if outcome == "converted" else 0.0, None, created))
        if outcome == "cancelled":
            continue

        first = (cust[2].split() or ["there"])[0]
        if kind == "service":
            body = (f"Hi {first}, it has been {cycle} days since your {pname} at {shop[1]}. You're due again "
                    f"around {depletes}. Reply YES and we'll book you a slot this week.")
        else:
            body = (f"Hi {first}, your {pname} ({qty:g} {unit}, bought {sale[4]}) should be running low around "
                    f"{depletes}. Shall we set aside {qty:g} {unit} for you? Reply YES and we'll keep it ready.")
        if code:
            body += f" Use code {code} for {coupon_rows[-1][5]:.0f}% off."
        msg_id += 1
        status = status_map[outcome]
        msg_rows.append((msg_id, sid, cust[0], rem_id, None, "out", channel, "sandbox",
                         cust[3] if channel != "email" else cust[5], shop[16],
                         f"Your {pname} — {shop[1]}" if channel == "email" else "", body, rkind, status,
                         f"sbx-wa-{msg_id}", "recipient number not on WhatsApp" if outcome == "failed" else "",
                         120.0 if channel == "sms" else 0.0, "", sent,
                         sent_dt.strftime("%Y-%m-%d %H:%M:%S") if status != "failed" else None, ""))
        reply_chance = {"replied": 1.0, "converted": 0.78, "superseded": 0.45}.get(outcome, 0.12)
        wants_reply = random.random() < reply_chance
        if wants_reply:
            msg_id += 1
            reply_dt = min(sent_dt + timedelta(minutes=random.randint(4, 1200)), today)
            txt = random.choice(reply_texts)
            intent = providers.classify_intent(txt)
            msg_rows.append((msg_id, sid, cust[0], rem_id, None, "in", channel, "sandbox", cust[3], shop[16],
                             "", txt, "", "replied", "", "", 0.0, intent,
                             reply_dt.strftime("%Y-%m-%d %H:%M:%S"), reply_dt.strftime("%Y-%m-%d %H:%M:%S"), ""))
            if intent == "opt_out":
                db.x("UPDATE customers SET opted_in=0, opt_out_at=? WHERE id=?",
                     (reply_dt.strftime("%Y-%m-%d %H:%M:%S"), cust[0]))

    db.xm("""INSERT INTO coupons (id,shop_id,code,description,discount_type,discount_value,min_spend,expires_on,
             max_uses,used_count,customer_id,reminder_id,status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", coupon_rows)
    db.xm("""INSERT INTO reminders (id,shop_id,customer_id,sale_item_id,product_name,kind,attempt,due_on,status,
             channel,coupon_id,message_id,sent_at,replied_at,converted_at,revenue,snooze_until,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", rem_rows)
    db.xm("""INSERT INTO messages (id,shop_id,customer_id,reminder_id,campaign_id,direction,channel,provider,
             to_addr,from_addr,subject,body,template_key,status,provider_msg_id,error,cost,intent,created_at,
             sent_at,delivered_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", msg_rows)
    # a couple of historical import jobs so the Imports page has a trail
    for jid, (kind, fn, ok, fail) in enumerate([("csv", "sales_book_2025.xlsx", 1240, 3),
                                                ("receipt_ocr", "receipt_R10482.jpg", 6, 0),
                                                ("csv", "pos_export_oct.csv", 318, 12)], start=1):
        job_rows.append((jid, 1, kind, fn, ok + fail, ok, fail, "done",
                         json.dumps({"sales": ok, "customers": int(ok * 0.7), "reminders": int(ok * 1.4),
                                     "errors": [f"row {i}: missing phone number" for i in range(min(fail, 3))]}),
                         (today - timedelta(days=jid * 9)).strftime("%Y-%m-%d %H:%M:%S")))
    db.xm("""INSERT INTO import_jobs (id,shop_id,kind,filename,rows_total,rows_ok,rows_failed,status,report,created_at)
             VALUES (?,?,?,?,?,?,?,?,?,?)""", job_rows)
    stats.update(reminders=len(rem_rows), messages=len(msg_rows), coupons=len(coupon_rows))


    _bulk_aggregates()
    _seed_credentials()
    _seed_platform_meta()
    db.audit("system", "seed.demo_fleet", None, detail=json.dumps(stats))
    return stats


def _bulk_aggregates() -> None:
    """Recompute customer aggregates + risk bands in set-based SQL (fast for 100k+ rows)."""
    db.x("""UPDATE customers SET
              visit_count = (SELECT COUNT(*) FROM sales s WHERE s.customer_id = customers.id),
              total_spent = (SELECT IFNULL(SUM(s.total),0) FROM sales s WHERE s.customer_id = customers.id),
              last_purchase_at = (SELECT MAX(s.sale_date) FROM sales s WHERE s.customer_id = customers.id),
              first_seen = IFNULL((SELECT MIN(s.sale_date) FROM sales s WHERE s.customer_id = customers.id), first_seen)""")
    db.x("""UPDATE customers SET lifetime_value =
              ROUND((IFNULL(total_spent,0) / MAX(1, visit_count) * MIN(visit_count, 12))::numeric, 2)""")
    db.x("""UPDATE customers SET risk_band = CASE
              WHEN last_purchase_at IS NULL OR visit_count = 0 THEN 'new'
              WHEN (julianday('now') - julianday(last_purchase_at)) /
                   MAX(6.0, IFNULL((SELECT AVG(si.cycle_days) FROM sale_items si
                                    JOIN sales s ON s.id = si.sale_id WHERE s.customer_id = customers.id), 30))
                   < 0.8 THEN 'healthy'
              WHEN (julianday('now') - julianday(last_purchase_at)) /
                   MAX(6.0, IFNULL((SELECT AVG(si.cycle_days) FROM sale_items si
                                    JOIN sales s ON s.id = si.sale_id WHERE s.customer_id = customers.id), 30))
                   < 1.15 THEN 'due'
              WHEN (julianday('now') - julianday(last_purchase_at)) /
                   MAX(6.0, IFNULL((SELECT AVG(si.cycle_days) FROM sale_items si
                                    JOIN sales s ON s.id = si.sale_id WHERE s.customer_id = customers.id), 30))
                   < 1.7 THEN 'overdue'
              WHEN (julianday('now') - julianday(last_purchase_at)) /
                   MAX(6.0, IFNULL((SELECT AVG(si.cycle_days) FROM sale_items si
                                    JOIN sales s ON s.id = si.sale_id WHERE s.customer_id = customers.id), 30))
                   < 3.2 THEN 'at_risk'
              ELSE 'lost' END""")
    db.x("UPDATE customers SET loyalty_points = "
         "CASE WHEN visit_count >= 5 THEN MIN(7, visit_count - 4 + (id % 3)) ELSE 0 END")
    db.x("""UPDATE shops SET trial_ends_at = CASE WHEN status='trial' THEN dayn(id, 3 + (id % 11))
                                                  ELSE trial_ends_at END""")


def _seed_credentials() -> None:
    """Platform channels start on the sandbox driver; live drivers are switched on
    from Admin -> Integrations (or via environment variables)."""
    now = db.now_iso()
    for ch, driver in (("whatsapp", "sandbox"), ("sms", "sandbox"), ("email", "sandbox")):
        if not db.q1("SELECT id FROM credentials WHERE scope='platform' AND provider=?", (ch,)):
            db.insert("credentials", {"scope": "platform", "shop_id": None, "provider": ch, "driver": driver,
                                      "enabled": 1, "config": json.dumps({}), "updated_at": now})
    # a few shops have connected their own WhatsApp Business number
    for sid in (1, 2, 3, 4, 6, 9):
        if not db.q1("SELECT id FROM credentials WHERE scope='shop' AND shop_id=? AND provider='whatsapp'", (sid,)):
            db.insert("credentials", {
                "scope": "shop", "shop_id": sid, "provider": "whatsapp", "driver": "sandbox", "enabled": 1,
                "config": json.dumps({"from_number": db.scalar("SELECT whatsapp_number FROM shops WHERE id=?", (sid,), ""),
                                      "note": "Sandbox until the Meta token + phone number id are pasted in."}),
                "updated_at": now})


def _seed_platform_meta() -> None:
    db.meta_set("seeded", "1")
    db.meta_set("platform_name", "Rebuy")
    db.meta_set("support_email", "support@rebuy.app")
    db.meta_set("currency", "UGX")


# --------------------------------------------------------------- sample imports
SAMPLE_CSV = """date,receipt_no,customer,phone,item,qty,price,total,payment
{s0},R1001,{n0},{p0},{i0},2,{pr0},{t0},mobile_money
{s0},R1001,{n0},{p0},{i1},1,{pr1},{t1},mobile_money
{s1},R1002,{n1},{p1},{i2},1,{pr2},{t2},cash
{s2},R1003,{n2},{p2},{i0},5,{pr0b},{t3},cash
{s3},R1004,{n0},{p0},{i3},1,{pr3},{t4},card
{s4},R1005,{n3},{p3},{i2},3,{pr2b},{t5},mobile_money
"""


def sample_csv() -> str:
    products = CATALOGUES["Supermarket"]
    d = db.utcnow()
    names = [f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}" for _ in range(4)]
    vals = {
        "s0": (d - timedelta(days=2)).strftime("%Y-%m-%d"), "s1": (d - timedelta(days=12)).strftime("%Y-%m-%d"),
        "s2": (d - timedelta(days=40)).strftime("%Y-%m-%d"), "s3": d.strftime("%Y-%m-%d"),
        "s4": (d - timedelta(days=6)).strftime("%Y-%m-%d"),
        "n0": names[0], "p0": "+256700111222", "n1": names[1], "p1": "+256701223344",
        "n2": names[2], "p2": "+256702334455", "n3": names[3], "p3": "+256703445566",
        "i0": products[0][0], "pr0": products[0][3], "t0": products[0][3] * 2,
        "i1": products[2][0], "pr1": products[2][3], "t1": products[2][3],
        "i2": products[3][0], "pr2": products[3][3], "t2": products[3][3], "pr2b": products[3][3], "t5": products[3][3] * 3,
        "pr0b": products[0][3], "t3": products[0][3] * 5,
        "i3": products[10][0], "pr3": products[10][3], "t4": products[10][3],
    }
    return SAMPLE_CSV.format(**vals)


SAMPLE_RECEIPT = """        KAMPALA FRESH MART
     Plot 45, Ntinda Road
    Receipt No: R10482
    Date: {date}   Time: 16:42
Served by: Grace

Customer: {name}
Phone: {phone}

Maize flour 2kg        2   4,500   9,000
Cooking oil 1L         1   9,500   9,500
Sugar 1kg              3   4,200  12,600
Bathing soap           2   2,500   5,000

Subtotal                    36,100
Discount                      -1,100
TOTAL                        35,000
Paid (Mobile Money)          35,000
Change                           0

   Thank you, come again!
"""


def sample_receipt() -> str:
    return SAMPLE_RECEIPT.format(date=db.utcnow().strftime("%d/%m/%Y"),
                                 name=f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}",
                                 phone="+256704556677")
