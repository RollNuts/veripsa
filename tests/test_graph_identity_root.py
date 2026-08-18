#!/usr/bin/env python3
"""GRAPH IDENTITY ROOT gate — graph bytes never inherit a stale GitHub repository.id.

The mutable ``owner/name`` coordinate can be reused after transfer/delete.  A generic full/patch writer knows
only graph content, so every successful write must clear ``graph_version.repo_id``; only an authenticated App
event may re-stamp it.  Payload-less self-heal additionally needs a current point-read matching the signed or
inventory-derived expected repository id, owner id, and exact full_name.  Any 404, redirect/mismatch, malformed
response, or transient failure keeps the healed graph but leaves its stable identity NULL (lifecycle fail-closed).

Run: python3 tests/test_graph_identity_root.py  (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import code_graph_extract as X  # noqa: E402
import ingest as I  # noqa: E402

DB = "veripsa_graph_identity_" + str(os.getpid())
OWNER_ID = "918273645"
TENANT = "ACCT-GH-" + OWNER_ID
REPO = "acme/replacement"
BRANCH = "main"
ID1, ID2 = "111000111", "222000222"
GRAPH = {
    "extractor_version": X.EXTRACTOR_VERSION,
    "metrics": {"schema_contract_version": X.SCHEMA_CONTRACT_VERSION},
    "nodes": [{"id": "app.py", "kind": "file", "path": "app.py",
               "name": "app.py", "language": "python"}],
    "edges": [],
}


class FakeGH:
    def __init__(self):
        self.head = "a" * 40
        self.current = {"id": ID2, "owner_id": OWNER_ID, "full_name": REPO}
        self.current_error = None

    def repo_default_branch_head(self, repo):
        return BRANCH, self.head

    def repo_default_branch_name(self, repo):
        return BRANCH

    def installation_account_id(self):
        return OWNER_ID

    def repo_current_identity(self, repo):
        if self.current_error is not None:
            raise self.current_error
        return self.current

    def download_tarball(self, repo, sha):
        data = b"def changed():\n    return 1\n"
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tf:
            item = tarfile.TarInfo("repo-" + sha[:7] + "/app.py")
            item.size = len(data)
            tf.addfile(item, io.BytesIO(data))
        return out.getvalue()

    def get_file_at(self, repo, path, sha):
        return b"def changed():\n    return 2\n" if path == "app.py" else None

    def target_file_modes(self, repo, sha, paths):
        return {
            "complete": True,
            "truncated": False,
            "malformed": False,
            "over_cap": False,
            "entries": {
                path: {"mode": "100644", "type": "blob"}
                for path in paths
            },
        }


def _obj(value):
    return json.loads(value) if isinstance(value, str) else value


def main() -> int:
    boot = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
                          capture_output=True, text=True)
    if boot.returncode != 0:
        print("bootstrap failed:\n" + (boot.stdout + boot.stderr)[-1600:])
        return 1

    checks: list[tuple[str, bool]] = []
    conn = psycopg2.connect(f"postgresql://veripsa_app@localhost/{DB}")
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (OWNER_ID,))

    def db(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    def admin(sql, args=()):
        with psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}") as ac:
            with ac.cursor() as cur:
                cur.execute("SET search_path=core,pg_catalog")
                cur.execute("SELECT set_config('core.current_account',%s,true)", (TENANT,))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None

    def repo_id():
        value = admin("SELECT repo_id FROM core.graph_version WHERE account_id=%s AND repo=%s AND branch=%s",
                      (TENANT, REPO, BRANCH))
        return None if value is None else str(value)

    def stored_sha():
        return admin("SELECT commit_sha FROM core.graph_version WHERE account_id=%s AND repo=%s AND branch=%s",
                     (TENANT, REPO, BRANCH))

    def stored_revision():
        return admin(
            "SELECT graph_revision FROM core.graph_version "
            "WHERE account_id=%s AND repo=%s AND branch=%s",
            (TENANT, REPO, BRANCH),
        )

    def stamp(value):
        return admin("SELECT core.mark_governed_write('graph_version'); "
                     "UPDATE core.graph_version SET repo_id=%s WHERE account_id=%s AND repo=%s AND branch=%s "
                     "RETURNING repo_id", (value, TENANT, REPO, BRANCH))

    def full(sha, captured_at=None):
        return _obj(db("SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
                       (json.dumps(GRAPH), REPO, BRANCH, sha, captured_at)))

    def patch(sha, changed=None, captured_at=None):
        changed = ["app.py"] if changed is None else changed
        patch_graph = {
            **GRAPH,
            "expected_base_sha": stored_sha(),
            "expected_base_revision": stored_revision(),
        }
        return _obj(db("SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s,%s)",
                       (json.dumps(patch_graph), REPO, BRANCH, changed, [], sha, captured_at)))

    def add(label, passed):
        checks.append((label, bool(passed)))

    gh = FakeGH()
    original_increment = I.increment_cochange_async
    I.increment_cochange_async = lambda *a, **k: None
    try:
        # Generic graph writers root identity at NULL.  A delayed ID1 lifecycle predicate therefore has no exact
        # graph identity to match after a same-name replacement writes either a full graph or a patch.
        full("1" * 40)
        stamp(ID1)
        full_result = full("2" * 40)
        add("successful generic full ingest clears the inherited ID1", full_result.get("ok") and repo_id() is None)
        add("delayed ID1 lifecycle proof cannot match the replacement graph after full ingest",
            admin("SELECT count(*)::int FROM core.graph_version WHERE account_id=%s AND repo=%s AND repo_id=%s",
                  (TENANT, REPO, ID1)) == 0)

        stamp(ID1)
        patch_result = patch("3" * 40)
        add("successful generic patch clears the inherited ID1",
            patch_result.get("mode") == "patch" and repo_id() is None)

        # Every early return is non-mutating: stale full/patch, no-op patch, and quota refusal retain the stamp.
        full("4" * 40, "2026-07-16T12:00:00Z")
        stamp(ID1)
        stale_full = full("5" * 40, "2026-07-16T11:00:00Z")
        stale_patch = patch("6" * 40, captured_at="2026-07-16T11:00:00Z")
        noop_patch = patch("7" * 40, changed=[])
        add("stale full/patch and no-op patch do not clear identity",
            stale_full.get("stale") and stale_patch.get("stale") and noop_patch.get("noop")
            and repo_id() == ID1 and stored_sha() == "4" * 40)

        old_limit = int(admin("SELECT core._plan_graph_units_limit('free')"))
        db("SELECT core.set_plan_graph_units_limit_with_authority('free',0)")
        quota_full = full("8" * 40)
        quota_patch = patch("9" * 40)
        db("SELECT core.set_plan_graph_units_limit_with_authority('free',%s)", (old_limit,))
        add("quota-refused full/patch do not clear identity",
            quota_full.get("quota_exceeded") and quota_patch.get("quota_exceeded") and repo_id() == ID1)

        def push_payload(sha, *, before=None, forced=False):
            return {"ref": "refs/heads/main", "before": before or ("0" * 40),
                    "after": sha, "forced": forced,
                    "repository": {"id": ID2, "full_name": REPO, "default_branch": BRANCH,
                                   "owner": {"id": OWNER_ID}},
                    "head_commit": {"id": sha, "added": [], "modified": ["app.py"], "removed": []},
                    "commits": [{"added": [], "modified": ["app.py"], "removed": []}],
                    "sender": {"login": "alice", "type": "User"}}

        # The live App path resets then re-stamps inside its caller's transaction.  A second reconcile in that
        # still-open transaction sees stamped=0, proving both full and patch already converged before COMMIT.
        conn.autocommit = False
        full_push = I.ingest_push(db, gh, REPO, BRANCH, "a" * 40,
                                  payload=push_payload("a" * 40, forced=True), coalesce=None)
        full_again = _obj(db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, ID2)))
        full_sha_in_tx = _obj(db("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH))).get("commit_sha")
        conn.commit()
        add("signed App full push re-stamps current ID2 in the graph-write transaction",
            full_push.get("mode") == "full" and full_again.get("stamped") == 0
            and full_sha_in_tx == "a" * 40 and repo_id() == ID2)

        patch_push = I.ingest_push(db, gh, REPO, BRANCH, "b" * 40,
                                   payload=push_payload(
                                       "b" * 40, before="a" * 40),
                                   coalesce=None)
        patch_again = _obj(db("SELECT core.reconcile_repo_identity_with_authority(%s,%s)", (REPO, ID2)))
        patch_sha_in_tx = _obj(db("SELECT core.coordinate_graph_sha(%s,%s)", (REPO, BRANCH))).get("commit_sha")
        conn.commit()
        conn.autocommit = True
        add("signed App incremental push re-stamps current ID2 in the patch transaction",
            patch_push.get("mode") == "patch" and patch_again.get("stamped") == 0
            and patch_sha_in_tx == "b" * 40 and repo_id() == ID2)

        # Payload-less self-heal gets an independent point read.  Exact id/owner/full agreement re-stamps.
        full("c" * 40)
        stamp(ID2)
        gh.head = "d" * 40
        gh.current = {"id": ID2, "owner_id": OWNER_ID, "full_name": REPO}
        gh.current_error = None
        healed = I.self_heal_main_graph(db, gh, REPO, BRANCH,
                                        expected_repository_id=ID2, expected_owner_id=OWNER_ID)
        add("self-heal exact current identity re-stamps ID2",
            healed.get("healed") is True and repo_id() == ID2 and stored_sha() == "d" * 40)

        # Each failure case starts with a stamped old graph and a newer HEAD.  The graph heal succeeds, but the
        # writer's NULL root remains because point-read authority is missing or disagrees on one required field.
        bad_cases = [
            (None, None, "404/absence"),
            ({"id": ID1, "owner_id": OWNER_ID, "full_name": REPO}, None, "repository id mismatch"),
            ({"id": ID2, "owner_id": "777", "full_name": REPO}, None, "owner mismatch"),
            ({"id": ID2, "owner_id": OWNER_ID, "full_name": "other/replacement"}, None, "redirect/full mismatch"),
            ({"id": ID2}, None, "malformed response"),
            (None, RuntimeError("temporary API failure"), "transient failure"),
        ]
        hexes = iter("ef0123456789")
        for current, error, label in bad_cases:
            old_sha, new_sha = next(hexes) * 40, next(hexes) * 40
            full(old_sha)
            stamp(ID2)
            gh.head, gh.current, gh.current_error = new_sha, current, error
            outcome = I.self_heal_main_graph(db, gh, REPO, BRANCH,
                                             expected_repository_id=ID2, expected_owner_id=OWNER_ID)
            add(f"self-heal {label} keeps healed graph identity NULL",
                outcome.get("healed") is True and stored_sha() == new_sha and repo_id() is None)

        # BOOT ORDER REGRESSION: account inventory binds ID2, then an open-PR synthetic replay self-heals first.
        # That replay has repository.id but no signed owner.id, so its successful graph write must leave NULL.
        # The explicit boot heal then sees already-current.  _reconcile_one_repo's final authenticated convergence
        # must nevertheless restore ID2 from inventory owner/id + the exact current point read.
        full("a" * 40)
        stamp(ID2)
        gh.head = "b" * 40
        gh.current = {"id": ID2, "owner_id": OWNER_ID, "full_name": REPO}
        gh.current_error = None
        original_backfill = I.backfill_open_prs
        after_open_pr_replay = []

        def synthetic_open_pr_replay(db_arg, gh_arg, repo_arg, default_branch=None,
                                     repository_id=None, **_kwargs):
            replay_heal = I.self_heal_main_graph(
                db_arg, gh_arg, repo_arg, default_branch or BRANCH,
                expected_repository_id=repository_id, expected_owner_id=None)
            after_open_pr_replay.append(repo_id())
            return {"backfilled": repo_arg, "count": 1, "truncated": False, "results": [],
                    "open_change_ids": ["PR-1"], "default_branch": default_branch or BRANCH,
                    "synthetic_graph_heal": replay_heal}

        try:
            I.backfill_open_prs = synthetic_open_pr_replay
            boot_result = I._reconcile_one_repo(
                db, gh, REPO, f"postgresql://veripsa_app@localhost/{DB}", repository_id=ID2)
        finally:
            I.backfill_open_prs = original_backfill
        add("boot open-PR-first heal ends with authenticated identity restored",
            after_open_pr_replay == [None]
            and boot_result.get("repository_identity_restamped") is True
            and stored_sha() == "b" * 40 and repo_id() == ID2)
    finally:
        I.increment_cochange_async = original_increment
        try:
            if not conn.autocommit:
                conn.rollback()
        except Exception:
            pass
        conn.close()
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    for label, passed in checks:
        print(("  [PASS] " if passed else "  [FAIL] ") + label)
    ok = bool(checks) and all(passed for _, passed in checks)
    print("GRAPH IDENTITY ROOT GATE: " + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
