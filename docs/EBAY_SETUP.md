# eBay API Setup & the Decisive Inventory-Item Test — A to Z

**Goal of this doc:** get you from "no eBay developer account" to running the single test that decides
this project's whole architecture (§5.1 of `DESIGN.md`): *is a real Dismantly listing reachable by
eBay's Inventory API, or not?*

- **HTTP 200 + item body** → **Path A**: listings are Inventory-API items → set compatibility by SKU,
  likely persists across relist → roughly a one-time push.
- **HTTP 404 + `errorId 25710`** → **Path B**: listings were created via Trading API / eBay UI →
  the SKU-based compatibility endpoint can't touch them → you'd need `bulkMigrateListing` or
  per-relist Trading `ReviseItem` (a continuously-running system).

> **Read this first:** the test must run against **PRODUCTION**, not Sandbox. Sandbox has none of your
> real Dismantly listings, so only Production answers the question. You *can* rehearse the OAuth flow in
> Sandbox, but the decisive call is a Production read against a real SKU. The test is **read-only**, so
> it's safe to run against the live account — nothing is modified.

---

## Part 0 — What you need before starting

- The **eBay seller account** that actually owns the 15k listings (you'll authorize against it).
- ~30–45 minutes. Most of it is one-time account setup.
- For the test itself you only need **read** access, so we'll request the low-risk
  `sell.inventory.readonly` scope. (The real automation later needs full `sell.inventory` to write.)

---

## Part 1 — Register a developer account

1. Go to **developer.ebay.com** → **Join** / **Sign in**.
2. Sign in with (or link) your eBay account and accept the developer agreement.
   You now have access to **Your Account → Application Keysets**.

> **Already have a developer account?** (e.g. a piece of software created one to connect to eBay.)
> Then skip registration and just sign in. Notes:
> - **Reuse vs. new keyset:** a *second* production keyset isn't instant — eBay requires a support
>   ticket and eligibility check. So only request one if the existing keyset backs a **live** integration
>   you must not disturb. If the existing app is **dormant/unused**, just **reuse it** — the app's name
>   (e.g. "PowerBI Connection") is cosmetic; a keyset is just credentials and works for any eBay API.
>   Generating a new user token does not affect other tokens; only changing the keyset's redirect/RuName
>   could disturb a live integration.
> - **Do not regenerate/revoke the existing keys or tokens** — that would break any software currently
>   authenticated with them.
> - **Confirm the account is yours:** you should be able to reach Application Keysets and mint a user
>   token *for your own seller account*. (Some vendors create the app under their own org.)
> - **Bonus signal:** whichever software set this up hints at Path A vs B — an older tool likely uses
>   the Trading API (→ Path B). The `getInventoryItem` test in Part 6 settles it regardless.

---

## Part 2 — Create your application keyset

1. **Your Account → Application Keysets.**
2. eBay gives you **two** keysets automatically: **Sandbox** and **Production**. Each contains:
   - **App ID (Client ID)**
   - **Cert ID (Client Secret)**
   - **Dev ID**
3. For this test you'll use the **Production** keyset. Keep the Cert ID secret (treat it like a password).

---

## Part 3 — Activate Production (compliance step)

- Production keys require accepting the **API License Agreement**; some accounts must also complete a
  short **application check / business details** step before Production is fully enabled.
- The test only makes a **read** call (`getInventoryItem`), which is generally available as soon as the
  Production keyset is active. If Production isn't active yet, complete the prompts eBay shows on the
  Application Keysets page.

---

## Part 4 — Get a USER OAuth token (the fiddly part)

The Inventory API requires a **user access token** (representing the seller), **not** an application /
client-credentials token. Two ways to get one — pick **4A** for a fast one-off test.

### First: set your redirect (RuName)
1. On **Application Keysets**, click the **User Tokens** link next to your **Production** Client ID.
2. Configure a **redirect** — eBay calls this a **RuName** (Redirect URL Name). You'll set a redirect
   URL (can be a simple placeholder like `https://localhost/ebay-callback` for manual testing) and eBay
   assigns you a RuName string. You'll use the **RuName**, not the raw URL, as `redirect_uri` in OAuth calls.
   Ref: [eBay OAuth redirect URI](https://developer.ebay.com/api-docs/static/oauth-redirect-uri.html)

### 4A — Quick path (recommended for the test)
On that same **User Tokens** page, eBay provides a hosted **"Get a User Token"** flow: it sends you
through eBay sign-in + consent using your RuName and hands back a **user access token** (valid ~2 hours).
Paste that token into the test in Part 6. No redirect handler to build.
- If asked to choose scopes, include `https://api.ebay.com/oauth/api_scope/sell.inventory.readonly`.

### 4B — Manual OAuth (works regardless of portal UI; also the basis for the real automation)
1. **Build the consent URL** and open it in a browser (replace `CLIENT_ID` and `RUNAME`):
   ```
   https://auth.ebay.com/oauth2/authorize?client_id=CLIENT_ID&response_type=code&redirect_uri=RUNAME&scope=https://api.ebay.com/oauth/api_scope/sell.inventory.readonly
   ```
2. Sign in as the **seller**, click **Agree**. eBay redirects to your RuName's URL with `?code=...`
   appended. **Copy that `code`** (it's URL-encoded and single-use, expires in ~5 min).
3. **Exchange the code for tokens** (replace `CODE`, `RUNAME`, and the Basic auth):
   ```bash
   curl -s -X POST 'https://api.ebay.com/identity/v1/oauth2/token' \
     -H 'Content-Type: application/x-www-form-urlencoded' \
     -H "Authorization: Basic $(printf 'CLIENT_ID:CERT_ID' | base64 -w0)" \
     -d 'grant_type=authorization_code' \
     -d 'code=CODE' \
     -d 'redirect_uri=RUNAME'
   ```
   The response contains `access_token` (use it for the test, ~2h life) and `refresh_token`
   (~18 months — this is what the real automation stores and uses to mint fresh access tokens).
   The helper script (`scripts/ebay_inventory_test.py exchange`) does this step for you.
   Ref: [eBay OAuth tokens](https://developer.ebay.com/api-docs/static/oauth-tokens.html)

---

## Part 5 — Get a real SKU to test

You need the **SKU (a.k.a. Custom Label)** of a real, currently-active Dismantly listing.

- **Easiest:** eBay **Seller Hub → Listings → Active**, and read the **Custom label (SKU)** column for
  any live item. That string is the SKU.
- Or pull it from Dismantly's listing view.
- **Pick 3–5 SKUs, not one** — vary the part type (an engine part *and* a body part) and listing age.
  Dismantly's behavior could differ across listing types, and you want to know if the answer is uniform.

---

## Part 6 — Run the test

### Option 1 — raw curl (fastest)
```bash
curl -s -w '\nHTTP %{http_code}\n' \
  -H "Authorization: Bearer ACCESS_TOKEN" \
  -H "Accept: application/json" \
  "https://api.ebay.com/sell/inventory/v1/inventory_item/SKU_HERE"
```

### Option 2 — the script (nicer, interprets the result for you)
```bash
# Zero dependencies — Python 3 standard library only.
python3 scripts/ebay_inventory_test.py test --token "ACCESS_TOKEN" --sku "SKU_HERE"

# Test several at once:
python3 scripts/ebay_inventory_test.py test --token "ACCESS_TOKEN" \
  --sku "SKU_ONE" --sku "SKU_TWO" --sku "SKU_THREE"
```
The script prints, per SKU, either **PATH A (Inventory-managed)** or **PATH B (Trading/UI listing)** and
a one-line reason.

---

## Part 7 — Read the result

| Result | Meaning | Architecture |
|--------|---------|--------------|
| **HTTP 200** + JSON inventory item | The SKU **is** an Inventory-API inventory item | **Path A** — set compatibility by SKU via `createOrReplaceProductCompatibility`; SKU-scoped, so it should ride across relists (confirm once). Roughly **one-time push** + a watcher for new SKUs. |
| **HTTP 404**, `errors[].errorId = 25710` | Listing was created via **Trading API or eBay UI** — no inventory item exists behind the SKU | **Path B** — SKU-based compatibility can't reach it. Either `bulkMigrateListing` into the Inventory model first, or set compatibility via Trading `ReviseItem` per Item ID **every relist cycle** (continuous system). |
| **HTTP 401 / 403** | Token expired, wrong scope, or wrong account | Not a listing-type answer — re-mint the token (Part 4) with the right scope and seller account, retry. |

If **all** your test SKUs return 25710, you're firmly on Path B and should design for the migrate-or-revise
route. If they return 200, Path A — proceed to the one Sandbox check that compatibility survives a
delete-offer / new-offer cycle (`DESIGN.md` §5.2) before building the backfill.

---

## Appendix — for the real automation (after the test)

- **Token lifecycle:** store the **refresh token** (~18 mo). Mint a short-lived access token from it via
  `grant_type=refresh_token` when needed. Never hard-code the access token.
- **Scopes:** the test uses `sell.inventory.readonly`; **writing** compatibility needs
  `https://api.ebay.com/oauth/api_scope/sell.inventory`. Re-consent with the broader scope when you build the writer.
- **Rate limits:** read your real per-endpoint quotas from the Analytics API `getRateLimits` rather than
  assuming; user tokens have far higher allowances than application tokens.
- **Never commit tokens or the Cert ID.** Keep them in environment variables / a local untracked file
  (see the `.gitignore` note in the repo).

Refs: [getInventoryItem](https://developer.ebay.com/api-docs/sell/inventory/resources/inventory_item/methods/getInventoryItem) ·
[error 25710 (KB 5210)](https://developer.ebay.com/support/kb-article?KBid=5210) ·
[Using OAuth to access eBay APIs](https://developer.ebay.com/api-docs/static/oauth-scopes.html)
