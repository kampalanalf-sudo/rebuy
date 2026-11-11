# Rebuy on Supabase

Project: **rebuy** (ref `amvkdevvjxbjnsrozobr`, region eu-central-1, Postgres 17.11)

Connection string (session pooler — use this one):

```
postgresql://postgres.amvkdevvjxbjnsrozobr:Rb7Kq2mZx9Tf4Ls8Wn3V@aws-1-eu-central-1.pooler.supabase.com:5432/postgres
```

The direct host `db.amvkdevvjxbjnsrozobr.supabase.co` is IPv6-only; the pooler
is reachable over IPv4 from anywhere.

## What is deployed

| Item | State |
|---|---|
| Schema (19 tables, 24 indexes, 3 compat functions) | created in `public` |
| Demo fleet | 121 shops, 6,095 customers, 17,763 sales, 39,223 line items, 39,205 reminders, 36,837 messages, 4,528 coupons |
| Database size | ~70 MB |
| App server | `REBUY_DATABASE_URL=<above> python3 serve.py` — shop SPA at `/`, admin at `/admin` |

## Load / reload the data

```bash
export REBUY_DATABASE_URL="postgresql://postgres.amvkdevvjxbjnsrozobr:Rb7Kq2mZx9Tf4Ls8Wn3V@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"
python3 scripts/load_supabase.py --shops 120 --wipe
```

`--wipe` truncates `public` tables and reseeds; `fix_sequences()` then moves
every `BIGSERIAL` past the seeded ids so app inserts do not collide.

## Logins

* Shop owner — `owner@kampalafresh.ug` / `Shop1234!`
* Platform admin — `admin@rebuy.app` / `Admin1234!`

## Rotating the password

Supabase → Project Settings → Database → Reset database password, then update
`REBUY_DATABASE_URL` wherever the app runs. Nothing is stored in the database
that depends on the password.

## Why the app is not "hosted by Supabase"

Supabase hosts Postgres (plus auth/storage/edge functions); it does not run a
long-lived Python HTTP server. So the split is:

* **Supabase** — durable Postgres, the source of truth.
* **App** — any container/VM (Fly, Render, Railway, a VPS, Docker locally) with
  `REBUY_DATABASE_URL` set. On Vercel the same code runs as a serverless
  function with `REBUY_DATABASE_URL` + `REBUY_WORKER=0` and the cron hitting
  `/api/cron/tick`.

## Verified

All 27 shop + admin API endpoints return 200 against Supabase; login, signup
(shop 121), sale creation, campaign send, reports and the admin cockpit read
real data from Postgres.
