#!/usr/bin/env python3
"""COMMENT-SIZE-CAP gate — the customer-facing PR comment must ALWAYS fit GitHub's 65536-char comment limit,
with THIS PR's own row visible, deterministic, content-free. PURE + OFFLINE (no DB, no network, no deploy).

Premise (audit:render-size 2026-06-18): render_pr_check assembles the PR comment from ~10 independent sections.
Each per-item LOOP is already capped to LIST_LINE_CAP LINES (and _fmt_list/_fmt_agents cap their inline lists).
But those caps bound the NUMBER of items, not:
  * the byte LENGTH of any ONE item — a single pathological label/path (a 10 KB crafted PR title, a deep monorepo
    path) renders as ONE giant line, so a handful of capped rows still clear 65536 by themselves; and
  * the AGGREGATE across all the sections at once — a busy PR lights up shared-foundation + depends-on-changing +
    queued-behind + blast-radius + the cluster land-order together, and even all-short lines can sum past the cap.
Either way the App's POST 422s and the customer gets NO Veripsa comment on exactly the BUSIEST, most-collision-
prone PR (a huge contention cluster) — the opposite of the intent. The final assembly-level net (_cap_comment_body
+ _clamp_line, COMMENT_BODY_CAP) must clamp the WHOLE body once, AFTER assembly, while ALWAYS keeping THIS PR's
own land-order row (so the developer can still see WHERE they land), the header framing, and the footer.

This gate drives a HUGE contention cluster (thousands of entangled PRs, pathological label/path lengths) through
the REAL render_pr_check and asserts: body ≤ cap, THIS PR's row visible (at the head AND when it sits deep past
the shown slice), deterministic (same input → byte-identical output), content-free (no code body leaks), and that
a NORMAL small comment is returned UNCHANGED (the cap is a safety net, not a reshaper of the common case).

Run:  python3 tests/test_comment_size_cap.py     (no DB needed)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import render as R  # noqa: E402

# GitHub's hard cap on a PR/issue comment body. render keeps a margin under this (COMMENT_BODY_CAP) for the
# upsert marker the poster wraps the body in; the test asserts BOTH the real GitHub cap and render's own margin.
GITHUB_COMMENT_CAP = 65536
FAIL = 0


def check(cond: bool, label: str):
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _huge_cluster_impact(n: int, label_len: int, me_index: int, path_depth: int = 1) -> tuple[dict, str]:
    """A pathological contention neighborhood: `n` entangled in-flight PRs, each label `label_len` chars long, a
    deep reserved path, and a verdict that lights up every section at once. `me_index` places THIS PR's row."""
    order = [("x" * label_len) + f"-{i:06d}" for i in range(n)]
    me = order[me_index]
    deep_path = "src/" + ("d/" * path_depth) + "mod.py"
    change = {
        "change_id": me,
        "label": me,
        "verdict": "warn",
        "paths": [deep_path],
        "impact": [deep_path] * 20,                      # blast-radius section (bounded inline by _fmt_list)
        "contested_with": order[: min(50, n)],           # warn coordinate section
        "shared_foundation": [{"path": deep_path, "fan_in": 99} for _ in range(20)],
        "depends_on_changing": [{"path": deep_path, "by": order[(me_index + 1) % n]} for _ in range(20)],
        "queued_behind": order[: min(30, n)],
        "queued_behind_paths": [deep_path] * 5,
    }
    impact = {
        "repo": "acme/huge",
        "branch": "main",
        "changes": [change],
        "clusters": [{"size": n, "changes": order, "agents": order, "suggested_order": order}],
    }
    return impact, me


def test_huge_cluster_long_labels_fit_cap():
    """Thousands of PRs with 10 KB labels each: the body must still fit the cap (the line caps bound COUNT, this
    bounds LENGTH)."""
    for me_index, where in ((0, "at the head of the order"),
                            (4999, "buried deep past the shown slice")):
        impact, me = _huge_cluster_impact(n=5000, label_len=10_000, me_index=me_index)
        out = R.render_pr_check(impact, me)
        body = out["comment"] or ""
        check(len(body) <= GITHUB_COMMENT_CAP,
              f"comment fits GitHub's 65536 cap with this PR {where} (len={len(body)})")
        check(len(body) <= R.COMMENT_BODY_CAP,
              f"comment honors render's own margin (COMMENT_BODY_CAP={R.COMMENT_BODY_CAP}, len={len(body)})")
        check(R._THIS_PR_MARKER in body,
              f"THIS PR's land-order row stays visible even when it sits {where}")


def test_this_pr_row_always_visible_past_cap():
    """When THIS PR sits FAR past the shown slice in a giant order, its true-position row must still be surfaced
    (the customer must always be able to see WHERE they land), and the body must still fit."""
    impact, me = _huge_cluster_impact(n=3000, label_len=4000, me_index=2999)
    out = R.render_pr_check(impact, me)
    body = out["comment"] or ""
    check(R._THIS_PR_MARKER in body, "this PR's row is surfaced even buried at position 3000/3000")
    # the true 1-based position appears on the marked row (deterministic position, never renumbered)
    marked = [ln for ln in body.splitlines() if R._THIS_PR_MARKER in ln]
    check(any(ln.lstrip().startswith("3000.") for ln in marked),
          f"the marked row carries this PR's TRUE 1-based position (3000.), not a renumbered one")
    check(len(body) <= GITHUB_COMMENT_CAP, f"buried-this-PR body still fits the cap (len={len(body)})")


def test_single_giant_line_clamped():
    """One pathological 50 KB label (a single line longer than the whole comment cap) must be clamped, not emitted
    whole — driven through the REAL render, AND the marker on this PR's huge label must survive the clamp."""
    impact, me = _huge_cluster_impact(n=60, label_len=50_000, me_index=59)
    out = R.render_pr_check(impact, me)
    body = out["comment"] or ""
    check(len(body) <= GITHUB_COMMENT_CAP, f"a 50 KB-label PR comment fits the cap (len={len(body)})")
    check(R._THIS_PR_MARKER in body, "this PR's marker survives clamping even on a 50 KB label")
    check(all(len(ln) <= R.COMMENT_LINE_CAP + 8 for ln in body.splitlines()),
          "no single rendered line exceeds the per-line clamp (a 50 KB line can't slip through)")


def test_body_elision_branch_keeps_frame():
    """Directly exercise the body-elision branch (_cap_comment_body): MANY moderate lines that each survive the
    per-line clamp but AGGREGATE past the cap. Header, footer, this-PR row kept; ONE content-free elision notice;
    result under cap; deterministic; idempotent below the cap."""
    lines = ["### Veripsa — heading to `main`", "**⚠ Heads up**", ""]
    lines += ["> filler " + ("q" * 1500) + f" #{i}" for i in range(120)]
    lines.append("42. **" + ("w" * 1500) + "** " + R._THIS_PR_MARKER)
    lines += ["> trailer " + ("r" * 1500) + f" @{i}" for i in range(30)]
    lines += ["", "<sub>Veripsa records what is heading to `main` — advisory.</sub>"]

    out = R._cap_comment_body(lines, cap=R.COMMENT_BODY_CAP)
    check(len(out) <= R.COMMENT_BODY_CAP, f"aggregated body is clamped under the cap (len={len(out)})")
    check(out.startswith("### Veripsa"), "header framing is kept at the top")
    check(out.rstrip().endswith("</sub>"), "footer advisory line is kept at the bottom")
    check(R._THIS_PR_MARKER in out, "this PR's row is kept through the elision")
    check("elided" in out, "a content-free elision notice replaces the dropped middle")
    check(out == R._cap_comment_body(lines, cap=R.COMMENT_BODY_CAP), "capping is deterministic (byte-identical)")
    small = ["### Veripsa", "**x**", "<sub>y</sub>"]
    check(R._cap_comment_body(small, cap=R.COMMENT_BODY_CAP) == "\n".join(small),
          "a small body is returned UNCHANGED (the cap is a net, not a reshaper of the common case)")


def test_normal_comment_unchanged():
    """The common case must pay NOTHING: a normal warn PR's comment is identical with and without the cap path —
    the net only fires on the pathological. (Asserts the cap didn't silently truncate a healthy comment.)"""
    order = [f"PR-{i}" for i in range(4)]
    me = order[1]
    impact = {
        "repo": "acme/x", "branch": "main",
        "changes": [{"change_id": me, "label": me, "verdict": "warn",
                     "paths": ["src/a.py"], "impact": ["src/b.py"], "contested_with": [order[0]]}],
        "clusters": [{"size": 4, "changes": order, "agents": order, "suggested_order": order}],
    }
    out = R.render_pr_check(impact, me)
    body = out["comment"] or ""
    check(0 < len(body) <= R.COMMENT_BODY_CAP, "a normal warn comment is produced and well under the cap")
    check("elided" not in body, "a normal comment carries NO elision notice (the net did not fire)")
    check(R._THIS_PR_MARKER in body, "a normal cluster comment marks this PR's row")


def test_content_free_no_body_leak():
    """Content-free contract holds through the cap path: a SECRET-looking token placed ONLY in a (hostile)
    multi-line label must never reach the rendered body — collapsed/escaped, not leaked as a code body run."""
    secret = "SUPER_SECRET_TOKEN_zzz999"
    hostile = "alice\n" + secret + "\n" + ("x" * 9000)   # a multi-line, secret-bearing label
    order = [hostile + f"-{i}" for i in range(2000)]
    me = order[1500]
    impact = {
        "repo": "acme/x", "branch": "main",
        "changes": [{"change_id": me, "label": me, "verdict": "warn",
                     "paths": ["src/a.py"], "impact": [], "contested_with": []}],
        "clusters": [{"size": 2000, "changes": order, "agents": order, "suggested_order": order}],
    }
    out = R.render_pr_check(impact, me)
    body = out["comment"] or ""
    check(len(body) <= GITHUB_COMMENT_CAP, f"hostile-label cluster still fits the cap (len={len(body)})")
    # the secret token may appear AS PART of a collapsed single-line label, but never as a NEWLINE-bearing body run
    check("\n" + secret + "\n" not in body, "no raw multi-line body run carrying the secret leaks into the comment")
    check(R._THIS_PR_MARKER in body, "this PR's row is still visible under a hostile-label cluster")


def main():
    print("=== COMMENT-SIZE-CAP gate (customer comment ≤ 65536, this-PR visible, deterministic, content-free) ===")
    print("-- huge cluster, 10 KB labels: body fits the cap, this PR's row visible (head + buried) --")
    test_huge_cluster_long_labels_fit_cap()
    print("-- this PR buried deep past the shown slice: true-position row still surfaced --")
    test_this_pr_row_always_visible_past_cap()
    print("-- one 50 KB label (a single line over the whole cap) is clamped, marker survives --")
    test_single_giant_line_clamped()
    print("-- body-elision branch keeps header/footer/this-PR row + one content-free notice --")
    test_body_elision_branch_keeps_frame()
    print("-- a normal comment is unchanged (the cap is a net, not a reshaper) --")
    test_normal_comment_unchanged()
    print("-- content-free holds through the cap path (no multi-line body run leaks) --")
    test_content_free_no_body_leak()
    print("------------------------------------------------------------")
    if FAIL == 0:
        print("COMMENT-SIZE-CAP GATE: PASS")
        return 0
    print("COMMENT-SIZE-CAP GATE: FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
