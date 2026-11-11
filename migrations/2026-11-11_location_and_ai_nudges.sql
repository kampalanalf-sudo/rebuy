-- Location + AI nudge tables.
-- Applied to production (Supabase amvkdevvjxbjnsrozobr) on 2026-11-11 as the
-- migration "add_location_and_ai_nudge_tables".
--
-- app/schema_pg.py creates these on boot for fresh databases; this file is the
-- record of the change for an existing database, where CREATE TABLE IF NOT
-- EXISTS would otherwise be the only path. Safe to re-run.
--
-- Reuses existing tables where it can: devices (app installs), consents
-- (per-purpose permissions), push_tokens (push delivery), visits (footfall),
-- messages (the queued nudge itself). Only the following are new.

CREATE TABLE IF NOT EXISTS shop_places (
  id          BIGSERIAL PRIMARY KEY,
  shop_id     BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  label       TEXT DEFAULT 'Main branch',
  address     TEXT DEFAULT '',
  lat         DOUBLE PRECISION NOT NULL,
  lng         DOUBLE PRECISION NOT NULL,
  radius_m    INTEGER DEFAULT 400,
  open_from   TEXT DEFAULT '07:00',
  open_to     TEXT DEFAULT '21:00',
  active      SMALLINT DEFAULT 1 CHECK (active IN (0,1)),
  created_at  TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS consents (
  id          BIGSERIAL PRIMARY KEY,
  customer_id BIGINT NOT NULL REFERENCES customers(id) ON DELETE CASCADE,
  shop_id     BIGINT NOT NULL,
  purpose     TEXT NOT NULL,
  granted     SMALLINT DEFAULT 0 CHECK (granted IN (0,1)),
  source      TEXT DEFAULT '',
  granted_at  TEXT,
  revoked_at  TEXT,
  updated_at  TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS devices (
  id           BIGSERIAL PRIMARY KEY,
  customer_id  BIGINT REFERENCES customers(id) ON DELETE CASCADE,
  shop_id      BIGINT,
  token_hash   TEXT NOT NULL UNIQUE,
  platform     TEXT DEFAULT 'android',
  push_token   TEXT DEFAULT '',
  app_version  TEXT DEFAULT '',
  lat          DOUBLE PRECISION,
  lng          DOUBLE PRECISION,
  accuracy_m   DOUBLE PRECISION,
  last_seen_at TEXT,
  created_at   TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS visits (
  id          BIGSERIAL PRIMARY KEY,
  shop_id     BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  place_id    BIGINT,
  customer_id BIGINT,
  device_id   BIGINT,
  event       TEXT DEFAULT 'enter',
  lat         DOUBLE PRECISION,
  lng         DOUBLE PRECISION,
  distance_m  DOUBLE PRECISION,
  dwell_min   INTEGER DEFAULT 0,
  sale_id     BIGINT,
  created_at  TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS nudge_decisions (
  id            BIGSERIAL PRIMARY KEY,
  shop_id       BIGINT NOT NULL REFERENCES shops(id) ON DELETE CASCADE,
  customer_id   BIGINT,
  reminder_id   BIGINT,
  trigger_kind  TEXT DEFAULT 'time',
  product_name  TEXT DEFAULT '',
  score         REAL DEFAULT 0,
  expected_value REAL DEFAULT 0,
  send          SMALLINT DEFAULT 0 CHECK (send IN (0,1)),
  reason        TEXT DEFAULT '',
  features      TEXT DEFAULT '{}',
  discount_pct  REAL DEFAULT 0,
  channel       TEXT DEFAULT '',
  send_at       TEXT DEFAULT '',
  outcome       TEXT DEFAULT '',
  revenue       REAL DEFAULT 0,
  created_at    TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS nudge_sends (
  id          BIGSERIAL PRIMARY KEY,
  shop_id     BIGINT NOT NULL,
  customer_id BIGINT NOT NULL,
  kind        TEXT DEFAULT 'proximity',
  created_at  TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS nudge_settings (
  id                  BIGSERIAL PRIMARY KEY,
  shop_id             BIGINT NOT NULL UNIQUE REFERENCES shops(id) ON DELETE CASCADE,
  enabled             SMALLINT DEFAULT 1 CHECK (enabled IN (0,1)),
  proximity_enabled   SMALLINT DEFAULT 1 CHECK (proximity_enabled IN (0,1)),
  ai_scoring          SMALLINT DEFAULT 1 CHECK (ai_scoring IN (0,1)),
  min_score           REAL DEFAULT 45,
  max_per_week        INTEGER DEFAULT 2,
  quiet_minutes       INTEGER DEFAULT 240,
  max_discount_pct    REAL DEFAULT 15,
  discount_aggressiveness REAL DEFAULT 0.5,
  best_time_enabled   SMALLINT DEFAULT 1 CHECK (best_time_enabled IN (0,1)),
  weather_aware       SMALLINT DEFAULT 0 CHECK (weather_aware IN (0,1)),
  updated_at          TEXT NOT NULL
)

CREATE TABLE IF NOT EXISTS push_tokens (
  id          BIGSERIAL PRIMARY KEY,
  customer_id BIGINT REFERENCES customers(id) ON DELETE CASCADE,
  shop_id     BIGINT,
  token       TEXT NOT NULL,
  platform    TEXT DEFAULT 'android',
  active      SMALLINT DEFAULT 1 CHECK (active IN (0,1)),
  created_at  TEXT NOT NULL
)
