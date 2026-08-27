#!/usr/bin/env python3
"""
Offline sanity check — no eBay, no Shopify, no network. Run after cloning to a new
machine (e.g. the Mac) to confirm the core logic and data files are intact:

  python3 scripts/selftest.py

Exercises the rule engine, the classifier logic, the reference + donor + part-number
data, and the chassis/part-number union+dedupe. Prints PASS/WARN/FAIL per check and
exits non-zero if any check FAILs. (A missing category tree is a WARN, not a FAIL —
classification still runs, but defaults to Rule B until the tree is present.)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

fails, warns = 0, 0


def ok(msg):
    print(f"  PASS  {msg}")


def warn(msg):
    global warns
    warns += 1
    print(f"  WARN  {msg}")


def bad(msg):
    global fails
    fails += 1
    print(f"  FAIL  {msg}")


def check(label, fn):
    print(label)
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        bad(f"unexpected error: {e!r}")


def c_reference():
    import fitment_rules as FR
    ref, emap, ebay = FR.load_all()
    if len(ref) > 50:
        ok(f"chassis reference loaded ({len(ref)} rows)")
    else:
        bad(f"chassis reference too small ({len(ref)})")
    if len(emap) > 100:
        ok(f"engine map loaded ({len(emap)} rows)")
    else:
        bad(f"engine map too small ({len(emap)})")
    if len(ebay) > 100:
        ok(f"eBay model vocabulary loaded ({len(ebay)} models)")
    else:
        bad(f"eBay model vocabulary too small ({len(ebay)})")


def c_rules():
    import fitment_rules as FR
    a = FR.expand("335i", 2014, "A")
    b = FR.expand("335i", 2014, "B")
    if a["ok"] and len(a["rows"]) > len(b["rows"]) > 0:
        ok(f"Rule A/B expand works (335i 2014: A={len(a['rows'])} rows > B={len(b['rows'])} rows)")
    else:
        bad(f"Rule A/B expand unexpected (A ok={a['ok']}, B ok={b['ok']})")
    # chassis-code path (Shopify donor path)
    ch = FR.expand_from_chassis("G87", "A", engine="S58", donor_model="M2")
    if ch["ok"] and ch["rows"]:
        ok(f"expand_from_chassis works (G87 -> {len(ch['rows'])} rows)")
    else:
        bad("expand_from_chassis failed for G87")
    # resolve a bare Shopify code that needs model disambiguation
    row, _ = FR.resolve_chassis("E90", "M3")
    if row and row["chassis_code"] == "E90 M3":
        ok("resolve_chassis disambiguates E90+M3 -> 'E90 M3'")
    else:
        bad(f"resolve_chassis E90+M3 gave {row and row.get('chassis_code')}")


def c_classify():
    import classify_part as CP
    include, exclude, default = CP.load_config()
    if include and default in ("A", "B"):
        ok(f"classifier config loaded ({len(include)} engine branches, default={default})")
    else:
        bad("classifier config missing include branches or default")
    # logic via a simulated tree (same as --self-test) — independent of the fetched file
    by_id = {"33615": {"categoryId": "33615", "ancestors": ["6000", "33612"], "path": "Engines"},
             "33707": {"categoryId": "33707", "ancestors": ["6000", "33642"], "path": "Mufflers"}}
    r_eng, _ = CP.classify("33615", by_id, include | {"33612"}, exclude, default)
    r_body, _ = CP.classify("33707", by_id, include | {"33612"}, exclude, default)
    if r_eng == "B" and r_body == "A":
        ok("classify logic: engine branch -> B, other -> A")
    else:
        bad(f"classify logic wrong (engine={r_eng}, body={r_body})")
    if CP.load_tree() is None:
        warn("category tree data/ebay_motors_categories.json NOT present -> live runs default to Rule B "
             "(commit it, or regenerate with scripts/ebay_fetch_categories.py)")
    else:
        ok("category tree present")


def c_donors():
    import json
    p = os.path.join(ROOT, "data", "shopify_donors.json")
    if not os.path.exists(p):
        warn("data/shopify_donors.json not present (run scripts/shopify_donor.py --dump)")
        return
    d = json.load(open(p, encoding="utf-8"))
    bmw = sum(1 for v in d.values() if (v.get("make") or "").lower() == "bmw")
    if bmw > 1000:
        ok(f"Shopify donor dump loaded ({len(d)} SKUs, {bmw} BMW)")
    else:
        warn(f"Shopify donor dump small ({len(d)} SKUs) — may need a refresh")


def c_partnumber_and_union():
    import ebay_batch as B
    import fitment_rules as FR
    p = os.path.join(ROOT, "spreadsheet-fitment", "data", "built", "ebay_ready_fitment.csv")
    if not os.path.exists(p):
        warn("part-number CSV not present (spreadsheet-fitment/data/built/ebay_ready_fitment.csv)")
        return
    pnf = B.load_partnumber_fitment(p)
    if len(pnf) > 100:
        ok(f"part-number fitment loaded ({len(pnf)} SKUs)")
    else:
        bad(f"part-number fitment too small ({len(pnf)})")
    # vocabulary consistency: every pn model must be in the eBay catalog
    ref, emap, vocab = FR.load_all()
    bad_models = {md for rows in pnf.values() for (_, _, md, _t) in rows if md not in vocab}
    if not bad_models:
        ok("all part-number models are in the eBay catalog vocabulary")
    else:
        bad(f"{len(bad_models)} part-number models NOT in catalog: {sorted(bad_models)[:6]}")
    # chassis-family expansion of part-number rows must be a superset of the literal rows
    cache, viol, sample = {}, 0, list(pnf.items())[:100]
    for _, veh in sample:
        lit = {(y, mk, md) for (y, mk, md, _tr) in veh}   # trim is the 4th element
        exp = {(r["Year"], r["Make"], r["Model"]) for r in B.expand_partnumber_rows(veh, "A", ref, emap, vocab, cache)}
        if any(v not in exp for v in lit):
            viol += 1
    if viol == 0:
        ok(f"part-number chassis expansion is a superset of literal (sampled {len(sample)} SKUs)")
    else:
        bad(f"part-number expansion dropped literal vehicles in {viol}/{len(sample)} SKUs")
    # union + dedupe on the full tuple (chassis rows + pn rows)
    chassis = [{"Year": 2014, "Make": "BMW", "Model": "328i"},
               {"Year": 2015, "Make": "BMW", "Model": "328i"}]
    pn = [{"Year": 2014, "Make": "BMW", "Model": "328i"},   # exact dup of a chassis row
          {"Year": 2013, "Make": "BMW", "Model": "335i"}]   # new
    comb, seen = [], set()
    for r in chassis + pn:
        k = (r["Year"], r["Make"], r["Model"], r.get("Trim"))
        if k not in seen:
            seen.add(k)
            comb.append(r)
    if len(comb) == 3:
        ok("union+dedupe correct (2 chassis + 2 pn, 1 overlap -> 3)")
    else:
        bad(f"union+dedupe wrong (expected 3, got {len(comb)})")


def c_display_path():
    """Run the display-path suite (scripts/test_display_fitment.py) as a subprocess so its
    stubbing of the catalog module cannot leak into this process."""
    import subprocess
    r = subprocess.run([sys.executable, os.path.join(ROOT, "scripts", "test_display_fitment.py")],
                       capture_output=True, text=True)
    tail = (r.stdout.strip().splitlines() or ["(no output)"])[-1]
    if r.returncode == 0:
        ok(f"display-path tests: {tail.replace('Result: PASS  ', '')}")
    else:
        for line in r.stdout.splitlines():
            if "FAIL" in line:
                bad(f"display-path: {line.strip()}")
        if r.returncode and "FAIL" not in r.stdout:
            bad(f"display-path tests crashed: {(r.stderr or r.stdout).strip()[:200]}")


def main():
    print("Offline self-test (no network)\n")
    check("Reference data:", c_reference)
    check("Rule engine:", c_rules)
    check("Classifier:", c_classify)
    check("Shopify donors:", c_donors)
    check("Part-number + union:", c_partnumber_and_union)
    check("Display path (trim/catalog validation):", c_display_path)
    print(f"\nResult: {'FAIL' if fails else 'PASS'}  ({fails} fail, {warns} warn)")
    sys.exit(1 if fails else 0)


if __name__ == "__main__":
    main()
