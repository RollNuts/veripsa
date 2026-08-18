#!/usr/bin/env python3
"""COVERAGE-NUDGE %-ONLY gate.

The account's early-access coverage nudge appended to a PR check summary must show a COVERAGE PERCENTAGE —
never a raw analyzed-file / Unit count, and never the internal billing metric or the plan limit (those stay
private; the metric/graph is the moat, the customer-facing magnitude is a %). This gate locks that:

  (a) OVER the plan  -> a 'covering ~X%' line (X = how much of the codebase is watched), no raw count/limit;
  (b) NEAR the limit -> an 'at ~X%' line, no raw count/limit;
  (c) under the line / unlimited (Enterprise or an unmapped paid plan: file_limit None) / garbled / non-dict
      -> None (never nudge a payer, never crash);
  (d) the rendered line NEVER contains the raw file_count / file_limit numbers nor 'analyzed files'.

Offline, NO Postgres — pure render. Content-free.
Run:  python3 tests/test_coverage_percent.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

from render import coverage_nudge_line   # noqa: E402


def test_over_shows_percent_not_count():
    line = coverage_nudge_line({"plan": "pro", "file_limit": 1000, "file_count": 1400, "over_by": 400, "near": False})
    assert line and "%" in line, line
    assert "pro" in line, line
    assert "71%" in line, line                       # 1000/1400 ≈ 71% of the codebase covered
    assert "Plan & limits" in line, line
    assert "Upgrade" not in line, line
    for raw in ("1400", "1000", "400", "analyzed files"):
        assert raw not in line, (raw, line)          # %-ONLY: no raw count / limit / metric leaks
    print("  over -> coverage %, no raw count — OK")


def test_near_shows_percent_not_count():
    line = coverage_nudge_line({"plan": "starter", "file_limit": 500, "file_count": 450, "over_by": 0, "near": True})
    assert line and "%" in line and "starter" in line, line
    assert "90%" in line, line                       # 450/500 = 90% of the plan used
    assert "Plan & limits" in line, line
    assert "Upgrade" not in line, line
    for raw in ("450", "500", "analyzed files"):
        assert raw not in line, (raw, line)
    print("  near -> usage %, no raw count — OK")


def test_none_and_safe_cases():
    assert coverage_nudge_line(None) is None
    assert coverage_nudge_line({}) is None
    assert coverage_nudge_line({"file_limit": None, "over_by": 9}) is None            # unlimited → never nudge
    assert coverage_nudge_line({"plan": "pro", "file_limit": 1000, "file_count": 100,
                                "over_by": 0, "near": False}) is None                 # under the line → no nudge
    assert coverage_nudge_line({"plan": "pro", "file_limit": "x",
                                "file_count": "y", "over_by": "z"}) is None           # garbled → no crash
    print("  none/unlimited/under/garbled -> None (never nudge a payer, never crash) — OK")


if __name__ == "__main__":
    test_over_shows_percent_not_count()
    test_near_shows_percent_not_count()
    test_none_and_safe_cases()
    print("COVERAGE PERCENT GATE: PASS")
