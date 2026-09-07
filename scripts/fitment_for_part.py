#!/usr/bin/env python3
"""Fitment rows for a part that is NOT on eBay yet.

The nightly sweep works backwards: it finds a live listing, reads its categoryId,
and expands the donor. A part still being prepared in Dismantly has no listing and
no category, so this is the same expansion driven from what Dismantly does hold --
the donor vehicle and the part_type.

Dismantly writes the rows BEFORE the part is priced. Pricing auto-publishes it, so
the fitment is present at listing-create time rather than edited in afterwards.
That ordering is the whole point: editing fitment onto a listing that already
exists destroyed it in 35 of 41 measured attempts, while create-time fitment is
durable (8,818 relisted listings display fine).

Input:  JSON list of parts on stdin or in a file. Per part:
          tag_id       (required) our SKU
          series       chassis code, e.g. "F30" -- preferred
          model        donor model, e.g. "335i" -- disambiguates a bare series
          year         donor model year (narrows lights to their LCI side)
          engine       engine family, e.g. "N55" (Rule B)
          part_type    Dismantly part type -- decides Rule A vs B
          category_id  eBay categoryId, if the auto-upload template knows it.
                       Preferred over part_type: it is the path the sweep uses.
Output: JSON list, one object per part, with `rows` ready for Dismantly's picker.

A part we cannot classify confidently emits NO rows and says why. That is
deliberate -- wrong fitment is worse than none, and an unknown part_type is a
one-line fix to data/part_type_rules.json, not a reason to guess.

Usage:
  python3 scripts/fitment_for_part.py parts.json > fitment.json
  echo '[{"tag_id":"65499","series":"F07","model":"535i GT","engine":"N55",
          "part_type":"Front Left Driver Fender"}]' | python3 scripts/fitment_for_part.py
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
DATA = os.path.join(os.path.dirname(HERE), "data")

import classify_part
import ebay_compat_catalog as CAT
import fitment_rules as FR

# Lights change at a facelift, so they get the donor's side of the LCI split only.
# Matched on part_type when no categoryId is available; the categoryId path uses
# data/lci_categories.json, which is what the sweep already trusts.
_LIGHT_WORDS = ("headlight", "headlamp", "tail light", "taillight", "tail lamp", "taillamp")
_NOT_THE_LIGHT = ("ballast", "bulb", "bracket", "washer", "nozzle", "wire", "harness",
                  "cover", "trim", "module", "switch", "relay", "socket", "connector")


def _lci_by_part_type(part_type):
    p = (part_type or "").lower()
    return any(w in p for w in _LIGHT_WORDS) and not any(w in p for w in _NOT_THE_LIGHT)


def load_part_type_rules():
    with open(os.path.join(DATA, "part_type_rules.json"), encoding="utf-8") as fh:
        return json.load(fh)


def classify(part, ptr, tree=None):
    """(rule, lci_sensitive, why) -- or (None, _, why) when we should not guess."""
    cid = part.get("category_id")
    if cid and tree:
        by_id, include, exclude, default = tree
        rule, why = classify_part.classify(str(cid), by_id, include, exclude, default)
        lci_inc, lci_exc = classify_part.load_lci_config()
        return rule, classify_part.lci_restricted(str(cid), by_id, lci_inc, lci_exc), \
            f"eBay category {cid}: {why}"
    part_type = (part.get("part_type") or "").strip()
    if not part_type:
        return None, False, "no part_type and no category_id"
    rule = ptr["rules"].get(part_type)
    if not rule:
        if part_type in ptr["ambiguous"]:
            return None, False, (f"part type '{part_type}' classifies as BOTH rules on eBay "
                                 f"(coarse category) - review it into non_engine_part_types.json")
        return None, False, f"part type '{part_type}' has no rule yet - add it to part_type_rules.json"
    return rule, _lci_by_part_type(part_type), f"part type '{part_type}'"


def fitment_for(part, ref, emap, ebay, ptr, tree=None):
    out = {"tag_id": part.get("tag_id"), "rows": []}
    rule, lci_sensitive, why = classify(part, ptr, tree)
    out["rule"], out["classified_by"] = rule, why
    if rule is None:
        out["skipped"] = why
        return out

    series, model = part.get("series"), part.get("model")
    if not series:
        out["skipped"] = "no chassis/series on the donor"
        return out
    row, note = FR.resolve_chassis(series, model, ref)
    if row is None:
        out["skipped"] = note
        return out
    chassis = row["chassis_code"]
    out["chassis"] = chassis

    window = None
    if lci_sensitive:
        window = FR.lci_window(chassis, part.get("year"), ref)
        out["lci_window"] = list(window) if window else None

    res = FR.expand_from_chassis(chassis, rule, ref, emap, ebay,
                                 engine=part.get("engine"), donor_model=model,
                                 year_window=window)
    if not res.get("ok"):
        out["skipped"] = res.get("reason", "expansion failed")
        return out

    # Same gatekeeper the sweep uses: a trim eBay does not spell our way is stored,
    # returns HTTP 200, and is then silently never displayed.
    rows, report = CAT.validate_rows(res["rows"], on_unmatched="drop")
    CAT.save_cache()
    out["rows"] = rows
    out["n"] = len(rows)
    out["dropped_by_catalog"] = len(res["rows"]) - len(rows)
    out["models"] = sorted({r["Model"] for r in rows})
    if not rows:
        out["skipped"] = f"all {len(res['rows'])} rows failed eBay's catalog"
    return out


def main():
    src = open(sys.argv[1], encoding="utf-8") if len(sys.argv) > 1 else sys.stdin
    parts = json.load(src)
    if isinstance(parts, dict):
        parts = [parts]
    ref, emap, ebay = FR.load_all()
    ptr = load_part_type_rules()
    try:
        by_id = classify_part.load_tree()
        include, exclude, default = classify_part.load_config()
        tree = (by_id, include, exclude, default)
    except (OSError, ValueError):
        tree = None                      # part_type path only
    results = [fitment_for(p, ref, emap, ebay, ptr, tree) for p in parts]
    json.dump(results, sys.stdout, indent=2)
    sys.stdout.write("\n")
    ok = sum(1 for r in results if r["rows"])
    print(f"{ok}/{len(results)} parts got fitment "
          f"({sum(r.get('n', 0) for r in results)} rows total)", file=sys.stderr)


if __name__ == "__main__":
    main()
