#!/usr/bin/env python3
"""Derive a part_type -> Rule A/B map from SKUs we have already classified.

Why this exists: our Rule A/B decision normally comes from the listing's eBay
categoryId (scripts/classify_part.py). A part that is not on eBay yet has no
category, so parts being prepared in Dismantly cannot be classified that way.

The ledger holds 6,006 SKUs whose rule WAS decided from a real category. Joining
that to each SKU's Dismantly part_type gives the same answer keyed by something a
pre-listing part does have. 544 part types are covered, which is 97% of parts.

A part type that the ledger shows under BOTH rules is NOT resolved here: eBay's
categories are coarser than Dismantly's part types (cat 33596 'ECUs & Computer
Modules' holds the engine DME next to Bluetooth and seat modules), so the split is
eBay's noise, not a real distinction. Those fall to data/non_engine_part_types.json
if a human has reviewed them, and are otherwise left unknown on purpose --
emitting no fitment beats emitting wrong fitment.
"""
import collections
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(HERE), "data")


def build():
    led = json.load(open(os.path.join(DATA, "pushed_ledger.json")))
    don = json.load(open(os.path.join(DATA, "shopify_donors.json")))
    override = set(json.load(open(os.path.join(DATA, "non_engine_part_types.json")))["part_types"])

    seen = collections.defaultdict(collections.Counter)
    for sku, entry in led.items():
        part_type = ((don.get(sku) or {}).get("part_type") or "").strip()
        if part_type and entry.get("rule") in ("A", "B"):
            seen[part_type][entry["rule"]] += 1

    rules, ambiguous = {}, {}
    for part_type, counts in seen.items():
        if part_type in override:
            rules[part_type] = "A"          # reviewed: not engine-dependent
        elif not counts["B"]:
            rules[part_type] = "A"
        elif not counts["A"]:
            rules[part_type] = "B"
        else:
            ambiguous[part_type] = dict(counts)
    return rules, ambiguous, seen


def main():
    rules, ambiguous, seen = build()
    out = {
        "_comment": (
            "part_type -> Rule A/B, derived from SKUs already classified by eBay category. "
            "Regenerate with scripts/build_part_type_rules.py. Used by fitment_for_part.py "
            "to classify parts that are not on eBay yet and so have no categoryId. "
            "'ambiguous' types appear under both rules because eBay's categories are coarser "
            "than these part types -- they are deliberately NOT given a rule; review one and "
            "move it into data/non_engine_part_types.json to resolve it as Rule A."
        ),
        "rules": dict(sorted(rules.items())),
        "ambiguous": dict(sorted(ambiguous.items(), key=lambda kv: -sum(kv[1].values()))),
    }
    path = os.path.join(DATA, "part_type_rules.json")
    with open(path, "w") as fh:
        json.dump(out, fh, indent=2, sort_keys=False)
        fh.write("\n")
    a = sum(1 for v in rules.values() if v == "A")
    print(f"{len(seen)} part types seen -> {len(rules)} resolved ({a} A, {len(rules)-a} B), "
          f"{len(ambiguous)} left ambiguous")
    print(f"wrote {path}")


if __name__ == "__main__":
    main()
