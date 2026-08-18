#!/usr/bin/env python3
"""STALE-GRAPH WITHHELD-CLEAR gate (G1) for deferred graph convergence.

The request process must never clone/extract a repository. A structural PR
persists its stable-repository/base coordinate, returns Unknown while the graph
is stale, and lets the isolated convergence worker resolve current HEAD,
extract, commit, and re-render later. A docs-only exact Files snapshot remains
graph-independent and may complete immediately.

This drives the real handler over the real DB gate and asserts:
  (0) docs-only exact Files snapshot → success, zero tarball reads;
  (A) stale structural work → durable enqueue + neutral, zero inline extract;
  (B) a real cross-PR collision remains action_required;
  (C) an available tarball cannot re-enable extraction in the live path;
  (D) an unavailable GitHub HEAD read is irrelevant to the live path: the
      signed base coordinate is queued and remains neutral.

Run:  python3 tests/test_stale_graph_failclosed.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server_harness import (DB, OWNER_ID, REPO, REPO_ID, ROOT, SHA,  # noqa: E402
                             FakeGitHub, make_db)  # make_db closes over DB

sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402

HEAD_AHEAD = "c" * 40          # the FakeGitHub's live main HEAD (the harness default) — ahead of the stored graph
STORED_SHA = SHA               # the stored graph is seeded here ("a"*40) → BEHIND HEAD_AHEAD ("c"*40)


class StaleGraphGitHub(FakeGitHub):
    """Fake whose expensive graph surfaces must remain untouched by live PR handling."""

    def __init__(self, files_by_pr, heal_raises=False, head_sha=HEAD_AHEAD, **kw):
        super().__init__(files_by_pr, **kw)
        self.heal_raises = heal_raises
        self.head_sha = head_sha
        self.tarball_downloads = 0

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha            # '' → head_sha None in graph_freshness → behind None

    def download_tarball(self, repo, sha):
        self.tarball_downloads += 1
        if self.heal_raises:
            raise RuntimeError("simulated transient tarball fetch failure (network/permission/timeout)")
        return super().download_tarball(repo, sha)


def _pr_payload(action, number, author, changed_files=1, head_sha=None, merged=False):
    head_sha = head_sha or f"{number:040x}"
    return {"action": action, "number": number,
            "installation": {"id": 4242},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": OWNER_ID, "login": "acme"}},
            "pull_request": {"base": {"ref": "main", "sha": STORED_SHA,
                                      "repo": {"id": REPO_ID, "full_name": REPO}},
                             "head": {"sha": head_sha,
                                      "repo": {"id": REPO_ID, "full_name": REPO}},
                             "user": {"login": author}, "merged": merged,
                             "changed_files": changed_files}}


def _conclusion(gh, head_sha):
    c = next((c for c in gh.checks if c["sha"] == head_sha), None)
    return c.get("conclusion") if c else None


def _title(gh, head_sha):
    c = next((c for c in gh.checks if c["sha"] == head_sha), None)
    return (c.get("title") or "") if c else ""


def _comment_for(gh, number):
    return next((c for c in gh.comments if c["number"] == number), None)


def _seed_graph_at(db, graph, sha):
    """(Re)seed main's stored graph coordinate at `sha` (overwrites, so each scenario starts from a known state)."""
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", sha))
    db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, str(REPO_ID)))


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    raw_db = make_db("veripsa_app")
    raw_db(
        "SELECT core.enter_installation_with_authority(%s)",
        (str(OWNER_ID),),
    )

    # This legacy gate calls handle_event directly instead of the composition
    # root. Reproduce the live processor's authenticated tenant pin on every
    # short DB statement so generation-21 graph enqueue is tested in the same
    # account as the seeded graph.
    def db(sql, args=()):
        conn = psycopg2.connect(
            f"postgresql://veripsa_app@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.enter_existing_installation_with_authority(%s)",
                    (str(OWNER_ID),),
                )
                assert cur.fetchone()[0] == f"ACCT-GH-{OWNER_ID}"
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))

    checks = []

    # ---- (0) docs-only exact snapshot → graph-independent clear; no whole-repo heal ---------------------------
    # This is the production Tier-4 shape. The stored graph is deliberately behind and tarball access deliberately
    # fails, but README.md is excluded by the exact same `_code_paths` filter the brain uses. The handler must read
    # + authoritatively prove Files first, skip the unrelated graph rebuild entirely, and post the exact success
    # Check. Before the fix, self-heal ran first and could consume two durable generations without reaching Files.
    _seed_graph_at(db, graph, STORED_SHA)
    gh0 = StaleGraphGitHub({1: ["README.md"]}, heal_raises=True)
    S.handle_event("pull_request", _pr_payload("opened", 1, "alice"), db, gh0)
    head1 = f"{1:040x}"
    checks.append(("0 docs-only exact PR snapshot posts its success Check without depending on a stale main graph",
                   _conclusion(gh0, head1) == "success"))
    checks.append(("0 docs-only exact PR snapshot performs ZERO tarball downloads / graph self-heal attempts",
                   gh0.tarball_downloads == 0))
    S.handle_event("pull_request", _pr_payload("closed", 1, "alice"), db, gh0)

    # ---- (A) stale structural work → durable enqueue + withheld-clear advisory -------------------------------
    _seed_graph_at(db, graph, STORED_SHA)
    ghA = StaleGraphGitHub({2: ["backend/api.py"]}, heal_raises=True)
    resultA = S.handle_event(
        "pull_request", _pr_payload("opened", 2, "bob"), db, ghA)
    head2 = f"{2:040x}"
    conclA = _conclusion(ghA, head2)
    titleA = _title(ghA, head2)
    cmtA = _comment_for(ghA, 2)
    checks.append(("A stale structural graph + would-be clear → check is `neutral` (NOT a false `success`)",
                   conclA == "neutral"))
    checks.append(("A the withheld-clear check is the STALE-GRAPH advisory (names 'not confirmed/updated current', "
                   "not a confident 'Clear')",
                   "not confirmed current" in titleA.lower() or "not cleared" in titleA.lower()))
    checks.append(("A an advisory 'not cleared' comment is posted (honest, content-free, jargon-free)",
                   bool(cmtA) and "not cleared" in cmtA["body"].lower() and "unknown" in cmtA["body"].lower()))
    checks.append(("A stale structural work is durably queued and performs no inline tarball read",
                   resultA.get("graph_heal", {}).get("queued") is True
                   and ghA.tarball_downloads == 0))
    # IDEMPOTENT: a synchronize on the SAME head PATCHES in place — no second comment (no flap).
    posts_before = len(ghA.comments)
    S.handle_event("pull_request", _pr_payload("synchronize", 2, "bob"), db, ghA)
    checks.append(("A the withheld-clear advisory is IDEMPOTENT (a synchronize PATCHES in place — no second comment)",
                   len(ghA.comments) == posts_before and _conclusion(ghA, head2) == "neutral"))
    S.handle_event("pull_request", _pr_payload("closed", 2, "bob"), db, ghA)   # release the lane for the next scenario

    # ---- (B) behind + heal FAILED + a REAL cross-PR collision → the collision is STILL surfaced ---------------
    # Two PRs on the SAME file over the SAME stale graph. PR-3 opens (would-be clear → withheld to neutral, but its
    # lane is still reconciled). PR-4 opens on the same file → a REAL collision → material `action_required`. The
    # withhold must ONLY downgrade a would-be clear — a real collision on a behind graph is NOT suppressed.
    _seed_graph_at(db, graph, STORED_SHA)
    ghB = StaleGraphGitHub({3: ["backend/worker.py"], 4: ["backend/worker.py"]}, heal_raises=True)
    S.handle_event("pull_request", _pr_payload("opened", 3, "carol"), db, ghB)
    S.handle_event("pull_request", _pr_payload("opened", 4, "dave"), db, ghB)
    head4 = f"{4:040x}"
    conclB4 = _conclusion(ghB, head4)
    titleB4 = _title(ghB, head4)
    checks.append(("B a REAL cross-PR collision on a BEHIND graph is STILL surfaced (PR-4 → `action_required`, "
                   "NOT downgraded/suppressed by the withhold)",
                   conclB4 == "action_required"))
    checks.append(("B PR-4's material verdict is a real collision note, NOT the stale-graph 'not cleared' advisory "
                   "(the withhold never touches a non-`success` verdict)",
                   "not confirmed current" not in titleB4.lower() and "not cleared" not in titleB4.lower()))
    S.handle_event("pull_request", _pr_payload("closed", 3, "carol"), db, ghB)
    S.handle_event("pull_request", _pr_payload("closed", 4, "dave"), db, ghB)

    # ---- (C) an available tarball still cannot re-enable live extraction -------------------------------------
    _seed_graph_at(db, graph, STORED_SHA)
    ghC = StaleGraphGitHub({5: ["backend/billing.py"]}, heal_raises=False)
    resultC = S.handle_event(
        "pull_request", _pr_payload("opened", 5, "erin"), db, ghC)
    head5 = f"{5:040x}"
    conclC = _conclusion(ghC, head5)
    checks.append(("C tarball availability does not move extraction back into the webhook path",
                   conclC == "neutral"
                   and resultC.get("graph_heal", {}).get("queued") is True
                   and ghC.tarball_downloads == 0))
    S.handle_event("pull_request", _pr_payload("closed", 5, "erin"), db, ghC)

    # ---- (D) live handler does not need an unversioned current-HEAD read --------------------------------------
    _seed_graph_at(db, graph, STORED_SHA)
    ghD = StaleGraphGitHub({6: ["backend/repo.py"]}, heal_raises=True, head_sha="")
    resultD = S.handle_event(
        "pull_request", _pr_payload("opened", 6, "frank"), db, ghD)
    head6 = f"{6:040x}"
    conclD = _conclusion(ghD, head6)
    titleD = _title(ghD, head6)
    checks.append(("D an unavailable unversioned HEAD surface cannot produce a false success",
                   conclD == "neutral" and conclD != "success"))
    checks.append(("D the signed base coordinate is queued without a HEAD/tarball dependency",
                   resultD.get("graph_heal", {}).get("queued") is True
                   and "not confirmed current" in titleD.lower()
                   and ghD.tarball_downloads == 0))
    S.handle_event("pull_request", _pr_payload("closed", 6, "frank"), db, ghD)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("STALE GRAPH FAILCLOSED GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
