#!/usr/bin/env python3
"""
Diagnose WHY the Trading (display) store rejects a compatibility push.

The Inventory store is lenient (keeps whatever validates loosely); the Trading store
validates each Year/Make/Model[/Trim] against eBay's strict vehicle catalog and is
ALL-OR-NOTHING (error 21916724 rejects the whole call on any single bad combo). This
tool builds the same rows the runner would push for one SKU, then tries them against
Trading in escalating fallbacks, printing eBay's FULL response each time and STOPPING
at the first variant that succeeds:

  1. full set, with Trim       (Rule-B engine restriction preserved)
  2. drop Trim, keep Year/Model (base combos -- almost always in eBay's catalog)
  3. one row at a time         (isolates exactly which combos eBay rejects)

A failed all-or-nothing call changes nothing on the listing, so this is safe to run on
a live SKU (variants 1-2 that SUCCEED will set that SKU's displayed fitment -- fine on a
test SKU you're iterating on).

Usage:
  python3 scripts/ebay_trading_debug.py 52566 --token "$(python3 scripts/ebay_auth.py)"
  python3 scripts/ebay_trading_debug.py 52566 --rule B --token "..."
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import fitment_rules as FR            # noqa: E402
import classify_part as CP            # noqa: E402

BASE = "https://api.ebay.com"
NS = "{urn:ebay:apis:eBLBaseComponents}"


def token(args):
    if args.token:
        return args.token.strip()
    try:
        import ebay_auth
        t = ebay_auth.get_access_token()
        if t:
            return t
    except Exception:  # noqa: BLE001
        pass
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    sys.exit("ERROR: no token (ebay_auth.json / token.txt / --token)")


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


def listing_id_and_category(sku, tok):
    s, off = get("/sell/inventory/v1/offer?" + urllib.parse.urlencode({"sku": sku}), tok)
    if s != 200:
        sys.exit(f"offer read HTTP {s}: {json.dumps(off)[:300]}")
    pub = [o for o in off.get("offers", []) if o.get("status") == "PUBLISHED"]
    if not pub:
        sys.exit("no PUBLISHED offer for this SKU (can't push to a non-live listing).")
    return pub[0].get("listing", {}).get("listingId"), pub[0].get("categoryId")


def build_rows(sku, rule):
    """Same expansion the runner uses: Shopify donor -> chassis-family rows."""
    sd = (json.load(open(os.path.join(ROOT, "data", "shopify_donors.json"), encoding="utf-8"))
          .get(str(sku)))
    if not sd:
        sys.exit(f"no Shopify donor for SKU {sku} (run shopify_donor.py --dump)")
    ref, emap, ebay = FR.load_all()
    row, note = FR.resolve_chassis(sd.get("series"), sd.get("model"), ref)
    if not row:
        sys.exit(f"unresolved chassis: {note}")
    res = FR.expand_from_chassis(row["chassis_code"], rule, ref, emap, ebay,
                                 engine=sd.get("engine_family"), donor_model=sd.get("model"))
    return sd, res


def compat_xml(rows):
    out = []
    for r in rows:
        parts = [("Year", str(r["Year"])), ("Make", r["Make"]), ("Model", r["Model"])]
        if r.get("Trim"):
            parts.append(("Trim", r["Trim"]))
        nv = "".join(f"<NameValueList><Name>{n}</Name><Value>{escape(str(v))}</Value></NameValueList>"
                     for n, v in parts)
        out.append(f"<Compatibility>{nv}</Compatibility>")
    return "".join(out)


def trading_write(item_id, rows, tok):
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">'
        f'<Item><ItemID>{escape(str(item_id))}</ItemID>'
        f'<ItemCompatibilityList><ReplaceAll>true</ReplaceAll>{compat_xml(rows)}</ItemCompatibilityList>'
        '</Item></ReviseFixedPriceItemRequest>'
    )
    hdrs = {"X-EBAY-API-CALL-NAME": "ReviseFixedPriceItem", "X-EBAY-API-SITEID": "100",
            "X-EBAY-API-COMPATIBILITY-LEVEL": "1199", "X-EBAY-API-IAF-TOKEN": tok,
            "Content-Type": "text/xml"}
    req = urllib.request.Request(BASE + "/ws/api.dll", data=body.encode("utf-8"), method="POST", headers=hdrs)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return None, [], f"transport: {e}"
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        return None, [], f"xml parse: {e} :: {raw[:300]}"
    ack = root.findtext(f"{NS}Ack")
    errs = []
    for se in root.findall(f"{NS}Errors"):
        errs.append({
            "code": se.findtext(f"{NS}ErrorCode"),
            "severity": se.findtext(f"{NS}SeverityCode"),
            "short": se.findtext(f"{NS}ShortMessage"),
            "long": se.findtext(f"{NS}LongMessage"),
            "params": [p.findtext(f"{NS}Value") for p in se.findall(f"{NS}ErrorParameters")],
        })
    return ack, errs, None


def show(ack, errs, transport):
    if transport:
        print(f"    -> TRANSPORT ERROR: {transport}")
        return
    print(f"    -> Ack = {ack}")
    for e in errs:
        sev = e["severity"] or "?"
        line = f"       [{e['code']}/{sev}] {e['long'] or e['short']}"
        print(line)
        if e["params"] and any(e["params"]):
            print(f"          invalid combo(s): {', '.join(p for p in e['params'] if p)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sku")
    ap.add_argument("--rule", choices=["A", "B"], help="force a rule (default: classify from category)")
    ap.add_argument("--token")
    ap.add_argument("--one-by-one", action="store_true",
                    help="also probe every row individually to map exactly which combos eBay rejects")
    args = ap.parse_args()
    tok = token(args)

    lid, category = listing_id_and_category(args.sku, tok)
    rule = args.rule
    if not rule:
        tree = CP.load_tree()
        inc, exc, default = CP.load_config()
        rule, why = (CP.classify(category, tree, inc, exc, default) if (category and tree) else (default, "no tree"))
        print(f"category {category} -> Rule {rule} ({why})")
    sd, res = build_rows(args.sku, rule)
    rows = res.get("rows", [])
    print(f"SKU {args.sku}  listingId={lid}  donor={sd.get('model')} [{sd.get('series')}] engine={sd.get('engine_family')}")
    print(f"Rule {rule}: {len(rows)} row(s), engines={res.get('donor_engines')}")
    for r in rows:
        print("   ", {k: r[k] for k in ("Year", "Make", "Model") if k in r}, ("Trim=" + r["Trim"]) if r.get("Trim") else "")

    print("\n[1] Full set, WITH Trim (what the runner pushes):")
    ack, errs, tr = trading_write(lid, rows, tok)
    show(ack, errs, tr)
    if ack in ("Success", "Warning"):
        print("    SUCCESS - trims accepted as-is. Displayed fitment now set.")
        return

    base_rows, seen = [], set()
    for r in rows:
        k = (r["Year"], r["Make"], r["Model"])
        if k not in seen:
            seen.add(k)
            base_rows.append({"Year": r["Year"], "Make": r["Make"], "Model": r["Model"]})
    print(f"\n[2] Drop Trim -> {len(base_rows)} base Year/Make/Model row(s):")
    ack, errs, tr = trading_write(lid, base_rows, tok)
    show(ack, errs, tr)
    if ack in ("Success", "Warning"):
        print("    SUCCESS without Trim -> the TRIM values are what eBay's catalog rejects.")
        if not args.one_by_one:
            return

    if args.one_by_one:
        print("\n[3] Probing each row individually (which exact combos does eBay accept?):")
        for r in rows:
            ack, errs, tr = trading_write(lid, [r], tok)
            tag = {k: r[k] for k in ("Year", "Make", "Model") if k in r}
            trim = (" Trim=" + r["Trim"]) if r.get("Trim") else ""
            verdict = "OK " if ack in ("Success", "Warning") else "BAD"
            codes = ",".join(e["code"] for e in errs) if errs else ""
            print(f"    {verdict} {tag}{trim}   {codes}")
        print("    (NOTE: this left the last single row as the listing's fitment -- re-push properly after.)")


if __name__ == "__main__":
    main()
