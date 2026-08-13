#!/usr/bin/env python3
"""
Pull eBay's authoritative vehicle vocabulary for BMW via the Taxonomy API, so our
chassis reference can be reconciled to the exact Year/Make/Model/Trim strings eBay
validates against (see docs/DESIGN.md sec 3.1).

eBay has NO chassis codes. It exposes vehicle aspects (Year, Make, Model, Trim,
Engine) and their valid values through:
  - getCompatibilityProperties      -> the aspect names for a category
  - getCompatibilityPropertyValues   -> valid values (e.g. every Model for Make=BMW)
Both live under category_tree_id 100 (US Motors / Parts & Accessories).

Read-only. Uses the same OAuth user token as the inventory test (a base-scope token
is enough for Taxonomy). Reads token from ./token.txt or --token.

Usage (in the Codespace, where token.txt exists):
  python3 scripts/ebay_fetch_bmw_catalog.py                 # BMW Model list
  python3 scripts/ebay_fetch_bmw_catalog.py --properties    # list aspect names
  python3 scripts/ebay_fetch_bmw_catalog.py --category 6763 # try a specific leaf
  python3 scripts/ebay_fetch_bmw_catalog.py --property Trim --filter "Make:BMW,Model:328i"

Writes BMW Model results to data/ebay_bmw_models.json.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API = {"prod": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
# Candidate US-Motors leaf categories that support "parts compatibility by
# specification". The vehicle catalog is shared across them, so any that works
# returns the same Make/Model values. We try them in order until one succeeds.
CANDIDATE_CATEGORIES = ["33615", "33559", "33694", "171047", "33637", "6763"]


def get_json(base, path, params, token):
    url = f"{base}{path}?" + urllib.parse.urlencode(params, safe=":,")
    req = urllib.request.Request(url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        try:
            body = json.loads(body)
        except ValueError:
            pass
        return e.code, body
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def load_token(args):
    if args.token:
        return args.token.strip()
    env = os.environ.get("EBAY_ACCESS_TOKEN")
    if env:
        return env.strip()
    tok_path = os.path.join(ROOT, "token.txt")
    if os.path.exists(tok_path):
        with open(tok_path, encoding="utf-8") as f:
            return "".join(f.read().split())
    print("ERROR: no token. Put it in token.txt (repo root) or pass --token.", file=sys.stderr)
    sys.exit(2)


def try_categories(base, token, requested):
    cats = [requested] if requested else CANDIDATE_CATEGORIES
    for cat in cats:
        status, payload = get_json(
            base, "/commerce/taxonomy/v1/category_tree/100/get_compatibility_properties",
            {"category_id": cat}, token)
        if status == 200 and isinstance(payload, dict) and payload.get("compatibilityProperties"):
            names = [p.get("name") for p in payload["compatibilityProperties"]]
            return cat, names, None
        last = (status, payload)
    return None, None, last


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--category", help="Leaf category id to query (else auto-detect)")
    ap.add_argument("--property", default="Model", help="Aspect to fetch values for (default Model)")
    ap.add_argument("--filter", default="Make:BMW", help="Aspect filter (default Make:BMW)")
    ap.add_argument("--properties", action="store_true", help="Just list the aspect names and exit")
    args = ap.parse_args()

    base = API["sandbox" if args.sandbox else "prod"]
    token = load_token(args)

    cat, names, err = try_categories(base, token, args.category)
    if not cat:
        print(f"Could not find a category that supports compatibility. Last response: {err}", file=sys.stderr)
        print("Try passing --category <leaf_id> for a US Motors parts leaf.", file=sys.stderr)
        sys.exit(1)
    print(f"Using category {cat}. Aspect names: {names}")
    if args.properties:
        return

    status, payload = get_json(
        base, "/commerce/taxonomy/v1/category_tree/100/get_compatibility_property_values",
        {"category_id": cat, "compatibility_property": args.property, "filter": args.filter}, token)

    if status != 200:
        print(f"HTTP {status} fetching values: {json.dumps(payload)[:1000]}", file=sys.stderr)
        sys.exit(1)

    values = [v.get("value") for v in payload.get("compatibilityPropertyValues", [])]
    print(f"\n{args.property} values for filter '{args.filter}': {len(values)} found")
    for v in values:
        print(f"  {v}")

    if args.property == "Model" and args.filter == "Make:BMW":
        out = os.path.join(ROOT, "data", "ebay_bmw_models.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump({"make": "BMW", "category_id": cat, "models": values}, f, indent=2)
        print(f"\nSaved {len(values)} BMW models -> {os.path.relpath(out, ROOT)}")


if __name__ == "__main__":
    main()
