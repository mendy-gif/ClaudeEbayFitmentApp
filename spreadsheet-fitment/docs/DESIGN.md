# Design — Part-Number-Driven Fitment

## Goal

Same end goal as the main rule-based project — accurate vehicle compatibility on eBay
listings — but learned from **our own historical part usage** instead of chassis-code
rules.

## Why this approach exists alongside the rule-based one

- **Rule-based (main project):** fast, hands-off, only as right as the rules.
- **Part-number-driven (this folder):** learns from real data. Every time a part came off
  a car, that's evidence the part fits that car. Aggregate the evidence per part number
  and you get a fitment table grounded in what we've actually handled.

They're complementary: this can confirm or correct the rules, and covers oddball parts the
rules miss.

## Data model

**Observation:** one (part number, vehicle) pair, extracted from an inventory row.
**Fitment table:** `part_number_7 -> { (year, make, model), ... }` — the union of all
vehicles observed for that part. 11-digit forms are retained for matching.

Join key is the **7-digit** part number (the universal form). See README for the
part-number rule and cell-parsing details (multi-part cells, BMW spacing).

## Pipeline

1. **build_fitment_table.py** — inventory workbook → `fitment_by_partnumber.csv`.
   Sheets `Small Parts` + `Sold`; part columns `Part #` / `Part # (2)`; vehicle from the
   `Car` column (`2014 BMW 228i`).
2. **enrich_listings.py** — listings CSV → `listing_fitment_to_add.csv`. Matches on
   `oeoempartnumber` (7-digit) + `manufacturerpartnumber` (11-digit).
3. **reconcile_models.py** — → `ebay_ready_fitment.csv`. Maps each model to eBay's
   catalog string using `../../data/ebay_bmw_models.json`.
4. **push_to_ebay.py** — → eBay Trading API `ReviseFixedPriceItem` with an
   `ItemCompatibilityList`, keyed by listing SKU. Dry-run by default.

## eBay-catalog reconciliation (step 3, the crux)

eBay validates fitment against its own vehicle catalog; free-text like `340xi` or `X5M`
is rejected. Mapping rules, applied in order:

| Rule | Example |
|------|---------|
| exact (case/space normalize) | `535I` → `535i` |
| `###xi` → `###i xDrive` | `340xi` → `340i xDrive` |
| `M###xi` → `M###i xDrive` | `M240xi` → `M240i xDrive` |
| `X#M` → base `X#` (M becomes a Trim, dropped here) | `X5M` → `X5` |
| small manual table | `M550i` → `M550i xDrive`, `750xi` → `750i xDrive` |

On the current data this reconciles 100% of matched rows to a valid eBay Model.

## Known limits / next work

- **Year-within-model validation.** Reconciliation validates the Model name, not that the
  model existed in the row's year. eBay enforces this at submit; step 4 reports per-listing
  rejections. A pre-check could call the Taxonomy API `getCompatibilityPropertyValues`
  filtered by year and drop invalid pairs before pushing.
- **Trim precision.** We push Year/Make/Model only. M-SUV parts mapped to the base model
  can over-broaden; adding Trim/Engine would tighten this but needs valid eBay trim strings.
- **Data trust.** A part mis-logged on the wrong donor injects a wrong vehicle; volume
  dilutes it. A "seen only once" flag could mark low-confidence entries.
- **Auth.** `--live` needs an eBay OAuth user token with the sell scope — reuse the setup
  in the main project's `docs/EBAY_SETUP.md`.
