#!/usr/bin/env python3
"""RENAME-COORDINATE-ORPHAN gate — a graph coordinate can orphan after a repository or owner rename and then
fire graph_stale forever because no future push addresses the old full name.

The synthetic fixture models one stable repository id observed under an old and a new full name. A missed
rename event leaves the old coordinate behind while pushes keep the new coordinate fresh. The ordinary
same-(repo, branch) orphan exclusion cannot group those differently named coordinates.

THE FIX this gate proves (content-free, fail-safe):
  RENAME DETECTION + COORDINATE MIGRATION keyed on GitHub's rename-STABLE repository.id (fixed across BOTH a repo
  rename and an owner-login rename, carried on every push payload). Every push STAMPS that id onto its coordinate
  (core.graph_version.repo_id) and, if a DIFFERENTLY-NAMED coordinate under the SAME tenant already carries that
  id (a rename we never webhooked), MIGRATES it old→new (core.reconcile_repo_identity_with_authority →
  _migrate_repo_coordinate, dedup-aware: when the new name already has a fresher post-rename coordinate, the stale
  old row is DROPPED, not collided). So the orphan is GONE and graph_stale stops spamming.

Driven through the LIVE per-event path (server.make_db_processor → handle_event), exactly like test_lifecycle_e2e
— the same recording FakeGitHub; only the GitHub I/O is faked, the brain + gate are real. State is read back as
the migrator with the tenant pinned.

Run:  python3 tests/test_rename_coordinate_orphan.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import policy_refresh_queue as PR  # noqa: E402
import server as S  # noqa: E402
from _installation_fixture import seed_live_installation  # noqa: E402

DB = "veripsa_renameorphan_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"

# the prod shape: ONE GitHub repo (a STABLE repository.id) owned by ONE account (a STABLE owner id → the tenant),
# renamed at the OWNER level. The owner ACCOUNT id never changes (rename-safe tenant) — only the login does, so
# full_name's owner segment flips example-user/… → RollNuts/….
INSTALL_ID = 277001
OWNER_ID = 42424242                      # the stable owning-account id → ACCT-GH-42424242 (matches the prod tenant)
TENANT = f"ACCT-GH-{OWNER_ID}"
REPO_ID = 543210987                       # the STABLE GitHub repository.id (fixed across the login rename)
OLD_REPO = "example-user/veripsa-core-old"         # full_name BEFORE the owner-login rename (the orphan)
NEW_REPO = "RollNuts/veripsa"         # full_name AFTER (the fresh coordinate)
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")


class FakeGitHub:
    """The recording fake from test_lifecycle_e2e (trimmed to what a push needs): serves a repo tarball from the
    sample_app fixture and records posted checks/comments. repo_default_branch_head serves a HEAD sha."""

    def __init__(self):
        self.checks, self.comments = [], []
        self.installations = []
        self.current_repo = OLD_REPO
        self.head_sha = "a" * 40
        self._check_id, self._comment_id = 2000, 1000

    def for_installation(self, installation_id):
        self.installations.append(str(installation_id))
        return self

    def repo_default_branch_head(self, repo):
        return "main", self.head_sha

    def repo_current_identity(self, repo):
        return {"id": REPO_ID, "full_name": self.current_repo, "owner_id": OWNER_ID}

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        check = {"id": self._check_id, "sha": sha, "conclusion": conclusion, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion})
                return c
        return None

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        comment = {"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}}
        self.comments.append(comment)
        return comment

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
        return None

    def upsert_comment(self, repo, number, marker, body):
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        return False

    def list_open_pull_requests(self, repo, limit=None):
        return []

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname=repo.split("/")[-1] + "-" + sha[:7])
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


def push_main(repo, sha, modified, repo_id=REPO_ID):
    """A push to main carrying the STABLE repository.id (the rename-stable signal the fix keys on)."""
    return {"ref": "refs/heads/main", "after": sha,
            "installation": {"id": INSTALL_ID, "account": {"id": OWNER_ID}},
            "repository": {"id": repo_id, "full_name": repo, "default_branch": "main",
                           "owner": {"id": OWNER_ID}},
            "pusher": {"name": "legacy-owner"},
            "head_commit": {"id": sha, "timestamp": "2026-06-20T00:00:00Z",
                            "added": [], "modified": modified, "removed": []},
            "commits": [{"added": [], "modified": modified, "removed": []}]}


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    seed_live_installation(
        DSN_APP,
        f"postgresql://veripsa_migrator@localhost/{DB}",
        OWNER_ID,
        INSTALL_ID,
    )

    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)

    def discard_refresh_surfaces(_gh, _repo, entries, **kwargs):
        pending = sorted(
            (entry for entry in (entries or [])
             if isinstance(entry, dict) and isinstance(entry.get("change"), str)
             and entry["change"] > str(kwargs.get("after_change") or "")),
            key=lambda entry: entry["change"],
        )
        return {"posted": 0, "processed": len(pending),
                "cursor": pending[-1]["change"] if pending else str(kwargs.get("after_change") or ""),
                "has_more": False, "errors": 0}

    def deliver(event_type, payload):
        if event_type == "push":
            repository = payload["repository"]
            gh.current_repo = repository["full_name"]
            gh.head_sha = payload["after"]
        result = proc(event_type, payload, None, gh)
        if event_type == "push":
            drained = PR._drain_policy_refreshes(
                PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
                graph_refresh_strict=S.converge_main_graph_strict,
                post_refreshes=discard_refresh_surfaces)
            assert drained.get("graph_drained", 0) >= 1, f"graph convergence failed: {drained!r}"
        return result

    def admin(sql, args=()):
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()

    def seed(sql, args=()):
        """Run a governed SEED write as the migrator with the tenant pinned (no result expected). Used to plant
        the repo-keyed CONSENT / live-lane / co-change rows under the OLD name so the rename migration can be
        proven to carry them — these tables aren't all populated by the bare push path, so the gate seeds them."""
        conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.current_account', %s, true)", (TENANT,))
                cur.execute(sql, args)
        finally:
            conn.close()

    def coords():
        """the (repo, repo_id, sha) of every graph_version coordinate this tenant holds (the orphan check)."""
        import json as _j
        v = admin("""SELECT COALESCE(jsonb_agg(jsonb_build_object('repo',repo,'repo_id',repo_id,'sha',commit_sha)
                       ORDER BY repo),'[]'::jsonb)::text FROM core.graph_version
                      WHERE account_id=%s AND branch='main'""", (TENANT,))
        return _j.loads(v)

    checks = []

    # ── STEP 1: the repo is live under its OLD name (pre-rename). A push to example-user/veripsa-core-old ingests the
    #    coordinate and STAMPS the stable repository.id onto it.
    deliver("push", push_main(OLD_REPO, "a" * 40, ["backend/api.py"]))
    after_old = coords()
    old_row = next((c for c in after_old if c["repo"] == OLD_REPO), None)
    checks.append((f"STEP 1: the repo is live under the OLD name + the stable repo_id is STAMPED "
                   f"(coords={after_old})",
                   old_row is not None and str(old_row.get("repo_id")) == str(REPO_ID)))

    # ── STEP 1b: SEED the repo-keyed rows the bare push path does NOT plant on its own — under the OLD name — so
    #    STEP 2 can prove the rename migration carries the FULL working set, not just the graph. These are exactly
    #    the tables a rename used to ORPHAN (the blind spot that let the consent-layer miss slip): the CONSENT row
    #    (workspace_member — the moat-critical bilateral link), a live LANE (claim), a co-change PAIR, and the
    #    GitHub store attachment whose `target` is semantically the same mutable repository coordinate.
    WS = "WS-RENAMEORPHAN-1"
    seed("SELECT core.mark_governed_write('workspace');"
         " INSERT INTO core.workspace(workspace_id, created_by_account, state)"
         " VALUES (%s, %s, 'active') ON CONFLICT (workspace_id) DO NOTHING;", (WS, TENANT))
    # an ACCEPTED consent row keyed to the OLD repo coordinate (the bilateral cross-tenant link's near side).
    seed("SELECT core.mark_governed_write('workspace_member');"
         " INSERT INTO core.workspace_member(workspace_id, account_id, repo, branch, consent_state, consented_at)"
         " VALUES (%s, %s, %s, 'main', 'accepted', now())"
         " ON CONFLICT (workspace_id, account_id, repo) DO UPDATE SET consent_state='accepted';",
         (WS, TENANT, OLD_REPO))
    # a live ACTIVE lane (claim) on the OLD coordinate — the renamed repo's in-flight PR is the SAME PR; it must
    # follow the rename, not strand under the dead name. (claim PK = (account_id, repo, claim_id).)
    seed("SELECT core.mark_governed_write('claim');"
         " INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path, claim_state)"
         " VALUES ('PR-1:backend/api.py', %s, 'AG-APP', 'PR-1', %s, 'main', 'backend/api.py', 'active');",
         (TENANT, OLD_REPO))
    # a co-change PAIR keyed to the OLD coordinate (the 2nd derived detector — also keyed by repo).
    seed("SELECT core.mark_governed_write('co_change');"
         " INSERT INTO core.co_change(account_id, repo, path_a, path_b, co, n_a, n_b, strength, lift, n_total)"
         " VALUES (%s, %s, 'backend/api.py', 'backend/billing.py', 3, 4, 5, 0.75, 2.0, 10)"
         " ON CONFLICT (account_id, repo, path_a, path_b) DO NOTHING;", (TENANT, OLD_REPO))
    seed("SELECT core.mark_governed_write('store_connection');"
         " INSERT INTO core.store_connection(connection_id, account_id, provider, target)"
         " VALUES ('CN-RENAME-OLD', %s, 'github', %s);", (TENANT, OLD_REPO))
    seeded_ws = admin("SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s "
                      "AND consent_state='accepted'", (TENANT, OLD_REPO))
    seeded_claim = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s "
                         "AND claim_state='active'", (TENANT, OLD_REPO))
    seeded_cc = admin("SELECT count(*)::int FROM core.co_change WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    seeded_store = admin("SELECT count(*)::int FROM core.store_connection WHERE account_id=%s "
                         "AND provider='github' AND target=%s", (TENANT, OLD_REPO))
    checks.append((f"STEP 1b SEED: consent / lane / co-change / GitHub attachment rows are planted under the OLD "
                   f"name (ws_member={seeded_ws}, claim={seeded_claim}, co_change={seeded_cc}, store={seeded_store})",
                   seeded_ws == 1 and seeded_claim == 1 and seeded_cc == 1 and seeded_store == 1))

    # ── STEP 1c COLLISION SEED — the 实测 PROD follow-up bug this gate now locks. The orphan-heal
    #    example-user/veripsa-core-old → RollNuts/veripsa ERRORED with `duplicate key value violates unique constraint
    #    "claim_pkey"`: the claim PK is (account_id, repo, claim_id) and BOTH coordinates already held a claim with
    #    the SAME claim_id (the renamed repo's in-flight PR carries the SAME 'PR-<n>:<path>' under both names), so
    #    the mover's bare `UPDATE core.claim SET repo=new` PK-collided and the WHOLE migration aborted. The old
    #    active-claim-index dedup did NOT catch it because the colliding NEW-side row was already RELEASED (not
    #    active). Co_change (PK incl. repo) has the same risk. We seed, UNDER THE NEW NAME, a colliding pair that
    #    will exist under BOTH coords at migration time. THE PROD SHAPE (load-bearing): the OLD-name claim is the
    #    LIVE/ACTIVE one (fresher heartbeat) and the NEW-name colliding row is an already-SETTLED 'released' row from
    #    the post-rename push — so the active OLD claim is the per-PK winner and must RE-POINT old→new (and the stale
    #    NEW row is dropped), exactly the verdict the graph_version winner discipline gives. We seed the NEW-name
    #    colliding claim with an explicitly OLDER claimed_at/heartbeat_at so the OLD-name active claim wins by
    #    freshness (and STEP 2b — "the active claim follows the rename" — stays exact: one ACTIVE claim under the new
    #    name). The co_change collision is the mirror case: the NEW-name pair already has HIGHER support (the
    #    post-rename data), so the NEW one wins there (co_change keeps the stronger side). Without the dedup-then-
    #    repoint fix the STEP-2 push that triggers the migration raises `claim_pkey` and EVERY later assertion fails;
    #    with it, the migration succeeds and keeps exactly the right side on each table.
    seed("SELECT core.mark_governed_write('claim');"
         " INSERT INTO core.claim(claim_id, account_id, agent_id, change_id, repo, branch, target_path,"
         "   claim_state, claimed_at, heartbeat_at)"
         " VALUES ('PR-1:backend/api.py', %s, 'AG-APP', 'PR-1', %s, 'main', 'backend/api.py',"
         "   'released', now() - interval '1 hour', now() - interval '1 hour');",   # OLDER → the active OLD claim wins
         (TENANT, NEW_REPO))
    seed("SELECT core.mark_governed_write('co_change');"
         " INSERT INTO core.co_change(account_id, repo, path_a, path_b, co, n_a, n_b, strength, lift, n_total)"
         " VALUES (%s, %s, 'backend/api.py', 'backend/billing.py', 9, 9, 9, 0.9, 3.0, 12)"
         " ON CONFLICT (account_id, repo, path_a, path_b) DO NOTHING;", (TENANT, NEW_REPO))   # higher support → new wins
    collide_claim = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s "
                          "AND claim_id='PR-1:backend/api.py'", (TENANT, NEW_REPO))
    collide_cc = admin("SELECT count(*)::int FROM core.co_change WHERE account_id=%s AND repo=%s "
                       "AND path_a='backend/api.py' AND path_b='backend/billing.py'", (TENANT, NEW_REPO))
    checks.append((f"STEP 1c COLLISION SEED: a same-PK claim ('PR-1:backend/api.py') + co_change pair now also exist "
                   f"under the NEW name — so the rename migration faces a claim_pkey/co_change_pkey collision under "
                   f"BOTH coords (new_claim={collide_claim}, new_cc={collide_cc})",
                   collide_claim == 1 and collide_cc == 1))

    # ── STEP 2: simulate the prod orphan — the OLDER push leaves the old coordinate at an OLD sha, then the OWNER
    #    is renamed (example-user→RollNuts) and a NEW push lands under RollNuts/veripsa. GitHub fires NO repository
    #    rename webhook for an owner-login rename, so the App sees ONLY a push to the new name carrying the SAME
    #    stable repository.id. The fix must DETECT the rename by id and MIGRATE old→new — leaving ONE coordinate.
    deliver("push", push_main(NEW_REPO, "b" * 40, ["backend/api.py", "backend/billing.py"]))
    after_new = coords()
    repos_now = sorted({c["repo"] for c in after_new})
    new_row = next((c for c in after_new if c["repo"] == NEW_REPO), None)
    checks.append((f"STEP 2 ORPHAN HEALED: after the owner-login rename + a push to the NEW name, the tenant holds "
                   f"EXACTLY ONE coordinate — the OLD orphan {OLD_REPO!r} is GONE, only {NEW_REPO!r} remains "
                   f"(coords now={repos_now})",
                   repos_now == [NEW_REPO]))
    checks.append((f"STEP 2: the surviving coordinate is the FRESH one (new sha) and carries the stable repo_id "
                   f"(row={new_row})",
                   new_row is not None and new_row.get("sha") == "b" * 40
                   and str(new_row.get("repo_id")) == str(REPO_ID)))

    # ── STEP 2b CONSENT + LANE + CO-CHANGE FOLLOW THE RENAME (the blind spot this gate now covers): the rename
    #    migration must carry the FULL repo-keyed working set, not just the graph. The consent row (workspace_member
    #    — the moat-critical bilateral link), the live lane (claim), and the co-change pair must ALL move to the new
    #    coordinate and leave NOTHING orphaned under the dead old name. workspace_member orphaning was the exact P0:
    #    the bilateral cross-tenant consent link going dark + a stale 'accepted' row lingering under the dead coord.
    ws_old = admin("SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    ws_new = admin("SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s "
                   "AND consent_state='accepted'", (TENANT, NEW_REPO))
    claim_old = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    claim_new = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s "
                      "AND claim_state='active'", (TENANT, NEW_REPO))
    cc_old = admin("SELECT count(*)::int FROM core.co_change WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    cc_new = admin("SELECT count(*)::int FROM core.co_change WHERE account_id=%s AND repo=%s "
                   "AND path_a='backend/api.py' AND path_b='backend/billing.py'", (TENANT, NEW_REPO))
    store_old = admin("SELECT count(*)::int FROM core.store_connection WHERE account_id=%s "
                      "AND provider='github' AND target=%s", (TENANT, OLD_REPO))
    store_new = admin("SELECT count(*)::int FROM core.store_connection WHERE account_id=%s "
                      "AND provider='github' AND target=%s", (TENANT, NEW_REPO))
    checks.append((f"STEP 2b CONSENT FOLLOWS RENAME (P0): the accepted workspace_member consent row moved old→new "
                   f"and NONE is orphaned under the dead old name (old={ws_old}, new_accepted={ws_new})",
                   ws_old == 0 and ws_new == 1))
    checks.append((f"STEP 2b LANE FOLLOWS RENAME: the active claim moved old→new, nothing stranded under the old "
                   f"name (old={claim_old}, new_active={claim_new})",
                   claim_old == 0 and claim_new == 1))
    checks.append((f"STEP 2b CO-CHANGE FOLLOWS RENAME: the co_change pair moved old→new, nothing stranded under "
                   f"the old name (old={cc_old}, new={cc_new})",
                   cc_old == 0 and cc_new == 1))
    checks.append((f"STEP 2b GITHUB ATTACHMENT FOLLOWS RENAME: store_connection.target moved old→new so later "
                   f"offboarding can find it (old={store_old}, new={store_new})",
                   store_old == 0 and store_new == 1))

    # ── STEP 2c CLAIM-PKEY COLLISION RESOLVED (the 实测 prod follow-up regression lock). Reaching here AT ALL proves
    #    the rename migration did NOT raise `duplicate key value violates unique constraint "claim_pkey"` — under the
    #    old bare `UPDATE core.claim SET repo=new`, the STEP-2 push that triggers the migration would have aborted and
    #    every read-back above would have failed. Now assert the WINNER was resolved correctly: for the colliding
    #    claim_id that existed under BOTH coords, there is EXACTLY ONE row under the new name (no duplicate, no double
    #    row), it is the FRESHER side that survived (the OLD-name ACTIVE claim, re-pointed — the stale 'released'
    #    NEW-side row was dropped), and ZERO rows linger under the dead old name. The co_change collision is the
    #    mirror (its PK also includes repo): exactly one row under the new name carrying the STRONGER support (co=9,
    #    the post-rename winner), none orphaned old. This locks the prod failure as a regression.
    collide_new_total = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s "
                              "AND claim_id='PR-1:backend/api.py'", (TENANT, NEW_REPO))
    collide_new_state = admin("SELECT claim_state FROM core.claim WHERE account_id=%s AND repo=%s "
                              "AND claim_id='PR-1:backend/api.py'", (TENANT, NEW_REPO))
    collide_old_total = admin("SELECT count(*)::int FROM core.claim WHERE account_id=%s AND repo=%s "
                              "AND claim_id='PR-1:backend/api.py'", (TENANT, OLD_REPO))
    cc_new_co = admin("SELECT co FROM core.co_change WHERE account_id=%s AND repo=%s "
                      "AND path_a='backend/api.py' AND path_b='backend/billing.py'", (TENANT, NEW_REPO))
    checks.append((f"STEP 2c CLAIM-PKEY COLLISION RESOLVED (prod regression lock): the same-PK claim existed under "
                   f"BOTH coords, yet the rename did NOT raise claim_pkey — EXACTLY ONE row survives under the new "
                   f"name (total={collide_new_total}, state={collide_new_state!r}, the fresher ACTIVE side re-pointed) "
                   f"and ZERO linger under the dead old name (old_total={collide_old_total})",
                   collide_new_total == 1 and collide_new_state == "active" and collide_old_total == 0))
    checks.append((f"STEP 2c CO-CHANGE-PKEY COLLISION RESOLVED: the same-PK co_change pair existed under both coords; "
                   f"the rename kept exactly one under the new name with the STRONGER support and orphaned none "
                   f"(new_co={cc_new_co})", cc_new_co == 9))

    # ── STEP 3: NO GHOST nodes/edges — the migration kept exactly one side's graph (no double rows under the new
    #    name). The fixture's backend/api.py must appear as a 'file' node EXACTLY ONCE under the new name, and ZERO
    #    nodes/edges remain under the dead old name.
    old_nodes = admin("SELECT count(*)::int FROM core.code_node WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    old_edges = admin("SELECT count(*)::int FROM core.code_edge WHERE account_id=%s AND repo=%s", (TENANT, OLD_REPO))
    api_node_dups = admin("SELECT count(*)::int FROM core.code_node WHERE account_id=%s AND repo=%s "
                          "AND node_kind='file' AND path=%s", (TENANT, NEW_REPO, "backend/api.py"))
    checks.append((f"STEP 3 NO GHOST: zero nodes/edges left under the dead old name "
                   f"(old_nodes={old_nodes}, old_edges={old_edges})",
                   old_nodes == 0 and old_edges == 0))
    checks.append((f"STEP 3 NO DOUBLE ROWS: backend/api.py is a file node EXACTLY ONCE under the new name "
                   f"(count={api_node_dups})", api_node_dups == 1))

    # ── STEP 4: the graph_stale ORPHAN is gone at the SOURCE — graph_freshness_all now sees ONE coordinate for
    #    this repo (the fresh new-name one), so the watchdog has nothing to perpetually page on. Drive the REAL
    #    freshness surface as the App and assert there is no longer a behind-forever orphan for this repo. The
    #    FakeGitHub serves HEAD='f'*40; the live coordinate is at 'b'*40 so it reports behind=True ONCE (a normal,
    #    self-healing "a newer HEAD exists" — NOT the perpetual orphan), but crucially there is no SECOND
    #    (old-name) coordinate that would be behind-FOREVER. The point: ONE coordinate, not two.
    fresh = S.graph_freshness_all(admin_db_for_app(DSN_APP), gh)
    repo_coords_in_fresh = sorted({f.get("repo") for f in fresh if f.get("repo") in (OLD_REPO, NEW_REPO)})
    checks.append((f"STEP 4: the freshness surface sees ONLY the new-name coordinate for this repo — no orphaned "
                   f"old-name coordinate that would fire graph_stale forever (fresh repos={repo_coords_in_fresh})",
                   repo_coords_in_fresh == [NEW_REPO]))

    # ── STEP 5: IDEMPOTENT — a redelivered push to the new name finds nothing to migrate (the old name is gone),
    #    and the coordinate set is unchanged. The reconcile must be a clean no-op the second time.
    deliver("push", push_main(NEW_REPO, "c" * 40, ["backend/api.py"]))
    after_redeliver = sorted({c["repo"] for c in coords()})
    checks.append((f"STEP 5 IDEMPOTENT: a later push to the new name does NOT resurrect the old coordinate "
                   f"(coords={after_redeliver})", after_redeliver == [NEW_REPO]))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("RENAME-COORDINATE-ORPHAN GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def admin_db_for_app(dsn):
    """A connect-per-query db runner pinned to the App identity + tenant (the path graph_freshness_all expects:
    it resolves a per-account client and reads the owner freshness surface as veripsa_app)."""
    def run(sql, args=()):
        conn = psycopg2.connect(dsn)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(OWNER_ID),))
                cur.execute(sql, args)
                row = cur.fetchone()
                return row[0] if row else None
        finally:
            conn.close()
    return run


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
