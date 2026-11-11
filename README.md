# Rebuy — customer retention for shops

Rebuy remembers what every customer bought, works out when each item runs out (or when a
service is due again), and messages the customer on WhatsApp a few days before — automatically.
It also supports SMS and email, imports from spreadsheets/receipts, and gives the platform
owner a full admin cockpit across every shop.

```
shop app     /            → shop-owner SPA (dashboard, inbox, reminders, sales, import…)
admin app    /admin       → platform cockpit (fleet KPIs, shops, messaging, revenue, queue)
api          /api/...     → JSON API (session cookie or Bearer token)
webhooks     /api/webhooks/whatsapp | /api/webhooks/twilio | /api/webhooks/email
cron         /api/cron/tick
```

## Run it locally

```bash
cd rebuy
python3 serve.py --port 8000        # stdlib only; no pip install needed
```

First boot creates the schema and seeds a deterministic demo fleet
(`REBUY_SEED_SHOPS`, default 120 shops) into `data/rebuy.db`.

| Login | Email | Password |
|---|---|---|
| Shop owner (supermarket) | `owner@kampalafresh.ug` | `Shop1234!` |
| Shop owner (salon) | `owner@nakatohair.ug` | `Shop1234!` |
| Platform admin | `admin@rebuy.app` | `Admin1234!` |

Shops 1–24 also have owner logins (`<shop-slug>@rebuy.ug` / `Shop1234!`).

## How the retention engine works

1. **A sale is recorded** (manual entry, CSV/Excel import, or pasted/OCR'd receipt).
   Every line item gets a restock date: `sale_date + effective_cycle(qty)`.
   Services (haircut, car service, gym session…) use a fixed re-booking interval instead.
2. **The worker tick** sends reminders whose `due_on - reminder_lead_days` has arrived,
   respecting quiet hours, monthly per-customer caps, opt-outs and `auto_reminders`.
3. **Replies come back** through the provider webhooks (or the sandbox), get an intent
   (`order`, `snooze`, `opt_out`, `question`, `negative`) and update the customer record.
4. **Conversions** are attributed for 21 days: a sale after a reminder marks it converted
   and books the recovered revenue.
5. Extra loops: follow-ups, win-backs with coupons, birthday treats, loyalty rewards,
   segment campaigns, RFM-style risk bands and per-shop KPIs.

Provider resolution per shop and channel: **shop credentials → platform credentials →
environment variables → sandbox**. Sandbox is the zero-config demo mode: it logs messages,
simulates delivery receipts and occasionally simulates a customer reply so the whole loop
is visible before any credentials exist.

## Going live with real providers

Open **Settings → Configure providers** (shop level) or **Admin → Providers** (platform level):

* **WhatsApp** — Meta WhatsApp Cloud API: permanent access token, Phone Number ID, API version.
  Register `https://<host>/api/webhooks/whatsapp` as the callback URL; the verify token and app
  secret come from the same form (or `WHATSAPP_VERIFY_TOKEN` / `WHATSAPP_APP_SECRET`).
* **SMS** — Twilio: Account SID, Auth Token, sender. Inbound webhook `/api/webhooks/twilio`,
  status callback `/api/webhooks/twilio-status`.
* **Email** — any SMTP relay (Gmail app password, SendGrid, SES…).

Environment variables are the fallback for all three
(`WHATSAPP_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`,
`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`).
Use **Send test** to verify a channel end-to-end.

## Scale

Everything is scoped by `shop_id`, hot paths are indexed (`reminders(status,due_on)`,
`messages(status,created_at)`, `customers(shop_id,phone)` unique, `sale_items(depletes_on)`…),
lists are paginated and the worker processes shops in batches. **Admin → Capacity** runs live
timings and projects row counts/size at 1,000 shops. SQLite (WAL) comfortably handles hundreds
of shops on one node; past ~5k shops or multi-region, move to Postgres — the schema is a
straight translation and nothing in the app assumes SQLite beyond `app/db.py`.

## Deploying to Vercel

`vercel.json` + `api/index.py` adapt the same `httpd.dispatch()` to a serverless function:

```bash
npm i -g vercel
vercel login
vercel            # links the project
vercel --prod     # production
```

Environment variables to set in the dashboard:

| Variable | Purpose |
|---|---|
| `REBUY_WORKER=0` | no background thread on serverless |
| `REBUY_SEED_SHOPS` | demo fleet size created on first boot |
| `REBUY_DB` | SQLite path (/tmp on Vercel — **ephemeral**) |
| `CRON_SECRET` | protects `/api/cron/tick` |

`vercel.json` already registers a 5-minute cron for `/api/cron/tick`, so reminders keep
flowing without the thread. **Persistence caveat:** Vercel's filesystem is read-only apart
from `/tmp`, which is per-instance and wiped on redeploy, so data does not survive. For a
durable deployment point `REBUY_DB` at a mounted volume, or switch `app/db.py` to Postgres
(every query is plain SQL with `?` placeholders).

`scripts/build_payload.py --vercel` produces the JSON bundle the Vercel API expects
(`{"files": [{"file": ..., "data": <base64>}]}`) if you want to deploy without the CLI;
`--vault data/rebuy.db` embeds the seeded SQLite file as a lazily restored snapshot.

## Layout

```
app/db.py         schema, connection, query helpers, tz-aware date helpers
app/auth.py       PBKDF2 passwords + 14-day opaque session cookies
app/providers.py  WhatsApp Cloud API / Twilio / SMTP drivers + sandbox + intent parsing
app/engine.py     reminder scheduling, templating, worker tick, KPIs, risk bands
app/imports.py    CSV/Excel/receipt parsing, fuzzy headers, customer/product upserts
app/api.py        shop-owner API (~60 routes)
app/api_admin.py  platform admin API
app/webhooks.py   inbound provider webhooks + /api/cron/tick
app/worker.py     background tick thread (disabled when REBUY_WORKER=0)
app/seed.py       deterministic 120-shop demo fleet (goods + services)
app/httpd.py      stdlib router/server; dispatch() is reused by api/index.py
static/index.html shop-owner SPA (hash routing, no build step)
static/admin.html platform admin SPA
```

## Deploy to Supabase (Postgres)

The storage layer runs on either SQLite (default, zero config) or Postgres.
Set one environment variable and every query switches engines:

```bash
export REBUY_DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-1-<region>.pooler.supabase.com:5432/postgres"
python3 scripts/load_supabase.py --shops 120 --wipe   # schema + demo fleet
REBUY_WORKER=1 python3 serve.py --port 8000            # app on Supabase
```

What the port does under the hood (`app/db.py`, `app/schema_pg.py`):

* translates SQLite SQL to Postgres on the way through — `?` → `%s`,
  `IFNULL` → `COALESCE`, scalar `MIN/MAX(a,b)` → `LEAST/GREATEST`,
  `SUM(status='sent')` → `SUM(CASE WHEN ...)`, `group_concat` → `string_agg`,
  bare `SELECT COUNT(*) n` aliases → `AS n`, plus `date()` / `julianday()` /
  `dayn()` compatibility functions created by the migration.
* bulk inserts go through `execute_values` (one round trip per 500 rows).
* timestamps stay ISO-8601 text in Africa/Kampala on both engines, so all the
  date maths in the engine is unchanged.

Use the **session pooler** host (`...pooler.supabase.com:5432`) with user
`postgres.<project-ref>`; the direct `db.<ref>.supabase.co` host is IPv6-only
and often unreachable from CI or a laptop.

Latency note: every query is a network round trip. The demo fleet is 120 shops
(~39k reminders), so a full worker pass is meant to run on a schedule rather
than on every request — `/api/cron/tick` is wired for that, and follow-up
automation is throttled by `REBUY_AUTOMATION_INTERVAL` (default 900s).
