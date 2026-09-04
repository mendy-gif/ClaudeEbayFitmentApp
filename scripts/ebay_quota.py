#!/usr/bin/env python3
"""
Report eBay's live Trading GetItem allowance, and size a sweep to fit inside it.

WHY: the nightly ran with a hard --limit 2000 for months. Measured on 2026-09-04 that
consumed 1,070 of the 5,000/day GetItem allowance -- 21%. The cap was never a quota
constraint, it was a guess, and it left 79% of the budget unused while the backlog sat
there. But a fixed bigger number is just a different guess: the fraction of SKUs that
reach the expensive Trading guard moves as the skip cache fills and as categories are
learned. So ask eBay what is left and size the run to that.

  python3 scripts/ebay_quota.py              # human-readable
  python3 scripts/ebay_quota.py --limit      # just the SKU limit, for the nightly

Uses an APPLICATION token (plain api_scope), not the user token -- the rate-limit
endpoint requires it. Stdlib only.
"""
import argparse
import base64
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Reserve for the two display audits that run AFTER the sweep (--recent 300, --sample 250)
# plus headroom. If the sweep eats the whole allowance the audits fail and we lose the only
# check that what we pushed actually DISPLAYS -- which is worth more than a few hundred
# extra pushes.
AUDIT_RESERVE = 700
# Share of processed SKUs that reach the Trading guard. The rest are decided on cheap
# Inventory reads (dead category, no donor, ended). Measured 539/2000 = 27% on 2026-09-04;
# assume WORSE than measured, because caching the dead categories means a higher share of
# each batch now reaches the guard.
GUARD_RATE = 0.60
# Bound the runtime regardless of quota -- a sweep runs ~1.7s/SKU, so 8000 is ~3.7 hours.
MAX_LIMIT = 8000
MIN_LIMIT = 200


def app_token():
    cfg = json.load(open(os.path.join(ROOT, "ebay_auth.json"), encoding="utf-8"))
    data = urllib.parse.urlencode({"grant_type": "client_credentials",
                                   "scope": "https://api.ebay.com/oauth/api_scope"}).encode()
    basic = base64.b64encode(f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
    req = urllib.request.Request(
        "https://api.ebay.com/identity/v1/oauth2/token", data=data,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())["access_token"]


def getitem_quota():
    """(used, limit, remaining) for Trading GetItem, or None if the call fails."""
    try:
        req = urllib.request.Request(
            "https://api.ebay.com/developer/analytics/v1_beta/rate_limit/",
            headers={"Authorization": f"Bearer {app_token()}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except Exception:                                       # noqa: BLE001
        return None
    for api in data.get("rateLimits", []):
        for res in api.get("resources", []):
            if res.get("name") == "GetItem":
                for rate in res.get("rates", []):
                    used, cap = int(rate.get("count") or 0), int(rate.get("limit") or 0)
                    if cap:
                        return used, cap, max(cap - used, 0)
    return None


def safe_limit():
    """How many SKUs tonight's sweep can afford. Falls back to the old fixed 2000 if the
    quota endpoint is unreachable -- never guess HIGH on a failed read."""
    q = getitem_quota()
    if not q:
        return 2000, "quota unreadable -- falling back to the previous fixed limit"
    used, cap, left = q
    budget = left - AUDIT_RESERVE
    if budget <= 0:
        return MIN_LIMIT, f"only {left} calls left, under the {AUDIT_RESERVE} audit reserve"
    n = int(budget / GUARD_RATE)
    n = max(MIN_LIMIT, min(MAX_LIMIT, n))
    return n, f"{left} of {cap} left, reserving {AUDIT_RESERVE} for audits"


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", action="store_true", help="print only the SKU limit")
    args = ap.parse_args()
    n, why = safe_limit()
    if args.limit:
        print(n)
        return
    q = getitem_quota()
    if q:
        used, cap, left = q
        print(f"GetItem: {used:,}/{cap:,} used, {left:,} left ({100*used//cap}%)")
    print(f"sweep limit tonight: {n:,} SKUs   ({why})")


if __name__ == "__main__":
    main()
