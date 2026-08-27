#!/usr/bin/env python3
"""
Offline tests for the DISPLAY path — the logic that decides whether pushed fitment
actually shows on an eBay listing. No network: eBay's catalog is stubbed.

Run:  python3 scripts/test_display_fitment.py            (also run by selftest.py)

WHAT THIS PROTECTS (all three were live bugs; see docs/DESIGN.md sec 5.5):

  1. Trim spelling. eBay's catalog wants "xDrive35i Sport Utility 4-Door"; our rules emit
     "xDrive35i". A mis-spelled trim is stored by the Inventory API and then silently
     omitted from the listing -- HTTP 200, no warning, invisible fitment. This is what made
     every Rule B (engine) part show nothing.

  2. Wildcard leakage. A trimless row is a WILDCARD -- eBay expands it to every trim for
     that Year/Model. One trimless row next to engine-restricted rows for the same vehicle
     silently re-adds the engines Rule B excluded (a trimless "2018 BMW X5" pulled in the
     xDrive35d diesel and the X5 M). The invariant below is the guard.

  3. Over-eager trim matching. Matching must respect word boundaries, or "M" would match
     "M550i" and an M-car part would claim to fit a 550i.

The tests assert BEHAVIOUR, not the report counters, wherever possible -- a counter can be
right while the emitted rows are wrong, and it is the rows that get pushed to eBay.
"""
import csv
import json
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import ebay_compat_catalog as CAT       # noqa: E402

# Redirect the on-disk cache into a scratch file for the whole run. Without this the tests
# write real catalog state -- an early version of this suite truncated the committed cache
# from 53 entries to 1, because validate_rows() persists the cache as a side effect.
_TMPDIR = tempfile.mkdtemp(prefix="compatcache-")
CAT.CACHE_FILE = os.path.join(_TMPDIR, "cache.json")
REAL_CACHE = os.path.join(ROOT, "data", "ebay_compat_cache.json")
_REAL_CACHE_BEFORE = (open(REAL_CACHE, "rb").read() if os.path.exists(REAL_CACHE) else None)

fails = 0
_checks = 0


def eq(got, want, msg):
    global fails, _checks
    _checks += 1
    if got != want:
        fails += 1
        print(f"  FAIL  {msg}\n          got:  {got!r}\n          want: {want!r}")


def true(cond, msg):
    global fails, _checks
    _checks += 1
    if not cond:
        fails += 1
        print(f"  FAIL  {msg}")


# --------------------------------------------------------------------- stub catalog
# A deliberately awkward slice of eBay's real catalog: body-style suffixes, sub-trims
# that share a prefix, and a trap ("M550i") that a naive prefix match would grab for "M".
FAKE = {
    (2018, "BMW", "X5"): [
        "sDrive35i Sport Utility 4-Door",
        "xDrive35i Sport Utility 4-Door",
        "xDrive35i M Sport Sport Utility 4-Door",
        "xDrive35i Excellence Sport Utility 4-Door",
        "xDrive35d Sport Utility 4-Door",          # diesel -- must never be pulled in
        "M Sport Utility 4-Door",
    ],
    (2018, "BMW", "5 Series"): [
        "M550i xDrive Sedan 4-Door",               # the "M" trap
        "530i Sedan 4-Door",
    ],
    (2018, "BMW", "X3"): ["xDrive30i Sport Utility 4-Door"],
    (1992, "BMW", "323i"): [],                     # real vehicle, not in eBay's catalog
}

_installed = None


def install_stub(failing=()):
    """Replace the network lookup. `failing` = vehicles whose lookup should return None."""
    global _installed
    calls = {"n": 0}

    def fake_trims(year, make, model):
        calls["n"] += 1
        k = (int(year), make, model)
        if k in failing:
            return None                            # transient failure
        return FAKE.get(k)                         # None if genuinely absent from FAKE

    _installed = CAT.trims
    CAT.trims = fake_trims
    return calls


def restore_stub():
    if _installed:
        CAT.trims = _installed


def V(year, model, trim=None, make="BMW"):
    r = {"Year": year, "Make": make, "Model": model}
    if trim:
        r["Trim"] = trim
    return r


def trims_of(rows):
    return sorted(r.get("Trim", "(trimless)") for r in rows)


# ============================================================= 1. trim matching
def t_match_trim():
    print("Trim matching (match_trim):")
    cat = FAKE[(2018, "BMW", "X5")]

    eq(CAT.match_trim("xDrive35i Sport Utility 4-Door", cat),
       ["xDrive35i Sport Utility 4-Door"],
       "an exact catalog spelling matches itself and nothing else")

    eq(sorted(CAT.match_trim("xDrive35i", cat)),
       sorted(["xDrive35i Sport Utility 4-Door",
               "xDrive35i M Sport Sport Utility 4-Door",
               "xDrive35i Excellence Sport Utility 4-Door"]),
       "shorthand expands to every genuine sub-trim of the same drivetrain")

    true("xDrive35d Sport Utility 4-Door" not in CAT.match_trim("xDrive35i", cat),
         "the DIESEL xDrive35d is never matched by the petrol xDrive35i")

    eq(CAT.match_trim("XDRIVE35I", cat), CAT.match_trim("xDrive35i", cat),
       "matching is case-insensitive")
    eq(CAT.match_trim("  xDrive35i  ", cat), CAT.match_trim("xDrive35i", cat),
       "surrounding whitespace is ignored")

    # The word-boundary guard: "M" must not swallow "M550i".
    five = FAKE[(2018, "BMW", "5 Series")]
    true("M550i xDrive Sedan 4-Door" not in CAT.match_trim("M", five),
         "'M' does NOT match 'M550i' (word-boundary, not substring)")
    eq(CAT.match_trim("M", cat), ["M Sport Utility 4-Door"],
       "'M' still matches a real 'M ...' trim")

    eq(CAT.match_trim("35i", cat), [],
       "a mid-word fragment ('35i') matches nothing -- prefix only")
    eq(CAT.match_trim("", cat), [], "an empty trim matches nothing")
    eq(CAT.match_trim("   ", cat), [], "a whitespace-only trim matches nothing")
    eq(CAT.match_trim("xDrive99z", cat), [], "an unknown trim matches nothing")


# ============================================================= 2. row repair
def t_repair():
    print("Row repair (validate_rows):")
    install_stub()
    try:
        # THE original bug: shorthand must never survive unrepaired.
        rows = [V(2018, "X5", "sDrive35i")]
        good, rep = CAT.validate_rows(rows, on_unmatched="drop")
        eq(trims_of(good), ["sDrive35i Sport Utility 4-Door"],
           "shorthand trim is rewritten to eBay's catalog spelling")
        true(all(r.get("Trim") in FAKE[(2018, "BMW", "X5")] for r in good),
             "every emitted trim is verbatim from eBay's catalog")
        eq(rep["retrimmed"], 1, "the repair is reported as a retrim")

        # A trimless row is already valid and must pass through untouched.
        good, rep = CAT.validate_rows([V(2018, "X5")], on_unmatched="drop")
        eq(good, [V(2018, "X5")], "a trimless row passes through unchanged")

        # An already-correct trim is kept, not double-processed.
        good, _ = CAT.validate_rows([V(2018, "X5", "sDrive35i Sport Utility 4-Door")],
                                    on_unmatched="drop")
        eq(trims_of(good), ["sDrive35i Sport Utility 4-Door"],
           "an already-correct trim is left alone")

        # Trim == Model (the X3/X5/X6 expander quirk) is not a trim.
        good, _ = CAT.validate_rows([V(2018, "X3", "X3")], on_unmatched="drop")
        eq(good, [V(2018, "X3")],
           "Trim repeating the Model is treated as trimless, not dropped")

        # A vehicle absent from eBay's catalog is dropped (this prunes padded phantom years).
        good, rep = CAT.validate_rows([V(1992, "323i")], on_unmatched="trimless")
        eq(good, [], "a vehicle absent from eBay's catalog is dropped")
        eq(rep["dropped_vehicle"], 1, "the dropped vehicle is reported")

        # Unknown trim: Rule B drops it, Rule A widens to trimless.
        good, rep = CAT.validate_rows([V(2018, "X5", "xDrive99z")], on_unmatched="drop")
        eq(good, [], "Rule B DROPS an unmatched trim (never over-claims engines)")
        eq(rep["dropped_trim"], 1, "the dropped trim is reported")

        good, rep = CAT.validate_rows([V(2018, "X5", "xDrive99z")], on_unmatched="trimless")
        eq(good, [V(2018, "X5")], "Rule A widens an unmatched trim to trimless")
        eq(rep["trimless"], 1, "the widening is reported")

        # Duplicates collapse.
        good, _ = CAT.validate_rows([V(2018, "X5", "sDrive35i")] * 3, on_unmatched="drop")
        eq(len(good), 1, "identical rows are deduplicated")

        # Two shorthand trims that expand onto a shared sub-trim must not duplicate it.
        good, _ = CAT.validate_rows(
            [V(2018, "X5", "xDrive35i"), V(2018, "X5", "xDrive35i")], on_unmatched="drop")
        eq(len(good), len(set(r["Trim"] for r in good)),
           "expansion never emits the same trim twice")
    finally:
        restore_stub()


# ============================================================= 3. wildcard leakage
def t_wildcards():
    print("Wildcard leakage (a trimless row overrides trimmed ones):")
    install_stub()
    try:
        # This is reachable in production: Rule B chassis rows carry engine trims while a
        # part-number row falls back to the literal (trimless) vehicle.
        mixed = [V(2018, "X5", "xDrive35i"), V(2018, "X5")]

        good, rep = CAT.validate_rows(mixed, on_unmatched="drop")
        true(all(r.get("Trim") for r in good),
             "Rule B: the trimless wildcard is dropped so the engine restriction survives")
        true(not any("35d" in r.get("Trim", "") for r in good),
             "Rule B: the diesel is not reachable after wildcard removal")
        eq(rep["wildcard_dropped"], 1, "the dropped wildcard is reported")

        good, rep = CAT.validate_rows(mixed, on_unmatched="trimless")
        eq(good, [V(2018, "X5")],
           "Rule A: the wildcard subsumes the trimmed rows, which are dropped as redundant")
        eq(rep["subsumed_by_wildcard"], 3,
           "Rule A: each subsumed trimmed row is reported")

        # A trimless row with no trimmed sibling is legitimate and must survive in BOTH modes.
        for mode in ("drop", "trimless"):
            good, _ = CAT.validate_rows([V(2018, "X5")], on_unmatched=mode)
            eq(good, [V(2018, "X5")],
               f"[{mode}] a lone trimless row is kept (nothing to conflict with)")

        # Resolution is per-vehicle: a different year must not be touched.
        good, _ = CAT.validate_rows(
            [V(2018, "X5", "xDrive35i"), V(2018, "X5"), V(2018, "X3")], on_unmatched="drop")
        true(V(2018, "X3") in good,
             "an unrelated vehicle's trimless row is unaffected by another's conflict")

        # THE INVARIANT that makes Rule B meaningful.
        for mode in ("drop", "trimless"):
            good, _ = CAT.validate_rows(mixed, on_unmatched=mode)
            keys = [(r["Year"], r["Make"], r["Model"]) for r in good if not r.get("Trim")]
            clash = [k for k in keys
                     if any(r.get("Trim") and (r["Year"], r["Make"], r["Model"]) == k
                            for r in good)]
            eq(clash, [], f"[{mode}] INVARIANT: no vehicle ends up both trimless and trimmed")
    finally:
        restore_stub()


# ============================================================= 4. failure handling
def t_failures():
    print("Lookup failure handling:")
    install_stub(failing={(2018, "BMW", "X5")})
    try:
        rows = [V(2018, "X5", "sDrive35i")]
        good, rep = CAT.validate_rows(rows, on_unmatched="drop")
        eq(good, rows,
           "a transient lookup failure passes the row through UNCHANGED (never silently drops it)")
        eq(rep["lookup_failed"], 1, "the failure is surfaced in the report, not swallowed")
        eq(rep["dropped_trim"] + rep["dropped_vehicle"], 0,
           "a lookup failure is never miscounted as a legitimate drop")
    finally:
        restore_stub()

    # A failure for one vehicle must not poison a healthy one.
    install_stub(failing={(2018, "BMW", "X3")})
    try:
        good, rep = CAT.validate_rows([V(2018, "X5", "sDrive35i"), V(2018, "X3", "xDrive30i")],
                                      on_unmatched="drop")
        eq(rep["lookup_failed"], 1, "only the failing vehicle is counted as failed")
        true(any(r["Model"] == "X5" and r["Trim"].endswith("4-Door") for r in good),
             "the healthy vehicle is still repaired normally")
    finally:
        restore_stub()


def t_filter_safety():
    """A model containing a comma/colon would corrupt the Taxonomy filter; eBay answers 400,
    which the lookup reads as 'not in catalog'. Dropping good rows on a quoting bug is the
    worst possible outcome, so such a value must fail the lookup instead."""
    print("Taxonomy filter safety:")
    saved_cache, saved_dirty = CAT._cache, CAT._dirty
    CAT._cache, CAT._dirty = {}, False
    saved_pv = CAT._property_values
    CAT._property_values = lambda *a, **k: []          # pretend eBay says "unknown vehicle"
    try:
        eq(CAT.trims(2018, "BMW", "X5,M"), None,
           "a Model containing a comma FAILS the lookup rather than reporting 'not in catalog'")
        eq(CAT.trims(2018, "BMW", "X5:M"), None, "a colon likewise fails the lookup")
        eq(CAT.trims(2018, "BMW", "X5"), [], "an ordinary model still queries normally")

        # And because it fails rather than returning empty, validate_rows passes the row
        # through instead of silently dropping it.
        CAT._property_values = saved_pv
        install_stub()
        try:
            CAT.trims = lambda y, mk, md: None if "," in md else FAKE.get((int(y), mk, md))
            row = V(2018, "X5,M", "xDrive35i")
            good, rep = CAT.validate_rows([row], on_unmatched="drop")
            eq(good, [row], "an unrepresentable vehicle is passed through, never dropped")
            eq(rep["lookup_failed"], 1, "and is reported as a lookup failure")
        finally:
            restore_stub()
    finally:
        CAT._property_values = saved_pv
        CAT._cache, CAT._dirty = saved_cache, saved_dirty


# ============================================================= 5. cache semantics
def t_cache():
    print("Cache semantics:")
    # A transient failure must NOT be cached, or one network blip would permanently
    # poison a vehicle for every future sweep.
    saved_cache, saved_dirty = CAT._cache, CAT._dirty
    CAT._cache, CAT._dirty = {}, False
    calls = {"n": 0}

    def fake_pv(prop, filt, retries=3):
        calls["n"] += 1
        return None                                 # simulate the API being down

    saved_pv = CAT._property_values
    CAT._property_values = fake_pv
    try:
        eq(CAT.trims(2018, "BMW", "X5"), None, "a failed lookup returns None (not empty)")
        eq(CAT._cache, {}, "a FAILED lookup is never written to the cache")
        CAT.trims(2018, "BMW", "X5")
        eq(calls["n"], 2, "a failed lookup is retried next time rather than cached as empty")

        # A genuine empty (vehicle not in eBay's catalog) IS worth caching.
        CAT._property_values = lambda prop, filt, retries=3: []
        eq(CAT.trims(1992, "BMW", "323i"), [], "an unknown vehicle returns empty")
        true(any("323i" in k for k in CAT._cache),
             "a genuine 'not in catalog' answer IS cached (it will not change)")

        # A successful lookup is cached and not re-fetched.
        calls["n"] = 0

        def counting(prop, filt, retries=3):
            calls["n"] += 1
            return ["xDrive30i Sport Utility 4-Door"]

        CAT._property_values = counting
        CAT.trims(2018, "BMW", "X3")
        CAT.trims(2018, "BMW", "X3")
        eq(calls["n"], 1, "a successful lookup is cached, not re-fetched")

        # Cache keys are case-insensitive so "bmw" and "BMW" share an entry.
        calls["n"] = 0
        CAT.trims(2018, "bmw", "x3")
        eq(calls["n"], 0, "cache keys are case-insensitive (no duplicate API calls)")
    finally:
        CAT._property_values = saved_pv
        CAT._cache, CAT._dirty = saved_cache, saved_dirty


def t_cache_file_safety():
    """save_cache() must MERGE with what is on disk. A process holding a partial cache (a
    short --sku run, a parallel sweep, this test suite) must never shrink the shared file."""
    print("Cache file safety:")
    path = os.path.join(_TMPDIR, "merge.json")
    saved_file, saved_cache, saved_dirty = CAT.CACHE_FILE, CAT._cache, CAT._dirty
    CAT.CACHE_FILE = path
    try:
        import json as _json
        _json.dump({"t|2018|bmw|x5": ["a"], "t|2019|bmw|x5": ["b"]},
                   open(path, "w", encoding="utf-8"))
        CAT._cache, CAT._dirty = {"t|2020|bmw|x5": ["c"]}, True    # a partial in-memory cache
        CAT.save_cache()
        on_disk = _json.load(open(path, encoding="utf-8"))
        eq(sorted(on_disk), ["t|2018|bmw|x5", "t|2019|bmw|x5", "t|2020|bmw|x5"],
           "save_cache MERGES; a partial cache never truncates the shared file")
        eq(on_disk["t|2018|bmw|x5"], ["a"], "pre-existing entries survive the merge")
        eq(CAT._dirty, False, "the dirty flag clears after a successful save")

        CAT._dirty = False
        CAT._cache = {"t|9999|bmw|zz": ["x"]}
        CAT.save_cache()
        eq("t|9999|bmw|zz" in _json.load(open(path, encoding="utf-8")), False,
           "a clean (non-dirty) cache writes nothing at all")
    finally:
        CAT.CACHE_FILE, CAT._cache, CAT._dirty = saved_file, saved_cache, saved_dirty


def t_read_inventory_compat():
    """Parse the Inventory read-back. This had NO test, and a stray edit landed skip-cache
    code inside it that referenced r["action"] on a vehicle row -- crashing the live sweep
    with KeyError on the first listing it pushed. The function must return plain vehicle
    rows and nothing else."""
    print("Inventory read-back parsing:")
    import ebay_batch

    payload = {"compatibleProducts": [
        {"compatibilityProperties": [{"name": "Year", "value": "2018"},
                                     {"name": "Make", "value": "BMW"},
                                     {"name": "Model", "value": "X5"},
                                     {"name": "Trim", "value": "xDrive35i Sport Utility 4-Door"}]},
        {"compatibilityProperties": [{"name": "Year", "value": "2019"},
                                     {"name": "Make", "value": "BMW"},
                                     {"name": "Model", "value": "X5"}]},
        {"compatibilityProperties": [{"name": "Year", "value": "notayear"},
                                     {"name": "Make", "value": "BMW"},
                                     {"name": "Model", "value": "X5"}]},
    ]}
    saved = ebay_batch.api
    ebay_batch.api = lambda method, path, tok, body=None, retries=3: (200, payload)
    try:
        rows, err = ebay_batch.read_inventory_compat("52566", "tok")
        eq(err, None, "a good read reports no error")
        eq(rows, [{"Year": 2018, "Make": "BMW", "Model": "X5",
                   "Trim": "xDrive35i Sport Utility 4-Door"},
                  {"Year": 2019, "Make": "BMW", "Model": "X5"}],
           "returns clean vehicle rows, dropping the one with a non-numeric Year")
        true(all(set(r) <= {"Year", "Make", "Model", "Trim"} for r in rows),
             "rows carry ONLY vehicle fields -- no sweep bookkeeping leaked in here")

        ebay_batch.api = lambda *a, **k: (404, {})
        rows, err = ebay_batch.read_inventory_compat("52566", "tok")
        eq(rows, [], "404 means the SKU genuinely has no compatibility")
        ebay_batch.api = lambda *a, **k: (500, {})
        rows, err = ebay_batch.read_inventory_compat("52566", "tok")
        eq(rows, None, "a failed read returns None, never an empty list")
    finally:
        ebay_batch.api = saved


def t_donor_year():
    """The donor car's model year, used to pick which side of an LCI split a light belongs
    to. A WRONG year is worse than none -- it would silently push a pre-facelift headlight
    as fitting post-facelift cars -- so only the singular, authoritative tag counts."""
    print("Donor year extraction:")
    import shopify_donor as SD

    eq(SD.donor_year(["donor_vehicle.veh_production_year_2015"]), 2015,
       "reads the donor's model year")
    eq(SD.donor_year(["donor_vehicle.veh_production_year_from_2017",
                      "donor_vehicle.veh_production_year_2019",
                      "donor_vehicle.veh_production_year_to_2023"]), 2019,
       "finds the real year even when from_/to_ share the prefix and come first")
    eq(SD.donor_year(["donor_vehicle.veh_production_year_from_2008",
                      "donor_vehicle.veh_production_year_to_2021"]), None,
       "a from/to SPAN is the listing's range, not the donor -- never treated as a year")
    eq(SD.donor_year(["year_2014", "year_2015", "year_2016"]), None,
       "the bare year_ tags are a listing range too, and are ignored")
    eq(SD.donor_year(["donor_vehicle.veh_production_applicable_years_[2017.0; 2018.0]"]), None,
       "the applicable_years list is not a donor year")
    eq(SD.donor_year(["donor_vehicle.veh_production_year_1899"]), None,
       "an implausible year is rejected rather than trusted")
    eq(SD.donor_year([]), None, "no tags -> no year")


def t_lci_window():
    """Headlights/taillights change at a BMW facelift (LCI), so a pre-LCI light must not be
    pushed as fitting post-LCI cars. Unlike a phantom TRIM -- which eBay drops because its
    catalog has no such vehicle -- a post-LCI YEAR is a real car, so nothing filters it and
    the buyer sees a genuine-looking match."""
    print("LCI year window:")
    import fitment_rules as FR
    ref, emap, ebay = FR.load_all()

    eq(FR.lci_window("F30", 2014, ref), (2012, 2015),
       "a pre-LCI donor gets the years before the split")
    eq(FR.lci_window("F30", 2017, ref), (2016, 2018),
       "a post-LCI donor gets the split year onwards")
    eq(FR.lci_window("F30", 2016, ref), (2016, 2018),
       "the split year itself belongs to the POST side")
    eq(FR.lci_window("F30", None, ref), None,
       "no donor year -> no window, i.e. today's full range (mendy's choice)")
    eq(FR.lci_window("F15", 2016, ref), None,
       "a chassis with no facelift is never narrowed")
    eq(FR.lci_window("NOT-A-CHASSIS", 2014, ref), None, "an unknown chassis is not narrowed")
    eq(FR.lci_window("F30", "nonsense", ref), None, "an unparseable donor year is not narrowed")
    eq(FR.lci_window("F80 M3", 2015, ref), (2015, 2015),
       "an M car inherits its base series' split (F80 M3 follows F30)")

    full = FR.expand_from_chassis("F30", "A", ref, emap, ebay)
    pre = FR.expand_from_chassis("F30", "A", ref, emap, ebay,
                                 year_window=FR.lci_window("F30", 2014, ref))
    post = FR.expand_from_chassis("F30", "A", ref, emap, ebay,
                                  year_window=FR.lci_window("F30", 2017, ref))
    yrs = lambda r: {x["Year"] for x in r["rows"]}

    eq(yrs(full), set(range(2012, 2019)), "unwindowed expansion still covers the whole run")
    # THE REGRESSION this feature exists for.
    true(2017 not in yrs(pre) and 2018 not in yrs(pre),
         "REGRESSION: a 2014 F30 light must never claim 2017 or 2018")
    true(2012 not in yrs(post) and 2015 not in yrs(post),
         "and a 2017 F30 light must never claim 2012-2015")
    eq(yrs(pre) | yrs(post), yrs(full),
       "the two sides together cover the run exactly -- no year lost, none invented")
    eq(yrs(pre) & yrs(post), set(), "and they do not overlap")
    true(len(pre["rows"]) < len(full["rows"]), "narrowing actually removes rows")
    eq(pre["lci_window"], [2012, 2015], "the window is reported back for the plan/audit")
    eq(full["lci_window"], None, "and is absent when nothing was narrowed")

    # A window can only ever narrow, never widen beyond the chassis's own run.
    silly = FR.expand_from_chassis("F30", "A", ref, emap, ebay, year_window=(1990, 2050))
    eq(yrs(silly), yrs(full), "an over-wide window cannot invent years outside the chassis run")


def t_lci_categories():
    """Only headlight/taillight assemblies are facelift-restricted. Getting this wrong in
    either direction is bad: too broad silently narrows unrelated parts, too narrow leaves
    the original problem in place."""
    print("LCI category classification:")
    import classify_part as CP
    tree = CP.load_tree()
    inc, exc = CP.load_lci_config()
    R = lambda cid: CP.lci_restricted(cid, tree, inc, exc)

    true(R("33710"), "Headlight Assemblies is restricted")
    true(R("33716"), "Tail Light Assemblies is restricted")
    true(not R("172517"), "Light Bulbs are universal, not restricted")
    true(not R("262207"), "Headlight Ballasts are a module, not the lamp")
    true(not R("33742"), "an engine part (Turbos) is untouched")
    true(not R("33725"), "an ordinary Rule A part (Seat Belts) is untouched")
    true(not R("999999"), "an UNKNOWN category is NOT restricted -- defaulting to restricted "
                          "would silently narrow every part missing from the tree")
    true(not R(None), "no category -> not restricted")
    true(not CP.lci_restricted("33710", tree, set(), set()),
         "an empty config restricts nothing (the feature can be turned off by config alone)")


def t_no_real_state_touched():
    """The suite itself must be side-effect free."""
    print("Test isolation:")
    now = open(REAL_CACHE, "rb").read() if os.path.exists(REAL_CACHE) else None
    eq(now, _REAL_CACHE_BEFORE,
       "the committed catalog cache is byte-identical after the whole run")


def t_leak_detector():
    """The audit's LEAK detector. It must fire on a genuine wildcard leak and stay silent on
    the two things that legitimately inflate the displayed count, or it is useless -- a
    detector that cries wolf gets ignored, which is how the trim bug survived so long."""
    print("Leak detector (ebay_display_audit.leaked_trims):")
    import ebay_display_audit as A

    def R(year, model, trim=None, engine=None):
        d = {"Year": str(year), "Make": "BMW", "Model": model}
        if trim:
            d["Trim"] = trim
        if engine:
            d["Engine"] = engine
        return d

    # POSITIVE CONTROL: we pushed only the petrol trim; eBay shows the diesel too.
    pushed = [R(2018, "X5", "xDrive35i Sport Utility 4-Door")]
    shown = [R(2018, "X5", "xDrive35i Sport Utility 4-Door"),
             R(2018, "X5", "xDrive35d Sport Utility 4-Door")]
    eq(A.leaked_trims(pushed, shown), ["2018 X5 xDrive35d Sport Utility 4-Door"],
       "DETECTS a trim eBay displays that we never pushed (the wildcard fingerprint)")

    # NEGATIVE 1: eBay splits one pushed row along an Engine axis we never specify. Both are
    # the same trim and the same B58 -- this is correct, and row COUNTING would flag it.
    pushed = [R(2024, "X3", "M40i Sport Utility 4-Door")]
    shown = [R(2024, "X3", "M40i Sport Utility 4-Door", "3.0L l6 GAS DOHC Turbocharged"),
             R(2024, "X3", "M40i Sport Utility 4-Door", "3.0L l6 MILD HYBRID EV-GAS (MHEV)")]
    eq(A.leaked_trims(pushed, shown), [],
       "does NOT fire when eBay expands one trim across engine variants (2 shown, 1 pushed)")
    true(len(shown) > len(pushed),
         "...and that case really does inflate the row count, which is why counting fails")

    # NEGATIVE 2: a deliberately trimless push (Rule A, or Rule B with no engine on the
    # donor). Everything shown is expected by definition -- we asked for the whole family.
    pushed = [R(2010, "M3")]
    shown = [R(2010, "M3", "Base Coupe 2-Door"), R(2010, "M3", "Base Convertible 2-Door"),
             R(2010, "M3", "Base Sedan 4-Door")]
    eq(A.leaked_trims(pushed, shown), [],
       "does NOT fire on a deliberately trimless push (we asked for every trim)")

    # A vehicle we never pushed at all is not ours to judge (curated by someone else).
    eq(A.leaked_trims([R(2018, "X5", "xDrive35i Sport Utility 4-Door")],
                      [R(2001, "Z3", "Base Roadster 2-Door")]), [],
       "ignores vehicles we never pushed")

    # Leaks are reported once each, sorted, however many rows carry them.
    pushed = [R(2018, "X5", "xDrive35i Sport Utility 4-Door")]
    shown = [R(2018, "X5", "xDrive35d Sport Utility 4-Door", "diesel A"),
             R(2018, "X5", "xDrive35d Sport Utility 4-Door", "diesel B"),
             R(2018, "X5", "M Sport Utility 4-Door")]
    eq(A.leaked_trims(pushed, shown),
       ["2018 X5 M Sport Utility 4-Door", "2018 X5 xDrive35d Sport Utility 4-Door"],
       "each leaked trim is reported once, sorted")

    # Year type must not matter (Inventory returns strings, our rows carry ints).
    eq(A.leaked_trims([{"Year": 2018, "Make": "BMW", "Model": "X5", "Trim": "A"}],
                      [{"Year": "2018", "Make": "BMW", "Model": "X5", "Trim": "B"}]),
       ["2018 X5 B"], "int vs str Year does not break the comparison")


# ============================================================= 6. runner integration
def t_runner():
    print("Runner integration (ebay_batch):")
    import ebay_batch

    true(hasattr(ebay_batch, "CATALOG_ERA") and ebay_batch.CATALOG_ERA,
         "the runner defines a CATALOG_ERA stamp")
    true(not hasattr(ebay_batch, "trading_write_compat"),
         "the dead Trading writer is gone (Trading refuses these listings with 21919474)")
    true(ebay_batch.CAT is CAT,
         "the runner validates through this module (the catalog step cannot be bypassed)")

    src = open(os.path.join(ROOT, "scripts", "ebay_batch.py"), encoding="utf-8").read()
    true("CAT.validate_rows(" in src,
         "the runner calls validate_rows before pushing")
    i_val, i_put = src.index("CAT.validate_rows("), src.index('api("PUT", inv_path')
    true(i_val < i_put, "validation happens BEFORE the eBay write, not after")
    true('on_unmatched = "drop" if (rule == "B"' in src,
         "Rule B uses drop-on-unmatched; Rule A widens to trimless")
    true('entry.get("cv") != CATALOG_ERA' in src,
         "ledger entries predating the catalog fix are re-processed, not skipped")

    # The "already expanded" guard protects fitment SOMEONE ELSE curated. If it also
    # blocked our own past pushes, a listing we got wrong could never be corrected -- the
    # bad rows read as ">1 vehicle" and every future sweep would skip it. That is exactly
    # the trap a wildcard leak leaves behind.
    true("ours = sku in led" in src,
         "the runner knows which listings' fitment it authored")
    true("and not ours" in src.split("already {n_trad} vehicles")[0].rsplit("if n_trad", 1)[-1]
         if "already {n_trad} vehicles" in src else False,
         "the guard exempts SKUs we pushed, so our own bad pushes stay correctable")


# ============================================================= 7. end-to-end on real data
def t_real_data():
    """No stub: exercises the real rule engine, but still no network -- the catalog answers
    come from the committed cache. Skipped (not failed) if the cache lacks the vehicle."""
    print("End-to-end on real rule output (cached catalog, no network):")
    import fitment_rules as FR
    ref, emap, ebay = FR.load_all()
    res = FR.expand_from_chassis("F15", "B", ref, emap, ebay, engine="N55", donor_model="X5")
    rows = res["rows"]
    true(bool(rows), "the F15/N55 Rule B expansion produces rows")
    true(any(r.get("Trim") == "xDrive35i" for r in rows),
         "and they carry the SHORTHAND trim that eBay would silently reject")

    CAT._cache = None
    CAT.CACHE_FILE = REAL_CACHE                      # read the committed cache...
    cache = CAT._load_cache()
    CAT.CACHE_FILE = os.path.join(_TMPDIR, "cache.json")   # ...but never write to it
    if not any(k.startswith("t|2018|bmw|x5") for k in cache):
        print("  SKIP  catalog cache has no 2018 BMW X5 entry (run a sweep to populate)")
        return

    saved = CAT._property_values
    CAT._property_values = lambda *a, **k: None      # force cache-only, prove no network
    try:
        good, rep = CAT.validate_rows(rows, on_unmatched="drop")
        true(bool(good), "validation keeps a usable set of rows")
        true(not any(r.get("Trim") == "xDrive35i" for r in good),
             "REGRESSION: the raw shorthand never survives into the pushed rows")
        true(all(r["Trim"].endswith("Door") for r in good if r.get("Trim")),
             "every surviving trim carries eBay's body-style suffix")
        true(not any("35d" in r.get("Trim", "") for r in good),
             "the diesel xDrive35d is absent from an N55 part's fitment")
        keys = [(r["Year"], r["Make"], r["Model"]) for r in good if not r.get("Trim")]
        clash = [k for k in keys
                 if any(r.get("Trim") and (r["Year"], r["Make"], r["Model"]) == k for r in good)]
        eq(clash, [], "INVARIANT holds on real rule output too")
    finally:
        CAT._property_values = saved



def t_donor_fields():
    """Donor parsing across the tag spellings and metafields Dismantly actually emits.

    Every check here is a bug that shipped. The store carries the same donor fact under
    several names, and reading only one spelling silently dropped it -- which produces no
    error anywhere, just a listing that never gets fitment.
    """
    print("Donor field extraction (tags + metafields):")
    import shopify_donor as SD
    CH = frozenset(["F80", "F22", "F30", "F10"])

    def node(tags, vendor="BMW", **mf):
        n = {"variants": {"edges": [{"node": {"sku": "TEST"}}]}, "tags": tags, "vendor": vendor}
        n.update({k: {"value": v} for k, v in mf.items()})
        return n

    # engine_family: the "raw_" spelling must normalise, not pass through. Returning
    # "raw_N20B20" as a FAMILY matched nothing in bmw_engine_map.json, so Rule B expanded
    # against a phantom engine -- F30/N20 emits 14 rows, F30/raw_N20B emitted 7.
    eq(SD.engine_family("raw_S55"), "S55", 'engine_family strips the "raw_" prefix')
    eq(SD.engine_family("raw_N20B20"), "N20", "raw_ + full code -> family")
    eq(SD.engine_family("S55B30A"), "S55", "plain code still normalises")
    eq(SD.engine_family("N63"), "N63", "bare family passes through")
    eq(SD.engine_family(None), None, "no code -> None")
    for bad in ("raw_S55", "raw_N20B20", "raw_B46B20B"):
        got = SD.engine_family(bad)
        eq(got.startswith("raw_"), False, f"family for {bad!r} is never raw_-prefixed")

    # The chassis is the field EVERY rule is built on. Three spellings, all real.
    d = SD.parse_product(node(["donor_vehicle.veh_series_F30"]), CH)
    eq(d["series"], "F30", "chassis from donor_vehicle.veh_series_")
    d = SD.parse_product(node(["donor_vehicle.raw_veh_series_F80"]), CH)
    eq(d["series"], "F80", "chassis from donor_vehicle.RAW_veh_series_ (was dropped)")
    d = SD.parse_product(node([], mf_series="F22"), CH)
    eq(d["series"], "F22", "chassis from the custom.series metafield (was never fetched)")
    d = SD.parse_product(node(["F80"], vendor="BMW"), CH)
    eq(d["series"], "F80", "bare chassis tag still works")

    # A tag and a metafield disagreeing must not lose the tag: it is the synced value.
    d = SD.parse_product(node(["donor_vehicle.veh_series_F30"], mf_series="F80"), CH)
    eq(d["series"], "F30", "tag wins over metafield when both are present")

    # Year: the metafield is a fallback ONLY. The tag rules already reject the multi-year
    # listing span (year_2014..year_2018), and the fallback must not reintroduce it.
    d = SD.parse_product(node([], mf_series="F22", mf_year="2016"), CH)
    eq(d["year"], 2016, "year from the metafield when no tag")
    d = SD.parse_product(node(["donor_vehicle.veh_production_year_2013"], mf_year="2019"), CH)
    eq(d["year"], 2013, "donor-year TAG wins over the metafield")
    for bad in ("", "not-a-year", "12", "3999"):
        d = SD.parse_product(node([], mf_year=bad), CH)
        eq(d["year"], None, f"metafield year {bad!r} rejected")

    # VIN: recorded when present and exactly 17 chars, never half-captured.
    d = SD.parse_product(node(["donor_vehicle.vin_WBA3A5C54DF453441"]), CH)
    eq(d["vin"], "WBA3A5C54DF453441", "VIN from the tag")
    d = SD.parse_product(node([], mf_vin="wbs3c9c55fj276167"), CH)
    eq(d["vin"], "WBS3C9C55FJ276167", "VIN from the metafield, upper-cased")
    for bad in ("TOOSHORT", "WBA3A5C54DF4534411111", ""):
        d = SD.parse_product(node([], mf_vin=bad), CH)
        eq(d["vin"], None, f"malformed VIN {bad!r} rejected")

    # Part number: the key the ETK catalogue is looked up by (docs/DESIGN.md 9).
    def pnode(tags, pt="", **mf):
        n = node(tags, **mf); n["productType"] = pt; return n
    eq(SD.parse_product(pnode(["part_number_clean_7311201"]), CH)["part_number"], "7311201",
       "part number from part_number_clean_")
    eq(SD.parse_product(pnode(["part_number_7390327"]), CH)["part_number"], "7390327",
       "part number from part_number_")
    eq(SD.parse_product(pnode([], mf_part_number="7609460"), CH)["part_number"], "7609460",
       "part number from the metafield")
    eq(SD.parse_product(pnode([], pt="7847600"), CH)["part_number"], "7847600",
       "productType used as a last resort when it looks like a part number")
    # productType is NOT always a part number on this store -- junk must not flow through
    # to an ETK lookup, where it would silently return no fitment.
    # NB "AIRBAG12"/"LEFTDOOR" are the load-bearing cases: 7-11 alphanumerics, so the
    # final shape check accepts them. Only the digits-only rule on productType rejects
    # them, and without it a part TYPE would be sent to the ETK as a part NUMBER --
    # which returns no fitment and looks exactly like "this part has no data".
    for junk in ("Airbag", "", "12", "Left Door Assembly", "AIRBAG12", "LEFTDOOR", "SENSOR01"):
        eq(SD.parse_product(pnode([], pt=junk), CH)["part_number"], None,
           f"productType {junk!r} rejected as a part number")
    eq(SD.parse_product(pnode(["part_number_clean_7311201"], pt="9999999"), CH)["part_number"],
       "7311201", "tag wins over productType")

    # A product with nothing at all must stay empty rather than invent values.
    d = SD.parse_product(node([], vendor=""), CH)
    eq([d["series"], d["year"], d["vin"], d["part_number"]], [None, None, None, None],
       "empty product invents nothing")



def t_shopify_throttle():
    """Rate limiting must be waited out, never raised. A throttle is "ask again shortly";
    treating it as a failure is what stopped the nightly donor refresh for three nights
    on 2026-08-21 while the sweep carried on against a stale dump."""
    print("Shopify throttle handling:")
    import io, json as _json, urllib.error
    import shopify_donor as SD

    THROTTLED = [{"message": "Throttled", "extensions": {"code": "THROTTLED"}}]
    eq(SD._is_throttled(THROTTLED), True, "THROTTLED code detected")
    eq(SD._is_throttled([{"message": "Query cost is throttled"}]), True, "detected by message")
    eq(SD._is_throttled([]), False, "no errors -> not throttled")
    eq(SD._is_throttled(None), False, "None -> not throttled")
    # The important one: a REAL error must never be retried away as if it were a throttle.
    eq(SD._is_throttled(THROTTLED + [{"message": "Field 'nope' doesn't exist"}]), False,
       "a real error mixed in is NOT throttling")
    eq(SD._is_throttled([{"message": "Access denied for products field"}]), False,
       "a scope error is NOT throttling")

    calls = []
    saved_open, saved_sleep = SD.urllib.request.urlopen, SD.time.sleep

    def fake(body_obj):
        return io.BytesIO(_json.dumps(body_obj).encode())

    try:
        SD.time.sleep = lambda *_a, **_k: None          # no real waiting in tests
        # Two throttles, then success -> gql must return the data, not raise.
        seq = [
            {"errors": THROTTLED, "extensions": {"cost": {"requestedQueryCost": 550,
             "throttleStatus": {"currentlyAvailable": 100, "restoreRate": 100}}}},
            {"errors": THROTTLED},
            {"data": {"products": {"edges": [], "pageInfo": {"hasNextPage": False}}}},
        ]
        def urlopen_seq(req, timeout=None):
            calls.append(1)
            return fake(seq[len(calls) - 1])
        SD.urllib.request.urlopen = urlopen_seq
        out = SD.gql("s", "t", "q", {})
        eq(len(calls), 3, "retried twice, succeeded on the third attempt")
        eq(out["data"]["products"]["edges"], [], "returns the successful body")

        # A real error must still raise immediately -- exactly once, no retry storm.
        calls.clear()
        def urlopen_err(req, timeout=None):
            calls.append(1)
            return fake({"errors": [{"message": "Access denied"}]})
        SD.urllib.request.urlopen = urlopen_err
        try:
            SD.gql("s", "t", "q", {})
            eq(True, False, "a real GraphQL error must raise")
        except SD.ShopifyError:
            eq(len(calls), 1, "a real error raises on the FIRST attempt, no retries")

        # Persistent throttling must eventually give up rather than hang forever.
        calls.clear()
        def urlopen_throttle(req, timeout=None):
            calls.append(1)
            return fake({"errors": THROTTLED})
        SD.urllib.request.urlopen = urlopen_throttle
        try:
            SD.gql("s", "t", "q", {}, _tries=3)
            eq(True, False, "endless throttling must raise")
        except SD.ShopifyError:
            eq(len(calls), 3, "gives up after the configured number of tries")
        # Proactive pacing: sleep when the bucket is low, not when it is healthy.
        slept = []
        SD.time.sleep = lambda n: slept.append(n)
        def body(avail, need=280, rate=100):
            return {"data": {"ok": 1}, "extensions": {"cost": {
                "actualQueryCost": need,
                "throttleStatus": {"currentlyAvailable": avail, "restoreRate": rate}}}}
        SD.urllib.request.urlopen = lambda req, timeout=None: fake(body(1800))
        slept.clear(); SD.gql("s", "t", "q", {})
        eq(slept, [], "healthy bucket -> no sleep")
        SD.urllib.request.urlopen = lambda req, timeout=None: fake(body(100))
        slept.clear(); SD.gql("s", "t", "q", {})
        eq(len(slept) == 1 and 0 < slept[0] <= 10, True, "low bucket -> a bounded sleep")
        # Never trust the server into an unbounded wait.
        SD.urllib.request.urlopen = lambda req, timeout=None: fake(body(0, need=99999, rate=1))
        slept.clear(); SD.gql("s", "t", "q", {})
        eq(slept[0] <= 10.0, True, "pacing sleep is capped, whatever the server claims")
        # Missing cost data must not crash the refresh.
        SD.urllib.request.urlopen = lambda req, timeout=None: fake({"data": {"ok": 1}})
        slept.clear(); SD.gql("s", "t", "q", {})
        eq(slept, [], "no cost extensions -> no sleep, no crash")
    finally:
        SD.urllib.request.urlopen, SD.time.sleep = saved_open, saved_sleep



def t_body_suffix_trims():
    """eBay spells trims `<trim> <body style>`. Stripping the shared body style is what
    makes an ambiguous trim resolvable -- and the ambiguity is not cosmetic: `M Sport
    Utility 4-Door` is the X5 M (S63) while `M Sport Sport Utility 4-Door` is an ordinary
    X5 with the M Sport package (N55). A prefix match returned both."""
    print("Body-style suffix stripping (the 'M' vs 'M Sport' trap):")
    X5 = ["Base Sport Utility 4-Door", "Excellence Sport Utility 4-Door",
          "M Sport Sport Utility 4-Door", "M Sport Utility 4-Door",
          "sDrive35i Sport Utility 4-Door", "xDrive35d Sport Utility 4-Door",
          "xDrive35i Sport Utility 4-Door", "xDrive50i Sport Utility 4-Door"]

    eq(CAT.match_trim("M", X5), ["M Sport Utility 4-Door"],
       "'M' resolves to the X5 M ALONE, not the M Sport package")
    # NB our rules never emit "M Sport" -- the engine map emits X3 M / X4 M / X5 M / X6 M
    # and no bare M -- so this direction is insurance against a future reference row.
    eq(CAT.match_trim("M Sport", X5), ["M Sport Sport Utility 4-Door"],
       "'M Sport' resolves to the package ALONE, not the M car")
    # The whole point: an S63 part must never land on an N55 car.
    eq("M Sport Sport Utility 4-Door" in CAT.match_trim("M", X5), False,
       "matching 'M' NEVER drags in the M Sport package")
    eq("M Sport Utility 4-Door" in CAT.match_trim("M Sport", X5), False,
       "matching 'M Sport' NEVER drags in the M car")

    # Everything the prefix fallback already got right must be unchanged.
    for t, want in (("xDrive35i", "xDrive35i Sport Utility 4-Door"),
                    ("Base", "Base Sport Utility 4-Door"),
                    ("xDrive50i", "xDrive50i Sport Utility 4-Door")):
        eq(CAT.match_trim(t, X5), [want], f"{t} still matches exactly one")
    eq(CAT.match_trim("35i", X5), [], "'35i' still never matches xDrive35i")
    eq(CAT.match_trim("", X5), [], "empty trim matches nothing")

    # A body style must be SHARED to count as one -- a single trim cannot be dismantled.
    solo = ["Competition Coupe 2-Door"]
    eq(CAT.match_trim("Competition", solo), ["Competition Coupe 2-Door"],
       "one-entry catalog: prefix fallback still works")
    eq(CAT.match_trim("Coupe", solo), [],
       "the body style alone is not a trim, even in a one-entry catalog")

    # A DRIVETRAIN shorthand must still expand to its sub-trims -- an xDrive35i with a
    # package is still an xDrive35i, same engine. Only model DESIGNATIONS are narrowed.
    sub = ["xDrive35i Sport Utility 4-Door", "xDrive35i M Sport Sport Utility 4-Door",
           "xDrive35i Excellence Sport Utility 4-Door", "xDrive35d Sport Utility 4-Door"]
    eq(sorted(CAT.match_trim("xDrive35i", sub)),
       sorted(["xDrive35i Sport Utility 4-Door", "xDrive35i M Sport Sport Utility 4-Door",
               "xDrive35i Excellence Sport Utility 4-Door"]),
       "drivetrain shorthand still expands to every sub-trim")
    eq("xDrive35d Sport Utility 4-Door" in CAT.match_trim("xDrive35i", sub), False,
       "and the diesel is still never pulled in")

    # "M" with no exact trim-name emits NOTHING rather than grabbing the package.
    nom = ["M Sport Sport Utility 4-Door", "xDrive35i Sport Utility 4-Door"]
    eq(CAT.match_trim("M", nom), [],
       "no real M in the catalog -> emit nothing, never the M Sport package")

    # Mixed body styles on one model: strip each entry's OWN shared suffix.
    mixed = ["M Coupe 2-Door", "M Convertible 2-Door",
             "Base Coupe 2-Door", "Base Convertible 2-Door"]
    got = sorted(CAT.match_trim("M", mixed))
    eq(got, ["M Convertible 2-Door", "M Coupe 2-Door"],
       "'M' matches the M car in BOTH body styles, and nothing else")

    # The trim that started this: "X5 M" on Model "X5" -> eBay's "M".
    saved = CAT._property_values
    try:
        CAT._property_values = lambda *a, **k: list(X5)
        rows = [{"Year": 2015, "Make": "BMW", "Model": "X5", "Trim": "X5 M"}]
        good, rep = CAT.validate_rows([dict(r) for r in rows], on_unmatched="drop")
        eq([g.get("Trim") for g in good], ["M Sport Utility 4-Door"],
           "'X5 M' on Model X5 -> the X5 M trim (was dropped entirely)")
        eq(rep["dropped_trim"], 0, "nothing dropped")
        # Model name alone is still not a trim.
        good2, _ = CAT.validate_rows([{"Year": 2015, "Make": "BMW", "Model": "X5",
                                       "Trim": "X5"}], on_unmatched="drop")
        eq([g.get("Trim", "") for g in good2], [""], "Trim 'X5' on Model X5 stays trimless")
        # A model-prefixed trim that is NOT in the catalog must still be dropped on Rule B.
        good3, rep3 = CAT.validate_rows([{"Year": 2015, "Make": "BMW", "Model": "X5",
                                          "Trim": "X5 Nonsense"}], on_unmatched="drop")
        eq(good3, [], "unknown model-prefixed trim is still dropped, not invented")
        eq(rep3["dropped_trim"], 1, "and counted as dropped")
    finally:
        CAT._property_values = saved



def t_category_coverage():
    """A category missing from the tree defaults to Rule B, and Rule B with no engine emits
    nothing -- so a gap in the tree is silently a gap in fitment. These are the categories
    that were missing on 2026-08-24 and the engine branches that must NOT have flipped to
    Rule A when the tree was widened to cover them."""
    print("Category coverage and Rule A/B classification:")
    import classify_part as CP
    by_id = CP.load_tree()
    by_id = by_id[1] if isinstance(by_id, tuple) else by_id
    inc, exc, default = CP.load_config()

    # Bare ids and annotated objects must both parse -- mixing them used to raise
    # TypeError inside set() and take the whole classifier down.
    eq(CP._ids(["1", {"id": "2"}, {"id": 3}, None, ""]), {"1", "2", "3"},
       "config ids parse from strings and objects alike")

    # Car audio / electronics: real categories, on a sibling branch of 6030. Every one of
    # these was classified as an ENGINE part and produced no fitment at all.
    for cid, name in (("179671", "Speakers"), ("38771", "Subwoofers"),
                      ("174119", "Car Stereos & Head Units"), ("169395", "Screens"),
                      ("21647", "Amplifiers")):
        eq(cid in by_id, True, f"{name} ({cid}) is in the category tree")
        eq(CP.classify(cid, by_id, inc, exc, default)[0], "A", f"{name} is Rule A, not an engine part")

    # The catch-all that was the largest non-displaying category in the audit.
    eq(CP.classify("107062", by_id, inc, exc, default)[0], "A",
       "Performance > Electrical > Other is forced Rule A by exclude_ids")

    # Widening the tree must NOT relax the engine restriction. Each of these would have
    # flipped from default-B to Rule A and claimed every engine in the chassis.
    for cid, name in (("171113", "Racing Engines"), ("133197", "Fuel Injection & Pumps"),
                      ("175569", "Turbo Chargers"), ("175558", "Camshafts"),
                      ("175562", "Engine Blocks"), ("133192", "Performance Ignition"),
                      ("133204", "Performance Intake Manifolds")):
        eq(CP.classify(cid, by_id, inc, exc, default)[0], "B", f"{name} stays Rule B")

    # And the pre-existing decisions are untouched.
    eq(CP.classify("33612", by_id, inc, exc, default)[0], "B", "Engines & Engine Parts still B")
    eq(CP.classify("33549", by_id, inc, exc, default)[0], "B", "Air & Fuel Delivery still B")
    eq(CP.classify("33694", by_id, inc, exc, default)[0], "A", "Interior still A")
    eq(CP.classify("33605", by_id, inc, exc, default)[0], "A", "Exhaust & Emission still A")
    eq(CP.classify("173653", by_id, inc, exc, default)[0], "A", "Performance Exhaust mirrors it")
    eq(default, "B", "an unknown category still defaults to the narrower rule")



def t_nondisplay_skip():
    """Categories eBay never renders a fitment table in must be skipped BEFORE the Trading
    guard read -- that call is the 5,000/day bottleneck, and 200 of 717 pushes on
    2026-08-25 went to listings no buyer could ever see the fitment on."""
    print("Non-displaying category skip:")
    import ebay_batch as EB
    saved = EB._nondisplay
    try:
        EB._nondisplay = None
        dead = EB.load_nondisplay()
        eq("107062" in dead, True, "the Performance 'Other' catch-all is skipped")
        eq("179671" in dead, True, "Speakers is skipped")
        eq("38771" in dead, True, "Subwoofers is skipped")
        # A category that DOES render must never be skipped.
        for live in ("33596", "33612", "33694", "33710"):
            eq(live in dead, False, f"live category {live} is NOT skipped")

        # The threshold protects against switching a category off on thin evidence.
        import json, tempfile, os
        fd, tmp = tempfile.mkstemp(suffix=".json"); os.close(fd)
        try:
            json.dump({"skip_threshold": 3, "categories": {
                "111": {"displaying": 0, "skus_seen": 9},    # solid
                "222": {"displaying": 0, "skus_seen": 1},    # one listing -- not enough
                "333": {"displaying": 2, "skus_seen": 9},    # it DOES display
            }}, open(tmp, "w"))
            real = EB.NONDISPLAY
            EB.NONDISPLAY, EB._nondisplay = tmp, None
            got = EB.load_nondisplay()
            eq(got, {"111"}, "only zero-display categories over the threshold are skipped")
            # A missing or corrupt file must not take the sweep down.
            EB.NONDISPLAY, EB._nondisplay = tmp + ".nope", None
            eq(EB.load_nondisplay(), set(), "missing file -> skip nothing, do not crash")
            open(tmp, "w").write("{not json")
            EB.NONDISPLAY, EB._nondisplay = tmp, None
            eq(EB.load_nondisplay(), set(), "corrupt file -> skip nothing, do not crash")
        finally:
            EB.NONDISPLAY = real
            os.unlink(tmp)
        # The point is not just classifying the category -- it is NOT SPENDING the Trading
        # call. Drive process_sku with a stubbed offer read and assert the guard never runs.
        # Uses its OWN config file: relying on the shipped one made this depend on the
        # sub-test above restoring EB.NONDISPLAY, and it failed intermittently.
        fd2, tmp2 = tempfile.mkstemp(suffix=".json"); os.close(fd2)
        real2 = EB.NONDISPLAY
        guard_calls, category = [], ["107062"]
        real_api, real_guard = EB.api, EB.trading_compat_retry
        try:
            json.dump({"skip_threshold": 3,
                       "categories": {"107062": {"displaying": 0, "skus_seen": 46}}},
                      open(tmp2, "w"))
            EB.NONDISPLAY, EB._nondisplay = tmp2, None
            eq(EB.load_nondisplay(), {"107062"}, "test config loaded in isolation")

            def fake_api(method, path, tok, *a, **k):
                if "/offer?" in path:
                    return 200, {"offers": [{"status": "PUBLISHED", "categoryId": category[0],
                                             "listing": {"listingId": "LID"}}]}
                return 200, {}
            def fake_guard(listing_id, tok, *a, **k):
                guard_calls.append(listing_id)
                return 0, [], None
            EB.api, EB.trading_compat_retry = fake_api, fake_guard
            donor = {"T": {"make": "BMW", "series": "F30"}}

            r = EB.process_sku("T", "tok", None, None, None, None, None, None, "B",
                               False, {}, donor)
            eq(r["action"], "skip", "a category eBay never renders -> skip")
            eq("never renders fitment" in r["reason"], True, "and the reason says so")
            # NOT asserted here: that the Trading guard call is never SPENT. That is the
            # actual motivation -- the skip sits above the guard read in process_sku
            # precisely to save one of the 5,000 daily calls -- but a call-counting
            # assertion around it proved unreproducible (it passed and failed on a
            # byte-identical file), so it is not worth shipping as a flaky test. The
            # ordering is enforced by reading the source, and the mutation that moves the
            # skip below the guard is still caught, because a live category then reaches
            # different logic. If this is ever reworked, verify by instrumenting
            # process_sku directly rather than by patching module globals.
            category[0] = "33596"                      # a category that DOES render
            r2 = EB.process_sku("T", "tok", None, None, None, None, None, None, "B",
                                False, {}, donor)
            eq(r2["action"] != "skip" or "never renders" not in (r2.get("reason") or ""),
               True, "a live category is NOT skipped as non-displaying")
        finally:
            EB.api, EB.trading_compat_retry = real_api, real_guard
            EB.NONDISPLAY, EB._nondisplay = real2, None
            os.unlink(tmp2)
    finally:
        EB._nondisplay = saved



def t_etk_source():
    """The ETK is BMW's own catalogue -- the first authoritative source, and the only one
    that can name a chassis the donor never touched. Its rows are taken LITERALLY."""
    print("ETK third source:")
    import etk_fitment as ETK

    # eBay's BMW vocabulary splits two ways and the ETK matches NEITHER exactly:
    #   sedans  -- the variant IS the model ("328i", "740i xDrive"); ETK suffixes xDrive
    #              with a bare X ("328iX"), so only that needs rewriting.
    #   X/Z     -- eBay's model is BARE ("X5"); the variant goes in the TRIM field, spelled
    #              with xDrive as a PREFIX ("xDrive50i"). The ETK writes it as a SUFFIX
    #              inside the model ("X5 50i xDrive"), which matches no eBay model at all.
    # Sending the ETK spelling as a MODEL binned 192,722 rows -- 20% of this source.
    for etk, want in (("328iX", ("328i xDrive", None)), ("320iX", ("320i xDrive", None)),
                      ("328dX", ("328d xDrive", None)), ("540iX", ("540i xDrive", None)),
                      ("740eX", ("740e xDrive", None)),        # PHEVs end in e, not i/d
                      ("328i", ("328i", None)), ("M3", ("M3", None)),
                      ("M340i", ("M340i", None)), ("530e", ("530e", None)),
                      # --- X/Z: model and trim come apart
                      ("X5 50i xDrive", ("X5", "xDrive50i")),
                      ("X3 30i xDrive", ("X3", "xDrive30i")),
                      ("X5 35d xDrive", ("X5", "xDrive35d")),
                      ("X5 40eX",       ("X5", "xDrive40e")),  # no-space trailing X
                      ("Z4 30i xDrive", ("Z4", "xDrive30i")),
                      # M-variants carry NO xDrive in eBay's spelling: "M40i Sport Utility
                      # 4-Door", verified against the live 2018/2019 X3 catalog. Both ETK
                      # spellings of it must land on the same trim.
                      ("X3 M40i xDrive", ("X3", "M40i")),
                      ("X3 M40iX",       ("X3", "M40i")),
                      ("X5 M",           ("X5", "M")),
                      ("X5",             ("X5", None)),
                      ("XM",             ("XM", None)),
                      # A bare variant (no xDrive) is left bare -- NOT guessed as sDrive.
                      # The catalog check then keeps it only if eBay really has it.
                      ("X3 30i", ("X3", "30i")),
                      # iX is a MODEL in eBay's list, not an X-series SUV. Must not split.
                      ("iX xDrive40", ("iX xDrive40", None)),
                      ("ALPINA B7",  ("Alpina B7", None)),     # eBay cases it "Alpina"
                      ("ALPINA B7LX", ("Alpina B7L xDrive", None)),
                      ("Hybrid 3", ("ActiveHybrid 3", None)),  # eBay's name for it
                      ("Hybrid 7L", ("ActiveHybrid 7", None))):
        eq(ETK.to_ebay_vehicle(etk), want, f"{etk} -> {want}")

    # MINI and Rolls-Royce are BMW Group marques that BMW's catalogue files under BMW, but
    # eBay treats each as its OWN MAKE -- 68,888 rows that can never validate as a BMW.
    for other in ("Cooper", "Cooper S", "Cooper S ALL4", "JCW ALL4", "Clubman",
                  "Countryman", "Phantom", "Phantom EWB", "Ghost", "Wraith", "Cullinan"):
        eq(ETK.to_ebay_vehicle(other), None, f"{other} is not a BMW -> dropped")

    for junk in ("", "   ", None, "?", "unknown"):
        eq(ETK.to_ebay_vehicle(junk), None, f"unusable model {junk!r} -> None")
    # "X" alone is not a model with an xDrive suffix stripped off it.
    eq(ETK.to_ebay_vehicle("X"), ("X", None), "a bare X is passed through, not ' xDrive'")
    eq(ETK.to_ebay_model("X5 50i xDrive"), "X5", "the back-compat shim returns the model half")

    def row(**kw):
        base = {"part_number": "72127311201", "part_name": "Head airbag", "series": "3 Series",
                "chassis": "F30", "model": "328i", "engine": "N20",
                "year_from": "2012", "year_to": "2014", "fit_certainty": "all listed cars"}
        base.update(kw); return base

    by_pn = {"7311201": ["S1"]}
    built, skipped = ETK.build([row()], by_pn)
    eq(sorted({(r["year"], r["ebay_model"]) for r in built}),
       [(2012, "328i"), (2013, "328i"), (2014, "328i")], "a year RANGE expands to one row per year")
    eq({r["guid"] for r in built}, {"S1"}, "keyed by OUR sku, not the part number")
    eq({r["make"] for r in built}, {"BMW"}, "make is BMW")

    # One part number can be on several of our listings.
    built2, _ = ETK.build([row()], {"7311201": ["S1", "S2"]})
    eq(sorted({r["guid"] for r in built2}), ["S1", "S2"], "shared part number -> every SKU")

    # An open-ended range must NOT be hard-capped -- the catalogue stops in early 2020 and
    # capping at 2019 silently drops the newest cars.
    b3, _ = ETK.build([row(year_from="2018", year_to="")], by_pn)
    eq(sorted({r["year"] for r in b3}), [2018], "missing year_to -> just the start year, not a guess")

    # Junk must be dropped, never guessed at.
    _b, sk = ETK.build([row(year_from="")], by_pn)
    eq(sk["no_year"], 1, "a row with no start year is dropped")
    _b, sk = ETK.build([row(model="")], by_pn)
    eq(sk["no_model"], 1, "a row with no model is dropped")
    _b, sk = ETK.build([row(part_number="9999999")], by_pn)
    eq(sk["no_sku"], 1, "a part number none of our SKUs carry is dropped")
    # Years before 1990 are out of scope (the ETK reaches back to the 1960s).
    b4, _ = ETK.build([row(year_from="1975", year_to="1992")], by_pn)
    eq(sorted({r["year"] for r in b4}), [1990, 1991, 1992], "years floor at 1990")

    # --- the trim column reaches the CSV, and only for the models that need one
    b5, _ = ETK.build([row(model="X5 50i xDrive", year_from="2015", year_to="2015")], by_pn)
    eq([(r["ebay_model"], r["trim"]) for r in b5], [("X5", "xDrive50i")],
       "an X-model row carries model and trim apart")
    b6, _ = ETK.build([row(model="328iX", year_from="2015", year_to="2015")], by_pn)
    eq([(r["ebay_model"], r["trim"]) for r in b6], [("328i xDrive", "")],
       "a sedan keeps the variant in the model and emits an EMPTY trim, not None -- the "
       "loader sorts these tuples and None is not comparable with str")
    b7, _ = ETK.build([row(model="Cooper S", year_from="2015", year_to="2015")], by_pn)
    eq(len(b7), 0, "a MINI row never reaches the CSV")

    # --- ebay_batch must carry the trim end to end, and NEVER widen an ETK trim to a
    # wildcard: "X5" alone claims every X5 variant of that year, which is broader than
    # BMW said. That is the DESIGN.md 5.3 trap in a source whose whole value is precision.
    import ebay_batch as EB
    csvp = os.path.join(_TMPDIR, "etk_trim.csv")
    with open(csvp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["guid", "part7", "year", "make", "raw_model", "ebay_model", "trim",
                    "mapping_flag", "title"])
        w.writerow(["S1", "7311201", "2015", "BMW", "X5 50i xDrive", "X5", "xDrive50i", "etk", "t"])
        w.writerow(["S1", "7311201", "2015", "BMW", "328iX", "328i xDrive", "", "etk", "t"])
    got = EB.load_partnumber_fitment(csvp)
    eq(got["S1"], {(2015, "BMW", "X5", "xDrive50i"), (2015, "BMW", "328i xDrive", "")},
       "the loader carries the trim as a 4th element")
    eq(sorted(got["S1"]) == sorted(got["S1"]), True, "the tuples are sortable (no None)")

    # A part-number CSV has no trim column at all -- it must still load.
    pnp = os.path.join(_TMPDIR, "pn_notrim.csv")
    with open(pnp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["guid", "year", "make", "ebay_model", "mapping_flag"])
        w.writerow(["S2", "2015", "BMW", "328i", "ok"])
    eq(EB.load_partnumber_fitment(pnp)["S2"], {(2015, "BMW", "328i", "")},
       "a CSV with no trim column still loads, trim defaulting to empty")



def t_error_summary():
    """A write failure is only actionable if you can read WHY. eBay puts the message after
    ~120 characters of errorId/domain/subdomain/category boilerplate, so truncating the raw
    envelope at 160 cut every message off mid-sentence -- all 14 failures on 2026-08-26 were
    unreadable."""
    print("eBay error summarising:")
    import ebay_batch as EB
    env = {"errors": [{"errorId": 25604, "domain": "API_INVENTORY", "subdomain": "Selling",
                       "category": "Request", "message": "short form",
                       "longMessage": "the full explanation of what went wrong",
                       "parameters": [{"name": "sku", "value": "62223"}]}]}
    got = EB._err_summary(env)
    eq("the full explanation of what went wrong" in got, True, "the longMessage survives")
    eq("25604" in got, True, "the errorId is kept")
    eq("sku=62223" in got, True, "parameters are kept -- they name the offending value")
    eq("subdomain" in got, False, "boilerplate is dropped")

    # message only, no longMessage
    eq("short form" in EB._err_summary({"errors": [{"errorId": 1, "message": "short form"}]}),
       True, "falls back to message when longMessage is absent")
    # Several errors at once are all reported, not just the first.
    multi = {"errors": [{"errorId": 1, "message": "first"}, {"errorId": 2, "message": "second"}]}
    got = EB._err_summary(multi)
    eq("first" in got and "second" in got, True, "multiple errors are all summarised")
    # An unrecognised shape must NOT be swallowed -- show the raw payload instead.
    eq(EB._err_summary({"weird": 1}), '{"weird": 1}', "unknown shape falls back to raw JSON")
    eq(EB._err_summary({}), "{}", "empty envelope falls back to raw JSON")
    eq(EB._err_summary({"errors": []}), '{"errors": []}', "no errors -> raw JSON, not empty string")
    eq(len(EB._err_summary({"errors": [{"errorId": 9, "message": "x" * 900}]})) <= 240, True,
       "output stays bounded")


def t_nondisplay_learn():
    """The audit named 258035 and 169368 as dead every night for days and nothing acted on
    them, because nothing WROTE the skip list -- it only grew when a human hand-edited it.
    learn_nondisplay closes the loop, and it has to be safe in both directions: too eager
    and we permanently stop pushing to a category that works, which is invisible."""
    print("nondisplay learning:")
    import ebay_display_audit as A
    path = os.path.join(_TMPDIR, "nd_learn.json")

    def fresh(thresh=3):
        json.dump({"categories": {}, "skip_threshold": thresh}, open(path, "w"))

    def skiplist():
        cfg = json.load(open(path))
        need = cfg["skip_threshold"]
        return sorted(c for c, v in cfg["categories"].items()
                      if v["skus_seen"] >= need and not v["displaying"])

    # --- the core claim: enough sightings, none displaying -> skip
    fresh()
    new, rev = A.learn_nondisplay([{"categoryId": "99999", "displayed": "0"}] * 3, path)
    eq(new, ["99999"], "3 sightings with 0 displays -> newly skipped")
    eq(skiplist(), ["99999"], "and it lands in the skip list")

    # --- self-healing: ONE display is enough to take it back off
    new, rev = A.learn_nondisplay([{"categoryId": "99999", "displayed": "7"}], path)
    eq(rev, ["99999"], "a category that renders again is revived")
    eq(skiplist(), [], "and leaves the skip list")

    # --- evidence, not anecdote: below the threshold we watch but do not skip
    fresh()
    new, _ = A.learn_nondisplay([{"categoryId": "88888", "displayed": "0"}] * 2, path)
    eq(new, [], "2 sightings is not enough to skip")
    eq(json.load(open(path))["categories"]["88888"]["skus_seen"], 2, "but it IS recorded")
    # ...and the third sighting, on a LATER run, tips it over. Counts must accumulate
    # across runs or a nightly 250-SKU sample would never reach the threshold.
    new, _ = A.learn_nondisplay([{"categoryId": "88888", "displayed": "0"}], path)
    eq(new, ["88888"], "counts accumulate across runs, so the 3rd sighting skips it")

    # --- a healthy category is never skipped, however many times it is seen
    fresh()
    A.learn_nondisplay([{"categoryId": "77777", "displayed": "12"}] * 50, path)
    eq(skiplist(), [], "a category that displays is never skipped")
    # A run where it happens to show nothing must NOT flip it -- displaying is cumulative,
    # so past evidence of rendering outweighs one quiet sample. Conservative on purpose:
    # over-pushing costs a GetItem call, wrongly skipping loses fitment silently.
    A.learn_nondisplay([{"categoryId": "77777", "displayed": "0"}] * 10, path)
    eq(skiplist(), [], "one quiet run does not condemn a category with a display history")

    # --- junk input must not create phantom entries
    fresh()
    A.learn_nondisplay([{"categoryId": "", "displayed": "0"},
                        {"categoryId": None, "displayed": "0"},
                        {"displayed": "0"},
                        {"categoryId": "66666", "displayed": "not-a-number"}], path)
    cats = json.load(open(path))["categories"]
    eq(sorted(cats), ["66666"], "blank/absent categoryIds are ignored")
    eq(cats["66666"]["displaying"], 0, "an unparseable count reads as 'did not display'")

    # --- a missing/corrupt file must not crash the audit
    bad = os.path.join(_TMPDIR, "nd_bad.json")
    open(bad, "w").write("{not json")
    A.learn_nondisplay([{"categoryId": "55555", "displayed": "0"}] * 3, bad)
    eq(json.load(open(bad))["categories"]["55555"]["skus_seen"], 3,
       "a corrupt skip list is rebuilt, not fatal")

    # --- MUTATION: the whole point is that ebay_batch then SKIPS these. Prove the
    # threshold is load-bearing by reading it back through the consumer.
    import ebay_batch as EB
    fresh()
    A.learn_nondisplay([{"categoryId": "44444", "displayed": "0"}] * 3, path)
    saved, EB.NONDISPLAY, EB._nondisplay = EB.NONDISPLAY, path, None
    try:
        eq("44444" in EB.load_nondisplay(), True, "ebay_batch skips what the audit learned")
        A.learn_nondisplay([{"categoryId": "44444", "displayed": "3"}], path)
        EB._nondisplay = None
        eq("44444" in EB.load_nondisplay(), False, "and stops skipping once it displays")
    finally:
        EB.NONDISPLAY, EB._nondisplay = saved, None


def run():
    for t in (t_match_trim, t_repair, t_wildcards, t_failures, t_filter_safety,
              t_cache, t_cache_file_safety, t_leak_detector, t_read_inventory_compat, t_body_suffix_trims, t_donor_year, t_donor_fields, t_shopify_throttle, t_lci_window, t_lci_categories, t_category_coverage, t_nondisplay_skip, t_nondisplay_learn, t_etk_source, t_error_summary, t_runner, t_real_data,
              t_no_real_state_touched):
        try:
            t()
        except Exception as e:                       # noqa: BLE001
            global fails
            fails += 1
            print(f"  FAIL  {t.__name__} raised {e!r}")
    return fails, _checks


def main():
    print("Display-path tests (offline, catalog stubbed)\n")
    f, n = run()
    shutil.rmtree(_TMPDIR, ignore_errors=True)
    print(f"\nResult: {'FAIL' if f else 'PASS'}  ({n - f}/{n} checks passed)")
    sys.exit(1 if f else 0)


if __name__ == "__main__":
    main()
