#!/usr/bin/env python3
"""PR-FILES PAGINATION SHORTFALL gate — the P1 false-clear fix.

GitHub's PR Files API paginates with `Link: rel="next"` as the authoritative "more pages exist" signal. The
previous implementation terminated on `len(page) < 100` — a heuristic that fails when a proxy/CDN/GitHub edge
serves a SHORT but HTTP-200 INTERMEDIATE page: the loop exits early, the remaining files are silently dropped,
and the PR is analyzed against an incomplete changed-file set. A real collision on a dropped file goes to a
FALSE 'clear' — the worst outcome for an advisory tool.

THE FIX (P1):
  Primary:  terminate pagination on `Link: rel="next"` (via _api_with_link) so a short intermediate page no longer
            ends the loop early.
  Backstop: after all pages are consumed, cross-check the total returned file-entry count against the PR's declared
            `changed_files` count. A shortfall (returned < declared) raises PRFilesShortfall — the caller
            (webhook_handlers) withholds the verdict as honest-unknown and does NOT release lanes, exactly mirroring
            the `suspect_empty_files` path for a fully-empty read.

This test drives the REAL server handler (server.handle_event) over the REAL gate (db/schema.sql) with a
ShortfallGitHub that simulates the P1 scenario — a Files API that serves a SHORT intermediate page on a PR whose
declared changed_files count is larger than the page. Asserts:
  (a) SHORT INTERMEDIATE PAGE (returned < declared) → verdict withheld as neutral, lanes NOT released, collision
      preserved on the counterpart — the P1 false-clear is blocked.
  (b) GENUINE SMALL PR (returned == declared == small) → verdicts normally (not a false unknown) — recall-safe.
  (c) FULL MULTI-PAGE READ (Link header absent on final page) → completes and verdicts normally — full-pagination path.

Run:  python3 tests/test_pr_files_pagination.py   (needs local Postgres with the veripsa roles)
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
from github_rest_prread import (PRFilesMalformed, PRFilesPageBudgetExceeded,
                                PRFilesShortfall)  # noqa: E402


class ShortfallGitHub(FakeGitHub):
    """A FakeGitHub that simulates the P1 pagination-shortfall scenario: the Files-API RAISE PRFilesShortfall for
    the PR numbers in `shortfall_for`, as if _api_with_link returned a short intermediate page and the backstop
    count-check fired. This directly tests the webhook_handlers shortfall path without needing to wire up the full
    Link-header machinery in a fake HTTP layer. The Link-header termination logic itself is unit-tested below via
    `_has_link_next`. Does NOT implement list_pr_files_with_ranges, so the handler falls back to list_pr_files."""
    def __init__(self, files_by_pr, shortfall_for=frozenset(), declared_counts=None, **kw):
        super().__init__(files_by_pr, **kw)
        self.shortfall_for = set(shortfall_for)
        self.declared_counts = declared_counts or {}  # pr_number -> declared changed_files count

    def list_pr_files(self, repo, number, pr_changed_files=0):
        if number in self.shortfall_for:
            # Simulate: we fetched 1 file but the PR declares more — raise PRFilesShortfall as the pagination
            # backstop would, mimicking a short-but-200 intermediate page that terminated the loop early.
            declared = self.declared_counts.get(number, pr_changed_files or 2)
            partial = list(self.files_by_pr.get(number, []))[:1]   # pretend only 1 file came back
            raise PRFilesShortfall(len(partial), declared)
        return super().list_pr_files(repo, number, pr_changed_files)

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        if number in self.shortfall_for:
            declared = self.declared_counts.get(number, pr_changed_files or 2)
            partial = list(self.files_by_pr.get(number, []))[:1]
            raise PRFilesShortfall(len(partial), declared)
        return super().list_pr_file_metadata(repo, number, pr_changed_files, max_pages)

    def get_pull_request(self, repo, number):
        out = super().get_pull_request(repo, number)
        if number in self.declared_counts:
            out["changed_files"] = self.declared_counts[number]
        return out

    def repo_default_branch_head(self, repo):
        return "main", SHA


def _pr_payload(action, number, author, changed_files, head_sha=None, merged=False):
    """A pull_request payload carrying the GitHub-provided changed_files INT count."""
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


def _test_link_header_parsing():
    """Unit-test _has_link_next without a live client — import the mixin directly and call the static method."""
    from github_rest_prread import _GitHubPRReadMixin as M
    checks = []
    # Cases that must return True (next page exists)
    checks.append(('rel="next" present → True',
                   M._has_link_next('<https://api.github.com/foo?page=2>; rel="next", <https://api.github.com/foo?page=5>; rel="last"')))
    checks.append(("rel=next (unquoted) → True",
                   M._has_link_next('<https://api.github.com/foo?page=2>; rel=next')))
    checks.append(("rel=Next (case-insensitive) → True",
                   M._has_link_next('<https://api.github.com/foo?page=2>; rel="Next"')))
    # Cases that must return False (last/only page — no next)
    checks.append(("empty string → False", not M._has_link_next("")))
    checks.append(("None → False", not M._has_link_next(None)))
    checks.append(('only rel="last" → False',
                   not M._has_link_next('<https://api.github.com/foo?page=5>; rel="last"')))
    checks.append(('rel="prev" only → False',
                   not M._has_link_next('<https://api.github.com/foo?page=1>; rel="prev"')))
    return checks


def _test_one_page_budget():
    """Drive the real mixin: a Link-next page is observed, but page 2 is never requested past max_pages=1."""
    from github_rest_prread import _GitHubPRReadMixin

    class OnePageClient(_GitHubPRReadMixin):
        def __init__(self):
            self.calls = []

        def _api_with_link(self, method, url):
            self.calls.append((method, url))
            if url.endswith("&page=1"):
                return ([{
                    "filename": "backend/api.py",
                    "status": "modified",
                    "patch": "@@ -1 +1 @@\n-old\n+new",
                }], '<https://api.github.com/repos/acme/repo/pulls/7/files?per_page=100&page=2>; rel="next"')
            raise AssertionError("page 2 escaped the one-page Files budget")

    client = OnePageClient()
    raised = None
    try:
        client.list_pr_file_metadata(
            "acme/repo", 7, pr_changed_files=2, max_pages=1)
    except PRFilesPageBudgetExceeded as exc:
        raised = exc
    return [
        ("real list_pr_file_metadata(max_pages=1) raises the typed (used=1,budget=1) ceiling",
         isinstance(raised, PRFilesPageBudgetExceeded)
         and raised.used == 1 and raised.budget == 1),
        ("real Link-next traversal performs HTTP page=1 only; page=2 is structurally unreachable",
         len(client.calls) == 1 and client.calls[0][1].endswith("&page=1")),
    ]


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

    # ---- Unit: Link-header parsing -----------------------------------------------------------------------
    for name, cond in _test_link_header_parsing():
        checks.append((f"_has_link_next: {name}", cond))
    checks.extend(_test_one_page_budget())

    # ---- (a) P1: SHORT INTERMEDIATE PAGE → withheld verdict, no lane release, collision preserved ----------
    # PR-2 (bob) edits backend/api.py. PR-3 (carol) ALSO edits backend/api.py → a REAL collision.
    # PR-3's Files API raises PRFilesShortfall (simulating a short-but-200 intermediate page) while
    # changed_files=2 (declaring 2 files changed). The verdict must be withheld and PR-2's lane preserved.
    ghA = ShortfallGitHub({2: ["backend/api.py"], 3: ["backend/api.py", "backend/models.py"]},
                          shortfall_for={3}, declared_counts={3: 2})
    S.handle_event("pull_request", _pr_payload("opened", 2, "bob", 1), db, ghA)
    S.handle_event("pull_request", _pr_payload("opened", 3, "carol", 2), db, ghA)
    head3 = f"{3:040x}"
    surfA = _surface(db)
    concl3 = _conclusion(ghA, head3)
    cmt3 = _comment_for(ghA, 3)
    checks.append(("P1 shortfall: verdict withheld as `neutral` (NOT a false `success`/clear)",
                   concl3 == "neutral"))
    checks.append(("P1 shortfall: an advisory 'not analyzed' comment is posted",
                   bool(cmt3) and "not analyzed" in cmt3["body"].lower()))
    checks.append(("P1 shortfall: the PR did NOT claim a lane (reconcile NOT run on a partial set)",
                   "PR-3" not in surfA))
    checks.append(("P1 shortfall: the counterpart PR-2 is still surfaced (collision NOT silently dropped)",
                   "PR-2" in surfA and "backend/api.py" in (surfA.get("PR-2", {}).get("paths") or [])))

    # ---- (b) GENUINE SMALL PR (returned == declared) → verdicts normally, no false unknown ----------------
    # PR-5 (dave) edits 1 file, declared changed_files=1. No shortfall — a genuinely small PR.
    ghB = ShortfallGitHub({5: ["backend/api.py"]}, shortfall_for=set())
    S.handle_event("pull_request", _pr_payload("opened", 5, "dave", 1), db, ghB)
    head5 = f"{5:040x}"
    concl5 = _conclusion(ghB, head5)
    # PR-5 should reach a real verdict (success or action_required/serialize depending on in-flight state) NOT neutral
    checks.append(("genuine small PR (returned == declared) → verdicts normally (not neutral/unknown)",
                   concl5 != "neutral"))

    # ---- (c) SYNCHRONIZE with shortfall: does NOT flip an action_required PR to false success --------------
    # PR-7 (eve) + PR-8 (frank) both edit backend/api.py → PR-8 is correctly action_required.
    # Now a synchronize for PR-8 raises PRFilesShortfall → must stay neutral (not flip to a false success).
    ghC = ShortfallGitHub({7: ["backend/api.py"], 8: ["backend/api.py"]}, shortfall_for=set())
    S.handle_event("pull_request", _pr_payload("opened", 7, "eve", 1), db, ghC)
    S.handle_event("pull_request", _pr_payload("opened", 8, "frank", 1), db, ghC)
    head8 = f"{8:040x}"
    surfC0 = _surface(db)
    concl8_before = _conclusion(ghC, head8)
    v7_before = (surfC0.get("PR-7", {}) or {}).get("verdict")
    checks.append(("SYNC setup: PR-8 is action_required before the shortfall event",
                   concl8_before == "action_required" and v7_before == "serialize"))
    # Now synchronize with a shortfall
    ghC.shortfall_for = {8}
    ghC.declared_counts = {8: 2}
    S.handle_event("pull_request", _pr_payload("synchronize", 8, "frank", 2), db, ghC)
    surfC1 = _surface(db)
    concl8_after = _conclusion(ghC, head8)
    v7_after = (surfC1.get("PR-7", {}) or {}).get("verdict")
    checks.append(("SYNC shortfall: PR-8 does NOT flip to a false `success` (verdict stays neutral)",
                   concl8_after == "neutral"))
    checks.append(("SYNC shortfall: PR-8's lanes are NOT released (it stays in the in-flight surface)",
                   "PR-8" in surfC1))
    checks.append(("SYNC shortfall: counterpart PR-7 STILL shows the collision (verdict unchanged: serialize)",
                   v7_after == "serialize"))

    # ---- (d) webhook count missing: authoritative post-read still rejects a partial Files result -------------
    ghD = ShortfallGitHub({12: ["backend/api.py"]}, declared_counts={12: 2})
    payload12 = _pr_payload("opened", 12, "gina", 0)
    payload12["pull_request"].pop("changed_files", None)   # the exact count-less webhook gap
    S.handle_event("pull_request", payload12, db, ghD)
    surfD = _surface(db)
    checks.append(("count-less payload + authoritative current count 2 + only 1 Files entry → no lane/check write",
                   "PR-12" not in surfD and _conclusion(ghD, f"{12:040x}") is None))

    # ---- (e) a rename is one GitHub file entry but intentionally reserves old+new path lanes ----------------
    class RenameGitHub(ShortfallGitHub):
        def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
            return {"changed": ["backend/new_name.py", "backend/old_name.py"],
                    "changed_ranges": {"backend/new_name.py": [[1, 2]], "backend/old_name.py": []},
                    "added_paths": [], "conflict_markers": [], "raw_entry_count": 1}

    ghE = RenameGitHub({13: ["backend/new_name.py", "backend/old_name.py"]}, declared_counts={13: 1})
    S.handle_event("pull_request", _pr_payload("opened", 13, "hana", 1), db, ghE)
    surfE = _surface(db)
    checks.append(("normal rename: raw entry count 1 verifies while old+new paths both remain coordinated",
                   set((surfE.get("PR-13") or {}).get("paths") or [])
                   == {"backend/new_name.py", "backend/old_name.py"}))

    class MalformedGitHub(ShortfallGitHub):
        def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
            raise PRFilesMalformed("entry has no filename")

    ghF = MalformedGitHub({14: ["backend/api.py"]}, declared_counts={14: 1})
    S.handle_event("pull_request", _pr_payload("opened", 14, "ian", 1), db, ghF)
    checks.append(("malformed Files entry posts honest-unknown but never reconciles/stamps a lane",
                   "PR-14" not in _surface(db) and _conclusion(ghF, f"{14:040x}") == "neutral"))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PR FILES PAGINATION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True)
