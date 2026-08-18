#!/usr/bin/env python3
"""
One-time helper: capture an eBay OAuth REFRESH token (~18 months) so the runner can
mint its own 2-hour access tokens forever (no more pasting token.txt every 2h).

eBay's portal "Get a User Token" only shows the short 2h access token. The long-lived
refresh token comes from the OAuth authorization-code grant, which this walks you through:

  1. You give it your app's Client ID, Client Secret, and RuName (redirect URL name).
  2. It prints a sign-in link. You open it, sign in as the seller, and click Agree.
  3. eBay redirects your browser to your app's redirect URL with "?code=..." in the address bar.
  4. You paste that whole URL (or just the code) back here.
  5. It exchanges the code for a refresh token and writes ebay_auth.json.

Then verify:  python3 scripts/ebay_auth.py --check

Usage:
  python3 scripts/ebay_get_refresh_token.py
"""
import base64
import getpass
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUTH_FILE = os.path.join(ROOT, "ebay_auth.json")
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
AUTHORIZE_URL = "https://auth.ebay.com/oauth2/authorize"
DEFAULT_SCOPES = ["https://api.ebay.com/oauth/api_scope/sell.inventory"]


def _existing():
    if os.path.exists(AUTH_FILE):
        try:
            return json.load(open(AUTH_FILE, encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def _prompt(cfg):
    print("Enter your eBay app details (from developer.ebay.com -> Application Keysets).")
    print("Values are read locally and written only to ebay_auth.json (gitignored).\n")
    cid = cfg.get("client_id") or input("  App ID (Client ID): ").strip()
    secret = cfg.get("client_secret") or getpass.getpass("  Cert ID (Client Secret) [hidden]: ").strip()
    ru = cfg.get("ru_name") or input("  RuName (eBay Redirect URL name, e.g. Dov_Auto_Sales-...): ").strip()
    scopes = cfg.get("scopes") or DEFAULT_SCOPES
    return cid, secret, ru, scopes


def _extract_code(pasted):
    pasted = pasted.strip()
    if "code=" in pasted:                       # a full redirect URL was pasted
        q = urllib.parse.urlparse(pasted).query or pasted.split("?", 1)[-1]
        code = urllib.parse.parse_qs(q).get("code", [None])[0]
        if code:
            return code
    return urllib.parse.unquote(pasted)         # assume they pasted just the code


def _exchange(cid, secret, ru, code):
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": ru,
    }).encode()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {auth}",
    })
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    cfg = _existing()
    cid, secret, ru, scopes = _prompt(cfg)
    if not (cid and secret and ru):
        sys.exit("Need App ID, Cert ID, and RuName.")

    consent = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code",
        "redirect_uri": ru, "scope": " ".join(scopes),
    })
    print("\n1) Open this link in your browser, sign in as the seller, and click Agree:\n")
    print(consent)
    print("\n2) eBay will redirect your browser to your app's URL. Copy the WHOLE address")
    print("   from the address bar (it contains 'code=...') and paste it below.\n")
    pasted = input("Paste the redirect URL (or just the code): ").strip()
    code = _extract_code(pasted)
    if not code:
        sys.exit("Could not find an authorization code in what you pasted.")

    try:
        tok = _exchange(cid, secret, ru, code)
    except urllib.error.HTTPError as e:
        sys.exit(f"Exchange failed HTTP {e.code}: {e.read().decode('utf-8','replace')[:400]}\n"
                 f"(The code expires in ~5 min — re-run and paste a fresh one if needed.)")

    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit(f"No refresh_token in eBay's response: {json.dumps(tok)[:300]}")

    out = dict(cfg)
    out.update({"client_id": cid, "client_secret": secret, "ru_name": ru,
                "scopes": scopes, "refresh_token": refresh})
    json.dump(out, open(AUTH_FILE, "w", encoding="utf-8"), indent=2)
    months = int(tok.get("refresh_token_expires_in", 0)) // (30 * 24 * 3600)
    print(f"\nSaved refresh token to {os.path.relpath(AUTH_FILE, ROOT)} "
          f"(valid ~{months} months).")
    print("Verify with:  python3 scripts/ebay_auth.py --check")
    print("Then you can delete token.txt — the runner mints its own tokens now.")


if __name__ == "__main__":
    main()
