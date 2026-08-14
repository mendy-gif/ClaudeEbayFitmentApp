#!/usr/bin/env python3
"""
Shopify donor source — pull the donor vehicle (chassis code, engine, make/model,
part type) per SKU from the Shopify Admin API. This is the clean, complete,
automatable input for the fitment pipeline (docs/DESIGN.md sec 3.2):
Shopify tags carry, per part:
  donor_vehicle.veh_make_BMW, veh_model_M2, veh_series_G87 (=chassis code!),
  veh_engine_code_S58B30T0, part_type_Engine Motor Starter, is_universal_fitment_False

Because Shopify gives the CHASSIS CODE directly, no year->generation inference or
VIN decoding is needed. The variant SKU matches the eBay SKU.

Auth: set env vars (or a .env-style file the Codespace exports):
  SHOPIFY_STORE=yourstore.myshopify.com
  SHOPIFY_TOKEN=shpat_...            (custom app Admin API token, read_products)

Usage:
  python3 scripts/shopify_donor.py --sku 1194 63142      # look up specific SKUs
  python3 scripts/shopify_donor.py --dump                # dump ALL BMW donors -> data/shopify_donors.json
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
API_VERSION = "2024-10"

# part_type -> Rule B (engine) keywords, aligned with the owner's category decisions.
# (engine, forced induction, fuel/intake, ignition, belts, cooling incl. radiator,
# starters/alternators/ECUs). Ambiguous bare words like "motor"/"fan"/"pump" excluded.
B_KEYWORDS = [
    "engine", "starter", "alternator", "ecu", "ecm", "dme", "turbo", "supercharger",
    "injector", "injection", "fuel pump", "fuel rail", "throttle", "intake manifold",
    "ignition", "spark", "coil", "distributor", "belt", "pulley", "tensioner", "idler",
    "radiator", "water pump", "coolant", "thermostat", "cooling", "fan clutch",
    "cylinder head", "piston", "crankshaft", "camshaft", "timing", "oil pump", "oil pan",
    "valve cover", "vacuum pump", "harmonic balancer", "flywheel", "flex plate",
]
# Force Rule A regardless (override) — these contain a B keyword but aren't engine parts.
A_OVERRIDE = ["exhaust", "a/c", " ac ", "air condition", "heater", "climate", "blower", "cabin"]


def store_token():
    store = os.environ.get("SHOPIFY_STORE")
    tok = os.environ.get("SHOPIFY_TOKEN")
    for name in ("shopify_token.txt", "shopify.env"):
        p = os.path.join(ROOT, name)
        if (not store or not tok) and os.path.exists(p):
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line.startswith("SHOPIFY_STORE="):
                    store = store or line.split("=", 1)[1].strip()
                elif line.startswith("SHOPIFY_TOKEN="):
                    tok = tok or line.split("=", 1)[1].strip()
                elif line and "=" not in line and not tok:
                    tok = line  # a bare token in shopify_token.txt
    return store, tok


def gql(store, tok, query, variables):
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "X-Shopify-Access-Token": tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _tag_value(tags, prefix):
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def engine_family(code):
    if not code:
        return None
    m = re.match(r"^([A-Za-z]\d{2})", code)     # S58B30T0 -> S58
    return m.group(1).upper() if m else code


def classify_part_type(part_type):
    if not part_type:
        return "B", "no part_type -> default B"      # safe/narrow default
    pt = " " + part_type.lower() + " "
    if any(a in pt for a in A_OVERRIDE):
        return "A", f"A-override ({part_type})"
    if any(k in pt for k in B_KEYWORDS):
        return "B", f"engine part_type ({part_type})"
    return "A", f"non-engine part_type ({part_type})"


def parse_product(node):
    tags = node.get("tags", [])
    sku = None
    for e in node.get("variants", {}).get("edges", []):
        sku = e["node"].get("sku") or sku
    return {
        "sku": sku,
        "make": _tag_value(tags, "donor_vehicle.veh_make_") or _tag_value(tags, "make_"),
        "model": _tag_value(tags, "donor_vehicle.veh_model_") or _tag_value(tags, "model_"),
        "series": _tag_value(tags, "donor_vehicle.veh_series_") or _tag_value(tags, "series_"),
        "engine_code_raw": _tag_value(tags, "donor_vehicle.veh_engine_code_") or _tag_value(tags, "engine_code_"),
        "part_type": _tag_value(tags, "part_type_"),
        "universal": _tag_value(tags, "is_universal_fitment_"),
    }


PRODUCT_FIELDS = """
  edges { node {
    variants(first: 1) { edges { node { sku } } }
    tags
  } }
  pageInfo { hasNextPage endCursor }
"""


def fetch_by_skus(store, tok, skus):
    out = {}
    for sku in skus:
        q = f"query($q:String!){{ products(first:5, query:$q) {{ {PRODUCT_FIELDS} }} }}"
        r = gql(store, tok, q, {"q": f"sku:{sku}"})
        for e in r.get("data", {}).get("products", {}).get("edges", []):
            d = parse_product(e["node"])
            if d["sku"]:
                d["engine_family"] = engine_family(d["engine_code_raw"])
                out[d["sku"]] = d
    return out


def dump_all(store, tok, vendor="BMW"):
    out, cursor, page = {}, None, 0
    while True:
        q = ("query($q:String!,$after:String){ products(first:50, query:$q, after:$after) {"
             + PRODUCT_FIELDS + "} }")
        r = gql(store, tok, q, {"q": f"vendor:{vendor} status:active", "after": cursor})
        prods = r.get("data", {}).get("products", {})
        for e in prods.get("edges", []):
            d = parse_product(e["node"])
            if d["sku"]:
                d["engine_family"] = engine_family(d["engine_code_raw"])
                out[d["sku"]] = d
        page += 1
        print(f"  page {page}: {len(out)} donors so far", file=sys.stderr)
        if not prods.get("pageInfo", {}).get("hasNextPage"):
            break
        cursor = prods["pageInfo"]["endCursor"]
    path = os.path.join(ROOT, "data", "shopify_donors.json")
    json.dump(out, open(path, "w", encoding="utf-8"), indent=2)
    print(f"Wrote {len(out)} donors -> {os.path.relpath(path, ROOT)}")
    return out


def build_from_bulk(path):
    """Build the donor dump from a Shopify bulkOperationRunQuery JSONL export.
    Bulk output interleaves product lines (carry `tags`) and variant lines (carry
    `sku` + `__parentId`); we join them by parent product id. This is the scalable
    path for the full catalog (7.8k BMW products) and needs no token to re-parse."""
    prod_tags, sku_of = {}, {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        o = json.loads(line)
        if o.get("id") and "tags" in o:
            prod_tags[o["id"]] = o.get("tags", [])
        elif o.get("__parentId") and o.get("sku"):
            sku_of.setdefault(o["__parentId"], o["sku"])   # first variant's SKU
    out = {}
    for pid, tags in prod_tags.items():
        sku = sku_of.get(pid)
        if not sku:
            continue
        node = {"tags": tags, "variants": {"edges": [{"node": {"sku": sku}}]}}
        d = parse_product(node)
        d["engine_family"] = engine_family(d["engine_code_raw"])
        out[d["sku"]] = d
    outp = os.path.join(ROOT, "data", "shopify_donors.json")
    json.dump(out, open(outp, "w", encoding="utf-8"), indent=2)
    print(f"Wrote {len(out)} donors -> {os.path.relpath(outp, ROOT)}")
    return out


def main():
    args = sys.argv[1:]
    if "--from-bulk" in args:
        build_from_bulk(args[args.index("--from-bulk") + 1])
        return
    store, tok = store_token()
    if not store or not tok:
        sys.exit("ERROR: set SHOPIFY_STORE and SHOPIFY_TOKEN (env or shopify_token.txt/shopify.env)")
    if "--dump" in args:
        dump_all(store, tok)
        return
    if "--sku" in args:
        skus = args[args.index("--sku") + 1:]
        res = fetch_by_skus(store, tok, skus)
        for sku, d in res.items():
            rule, why = classify_part_type(d["part_type"])
            print(f"{sku}: {d['make']} {d['model']} series={d['series']} engine={d['engine_family']} "
                  f"part={d['part_type']!r} -> Rule {rule} ({why})")
        return
    print(__doc__)


if __name__ == "__main__":
    main()
