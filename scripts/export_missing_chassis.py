#!/usr/bin/env python3
"""
Export the BMW donors that have no chassis code, as a spreadsheet to work through.

The chassis (series) is the one field the fitment rules cannot do without: it is what
turns "a BMW part" into "fits these vehicles". ~1,176 BMW products in Shopify have no
`veh_series_` tag, so they produce no fitment at all.

Their chassis is usually sitting in the listing title ("2011 BMW 335i E93"), so this
pulls each title from eBay and pre-extracts every chassis code it can find, checked
against data/bmw_chassis_reference.json. You confirm or correct rather than research.

Titles often name SEVERAL chassis ("OEM BMW F22 F30 F32 F36") because the part fits all
of them -- that is a fitment range, not the donor. Those rows are flagged `AMBIGUOUS`
with every candidate listed, since only you know which car it actually came off.

Reads eBay's Inventory API for titles: 2,000,000 calls/day, so this costs nothing that
matters. It does NOT touch the Trading API (5,000/day) that paces the sweep.

Usage:
  python3 scripts/export_missing_chassis.py                     # -> data/missing_chassis.csv
  python3 scripts/export_missing_chassis.py --limit 50          # quick sample
  python3 scripts/export_missing_chassis.py --out ~/Desktop/x.csv
"""
import argparse
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
STORE_SLUG = "oe-mgarage"          # for the clickable Shopify admin link


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


def chassis_codes():
    ref = json.load(open(os.path.join(ROOT, "data", "bmw_chassis_reference.json"),
                         encoding="utf-8"))
    rows = ref if isinstance(ref, list) else (ref.get("rows") or [])
    return sorted({(r.get("chassis_code") or "").split()[0].split("/")[0].upper()
                   for r in rows if r.get("chassis_code")} - {""})


def ebay_title(sku, tok):
    q = urllib.parse.quote(str(sku), safe="")
    req = urllib.request.Request(f"{BASE}/sell/inventory/v1/inventory_item/{q}", headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "replace") or "{}")
        return (d.get("product") or {}).get("title") or ""
    except (urllib.error.HTTPError, urllib.error.URLError, ValueError, OSError):
        return ""


def find_chassis(title, codes):
    """Chassis codes appearing as whole words in the title, in order of appearance."""
    if not title:
        return []
    up = title.upper()
    hits = []
    for c in codes:
        if re.search(rf"(?<![A-Z0-9]){re.escape(c)}(?![A-Z0-9])", up) and c not in hits:
            hits.append((up.index(c), c))
    return [c for _, c in sorted(hits)]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "missing_chassis.csv"))
    ap.add_argument("--token")
    args = ap.parse_args()
    tok = token(args)

    donors = json.load(open(os.path.join(ROOT, "data", "shopify_donors.json"), encoding="utf-8"))
    codes = chassis_codes()
    missing = [(s, d) for s, d in donors.items()
               if (d.get("make") or "").upper() == "BMW" and not d.get("series")]
    missing.sort(key=lambda kv: kv[0])
    if args.limit:
        missing = missing[: args.limit]
    print(f"{len(missing)} BMW donor(s) with no chassis code. Fetching titles from eBay...")

    rows, counts = [], {"suggested": 0, "ambiguous": 0, "none": 0}
    for i, (sku, d) in enumerate(missing, 1):
        title = ebay_title(sku, tok)
        found = find_chassis(title, codes)
        if len(found) == 1:
            status, suggested = "SUGGESTED", found[0]
            counts["suggested"] += 1
        elif len(found) > 1:
            status, suggested = "AMBIGUOUS", ""
            counts["ambiguous"] += 1
        else:
            status, suggested = "NO CHASSIS IN TITLE", ""
            counts["none"] += 1
        rows.append({
            "sku": sku,
            "status": status,
            "suggested_chassis": suggested,
            "candidates_in_title": " ".join(found),
            "title": title,
            "model": d.get("model") or "",
            "engine": d.get("engine_family") or "",
            "part_type": d.get("part_type") or "",
            "shopify_admin": f"https://admin.shopify.com/store/{STORE_SLUG}/products?query={sku}",
        })
        if i % 100 == 0:
            print(f"  {i}/{len(missing)}...", file=sys.stderr)

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["sku"])
        w.writeheader()
        w.writerows(rows)

    print(f"\n  {counts['suggested']:>5}  SUGGESTED           one chassis in the title -- just confirm")
    print(f"  {counts['ambiguous']:>5}  AMBIGUOUS           several named; pick the donor car")
    print(f"  {counts['none']:>5}  NO CHASSIS IN TITLE needs the VIN or the car it came off")
    print(f"\nWrote {len(rows)} row(s) -> {args.out}")


if __name__ == "__main__":
    main()
