# BMW Chassis-Family Fitment — Design & Decisions

**Status:** Built and live — pipeline pushes chassis-family + part-number fitment to real listings
(Path A confirmed; Shopify donor source + eBay-category classification + part-number union all wired).
**Last updated:** 2026-08-17
**Scope:** The "fast fitment" layer only — broad, rule-based BMW compatibility applied at scale. Granular cross-series fitment (parts that also fit 4-/5-series, etc.) stays in the slower DPC-research track and is explicitly **out of scope** here.

---

## 1. The problem in one paragraph

~15,000+ active eBay listings, most showing fitment for only the single donor vehicle the part came from, even though many parts fit a whole BMW chassis family. Expanding fitment is the highest-leverage listing improvement available. We want to do it with the least *judgment* labor possible: convert per-part research into a one-time reference table + mechanical rules, then push the result to eBay via API (Dismantly offers no API for its fitment fields, so we bypass it and go straight to eBay).

---

## 2. Decisions (TL;DR)

| # | Question | Decision |
|---|----------|----------|
| 1 | Does Rule B need a per-trim year table? | **No.** Reuse the chassis year range; eBay drops phantom years automatically. One reference table total. |
| 2 | Does compatibility survive Dismantly's relist? | **Yes, *if* the listing is an Inventory-API item and the SKU is reused** — because compatibility is stored at the SKU/inventory-item level, not the Item ID. Needs one Sandbox confirmation. |
| 3 | One-time push or continuously running? | **One-time push per SKU (confirmed live, §5.2).** Dismantly's relist carries our compatibility forward (Trading store), so we write once. Caveat: verify via the Trading store, not `getProductCompatibility` (reads 0 post-relist). |
| 4 | How to store/maintain the reference data? | **A single versioned data file (CSV/JSON) in this repo**, one row per chassis code. Small enough that a database is overkill; git gives us history and review. |
| 5 | eBay API setup | Self-serve developer account → keyset → compliance step → **user** OAuth token with `sell.inventory` scope. See §7. |

**TEST RESULT (2026-08-13): Path A confirmed, uniformly.** `getInventoryItem` returned HTTP 200 for **5 of
5** real live SKUs tested (`5978`, `52635`, `484`, `51917`, `5452`) — Dismantly's listings **are**
Inventory-API inventory items, reachable by SKU. This resolves the project's single biggest architectural
risk in the favorable direction: compatibility can be set by SKU via `createOrReplaceProductCompatibility`,
no `bulkMigrateListing` needed, and the system is a one-time push rather than a continuous re-apply.
**Note on SKU format:** real Custom Labels are bare numbers (e.g. `5978`), not prefixed. **Gotcha learned:**
error `25710` is returned identically for a Trading/UI listing *and* for a nonexistent/mistyped SKU — an
early false "Path B" was just a stray `SKU_` prefix, so always test the exact Custom Label.
**Still open:** relist *persistence* (§5.2) is a strong inference (SKU-scoped storage) but not yet observed
across an actual Dismantly relist.

---

## 3. The fitment rules (simplified to a single table)

The key simplification: **for BMW engine parts, the trim badge *is* the engine spec** (335 vs 328 = a different engine, full stop). So Rule B never needs to *look up* compatible trims — the donor vehicle already names the trim, and "same trim" = "same engine" by definition. That eliminated the per-chassis Trim Detail tables entirely.

Both rules run off **one** table (§4):

- **Rule A — default (body / interior / electrical / most parts):**
  Donor chassis → add **every trim** in that chassis × the **chassis's full US year range**.
  *Example: 2014 335i (F30) brake caliper → fitment for 320i / 328i / 330i / 335i / 340i across the full F30 year range.*

- **Rule B — engine & engine-accessory parts only:**
  Donor chassis → add **only the donor's own trim** × the **chassis's full US year range**.
  *Example: 2012 335i (F30) turbo → fitment for 335i across the full F30 year range (2012–2019). eBay keeps the years the 335i actually existed (2012–2015) and drops the rest.*

**Why year-padding is safe but trim-padding is not:**
- Padding *years within the correct trim* → a buyer can never select a "2017 335i" (it doesn't exist in eBay's catalog), so a phantom year can never produce a false match. eBay drops it. **Safe.**
- Padding *across trims* (tagging a 335i engine part as fitting a 328i) → a 328i is a real vehicle a buyer *can* select, and eBay validation won't catch it because the vehicle exists. This is the exact false positive Rule B prevents. **Not safe — hence Rule B.**

**Accepted imprecision (recorded, not a blocker):** the "trim = engine" mapping is ~95% clean because BMW renames the badge when the engine changes (F30 328i→330i tracked the N20→B48 swap). Rare exceptions are a single badge spanning two engines of the same *family* mid-generation (e.g. 335i N54→N55 ~2011–2013) — interchangeable enough for a broad-fitment layer, and well inside the tolerance eBay's own non-granular fitment already implies.

**The one remaining judgment call:** how the automation decides a listing is an engine part (Rule B) vs. everything else (Rule A). See §6.1 — this is the only non-mechanical step left in the pipeline.

### 3.1 Two namespaces: chassis (internal) vs. eBay Year/Make/Model (output)

Critical distinction that shapes the whole data layer:

- **Chassis code (F30, E90, G05, …) is INTERNAL only.** eBay's parts-compatibility catalog has **no concept
  of a chassis code.** We use the chassis purely as the key that lets the rules know which vehicles share a
  family. eBay never sees it.
- **What we actually push to eBay is `Year / Make / Model / Trim / Engine`,** and those values **must match
  eBay's own vehicle catalog exactly.** eBay validates the combination and silently drops rows it doesn't
  recognize (the partial-acceptance behavior in §5.3). If our Model string is `330i xDrive` but eBay's
  catalog spells it differently, that row is dropped and the part loses that fitment.
- **eBay's Model field is badge-level and often bakes in drivetrain** — e.g. `328i xDrive` is its own Model
  entry, not `Model=328i` + `Trim=xDrive`. So our trim vocabulary has to be reconciled to eBay's, not
  authored from enthusiast/Wikipedia naming.
- **Source of truth for eBay's vocabulary = the Taxonomy API** (`category_tree_id = 100`):
  `getCompatibilityProperties` returns the aspect names (Year, Make, Model, Trim, Engine);
  `getCompatibilityPropertyValues` returns the valid values (e.g. every Model eBay recognizes for
  Make=BMW). This is machine-pullable and is the naming we must target.

**Implication for the data model (§4):** the reference table's job is chassis grouping + US year ranges (the
part eBay can't provide). Its trim list is a **provisional mapping** that must be reconciled to eBay's exact
Model strings before any push. Plan: pull eBay's BMW Model list, then map each chassis's members to eBay
Model values (mostly 1:1, but flag mismatches like drivetrain-in-Model). The rules then emit eBay-vocabulary
rows: **Rule A** = all eBay Models in the chassis × year range; **Rule B** = donor's eBay Model × year range.

**Two donor-identification problems the rule engine surfaced (scripts/fitment_rules.py):**
1. **Year + model is often ambiguous at generation boundaries.** A "2012 335i" is *both* an E92 coupe
   (2007–2013) and an F30 sedan (2012–2018); a "2008 328i" spans E90/E91/E92/E93. The donor's **body
   style / generation** is needed to pick the chassis. The engine detects this and refuses to guess
   (returns an ambiguity list) rather than silently mis-expanding. In production the donor is a specific
   listing, so its body/engine should be available to disambiguate.
2. **BMW reuses badges across nameplates.** "xDrive35i" alone matches X1/X3/X4/X5/X6. This is *why* the
   eBay **Model** field matters: for SUVs eBay's Model is the nameplate ("X5") with drivetrain as Trim,
   whereas for sedans the badge *is* the Model ("328i"). Our provisional `trims` column mixes these two
   conventions — reconciling to eBay's Model vocabulary (pull via Taxonomy API) is what makes donor
   lookup and output both unambiguous. This puts the catalog pull on the critical path, not optional.

### 3.2 Donor source & scope (learned live 2026-08-15)

Where the donor vehicle actually comes from, and what's in scope:

- **Two writers populate eBay, neither is us:** **PartOutPro** pushes the structured **compatibility list**
  (the donor vehicle as fitment) — present on **~9,500 of ~15k** listings, lags/misses new ones.
  **Dismantly** pushes **item specifics** (Make/Model, VIN, Engine Code, etc.) on **~all** listings.
- **Donor source A — compatibility list (PartOutPro):** clean Year/Make/Model/Trim, but only ~9,500
  listings. `ebay_batch.py` reads this today and expands the **BMW** subset.
- **Donor source B — item specifics (Dismantly):** on ~all listings, but has **Make/Model and no clean
  Year** — the model year lives only in the **VIN**. To use B we must **decode year from VIN** (10th char).
  Phase-2 add for the ~5,500 without a compatibility donor.
- **Scope: BMW-only.** The catalog is multi-make Euro (e.g. Audi Q3 seen live). Our chassis/engine
  reference is BMW-only, so `ebay_batch.py` **skips non-BMW donors explicitly** ("non-BMW - out of scope").
  Other makes would each need their own reference data (future).
- **Fastest/complete option:** a bulk **SKU → donor (Year/Make/Model)** export from PartOutPro or Dismantly
  would remove ~2 eBay reads per SKU and give full coverage — feed it via a `--donor-file` (to build).
- **Donor source C — Shopify (BUILT, the clean/automatable one):** every listing also lives on the owner's
  Shopify, where per-SKU tags carry the donor **make/model, chassis code (`veh_series_G87`), engine code
  (`veh_engine_code_S58B30T0`), and part_type** directly. `scripts/shopify_donor.py --dump` pulls all active
  BMW products via the Admin GraphQL API → `data/shopify_donors.json` (keyed by variant SKU = eBay SKU).
  This gives the **chassis code with no year/VIN inference and the engine family with no map lookup** —
  the best donor source. Auth: `SHOPIFY_STORE` + `SHOPIFY_TOKEN` (custom app, `read_products`).

**Chassis-code reconciliation (`fitment_rules.resolve_chassis`).** Shopify gives a **bare** code (`G80`,
`E90`, `G26`); our reference uses **composite** codes for M-cars/EVs (`G80 M3`, `E90 M3`, `G26 (i4)`,
`E70 X5 M`). Some bare codes map to **both** a plain row and an M/EV row (`E90` = 3-Series sedan *or* M3;
`E70` = X5 *or* X5 M; `G26` = 4-GC *or* i4), so the donor **Model disambiguates**: `E90`+`M3` → `E90 M3`,
`E90`+`328i` → `E90`, no model match → the plain row. Non-BMW series (e.g. Audi `8U`) resolve to `None` → skipped.

**Runner wiring.** `ebay_batch.py --from-shopify` swaps the **donor source** to `data/shopify_donors.json`
(via `resolve_chassis` + `expand_from_chassis`), while **Rule A/B classification stays on the eBay category**
(owner's accuracy preference). It still reads the Trading item to honor the "already multi-fit → skip" guard,
but `n_trad == 0` no longer skips (Shopify supplies the donor — the fix for the 22/25 "no donor" misses).
With no `--sku`/`--from-inventory`, it enumerates the dump's SKUs directly.

---

## 4. Data model

**Decision: one file, one row per chassis code.** No trim table, no per-trim year table.

Recommended: a CSV (human-editable, diff-friendly) and/or JSON (machine-consumed) checked into `data/` in this repo. It's ~110 rows — a database buys nothing here, and git gives us versioned history, review, and blame for free. Revisit only if this grows into a multi-marque catalog.

Proposed schema, one row per chassis code:

| column | meaning | example |
|--------|---------|---------|
| `chassis_code` | BMW chassis code | `F30` |
| `series` | Human label | `3 Series Sedan` |
| `us_start_year` | First US **model year sold** (not unveiling/production year) | `2012` |
| `us_end_year` | Last US model year sold | `2019` |
| `trims` | List of trims (make/model badges) sold in this chassis | `["320i","328i","330i","335i","340i"]` |
| `verified` | Has this row been spot-checked against the "sold year, not production year" standard? | `true` / `false` |
| `notes` | Free text (LCI splits, related bodystyles, caveats) | `F30 sedan; F31 wagon, F34 GT, F80 M3 tracked separately` |

- **Rule A** consumes `trims` × (`us_start_year`..`us_end_year`).
- **Rule B** consumes `[donor_trim]` × (`us_start_year`..`us_end_year`).
- The `verified` flag matters: only a subset of the existing ~110-row spreadsheet was checked against the US-sold-year standard; the rest came from one retailer's chart that mixes conventions. Treat `verified=false` rows as provisional and spot-check before mass-pushing.

**Maintenance (Open Question #4):** new model years / chassis are rare (a few per year). Keeping this as a reviewed file means updates are a small PR, not a data-migration. Recommend an annual review pass plus ad-hoc edits when BMW introduces a chassis.

---

## 5. eBay API integration — the findings that matter

All facts below are from eBay developer docs / SDK mirrors; items I could not fully verify from docs are flagged as **[verify in Sandbox]**.

### 5.1 THE critical gotcha — Inventory API vs Trading API (design around this first)

> **✅ RESOLVED 2026-08-13 — Path A.** A live production SKU (`5978`) returned HTTP 200 from
> `getInventoryItem`, so Dismantly's listings are genuine Inventory-API inventory items. Path B (the
> Trading/UI world requiring `bulkMigrateListing`) does **not** apply. The rest of this section is retained
> as background; the two-paths decision below is settled in favor of **(A)**, and uniformity is confirmed
> (5/5 real SKUs returned 200). Remaining: the §5.2 relist-persistence check. Note `25710` is ambiguous
> between "Trading listing" and "SKU doesn't exist" — always test the exact Custom Label.

eBay has two separate listing worlds, and the compatibility endpoint only reaches one of them:

- `createOrReplaceProductCompatibility` is `PUT /sell/inventory/v1/inventory_item/{sku}/product_compatibility`. It targets an **Inventory-API inventory item**, keyed by SKU in the URL.
- **A listing created via the Trading API or the eBay web UI is NOT an inventory item.** `getInventoryItem` on such a SKU returns **error 25710 ("we didn't find the resource")**. The compatibility endpoint literally has nothing to target.
- Trading listings *can* carry a SKU (`Item.SKU`), but that's a Trading-side tracking value — **not** an Inventory-API inventory item. Do not conflate them.

**Why this is the whole ballgame:** most third-party listing tools historically drive eBay through the Trading API. **If Dismantly does, then `createOrReplaceProductCompatibility` will not work on any Dismantly listing** until each is migrated into the Inventory model via `bulkMigrateListing` (which creates the inventory item + offer, and carries over any existing Trading `ItemCompatibilityList`).

**Two viable paths depending on what the test shows:**
- **(A) Dismantly listings ARE inventory items** → clean path: set compatibility by SKU, done.
- **(B) Dismantly listings are Trading/UI listings** → either (i) `bulkMigrateListing` them into the Inventory model first (heavier, and may conflict with Dismantly's own management), or (ii) set compatibility the **Trading way** via `ReviseItem` + `Item.ItemCompatibilityList` on the live Item ID — which *does* let you add compatibility to an existing listing, but is **Item-ID-scoped, so it will NOT survive Dismantly's relist** and must be re-applied every ~40–60 day cycle.

**→ Required test before building (see §8):** pick one real Dismantly listing, get its SKU, call `getInventoryItem`. Resolves → Path A. 25710 → Path B. This single call decides the entire architecture.

### 5.2 Persistence across relist (Open Questions #2 & #3)

> **✅ RESOLVED LIVE 2026-08-15 — SURVIVES; one-time push per SKU is viable.** Real test on SKU `1194`:
> wrote 10 vehicles → ended in Dismantly → resent to eBay (new item `407144851193`). Reading **both**
> compatibility stores on the new item (`ebay_inspect.py`):
> - **Inventory store** (`getProductCompatibility`, by SKU) = **0 vehicles**
> - **Trading store** (`GetItem` `ItemCompatibilityList`, by ItemID) = **10 vehicles — our exact expansion**,
>   and it's what the listing page displays and what buyers search.
>
> So the compatibility **did not get cleared** — it was **carried forward** into the new item at the Trading
> level. **Mechanism (corrected):** this is **eBay's native relist copy behavior**, NOT Dismantly — Dismantly
> never knew about our fitment (we wrote straight to eBay, outside Dismantly). Dismantly's end-and-resend is
> an eBay relist under the hood, and eBay copies the item's `ItemCompatibilityList` to the new item
> regardless of who set it. `getProductCompatibility` just can't see the Trading store, which is why it read 0.
> **=> Push once per SKU; it persists across relists. This is a one-time backfill, not a recurring re-apply.**
>
> **Honest caveat — observed once, NOT fully understood.** This result *contradicts* our own §5.4 research,
> which said Trading relists mint a new Item ID and do **not** carry compatibility forward. Yet it did carry.
> So either that behavior is more nuanced, or Dismantly's "resend" isn't a plain relist. We have **one**
> positive data point, against documented behavior that predicts the opposite — so treat persistence as
> **observed, not guaranteed.** Before trusting it at scale: (a) repeat the end-resend test on 2–3 more SKUs
> and across a real ~40–60 day Dismantly cycle (not just a manual resend), and (b) build the pipeline as
> **one-time push + a periodic AUDIT** — Trading-read a sample of pushed SKUs, re-push any that lost fitment.
> The audit makes us correct whether persistence turns out reliable, flaky, or path-dependent.
>
> **Two consequences for the batch runner (§8):**
> 1. **"Already done?" and donor detection must read the TRADING store** (`GetItem` `ItemCompatibilityList`),
>    NOT `getProductCompatibility` — the Inventory read returns 0 after any relist and would falsely say
>    "needs fitment." Best: keep our **own ledger of pushed SKUs** (push new ones once) + periodic Trading-read
>    audit to confirm persistence.
> 2. **`ebay_writer.py --detect` currently reads the Inventory store** for the donor, so it works on a
>    never-relisted listing but returns nothing on a relisted one. Add a Trading-store fallback so `--detect`
>    finds the donor either way.

- **Inventory API (as documented):** compatibility is stored on the **inventory item (SKU)** and applied at
  `publishOffer`. The docs suggested a same-SKU relist *might* keep it — **the live test above shows Dismantly's
  end-and-resend does NOT** (it recreates the inventory record fresh).
- **Trading API:** compatibility lives on the **Item ID**. A relist mints a new Item ID and does **not** carry compatibility forward — must be re-applied every cycle.

**So the "one-time vs continuous" answer (Open Question #3) is:**
- Path A (Inventory, same SKU reused) → **effectively one-time per genuinely new SKU**, plus a light watcher to catch brand-new SKUs. **[verify in Sandbox]**
- Path B via ReviseItem (Trading) → **continuous**: must re-apply on every relist, indefinitely.

This is why §5.1's test isn't just a detail — it determines whether this is a one-time backfill or a permanently-running system.

### 5.3 Validation behavior — confirms the phantom-year decision (Open Question #1)

> **✅ CONFIRMED LIVE 2026-08-15.** First real write succeeded (SKU `1194`, an S58 M2 starter):
> `createOrReplaceProductCompatibility` returned **HTTP 200** and set the valid rows while returning
> **warning `25023`** for the invalid ones (`[2023-2026][BMW][M2 CS]`) — partial acceptance, exactly as
> assumed. The M2 rows applied; the phantom `M2 CS` rows were dropped. The year-padding premise holds on
> real data. Also learned: **M performance suffixes (Competition/CS/CSL/GTS) are eBay *Trims*, not Models**
> — the base M Model already covers them, so `fitment_rules.ebay_model()` now collapses them (e.g. `M2 CS`
> → Model `M2`) to avoid the rejected rows. **The relist-persistence check (§5.2) is now armed on SKU 1194:
> re-read its compatibility after Dismantly's next relist to confirm it survives.**

- Request body is a `Compatibility` object → `compatibleProducts[]` → each `CompatibleProduct` has `compatibilityProperties[]` = `NameValueList` (`name`/`value`) pairs. Canonical aspect names: `Make`, `Model`, `Year`, `Trim`, `Engine`.
- For categories supporting "parts compatibility by specification," eBay **validates the combination**. On a bad combo it uses **partial acceptance**: invalid rows are reported in the response's errors/warnings node and dropped; **valid rows are kept and the call succeeds** as long as ≥1 row is valid.
- **→ Sending a padded year range is safe.** The phantom years (e.g. 2016–2019 335i) come back as warnings; the real years go through. No trim table, no pre-trimming required.
- Optional hardening: the **Metadata API** `getCompatibilitiesBySpecification` returns valid combinations for a category — use it to pre-validate (or to *generate* exact rows) if we ever want zero warning noise.

### 5.4 Also worth knowing

- **Adding compatibility to a live Trading Item:** `ReviseItem` / `ReviseFixedPriceItem` with `Item.ItemCompatibilityList` adds compatibilities to an existing Item by Item ID; duplicates are ignored; `ReplaceAll=true` wipes. Restriction: an item with bids or ending within 12h can still *add* but not *delete* compatibilities. (This is the Path-B mechanism.)
- **Rate limits:** compatibility calls fall under general Sell Inventory limits (account-specific; commonly a large daily ceiling). Read real numbers from Analytics API `getRateLimits` rather than assuming. **User** OAuth tokens have far higher allowances than application tokens — and the Inventory API requires a user token anyway.
- **`createOrReplaceProductCompatibility` returns HTTP 201** (Created) on the first write to a SKU, **200/204** on replace. All three are success — the runner treats `{200,201,204}` as pushed (a 201 was briefly mis-logged as an error in the first live run; fixed).

---

## 6. Remaining decisions & risks

### 6.1 Rule A vs Rule B classification (the one judgment step left)

The pipeline is fully mechanical *except* deciding whether a listing is an engine part (Rule B) or not (Rule A). **This is solved entirely by eBay's own category tree** — no keyword matching. (Dismantly's category assignments are clean and consistent across the catalog, so the listing's category is a reliable single signal; the keyword heuristic that earlier drafts kept as a backstop has been dropped.)

**The signal — eBay category ID (the listing already has one):**
- eBay's category taxonomy is queryable via the **Taxonomy API** (`getCategoryTree` / `getCategorySubtree`). For US Motors parts, use **`category_tree_id = 100`** (the Motors / Parts & Accessories vertical — a separate tree from the main marketplace).
- The engine branch has a stable ID: **`33612` — "Car & Truck Engines & Engine Parts."** That subtree and its leaf children are the core Rule-B set.
- **Every listing already carries its `categoryId`** (Trading `GetItem` → `PrimaryCategory.CategoryID`; Inventory `offer.categoryId`). Classification is then pure set-membership: listing's category ∈ engine set → Rule B, else Rule A.

**One-time curation (the only judgment, done once — not per listing):** eBay's tree gives the *structure*; we draw the Rule-A/Rule-B line on it once. The `33612` subtree is unambiguous; the fuzzy edge is "engine *accessory*" — decide once whether turbochargers (Auto Performance Engine & Components ~`171112`), fuel-injection, and cooling-system branches count as Rule B. That's a decision over ~20–40 category IDs, stored as a set alongside the reference data — after which classification is a lookup.

**Only fallback:** if a listing somehow has no usable/mappable category (expected to be near-zero given Dismantly's clean assignments), default it to **Rule B** (narrower) — over-narrowing loses a little reach but never creates a wrong-engine false positive. No keyword logic involved.

**Dependency (accepted):** classification is exactly as good as Dismantly's category assignment. Confirmed clean and consistent across the catalog, which is what lets us rely on category alone. Still worth a quick early look at the category distribution to confirm nothing lands in a generic "Other Parts" bucket at scale.

**Implementation (2026-08-15):** `scripts/ebay_fetch_categories.py` pulls eBay's Motors parts subtree
(Taxonomy API `getCategorySubtree`, tree 100) → `data/ebay_motors_categories.json`.
`data/rule_b_categories.json` is the one judgment artifact: engine-branch ancestor IDs to **include** as
Rule B (seeded with `33612`), plus candidate accessory branches (turbos, fuel/intake, ignition, belts,
cooling, exhaust) to decide include/exclude, and an `exclude_ids` override. `scripts/classify_part.py`
then classifies any `categoryId` by ancestry — Rule B if under an included engine branch, else Rule A,
default Rule B when a category is unknown. Pure lookup; no keyword matching.

### 6.2 Data quality

Only a subset of the ~110 chassis rows are verified against the US-sold-year standard. Spot-check `verified=false` rows before a mass push; the padding tolerance and eBay's broad-fitment model make small year errors low-consequence, but a wholesale-wrong chassis range is not.

### 6.3 Interaction with Dismantly's own management

If we go Path A via `bulkMigrateListing`, migrating a listing into the Inventory model may change how (or whether) Dismantly can continue to manage/relist it. **[verify]** Do not migrate at scale until we've confirmed one migrated listing still behaves under Dismantly's relist cycle.

### 6.4 Future enhancement (gated): cross-engine fanning for Rule B

Rule B currently restricts engine-part fitment to trims **within the donor's chassis** — the conservative,
in-scope behavior (a 2024 M2 starter → M2 + M2 CS only). But BMW shares an engine across many models, and
`data/bmw_engine_map.json` is keyed by **engine family across all chassis**, so a broader Rule B is a small,
ready toggle: fan to **every `(chassis, trim)` sharing the donor's engine family**. Example: an S58 starter
→ G87 M2/M2 CS **+ G80 M3 + G82/G83 M4 + F97 X3 M + F98 X4 M**. This is genuinely correct for many engine
parts (starters, alternators, coils, sensors, water pumps, oil pumps) and is a high-value reach increase.

Implementation: one flag in `fitment_rules.expand` — instead of iterating `row["trims"]`, iterate all engine-map
entries whose `engine_code` matches the donor's, emit each with its chassis's year range and correct eBay
Model/Trim (nameplate vs badge). **Caveats to weigh when enabling:** (a) some "engine" parts are actually
chassis-packaged (oil pan, certain brackets, exhaust manifolds shaped to the bay) and don't cross bodies —
may want a sub-list of "engine-family-safe" categories vs "engine-but-chassis-specific"; (b) higher reach =
slightly more false-positive risk, though within eBay's broad-fitment tolerance. **Owner flagged this as a
smart, easy add — revisit once the one-SKU live write + relist-persistence are confirmed.**

---

## 7. eBay developer account setup (Open Question #5)

1. Register a free developer account at developer.ebay.com.
2. Create an **application keyset** — you get separate **Sandbox** and **Production** keysets (App ID / Client ID, Cert ID / Client Secret, Dev ID).
3. Complete the **compliance / verification step** (accept the API License Agreement; for Production, the application-check/business details) — **Production keys don't activate until this is done.**
4. Generate a **user OAuth token** carrying scope `https://api.ebay.com/oauth/api_scope/sell.inventory`, authorizing calls against the seller's own account. (Inventory API needs a *user* token, not an application/client-credentials token.)
5. Do all first testing in **Sandbox**, then flip to Production keys.

### 7.1 Two token modes (manual vs. auto-refresh)

**Manual (temporary):** paste a fresh user access token into `token.txt`. These expire in **~2 hours**, so a
full 15k sweep needs re-pasting a few times. The runner preflights the token and **stops loud** on 401 (it no
longer masquerades as "no published offer"); on stop, refresh `token.txt` and re-run — the ledger resumes.

**Auto-refresh (ongoing — no more pasting).** eBay's user-consent flow also returns a **refresh token**
(valid ~18 months). With it plus the app's Client ID/Secret, `scripts/ebay_auth.py` mints a fresh 2-hour
access token on demand (cached in `.ebay_token_cache.json`, re-minted mid-run near expiry). Runner uses it
automatically when configured, else falls back to `token.txt`.

One-time setup:
1. In the developer portal, get your **App ID (Client ID)** and **Cert ID (Client Secret)** from the
   production keyset.
2. Run the **OAuth user-consent flow** (portal "Get a User Token", or the auth-code flow) with scope
   `sell.inventory`, log in as the seller, and capture the **refresh token** it returns (not just the access
   token). The redirect-URL scopes must include everything the runner needs (Inventory writes + the Trading
   `GetItem` read the guard uses).
3. Create **`ebay_auth.json`** (gitignored) in the repo root:
   ```json
   {"client_id":"App-ID","client_secret":"Cert-ID","refresh_token":"v^1.1#...",
    "scopes":["https://api.ebay.com/oauth/api_scope/sell.inventory"]}
   ```
   or set `EBAY_CLIENT_ID` / `EBAY_CLIENT_SECRET` / `EBAY_REFRESH_TOKEN` as env vars.
4. Verify: `python3 scripts/ebay_auth.py --check` → "OK - minted an access token…".

### 7.2 Scheduled sweep (`.github/workflows/fitment-sweep.yml`)

A daily GitHub Actions job refreshes the Shopify donor dump (`shopify_donor.py --dump`, needs a Shopify Admin
`read_products` token) and runs `ebay_batch.py apply --from-shopify --from-inventory --live`, then commits the
updated dump + ledger back. After the first full sweep this is **cheap**: ledgered SKUs skip before any eBay
read, so each run only processes genuinely **new** listings — new inventory auto-expands within a day.
Required repo secrets: `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET`, `EBAY_REFRESH_TOKEN`, `SHOPIFY_STORE`,
`SHOPIFY_TOKEN`. (Until the Shopify token exists, the dump can be refreshed on demand via the Admin API
connection; the eBay push half runs on the refresh token alone.)

---

## 8. Recommended next steps (in order)

1. ~~**Run the one decisive test (§5.1).**~~ **✅ DONE 2026-08-13 — Path A** (SKU `5978` → HTTP 200,
   Inventory-API managed). Project is sized for the one-time-push architecture.
2. ~~**Confirm uniformity.**~~ **✅ DONE 2026-08-13 — 5/5 SKUs returned Path A.** (Optionally spot-check a
   couple more across very different part categories if you want extra assurance, but this is convincing.)
3. **Confirm relist persistence (§5.2):** verify compatibility set on a SKU survives Dismantly's end/relist
   cycle. Cleanest real-world check — set compatibility on one test SKU (needs `sell.inventory` *write*
   scope), record it, and re-read after the next relist (~40–60 days); or reason it out from whether
   Dismantly reuses the same inventory item vs. recreating it.
4. ~~**Lock the reference data (§4).**~~ **✅ BUILT 2026-08-15** — `data/bmw_chassis_reference.json` (112
   US chassis), `data/ebay_bmw_models.json` (eBay's 269 Models, authoritative output vocab), and
   `data/bmw_engine_map.json` (397 `(chassis,trim)→engine` rows). Owner spot-check ongoing (F26 M40i and
   E84 xDrive28i confirmed).
5. ~~**Build the rule engine.**~~ **✅ BUILT 2026-08-15** — `scripts/fitment_rules.py` expands a donor into
   eBay-shaped rows: Rule A = chassis family (badge Models for sedans, nameplate Model for X/i/Z);
   Rule B = engine-map-driven, restricting to the donor's engine family (e.g. F30 335i→N55 also tags the
   N55 ActiveHybrid 3; G05 X5 xDrive40i→B58 tags all B58 X5 trims via Model=X5 + Trim, excludes the V8
   M50i/M60i). Validates emitted Models against eBay's catalog list.
6. ~~**Decide classification (§6.1).**~~ **✅ DONE 2026-08-15** — category tree fetched
   (`data/ebay_motors_categories.json`); Rule-B engine branches locked in `data/rule_b_categories.json`:
   `33612` Engines, `33549` Air & Fuel Delivery (incl. turbos), `33687` Ignition, `33599` Engine Cooling
   (all, incl. radiators), `262059` Accessory Belts, `33572` Starters/Alternators/ECUs/Wiring. Exhaust,
   A/C & Heating, EV/Hybrid parts, and everything else = Rule A. `scripts/classify_part.py` labels any
   categoryId by ancestry.
7. ~~**Build the writer.**~~ **✅ BUILT 2026-08-15 (dry-run verified)** — `scripts/ebay_writer.py` turns
   generated rows into a `createOrReplaceProductCompatibility` PUT payload. Dry-run by default (prints the
   exact URL + JSON body); `--live` writes. `--detect` reads the donor vehicle + category off a live
   listing; `--category` auto-classifies Rule A/B. **Remaining to go live:** (a) a token with the
   `sell.inventory` **write** scope (a one-line scope change vs. the read-only tokens used so far);
   (b) the relist-persistence check — write compatibility to one real SKU, confirm it survives Dismantly's
   relist. That live one-SKU write is the safest first production test. **✅ Both done: SKU 1194 written
   live (HTTP 200) and survived a relist (§5.2).**
8. ~~**Build the batch runner.**~~ **✅ BUILT 2026-08-15** — `scripts/ebay_batch.py`. `plan` (default) is a
   dry-run producing `data/batch_plan.csv`; `apply --live` writes and records a `data/pushed_ledger.json`
   (skips done SKUs, resumable); `audit` Trading-reads pushed SKUs and re-pushes any that lost fitment.
   Reads donor + state from the **Trading** store (survives relist), writes via the Inventory API. Handles
   badge and nameplate (X/i/Z) donors; ambiguous/edge donors go to a `review` bucket instead of a bad push.
   **Remaining = operate it:** dry-run a small batch → review CSV → `apply --live` in batches → scale →
   periodic `audit`. Optionally schedule it (GitHub Actions) to catch genuinely-new SKUs.

---

## Appendix: answers to the five open questions

1. **Rule B precision / trim table?** No table. Reuse chassis year range; eBay drops phantom years (partial-acceptance validation). Trim ≠ needed because for engine parts the donor's trim already = the engine.
2. **Does compatibility survive relist?** **Yes — confirmed live 2026-08-15 (§5.2).** Dismantly carries it forward to the new item at the Trading level (10/10 vehicles survived). `getProductCompatibility` reads 0 post-relist only because the surviving copy is in the Trading store, not the SKU store.
3. **One-time or continuous?** **One-time push per SKU.** It persists across relists. Batch runner tracks a ledger of pushed SKUs and reads the **Trading** store (not `getProductCompatibility`) to check state.
4. **How to store the reference data?** Single versioned CSV/JSON in-repo, one row per chassis; annual + ad-hoc review. No database needed at this size.
5. **eBay API setup?** Dev account → keyset → compliance step → user OAuth token with `sell.inventory`. Sandbox first. See §7.
