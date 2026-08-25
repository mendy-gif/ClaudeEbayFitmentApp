#!/usr/bin/env python3
"""
Turn the BMW ETK parts catalogue into fitment rows keyed by OUR SKUs.

The union has had two sources, both inferences: chassis rules (from the car the part came
off) and part-number history (cars a number has been seen on). The ETK is BMW's own
catalogue -- the first authoritative one, and the only source that can say a part fits a
DIFFERENT chassis than the donor. SKU 13611 is the standing example: the donor is an F80,
our chassis rule emits F80 alone, and the ETK says that airbag also fits F30, F36 and the M2.

Output is deliberately the SAME shape as spreadsheet-fitment's ebay_ready_fitment.csv, so
ebay_batch loads it with the existing --partnumber-fitment loader and it flows through the
existing union and the catalog gatekeeper. No new push path.

    python3 scripts/etk_fitment.py                 # all donors with a part number
    python3 scripts/etk_fitment.py --limit 200     # a quick sample
    python3 scripts/etk_fitment.py --sku 13611     # one SKU, printed

Reads the ETK read-only and NEVER writes to the BMW-ETK repo. Python stdlib only.
"""
import argparse
import csv
import gzip
import json
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONORS = os.path.join(ROOT, "data", "shopify_donors.json")
# Gzipped: 932k rows is 59 MB raw and 2.6 MB compressed, and this file is
# regenerated on every donor refresh. The 1 GB ETK database cannot live in this
# repo, so the derived CSV has to be committed for the nightly job to use it.
OUT = os.path.join(ROOT, "data", "etk_fitment.csv.gz")
# The ETK lives in a SEPARATE repo. Never modify anything inside it.
ETK = os.path.expanduser("~/Documents/GitHub/BMW-ETK/bmw-etk")
EMITTER = os.path.join(ETK, "scripts", "ebay_fitment.py")

# eBay names the xDrive variants in full; the ETK suffixes the model with X ("328iX").
# Everything else passes through -- eBay's BMW vocabulary is per-variant ("328i", "M3"),
# not per-series, which is why the ETK's `model` maps to eBay's MODEL and not to a trim.
_XDRIVE = re.compile(r"^(.*?)X$")


def to_ebay_model(etk_model):
    """ETK model designation -> eBay's model vocabulary, or None if unusable."""
    m = (etk_model or "").strip()
    if not m or m.lower() in ("?", "unknown"):
        return None
    hit = _XDRIVE.match(m)
    if hit and hit.group(1) and not hit.group(1).endswith(" "):
        # 328iX -> 328i xDrive. Guard against a bare "X" and against names that merely
        # end in X for other reasons (there is no BMW model that does, but be explicit).
        base = hit.group(1)
        if re.search(r"\d$|i$|d$", base):
            return f"{base} xDrive"
    return m


def load_donor_parts(limit=None, only=None):
    """{sku: part_number} for BMW donors that carry one."""
    sd = json.load(open(DONORS, encoding="utf-8"))
    out = {}
    for sku, v in sd.items():
        if only and sku not in only:
            continue
        if (v.get("make") or "").upper() != "BMW":
            continue
        pn = (v.get("part_number") or "").strip()
        if pn:
            out[sku] = pn
        if limit and len(out) >= limit:
            break
    return out


def run_emitter(part_numbers, confirmed_only=True):
    """Call the ETK's own emitter. We do NOT write fitment-condition SQL ourselves --
    there are three measured traps in that data (exclude-only conditions, diagram-level
    option gates, ambiguous mixed rows) and their script is the canonical reader.

    confirmed_only drops option-gated rows. We cannot tell here whether a SKU will be
    classified Rule A or Rule B, and a VERIFY row on a Rule B part would claim fitment
    that depends on an option we cannot check -- so take only the unconditional rows.
    """
    if not os.path.exists(EMITTER):
        sys.exit(f"ETK emitter not found at {EMITTER}. Is the BMW-ETK repo checked out?")
    fd, pn_file = tempfile.mkstemp(suffix=".txt"); os.close(fd)
    fd, csv_file = tempfile.mkstemp(suffix=".csv"); os.close(fd)
    try:
        with open(pn_file, "w", encoding="utf-8") as f:
            f.write("\n".join(sorted(set(part_numbers))))
        cmd = [sys.executable, EMITTER, "--file", pn_file, "-o", csv_file]
        if confirmed_only:
            cmd.append("--confirmed-only")
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"ETK emitter failed ({r.returncode}):\n{r.stderr[:800]}")
        with open(csv_file, encoding="utf-8") as f:
            return list(csv.DictReader(f))
    finally:
        for p in (pn_file, csv_file):
            try: os.unlink(p)
            except OSError: pass


def build(rows, sku_by_pn):
    """ETK rows -> the ebay_ready_fitment.csv shape, keyed by our SKU."""
    out, skipped = [], {"no_model": 0, "no_year": 0, "no_sku": 0}
    for r in rows:
        pn7 = (r.get("part_number") or "")[-7:].upper()
        skus = sku_by_pn.get(pn7)
        if not skus:
            skipped["no_sku"] += 1
            continue
        model = to_ebay_model(r.get("model"))
        if not model:
            skipped["no_model"] += 1
            continue
        fr, to = (r.get("year_from") or "").strip(), (r.get("year_to") or "").strip()
        if not fr.isdigit():
            skipped["no_year"] += 1
            continue
        # An open-ended range means "still in production at the catalogue cutoff" (early
        # 2020). Do NOT hard-cap it at 2019 -- that silently drops the newest cars, which
        # is the mistake the ETK side already made once and corrected.
        hi = int(to) if to.isdigit() else int(fr)
        for year in range(max(int(fr), 1990), hi + 1):
            for sku in skus:
                out.append({"guid": sku, "part7": pn7, "year": year, "make": "BMW",
                            "raw_model": r.get("model") or "", "ebay_model": model,
                            "mapping_flag": "etk", "title": r.get("part_name") or ""})
    return out, skipped


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, help="only the first N donors (for a quick run)")
    ap.add_argument("--sku", action="append", help="just these SKUs; prints instead of writing")
    ap.add_argument("--all-certainty", action="store_true",
                    help="include option-gated rows too (NOT safe for Rule B parts)")
    ap.add_argument("-o", "--out", default=OUT)
    args = ap.parse_args()

    parts = load_donor_parts(args.limit, set(args.sku) if args.sku else None)
    if not parts:
        sys.exit("No donors with a part number. Has the donor dump been refreshed?")
    print(f"{len(parts)} SKU(s) with a part number", file=sys.stderr)

    sku_by_pn = {}
    for sku, pn in parts.items():
        sku_by_pn.setdefault(pn[-7:].upper(), []).append(sku)

    rows = run_emitter(parts.values(), confirmed_only=not args.all_certainty)
    print(f"  ETK returned {len(rows)} fitment row(s)", file=sys.stderr)
    built, skipped = build(rows, sku_by_pn)
    covered = len({r["guid"] for r in built})
    print(f"  -> {len(built)} row(s) for {covered} SKU(s); skipped {skipped}", file=sys.stderr)

    if args.sku:
        for r in built[:60]:
            print(f"  {r['guid']}  {r['year']} {r['make']} {r['ebay_model']}   ({r['raw_model']})")
        return
    opener = gzip.open if args.out.endswith(".gz") else open
    with opener(args.out, "wt", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["guid", "part7", "year", "make", "raw_model",
                                          "ebay_model", "mapping_flag", "title"])
        w.writeheader(); w.writerows(built)
    print(f"Wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
