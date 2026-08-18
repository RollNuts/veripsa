#!/usr/bin/env python3
"""Moat gate: evaluate.py report must not expose raw coupled/random co-change rates.

A reader who sees BOTH the coupled rate AND the random baseline rate can recover the
exact lift formula (lift = coupled_rate / random_rate).  The report must emit ONLY the
final multiplier so the scoring formula stays internal (not reverse-engineerable from a
shared pre-sales report).

This test is OFFLINE and requires NO Postgres.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import evaluate as E  # noqa: E402


def _result(c_rate, r_rate, cx_rate, rx_rate, sd_rate=0.8, pairs_x=30, files=40, repo="demo"):
    def stat(rate):
        return (rate, 0.0, 10)
    return {
        "repo": repo, "files": files, "commits": 60, "pairs": 50, "pairs_x": pairs_x,
        "coupled": stat(c_rate), "rnd": stat(r_rate), "samedir": stat(sd_rate),
        "coupled_x": stat(cx_rate), "rnd_x": stat(rx_rate),
        "by_type": {t: (stat(0.5), 5) for t in ("import", "call", "schema", "config")},
    }


def main() -> int:
    checks = []

    # BEFORE-state characterisation: we know the old report emitted raw rates.  After the fix:
    #   - the multiplier IS present (buyer still sees the proof)
    #   - the raw c_rate / r_rate / cx_rate / rx_rate percentages are NOT present together in one output
    #     (formula is not reverse-engineerable from the shared report)

    strong = E._verdict(_result(c_rate=0.60, r_rate=0.15, cx_rate=0.45, rx_rate=0.10))
    md = E._report_md([strong])

    # --- multiplier IS present (the signal-quality proof the buyer needs) ---
    # rand_lift = 0.60/0.15 = 4.0x,  cross_lift = 0.45/0.10 = 4.5x
    checks.append(("multiplier: rand_lift '4.0x stronger than random' in report",
                   "4.0× stronger than random" in md))
    checks.append(("multiplier: cross_lift '4.5x stronger than random' in report",
                   "4.5× stronger than random" in md))

    # --- raw rates NOT printed together (formula non-recoverable) ---
    # c_rate = 60.0%, r_rate = 15.0%: if both appear we can compute 60/15 = 4.0.
    # cx_rate = 45.0%, rx_rate = 10.0%: if both appear we can compute 45/10 = 4.5.
    # We check that neither 60.0% nor 15.0% nor 45.0% nor 10.0% appear in the report.
    checks.append(("raw c_rate '60.0%' NOT in report", "60.0%" not in md))
    checks.append(("raw r_rate '15.0%' NOT in report", "15.0%" not in md))
    checks.append(("raw cx_rate '45.0%' NOT in report", "45.0%" not in md))
    checks.append(("raw rx_rate '10.0%' NOT in report", "10.0%" not in md))

    # --- report still useful + honest ---
    checks.append(("report: headline verdict present", "SIGNAL IS REAL" in md))
    checks.append(("report: honest boundary present", "does NOT prove rework-hours-saved" in md))
    checks.append(("report: cross-directory mention present", "cross-directory" in md.lower()))

    # --- a WEAK repo: same guarantee, multiplier present, raw rates absent ---
    weak = E._verdict(_result(c_rate=0.16, r_rate=0.15, cx_rate=0.11, rx_rate=0.10))
    md_weak = E._report_md([weak])
    # rand_lift = 0.16/0.15 ~ 1.1x,  cross_lift = 0.11/0.10 = 1.1x
    checks.append(("weak: multiplier present in report", "× stronger than random" in md_weak))
    checks.append(("weak: raw coupled rate '16.0%' NOT in report", "16.0%" not in md_weak))
    checks.append(("weak: raw random rate '15.0%' NOT in report", "15.0%" not in md_weak))

    # --- N/A (tiny repo) branch: no rates and no spurious multiplier printed ---
    tiny = E._verdict(_result(c_rate=0.60, r_rate=0.0, cx_rate=0.50, rx_rate=0.0, pairs_x=4, files=4, repo="tiny"))
    md_tiny = E._report_md([tiny])
    checks.append(("tiny: 'N/A for this repo' present", "N/A for this repo" in md_tiny))
    checks.append(("tiny: raw rates NOT in tiny-repo output", "60.0%" not in md_tiny and "0.0%" not in md_tiny))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("EVALUATE-MOAT GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
