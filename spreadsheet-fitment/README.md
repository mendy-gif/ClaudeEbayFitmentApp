# Spreadsheet-Driven Fitment (Approach 2)

This is a **second, independent approach** to the same goal as the main project: getting good
vehicle-fitment (compatibility) data onto our eBay listings.

- The **main project** (in the repo's top-level `docs/`, `scripts/`, and `data/` folders) does this
  **automatically**, using BMW chassis-code rules.
- **This folder** does it a different way: **you control the fitment by hand in a spreadsheet.**
  Instead of the computer guessing which vehicles a part fits, you type it into a simple table.

Nothing in this folder touches or changes the main project. The two live side-by-side so we can
compare them.

## The idea in one sentence

You keep one spreadsheet where **each row says "this part (SKU) fits this vehicle."** When you're
happy with it, that spreadsheet becomes the fitment we push to eBay.

## How the spreadsheet works

Open **`data/fitment_mapping_template.xlsx`** (works in Excel or Google Sheets). It has one row of
example data so you can see the shape. The columns are:

| Column      | What to put there                                    | Example              |
|-------------|------------------------------------------------------|----------------------|
| `sku`       | Your eBay listing's SKU / custom label               | `BMW-E90-HEADLIGHT-01` |
| `part_title`| A short note to yourself (not sent to eBay)          | `E90 LH headlight`   |
| `year`      | The vehicle's year the part fits                     | `2008`               |
| `make`      | Vehicle make                                         | `BMW`                |
| `model`     | Vehicle model                                        | `328i`               |
| `trim`      | Trim (optional; leave blank if it fits all trims)    | `Base`               |
| `engine`    | Engine (optional)                                    | `3.0L L6`            |
| `notes`     | Fitment note shown to the buyer (optional)           | `Left/driver side`   |

**One part that fits several vehicles = several rows** with the same `sku`. For example, a headlight
that fits 2007–2011 BMW 328i is five rows (one per year), all with the same SKU.

There's also a `data/fitment_mapping_template.csv` — the exact same thing as a plain file, in case
you prefer that.

## The workflow (what happens, step by step)

1. **You fill in the spreadsheet** — one row per part-fits-vehicle.
2. **A checking step** looks over your spreadsheet for obvious mistakes (missing SKU, blank year,
   etc.) and tells you which rows to fix. *(To be built — see `docs/DESIGN.md`.)*
3. **A push step** sends the finished list to eBay as compatibility on each listing.
   *(To be built.)*

Steps 2 and 3 are code that will be added here later, in **this** folder, without affecting the main
project.

## What's here now

- `data/fitment_mapping_template.xlsx` — the spreadsheet to fill in (with one example row)
- `data/fitment_mapping_template.csv` — the same template as a plain file
- `docs/DESIGN.md` — the plan for the checking and push steps

## What's *not* built yet (on purpose)

The checking step and the eBay push step. We start with the spreadsheet format so you can begin
entering data right away; the automation gets added on top once the format feels right to you.
