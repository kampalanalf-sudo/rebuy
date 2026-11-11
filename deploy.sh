#!/usr/bin/env bash
# Build a Vercel-ready payload for Rebuy.
#
#   ./deploy.sh                -> /tmp/rebuy_payload.json (slim bundle, pre-gzipped static)
#   ./deploy.sh --with-vault   -> also embed the seeded SQLite snapshot as scripts/restore_vault.py
#   ./deploy.sh --split        -> same bundle plus part files of <88 KB each
#
# Then either:
#   A) deploy with the Vercel CLI (recommended):
#        npm i -g vercel && vercel login && vercel && vercel --prod
#   B) POST the payload to https://api.vercel.com/v13/deployments
#      body: {"name": "rebuy", "files": [...], "target": "production"}
#      (the API requires projectSettings for a brand-new project, so create the
#       project once in the dashboard or with the CLI first)
set -euo pipefail
cd "$(dirname "$0")"

ARGS=(--vercel --out /tmp/rebuy_payload.json)
for arg in "$@"; do
  case "$arg" in
    --with-vault) ARGS+=(--vault data/rebuy.db) ;;
    --split) ARGS+=(--split 88000) ;;
  esac
done

python3 scripts/build_payload.py "${ARGS[@]}"

cat <<'TXT'

Environment variables for the Vercel project (Settings -> Environment Variables):
  REBUY_ENV=production      turns on strict config checks, Secure cookies, closed signup
  REBUY_SECRET_KEY=...      REQUIRED in production; 32+ chars, from a password manager
  REBUY_PUBLIC_URL=...      https URL of the deployment (OAuth redirect base)
  REBUY_DATABASE_URL=...    Postgres/Supabase DSN (omit to use SQLite on /tmp)
  REBUY_GOOGLE_CLIENT_ID / REBUY_GOOGLE_CLIENT_SECRET    Google sign-in
  REBUY_ADMIN_EMAILS=...    comma list promoted to platform admin on first sign-in
  REBUY_ALLOWED_EMAILS=...  optional invite wall for a private beta
  REBUY_WORKER=0            no background thread on serverless (Vercel Cron drives the queue)
  REBUY_DB=/tmp/rebuy.db    SQLite path; /tmp is ephemeral on Vercel (see README)
  CRON_SECRET               random 40-char value, generated per deployment:
                            python3 -c "import secrets;print(secrets.token_urlsafe(40))"
  WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_VERIFY_TOKEN / WHATSAPP_APP_SECRET
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_SECURITY
TXT
