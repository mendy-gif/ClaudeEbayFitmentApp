#!/usr/bin/env python3
"""
Diagnose why a SKU's fitment does or does not DISPLAY on its live listing.

WHAT WE LEARNED WITH THIS TOOL (the answer, kept here so it isn't rediscovered):

  1. eBay's vehicle catalog spells trims with a body-style suffix. Our rules emit BMW
     shorthand ("xDrive35i"); the catalog wants "xDrive35i Sport Utility 4-Door". The
     Inventory API accepts the shorthand (HTTP 200, stores it, reads it back) but the
     listing DISPLAYS nothing for it. This hit every Rule B (engine) part -- Rule A rows
     are trimless, so they always displayed. scripts/ebay_compat_catalog.py now repairs
     trims against eBay's catalog before the push, which fixes it.

  2. The Trading API CANNOT write these listings at all. ReviseFixedPriceItem answers
     every attempt with [21919474] "Inventory-based listing management is not currently
     supported by this tool", because the listings are Inventory-API-managed. Any plan
     that routes the display write through Trading is a dead end; the Inventory write
     displays on its own once the rows are catalog-valid.

So the Trading probes below are now a DIAGNOSTIC ONLY -- expect 21919474 on a normal
inventory-managed listing. The useful output is section [0], which compares the rows we
would push against eBay's actual catalog and names any that would be dropped.

Usage:
  python3 scripts/ebay_trading_debug.py 52566 --token "$(python3 scripts/ebay_auth.py)"
  python3 scripts/ebay_trading_debug.py 52566 --probe-trading   # also try the Trading writes
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
import ebay_compat_catalog as CAT     # noqa: E402

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
    ap.add_argument("--probe-trading", action="store_true",
                    help="also attempt the Trading writes (expect 21919474 on inventory-managed listings)")
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

    print("\n[0] Catalog check -- what eBay's vehicle catalog actually contains:")
    on_unmatched = "drop" if rule == "B" else "trimless"
    good, rep = CAT.validate_rows(rows, on_unmatched=on_unmatched)
    print(f"    {len(rows)} row(s) in -> {len(good)} row(s) that will display "
          f"(kept={rep['kept']} retrimmed={rep['retrimmed']} trimless={rep['trimless']} "
          f"dropped={rep['dropped_vehicle'] + rep['dropped_trim']} lookup_failed={rep['lookup_failed']})")
    for n in rep["notes"][:10]:
        print(f"       ! {n}")
    for g in good:
        print("    OK ", g)
    if not args.probe_trading:
        print("\n(Trading write probes skipped -- pass --probe-trading to run them. They are")
        print(" expected to fail with 21919474 on an Inventory-API-managed listing.)")
        return

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
