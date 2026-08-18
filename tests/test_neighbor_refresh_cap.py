#!/usr/bin/env python3
"""DoS / cost gate — the LIVE neighbor-refresh fan-out (`_post_refreshes`) must be BOUNDED, so one push-to-main
or one PR open/sync can NEVER fan out into an unbounded GitHub post storm under the held per-(account,repo)
advisory lock.

THE GAP THIS LOCKS (audit P2 — manifested live: 3 rapid merges churned the worker). On EVERY push-to-main
(refresh_inflight) and EVERY PR open/sync, the brain re-renders the verdict for EVERY in-flight change in
core.main_impact_surface (webhook._refresh_changes) and the App POSTS each via _post_refreshes — up to 3 GitHub API
calls per non-clear neighbor (head fetch + check upsert + comment upsert). ALL of that runs while the per-repo
advisory lock is HELD (event_processor: the session lock spans the whole event incl. its GitHub calls). On a busy
monorepo (100s–1000s of overlapping open PRs on a shared foundation) ONE push to main fanned this across ALL of
them → hundreds of posts under a single held lock → that repo's subsequent events serialized behind a multi-minute
post storm. The merge_group / check-rerun replay paths were ALREADY bounded by _RERUN_PR_CAP; this is the matching
bound for the live push-to-main + open/sync neighbor refresh.

THE BOUND: _post_refreshes processes AT MOST server._NEIGHBOR_REFRESH_CAP refresh entries per event (read off
`server` at call time, so a test patch is honored — same contract as _RERUN_PR_CAP). main_impact_surface returns
`changes` in label order (NOT materiality), so this is a DETERMINISTIC bound + a content-free truncation log; the
rest are NOT lost (each in-flight PR re-renders on its OWN next webhook/backfill). The common small-N case
(N <= cap) never trips the cap and is byte-for-byte unchanged.

WHAT THIS ASSERTS (over the REAL production path — _refresh_changes builds the payload, _post_refreshes posts it):
  1. CAPPED: with N (=200) >> cap overlapping in-flight changes, _post_refreshes touches AT MOST cap PRs (not N) —
     so the GitHub round-trips under the held lock are bounded. (The default cap is exercised, then a patched cap.)
  2. CONTENT-FREE + NO CRASH: the truncation log names only counts + the env var — never a PR identity / path /
     code token — and the bounded fan-out never raises.
  3. SMALL-N UNCHANGED: with N <= cap, EVERY neighbor is posted and NO cap log is emitted (byte-identical to the
     pre-cap behaviour for the common case).

Run:  python3 tests/test_neighbor_refresh_cap.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import server                                       # noqa: E402  (the cap lives here; read at call time)
import webhook as W                                 # noqa: E402  (the pure render that builds the refresh payload)
from webhook_handlers import _post_refreshes        # noqa: E402  (the POST chokepoint under test)

FAIL = 0


def check(cond: bool, label: str) -> None:
    global FAIL
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    if not cond:
        FAIL = 1


class _RecordingGH:
    """Just enough of the GitHub client for _post_refreshes. Records every PR whose head it RESOLVED — that head
    fetch is the FIRST of the up-to-3 API calls per neighbor, so 'distinct PRs head-resolved' == the fan-out size
    we must bound. Every neighbor here is a non-clear base-repo PR (not a fork) → it always takes the head fetch."""

    def __init__(self):
        self.head_prs: list[int] = []               # every PR we resolved a head for (the per-neighbor API entry)
        self.posted: dict[int, str] = {}            # PR -> last comment body posted (proves we posted, content-free check)

    def pull_request_head_and_fork(self, repo, number):
        self.head_prs.append(number)
        return (f"sha-{number}", False)             # base-repo PR (not a fork) → takes the full check+comment path

    def pull_request_head(self, repo, number):      # the clear_reset path uses this; unused here (all non-clear)
        self.head_prs.append(number)
        return f"sha-{number}"

    def upsert_check(self, repo, sha, conclusion, title, summary):
        pass

    def create_check(self, *a, **k):
        pass

    def upsert_comment(self, repo, number, marker, body):
        self.posted[number] = body

    def patch_comment_if_exists(self, repo, number, marker, body_fn):
        return False

    def pr_labels(self, repo, number, strict=False):
        return []


def make_impact(n: int) -> dict:
    """A realistic core.main_impact_surface result with N in-flight changes that ALL have something to coordinate
    (a 'serialize'/'warn' verdict → a non-clear refresh entry that costs the full head+check+comment fan-out). Each
    carries a couple of partner rows so render_pr_check walks the real shape. Content-free synthetic (PR refs +
    synthetic paths only); deterministic (no randomness → no flake). PR refs start at 1 (PR-0 → change ref '0',
    which _pr_number_from_change reads as falsy and the pre-existing guard skips — not a property under test)."""
    changes = []
    for i in range(1, n + 1):
        partners = [{"change_id": f"PR-{1 + (i % n)}", "label": f"agent-{1 + (i % n)} PR-{1 + (i % n)}",
                     "agent": f"agent-{1 + (i % n)}", "paths": [f"src/mod_{1 + (i % n)}.py"]}]
        changes.append({
            "change_id": f"PR-{i}",
            "agent": f"agent-{i}",
            "label": f"agent-{i} PR-{i}",
            "verdict": "serialize" if i % 2 == 0 else "warn",
            "paths": [f"src/mod_{i}.py", f"src/util_{i}.py"],
            "impact": [{"path": f"src/dep_{i}.py", "count": 2}],
            "contested_with": partners,
            "serialize_behind": partners,
            "queued_behind_paths": [f"src/mod_{i}.py"],
        })
    return {"repo": "busy/monorepo", "branch": "main", "changes": changes}


def _post(impact: dict, gh: _RecordingGH) -> tuple[int, str]:
    """Drive the REAL path: render the full refresh payload (no DB — pure), then post it through the chokepoint.
    db=None → the pause-ack overlay branch (the only DB reader in _post_refreshes) is skipped, so this stays a
    pure render+post of the fan-out. Returns (touched_count, captured_stdout)."""
    refreshed = W._refresh_changes(impact)          # pure in-memory render of EVERY in-flight change (no cap here)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        touched = _post_refreshes(gh, impact["repo"], refreshed, db=None, branch="main")
    return touched, buf.getvalue()


def main() -> int:
    cap = server._NEIGHBOR_REFRESH_CAP
    check(isinstance(cap, int) and cap >= 1,
          f"server._NEIGHBOR_REFRESH_CAP is a positive int (default {cap}) — env_int fails LOUD on a bad knob")

    N = max(200, cap * 4)                            # N >> cap so the cap MUST bite no matter the configured default

    # ── (1) CAPPED at the DEFAULT cap: N non-clear neighbors → AT MOST cap PRs head-resolved + posted (not N). ──
    impact = make_impact(N)
    rendered = len(W._refresh_changes(impact))      # the pure render is UNBOUNDED (the perf gate relies on this)
    check(rendered == N, f"_refresh_changes still renders ALL {N} in-flight changes (pure render is uncapped) — "
                         f"the bound lives in the POST layer, not the render (got {rendered})")

    gh = _RecordingGH()
    touched, out = _post(impact, gh)
    distinct_heads = len(set(gh.head_prs))
    check(touched <= cap,
          f"_post_refreshes touched AT MOST the cap ({touched} <= {cap}) with N={N} in-flight — the live "
          f"push-to-main / open-sync fan-out is BOUNDED (not {N} posts under the held per-repo lock)")
    check(distinct_heads <= cap,
          f"GitHub head fetches (the first of up-to-3 calls/neighbor) are bounded to <= cap "
          f"({distinct_heads} <= {cap}) — the API storm under the lock is capped, not O(N)")
    check(len(gh.posted) <= cap,
          f"distinct PRs actually commented on is <= cap ({len(gh.posted)} <= {cap})")

    # ── (2) CONTENT-FREE truncation log + no crash. The cap log may name COUNTS + the env var, never an identity. ──
    cap_lines = [ln for ln in out.splitlines() if "capped" in ln]
    check(len(cap_lines) >= 1, "a truncation log line is emitted when the cap trims the fan-out (observable, not silent)")
    forbidden = ("src/", "agent-", "/util_", "/mod_", "/dep_")   # any path/identity token must NOT be in the log
    leaked = [t for ln in cap_lines for t in forbidden if t in ln]
    check(not leaked,
          "the truncation log is CONTENT-FREE (counts + env-var name only; no PR path / label / code token)"
          + ("" if not leaked else f" — LEAKED: {sorted(set(leaked))}"))
    check(all("VERIPSA_NEIGHBOR_REFRESH_CAP" in ln for ln in cap_lines),
          "the truncation log names the env knob so an operator can see WHICH cap fired (actionable)")

    # ── (3) the cap is HONORED when patched on `server` (the same monkeypatch contract _RERUN_PR_CAP uses). ──
    orig = server._NEIGHBOR_REFRESH_CAP
    try:
        server._NEIGHBOR_REFRESH_CAP = 5
        gh5 = _RecordingGH()
        touched5, _ = _post(impact, gh5)
        check(touched5 <= 5 and len(set(gh5.head_prs)) <= 5,
              f"a patched cap (server._NEIGHBOR_REFRESH_CAP=5) is read at CALL time and honored "
              f"(touched={touched5}, heads={len(set(gh5.head_prs))}) — the test/operator knob actually binds")
    finally:
        server._NEIGHBOR_REFRESH_CAP = orig

    # ── (4) BACKGROUND progress mode: deterministic pages cover all N and never cursor past a failed surface. ──
    page_impact = make_impact(12)
    page_refreshes = W._refresh_changes(page_impact)
    ordered_changes = sorted(row["change"] for row in page_refreshes)
    try:
        server._NEIGHBOR_REFRESH_CAP = 5
        cursor = ""
        seen_cursors = []
        total_processed = 0
        for _ in range(3):
            progress = _post_refreshes(
                _RecordingGH(), page_impact["repo"], page_refreshes,
                db=None, branch="main", return_progress=True, after_change=cursor)
            check(isinstance(progress, dict),
                  "background return_progress uses the structured cursor contract (live callers still receive int)")
            total_processed += progress["processed"]
            cursor = progress["cursor"]
            seen_cursors.append(cursor)
        check(total_processed == 12 and seen_cursors == [
            ordered_changes[4], ordered_changes[9], ordered_changes[11]],
            "three deterministic change-label pages cover all 12 neighbors without a dropped cap tail")

        failed_change = ordered_changes[2]
        failed_pr = int(failed_change.split("-", 1)[1])

        class _FailOnceGH(_RecordingGH):
            def upsert_comment(self, repo, number, marker, body):
                if number == failed_pr:
                    raise RuntimeError("simulated page failure")
                return super().upsert_comment(repo, number, marker, body)

        failed_page = _post_refreshes(
            _FailOnceGH(), page_impact["repo"], page_refreshes,
            db=None, branch="main", return_progress=True, after_change="")
        retried_page = _post_refreshes(
            _RecordingGH(), page_impact["repo"], page_refreshes,
            db=None, branch="main", return_progress=True,
            after_change=failed_page["cursor"])
        check(failed_page["errors"] == 1
              and failed_page["cursor"] == ordered_changes[1]
              and failed_page["has_more"] is True
              and retried_page["cursor"] >= failed_change,
              "background cursor stops before the first failed PR and the next page retries it (no error skip)")
    finally:
        server._NEIGHBOR_REFRESH_CAP = orig

    # ── (5) SMALL-N (N <= cap) is BYTE-IDENTICAL: every neighbor posted, NO cap log (the common case is untouched). ──
    small_n = max(1, cap - 1)
    small = make_impact(small_n)
    gh_s = _RecordingGH()
    touched_s, out_s = _post(small, gh_s)
    check(touched_s == small_n,
          f"small-N (N={small_n} <= cap {cap}): ALL {small_n} neighbors are posted (no neighbor silently dropped)")
    check("capped" not in out_s,
          "small-N emits NO truncation log — the common case is byte-for-byte the pre-cap behaviour")

    print("NEIGHBOR REFRESH CAP GATE: " + ("PASS" if FAIL == 0 else "FAIL"))
    return FAIL


if __name__ == "__main__":
    raise SystemExit(main())
