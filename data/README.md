# BMW Chassis Reference Data

The single reference table the fitment rules run off of (see `../docs/DESIGN.md`). One row per US-market
BMW chassis/body variant.

## Files
- **`bmw_chassis_reference.json`** — source of truth (edit this).
- **`bmw_chassis_reference.csv`** — flat export (regenerated).
- **`bmw_chassis_reference.xlsx`** — review-friendly spreadsheet with confidence color-coding.
- Regenerate CSV/XLSX after editing the JSON: `python3 ../scripts/build_reference.py`

## Columns
| column | meaning |
|--------|---------|
| `chassis_code` | BMW chassis code (M-cars that reuse a base code are suffixed, e.g. `F10 M5`) |
| `series` / `body_style` | human labels |
| `us_start_year` / `us_end_year` | **US model years *sold*** (first year at US dealers → last US year). Not global production/unveiling years. |
| `still_selling` | true if still sold new as of 2026 (then `us_end_year` = 2026, a moving target) |
| `trims` | every trim/model badge sold in the US under that chassis — what **Rule A** fans out to. **PROVISIONAL:** these are BMW badge names and must be reconciled to eBay's exact **Model** vocabulary before any push (eBay drops non-matching rows). See `docs/DESIGN.md` §3.1. Pull eBay's real BMW Model list with `scripts/ebay_fetch_bmw_catalog.py`. |
| `confidence` | research confidence: high / medium / low |
| `verified` | **`false` until a human spot-checks the row** — all rows start unverified |
| `notes` | US-specific caveats, engine-rename notes, exclusions |
| `sources` | reference URL(s) |

## How the rules consume it
- **Rule A (body/interior/most parts):** donor's chassis → add *all* `trims` × (`us_start_year`..`us_end_year`).
- **Rule B (engine parts):** donor's chassis → add *only the donor's own trim* × the same year range.

## Design decisions worth knowing (flag on review if you disagree)
1. **M-cars are their own rows**, not folded into the base chassis (e.g. `F80 M3` is separate from `F30`,
   `F10 M5` separate from `F10`). Rationale: M engine/suspension/body parts mostly do **not** interchange
   with the regular models, so fanning an M part across the base family would be a false positive. This is
   the safe choice for a broad-fitment layer. Downside: a shared M part (rare) won't fan to the base cars.
2. **Electric siblings that share a body are separate rows** (`G26 (i4)` vs `G26` 4-Series;
   `G60 (i5)` vs `G60` 5-Series; `G70 (i7)` vs `G70` 7-Series). Body panels are shared, but drivetrains
   differ entirely, so keeping them separate avoids nonsense engine-part fitment. Minor under-reach on
   shared body panels — acceptable for v1.
3. **US-only scope.** Non-US bodies are excluded (E30/E36/E46 Touring wagons, 1-Series hatchbacks,
   F11/G31 5-wagons, iX1/iX2/gen-1 iX3, etc.). Notable exclusions are recorded in row `notes`.
4. **xDrive/sDrive and diesel are badges within a row, not separate rows** (e.g. `330i` and `330i xDrive`
   both appear as trims; a `328d` diesel is a trim). This keeps Rule A fan-out complete without row bloat.

## What to spot-check first (highest value)
- **The 10 `medium`-confidence rows** (open the XLSX and filter `confidence = medium`): F44 2-GC, E61 wagon,
  G32 6-GT, F01/F02 7-Series trim splits, G14/G15/G16 8-Series, G02 X4, F98 X4 M.
- **`still_selling` rows' `us_end_year`** — they're set to 2026 and will drift as model years roll.
- **Any chassis you stock heavily** — those are the ones where a wrong year range costs the most.

> Every row is `verified: false` until you check it. Flip it to `true` (or edit) as you review — that's the
> provisional-vs-confirmed signal the design doc calls for before a mass push.
