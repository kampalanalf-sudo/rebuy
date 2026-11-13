# Rebuy — app store submission runbook

Everything in this repo that a store submission needs is done. This document is the
list of things only you can do, in the order that unblocks the next step fastest.

**Status at a glance**

| | Google Play | Apple App Store |
|---|---|---|
| Web app is installable | ✅ verified | ✅ verified |
| Icons / manifest / service worker | ✅ | ✅ |
| Privacy policy URL | ✅ `https://rebuy.website/privacy.html` | ✅ same |
| Terms URL | ✅ `https://rebuy.website/terms.html` | ✅ same |
| In-app account deletion | ✅ `/api/auth/delete-account` | ✅ same |
| Account needed | $25 one-time, Play Console | $99/year, Apple Developer Program |
| Hard blocker | 12 testers × 14 continuous days (personal accounts only) | Guideline 4.2 — see §4 |
| Realistic time to live | 3–5 weeks | 1–2 weeks after the build exists |

---

## 1. What was built for the PWA

| File | Why it matters |
|---|---|
| `static/manifest.webmanifest` | name, short_name, `id`, `start_url`, `display: standalone`, 10 `any` icons + 2 `maskable`, 4 app shortcuts, 2 screenshots |
| `static/sw.js` | network-only for `/api/*`, network-first for navigation, stale-while-revalidate for assets, offline fallback, push + notificationclick handlers |
| `static/offline.html` | the screen a shopkeeper sees with no signal |
| `static/icons/*` | 48→512 PNG, maskable variants, Apple touch icon, favicons |
| `static/privacy.html` | store-required privacy policy |
| `static/terms.html` | terms of service |
| `static/.well-known/assetlinks.json` | proves domain ownership to Android (TWA) |
| `app/httpd.py` | `.webmanifest` MIME type, `Service-Worker-Allowed: /`, `no-store` on `sw.js`, `worker-src`/`manifest-src` in the CSP |

Verified with Chrome's own installability check (`Page.getInstallabilityErrors`),
which returned **zero errors** — this is the same signal that decides whether the
install prompt appears. Reproduce with:

```bash
node installability.cjs          # zero errors, manifest parsed, 12 icons
node verify_pwa.cjs              # SW active, scope /, API never cached, offline shell serves
```

**One deliberate design decision.** The service worker does **not** cache API
responses. A shopkeeper looking at "who is due today" must see the server's answer
or an honest error — a cached queue that is a day old would cause a real business
error (messaging the wrong person, or missing someone). The shell survives offline;
the data does not pretend to.

---

## 2. Google Play — the TWA route

A PWA reaches Play as a **Trusted Web Activity**: an Android app that opens your
site full-screen in Chrome, with no address bar. It is the only route Google
supports for a web app, and it is legitimate — Play has no rule against it.

### 2.1 One-time setup you must do

1. Create a **Play Console** account: https://play.google.com/console — **$25, one-time**.
2. **Choose Organization, not Personal, if you can.** An organization account (needs a
   D-U-N-S number and a business document) is **exempt** from the closed-test
   requirement. A personal account created after 13 Nov 2023 must run a closed test
   with **12 testers opted in for 14 continuous days** before you may apply for
   production — that is the single longest item in this whole process.
3. Complete **identity verification** (Google now requires it before any release).

### 2.2 Build the bundle

Prerequisites: Node 18+, a JDK (17+), and Android SDK build-tools.

```bash
npm install -g @bubblewrap/cli
bubblewrap init --manifest https://rebuy.website/manifest.webmanifest
# applicationId: com.rebuy.app     (must match assetlinks.json and never change)
# display mode:  standalone
# signing key:   create a NEW keystore and back it up in two places
bubblewrap build
# → app-release-bundle.aab  (upload this)
# → app-release-signed.apk  (for local testing)
```

`bubblewrap init` writes the Android project to `./app` — it is generated, so it is
git-ignored here. Keep the keystore **out of git**.

### 2.3 Fill in the Digital Asset Links file

Without a correct fingerprint the app still installs, but shows a URL bar and Play
reviewers read it as "a browser". Get the fingerprint:

```bash
keytool -list -v -keystore android.keystore -alias android \
  | grep -i "SHA256:" | head -1
```

Then, **two** fingerprints — your upload key *and* the Play **app-signing** key
(Play Console → Release → Setup → App integrity → App signing key certificate):

```bash
python3 scripts/make_assetlinks.py \
  --fingerprint "AA:BB:…" \
  --fingerprint "CC:DD:…"
# or straight from the keystore:
python3 scripts/make_assetlinks.py --from-keytool android.keystore --alias android --storepass "$KS_PASS"
```

Commit and deploy. Confirm it is live **before** uploading:

```bash
curl -s https://rebuy.website/.well-known/assetlinks.json | python3 -m json.tool
```

Google's verifier also wants a statement list; check it end to end with:

```
https://developers.google.com/digital-asset-links/tools/generator
```

### 2.4 Store listing assets

| Asset | Spec | Have it? |
|---|---|---|
| App icon | 512×512 PNG, 32-bit | ✅ `static/icons/icon-512.png` |
| Feature graphic | 1024×500 PNG/JPG | ⬜ needs making |
| Phone screenshots | 2–8, min 1080px on the short side | ✅ 3 captured (1440×1000) — recapture at phone size |
| Short description | ≤80 chars | ⬜ draft below |
| Full description | ≤4000 chars | ⬜ draft below |
| Privacy policy URL | public | ✅ `https://rebuy.website/privacy.html` |
| App access | test credentials for the reviewer | ✅ see §5 |

Screenshot captures live in `static/icons/shot-*.png`. To recapture after a UI change:

```bash
node shots.cjs     # dashboard, reminders, customers at 1440×1000
```

### 2.5 Data safety form — answers for this app

Play asks you to declare exactly what you collect. Rebuy's honest answers:

| Data type | Collected | Shared | Purpose | Optional? |
|---|---|---|---|---|
| Name | Yes | No | Account management | Required |
| Email address | Yes | No | Account management, sign-in | Required |
| Phone number (your customers') | Yes | Yes — passed to WhatsApp/SMS provider to deliver the message | App functionality | Required |
| Purchase history | Yes | No | App functionality (computing restock dates) | Required |
| Approximate location | Yes | No | App functionality (proximity offers) | **Optional, off by default** |
| Device or other IDs | Yes | No | Push notifications, diagnostics | Required |
| Crash logs | Yes | No | Diagnostics | Required |

- Data is **encrypted in transit** — yes (TLS everywhere).
- Users can **request data deletion** — yes, in-app and by email; see §6.
- **No** data is sold. **No** advertising SDKs. **No** third-party analytics beyond
  crash reporting.
- Clinical fields are encrypted at rest; declare "Health info" as collected only if
  you ship the clinic edition with clinical notes enabled.

### 2.6 Content rating

Complete the IARC questionnaire. For this app the answers are "no" to all
violence/sex/gambling/drugs categories; it will land at **Everyone / PEGI 3**.
One item to answer carefully: *"Does the app allow users to interact or exchange
content?"* — Rebuy sends messages to the shop's **own** customers at the shop's
instruction; there is no user-to-user social feed. Answer "no" to user-generated
content sharing between users, and explain in the notes if challenged.

### 2.7 Closed test (personal accounts only)

1. Create the release on the **Closed testing** track, upload the `.aab`.
2. Add ≥12 testers by email list or Google Group — **invite 15**, because a dropout
   restarts that tester's 14-day clock.
3. Testers must opt in through the link and actually open the app; Google assesses
   engagement when you apply.
4. Wait 14 **continuous** days, then apply for production access (the form is 10
   short questions, ~300 characters each). Allow ~7 days for the decision.

---

## 3. Apple App Store

### 3.1 The honest constraint

**Apple does not accept PWAs.** There is no "wrap the manifest" route. To be on the
App Store you need a real Xcode project that passes review. Apple's guideline 4.2
(Minimum Functionality) rejects apps that are "a repackaged website", and adding a
push notification to a bare web view does not fix it.

So the iOS work is genuinely native, and the cheapest honest shape is:

> **WKWebView shell + native navigation + native push + native offline handling**

That is what `docs/NATIVE_API.md` already exists for: the pairing, consent,
location and push-token endpoints are live and tested. The iOS app is the client
this repo was designed against; it has simply not been built.

### 3.2 What the iOS build must include to clear 4.2

Not optional extras — these are the difference between approval and rejection:

- **Native tab bar / navigation**, not just a web view with a back button.
- **APNs push** for "your reminder went out" and "a customer replied", registered
  through `POST /api/native/push-token`.
- **Offline handling** that shows a native screen, not a web error page.
- **Sign in with Apple** if you also offer Google sign-in inside the app (guideline
  4.8). This is a real gap today: the web app offers Google OAuth. Either add
  Sign in with Apple to the iOS build, or ship the iOS app without Google sign-in.
- **In-app account deletion** (5.1.1(v)) — the endpoint exists (§6).
- **A demo account for the reviewer** in App Review notes (§5).

### 3.3 Apple checklist

| Item | Spec | Status |
|---|---|---|
| Apple Developer Program | $99/year | ⬜ you |
| Bundle ID | e.g. `website.rebuy.app` | ⬜ you |
| Xcode project | SwiftUI + WKWebView | ⬜ not built |
| App icon | 1024×1024, no alpha, no rounded corners | ⬜ derive from `icon-512.png` |
| Screenshots | 6.7" and 6.5" iPhone, plus iPad if you support it | ⬜ after build |
| Privacy policy URL | | ✅ |
| App Privacy answers ("nutrition label") | mirrors the Play Data safety table in §2.5 | ⬜ |
| Demo account | | ✅ §5 |
| Sign in with Apple | required alongside Google sign-in | ⬜ gap |

**App Privacy label, filled in:** Data Linked to You — Contact Info (name, email,
phone), Purchases, Location (approximate, optional), Identifiers, Diagnostics.
Used for App Functionality — yes. Used for Tracking — **no**. That "no tracking"
answer matters: it keeps the app out of the ATT prompt and is accurate, because
there is no ad SDK and no cross-app tracking.

---

## 4. What cannot be finished without you

Being explicit, because this is the part people underestimate:

1. **Paying both accounts** — $25 Play, $99/year Apple.
2. **Creating the signing keys** and backing them up. Lose the Android keystore and
   you cannot update the app without Play App Signing key reset (a support ticket,
   not a self-serve fix).
3. **The 12-tester closed test** — 14 continuous days, real people.
4. **Building the iOS project in Xcode** and running it on a device.
5. **Review responses** — a rejection needs a human to answer it.
6. **The Play feature graphic** (1024×500) and phone-sized screenshots.

---

## 5. Reviewer credentials

Both stores require a working login for review, because the app is behind auth.
Use the demo shop, not your admin account:

```
URL:      https://rebuy.website/#/login
Email:    demo@c-care.ug
Password: CCare-Demo-2026!
```

Put this in **Play Console → App content → App access** and in **App Review Notes**.
If you rotate that password, update both forms in the same hour — a stale reviewer
login is one of the most common avoidable rejections.

---

## 6. Account deletion — what was added and why

Google (since 2024) and Apple (5.1.1(v)) both require a deletion path *inside* the
app, not only a web form.

- **Endpoint:** `POST /api/auth/delete-account` — requires a typed confirmation
  (`delete` or your email) and your password.
- **Owner:** deletes the shop and every row that hangs off it — customers, sales,
  sale items, reminders, messages, receipts, import jobs, coupons, loyalty events,
  credentials, products, clients, push tokens, invites, all users.
- **Staff:** removes the login only; the shop survives.
- **Audit:** the deletion is written to the audit log before the account disappears.
- **UI:** Account → Delete account, with copy that states plainly what will be lost.

Verified against a copy of the demo database: 220 customers, 840 sales, 1,937 sale
items, 1,924 reminders and 1,548 messages all removed, rejected confirmations left
the data untouched, and the login stopped working afterwards.

---

## 7. Store listing copy (drafts)

**Short description (Play, ≤80 chars)**

> Know when every customer is due back. Remind them automatically.

**Full description**

> **Your customers buy once. Rebuy brings them back.**
>
> Most shops lose a customer the moment the product runs out — not because the
> customer stopped caring, but because nobody reminded them. Rebuy watches what each
> customer bought, works out when it will run out or when their service is due, and
> sends a message at exactly that moment over WhatsApp, SMS or email.
>
> **Built for how shops in Uganda actually work**
> - Import your sales book or type in receipts — Rebuy sorts the mess out.
> - Messages go out in the language your customers speak, during quiet hours you set.
> - UGX pricing, no card needed to start.
>
> **What you get**
> - A queue of reminders, each with the reason and the date it is due.
> - A dashboard showing revenue recovered, reply rate and who is at risk of leaving.
> - Customer health bands so you can see who needs attention this week.
> - Win-back campaigns, coupons and loyalty points for the ones already gone.
>
> **Private by design**
> - Your customer data stays yours. Export or delete it any time.
> - Clinical notes in clinic accounts are encrypted, and every access is logged.
> - Diagnoses and test results are blocked from automated messages by default.
>
> Works on the web, and as an app on your phone.

**Keywords (Apple, ≤100 chars, comma separated)**

```
customer retention,reminders,whatsapp,loyalty,winback,shop,clinic,repeat sales
```

---

## 8. Order of operations

Do these in this order; each unlocks the next.

1. Decide Play account type — **Organization if you have a D-U-N-S number**, because
   it skips the 14-day test.
2. Generate the Android keystore (back it up now, not later).
3. `bubblewrap init` + `build` → get the upload fingerprint.
4. `python3 scripts/make_assetlinks.py --fingerprint …` → commit → deploy → curl to verify.
5. Create the Play app, complete App content (data safety, content rating, app access).
6. Upload the `.aab` to **internal testing**, install it on a real phone, confirm no
   address bar.
7. Promote to closed testing, invite 15 testers, start the 14-day clock.
8. While that runs: make the feature graphic and phone screenshots.
9. Meanwhile start the iOS build (§3.2) and the Apple Developer Program enrolment.
10. After 14 days: apply for Play production access. Submit iOS when the build is ready.
