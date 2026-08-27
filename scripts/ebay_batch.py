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
import gzip
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
import ebay_compat_catalog as CAT   # noqa: E402

BASE = "https://api.ebay.com"
LEDGER = os.path.join(ROOT, "data", "pushed_ledger.json")
PLAN = os.path.join(ROOT, "data", "batch_plan.csv")
ERRLOG = os.path.join(ROOT, "data", "sweep_errors.log")
SKIPCACHE = os.path.join(ROOT, "data", "skip_cache.json")
PLAN_COLS = ["sku", "listingId", "donor", "rule", "engine", "n_vehicles", "sources", "models", "action", "reason"]

# Bump when a change makes previously-pushed fitment wrong or invisible; ledger entries
# stamped with an older value are re-processed instead of skipped. "cat1" = the first
# release that validates trims against eBay's vehicle catalog (before it, pushes stored
# fine but displayed nothing).
CATALOG_ERA = "cat1"


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


# NOTE: there used to be a trading_write_compat() here that mirrored compatibility into the
# DISPLAY store via Trading ReviseFixedPriceItem. It is gone because it CANNOT work on these
# listings: they are Inventory-API-managed, and Trading answers every revise attempt with
#   [21919474] "Inventory-based listing management is not currently supported by this tool."
# regardless of how valid the rows are. The Inventory write displays on its own once the rows
# match eBay's vehicle catalog -- see the catalog repair step in process_sku().


def read_inventory_compat(sku, tok):
    """Read back the compatibility eBay actually KEPT in the Inventory store for a SKU,
    as rows [{Year:int, Make, Model[, Trim]}]. Returns (rows, err).

    This is a read-back confirmation, NOT a validator: the Inventory store keeps whatever
    you send it verbatim, including trims eBay's catalog does not recognise (which then
    display as nothing). Validation happens before the write, in ebay_compat_catalog.
    rows is None on a read failure so the caller never mistakes 'read broke' for 'empty'."""
    path = f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}/product_compatibility"
    s, p = api("GET", path, tok)
    if s in (401, 403):
        return None, f"auth HTTP {s}"
    if s == 404:
        return [], "no compatibility on record"      # truthful empty (SKU has none)
    if s != 200:
        return None, f"HTTP {s}"
    rows = []
    for cp in p.get("compatibleProducts", []):
        props = {pr.get("name", "").lower(): pr.get("value") for pr in cp.get("compatibilityProperties", [])}
        y = str(props.get("year", "")).strip()
        if y.isdigit() and props.get("make") and props.get("model"):
            r = {"Year": int(y), "Make": props["make"], "Model": props["model"]}
            if props.get("trim"):
                r["Trim"] = props["trim"]
            rows.append(r)
    return rows, None


def load_partnumber_fitment(path):
    """Approach-2 part-number history -> {sku: set((year:int, make, ebay_model, trim))}.
    Drops STOCK-prefixed guids (not live eBay SKUs) and UNMAPPED model rows.

    `trim` is "" for every part-number row and for most ETK rows -- eBay keeps the variant
    in the MODEL for sedans ("740i xDrive" is a model). It is only set for the X/Z models,
    where eBay's model is bare ("X5") and the variant belongs in the Trim field. It is ""
    rather than None so the tuples stay sortable."""
    out = {}
    opener = gzip.open if str(path).endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            guid = (r.get("guid") or "").strip()
            if not guid or guid.upper().startswith("STOCK"):
                continue
            if (r.get("mapping_flag") or "").upper() == "UNMAPPED":
                continue
            y, mk, md = (r.get("year") or "").strip(), (r.get("make") or "").strip(), (r.get("ebay_model") or "").strip()
            tr = (r.get("trim") or "").strip()          # absent in the part-number CSV
            if y.isdigit() and mk and md:
                out.setdefault(guid, set()).add((int(y), mk, md, tr))
    return out


# How long to trust a skip before re-checking it. The sweep sees 22k+ SKUs but can only
# afford ~700 Trading calls a night, and most skips are stable facts -- a SKU with no eBay
# listing today almost certainly has none tomorrow. Without this, every night re-spends its
# whole budget rediscovering the same dead ends and never reaches new inventory.
SKIP_TTL_DAYS = {
    "non-BMW": 90,                    # never becomes a BMW
    "offer read HTTP 404": 30,        # no listing for this SKU at all
    "already": 30,                    # curated by another system; drift watch covers ours
    "unresolved chassis": 30,         # a data gap, fixed by editing the reference not by retrying
    "no published offer": 7,          # ended/sold -- but could be relisted
    "no Shopify donor": 7,            # a donor tag could be added
}


def load_skip_cache():
    try:
        return json.load(open(SKIPCACHE, encoding="utf-8"))
    except (ValueError, OSError):
        return {}


def save_skip_cache(cache):
    try:
        tmp = SKIPCACHE + ".tmp"
        json.dump(cache, open(tmp, "w", encoding="utf-8"), indent=0, sort_keys=True)
        os.replace(tmp, SKIPCACHE)
    except OSError:
        pass


def skip_ttl(reason):
    """Days to remember this skip, or None to always re-check (transient failures)."""
    r = (reason or "").lower()
    if "guard read failed" in r or "rate" in r or "http 5" in r:
        return None                   # transient -- never cache a failure to LOOK
    for key, days in SKIP_TTL_DAYS.items():
        if key.lower() in r:
            return days
    return None


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


class RateLimited(RuntimeError):
    """eBay refused the call on quota (error 518). It is a DAILY allowance, so retrying
    within the run is pointless -- every remaining SKU will fail the same way."""


def trading_compat_retry(listing_id, tok, retries=3):
    """Trading 'already-expanded' read with retry. Returns (count, sample, err); count
    is None only if ALL attempts failed -> the caller MUST fail closed (skip the SKU).
    Raises RateLimited when eBay reports the daily call limit, so the sweep can stop."""
    n, sample, terr = None, [], "no listingId"
    for attempt in range(retries):
        n, sample, terr = trading_getitem_compat(listing_id, tok)
        if n is not None:
            return n, sample, terr
        if terr and "exceeded usage limit" in terr:
            raise RateLimited(terr)
        if attempt < retries - 1:
            time.sleep(2 ** attempt)
    return n, sample, terr


def enumerate_skus(tok, limit, in_stock_only=True):
    """List SKUs via getInventoryItems (paginated).

    Skips out-of-stock SKUs by default. These are one-of-a-kind salvage parts -- quantity 0
    means it sold, and fitment on a sold listing helps nobody. The quantity is already in the
    enumeration response, so filtering here costs NOTHING and saves a Trading call per SKU
    later, which is the actual scarce resource (5,000/day).
    """
    skus, offset, dropped = [], 0, 0
    while True:
        s, p = api("GET", f"/sell/inventory/v1/inventory_item?limit=100&offset={offset}", tok)
        if s != 200:
            print(f"  getInventoryItems HTTP {s}: {json.dumps(p)[:200]}", file=sys.stderr)
            break
        for it in p.get("inventoryItems", []):
            sku = it.get("sku")
            if not sku:
                continue
            if in_stock_only:
                qty = (it.get("availability", {})
                         .get("shipToLocationAvailability", {})
                         .get("quantity"))
                if qty is not None and qty < 1:
                    dropped += 1
                    continue
            skus.append(sku)
        if limit and len(skus) >= limit:
            skus = skus[:limit]
            break
        if not p.get("next"):
            break
        offset += 100
    if dropped:
        print(f"  skipped {dropped} out-of-stock SKU(s) (sold -- fitment would help nobody)")
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


def _chassis_expand(sku, shopify, sd, n_trad, sample, terr, rule, ref, emap, ebay, year_window=None):
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
                                         engine=sd.get("engine_family"), donor_model=model,
                                         year_window=year_window)
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
    for (y, mk, md, _tr) in vehicles:
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


def process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify=None,
                pnf=None, pn_cache=None, force=False, lci_inc=None, lci_exc=None, etk=None):
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

    # CHEAP CHECKS FIRST. The Trading guard read below is the scarce resource -- 5,000/day,
    # versus 2,000,000 for the Inventory calls above. eBay carries ~22k SKUs while Shopify has
    # ~7.8k BMW donors, so roughly 14k SKUs can never produce fitment. Spending the expensive
    # call on them before discovering that burned most of the daily budget on SKUs we were
    # always going to skip. In the Shopify path the guard read is NOT the donor source (see
    # _chassis_expand), so nothing is lost by deciding this first.
    # eBay will not render a fitment table in some categories at all (car audio, the
    # Performance "Other" catch-all). Decide that HERE, off the cheap Inventory read, so we
    # never spend a Trading guard call on a listing whose fitment nobody can ever see.
    if category and str(category) in load_nondisplay():
        return {"sku": sku, "listingId": listing_id, "action": "skip",
                "reason": f"category {category} never renders fitment on eBay"}

    if shopify is not None:
        sd_early = shopify.get(str(sku)) or shopify.get(sku)
        has_pn = bool(pnf) and sku in pnf
        if not sd_early and not has_pn:
            return {"sku": sku, "listingId": listing_id, "action": "skip",
                    "reason": "no Shopify donor for SKU"}
        if sd_early and not has_pn and FR._norm(sd_early.get("make") or "") != "bmw":
            return {"sku": sku, "listingId": listing_id, "action": "skip",
                    "reason": f"non-BMW ({sd_early.get('make')}) - out of scope (BMW-only reference)"}

    # Trading count is the "already expanded" guard in both paths (with retry). In the
    # Shopify path it is NOT the donor source, so n_trad==0 no longer means "skip".
    n_trad, sample, terr = trading_compat_retry(listing_id, tok) if listing_id else (None, [], "no listingId")
    # The guard exists to protect fitment SOMEONE ELSE curated -- never to protect our own.
    # If the SKU is in the ledger we put that fitment there, so we may replace it. Without
    # this exemption a listing we pushed WRONG stays wrong forever: the bad rows display as
    # >1 vehicle, the guard reads that as "curated", and every future sweep skips it. That
    # is exactly the state the wildcard leak leaves behind (SKU 8478: 6 pushed, 17 shown).
    ours = sku in led
    if n_trad is not None and n_trad > 1 and not force and not ours:
        return {"sku": sku, "listingId": listing_id, "action": "skip", "reason": f"already {n_trad} vehicles (multi-fit/expanded)"}
    # FAIL CLOSED: if the guard read failed (or there's no listingId), we cannot tell
    # whether this listing already has curated fitment -> skip rather than risk a PUT
    # that overwrites it. (A push only happens when we KNOW the listing has <=1 vehicle.)
    if n_trad is None and not force:
        return {"sku": sku, "listingId": listing_id, "action": "skip",
                "reason": f"guard read failed ({terr}) - skipped to protect existing fitment"}

    sd = (shopify or {}).get(str(sku)) or (shopify or {}).get(sku)
    rule, why = classify_rule(category, tree, inc, exc, default)
    # Part-number vehicles get the SAME chassis-family expansion as the donor (Rule A/B),
    # falling back to the literal vehicle when unresolvable. See expand_partnumber_rows.
    pn_rows = expand_partnumber_rows((pnf or {}).get(sku, ()), rule, ref, emap, ebay, pn_cache) if pnf else []

    # The ETK is BMW's own catalogue, so its rows are taken LITERALLY -- deliberately NOT
    # run through expand_partnumber_rows like the part-number history is. Part-number
    # history is a sample of cars a number has been seen on, so widening it to the chassis
    # family is a reasonable inference. The ETK already states the complete set, and the
    # models it leaves out are ones BMW says the part does NOT fit -- expanding would put
    # them back. For SKU 13611 the ETK gives 23 models where the donor's chassis reveals
    # only M3; that breadth is the source's value and it is already correct.
    etk_rows = [dict({"Year": y, "Make": mk, "Model": md}, **({"Trim": tr} if tr else {}))
                for (y, mk, md, tr) in sorted((etk or {}).get(sku, ()))]
    # An ETK trim is a PRECISE statement from BMW ("X5" + "xDrive50i"), so it must never be
    # widened to a trimless wildcard -- that would claim every X5 variant of that year,
    # which is broader than BMW said and exactly the trap in DESIGN.md 5.3. Validate the
    # ETK rows on their own with on_unmatched="drop" before they reach the shared pass.
    # This can only be equal or better than before the model/trim split: those rows had an
    # invalid MODEL and were dropped wholesale.
    if etk_rows:
        etk_rows, _etkrep = CAT.validate_rows(etk_rows, on_unmatched="drop")

    # Headlights and taillights change at a BMW facelift (LCI), so a pre-LCI light does not
    # fit a post-LCI car of the same chassis. Unlike a phantom TRIM -- which eBay silently
    # drops because it is not in its catalog -- a post-LCI year IS a real vehicle, so nothing
    # filters it out and the buyer sees a genuine-looking match. Narrow to the donor's side.
    year_window = None
    if sd and CP.lci_restricted(category, tree, lci_inc or set(), lci_exc or set()):
        row_lci, _n = FR.resolve_chassis(sd.get("series"), sd.get("model"), ref)
        if row_lci:
            year_window = FR.lci_window(row_lci["chassis_code"], sd.get("year"), ref)

    res, donor_str, fail = _chassis_expand(sku, shopify, sd, n_trad, sample, terr, rule, ref,
                                           emap, ebay, year_window=year_window)
    # A chassis basis is missing entirely. Return that verdict UNLESS part-number rows
    # exist for this SKU, in which case fall through and push those (the pn-only rescue).
    # Exception: a NON-BMW donor always skips (never push BMW part-number fitment onto a
    # non-BMW listing), even if a part-number row happens to collide on the SKU.
    if fail and ((not pn_rows and not etk_rows) or "non-BMW" in fail[1]):
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
    if not chassis_ok and not pn_rows and not etk_rows:   # chassis attempted but ambiguous/empty
        reason = "ambiguous donor" if res.get("ambiguous") else res.get("reason", "")
        return {"sku": sku, "listingId": listing_id, "donor": donor_str, "rule": rule, "action": "review", "reason": reason}

    # Preserve the listing's EXISTING donor vehicle (from the guard read) so a ReplaceAll
    # write never drops known-good fitment -- critical for the pn-only rescue, where the
    # part-number rows may not include the car the part actually came off. Kept at the
    # Model level (no trim) = a clean, valid superset row.
    #
    # BUT: a trimless row is a WILDCARD -- eBay expands it to every trim in the catalog for
    # that Year/Model. On a Rule B (engine-restricted) part that silently re-adds the very
    # engines the rule excluded: a trimless "2018 BMW X5" pulled in the xDrive35d diesel and
    # the X5 M. So we only keep a donor row for a Year/Model our own rows do not already
    # cover. Nothing is lost (a donor on some other year/model is still preserved) and the
    # engine restriction survives.
    covered = {(r["Year"], r["Make"], r["Model"]) for r in chassis_rows + pn_rows + etk_rows}
    donor_rows = []
    for nv in (sample or []):
        y = str(nv.get("Year", "")).strip()
        if y.isdigit() and nv.get("Make") and nv.get("Model"):
            if (int(y), nv["Make"], nv["Model"]) in covered:
                continue
            donor_rows.append({"Year": int(y), "Make": nv["Make"], "Model": nv["Model"]})

    combined, seen = [], set()                            # dedupe on the full tuple
    for r in chassis_rows + pn_rows + etk_rows + donor_rows:
        key = (r["Year"], r["Make"], r["Model"], r.get("Trim"))
        if key not in seen:
            seen.add(key)
            combined.append(r)

    sources = ((["chassis"] if chassis_rows else []) + (["pn"] if pn_rows else [])
               + (["etk"] if etk_rows else []))
    models_used = sorted({r["Model"] for r in combined})
    note = why if chassis_rows else ("part-number/ETK only" if (pn_rows or etk_rows) else why)
    if chassis_rows and pn_rows:
        note += f" (+{len(pn_rows)} part# rows)"
    if etk_rows:
        note += f" (+{len(etk_rows)} ETK rows)"
    row = {"sku": sku, "listingId": listing_id, "donor": donor_str,
           "rule": rule if chassis_rows else "-",
           "engine": ",".join(res.get("donor_engines") or []) if chassis_ok else "",
           "n_vehicles": len(combined), "sources": "+".join(sources),
           "models": ",".join(models_used), "action": "push", "reason": note}

    # ---- CATALOG REPAIR (the step that makes fitment actually display) --------------
    # Our rules emit BMW shorthand trims ("xDrive35i"); eBay's vehicle catalog spells them
    # with a body-style suffix ("xDrive35i Sport Utility 4-Door"). The Inventory API stores
    # an unrecognised trim happily (HTTP 200, reads back fine) but the listing DISPLAYS
    # nothing for it. So we repair every row against eBay's own catalog before pushing.
    # Rule B rows carry an engine restriction -- an unmatched trim is dropped rather than
    # widened to trimless, so an engine part never claims to fit every engine. Rule A rows
    # cover the whole chassis family anyway, so trimless is a fine fallback there.
    on_unmatched = "drop" if (rule == "B" and chassis_rows) else "trimless"
    combined, creport = CAT.validate_rows(combined, on_unmatched=on_unmatched)
    if creport["retrimmed"] or creport["trimless"] or creport["dropped_vehicle"] or creport["dropped_trim"]:
        bits = [f"{creport[k]} {k}" for k in ("retrimmed", "trimless", "dropped_vehicle", "dropped_trim")
                if creport[k]]
        note += f" [catalog: {', '.join(bits)}]"
    if creport["lookup_failed"]:
        note += f" [catalog lookup failed x{creport['lookup_failed']} -- rows passed through unverified]"
    row["n_vehicles"] = len(combined)
    row["models"] = ",".join(sorted({r["Model"] for r in combined}))
    row["reason"] = note
    if not combined:
        row["action"] = "skip"
        row["reason"] = "no rows survived eBay catalog validation: " + "; ".join(creport["notes"][:3])
        return row

    if live:
        # SINGLE WRITE to the Inventory store, by SKU.
        #
        # These listings are Inventory-API-managed, and the Trading API flatly REFUSES to
        # revise them: ReviseFixedPriceItem returns error 21919474 ("Inventory-based listing
        # management is not currently supported by this tool") no matter how valid the rows
        # are. The old dual-write could therefore never have displayed anything. The
        # Inventory write DOES display -- once the rows match eBay's catalog, which is what
        # the repair step above guarantees.
        #
        # We only reach here with <=1 existing vehicle (the guard) on a live PUBLISHED
        # offer -> touching compatibility can never revive an ended/sold item.
        inv_path = f"/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}/product_compatibility"
        st, resp = api("PUT", inv_path, tok, rows_to_payload(combined))
        if st in (401, 403):
            row["action"] = "auth_error"
            row["reason"] = f"inventory write auth HTTP {st}"
        elif st not in (200, 201, 204):
            row["action"] = "error"
            # Keep the eBay MESSAGE, not the first 160 characters of the envelope. The
            # useful part ("Input error. Seller Inventory Service can not ...") sits after
            # ~120 characters of errorId/domain/subdomain/category boilerplate, so the old
            # truncation reliably cut it off mid-sentence -- 14 write failures on
            # 2026-08-26 and the reason was unreadable in every one of them.
            row["reason"] = f"inventory write HTTP {st}: {_err_summary(resp)}"
        else:
            stored, verr = read_inventory_compat(sku, tok)
            if stored is None:
                row["action"] = "error"
                row["reason"] = f"read-back failed: {verr}"
            elif not stored:
                row["action"] = "error"
                row["reason"] = "eBay stored 0 rows"
            else:
                row["action"] = "pushed"
                row["n_vehicles"] = len(stored)
                led[sku] = {"listingId": listing_id, "rule": rule if chassis_rows else "-",
                            "n": len(stored), "models": sorted({r["Model"] for r in stored}),
                            "src": sources, "cv": CATALOG_ERA}
    return row



NONDISPLAY = os.path.join(ROOT, "data", "nondisplay_categories.json")
_nondisplay = None


def load_nondisplay():
    """Categories eBay stores fitment for but never RENDERS. Returns a set of ids.

    Pushing here is not harmful, just pointless: the rows go in, read back correctly, and
    no buyer ever sees them. Verified 2026-08-25 by pushing catalog-valid Rule A rows and
    re-reading hours later -- SKU 30825 stored 24 vehicles and displayed 0. Skipping them
    matters because the guard read costs one of 5,000 daily GetItem calls; 200 of the 717
    pushes on 2026-08-25 went to these categories.

    Only categories seen at least `skip_threshold` times with zero displays are skipped, so
    one unlucky listing cannot silently switch a whole category off.
    """
    global _nondisplay
    if _nondisplay is None:
        try:
            cfg = json.load(open(NONDISPLAY, encoding="utf-8"))
        except (OSError, ValueError):
            _nondisplay = set()
            return _nondisplay
        need = cfg.get("skip_threshold", 3)
        _nondisplay = {str(c) for c, v in (cfg.get("categories") or {}).items()
                       if (v or {}).get("displaying", 0) == 0
                       and (v or {}).get("skus_seen", 0) >= need}
    return _nondisplay


def _err_summary(resp, limit=240):
    """The human-readable part of an eBay error envelope.

    eBay returns {"errors":[{errorId, domain, subdomain, category, message, longMessage,
    parameters...}]}. The message is what tells you what to do; everything before it is
    fixed boilerplate. Falls back to the raw JSON when the shape is not what we expect,
    so an unrecognised error is never swallowed.
    """
    try:
        errs = resp.get("errors") or []
        parts = []
        for e in errs[:3]:
            msg = (e.get("longMessage") or e.get("message") or "").strip()
            eid = e.get("errorId")
            params = e.get("parameters") or []
            pv = " ".join(f"{p.get('name')}={p.get('value')}" for p in params
                          if isinstance(p, dict) and p.get("value"))
            parts.append(f"[{eid}] {msg}" + (f" ({pv})" if pv else ""))
        out = " | ".join(p for p in parts if p.strip(" []"))
        if out.strip():
            return out[:limit]
    except (AttributeError, TypeError):
        pass
    return json.dumps(resp)[:limit]

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
    ap.add_argument("--etk-fitment", metavar="CSV",
                    help="union in BMW's own ETK catalogue fitment (data/etk_fitment.csv, "
                         "built by scripts/etk_fitment.py). Taken literally, not expanded.")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--sleep", type=float, default=0.3, help="seconds between SKUs (rate-limit pacing)")
    ap.add_argument("--token")
    ap.add_argument("--force", action="store_true",
                    help="re-push even if the listing already shows >1 vehicle (bypasses the "
                         "'already expanded' guard). Use with an explicit --sku when correcting "
                         "fitment that was pushed with bad trims.")
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
    lci_inc, lci_exc = CP.load_lci_config()
    inc, exc, default = CP.load_config()
    led = load_ledger()
    live = args.live and args.mode in ("apply", "audit")

    shopify = None
    if args.from_shopify:
        sp = os.path.join(ROOT, "data", "shopify_donors.json")
        if not os.path.exists(sp):
            sys.exit("ERROR: --from-shopify needs data/shopify_donors.json (run: python3 scripts/shopify_donor.py --dump)")
        shopify = json.load(open(sp, encoding="utf-8"))

    etk = None
    if getattr(args, "etk_fitment", None):
        etk = load_partnumber_fitment(args.etk_fitment)   # same CSV shape
        print(f"  ETK fitment: {len(etk)} SKU(s), {sum(len(v) for v in etk.values())} rows loaded")
    pnf, pn_cache = None, {}
    if args.partnumber_fitment:
        pnf = load_partnumber_fitment(args.partnumber_fitment)
        print(f"  part-number fitment: {len(pnf)} SKU(s), {sum(len(v) for v in pnf.values())} rows loaded"
              f" (expanded to chassis families at push time)")

    if args.mode == "audit":
        return run_audit(tok, ref, emap, ebay, tree, inc, exc, default, led, args, live)

    # SKU source: explicit --sku, else eBay inventory, else (Shopify mode) the dump's keys.
    skus = args.sku or (enumerate_skus(tok, None) if args.from_inventory else [])
    if not skus and shopify is not None:
        skus = list(shopify.keys())
    if not skus:
        sys.exit("Provide --sku ..., --from-inventory, or --from-shopify (uses the dump's SKUs)")

    # Apply --limit to SKUs actually WORTH processing, not to the raw enumeration.
    # enumerate_skus returns eBay's list in a stable order, so slicing it directly meant the
    # sweep saw the same head of the list every night -- and as the ledger grew, more of that
    # head was already done. It would have converged to doing nothing while reporting success.
    skipped_now = 0
    if not args.sku:
        total = len(skus)
        cache = load_skip_cache()
        now = time.time()
        fresh = []
        for sku in skus:
            entry = led.get(sku)
            # A SKU already in the ledger is revisited when a source it has never been
            # pushed from now has rows for it -- otherwise adding the ETK would only ever
            # help listings we had not reached yet, and every SKU already done would keep
            # its narrower donor-only fitment forever.
            src = set((entry or {}).get("src", []))
            pn_pending = (bool(pnf) and sku in pnf and "pn" not in src) or \
                         (bool(etk) and sku in etk and "etk" not in src)
            if entry and entry.get("cv") == CATALOG_ERA and not pn_pending:
                continue                                   # done, and done correctly
            c = cache.get(sku)
            if c and c.get("until", 0) > now:
                skipped_now += 1
                continue                                   # known dead end, not due a re-check
            fresh.append(sku)
        print(f"  {total} SKU(s) on eBay  |  {len(led)} ledgered  |  {skipped_now} cached skips"
              f"  |  {len(fresh)} to consider")
        skus = fresh
    if args.limit:
        skus = skus[: args.limit]

    print(f"Mode: {args.mode}{' (LIVE)' if live else ' (dry-run)'}  |  donor={'Shopify' if shopify is not None else 'eBay'}"
          f"  |  {len(skus)} SKU(s)  |  ledger has {len(led)}")
    if live:
        log_error("-", "run-start", f"{args.mode} live, {len(skus)} SKU(s)")   # delineates runs in the log
    rows, counts, rate_limited = [], {}, False
    skip_cache_updates = {}
    for i, sku in enumerate(skus, 1):
        # Skip ledgered SKUs, UNLESS part-number rows exist that weren't applied yet
        # (so the combined run can add Approach-2 fitment to chassis-only pushes).
        entry = led.get(sku) if (args.mode == "apply" and not args.sku) else None  # explicit --sku always reprocesses
        # Entries written before the catalog-validation fix pushed trims eBay does not
        # recognise, so they display NOTHING. Re-process them automatically rather than
        # trusting a ledger row that records a push which never actually showed up.
        if entry and entry.get("cv") != CATALOG_ERA:
            entry = None
        src = set((entry or {}).get("src", []))
        pn_pending = (bool(pnf) and sku in pnf and "pn" not in src) or \
                     (bool(etk) and sku in etk and "etk" not in src)
        if entry and not pn_pending:
            rows.append({"sku": sku, "action": "skip", "reason": "already in ledger"})
        else:
            try:
                r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify, pnf, pn_cache, args.force, lci_inc, lci_exc, etk)
            except RateLimited as e:
                # The allowance is daily, so the remaining SKUs cannot succeed. Run #7 spent
                # 40 minutes and 630 SKUs discovering this one call at a time; stop instead,
                # and leave the rest for the next run (the ledger resumes exactly here).
                print(f"\n  STOPPED at {i}/{len(skus)}: eBay daily call limit reached.")
                print(f"  {len(skus) - i + 1} SKU(s) left for the next run. Detail: {str(e)[:120]}")
                log_error(sku, "rate-limited", str(e)[:200])
                rate_limited = True
                break
            if r["action"] == "auth_error":                  # try to self-heal (auto-refresh mode)
                nt = refresh_token()
                if nt and nt != tok:
                    tok = nt
                    print(f"  [{i}/{len(skus)}] {sku}: token auto-refreshed, retrying")
                    r = process_sku(sku, tok, ref, emap, ebay, tree, inc, exc, default, live, led, shopify, pnf, pn_cache, args.force, lci_inc, lci_exc, etk)
            # Remember stable skips so tomorrow's run does not re-check them (see SKIP_TTL_DAYS).
            ttl = skip_ttl(r.get("reason")) if r.get("action") in ("skip", "review") else None
            if ttl:
                skip_cache_updates[sku] = {"reason": (r.get("reason") or "")[:120],
                                           "until": time.time() + ttl * 86400}
            rows.append(r)
        counts[rows[-1]["action"]] = counts.get(rows[-1]["action"], 0) + 1
        _act, _why = rows[-1]["action"], rows[-1].get("reason", "")
        # Routine lines are clipped to keep a 2,000-SKU log readable, but an ERROR is never
        # clipped: for three nights the only record of a write failure was "[25023] Invalid
        # compatibility information. The item co" -- the half that says WHY was past the cut.
        print(f"  [{i}/{len(skus)}] {sku}: {_act} - "
              f"{_why if _act == 'error' else _why[:80]}")
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
    if live and skip_cache_updates:
        cache = load_skip_cache()
        cache.update(skip_cache_updates)
        # Drop entries whose TTL lapsed so the file cannot grow without bound.
        now = time.time()
        cache = {k: v for k, v in cache.items() if v.get("until", 0) > now}
        save_skip_cache(cache)
        print(f"Skip cache: +{len(skip_cache_updates)} recorded, {len(cache)} held "
              f"(these are not re-checked until their TTL lapses)")

    print(f"\nSummary: {counts}")
    if rate_limited:
        print("NOTE: stopped early on eBay's daily call limit. Everything pushed before that "
              "point IS recorded in the ledger; the rest resumes on the next run. If this "
              "keeps happening, lower the per-run limit.")
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
