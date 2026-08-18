#!/usr/bin/env python3
"""Branch-delete lane release gate.

Regression for issue #765: a non-main branch push reserves lanes under
BR-<branch>. If the remote branch is deleted before, or without, a PR owning
that branch, the delete push must release those BR lanes immediately. Otherwise
later unrelated PRs can wait behind a ghost branch that no longer exists.

Run: python3 tests/test_branch_delete_lane_release.py
"""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, os.path.join(ROOT, "tests"))

import psycopg2  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402
from test_draft_branch_lane_leak import (  # noqa: E402
    ACCOUNT_ID, TENANT, FakeGitHub as DraftFakeGitHub, _pr, _push, _repo_id)

DB = "veripsa_branchdel_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"
checks: list[bool] = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def _delete_push(repo: str, branch: str, before: str = "d" * 40) -> dict:
    return {
        "ref": f"refs/heads/{branch}",
        "before": before,
        "after": "0" * 40,
        "deleted": True,
        "installation": {"id": 4242},
        "repository": {"id": _repo_id(repo), "full_name": repo, "default_branch": "main",
                       "owner": {"id": ACCOUNT_ID}},
        "commits": [],
        "pusher": {"name": "dev"},
    }


class FakeGitHub(DraftFakeGitHub):
    def list_open_pull_requests(self, repo, cap=None):
        return []


def admin(sql, args=()):
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
            cur.execute(sql, args)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                row = None
            conn.commit()
            return row[0] if row else None
    finally:
        conn.close()


def live_cid(repo: str, cid: str) -> int:
    return admin(
        """SELECT count(*)::int FROM core.claim
              WHERE repo=%s AND change_id=%s AND claim_state IN ('active','waiting')""",
        (repo, cid),
    ) or 0


def live_all(repo: str) -> int:
    return admin(
        """SELECT count(*)::int FROM core.claim
              WHERE repo=%s AND claim_state IN ('active','waiting')""",
        (repo,),
    ) or 0


def claim_state(repo: str, cid: str) -> str:
    return admin(
        """SELECT claim_state FROM core.claim
              WHERE repo=%s AND change_id=%s
              ORDER BY claimed_at DESC LIMIT 1""",
        (repo, cid),
    ) or ""


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    seed_live_installation(
        DSN_APP,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        ACCOUNT_ID,
        4242,
    )

    print("BRANCH DELETE LANE RELEASE GATE")
    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)

    def deliver(event_type: str, payload: dict):
        return proc(event_type, payload, None, gh)

    repo = "acme/branch-delete"
    branch = "feat/ghost"
    cid = "BR-feat/ghost"
    file_path = "backend/api.py"

    deliver("push", _push(repo, "main", "b" * 40))
    deliver("push", _push(repo, branch, "e" * 40, files=[file_path]))
    after_push = live_cid(repo, cid)
    chk(after_push == 1, f"(A) feature push reserves one BR lane before a PR exists (got {after_push})")

    deliver("push", _delete_push(repo, branch))
    after_delete = live_cid(repo, cid)
    total_after_delete = live_all(repo)
    chk(
        after_delete == 0 and total_after_delete == 0,
        "(B) branch delete releases the BR lane immediately; no ghost in-flight remains "
        f"(br={after_delete} total={total_after_delete})",
    )

    gh.files_by_pr = {21: [file_path]}
    deliver("pull_request", _pr("opened", repo, 21, "bea", "f" * 40, head_ref="feat/new"))
    pr_state = claim_state(repo, "PR-21")
    total_with_pr = live_all(repo)
    chk(
        pr_state == "active" and total_with_pr == 1,
        "C) later PR on the same file is active by itself, not waiting behind the deleted branch "
        f"(state={pr_state} total={total_with_pr})",
    )

    repo2 = "acme/branch-delete-repush"
    deliver("push", _push(repo2, "main", "c" * 40))
    deliver("push", _push(repo2, branch, "a" * 40, files=[file_path]))
    deliver("push", _delete_push(repo2, branch, before="a" * 40))
    deliver("push", _push(repo2, branch, "b" * 40, files=[file_path]))
    repush_lanes = live_cid(repo2, cid)
    chk(
        repush_lanes == 1,
        f"(D) deleting a branch is not a tombstone; a later push to the same branch can reserve again (got {repush_lanes})",
    )

    print()
    if all(checks):
        print("BRANCH DELETE LANE RELEASE GATE: PASS")
        return 0
    print(f"BRANCH DELETE LANE RELEASE GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
