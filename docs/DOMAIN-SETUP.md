# Setting up rebuy.website

**Status: waiting on you.** The Vercel side is done; the domain is not yet
pointing anywhere.

## What I found

`rebuy.website` returns **NXDOMAIN** from the registry's own nameservers
(`ns01.trs-dns.com`), not just from a local resolver. That means one of:

- the domain is registered but **not yet delegated** (normal for the first
  30–60 minutes after buying), or
- it is registered but the nameservers were never set, or
- it is reserved but not actually paid for.

Vercel reports the domain as `serviceType: external` with registry-default
nameservers, so it is not using Vercel DNS.

## What I did already

Added `rebuy.website` to the Vercel project, so the DNS records now exist and
`www` is ready. Removing it is one call if you change your mind.

## What you need to do

Two options. **Option A is simpler** because Vercel manages everything after it.

### Option A — point the nameservers at Vercel (recommended)

At your registrar (looks like a Tucows reseller — Namecheap, Hover, OpenSRS,
or similar), find **Nameservers** for `rebuy.website` and replace the four
`trs-dns` entries with:

```
ns1.vercel-dns.com
ns2.vercel-dns.com
```

Then in Vercel: Project → Settings → Domains → `rebuy.website` → **Verify**.

### Option B — keep your registrar's DNS and add records

At your registrar's DNS panel, add:

| Type  | Name | Value |
|-------|------|-------|
| A     | `@`  | `216.198.79.1` |
| A     | `@`  | `64.29.17.1` |
| CNAME | `www`| `7bfb1ee66e543ca8.vercel-dns-017.com.` |

Delete any existing A/CNAME on `@` or `www` first, or Vercel will keep
reporting the domain as misconfigured.

## How long it takes

Nameservers: usually 15–60 minutes, occasionally a few hours.
A/CNAME records: usually under 10 minutes.

## What I do once it resolves

1. Verify propagation from here.
2. Set `REBUY_PUBLIC_URL=https://rebuy.website` in Vercel (production).
   This changes password-reset links, invite links and the Google redirect URI.
3. Add `https://rebuy.website/api/auth/google/callback` to the **Authorised
   redirect URIs** in Google Cloud Console. **Google sign-in breaks until this
   is done** — the new hostname will send a redirect URI Google does not
   recognise.
4. Redeploy so the new env value takes effect.
5. Confirm the apex and `www` both serve the app over HTTPS, and that the
   certificate is issued.

## Check it yourself

```bash
curl -s "https://cloudflare-dns.com/dns-query?name=rebuy.website&type=NS" \
  -H 'accept: application/dns-json'
```

`Status: 3` means still not delegated. `Status: 0` with Vercel nameservers in
the answer means you are good and I can take it from there.

Or just open https://rebuy.website — if it loads the login page, it worked.
