#!/usr/bin/env python3
"""
Fleet-wide DISPLAY audit — does the fitment we pushed actually show on the listings?

The ledger records that we PUSHED. It cannot tell you the push was VISIBLE: eBay stores
a compatibility row whose Trim isn't in its vehicle catalog and then silently omits it
from the listing (see docs/DESIGN.md sec 5.5). This tool reads both stores for every
ledgered SKU and reports where they disagree:

    Inventory store (by SKU)   = what we wrote
    Trading store  (by ItemID) = what actually displays

Two different failures show up here, and they look OPPOSITE:

  INVISIBLE (displayed = 0)  -- nothing shows. The trim-spelling bug: eBay stored a trim it
                                does not recognise and omitted it from the listing.
  LEAKING   (displayed > stored, on a Rule B part) -- TOO MUCH shows. A trimless row is a
                                WILDCARD: eBay fans it out to every trim for that year/model.
                                On an engine-restricted part that silently re-adds the engines
                                the rule excluded, so an N55 turbo starts claiming the diesel
                                and the M car. This is how the leak was originally spotted:
                                17 rows pushed, 41 displayed.

On a Rule A part displayed > stored is expected and desirable -- the whole family really does
fit, and the wildcard is doing its job.

It slices the result three ways, because the two known causes look different:
  * LEAKING     -- Rule B SKUs showing more than we pushed (see above).
  * by RULE     -- the trim bug hit Rule B only (Rule A rows are trimless). Before the
                   catalog fix this read ~96% Rule A vs ~30% Rule B.
  * by CATEGORY -- a category at 0/N *including Rule A SKUs* is a genuine category-level
                   problem, not our row data.
  * the worst offenders, so you can spot-check individual listings.

Read-only: no writes, safe to run any time. ~3 API calls per SKU, so a full 281-SKU pass
takes a few minutes.

Usage:
  python3 scripts/ebay_display_audit.py                    # every ledgered SKU
  python3 scripts/ebay_display_audit.py --limit 40         # quick sample
  python3 scripts/ebay_display_audit.py --csv out.csv      # per-SKU detail
"""
import argparse
import collections
import csv
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

BASE = "https://api.ebay.com"
LEDGER = os.path.join(ROOT, "data", "pushed_ledger.json")
TREE = os.path.join(ROOT, "data", "ebay_motors_categories.json")


def token(args):
    if args.token:
        return args.token.strip()
    try:
        import ebay_auth
        t = ebay_auth.get_access_token()
        if t:
            return t
    except Exception:                                    # noqa: BLE001
        pass
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    sys.exit("ERROR: no token (ebay_auth.json / token.txt / --token)")


def rest(path, tok):
    req = urllib.request.Request(BASE + path, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
        return {}


def trading_count(item_id, tok):
    """Number of compatibility rows the DISPLAY store holds. -1 if the read failed."""
    body = ('<?xml version="1.0" encoding="utf-8"?>'
            '<GetItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
            f'<ItemID>{item_id}</ItemID>'
            '<IncludeItemCompatibilityList>true</IncludeItemCompatibilityList>'
            '</GetItemRequest>')
    hdrs = {"X-EBAY-API-CALL-NAME": "GetItem", "X-EBAY-API-SITEID": "100",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1199", "X-EBAY-API-IAF-TOKEN": tok,
            "Content-Type": "text/xml"}
    req = urllib.request.Request(BASE + "/ws/api.dll", data=body.encode("utf-8"),
                                 method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except urllib.error.URLError:
        return -1
    if "<Ack>Failure</Ack>" in raw and "<Compatibility>" not in raw:
        return -1
    return raw.count("<Compatibility>")


def category_path(cid, tree):
    if not tree:
        return ""

    def walk(node, path):
        c = node.get("category", {})
        p = path + [c.get("categoryName", "?")]
        if str(c.get("categoryId")) == str(cid):
            return p
        for ch in node.get("childCategoryTreeNodes", []) or []:
            hit = walk(ch, p)
            if hit:
                return hit
        return None

    hit = walk(tree.get("rootCategoryNode", tree), [])
    return " > ".join(hit[-2:]) if hit else ""


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only audit the first N ledgered SKUs")
    ap.add_argument("--csv", metavar="PATH", help="write per-SKU detail here")
    ap.add_argument("--token")
    ap.add_argument("--quiet", action="store_true", help="summary only, no per-SKU lines")
    args = ap.parse_args()
    tok = token(args)

    if not os.path.exists(LEDGER):
        sys.exit(f"no ledger at {LEDGER} -- nothing pushed yet")
    led = json.load(open(LEDGER, encoding="utf-8"))
    tree = json.load(open(TREE, encoding="utf-8")) if os.path.exists(TREE) else None
    skus = list(led)[: args.limit] if args.limit else list(led)
    print(f"Auditing {len(skus)} ledgered SKU(s) -- stored (Inventory) vs displayed (Trading)\n")

    detail, by_rule, by_cat = [], collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0])
    unreadable = 0
    for i, sku in enumerate(skus, 1):
        off = rest("/sell/inventory/v1/offer?" + urllib.parse.urlencode({"sku": sku}), tok)
        pub = [o for o in off.get("offers", []) if o.get("status") == "PUBLISHED"]
        if not pub:
            continue
        cid = str(pub[0].get("categoryId") or "?")
        lid = pub[0].get("listing", {}).get("listingId")
        q = urllib.parse.quote(sku, safe="")
        stored = len(rest(f"/sell/inventory/v1/inventory_item/{q}/product_compatibility", tok)
                     .get("compatibleProducts", []))
        if stored == 0:
            continue
        shown = trading_count(lid, tok) if lid else -1
        if shown < 0:
            unreadable += 1
            continue
        rule = led[sku].get("rule", "?")
        ok = shown > 0
        # Rule B rows are all trimmed, so they display 1:1. More displayed than pushed means
        # a trimless wildcard slipped in and fanned out across the excluded engines.
        leaking = rule == "B" and shown > stored
        by_rule[rule][0] += 1
        by_rule[rule][1] += ok
        by_cat[cid][0] += 1
        by_cat[cid][1] += ok
        detail.append({"sku": sku, "listingId": lid, "categoryId": cid, "rule": rule,
                       "stored": stored, "displayed": shown,
                       "status": "INVISIBLE" if not ok else ("LEAKING" if leaking else "OK")})
        if not args.quiet:
            flag = ("  <-- INVISIBLE" if not ok else
                    "  <-- LEAKING (wildcard re-added excluded engines)" if leaking else "   ")
            print(f"  [{i}/{len(skus)}] {sku:>8}  cat={cid:<7} rule={rule}  stored={stored:<4} displayed={shown:<4}{flag}")

    n = len(detail)
    if not n:
        sys.exit("\nNo ledgered SKU has stored compatibility on a published offer.")
    good = sum(1 for d in detail if d["status"] == "OK")
    leaks = [d for d in detail if d["status"] == "LEAKING"]
    print(f"\n{'=' * 62}\nOVERALL: {good}/{n} display ({100 * good // n}%)"
          + (f"   [{unreadable} unreadable, skipped]" if unreadable else ""))

    if leaks:
        print(f"\nLEAKING: {len(leaks)} Rule B SKU(s) display MORE than we pushed -- a trimless")
        print("wildcard fanned out across the engines the rule excluded. These listings claim")
        print("fitment they should not (e.g. a petrol turbo advertised for the diesel):")
        for d in sorted(leaks, key=lambda x: x["stored"] - x["displayed"])[:15]:
            print(f"   {d['sku']:>8}  pushed {d['stored']} -> displayed {d['displayed']}"
                  f"   listing {d['listingId']}")
        if len(leaks) > 15:
            print(f"   ... and {len(leaks) - 15} more")
    else:
        print("\nLEAKING: none -- every Rule B SKU displays exactly what we pushed.")

    print("\nBy RULE  (Rule B lagging Rule A = the trim bug; see DESIGN.md 5.5):")
    for r, (t, d) in sorted(by_rule.items()):
        print(f"   Rule {r}: {d}/{t} display ({100 * d // max(t, 1)}%)")

    bad = sorted((c for c in by_cat if by_cat[c][1] == 0), key=lambda c: -by_cat[c][0])
    if bad:
        print("\nCategories where NOTHING displays:")
        for c in bad:
            t = by_cat[c][0]
            rules = sorted({d["rule"] for d in detail if d["categoryId"] == c})
            hint = ("all Rule B -> most likely still the trim bug; re-check after a re-sweep"
                    if rules == ["B"] else
                    "includes Rule A -> a genuine CATEGORY-level display problem")
            print(f"   cat {c}: 0/{t}   [{','.join(rules)}] {hint}")
            p = category_path(c, tree)
            if p:
                print(f"            {p}")

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["sku", "listingId", "categoryId", "rule",
                                              "stored", "displayed", "status"])
            w.writeheader()
            w.writerows(detail)
        print(f"\nPer-SKU detail -> {args.csv}")


if __name__ == "__main__":
    main()
