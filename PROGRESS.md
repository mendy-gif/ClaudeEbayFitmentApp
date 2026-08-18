# Where the fitment project stands

Plain-English status. Updated as work happens, so a new chat (or you) can pick up
without re-reading the whole history. Technical detail lives in `CLAUDE.md` and
`docs/DESIGN.md`; this is the running summary.

**Last updated:** 2026-08-18

---

## The headline

Fitment was being pushed to eBay successfully for months and **displaying nothing** on
engine parts. That is fixed and proven on live listings.

| | Before | Now |
|---|---|---|
| Engine parts (Rule B) displaying | 30% | **76%** |
| Body/interior parts (Rule A) displaying | 96% | 96% |
| Overall | ~56% | **87%** |

The remaining 13% is not fixable — see "Known dead ends" below.

## What was actually wrong

eBay spells a trim `xDrive35i Sport Utility 4-Door`. Our rules produced `xDrive35i`.
eBay **accepted the short version, stored it, returned HTTP 200 — and silently left it
off the listing.** No error, no warning. That is why it looked like it was working.

Body/interior parts were unaffected because they don't specify a trim at all.

## Current state

- **305 listings** pushed with the fix. 100% of them display, except those in the dead
  categories below.
- **~7,500 listings** not yet swept.
- **Nightly automation** is set up and armed (see below).
- All work is committed and pushed to branch `claude/ebay-fitment-chassis-rules-3pztn4`.

## The nightly automation

Runs at ~3 AM ET daily via GitHub Actions (`.github/workflows/fitment-sweep.yml`).

Each night it: self-tests → pushes fitment to up to **1,500** listings → **audits what it
just pushed and fails loudly if fitment stops displaying** → saves its progress.

That audit step is the important part. The original bug survived for months precisely
because "the push succeeded" was treated as proof. It isn't. Only what the listing
*shows* counts.

At 1,500/night the backlog clears in about 5 nights. After that, nightly runs only see
new listings and finish in minutes.

**History:** the schedule had been firing since Aug 15 and failing in ~10 seconds every
night because the GitHub credentials were never added. It never pushed anything. The
credentials went in on Aug 18.

## Known dead ends (not fixable)

**39 listings in 9 categories will never show fitment**, no matter what we send. eBay
simply doesn't render a fitment table in those categories. Proven by pushing perfect,
catalog-verified data and still getting nothing.

Biggest group: **19 ECUs** sitting in *Performance & Racing > Electrical Components >
Other*. Also car audio (speakers, amps, subwoofers, head units), wheels, and emblems.

The only fix would be re-categorising those listings into *Car & Truck Parts*, which is a
commercial decision (it changes search placement and fees). **Decided against for now.**

The list is recorded in `data/nondisplay_categories.json` so the nightly audit doesn't
keep flagging them as failures.

## How to check on things yourself

```bash
# Did the fitment actually show up? (read-only, safe any time)
python3 scripts/ebay_display_audit.py --quiet

# What's going on with one listing?
python3 scripts/ebay_inspect.py 52566 --token "$(python3 scripts/ebay_auth.py)"

# Is the logic healthy? (offline, 10 seconds)
python3 scripts/selftest.py
```

The nightly run's results appear at
`github.com/mendy-gif/ClaudeEbayFitmentApp/actions` — click the newest run and read
the summary at the bottom.

## Open items

- [ ] **Bump the GitHub Actions versions.** They warn that Node.js 20 is deprecated;
      harmless now, will eventually break the nightly job.
- [ ] **No Shopify credentials on this Mac.** The donor data is committed so everything
      works, but new SKUs added in Shopify won't be discovered automatically until this
      is sorted.
- [ ] **Nobody has visually confirmed a listing page.** Every check so far reads eBay's
      API. Opening a live listing and eyeballing the fitment box would close the last
      gap in the evidence.
- [ ] **7,500 listings still to sweep** — the nightly job works through these.

## Working agreement

Live pushes go only to the listings you name. Before any wider batch, the count gets
stated and waits for your yes. Dry runs and read-only audits need no approval.

This exists because on 2026-08-18 one test SKU was approved and 305 got swept. There was
no snapshot, so it couldn't be undone.
