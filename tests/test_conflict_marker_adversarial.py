#!/usr/bin/env python3
"""ADVERSARIAL: conflict-marker detector + Unknown verdict reasoning.

Companion to tests/test_conflict_marker_detector.py and the Unknown-verdict honesty refinement (PO 2026-06-25
split: "new in this PR" vs "extractor gap"). The base gate pins the HAPPY-PATH contract. This gate pins the
ADVERSARIAL boundary — malformed / edge / "looks-like-a-marker-but-isn't" inputs designed to break the invariants:

  (A1) `<<<<<<<` lives INSIDE a string literal (e.g. a parsing test adds a fixture string holding the marker
       shape). The detector must NOT fire — the marker is mid-line content, not column 0. The high-precision
       gate is "marker at line-START of an added line", so leading whitespace / indentation / a leading code
       token MUST short-circuit the match.

  (A2) TRUNCATED PATCH — `<<<<<<<` present but no closing `>>>>>>>`. A GitHub diff truncated mid-conflict
       (the PR is huge, the API returned the patch capped). The pair-gate must NOT fire: a half-marker is
       insufficient signal, "cry-wolf on truncated diffs" is the wrong tradeoff.

  (A3) MARKDOWN FENCED CODE BLOCK — a docs PR adds an example showing a conflict-resolution workflow inside
       a fenced code block. The marker shape sits at column 0 (after the fence's content prefix). Per the
       current detector contract, this DOES fire on the `+` added lines (the detector cannot distinguish
       fenced code from real code). Document the behavior: the detector is honestly LOCAL — it sees only
       diff syntax, not file-language. The escalation copy in render still names path + line, content-free.

  (B) UNKNOWN-VERDICT REASONING SPLIT — two semantically-distinct causes that the old lumped copy buried:
      (b1) NEW FILE in this PR (Files-API status='added') → "coupling computable after merge — expected,
           not a warning"
      (b2) EXTRACTOR GAP (existed in main but absent from Veripsa's graph) → "treat as unknown, not clear —
           possible extractor gap"
      Same verdict='unknown', distinct reason copy. The customer must SEE the difference, not get a single
      blob that trains them to ignore both.

CONTENT-FREE FIXTURE DISCIPLINE: the marker shapes in this file are built by '*'-multiplication (M_OURS =
"<" * 7 etc.) so the detector's regex (which scans line-starts of patch ADDED lines, NOT this file's source)
cannot accidentally match a literal triplet in THIS file's bytes.

PURE + OFFLINE. No DB. No network.

Run:  python3 tests/test_conflict_marker_adversarial.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import github_rest  # noqa: E402
import render  # noqa: E402


# CONTENT-FREE FIXTURE: build the marker shapes via string-concatenation (see module docstring).
M_OURS = "<" * 7
M_SEP = "=" * 7
M_THEIRS = ">" * 7


FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


# ── (A1) marker shape INSIDE a string literal — NOT at column 0 ───────────────────────────────────────────

def _patch_marker_in_string_literal() -> str:
    """A parsing test fixture that legitimately stores the marker shape INSIDE a Python string literal. The
    `+`-prefixed added line begins with the `+` diff-marker and then a code-token (`MARKER = "..."`), so the
    marker shape sits MID-LINE — not at column 0. The detector contract is "marker at the START of an added
    line"; a mid-line marker MUST NOT match.

    Also a pair (`<<<<<<<` AND `>>>>>>>` both inside the literal) — so the file-level pair gate doesn't save
    us. The line-start gate is what must hold."""
    lines = [
        "@@ -1,3 +1,6 @@",
        " # tests/test_parser_fixtures.py",
        " import textwrap",
        "+",
        '+OURS_FIXTURE = "' + M_OURS + ' HEAD"        # parsing fixture, not a real conflict',
        '+THEIRS_FIXTURE = "' + M_THEIRS + ' feature"  # parsing fixture, not a real conflict',
        "+",
    ]
    return "\n".join(lines)


def _patch_marker_with_leading_whitespace() -> str:
    """Even more adversarial: the marker shape appears at the START of a string but with INDENTATION before
    it on the line (a marker shape inside an indented multiline triple-quote in a test fixture). Git's real
    conflict markers ALWAYS sit at column 0; an indented `<<<<<<<` is content. Must not fire."""
    lines = [
        "@@ -1,3 +1,6 @@",
        " # docs/example.md",
        "+",
        "+    " + M_OURS + " example shown indented in a code block",
        "+    " + M_SEP,
        "+    " + M_THEIRS + " other branch",
        "+",
    ]
    return "\n".join(lines)


# ── (A2) TRUNCATED diff — `<<<<<<<` present, no `>>>>>>>` ─────────────────────────────────────────────────

def _patch_truncated_no_theirs() -> str:
    """GitHub's PR Files API CAPS patch payloads. A huge PR may have its diff truncated MID-CONFLICT — the
    `<<<<<<<` head landed in the payload but the `>>>>>>>` tail was cut off. The pair gate's discipline
    ("both halves required") is precisely what protects against false alarms on truncated payloads — and
    against any other "half a marker shape" pattern that isn't a real merge conflict."""
    lines = [
        "@@ -10,3 +10,6 @@",
        " def foo():",
        "     pass",
        "+def bar():",
        "+" + M_OURS + " HEAD",
        "+    x = 1",
        "+" + M_SEP,
        "+    x = 2",
        # NB: NO `>>>>>>>` line — the patch was truncated mid-conflict, OR the second half lives in a
        # different file the API didn't return. Either way, this single file should not fire alone.
    ]
    return "\n".join(lines)


def _patch_truncated_only_theirs() -> str:
    """The mirror case: a truncated payload starts AFTER the `<<<<<<<` head but contains the `>>>>>>>` tail.
    Again, half a marker shape is insufficient — the pair gate must hold."""
    lines = [
        "@@ -100,3 +100,5 @@",
        "     x = 2",
        "+" + M_THEIRS + " feature",
        "+    return x",
        " ",
    ]
    return "\n".join(lines)


# ── (A3) MARKDOWN FENCED CODE BLOCK — column-0 marker shape inside a docs example ─────────────────────────

def _patch_marker_in_markdown_fenced_block() -> str:
    """A docs PR adds a markdown fenced code block illustrating "how to resolve a conflict" — INSIDE the
    fence the marker shapes sit at column 0 (after the `+` diff-prefix), satisfying both the line-start
    gate AND the file-level pair gate. The detector IS local (sees only diff syntax, not file language),
    so it does fire here — and we document that as the honest boundary: the detector cannot distinguish
    "docs example of a marker" from a "real botched rebase".

    Why we PIN this behavior rather than suppress it:
      • Catching real markers in docs files is RARE — most docs PRs do not insert the column-0 triplet shape.
      • Adding language-aware logic ("if path endswith .md, suppress") trades a tiny FP risk for a real FN
        risk (a botched rebase in a .md file IS still a build-breaker for any markdown linter / docs site).
      • The escalation copy is CONTENT-FREE (path + line only — no body), so even on a docs-example FP the
        leak is purely the path the contributor already named.
      • The render comment says "likely git conflict" (PO 2026-06-25 honest-copy softening, #457) — it does
        NOT claim certainty. A doc author who DID intend the markers can dismiss with one comment.
    The trade-off is documented; this adversarial gate pins it explicitly so any later detector change
    that flips this behavior must update the gate too (and own the precision/recall trade explicitly)."""
    lines = [
        "@@ -1,3 +1,12 @@",
        " # CONFLICT-RESOLUTION GUIDE",
        " ",
        "+## Example conflict",
        "+",
        "+When git cannot auto-merge, you will see:",
        "+",
        "+```",
        "+" + M_OURS + " HEAD",
        "+    your_change()",
        "+" + M_SEP,
        "+    their_change()",
        "+" + M_THEIRS + " feature",
        "+```",
    ]
    return "\n".join(lines)


# ── (B) UNKNOWN-VERDICT REASONING SPLIT ───────────────────────────────────────────────────────────────────


def _impact_unknown(unknown_paths: list, repo: str = "acme/app") -> dict:
    """Build a minimal impact dict for a verdict='unknown' PR with the supplied unknown_paths."""
    return {
        "repo": repo,
        "branch": "main",
        "changes": [{
            "change_id": "PR-U",
            "label": "alice PR-U",
            "agent": "alice",
            "verdict": "unknown",
            "paths": list(unknown_paths),
            "unknown_paths": list(unknown_paths),
        }],
    }


def main() -> int:
    # ── (A1) marker inside a string literal — must NOT fire ───────────────────────────────────────────────
    print("-- (A1) marker shape inside a STRING LITERAL (not column 0) → must NOT fire --")

    f1 = github_rest.conflict_markers_from_patch(_patch_marker_in_string_literal())
    check(f1 == [],
          "(A1a) marker shape mid-line inside a Python string literal → detector returns [] (line-start gate holds)")

    f2 = github_rest.conflict_markers_from_patch(_patch_marker_with_leading_whitespace())
    check(f2 == [],
          "(A1b) marker shape preceded by 4-space indent (inside a code block example) → detector returns "
          "[] (git's real markers always sit at column 0; indented marker text is content)")

    # ── (A2) truncated diff — pair gate must hold ─────────────────────────────────────────────────────────
    print("-- (A2) TRUNCATED diff (half a marker shape) → pair-gate must hold; must NOT fire --")

    f3 = github_rest.conflict_markers_from_patch(_patch_truncated_no_theirs())
    check(f3 == [],
          "(A2a) patch has <<<<<<< + ======= but no matching >>>>>>> → pair-gate returns [] "
          "(a half-marker shape is insufficient signal; the API truncated the diff or the tail is in "
          "another file the per-file scanner doesn't see)")

    f4 = github_rest.conflict_markers_from_patch(_patch_truncated_only_theirs())
    check(f4 == [],
          "(A2b) patch has only >>>>>>> (no opening <<<<<<<) → pair-gate returns []")

    # SANITY: assemble the two halves into ONE file's patch — NOW the pair fires (validates the gate isn't
    # spuriously suppressing pair-complete inputs; the (A2a/b) negatives are specifically about the HALF
    # case).
    combined_lines = _patch_truncated_no_theirs().split("\n") + [
        "@@ -100,3 +100,5 @@",
        "     x = 2",
        "+" + M_THEIRS + " feature",
        "+    return x",
    ]
    combined_full = "\n".join(combined_lines)
    f_combined = github_rest.conflict_markers_from_patch(combined_full)
    check(len(f_combined) >= 2,
          "(A2c) SANITY: when BOTH halves are present in the same file's patch, the pair-gate DOES fire "
          "(confirms the (A2a/b) negatives aren't a bug — they're the gate correctly holding on a half-shape)")

    # ── (A3) markdown fenced block — detector IS local; document the boundary ─────────────────────────────
    print("-- (A3) marker shape in a MARKDOWN FENCED BLOCK (column 0) → detector IS local --")

    f5 = github_rest.conflict_markers_from_patch(_patch_marker_in_markdown_fenced_block())
    # Honest documentation of the boundary: the detector cannot distinguish "docs example of a marker" from
    # a "real botched rebase". It fires. The render-side honest-copy ("likely git conflict") is what
    # prevents a confident overclaim — see (A3 follow-up) below for the render assertion.
    check(len(f5) >= 2,
          "(A3a) marker shape at column 0 inside a docs fenced code block IS detected (the detector is "
          "honestly LOCAL — it sees only diff syntax, not file language). The trade-off favors recall on "
          "real botched-rebase docs PRs over precision on docs-example PRs.")
    # And the render copy still uses the honest-softened "likely git conflict" wording (PO 2026-06-25
    # honest-copy fix #457) — never a confident "is" claim that would mislabel a docs example.
    impact_docs = {
        "repo": "acme/docs", "branch": "main",
        "changes": [{"change_id": "PR-D", "label": "doc PR-D", "agent": "doc",
                     "verdict": "clear", "paths": ["docs/conflict-guide.md"]}],
    }
    out_docs = render.render_pr_check(impact_docs, "PR-D",
                                      conflict_markers=[{"path": "docs/conflict-guide.md",
                                                         "line": 8, "kind": "ours"}])
    check(out_docs["conclusion"] == "action_required",
          "(A3b) docs fenced-block marker → render still escalates to action_required (recall over precision)")
    cmt_docs = (out_docs.get("comment") or "").lower()
    check("likely" in cmt_docs,
          "(A3c) render copy uses the honest-softened 'likely' qualifier (the PO 2026-06-25 #457 fix) — no "
          "confident 'IS a git conflict' overclaim that would mis-label a docs example as a build break")
    # CONTENT-FREE preserved: the comment names the path the contributor already named, never the line body.
    for forbidden in ("your_change", "their_change", "auto-merge"):
        check(forbidden not in (out_docs.get("comment") or ""),
              f"(A3d) docs escalation comment does not leak the line body — '{forbidden}' absent (only "
              f"path + line metadata rendered)")

    # ── (B) UNKNOWN-VERDICT REASONING SPLIT — same verdict, different reason copy ──────────────────────────
    print("-- (B) Unknown verdict: new-file vs extractor-gap → distinct reason strings --")

    NEW_PATH = "new/brand_new_feature.py"
    GAP_PATH = "legacy/already_in_main.rs"

    # (b1) NEW FILE: every unknown_paths entry is in added_paths → "new in this PR" wording, framed as
    #      "expected, not a warning". No "extractor gap" / "unsupported language" wording.
    out_new = render.render_pr_check(_impact_unknown([NEW_PATH]), "PR-U",
                                     added_paths=[NEW_PATH])
    cmt_new = out_new.get("comment") or ""
    check("NEW in this PR" in cmt_new,
          "(B1a) new-file Unknown: comment says 'NEW in this PR' (the expected-not-a-warning framing)")
    check("computable after merge" in cmt_new,
          "(B1b) new-file Unknown: comment says 'computable after merge' (the action the customer expects)")
    check("expected, not a warning" in cmt_new,
          "(B1c) new-file Unknown: comment frames it as 'expected, not a warning' (no false-alarm habituation)")
    # NEGATIVE: the gap-only wording MUST NOT leak here (this is what the split fixes — the lumped copy did)
    check("extractor gap" not in cmt_new,
          "(B1d) new-file Unknown: comment does NOT say 'extractor gap' (that wording is reserved for real "
          "main-side absence — using it on a new file would mislabel an EXPECTED case as a Veripsa defect)")
    check("unsupported language" not in cmt_new,
          "(B1e) new-file Unknown: comment does NOT say 'unsupported language' (same reason)")

    # (b2) EXTRACTOR GAP: the unknown path is NOT in added_paths (it existed in main but absent from
    #      Veripsa's graph — a real extractor gap). Different reason copy: "extractor gap" wording allowed,
    #      "treat as unknown, not clear" framing, no "NEW in this PR" claim.
    out_gap = render.render_pr_check(_impact_unknown([GAP_PATH]), "PR-U",
                                     added_paths=[])    # empty added_paths → the path is NOT new in this PR
    cmt_gap = out_gap.get("comment") or ""
    check("extractor gap" in cmt_gap or "Not analyzed" in cmt_gap or "un-indexed" in cmt_gap,
          "(B2a) gap-path Unknown: comment uses the extractor-gap / un-indexed wording (the real-defect "
          "framing — a Veripsa coverage limit, not an expected case)")
    check("Treat as unknown, not clear" in cmt_gap or "Treat those as unknown, not clear" in cmt_gap,
          "(B2b) gap-path Unknown: comment says 'Treat as unknown, not clear' (honest-copy: never claim "
          "'clear' about a file Veripsa never looked at)")
    check("NEW in this PR" not in cmt_gap,
          "(B2c) gap-path Unknown: comment does NOT say 'NEW in this PR' (the path EXISTED in main; calling "
          "it new would be wrong)")
    check("computable after merge" not in cmt_gap,
          "(B2d) gap-path Unknown: comment does NOT say 'computable after merge' (the path already exists; "
          "merging this PR will NOT make it analyzable — extractor coverage is the bottleneck)")

    # (b3) DIFFERENT REASON STRINGS — the two Unknown rendered bodies must DIFFER. This is the whole point
    #      of the split: same verdict, distinguishable reason. A regression that re-lumps them would make
    #      these two comments identical (or near-identical) → this assertion guards the split.
    check(cmt_new != cmt_gap,
          "(B3a) new-file Unknown vs gap-path Unknown render to DIFFERENT comment bodies (the split is "
          "preserved — same verdict, distinct reason copy)")
    # And specifically: the distinguishing tokens are in EXACTLY ONE of the two comments, not both.
    check(("NEW in this PR" in cmt_new) and ("NEW in this PR" not in cmt_gap),
          "(B3b) the 'NEW in this PR' token is in EXACTLY the new-file Unknown comment, not the gap one")
    check(("expected, not a warning" in cmt_new) and ("expected, not a warning" not in cmt_gap),
          "(B3c) the 'expected, not a warning' framing is in EXACTLY the new-file Unknown comment, not the "
          "gap one")

    # (b4) BOTH CAUSES on the same PR (one new file + one gap path) — the split renders both paragraphs in
    #      semantic order: the expected (new) case first, the warranted-attention (gap) case second. The
    #      lumped fallback (no added_paths) would have folded them into one paragraph; the split must keep
    #      them as two semantically-distinct facts the customer can act on independently.
    out_both = render.render_pr_check(_impact_unknown([NEW_PATH, GAP_PATH]), "PR-U",
                                      added_paths=[NEW_PATH])
    cmt_both = out_both.get("comment") or ""
    check("NEW in this PR" in cmt_both,
          "(B4a) both-causes Unknown: the new-file paragraph is rendered")
    check("extractor gap" in cmt_both or "un-indexed" in cmt_both or "Not analyzed" in cmt_both,
          "(B4b) both-causes Unknown: the gap-path paragraph is also rendered")
    idx_new = cmt_both.find("NEW in this PR")
    idx_gap = max(cmt_both.find("extractor gap"), cmt_both.find("un-indexed"), cmt_both.find("Not analyzed"))
    check(idx_new >= 0 and idx_gap >= 0 and idx_new < idx_gap,
          "(B4c) both-causes Unknown: the new-file paragraph is rendered BEFORE the gap-path one "
          "(expected case first, warranted-attention case second — preserves the semantic ordering the "
          "PO 2026-06-25 split established)")

    # (b5) LUMPED FALLBACK — when added_paths is None, the renderer falls back to the byte-identical lumped
    #      copy (older call sites: neighbor refresh, fork variant, fetch error). This is the strictly
    #      behavior-preserving leg of the split; assert the fallback still renders SOMETHING about the
    #      unknown paths (so older call sites don't silently drop the honesty note).
    out_lumped = render.render_pr_check(_impact_unknown([GAP_PATH]), "PR-U", added_paths=None)
    cmt_lumped = out_lumped.get("comment") or ""
    check(GAP_PATH in cmt_lumped,
          "(B5a) lumped fallback (added_paths=None): the unknown path is STILL surfaced (the split's "
          "fail-open path preserves honesty even when the renderer wasn't told about added_paths)")
    check("Not analyzed" in cmt_lumped or "unknown" in cmt_lumped.lower(),
          "(B5b) lumped fallback: comment carries the honest-unknown framing")

    # ── done ──────────────────────────────────────────────────────────────────────────────────────────────
    if FAIL == 0:
        print("CONFLICT-MARKER ADVERSARIAL GATE: PASS")
        return 0
    else:
        print("CONFLICT-MARKER ADVERSARIAL GATE: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
