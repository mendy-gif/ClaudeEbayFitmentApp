---
description: Preview the fitment sweep safely (dry-run, no eBay writes)
---
Run a safe **dry-run** preview — this writes `data/batch_plan.csv` and makes NO changes to eBay:

```bash
python3 scripts/ebay_batch.py plan --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --limit 50
```

Then summarize for the user: how many would `push` vs `skip` vs `review`, the `sources` mix
(chassis / pn / chassis+pn), and flag anything unusual (e.g. lots of "category not in tree →
default", which means the category tree file may be missing). Offer to run the live `/sweep` if it
looks right. You can raise `--limit` if the user wants a bigger preview.
