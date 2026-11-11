# Rebuy native app — integration contract

The server side and this contract are built; the iOS/Android binaries are not.
This document is what the app team builds against. Nothing here is a mock: every
endpoint below is live and covered by `scripts/test_api.sh`.

Base URL: whatever `REBUY_PUBLIC_URL` is set to (production) or
`http://127.0.0.1:8504` (local). All requests and responses are JSON.

---

## 1. Authentication: one pairing, then a device token

Shoppers do not have passwords in the app. They pair once and the app keeps the
returned token.

### `POST /api/native/pair`

```json
{ "shop_slug": "kampala-fresh-mart",
  "phone": "+256772123456",
  "platform": "ios",
  "app_version": "1.0.0",
  "push_token": "optional-apns-or-fcm-token" }
```

Response `200`:

```json
{ "ok": true,
  "token": "…43 chars…",
  "expires_days": 180,
  "customer": { "id": 42, "name": "Sharon Okello" },
  "shop":     { "name": "Kampala Fresh Mart", "slug": "kampala-fresh-mart" },
  "consent":  { "location": false, "marketing": false, "analytics": false } }
```

The phone must already be on file for a customer of that shop. That is
deliberate: pairing cannot be used to create accounts or to discover whether an
arbitrary number is a customer of an arbitrary shop, because a failed pair only
ever says "we do not have that number on file".

**Production caveat.** Until an SMS or WhatsApp provider is configured, pairing is
allowed only outside production (`REBUY_ENV=production` refuses it). The upgrade
is a 6-digit code sent to the phone and exchanged for the token — the token
exchange already exists, only the code step is missing. Do not ship pairing to
real customers before that step lands, or a phone number is the only secret
protecting a shopper's purchase history.

Store the token in the keychain / Keystore, never in plain preferences.

### Authenticated calls

Every call below sends:

```
Authorization: Bearer <token>
```

A wrong or missing token is `401` with `{"error": "unpaired_device"}`. There is
no refresh endpoint; tokens last `PAIR_TTL_DAYS` (180) and the app re-pairs after.

`GET /api/native/me` returns the shopper, the shop, the branch list and the
current consent state. Call it on launch to decide whether to show the
location-permission prompt and whether the shopper is signed in at all.

---

## 2. Consent: separate purposes, and the app must respect them

### `POST /api/native/consent`

```json
{ "purpose": "location", "granted": true }
```

`purpose` is one of `location`, `marketing`, `analytics`. Returns the new state:

```json
{ "ok": true, "purpose": "location", "granted": true,
  "state": { "location": true, "marketing": false, "analytics": false } }
```

These are genuinely separate. A shopper can allow location and refuse marketing:
the shopper's position is then used only to count footfall, and no nudge is sent.
The server enforces that (`reason: "no marketing consent on file"` in the
decision log), so the app does not have to — but the app should not ask for
location in a way that implies marketing consent was given.

---

## 3. Location fixes

### `POST /api/native/location`

```json
{ "lat": 0.3476, "lng": 32.5825, "accuracy": 25 }
```

Response `200`:

```json
{ "ok": true,
  "used": true,
  "distance_m": 42.1,
  "place": "Ntinda branch",
  "event": "enter",
  "inside": true,
  "offer": null,
  "decided": false,
  "reason": "nothing due for replenishment" }
```

`event` is `enter`, `exit` or `""` — it changes only on a crossing, so the app can
use it to decide whether to re-render. `offer` is `null` when nothing was sent,
and `reason` then says why in plain words (`"nothing due for replenishment"`,
`"nudged recently — inside the cooling-off window"`, `"no marketing consent on
file"`). When an offer *is* sent, `offer` carries
`{ headline, body, product_name, discount_pct }` and `decided` is `true`.

A fix that lands outside every branch returns `"place": "<nearest label>"` with
`"inside": false` — the nearest branch is still named so the app can say how far
away the shopper is.

`used` is `false` when the fix could not be trusted. That is not an error — the
app should send the next fix and carry on. The reasons, exactly as returned:

| `reason` | Meaning | What the app should do |
|---|---|---|
| `no location consent` | Shopper has not allowed location | Ask for permission, do not retry blindly |
| `fix too imprecise (N m)` | `accuracy` worse than 250 m | Wait for a better fix |

Outside every branch is **not** a refusal: the response is `used: true` with
`inside: false`, so the app can still show distance. Only an untrusted fix or a
missing consent returns `used: false`.

Out-of-range coordinates are rejected before that with `400 bad_fix`, and a
missing or unpaired token with `401 unpaired_device`.

**Fix quality matters more than frequency.** A 400 m geofence and a 2 km
cell-tower fix are incompatible — the server treats a 2 km accuracy figure as
unusable rather than guessing, so sending frequent bad fixes achieves nothing
and burns battery. Send on significant movement (iOS
`startMonitoringSignificantLocationChanges`, Android fused-location with a
geofence around each branch from `GET /api/native/me`), not on a timer.

---

## 4. Getting the offer without a push

### `GET /api/native/nudges`

Returns the live offers for this shopper, so the app can show a card on open even
if the push was never delivered or was dismissed.

```json
{ "offers": [ { "headline": "Milk runs out today",
                "body": "You are 2 minutes away from Ntinda branch.",
                "product_name": "Fresh Milk 500ml",
                "discount_pct": 0 } ],
  "evaluated": 3 }
```

Pushes are a delivery mechanism, not the source of truth. If the app only ever
learns about offers from push payloads it will drift.

### `POST /api/native/push-token`

```json
{ "token": "…fcm-or-apns…", "platform": "android" }
```

Registers or refreshes the push token for the paired device.

---

## 5. What the server decides, and what it refuses

The nudge is not "a customer is near a shop". All three must hold:

1. The customer is inside a branch geofence, with a fix accurate enough to
   trust.
2. A product is at or past its projected run-out date (`depletes_on`), derived
   from that customer's own purchase history.
3. The model scores the moment above the shop's threshold **and** the customer
   has marketing consent **and** they are outside the cooling-off window.

Fatigue is the strongest negative signal in the model. A shopper messaged twice
recently is not messaged again whatever the offer or the urgency, and this is
visible in the decision log as
`"nudged recently — inside the cooling-off window"`. A high score never
overrides consent or fatigue; both are hard gates applied after scoring.

---

## 6. What the app must not do

- **Compute the geofence itself and push a notification locally.** The nudge
  depends on the customer's projected run-out date, consent state, fatigue
  window and the shop's threshold — none of which live on the phone. A local
  geofence notification would fire for shoppers with no consent and no product
  due, which is exactly the spam the model exists to prevent.
- **Send location without consent marked granted.** The server suppresses the
  nudge, but the fix is still recorded, so the shop sees footfall it has no
  permission to see.
- **Treat `used: false` as a failure to retry.** It is a normal answer.

---

## 7. Local development

```bash
REBUY_DB=/tmp/geo.db REBUY_ENV=development REBUY_WORKER=0 python3 serve.py --port 8504
python3 scripts/demo_geo.py          # branch + 6 consented installs + a proximity pass
```

`scripts/demo_geo.py` refuses to run when `REBUY_ENV=production`, so it cannot
put fake footfall into a real shop's data.
