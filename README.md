# eBay BMW Chassis-Family Fitment

Rule-based "fast fitment" for a BMW-focused salvage-yard eBay catalog: expand each listing's
compatibility from just the donor vehicle to the whole BMW chassis family (plus every car a part
number has historically come off of), and push the result to eBay via the Sell Inventory API.

**New here? Read [`CLAUDE.md`](CLAUDE.md)** — the project memory: what it does, how to run it, the
key gotchas, and the file map. It's the fastest way to get oriented.

## Status: live

The pipeline is **built and pushing to real listings.** It:
1. Pulls each SKU's donor vehicle (chassis code + engine) from Shopify (`scripts/shopify_donor.py`).
2. Classifies the part as Rule A (whole chassis family) or Rule B (engine part → donor engine only)
   from its eBay category (`scripts/classify_part.py`).
3. Expands to eBay-shaped compatibility rows (`scripts/fitment_rules.py`) and **unions in** exact
   part-number history from `spreadsheet-fitment/`.
4. Pushes once per SKU via the Inventory API, resuming safely via a ledger (`scripts/ebay_batch.py`).

A refresh token (`scripts/ebay_auth.py`) keeps long sweeps alive past eBay's 2-hour token limit, and
`.github/workflows/fitment-sweep.yml` runs the whole thing on a daily schedule.

## Run it

See **[`CLAUDE.md`](CLAUDE.md)** for the canonical commands, and **[`docs/SETUP_MAC.md`](docs/SETUP_MAC.md)**
to set up on a Mac. The short version:

```bash
python3 scripts/shopify_donor.py --dump                                    # refresh donor data
python3 scripts/ebay_batch.py plan  --from-shopify --from-inventory \      # dry-run preview
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --limit 50
python3 scripts/ebay_batch.py apply --from-shopify --from-inventory \      # live push
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --sleep 0.15 --live
```

> eBay's API is **not reachable from a Claude cloud session** (403 by network policy). Run the
> pipeline locally (Mac) or in a GitHub Codespace. See [`docs/EBAY_ACCESS_NOTE.md`](docs/EBAY_ACCESS_NOTE.md).

## More docs

- [`docs/DESIGN.md`](docs/DESIGN.md) — design decisions, rule definitions, eBay API findings.
- [`docs/EBAY_SETUP.md`](docs/EBAY_SETUP.md) — eBay developer account, keyset, OAuth token.
- [`docs/SETUP_MAC.md`](docs/SETUP_MAC.md) — get running on a Mac, step by step.
- [`docs/EBAY_ACCESS_NOTE.md`](docs/EBAY_ACCESS_NOTE.md) — why eBay calls run from the machine, not Claude.
