#!/usr/bin/env python3
"""PROTO-REF-PERF GATE — offline, hermetic (no DB, no network, no clone).

Guards the perf fix in `_cg_api_contract._proto_references`:

  OLD: for EVERY non-exact identifier, loop the ENTIRE `known` proto-symbol set doing
       startswith + suffix-membership  =>  O(idents * known).  Measured ~15 s PER FILE at
       known=20k; one build_graph on googleapis (known ~= 23k) extrapolated to ~41 minutes.

  NEW: strip each FIXED stub suffix (~9 entries) off the ident and test exact membership of the
       base in `known`  =>  O(idents * suffixes) = O(idents), independent of `known` size.

The rewrite is BYTE-IDENTICAL: a stub match needs ident == base + suffix with base in `known` and
suffix in _PROTO_STUB_SUFFIXES, and no suffix is a proper string-suffix of another, so AT MOST ONE
such split exists for any ident — i.e. the old loop's set-iteration order never affected which name
was returned, and the suffix-strip finds that same unique base.

This gate proves two things and FAILS (non-zero exit) if either breaks:

  1. EQUIVALENCE — an inline reference copy of the OLD logic vs the live (NEW) implementation must
     produce IDENTICAL output over targeted adversarial fixtures (ambiguous prefix
     `OrderServiceClient` with {Order, OrderService}; Service/Servicer overlap; exact matches that
     equal a suffix; base == a suffix-name; no-match) PLUS thousands of randomized fixtures. 0
     mismatches.

  2. SCALING (bounded-ness) — the NEW _proto_references time at known=20,000 must be within a small
     factor (<=5x, generous so it is not flaky) of its time at known=1,000. The OLD code is ~20x+
     here; a reintroduced O(known) loop trips this.

Run directly: `python3 tests/test_proto_ref_perf.py`. Prints `PROTO-REF-PERF GATE: PASS` on success.
"""
import os
import random
import re
import string
import sys
import time

# Import the module under test from the repo root (this file lives in tests/).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _cg_api_contract as M  # noqa: E402

# Pull the SAME fixed inputs the production code uses, so the reference and the SUT share them.
_SUF = M._PROTO_STUB_SUFFIXES
_IDENT_RE = M._PROTO_IDENT_RE


def _old_proto_references(text, known):
    """Verbatim copy of the PRE-FIX _proto_references inner logic (the O(idents*known) form).

    This is the reference oracle. It must stay a faithful copy of the original loop-over-known so
    the equivalence assertion is meaningful. Original:

        for known_name in known:
            if ident != known_name and ident.startswith(known_name):
                suffix = ident[len(known_name):]
                if suffix in _PROTO_STUB_SUFFIXES:
                    found.add(known_name); break
    """
    found = set()
    if not known:
        return found
    for m in _IDENT_RE.finditer(text):
        ident = m.group(1)
        if ident in known:
            found.add(ident)
            continue
        for known_name in known:
            if ident != known_name and ident.startswith(known_name):
                suffix = ident[len(known_name):]
                if suffix in _SUF:
                    found.add(known_name)
                    break
    return found


# ---------------------------------------------------------------------------
# 1. EQUIVALENCE
# ---------------------------------------------------------------------------

def _targeted_fixtures():
    """(ident-bag, known) pairs that stress the exact semantic corners called out in the audit."""
    return [
        # Ambiguous prefix: BOTH Order and OrderService are known. Only OrderService+Client is a
        # valid (base, suffix) split — Order+'ServiceClient' is NOT a stub suffix. Must add
        # OrderService, deterministically, regardless of iteration order.
        ("OrderServiceClient", frozenset({"Order", "OrderService"})),
        ("OrderServiceClient", frozenset({"OrderService"})),
        ("OrderServiceClient", frozenset({"Order"})),            # no valid split -> no match
        # Service / Servicer overlap (Servicer ends with ...icer, NOT with 'Service').
        ("FooServicer", frozenset({"Foo", "FooService"})),       # Foo + 'Servicer' -> Foo
        ("FooServicer", frozenset({"FooService"})),              # FooService + 'r' -> no match
        ("FooService", frozenset({"Foo"})),                       # Foo + 'Service' -> Foo
        ("HealthCheckRequest", frozenset({"Health", "HealthCheck"})),  # HealthCheck + 'Request'
        # Exact match that itself equals a stub-suffix string.
        ("Client", frozenset({"Client"})),                        # exact -> Client
        ("ClientServer", frozenset({"Client"})),                  # Client + 'Server' -> Client
        # Arbitrary non-suffix remainder must NOT match.
        ("OrderXyz", frozenset({"Order"})),
        ("Order", frozenset({"Order"})),                          # bare exact
        # Empty known.
        ("OrderServiceClient", frozenset()),
        # Suffix-only ident with no known base (base would be empty -> must NOT match).
        ("Client", frozenset({"Server"})),
    ]


def _check_equivalence():
    rng = random.Random(20260620)
    mismatches = 0

    # Targeted adversarial fixtures.
    for ident, known in _targeted_fixtures():
        text = " ".join([ident] * 3)
        a = _old_proto_references(text, known)
        b = M._proto_references(text, known)
        if a != b:
            mismatches += 1
            print("  EQUIV MISMATCH (targeted): ident=%r known=%s old=%s new=%s"
                  % (ident, sorted(known), sorted(a), sorted(b)))

    # Randomized fixtures: random known sets + texts mixing exact names, stub-suffixed names,
    # ambiguous-prefix constructions, and pure noise.
    alpha = string.ascii_letters + "_"

    def rand_name():
        return "".join(rng.choice(alpha) for _ in range(rng.randint(1, 12)))

    suf_list = sorted(_SUF)
    N_RANDOM = 8000
    for _ in range(N_RANDOM):
        known_list = [rand_name() for _ in range(rng.randint(0, 40))]
        # Deliberately seed ambiguous prefixes: sometimes add base AND base+Service to known.
        if known_list and rng.random() < 0.3:
            b0 = rng.choice(known_list)
            known_list.append(b0 + "Service")
        known = frozenset(known_list)
        toks = []
        for _ in range(rng.randint(1, 30)):
            r = rng.random()
            if known and r < 0.45:
                base = rng.choice(list(known))
                if rng.random() < 0.5:
                    toks.append(base + rng.choice(suf_list))   # stub form
                else:
                    toks.append(base)                          # exact
            elif known and r < 0.6:
                # base + base-of-another => double-name token (exercises startswith vs endswith)
                toks.append(rng.choice(list(known)) + rng.choice(list(known)))
            else:
                toks.append(rand_name())                        # noise
        text = " ".join(toks)
        a = _old_proto_references(text, known)
        b = M._proto_references(text, known)
        if a != b:
            mismatches += 1
            if mismatches <= 10:
                print("  EQUIV MISMATCH (random): text=%r known=%s old=%s new=%s"
                      % (text[:80], sorted(known)[:8], sorted(a), sorted(b)))

    if mismatches:
        print("EQUIVALENCE: FAIL (%d mismatches over targeted + %d randomized fixtures)"
              % (mismatches, N_RANDOM))
        return False
    print("EQUIVALENCE: PASS (0 mismatches over %d targeted + %d randomized fixtures, "
          "incl. ambiguous-prefix OrderServiceClient/{Order,OrderService})"
          % (len(_targeted_fixtures()), N_RANDOM))
    return True


# ---------------------------------------------------------------------------
# 2. SCALING (bounded-ness)
# ---------------------------------------------------------------------------

def _build_code_text():
    """A code-file-shaped blob: a few hundred real stub references + many non-matching idents
    (the hot path the OLD code degraded on — every miss looped the whole known set)."""
    toks = []
    for i in range(500):
        toks.append("Sym%05dServiceClient" % (i % 50))   # real generated-stub references
        toks.append("localVar%d" % i)                     # non-matching identifiers
        toks.append("SomeRandomToken_%d" % i)
    return " ".join(toks)


def _mk_known(n):
    return (frozenset("Sym%05dService" % i for i in range(n))
            | frozenset("Msg%05d" % i for i in range(n)))


def _time_new(text, known, repeat=3):
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        M._proto_references(text, known)
        best = min(best, time.perf_counter() - t)
    return best


def _check_scaling():
    text = _build_code_text()
    known_small = _mk_known(1000)     # 2,000 names
    known_large = _mk_known(20000)    # 40,000 names

    # Warm caches / interpreter, then measure (best-of-N to reduce noise).
    M._proto_references(text, known_small)
    M._proto_references(text, known_large)

    t_small = _time_new(text, known_small)
    t_large = _time_new(text, known_large)

    # Output must be identical at both sizes for the matches that DO exist (sanity: the fix still
    # finds the real references), independent of known size beyond the relevant names.
    out_small = M._proto_references(text, known_small)
    out_large = M._proto_references(text, known_large)
    # The large set is a superset of the small for the Sym*Service names, so out_large >= out_small.
    if not out_small.issubset(out_large):
        print("SCALING: FAIL (functional sanity: small-known matches not a subset of large-known)")
        return False
    if not out_small:
        print("SCALING: FAIL (functional sanity: no references matched at all — fixture broken)")
        return False

    ratio = t_large / t_small if t_small > 0 else float("inf")
    FACTOR = 5.0
    print("SCALING: known=1k -> %.3f ms | known=20k -> %.3f ms | ratio=%.2fx (threshold <= %.0fx; "
          "OLD O(known) form is ~20x+)"
          % (t_small * 1000.0, t_large * 1000.0, ratio, FACTOR))
    if ratio > FACTOR:
        print("SCALING: FAIL (time grows with known-set size — looks like a reintroduced "
              "O(idents*known) loop)")
        return False
    print("SCALING: PASS (time is bounded / flat in known-set size)")
    return True


def main():
    ok_equiv = _check_equivalence()
    ok_scale = _check_scaling()
    if ok_equiv and ok_scale:
        print("PROTO-REF-PERF GATE: PASS")
        return 0
    print("PROTO-REF-PERF GATE: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
