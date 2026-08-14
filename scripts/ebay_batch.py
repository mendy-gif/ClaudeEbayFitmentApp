#!/usr/bin/env python3
"""
Batch fitment runner — expand + push compatibility across many SKUs.

Given the confirmed operating mode (one-time push per SKU; see docs/DESIGN.md sec 5.2),
this: enumerates SKUs -> for each not already done: reads its donor vehicle + category
-> classifies Rule A/B -> expands via the chassis rules -> writes via the Inventory API.

MODES
  plan  (default): dry-run. Resolves everything and writes a CSV plan; NO writes to eBay.
  apply --live   : executes the writes, recording each in the ledger.
  audit          : Trading-reads a sample of already-pushed SKUs and reports any that lost
                   fitment (the persistence spot-check); add --live to re-push them.

SKU SOURCE
  --sku A B C        explicit SKUs, or
  --from-inventory   enumerate via getInventoryItems (paginated).
  --limit N          cap how many SKUs to process (use a small N for first runs).

State/output (in data/):
  batch_plan.csv        what it did / would do (SKU, listingId, donor, rule, engine, #vehicles, action, reason)
  pushed_ledger.json    {sku: {ts, listingId, rule, models, n}}  -> skipped on future runs

Read stores per the two-store finding: donor/state read from the TRADING item store (what
displays and survives relist); writes go to the Inventory API by SKU. Token: token.txt / --token.
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import fitment_rules as FR          # noqa: E402
import classify_part as CP          # noqa: E402
from ebay_writer import rows_to_payload  # noqa: E402
from ebay_inspect import trading_getitem_compat  # noqa: E402

BASE = "https://api.ebay.com"
LEDGER = os.path.join(ROOT, "data", "pushed_ledger.json")
PLAN = os.path.join(ROOT, "data", "batch_plan.csv")
PLAN_COLS = ["sku", "listingId", "donor", "rule", "engine", "n_vehicles", "models", "action", "reason"]


def token(args):
    if getattr(args, "token", None):
        return args.token.strip()
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    sys.exit("ERROR: no token (token.txt or --token)")


def api(method, path, tok, body=None):
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Language"] = "en-US"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except ValueError:
            return e.code, {"_raw": raw}
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def load_ledger():
    return json.load(open(LEDGER, encoding="utf-8")) if os.path.exists(LEDGER) else {}


def save_ledger(led):
    json.dump(led, open(LEDGER, "w", encoding="utf-8"), indent=2)


def enumerate_skus(tok, limit):
    """List SKUs via getInventoryItems (paginated)."""
    skus, offset = [], 0
    while True:
        s, p = api("GET", f"/sell/inventory/v1/inventory_item?limit=100&offset={offset}", tok)
        if s != 200:
            print(f"  getInventoryItems HTTP {s}: {json.dumps(p)[:200]}", file=sys.stderr)
            break
        items = p.get("inventoryItems", [])
        skus += [it.get("sku") for it in items if it.get("sku")]
        if limit and len(skus) >= limit:
            return skus[:limit]
        if not p.get("next"):
            break
        offset += 100
    return skus


def resolve_lookup(donor, reference):
    """Return (lookup_trim, chassis_hint) for expand(): badge donors use Model; nameplate
    donors (X/i/Z) extract the drivetrain badge from the eBay Trim string."""
    model = donor.get("Model", "")
    trim_str = donor.get("Trim", "") or ""
    year = int(donor["Year"])
    if FR._norm(model) in FR.NAMEPLATES:
        cands = [row for row in reference
                 if FR.nameplate_of(row) and FR._norm(FR.nameplate_of(row)) == FR._norm(model)
                 and row["us_start_year"] <= year <= (row.get("us_end_year") or FR.MAX_YEAR)]
        # Prefer the chassis+trim whose reference badge appears in the eBay Trim string.
        for row in cands:
            for t in sorted(row["trims"], key=len, reverse=True):   # longest match first (e.g. 'X5 M Competition')
                if FR._norm(t) in FR._norm(trim_str):
                    return t, row["chassis_code"]
        if len(cands) == 1:                       # unambiguous chassis, no trim match -> Rule A only
            return model, cands[0]["chassis_code"]
        return model, None                        # ambiguous -> expand() will flag it for review
    return model, None


def process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led):
    s, off = api("GET", f"/sell/inventory/v1/offer?{urllib.parse.urlencode({'sku': sku})}", tok)
    offers = off.get("offers", []) if s == 200 else []
    pub = [o for o in offers if o.get("status") == "PUBLISHED"]
    if not pub:
        return {"sku": sku, "action": "skip", "reason": "no published offer"}
    listing_id = pub[0].get("listing", {}).get("listingId")
    category = pub[0].get("categoryId")

    n_trad, sample, terr = trading_getitem_compat(listing_id, tok) if listing_id else (None, [], "no listingId")
    if n_trad is None:
        return {"sku": sku, "listingId": listing_id, "action": "skip", "reason": f"trading read failed: {terr}"}
    if n_trad == 0:
        return {"sku": sku, "listingId": listing_id, "action": "skip", "reason": "no donor vehicle on listing"}
    if n_trad > 1:
        return {"sku": sku, "listingId": listing_id, "action": "skip", "reason": f"already {n_trad} vehicles (multi-fit/expanded)"}

    donor = sample[0]
    donor_str = f"{donor.get('Year')} {donor.get('Make')} {donor.get('Model')} {donor.get('Trim','')}".strip()
    if FR._norm(donor.get("Make", "")) != "bmw":
        return {"sku": sku, "listingId": listing_id, "donor": donor_str, "action": "skip",
                "reason": f"non-BMW ({donor.get('Make')}) - out of scope (BMW-only reference)"}
    rule, why = classify_rule(category, tree, inc, exc, default)
    lookup, chassis_hint = resolve_lookup(donor, ref)
    try:
        res = FR.expand(lookup, int(donor["Year"]), rule, ref, emap, ebay, chassis_hint=chassis_hint)
    except Exception as e:  # noqa: BLE001
        return {"sku": sku, "listingId": listing_id, "donor": donor_str, "rule": rule, "action": "skip", "reason": f"expand error: {e}"}
    if not res["ok"]:
        reason = "ambiguous donor" if res.get("ambiguous") else res["reason"]
        return {"sku": sku, "listingId": listing_id, "donor": donor_str, "rule": rule, "action": "review", "reason": reason}

    row = {"sku": sku, "listingId": listing_id, "donor": donor_str, "rule": rule,
           "engine": ",".join(res.get("donor_engines") or []), "n_vehicles": len(res["rows"]),
           "models": ",".join(res["models"]), "action": "push", "reason": why}

    if live:
        payload = rows_to_payload(res["rows"])
        st, resp = api("PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}/product_compatibility", tok, payload)
        if st in (200, 201, 204):
            row["action"] = "pushed"
            led[sku] = {"listingId": listing_id, "rule": rule, "n": len(res["rows"]), "models": res["models"]}
        else:
            row["action"] = "error"
            row["reason"] = f"PUT HTTP {st}: {json.dumps(resp)[:160]}"
    return row


def classify_rule(category, tree, inc, exc, default):
    if not category or tree is None:
        return default, "no category/tree -> default"
    return CP.classify(category, tree, inc, exc, default)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", default="plan", choices=["plan", "apply", "audit"])
    ap.add_argument("--sku", nargs="*")
    ap.add_argument("--from-inventory", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between SKUs (rate-limit pacing)")
    ap.add_argument("--token")
    args = ap.parse_args()
    tok = token(args)

    ref, emap, ebay = FR.load_all()
    tree = CP.load_tree()
    inc, exc, default = CP.load_config()
    led = load_ledger()
    live = args.live and args.mode in ("apply", "audit")

    if args.mode == "audit":
        return run_audit(tok, ref, emap, ebay, tree, inc, exc, default, led, args, live)

    skus = args.sku or (enumerate_skus(tok, args.limit) if args.from_inventory else [])
    if not skus:
        sys.exit("Provide --sku ... or --from-inventory")
    if args.limit:
        skus = skus[: args.limit]

    print(f"Mode: {args.mode}{' (LIVE)' if live else ' (dry-run)'}  |  {len(skus)} SKU(s)  |  ledger has {len(led)}")
    rows, counts = [], {}
    for i, sku in enumerate(skus, 1):
        if sku in led and args.mode == "apply":
            rows.append({"sku": sku, "action": "skip", "reason": "already in ledger"})
        else:
            r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led)
            rows.append(r)
        counts[rows[-1]["action"]] = counts.get(rows[-1]["action"], 0) + 1
        print(f"  [{i}/{len(skus)}] {sku}: {rows[-1]['action']} - {rows[-1].get('reason','')[:80]}")
        if live:
            save_ledger(led)
        time.sleep(args.sleep)

    with open(PLAN, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PLAN_COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in PLAN_COLS})
    print(f"\nSummary: {counts}")
    print(f"Plan written -> data/batch_plan.csv" + ("  |  ledger updated" if live else "  (dry-run; re-run 'apply --live' to write)"))


def run_audit(tok, ref, emap, ebay, tree, inc, exc, default, led, args, live):
    skus = args.sku or list(led.keys())
    if args.limit:
        skus = skus[: args.limit]
    print(f"Audit: {len(skus)} pushed SKU(s){' (will re-push losses)' if live else ''}")
    lost = 0
    for i, sku in enumerate(skus, 1):
        s, off = api("GET", f"/sell/inventory/v1/offer?{urllib.parse.urlencode({'sku': sku})}", tok)
        pub = [o for o in off.get("offers", []) if o.get("status") == "PUBLISHED"] if s == 200 else []
        if not pub:
            print(f"  [{i}] {sku}: no published offer"); continue
        lid = pub[0].get("listing", {}).get("listingId")
        n, _, _ = trading_getitem_compat(lid, tok)
        expected = led.get(sku, {}).get("n")
        ok = n and n >= 2
        print(f"  [{i}] {sku}: {n} vehicles on listing (expected ~{expected}) {'OK' if ok else '<-- LOST'}")
        if not ok:
            lost += 1
            if live:
                r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, True, led)
                save_ledger(led)
                print(f"        re-push: {r['action']}")
        time.sleep(args.sleep)
    print(f"\nAudit done. {lost} SKU(s) had lost/'<2 vehicle' fitment.")


if __name__ == "__main__":
    main()
