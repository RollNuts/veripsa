#!/usr/bin/env python3
"""RERUN-FORK-SAFETY gate (NO DB, fully offline): the check_suite/check_run *rerequested* replay must carry the
same fork-/freshness-bearing fields a real pull_request webhook does — or it silently degrades into a fork
INFO LEAK and a verdict regression.

THE BUG it locks out (round-1 audit A4-F1/F2/F5): the synthesized 'synchronize' replay used to be
`base: {"ref": ...}` only — no base.repo, no base.sha, no neighbor-refresh suppressor. So when an EXTERNAL
contributor pressed "Re-run" on a FORK PR's Veripsa check, the synchronize path read base_repo_id=None ⇒
is_fork=False ⇒ it posted the FULL, non-redacted body (the base repo's OTHER in-flight PR identities + paths)
on the fork PR's conversation — readable by the external contributor. (Plus: no base.sha ⇒ the verdict
degraded to file-level; no suppressor ⇒ one click fanned an N×M neighbor-refresh storm.)

This test drives the PURE replay builder webhook_handlers._rerun_replay directly (no Postgres, no GitHub) and
asserts the replay now carries base.repo (so is_fork is computable), the base repository's stable id at the
top-level lifecycle coordinate, base.sha, head.repo, and the
_veripsa_no_neighbor_refresh suppressor — and that the SAME is_fork formula the synchronize path uses now
returns True for a fork PR (and False for a non-fork, so nothing is over-redacted).

Run:  python3 tests/test_rerun_fork_safety.py     (no DB needed)
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import webhook_handlers as W  # noqa: E402


def _is_fork(replay: dict) -> bool:
    """Mirror of the synchronize path's is_fork formula (handle_event): head.repo.id != base.repo.id. The replay
    this gate guards must satisfy THIS contract — that is what drives fork redaction downstream."""
    prj = replay.get("pull_request") or {}
    head_repo_id = ((prj.get("head") or {}).get("repo") or {}).get("id")
    base_repo_id = ((prj.get("base") or {}).get("repo") or {}).get("id")
    return (replay.get("_veripsa_fork_identity_unknown") is True
            or (head_repo_id is not None and base_repo_id is not None and head_repo_id != base_repo_id))


def main() -> int:
    checks: list[tuple[str, bool]] = []

    # A FORK PR: head commit lives in repo 2, base is the upstream repo 1.
    fork_full = {"number": 7,
                 "base": {"ref": "main", "sha": "basesha123", "repo": {"id": 1, "full_name": "acme/app"}},
                 "head": {"ref": "feat", "sha": "headsha456", "repo": {"id": 2, "full_name": "ext/app"}},
                 "user": {"login": "ext-contributor"}, "draft": False, "merged": False}
    r = W._rerun_replay(fork_full, "acme/app", "main", "main", 7)
    pr = r["pull_request"]

    checks.append(("replay carries base.repo (so the synchronize path can COMPUTE is_fork)",
                   (pr["base"].get("repo") or {}).get("id") == 1))
    checks.append(("replay carries the authoritative base repository id through the top-level lifecycle guard",
                   (r.get("repository") or {}).get("id") == 1))
    checks.append(("replay carries head.repo (the other side of is_fork)",
                   (pr["head"].get("repo") or {}).get("id") == 2))
    checks.append(("replay carries base.sha (the finer-collision freshness input — no silent file-level degrade)",
                   pr["base"].get("sha") == "basesha123"))
    checks.append(("replay suppresses the neighbor-refresh storm (one re-run click != N×M GitHub posts)",
                   r.get("_veripsa_no_neighbor_refresh") is True))
    checks.append(("THE LEAK FIX: a re-run on a FORK PR is now detected as a fork (was False ⇒ non-redacted leak)",
                   _is_fork(r) is True))
    checks.append(("replay is a synchronize for the right PR number",
                   r.get("action") == "synchronize" and r.get("number") == 7))
    checks.append(("replay preserves draft/merged/user from the authoritative PR",
                   pr.get("draft") is False and pr.get("merged") is False
                   and (pr.get("user") or {}).get("login") == "ext-contributor"))

    # A NON-fork PR: head and base in the SAME repo — must NOT be flagged a fork (no over-redaction of normal PRs).
    nf_full = {"number": 8,
               "base": {"ref": "main", "sha": "b2", "repo": {"id": 1}},
               "head": {"ref": "feat", "sha": "h2", "repo": {"id": 1}},
               "user": {"login": "teammate"}, "draft": "false", "merged": "false"}
    rn = W._rerun_replay(nf_full, "acme/app", "main", "main", 8)
    checks.append(("a re-run on a NON-fork PR is NOT a fork (the fix doesn't over-redact normal PRs)",
                   _is_fork(rn) is False))
    checks.append(("a non-fork replay carries the same stable base repository id",
                   (rn.get("repository") or {}).get("id") == 1))
    checks.append(("malformed truthy draft/merged strings do not change replay state",
                   rn["pull_request"].get("draft") is False and rn["pull_request"].get("merged") is False))

    # NEVER-CRASH: a sparse / missing base+head object (a stub PR object) must degrade to {} — not raise.
    sparse = W._rerun_replay({"number": 9}, "acme/app", "main", "main", 9)
    checks.append(("fail-soft: a sparse PR object (no base/head) does not crash the builder",
                   sparse["pull_request"]["base"].get("repo") == {} and sparse["pull_request"]["head"] == {}))
    checks.append(("fail-soft: a sparse PR object does not invent a repository id",
                   "id" not in sparse.get("repository", {})))
    # Unknown fork identity is privacy-sensitive: a deleted fork can return head.repo=null. The replay must remain
    # processable but use the redacted surface instead of assuming same-repo and exposing base-repo details.
    checks.append(("privacy fail-closed: a sparse replay is redacted as potentially forked",
                   _is_fork(sparse) is True))

    ok = all(c[1] for c in checks)
    print("\n=== RERUN-FORK-SAFETY (pure, offline) ===")
    for name, passed in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {name}")
    print("\nRERUN-FORK-SAFETY GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
