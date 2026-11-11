#!/usr/bin/env bash
# Rebuy HTTP smoke test — starts nothing, tests a running instance.
#
#   REBUY_DB=/tmp/smoke.db REBUY_ENV=development python3 serve.py --port 8300 &
#   bash scripts/test_api.sh http://127.0.0.1:8300
#
# Assumes a fresh database with REBUY_SEED_DEMO=0, so the signup it performs is
# the first account. Run against a throwaway database, never production.
set -u
B="${1:-http://127.0.0.1:8000}"
P=0; F=0
J=$(mktemp); C=$(mktemp); trap 'rm -f "$J" "$C"' EXIT

t() { if [ "$1" = "$2" ]; then P=$((P+1)); printf "  \033[32mPASS\033[0m %s\n" "$3"
      else F=$((F+1)); printf "  \033[31mFAIL\033[0m %s (got %s, want %s)\n" "$3" "$1" "$2"; fi }
code() { curl -s -o "$J" -w "%{http_code}" "$@"; }
hdrs() { curl -sI "$1"; }

EMAIL="smoke-$(date +%s)@shop.ug"

echo "== public =="
t "$(code "$B/")" 200 "shop app loads"
t "$(code "$B/admin")" 200 "admin cockpit loads"
t "$(code "$B/api/health")" 200 "health endpoint"

echo "== anonymous access is refused =="
t "$(code "$B/api/shop")" 401 "shop API"
t "$(code "$B/api/team")" 401 "team API"
t "$(code "$B/api/auth/sessions")" 401 "sessions API"
t "$(code "$B/api/admin/overview")" 401 "admin API"
t "$(code "$B/api/customers")" 401 "customers API"

echo "== signup validation =="
t "$(code -X POST "$B/api/signup" -H 'content-type: application/json' \
     -d '{"shop_name":"X","owner_name":"Y","email":"a@b.co","password":"short"}')" 400 "short password rejected"
t "$(code -X POST "$B/api/signup" -H 'content-type: application/json' \
     -d "{\"shop_name\":\"Smoke Shop\",\"owner_name\":\"Smoke Owner\",\"email\":\"$EMAIL\",\"password\":\"Valid-Password-99\",\"phone\":\"+256772000111\"}")" 200 "valid signup succeeds"
t "$(code -X POST "$B/api/signup" -H 'content-type: application/json' \
     -d "{\"shop_name\":\"Dup\",\"owner_name\":\"X\",\"email\":\"$EMAIL\",\"password\":\"Valid-Password-99\"}")" 409 "duplicate email rejected"

echo "== session =="
curl -s -c "$C" -X POST "$B/api/auth/login" -H 'content-type: application/json' \
     -d "{\"email\":\"$EMAIL\",\"password\":\"Valid-Password-99\"}" -o /dev/null
t "$(code -b "$C" "$B/api/shop")" 200 "session works on shop API"
t "$(code -b "$C" "$B/api/team")" 200 "session works on team API"
t "$(code -b "$C" "$B/api/auth/sessions")" 200 "session list works"

echo "== authorisation =="
t "$(code -b "$C" "$B/api/admin/overview")" 403 "shop owner blocked from admin API"
t "$(code -X POST "$B/api/auth/logout" -H 'origin: https://evil.test')" 403 "cross-origin write blocked"
t "$(code -b "$C" -X POST "$B/api/team/invite" -H 'content-type: application/json' \
     -d '{"email":"not-an-email","role":"staff"}')" 400 "invalid invite email rejected"

echo "== security headers =="
H=$(hdrs "$B/")
echo "$H" | grep -qi "x-content-type-options: nosniff" \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m nosniff header\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m nosniff header\n"; }
echo "$H" | grep -qi "content-security-policy" \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m CSP header\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m CSP header\n"; }
echo "$H" | grep -qi "strict-origin-when-cross-origin" \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m referrer policy\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m referrer policy\n"; }

echo "== AI nudges / location =="
t "$(code -b "$C" "$B/api/nudges/insights")" 200 "nudge insights readable"
t "$(code -b "$C" "$B/api/nudges/places")" 200 "places list readable"
t "$(code -b "$C" "$B/api/nudges/settings")" 200 "nudge settings readable"
t "$(code -b "$C" -X PATCH "$B/api/nudges/settings" -H 'content-type: application/json' \
     -d '{"max_per_week":2}')" 200 "nudge settings writable"
INS=$(curl -s -b "$C" "$B/api/nudges/insights")
echo "$INS" | grep -q '"verdict"' \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m insights carry a lift verdict\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m insights carry a lift verdict\n"; }
# a fresh shop has no decisions yet, so the explanation array is legitimately absent;
# what must always be present is the container key the cockpit renders from.
echo "$INS" | grep -q '"recent"' \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m insights expose the decision log\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m insights expose the decision log\n"; }
echo "$INS" | grep -q '"consent"' \
  && { P=$((P+1)); printf "  \033[32mPASS\033[0m insights expose the consent funnel\n"; } \
  || { F=$((F+1)); printf "  \033[31mFAIL\033[0m insights expose the consent funnel\n"; }
# unauthenticated device API must refuse
t "$(code -X POST "$B/api/native/location" -H 'content-type: application/json' -d '{"lat":0.34,"lng":32.58,"accuracy":30}')" 401 "location needs a paired device"
t "$(code -X POST "$B/api/native/pair" -H 'content-type: application/json' -d '{"code":"000000"}')" 400 "bad pairing code rejected"

echo "== logout =="
curl -s -b "$C" -X POST "$B/api/auth/logout" -o /dev/null
t "$(code -b "$C" "$B/api/shop")" 401 "session dead after logout"

echo
echo "$P passed, $F failed"
[ "$F" -eq 0 ]
