#!/usr/bin/env python3
"""
Rule A / Rule B fitment engine (engine-map aware).

Given a donor vehicle (year + model) and whether the part is an engine part,
expand it into eBay-compatibility-shaped rows {Year, Make, Model[, Trim]} per the
chassis-family rules (docs/DESIGN.md sec 3), using:
  - data/bmw_chassis_reference.json  (chassis grouping + US year ranges)
  - data/bmw_engine_map.json         ((chassis,trim) -> engine family; Rule B)
  - data/ebay_bmw_models.json        (eBay's exact Model vocabulary; validation)

Output vocabulary follows eBay's two conventions (docs/DESIGN.md sec 3.1):
  - Sedans/coupes: eBay Model = the badge (328i, 335i, M340i, ...).
  - Nameplate vehicles (X1-X7, XM, Z3/Z4/Z8, i3..iX): eBay Model = the nameplate
    (X5, Z4, i4), and the drivetrain badge is the eBay Trim.

Rules:
  Rule A (body/interior/most parts): whole chassis family.
      sedan   -> every badge Model in the chassis x year range
      nameplate -> the nameplate Model x year range (all trims)
  Rule B (engine parts): restricted to the donor's ENGINE.
      sedan   -> every badge in the chassis sharing the donor's engine family
      nameplate -> Model=nameplate + Trim=each drivetrain badge sharing the engine

Year padding within a correct Model/Trim is safe: eBay drops catalog rows it does
not recognize (partial-acceptance validation), so we emit the full chassis year
range and let eBay trim phantom years.

CLI:
  python3 scripts/fitment_rules.py --model "335i" --year 2014 --rule A
  python3 scripts/fitment_rules.py --model "xDrive40i" --year 2021 --rule B --chassis G05
  python3 scripts/fitment_rules.py --demo
"""
import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
MAKE = "BMW"
MAX_YEAR = 2027

NAMEPLATES = {"x1", "x2", "x3", "x4", "x5", "x6", "x7", "xm",
              "z3", "z4", "z8", "i3", "i4", "i5", "i7", "i8", "ix"}

# M-performance suffixes: eBay treats these as a Trim of the base M Model, not a
# distinct Model (e.g. "M2 CS" -> Model "M2"). The base Model already covers them,
# so we collapse them to avoid rows eBay rejects (confirmed live: warning 25023).
M_SUFFIXES = [" Competition xDrive", " Competition", " CSL", " CS", " GTS"]


def ebay_model(badge):
    """Map a reference trim badge to eBay's Model string (collapse M sub-trims)."""
    for suf in M_SUFFIXES:
        if badge.endswith(suf):
            return badge[: -len(suf)]
    return badge


def _load(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def load_all():
    ref = _load("bmw_chassis_reference.json")
    emap = _load("bmw_engine_map.json")
    try:
        ebay = set(m for m in _load("ebay_bmw_models.json")["models"])
    except (FileNotFoundError, KeyError):
        ebay = set()
    return ref, emap, ebay


def _norm(s):
    return " ".join(str(s).lower().split())


def _years(y):
    a, b = y.split("-")
    return int(a), min(int(b), MAX_YEAR)


def nameplate_of(row):
    """Return the eBay nameplate Model for a nameplate-style chassis, else None."""
    first = row.get("series", "").split()[0] if row.get("series") else ""
    return first if _norm(first) in NAMEPLATES else None


def find_chassis_candidates(model, year, reference, body_hint=None, chassis_hint=None):
    model_n = _norm(model)
    out = []
    for row in reference:
        if model_n not in {_norm(t) for t in row.get("trims", [])}:
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


def engine_index(emap):
    idx = {}
    for e in emap:
        idx.setdefault((_norm(e["chassis_code"]), _norm(e["trim"])), []).append(e)
    return idx


def engine_family(idx, chassis, trim, year=None):
    """Engine family/families for a (chassis, trim), optionally at a given year."""
    entries = idx.get((_norm(chassis), _norm(trim)), [])
    if year is not None:
        hit = [e for e in entries if _years(e["year_range"])[0] <= year <= _years(e["year_range"])[1]]
        if hit:
            return {e["engine_code"] for e in hit}
    return {e["engine_code"] for e in entries}


def expand(donor_model, year, rule, reference=None, emap=None, ebay=None,
           body_hint=None, chassis_hint=None):
    if reference is None:
        reference, emap, ebay = load_all()
    idx = engine_index(emap)

    cands = find_chassis_candidates(donor_model, year, reference, body_hint, chassis_hint)
    if not cands:
        return {"ok": False, "reason": f"No chassis found for {donor_model} ({year}).", "rows": []}
    if len(cands) > 1:
        return {"ok": False, "ambiguous": True,
                "reason": f"{donor_model} ({year}) matches {len(cands)} chassis (generation overlap).",
                "candidates": [{"chassis_code": c["chassis_code"], "series": c.get("series"),
                                "body_style": c.get("body_style"),
                                "year_range": [c["us_start_year"], c.get("us_end_year")]} for c in cands],
                "rows": []}
    row = cands[0]
    chassis = row["chassis_code"]
    start = row["us_start_year"]
    end = min(row.get("us_end_year") or MAX_YEAR, MAX_YEAR)
    years = list(range(start, end + 1))
    plate = nameplate_of(row)
    rule = rule.upper()

    # Which trims of this chassis do we emit?
    if rule == "A":
        trims = list(row["trims"])
        donor_engines = None
    elif rule == "B":
        donor_engines = engine_family(idx, chassis, donor_model, year)
        trims = [t for t in row["trims"]
                 if engine_family(idx, chassis, t) & donor_engines] or [donor_model]
    else:
        raise ValueError("rule must be A or B")

    # Build eBay-shaped rows.
    rows, seen = [], set()
    for t in trims:
        if plate:                       # nameplate vehicle: Model = nameplate
            model_out = plate
            trim_out = t if rule == "B" else None   # Rule A collapses to just the nameplate
        else:                           # sedan/coupe: Model = badge (M sub-trims collapsed)
            model_out = ebay_model(t)
            trim_out = None
        for y in years:
            key = (y, model_out, trim_out)
            if key in seen:
                continue
            seen.add(key)
            r = {"Year": y, "Make": MAKE, "Model": model_out}
            if trim_out:
                r["Trim"] = trim_out
            rows.append(r)

    models_used = sorted({r["Model"] for r in rows})
    unmatched = [m for m in models_used if ebay and m not in ebay]
    return {"ok": True, "chassis": chassis, "series": row.get("series"), "rule": rule,
            "nameplate": bool(plate), "year_range": [start, end],
            "donor_engines": sorted(donor_engines) if rule == "B" else None,
            "models": models_used, "unmatched_models": unmatched, "rows": rows}


def _demo():
    ref, emap, ebay = load_all()
    cases = [
        ("335i", 2014, "A", None), ("335i", 2014, "B", None),
        ("xDrive40i", 2021, "A", "G05"), ("xDrive40i", 2021, "B", "G05"),
        ("M3", 2016, "B", "F80 M3"), ("sDrive35i", 2010, "B", "E89"),
    ]
    for model, yr, rule, ch in cases:
        res = expand(model, yr, rule, ref, emap, ebay, chassis_hint=ch)
        head = f"{model} {yr} Rule {rule}" + (f" [{ch}]" if ch else "")
        if not res["ok"]:
            print(f"{head}: {'AMBIGUOUS' if res.get('ambiguous') else res['reason']}"); continue
        eng = f" engine={res['donor_engines']}" if res["donor_engines"] else ""
        print(f"{head}: {res['chassis']} ({'nameplate' if res['nameplate'] else 'badge'}){eng} "
              f"-> {len(res['rows'])} rows, models={res['models']}"
              + (f", TRIMS emitted" if any('Trim' in r for r in res['rows']) else ""))


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model"); ap.add_argument("--year", type=int)
    ap.add_argument("--rule", choices=["A", "B", "a", "b"])
    ap.add_argument("--body"); ap.add_argument("--chassis")
    ap.add_argument("--json", action="store_true"); ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        _demo(); return
    if not (args.model and args.year and args.rule):
        ap.error("provide --model, --year and --rule (or --demo)")

    res = expand(args.model, args.year, args.rule, body_hint=args.body, chassis_hint=args.chassis)
    if not res["ok"]:
        if res.get("ambiguous"):
            print(res["reason"]); print("Candidates:")
            for c in res["candidates"]:
                print(f"  {c['chassis_code']:12} {c['series']:24} {c['body_style']} ({c['year_range'][0]}-{c['year_range'][1]})")
            print("Re-run with --body '<style>' or --chassis '<code>'."); sys.exit(2)
        print(res["reason"], file=sys.stderr); sys.exit(1)

    kind = "nameplate (Model=nameplate)" if res["nameplate"] else "badge (Model=trim)"
    print(f"Donor: {args.model} {args.year} -> chassis {res['chassis']} ({res['series']}), Rule {res['rule']}, {kind}")
    if res["donor_engines"]:
        print(f"Donor engine family: {res['donor_engines']}")
    print(f"Year range: {res['year_range'][0]}-{res['year_range'][1]}   eBay rows: {len(res['rows'])}")
    print(f"Models: {res['models']}")
    if res["unmatched_models"]:
        print(f"WARNING: Model(s) not in eBay catalog list: {res['unmatched_models']}")
    if args.json:
        print(json.dumps(res["rows"], indent=2))


if __name__ == "__main__":
    main()
