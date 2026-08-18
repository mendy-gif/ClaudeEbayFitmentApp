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


def run():
    for t in (t_match_trim, t_repair, t_wildcards, t_failures, t_filter_safety,
              t_cache, t_cache_file_safety, t_leak_detector, t_runner, t_real_data,
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
