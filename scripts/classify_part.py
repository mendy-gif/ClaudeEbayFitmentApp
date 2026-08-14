#!/usr/bin/env python3
"""
Classify an eBay listing as Rule A (whole chassis family) or Rule B (engine part,
fitment restricted to the donor's engine) purely from its eBay categoryId
(docs/DESIGN.md sec 6.1).

Logic (no keyword matching — Dismantly's categories are clean):
  - Look the categoryId up in the fetched Motors category tree
    (data/ebay_motors_categories.json, produced by ebay_fetch_categories.py).
  - If the category (or any ancestor) is in rule_b_categories.json 'exclude_ids'
    -> Rule A (explicit override).
  - Else if the category (or any ancestor) is under an 'include_ancestors' branch
    -> Rule B (engine part).
  - Else -> Rule A (a body/interior/other part).
  - If the categoryId is not found in the tree -> the configured default
    (Rule B, the safe/narrow default).

Usage:
  python3 scripts/classify_part.py 33615            # one category id
  python3 scripts/classify_part.py 33615 33707 6759 # several
  python3 scripts/classify_part.py --self-test      # runs without the fetched tree
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TREE = os.path.join(ROOT, "data", "ebay_motors_categories.json")
CONFIG = os.path.join(ROOT, "data", "rule_b_categories.json")


def load_config():
    cfg = json.load(open(CONFIG, encoding="utf-8"))
    include = {a["id"] for a in cfg.get("include_ancestors", []) if a.get("id")}
    exclude = set(cfg.get("exclude_ids", []))
    default = cfg.get("default_when_category_unknown", "B").upper()
    return include, exclude, default


def load_tree():
    if not os.path.exists(TREE):
        return None
    data = json.load(open(TREE, encoding="utf-8"))
    by_id = {}
    for n in data["nodes"]:
        by_id[str(n["categoryId"])] = n
    return by_id


def classify(category_id, by_id, include, exclude, default):
    cid = str(category_id)
    node = by_id.get(cid) if by_id else None
    if node is None:
        return default, "category not in tree -> default"
    chain = [str(a) for a in node.get("ancestors", [])] + [cid]
    if exclude & set(chain):
        return "A", f"excluded branch in {node['path']}"
    if include & set(chain):
        hit = next(i for i in chain if i in include)
        return "B", f"under engine ancestor {hit} ({node['path']})"
    return "A", f"not under an engine branch ({node['path']})"


def main():
    args = sys.argv[1:]
    include, exclude, default = load_config()

    if "--self-test" in args:
        # Simulate a tiny tree to prove the logic without the fetched file.
        by_id = {
            "33615": {"categoryId": "33615", "ancestors": ["6000", "6030", "33612"], "path": "Motors > Parts > Engines & Engine Parts > Complete Engines"},
            "33707": {"categoryId": "33707", "ancestors": ["6000", "6030", "33642"], "path": "Motors > Parts > Exhaust > Mufflers"},
        }
        include2 = include | {"33612"}
        for cid in ["33615", "33707", "999999"]:
            r, why = classify(cid, by_id, include2, exclude, default)
            print(f"{cid}: Rule {r}  ({why})")
        return

    if not args:
        print("Usage: classify_part.py <categoryId> [<categoryId> ...]  |  --self-test", file=sys.stderr)
        sys.exit(2)

    by_id = load_tree()
    if by_id is None:
        print("NOTE: data/ebay_motors_categories.json not found. Run "
              "scripts/ebay_fetch_categories.py first (only --self-test works without it).", file=sys.stderr)
        sys.exit(1)
    if not include:
        print("NOTE: no include_ancestors with IDs set in rule_b_categories.json yet — everything "
              "classifies as Rule A until the engine branch IDs are filled in.", file=sys.stderr)
    for cid in args:
        r, why = classify(cid, by_id, include, exclude, default)
        print(f"{cid}: Rule {r}  ({why})")


if __name__ == "__main__":
    main()
