#!/usr/bin/env python3
"""RECALL INTEGRITY GATE — deterministic, git-free proof of the additive high-trust measurement logic.

No git / Postgres / network. We craft tiny synthetic inputs and assert the pure LOGIC the real tool
(tests/recall_integrity.py) computes, plus its content-free source posture:

  1. COMMIT-SIZE WEIGHTING: each correction commit has TOTAL weight 1 (its C(n,2) pairs share it), so one
     20-file commit cannot outvote small commits — weighted recall >> pair-micro recall on the same data.
  2. WEIGHT ACCUMULATION: a pair recurring across commits accumulates weight; total_weight == #commits.
  3. UNION PROPERTY holds under weighting: weighted(graph ∪ co-change) ≥ max(weighted parts).
  4. MECHANICAL EXCLUSION: rename/format/lint/bump/regenerate subjects are mechanical; real bug subjects are not.
  5. UNKNOWN-AWARE SPLIT: incident pairs partition into warned / at-least-unknown (dampened edge exists) /
     clear-blind (no edge at all), and not_clear = warned + unknown (never a false clear).
  6. MACRO/MEDIAN: mean / median of per-repo percentages (each repo weighted equally).
  7. DETERMINISM: pair enumeration is order-independent.
  8. CONTENT-FREE: the tool source reads no repo file body; uses git --name-only + %s only (no %b/-p/--patch).
  9. MANIFEST well-formed: pinned entries are (sha_or_None, int_count, language).

Prints `RECALL INTEGRITY GATE: PASS` / `FAIL` and returns 0/1.
"""
from __future__ import annotations

import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
import recall_integrity as RI  # noqa: E402


def run():
    failures = []

    # ---- (1) COMMIT-SIZE WEIGHTING: a big commit cannot dominate --------------------------------------------
    P_ab = frozenset(("src/a.py", "src/b.py"))
    small = ["src/a.py", "src/b.py"]                                   # 2 files -> 1 pair (P_ab)
    big = [f"pkg/c{i}.py" for i in range(20)]                          # 20 files -> 190 pairs
    pw, total = RI.weighted_ground_truth([small, big])
    if abs(total - 2.0) > 1e-9:
        failures.append(f"total_weight {total} != #commits 2")
    # detector covers ONLY the small commit's pair
    wr = RI.weighted_recall(pw, total, {P_ab})
    if abs(wr - 50.0) > 1e-9:
        failures.append(f"weighted_recall {wr} != 50% (small commit = 1 of 2 commit-votes)")
    # pair-micro on the SAME data would be 1/191 = 0.52% — prove weighting massively de-dominates the big commit
    micro = 1 / (1 + math.comb(20, 2)) * 100.0
    if not (wr > micro + 40):
        failures.append(f"weighting did not de-dominate the big commit (weighted {wr} vs micro {micro:.2f})")

    # ---- (2) WEIGHT ACCUMULATION across commits ------------------------------------------------------------
    pw2, total2 = RI.weighted_ground_truth([["a", "b"], ["a", "b", "x"]])
    if abs(pw2[frozenset(("a", "b"))] - (1.0 + 1.0 / 3.0)) > 1e-9:
        failures.append(f"pair ab weight {pw2[frozenset(('a','b'))]} != 1 + 1/3 (recurs across 2 commits)")
    if abs(total2 - 2.0) > 1e-9:
        failures.append(f"total_weight {total2} != 2")

    # ---- (3) UNION PROPERTY under weighting ---------------------------------------------------------------
    P_cd = frozenset(("svc/c.py", "ui/d.py"))
    pw3, total3 = RI.weighted_ground_truth([["src/a.py", "src/b.py"], ["svc/c.py", "ui/d.py"]])
    wg = RI.weighted_recall(pw3, total3, {P_ab})
    wc = RI.weighted_recall(pw3, total3, {P_cd})
    wu = RI.weighted_recall(pw3, total3, {P_ab} | {P_cd})
    if wu < max(wg, wc) - 1e-9:
        failures.append(f"weighted union {wu} < max(parts {wg},{wc})")
    if abs(wu - 100.0) > 1e-9:
        failures.append(f"weighted union should cover both commits = 100%, got {wu}")

    # ---- (4) MECHANICAL EXCLUSION -------------------------------------------------------------------------
    mech = ["fix: rename foo to bar", "chore: bump deps to 2.0", "fix formatting / prettier",
            "style: eslint --fix", "regenerate protobuf stubs", "fix lint errors"]
    real = ["fix: null deref in auth handler", "hotfix: wrong order id in checkout",
            "fix regression in payment flow", "revert broken migration"]
    for s in mech:
        if not RI.is_mechanical(s):
            failures.append(f"is_mechanical false-negative on {s!r}")
    for s in real:
        if RI.is_mechanical(s):
            failures.append(f"is_mechanical false-POSITIVE on real correction {s!r}")

    # ---- (5) UNKNOWN-AWARE SPLIT --------------------------------------------------------------------------
    P1 = frozenset(("a", "b")); P2 = frozenset(("c", "d")); P3 = frozenset(("e", "f"))
    incidents = {P1, P2, P3}
    combined = {P1}                 # product warns
    raw_edge = {P1, P2}             # P2 has a real edge that dampening dropped -> at-least-unknown
    sp = RI.unknown_aware_split(incidents, combined, raw_edge)
    if not (sp["warned"] == 1 and sp["unknown"] == 1 and sp["clear_blind"] == 1):
        failures.append(f"unknown-aware split {sp} != warned1/unknown1/clear_blind1")
    if abs(sp["not_clear_pct"] - (2 / 3 * 100)) > 1e-9:
        failures.append(f"not_clear_pct {sp['not_clear_pct']} != 66.7 (warned+unknown)")
    if abs(sp["clear_blind_pct"] - (1 / 3 * 100)) > 1e-9:
        failures.append(f"clear_blind_pct {sp['clear_blind_pct']} != 33.3")
    if sp["not_clear_pct"] < sp["warned_pct"] - 1e-9:
        failures.append("not_clear must be >= warned (unknown only ever adds, never subtracts)")

    # ---- (6) MACRO / MEDIAN -------------------------------------------------------------------------------
    if abs(RI.macro([10.0, 20.0, 60.0]) - 30.0) > 1e-9:
        failures.append("macro != mean")
    if abs(RI.median([10.0, 20.0, 60.0]) - 20.0) > 1e-9:
        failures.append("median != middle value")

    # ---- (7) DETERMINISM: pair enumeration order-independent ----------------------------------------------
    if RI.pairs_of(["b", "a", "c"]) != RI.pairs_of(["c", "b", "a"]):
        failures.append("pairs_of is order-dependent — must be deterministic")

    # ---- (8) CONTENT-FREE source posture of the TOOL ------------------------------------------------------
    src = open(os.path.join(ROOT, "tests", "recall_integrity.py"), "r", encoding="utf-8").read()
    for forbidden in ("open(", ".read_text(", ".read_bytes(", "io.open(", "codecs.open("):
        if forbidden in src:
            failures.append(f"tool source contains '{forbidden}' — must not read file contents")
    if "--name-only" not in src or "%s" not in src:
        failures.append("tool must use git --name-only (paths) + %s (subject only)")
    if "%b" in src:
        failures.append("tool must NOT request commit bodies (%b)")
    for tok in ('"-p"', "'-p'", '"--patch"', "'--patch'", '"-u"', "'-u'"):
        if tok in src:
            failures.append(f"tool must NOT request diffs/patches (found {tok})")

    # ---- (9) MANIFEST well-formed -------------------------------------------------------------------------
    for full, val in RI.PANEL_MANIFEST.items():
        if not (isinstance(val, tuple) and len(val) == 3):
            failures.append(f"manifest {full} malformed: {val!r}")
            continue
        sha, ev, lang = val
        if sha is not None and not (isinstance(sha, str) and 7 <= len(sha) <= 40 and all(c in "0123456789abcdef" for c in sha)):
            failures.append(f"manifest {full} sha not hex: {sha!r}")
        if not isinstance(ev, int) or ev < 0:
            failures.append(f"manifest {full} evaluable count invalid: {ev!r}")
        if lang not in ("python", "javascript", "typescript", "go"):
            failures.append(f"manifest {full} language invalid: {lang!r}")

    # ---- (10) DEV/HOLDOUT split is disjoint, non-empty, and a subset of the evaluable manifest -------------
    evaluable = {full for full, (_s, ev, _l) in RI.PANEL_MANIFEST.items() if ev > 0}
    if RI.DEV_REPOS & RI.HOLDOUT_REPOS:
        failures.append(f"dev/holdout overlap: {RI.DEV_REPOS & RI.HOLDOUT_REPOS}")
    if not RI.DEV_REPOS or not RI.HOLDOUT_REPOS:
        failures.append("dev/holdout split must be non-empty on both sides")
    if not RI.HOLDOUT_REPOS <= evaluable:
        failures.append(f"holdout has non-evaluable repos: {RI.HOLDOUT_REPOS - evaluable}")
    if (RI.DEV_REPOS | RI.HOLDOUT_REPOS) != evaluable:
        failures.append("dev ∪ holdout must exactly cover the evaluable manifest")

    ok = not failures
    print("\n=== RECALL INTEGRITY GATE ===")
    print(f"  weighting: big-commit down-vote weighted {wr:.0f}% vs pair-micro {micro:.2f}% (de-dominated)")
    print(f"  unknown-aware: warned {sp['warned_pct']:.0f}% / not-clear {sp['not_clear_pct']:.0f}% / "
          f"clear-blind {sp['clear_blind_pct']:.0f}%")
    if failures:
        for f in failures:
            print("  FAIL:", f)
    print("\nRECALL INTEGRITY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(run())
