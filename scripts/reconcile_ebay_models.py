#!/usr/bin/env python3
"""
Cross-check the chassis reference table's provisional trim names against eBay's
authoritative BMW Model list (data/ebay_bmw_models.json). Reports:
  - which of our trim strings exactly match an eBay Model (usable as-is)
  - which don't (need mapping)
  - which chassis rows are "nameplate" style, where eBay's Model is the nameplate
    (X5, i4, Z4, M3...) and our drivetrain badges are Trim-level, not Model-level.

Run: python3 scripts/reconcile_ebay_models.py
"""
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def norm(s):
    return " ".join(str(s).lower().split())


def main():
    with open(os.path.join(ROOT, "data", "bmw_chassis_reference.json"), encoding="utf-8") as f:
        ref = json.load(f)
    with open(os.path.join(ROOT, "data", "ebay_bmw_models.json"), encoding="utf-8") as f:
        ebay = json.load(f)

    ebay_models = ebay["models"]
    ebay_norm = {norm(m): m for m in ebay_models}

    # Nameplates eBay uses as a single Model (drivetrain lives in Trim, not Model).
    nameplates = ["X1", "X2", "X3", "X4", "X5", "X6", "X7", "XM", "Z3", "Z4", "Z8",
                  "i3", "i4", "i5", "i7", "i8", "iX"]
    nameplate_norm = {norm(n) for n in nameplates}

    total_trims = matched = 0
    unmatched = {}
    nameplate_rows = []
    clean_rows = []

    for row in ref:
        code = row["chassis_code"]
        series = row.get("series", "")
        row_matched, row_unmatched = [], []
        for t in row.get("trims", []):
            total_trims += 1
            if norm(t) in ebay_norm:
                matched += 1
                row_matched.append(t)
            else:
                row_unmatched.append(t)
                unmatched.setdefault(t, []).append(code)
        # Is this a nameplate-style vehicle (X/i/Z + standalone M like XM)?
        first = series.split()[0]
        is_nameplate = norm(first) in nameplate_norm or any(norm(first) == norm(n) for n in nameplates)
        if is_nameplate:
            nameplate_rows.append((code, series, first))
        elif not row_unmatched:
            clean_rows.append(code)

    print(f"Reference rows: {len(ref)}")
    print(f"Trim strings: {total_trims}  |  exact eBay-Model matches: {matched} "
          f"({100*matched//max(total_trims,1)}%)  |  unmatched: {total_trims - matched}")
    print(f"Rows with ALL trims matching an eBay Model: {len(clean_rows)}")
    print(f"Nameplate-style rows (eBay Model = nameplate, our badges are Trim-level): {len(nameplate_rows)}")

    print("\n--- Nameplate-style chassis (need Model=nameplate, engine via Trim/Engine) ---")
    for code, series, plate in nameplate_rows:
        in_ebay = "OK" if norm(plate) in {norm(m) for m in ebay_models} else "MISSING"
        print(f"  {code:14} {series:22} -> eBay Model '{plate}' [{in_ebay}]")

    print(f"\n--- Unmatched trim strings ({len(unmatched)} distinct) ---")
    for t in sorted(unmatched):
        print(f"  '{t}'  (rows: {', '.join(sorted(set(unmatched[t])))})")


if __name__ == "__main__":
    main()
