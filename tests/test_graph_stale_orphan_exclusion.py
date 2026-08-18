#!/usr/bin/env python3
"""Graph-freshness alert — ORPHAN/PHANTOM-COORDINATE exclusion lock (pure: no DB, no network, a fake poster).

THE DEFECT THIS PINS (audit): the operator `graph_stale` alert (alerts.evaluate_graph_freshness) reads the
App's per-coordinate freshness (the stored main-graph sha vs main HEAD, per coordinate). A single (repo, branch)
can have MORE THAN ONE stored coordinate:
  • the LIVE coordinate — the account the webhook actually addresses; predictions run on it; it tracks HEAD.
  • an ABANDONED/ORPHAN duplicate — a graph_version written into a DEAD account (a refresh_demo `backfill`
    that wrote into an ACCT-GH- orphan the webhook never re-addresses, or a renamed-away coordinate). No PR
    will ever touch that dead account, so the per-PR self-heal can NEVER fix it: the orphan is BEHIND main
    HEAD *forever*.

Before the fix, the orphan paged `graph_stale` every interval — an un-actionable flood for a condition that
cannot be acted on (there is nothing to heal; the LIVE coordinate is already current). A flood gets MUTED, and a
muted alert is a DEAF alert: a LATER, REAL drift on another repo is then silently missed. A false page begets a
missed page — the worst alerting failure mode (the same lesson as the perpetual orphan `graph_stale` in CLAUDE.md).

THE CONTRACT THIS LOCKS (surgical, content-free):
  • A (repo, branch) that has ANY CURRENT (behind=False) record is being tracked FRESH → a behind=True record
    for that SAME (repo, branch) is a stale orphan/duplicate, NOT live drift → it does NOT page.
  • A behind=True coordinate whose (repo, branch) has NO fresh sibling is GENUINE drift → it STILL pages
    (the exclusion is keyed on repo+branch, never a blanket silence — a real missed push is never hidden).
  • The exclusion is per (repo, branch): a fresh `main` says NOTHING about a stale `release` branch of the
    same repo — each branch is its own coordinate and pages independently.
  • Empty/unusable repo+branch metadata never silences anything (we cannot prove a fresh sibling → keep it).
  • Content-free + fail-open are preserved: only counts cross the boundary, a garbage sample never raises.

Pure — no Postgres, no network, no server. Runs in milliseconds, independent of the heavier DR gate. Asserts the
"fires on a REAL behind coordinate, QUIET on an orphan with a fresh live sibling" boundary so it cannot drift.

Run:  python3 tests/test_graph_stale_orphan_exclusion.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class FakePoster:
    def __init__(self):
        self.posts = []

    def __call__(self, url, body):
        self.posts.append((url, body))


def _sink():
    return alerts.AlertSink(webhook_url="https://hook.example/x", min_interval=900,
                            clock=FakeClock(), poster=FakePoster())


def _stale_posts(sink):
    return [p for p in sink._poster.posts if p[1]["key"] == "graph_stale"]


def checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1) ORPHAN-ONLY-DUPLICATE: a (repo, branch) with a FRESH live record AND a behind-forever orphan record →
    #    the orphan must NOT page (predictions run on the fresh coordinate; the orphan is un-actionable).
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # LIVE, fresh
        {"repo": "acme/app", "branch": "main", "behind": True, "age_seconds": 999999},   # ORPHAN, behind forever
    ], behind_count_threshold=1)
    check("orphan behind-record with a FRESH live sibling for the same (repo,branch) does NOT page graph_stale",
          n == 0 and not _stale_posts(s))

    # 2) GENUINE drift in the SAME sample as an orphan: the no-fresh-sibling coordinate STILL pages, and the
    #    behind_count counts ONLY the genuine one (the orphan is excluded from the count, not just the message).
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # fresh sibling
        {"repo": "acme/app", "branch": "main", "behind": True, "age_seconds": 999999},   # orphan (excluded)
        {"repo": "acme/svc", "branch": "main", "behind": True, "age_seconds": 7200},     # GENUINE drift (no sibling)
    ], behind_count_threshold=1)
    pages = _stale_posts(s)
    check("a GENUINE behind coordinate (no fresh sibling) STILL pages when an orphan shares the sample",
          n == 1 and bool(pages) and pages[0][1]["fields"]["behind_count"] == 1)

    # 3) PER-BRANCH: a fresh `main` does NOT silence a stale `release` of the same repo (each branch is its own
    #    coordinate; the exclusion is keyed on (repo, branch), not on repo alone).
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/app", "branch": "main", "behind": False, "age_seconds": 30},      # main fresh
        {"repo": "acme/app", "branch": "release", "behind": True, "age_seconds": 7200},  # release genuinely behind
    ], behind_count_threshold=1)
    check("orphan exclusion is per (repo,branch): a stale OTHER branch still pages (main fresh != release fresh)",
          n == 1 and bool(_stale_posts(s)))

    # 4) ALL-ORPHAN (every behind coordinate has a fresh sibling) → completely SILENT (the disarming case the old
    #    code flooded on). behind_count returns 0 → no page even at the lowest threshold.
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "a/x", "branch": "main", "behind": False, "age_seconds": 10},
        {"repo": "a/x", "branch": "main", "behind": True, "age_seconds": 500000},
        {"repo": "b/y", "branch": "main", "behind": False, "age_seconds": 20},
        {"repo": "b/y", "branch": "main", "behind": True, "age_seconds": 800000},
    ], behind_count_threshold=1)
    check("an all-orphan sample (every behind has a fresh sibling) is completely SILENT (no flood)",
          n == 0 and not _stale_posts(s))

    # 5) NO REGRESSION on the plain case: a lone behind coordinate (no fresh sibling anywhere) still pages exactly
    #    as before the fix — the real condition the alert exists for is untouched.
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/lonely", "branch": "main", "behind": True, "age_seconds": 7200},
    ], behind_count_threshold=1)
    check("no regression: a lone genuinely-behind coordinate still pages graph_stale",
          n == 1 and bool(_stale_posts(s)))

    # 6) behind=None (HEAD unresolvable) is still NEVER counted/paged, fresh sibling or not (we never page on what
    #    we can't see). A None record does not act as a "fresh sibling" either (it is not behind=False).
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/u", "branch": "main", "behind": None, "age_seconds": 99999},
    ], behind_count_threshold=1)
    check("behind=None (HEAD unknown) never pages and never acts as a fresh sibling",
          n == 0 and not _stale_posts(s))

    # 7) EMPTY/UNUSABLE repo+branch never silences: a behind record with no usable coordinate key cannot be
    #    proven to have a fresh sibling, so it is NOT excluded (we never silence on missing metadata).
    s = _sink()
    n = alerts.evaluate_graph_freshness(s, [
        {"repo": "", "branch": "", "behind": False, "age_seconds": 5},     # not a usable key → not a "fresh sibling"
        {"repo": "", "branch": "", "behind": True, "age_seconds": 7200},   # behind, no provable fresh sibling → pages
    ], behind_count_threshold=1)
    check("empty/unusable (repo,branch) is never silenced (a behind record with no provable fresh sibling pages)",
          n == 1 and bool(_stale_posts(s)))

    # 8) CONTENT-FREE preserved: the page body carries only count + age + threshold — never a repo/branch/sha/id.
    s = _sink()
    alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/secret-repo", "branch": "feature/x", "behind": True, "age_seconds": 7200},
    ], behind_count_threshold=1)
    pages = _stale_posts(s)
    body = pages[0][1] if pages else {}
    fields_ok = bool(pages) and set(body.get("fields", {})).issubset(
        {"behind_count", "worst_age_seconds", "stale_threshold_seconds"})
    text_ok = bool(pages) and "acme/secret-repo" not in body.get("text", "") and "feature/x" not in body.get("text", "")
    check("graph_stale stays content-free (count+age only; no repo/branch/sha leaks into the page)",
          fields_ok and text_ok)

    # 9) FAIL-OPEN preserved: a garbage / None / non-dict-laden sample never raises.
    threw = False
    try:
        alerts.evaluate_graph_freshness(_sink(), None)
        alerts.evaluate_graph_freshness(_sink(), "garbage")
        alerts.evaluate_graph_freshness(_sink(), [None, 7, {"behind": True}])  # mixed junk + a keyless behind
    except Exception:
        threw = True
    check("graph-freshness eval stays FAIL-OPEN on a missing/garbage/mixed sample", not threw)

    return results


def main() -> int:
    print("== graph_stale orphan/phantom-coordinate exclusion (pure: no DB) ==")
    results = checks()
    ok = all(c for _, c in results)
    print("GRAPH-STALE ORPHAN-EXCLUSION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
