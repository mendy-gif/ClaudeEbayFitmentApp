---
description: Refresh the Shopify donor data (chassis code + engine per SKU)
---
Refresh the Shopify donor dump so new/changed listings are covered:

```bash
python3 scripts/shopify_donor.py --dump
```

This needs Shopify credentials (`shopify_token.txt` or `SHOPIFY_STORE`/`SHOPIFY_TOKEN`). It rewrites
`data/shopify_donors.json`. Report how many donors were written. If credentials aren't set up and you
(Claude) have a working Shopify connection available, you may instead rebuild the dump from a bulk
export and `scripts/shopify_donor.py --from-bulk <export.jsonl>`. After refreshing, the next `/sweep`
or `/dry-run` will pick up the newly-covered SKUs.
