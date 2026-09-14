#!/usr/bin/env python3
"""Fitment for a PART NUMBER, not a vehicle.

`fitment_for_part.py` answers "this part came off THIS car, what else does it fit?" and
infers the rest from chassis rules. A Dismantly DPC is the other shape: it IS a part
number, and the question is simply which cars BMW says use it.

That is what the ETK is for, so this does not infer anything. BMW states the complete set,
and the models it omits are ones the part does not fit -- so the rows are used LITERALLY and
never widened to the chassis family. On a door that distinction is the whole game: an F31
Sport Wagon rear door carries a different part number from the F30 sedan's, so asking BMW
excludes the wagon for free, where a model-level expansion would wrongly claim it.

Input:  JSON list on stdin or in a file, one object per DPC:
          {"id": "<dpc number>", "part_numbers": ["7327345", "7386741"]}
        A bare list of part-number strings also works and is treated as one group.
Output: JSON list, one object per DPC, with `rows` ready for the fitment picker.

Usage:
  echo '[{"id":"DPC-1","part_numbers":["7327345"]}]' | python3 scripts/fitment_for_partnumbers.py
  python3 scripts/fitment_for_partnumbers.py dpcs.json --include-option-gated
"""
import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ebay_compat_catalog as CAT
import etk_fitment as ETK


def rows_for(part_numbers, confirmed_only=True):
    """ETK rows -> deduped eBay {Year, Make, Model[, Trim]} for one group of part numbers."""
    raw = ETK.run_emitter(part_numbers, confirmed_only=confirmed_only)
    seen, dropped = set(), collections.Counter()
    for r in raw:
        hit = ETK.to_ebay_vehicle(r.get("model"))
        if not hit:
            dropped["model not usable on eBay (MINI/Rolls/unparsed)"] += 1
            continue
        model, trim = hit
        fr, to = (r.get("year_from") or "").strip(), (r.get("year_to") or "").strip()
        if not fr.isdigit():
            dropped["no year"] += 1
            continue
        hi = int(to) if to.isdigit() else int(fr)
        for year in range(max(int(fr), 1990), hi + 1):
            seen.add((year, model, trim or ""))
    rows = [dict({"Year": y, "Make": "BMW", "Model": m}, **({"Trim": t} if t else {}))
            for (y, m, t) in sorted(seen)]
    return rows, raw, dropped


def _prune_bodies(rows, words):
    """Replace each TRIMLESS row with its explicit catalog trims, minus the excluded bodies.

    A trimless row is a wildcard: eBay expands it to every trim of that Year/Model. That is
    what we want on most parts, and exactly what we must not have here -- see --exclude-body.
    Naming the surviving trims explicitly is the only way to keep a body style out.
    """
    words = [w.lower() for w in words]
    out, pruned = [], collections.Counter()
    for r in rows:
        if r.get("Trim"):
            if any(w in r["Trim"].lower() for w in words):
                pruned[r["Trim"]] += 1
            else:
                out.append(r)
            continue
        catalog = CAT.trims(r["Year"], r["Make"], r["Model"])
        if not catalog:
            out.append(r)                     # nothing to enumerate; leave the wildcard alone
            continue
        kept = [t for t in catalog if not any(w in t.lower() for w in words)]
        if len(kept) == len(catalog):
            out.append(r)                     # no excluded body here -- keep it trimless
            continue
        for t in kept:
            out.append({"Year": r["Year"], "Make": r["Make"], "Model": r["Model"], "Trim": t})
        for t in catalog:
            if t not in kept:
                pruned[t] += 1
    return out, dict(pruned)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file", nargs="?", help="JSON input (default: stdin)")
    ap.add_argument("--exclude-body", action="append", metavar="WORD", default=[],
                    help="drop body styles whose eBay trim contains WORD, e.g. --exclude-body wagon. "
                         "Needed on REAR doors: BMW gives the F31 Sport Wagon its own part number so "
                         "the catalogue already excludes it, but eBay files 'Base Wagon 4-Door' as a "
                         "TRIM of model '328i xDrive' -- so a trimless row wildcard-expands and puts "
                         "the wagon straight back. Front doors and SUVs do not need this.")
    ap.add_argument("--include-option-gated", action="store_true",
                    help="keep rows BMW gates on a build option (e.g. '552 Adaptive LED "
                         "headlight'). Safe on a whole-vehicle part like a door, NOT on an "
                         "engine part, where it claims fitment we cannot verify.")
    args = ap.parse_args()

    src = open(args.file, encoding="utf-8") if args.file else sys.stdin
    groups = json.load(src)
    if groups and isinstance(groups[0], str):                 # a bare list of part numbers
        groups = [{"id": "group", "part_numbers": groups}]

    out = []
    for g in groups:
        pns = [str(p).strip() for p in (g.get("part_numbers") or []) if str(p).strip()]
        rec = {"id": g.get("id"), "part_numbers": pns, "rows": []}
        if not pns:
            rec["skipped"] = "no part numbers"
            out.append(rec)
            continue
        rows, raw, dropped = rows_for(pns, confirmed_only=not args.include_option_gated)
        if not raw:
            rec["skipped"] = "no US fitment in BMW's catalogue for these part numbers"
            out.append(rec)
            continue
        # Same gatekeeper the sweep uses. on_unmatched="drop" because an ETK row is never
        # widened to trimless: a bare "X5" row is a wildcard and would claim more than BMW said.
        rows, _rep = CAT.validate_rows(rows, on_unmatched="drop")
        if args.exclude_body:
            rows, pruned = _prune_bodies(rows, args.exclude_body)
            if pruned:
                rec["excluded_bodies"] = pruned
        rec["rows"] = rows
        rec["n"] = len(rows)
        rec["models"] = sorted({r["Model"] for r in rows})
        rec["years"] = [min(r["Year"] for r in rows), max(r["Year"] for r in rows)] if rows else []
        rec["etk_rows_read"] = len(raw)
        if dropped:
            rec["dropped"] = dict(dropped)
        if not rows:
            rec["skipped"] = "no rows survived eBay's catalog"
        out.append(rec)
    CAT.save_cache()

    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    ok = sum(1 for r in out if r["rows"])
    print(f"{ok}/{len(out)} groups got fitment ({sum(r.get('n', 0) for r in out)} rows)",
          file=sys.stderr)


if __name__ == "__main__":
    main()
