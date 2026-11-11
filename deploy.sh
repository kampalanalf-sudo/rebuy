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
  REBUY_WORKER=0            no background thread on serverless (Vercel Cron drives the queue)
  REBUY_SEED_SHOPS=120      demo fleet size created on first boot
  REBUY_DB=/tmp/rebuy.db    SQLite path; /tmp is ephemeral on Vercel (see README)
  CRON_SECRET=<random>      protects /api/cron/tick
  WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID / WHATSAPP_VERIFY_TOKEN / WHATSAPP_APP_SECRET
  TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_FROM
  SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM / SMTP_SECURITY
TXT
