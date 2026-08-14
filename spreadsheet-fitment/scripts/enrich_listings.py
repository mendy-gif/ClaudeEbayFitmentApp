#!/usr/bin/env python3
"""Step 2 — match listings to the fitment table.

For each listing whose part number (columns oeoempartnumber / manufacturerpartnumber,
i.e. eBay's 7-digit + 11-digit) matches a part in the fitment table, emit one row
per vehicle to add.

Input listings CSV must have columns: guid, oeoempartnumber, manufacturerpartnumber,
title (extra columns are ignored). Reads the table produced by build_fitment_table.py.

Usage:
  python3 enrich_listings.py --listings /path/to/listings.csv \
      --table ../data/fitment_by_partnumber.csv --out ../data/listing_fitment_to_add.csv
"""
import argparse
import csv
import os
from collections import defaultdict

from fitment_common import part_keys


def load_table(table_path):
    part = defaultdict(set)
    with open(table_path, newline="") as f:
        for row in csv.DictReader(f):
            k = row["part_number_7"]
            for v in row["vehicles"].split(";"):
                v = v.strip()
                if not v:
                    continue
                toks = v.split()
                if len(toks) >= 3:
                    part[k].add((toks[0], toks[1], " ".join(toks[2:])))
    return part


def dedup(vehset):
    seen = {}
    for (y, mk, md) in sorted(vehset):
        seen.setdefault((y, mk.casefold(), md.casefold()), (y, mk, md))
    return sorted(seen.values())


def enrich(listings_path, table_path, out_path, pn_cols, skip_prefixes):
    part = load_table(table_path)
    rows_out = []
    matched = 0
    skipped = 0
    with open(listings_path, newline="", encoding="utf-8", errors="replace") as f:
        for L in csv.DictReader(f):
            guid = L.get("guid", "")
            if any(guid.upper().startswith(p.upper()) for p in skip_prefixes if p):
                skipped += 1
                continue
            cand = set()
            for c in pn_cols:
                for k7, _ in part_keys(L.get(c, "")):
                    cand.add(k7)
            hits = [k for k in cand if k in part]
            if not hits:
                continue
            matched += 1
            veh = set()
            for k in hits:
                veh |= part[k]
            for (y, mk, md) in dedup(veh):
                rows_out.append([L.get("guid", ""), ";".join(sorted(set(hits))),
                                 y, mk, md, (L.get("title", "") or "")[:80]])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["guid", "matched_part7", "year", "make", "model", "title"])
        w.writerows(rows_out)
    print(f"listings enriched: {matched}   fitment rows: {len(rows_out)}   "
          f"skipped(prefix): {skipped}   -> {out_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--listings", required=True)
    ap.add_argument("--table", default="../data/fitment_by_partnumber.csv")
    ap.add_argument("--out", default="../data/listing_fitment_to_add.csv")
    ap.add_argument("--pn-cols", nargs="+",
                    default=["oeoempartnumber", "manufacturerpartnumber"])
    ap.add_argument("--skip-guid-prefix", nargs="*", default=["STOCK"],
                    help="drop listings whose guid starts with any of these (default: STOCK)")
    a = ap.parse_args()
    enrich(a.listings, a.table, a.out, a.pn_cols, a.skip_guid_prefix)
