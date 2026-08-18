#!/usr/bin/env python3
"""
eBay vehicle-catalog validator — the missing piece that makes fitment actually DISPLAY.

WHY THIS EXISTS
---------------
Our rules generate trims in BMW's shorthand ("xDrive35i"). eBay's vehicle catalog spells
the same trim with a body-style suffix ("xDrive35i Sport Utility 4-Door"). A compatibility
row whose Trim is not verbatim in eBay's catalog is accepted by the Inventory API (HTTP
200, stored, readable back) but is SILENTLY DROPPED from what the listing displays. That
is why pushes "succeeded" for months while listings showed no fitment.

This module asks eBay what the catalog actually contains -- Taxonomy API
`get_compatibility_property_values` on the Motors category tree (id 100) -- and repairs
or drops our rows accordingly.

TWO IMPORTANT FACTS ESTABLISHED BY EXPERIMENT (see docs/DESIGN.md):
  * The catalog is the SAME for every Motors *parts* category (33615 and 33742 return
    identical trims), so we probe one fixed category and cache by (year, make, model).
  * The Taxonomy API needs the plain `api_scope`, which the sell.inventory user token does
    NOT have. We mint a separate CLIENT-CREDENTIALS application token here -- no user
    consent needed, just client_id/client_secret from ebay_auth.json.

Cache: data/ebay_compat_cache.json (regenerable; safe to delete).

CLI:
  python3 scripts/ebay_compat_catalog.py --trims 2014 BMW X5
  python3 scripts/ebay_compat_catalog.py --models 2014 BMW
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FILE = os.path.join(ROOT, "ebay_auth.json")
CACHE_FILE = os.path.join(ROOT, "data", "ebay_compat_cache.json")
APP_TOKEN_CACHE = os.path.join(ROOT, ".ebay_app_token_cache.json")   # gitignored

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
BASE = "https://api.ebay.com"
MOTORS_TREE = "100"          # eBay Motors category tree
PROBE_CATEGORY = "33615"     # any Motors parts category; the vehicle catalog is shared

_cache = None
_dirty = False
_app_token = None


# --------------------------------------------------------------------------- token
def _app_access_token(force=False):
    """Client-credentials token for the read-only Taxonomy API (basic api_scope)."""
    global _app_token
    if _app_token and not force:
        return _app_token
    if not force and os.path.exists(APP_TOKEN_CACHE):
        try:
            c = json.load(open(APP_TOKEN_CACHE, encoding="utf-8"))
            if c.get("expires_at", 0) - 120 > time.time():
                _app_token = c["access_token"]
                return _app_token
        except (ValueError, OSError):
            pass
    cid = os.environ.get("EBAY_CLIENT_ID")
    secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not (cid and secret) and os.path.exists(AUTH_FILE):
        try:
            cfg = json.load(open(AUTH_FILE, encoding="utf-8"))
            cid = cid or cfg.get("client_id")
            secret = secret or cfg.get("client_secret")
        except (ValueError, OSError):
            pass
    if not (cid and secret):
        return None
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "scope": "https://api.ebay.com/oauth/api_scope",
    }).encode()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {auth}",
    })
    try:
        with urllib.request.urlopen(req) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, urllib.error.URLError):
        return None
    _app_token = d["access_token"]
    try:
        json.dump({"access_token": _app_token, "expires_at": time.time() + int(d.get("expires_in", 7200))},
                  open(APP_TOKEN_CACHE, "w", encoding="utf-8"))
    except OSError:
        pass
    return _app_token


# --------------------------------------------------------------------------- cache
def _load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.load(open(CACHE_FILE, encoding="utf-8"))
        except (ValueError, OSError):
            _cache = {}
    return _cache


def save_cache():
    """Persist new catalog answers.

    MERGES with whatever is already on disk instead of overwriting. Catalog answers are
    append-only facts, so a union is always correct -- and it means a process holding only
    a partial cache (a short --sku run, a parallel sweep, a test) can never shrink the file
    it shares with everyone else.
    """
    global _dirty
    if not _dirty:
        return
    merged = {}
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            on_disk = json.load(f)
        if isinstance(on_disk, dict):
            merged.update(on_disk)
    except (ValueError, OSError):
        pass
    merged.update(_load_cache())
    try:
        os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
        tmp = f"{CACHE_FILE}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(merged, f, indent=0, sort_keys=True)
        os.replace(tmp, CACHE_FILE)          # atomic: readers never see a partial file
        _dirty = False
    except OSError:
        pass


# --------------------------------------------------------------------------- lookups
def _filter_safe(*values):
    """The Taxonomy `filter` param is comma/colon delimited with no escaping, so a value
    containing either would silently corrupt the query -- eBay would answer 400 and we
    would read that as 'vehicle not in catalog' and DROP good rows. No BMW model needs
    these characters today; if one ever does, fail the lookup (None) instead of lying."""
    return not any(c in str(v) for v in values for c in ",:")


def _property_values(prop, filt, retries=3):
    """Raw Taxonomy lookup. Returns a list of strings, or None on failure (!= empty)."""
    tok = _app_access_token()
    if not tok:
        return None
    q = urllib.parse.urlencode({
        "category_id": PROBE_CATEGORY,
        "compatibility_property": prop,
        "filter": filt,
    })
    url = f"{BASE}/commerce/taxonomy/v1/category_tree/{MOTORS_TREE}/get_compatibility_property_values?{q}"
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {tok}",
            "Accept": "application/json",
            "X-EBAY-C-MARKETPLACE-ID": "EBAY_MOTORS",
        })
        try:
            with urllib.request.urlopen(req) as r:
                d = json.loads(r.read().decode("utf-8", "replace") or "{}")
            return [v["value"] for v in d.get("compatibilityPropertyValues", [])]
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):                     # token aged out mid-sweep
                tok = _app_access_token(force=True)
                if not tok:
                    return None
                continue
            if e.code == 400:                            # unknown model/year -> genuinely empty
                return []
            if e.code == 429 or e.code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
            return None
        except urllib.error.URLError:
            time.sleep(1.5 * (attempt + 1))
    return None


def trims(year, make, model):
    """eBay's catalog trims for one vehicle. [] = vehicle unknown; None = lookup failed."""
    global _dirty
    key = f"T|{year}|{make}|{model}".lower()
    c = _load_cache()
    if key in c:
        return c[key]
    if not _filter_safe(year, make, model):
        return None                                      # unrepresentable -> fail loudly
    vals = _property_values("Trim", f"Year:{year},Make:{make},Model:{model}")
    if vals is None:
        return None                                      # transient -- do NOT cache
    c[key] = vals
    _dirty = True
    return vals


def models(year, make):
    """eBay's catalog models for a year+make. None = lookup failed."""
    global _dirty
    key = f"M|{year}|{make}".lower()
    c = _load_cache()
    if key in c:
        return c[key]
    if not _filter_safe(year, make):
        return None
    vals = _property_values("Model", f"Year:{year},Make:{make}")
    if vals is None:
        return None
    c[key] = vals
    _dirty = True
    return vals


# --------------------------------------------------------------------------- matching
def _norm(s):
    return " ".join(str(s).strip().lower().split())


def match_trim(our_trim, catalog):
    """Map our shorthand trim onto eBay's catalog spellings.

    "xDrive35i" -> ["xDrive35i Sport Utility 4-Door"]      (body-style suffix added)
    "M"         -> ["M Sport Utility 4-Door", "M Sport Sport Utility 4-Door"]
    Word-boundary prefix match only, so "35i" never matches "xDrive35i" and
    "M" never matches "M550i".
    """
    want = _norm(our_trim)
    if not want:
        return []
    exact = [c for c in catalog if _norm(c) == want]
    if exact:
        return exact
    return [c for c in catalog if _norm(c).startswith(want + " ")]


def validate_rows(rows, on_unmatched="drop"):
    """Repair rows against eBay's catalog so they will actually DISPLAY.

    on_unmatched: what to do when a row's Trim has no catalog spelling --
      "drop"     : discard the row  (Rule B / engine parts: never over-claim engines)
      "trimless" : keep Year/Make/Model only (Rule A: the whole family fits anyway)

    Returns (good_rows, report) where report explains every change/drop.
    """
    good, seen, report = [], set(), {"kept": 0, "retrimmed": 0, "trimless": 0,
                                     "dropped_vehicle": 0, "dropped_trim": 0, "lookup_failed": 0,
                                     "wildcard_dropped": 0, "subsumed_by_wildcard": 0,
                                     "notes": []}

    def add(r):
        k = (r["Year"], r["Make"], r["Model"], r.get("Trim", ""))
        if k not in seen:
            seen.add(k)
            good.append(r)

    for r in rows:
        year, make, model = r["Year"], r["Make"], r["Model"]
        cat = trims(year, make, model)
        if cat is None:
            # Taxonomy unreachable -- pass the row through untouched rather than
            # silently shrinking the sweep's output.
            report["lookup_failed"] += 1
            add(dict(r))
            continue
        if not cat:
            report["dropped_vehicle"] += 1
            report["notes"].append(f"{year} {make} {model}: not in eBay's catalog")
            continue
        our = r.get("Trim")
        # Some chassis (the X3/X5/X6 SUVs) have no trim breakdown in our engine map and the
        # expander falls back to repeating the model name -- Trim "X3" on Model "X3". That
        # is not a trim, and eBay's catalog has no such value, so treat the row as trimless
        # rather than dropping it: the year/model fitment is still correct and useful.
        if our and _norm(our) == _norm(model):
            our = None
        if not our:
            report["kept"] += 1
            add({"Year": year, "Make": make, "Model": model})
            continue
        hits = match_trim(our, cat)
        if hits:
            if len(hits) == 1 and _norm(hits[0]) == _norm(our):
                report["kept"] += 1
            else:
                report["retrimmed"] += 1
            for h in hits:
                add({"Year": year, "Make": make, "Model": model, "Trim": h})
        elif on_unmatched == "trimless":
            report["trimless"] += 1
            report["notes"].append(f"{year} {make} {model}: trim '{our}' unknown -> trimless")
            add({"Year": year, "Make": make, "Model": model})
        else:
            report["dropped_trim"] += 1
            report["notes"].append(f"{year} {make} {model}: trim '{our}' unknown -> dropped")

    good = _resolve_wildcards(good, on_unmatched, report)
    save_cache()
    return good, report


def _resolve_wildcards(rows, on_unmatched, report):
    """A trimless row is a WILDCARD: eBay expands it to every trim for that Year/Model. So a
    trimless row sitting alongside trimmed rows for the SAME vehicle silently overrides them.

    That combination is reachable whenever sources are unioned -- e.g. Rule B chassis rows
    carry the donor's engine trims while a part-number row fell back to the literal (trimless)
    vehicle. Left alone, the wildcard re-adds exactly the engines Rule B excluded.

    Resolve it in the direction the rule intends:
      engine-restricted (on_unmatched="drop", Rule B) -> keep the trims, drop the wildcard
      family-wide       (on_unmatched="trimless", Rule A) -> keep the wildcard, drop the
                                                             trims it already subsumes
    """
    trimless, trimmed = set(), set()
    for r in rows:
        key = (r["Year"], r["Make"], r["Model"])
        (trimmed if r.get("Trim") else trimless).add(key)
    conflicted = trimless & trimmed
    if not conflicted:
        return rows
    out = []
    for r in rows:
        key = (r["Year"], r["Make"], r["Model"])
        if key not in conflicted:
            out.append(r)
            continue
        if on_unmatched == "trimless":
            if r.get("Trim"):                 # the wildcard already covers this row
                report["subsumed_by_wildcard"] += 1
                continue
        elif not r.get("Trim"):               # engine-restricted: the wildcard must go
            report["wildcard_dropped"] += 1
            report["notes"].append(
                f"{key[0]} {key[1]} {key[2]}: dropped trimless wildcard that would have "
                "re-added every engine")
            continue
        out.append(r)
    return out


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    if args[0] == "--trims" and len(args) == 4:
        v = trims(args[1], args[2], args[3])
        save_cache()
        print("\n".join(v) if v else f"(no catalog trims -- {'lookup failed' if v is None else 'vehicle unknown'})")
    elif args[0] == "--models" and len(args) == 3:
        v = models(args[1], args[2])
        save_cache()
        print("\n".join(v) if v else "(none)")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
