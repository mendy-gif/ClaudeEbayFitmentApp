# eBay BMW Chassis-Family Fitment

Rule-based "fast fitment" for a BMW-focused salvage-yard eBay catalog: expand each listing's
compatibility from just the donor vehicle to the whole BMW chassis family, using known chassis-code
structure instead of per-part research, and push the result to eBay via the Sell API.

**Start here → [`docs/DESIGN.md`](docs/DESIGN.md)** — the design decisions, the rule definitions,
the eBay API findings, and the single test that must run before any code is written.

**Setting up eBay + running the decisive test → [`docs/EBAY_SETUP.md`](docs/EBAY_SETUP.md)** — A-to-Z:
developer account, keyset, OAuth user token, and how to run `scripts/ebay_inventory_test.py` to learn
whether Dismantly's listings are reachable by the Inventory API (Path A vs Path B).

## Where things stand

- **Approach agreed:** two rules (A = whole chassis family, B = engine parts → donor trim only),
  both driven by **one** reference table. No per-trim table needed.
- **Blocking test:** whether Dismantly's listings are reachable by eBay's Inventory API
  (`getInventoryItem` on one real SKU). This decides one-time-push vs. continuous-system. See
  `docs/DESIGN.md` §5.1 and §8.
- **Not built yet:** reference data file, classification, and the apply pipeline — intentionally,
  pending the test above.
