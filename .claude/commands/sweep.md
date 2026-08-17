---
description: Run the live eBay fitment sweep (pushes fitment; resumes via the ledger)
---
Run the canonical **live** fitment sweep from the repo root:

```bash
python3 scripts/ebay_batch.py apply --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --sleep 0.15 --live
```

Before running, remember:
- This only works on the Mac or in a Codespace — eBay is unreachable from a Claude cloud session.
- It resumes where the last run stopped (the ledger), so it's safe to re-run.
- If it stops with a token error, tell the user to refresh `token.txt` (or set up `ebay_auth.json`)
  and re-run — do not treat that as a failure of the tool.

When it finishes (or stops), report the `Summary: {...}` line (pushed / skip / review / auth_error
counts) and, if anything looks off, the first few relevant rows from `data/batch_plan.csv`.
