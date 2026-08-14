# Part-Number-Driven Fitment (Approach 2)

A second, independent approach to the same goal as the main project: getting accurate
vehicle-fitment (compatibility) onto our eBay listings.

- The **main project** (top-level `docs/`, `scripts/`, `data/`) does this with **BMW
  chassis-code rules**.
- **This folder** does it from **your own historical data**: it learns which vehicles a
  part fits by looking at every car that part has actually come off of, then applies that
  to any listing carrying the same part number.

Nothing here touches the main project. They share one thing — the main project's
`data/ebay_bmw_models.json` (eBay's official BMW model list), which this pipeline reuses
to make sure the fitment we push matches eBay's catalog.

## The idea in one line

**If a part number shows up on 12 different cars in our history, that part fits those 12
cars — so add that fitment to every listing with that part number.**

## The pipeline (4 steps)

```
Inventory.xlsx ──①──> fitment_by_partnumber.csv ──②──> listing_fitment_to_add.csv
                                                          │
                                    eBay listings CSV ────┘
                                                          │
                        ③ reconcile models to eBay's catalog
                                                          ▼
                                   ebay_ready_fitment.csv ──④──> eBay (Trading API)
```

| # | Script | What it does |
|---|--------|--------------|
| 1 | `scripts/build_fitment_table.py` | Reads the inventory workbook (Sold + Small Parts), aggregates every part number → the set of vehicles it's been seen on. |
| 2 | `scripts/enrich_listings.py` | For each live listing, matches its part number and lists the vehicles to add. |
| 3 | `scripts/reconcile_models.py` | Rewrites each model to eBay's exact catalog spelling (e.g. `340xi` → `340i xDrive`, `X5M` → `X5`). |
| 4 | `scripts/push_to_ebay.py` | Builds the eBay compatibility request and (with `--live` + a token) applies it. **Dry-run by default.** |

`scripts/fitment_common.py` holds the shared part-number logic.

## The part-number rule (BMW)

BMW part numbers are 11 digits, but the **last 7** are what's on the part and what people
use. The pipeline keys everything on the **7-digit** form and keeps any 11-digit forms
alongside, so a listing matches whether it carries the 7- or 11-digit number. A single
cell may hold several numbers (`2284132, 2284137`) and BMW spacing/dashes
(`63.21-8 383 099`) — all handled.

## How to run it

```bash
cd spreadsheet-fitment/scripts

# 1. build the table from your inventory export
python3 build_fitment_table.py --inventory /path/to/Inventory.xlsx \
    --out ../data/fitment_by_partnumber.csv

# 2. match it to your eBay listings export
python3 enrich_listings.py --listings /path/to/listings.csv \
    --table ../data/fitment_by_partnumber.csv --out ../data/listing_fitment_to_add.csv

# 3. reconcile models to eBay's catalog
python3 reconcile_models.py --in ../data/listing_fitment_to_add.csv \
    --catalog ../../data/ebay_bmw_models.json --out ../data/ebay_ready_fitment.csv

# 4. preview what would be pushed (nothing is sent)
python3 push_to_ebay.py --in ../data/ebay_ready_fitment.csv
#    ...and when ready, with an eBay token (see ../../docs/EBAY_SETUP.md):
python3 push_to_ebay.py --in ../data/ebay_ready_fitment.csv --live
```

Requires `openpyxl` (`pip install openpyxl`). The generated `.csv` files and any raw
source data are **git-ignored** (they hold business data and are reproducible).

## Known limits (see `docs/DESIGN.md`)

- **Year-within-model** isn't pre-validated — eBay checks that a model existed in a given
  year at submit time and rejects invalid pairs; step 4 reports those per listing.
- **M-trim on SUVs** (`X5M`) is mapped to the base model (`X5`); the "M" distinction is
  dropped, which can over-broaden a genuinely M-only part.
- The table trusts the source data: a part logged on the wrong donor once will carry that
  vehicle. High volume averages this out.
