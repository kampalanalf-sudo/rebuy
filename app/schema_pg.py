"""
Rebuy — PostgreSQL schema (Supabase).

Mirrors app/db.py's SQLite schema 1:1, with:
  * BIGSERIAL ids (so explicit seed ids and future inserts coexist safely)
  * booleans kept as smallint with a 0/1 CHECK (same values the app already writes)
  * `?` placeholders never appear here; db.py translates them to %s
  * timestamps stay TEXT in 'YYYY-MM-DD HH:MM:SS' (Africa/Kampala) — the app
    compares them lexicographically and does date maths in Python.
"""
from __future__ import annotations

_TS = "TEXT"
_BOOL = "SMALLINT DEFAULT 0 CHECK (%s IN (0,1))"

STATEMENTS = [
    """CREATE TABLE IF NOT EXISTS shops (
  id              BIGSERIAL PRIMARY KEY,
  name            TEXT NOT NULL,
  slug            TEXT NOT NULL UNIQUE,
  owner_name      TEXT DEFAULT '',
  owner_email     TEXT DEFAULT '',
  phone           TEXT DEFAULT '',
  city            TEXT DEFAULT '',
  country         TEXT DEFAULT 'Uganda',
  sector          TEXT DEFAULT 'Retail',
  plan            TEXT DEFAULT 'starter',
  status          TEXT DEFAULT 'trial',
  monthly_price   DOUBLE PRECISION DEFAULT 0,
  currency        TEXT DEFAULT 'UGX',
  language        TEXT DEFAULT 'en',
  timezone        TEXT DEFAULT 'Africa/Kampala',
  sender_name     TEXT DEFAULT '',
  whatsapp_number TEXT DEFAULT '',
  auto_reminders  SMALLINT DEFAULT 1 CHECK (auto_reminders IN (0,1)),
  reminder_lead_days INTEGER DEFAULT 3,
  max_followups   INTEGER DEFAULT 2,
  max_msgs_per_customer_month INTEGER DEFAULT 6,
  quiet_start     TEXT DEFAULT '21:00',
  quiet_end       TEXT DEFAULT '07:00',
  loyalty_enabled SMALLINT DEFAULT 1 CHECK (loyalty_enabled IN (0,1)),
  loyalty_rule    TEXT DEFAULT 'buy_5_get_1',
  onboarded_at    TEXT,
  trial_ends_at   TEXT,
  created_at      TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_shops_status ON shops(status)",
    "CREATE INDEX IF NOT EXISTS idx_shops_created ON shops(created_at)",

    """CREATE TABLE IF NOT EXISTS users (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       BIGINT REFERENCES shops(id) ON DELETE CASCADE,
  role          TEXT NOT NULL DEFAULT 'owner',
  name          TEXT NOT NULL,
  email         TEXT NOT NULL UNIQUE,
  password_hash TEXT DEFAULT '',
  salt          TEXT DEFAULT '',
  google_sub    TEXT DEFAULT '',
  email_verified SMALLINT DEFAULT 0 CHECK (email_verified IN (0,1)),
  last_login    TEXT,
  created_at    TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_google ON users(google_sub) WHERE google_sub <> ''",
    "CREATE INDEX IF NOT EXISTS idx_users_shop ON users(shop_id)",

    """CREATE TABLE IF NOT EXISTS sessions (
  token_hash TEXT PRIMARY KEY,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  ip         TEXT DEFAULT '',
  user_agent TEXT DEFAULT '',
  method     TEXT DEFAULT 'password'
)""",
    "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)",

    """CREATE TABLE IF NOT EXISTS customers (
  id             BIGSERIAL PRIMARY KEY,
  shop_id        BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  name           TEXT NOT NULL,
  phone          TEXT DEFAULT '',
  whatsapp       TEXT DEFAULT '',
  email          TEXT DEFAULT '',
  area           TEXT DEFAULT '',
  tags           TEXT DEFAULT '',
  notes          TEXT DEFAULT '',
  language       TEXT DEFAULT 'en',
  opted_in       SMALLINT DEFAULT 1 CHECK (opted_in IN (0,1)),
  opt_out_at     TEXT,
  consent_source TEXT DEFAULT 'sale',
  birthday       TEXT DEFAULT '',
  first_seen     TEXT,
  last_purchase_at TEXT,
  total_spent    DOUBLE PRECISION DEFAULT 0,
  visit_count    INTEGER DEFAULT 0,
  loyalty_points INTEGER DEFAULT 0,
  lifetime_value DOUBLE PRECISION DEFAULT 0,
  risk_band      TEXT DEFAULT 'new',
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_phone ON customers(shop_id, phone) WHERE phone <> ''",
    "CREATE INDEX IF NOT EXISTS idx_customers_shop ON customers(shop_id, last_purchase_at)",
    "CREATE INDEX IF NOT EXISTS idx_customers_risk ON customers(shop_id, risk_band)",
    "CREATE INDEX IF NOT EXISTS idx_customers_name ON customers(shop_id, name)",

    """CREATE TABLE IF NOT EXISTS products (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  name          TEXT NOT NULL,
  sku           TEXT DEFAULT '',
  category      TEXT DEFAULT 'General',
  kind          TEXT DEFAULT 'goods',
  unit          TEXT DEFAULT 'pc',
  unit_price    DOUBLE PRECISION DEFAULT 0,
  cycle_days    INTEGER DEFAULT 30,
  baseline_qty  DOUBLE PRECISION DEFAULT 1,
  active        SMALLINT DEFAULT 1 CHECK (active IN (0,1)),
  created_at    TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_product ON products(shop_id, name, unit)",
    "CREATE INDEX IF NOT EXISTS idx_products_shop ON products(shop_id, active)",

    """CREATE TABLE IF NOT EXISTS sales (
  id             BIGSERIAL PRIMARY KEY,
  shop_id        BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  receipt_no     TEXT DEFAULT '',
  customer_id    BIGINT REFERENCES customers(id) ON DELETE SET NULL,
  sale_date      TEXT NOT NULL,
  sale_time      TEXT DEFAULT '',
  total          DOUBLE PRECISION DEFAULT 0,
  currency       TEXT DEFAULT 'UGX',
  payment_method TEXT DEFAULT 'cash',
  channel        TEXT DEFAULT 'in_store',
  staff_name     TEXT DEFAULT '',
  source         TEXT DEFAULT 'manual',
  notes          TEXT DEFAULT '',
  created_at     TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_sales_shop_date ON sales(shop_id, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_sales_customer ON sales(customer_id, sale_date)",
    "CREATE INDEX IF NOT EXISTS idx_sales_source ON sales(shop_id, source)",

    """CREATE TABLE IF NOT EXISTS sale_items (
  id            BIGSERIAL PRIMARY KEY,
  sale_id       BIGINT NOT NULL REFERENCES sales(id) ON DELETE CASCADE,
  shop_id       BIGINT NOT NULL,
  product_id    BIGINT,
  product_name  TEXT NOT NULL,
  category      TEXT DEFAULT 'General',
  kind          TEXT DEFAULT 'goods',
  qty           DOUBLE PRECISION NOT NULL DEFAULT 1,
  unit          TEXT DEFAULT 'pc',
  unit_price    DOUBLE PRECISION DEFAULT 0,
  line_total    DOUBLE PRECISION DEFAULT 0,
  cycle_days    INTEGER DEFAULT 30,
  depletes_on   TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_items_sale ON sale_items(sale_id)",
    "CREATE INDEX IF NOT EXISTS idx_items_deplete ON sale_items(shop_id, depletes_on)",

    """CREATE TABLE IF NOT EXISTS reminders (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  customer_id   BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  sale_item_id  BIGINT,
  product_name  TEXT DEFAULT '',
  kind          TEXT DEFAULT 'replenish',
  attempt       INTEGER DEFAULT 1,
  due_on        TEXT NOT NULL,
  status        TEXT DEFAULT 'scheduled',
  channel       TEXT DEFAULT 'whatsapp',
  coupon_id     BIGINT,
  message_id    BIGINT,
  sent_at       TEXT,
  replied_at    TEXT,
  converted_at  TEXT,
  revenue       DOUBLE PRECISION DEFAULT 0,
  snooze_until  TEXT,
  created_at    TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_on)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_shop ON reminders(shop_id, status, due_on)",
    "CREATE INDEX IF NOT EXISTS idx_reminders_cust ON reminders(customer_id, created_at)",

    """CREATE TABLE IF NOT EXISTS messages (
  id             BIGSERIAL PRIMARY KEY,
  shop_id        BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  customer_id    BIGINT,
  reminder_id    BIGINT,
  campaign_id    BIGINT,
  direction      TEXT DEFAULT 'out',
  channel        TEXT DEFAULT 'whatsapp',
  provider       TEXT DEFAULT 'sandbox',
  to_addr        TEXT DEFAULT '',
  from_addr      TEXT DEFAULT '',
  subject        TEXT DEFAULT '',
  body           TEXT NOT NULL,
  template_key   TEXT DEFAULT '',
  status         TEXT DEFAULT 'queued',
  provider_msg_id TEXT DEFAULT '',
  error          TEXT DEFAULT '',
  cost           DOUBLE PRECISION DEFAULT 0,
  intent         TEXT DEFAULT '',
  created_at     TEXT NOT NULL,
  sent_at        TEXT,
  delivered_at   TEXT
)""",
    "CREATE INDEX IF NOT EXISTS idx_messages_queue ON messages(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_shop ON messages(shop_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_customer ON messages(customer_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, status)",
    "CREATE INDEX IF NOT EXISTS idx_messages_reminder ON messages(reminder_id)",

    """CREATE TABLE IF NOT EXISTS templates (
  id         BIGSERIAL PRIMARY KEY,
  shop_id    BIGINT,
  key        TEXT NOT NULL,
  channel    TEXT DEFAULT 'whatsapp',
  language   TEXT DEFAULT 'en',
  subject    TEXT DEFAULT '',
  body       TEXT NOT NULL,
  active     SMALLINT DEFAULT 1 CHECK (active IN (0,1)),
  created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_templates ON templates(shop_id, key, channel)",

    """CREATE TABLE IF NOT EXISTS coupons (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  code          TEXT NOT NULL,
  description   TEXT DEFAULT '',
  discount_type TEXT DEFAULT 'percent',
  discount_value DOUBLE PRECISION DEFAULT 0,
  min_spend     DOUBLE PRECISION DEFAULT 0,
  expires_on    TEXT,
  max_uses      INTEGER DEFAULT 1,
  used_count    INTEGER DEFAULT 0,
  customer_id   BIGINT,
  reminder_id   BIGINT,
  status        TEXT DEFAULT 'active',
  created_at    TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_coupon ON coupons(shop_id, code)",

    """CREATE TABLE IF NOT EXISTS campaigns (
  id          BIGSERIAL PRIMARY KEY,
  shop_id     BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  name        TEXT NOT NULL,
  segment     TEXT DEFAULT '{}',
  template_key TEXT DEFAULT 'broadcast',
  channel     TEXT DEFAULT 'whatsapp',
  status      TEXT DEFAULT 'draft',
  scheduled_at TEXT,
  coupon_id   BIGINT,
  sent_count  INTEGER DEFAULT 0,
  replied_count INTEGER DEFAULT 0,
  converted_count INTEGER DEFAULT 0,
  revenue     DOUBLE PRECISION DEFAULT 0,
  created_at  TEXT NOT NULL
)""",

    """CREATE TABLE IF NOT EXISTS receipts (
  id           BIGSERIAL PRIMARY KEY,
  shop_id      BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  filename     TEXT DEFAULT '',
  stored_path  TEXT DEFAULT '',
  mime         TEXT DEFAULT '',
  size         INTEGER DEFAULT 0,
  raw_text     TEXT DEFAULT '',
  parsed_json  TEXT DEFAULT '[]',
  parse_status TEXT DEFAULT 'needs_review',
  sale_id      BIGINT,
  created_at   TEXT NOT NULL
)""",

    """CREATE TABLE IF NOT EXISTS import_jobs (
  id         BIGSERIAL PRIMARY KEY,
  shop_id    BIGINT NOT NULL,
  kind       TEXT DEFAULT 'csv',
  filename   TEXT DEFAULT '',
  rows_total INTEGER DEFAULT 0,
  rows_ok    INTEGER DEFAULT 0,
  rows_failed INTEGER DEFAULT 0,
  status     TEXT DEFAULT 'done',
  report     TEXT DEFAULT '[]',
  created_at TEXT NOT NULL
)""",

    """CREATE TABLE IF NOT EXISTS loyalty_events (
  id          BIGSERIAL PRIMARY KEY,
  shop_id     BIGINT NOT NULL,
  customer_id BIGINT NOT NULL,
  points      INTEGER NOT NULL,
  reason      TEXT DEFAULT '',
  ref         TEXT DEFAULT '',
  created_at  TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_loyalty ON loyalty_events(shop_id, customer_id)",

    """CREATE TABLE IF NOT EXISTS credentials (
  id         BIGSERIAL PRIMARY KEY,
  scope      TEXT DEFAULT 'platform',
  shop_id    BIGINT,
  provider   TEXT NOT NULL,
  driver     TEXT NOT NULL,
  enabled    SMALLINT DEFAULT 0 CHECK (enabled IN (0,1)),
  config     TEXT DEFAULT '{}',
  updated_at TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_cred ON credentials(scope, COALESCE(shop_id, 0), provider)",

    """CREATE TABLE IF NOT EXISTS audit_log (
  id         BIGSERIAL PRIMARY KEY,
  actor      TEXT DEFAULT 'system',
  shop_id    BIGINT,
  action     TEXT NOT NULL,
  entity     TEXT DEFAULT '',
  entity_id  TEXT DEFAULT '',
  detail     TEXT DEFAULT '',
  created_at TEXT NOT NULL
)""",
    "CREATE INDEX IF NOT EXISTS idx_audit ON audit_log(created_at)",

    """CREATE TABLE IF NOT EXISTS webhook_events (
  id           BIGSERIAL PRIMARY KEY,
  provider     TEXT DEFAULT '',
  event_type   TEXT DEFAULT '',
  payload      TEXT DEFAULT '{}',
  message_id   BIGINT,
  processed    SMALLINT DEFAULT 0 CHECK (processed IN (0,1)),
  created_at   TEXT NOT NULL
)""",

    """CREATE TABLE IF NOT EXISTS password_resets (
  token_hash TEXT PRIMARY KEY,
  user_id    BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  expires_at TEXT NOT NULL,
  used_at    TEXT,
  created_at TEXT NOT NULL
)""",

    """CREATE TABLE IF NOT EXISTS invites (
  id         BIGSERIAL PRIMARY KEY,
  shop_id    BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  email      TEXT NOT NULL,
  role       TEXT DEFAULT 'staff',
  token_hash TEXT NOT NULL,
  invited_by TEXT DEFAULT '',
  expires_at TEXT NOT NULL,
  accepted_at TEXT,
  created_at TEXT NOT NULL
)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_invite ON invites(shop_id, email)",

    """CREATE TABLE IF NOT EXISTS platform_meta (
  k TEXT PRIMARY KEY,
  v TEXT
)""",

    # SQLite-compat helpers so application SQL can stay engine-agnostic
    """CREATE OR REPLACE FUNCTION date(ts TEXT) RETURNS TEXT
  LANGUAGE sql STABLE AS $$
    SELECT SUBSTRING(COALESCE(ts, '') FROM 1 FOR 10)
  $$""",
    """CREATE OR REPLACE FUNCTION date(ts TEXT, mod TEXT) RETURNS TEXT
  LANGUAGE sql STABLE AS $$
    SELECT TO_CHAR(date(ts)::date + (regexp_replace(mod, '[^0-9-]', '', 'g'))::int, 'YYYY-MM-DD')
  $$""",
    """CREATE OR REPLACE FUNCTION substr(s TEXT, start_ INT, len INT) RETURNS TEXT
  LANGUAGE sql IMMUTABLE AS $$
    SELECT SUBSTRING(COALESCE(s, '') FROM start_ FOR len)
  $$""",
    "DROP FUNCTION IF EXISTS dayn(BIGINT, BIGINT)",
    """CREATE OR REPLACE FUNCTION dayn(base_id BIGINT, days BIGINT) RETURNS TEXT
  LANGUAGE sql STABLE AS $$
    SELECT TO_CHAR((NOW() AT TIME ZONE 'Africa/Kampala')::date + (days::int), 'YYYY-MM-DD')
  $$""",
    """CREATE OR REPLACE FUNCTION julianday(ts TEXT) RETURNS DOUBLE PRECISION
  LANGUAGE sql STABLE AS $$
    SELECT EXTRACT(EPOCH FROM (COALESCE(NULLIF(ts,''), '1970-01-01 00:00:00'))::timestamp) / 86400.0
  $$""",
    """CREATE OR REPLACE FUNCTION julianday() RETURNS DOUBLE PRECISION
  LANGUAGE sql STABLE AS $$
    SELECT EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'Africa/Kampala')::timestamp) / 86400.0
  $$""",

    # demo-facing view used by the admin cockpit's "revenue 30d" probe
    """CREATE OR REPLACE VIEW v_sales_30d AS
  SELECT shop_id, SUM(total) AS revenue, COUNT(*) AS orders
  FROM sales
  WHERE sale_date >= to_char((now() AT TIME ZONE 'Africa/Kampala')::date - 30, 'YYYY-MM-DD')
  GROUP BY shop_id""",
]

# Ids are inserted explicitly during seeding; bump every sequence past the data.
SEQUENCE_FIX = """
DO $$
DECLARE r record; maxid bigint;
BEGIN
  FOR r IN SELECT c.relname AS seq, t.relname AS tbl, a.attname AS col
           FROM pg_class c
           JOIN pg_depend d ON d.objid = c.oid AND d.deptype = 'a'
           JOIN pg_class t ON t.oid = d.refobjid
           JOIN pg_namespace n ON n.oid = t.relnamespace
           JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = d.refobjsubid
           WHERE c.relkind = 'S' AND n.nspname = 'public'
  LOOP
    EXECUTE format('SELECT COALESCE(MAX(%I),0) FROM %I', r.col, r.tbl) INTO maxid;
    EXECUTE format('SELECT setval(%L, GREATEST(%s,1))', r.seq, maxid);
  END LOOP;
END $$;
"""
