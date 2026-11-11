# Rebuy on Supabase

Project: **rebuy** (ref `amvkdevvjxbjnsrozobr`, region eu-central-1, Postgres 17.11)

## Connection string

Find it in the Supabase dashboard → **Project Settings → Database → Connection
string → URI**. Use the **session pooler** (port 5432): the direct host
`db.<ref>.supabase.co` is IPv6-only, the pooler is reachable over IPv4.

```bash
# .env — never committed
REBUY_DATABASE_URL="postgresql://postgres.<ref>:<DB_PASSWORD>@aws-1-eu-central-1.pooler.supabase.com:5432/postgres"
```

The database password is not recorded anywhere in this repository. It lives in
the platform's secret store (Vercel → Settings → Environment Variables) and in
your password manager.

> **If a password has ever been pasted into a chat, an issue, or a commit, treat
> it as compromised.** Rotate it in Project Settings → Database → Reset database
> password, then update `REBUY_DATABASE_URL` in every environment. `git log -S`
> will tell you whether it was committed.

## What is deployed

| Item | State |
|---|---|
| Schema (22 tables, 26 indexes, 3 compat functions) | created in `public` |
| Demo fleet | opt-in only — see `REBUY_SEED_DEMO` below |
| App server | `REBUY_DATABASE_URL=... python3 serve.py` — shop SPA at `/`, admin at `/admin` |

## Load the schema

```bash
export REBUY_DATABASE_URL="postgresql://..."      # from your secret store
python3 scripts/load_supabase.py --schema-only
```

To load a **demo** fleet into a throwaway project:

```bash
REBUY_SEED_DEMO=1 python3 scripts/load_supabase.py --shops 120 --wipe
```

`--wipe` truncates the `public` tables and reseeds; `fix_sequences()` then moves
every `BIGSERIAL` past the seeded ids so app inserts do not collide. Demo users
are created on `@example.com` addresses with **random** passwords — they cannot
be signed into.

## Accounts

There are no seeded credentials. Accounts are created one of three ways:

1. **Google sign-in** — anyone whose email is in `REBUY_ALLOWED_EMAILS` (or
   anyone at all, if that variable is unset) signs in and gets an account.
2. **Self-serve signup** — only when `REBUY_ALLOW_SIGNUP=1`.
3. **Platform admin** — the first account whose email is listed in
   `REBUY_ADMIN_EMAILS` is promoted to `role='admin'` on sign-in.

Set `REBUY_ADMIN_EMAILS=you@yourcompany.ug` before the first production login,
otherwise nobody can reach `/admin`.

## Why the app is not "hosted by Supabase"

Supabase hosts Postgres (plus auth/storage/edge functions); it does not run a
long-lived Python HTTP server. So the split is:

* **Supabase** — durable Postgres, the source of truth.
* **App** — any container/VM (Fly, Render, Railway, a VPS, Docker locally) with
  `REBUY_DATABASE_URL` set. On Vercel the same code runs as a serverless
  function with `REBUY_DATABASE_URL` + `REBUY_WORKER=0` and the cron hitting
  `/api/cron/tick`.

## Verified

Shop and admin API endpoints return 200 against Supabase; login, signup, sale
creation, campaign send, reports and the admin cockpit read real data from
Postgres.
