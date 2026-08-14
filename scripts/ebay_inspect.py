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


def trading_getitem_compat(item_id, tok):
    """Read the Trading-API Item-level ItemCompatibilityList (the store that DISPLAYS
    and that Dismantly manages). Returns (count, sample_list, error_str)."""
    import xml.etree.ElementTree as ET
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        '<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>'
        f'<ItemID>{item_id}</ItemID><DetailLevel>ReturnAll</DetailLevel>'
        '</GetItemRequest>'
    )
    req = urllib.request.Request(
        BASE + "/ws/api.dll", data=body.encode("utf-8"), method="POST",
        headers={
            "X-EBAY-API-CALL-NAME": "GetItem",
            "X-EBAY-API-SITEID": "100",              # eBay Motors US
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1199",
            "X-EBAY-API-IAF-TOKEN": tok,             # OAuth token for Trading API
            "Content-Type": "text/xml",
        })
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return None, [], f"HTTP {e.code}: {e.read().decode('utf-8','replace')[:300]}"
    except urllib.error.URLError as e:
        return None, [], f"transport: {e}"
    ns = "{urn:ebay:apis:eBLBaseComponents}"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, [], f"xml parse: {e}"
    ack = root.findtext(f"{ns}Ack")
    if ack not in ("Success", "Warning"):
        msgs = [se.findtext(f"{ns}LongMessage") or se.findtext(f"{ns}ShortMessage")
                for se in root.findall(f"{ns}Errors")]
        return None, [], f"Ack={ack}: {'; '.join(m for m in msgs if m)[:300]}"
    compat = root.findall(f".//{ns}ItemCompatibilityList/{ns}Compatibility")
    sample = []
    for c in compat[:6]:
        nv = {nvl.findtext(f'{ns}Name'): nvl.findtext(f'{ns}Value')
              for nvl in c.findall(f"{ns}NameValueList")}
        sample.append(nv)
    return len(compat), sample, None


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
    listing_id = offers[0].get("listing", {}).get("listingId") if offers else None

    print(f"SKU {sku}")
    print(f"  inventory_item:            HTTP {s_inv}  -> {'IS an Inventory-API item' if is_item else 'NOT an inventory item (' + str(err_ids(inv)) + ')'}")
    print(f"  INVENTORY compatibility:   HTTP {s_cmp}  -> {n_compat} vehicle(s)  [product_compatibility, by SKU]")
    if offers:
        for o in offers:
            print(f"  offer:                     listingId={o.get('listing', {}).get('listingId')} status={o.get('status')} format={o.get('format')}")
    else:
        print(f"  offer:                     HTTP {s_off}  -> {'none' if s_off == 200 else err_ids(off)}")

    # Trading-API Item-level compatibility (the store that DISPLAYS / Dismantly manages).
    n_trad, sample, terr = (None, [], "no listingId")
    if listing_id:
        n_trad, sample, terr = trading_getitem_compat(listing_id, tok)
    if n_trad is None:
        print(f"  TRADING compatibility:     n/a  ({terr})  [GetItem ItemCompatibilityList, by ItemID]")
    else:
        print(f"  TRADING compatibility:     {n_trad} vehicle(s)  [GetItem ItemCompatibilityList, by ItemID]")
        for nv in sample:
            print(f"      - {nv.get('Year','?')} {nv.get('Make','?')} {nv.get('Model','?')} {nv.get('Trim','') or ''}".rstrip())

    print("\nVERDICT:")
    if not is_item:
        print("  SKU is NOT an inventory item (25710). Re-run shortly; if it persists the relist path needs work.")
    elif n_compat > 0:
        print(f"  Compatibility present at the INVENTORY (SKU) level ({n_compat}). Freshly set, pre-relist.")
    elif n_trad and n_trad > 0:
        print(f"  KEY FINDING: INVENTORY store empty but TRADING (Item) store has {n_trad} vehicles = the")
        print("  displayed/searchable fitment. Dismantly carried compatibility FORWARD to the new item at the")
        print("  Trading level. => our push SURVIVED the relist; one-time push per SKU is viable. Verify via")
        print("  Trading (GetItem)/the listing page, NOT getProductCompatibility (which only sees the SKU store).")
    elif n_trad == 0:
        print("  Both stores empty. The relist cleared compatibility. => recurring re-apply needed.")
    else:
        print("  INVENTORY empty; TRADING read unavailable (see error above). Fix the Trading read to be sure.")


if __name__ == "__main__":
    main()
