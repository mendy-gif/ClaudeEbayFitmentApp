# Built part-number fitment database (committed snapshot)

These are the **generated Approach-2 outputs**, committed here so the sister branch
(`claude/ebay-fitment-chassis-rules-3pztn4`) can reach them directly — the source
`Inventory.xlsx` lives only in one Claude session's uploads, so the sister cannot rebuild
them itself. Normally these files are git-ignored (business data); this is a deliberate,
force-added snapshot.

**Source:** built from `Inventory_Limited.xlsx` + `SuredoneAll_20260319_213610.csv`.

| File | Rows | What it is |
|------|------|-----------|
| `fitment_by_partnumber.csv` | ~18,937 | **The master database.** `part_number_7, part_number_11_forms, vehicle_count, vehicles` — each 7-digit BMW part number → the set of vehicles it has historically come off (`;`-separated `"Year Make Model"`). |
| `listing_fitment_to_add.csv` | ~3,919 | Per-listing raw match: `guid, listing_part7, matched_part7, year, make, model, title`. |
| `ebay_ready_fitment.csv` | ~3,919 | **Consume this in the combined tool.** `guid, part7, year, make, raw_model, ebay_model, mapping_flag, title` — models reconciled to eBay's catalog. Use `ebay_model`; skip `mapping_flag == "UNMAPPED"`. |

**Heads-up for the combined-tool builder:** the `guid` values in the enriched files here
carry a `STOCK` prefix (e.g. `STOCK9350723`), whereas live eBay Custom Labels / SKUs are
bare numbers (e.g. `5978`). Before unioning by SKU, confirm how these `guid`s map to the
real eBay SKUs on the live listings (strip the prefix, or re-run `enrich_listings.py`
against a Suredone/eBay export whose `guid` column is the real Custom Label). If they don't
line up, the part-number rows won't attach to the right listings.

To refresh: re-run the Approach-2 pipeline (`build_fitment_table.py` → `enrich_listings.py`
→ `reconcile_models.py`) against an updated inventory workbook.
