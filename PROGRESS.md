# Where the fitment project stands

Plain-English status. Updated as work happens, so a new chat (or you) can pick up
without re-reading the whole history. Technical detail lives in `CLAUDE.md` and
`docs/DESIGN.md`; this is the running summary.

**Last updated:** 2026-08-19 (nightly automation proven; donor pool ~30% bigger than we thought)

---

## The headline

Fitment was being pushed to eBay successfully for months and **displaying nothing** on
engine parts. That is fixed, proven on live listings, and now runs itself nightly.

**Of every listing we have pushed that sits in a category eBay renders fitment in,
100% displays.** No leaks — no listing shows a vehicle we did not send.

## What was actually wrong

eBay spells a trim `xDrive35i Sport Utility 4-Door`. Our rules produced `xDrive35i`.
eBay **accepted the short version, stored it, returned HTTP 200 — and silently left it
off the listing.** No error, no warning. That is why it looked like it was working.

Body/interior parts were unaffected because they don't specify a trim at all — which is
why the failure stayed invisible: over half the listings looked fine.

## The clearest proof it works

Two parts off the SAME donor car (an E90 328i), pushed under different rules:

| | SKU 60962 — Rule B (water pump) | SKU 60972 — Rule A (seatbelt buckle) |
|---|---|---|
| Displays | 325i, 325xi, 328i, 328xi, 330i, 330xi | 325i … **335d, 335i, 335i xDrive** |
| Count | 61 vehicles | 88 vehicles |

The water pump is correctly restricted to the N52 six — no 335i (N54 turbo), no 335d
(diesel). The seatbelt correctly fits the whole family. Verified on the live storefront.

## Current state (2026-08-19)

- **351 listings** pushed and verified displaying.
- **10,000+** BMW products in Shopify — see "The donor pool", this grew a lot today.
- **Nightly automation runs at 03:30 ET** and is proven: it refreshes donor data, pushes
  up to 2,000 listings, then **audits its own work and fails loudly if anything stops
  displaying**.
- All work is committed to branch `claude/ebay-fitment-chassis-rules-3pztn4`.

## The nightly automation

`.github/workflows/fitment-sweep.yml`, 03:30 ET daily. Each night it:

1. Checks credentials, runs the offline self-test — broken logic never reaches eBay
2. Refreshes donor data from Shopify
3. Pushes fitment to up to **2,000** listings
4. **Audits the listings it just pushed** — fails the run if under 70% display
5. **Drift watch** — samples 250 older listings, rotating, and fails if another system has
   overwritten our fitment
6. Saves its progress, retrying if someone pushed to the branch mid-run

Steps 4 and 5 are the point. The original bug survived for months because "the push
succeeded" was treated as proof. It isn't — only what the listing *shows* counts.

**Notification:** GitHub emails you when a scheduled run fails. Nothing else to set up.

**History:** the schedule fired nightly from Aug 15 and failed in ~10 seconds every time —
the credentials were never added. It has only genuinely worked since Aug 18.

## The donor pool — bigger than we thought (found 2026-08-19)

eBay carries ~22,000 in-stock SKUs while the donor dump saw only 7,858. Chasing that gap
found three separate problems, all now fixed:

1. **The vendor field is usually the CHASSIS CODE, not the make.** A 2015 X5 door shell has
   vendor `F85`; a 2020 X3 has `G01`. Filtering on `vendor:BMW` excluded thousands of real
   BMW products.
2. **A whole generation of products carries no donor tags at all.** SKU 17089 is "OEM BMW
   F80 F82 F87 M2 M3 M4 Steering Wheel" with vendor `F80`, tags exactly `["F80"]`, and the
   part number in productType. No make, model, year or engine anywhere.
3. **Pagination was silently dropping records.** The Shopify query had no explicit sort, so
   the ordering drifted across a 158-page walk — some products came back twice, others
   never. It reported "Wrote 7858 donors" and looked perfectly healthy.

The dump now pages the whole active catalogue and decides in Python: BMW if the tags say
so, or if the vendor matches a chassis code from the committed reference. It also tolerates
the vendor field being tidied up later, so a store cleanup cannot silently delete donors.

**What the new donors can and cannot do:** 75 of 94 chassis codes resolve from the vendor
alone, so Rule A works for them. The other 19 (`E90` vs `E90 M3`, `F10` vs `F10 M5`) are
ambiguous and land in *review* rather than guessing. **Rule B produces nothing for them** —
they have no engine code, and an engine part with no engine restriction would claim every
engine. It correctly waits for data that does not exist yet.

## The other real constraint: eBay's daily API allowance

Every listing costs one eBay "Trading" call just to check what fitment it already has,
before we touch it. That allowance is **5,000/day**, resetting 07:00 UTC, and it is what
limits how fast the backlog clears.

Measured the hard way on 2026-08-18: a run asking for 1,500 listings hit the limit at
number 867, then spent 42 more minutes and 626 listings asking a question eBay had already
refused. The sweep now stops the moment it sees that refusal.

Two fixes made the budget go much further:

- **The cheap checks run first.** ~14,000 eBay SKUs have no BMW donor at all; we were
  spending the scarce call on them before discovering that. Now they cost nothing.
- **The nightly limit applies to listings worth processing**, not to a raw slice of eBay's
  list. Previously it took the same first N every night — and since all finished work sat
  inside that slice, the sweep was converging on doing nothing while still reporting success.

A skip cache remembers dead ends (no eBay listing, curated by another system) with
per-reason expiry so they are not re-checked nightly. Transient failures are never cached —
a failure to *look* must never be recorded as a fact.

## Known dead ends (not fixable)

**Some listings will never show fitment**, no matter what we send. eBay simply does not
render a fitment table in certain categories. Proven by pushing perfect, catalog-verified
data and still getting nothing.

Affected: car audio (speakers, amps, subwoofers, head units), wheels, emblems, and a
Performance & Racing catch-all category holding ~19 ECUs. The only fix would be
re-categorising those listings, which changes search placement and fees — **decided against**.

Recorded in `data/nondisplay_categories.json` so the nightly audit does not keep flagging
them as failures.

## Watch: control modules moving out of a dead category (2026-08-21)

mendy remapped TPMS / telematics / park-assist / suspension / fuel-pump / cooling-fan /
body-control modules from eBay category **107062** (Performance & Racing > Electrical
Components > *Other*, which never renders a fitment table) to **33596** (Car & Truck Parts >
Starters, Alternators, ECUs & Wiring > ECUs & Computer Modules, which does). New listings use
the new category immediately; existing ones update as Dismantly resyncs, over roughly 40 days.

~100 audited listings sit in 107062 carrying **1,223 pushed vehicle rows nobody can see**.

**Expectation: no re-push needed.** Compatibility is stored against the SKU in the Inventory
store, independent of category — the rows are already there, they just are not rendered.
Moving the listing should make them appear on its own.

**This is unverified.** No listing has crossed that boundary yet. Check in a couple of weeks:
take a SKU from `data/dead_category_listings.csv` that has moved to 33596 and run
`ebay_inspect.py` on it. If it displays, nothing to do. If it does not, those SKUs need a
re-push — and note the guard will skip them, since they are already ledgered with the current
`CATALOG_ERA`, so it would take `--force` with an explicit SKU list.

Either way the overall display rate should climb over that window without us doing anything,
and `107062` should eventually be removed from `data/nondisplay_categories.json`.

The other ~85 dead-category listings (car audio, wheels, emblems, spare-tire kits) have no
sensible Car & Truck Parts equivalent and stay as they are.

## Open items

- [ ] **Backlog:** roughly 9,000+ listings still to sweep, at 2,000/night.
- [ ] **Recover 42 orphaned pushes.** On 2026-08-18 a run pushed 42 listings, then its git
      push was rejected (someone pushed to the branch mid-run) and the record was discarded.
      They carry correct fitment on eBay but are absent from the ledger, so the guard now
      skips them as if hand-curated. SKUs and the one-line recovery command are in
      `data/orphaned_pushes.txt`.
- [x] **~2,781 BMW products had no chassis.** Mostly NOT missing — we were failing to read
      it. The chassis is written under several names (`donor_vehicle.raw_veh_series_F80`,
      the `custom.series` metafield, the vendor field, a bare tag) and we read one of them.
      Fixed 2026-08-21; measured 38/40 recovered on a real sample, ~2,641 projected. Same
      commit fixed `engine_family("raw_S55")` returning `raw_S55` as the family, which had
      Rule B expanding against a phantom engine on 2,071 donors.
- [ ] **Engine data on the older products.** Without an engine code Rule B produces nothing.
      Two routes now: ask Dismantly to backfill `veh_engine_code_`, or decode the donor VIN
      (we started recording it 2026-08-21) against the ETK's `w_fztyp` — see below.
- [ ] **Pull fitment from the ETK catalogue (agreed 2026-08-21).** BMW's own parts catalogue
      as a THIRD fitment source — the first authoritative one. **The database is already
      built and the part→vehicle join works**; `bmw-etk/scripts/ebay_fitment.py` emits
      eBay-ready rows today. Verified on SKU 13611: the donor is an F80, our chassis rule
      emits F80 only, and the ETK says the same airbag also fits F30, F36 and the M2 —
      cross-chassis fitment neither current source can produce. Full plan, warnings and
      conventions in `docs/DESIGN.md` §9. Next step is measuring how many of our listings
      have a part number the ETK knows. Does not gate the nightly sweep.
- [ ] **Shopify store cleanup** — being assessed separately. The code already tolerates
      whatever the vendor field ends up as.

## Parked: extend to Audi (decided 2026-08-19)

**Agreed to do this, but only once BMW is finished.** Not started, nothing built.

**The trigger to pick it back up:** the BMW backlog is swept and the nightly run has
settled into steady state — i.e. it only finds newly-listed parts, not a queue of
thousands. At that point the eBay API budget frees up and there is one *finished*
dataset instead of several half-done ones.

**Why Audi and not Mercedes:**

| Make | Active listings | Verdict |
|---|---|---|
| BMW | 9,698 | in progress |
| **Audi** | **961** (and growing — more Audis coming through the dismantling area lately) | **worth doing after BMW** |
| Mercedes | 105 | not worth it — the same reference-building effort for a ninth of the payoff |
| Porsche | 57 | no |

**What already works for any make (~90% of the system):** eBay auth, the category
classifier, trim-catalog validation, the push logic, the ledger, the audit, the drift
watch, the nightly automation. None of it knows or cares that it is BMW.

**What has to be built per make — this is the whole cost:** three reference datasets
equivalent to `bmw_chassis_reference.json` (112 chassis with models and US year ranges),
`bmw_engine_map.json` (396 engine rows), and `ebay_bmw_models.json` (269 eBay model
names). For Audi that means the 8U / 8R / B8 / B9 / 8V / 4G platform codes.

**Effort, realistically:** the BMW tables took about an hour of research with mendy
confirming as we went. **Audi will take longer** — mendy knows Audi less well, so expect
more verification against sources and fewer quick confirmations. Budget several sessions,
not one.

**The good news:** Dismantly already tags other makes' chassis codes — an Audi Q3 in the
store carries `donor_vehicle.veh_series_8U`. So the input data is already in the right
shape; it is only the reference tables that are missing.

**Re-check the volume when starting.** 961 is today's number and Audi intake is rising,
so it may well be higher by then.

## Things that write fitment besides us

Dismantly, PartOutPro, eBay's own auto-fitment setting, and historically MyFitment all push
fitment to these listings. Two consequences:

- The "already expanded" guard is load-bearing. A listing showing vehicles we never pushed
  belongs to one of them, and overwriting it destroys their work.
- **Our fitment can be overwritten by them.** That is what the nightly drift watch is for.

## How to check on things yourself

```bash
# Did the fitment actually show up? (read-only, safe any time)
python3 scripts/ebay_display_audit.py --quiet

# What's going on with one listing?
python3 scripts/ebay_inspect.py 52566 --token "$(python3 scripts/ebay_auth.py)"

# Is the logic healthy? (offline, 10 seconds)
python3 scripts/selftest.py
```

Nightly results: `github.com/mendy-gif/ClaudeEbayFitmentApp/actions` — open the newest run
and read the summary at the bottom. Two lines tell you most of it: the display rate, and
`LEAKING: none`.

## Working agreement

Live pushes go only to the listings you name. Before any wider batch, the count gets stated
and waits for your yes. Dry runs and read-only audits need no approval.

This exists because on 2026-08-18 one test SKU was approved and 305 got swept. There was no
snapshot, so it could not be undone.
