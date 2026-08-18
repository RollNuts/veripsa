#!/usr/bin/env python3
"""RENDER WARN EMPHASIS — defense-in-depth on the "no textual conflict — semantic coupling" framing.

The synthetic regression fixture shows that a "likely git conflict" claim can leak onto a PR whose verdict is
semantic-only (WARN, no line overlap). The precision fix softened the
SERIALIZE/SERIALIZE_SOFT merge-conflict headlines and led the WARN bold tag with "semantic coupling, not a
git conflict". That was the CORRECTNESS fix.

THIS GATE is the DEFENSE-IN-DEPTH lock that pins the no-merge-conflict-on-warn contract under ADVERSARIAL
engine inputs the renderer might see in the wild:

  (1) A WARN that ALSO carries a stray `merge_conflict_likely=True` flag (a future engine bug, a malformed
      payload, a fork-redaction edge case) must STILL NOT emit a merge-conflict heads-up. The renderer scrubs
      these inputs at the call site before they ever reach `_render_collision_prose`.

  (2) Same for a WARN with a stray non-empty `conflict_points` list.

  (3) Same for UNKNOWN and CLEAR — neither of these verdicts has analyzable line-range geometry, so a stray
      mechanical heads-up here would be just as much an overclaim as on a WARN.

  (4) The WARN copy's "semantic coupling, not a git conflict" framing must land in the BOLD LEAD (not buried
      mid-sentence) on BOTH the contested_with arm AND the corroborated-hub arm — so the reader can't skim
      past it.

  (5) Honest-copy invariants: no "will definitely" / "guaranteed" / "certain" overclaims on any of these
      adversarial cases.

WHY THIS WIDENS the existing test_render_collision_precision.py gate:
  The existing gate (gate-196) drives the HAPPY PATH — engine emits an honest payload, the renderer reads it
  correctly. THIS gate drives the engine-bug path — engine emits a CONTRADICTORY payload (warn + stray
  conflict flag), the renderer must REFUSE to overclaim. The two together pin the contract from both sides.

PURE + OFFLINE (no DB, no network). Drives the REAL render_pr_check on synthetic engine payloads.
Content-free throughout (paths + line numbers only).

Run:  python3 tests/test_render_warn_emphasis.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import render  # noqa: E402


BASE = {"repo": "acme/app", "branch": "main"}
FAIL = 0

# Phrases that, if they appear on a non-collision verdict (warn/unknown/clear), would overclaim a textual
# git conflict. The mechanical heads-up uses both "small rebase" headlines — both must stay absent on the
# adversarial cases. NOTE: the WARN copy itself contains "not a git conflict" / "not a textual conflict",
# which is the NEGATION of an overclaim and must NOT be matched here. So we look for the headline phrasings
# only (the `> **⏳ ...` heads-up block), never the disclaimers.
MERGE_CONFLICT_HEADLINES = (
    "you may need a small rebase",            # serialize_soft headline (the softened mechanical heads-up)
    "a small rebase may also be needed",      # serialize     headline (the softened mechanical heads-up)
)


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _body(impact: dict, ref: str) -> str:
    out = render.render_pr_check(impact, ref)
    return ((out.get("summary") or "") + "\n" + (out.get("comment") or "")).lower()


def _has_any_merge_conflict_headline(body: str) -> bool:
    return any(h in body for h in MERGE_CONFLICT_HEADLINES)


def main() -> int:
    # ── (1) WARN + stray merge_conflict_likely=True (engine-bug payload) ─────────────────────────────────
    # A WARN with a stray mechanical flag set must not emit any heads-up — the verdict is semantic-only and
    # the heads-up is honest only on a verdict the engine derived from line-range geometry.
    warn_stray_flag = {**BASE, "changes": [{
        "change_id": "PR-W1", "label": "bob PR-W1", "agent": "bob",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "contested_with": ["alice PR-1"],
        # ADVERSARIAL: engine somehow emitted these on a warn verdict
        "merge_conflict_likely": True,
    }]}
    wb1 = _body(warn_stray_flag, "PR-W1")
    check(not _has_any_merge_conflict_headline(wb1),
          "(1) WARN + stray merge_conflict_likely=True: NO mechanical merge-conflict heads-up emits")
    check("semantic coupling, not a git conflict" in wb1,
          "(1) WARN + stray flag: the bold lead still carries 'semantic coupling, not a git conflict'")

    # ── (2) WARN + stray conflict_points list (engine-bug payload) ───────────────────────────────────────
    warn_stray_points = {**BASE, "changes": [{
        "change_id": "PR-W2", "label": "carol PR-W2", "agent": "carol",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "contested_with": ["dave PR-2"],
        # ADVERSARIAL: engine emitted conflict_points on a warn (would normally only happen for serialize/_soft)
        "conflict_points": [{"behind": "dave PR-2", "path": "backend/auth.py", "line": 42}],
    }]}
    wb2 = _body(warn_stray_points, "PR-W2")
    check(not _has_any_merge_conflict_headline(wb2),
          "(2) WARN + stray conflict_points: NO mechanical merge-conflict heads-up emits")
    check("near line 42" not in wb2,
          "(2) WARN + stray conflict_points: the line number is NOT leaked into a conflict heads-up")

    # ── (3) WARN + BOTH stray flag AND stray points (worst-case adversarial payload) ─────────────────────
    warn_stray_both = {**BASE, "changes": [{
        "change_id": "PR-W3", "label": "eve PR-W3", "agent": "eve",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "contested_with": ["frank PR-3"],
        # ADVERSARIAL: both stray
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "frank PR-3", "path": "backend/auth.py", "line": 99}],
    }]}
    wb3 = _body(warn_stray_both, "PR-W3")
    check(not _has_any_merge_conflict_headline(wb3),
          "(3) WARN + stray flag AND stray points: NO mechanical merge-conflict heads-up emits")
    check("near line 99" not in wb3,
          "(3) WARN + both stray: the line number is NOT leaked into a conflict heads-up")
    check("semantic coupling, not a git conflict" in wb3,
          "(3) WARN + both stray: the bold lead still leads with 'semantic coupling, not a git conflict'")

    # ── (4) UNKNOWN + stray merge_conflict_likely=True ───────────────────────────────────────────────────
    # UNKNOWN has no analyzable line ranges by construction. A stray mechanical heads-up here would be just
    # as much an overclaim as on a warn — the renderer must scrub the stray flag.
    unknown_stray = {**BASE, "changes": [{
        "change_id": "PR-U", "label": "gina PR-U", "agent": "gina",
        "verdict": "unknown",
        "paths": ["new_feature/handler.py"],
        "unknown_paths": ["new_feature/handler.py"],
        # ADVERSARIAL: engine emitted a stray mechanical flag on an unknown verdict
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "ghost PR-X", "path": "new_feature/handler.py", "line": 1}],
    }]}
    ub = _body(unknown_stray, "PR-U")
    check(not _has_any_merge_conflict_headline(ub),
          "(4) UNKNOWN + stray flag/points: NO mechanical merge-conflict heads-up emits")

    # ── (5) CLEAR + stray merge_conflict_likely=True ─────────────────────────────────────────────────────
    # CLEAR means no contention at all — no behind, no contested_with. A stray mechanical heads-up on a
    # clear PR would directly contradict the green check.
    clear_stray = {**BASE, "changes": [{
        "change_id": "PR-C", "label": "hank PR-C", "agent": "hank",
        "verdict": "clear",
        "paths": ["backend/handler.py"],
        # ADVERSARIAL: engine emitted a stray mechanical flag on a clear verdict
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "ghost PR-Y", "path": "backend/handler.py", "line": 1}],
    }]}
    cb = _body(clear_stray, "PR-C")
    check(not _has_any_merge_conflict_headline(cb),
          "(5) CLEAR + stray flag/points: NO mechanical merge-conflict heads-up emits")

    # ── (6) WARN bold-lead emphasis: the no-textual-conflict framing lands FIRST ─────────────────────────
    # The PO finding 4 root cause was that the no-textual-conflict disclaimer was BURIED mid-sentence and
    # the bold "Coordinate before merge" read as a git-conflict claim. The render now leads the bold tag
    # with the no-textual-conflict framing on BOTH the contested_with arm AND the corroborated-hub arm.
    # Pin BOTH arms here.
    warn_contested = {**BASE, "changes": [{
        "change_id": "PR-W-CT", "label": "ivy PR-W-CT", "agent": "ivy",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "contested_with": ["jay PR-7"],
    }]}
    wcb = _body(warn_contested, "PR-W-CT")
    # The bold lead must appear BEFORE the structural-link explanation (so the reader sees "not a git
    # conflict" FIRST, not after they've already inferred a git conflict).
    lead_idx = wcb.find("semantic coupling, not a git conflict")
    explain_idx = wcb.find("structural dependency links your")
    check(lead_idx >= 0 and explain_idx >= 0 and lead_idx < explain_idx,
          "(6a) WARN contested: the 'semantic coupling, not a git conflict' framing leads BEFORE the structural-link explanation")

    # corroborated-hub arm: a dampened coupling with co-change corroboration — the bold lead must carry the
    # same framing so the reader can't misread it as a git conflict either.
    warn_corrob = {**BASE, "changes": [{
        "change_id": "PR-W-CR", "label": "kim PR-W-CR", "agent": "kim",
        "verdict": "warn",
        "paths": ["backend/auth.py"],
        "dampened_with": [{
            "by": "leo PR-8", "via_hub": "backend/base.py", "corroborated": True,
        }],
    }]}
    wrb = _body(warn_corrob, "PR-W-CR")
    check("semantic coupling, not a git conflict" in wrb,
          "(6b) WARN corroborated-hub: the bold lead carries 'semantic coupling, not a git conflict'")
    check("not a textual conflict" in wrb,
          "(6b) WARN corroborated-hub: the 'not a textual conflict' disclaimer survives in the explanation")
    # the corroborated-hub arm too — bold lead before the "linked to ... through a shared file" explanation.
    lead2_idx = wrb.find("semantic coupling, not a git conflict")
    explain2_idx = wrb.find("through a shared file")
    check(lead2_idx >= 0 and explain2_idx >= 0 and lead2_idx < explain2_idx,
          "(6b) WARN corroborated-hub: the bold lead lands BEFORE the 'through a shared file' explanation")

    # ── (7) HONEST FRAMING preserved on every adversarial case ───────────────────────────────────────────
    for label, body in (("warn+stray-flag", wb1), ("warn+stray-points", wb2),
                        ("warn+both-stray", wb3), ("unknown+stray", ub),
                        ("clear+stray", cb), ("warn-contested", wcb),
                        ("warn-corrob", wrb)):
        for overclaim in ("will definitely", "guaranteed", "you will conflict"):
            check(overclaim not in body,
                  f"(7) {label}: no '{overclaim}' overclaim")

    # ── (8) SERIALIZE_SOFT happy path STILL works (we did NOT break the existing softened heads-up) ──────
    # Defense-in-depth must not regress the legitimate use case. A real serialize_soft with the engine's
    # honest flag MUST still get the softened heads-up — otherwise the regression that bit us (a SURPRISE
    # conflict on rebase because the heads-up was dropped) returns.
    soft_happy = {**BASE, "changes": [{
        "change_id": "PR-S", "label": "mia PR-S", "agent": "mia",
        "verdict": "serialize_soft",
        "paths": ["run_gates.sh"],
        "serialize_behind": ["nick PR-2"],
        "collision_points": [{"behind": "nick PR-2", "path": "run_gates.sh",
                              "symbol": None, "line_lo": 205, "line_hi": 207}],
        "merge_conflict_likely": True,
        "conflict_points": [{"behind": "nick PR-2", "path": "run_gates.sh", "line": 205}],
    }]}
    sb = _body(soft_happy, "PR-S")
    check("you may need a small rebase" in sb,
          "(8) SOFT happy-path: the softened heads-up STILL emits (defense-in-depth did not break it)")
    check("near line 205" in sb,
          "(8) SOFT happy-path: the approximate line is still named")

    print("RENDER WARN EMPHASIS GATE:", "PASS" if not FAIL else "FAIL")
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
