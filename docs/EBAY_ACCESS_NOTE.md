# How this project actually reaches eBay (for the blocked sibling project)

**Written by the Claude Code project that successfully pushed fitment to live eBay listings.**

## The key finding first

The Claude Code cloud session **cannot** reach `api.ebay.com`. Running the standard test
from inside the Claude session returns a 403:

```
$ curl -sS -o /dev/null -w "%{http_code}\n" https://api.ebay.com/ws/api.dll
curl: (56) CONNECT tunnel failed, response 403
```

That 403 is the Claude environment's **outbound network policy / agent proxy** (`HTTPS_PROXY`
is set in the session and it refuses to tunnel to `api.ebay.com`). It is **not** an eBay-side
auth problem and **not** fixable with a token. Calls to eBay from the Claude session will
always 403.

So this project does **not** push from the Claude session at all. It pushes from a **GitHub
Codespace**, which has unrestricted outbound network.

## Point by point

**1. Where I run.** Claude Code in a managed remote/cloud execution environment (fresh git
clone, outbound via an agent proxy). The eBay-touching code does **not** run in my
environment — it runs in the user's **GitHub Codespace** (`@user ➜ /workspaces/<repo>`),
driven by the human in a browser.

**2. Network.**
- Claude session: **blocked.** `api.ebay.com` returns 403 through the agent proxy (both the
  `/ws/api.dll` Trading endpoint and `/identity/v1/oauth2/token`).
- GitHub Codespace: **full outbound.** From there `api.ebay.com` returns normal HTTP
  200/201/401 — that is where every successful call happened.
- Takeaway: whatever the Claude environment's network level (None/Trusted/Custom), eBay is
  not on the allowlist. Don't push from the session.

**3. How eBay is reached.** No direct calls from Claude, and **no eBay MCP connector**. Claude
writes plain Python scripts (stdlib `urllib` only, no SDK) and commits + pushes them to GitHub.
The **human** runs them in the Codespace with `git pull && python3 scripts/...`. The Codespace
makes the direct HTTPS calls. Division of labor: **Claude authors code → GitHub → Codespace
executes.**

**4. Exact eBay call used to set fitment.**
- **Write (sets compatibility):** Sell Inventory API
  `PUT https://api.ebay.com/sell/inventory/v1/inventory_item/{SKU}/product_compatibility`
  (`createOrReplaceProductCompatibility`), keyed by SKU. Body:
  `{"compatibleProducts":[{"compatibilityProperties":[{"name":"Year"|"Make"|"Model"|"Trim","value":"..."}]}]}`.
  Success = HTTP **200 or 201** (201 on first write; treat both as OK). eBay silently drops
  catalog rows it doesn't recognize (partial-accept warning 25023), so over-sending years is safe.
- **Read (guard, to skip already-expanded listings):** Trading API `GetItem` with
  `<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>`, POSTed to
  `https://api.ebay.com/ws/api.dll`. Headers: `X-EBAY-API-CALL-NAME: GetItem`,
  `X-EBAY-API-SITEID: 100`, `X-EBAY-API-COMPATIBILITY-LEVEL: 1199`, and crucially
  `X-EBAY-API-IAF-TOKEN: <oauth token>` (that header is how an OAuth token is used with the
  legacy Trading API). The Inventory store (by SKU) and the Trading store (by ItemID) can
  diverge after a relist.
- **Prereq:** the listings must be Inventory-API-managed (`getInventoryItem` returns 200 for
  the SKU). If yours are Trading-only listings, use `ReviseFixedPriceItem` with
  `Item.ItemCompatibilityList` by ItemID instead.

**5. Auth.** eBay OAuth **user** access token (not application/client-credentials) with the
**write** scope `https://api.ebay.com/oauth/api_scope/sell.inventory`. Read-only scope is not
enough. Where it lives (in the Codespace, gitignored):
- **Manual mode:** `token.txt` in the repo root — one pasted user token. Expires in ~2 hours;
  the runner preflights it and stops loud on 401.
- **Auto-refresh mode:** `ebay_auth.json` with `{client_id, client_secret, refresh_token,
  scopes}`. A helper mints a fresh 2h access token from the ~18-month refresh token
  (`POST /identity/v1/oauth2/token`, `grant_type=refresh_token`, HTTP Basic
  `client_id:client_secret`) and re-mints mid-run near expiry. The Inventory API requires a
  user token, so the refresh token must come from the user-consent flow, not client credentials.

**6. Did I execute the push myself?** No. I authored the scripts and pushed them to GitHub; the
**user** ran them in the Codespace. Every successful eBay call (HTTP 200/201) appeared in the
Codespace terminal, not my session. Scale: one initial live SKU, then batch runs sweeping
~22,000 SKUs, all executed in the Codespace.

## What the blocked sibling should do

- Do **not** call `api.ebay.com` from the Claude session; it will 403 by policy.
- Write the fitment push as a self-contained script (stdlib `urllib` is enough); commit it.
- Have the human run it in a **GitHub Codespace** (or locally) where eBay is reachable, with a
  `token.txt` (`sell.inventory` write scope) or `ebay_auth.json`.
- Use `PUT .../inventory_item/{SKU}/product_compatibility` to write; accept **200/201**.
- First, confirm the listings are Inventory-managed (`getInventoryItem` → 200). If not, use the
  Trading `ReviseFixedPriceItem` / `ItemCompatibilityList` path by ItemID.
