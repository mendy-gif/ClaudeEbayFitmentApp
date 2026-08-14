#!/usr/bin/env python3
"""Step 3 — reconcile models to eBay's catalog.

eBay only accepts fitment whose Make/Model exactly match its vehicle catalog.
This maps each raw model to the canonical eBay Model string from
data/ebay_bmw_models.json (fetched from eBay's Taxonomy API by the main project).

Mapping rules, in order: exact (case/space) -> "###xi" to "###i xDrive" ->
"M###xi" to "M###i xDrive" -> "X#M" to base "X#" (M is a Trim on eBay, dropped
here) -> a small manual table. Anything left is flagged UNMAPPED.

NOTE: this validates the Model name, not the year+model pair. eBay also checks
the model existed in that year; that check happens at push time (see push_to_ebay.py).

Usage:
  python3 reconcile_models.py --in ../data/listing_fitment_to_add.csv \
      --catalog ../../data/ebay_bmw_models.json --out ../data/ebay_ready_fitment.csv
"""
import argparse
import csv
import json
import os
import re
from collections import Counter

MANUAL = {
    "m550i": "M550i xDrive",
    "750xi": "750i xDrive",
    "535ix gt": "535i GT xDrive",
    "x5d": "X5",
}


def norm(s):
    return " ".join(str(s).lower().split())


def build_mapper(catalog_path):
    models = json.load(open(catalog_path))["models"]
    E = {norm(m): m for m in models}

    def mapper(raw):
        n = norm(raw)
        if n in E:
            return E[n], "exact"
        m = re.fullmatch(r"([a-z]?\d{3})xi", n)
        if m and f"{m.group(1)}i xdrive" in E:
            return E[f"{m.group(1)}i xdrive"], "xi->xDrive"
        m = re.fullmatch(r"m(\d{3})xi", n)
        if m and f"m{m.group(1)}i xdrive" in E:
            return E[f"m{m.group(1)}i xdrive"], "xi->xDrive"
        m = re.fullmatch(r"x(\d)m", n)
        if m and f"x{m.group(1)}" in E:
            return E[f"x{m.group(1)}"], "M-suv->base(trim=M dropped)"
        if n in MANUAL and norm(MANUAL[n]) in E:
            return MANUAL[n], "manual"
        return "", "UNMAPPED"

    return mapper


def reconcile(in_path, catalog_path, out_path):
    mapper = build_mapper(catalog_path)
    flags = Counter()
    rows = []
    with open(in_path, newline="") as f:
        for L in csv.DictReader(f):
            ebay_model, flag = mapper(L["model"])
            flags[flag] += 1
            rows.append([L["guid"], L["matched_part7"], L["year"], L["make"],
                         L["model"], ebay_model, flag, L.get("title", "")])

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["guid", "part7", "year", "make", "raw_model",
                    "ebay_model", "mapping_flag", "title"])
        w.writerows(rows)

    ok = len(rows) - flags["UNMAPPED"]
    print(f"rows: {len(rows)}   eBay-valid model: {ok}   unmapped: {flags['UNMAPPED']}")
    print("  flags:", dict(flags), "->", out_path)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="../data/listing_fitment_to_add.csv")
    ap.add_argument("--catalog", default="../../data/ebay_bmw_models.json")
    ap.add_argument("--out", default="../data/ebay_ready_fitment.csv")
    a = ap.parse_args()
    reconcile(a.inp, a.catalog, a.out)
