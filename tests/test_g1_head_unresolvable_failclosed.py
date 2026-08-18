#!/usr/bin/env python3
"""HEAD-UNRESOLVABLE WITHHELD-CLEAR gate (G1 tail) — the ONE unconfirmable-currency state the shipped G1 withhold
DELIBERATELY let through.

THE GAP: github-app/graph_freshness.py::graph_freshness returns behind=None when main's HEAD cannot be resolved
from GitHub (head_sha is None — a repo_default_branch_head API failure). The shipped G1 withhold
(_pr_stale_graph_unknown_result) and the shared predicate _graph_degraded(graph_heal) DELIBERATELY treat
head_sha is None as NOT degraded (they return False). So when HEAD is unresolvable a would-be CLEAR verdict
STANDS even though Veripsa cannot confirm the stored code graph is current — it cannot compare the stored sha to
an unknown HEAD. That is a would-be-Clear emitted from an unconfirmable-currency state → a real overlap whose
graph link only exists at the true (unread) HEAD would be missed → a SILENT false clear.

THE FIX (recall-safe, would-be-Clear ONLY): when currency cannot be confirmed AND the result would be a clean
CLEAR (check.conclusion == 'success'), withhold it to the honest Unknown — an advisory `neutral` "code graph
currency not confirmed — not cleared" check + comment — exactly like the shipped stale-graph withhold, but as a
SEPARATE, clearly-scoped branch keyed on self_heal's reason 'main HEAD unresolvable' (a genuine API failure).
_graph_degraded stays UNCHANGED (head_sha None is still NOT degraded); the behind-and-not-current withhold path
is byte-for-byte as-is. It fires ONLY on a would-be clear — a heads-up / wait-in-line / an existing collision
signal passes through unchanged even when HEAD is unresolvable (monotonic: this can only turn a would-be Clear
into Unknown, never suppress a collision, strip an ACK, or change a non-success verdict).

This drives the REAL handler (server.handle_event) over the REAL gate (db/schema.sql) with a FakeGitHub whose
main HEAD is UNRESOLVABLE (repo_default_branch_head returns an empty head), and asserts:
  (a) HEAD unresolvable + would-be clear      → advisory `neutral` "currency not confirmed" (NOT `success`)
  (b) HEAD unresolvable + a REAL collision     → the collision STANDS (action_required), NOT suppressed (recall)
  (c) HEAD resolvable + current                → the clear STANDS (`success`) — no over-firing
  (d) HEAD resolvable + behind-and-not-current → the EXISTING G1 stale-graph withhold still fires (unchanged)

(a) FAILS before the fix (a would-be clear over an unresolvable HEAD was emitted as `success`) and PASSES after.

Run:  python3 tests/test_g1_head_unresolvable_failclosed.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _server_harness import DB, OWNER_ID, REPO, REPO_ID, ROOT, SHA, FakeGitHub, make_db  # noqa: E402

sys.path.insert(0, os.path.join(ROOT, "github-app"))
import server as S  # noqa: E402
import webhook_handlers as WH  # noqa: E402

HEAD_AHEAD = "c" * 40          # a RESOLVABLE main HEAD ahead of the stored graph (for the behind-HEAD scenario)
STORED_SHA = SHA               # the stored graph is seeded here ("a"*40)


class HeadUnknownGitHub(FakeGitHub):
    """A FakeGitHub whose main HEAD can be made UNRESOLVABLE (repo_default_branch_head returns '' → graph_freshness
    head_sha None → behind None → self_heal_main_graph returns reason 'main HEAD unresolvable'), RESOLVABLE-CURRENT
    (head_sha == the stored sha → behind False → 'already current'), or RESOLVABLE-BEHIND with a failing re-ingest
    (head_sha ahead + heal_raises → self_heal returns reason 'ingest error' → the existing behind-and-not-current
    withhold). `head_sha=''` is the UNRESOLVABLE case this gate targets."""

    def __init__(self, files_by_pr, head_sha="", heal_raises=False, **kw):
        super().__init__(files_by_pr, **kw)
        self.head_sha = head_sha
        self.heal_raises = heal_raises

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha            # '' → head_sha None in graph_freshness → behind None (unresolvable)

    def download_tarball(self, repo, sha):
        if self.heal_raises:
            raise RuntimeError("simulated transient tarball fetch failure (network/permission/timeout)")
        return super().download_tarball(repo, sha)


def _pr_payload(action, number, author, changed_files=1, head_sha=None, merged=False):
    head_sha = head_sha or f"{number:040x}"
    return {"action": action, "number": number,
            "installation": {"id": 4242},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": OWNER_ID}},
            "pull_request": {"base": {"ref": "main", "sha": STORED_SHA,
                                       "repo": {"id": REPO_ID}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID}},
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
    db = make_db("veripsa_app")
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))

    checks = []

    # ---- (a) HEAD unresolvable + would-be clear → withheld to `neutral` "currency not confirmed" (NOT success) --
    # repo_default_branch_head returns '' → graph_freshness head_sha None → behind None → self_heal returns reason
    # 'main HEAD unresolvable'. A solo PR that would otherwise CLEAR must NOT show `success`; Veripsa cannot
    # confirm the stored graph is current (it cannot compare the stored sha to an unknown HEAD), so the would-be
    # clear is withheld as the honest head-unresolvable advisory `neutral`. THIS is the assertion that FAILS
    # before the fix (the clear stood as `success`) and PASSES after.
    _seed_graph_at(db, graph, STORED_SHA)
    ghA = HeadUnknownGitHub({2: ["backend/api.py"]}, head_sha="")
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {
        "head_sha": None, "reason": "main HEAD unresolvable"}
    S.handle_event("pull_request", _pr_payload("opened", 2, "bob"), db, ghA)
    head2 = f"{2:040x}"
    conclA = _conclusion(ghA, head2)
    titleA = _title(ghA, head2)
    cmtA = _comment_for(ghA, 2)
    checks.append(("a HEAD unresolvable + would-be clear → check is `neutral` (NOT a false `success`)",
                   conclA == "neutral" and conclA != "success"))
    checks.append(("a the withheld-clear check is the HEAD-UNRESOLVABLE advisory (names 'currency not confirmed' "
                   "+ 'not cleared', an honest Unknown — not a confident 'Clear')",
                   "currency not confirmed" in titleA.lower() and "not cleared" in titleA.lower()))
    checks.append(("a an advisory 'not cleared'/'unknown' comment is posted, honest about 'couldn't read the "
                   "latest commit' (content-free, jargon-free)",
                   bool(cmtA) and "not cleared" in cmtA["body"].lower() and "unknown" in cmtA["body"].lower()
                   and "couldn't read the latest commit" in cmtA["body"].lower()))
    # IDEMPOTENT: a synchronize on the SAME head PATCHES in place — no second comment (no flap).
    posts_before = len(ghA.comments)
    S.handle_event("pull_request", _pr_payload("synchronize", 2, "bob"), db, ghA)
    checks.append(("a the withheld-clear advisory is IDEMPOTENT (a synchronize PATCHES in place — no second comment)",
                   len(ghA.comments) == posts_before and _conclusion(ghA, head2) == "neutral"))
    S.handle_event("pull_request", _pr_payload("closed", 2, "bob"), db, ghA)   # release the lane for the next scenario

    # ---- (b) HEAD unresolvable + a REAL cross-PR collision → the collision STANDS (recall-safe negative control) -
    # Two PRs on the SAME file while main HEAD is unresolvable. PR-3 opens (would-be clear → withheld). PR-4 opens
    # on the same file → a REAL collision → material `action_required`. The head-unresolvable withhold must ONLY
    # downgrade a would-be clear — a real collision is NEVER suppressed by "single transient API failure".
    _seed_graph_at(db, graph, STORED_SHA)
    ghB = HeadUnknownGitHub({3: ["backend/worker.py"], 4: ["backend/worker.py"]}, head_sha="")
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {
        "head_sha": None, "reason": "main HEAD unresolvable"}
    S.handle_event("pull_request", _pr_payload("opened", 3, "carol"), db, ghB)
    S.handle_event("pull_request", _pr_payload("opened", 4, "dave"), db, ghB)
    head4 = f"{4:040x}"
    conclB4 = _conclusion(ghB, head4)
    titleB4 = _title(ghB, head4)
    checks.append(("b a REAL cross-PR collision while HEAD is unresolvable is STILL surfaced (PR-4 → "
                   "`action_required`, NOT downgraded/suppressed by the head-unresolvable withhold)",
                   conclB4 == "action_required"))
    checks.append(("b PR-4's material verdict is a real collision note, NOT the head-unresolvable 'currency not "
                   "confirmed' advisory (the withhold never touches a non-`success` verdict)",
                   "currency not confirmed" not in titleB4.lower() and "not cleared" not in titleB4.lower()))
    S.handle_event("pull_request", _pr_payload("closed", 3, "carol"), db, ghB)
    S.handle_event("pull_request", _pr_payload("closed", 4, "dave"), db, ghB)

    # ---- (c) HEAD resolvable + current → the clear STANDS (`success`) — no over-firing ------------------------
    # repo_default_branch_head returns the stored sha → graph_freshness head_sha == stored_sha → behind False →
    # self_heal 'already current'. Currency IS confirmed → a would-be clear must remain `success` (the withhold
    # must not fire when HEAD is readable and the graph is current).
    _seed_graph_at(db, graph, STORED_SHA)
    ghC = HeadUnknownGitHub({5: ["backend/billing.py"]}, head_sha=STORED_SHA)
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {
        "head_sha": STORED_SHA, "reason": "already current"}
    S.handle_event("pull_request", _pr_payload("opened", 5, "erin"), db, ghC)
    head5 = f"{5:040x}"
    conclC = _conclusion(ghC, head5)
    checks.append(("c HEAD resolvable + current → the clear STANDS (`success`) — the head-unresolvable withhold "
                   "does NOT over-fire when HEAD is readable and the graph is current",
                   conclC == "success"))
    S.handle_event("pull_request", _pr_payload("closed", 5, "erin"), db, ghC)

    # ---- (d) HEAD resolvable + behind-and-not-current → the EXISTING G1 stale-graph withhold still fires -------
    # head_sha ahead ("c") of the stored graph ("a") + the re-ingest RAISES → self_heal returns reason 'ingest
    # error' with head_sha PRESENT → _graph_degraded True → the SHIPPED behind-HEAD withhold fires UNCHANGED
    # ('code graph not confirmed current', the behind-HEAD wording). This proves the G1-tail branch did NOT
    # regress or absorb the shipped behind-and-not-current path.
    _seed_graph_at(db, graph, STORED_SHA)
    ghD = HeadUnknownGitHub({6: ["backend/repo.py"]}, head_sha=HEAD_AHEAD, heal_raises=True)
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {
        "head_sha": HEAD_AHEAD, "reason": "ingest error"}
    S.handle_event("pull_request", _pr_payload("opened", 6, "frank"), db, ghD)
    head6 = f"{6:040x}"
    conclD = _conclusion(ghD, head6)
    titleD = _title(ghD, head6)
    checks.append(("d HEAD resolvable + behind-and-not-current → the EXISTING G1 stale-graph withhold still fires "
                   "(`neutral`, the behind-HEAD 'not confirmed current' wording) — unchanged by the G1 tail",
                   conclD == "neutral" and "not confirmed current" in titleD.lower()
                   and "currency not confirmed" not in titleD.lower()))
    S.handle_event("pull_request", _pr_payload("closed", 6, "frank"), db, ghD)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("G1 HEAD UNRESOLVABLE FAILCLOSED GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
