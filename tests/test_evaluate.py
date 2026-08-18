#!/usr/bin/env python3
"""evaluate.py gate — the customer-facing 'try Veripsa on your repo' report must not silently rot.

Fast + git-free: feeds SYNTHETIC analyze() results (the heavy real-repo backtest is proven separately in
tests/backtest_cochange.py) through evaluate._verdict + evaluate._report_md + evaluate._exit_code, and asserts
the PASS criteria (beats random AND adds cross-directory value) + that the shareable markdown carries the
verdict, the differentiated cross-dir proof, and the honest boundary — AND the small/flat-repo APPLICABILITY
GUARD: a tiny lib renders honest N/A (never a fragile "inf×"/"0.0×") and is NOT counted as a failure (exit 0),
so a buyer who tries Veripsa on a tiny single-package repo never sees it "fail".
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import evaluate as E  # noqa: E402


def _result(c_rate, r_rate, cx_rate, rx_rate, sd_rate=0.8, pairs_x=30, files=40, repo="demo"):
    """A synthetic analyze() return shaped like tests/backtest_cochange.analyze() (rate, median, n) tuples."""
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

    # STRONG: coupled beats random AND the cross-dir lift clears the bar → SIGNAL IS REAL.
    strong = E._verdict(_result(c_rate=0.60, r_rate=0.15, cx_rate=0.45, rx_rate=0.10))
    checks.append(("strong signal → signal_is_real True (beats random + cross-dir lift)", strong["signal_is_real"] is True))
    checks.append(("strong signal → cross_lift computed (4.5x)", round(strong["cross_lift"], 1) == 4.5))

    # WEAK by cross-dir: beats random overall, but adds NO value where folders are blind → NOT real (the bar).
    weak_x = E._verdict(_result(c_rate=0.60, r_rate=0.15, cx_rate=0.11, rx_rate=0.10))
    checks.append(("no cross-dir lift → signal_is_real False (the differentiated bar)", weak_x["signal_is_real"] is False))

    # WEAK by random: doesn't even beat random → not real.
    weak_r = E._verdict(_result(c_rate=0.16, r_rate=0.15, cx_rate=0.45, rx_rate=0.10))
    checks.append(("doesn't beat random → signal_is_real False", weak_r["signal_is_real"] is False))

    # the shareable report carries the verdict, the cross-dir proof, the per-type line, and the honest boundary.
    md = E._report_md([strong])
    checks.append(("report: headline verdict present", "SIGNAL IS REAL" in md))
    checks.append(("report: differentiated cross-directory proof present", "cross-directory" in md.lower() and "4.5×" in md))
    checks.append(("report: per-coupling-type breakdown present", "import" in md and "schema" in md and "config" in md))
    checks.append(("report: honest boundary (no hours-saved overclaim) present",
                   "does NOT prove rework-hours-saved" in md))
    checks.append(("report: content-free claim present", "content-free" in md.lower()))

    # a WEAK repo is reported honestly, not hidden.
    md_weak = E._report_md([weak_x])
    checks.append(("report: a weak repo says WEAK, not a false PASS", "WEAK" in md_weak and "0/1" in md_weak))

    # APPLICABILITY GUARD (tiny/flat repo honesty): a small lib with too few cross-directory coupled pairs is
    # the WRONG SHAPE for the differentiated proof — it must render N/A, NEVER a fragile "inf×" / "0.0×", and
    # must NOT be counted as a weak/failed repo (a buyer running it on a tiny lib should not see Veripsa "fail").
    tiny = E._verdict(_result(c_rate=0.60, r_rate=0.0, cx_rate=0.50, rx_rate=0.0, pairs_x=4, files=4, repo="is-number"))
    checks.append(("tiny/flat repo → applicable False", tiny["applicable"] is False))
    checks.append(("tiny/flat repo → signal_is_real False (not a PASS either — just N/A)", tiny["signal_is_real"] is False))
    md_tiny = E._report_md([tiny])
    checks.append(("report: tiny repo says N/A", "N/A for this repo" in md_tiny))
    checks.append(("report: tiny repo NEVER prints a fragile 'inf×' / '0.0×'",
                   "inf×" not in md_tiny and "inf x" not in md_tiny.lower() and "0.0×" not in md_tiny))
    checks.append(("report: tiny repo headline marks it N/A, not WEAK/FAIL",
                   "N/A" in md_tiny and "WEAK" not in md_tiny))

    # EXIT CODE = the pre-sales / CI check. N/A repos are not failures; only applicable repos can fail.
    checks.append(("exit: N/A-only → 0 (tiny lib is not a failure)", E._exit_code([tiny]) == 0))
    checks.append(("exit: a real applicable repo → 0", E._exit_code([strong]) == 0))
    checks.append(("exit: a weak APPLICABLE repo → 1", E._exit_code([weak_x]) == 1))
    checks.append(("exit: N/A + real → 0 (N/A doesn't drag a pass down)", E._exit_code([tiny, strong]) == 0))
    checks.append(("exit: N/A + weak-applicable → 1 (a real failure still fails)", E._exit_code([tiny, weak_x]) == 1))

    # a mixed report counts only APPLICABLE repos in its headline ("1/1 applicable"), and footnotes the N/A.
    md_mixed = E._report_md([tiny, strong])
    checks.append(("report: mixed headline counts only applicable repos (1/1) + notes N/A",
                   "1/1 applicable" in md_mixed and "N/A" in md_mixed))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("EVALUATE GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
