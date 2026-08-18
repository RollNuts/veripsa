#!/usr/bin/env python3
"""COCHANGE-MIN-SUPPORT gate — raising min_support from 3 to 5 drops annotation/format-sweep false positives
while keeping genuine high-support couplings.

BACKGROUND: A dogfood precision audit on Flask (window=800) measured 30-37% FP rate at support=3, dominated by
tooling/annotation SWEEP commits (e.g. "add type hints" touching 20 files simultaneously) that inflate lift for
rarely-changed files. These FPs have strength=1.0 so the render-layer min_prob=0.4 filter does NOT catch them —
they slip through because both files are rare movers, making their co/n_a and co/n_b = 1.0 (they *only* change
together, in the sweep). Only the support floor can gate them out. Measured: support=3 -> 108 src/ pairs with 94
low-support FPs; support=5 -> 90 src/ pairs with 0 low-support FPs. Recall cost: only pairs co-changed 3-4 times
in an 800-commit window (weak signals by definition).

WHAT THIS GATE PROVES (offline, NO git clone, NO Postgres):
  (1) a pair co-changed ONLY via SWEEP commits (exactly 4 times, all in giant-sized sweeps) is DROPPED at
      support=5 via the default; it survives at support=3 (the old default, proving it was a FP there).
      Note: a sweep commit > max_commit_files=40 is skipped by fold_commits entirely, so true annotation-sweep
      FPs that arrive in giant commits are ALREADY dropped by the giant-commit skip. We test the complementary
      case: low-support pairs that slip through because the sweep was small (<=40 files) but the pair itself
      only accumulated 3-4 co-changes. This is the measured FP shape in Flask src/.
  (2) a pair co-changed GENUINELY (exactly 5 times, each in a small commit) is KEPT at support=5 via the
      default — no recall regression.
  (3) a pair co-changed 3 times (old threshold) but NOT 5 is DROPPED at the new default=5 (explicit confirm).
  (4) callers that pass min_support=3 explicitly still get the old behavior (backwards-compat for gates that
      pin the threshold for their own fixture counts).

Run:  python3 tests/test_cochange_min_support.py    (NO Postgres, NO git clone)
"""
from __future__ import annotations

import collections
import sys
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import _cg_cochange as CC  # noqa: E402

FAIL = 0


def chk(cond, label):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _make_counters(commit_filesets, max_commit_files=40):
    """Fold a list of per-commit file sets (each a set of paths) into (change, co, n_total) counters."""
    change: collections.Counter = collections.Counter()
    co: collections.Counter = collections.Counter()
    n_total = CC.fold_commits(commit_filesets, change, co, max_commit_files)
    return change, co, n_total


def main() -> int:
    # --- shared background: 80 unrelated commits so the pair files are rare (keeps lift high for both scenarios)
    background = [{f"bg/m{k}.py"} for k in range(80)]

    # --- SCENARIO A: a SWEEP FP pair — co-changed exactly 4 times in small (<=40 file) sweep commits.
    # "sweep.A" and "sweep.B" only ever change together (in annotation sweeps), never alone.
    # Both are rare (4 changes each), so strength=1.0 (co/n_a = 4/4 = 1.0) AND lift = 4*N/(4*4) >> 1.
    # At support=3: KEPT (FP!). At support=5 (new default): DROPPED (correct).
    sweep_commits = [{"sweep/a.py", "sweep/b.py"} for _ in range(4)]  # 4 small sweep commits, co=4
    all_commits_A = background + sweep_commits
    change_A, co_A, n_A = _make_counters(all_commits_A)

    # emit with default (support=5) — FP must be DROPPED
    pairs_default = CC.emit_pairs(change_A, co_A, n_A)  # uses default min_support=5
    sweep_pair_default = next(
        (p for p in pairs_default if {p["a"], p["b"]} == {"sweep/a.py", "sweep/b.py"}), None)
    chk(sweep_pair_default is None,
        "(1) sweep FP (co=4, str=1.0, lift>>1) is DROPPED at default min_support=5 -- "
        f"would be a false positive at support=3 (got {sweep_pair_default})")

    # emit with explicit support=3 — FP SURVIVES at old default (proves the change matters)
    pairs_old = CC.emit_pairs(change_A, co_A, n_A, min_support=3, min_prob=0.3, min_lift=2.0)
    sweep_pair_old = next(
        (p for p in pairs_old if {p["a"], p["b"]} == {"sweep/a.py", "sweep/b.py"}), None)
    chk(sweep_pair_old is not None and sweep_pair_old["co"] == 4,
        f"(1b) at explicit support=3 the same pair IS present (co={sweep_pair_old['co'] if sweep_pair_old else '?'}) "
        "-- confirming the default change is what removes it")

    # --- SCENARIO B: a GENUINE coupling — co-changed 5 times in small commits.
    # "real.A" and "real.B" change together 5 times + each solo once; background keeps lift high.
    real_commits = [{"real/a.py", "real/b.py"} for _ in range(5)]  # 5 real co-changes
    real_commits += [{"real/a.py"}]   # a solo (n_a=6)
    real_commits += [{"real/b.py"}]   # a solo (n_b=6)
    all_commits_B = background + real_commits
    change_B, co_B, n_B = _make_counters(all_commits_B)

    pairs_real = CC.emit_pairs(change_B, co_B, n_B)  # default min_support=5
    real_pair = next((p for p in pairs_real if {p["a"], p["b"]} == {"real/a.py", "real/b.py"}), None)
    chk(real_pair is not None and real_pair["co"] == 5,
        f"(2) genuine coupling (co=5) is KEPT at default min_support=5 (got {real_pair})")

    # --- SCENARIO C: a 3-co pair is explicitly dropped by the new default.
    barely_commits = [{"edge/x.py", "edge/y.py"} for _ in range(3)]  # exactly 3 co-changes
    all_commits_C = background + barely_commits
    change_C, co_C, n_C = _make_counters(all_commits_C)

    pairs_3co_default = CC.emit_pairs(change_C, co_C, n_C)  # default min_support=5
    edge_pair = next((p for p in pairs_3co_default if {p["a"], p["b"]} == {"edge/x.py", "edge/y.py"}), None)
    chk(edge_pair is None,
        f"(3) a pair with co=3 is DROPPED at new default min_support=5 (got {edge_pair})")

    # --- SCENARIO D: explicit min_support=3 still keeps the 3-co pair (backwards-compat for pinned gates).
    pairs_3co_explicit = CC.emit_pairs(change_C, co_C, n_C, min_support=3, min_prob=0.3, min_lift=2.0)
    edge_pair_explicit = next(
        (p for p in pairs_3co_explicit if {p["a"], p["b"]} == {"edge/x.py", "edge/y.py"}), None)
    chk(edge_pair_explicit is not None and edge_pair_explicit["co"] == 3,
        f"(4) at explicit min_support=3 the co=3 pair IS kept (backwards-compat for pinned callers) "
        f"(got {edge_pair_explicit})")

    # --- VERIFY the three function signatures all carry default=5 (documentation check via inspection)
    import inspect
    for fn_name in ("emit_pairs", "cochange_pairs", "cochange_pairs_incremental"):
        fn = getattr(CC, fn_name)
        sig = inspect.signature(fn)
        default_val = sig.parameters.get("min_support")
        chk(default_val is not None and default_val.default == 5,
            f"(sig) {fn_name}: min_support default == 5 (got {default_val.default if default_val else 'missing'})")

    print("COCHANGE-MIN-SUPPORT GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
