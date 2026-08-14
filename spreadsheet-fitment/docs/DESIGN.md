# Design — Spreadsheet-Driven Fitment

## Goal

Same end goal as the main rule-based project — accurate vehicle compatibility on eBay listings —
but sourced from a **human-maintained spreadsheet** instead of chassis-code rules.

## Why this approach exists alongside the rule-based one

- **Rule-based (main project):** fast and hands-off, but only as right as the rules. Good for parts
  that follow clean chassis-family logic.
- **Spreadsheet (this folder):** slower to fill in, but you have exact control. Good for oddball
  parts, one-offs, and anything the rules get wrong. Also usable as the source of truth we check the
  rules *against*.

They are not competitors — this spreadsheet can be where a human corrects or overrides the automatic
result.

## Data format (the contract)

One flat table. **One row = one (part, vehicle) pair.** Columns:

| Column       | Required | Notes                                                        |
|--------------|----------|--------------------------------------------------------------|
| `sku`        | yes      | Matches the listing's SKU / custom label on eBay.            |
| `part_title` | no       | Human reference only; never sent to eBay.                    |
| `year`       | yes      | Single year per row (keeps eBay compatibility unambiguous).  |
| `make`       | yes      | e.g. `BMW`.                                                  |
| `model`      | yes      | e.g. `328i`.                                                 |
| `trim`       | no       | Blank = fits all trims for that year/model.                 |
| `engine`     | no       | Blank = fits all engines.                                   |
| `notes`      | no       | Buyer-facing fitment note (e.g. "driver side").             |

Multiple compatible vehicles for one part are expressed as multiple rows sharing the same `sku`.

This lines up with eBay's parts-compatibility model (Year / Make / Model / Trim / Engine + notes),
so the push step is a direct mapping with no guesswork.

## Planned steps (not built yet)

### 1. Validate (`validate_mapping.py` — to build)
Reads the spreadsheet and flags rows that would fail on eBay:
- missing `sku`, `year`, `make`, or `model`
- `year` not a plausible 4-digit year
- duplicate identical rows
Outputs a simple "these rows need fixing" report. Changes nothing.

### 2. Push to eBay (`push_fitment.py` — to build)
Groups rows by `sku`, turns each group into a compatibility list, and sends it to the listing via
eBay's API. This can reuse the eBay auth/writer learnings from the main project's `docs/EBAY_SETUP.md`
without importing its rule logic.

## Explicitly out of scope here

- No chassis-code rules. If we want automatic suggestions, they come *from* the main project and get
  pasted in for a human to confirm — this folder stays the human-controlled source of truth.
