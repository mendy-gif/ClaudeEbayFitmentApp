#!/usr/bin/env python3
"""
Inspect the live eBay state of a SKU — the diagnostic for the relist-persistence
question (docs/DESIGN.md sec 5.2). Reads three things and prints a verdict:

  1. getInventoryItem      -> is the SKU still an Inventory-API item? (Path A)
  2. getProductCompatibility -> how many compatible vehicles are stored on the SKU?
  3. getOffers             -> current offer(s): listingId + status (did it relist?)

Read-only. Token from token.txt or --token.

Usage:  python3 scripts/ebay_inspect.py 1194
        python3 scripts/ebay_inspect.py 1194 --token "..."
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://api.ebay.com"


def token():
    for v in (os.environ.get("EBAY_ACCESS_TOKEN"),):
        if v:
            return v.strip()
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    print("ERROR: no token (token.txt or EBAY_ACCESS_TOKEN).", file=sys.stderr)
    sys.exit(2)


def get(path, tok):
    req = urllib.request.Request(BASE + path, headers={"Authorization": f"Bearer {tok}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw}
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def err_ids(p):
    return [e.get("errorId") for e in p.get("errors", [])] if isinstance(p, dict) else []


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    tok = None
    if "--token" in sys.argv:
        tok = sys.argv[sys.argv.index("--token") + 1]
    if not args:
        print("Usage: ebay_inspect.py <SKU> [--token ...]", file=sys.stderr); sys.exit(2)
    sku = args[0]
    tok = tok or token()
    q = urllib.parse.quote(sku, safe="")

    s_inv, inv = get(f"/sell/inventory/v1/inventory_item/{q}", tok)
    s_cmp, cmp = get(f"/sell/inventory/v1/inventory_item/{q}/product_compatibility", tok)
    s_off, off = get(f"/sell/inventory/v1/offer?{urllib.parse.urlencode({'sku': sku})}", tok)

    is_item = s_inv == 200
    n_compat = len(cmp.get("compatibleProducts", [])) if s_cmp == 200 else 0
    offers = off.get("offers", []) if s_off == 200 else []

    print(f"SKU {sku}")
    print(f"  inventory_item:        HTTP {s_inv}  -> {'IS an Inventory-API item' if is_item else 'NOT an inventory item (' + str(err_ids(inv)) + ')'}")
    print(f"  product_compatibility: HTTP {s_cmp}  -> {n_compat} compatible vehicle(s) stored on the SKU")
    if offers:
        for o in offers:
            print(f"  offer:                 listingId={o.get('listing', {}).get('listingId')} status={o.get('status')} format={o.get('format')}")
    else:
        print(f"  offer:                 HTTP {s_off}  -> {'none' if s_off == 200 else err_ids(off)}")

    print("\nVERDICT:")
    if is_item and n_compat > 0:
        print(f"  Compatibility SURVIVED at the SKU level ({n_compat} vehicles). If the listing page shows")
        print("  nothing, that's display/index lag - it should render. => one-time push is viable.")
    elif is_item and n_compat == 0:
        print("  SKU is still an inventory item but compatibility is EMPTY. The relist cleared it")
        print("  (Dismantly recreated the inventory record). => must RE-APPLY per relist (recurring job).")
    elif not is_item:
        print("  SKU is NOT currently an inventory item (25710). Either the relisted item isn't an")
        print("  Inventory-API item, or the record is still being (re)created. Re-run in a bit; if it")
        print("  stays 25710, this relist path needs re-migration/re-apply. ")
    if is_item and n_compat == 0:
        print("  NOTE: also re-run in ~15-30 min in case Dismantly is still syncing the new listing.")


if __name__ == "__main__":
    main()
