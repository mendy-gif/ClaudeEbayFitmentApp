#!/usr/bin/env python3
"""
ebay_inventory_test.py — the decisive "is this listing an Inventory-API item?" test.

Answers the one question that decides the fitment project's architecture (see
docs/DESIGN.md sec 5.1 and docs/EBAY_SETUP.md):

  * getInventoryItem returns 200  -> PATH A: listing IS an Inventory-API item.
      Compatibility can be set by SKU and should ride across relists. ~one-time push.
  * getInventoryItem returns 25710 -> PATH B: listing was made via Trading API / eBay UI.
      No inventory item behind the SKU; SKU-based compatibility can't reach it.
      Needs bulkMigrateListing, or per-relist Trading ReviseItem (continuous system).

Zero dependencies: Python 3 standard library only. Read-only against the live
account (getInventoryItem is a GET) — safe to run against production.

Usage:
  # Run the decisive test (needs a user access token with sell.inventory[.readonly]):
  python3 scripts/ebay_inventory_test.py test --token "ACCESS_TOKEN" --sku "SKU1" [--sku "SKU2" ...]

  # Optional helper: exchange an OAuth authorization code for tokens (Part 4B of the setup doc):
  python3 scripts/ebay_inventory_test.py exchange \
      --client-id "APPID" --cert-id "CERTID" --runame "RUNAME" --code "AUTH_CODE"

  # Add --sandbox to hit the eBay Sandbox host instead of Production.

The token can also be supplied via the EBAY_ACCESS_TOKEN environment variable
instead of --token, so it doesn't land in your shell history.
"""

import argparse
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

PROD = {
    "api": "https://api.ebay.com",
    "label": "PRODUCTION",
}
SANDBOX = {
    "api": "https://api.sandbox.ebay.com",
    "label": "SANDBOX",
}


def _hosts(sandbox: bool):
    return SANDBOX if sandbox else PROD


def _get(url: str, headers: dict):
    """GET returning (status_code, parsed_json_or_text)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req) as resp:
            body = resp.read().decode("utf-8", "replace")
            return resp.status, _maybe_json(body)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        return e.code, _maybe_json(body)
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def _post_form(url: str, headers: dict, form: dict):
    data = urllib.parse.urlencode(form).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, _maybe_json(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, _maybe_json(e.read().decode("utf-8", "replace"))
    except urllib.error.URLError as e:
        return None, {"_transport_error": str(e)}


def _maybe_json(text: str):
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


def _extract_error_ids(payload):
    ids = []
    if isinstance(payload, dict):
        for err in payload.get("errors", []) or []:
            if isinstance(err, dict) and "errorId" in err:
                ids.append(err["errorId"])
    return ids


def cmd_test(args):
    host = _hosts(args.sandbox)
    token = args.token or os.environ.get("EBAY_ACCESS_TOKEN")
    if not token:
        print("ERROR: no token. Pass --token or set EBAY_ACCESS_TOKEN.", file=sys.stderr)
        return 2

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    print(f"Environment: {host['label']}  ({host['api']})")
    print(f"Testing {len(args.sku)} SKU(s)...\n")

    verdicts = []
    for sku in args.sku:
        url = f"{host['api']}/sell/inventory/v1/inventory_item/{urllib.parse.quote(sku, safe='')}"
        status, payload = _get(url, headers)
        error_ids = _extract_error_ids(payload)

        if status == 200:
            verdict = "PATH A"
            reason = "Inventory-API item (compatibility can be set by SKU; should persist across relist)"
        elif status == 404 and 25710 in error_ids:
            verdict = "PATH B"
            reason = "25710: Trading-API/UI listing, no inventory item behind this SKU"
        elif status in (401, 403):
            verdict = "AUTH?"
            reason = f"HTTP {status}: token expired / wrong scope / wrong account (not a listing-type answer)"
        elif status == 404:
            verdict = "PATH B?"
            reason = f"HTTP 404 but errorId!=25710 ({error_ids}); inspect payload"
        elif status is None:
            verdict = "NET"
            reason = f"transport error: {payload.get('_transport_error')}"
        else:
            verdict = "??"
            reason = f"HTTP {status}, errorIds={error_ids}"

        verdicts.append(verdict)
        print(f"  [{verdict:7}] {sku}")
        print(f"            {reason}")
        if args.verbose:
            print("            raw: " + json.dumps(payload)[:800])
        print()

    # Summary
    a = verdicts.count("PATH A")
    b = sum(1 for v in verdicts if v.startswith("PATH B"))
    print("-" * 60)
    print(f"Summary: PATH A = {a}, PATH B = {b}, other = {len(verdicts) - a - b}")
    if a and not b:
        print("=> Inventory-API managed. Proceed to the Sandbox relist-persistence check (DESIGN sec 5.2).")
    elif b and not a:
        print("=> Trading/UI listings. Design for bulkMigrateListing or per-relist ReviseItem (continuous).")
    elif a and b:
        print("=> MIXED. Dismantly listings are not uniform — the pipeline must handle both paths.")
    else:
        print("=> No clear listing-type verdict. Fix auth/network above and re-run.")
    return 0


def cmd_exchange(args):
    host = _hosts(args.sandbox)
    token_url = f"{host['api']}/identity/v1/oauth2/token"
    basic = base64.b64encode(f"{args.client_id}:{args.cert_id}".encode()).decode()
    headers = {
        "Authorization": f"Basic {basic}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    form = {
        "grant_type": "authorization_code",
        "code": args.code,
        "redirect_uri": args.runame,
    }
    status, payload = _post_form(token_url, headers, form)
    print(f"HTTP {status}")
    print(json.dumps(payload, indent=2) if isinstance(payload, (dict, list)) else payload)
    if isinstance(payload, dict) and "access_token" in payload:
        print("\nOK. Use the access_token above with:  test --token '<access_token>'")
        print("Store the refresh_token securely for the real automation (do NOT commit it).")
        return 0
    return 1


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sandbox", action="store_true", help="Use eBay Sandbox host instead of Production")
    sub = p.add_subparsers(dest="cmd", required=True)

    t = sub.add_parser("test", help="Run the getInventoryItem decisive test")
    t.add_argument("--token", help="User access token (or set EBAY_ACCESS_TOKEN)")
    t.add_argument("--sku", action="append", required=True, help="SKU to test (repeatable)")
    t.add_argument("--verbose", action="store_true", help="Print raw response bodies")
    t.set_defaults(func=cmd_test)

    e = sub.add_parser("exchange", help="Exchange an OAuth auth code for tokens")
    e.add_argument("--client-id", required=True)
    e.add_argument("--cert-id", required=True)
    e.add_argument("--runame", required=True, help="Your RuName (used as redirect_uri)")
    e.add_argument("--code", required=True, help="Authorization code from the consent redirect")
    e.set_defaults(func=cmd_exchange)

    args = p.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
