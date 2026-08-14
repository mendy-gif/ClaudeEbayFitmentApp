# BMW Chassis-Family Fitment — Design & Decisions

**Status:** Design agreed on core approach; one load-bearing test still required before building.
**Last updated:** 2026-08-13
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
| 3 | One-time push or continuously running? | **Leaning one-time push per SKU.** Decision 2's precondition is now **confirmed** (see §5.1 test result): Dismantly's listings are Inventory-API items. A light watcher still catches genuinely new SKUs. |
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

- **Inventory API:** compatibility is stored on the **inventory item (SKU)**, independent of any offer, and is applied to the listing at `publishOffer` time. So a new offer/relist for the **same SKU** picks up the existing compatibility — **you don't re-send per relist.** **[verify in Sandbox]** — the docs describe SKU-level storage + apply-at-publish, but don't contain a single sentence literally guaranteeing survival across delete-offer/create-new-offer. Confirm once.
- **Trading API:** compatibility lives on the **Item ID**. A relist mints a new Item ID and does **not** carry compatibility forward — must be re-applied every cycle.

**So the "one-time vs continuous" answer (Open Question #3) is:**
- Path A (Inventory, same SKU reused) → **effectively one-time per genuinely new SKU**, plus a light watcher to catch brand-new SKUs. **[verify in Sandbox]**
- Path B via ReviseItem (Trading) → **continuous**: must re-apply on every relist, indefinitely.

This is why §5.1's test isn't just a detail — it determines whether this is a one-time backfill or a permanently-running system.

### 5.3 Validation behavior — confirms the phantom-year decision (Open Question #1)

- Request body is a `Compatibility` object → `compatibleProducts[]` → each `CompatibleProduct` has `compatibilityProperties[]` = `NameValueList` (`name`/`value`) pairs. Canonical aspect names: `Make`, `Model`, `Year`, `Trim`, `Engine`.
- For categories supporting "parts compatibility by specification," eBay **validates the combination**. On a bad combo it uses **partial acceptance**: invalid rows are reported in the response's errors/warnings node and dropped; **valid rows are kept and the call succeeds** as long as ≥1 row is valid.
- **→ Sending a padded year range is safe.** The phantom years (e.g. 2016–2019 335i) come back as warnings; the real years go through. No trim table, no pre-trimming required.
- Optional hardening: the **Metadata API** `getCompatibilitiesBySpecification` returns valid combinations for a category — use it to pre-validate (or to *generate* exact rows) if we ever want zero warning noise.

### 5.4 Also worth knowing

- **Adding compatibility to a live Trading Item:** `ReviseItem` / `ReviseFixedPriceItem` with `Item.ItemCompatibilityList` adds compatibilities to an existing Item by Item ID; duplicates are ignored; `ReplaceAll=true` wipes. Restriction: an item with bids or ending within 12h can still *add* but not *delete* compatibilities. (This is the Path-B mechanism.)
- **Rate limits:** compatibility calls fall under general Sell Inventory limits (account-specific; commonly a large daily ceiling). Read real numbers from Analytics API `getRateLimits` rather than assuming. **User** OAuth tokens have far higher allowances than application tokens — and the Inventory API requires a user token anyway.

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

### 6.2 Data quality

Only a subset of the ~110 chassis rows are verified against the US-sold-year standard. Spot-check `verified=false` rows before a mass push; the padding tolerance and eBay's broad-fitment model make small year errors low-consequence, but a wholesale-wrong chassis range is not.

### 6.3 Interaction with Dismantly's own management

If we go Path A via `bulkMigrateListing`, migrating a listing into the Inventory model may change how (or whether) Dismantly can continue to manage/relist it. **[verify]** Do not migrate at scale until we've confirmed one migrated listing still behaves under Dismantly's relist cycle.

---

## 7. eBay developer account setup (Open Question #5)

1. Register a free developer account at developer.ebay.com.
2. Create an **application keyset** — you get separate **Sandbox** and **Production** keysets (App ID / Client ID, Cert ID / Client Secret, Dev ID).
3. Complete the **compliance / verification step** (accept the API License Agreement; for Production, the application-check/business details) — **Production keys don't activate until this is done.**
4. Generate a **user OAuth token** carrying scope `https://api.ebay.com/oauth/api_scope/sell.inventory`, authorizing calls against the seller's own account. (Inventory API needs a *user* token, not an application/client-credentials token.)
5. Do all first testing in **Sandbox**, then flip to Production keys.

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
6. **Decide classification (§6.1):** pull the distinct eBay categories across the catalog and build the Rule-B category set (anchored on `33612`).
7. **Then build** the apply pipeline (rule engine → eBay writer `createOrReplaceProductCompatibility`), sized for Path A / one-time push. Needs the `sell.inventory` *write* scope.

---

## Appendix: answers to the five open questions

1. **Rule B precision / trim table?** No table. Reuse chassis year range; eBay drops phantom years (partial-acceptance validation). Trim ≠ needed because for engine parts the donor's trim already = the engine.
2. **Does compatibility survive relist?** **Path A confirmed (2026-08-13)** — listings are Inventory-API items, so compatibility is SKU-scoped storage and should survive a same-SKU relist. Persistence across an actual Dismantly relist still to be observed (§5.2 / step 3).
3. **One-time or continuous?** **Path A → ~one-time per new SKU + a light watcher.** (Confirmed via the §5.1 test.)
4. **How to store the reference data?** Single versioned CSV/JSON in-repo, one row per chassis; annual + ad-hoc review. No database needed at this size.
5. **eBay API setup?** Dev account → keyset → compliance step → user OAuth token with `sell.inventory`. Sandbox first. See §7.
