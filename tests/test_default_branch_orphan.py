#!/usr/bin/env python3
"""Default-branch CHANGE orphan — freshness/graph_stale exclusion lock (pure: no DB, no network, fake db/gh).

THE DEFECT THIS PINS: Veripsa ingests a repository's default branch, so the
graph coordinate is (account_id, repo, DEFAULT branch). When a repo's default branch MOVES — `main` → `master`
(the classic GitHub default-rename) — TWO things happen and NOTHING healed the fallout before this fix:
  • there is NO `repository.edited` default_branch handler (the webhook's `_handle_repository_event` dispatches
    only deleted/renamed/transferred/archived → a default_branch edit falls to noop), so no migration runs; and
  • the next push to the NEW default (`master`) ingests a fresh (…, master) coordinate that tracks HEAD, while
    the OLD (…, main) coordinate FREEZES — no push ever re-addresses `main` again (it is no longer the default).

The frozen (…, main) coordinate is BEHIND main HEAD *FOREVER*, and the pre-existing (repo,branch)-sibling
orphan rule CANNOT save it: that rule downgrades a behind record only when a FRESH sibling exists at the SAME
(repo, branch) key — but the live, fresh coordinate is at (repo, MASTER), a DIFFERENT key. So (repo, main) has
no fresh sibling at (repo, main) → it reports behind=True every interval → `graph_stale` PAGES PERPETUALLY (an
un-actionable flood; there is nothing to heal, the live `master` graph is already current). A flood gets MUTED,
and a muted alert is a DEAF alert — a LATER, REAL drift on another repo is then silently missed. A false page
begets a missed page (the perpetual-orphan `graph_stale` lesson in CLAUDE.md).

THE FIX THIS LOCKS (Option B — surgical, content-free, no DDL, auto-healing): repo_default_branch_head already
resolves the repo's LIVE default branch (the `_b` graph_freshness_all previously discarded). Each freshness
record now carries `live_default` (is this coordinate's branch the repo's CURRENT default?). A behind record
CONFIRMED on a NON-current default branch (live_default is False) is a frozen non-live coordinate predictions
never run on → it is downgraded behind:false + tagged orphan:true on the SURFACE (graph_freshness_all) and
EXCLUDED from the `graph_stale` page (alerts.evaluate_graph_freshness) — the two MIRROR each other so the
/freshz surface and the operator alert agree. This auto-heals ANY future default-branch change with NO schema
migration and NO data migration (the dormant old-branch graph_version row is left in place — row cleanup /
content migration is a separate PO-gated op, consistent with the existing orphan-row treatment).

THE CONTRACT (and the specific assertion the task names):
  • After a default-branch change (ingest at `main`, then default→`master` + ingest at `master`): the surface
    reports any_behind=false / behind_count=0, the orphaned `main` is tagged orphan:true, and — the headline —
    `evaluate_graph_freshness` returns behind_count == 0 and fires NO graph_stale page.
  • live_default None (default/HEAD unresolvable) is NEVER excluded (we never silence on what we can't see).
  • A genuine drift on the LIVE default branch STILL pages (the exclusion is surgical, never a blanket silence).
  • Content-free + fail-open preserved on both layers.

Pure — no Postgres, no network, no server: graph_freshness_all's `db`/`gh` seams and the alert's poster are
fakes. Runs in milliseconds.

Run:  python3 tests/test_default_branch_orphan.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import alerts            # noqa: E402
import graph_freshness as gf  # noqa: E402

# Distinct, fixed shas (content-free — a sha is public git metadata).
OLD_HEAD = "a" * 40   # main's HEAD before the default-branch move (what the `main` coordinate ingested)
NEW_HEAD = "d" * 40   # master's HEAD after the move (what the live `master` coordinate tracks)
DRIFT = "c" * 40      # a genuinely-drifted live coordinate's stale stored sha (!= HEAD → real behind)


def _db(coords):
    """Fake scoped-query seam: returns the owner_graph_freshness_surface() dict graph_freshness_all reads."""
    def db(sql, params=None):
        if "owner_graph_freshness_surface" in sql:
            return {"coordinates": coords}
        return None
    return db


class FakeGH:
    """Fake GitHub client: repo_default_branch_head(repo) -> (default_branch, head_sha).

    `default_by_repo[repo]` = the repo's CURRENT default branch (this is what MOVES in the simulation);
    `head_by_repo[repo]` = that default branch's current HEAD sha (None = unresolvable);
    `canonical_by_repo[repo]` = GitHub's CURRENT full_name after a repo/owner rename."""
    def __init__(self, default_by_repo, head_by_repo, canonical_by_repo=None):
        self.default_by_repo = default_by_repo
        self.head_by_repo = head_by_repo
        self.canonical_by_repo = canonical_by_repo or {}

    def repo_default_branch_head(self, repo):
        default = self.default_by_repo.get(repo, "main")
        return (default if default is not None else "", self.head_by_repo.get(repo))

    def repo_default_branch_head_info(self, repo):
        default, head = self.repo_default_branch_head(repo)
        return default, head, self.canonical_by_repo.get(repo, repo)


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


def _freshz(records):
    """Reproduce the /freshz handler's derivation EXACTLY (server_http.py): off `behind is True`."""
    behind = [f for f in records if f.get("behind") is True]
    return {"coordinate_count": len(records), "behind_count": len(behind), "any_behind": bool(behind)}


def checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    REPO = "acme/app"

    # ── PHASE 1: BEFORE the move. The repo's default is `main`; the only coordinate is (REPO, main), ingested at
    #    main's HEAD → fresh. The surface is clean and graph_stale is silent. (Baseline: nothing to page on.) ──
    coords_before = [{"repo": REPO, "branch": "main", "commit_sha": OLD_HEAD, "age_seconds": 30}]
    out_before = gf.graph_freshness_all(_db(coords_before),
                                        FakeGH({REPO: "main"}, {REPO: OLD_HEAD}))
    s_before = _freshz(out_before)
    check("phase-1 (default=main, coordinate tracks HEAD): surface clean (any_behind=false)",
          s_before["any_behind"] is False and s_before["behind_count"] == 0
          and out_before[0].get("live_default") is True)
    s = _sink()
    n_before = alerts.evaluate_graph_freshness(s, out_before, behind_count_threshold=1)
    check("phase-1: graph_stale is silent (behind_count==0, no page)",
          n_before == 0 and not _stale_posts(s))

    # ── PHASE 2: THE DEFAULT-BRANCH CHANGE main → master. The repo's default is now `master`; the webhook
    #    ingested a NEW (REPO, master) coordinate at master's NEW HEAD (fresh), while the OLD (REPO, main)
    #    coordinate FROZE at OLD_HEAD (it is now behind master's HEAD forever; no push re-addresses it). This is
    #    the exact prod state that paged graph_stale PERPETUALLY before the fix. ──
    coords_after = [
        {"repo": REPO, "branch": "main",   "commit_sha": OLD_HEAD, "age_seconds": 999999},  # FROZEN old default
        {"repo": REPO, "branch": "master", "commit_sha": NEW_HEAD, "age_seconds": 30},       # LIVE new default (fresh)
    ]
    out_after = gf.graph_freshness_all(_db(coords_after),
                                       FakeGH({REPO: "master"}, {REPO: NEW_HEAD}))
    s_after = _freshz(out_after)
    main_rec = next((r for r in out_after if r.get("branch") == "main"), {})
    master_rec = next((r for r in out_after if r.get("branch") == "master"), {})

    # (a) SURFACE: the orphaned `main` is excluded (behind:false), tagged orphan:true, and flagged non-current
    #     (live_default:false); the live `master` is fresh; any_behind/behind_count are 0 (no perpetual red).
    check("phase-2 surface: orphaned old-default `main` is downgraded behind:false + tagged orphan:true + live_default:false",
          main_rec.get("behind") is False and main_rec.get("orphan") is True
          and main_rec.get("live_default") is False)
    check("phase-2 surface: live new-default `master` is fresh (behind:false, live_default:true, not an orphan)",
          master_rec.get("behind") is False and master_rec.get("live_default") is True
          and master_rec.get("orphan") is None)
    check("phase-2 surface: /freshz reports any_behind=false / behind_count=0 (the perpetual-red surface is gone)",
          s_after["any_behind"] is False and s_after["behind_count"] == 0)

    # (b) THE HEADLINE ASSERTION the task names: feed the post-move freshness to the operator alert →
    #     graph_stale behind_count == 0 (the orphaned `main` is excluded, the live `master` is fresh) → NO PAGE.
    s = _sink()
    n_after = alerts.evaluate_graph_freshness(s, out_after, behind_count_threshold=1)
    check("phase-2 ALERT (headline): graph_stale behind_count == 0 after the default-branch change → NO perpetual page",
          n_after == 0 and not _stale_posts(s))

    # ── OWNER-LOGIN / REPO-RENAME ORPHAN: GitHub redirects the OLD full_name to the NEW canonical full_name.
    #    The fresh sibling is not at the same (repo, branch) key, so the first orphan rule cannot catch it. The
    #    canonical full_name + same branch + same HEAD proves the old-name row is renamed-away, not live drift. ──
    OLD_REPO = "example-user/veripsa-core-old"
    NEW_REPO = "RollNuts/veripsa"
    coords_rename = [
        {"repo": OLD_REPO, "branch": "main", "commit_sha": OLD_HEAD, "age_seconds": 999999},
        {"repo": NEW_REPO, "branch": "main", "commit_sha": NEW_HEAD, "age_seconds": 30},
    ]
    gh_rename = FakeGH({OLD_REPO: "main", NEW_REPO: "main"},
                       {OLD_REPO: NEW_HEAD, NEW_REPO: NEW_HEAD},
                       {OLD_REPO: NEW_REPO, NEW_REPO: NEW_REPO})
    out_rename = gf.graph_freshness_all(_db(coords_rename), gh_rename)
    old_name_rec = next((r for r in out_rename if r.get("repo") == OLD_REPO), {})
    new_name_rec = next((r for r in out_rename if r.get("repo") == NEW_REPO), {})
    s_rename = _freshz(out_rename)
    check("owner-login rename surface: old full_name resolving to a fresh canonical sibling is downgraded orphan",
          old_name_rec.get("behind") is False and old_name_rec.get("orphan") is True
          and new_name_rec.get("behind") is False
          and s_rename["any_behind"] is False and s_rename["behind_count"] == 0)
    check("owner-login rename surface: canonical helper is internal and not exposed in /freshz rows",
          all("_live_repo" not in r for r in out_rename))

    # ── NO OVER-SILENCING: a GENUINE drift on the repo's LIVE default branch STILL pages (the exclusion only
    #    quiets the FROZEN non-current branch; a real missed push on the live default is never hidden). Here the
    #    live default is `master` but its stored coordinate is STALE (DRIFT != NEW_HEAD) → genuine behind → pages,
    #    while the frozen `main` is still excluded. behind_count counts ONLY the live-default drift. ──
    coords_drift = [
        {"repo": REPO, "branch": "main",   "commit_sha": OLD_HEAD, "age_seconds": 999999},  # frozen orphan (excluded)
        {"repo": REPO, "branch": "master", "commit_sha": DRIFT,    "age_seconds": 7200},     # LIVE default, genuinely behind
    ]
    out_drift = gf.graph_freshness_all(_db(coords_drift),
                                       FakeGH({REPO: "master"}, {REPO: NEW_HEAD}))
    s_drift = _freshz(out_drift)
    behind_live = [r for r in out_drift if r.get("behind") is True]
    check("no over-silence: a genuine drift ON the live default branch still reports behind (surface behind_count==1)",
          s_drift["behind_count"] == 1 and len(behind_live) == 1 and behind_live[0]["branch"] == "master")
    s = _sink()
    n_drift = alerts.evaluate_graph_freshness(s, out_drift, behind_count_threshold=1)
    pages = _stale_posts(s)
    check("no over-silence: graph_stale STILL pages on the live-default drift (behind_count==1) while the orphan stays excluded",
          n_drift == 1 and bool(pages) and pages[0][1]["fields"]["behind_count"] == 1)

    # ── ALERT MIRROR is self-sufficient: evaluate_graph_freshness excludes a non-current-default record on its
    #    OWN (off the `live_default` field the records carry) — proven by feeding it HAND-BUILT records directly
    #    (the watchdog path), so the surface and the alert provably agree even when the alert runs standalone. ──
    s = _sink()
    n_mirror = alerts.evaluate_graph_freshness(s, [
        {"repo": REPO, "branch": "master", "behind": False, "live_default": True,  "age_seconds": 30},   # live, fresh
        {"repo": REPO, "branch": "main",   "behind": True,  "live_default": False, "age_seconds": 999999},  # frozen orphan
    ], behind_count_threshold=1)
    check("alert mirror: evaluate_graph_freshness excludes a live_default:false record on its own (behind_count==0, no page)",
          n_mirror == 0 and not _stale_posts(s))

    # ── live_default None (default/HEAD unresolvable) is NEVER excluded by the default-branch rule (we never
    #    silence on what we can't see): a behind record with live_default None and no fresh sibling STILL pages. ──
    s = _sink()
    n_unknown = alerts.evaluate_graph_freshness(s, [
        {"repo": REPO, "branch": "main", "behind": True, "live_default": None, "age_seconds": 7200},
    ], behind_count_threshold=1)
    check("live_default None (default unknown) is never excluded → a behind coordinate still pages",
          n_unknown == 1 and bool(_stale_posts(s)))

    # ── CONTENT-FREE preserved on the alert: even when the only thing paging is a live-default drift, the page
    #    body carries ONLY count + age + threshold — never a repo/branch/sha. ──
    s = _sink()
    alerts.evaluate_graph_freshness(s, [
        {"repo": "acme/secret-repo", "branch": "trunk", "behind": True, "live_default": True, "age_seconds": 7200},
    ], behind_count_threshold=1)
    pages = _stale_posts(s)
    body = pages[0][1] if pages else {}
    fields_ok = bool(pages) and set(body.get("fields", {})).issubset(
        {"behind_count", "worst_age_seconds", "stale_threshold_seconds"})
    text_ok = bool(pages) and "acme/secret-repo" not in body.get("text", "") and "trunk" not in body.get("text", "")
    check("content-free preserved: the graph_stale page leaks no repo/branch/sha (count+age only)",
          fields_ok and text_ok)

    # ── FAIL-OPEN preserved on BOTH layers across the default-branch path: a raising/garbage surface and a
    #    garbage alert sample never crash. ──
    threw = False
    try:
        def _db_raises(sql, params=None):
            raise RuntimeError("surface boom")
        r_raise = gf.graph_freshness_all(_db_raises, FakeGH({}, {}))                 # surface read raises → []
        gf.graph_freshness_all(_db([None, 7, {"repo": REPO, "branch": "master", "commit_sha": NEW_HEAD}]),
                               FakeGH({REPO: "master"}, {REPO: NEW_HEAD}))            # mixed junk in coordinates
        alerts.evaluate_graph_freshness(_sink(), None)
        alerts.evaluate_graph_freshness(_sink(), [None, 7, {"behind": True, "live_default": False}])
    except Exception:
        threw = True
    check("fail-open preserved on both layers across the default-branch path (raising/garbage never crashes)",
          not threw and r_raise == [])

    return results


def main() -> int:
    print("== default-branch-change orphan exclusion (pure: no DB; surface + alert mirror) ==")
    results = checks()
    ok = all(c for _, c in results)
    print("DEFAULT-BRANCH-ORPHAN GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
