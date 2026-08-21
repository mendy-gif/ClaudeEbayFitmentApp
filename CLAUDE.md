# CLAUDE.md — project memory for the eBay fitment automation

Read this first. It's the durable knowledge that must survive new chats and context compaction.
The owner (`mendy`) is **not a developer** — explain plainly, and prefer running things for them
over handing over long commands.

## What this project does

Expands vehicle **fitment (compatibility)** on ~15k BMW salvage-parts eBay listings from just the
**donor vehicle** to the whole **BMW chassis family**, using rule-based logic (no per-part research).
Then pushes the result to eBay by SKU. **BMW-only** — non-BMW donors are skipped.

- **Rule A** (body/interior/most parts): donor's chassis → every trim in that chassis × the chassis's
  US year range.
- **Rule B** (engine parts): donor's chassis → restrict to the donor's **engine family** × year range.
- Which rule applies is decided by the listing's **eBay category** (see `data/rule_b_categories.json`).
- Two fitment **sources**, unioned per SKU: **chassis rules** (from the donor) + **part-number history**
  (every car a part number has historically come off, from `spreadsheet-fitment/`). **Both sources are
  expanded the same way** — each part-number vehicle is run through the Rule A/B chassis-family logic
  too (not just the donor), falling back to the literal vehicle when it can't be resolved.

## Golden facts / gotchas (the load-bearing knowledge)

1. **eBay is NOT reachable from a Claude cloud session** — `api.ebay.com` returns 403 by network
   policy. Claude authors code + commits; the **human runs it locally (Mac) or in a Codespace** where
   eBay is reachable. See `docs/EBAY_ACCESS_NOTE.md`.
2. **Two compatibility stores:** Inventory API (by **SKU**, the ONLY store we can *write*, via
   `createOrReplaceProductCompatibility`) vs Trading API (by **ItemID**, what *displays* and survives
   relist, **read-only** for us via `GetItem` with the `X-EBAY-API-IAF-TOKEN` header — see #9).
   The "already expanded" guard reads Trading; writes go to Inventory. They diverge when a row is
   stored but not catalog-valid (see #6) — Trading showing fewer vehicles than Inventory is the
   signature of that bug. Trading can lag a minute or two behind a write.
3. **HTTP 200, 201, and 204 are ALL success** for the compatibility write (201 = first write to a SKU).
4. **eBay user tokens expire in ~2 hours.** Manual `token.txt` mode stops loudly on 401 — refresh and
   re-run (the ledger resumes). `ebay_auth.json` mode mints fresh tokens automatically — use it for
   long sweeps.
5. **The category tree file `data/ebay_motors_categories.json` MUST exist**, or the classifier silently
   defaults every listing to Rule B. It's committed; if missing, regenerate with
   `scripts/ebay_fetch_categories.py` (needs a token).
6. **Trims MUST match eBay's vehicle catalog verbatim, or the fitment is INVISIBLE.** eBay spells a
   trim `xDrive35i Sport Utility 4-Door`; our rules emit `xDrive35i`. A mis-spelled trim is accepted
   (HTTP 200, stored, reads back) and then **silently omitted from the listing display** — no warning.
   This is what made every Rule B (engine) part show nothing for months. `scripts/ebay_compat_catalog.py`
   now repairs every row against eBay's catalog before the push. Never bypass it. (See `docs/DESIGN.md` §5.5.)
7. **Over-including fitment is safe:** eBay drops rows whose *Year/Make/Model* isn't in the catalog
   (partial-accept warning 25023), so padding years / unioning both sources won't create bad listings.
   The silent-drop caveat in #6 applies to **Trim** only.
8. **A trimless row is a WILDCARD** — eBay expands `2018 BMW X5` to every 2018 X5 trim (7 pushed rows
   became 49 displayed). Great for Rule A; on a Rule B engine part it silently re-adds the excluded
   engines, so never pad a Rule B push with trimless rows.
9. **Trading CANNOT write these listings** — `ReviseFixedPriceItem` always fails with `21919474`
   ("Inventory-based listing management is not currently supported by this tool"). Trading is
   **read-only** for us. The Inventory write displays by itself once the rows are catalog-valid.
10. **WE ARE NOT THE ONLY SYSTEM WRITING FITMENT.** Dismantly, PartOutPro, eBay's own
   auto-fitment setting, and (historically) MyFitment all push fitment to these listings.
   So the "already expanded" guard is load-bearing — a listing showing vehicles we never
   pushed belongs to one of them, and overwriting it destroys their work. It also means
   **our** fitment can be overwritten by them; `scripts/ebay_display_audit.py` is how you
   detect it (listings displaying vehicles we did not send).
11. **Throughput is capped by eBay's Trading `GetItem` quota: 5,000 calls/day**, resetting
   07:00 UTC. Every SKU costs one for the guard; each audit costs up to 300 more. Hence
   `NIGHTLY_LIMIT: 700`. Read the live counter any time (app token, plain `api_scope`):
   `GET https://api.ebay.com/developer/analytics/v1_beta/rate_limit/`. The Inventory API's
   cap is 2,000,000/day by comparison — moving the guard there is the obvious speedup, but
   see #10 for why it must be tested first.
12. **Headlights and taillights are year-restricted (LCI).** BMW facelifts a chassis
   mid-generation and the lights change at the split, so a pre-LCI headlight does NOT fit a
   post-LCI car of the same chassis. eBay categories `33710`/`33716` therefore narrow to the
   donor's side of the split (`data/bmw_lci_reference.json`, `data/lci_categories.json`).
   **The year-padding argument in DESIGN.md §5.3 does not cover this** — it holds because a
   phantom *trim* is absent from eBay's catalog and gets dropped, but a post-LCI *year* is a
   real vehicle, so nothing filters it and the buyer sees a genuine-looking match. With no
   donor year recorded we keep the full range (mendy's call: no fitment is worse than a wide
   range), so this can only improve on the old behaviour, never worsen it.
13. **The ledger prevents double-work** but only records *pushes*; skips are re-evaluated each run (that's
   why the same "already N vehicles / no donor" lines recur — harmless). Entries carry a `cv` stamp
   (`CATALOG_ERA`); entries older than the current era are re-processed automatically so fitment that
   was pushed but never displayed self-heals.

## Canonical commands (run on the Mac or in a Codespace, never from a Claude cloud session)

```bash
# 1. Refresh the Shopify donor data (chassis code + engine per SKU)
python3 scripts/shopify_donor.py --dump

# 2. Preview safely (dry-run, writes data/batch_plan.csv, NO eBay writes)
python3 scripts/ebay_batch.py plan --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --limit 50

# 3. THE LIVE SWEEP (pushes fitment; resumes via the ledger; ~500-SKU-safe on a manual token)
python3 scripts/ebay_batch.py apply --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --sleep 0.15 --live

# Why is a SKU's fitment not showing? (catalog check, read-only)
python3 scripts/ebay_trading_debug.py 52566 --token "$(python3 scripts/ebay_auth.py)"

# Inspect one SKU's live eBay state (read-only)
python3 scripts/ebay_inspect.py 1194

# Spot-check that pushed fitment survived relists
python3 scripts/ebay_batch.py audit --live

# Did the pushed fitment actually DISPLAY? (read-only; run after a sweep)
python3 scripts/ebay_display_audit.py --quiet
```

`ebay_batch.py` modes: `plan` (dry-run) · `apply` (live writes) · `audit` (persistence check).
Key flags: `--from-shopify` (donor source), `--from-inventory` (enumerate live eBay listings),
`--partnumber-fitment CSV` (union in part-number history), `--live`, `--sleep`, `--limit`, `--token`.

## Credentials (all gitignored — never commit; recreate per machine)

- **eBay, manual:** `token.txt` at repo root — one pasted OAuth user token (`sell.inventory` write scope).
- **eBay, auto-refresh (preferred for sweeps):** `ebay_auth.json` — `{client_id, client_secret,
  refresh_token, scopes}`. Verify with `python3 scripts/ebay_auth.py --check`. See `docs/DESIGN.md` §7.1.
- **Shopify:** `shopify_token.txt` (or `shopify.env`) — `SHOPIFY_STORE=` + `SHOPIFY_TOKEN=` (read_products).
  Alternatively, the donor dump can be rebuilt from a Shopify bulk export with no token:
  `python3 scripts/shopify_donor.py --from-bulk <export.jsonl>`.

## Data files

- **Committed reference (source of truth):** `bmw_chassis_reference.json`, `bmw_engine_map.json`,
  `ebay_bmw_models.json`, `rule_b_categories.json`, `ebay_motors_categories.json` (category tree),
  and `spreadsheet-fitment/data/built/ebay_ready_fitment.csv` (part-number fitment).
- **Generated / state (regenerated by runs):** `shopify_donors.json`, `pushed_ledger.json`,
  `batch_plan.csv`, `ebay_compat_cache.json` (eBay catalog answers; safe to delete).

## Repo map

- `scripts/fitment_rules.py` — Rule A/B engine: donor → eBay `{Year,Make,Model[,Trim]}` rows.
- `scripts/ebay_batch.py` — **the orchestrator.** Sweeps listings, classifies, expands, unions, pushes.
- `scripts/shopify_donor.py` — pulls donor chassis/engine per SKU from Shopify (`--dump` / `--from-bulk`).
- `scripts/classify_part.py` — Rule A vs B from the eBay categoryId + the category tree.
- `scripts/ebay_auth.py` — mints 2h eBay tokens from an ~18-month refresh token.
- `scripts/ebay_compat_catalog.py` — **the display gatekeeper.** Repairs/validates rows against eBay's
  real vehicle catalog (Taxonomy API) so the pushed fitment actually shows. `--trims 2014 BMW X5` to peek.
- `scripts/ebay_display_audit.py` — read-only fleet check: for every ledgered SKU, does what we pushed
  actually DISPLAY? Slices by rule + category. **Run this after a sweep** — the ledger only proves we
  pushed, not that eBay showed it.
- `scripts/ebay_inspect.py` — read-only per-SKU eBay diagnostic (both stores + item specifics).
- `scripts/ebay_writer.py` — single-SKU compatibility writer (used by the batch runner).
- `scripts/ebay_fetch_categories.py` / `ebay_fetch_bmw_catalog.py` — one-time reference builders.
- `spreadsheet-fitment/` — Approach 2: part-number → historical vehicles (the union's second source).
- `.github/workflows/fitment-sweep.yml` — daily automated sweep (needs the repo secrets set).
- `scripts/selftest.py` — offline sanity check (no eBay); run after cloning to a new machine.

## Conventions

- Python **stdlib only** except `openpyxl` (optional, for rebuilding reference tables from Excel).
- Every script derives its own paths from `__file__` — no hardcoded machine paths; runs on macOS as-is.
- Work happens on branch `claude/ebay-fitment-chassis-rules-3pztn4`. Commit + push when a change is done.
- Detailed design/rationale lives in `docs/DESIGN.md`; Mac setup in `docs/SETUP_MAC.md`.
