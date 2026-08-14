#!/usr/bin/env python3
"""Step 4 — push fitment to eBay (DRY-RUN BY DEFAULT).

Reads ebay_ready_fitment.csv, groups vehicles per listing (by guid = SKU), and
builds an eBay Trading API ReviseFixedPriceItem request carrying an
ItemCompatibilityList (parts compatibility by application: Year/Make/Model).

Nothing is sent unless you pass --live. --live needs an eBay OAuth user token with
the sell scope (see ../../docs/EBAY_SETUP.md), read from --token, $EBAY_ACCESS_TOKEN,
or ./token.txt. eBay validates each Year+Model against its catalog on submit and
returns an error for any invalid pair; those are reported per listing.

Usage:
  python3 push_to_ebay.py                         # dry-run: print first few payloads
  python3 push_to_ebay.py --in ../data/ebay_ready_fitment.csv --limit 3
  python3 push_to_ebay.py --live                  # actually revise listings
"""
import argparse
import csv
import os
import sys
import urllib.request
import urllib.error
from collections import defaultdict
from xml.sax.saxutils import escape

TRADING_ENDPOINT = "https://api.ebay.com/ws/api.dll"
COMPAT_LEVEL = "1247"
SITE_ID = "0"  # US


def load_token(arg):
    if arg:
        return arg.strip()
    if os.environ.get("EBAY_ACCESS_TOKEN"):
        return os.environ["EBAY_ACCESS_TOKEN"].strip()
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "token.txt"),
              os.path.join(here, "..", "..", "token.txt")):
        if os.path.exists(p):
            return "".join(open(p, encoding="utf-8").read().split())
    return None


def group(in_path):
    """guid -> ordered unique list of (year, make, ebay_model)."""
    listings = defaultdict(list)
    seen = defaultdict(set)
    with open(in_path, newline="") as f:
        for L in csv.DictReader(f):
            model = L["ebay_model"].strip()
            if not model or L["mapping_flag"] == "UNMAPPED":
                continue
            key = (L["year"], L["make"], model)
            if key not in seen[L["guid"]]:
                seen[L["guid"]].add(key)
                listings[L["guid"]].append(key)
    return listings


def build_xml(sku, vehicles, token):
    compat = []
    for (year, make, model) in vehicles:
        compat.append(
            "    <Compatibility>\n"
            "      <NameValueList><Name>Year</Name><Value>%s</Value></NameValueList>\n"
            "      <NameValueList><Name>Make</Name><Value>%s</Value></NameValueList>\n"
            "      <NameValueList><Name>Model</Name><Value>%s</Value></NameValueList>\n"
            "    </Compatibility>" % (escape(year), escape(make), escape(model)))
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<ReviseFixedPriceItemRequest xmlns="urn:ebay:apis:eBLBaseComponents">\n'
        "  <RequesterCredentials><eBayAuthToken>%s</eBayAuthToken></RequesterCredentials>\n"
        "  <Item>\n"
        "    <SKU>%s</SKU>\n"
        "    <ItemCompatibilityList>\n%s\n    </ItemCompatibilityList>\n"
        "  </Item>\n"
        "</ReviseFixedPriceItemRequest>\n"
        % (escape(token or "TOKEN"), escape(sku), "\n".join(compat))
    )


def send(xml, token, call="ReviseFixedPriceItem"):
    headers = {
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPAT_LEVEL,
        "X-EBAY-API-CALL-NAME": call,
        "X-EBAY-API-SITEID": SITE_ID,
        "X-EBAY-API-IAF-TOKEN": token,
        "Content-Type": "text/xml",
    }
    req = urllib.request.Request(TRADING_ENDPOINT, data=xml.encode("utf-8"),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="../data/ebay_ready_fitment.csv")
    ap.add_argument("--live", action="store_true", help="actually send (default: dry-run)")
    ap.add_argument("--token", default=None)
    ap.add_argument("--limit", type=int, default=3, help="dry-run: how many payloads to print")
    a = ap.parse_args()

    listings = group(a.inp)
    total_v = sum(len(v) for v in listings.values())
    print(f"{len(listings)} listings, {total_v} compatibility rows.\n")

    if not a.live:
        token = load_token(a.token)  # only to render the payload; not required for dry-run
        for i, (sku, veh) in enumerate(listings.items()):
            if i >= a.limit:
                print(f"... ({len(listings) - a.limit} more listings not shown)")
                break
            print(f"===== {sku}  ({len(veh)} vehicles) =====")
            print(build_xml(sku, veh, token))
        print("\nDRY-RUN. Re-run with --live (and a valid token) to apply.")
        return

    token = load_token(a.token)
    if not token:
        sys.exit("No eBay token. Provide --token, $EBAY_ACCESS_TOKEN, or token.txt "
                 "(see ../../docs/EBAY_SETUP.md).")
    ok = fail = 0
    for sku, veh in listings.items():
        try:
            resp = send(build_xml(sku, veh, token), token)
            if "<Ack>Success</Ack>" in resp or "<Ack>Warning</Ack>" in resp:
                ok += 1
                print(f"  OK   {sku}  ({len(veh)} vehicles)")
            else:
                fail += 1
                short = resp.replace("\n", " ")
                idx = short.find("<ShortMessage>")
                print(f"  FAIL {sku}: {short[idx:idx+160] if idx>=0 else short[:160]}")
        except urllib.error.HTTPError as e:
            fail += 1
            print(f"  HTTP {e.code} {sku}: {e.read().decode('utf-8','replace')[:160]}")
    print(f"\nDone. success={ok} fail={fail}")


if __name__ == "__main__":
    main()
