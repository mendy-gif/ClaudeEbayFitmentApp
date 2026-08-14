#!/usr/bin/env python3
"""
eBay compatibility writer — turns generated fitment rows into a
createOrReplaceProductCompatibility payload and (optionally) pushes it.

DRY-RUN BY DEFAULT: prints the exact PUT URL + JSON body so you can eyeball it.
Nothing is sent unless you pass --live (which needs a token with the
sell.inventory WRITE scope; the read-only token used for tests is not enough).

Pipeline: donor vehicle + rule -> scripts/fitment_rules.py expands to eBay rows
-> wrapped as compatibleProducts -> PUT /sell/inventory/v1/inventory_item/{sku}/product_compatibility

Ways to supply the donor + rule:
  1. Manually:
       python3 scripts/ebay_writer.py --sku 5978 --model 335i --year 2014 --rule B
  2. Classify from a category id (Rule A/B via classify_part.py):
       python3 scripts/ebay_writer.py --sku 5978 --model 335i --year 2014 --category 33615
  3. Auto-detect donor + category from the live listing (best-effort; needs the
     listing to already carry its single donor vehicle + an offer):
       python3 scripts/ebay_writer.py --sku 5978 --detect
  Add --live to actually write. Add --chassis/--body to disambiguate a donor.

Read-only calls (getProductCompatibility, getOffers) and the final PUT all use the
token (token.txt or --token).
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import fitment_rules as FR            # noqa: E402
import classify_part as CP            # noqa: E402

API = {"prod": "https://api.ebay.com", "sandbox": "https://api.sandbox.ebay.com"}
NAMEPLATES = FR.NAMEPLATES


def load_token(args):
    if args.token:
        return args.token.strip()
    env = os.environ.get("EBAY_ACCESS_TOKEN")
    if env:
        return env.strip()
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    return None


def _req(method, url, token, body=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Language"] = "en-US"
        data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw}
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def rows_to_payload(rows):
    cps = []
    for r in rows:
        props = [{"name": "Year", "value": str(r["Year"])},
                 {"name": "Make", "value": r["Make"]},
                 {"name": "Model", "value": r["Model"]}]
        if r.get("Trim"):
            props.append({"name": "Trim", "value": r["Trim"]})
        cps.append({"compatibilityProperties": props})
    return {"compatibleProducts": cps}


def detect_donor_and_category(sku, token, base):
    """Best-effort: read the listing's existing donor vehicle + offer category."""
    donor, category = None, None
    s, payload = _req("GET", f"{base}/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}/product_compatibility", token)
    if s == 200:
        for cp in payload.get("compatibleProducts", []):
            props = {p["name"].lower(): p["value"] for p in cp.get("compatibilityProperties", [])}
            if "model" in props and "year" in props:
                donor = props
                break
    s, payload = _req("GET", f"{base}/sell/inventory/v1/offer?" + urllib.parse.urlencode({"sku": sku}), token)
    if s == 200 and payload.get("offers"):
        category = payload["offers"][0].get("categoryId")
    return donor, category


def lookup_key(model, trim):
    """Reference/engine lookup key: drivetrain badge (Trim) for nameplate Models, else the Model badge."""
    if FR._norm(model) in NAMEPLATES and trim:
        return trim
    return model


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sku", required=True)
    ap.add_argument("--model"); ap.add_argument("--year", type=int)
    ap.add_argument("--rule", choices=["A", "B", "a", "b"])
    ap.add_argument("--category", help="eBay categoryId to classify Rule A/B via classify_part")
    ap.add_argument("--chassis"); ap.add_argument("--body")
    ap.add_argument("--detect", action="store_true", help="Read donor + category from the live listing")
    ap.add_argument("--live", action="store_true", help="Actually PUT the compatibility (needs sell.inventory write scope)")
    ap.add_argument("--sandbox", action="store_true"); ap.add_argument("--token")
    args = ap.parse_args()

    base = API["sandbox" if args.sandbox else "prod"]
    token = load_token(args)
    model, year, rule, category = args.model, args.year, args.rule, args.category

    if args.detect:
        if not token:
            print("ERROR: --detect needs a token (token.txt or --token).", file=sys.stderr); sys.exit(2)
        donor, cat = detect_donor_and_category(args.sku, token, base)
        if donor:
            model = model or lookup_key(donor.get("model"), donor.get("trim"))
            year = year or int(donor["year"])
            print(f"Detected donor: {donor}  -> lookup '{model}' ({year})")
        else:
            print("Could not detect a donor vehicle from the listing; supply --model/--year.", file=sys.stderr)
        category = category or cat
        if cat:
            print(f"Detected category: {cat}")

    if not (model and year):
        ap.error("need a donor: --model and --year (or --detect that finds one)")

    # Decide the rule.
    if not rule:
        if not category:
            ap.error("need --rule, or --category (to classify), or --detect")
        by_id = CP.load_tree()
        if by_id is None:
            ap.error("classify needs data/ebay_motors_categories.json (run ebay_fetch_categories.py)")
        inc, exc, default = CP.load_config()
        rule, why = CP.classify(category, by_id, inc, exc, default)
        print(f"Category {category} -> Rule {rule}  ({why})")

    res = FR.expand(model, year, rule, body_hint=args.body, chassis_hint=args.chassis)
    if not res["ok"]:
        if res.get("ambiguous"):
            print(res["reason"]); print("Candidates: " + ", ".join(f"{c['chassis_code']} ({c['body_style']})" for c in res["candidates"]))
            print("Re-run with --chassis or --body."); sys.exit(2)
        print(res["reason"], file=sys.stderr); sys.exit(1)

    payload = rows_to_payload(res["rows"])
    url = f"{base}/sell/inventory/v1/inventory_item/{urllib.parse.quote(args.sku, safe='')}/product_compatibility"

    print(f"\nSKU {args.sku}  |  donor {model} {year}  |  chassis {res['chassis']} ({res['series']})  |  Rule {res['rule']}")
    if res.get("donor_engines"):
        print(f"Engine family: {res['donor_engines']}")
    print(f"Compatible vehicles: {len(payload['compatibleProducts'])}  |  Models: {res['models']}")
    if res["unmatched_models"]:
        print(f"WARNING: Model(s) not in eBay catalog list (may be dropped by eBay): {res['unmatched_models']}")

    if not args.live:
        print("\n--- DRY RUN (nothing sent). This is the request that WOULD be made: ---")
        print(f"PUT {url}")
        print("Headers: Authorization: Bearer <token>, Content-Type: application/json, Content-Language: en-US")
        print("Body (first 6 of {} compatibleProducts shown):".format(len(payload["compatibleProducts"])))
        preview = {"compatibleProducts": payload["compatibleProducts"][:6]}
        print(json.dumps(preview, indent=2))
        print("\nRe-run with --live to actually write (requires sell.inventory WRITE scope).")
        return

    if not token:
        print("ERROR: --live needs a token.", file=sys.stderr); sys.exit(2)
    status, resp = _req("PUT", url, token, payload)
    print(f"\nLIVE PUT -> HTTP {status}")
    if status == 200 or status == 204:
        print("OK - compatibility set. (Warnings, if any, below.)")
    print(json.dumps(resp, indent=2) if resp else "(empty response = success)")
    if status in (401, 403):
        print("Auth error - the token likely lacks the sell.inventory WRITE scope. Re-mint with that scope.", file=sys.stderr)


if __name__ == "__main__":
    main()
