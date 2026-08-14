#!/usr/bin/env python3
"""
eBay OAuth token management — mint short-lived (2h) ACCESS tokens on demand from a
long-lived (~18-month) REFRESH token, so unattended runs never die at the 2-hour wall.

The runner calls get_access_token(); this module mints, caches (with expiry), and
re-mints as needed. If no refresh credentials are configured it returns None and the
caller falls back to a hand-pasted token.txt (the temporary manual mode).

Credentials (either env vars or ebay_auth.json — both gitignored):
  EBAY_CLIENT_ID / client_id          your app's App ID (Client ID)
  EBAY_CLIENT_SECRET / client_secret  your app's Cert ID (Client Secret)
  EBAY_REFRESH_TOKEN / refresh_token  minted once via the user-consent flow
  scopes (ebay_auth.json only)        OAuth scopes the refresh token was granted;
                                      defaults to sell.inventory. Must be a subset
                                      of what the refresh token was consented for.

One-time setup to obtain the refresh token: see docs/DESIGN.md sec 7.

CLI:
  python3 scripts/ebay_auth.py            # mint + print a fresh access token
  python3 scripts/ebay_auth.py --check    # verify creds work, print expiry
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
AUTH_FILE = os.path.join(ROOT, "ebay_auth.json")            # gitignored
CACHE_FILE = os.path.join(ROOT, ".ebay_token_cache.json")   # gitignored
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
DEFAULT_SCOPES = ["https://api.ebay.com/oauth/api_scope/sell.inventory"]


def load_auth():
    cfg = {}
    if os.path.exists(AUTH_FILE):
        try:
            cfg = json.load(open(AUTH_FILE, encoding="utf-8"))
        except (ValueError, OSError):
            cfg = {}
    cid = os.environ.get("EBAY_CLIENT_ID") or cfg.get("client_id")
    secret = os.environ.get("EBAY_CLIENT_SECRET") or cfg.get("client_secret")
    refresh = os.environ.get("EBAY_REFRESH_TOKEN") or cfg.get("refresh_token")
    scopes = cfg.get("scopes") or DEFAULT_SCOPES
    return cid, secret, refresh, scopes


def _mint(cid, secret, refresh, scopes):
    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "scope": " ".join(scopes),
    }).encode()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Authorization": f"Basic {auth}",
    })
    with urllib.request.urlopen(req) as r:
        d = json.loads(r.read().decode("utf-8", "replace"))
    return d["access_token"], int(d.get("expires_in", 7200))


def get_access_token(force=False):
    """Return a valid access token, minting/caching as needed. None if unconfigured."""
    if not force and os.path.exists(CACHE_FILE):
        try:
            c = json.load(open(CACHE_FILE, encoding="utf-8"))
            if c.get("expires_at", 0) - 120 > time.time():   # 2-min safety margin
                return c["access_token"]
        except (ValueError, OSError):
            pass
    cid, secret, refresh, scopes = load_auth()
    if not (cid and secret and refresh):
        return None                                          # -> caller uses token.txt
    tok, ttl = _mint(cid, secret, refresh, scopes)
    try:
        json.dump({"access_token": tok, "expires_at": time.time() + ttl},
                  open(CACHE_FILE, "w", encoding="utf-8"))
    except OSError:
        pass
    return tok


def main():
    check = "--check" in sys.argv
    cid, secret, refresh, scopes = load_auth()
    if not (cid and secret and refresh):
        sys.exit("No refresh credentials (set EBAY_CLIENT_ID/SECRET/REFRESH_TOKEN or ebay_auth.json).")
    try:
        tok, ttl = _mint(cid, secret, refresh, scopes)
    except urllib.error.HTTPError as e:
        sys.exit(f"Mint failed HTTP {e.code}: {e.read().decode('utf-8','replace')[:400]}")
    if check:
        print(f"OK - minted an access token valid ~{ttl}s ({ttl // 60} min). Scopes: {' '.join(scopes)}")
    else:
        print(tok)


if __name__ == "__main__":
    main()
