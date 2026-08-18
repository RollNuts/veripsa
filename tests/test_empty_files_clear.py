#!/usr/bin/env python3
"""EMPTY-FILES WRONG-CLEAR gate — the audited silent-miss (a wrong-clear-on-error).

GitHub's changed-files API can return an EMPTY list on a 200 WITHOUT raising (a proxy/CDN edge serving an empty
body, a 204, eventual-consistency right after a push). That empty list bypasses webhook_handlers' fetch
try/except (which only catches a RAISE) and is then INDISTINGUISHABLE from a genuine 0-file PR — so the brain
runs reconcile(cid, …, []) (RELEASING the PR's lanes) and renders `success` ("clear to land"). That is a SILENT
MISS over a real overlap, and WORSE on a synchronize: a PR that was correctly action_required flips to success
AND drops out of the in-flight set (its real collision counterpart loses its partner); if that is the last event
before merge, it merges GREEN over a real collision.

THE FIX (recall-safe): cross-check the empty file list against the PR's OWN changed-files COUNT — the
pull_request object carries `changed_files` (an int) independently of the Files API. A RAW (pre-_code_paths)
empty list while the PR object reports changed_files > 0 is a PROVABLE read failure → HONEST-UNKNOWN: do NOT
analyze (so reconcile never releases lanes), post an advisory `neutral` "couldn't read changed files — not
analyzed" (NEVER success). The genuine 0-code-file case is preserved: a docs-only PR returns a NON-empty raw
list that _code_paths filters to [] → raw non-empty → the guard does not fire → it stays `success`.

This drives the REAL handler (server.handle_event) over the REAL gate (db/schema.sql) with a FakeGitHub whose
Files API returns [] for the chosen PRs (the non-raising 200-empty-body), and asserts:
  (a) empty-Files-API + changed_files>0          → neutral/skip, NO lane released, the collision still surfaces
                                                    on the counterpart (recall preserved);
  (b) genuine docs-only PR (README.md)            → success (the 0-code-file clear is preserved);
  (c) a real 0-change PR (changed_files==0)       → success (the guard does not fire — recall-safe).

Run:  python3 tests/test_empty_files_clear.py   (needs local Postgres with the veripsa roles)
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


class EmptyFilesGitHub(FakeGitHub):
    """A FakeGitHub whose changed-files API returns [] WITHOUT raising for the PR numbers in `empty_for` —
    exactly the 200-with-empty-body (proxy/CDN edge / 204 / eventual-consistency) the real list_pr_files yields.
    Deliberately does NOT implement list_pr_files_with_ranges, so the handler's hasattr check falls back to
    list_pr_files (the path under test). `empty_for` is mutable so a scenario can flip a PR to empty mid-test."""
    def __init__(self, files_by_pr, empty_for=frozenset(), **kw):
        super().__init__(files_by_pr, **kw)
        self.empty_for = set(empty_for)

    def list_pr_files(self, repo, number, pr_changed_files=0):
        if number in self.empty_for:
            return []                          # 200-empty-body: a NON-raising empty list (the bug's trigger)
        return super().list_pr_files(repo, number, pr_changed_files)

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        if number in self.empty_for:
            return {"changed": [], "changed_ranges": {}, "added_paths": [], "conflict_markers": []}
        return super().list_pr_file_metadata(repo, number, pr_changed_files, max_pages)

    def repo_default_branch_head(self, repo):
        return "main", SHA


def _pr_payload(action, number, author, changed_files, head_sha=None, merged=False):
    """A pull_request payload that ALSO carries the GitHub-provided changed_files INT count — the cross-check
    field. A real pull_request webhook + GET /pulls/{n} both carry pull_request.changed_files (int)."""
    head_sha = head_sha or f"{number:040x}"
    return {"action": action, "number": number,
            "installation": {"id": 4242},
            "repository": {"id": REPO_ID, "full_name": REPO, "default_branch": "main",
                           "owner": {"id": OWNER_ID}},
            "pull_request": {"base": {"ref": "main", "sha": SHA, "repo": {"id": REPO_ID}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID}},
                             "user": {"login": author}, "merged": merged,
                             "changed_files": changed_files}}


def _conclusion(gh, head_sha):
    c = next((c for c in gh.checks if c["sha"] == head_sha), None)
    return c.get("conclusion") if c else None


def _comment_for(gh, number):
    return next((c for c in gh.comments if c["number"] == number), None)


def _surface(db):
    impact = db("SELECT core.main_impact_surface(%s,%s)", (REPO, "main")) or {}
    if isinstance(impact, str):
        impact = json.loads(impact)
    return {c.get("change_id"): c for c in impact.get("changes", []) if isinstance(c, dict)}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1
    db = make_db("veripsa_app")
    sys.path.insert(0, ROOT)
    import code_graph_extract as X
    graph = X.build_graph(os.path.join(ROOT, "tests", "fixtures", "sample_app"))
    db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s)", (json.dumps(graph), REPO, "main", SHA))
    db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, str(REPO_ID)))
    WH.request_main_graph_refresh_wake_only = lambda *args, **kwargs: {"queued": False, "current": True}

    checks = []

    # ---- (a) OPEN-TIME wrong-clear: a real collision hidden by an empty Files API ------------------------
    # PR-2 (bob) genuinely edits backend/api.py. PR-3 (carol) ALSO edits backend/api.py → a REAL collision —
    # but PR-3's Files API returns [] (the bug), while PR-3 honestly reports changed_files=1.
    ghA = EmptyFilesGitHub({2: ["backend/api.py"], 3: ["backend/api.py"]}, empty_for={3})
    S.handle_event("pull_request", _pr_payload("opened", 2, "bob", 1), db, ghA)
    S.handle_event("pull_request", _pr_payload("opened", 3, "carol", 1), db, ghA)
    head3 = f"{3:040x}"
    surfA = _surface(db)
    concl3 = _conclusion(ghA, head3)
    cmt3 = _comment_for(ghA, 3)
    # The check is NEUTRAL (not success), an advisory note is posted, and the PR did NOT claim a lane.
    checks.append(("OPEN empty-Files-API + changed_files>0 → check is `neutral` (NOT a false `success`/clear)",
                   concl3 == "neutral"))
    checks.append(("OPEN empty-Files-API → an advisory 'not analyzed' comment is posted (honest, content-free)",
                   bool(cmt3) and "not analyzed" in cmt3["body"].lower()))
    checks.append(("OPEN empty-Files-API → the PR did NOT claim a lane (no reconcile ran on an empty set)",
                   "PR-3" not in surfA))
    # RECALL PRESERVED: the counterpart PR-2 (read fine) STILL serializes — the real collision is NOT lost just
    # because PR-3 couldn't be read (PR-2 holds the lane; with only PR-2 present it is the clear lane holder,
    # and the moment PR-3 is re-read it will collide again). Assert PR-2 is present + has a real (non-clear-by-
    # accident) reservation on the contested path.
    checks.append(("OPEN empty-Files-API → the counterpart PR-2 is still surfaced (collision not silently dropped)",
                   "PR-2" in surfA and "backend/api.py" in (surfA.get("PR-2", {}).get("paths") or [])))

    # ---- (b) SYNCHRONIZE erases a real verdict (the WORSE variant) ---------------------------------------
    # PR-5 (dave) + PR-6 (erin) both edit backend/api.py → PR-6 is correctly action_required (paused material).
    ghB = EmptyFilesGitHub({5: ["backend/api.py"], 6: ["backend/api.py"]}, empty_for=set())
    S.handle_event("pull_request", _pr_payload("opened", 5, "dave", 1), db, ghB)
    S.handle_event("pull_request", _pr_payload("opened", 6, "erin", 1), db, ghB)
    head6 = f"{6:040x}"
    surfB0 = _surface(db)
    concl6_before = _conclusion(ghB, head6)
    v5_before = (surfB0.get("PR-5", {}) or {}).get("verdict")
    checks.append(("SYNC setup: PR-6 starts action_required (a real material collision) and PR-5 sees it (serialize)",
                   concl6_before == "action_required" and v5_before == "serialize"))
    # Now a synchronize whose Files API returns [] (the bug). changed_files is still 1 (honest).
    ghB.empty_for = {6}
    S.handle_event("pull_request", _pr_payload("synchronize", 6, "erin", 1), db, ghB)
    surfB1 = _surface(db)
    concl6_after = _conclusion(ghB, head6)
    v5_after = (surfB1.get("PR-5", {}) or {}).get("verdict")
    # The verdict is NOT flipped to a false green, and PR-6's lanes are NOT released (it stays in flight).
    checks.append(("SYNC empty-Files-API → PR-6 does NOT flip to a false `success` (stays `neutral`, not cleared)",
                   concl6_after == "neutral"))
    checks.append(("SYNC empty-Files-API → PR-6's lanes are NOT released (it stays in the in-flight surface)",
                   "PR-6" in surfB1))
    # THE CRUX (the merge-green-over-collision fix): the counterpart PR-5 STILL sees the collision — its verdict
    # is unchanged by PR-6's unreadable synchronize. Without the fix, reconcile(PR-6, []) released PR-6's lane and
    # PR-5 dropped to clear.
    checks.append(("SYNC empty-Files-API → the counterpart PR-5 STILL shows the collision (verdict unchanged: serialize)",
                   v5_after == "serialize"))

    # ---- (c) GENUINE docs-only PR is PRESERVED (recall-safe — not a false unknown) -----------------------
    # PR-8 (frank) edits ONLY README.md — a NON-empty RAW list that _code_paths filters to [] (a real 0-code-file
    # clear). The guard must NOT fire (raw is non-empty), so it stays `success`.
    ghC = EmptyFilesGitHub({8: ["README.md"]}, empty_for=set())
    S.handle_event("pull_request", _pr_payload("opened", 8, "frank", 1), db, ghC)
    head8 = f"{8:040x}"
    concl8 = _conclusion(ghC, head8)
    checks.append(("docs-only PR (README.md) stays `success` — the genuine 0-code-file clear is preserved",
                   concl8 == "success"))

    # ---- (d) A REAL 0-CHANGE PR (changed_files==0) is NOT turned into a false unknown --------------------
    # PR-9 (gail): empty Files API AND the PR object honestly reports changed_files==0 (a genuine no-file PR).
    # The cross-check must NOT fire (0 is not > 0) → today's behavior (a clean `success`), never a false unknown.
    ghD = EmptyFilesGitHub({}, empty_for={9})
    ghD.pr_objects[9] = {"changed_files": 0}  # authoritative GET(PR) agrees this is a genuine empty PR
    S.handle_event("pull_request", _pr_payload("opened", 9, "gail", 0), db, ghD)
    head9 = f"{9:040x}"
    concl9 = _conclusion(ghD, head9)
    checks.append(("real 0-change PR (changed_files==0) stays `success` — the guard is recall-safe (no false unknown)",
                   concl9 == "success"))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("EMPTY FILES CLEAR GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
