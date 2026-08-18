#!/usr/bin/env python3
"""/freshz + /readyz freshness SURFACE — ORPHAN/PHANTOM-COORDINATE exclusion lock (pure: no DB, no network).

THE DEFECT THIS PINS (verified, MINOR/cosmetic): graph_freshness.graph_freshness_all feeds the internal
aggregation behind /freshz — it returns one record per stored coordinate with `behind: stored_sha != HEAD`.
A single (repo, branch) can have MORE THAN ONE stored coordinate:
  • the LIVE coordinate — the account the webhook actually addresses; predictions run on it; it tracks HEAD.
  • an ABANDONED/ORPHAN duplicate — a graph_version written into a DEAD/renamed-away account (a backfill /
    old-account write the webhook never re-addresses; the live `de75fe23` orphan). The per-PR self-heal only
    re-ingests under the LIVE account, so the orphan is BEHIND main HEAD *forever*.

Before the fix, /freshz's consumer (`behind = [f for f in fresh if f.get("behind") is True]` →
`any_behind / behind_count`) counted the orphan, so /freshz reported `any_behind:true` PERPETUALLY even though
the LIVE coordinate is fresh — a permanently-red surface that trains the operator to ignore it (and then a
LATER REAL drift hides in the noise). #338 fixed the analogous OPERATOR ALERT (alerts.evaluate_graph_freshness)
with the SAME exclusion but did NOT touch this surface. This locks the MIRRORED exclusion so surface == alert.

THE CONTRACT THIS LOCKS (surgical, content-free, surface-only):
  • A (repo, branch) with ANY CURRENT (behind=False) record is tracked FRESH → a behind=True record for that
    SAME (repo, branch) is a stale orphan/duplicate → its `behind` is DOWNGRADED to False (so the surface's
    `behind is True` filter excludes it from behind_count/any_behind) and it is tagged `orphan:true`.
  • A behind=True coordinate whose (repo, branch) has NO fresh sibling is GENUINE drift → it STILL reports
    behind (exclusion keyed on repo+branch, never a blanket silence — a real missed push is never hidden).
  • Per (repo, branch): a fresh `main` says NOTHING about a stale `release` of the same repo.
  • Empty/unusable repo+branch never silences (we cannot prove a fresh sibling → keep it behind).
  • behind=None (HEAD unresolvable) is NEVER reported behind and never acts as a fresh sibling.
  • Surface-only: graph_freshness_all does NOT mutate the DB (no row delete — orphan ROW cleanup is PO-gated).
  • Content-free + fail-open preserved: a missing/garbage surface or record never raises.

This MIRRORS tests/test_graph_stale_orphan_exclusion.py (#338) on the SURFACE side so the two stay consistent.
Pure — no Postgres, no network: graph_freshness_all's `db` and `gh` seams are injected fakes.

Run:  python3 tests/test_freshz_orphan.py
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import graph_freshness as gf  # noqa: E402

# Distinct, fixed shas (content-free — a sha is public git metadata). HEAD is "current main".
HEAD = "a" * 40
ORPH = "b" * 40   # an orphan's stale stored sha (!= HEAD → behind)
DRIFT = "c" * 40  # a genuinely-drifted coordinate's stale stored sha (!= HEAD → behind)


def _db(coords):
    """A fake scoped-query seam: returns the freshness surface dict graph_freshness_all reads."""
    def db(sql, params=None):
        if "owner_graph_freshness_surface" in sql:
            return {"coordinates": coords}
        return None
    return db


def _db_raises():
    def db(sql, params=None):
        raise RuntimeError("surface boom")
    return db


class FakeGH:
    """A fake GitHub client: repo_default_branch_head(repo) -> (default_branch, head_sha).

    `head_by_repo` maps repo -> the repo's CURRENT HEAD sha (None = HEAD unresolvable). `default_by_repo`
    (optional) maps repo -> the repo's CURRENT DEFAULT branch (used by the default-branch-change exclusion —
    graph_freshness_all marks a coordinate whose branch != this as non-current); a repo absent from it defaults
    to "main" (the common case). A repo whose entry is None reports an UNKNOWN default ("" → live_default None,
    never excluded) — the way an unresolvable repo lookup degrades content-free."""
    def __init__(self, head_by_repo, default_by_repo=None):
        self.head_by_repo = head_by_repo
        self.default_by_repo = default_by_repo or {}

    def repo_default_branch_head(self, repo):
        default = self.default_by_repo.get(repo, "main")
        return (default if default is not None else "", self.head_by_repo.get(repo))


def _surface(records):
    """Reproduce /freshz's public aggregate derivation from internal `behind is True` records."""
    behind = [f for f in records if f.get("behind") is True]
    return {"coordinate_count": len(records), "behind_count": len(behind), "any_behind": bool(behind)}


def checks():
    results = []

    def check(name, cond):
        results.append((name, bool(cond)))
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")

    # 1) ORPHAN-ONLY DUPLICATE: a (repo, branch) with a FRESH live row AND a behind-forever orphan row → the
    #    orphan is downgraded (behind:false, orphan:true); the surface reports any_behind=false / behind_count=0.
    coords = [
        {"repo": "acme/app", "branch": "main", "commit_sha": HEAD, "age_seconds": 30},      # LIVE, fresh (==HEAD)
        {"repo": "acme/app", "branch": "main", "commit_sha": ORPH, "age_seconds": 999999},  # ORPHAN (!=HEAD, fresh sibling)
    ]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"acme/app": HEAD}))
    s = _surface(out)
    orphan_tagged = any(r.get("orphan") is True and r.get("behind") is False for r in out)
    check("orphan-with-fresh-sibling: surface reports any_behind=false / behind_count=0 (no perpetual red)",
          s["any_behind"] is False and s["behind_count"] == 0)
    check("orphan-with-fresh-sibling: the orphan row is downgraded behind:false and tagged orphan:true",
          orphan_tagged)

    # 2) GENUINE drift alongside an orphan: the no-fresh-sibling coordinate STILL reports behind, and the surface
    #    counts ONLY it (the orphan is excluded from the count, not merely tagged).
    coords = [
        {"repo": "acme/app", "branch": "main", "commit_sha": HEAD, "age_seconds": 30},       # fresh sibling
        {"repo": "acme/app", "branch": "main", "commit_sha": ORPH, "age_seconds": 999999},   # orphan (excluded)
        {"repo": "acme/svc", "branch": "main", "commit_sha": DRIFT, "age_seconds": 7200},    # GENUINE drift (no sibling)
    ]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"acme/app": HEAD, "acme/svc": HEAD}))
    s = _surface(out)
    drift = [r for r in out if r.get("behind") is True]
    check("genuine drift (no fresh sibling) still reports behind while the orphan is excluded (behind_count==1)",
          s["any_behind"] is True and s["behind_count"] == 1
          and len(drift) == 1 and drift[0]["repo"] == "acme/svc"
          and drift[0].get("orphan") is None)

    # 3) PER-BRANCH: a genuinely-behind coordinate on the repo's CURRENT default branch (`release`) still reports
    #    behind; a fresh sibling on a DIFFERENT branch (`main`) does not silence it (the sibling exclusion is keyed
    #    on (repo, branch), not repo alone). Here the repo's live default IS `release`, so `release` is the live
    #    coordinate that pages and `main` is the non-current-default row (correctly downgraded by the default-branch
    #    rule — already behind=False, so only tagged orphan). The point this pins: a fresh OTHER-branch row never
    #    silences a behind coordinate on the live default branch.
    coords = [
        {"repo": "acme/app", "branch": "main", "commit_sha": HEAD, "age_seconds": 30},        # other-branch (non-default) fresh
        {"repo": "acme/app", "branch": "release", "commit_sha": DRIFT, "age_seconds": 7200},  # release (live default) genuinely behind
    ]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"acme/app": HEAD}, {"acme/app": "release"}))
    s = _surface(out)
    behind = [r for r in out if r.get("behind") is True]
    check("exclusion is per (repo,branch): a behind coordinate on the live default branch still reports behind (a fresh OTHER branch does not silence it)",
          s["behind_count"] == 1 and behind[0]["branch"] == "release")

    # 4) ALL-ORPHAN: every behind coordinate has a fresh sibling → the surface is completely CLEAN (any_behind=false).
    coords = [
        {"repo": "a/x", "branch": "main", "commit_sha": HEAD, "age_seconds": 10},
        {"repo": "a/x", "branch": "main", "commit_sha": ORPH, "age_seconds": 500000},
        {"repo": "b/y", "branch": "main", "commit_sha": HEAD, "age_seconds": 20},
        {"repo": "b/y", "branch": "main", "commit_sha": ORPH, "age_seconds": 800000},
    ]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"a/x": HEAD, "b/y": HEAD}))
    s = _surface(out)
    check("all-orphan sample (every behind has a fresh sibling) → any_behind=false (the perpetual-red bug is gone)",
          s["any_behind"] is False and s["behind_count"] == 0)

    # 5) NO REGRESSION: a lone genuinely-behind coordinate (no fresh sibling anywhere) still reports behind exactly
    #    as before — the real drift the surface exists to show is untouched, and it is NOT tagged orphan.
    coords = [{"repo": "acme/lonely", "branch": "main", "commit_sha": DRIFT, "age_seconds": 7200}]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"acme/lonely": HEAD}))
    s = _surface(out)
    check("no regression: a lone genuinely-behind coordinate still reports behind (any_behind=true, not tagged orphan)",
          s["any_behind"] is True and s["behind_count"] == 1 and out[0].get("orphan") is None)

    # 6) behind=None (HEAD unresolvable) is NEVER reported behind and NEVER acts as a fresh sibling: a coordinate
    #    whose HEAD cannot be resolved stays behind=None; an orphan it shares a (repo,branch) with is NOT downgraded
    #    by it (None is not behind=False), so a genuine behind in that group is preserved.
    coords = [
        {"repo": "acme/u", "branch": "main", "commit_sha": HEAD, "age_seconds": 50},     # HEAD unknown for this repo
        {"repo": "acme/u", "branch": "main", "commit_sha": ORPH, "age_seconds": 7200},   # would-be behind, no FRESH sibling
    ]
    out = gf.graph_freshness_all(_db(coords), FakeGH({"acme/u": None}))   # HEAD unresolvable → behind=None for both
    none_recs = [r for r in out if r.get("behind") is None]
    not_silenced = all(r.get("orphan") is None for r in out)  # a None record never acts as a fresh sibling → no downgrade
    check("behind=None (HEAD unknown) is never reported behind and never acts as a fresh sibling",
          len(none_recs) == 2 and not_silenced)

    # 7) EMPTY/UNUSABLE (repo,branch) never silences: a behind record with no usable coordinate key cannot be proven
    #    to have a fresh sibling, so it is NOT excluded (it keeps behind=True; a fresh empty-key row is not a sibling).
    coords = [
        {"repo": "", "branch": "", "commit_sha": HEAD, "age_seconds": 5},      # not a usable key → not a fresh sibling
        {"repo": "", "branch": "", "commit_sha": ORPH, "age_seconds": 7200},   # behind, no provable fresh sibling
    ]
    # the empty repo reports an UNKNOWN default (None) → live_default None → the default-branch rule never fires on
    # it either (we never silence on an unprovable default, exactly as on an unprovable fresh sibling).
    out = gf.graph_freshness_all(_db(coords), FakeGH({"": HEAD}, {"": None}))
    s = _surface(out)
    check("empty/unusable (repo,branch) is never silenced (a behind row with no provable fresh sibling still counts)",
          s["behind_count"] == 1)

    # 8) SURFACE-ONLY (content-free, no mutation of the DB): graph_freshness_all reads the surface and NEVER issues a
    #    write/delete — a fake db that records every SQL it sees must see ONLY the read, no DELETE/UPDATE of the row.
    seen = []
    def recording_db(sql, params=None):
        seen.append(sql)
        if "owner_graph_freshness_surface" in sql:
            return {"coordinates": [
                {"repo": "acme/app", "branch": "main", "commit_sha": HEAD, "age_seconds": 30},
                {"repo": "acme/app", "branch": "main", "commit_sha": ORPH, "age_seconds": 999999},
            ]}
        return None
    gf.graph_freshness_all(recording_db, FakeGH({"acme/app": HEAD}))
    no_mutation = all(
        not any(w in sql.upper() for w in ("DELETE", "UPDATE", "INSERT", "TRUNCATE", "DROP"))
        for sql in seen
    )
    check("surface-only: graph_freshness_all issues NO row mutation (orphan ROW cleanup is a separate PO-gated op)",
          no_mutation and len(seen) >= 1)

    # 9) FAIL-OPEN preserved: a surface read that raises → [] (no crash, unchanged behavior); a garbage/mixed
    #    coordinate list never raises and the exclusion pass tolerates non-dict/keyless junk.
    threw = False
    try:
        r_raise = gf.graph_freshness_all(_db_raises(), FakeGH({}))                      # read raises → []
        gf.graph_freshness_all(_db("garbage-not-a-list"), FakeGH({}))                   # surface not a dict → []
        gf.graph_freshness_all(_db([None, 7, {"repo": "z", "branch": "main", "commit_sha": ORPH}]),
                               FakeGH({"z": HEAD}))                                       # mixed junk in coordinates
    except Exception:
        threw = True
    check("fail-open: a raising/garbage/mixed freshness sample never crashes (degrades to a safe surface)",
          not threw and r_raise == [])

    return results


def main() -> int:
    print("== /freshz orphan/phantom-coordinate exclusion (pure: no DB; mirrors #338 on the surface) ==")
    results = checks()
    ok = all(c for _, c in results)
    print("FRESHZ ORPHAN GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
