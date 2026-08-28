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
import time
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
            # Skip blanks and comments. Without this the bare-token branch below swallows
            # any comment line -- it has no "=" in it -- and silently uses "# Shopify
            # credentials for..." as the access token. The symptom is a clean-looking
            # "token minted OK" followed by HTTP 401, which sends you hunting for a
            # credentials problem that does not exist.
            if not line or line.startswith("#"):
                continue
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


def _is_throttled(errors):
    """True when Shopify's `errors` payload is purely a rate-limit rejection."""
    if not errors:
        return False
    for e in errors:
        code = ((e.get("extensions") or {}).get("code") or "").upper()
        if code != "THROTTLED" and "throttl" not in (e.get("message") or "").lower():
            return False            # a REAL error is mixed in -- do not retry it away
    return True


def _pace(body):
    """Sleep just enough to keep the next query inside Shopify's leaky bucket.

    Retrying AFTER a throttle works but is slow and noisy: the full catalogue is ~880
    queries, and erroring on most of them turns a refresh into 40 minutes of backoff.
    Shopify returns the bucket state on every SUCCESSFUL call, so pace off that and mostly
    never get throttled at all. The retry in gql() stays as the safety net.
    """
    cost = (body.get("extensions") or {}).get("cost") or {}
    ts = cost.get("throttleStatus") or {}
    avail, rate = ts.get("currentlyAvailable"), ts.get("restoreRate")
    need = cost.get("actualQueryCost") or cost.get("requestedQueryCost")
    if not (avail is not None and rate and need):
        return
    # Keep one query's worth of headroom in the bucket.
    if avail < need * 2:
        wait = min((need * 2 - avail) / rate, 10.0)
        if wait > 0:
            time.sleep(wait)


def gql(store, tok, query, variables, _tries=6):
    """GraphQL call that FAILS LOUDLY, but waits out rate limits first.

    Shopify answers a rejected query with HTTP 200 and an `errors` payload (bad token,
    missing read_products scope, throttling). Treating that as "no products" is how a
    refresh silently wipes a good donor dump -- so raise instead.

    THROTTLED is the exception: it is not a failure, it is "ask again shortly". There was
    no retry here, so the FIRST throttle killed the whole dump. That is exactly what
    happened on 2026-08-21: adding 8 metafield lookups to a 50-product page raised the
    query cost ~3.6x, Shopify started throttling, and the nightly donor refresh silently
    stopped updating for three nights while the sweep carried on against a stale dump.
    Back off using Shopify's own restoreRate when it tells us, and retry.
    """
    url = f"https://{store}/admin/api/{API_VERSION}/graphql.json"
    data = json.dumps({"query": query, "variables": variables}).encode()
    delay = 2.0
    for attempt in range(1, _tries + 1):
        req = urllib.request.Request(url, data=data, method="POST", headers={
            "X-Shopify-Access-Token": tok, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                body = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < _tries:      # rate limit at the HTTP layer
                time.sleep(delay); delay = min(delay * 2, 60); continue
            raise ShopifyError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from None
        except (urllib.error.URLError, ValueError, OSError) as e:
            if attempt < _tries:                         # transient network blip
                time.sleep(delay); delay = min(delay * 2, 60); continue
            raise ShopifyError(f"transport: {e}") from None

        errors = body.get("errors")
        if errors and _is_throttled(errors) and attempt < _tries:
            # Prefer Shopify's own numbers: how far below the requested cost we are, and
            # how fast the bucket refills. Falls back to exponential backoff.
            wait = delay
            cost = (body.get("extensions") or {}).get("cost") or {}
            ts = cost.get("throttleStatus") or {}
            need, avail = cost.get("requestedQueryCost"), ts.get("currentlyAvailable")
            rate = ts.get("restoreRate")
            if need is not None and avail is not None and rate:
                wait = max(1.0, (need - avail) / rate + 0.5)
            print(f"  throttled by Shopify, waiting {wait:.0f}s (attempt {attempt}/{_tries})",
                  file=sys.stderr)
            time.sleep(min(wait, 60)); delay = min(delay * 2, 60)
            continue
        if errors:
            raise ShopifyError(f"GraphQL errors: {json.dumps(errors)[:300]}")
        if body.get("data") is None:
            raise ShopifyError(f"no data in response: {json.dumps(body)[:300]}")
        _pace(body)
        return body
    raise ShopifyError(f"still throttled after {_tries} attempts")


def _tag_value(tags, prefix):
    for t in tags:
        if t.startswith(prefix):
            return t[len(prefix):]
    return None


def engine_family(code):
    """Normalise an engine code to its FAMILY (S58B30T0 -> S58).

    Dismantly writes the code under two different tag spellings --
    `donor_vehicle.veh_engine_code_S58B30T0` and
    `donor_vehicle.veh_engine_code_raw_S58B30T0`. `_tag_value` matches on the shorter
    prefix, so the second spelling arrives here as "raw_S58B30T0". The family regex is
    anchored at the start, so it did not match, and the `else code` fallback returned
    "raw_S58B30T0" verbatim as the family. That is not a family: it matches nothing in
    bmw_engine_map.json, so Rule B silently expanded against a phantom engine (F30 with
    "raw_N20B" emitted 7 rows where "N20" emits 14). It affected 2,071 donors.
    """
    if not code:
        return None
    code = code.strip()
    if code.lower().startswith("raw_"):          # veh_engine_code_raw_S58... -> S58...
        code = code[4:]
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


def donor_year(tags):
    """The donor car's model year, or None.

    ONLY reads `donor_vehicle.veh_production_year_<YYYY>`, which is singular and describes
    the actual car the part came off. There is a bare `year_<YYYY>` tag too, but on some
    products that is a multi-year LISTING RANGE (SKU 15110 carries year_2014 through
    year_2018), so using it as a fallback would silently invent a donor year. A wrong year
    is worse than no year here: it decides which side of an LCI split a headlight lands on.
    """
    # Scan EVERY matching tag, not just the first. The same prefix also produces
    # `..._production_year_from_2017` and `..._to_2023` (the listing's fitment span, not the
    # donor), and _tag_value returns only the first hit -- so a product carrying from/to
    # BEFORE the real year would lose it. Take the first value that is purely a year.
    pre = "donor_vehicle.veh_production_year_"
    for t in tags:
        if not t.startswith(pre):
            continue
        v = t[len(pre):].strip()
        if v.isdigit() and 1980 <= int(v) <= 2030:   # sanity bound; from_/to_ fail isdigit
            return int(v)
    return None


def _mf_year(node):
    """Donor year from `custom.donor_vehicle_veh_production_year`, validated like the tag."""
    v = _mf(node, "mf_year")
    if v and v.isdigit() and 1980 <= int(v) <= 2030:
        return int(v)
    return None


def parse_product(node, chassis_codes=frozenset()):
    tags = node.get("tags", [])
    sku = None
    for e in node.get("variants", {}).get("edges", []):
        sku = e["node"].get("sku") or sku
    make = _tag_value(tags, "donor_vehicle.veh_make_") or _tag_value(tags, "make_") or _mf(node, "mf_make")
    series = _series_tag(tags) or _mf(node, "mf_series")

    # The vendor field is used inconsistently on this store, so accept every shape it takes.
    # Older products carry no donor tags at all -- the chassis code is only in the vendor
    # (vendor "F80", tags ["F80"], productType = the part number), which makes the vendor both
    # the make AND the series for those. Newer ones set vendor "BMW". Either must work, and
    # so must a future cleanup that switches products from one to the other: mendy is
    # reviewing the store separately, and a donor silently disappearing because its vendor was
    # tidied up is exactly the kind of quiet loss this project keeps having to fix.
    vendor = (node.get("vendor") or "").strip()
    if vendor.upper() in chassis_codes:
        series = series or vendor.upper()
        make = make or "BMW"
    elif vendor.upper() == "BMW":
        make = make or "BMW"
    # Last resort for the chassis: a bare chassis code among the tags (["F80"]), which is
    # where it also lives on those older products.
    if not series:
        for t in tags:
            if t.strip().upper() in chassis_codes:
                series = t.strip().upper()
                break

    return {
        "sku": sku,
        "make": make,
        "model": (_tag_value(tags, "donor_vehicle.veh_model_") or _tag_value(tags, "model_")
                  or _mf(node, "mf_model")),
        "series": series,
        "year": donor_year(tags) or _mf_year(node),
        "engine_code_raw": (_tag_value(tags, "donor_vehicle.veh_engine_code_")
                            or _tag_value(tags, "engine_code_") or _mf(node, "mf_engine")),
        "part_type": _tag_value(tags, "part_type_") or _mf(node, "mf_part_type"),
        "universal": _tag_value(tags, "is_universal_fitment_"),
        "vin": _donor_vin(tags, node),
        "part_number": _part_number(tags, node),
    }


# The `custom.*` metafields carry the same donor facts as the tags, but they are populated
# on products whose tags are missing or spelled differently -- SKU 13611 has NO readable
# series tag (`donor_vehicle.raw_veh_series_F80`, see _series_tag) yet `custom.series` is
# "F80". Tags alone lost the chassis on those products, which is the single field every
# rule is built on. Fetch both and let parse_product prefer whichever is present.
PRODUCT_FIELDS = """
  edges { node {
    variants(first: 1) { edges { node { sku } } }
    tags
    vendor
    mf_series: metafield(namespace: "custom", key: "series") { value }
    mf_model: metafield(namespace: "custom", key: "model") { value }
    mf_engine: metafield(namespace: "custom", key: "donor_vehicle_veh_engine_code_raw") { value }
    mf_year: metafield(namespace: "custom", key: "donor_vehicle_veh_production_year") { value }
    mf_vin: metafield(namespace: "custom", key: "donor_vehicle_vin") { value }
    mf_make: metafield(namespace: "custom", key: "donor_vehicle_veh_make") { value }
    mf_part_type: metafield(namespace: "custom", key: "part_type") { value }
    mf_part_number: metafield(namespace: "custom", key: "part_number") { value }
    productType
  } }
  pageInfo { hasNextPage endCursor }
"""


def _mf(node, alias):
    """Value of an aliased metafield, or None when absent/blank."""
    m = node.get(alias)
    if not isinstance(m, dict):
        return None
    v = (m.get("value") or "").strip()
    return v or None


def _series_tag(tags):
    """The donor chassis from the tags, across BOTH spellings Dismantly emits.

    `donor_vehicle.veh_series_F80` and `donor_vehicle.raw_veh_series_F80` both occur. Only
    the first was matched, and `_tag_value` uses startswith, so `raw_veh_series_` never
    fired -- the chassis was silently dropped on every product using it.
    """
    for pre in ("donor_vehicle.veh_series_", "donor_vehicle.raw_veh_series_", "series_"):
        v = _tag_value(tags, pre)
        if v:
            return v
    return None


def _part_number(tags, node):
    """The BMW part number for this listing, or None.

    Needed to look a listing up in the ETK catalogue (docs/DESIGN.md 9), which is keyed on
    the LAST 7 characters of the number, uppercase. Four sources, in descending
    trustworthiness -- `part_number_clean_` is Dismantly's normalised form, productType is
    last because on this store it is *usually* the part number but not always.
    """
    v = (_tag_value(tags, "part_number_clean_") or _tag_value(tags, "part_number_")
         or _mf(node, "mf_part_number"))
    if not v:
        pt = (node.get("productType") or "").strip()
        v = pt if re.fullmatch(r"\d{7,11}", pt) else None
    if not v:
        return None
    v = re.sub(r"[^0-9A-Za-z]", "", v).upper()
    # ETK keys on the last 7; keep the full number here and let the consumer trim, but
    # reject anything that cannot be a part number rather than passing junk downstream.
    return v if re.fullmatch(r"[0-9A-Z]{7,11}", v) else None


def _donor_vin(tags, node):
    """The donor VIN -- tag `donor_vehicle.vin_<VIN>` or the `custom.donor_vehicle_vin`
    metafield. Recorded but not yet decoded; a BMW VIN also encodes year and engine."""
    v = _tag_value(tags, "donor_vehicle.vin_") or _mf(node, "mf_vin")
    v = (v or "").strip().upper()
    return v if len(v) == 17 else None


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
        # 25, not 50: each node now costs ~11 points (8 metafield lookups), so a 50-product
        # page runs ~550 and exhausts the bucket almost immediately. Halving the page
        # halves the per-query cost; the retry above covers what is left.
        q = ("query($q:String!,$after:String){ products(first:25, query:$q, after:$after, "
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
    # Report the fields that downstream rules depend on, so a silently-missing one shows up
    # in the run log instead of being discovered days later in the committed file. The donor
    # year drives the LCI year-window for headlights/taillights; a dump written without it
    # leaves that feature inert while everything still looks green.
    have_year = sum(1 for v in out.values() if v.get("year"))
    have_series = sum(1 for v in out.values() if v.get("series"))
    have_engine = sum(1 for v in out.values() if v.get("engine_family"))
    print(f"  fields: year {have_year}/{len(out)}  |  chassis {have_series}/{len(out)}"
          f"  |  engine {have_engine}/{len(out)}")
    if out and not have_year:
        print("  WARNING: not one donor has a year -- the LCI window for lights cannot "
              "apply, and lights will keep using the full chassis range.", file=sys.stderr)
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
