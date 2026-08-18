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
  LEAKING   -- eBay displays a TRIM we never pushed. A trimless row is a WILDCARD: eBay fans
               it out to every trim for that year/model, which on an engine-restricted part
               silently re-adds the engines the rule excluded (an N55 turbo claiming the
               diesel and the M car).

Note this compares TRIMS, not row counts. A higher displayed count is often perfectly
correct: eBay also expands along an Engine axis we never specify, so one pushed 2024 X3
M40i row legitimately becomes two displayed rows (petrol and mild-hybrid -- both the same
B58). Counting rows flags that as a leak; comparing trims does not. And where we pushed a
trimless row on purpose (Rule A), everything shown is expected by definition.

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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

BASE = "https://api.ebay.com"
LEDGER = os.path.join(ROOT, "data", "pushed_ledger.json")
TREE = os.path.join(ROOT, "data", "ebay_motors_categories.json")
NONDISPLAY = os.path.join(ROOT, "data", "nondisplay_categories.json")


def load_nondisplay():
    """Categories eBay simply does not render a fitment table in. Rows pushed there are
    stored but never shown, whatever we send -- so counting them against the display-rate
    threshold would fail the nightly job forever for something we cannot fix."""
    try:
        return set(json.load(open(NONDISPLAY, encoding="utf-8")).get("categories", {}))
    except (ValueError, OSError):
        return set()


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
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError):
        return {}       # OSError covers socket.timeout, which is NOT a URLError


class RateLimited(RuntimeError):
    """eBay refused the call on quota (error 518). Every further read will fail too, so
    stop rather than spending the rest of the allowance discovering that 300 times."""


def trading_rows(item_id, tok):
    """The compatibility rows the DISPLAY store holds, as [{Year,Make,Model,Trim}].
    None if the read failed (distinct from an empty list, which means 'displays nothing')."""
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
        try:
            raw = e.read().decode("utf-8", "replace")
        except OSError:
            return None
    except (urllib.error.URLError, OSError):
        return None     # OSError covers socket.timeout, which is NOT a URLError
    if "<ErrorCode>518</ErrorCode>" in raw:
        raise RateLimited("eBay call limit exceeded (error 518)")
    if "<Ack>Failure</Ack>" in raw and "<Compatibility>" not in raw:
        return None
    out = []
    for block in re.findall(r"<Compatibility>(.*?)</Compatibility>", raw, re.S):
        d = dict(re.findall(r"<Name>(.*?)</Name><Value>(.*?)</Value>", block))
        if d.get("Year") and d.get("Model"):
            out.append(d)
    return out


def leaked_trims(pushed, shown):
    """Trims eBay DISPLAYS that we never pushed -- the wildcard fingerprint.

    Counting rows cannot detect this: eBay legitimately expands one pushed row across an
    Engine axis we do not specify (a 2024 X3 M40i splits into plain petrol and mild-hybrid,
    both genuinely the same B58). So compare TRIMS, per vehicle, and only for vehicles where
    we were specific -- if we deliberately pushed a trimless row, everything shown is expected.
    """
    want = {}
    for r in pushed:
        key = (str(r["Year"]), r["Model"])
        want.setdefault(key, set()).add(r.get("Trim") or "*")
    leaked = set()
    for r in shown:
        key = (str(r.get("Year")), r.get("Model"))
        allowed = want.get(key)
        if not allowed or "*" in allowed:      # vehicle we never pushed, or pushed trimless
            continue
        if r.get("Trim") and r["Trim"] not in allowed:
            leaked.add(f"{key[0]} {key[1]} {r['Trim']}")
    return sorted(leaked)


def inventory_rows(sku, tok):
    q = urllib.parse.quote(sku, safe="")
    out = []
    for cp in rest(f"/sell/inventory/v1/inventory_item/{q}/product_compatibility", tok) \
            .get("compatibleProducts", []):
        d = {p.get("name"): p.get("value") for p in cp.get("compatibilityProperties", [])}
        if d.get("Year") and d.get("Model"):
            out.append(d)
    return out


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


def _write_csv(path, detail):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["sku", "listingId", "categoryId", "rule",
                                          "stored", "displayed", "leaked", "status"])
        w.writeheader()
        w.writerows(detail)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only audit the first N ledgered SKUs")
    ap.add_argument("--recent", type=int, metavar="N",
                    help="audit the N most recently pushed SKUs instead of the oldest. This is "
                         "what a post-sweep regression check wants -- --limit samples the oldest "
                         "entries, which a fresh sweep never touched.")
    ap.add_argument("--sample", type=int, metavar="N",
                    help="audit a rotating slice of N SKUs from the WHOLE ledger, offset by the "
                         "day of the year so coverage cycles over time. This is the drift check: "
                         "--recent only ever looks at new work, so a listing overwritten by "
                         "another system months ago would never be noticed.")
    ap.add_argument("--fail-on-leak", action="store_true",
                    help="exit non-zero if any listing displays a trim we never pushed")
    ap.add_argument("--fail-under", type=float, metavar="PCT",
                    help="exit non-zero if fewer than PCT%% of audited SKUs display. Lets CI "
                         "fail loudly on a regression instead of pushing invisible fitment "
                         "night after night, which is exactly how the trim bug survived.")
    ap.add_argument("--csv", metavar="PATH", help="write per-SKU detail here")
    ap.add_argument("--token")
    ap.add_argument("--quiet", action="store_true", help="summary only, no per-SKU lines")
    args = ap.parse_args()
    tok = token(args)

    if not os.path.exists(LEDGER):
        sys.exit(f"no ledger at {LEDGER} -- nothing pushed yet")
    led = json.load(open(LEDGER, encoding="utf-8"))
    tree = json.load(open(TREE, encoding="utf-8")) if os.path.exists(TREE) else None
    if args.sample:
        allk = list(led)
        n = min(args.sample, len(allk))
        # Deterministic rotation: each day starts where the previous left off, so the whole
        # ledger is covered over time without needing to remember a cursor anywhere.
        import datetime
        start = (datetime.date.today().timetuple().tm_yday * n) % max(len(allk), 1)
        skus = [allk[(start + i) % len(allk)] for i in range(n)]
        print(f"(rotating drift sample: {n} of {len(allk)} ledgered SKUs, offset {start})")
    elif args.recent:
        skus = list(led)[-args.recent:]
    elif args.limit:
        skus = list(led)[: args.limit]
    else:
        skus = list(led)
    print(f"Auditing {len(skus)} ledgered SKU(s) -- stored (Inventory) vs displayed (Trading)\n")

    detail, by_rule, by_cat = [], collections.defaultdict(lambda: [0, 0]), collections.defaultdict(lambda: [0, 0])
    unreadable = 0
    rate_limited = False
    for i, sku in enumerate(skus, 1):
        off = rest("/sell/inventory/v1/offer?" + urllib.parse.urlencode({"sku": sku}), tok)
        pub = [o for o in off.get("offers", []) if o.get("status") == "PUBLISHED"]
        if not pub:
            continue
        cid = str(pub[0].get("categoryId") or "?")
        lid = pub[0].get("listing", {}).get("listingId")
        pushed_rows = inventory_rows(sku, tok)
        stored = len(pushed_rows)
        if stored == 0:
            continue
        try:
            shown_rows = trading_rows(lid, tok) if lid else None
        except RateLimited as e:
            print(f"\n  STOPPED at {i}/{len(skus)}: {e}")
            rate_limited = True
            break
        if shown_rows is None:
            unreadable += 1
            continue
        shown = len(shown_rows)
        rule = led[sku].get("rule", "?")
        ok = shown > 0
        leaks_here = leaked_trims(pushed_rows, shown_rows)
        leaking = bool(leaks_here)
        by_rule[rule][0] += 1
        by_rule[rule][1] += ok
        by_cat[cid][0] += 1
        by_cat[cid][1] += ok
        detail.append({"sku": sku, "listingId": lid, "categoryId": cid, "rule": rule,
                       "stored": stored, "displayed": shown,
                       "leaked": "; ".join(leaks_here[:6]),
                       "status": "INVISIBLE" if not ok else ("LEAKING" if leaking else "OK")})
        if not args.quiet:
            flag = ("  <-- INVISIBLE" if not ok else
                    f"  <-- LEAKING: {leaks_here[0]}" if leaking else "   ")
            print(f"  [{i}/{len(skus)}] {sku:>8}  cat={cid:<7} rule={rule}  stored={stored:<4} displayed={shown:<4}{flag}")

    n = len(detail)
    if rate_limited and n == 0:
        sys.exit("\nCOULD NOT AUDIT: eBay's call limit (error 518) was hit before a single "
                 "listing could be read. The quota resets daily. Nothing is known about "
                 "whether fitment is displaying -- this is not a pass.")
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
        for d in sorted(leaks, key=lambda x: -x["displayed"])[:15]:
            print(f"   {d['sku']:>8}  pushed {d['stored']} -> displayed {d['displayed']}"
                  f"   listing {d['listingId']}")
            print(f"             never pushed: {d['leaked']}")
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
            # Rule A rows are trimless and always displayed even before the catalog fix, so a
            # category failing on Rule A too cannot be the trim bug.
            known = " [known non-displaying category]" if c in load_nondisplay() else ""
            hint = (("includes Rule A -> a genuine CATEGORY-level display problem"
                     if "A" in rules else
                     "all engine-restricted -> re-check after a re-sweep, else category-level")
                    + known)
            print(f"   cat {c}: 0/{t}   [{','.join(rules)}] {hint}")
            p = category_path(c, tree)
            if p:
                print(f"            {p}")

    attempted = len(detail) + unreadable
    if attempted and unreadable / attempted > 0.2:
        print(f"\nINCONCLUSIVE: {unreadable} of {attempted} listings could not be read"
              + (" (eBay call limit)" if rate_limited else "")
              + f", so this audit only saw {len(detail)}. That is not enough to say anything "
              f"about whether fitment is displaying.")
        if args.csv:
            _write_csv(args.csv, detail)
        # Exit non-zero when a threshold was requested: a run that could not verify itself
        # must not report success. Silence here is exactly how the trim bug survived.
        sys.exit(1 if args.fail_under is not None else 0)

    if args.fail_on_leak and leaks:
        print(f"\nFAIL: {len(leaks)} listing(s) display a trim we never pushed. Either a "
              f"wildcard leaked from our own rows, or another system (Dismantly, PartOutPro, "
              f"eBay auto-fitment) has rewritten the fitment on these listings.")
        if args.csv:
            _write_csv(args.csv, detail)
        sys.exit(1)

    if args.fail_under is not None:
        dead = load_nondisplay()
        scored = [d for d in detail if d["categoryId"] not in dead]
        excluded = len(detail) - len(scored)
        if not scored:
            print(f"\nNothing to score ({excluded} SKU(s) all in known non-displaying "
                  f"categories) -- threshold not applied.")
        else:
            sgood = sum(1 for d in scored if d["status"] != "INVISIBLE")
            pct = 100.0 * sgood / len(scored)
            note = (f" ({excluded} SKU(s) excluded: categories eBay never renders fitment in)"
                    if excluded else "")
            if pct < args.fail_under:
                print(f"\nFAIL: {pct:.1f}% of scorable SKUs display ({sgood}/{len(scored)}), "
                      f"below the {args.fail_under}% threshold.{note}\n"
                      f"Fitment is being pushed but not shown -- stop sweeping until this is "
                      f"understood.")
                if args.csv:
                    _write_csv(args.csv, detail)
                sys.exit(1)
            print(f"\nDisplay rate {pct:.1f}% ({sgood}/{len(scored)}) is at or above the "
                  f"{args.fail_under}% threshold.{note}")

    if args.csv:
        _write_csv(args.csv, detail)
        print(f"\nPer-SKU detail -> {args.csv}")


if __name__ == "__main__":
    main()
