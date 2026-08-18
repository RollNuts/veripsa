#!/usr/bin/env python3
"""NO 'CLEAR TO LAND' BASE-SIGNAL gate — the customer-visible base signal is "Clear", never "Clear to land".

The fixed signal contract has EXACTLY four base signals — Clear / Heads up / Wait in line / Unknown (plus the
Paused overlay and the "Cleared — the earlier overlap has resolved" state-transition). "Clear to land" (and its
past-tense "Cleared to land") is FORBIDDEN as a base-signal NAME: it read as a fifth verdict on the always-visible
check title, the PR-comment bold header, and the one-line summary lead — including a clear-with-co-signal PR
whose title previously split to "Veripsa — Clear to land".

This gate drives the REAL render_pr_check / cleared_comment_body (pure, offline — no Postgres, no network) across
EVERY path that renders a clear/cleared customer surface and asserts the forbidden phrase appears on NONE of them:

  (A) clear-with-co-signal — each of the four co-signals that flip the non-absolute lead:
      queued_behind (lane holder) · shared_foundation · cluster size >= 2 · co-change;
  (B) absolute clear (no co-signal) — the "nothing else in flight touches this" lead;
  (C) fork-REDACTED clear — the redacted one-line summary AND the redacted PR comment (a fork PR posts on the
      base-repo conversation the external contributor can read; the redaction must stay content-free);
  (D) the cleared-TRANSITION comment (cleared_comment_body).

The state-transition HEADING "Cleared — the earlier overlap has resolved" is EXPLICITLY allowed (it is a
transition sentence, not a base verdict) and is asserted PRESENT — the ban is on "clear to land" / "cleared to
land", never on the transition heading's bare word "Cleared".

Run:  python3 tests/test_no_clear_to_land_base_signal.py   (PURE / offline)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import render as R  # noqa: E402

FAIL = 0

# The forbidden base-signal name in every casing we render (title/header are title-case, prose is lower-case).
FORBIDDEN = ("clear to land", "cleared to land")
# The ONE allowed use of the word "Cleared": the state-transition heading (NOT a base verdict).
ALLOWED_TRANSITION_HEADING = "Cleared — the earlier overlap has resolved"

BASE = {"repo": "acme/app", "branch": "main"}


def chk(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


def _has_forbidden(text: str | None) -> str | None:
    """Return the forbidden phrase found in `text` (case-insensitive), or None. Content preserved verbatim."""
    low = (text or "").lower()
    for f in FORBIDDEN:
        if f in low:
            return f
    return None


def _header_line(comment: str | None) -> str:
    """The bold verdict header line — the 2nd line, right under '### Veripsa — heading to ...'."""
    parts = (comment or "").split("\n")
    return parts[1] if len(parts) > 1 else (comment or "")


def _scan_surfaces(desc: str, out: dict) -> None:
    """Assert the forbidden phrase is on NONE of the always-visible surfaces of a rendered check:
    the check TITLE, the one-line SUMMARY (its lead), the comment HEADER, and the FULL comment body."""
    title = out.get("title") or ""
    summary = out.get("summary") or ""
    comment = out.get("comment") or ""
    header = _header_line(comment)
    for surface_name, text in (("title", title), ("summary lead", summary),
                               ("comment header", header), ("comment body", comment)):
        found = _has_forbidden(text)
        chk(found is None, f"[{desc}] no '{found or 'clear to land'}' in the {surface_name} (got: {text[:80]!r})")
    # TITLE ENUM: the base-signal segment of the title is EXACTLY the base signal "Clear" — never a fifth verdict.
    if title:
        base_signal = title.replace("Veripsa —", "").split(" — ")[0].strip()
        chk(base_signal == "Clear",
            f"[{desc}] the title's base-signal segment is exactly 'Clear' (got: {base_signal!r})")


def main() -> int:
    # ── (A) clear-with-co-signal — all four co-signals that flip to the non-absolute lead ──────────────────────
    co_signals = {
        "clear+queued_behind (lane holder)": {
            "change_id": "PR-1", "label": "a PR-1", "agent": "a", "verdict": "clear", "paths": ["x.py"],
            "queued_behind": ["b PR-7"], "queued_behind_paths": ["x.py"],
        },
        "clear+shared_foundation": {
            "change_id": "PR-2", "label": "a PR-2", "agent": "a", "verdict": "clear", "paths": ["core.py"],
            "shared_foundation": [{"path": "core.py", "fan_in": 40, "churn": 20}],
        },
    }
    for desc, row in co_signals.items():
        out = R.render_pr_check({**BASE, "changes": [row]}, row["change_id"])
        chk(out.get("comment") is not None, f"[{desc}] precondition: a co-signal clear posts a comment")
        _scan_surfaces(desc, out)

    # cluster size >= 2 co-signal (the change sits in a contention neighborhood). NB a cluster-ONLY clear flips
    # the title/summary to the non-absolute lead but posts NO comment (the comment gate needs queued_behind /
    # shared_foundation / cochange / truncated) — so we assert the co-signal WORDING fired, not a comment.
    cluster_imp = {**BASE, "changes": [
        {"change_id": "PR-3", "label": "a PR-3", "agent": "a", "verdict": "clear", "paths": ["m.py"]}],
        "clusters": [{"changes": ["PR-3", "PR-8"], "size": 2, "suggested_order": ["a PR-3", "b PR-8"]}]}
    out_cl = R.render_pr_check(cluster_imp, "PR-3")
    chk("nothing is blocking you" in (out_cl.get("summary") or "").lower(),
        "[clear+cluster>=2] precondition: the cluster co-signal flipped the summary to the non-absolute lead")
    _scan_surfaces("clear+cluster>=2", out_cl)

    # co-change co-signal (a strong empirical coupling on an otherwise-clear PR)
    cc = [{"edited": "backend/auth.py", "partner": "backend/api.py", "prob": 0.73, "lift": 4.5, "co": 11, "n": 15}]
    cc_imp = {**BASE, "changes": [
        {"change_id": "PR-4", "label": "a PR-4", "agent": "a", "verdict": "clear", "paths": ["backend/auth.py"]}]}
    out_cc = R.render_pr_check(cc_imp, "PR-4", cochange=cc)
    chk(out_cc.get("comment") is not None, "[clear+cochange] precondition: a co-change clear posts a comment")
    _scan_surfaces("clear+cochange", out_cc)

    # ── (B) absolute clear (NO co-signal) — the "nothing else in flight touches this" lead ─────────────────────
    out_abs = R.render_pr_check({**BASE, "changes": [
        {"change_id": "PR-5", "label": "a PR-5", "agent": "a", "verdict": "clear", "paths": ["y.py"]}]}, "PR-5")
    _scan_surfaces("absolute clear", out_abs)
    # the absolute clear lead is PRESERVED (regression guard — it must still read "Clear ...").
    chk("Clear" in (out_abs.get("summary") or "") and _has_forbidden(out_abs.get("summary")) is None,
        "[absolute clear] the absolute clear summary still reads 'Clear ...' and is not 'Clear to land'")

    # ── (C) fork-REDACTED clear — the redacted summary AND the redacted comment (holder so it still posts) ─────
    fork_imp = {**BASE, "changes": [
        {"change_id": "PR-9", "label": "ext PR-9", "agent": "ext", "verdict": "clear", "paths": ["z.py"],
         "queued_behind": ["b PR-7"], "queued_behind_paths": ["z.py"]}]}
    out_fork = R.render_pr_check(fork_imp, "PR-9", is_fork=True)
    chk(out_fork.get("comment") is not None, "[fork clear] precondition: a fork clear holder still posts a comment")
    _scan_surfaces("fork-redacted clear", out_fork)
    # a plain fork clear (nothing nearby) still renders a clean redacted SUMMARY (no comment posted).
    out_fork2 = R.render_pr_check({**BASE, "changes": [
        {"change_id": "PR-10", "label": "ext PR-10", "agent": "ext", "verdict": "clear", "paths": ["q.py"]}]},
        "PR-10", is_fork=True)
    chk(_has_forbidden(out_fork2.get("summary")) is None,
        f"[fork plain clear] no 'clear to land' in the redacted summary (got: {out_fork2.get('summary')!r})")
    # FORK REDACTION preserved: the redacted surfaces leak NO base-repo PR ref / path / other author / order.
    fork_blob = " ".join(str(out_fork.get(k) or "") for k in ("title", "summary", "comment"))
    leaks = [t for t in ("PR-7", " b ", "queued") if t in fork_blob]
    chk("PR-7" not in fork_blob and "z.py" not in fork_blob,
        f"[fork redaction] the redacted clear surfaces leak NO base-repo PR ref / path (found: {leaks})")

    # ── (D) the cleared-TRANSITION comment ─────────────────────────────────────────────────────────────────────
    cleared = R.cleared_comment_body(branch="main")
    chk(_has_forbidden(cleared) is None,
        f"[cleared transition] no 'clear to land' / 'cleared to land' anywhere in the cleared comment body")
    # the ALLOWED transition heading is present + explicitly whitelisted (the ban never touches this sentence).
    chk(ALLOWED_TRANSITION_HEADING in cleared,
        "[cleared transition] the allowed state-transition heading 'Cleared — the earlier overlap has resolved' "
        "is present (whitelisted — the word 'Cleared' as a transition is NOT banned)")

    print("NO-CLEAR-TO-LAND BASE-SIGNAL GATE:", "PASS" if FAIL == 0 else "FAIL")
    return FAIL


if __name__ == "__main__":
    sys.exit(main())
