# Setting up on a Mac (step by step)

For running the fitment pipeline locally on a Mac Mini. Written for a non-developer — follow the
steps in order. On the Mac, eBay's API **is** reachable (unlike a Claude cloud session), so you can
run everything directly here — no Codespace needed.

You'll use the **Terminal** app (Applications → Utilities → Terminal). Copy/paste one command at a
time and press Return.

## 1. Install the basics

**a) Command Line Tools** (gives you `git` and `python3`). Paste this and follow the popup:
```bash
xcode-select --install
```

**b) Check they're there:**
```bash
git --version
python3 --version      # expect 3.9 or newer; 3.11+ is ideal
```
If `python3` says 3.8 or older, install a newer one with Homebrew:
```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python
```

**c) Claude Code** — install from https://claude.com/claude-code (or the desktop app), so you can
keep working with Claude on this project from the Mac.

## 2. Get the project

```bash
cd ~
git clone https://github.com/mendy-gif/ClaudeEbayFitmentApp.git
cd ClaudeEbayFitmentApp
git checkout claude/ebay-fitment-chassis-rules-3pztn4
```

Now the whole project lives at `~/ClaudeEbayFitmentApp`. Open it in Claude Code from there.

## 3. (Optional) install the one dependency

Only needed if you'll rebuild the reference tables from Excel. The daily sweep does NOT need it.
```bash
pip3 install -r requirements.txt
```

## 4. Recreate your secret files

These are deliberately **not** in the repo (they're private). Create them in the project folder
(`~/ClaudeEbayFitmentApp`). Use a text editor or Claude Code — don't share their contents.

**eBay — pick ONE mode:**

- *Simple (manual):* a file named **`token.txt`** containing just your eBay OAuth user token.
  Note: it expires every ~2 hours, so you re-paste a fresh one for long runs.

- *Better (auto-refresh, no re-pasting):* a file named **`ebay_auth.json`**:
  ```json
  {
    "client_id": "YOUR-EBAY-APP-ID",
    "client_secret": "YOUR-EBAY-CERT-ID",
    "refresh_token": "v^1.1#...",
    "scopes": ["https://api.ebay.com/oauth/api_scope/sell.inventory"]
  }
  ```
  See `docs/DESIGN.md` §7.1 for how to get the refresh token.

**Shopify** — a file named **`shopify_token.txt`**:
```
SHOPIFY_STORE=oe-mgarage.myshopify.com
SHOPIFY_TOKEN=shpat_xxxxxxxx
```

## 5. Verify everything works (no eBay writes)

```bash
python3 scripts/selftest.py            # offline sanity check — expect all PASS
python3 scripts/ebay_auth.py --check   # only if you set up ebay_auth.json; expect "OK - minted..."
python3 scripts/ebay_batch.py plan --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --limit 5
```
The `plan` run is a dry-run — it writes `data/batch_plan.csv` and makes **no** changes to eBay. If you
see `push` / `skip` rows (not an auth error), you're good.

## 6. Run for real

```bash
python3 scripts/ebay_batch.py apply --from-shopify --from-inventory \
  --partnumber-fitment spreadsheet-fitment/data/built/ebay_ready_fitment.csv --sleep 0.15 --live
```
It resumes where it left off each time (the ledger), so you can stop and restart freely. With
`ebay_auth.json` set up, it won't stall at the 2-hour token limit.

## Tips

- Everything is in `CLAUDE.md` — if you forget a command, ask Claude "how do I run the sweep?" and it
  will read that file.
- If a `plan` run shows lots of "category not in tree → default", the category tree file may be missing;
  regenerate it with `python3 scripts/ebay_fetch_categories.py` (needs your eBay token).
