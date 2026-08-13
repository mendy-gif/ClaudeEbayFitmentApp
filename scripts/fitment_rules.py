#!/usr/bin/env python3
"""
Rule A / Rule B fitment engine.

Given a donor vehicle (year + model) and whether the part is an engine part, expand
it into the full list of compatible vehicles per the chassis-family rules
(docs/DESIGN.md sec 3):

  Rule A (default: body/interior/electrical/most parts):
      every trim in the donor's chassis  x  the chassis's full US year range
  Rule B (engine & engine-accessory parts):
      the donor's OWN trim only          x  the chassis's full US year range

Output rows are eBay-compatibility-shaped: {"Year", "Make", "Model"}. The Model
values come from data/bmw_chassis_reference.json (currently BMW badge names, which
are PROVISIONAL). Once eBay's real Model vocabulary is pulled
(scripts/ebay_fetch_bmw_catalog.py), drop a mapping into data/ebay_model_map.json
as {"our_trim": "eBay Model string", ...} and it is applied automatically here.
eBay validates and drops rows it doesn't recognize, so exact strings matter before
a real push — but the grouping/expansion logic below is vocabulary-independent.

CLI:
  python3 scripts/fitment_rules.py --model "335i" --year 2014 --rule A
  python3 scripts/fitment_rules.py --model "335i" --year 2012 --rule B
  python3 scripts/fitment_rules.py --demo
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REF_PATH = os.path.join(ROOT, "data", "bmw_chassis_reference.json")
MAP_PATH = os.path.join(ROOT, "data", "ebay_model_map.json")
MAKE = "BMW"
# Upper bound used when a still-selling chassis has us_end_year set to a rolling year.
# The reference file already stores the current year; this is just a safety clamp.
MAX_YEAR = 2027


def load_reference(path=REF_PATH):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_model_map(path=MAP_PATH):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def _norm(s):
    return " ".join(str(s).lower().split())


def find_chassis_candidates(model, year, reference, body_hint=None, chassis_hint=None):
    """Return ALL chassis rows whose trims contain `model` and whose US year range
    includes `year`. Usually one, but generation overlaps produce more than one
    (e.g. a 2012 335i is BOTH E92 coupe 2007-2013 AND F30 sedan 2012-2018). Optional
    body_hint / chassis_hint narrow an ambiguous match."""
    model_n = _norm(model)
    out = []
    for row in reference:
        trims_n = {_norm(t) for t in row.get("trims", [])}
        if model_n not in trims_n:
            continue
        start = row.get("us_start_year")
        end = row.get("us_end_year") or MAX_YEAR
        if start is None or not (start <= year <= end):
            continue
        if chassis_hint and _norm(chassis_hint) != _norm(row.get("chassis_code", "")):
            continue
        if body_hint and _norm(body_hint) not in _norm(row.get("body_style", "")):
            continue
        out.append(row)
    return out


def expand(donor_model, year, rule, reference=None, model_map=None, body_hint=None, chassis_hint=None):
    """Expand a donor into eBay-shaped compatibility rows. rule in {'A','B'}."""
    reference = reference if reference is not None else load_reference()
    model_map = model_map if model_map is not None else load_model_map()

    cands = find_chassis_candidates(donor_model, year, reference, body_hint, chassis_hint)
    if not cands:
        return {"ok": False, "reason": f"No chassis found for {donor_model} ({year}). "
                                       f"Check the model string matches a trim in the reference.",
                "rows": []}
    if len(cands) > 1:
        return {"ok": False, "ambiguous": True,
                "reason": f"{donor_model} ({year}) matches {len(cands)} chassis (generation overlap). "
                          f"Disambiguate with the donor's body style or chassis.",
                "candidates": [{"chassis_code": c["chassis_code"], "series": c.get("series"),
                                "body_style": c.get("body_style"),
                                "year_range": [c["us_start_year"], c.get("us_end_year")]} for c in cands],
                "rows": []}
    row = cands[0]

    start = row["us_start_year"]
    end = row.get("us_end_year") or MAX_YEAR
    years = list(range(start, min(end, MAX_YEAR) + 1))

    if rule.upper() == "A":
        models = list(row.get("trims", []))
    elif rule.upper() == "B":
        models = [donor_model]
    else:
        raise ValueError("rule must be 'A' or 'B'")

    def to_ebay(m):
        return model_map.get(m, m)  # passthrough until eBay names are mapped

    rows = []
    seen = set()
    for m in models:
        ebay_model = to_ebay(m)
        for y in years:
            key = (y, ebay_model)
            if key not in seen:
                seen.add(key)
                rows.append({"Year": y, "Make": MAKE, "Model": ebay_model})
    return {"ok": True, "chassis": row["chassis_code"], "series": row.get("series"),
            "rule": rule.upper(), "year_range": [start, min(end, MAX_YEAR)],
            "unmapped": [m for m in models if m not in model_map],
            "rows": rows}


def _demo():
    ref = load_reference()
    for model, year, rule in [("335i", 2014, "A"), ("335i", 2012, "B"),
                              ("328i", 2008, "A"), ("M3", 2016, "A"), ("xDrive35i", 2015, "B")]:
        res = expand(model, year, rule, ref)
        head = f"{model} {year} Rule {rule}"
        if not res["ok"]:
            if res.get("ambiguous"):
                opts = ", ".join(f"{c['chassis_code']} ({c['body_style']})" for c in res["candidates"])
                print(f"{head}: AMBIGUOUS -> {opts}")
            else:
                print(f"{head}: {res['reason']}")
            continue
        models = sorted({r['Model'] for r in res['rows']})
        print(f"{head}: chassis {res['chassis']} ({res['series']}), years {res['year_range'][0]}-{res['year_range'][1]}, "
              f"{len(res['rows'])} rows across models {models}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", help="Donor model/trim, e.g. '335i'")
    ap.add_argument("--year", type=int, help="Donor model year")
    ap.add_argument("--rule", choices=["A", "B", "a", "b"], help="A=whole chassis family, B=engine (donor trim only)")
    ap.add_argument("--body", help="Body-style hint to disambiguate overlaps (e.g. 'Coupe', 'Sedan')")
    ap.add_argument("--chassis", help="Chassis-code hint to disambiguate (e.g. 'F30')")
    ap.add_argument("--json", action="store_true", help="Print full JSON rows")
    ap.add_argument("--demo", action="store_true", help="Run built-in examples")
    args = ap.parse_args()

    if args.demo:
        _demo()
        return
    if not (args.model and args.year and args.rule):
        ap.error("provide --model, --year and --rule (or use --demo)")

    res = expand(args.model, args.year, args.rule, body_hint=args.body, chassis_hint=args.chassis)
    if not res["ok"]:
        if res.get("ambiguous"):
            print(res["reason"])
            print("Candidates:")
            for c in res["candidates"]:
                print(f"  {c['chassis_code']:12} {c['series']:24} {c['body_style']}  ({c['year_range'][0]}-{c['year_range'][1]})")
            print("Re-run with --body '<style>' or --chassis '<code>'.")
            sys.exit(2)
        print(res["reason"], file=sys.stderr)
        sys.exit(1)
    print(f"Donor: {args.model} {args.year}  ->  chassis {res['chassis']} ({res['series']}), Rule {res['rule']}")
    print(f"Year range: {res['year_range'][0]}-{res['year_range'][1]}   Rows: {len(res['rows'])}")
    if res["unmapped"]:
        print(f"NOTE: {len(res['unmapped'])} model name(s) not yet mapped to eBay vocabulary "
              f"(passthrough): {res['unmapped']}")
    if args.json:
        print(json.dumps(res["rows"], indent=2))
    else:
        for m in sorted({r["Model"] for r in res["rows"]}):
            yrs = sorted(r["Year"] for r in res["rows"] if r["Model"] == m)
            print(f"  BMW {m}: {yrs[0]}-{yrs[-1]}")


if __name__ == "__main__":
    main()
