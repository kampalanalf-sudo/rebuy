"""
Rebuy — storage layer.

Two backends behind one API:

  * SQLite  (default, zero-config local dev / single-box deploys)
  * PostgreSQL / Supabase  (set REBUY_DATABASE_URL or DATABASE_URL)

Application SQL is written once with `?` placeholders + `IFNULL(...)`; the
Postgres driver rewrites `?` -> `%s` and NULL-safe helpers on the way through,
so no caller needs to know which engine is live.

Everything is scoped by shop_id so one installation can host 100+ shops.
Indexes are declared for the hot paths (reminders due, messages queue,
customer lookup by phone, per-shop sales by date).

Timestamps are stored as ISO-8601 strings in the platform timezone
(default Africa/Kampala, UTC+3) so date maths on "days" is trivial and
human-readable in the database.
"""
from __future__ import annotations

import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

TZ_OFFSET_HOURS = 3  # Africa/Kampala (EAT). Change per deployment.
DB_PATH = os.environ.get("REBUY_DB", os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "rebuy.db"))

# Supabase/pg connection string. Either name works; REBUY_DATABASE_URL wins.
DATABASE_URL = (os.environ.get("REBUY_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
IS_PG = bool(DATABASE_URL)

_local = threading.local()

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS shops (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  owner_name      TEXT DEFAULT '',
  owner_email     TEXT DEFAULT '',
  phone           TEXT DEFAULT '',
  city            TEXT DEFAULT '',
  country         TEXT DEFAULT 'Uganda',
  sector          TEXT DEFAULT 'Retail',
  plan            TEXT DEFAULT 'starter',      -- starter | growth | scale
  status          TEXT DEFAULT 'trial',        -- trial | active | paused | churned
  monthly_price   REAL DEFAULT 0,
  currency        TEXT DEFAULT 'UGX',
  language        TEXT DEFAULT 'en',
  timezone        TEXT DEFAULT 'Africa/Kampala',
  sender_name     TEXT DEFAULT '',             -- business name shown on WhatsApp/SMS
  whatsapp_number TEXT DEFAULT '',             -- sender / business number
  auto_reminders  INTEGER DEFAULT 1,
  reminder_lead_days INTEGER DEFAULT 3,
  max_followups   INTEGER DEFAULT 2,
  max_msgs_per_customer_month INTEGER DEFAULT 6,
  quiet_start     TEXT DEFAULT '21:00',
  quiet_end       TEXT DEFAULT '07:00',
  loyalty_enabled INTEGER DEFAULT 1,
  loyalty_rule    TEXT DEFAULT 'buy_5_get_1',
  onboarded_at    TEXT,
  trial_ends_at   TEXT,
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_shops_status ON shops(status);
CREATE INDEX IF NOT EXISTS idx_shops_created ON shops(created_at);

CREATE TABLE IF NOT EXISTS users (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       INTEGER,                       -- NULL => platform staff
  role          TEXT NOT NULL DEFAULT 'owner', -- admin | owner | staff
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT DEFAULT '',
  salt          TEXT DEFAULT '',
  google_sub    TEXT DEFAULT '',
  email_verified INTEGER DEFAULT 0,
  last_login    TEXT,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ip         TEXT DEFAULT '',
  user_agent TEXT DEFAULT '',
  method     TEXT DEFAULT 'password',
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS password_resets (
  token_hash TEXT PRIMARY KEY,
  user_id    INTEGER NOT NULL,
  expires_at TEXT NOT NULL,
  used_at    TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS invites (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER NOT NULL,
  email      TEXT NOT NULL,
  role       TEXT DEFAULT 'staff',
  token_hash TEXT NOT NULL,
  invited_by TEXT DEFAULT '',
  expires_at TEXT NOT NULL,
  accepted_at TEXT,
  created_at TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invite ON invites(shop_id, email);

CREATE TABLE IF NOT EXISTS customers (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id        INTEGER NOT NULL,
  name           TEXT NOT NULL,
  phone          TEXT DEFAULT '',
  whatsapp       TEXT DEFAULT '',
  email          TEXT DEFAULT '',
  area           TEXT DEFAULT '',
  tags           TEXT DEFAULT '',              -- comma separated
  notes          TEXT DEFAULT '',
  language       TEXT DEFAULT 'en',
  opted_in       INTEGER DEFAULT 1,
  opt_out_at     TEXT,
  consent_source TEXT DEFAULT 'sale',
  birthday       TEXT DEFAULT '',
  first_seen     TEXT,
  last_purchase_at TEXT,
  total_spent    REAL DEFAULT 0,
  visit_count    INTEGER DEFAULT 0,
  loyalty_points INTEGER DEFAULT 0,
  lifetime_value REAL DEFAULT 0,
  risk_band      TEXT DEFAULT 'new',           -- new | healthy | due | overdue | at_risk | lost
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_phone ON customers(shop_id, phone) WHERE phone <> '';
CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id, last_purchase_at);
CREATE INDEX IF NOT EXISTS idx_customers_risk ON customers(shop_id, risk_band);

CREATE TABLE IF NOT EXISTS products (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       INTEGER NOT NULL,
  name          TEXT NOT NULL,
  sku           TEXT DEFAULT '',
  category      TEXT DEFAULT 'General',
  kind          TEXT DEFAULT 'goods',          -- goods | service
  unit          TEXT DEFAULT 'pc',
  unit_price    REAL DEFAULT 0,
  cycle_days    INTEGER DEFAULT 30,            -- how long one unit lasts / service interval
  baseline_qty  REAL DEFAULT 1,                 -- qty the cycle_days refers to
  active        INTEGER DEFAULT 1,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_product ON products(shop_id, name, unit);
CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id, active);

CREATE TABLE IF NOT EXISTS sales (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id        INTEGER NOT NULL,
  receipt_no     TEXT DEFAULT '',
  customer_id    INTEGER,
  sale_date      TEXT NOT NULL,                 -- YYYY-MM-DD
  sale_time      TEXT DEFAULT '',
  total          REAL DEFAULT 0,
  currency       TEXT DEFAULT 'UGX',
  payment_method TEXT DEFAULT 'cash',
  channel        TEXT DEFAULT 'in_store',       -- in_store | delivery | phone | online
  staff_name     TEXT DEFAULT '',
  source         TEXT DEFAULT 'manual',         -- manual | csv | receipt_ocr | api | whatsapp
  notes          TEXT DEFAULT '',
  created_at     TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_sales_shop_date ON sales(shop_id, sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id, sale_date);
CREATE INDEX IF NOT EXISTS idx_sales_source ON sales(shop_id, source);

CREATE TABLE IF NOT EXISTS sale_items (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id       INTEGER NOT NULL,
  shop_id       INTEGER NOT NULL,
  product_id    INTEGER,
  product_name  TEXT NOT NULL,
  category      TEXT DEFAULT 'General',
  kind          TEXT DEFAULT 'goods',
  qty           REAL NOT NULL DEFAULT 1,
  unit          TEXT DEFAULT 'pc',
  unit_price    REAL DEFAULT 0,
  line_total    REAL DEFAULT 0,
  cycle_days    INTEGER DEFAULT 30,
  depletes_on   TEXT,                           -- expected run-out / next service due
  FOREIGN KEY (sale_id) REFERENCES sales(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_items_deplete ON sale_items(shop_id, depletes_on);

CREATE TABLE IF NOT EXISTS shop_places (          -- physical branches, geofenced
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL,
  label       TEXT DEFAULT 'Main branch',
  address     TEXT DEFAULT '',
  lat         REAL NOT NULL,
  lng         REAL NOT NULL,
  radius_m    INTEGER DEFAULT 400,
  open_from   TEXT DEFAULT '07:00',
  open_to     TEXT DEFAULT '21:00',
  active      INTEGER DEFAULT 1,
  created_at  TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_places_shop ON shop_places(shop_id);

CREATE TABLE IF NOT EXISTS consents (             -- one row per customer per purpose
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER NOT NULL,
  shop_id     INTEGER NOT NULL,
  purpose     TEXT NOT NULL,                     -- location | marketing | analytics
  granted     INTEGER DEFAULT 0,
  source      TEXT DEFAULT '',                   -- app | web | sale | staff
  granted_at  TEXT,
  revoked_at  TEXT,
  updated_at  TEXT NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_consent ON consents(customer_id, purpose);

CREATE TABLE IF NOT EXISTS devices (              -- native app installs
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id  INTEGER,
  shop_id      INTEGER,
  token_hash   TEXT NOT NULL UNIQUE,
  platform     TEXT DEFAULT 'android',
  push_token   TEXT DEFAULT '',
  app_version  TEXT DEFAULT '',
  lat          REAL,
  lng          REAL,
  accuracy_m   REAL,
  last_seen_at TEXT,
  created_at   TEXT NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_devices_customer ON devices(customer_id);
CREATE INDEX IF NOT EXISTS idx_devices_shop ON devices(shop_id);

CREATE TABLE IF NOT EXISTS visits (               -- geofence entry/exit events
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL,
  place_id    INTEGER,
  customer_id INTEGER,
  device_id   INTEGER,
  event       TEXT DEFAULT 'enter',              -- enter | exit | dwell
  lat         REAL,
  lng         REAL,
  distance_m  REAL,
  dwell_min   INTEGER DEFAULT 0,
  sale_id     INTEGER,                           -- attributed purchase, if any
  created_at  TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_visits_shop_time ON visits(shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_visits_customer ON visits(customer_id);

CREATE TABLE IF NOT EXISTS nudge_decisions (      -- every AI decision, and why
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       INTEGER NOT NULL,
  customer_id   INTEGER,
  reminder_id   INTEGER,
  trigger_kind  TEXT DEFAULT 'time',              -- time | proximity
  product_name  TEXT DEFAULT '',
  score         REAL DEFAULT 0,
  expected_value REAL DEFAULT 0,
  send          INTEGER DEFAULT 0,
  reason        TEXT DEFAULT '',
  features      TEXT DEFAULT '{}',
  discount_pct  REAL DEFAULT 0,
  channel       TEXT DEFAULT '',
  send_at       TEXT DEFAULT '',                  -- best-time slot
  outcome       TEXT DEFAULT '',                  -- opened | replied | converted | ignored
  revenue       REAL DEFAULT 0,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_nd_shop ON nudge_decisions(shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_nd_reminder ON nudge_decisions(reminder_id);

CREATE TABLE IF NOT EXISTS nudge_sends (          -- throttle: one nudge per customer per window
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  kind        TEXT DEFAULT 'proximity',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ns_lookup ON nudge_sends(shop_id, customer_id, created_at);

CREATE TABLE IF NOT EXISTS nudge_settings (       -- per-shop knobs, one row
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id             INTEGER NOT NULL UNIQUE,
  enabled             INTEGER DEFAULT 1,
  proximity_enabled   INTEGER DEFAULT 1,
  ai_scoring          INTEGER DEFAULT 1,
  min_score           REAL DEFAULT 45,
  max_per_week        INTEGER DEFAULT 2,
  quiet_minutes       INTEGER DEFAULT 240,
  max_discount_pct    REAL DEFAULT 15,
  discount_aggressiveness REAL DEFAULT 0.5,       -- 0 = never discount, 1 = discount early
  best_time_enabled   INTEGER DEFAULT 1,
  updated_at          TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS push_tokens (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  customer_id INTEGER,
  shop_id     INTEGER,
  token       TEXT NOT NULL,
  platform    TEXT DEFAULT 'android',
  active      INTEGER DEFAULT 1,
  created_at  TEXT NOT NULL,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_push_token ON push_tokens(token);

CREATE TABLE IF NOT EXISTS reminders (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       INTEGER NOT NULL,
  customer_id   INTEGER NOT NULL,
  sale_item_id  INTEGER,
  product_name  TEXT DEFAULT '',
  kind          TEXT DEFAULT 'replenish',       -- replenish | service_due | followup | winback | loyalty | birthday | campaign
  attempt       INTEGER DEFAULT 1,
  due_on        TEXT NOT NULL,                  -- date the reminder should fire
  status        TEXT DEFAULT 'scheduled',       -- scheduled | queued | sent | delivered | replied | converted | failed | cancelled | snoozed
  channel       TEXT DEFAULT 'whatsapp',
  coupon_id     INTEGER,
  message_id    INTEGER,
  sent_at       TEXT,
  replied_at    TEXT,
  converted_at  TEXT,
  revenue       REAL DEFAULT 0,
  snooze_until  TEXT,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE,
  FOREIGN KEY (customer_id) REFERENCES customers(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_on);
CREATE INDEX IF NOT EXISTS idx_reminders_shop ON reminders(shop_id, status, due_on);
CREATE INDEX IF NOT EXISTS idx_reminders_cust ON reminders(customer_id, created_at);

CREATE TABLE IF NOT EXISTS messages (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id        INTEGER NOT NULL,
  customer_id    INTEGER,
  reminder_id    INTEGER,
  campaign_id    INTEGER,
  direction      TEXT DEFAULT 'out',            -- out | in
  channel        TEXT DEFAULT 'whatsapp',       -- whatsapp | sms | email
  provider       TEXT DEFAULT 'sandbox',        -- sandbox | meta | twilio | smtp | gmail
  to_addr        TEXT DEFAULT '',
  from_addr      TEXT DEFAULT '',
  subject        TEXT DEFAULT '',
  body           TEXT NOT NULL,
  template_key   TEXT DEFAULT '',
  status         TEXT DEFAULT 'queued',         -- queued | sent | delivered | read | replied | failed
  provider_msg_id TEXT DEFAULT '',
  error          TEXT DEFAULT '',
  cost           REAL DEFAULT 0,
  intent         TEXT DEFAULT '',               -- classified inbound intent
  created_at     TEXT NOT NULL,
  sent_at        TEXT,
  delivered_at   TEXT,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(status, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_shop ON messages(shop_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_customer ON messages(customer_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, status);

CREATE TABLE IF NOT EXISTS templates (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER,                           -- NULL = platform default
  key        TEXT NOT NULL,
  channel    TEXT DEFAULT 'whatsapp',
  language   TEXT DEFAULT 'en',
  subject    TEXT DEFAULT '',
  body       TEXT NOT NULL,
  active     INTEGER DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_templates ON templates(shop_id, key, channel);

CREATE TABLE IF NOT EXISTS coupons (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id       INTEGER NOT NULL,
  code          TEXT NOT NULL,
  description   TEXT DEFAULT '',
  discount_type TEXT DEFAULT 'percent',         -- percent | amount | free_item
  discount_value REAL DEFAULT 0,
  min_spend     REAL DEFAULT 0,
  expires_on    TEXT,
  max_uses      INTEGER DEFAULT 1,
  used_count    INTEGER DEFAULT 0,
  customer_id   INTEGER,
  reminder_id   INTEGER,
  status        TEXT DEFAULT 'active',
  created_at    TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon ON coupons(shop_id, code);

CREATE TABLE IF NOT EXISTS campaigns (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL,
  name        TEXT NOT NULL,
  segment     TEXT DEFAULT '{}',                -- json filter
  template_key TEXT DEFAULT 'broadcast',
  channel     TEXT DEFAULT 'whatsapp',
  status      TEXT DEFAULT 'draft',             -- draft | scheduled | running | done
  scheduled_at TEXT,
  coupon_id   INTEGER,
  sent_count  INTEGER DEFAULT 0,
  replied_count INTEGER DEFAULT 0,
  converted_count INTEGER DEFAULT 0,
  revenue     REAL DEFAULT 0,
  created_at  TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS receipts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id      INTEGER NOT NULL,
  filename     TEXT DEFAULT '',
  stored_path  TEXT DEFAULT '',
  mime         TEXT DEFAULT '',
  size         INTEGER DEFAULT 0,
  raw_text     TEXT DEFAULT '',                 -- pasted / OCR text
  parsed_json  TEXT DEFAULT '[]',
  parse_status TEXT DEFAULT 'needs_review',      -- needs_review | imported | failed
  sale_id      INTEGER,
  created_at   TEXT NOT NULL,
  FOREIGN KEY (shop_id) REFERENCES shops(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS import_jobs (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id    INTEGER NOT NULL,
  kind       TEXT DEFAULT 'csv',                 -- csv | receipt | api
  filename   TEXT DEFAULT '',
  rows_total INTEGER DEFAULT 0,
  rows_ok    INTEGER DEFAULT 0,
  rows_failed INTEGER DEFAULT 0,
  status     TEXT DEFAULT 'done',
  report     TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS loyalty_events (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  shop_id     INTEGER NOT NULL,
  customer_id INTEGER NOT NULL,
  points      INTEGER NOT NULL,
  reason      TEXT DEFAULT '',
  ref         TEXT DEFAULT '',
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_loyalty ON loyalty_events(shop_id, customer_id);

CREATE TABLE IF NOT EXISTS credentials (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  scope      TEXT DEFAULT 'platform',            -- platform | shop
  shop_id    INTEGER,
  provider   TEXT NOT NULL,                      -- whatsapp | sms | email
  driver     TEXT NOT NULL,                      -- sandbox | meta | twilio | smtp
  enabled    INTEGER DEFAULT 0,
  config     TEXT DEFAULT '{}',                  -- json: tokens, ids, sender, host...
  updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_cred ON credentials(scope, shop_id, provider);

CREATE TABLE IF NOT EXISTS audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  actor      TEXT DEFAULT 'system',
  shop_id    INTEGER,
  action     TEXT NOT NULL,
  entity     TEXT DEFAULT '',
  entity_id  TEXT DEFAULT '',
  detail     TEXT DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit ON audit_log(created_at);

CREATE TABLE IF NOT EXISTS webhook_events (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  provider     TEXT DEFAULT '',
  event_type   TEXT DEFAULT '',
  payload      TEXT DEFAULT '{}',
  message_id   INTEGER,
  processed    INTEGER DEFAULT 0,
  created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS platform_meta (
  k TEXT PRIMARY KEY,
  v TEXT
);
"""


def utcnow() -> datetime:
    return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=TZ_OFFSET_HOURS))).replace(tzinfo=None)


def now_iso() -> str:
    return utcnow().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return utcnow().strftime("%Y-%m-%d")


def days_from_today(n: int) -> str:
    return (utcnow() + timedelta(days=n)).strftime("%Y-%m-%d")


def at_time(date_str: str, hhmm: str) -> str:
    return f"{date_str} {hhmm}:00"


# ---------------------------------------------------------------- backends
_PG_LOCK = threading.Lock()


class _PgRow(dict):
    """dict that also supports integer indexing, like sqlite3.Row."""

    def __getitem__(self, k):
        if isinstance(k, int):
            return list(self.values())[k]
        return super().__getitem__(k)


def _top_level(expr: str) -> str:
    """Strip scalar subqueries from an expression, keeping everything else.

    `SUM(status IN ('a','b'))` must still look like a boolean, while
    `AVG(x / (SELECT COUNT(*) ...))` is plain arithmetic and must not."""
    out, i, n, quote = [], 0, len(expr), None
    while i < n:
        ch = expr[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "(" and re.match(r"\s*(SELECT|WITH)\b", expr[i + 1:], re.IGNORECASE):
            depth, j, q2 = 1, i + 1, None
            while j < n and depth:
                c = expr[j]
                if q2:
                    if c == q2:
                        q2 = None
                elif c in ("'", '"'):
                    q2 = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            out.append(" __SUBQUERY__ ")
            i = j
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _bool_aggregates(sql: str) -> str:
    """SQLite sums booleans as 0/1; Postgres needs an explicit CASE. Wraps the
    argument of SUM()/AVG() when it evaluates to a boolean."""
    out, i, n, quote = [], 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        m = re.match(r"(SUM|AVG)\(", sql[i:], re.IGNORECASE)
        if m and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            fn = m.group(1)
            start = i + len(fn) + 1
            depth, j, in_q = 1, start, None
            while j < n and depth:
                c = sql[j]
                if in_q:
                    if c == in_q:
                        in_q = None
                elif c in ("'", '"'):
                    in_q = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                j += 1
            inner = sql[start:j - 1]
            upper = inner.upper()
            # already a CASE expression? leave it alone.
            already_case = bool(re.search(r"\bCASE\b.*\bTHEN\b", upper, re.DOTALL))
            if not already_case:
                # only comparisons at the argument's own nesting level count;
                # inside a subquery they are filters, not the aggregate's value
                top = _top_level(inner)
                has_cmp = (re.search(r"(?<![<>!=])=(?!=)|>=|<=|<>|(?<![<>!=])>(?!=)|(?<![<>!])<(?![=>])", top)
                           or re.search(r"\bIN\s*\(", top, re.IGNORECASE)
                           or re.search(r"\bIS\s+(NOT\s+)?NULL\b", top, re.IGNORECASE))
                if has_cmp:
                    out.append(f"SUM(CASE WHEN {inner} THEN 1 ELSE 0 END)" if fn.upper() == "SUM"
                               else f"AVG(CASE WHEN {inner} THEN 1 ELSE 0 END)")
                    i = j
                    continue
        out.append(ch)
        i += 1
    return "".join(out)


_PG_RESERVED_ALIASES = {"DAY", "MONTH", "YEAR", "WEEK", "VALUE", "USER", "ORDER", "GROUP", "ALL",
                        "CHECK", "COLUMN", "TABLE", "END", "POSITION", "SESSION_USER", "CURRENT_DATE"}
_ALIAS_STOP = {"FROM", "WHERE", "GROUP", "ORDER", "LIMIT", "OFFSET", "HAVING", "UNION", "ON", "JOIN",
               "LEFT", "RIGHT", "INNER", "OUTER", "CROSS", "USING", "AND", "OR", "AS", "ASC", "DESC"}


def _implicit_aliases(sql: str) -> str:
    """`SELECT COUNT(*) n` is valid SQLite but not Postgres. Insert AS before
    bare aliases, only inside the top-level select list."""
    m = re.match(r"(.*?\bSELECT\s+(?:DISTINCT\s+)?)", sql, re.IGNORECASE | re.DOTALL)
    if not m:
        return sql
    head = m.group(1)
    rest = sql[m.end():]

    # find where the select list ends (first top-level clause keyword)
    depth, quote, cut = 0, None, len(rest)
    for i, ch in enumerate(rest):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            continue
        if ch == "(":
            depth += 1
            continue
        if ch == ")":
            depth -= 1
            continue
        if depth == 0:
            tail = rest[i:].upper()
            hit = next((kw for kw in (" FROM ", " WHERE ", " GROUP ", " ORDER ", " LIMIT ", " OFFSET ",
                                      " HAVING ", " UNION ") if tail.startswith(kw)), None)
            if hit:
                cut = i
                break
    items, tail_sql = rest[:cut], rest[cut:]

    parts, depth, cur, quote = [], 0, "", None
    for ch in items:
        if quote:
            cur += ch
            if ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            cur += ch
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
            continue
        cur += ch
    parts.append(cur)

    fixed = []
    for part in parts:
        lead = part[:len(part) - len(part.lstrip())]
        body = part.strip()
        am = re.search(r"\s+([A-Za-z_][A-Za-z0-9_]*)$", body)
        if am and not re.search(r"\bAS\s+[A-Za-z_][A-Za-z0-9_]*$", body, re.IGNORECASE):
            before, word = body[:am.start()].rstrip(), am.group(1).upper()
            # `SELECT sale_date day` is valid SQLite but Postgres reserves DAY.
            reserved_alias = word in _PG_RESERVED_ALIASES
            if before and reserved_alias:
                body = f"{before} AS {am.group(1)}"
                body = f"{before} AS {am.group(1)}"
        fixed.append(lead + body)
    return head + ", ".join(fixed) + tail_sql


def _two_arg_minmax(sql: str) -> str:
    """SQLite spells scalar min/max as MIN(a,b)/MAX(a,b); Postgres needs
    LEAST/GREATEST. Rewrites only calls that actually contain a top-level comma,
    leaving aggregate MIN(x)/MAX(x) untouched. String literals are skipped."""
    out = []
    i, n, quote = 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if sql[i:i + 4].upper() in ("MAX(", "MIN(") and (i == 0 or not (sql[i - 1].isalnum() or sql[i - 1] == "_")):
            fn = sql[i:i + 3].upper()
            depth, j, has_comma, inner_quote = 0, i + 3, False, None
            while j < n:
                c = sql[j]
                if inner_quote:
                    if c == inner_quote:
                        inner_quote = None
                elif c in ("'", '"'):
                    inner_quote = c
                elif c == "(":
                    depth += 1
                elif c == ")":
                    depth -= 1
                    if depth == 0:
                        break
                elif c == "," and depth == 1:
                    has_comma = True
                j += 1
            if j < n and has_comma:
                out.append("GREATEST(" if fn == "MAX" else "LEAST(")
                i += 4
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _pg_translate(sql: str) -> str:
    """SQLite-flavoured SQL -> PostgreSQL. Only touches spots outside quotes."""
    out, i, n, quote = [], 0, len(sql), None
    while i < n:
        ch = sql[i]
        if quote:
            out.append(ch)
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "?":
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    sql = "".join(out)

    sql = sql.replace("IFNULL(", "COALESCE(")
    sql = re.sub(r"\bgroup_concat\s*\(", "string_agg(", sql, flags=re.IGNORECASE)
    # SQLite allows `string_agg`-style 2-arg form with a literal separator; Postgres
    # needs the aggregate form string_agg(x, ', ' ORDER BY ...) which we already match.
    sql = _two_arg_minmax(sql)
    sql = _bool_aggregates(sql)
    sql = _implicit_aliases(sql)
    # julianday(x) is provided as a Postgres function (schema_pg.py), so the
    # expression is left untouched and evaluates identically in both engines.
    sql = sql.replace("INSERT OR REPLACE INTO", "INSERT INTO")
    sql = sql.replace("INSERT OR IGNORE INTO", "INSERT INTO")
    # SQLite date maths -> Postgres equivalents (platform timezone)
    sql = sql.replace("date('now','-30 day')", "TO_CHAR((NOW() AT TIME ZONE 'Africa/Kampala')::date - 30, 'YYYY-MM-DD')")
    sql = sql.replace("date('now')", "TO_CHAR((NOW() AT TIME ZONE 'Africa/Kampala')::date, 'YYYY-MM-DD')")
    # SQLite date arithmetic (date('now','-30 day'), dayn(id, n)) -> Postgres
    sql = re.sub(r"date\('now'\s*,\s*'([+-])\s*(\d+)\s+day'\)",
                 r"TO_CHAR((NOW() AT TIME ZONE 'Africa/Kampala')::date \1 \2, 'YYYY-MM-DD')", sql)
    # dayn(id, n) is installed as a Postgres function (see schema_pg.py), so it
    # can be left as-is: date_n_days_from_today(id + n).
    return sql


def _pg_connect():
    import psycopg2
    import psycopg2.extras

    conn = psycopg2.connect(DATABASE_URL, connect_timeout=15, sslmode=os.environ.get("REBUY_PG_SSLMODE", "require"),
                            cursor_factory=psycopg2.extras.RealDictCursor)
    conn.autocommit = True
    return conn


def _pg():
    conn = getattr(_local, "pg", None)
    if conn is None or getattr(conn, "closed", 1):
        try:
            conn = _pg_connect()
        except Exception:
            conn = _pg_connect()  # one retry: pooled connections get recycled
        conn.autocommit = True
        _local.pg = conn
    return conn


def _pg_reset():
    conn = getattr(_local, "pg", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _local.pg = None


def _tz_today():
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(hours=3)).date()


def _julian(ts) -> float:
    """SQLite-compatible julianday(): days since the Julian epoch, or NULL."""
    from datetime import datetime, timedelta, timezone

    if ts in (None, "") or str(ts).strip().lower() in ("now", "current_timestamp"):
        dt = datetime.now(timezone.utc)
    else:
        txt = str(ts).strip().replace("T", " ")
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(txt[:19], fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return 0.0
    return (dt - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds() / 86400.0 + 2440587.5


def connect():
    if IS_PG:
        return _pg()
    conn = getattr(_local, "conn", None)
    if conn is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 8000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        # Compat functions that exist natively in Postgres (schema_pg.py). Keeping
        # the same SQL running on both engines is why these live here too.
        conn.create_function(
            "dayn", 2,
            lambda _base_id, days: (_tz_today() + timedelta(days=int(days or 0))).strftime("%Y-%m-%d"),
            deterministic=True,
        )
        conn.create_function(
            "julianday", 1,
            lambda ts: _julian(ts), deterministic=True,
        )
        conn.create_function("julianday", 0, lambda: _julian(None), deterministic=True)
        _local.conn = conn
    return conn


def init_schema() -> None:
    if IS_PG:
        from . import schema_pg

        conn = _pg()
        with conn.cursor() as cur:
            for stmt in schema_pg.STATEMENTS:
                cur.execute(stmt)
        _migrate_postgres()
        return
    conn = connect()
    conn.executescript(SCHEMA)
    conn.commit()
    _migrate_sqlite()


# Columns added after the first release. A database created by an older build
# will have the tables but not these columns; `CREATE TABLE IF NOT EXISTS` is a
# no-op in that case, so they are added explicitly here. Keep this list additive
# and idempotent — it runs on every boot.
_ADDED_COLUMNS = {
    "users": [
        ("google_sub", "TEXT DEFAULT ''"),
        ("email_verified", "INTEGER DEFAULT 0"),
    ],
    "sessions": [
        ("ip", "TEXT DEFAULT ''"),
        ("user_agent", "TEXT DEFAULT ''"),
        ("method", "TEXT DEFAULT 'password'"),
    ],
}

# (table, index name, column) — created after the columns exist
_ADDED_INDEXES = [
    ("users", "uq_users_google", "google_sub", "google_sub <> ''"),
]


def _migrate_sqlite() -> None:
    conn = connect()
    for table, cols in _ADDED_COLUMNS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in cols:
            if name not in existing:
                conn.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {decl}')
                conn.commit()
    for table, index, column, predicate in _ADDED_INDEXES:
        try:
            conn.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {index} ON "{table}"({column}) WHERE {predicate}')
            conn.commit()
        except sqlite3.OperationalError:
            pass  # column still absent on a very old database


def _migrate_postgres() -> None:
    conn = _pg()
    with conn.cursor() as cur:
        for table, cols in _ADDED_COLUMNS.items():
            for name, decl in cols:
                cur.execute(f'ALTER TABLE "{table}" ADD COLUMN IF NOT EXISTS "{name}" {decl}')
        for table, index, column, predicate in _ADDED_INDEXES:
            try:
                cur.execute(f'CREATE UNIQUE INDEX IF NOT EXISTS {index} ON "{table}"({column}) WHERE {predicate}')
            except Exception:  # noqa: BLE001
                pass


def fix_sequences() -> None:
    """Explicit ids inserted during seeding do not advance BIGSERIAL counters."""
    if not IS_PG:
        return
    from . import schema_pg

    with _pg().cursor() as cur:
        cur.execute(schema_pg.SEQUENCE_FIX)


def backend() -> str:
    return "postgres" if IS_PG else "sqlite"


def size_bytes() -> int:
    """Approximate on-disk size of the database (used by admin capacity)."""
    if IS_PG:
        try:
            return int(scalar("SELECT pg_database_size(current_database())", (), 0) or 0)
        except Exception:  # noqa: BLE001
            return 0
    return os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0


# ---------------------------------------------------------------- query helpers
def _exec(sql: str, params):
    """Returns (cursor_rows, lastrowid) for either engine."""
    if IS_PG:
        conn = _pg()
        try:
            with conn.cursor() as cur:
                translated = _pg_translate(sql)
                if params:
                    cur.execute(translated, tuple(params))
                else:
                    cur.execute(translated)
                rows = cur.fetchall() if cur.description else []
                rid = 0
                if cur.description is None:
                    try:
                        cur.execute("SELECT LASTVAL()")
                        rid = int(cur.fetchone()["lastval"])
                    except Exception:  # noqa: BLE001
                        rid = 0
                return rows, rid
        except Exception:
            _pg_reset()
            raise
    conn = connect()
    cur = conn.execute(sql, tuple(params or ()))
    rows = cur.fetchall()
    rid = cur.lastrowid
    cur.close()
    return rows, rid


def q(sql: str, params: tuple | list = ()) -> list[dict]:
    rows, _ = _exec(sql, params)
    return [_PgRow(r) if IS_PG else dict(r) for r in rows]


def q1(sql: str, params: tuple | list = ()) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def scalar(sql: str, params: tuple | list = (), default=None):
    rows, _ = _exec(sql, params)
    if not rows:
        return default
    row = rows[0]
    if isinstance(row, (int, float, str)):
        val = row
    elif hasattr(row, "values"):          # dict / psycopg2 RealDictRow
        val = list(row.values())[0]
    else:                                  # sqlite3.Row supports index access only
        val = row[0]
    if val is None:
        return default
    return val


def x(sql: str, params: tuple | list = ()) -> int:
    """Execute a write and commit; returns the new row id where one exists.

    SQLite is opened in Python's default deferred-transaction mode, so a write
    that is not committed is rolled back when the connection closes — and with
    a per-thread connection that means almost immediately. Postgres runs with
    autocommit on, so only the SQLite path needs the explicit commit.
    """
    _, rid = _exec(sql, params)
    if not IS_PG:
        connect().commit()
    return rid or 0


def xm(sql: str, seq: list[tuple | list]) -> None:
    seq = list(seq)
    if not seq:
        return
    if IS_PG:
        from psycopg2.extras import execute_values

        conn = _pg()
        translated = _pg_translate(sql)
        # one multi-row INSERT per page instead of one round trip per row
        sql_multi = re.sub(r"\)\s*VALUES\s*\([^)]*\)\s*;?\s*$", ") VALUES %s", translated.rstrip().rstrip(";"),
                           flags=re.IGNORECASE | re.DOTALL)
        try:
            with conn.cursor() as cur:
                execute_values(cur, sql_multi, [tuple(r) for r in seq], page_size=500)
        except Exception:
            _pg_reset()
            raise
        return
    conn = connect()
    conn.executemany(sql, seq)
    conn.commit()


def insert(table: str, values: dict) -> int:
    cols = ", ".join(values.keys())
    ph = ", ".join("?" for _ in values)
    return x(f"INSERT INTO {table} ({cols}) VALUES ({ph})", list(values.values()))


def update(table: str, pk: int, values: dict) -> None:
    if not values:
        return
    sets = ", ".join(f"{k} = ?" for k in values)
    x(f"UPDATE {table} SET {sets} WHERE id = ?", list(values.values()) + [pk])


def audit(actor: str, action: str, shop_id=None, entity: str = "", entity_id="", detail: str = "") -> None:
    insert("audit_log", {
        "actor": actor, "shop_id": shop_id, "action": action, "entity": entity,
        "entity_id": str(entity_id), "detail": detail[:800], "created_at": now_iso(),
    })


def meta_get(k: str, default=None):
    return scalar("SELECT v FROM platform_meta WHERE k = ?", (k,), default)


def meta_set(k: str, v: str) -> None:
    x("INSERT INTO platform_meta (k, v) VALUES (?, ?) "
      "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))


def reset_demo_data(seed_shops: int | None = None) -> dict:
    """Truncate every table and reseed. Used by scripts/load_supabase.py and
    the admin 'reset demo' path on Postgres."""
    tables = [t for t in q(
        "SELECT table_name AS name FROM information_schema.tables "
        "WHERE table_schema='public' AND table_type='BASE TABLE' ORDER BY table_name")] if IS_PG else \
             q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
    names = [t["name"] for t in tables]
    if IS_PG:
        with _pg().cursor() as cur:
            cur.execute("TRUNCATE " + ", ".join(f'"{n}"' for n in names) + " RESTART IDENTITY CASCADE")
    else:
        conn = connect()
        for n in names:
            conn.execute(f'DELETE FROM "{n}"')
        conn.commit()
    from . import seed as seed_mod
    return seed_mod.ensure_bootstrapped(force=True)
