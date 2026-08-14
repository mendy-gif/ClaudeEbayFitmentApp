# Handoff: build the COMBINED fitment tool (for the sister Claude)

**From:** the Claude on branch `claude/ebay-fitment-structure-bmorc4` (holds Approach 2).
**To:** the Claude on branch `claude/ebay-fitment-chassis-rules-3pztn4` (holds the more-developed Approach 1 engine).
**Goal:** build ONE tool that sets eBay fitment from **both** methods at once.

---

## 0. The one hard requirement

For every listing, the combined tool must apply the **UNION** of both fitment sources —
**not** a fallback, **not** "prefer one." Both always run, results are merged:

- **Approach 1 (chassis rules):** broad BMW chassis-family fitment, derived from the
  listing's **donor vehicle**.
- **Approach 2 (part-number history):** exact fitment derived from the **part number** —
  every vehicle that part has historically come off of, from the seller's inventory
  workbook.

Final per-SKU fitment = `set(Approach 1 rows) ∪ set(Approach 2 rows)`, deduped, reconciled
to eBay's catalog vocabulary, pushed once. eBay silently drops any row it can't validate
(partial-accept warning 25023), so over-including from both sources is safe.

---

## 1. Critical context (read before coding)

1. **Approach 2's code exists only on the OTHER branch** (`ebay-fitment-structure-bmorc4`),
   in the top-level `spreadsheet-fitment/` folder. Your branch does **not** have it yet.
   Pull it in first (see §5, Step 0).
2. **eBay is unreachable from the Claude session** — `api.ebay.com` returns 403 by network
   policy. You author code + commit; the **human runs it in a GitHub Codespace**, where eBay
   is reachable. (Same finding you documented in `EBAY_ACCESS_NOTE.md`.)
3. **Path A is confirmed:** Dismantly's listings are Inventory-API items reachable by SKU
   (`getInventoryItem` → HTTP 200 on 5/5 real SKUs, `docs/DESIGN.md` §2). So compatibility
   can be set by SKU via `createOrReplaceProductCompatibility`, one-time push, survives relist.
4. **SKU is the shared key.** Approach 1 calls it `sku`; Approach 2 calls it `guid`. Same
   value (eBay Custom Label, a bare number like `5978`).
5. **The source data is git-ignored business data** — `Inventory.xlsx` and the eBay
   `listings.csv` export are NOT in the repo. The human supplies them in the Codespace.

---

## 2. Approach 1 — chassis rules (you already have this)

- **Engine:** `scripts/fitment_rules.py` — donor `(year, model[, chassis])` + Rule A/B →
  rows shaped `{Year, Make, Model, Trim}` in eBay's Model vocabulary.
  - Rule A (body/most parts): whole chassis family × chassis year range.
  - Rule B (engine parts): donor's engine/trim only × chassis year range.
- **Orchestrator:** `scripts/ebay_batch.py` — sweeps live listings, reads each donor from the
  eBay compatibility list, classifies Rule A vs B (`scripts/classify_part.py`), expands, and
  pushes.
- **Push:** Sell Inventory API
  `PUT /sell/inventory/v1/inventory_item/{SKU}/product_compatibility`. Success = **200, 201,
  or 204** (201 on first write — your `ebay_batch.py` already handles this; note your
  `ebay_writer.py` still checks only 200/204 and should be updated to include 201, already
  fixed on the sibling branch).
- **Model vocabulary source of truth:** `data/ebay_bmw_models.json` (pulled from eBay
  Taxonomy API, `category_tree_id = 100`).

## 3. Approach 2 — part-number history (bring this over from the sibling branch)

**Idea:** "if a part number has come off 12 different cars in our history, it fits those 12
cars — add that fitment to every listing with that part number." Keyed on the **7-digit** BMW
part number (last 7 of the 11-digit OEM number).

**Location:** `spreadsheet-fitment/` on branch `ebay-fitment-structure-bmorc4`.

**Scripts:**
| Script | Role |
|--------|------|
| `spreadsheet-fitment/scripts/fitment_common.py` | `part_keys(raw)` → 7-digit keys from a messy cell (handles `2284132, 2284137`, BMW spacing `63.21-8 383 099`, drops alpha/wheel-size junk). `parse_car("2014 BMW 228i")` → `("2014","BMW","228i")`. |
| `.../build_fitment_table.py` | Step 1 — reads `Inventory.xlsx` (Sold + Small Parts sheets) → aggregates part# → set of vehicles. |
| `.../enrich_listings.py` | Step 2 — matches each live listing's part number to the table → rows to add. |
| `.../reconcile_models.py` | Step 3 — rewrites each raw model to eBay's exact spelling using `data/ebay_bmw_models.json`. |
| `.../push_to_ebay.py` | Step 4 — **RETIRE for the combined tool** (it pushes via Trading API `ReviseFixedPriceItem`; we standardize on the Inventory API instead). Its *table* is what's valuable, not its pusher. |

**Exact CSV schemas (so you can consume them directly):**

- `fitment_by_partnumber.csv` (Step 1 out):
  `part_number_7, part_number_11_forms, vehicle_count, vehicles`
  — `vehicles` is `;`-separated `"Year Make Model"` (e.g. `2014 BMW 328i;2013 BMW 335i`).
- `listing_fitment_to_add.csv` (Step 2 out):
  `guid, matched_part7, year, make, model, title` — one row per (listing, vehicle).
- `ebay_ready_fitment.csv` (Step 3 out — **this is the one to consume**):
  `guid, part7, year, make, raw_model, ebay_model, mapping_flag, title`
  — use `ebay_model`; skip rows where `mapping_flag == "UNMAPPED"`.

**Inputs the human must provide (Codespace):** `Inventory.xlsx` (historical inventory),
and the eBay `listings.csv` export (needs columns `guid, oeoempartnumber,
manufacturerpartnumber, title`). Both git-ignored.

---

## 4. The combined design (recommended)

**One orchestrator, one push, two fitment sources unioned per SKU.**

```
Inventory.xlsx ─┐
                ├─(Approach 2 steps 1-3)→ ebay_ready_fitment.csv ─┐
listings.csv ───┘                                                 │  per-SKU part# rows
                                                                  ▼
live eBay listing donor ─(Approach 1: fitment_rules Rule A/B)→ chassis rows ─┐
                                                                             ▼
                       ebay_batch.py  ──union(chassis ∪ part#) → dedupe → reconcile
                                                                             ▼
                          PUT /sell/inventory/v1/inventory_item/{SKU}/product_compatibility
                                          (accept 200 / 201 / 204)
```

**Concrete decisions already agreed with the user:**
1. **`ebay_batch.py` becomes the single combined tool** — the orchestrator that already sweeps
   live listings and pushes via the Inventory API.
2. **Standardize the push on the Sell Inventory API** `product_compatibility` (Path A
   confirmed, keyed by SKU, survives relist). Approach 2's Trading-API pusher is retired.
3. **Union, not fallback** — always merge both sources.

**Merge details to implement:**
- Add a `--partnumber-fitment <csv>` arg to `ebay_batch.py` pointing at
  `ebay_ready_fitment.csv`. Load it once into `{sku(guid): set((Year, Make, ebay_model))}`.
- Per SKU, after computing the chassis-rule rows, **union in** that SKU's part-number rows.
- **Dedupe on the full tuple.** Approach 1 rows carry a `Trim`; Approach 2 rows don't
  (Year/Make/Model only, which eBay reads as "all trims of that model/year"). Keep both;
  dedupe exact duplicates. A no-Trim row plus a Trim'd row for the same model/year is
  redundant-but-harmless (eBay drops invalids).
- **Unify model vocabulary.** Both sources must emit identical eBay Model strings. Approach 1
  uses `fitment_rules.ebay_model()` + `ebay_bmw_models.json`; Approach 2 uses
  `reconcile_models.py` against the same JSON. Confirm they agree (e.g. `340xi` →
  `340i xDrive`, `X5M` → `X5`); if they diverge, make Approach 2's `ebay_model` the input and
  don't re-map.
- Feed the unioned rows through the existing `rows_to_payload()` → one PUT per SKU.

---

## 5. Build steps for you (the sister)

**Step 0 — bring Approach 2 onto your branch:**
```bash
git fetch origin claude/ebay-fitment-structure-bmorc4
git checkout origin/claude/ebay-fitment-structure-bmorc4 -- spreadsheet-fitment
```
(Brings the whole `spreadsheet-fitment/` folder in without merging the rest.)

**Step 1 — run Approach 2's table build (human, in Codespace, needs the data files):**
```bash
cd spreadsheet-fitment/scripts
python3 build_fitment_table.py --inventory /path/to/Inventory.xlsx --out ../data/fitment_by_partnumber.csv
python3 enrich_listings.py --listings /path/to/listings.csv --table ../data/fitment_by_partnumber.csv --out ../data/listing_fitment_to_add.csv
python3 reconcile_models.py --in ../data/listing_fitment_to_add.csv --catalog ../../data/ebay_bmw_models.json --out ../data/ebay_ready_fitment.csv
```

**Step 2 — extend `ebay_batch.py`:**
- New arg `--partnumber-fitment ../spreadsheet-fitment/data/ebay_ready_fitment.csv`.
- Loader → `{guid: set((year, make, ebay_model))}`, skipping `mapping_flag == "UNMAPPED"`.
- In the per-SKU path (around the `rows_to_payload` call), union the part-number rows into the
  chassis-rule rows before building the payload. Keep the existing 200/201/204 success check.

**Step 3 — verify on ONE SKU, dry-run, then `--live`** (Codespace, `token.txt` with
`sell.inventory` write scope). Suggested test SKU: `42672`. Expect `HTTP 201` on first write.

---

## 6. Gotchas / decisions still open for the user

- **201 on first write** = success (not an error). Inventory API returns 201 the first time a
  SKU gets compatibility, 200 on overwrite, 204 on empty-body success. Accept all three.
- **Trim granularity mismatch** between the two sources — handled by full-tuple dedupe (§4).
  If the user later wants Approach 2 rows to also carry Trim, the part-number table would need
  a trim column (not currently built).
- **Token longevity for a full sweep** — a manual `token.txt` expires ~2h. A ~15k-SKU sweep
  needs the `ebay_auth.json` refresh-token helper (your branch's `scripts/ebay_auth.py`).
  Wire the combined `ebay_batch.py` to use it.
- **BMW-only scope** — both approaches are BMW-only; non-BMW donors are skipped. Unchanged.
- **`getInventoryItem` prereq** — combined push assumes Inventory-managed listings (Path A,
  confirmed). Any Trading-only SKU would need the `ReviseFixedPriceItem` path instead (not
  expected given the Path-A result).

---

## 7. What to ask the user for

1. The **`Inventory.xlsx`** historical workbook (which sheet names hold Sold + Small Parts).
2. The current eBay **`listings.csv`** export (with `guid, oeoempartnumber,
   manufacturerpartnumber, title`).
3. Confirmation to **standardize the push on the Sell Inventory API** (§4 decision 2).
