#!/usr/bin/env python3
"""
Pull eBay's US Motors parts category subtree via the Taxonomy API, so we can build
the Rule-A vs Rule-B classifier from real category IDs (docs/DESIGN.md sec 6.1).

getCategorySubtree(category_tree_id=100, category_id=ROOT) returns the nested tree
rooted at ROOT. We flatten it to a list of nodes with their ancestor chain, which
lets classify_part.py decide "is this category under an engine branch?".

Read-only. Uses the same OAuth token as the other scripts (token.txt or --token).

Usage (in the Codespace):
  python3 scripts/ebay_fetch_categories.py                 # whole Car&Truck Parts tree (root 6030)
  python3 scripts/ebay_fetch_categories.py --category 33612 # just the Engines & Engine Parts subtree
  python3 scripts/ebay_fetch_categories.py --engines-only   # print the engine (33612) leaves and exit

Writes the flattened tree to data/ebay_motors_categories.json.
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
CAR_TRUCK_PARTS = "6030"   # Car & Truck Parts & Accessories (US Motors)
ENGINES = "33612"          # Car & Truck Engines & Engine Parts

# Roots to fetch. 6030 alone is NOT enough: a category missing from this file defaults to
# Rule B (rule_b_categories.json), and Rule B on a part with no engine emits nothing at all.
# That is how speakers, subwoofers, amplifiers, screens, head units and a catch-all "Other"
# came to be classified as engine parts -- 89 listings produced no fitment and another 90
# were pushed wrongly engine-restricted in the 2026-08-24 sweep alone. Those categories are
# real, they simply hang off sibling branches of Parts & Accessories (6028).
DEFAULT_ROOTS = [
    "6030",     # Car & Truck Parts & Accessories -- the bulk of the inventory
    "38635",    # In-Car Technology, GPS & Security -- speakers, head units, screens, alarms
    "107057",   # Performance & Racing Parts -- incl. the "Other" catch-all at 107062
]


def load_token(args):
    if args.token:
        return args.token.strip()
    env = os.environ.get("EBAY_ACCESS_TOKEN")
    if env:
        return env.strip()
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    print("ERROR: no token. Put it in token.txt or pass --token.", file=sys.stderr)
    sys.exit(2)


def get_subtree(base, category_id, token):
    url = (f"{base}/commerce/taxonomy/v1/category_tree/100/get_category_subtree?"
           + urllib.parse.urlencode({"category_id": category_id}))
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8", "replace"))
        except ValueError:
            return e.code, {}
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def flatten(node, ancestors, path, out):
    cat = node.get("category", {})
    cid = cat.get("categoryId")
    name = cat.get("categoryName", "")
    leaf = bool(node.get("leafCategoryTreeNode"))
    here = path + [name]
    out.append({"categoryId": cid, "categoryName": name, "leaf": leaf,
                "ancestors": list(ancestors), "path": " > ".join(here)})
    for child in node.get("childCategoryTreeNodes", []) or []:
        flatten(child, ancestors + [cid], here, out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--token")
    ap.add_argument("--sandbox", action="store_true")
    ap.add_argument("--category", default=None,
                    help=f"Single root to fetch (default: the {len(DEFAULT_ROOTS)} roots in DEFAULT_ROOTS)")
    ap.add_argument("--engines-only", action="store_true", help="Fetch the 33612 engine subtree and print its leaves")
    args = ap.parse_args()

    base = API["sandbox" if args.sandbox else "prod"]
    token = load_token(args)
    if args.engines_only:
        roots = [ENGINES]
    elif args.category:
        roots = [args.category]
    else:
        roots = list(DEFAULT_ROOTS)

    flat, seen = [], set()
    for root in roots:
        status, payload = get_subtree(base, root, token)
        if status != 200 or "categorySubtreeNode" not in payload:
            # One bad root must not silently shrink the file -- a category that vanishes
            # from here starts defaulting to Rule B again, which is the bug this fixes.
            print(f"Root {root}: HTTP {status}: {json.dumps(payload)[:400]}", file=sys.stderr)
            sys.exit(1)
        part = []
        flatten(payload["categorySubtreeNode"], [], [], part)
        added = 0
        for n in part:
            if n["categoryId"] in seen:
                continue
            seen.add(n["categoryId"])
            flat.append(n)
            added += 1
        print(f"Root {root}: {len(part)} nodes ({added} new).")
    root = roots[0]
    leaves = [n for n in flat if n["leaf"]]
    print(f"Total: {len(flat)} nodes, {len(leaves)} leaf categories.")

    if args.engines_only:
        print("\nEngine (33612) LEAF categories — these are the core Rule-B set:")
        for n in leaves:
            print(f"  {n['categoryId']:>10}  {n['path']}")
        return

    out = os.path.join(ROOT, "data", "ebay_motors_categories.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"root": root, "roots": roots, "count": len(flat), "nodes": flat}, f, indent=2)
    print(f"Saved {len(flat)} nodes -> {os.path.relpath(out, ROOT)}")
    # Show the top-level branches so we can pick the engine-accessory ancestors to include.
    top = [n for n in flat if n["ancestors"] == [root]]
    print("\nTop-level branches under this root (candidate Rule-B ancestors to decide on):")
    for n in top:
        print(f"  {n['categoryId']:>10}  {n['categoryName']}")


if __name__ == "__main__":
    main()
