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

Auth: set env vars (or a .env-style file / shopify.env at the repo root):
  SHOPIFY_STORE=yourstore.myshopify.com
  either  SHOPIFY_TOKEN=shpat_...          a static Admin API token, or
  or      SHOPIFY_CLIENT_ID=... + SHOPIFY_CLIENT_SECRET=...

Shopify stopped allowing new legacy custom apps (the ones with permanent shpat_ tokens)
on 2026-01-01. For a server-to-server integration against your own store the supported
route is now the CLIENT CREDENTIALS grant: create an app in the Dev Dashboard with the
read_products scope, install it on the store, then hand this script the app's client id
and secret -- it exchanges them for a 24h token on each run, so nothing expires and
there is no token to paste. Same pattern as scripts/ebay_auth.py.

Usage:
  python3 scripts/shopify_donor.py --sku 1194 63142      # look up specific SKUs
  python3 scripts/shopify_donor.py --dump                # dump ALL BMW donors -> data/shopify_donors.json
  python3 scripts/shopify_donor.py --dump --force-shrink # allow a dump that halves the donor count
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
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


def mint_token(store, client_id, client_secret):
    """Exchange app client credentials for a 24h Admin API token.

    Shopify closed off creating legacy custom apps (with their permanent shpat_ tokens)
    on 2026-01-01. The supported replacement for a server-to-server integration against
    your OWN store is the client credentials grant: POST the app's client id/secret and
    get back a token, no OAuth redirect and nothing to paste by hand. Same shape as the
    eBay auth in scripts/ebay_auth.py.

    Requires the app to be installed on the store with the read_products scope.
    Returns None if unconfigured or the exchange fails.
    """
    if not (store and client_id and client_secret):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode()
    req = urllib.request.Request(
        f"https://{store}/admin/oauth/access_token", data=body, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8", "replace")).get("access_token")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:200]
        print(f"WARNING: Shopify token exchange failed HTTP {e.code}: {detail}", file=sys.stderr)
    except (urllib.error.URLError, ValueError, OSError) as e:
        print(f"WARNING: Shopify token exchange failed: {e}", file=sys.stderr)
    return None


def store_token():
    """(store, token). Prefers a static SHOPIFY_TOKEN; otherwise mints one from
    SHOPIFY_CLIENT_ID / SHOPIFY_CLIENT_SECRET via the client credentials grant."""
    store = os.environ.get("SHOPIFY_STORE")
    tok = os.environ.get("SHOPIFY_TOKEN")
    cid = os.environ.get("SHOPIFY_CLIENT_ID")
    secret = os.environ.get("SHOPIFY_CLIENT_SECRET")
    for name in ("shopify_token.txt", "shopify.env"):
        p = os.path.join(ROOT, name)
        if not os.path.exists(p):
            continue
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line.startswith("SHOPIFY_STORE="):
                store = store or line.split("=", 1)[1].strip()
            elif line.startswith("SHOPIFY_TOKEN="):
                tok = tok or line.split("=", 1)[1].strip()
            elif line.startswith("SHOPIFY_CLIENT_ID="):
                cid = cid or line.split("=", 1)[1].strip()
            elif line.startswith("SHOPIFY_CLIENT_SECRET="):
                secret = secret or line.split("=", 1)[1].strip()
            elif line and "=" not in line and not tok:
                tok = line  # a bare token in shopify_token.txt
    if not tok:
        tok = mint_token(store, cid, secret)
    return store, tok


class ShopifyError(RuntimeError):
    """A Shopify API failure that must stop the run rather than look like empty data."""


def gql(store, tok, query, variables):
    """GraphQL call that FAILS LOUDLY. Shopify answers a rejected query with HTTP 200 and
    an `errors` payload (bad token, missing read_products scope, throttling). Treating that
    as "no products" is how a refresh silently wipes a good donor dump -- so raise instead."""
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    data = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={
        "X-Shopify-Access-Token": tok, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            body = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise ShopifyError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
    except (urllib.error.URLError, ValueError, OSError) as e:
        raise ShopifyError(f"transport: {e}") from None
    if body.get("errors"):
        raise ShopifyError(f"GraphQL errors: {json.dumps(body['errors'])[:300]}")
    if body.get("data") is None:
        raise ShopifyError(f"no data in response: {json.dumps(body)[:300]}")
    return body


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


def parse_product(node, chassis_codes=frozenset()):
    tags = node.get("tags", [])
    sku = None
    for e in node.get("variants", {}).get("edges", []):
        sku = e["node"].get("sku") or sku
    make = _tag_value(tags, "donor_vehicle.veh_make_") or _tag_value(tags, "make_")
    series = _tag_value(tags, "donor_vehicle.veh_series_") or _tag_value(tags, "series_")

    # Older products carry no donor tags -- the chassis code is only in the vendor field
    # (vendor "F80", tags ["F80"], productType = the part number). A vendor matching a known
    # BMW chassis code is therefore both the make AND the series for those.
    vendor = (node.get("vendor") or "").strip()
    if vendor.upper() in chassis_codes:
        series = series or vendor.upper()
        make = make or "BMW"

    return {
        "sku": sku,
        "make": make,
        "model": _tag_value(tags, "donor_vehicle.veh_model_") or _tag_value(tags, "model_"),
        "series": series,
        "engine_code_raw": _tag_value(tags, "donor_vehicle.veh_engine_code_") or _tag_value(tags, "engine_code_"),
        "part_type": _tag_value(tags, "part_type_"),
        "universal": _tag_value(tags, "is_universal_fitment_"),
    }


PRODUCT_FIELDS = """
  edges { node {
    variants(first: 1) { edges { node { sku } } }
    tags
    vendor
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


# Which Shopify products count as BMW donors.
#
# NOT `vendor:BMW`. On this store the vendor field is frequently the CHASSIS CODE -- a 2015
# X5 door shell has vendor "F85", a 2020 X3 has "G01" -- so filtering on vendor silently
# excluded ~1,860 real BMW products, about 19% of the donor pool. The make lives in the tags
# (`make_BMW` / `donor_vehicle.veh_make_BMW`), which is what parse_product already reads.
# Union with vendor:BMW as well, since older products do use it: 9,718 vs 7,858 by vendor.
# Fetch ALL active products and decide what is BMW in Python. Filtering in the Shopify
# query cannot express this store's reality:
#   * `vendor:BMW` misses most of them -- the vendor is usually the CHASSIS CODE ("F80").
#   * `tag:make_BMW` misses a whole generation of products that carry NO donor tags at all;
#     SKU 17089 is "OEM BMW F80 F82 F87 M2 M3 M4 Steering Wheel" with tags exactly ["F80"].
#   * A vendor:<code> OR-list for all 94 chassis codes exceeds the API's 10,000 count cap,
#     so it cannot even be measured, let alone paginated reliably.
# Paging the whole active catalogue costs a few hundred extra Shopify calls against a
# 2,000,000/day allowance -- irrelevant -- and lets us apply the real test below.
BMW_QUERY = "status:active"


def _bmw_chassis_codes():
    """Base chassis codes from the committed reference, e.g. {'F80','G05',...}. Used to
    recognise a product whose ONLY BMW signal is a chassis code in the vendor field."""
    try:
        ref = json.load(open(os.path.join(ROOT, "data", "bmw_chassis_reference.json"),
                             encoding="utf-8"))
    except (ValueError, OSError):
        return set()
    rows = ref if isinstance(ref, list) else (ref.get("rows") or [])
    out = set()
    for r in rows:
        code = (r.get("chassis_code") or "").strip()
        if code:
            out.add(code.split()[0].split("/")[0].upper())
    return {c for c in out if c}


def dump_all(store, tok, vendor="BMW", force_shrink=False):
    out, cursor, page = {}, None, 0
    chassis_codes = _bmw_chassis_codes()
    non_bmw = 0
    while True:
        # sortKey:ID is load-bearing. Without an explicit stable sort, Shopify orders a
        # filtered product query by relevance, which can shift between pages -- so a long
        # pagination silently returns some products twice and misses others. That is why an
        # earlier dump captured 7,858 of 7,881 BMW products, losing real donors (SKUs 6407,
        # 60087) that the query definitely matched.
        q = ("query($q:String!,$after:String){ products(first:50, query:$q, after:$after, "
             "sortKey:ID) {" + PRODUCT_FIELDS + "} }")
        r = gql(store, tok, q, {"q": BMW_QUERY, "after": cursor})
        prods = r.get("data", {}).get("products", {})
        for e in prods.get("edges", []):
            d = parse_product(e["node"], chassis_codes)
            if not d["sku"]:
                continue
            # We now page the whole active catalogue, so drop other makes here. A product is
            # BMW if the tags say so, or if its vendor is a known BMW chassis code.
            if (d.get("make") or "").strip().upper() != "BMW":
                non_bmw += 1
                continue
            d["engine_family"] = engine_family(d["engine_code_raw"])
            out[d["sku"]] = d
        page += 1
        if page % 20 == 0:
            print(f"  page {page}: {len(out)} BMW donors ({non_bmw} other makes skipped)",
                  file=sys.stderr)
        if not prods.get("pageInfo", {}).get("hasNextPage"):
            break
        cursor = prods["pageInfo"]["endCursor"]
    path = os.path.join(ROOT, "data", "shopify_donors.json")
    previous = 0
    try:
        with open(path, encoding="utf-8") as f:
            previous = len(json.load(f))
    except (ValueError, OSError):
        pass

    # Sanity gate. The donor dump is the sweep's only source of donor vehicles; replacing a
    # healthy one with a truncated result (a mid-pagination failure, a scope that silently
    # returns nothing) would stall every future sweep without failing anything. A real
    # inventory never collapses by half overnight, so treat that as a broken read.
    if previous and len(out) < previous * 0.5 and not force_shrink:
        raise ShopifyError(
            f"refusing to overwrite {previous} donors with only {len(out)} -- this looks like "
            f"a failed read, not a real inventory change. The existing dump is untouched. "
            f"Re-run once the cause is understood, or pass --force-shrink if it is genuine.")

    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    os.replace(tmp, path)                      # atomic: never a half-written dump
    delta = f" ({len(out) - previous:+d} vs previous {previous})" if previous else ""
    print(f"Wrote {len(out)} donors{delta} -> {os.path.relpath(path, ROOT)}")
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
        sys.exit("ERROR: set SHOPIFY_STORE plus either SHOPIFY_TOKEN, or "
                 "SHOPIFY_CLIENT_ID + SHOPIFY_CLIENT_SECRET (env or shopify.env). "
                 "See this file's docstring.")
    if "--dump" in args:
        try:
            dump_all(store, tok, force_shrink="--force-shrink" in args)
        except ShopifyError as e:
            sys.exit(f"ERROR: Shopify refresh failed -- {e}\n"
                     "The existing donor dump was NOT modified. Common causes: the app is "
                     "missing the read_products scope, the released app version predates the "
                     "scope change, or the app and store are in different Shopify organizations "
                     "(the client credentials grant requires the same org).")
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
