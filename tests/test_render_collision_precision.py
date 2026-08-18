#!/usr/bin/env python3
"""RENDER PRECISION on the COLLISION/CONFLICT prose.

Synthetic regression observation: the "likely git conflict" wording is too strong in cases with no
actual text overlap — a PR that had ONLY semantic coupling (verdict='warn') and NO conflict_points was reading
as if git would textually conflict on rebase. Two render-side fixes this gate pins:

  (A) WARN is semantic-only: a 'warn' verdict (semantic coupling, no overlapping line ranges) must NOT make any
      claim a git textual conflict is likely. The rendered copy must lead with the no-textual-conflict framing
      (so the customer reads "semantic coupling, not a git conflict" FIRST, not buried mid-sentence).

  (B) SERIALIZE_SOFT softens the conflict headline: the soft case sits on append-mostly low-value files
      (run_gates.sh and kin), and the conflict heuristic is line-range geometry only (overlap-or-adjacent within
      a 2-line tolerance) — no 3-way merge of bodies. The previous "Likely a small merge conflict on rebase"
      headline read as "you WILL conflict" — a heuristic with no body-level signal cannot claim that. The
      softened headline is "You may need a small rebase"; the "likely" hedge stays in the explanation; the term
      "merge conflict" still appears (so a developer scanning the text sees the familiar git term).

  (C) Honest framing preserved on BOTH paths: never "will definitely" / "guaranteed", always "likely" / "may".

PURE + OFFLINE (no DB, no network). Drives the REAL render_pr_check on synthetic engine payloads. Content-free
throughout (paths + line numbers only).

Run:  python3 tests/test_render_collision_precision.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import render  # noqa: E402


BASE = {"repo": "acme/app", "branch": "main"}
FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _body(impact: dict, ref: str) -> str:
    out = render.render_pr_check(impact, ref)
    return ((out.get("summary") or "") + "\n" + (out.get("comment") or "")).lower()


def main() -> int:
    # ── (A) WARN-ONLY (semantic coupling, no conflict_points) → NO merge-conflict claim ──────────────────
    # Synthetic case: a PR has semantic coupling with another in-flight PR, but the line ranges
    # do NOT overlap (no conflict_points, merge_conflict_likely=False). Rendered output must NOT claim a
    # git conflict is likely, and SHOULD lead with the "not a git conflict" framing.
    warn_impact = {**BASE, "changes": [{
        "change_id": "PR-W", "label": "bob PR-W", "agent": "bob",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "impact": ["backend/handlers.py"],
        "contested_with": ["alice PR-1"],
        # KEY: no merge_conflict_likely, no conflict_points — pure semantic case
    }]}
    wb = _body(warn_impact, "PR-W")
    check("coordinate before merge" in wb,
          "(A) WARN: the 'Coordinate before merge' lead appears")
    check("semantic coupling, not a git conflict" in wb,
          "(A) WARN: the bold lead carries the 'semantic coupling, not a git conflict' framing")
    check("not a textual conflict" in wb,
          "(A) WARN: the 'not a textual conflict git would catch' disclaimer survives")
    # The mechanical merge-conflict heads-up must NOT fire for a warn (no overlap geometry).
    check("you may need a small rebase" not in wb and "a small rebase may also be needed" not in wb,
          "(A) WARN: NO 'you may need a small rebase' merge-conflict heads-up (semantic-only, no line overlap)")
    check("merge conflict" not in wb,
          "(A) WARN: the term 'merge conflict' does not appear (warn is semantic-only)")

    # ── (B) SERIALIZE_SOFT keeps a softened merge-conflict heads-up, not a strong "Likely a merge conflict" ──
    # The soft case sits on an append-mostly low-value file. The heuristic is line-range geometry only, so the
    # headline must be soft ("you may need a small rebase"), not the previous overclaim ("Likely a small merge
    # conflict on rebase"). The familiar git term ("merge conflict") and the honest hedge ("likely") still
    # appear in the body — so a developer scanning the comment recognises the regression-fix heads-up.
    soft_impact = {**BASE, "changes": [{
        "change_id": "PR-S", "label": "dave PR-S", "agent": "dave",
        "verdict": "serialize_soft",
        "paths": ["run_gates.sh"],
        "serialize_behind": ["carol PR-2"],
        "collision_points": [{"behind": "carol PR-2", "path": "run_gates.sh",
                              "symbol": None, "line_lo": 205, "line_hi": 207}],
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "carol PR-2", "path": "run_gates.sh", "line": 205}],
    }]}
    sb = _body(soft_impact, "PR-S")
    check("you may need a small rebase" in sb,
          "(B) SOFT: the softened headline 'You may need a small rebase' is used")
    check("likely a small merge conflict" not in sb,
          "(B) SOFT: the OLD overstrong 'Likely a small merge conflict' headline is GONE")
    check("merge conflict" in sb,
          "(B) SOFT: the term 'merge conflict' still appears in the body (the developer-recognised git term)")
    check("near line 205" in sb,
          "(B) SOFT: the conflict heads-up still names the approximate line (content-free locus preserved)")
    # the run_gates.sh file is named in the heads-up (the regression-fix preservation).
    check("run_gates.sh" in sb,
          "(B) SOFT: the file is still named (the regression fix preserved — no surprise conflict)")

    # ── (C) HARD SERIALIZE also softens the headline, but the term + the honest hedge survive ──────────────
    hard_impact = {**BASE, "changes": [{
        "change_id": "PR-H", "label": "eve PR-H", "agent": "eve",
        "verdict": "serialize",
        "paths": ["backend/auth.py"],
        "serialize_behind": ["frank PR-3"],
        "collision_points": [{"behind": "frank PR-3", "path": "backend/auth.py",
                              "symbol": "login", "line_lo": 20, "line_hi": 30}],
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "frank PR-3", "path": "backend/auth.py", "line": 25}],
    }]}
    hb = _body(hard_impact, "PR-H")
    check("a small rebase may also be needed" in hb,
          "(C) HARD: the softened 'A small rebase may also be needed' headline is used")
    check("expect a merge conflict" not in hb,
          "(C) HARD: the OLD overstrong 'Expect a merge conflict' headline is GONE")
    check("merge conflict" in hb,
          "(C) HARD: the term 'merge conflict' still appears (the developer-recognised git term)")
    check("land in order" in hb,
          "(C) HARD: the 'Land in order' lead still carries the wait-in-line copy")
    check("likely" in hb,
          "(C) HARD: the honest 'likely' hedge survives")

    # ── (D) HONEST FRAMING — no "will definitely" / "guaranteed" / "certain" on either path ─────────────────
    for label, body in (("warn", wb), ("soft", sb), ("hard", hb)):
        check("will definitely" not in body,
              f"(D) {label}: no 'will definitely' overclaim")
        check("guaranteed" not in body,
              f"(D) {label}: no 'guaranteed' overclaim")

    # ── (E) WARN+depends_on (semantic + upstream change) still says NO git conflict ────────────────────────
    # A real-world WARN scenario: semantic coupling AND a depends_on_changing upstream dep being edited. The
    # render must STILL not assert any git textual conflict in the warn block (the depends_on copy is its own
    # block; the warn copy must keep the "semantic coupling, not a git conflict" lead).
    warn_with_depends = {**BASE, "changes": [{
        "change_id": "PR-WD", "label": "gina PR-WD", "agent": "gina",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "contested_with": ["alice PR-1"],
        "depends_on_changing": [{"path": "backend/base.py", "by": "alice PR-1"}],
    }]}
    wdb = _body(warn_with_depends, "PR-WD")
    check("semantic coupling, not a git conflict" in wdb,
          "(E) WARN+depends: the bold lead still carries 'semantic coupling, not a git conflict'")
    check("you may need a small rebase" not in wdb and "a small rebase may also be needed" not in wdb,
          "(E) WARN+depends: still no 'small rebase' merge-conflict heads-up (warn is semantic-only)")

    print("RENDER COLLISION PRECISION GATE:", "PASS" if not FAIL else "FAIL")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
