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
import socket
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
ERRLOG = os.path.join(ROOT, "data", "sweep_errors.log")
PLAN_COLS = ["sku", "listingId", "donor", "rule", "engine", "n_vehicles", "sources", "models", "action", "reason"]


def log_error(sku, action, reason):
    """Append an error line to the log the instant it happens (flushed on close), so it
    survives even a hard kill -- unlike batch_plan.csv, which is only written at the end."""
    try:
        with open(ERRLOG, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{sku}\t{action}\t{reason}\n")
    except OSError:
        pass


def token(args):
    if getattr(args, "token", None):
        return args.token.strip()
    try:                                    # preferred: auto-refresh from a refresh token
        import ebay_auth
        t = ebay_auth.get_access_token()
        if t:
            return t
    except Exception:                       # noqa: BLE001 - fall back to manual token
        pass
    p = os.path.join(ROOT, "token.txt")
    if os.path.exists(p):
        return "".join(open(p, encoding="utf-8").read().split())
    sys.exit("ERROR: no token (configure ebay_auth.json, or provide token.txt / --token)")


def refresh_token():
    """Force-mint a new access token mid-run (auto-refresh mode). None if unconfigured."""
    try:
        import ebay_auth
        return ebay_auth.get_access_token(force=True)
    except Exception:                       # noqa: BLE001
        return None


def api(method, path, tok, body=None, retries=3):
    """eBay REST call with retry+backoff on 429/5xx and transport errors. The write
    (createOrReplaceProductCompatibility) is idempotent, so retrying is safe."""
    headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Language"] = "en-US"
        data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req) as r:
                raw = r.read().decode("utf-8", "replace")
                return r.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "replace")
            if (e.code == 429 or 500 <= e.code < 600) and attempt < retries - 1:
                time.sleep(2 ** attempt)          # 1s, 2s backoff
                continue
            try:
                return e.code, json.loads(raw)
            except ValueError:
                return e.code, {"_raw": raw}
        except urllib.error.URLError as e:         # timeout / transport blip
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            return None, {"_transport_error": str(e)}


def load_partnumber_fitment(path):
    """Approach-2 part-number history -> {sku: set((year:int, make, ebay_model))}.
    Drops STOCK-prefixed guids (not live eBay SKUs) and UNMAPPED model rows."""
    out = {}
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            guid = (r.get("guid") or "").strip()
            if not guid or guid.upper().startswith("STOCK"):
                continue
            if (r.get("mapping_flag") or "").upper() == "UNMAPPED":
                continue
            y, mk, md = (r.get("year") or "").strip(), (r.get("make") or "").strip(), (r.get("ebay_model") or "").strip()
            if y.isdigit() and mk and md:
                out.setdefault(guid, set()).add((int(y), mk, md))
    return out


def load_ledger():
    if not os.path.exists(LEDGER):
        return {}
    try:
        return json.load(open(LEDGER, encoding="utf-8"))
    except (ValueError, OSError) as e:            # corrupt/partial (e.g. interrupted write)
        print(f"WARNING: ledger unreadable ({e}); starting fresh. Old file -> {LEDGER}.bak", file=sys.stderr)
        try:
            os.replace(LEDGER, LEDGER + ".bak")
        except OSError:
            pass
        return {}


def save_ledger(led):
    """Atomic write: dump to a temp file then rename, so an interruption can never
    leave a truncated ledger that bricks the next run."""
    tmp = LEDGER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(led, f, indent=2)
    os.replace(tmp, LEDGER)


def trading_compat_retry(listing_id, tok, retries=3):
    """Trading 'already-expanded' read with retry. Returns (count, sample, err); count
    is None only if ALL attempts failed -> the caller MUST fail closed (skip the SKU)."""
    n, sample, terr = None, [], "no listingId"
    for attempt in range(retries):
        n, sample, terr = trading_getitem_compat(listing_id, tok)
        if n is not None:
            return n, sample, terr
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return n, sample, terr


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


def _chassis_expand(sku, shopify, sd, n_trad, sample, terr, rule, ref, emap, ebay):
    """Determine the chassis-rule expansion for a SKU. Returns (res, donor_str, fail):
    res is the expand() dict (may be not-ok), or None if chassis couldn't be attempted;
    fail is (action, reason) when there's no chassis basis at all, else None. The caller
    suppresses `fail` when part-number rows exist (union still has something to push)."""
    if shopify is not None:                              # ---- Shopify donor path ----
        if not sd:
            return None, "", ("skip", "no Shopify donor for SKU")
        make, model, series = sd.get("make"), sd.get("model"), sd.get("series")
        donor_str = f"{make} {model} [{series}]".strip()
        if FR._norm(make or "") != "bmw":
            return None, donor_str, ("skip", f"non-BMW ({make}) - out of scope (BMW-only reference)")
        row, note = FR.resolve_chassis(series, model, ref)
        if not row:
            return None, donor_str, ("review", f"unresolved chassis: {note}")
        try:
            res = FR.expand_from_chassis(row["chassis_code"], rule, ref, emap, ebay,
                                         engine=sd.get("engine_family"), donor_model=model)
        except Exception as e:  # noqa: BLE001
            return None, donor_str, ("skip", f"expand error: {e}")
        return res, donor_str, None
    # ---- eBay Trading donor path ----
    if n_trad is None:
        return None, "", ("skip", f"trading read failed: {terr}")
    if n_trad == 0:
        return None, "", ("skip", "no donor vehicle on listing")
    donor = sample[0]
    donor_str = f"{donor.get('Year')} {donor.get('Make')} {donor.get('Model')} {donor.get('Trim','')}".strip()
    if FR._norm(donor.get("Make", "")) != "bmw":
        return None, donor_str, ("skip", f"non-BMW ({donor.get('Make')}) - out of scope (BMW-only reference)")
    lookup, chassis_hint = resolve_lookup(donor, ref)
    try:
        res = FR.expand(lookup, int(donor["Year"]), rule, ref, emap, ebay, chassis_hint=chassis_hint)
    except Exception as e:  # noqa: BLE001
        return None, donor_str, ("skip", f"expand error: {e}")
    return res, donor_str, None


def expand_partnumber_rows(vehicles, rule, ref, emap, ebay, cache=None):
    """Expand each part-number historical vehicle (year, make, model) through the SAME
    chassis rules used on the donor, unioned + deduped. Falls back to the literal row
    when a vehicle can't be resolved (ambiguous generation / model not found) or is a
    Rule-B bare nameplate with no derivable engine. Result is a superset of the literal
    rows. Memoized by (model, year, rule) in `cache` so repeated vehicles compute once."""
    if cache is None:
        cache = {}
    out, seen = [], set()
    for (y, mk, md) in vehicles:
        ck = (md, y, rule)
        rows = cache.get(ck)
        if rows is None:
            try:
                res = FR.expand(md, y, rule, ref, emap, ebay)
                usable = res.get("ok") and not (rule.upper() == "B" and not res.get("donor_engines"))
                rows = res["rows"] if usable else [{"Year": y, "Make": mk, "Model": md}]
            except Exception:  # noqa: BLE001 - never let one bad row break the SKU
                rows = [{"Year": y, "Make": mk, "Model": md}]
            cache[ck] = rows
        for r in rows:
            key = (r["Year"], r["Make"], r["Model"], r.get("Trim"))
            if key not in seen:
                seen.add(key)
                out.append(r)
    return out


def process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify=None, pnf=None, pn_cache=None):
    s, off = api("GET", f"/sell/inventory/v1/offer?{urllib.parse.urlencode({'sku': sku})}", tok)
    offers = off.get("offers", []) if s == 200 else []
    if s in (401, 403):
        return {"sku": sku, "action": "auth_error", "reason": f"offer read HTTP {s} - token expired/invalid?"}
    if s != 200:
        return {"sku": sku, "action": "skip", "reason": f"offer read HTTP {s}"}
    pub = [o for o in offers if o.get("status") == "PUBLISHED"]
    if not pub:
        why = "offer exists but not PUBLISHED (ended/sold?)" if offers else "no offer for this SKU on eBay"
        return {"sku": sku, "action": "skip", "reason": f"no published offer ({why})"}
    listing_id = pub[0].get("listing", {}).get("listingId")
    category = pub[0].get("categoryId")

    # Trading count is the "already expanded" guard in both paths (with retry). In the
    # Shopify path it is NOT the donor source, so n_trad==0 no longer means "skip".
    n_trad, sample, terr = trading_compat_retry(listing_id, tok) if listing_id else (None, [], "no listingId")
    if n_trad is not None and n_trad > 1:
        return {"sku": sku, "listingId": listing_id, "action": "skip", "reason": f"already {n_trad} vehicles (multi-fit/expanded)"}
    # FAIL CLOSED: if the guard read failed (or there's no listingId), we cannot tell
    # whether this listing already has curated fitment -> skip rather than risk a PUT
    # that overwrites it. (A push only happens when we KNOW the listing has <=1 vehicle.)
    if n_trad is None:
        return {"sku": sku, "listingId": listing_id, "action": "skip",
                "reason": f"guard read failed ({terr}) - skipped to protect existing fitment"}

    sd = (shopify or {}).get(str(sku)) or (shopify or {}).get(sku)
    rule, why = classify_rule(category, tree, inc, exc, default)
    # Part-number vehicles get the SAME chassis-family expansion as the donor (Rule A/B),
    # falling back to the literal vehicle when unresolvable. See expand_partnumber_rows.
    pn_rows = expand_partnumber_rows((pnf or {}).get(sku, ()), rule, ref, emap, ebay, pn_cache) if pnf else []

    res, donor_str, fail = _chassis_expand(sku, shopify, sd, n_trad, sample, terr, rule, ref, emap, ebay)
    # A chassis basis is missing entirely. Return that verdict UNLESS part-number rows
    # exist for this SKU, in which case fall through and push those (the pn-only rescue).
    # Exception: a NON-BMW donor always skips (never push BMW part-number fitment onto a
    # non-BMW listing), even if a part-number row happens to collide on the SKU.
    if fail and (not pn_rows or "non-BMW" in fail[1]):
        action, reason = fail
        d = {"sku": sku, "listingId": listing_id, "action": action, "reason": reason}
        if donor_str:
            d["donor"] = donor_str
        if action == "review":
            d["rule"] = rule
        return d
    if res is None:
        res = {"ok": False, "reason": fail[1] if fail else "no chassis", "rows": []}
    chassis_ok = res["ok"]
    chassis_rows = res["rows"] if chassis_ok else []
    if not chassis_ok and not pn_rows:                    # chassis attempted but ambiguous/empty
        reason = "ambiguous donor" if res.get("ambiguous") else res.get("reason", "")
        return {"sku": sku, "listingId": listing_id, "donor": donor_str, "rule": rule, "action": "review", "reason": reason}

    combined, seen = [], set()                            # dedupe on the full tuple
    for r in chassis_rows + pn_rows:
        key = (r["Year"], r["Make"], r["Model"], r.get("Trim"))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    sources = (["chassis"] if chassis_rows else []) + (["pn"] if pn_rows else [])
    models_used = sorted({r["Model"] for r in combined})
    note = why if chassis_rows else "part-number only"
    if chassis_rows and pn_rows:
        note += f" (+{len(pn_rows)} part# rows)"
    row = {"sku": sku, "listingId": listing_id, "donor": donor_str,
           "rule": rule if chassis_rows else "-",
           "engine": ",".join(res.get("donor_engines") or []) if chassis_ok else "",
           "n_vehicles": len(combined), "sources": "+".join(sources),
           "models": ",".join(models_used), "action": "push", "reason": note}

    if live:
        payload = rows_to_payload(combined)
        st, resp = api("PUT", f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}/product_compatibility", tok, payload)
        if st in (200, 201, 204):
            row["action"] = "pushed"
            led[sku] = {"listingId": listing_id, "rule": rule if chassis_rows else "-",
                        "n": len(combined), "models": models_used, "src": sources}
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
    ap.add_argument("--from-shopify", action="store_true",
                    help="donor = Shopify dump (data/shopify_donors.json); classification still by eBay category")
    ap.add_argument("--partnumber-fitment", metavar="CSV",
                    help="union in Approach-2 part-number history (e.g. spreadsheet-fitment/data/built/ebay_ready_fitment.csv)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between SKUs (rate-limit pacing)")
    ap.add_argument("--token")
    args = ap.parse_args()
    tok = token(args)

    # Every HTTP call gets a hard timeout so a single stalled socket can't freeze an
    # unattended overnight run forever (it surfaces as a per-SKU error and the run continues).
    socket.setdefaulttimeout(30)

    # Preflight: a cheap authenticated call. eBay user tokens expire in ~2h, and an
    # expired token otherwise shows up as "no published offer" on every SKU (a 401 that
    # the offer read swallows). Fail loud and early instead.
    st, _ = api("GET", "/sell/inventory/v1/inventory_item?limit=1", tok)
    if st in (401, 403):
        sys.exit(f"ERROR: eBay token rejected (HTTP {st}). It has likely EXPIRED "
                 f"(user tokens last ~2 hours). Paste a fresh token into token.txt and re-run.")
    if st is None:
        sys.exit("ERROR: could not reach eBay (network/proxy). Check connection and retry.")

    ref, emap, ebay = FR.load_all()
    tree = CP.load_tree()
    inc, exc, default = CP.load_config()
    led = load_ledger()
    live = args.live and args.mode in ("apply", "audit")

    shopify = None
    if args.from_shopify:
        sp = os.path.join(ROOT, "data", "shopify_donors.json")
        if not os.path.exists(sp):
            sys.exit("ERROR: --from-shopify needs data/shopify_donors.json (run: python3 scripts/shopify_donor.py --dump)")
        shopify = json.load(open(sp, encoding="utf-8"))

    pnf, pn_cache = None, {}
    if args.partnumber_fitment:
        pnf = load_partnumber_fitment(args.partnumber_fitment)
        print(f"  part-number fitment: {len(pnf)} SKU(s), {sum(len(v) for v in pnf.values())} rows loaded"
              f" (expanded to chassis families at push time)")

    if args.mode == "audit":
        return run_audit(tok, ref, emap, ebay, tree, inc, exc, default, led, args, live)

    # SKU source: explicit --sku, else eBay inventory, else (Shopify mode) the dump's keys.
    skus = args.sku or (enumerate_skus(tok, args.limit) if args.from_inventory else [])
    if not skus and shopify is not None:
        skus = list(shopify.keys())
    if not skus:
        sys.exit("Provide --sku ..., --from-inventory, or --from-shopify (uses the dump's SKUs)")
    if args.limit:
        skus = skus[: args.limit]

    print(f"Mode: {args.mode}{' (LIVE)' if live else ' (dry-run)'}  |  donor={'Shopify' if shopify is not None else 'eBay'}"
          f"  |  {len(skus)} SKU(s)  |  ledger has {len(led)}")
    if live:
        log_error("-", "run-start", f"{args.mode} live, {len(skus)} SKU(s)")   # delineates runs in the log
    rows, counts = [], {}
    for i, sku in enumerate(skus, 1):
        # Skip ledgered SKUs, UNLESS part-number rows exist that weren't applied yet
        # (so the combined run can add Approach-2 fitment to chassis-only pushes).
        entry = led.get(sku) if args.mode == "apply" else None
        pn_pending = bool(pnf) and sku in pnf and "pn" not in set((entry or {}).get("src", []))
        if entry and not pn_pending:
            rows.append({"sku": sku, "action": "skip", "reason": "already in ledger"})
        else:
            r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify, pnf, pn_cache)
            if r["action"] == "auth_error":                  # try to self-heal (auto-refresh mode)
                nt = refresh_token()
                if nt and nt != tok:
                    tok = nt
                    print(f"  [{i}/{len(skus)}] {sku}: token auto-refreshed, retrying")
                    r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify, pnf, pn_cache)
            rows.append(r)
        counts[rows[-1]["action"]] = counts.get(rows[-1]["action"], 0) + 1
        print(f"  [{i}/{len(skus)}] {sku}: {rows[-1]['action']} - {rows[-1].get('reason','')[:80]}")
        if rows[-1]["action"] in ("error", "auth_error"):     # persist immediately (crash-safe)
            log_error(sku, rows[-1]["action"], rows[-1].get("reason", ""))
        if rows[-1]["action"] == "auth_error":
            print("\n  STOPPING: eBay token rejected and no refresh credentials configured.")
            print("  Temp mode: refresh token.txt and re-run (ledger resumes). Or set up ebay_auth.json (docs sec 7).")
            break
        if live and rows[-1]["action"] == "pushed":     # only write when the ledger changed
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
