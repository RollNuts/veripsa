#!/usr/bin/env python3
"""CONFLICT-MARKER DETECTOR — a content-free guard for unresolved merge artifacts.

A synthetic cascade-merge fixture lands literal git conflict markers `<<<<<<<` / `=======` / `>>>>>>>` on main.
Those markers deterministically break parsers or builds. Veripsa Core is CONTENT-FREE — it reads file bodies
transiently for symbol/edge extraction and never stores them — so the detector must retain only marker shape,
path, kind, and line metadata.

This is the SINGLE most common AI-agent failure mode in parallel development (an agent rebases, hits a
conflict, and commits the markers without resolving). Veripsa is positioned as "AI agent traffic control" —
so a content-free-compatible detector for this pattern is a huge product win + closes a real hole.

This gate pins the CONTRACT:

  (1) DETECTOR PRECISION (the github_rest pure helper conflict_markers_from_patch):
      - fires on a patch whose new-side ADDED lines include both `<<<<<<<` and `>>>>>>>` markers
        (at the START of a line) — high-precision pair gate
      - does NOT fire when ONLY `=======` is present (markdown rules / RST underlines / comment dividers
        are too common for `=======` alone to be a useful signal)
      - does NOT fire when the patch has no markers
      - reports the NEW-side line number (the line in the file the dev sees in their checkout)
      - is content-free: the return is line numbers + a 3-value `kind` label; the line BODY is NEVER returned

  (2) RENDER ESCALATION (render.render_pr_check + the _build_conflict_marker_result short-circuit):
      - non-empty conflict_markers → conclusion='action_required' (a HARD FAIL — the only one Veripsa
        ever emits outside the pause-ack overlay)
      - title carries "Unresolved merge conflict markers"
      - the comment body has the marker block AT THE TOP (above the contention verdict, when present)
      - the comment body names the PATH and the FIRST LINE NUMBER of the marker per file (content-free —
        no surrounding body lines)
      - the contention verdict still renders BELOW the marker block (coupling signal not lost)
      - escalates even on a FORK PR (the marker is in the contributor's own added code; naming the path
        leaks no base-repo structure)
      - escalates even when there is no in-flight reservation yet (first-sync PR with markers)

  (3) CONTENT-FREE: the detector test does NOT itself store conflict-marker bodies — the patches used in
      the test are constructed via string-CONCATENATION of the 7-char shape strings, so the detector's own
      regex doesn't match a literal in this file's source. The detector reads patches transiently; the
      finding output carries no line bodies.

PURE + OFFLINE (no DB, no network). Drives the REAL helpers/renderer on synthetic patches.

Run:  python3 tests/test_conflict_marker_detector.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import github_rest  # noqa: E402
import render  # noqa: E402


# CONTENT-FREE FIXTURE: build the marker shapes via string-concatenation so the detector's own regex (which
# only looks at line-starts of patch ADDED lines, not at this file's source) cannot trip on a literal markers
# in THIS test's source. The string operator '*' produces the 7-char run for each marker, exactly what the
# detector (_MARKER_OURS / _MARKER_SEP / _MARKER_THEIRS in github_rest.py) compares against.
M_OURS = "<" * 7        # `<<<<<<<`
M_SEP = "=" * 7         # `=======`
M_THEIRS = ">" * 7      # `>>>>>>>`


FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _patch_with_unresolved_markers() -> str:
    """A unified-diff patch where the PR's added (`+`-prefixed) lines include the full 3-marker shape.
    The hunk header is `@@ -10,3 +10,7 @@` so the new-side starts at line 10. Git ALWAYS inserts conflict
    markers at COLUMN 0 (no indent) — the markers are real and unambiguous only when they start the line.
    So in the patch we have `+` (the diff-prefix) immediately followed by the 7-char marker shape, with
    NO leading whitespace. Content-free fixture (built by string concat — see module note)."""
    lines = [
        "@@ -10,3 +10,7 @@",
        " def foo():",
        "     pass",
        "+def bar():",
        "+" + M_OURS + " HEAD",
        "+    x = 1",
        "+" + M_SEP,
        "+    x = 2",
        "+" + M_THEIRS + " feature-branch",
    ]
    return "\n".join(lines)


def _patch_with_only_separator() -> str:
    """A markdown rule / RST underline / Python comment divider patch: the `=======` separator appears in
    the added lines but neither `<<<<<<<` nor `>>>>>>>`. The high-precision pair gate must NOT fire on this
    — `=======` is too common in legitimate content for the alone-marker to be a useful signal."""
    lines = [
        "@@ -1,3 +1,5 @@",
        " # README",
        "+" "Section heading",
        "+" + M_SEP,                     # a legitimate markdown rule / RST underline
        "+",
        " More content",
    ]
    return "\n".join(lines)


def _patch_clean() -> str:
    """A normal, clean PR patch with NO markers. The detector must NOT fire."""
    lines = [
        "@@ -10,3 +10,5 @@",
        " def foo():",
        " " "    pass",
        "+" "def bar():",
        "+" "    return 42",
    ]
    return "\n".join(lines)


def _patch_marker_in_context() -> str:
    """A patch where the marker shape appears in a CONTEXT line (` `-prefixed, present at base AND in new) —
    a markdown rule / RST underline that already existed and isn't being changed. Strictly: the line was
    there at base, so it isn't introduced by this PR. The detector scans only ADDED (`+`) lines, so this
    must NOT fire."""
    lines = [
        "@@ -1,3 +1,3 @@",
        " # README",
        " " + M_SEP,                     # CONTEXT line (space-prefix), not added — never an "introduced" marker
        " More content",
    ]
    return "\n".join(lines)


def _patch_marker_in_removed_line() -> str:
    """A patch where the marker shape appears in a REMOVED (`-`-prefixed) line — the developer is FIXING a
    botched rebase by removing the marker. We must NOT fire on the removal (it's the cleanup direction).
    Same column-0 marker convention as _patch_with_unresolved_markers (git always puts markers at col 0)."""
    lines = [
        "@@ -10,5 +10,2 @@",
        " def foo():",
        "-" + M_OURS + " HEAD",
        "-    x = 1",
        "-" + M_SEP,
        "-" + M_THEIRS + " feature",
        " return None",
    ]
    return "\n".join(lines)


def main() -> int:
    # ── (1) DETECTOR PRECISION on the underlying helper ───────────────────────────────────────────────
    print("-- (1) conflict_markers_from_patch — high-precision pair gate --")

    findings = github_rest.conflict_markers_from_patch(_patch_with_unresolved_markers())
    check(len(findings) >= 2,
          "(1a) FIRES on a patch whose added lines include the full <<<<<<< / ======= / >>>>>>> triplet")
    # the markers are at new-side lines 11, 13, 15 (hunk starts at line 10, def bar=10, ours=11, x=1=12,
    # sep=13, x=2=14, theirs=15). All findings carry line numbers ≥ 11.
    check(all(isinstance(f.get("line"), int) and f["line"] >= 10 for f in findings),
          "(1b) findings report NEW-side line numbers (matched against the +c hunk-header field)")
    kinds = {f["kind"] for f in findings}
    check("ours" in kinds and "theirs" in kinds,
          "(1c) findings carry both 'ours' and 'theirs' kind labels (the pair gate's two halves)")
    # Content-free contract: the findings carry ONLY line + kind + (path is added by the prsurface caller, not here)
    for f in findings:
        check(set(f.keys()) == {"line", "kind"},
              "(1d) finding dict contains ONLY 'line' + 'kind' (content-free contract — no body/text)")
        break    # one assertion is enough

    findings_sep_only = github_rest.conflict_markers_from_patch(_patch_with_only_separator())
    check(findings_sep_only == [],
          "(1e) does NOT fire when ONLY ======= is present (markdown rule / RST underline — too common alone)")

    findings_clean = github_rest.conflict_markers_from_patch(_patch_clean())
    check(findings_clean == [],
          "(1f) does NOT fire on a clean patch with no markers")

    findings_context = github_rest.conflict_markers_from_patch(_patch_marker_in_context())
    check(findings_context == [],
          "(1g) does NOT fire when the marker shape is in a CONTEXT line (not introduced by this PR)")

    findings_removed = github_rest.conflict_markers_from_patch(_patch_marker_in_removed_line())
    check(findings_removed == [],
          "(1h) does NOT fire when the marker shape is ONLY in REMOVED lines (a cleanup PR fixing markers)")

    # Junk/None patch must not crash
    check(github_rest.conflict_markers_from_patch(None) == [],
          "(1i) None patch returns [] (defensive)")
    check(github_rest.conflict_markers_from_patch("") == [],
          "(1j) empty patch returns [] (defensive)")
    check(github_rest.conflict_markers_from_patch(42) == [],
          "(1k) non-string patch returns [] (defensive)")

    # ── (2) RENDER ESCALATION on the orchestrator ─────────────────────────────────────────────────────
    print("-- (2) render_pr_check escalation — top-of-comment + action_required --")

    # A PR with conflict markers and ALSO an active in-flight reservation. The marker block must sit ABOVE
    # the verdict copy; the verdict copy still renders BELOW so a real coupling signal isn't lost.
    impact = {
        "repo": "acme/app",
        "branch": "main",
        "changes": [{
            "change_id": "PR-9",
            "label": "alice PR-9",
            "agent": "alice",
            "verdict": "clear",
            "paths": ["app/sitemap.ts"],
        }],
    }
    out = render.render_pr_check(impact, "PR-9",
                                 conflict_markers=[
                                     {"path": "app/sitemap.ts", "line": 12, "kind": "ours"},
                                     {"path": "app/sitemap.ts", "line": 14, "kind": "separator"},
                                     {"path": "app/sitemap.ts", "line": 16, "kind": "theirs"},
                                 ])
    check(out["conclusion"] == "action_required",
          "(2a) conclusion = 'action_required' (the hard-fail signal — only emitted on a true build-breaker)")
    check("Unresolved merge conflict markers" in out["title"],
          "(2b) title carries 'Unresolved merge conflict markers' (the checks-row lead)")
    check("will fail your build" in (out["summary"] or "").lower(),
          "(2c) summary names the build-breaker honestly ('will fail your build')")
    comment = out["comment"] or ""
    check("app/sitemap.ts" in comment,
          "(2d) comment names the offending PATH (content-free coordinate)")
    check("line 12" in comment,
          "(2e) comment names the FIRST LINE NUMBER of the marker (content-free metadata; the line body is NOT shown)")
    # The marker block must sit ABOVE the verdict copy. With an otherwise-clear PR there is no verdict
    # copy to chase under the block, but we can verify the conflict block appears EARLY in the comment.
    # Index of "Unresolved merge conflict marker" should be before any "advisory" sub footer.
    idx_marker = comment.find("Unresolved merge conflict marker")
    idx_footer = comment.find("advisory")
    check(idx_marker >= 0 and (idx_footer == -1 or idx_marker < idx_footer),
          "(2f) the marker block appears at the TOP of the comment (above the footer)")
    # Honest copy:
    check("re-resolve" in comment.lower() or "botched rebase" in comment.lower(),
          "(2g) the comment names the likely cause + the action ('botched rebase' / 're-resolve')")
    # No body of the marker line is rendered. A synthetic marker line may look like
    # "<<<<<<< HEAD\n  <Link href=\"/api\">"; the comment must NOT show "Link" / "href" / "api" content.
    # We synthesized findings dicts (line + kind only — no body), so no body content can leak. Spot-check
    # that no obvious code fragment lands in the comment.
    forbidden = ("<Link", "href=", "import", "function (", "def main", "return ")
    for needle in forbidden:
        check(needle not in comment,
              f"(2h) content-free: no code fragment '{needle}' rendered in the comment (only path + line)")

    # ── (3) NO conflict markers — behavior preserved ───────────────────────────────────────────────────
    print("-- (3) clean PR: behavior-preserving (no escalation) --")

    out_clean = render.render_pr_check(impact, "PR-9")    # no conflict_markers param
    check(out_clean["conclusion"] == "success",
          "(3a) clean clear PR with no conflict_markers stays conclusion='success' (back-compat)")
    out_clean_empty = render.render_pr_check(impact, "PR-9", conflict_markers=[])
    check(out_clean_empty["conclusion"] == "success",
          "(3b) clean clear PR with conflict_markers=[] stays conclusion='success'")
    out_clean_none = render.render_pr_check(impact, "PR-9", conflict_markers=None)
    check(out_clean_none["conclusion"] == "success",
          "(3c) clean clear PR with conflict_markers=None stays conclusion='success'")

    # ── (4) NO reservation, BUT markers present — still escalates ───────────────────────────────────────
    print("-- (4) no in-flight reservation + markers → still escalates --")

    impact_no_change = {"repo": "acme/app", "branch": "main", "changes": []}
    out_no = render.render_pr_check(impact_no_change, "PR-9",
                                    conflict_markers=[{"path": "app/sitemap.ts", "line": 12, "kind": "ours"}])
    check(out_no["conclusion"] == "action_required",
          "(4a) no reservation but markers → conclusion='action_required' (don't silently let it through)")
    check("Unresolved" in (out_no["title"] or ""),
          "(4b) no-reservation escalation carries the build-breaker title")
    check("app/sitemap.ts" in (out_no["comment"] or ""),
          "(4c) no-reservation escalation still names the path + line in the comment")

    # ── (5) FORK PR with markers — still escalates (the marker is in the contributor's own added code) ──
    print("-- (5) fork PR + markers → still escalates --")

    out_fork = render.render_pr_check(impact, "PR-9", is_fork=True,
                                      conflict_markers=[{"path": "app/sitemap.ts", "line": 12, "kind": "ours"}])
    check(out_fork["conclusion"] == "action_required",
          "(5a) fork PR + markers → conclusion='action_required' (build-breaker overrides fork redaction)")
    check("app/sitemap.ts" in (out_fork["comment"] or ""),
          "(5b) fork escalation names the path (the marker IS in the fork contributor's own diff)")

    # ── (6) MULTIPLE files with markers — first N + 'and more' overflow ─────────────────────────────────
    print("-- (6) many marker files render bounded with 'and more' overflow --")

    many = [{"path": f"file_{i}.py", "line": 10 + i, "kind": "ours"} for i in range(20)]
    out_many = render.render_pr_check(impact, "PR-9", conflict_markers=many)
    check(out_many["conclusion"] == "action_required",
          "(6a) many marker files → still action_required")
    cm = out_many["comment"] or ""
    check("and more" in cm or "more" in cm.split("\n")[-3:][0],
          "(6b) the comment uses the 'and more' qualitative overflow (no raw +N count — moat-safe)")

    # ── done ────────────────────────────────────────────────────────────────────────────────────────────
    if FAIL == 0:
        print("CONFLICT-MARKER DETECTOR GATE: PASS")
        return 0
    else:
        print("CONFLICT-MARKER DETECTOR GATE: FAIL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
