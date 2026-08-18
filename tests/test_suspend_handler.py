#!/usr/bin/env python3
"""INSTALLATION.SUSPEND HANDLER GATE — a suspended install is handled, not silently noop'd.

THE DEFECT this locks (MED, lifecycle — same class as the round-4 #368 unhandled-lifecycle-event HIGH, lower
blast radius): the GitHub webhook action `installation.suspend` was UNHANDLED. The `installation` handler in
github-app/webhook_handlers.py branched only on created / unsuspend / new_permissions_accepted (onboard) and
deleted (purge); `suspend` matched NO branch → fell through to the generic `noop`. event_processor's
_INSTALL_FANOUT_FIELD had no `suspend` entry either. OBSERVED HARM (reproduced here): after a suspend was
delivered, the account's in-flight lanes stayed HELD (Veripsa never released them — correct behavior depended
ENTIRELY on GitHub's external masking, which does NOT cover at-least-once redelivery or the rolling-deploy
2-instance overlap during the suspend transition), AND a subsequent pull_request event for that account was
STILL fully processed — it posted a check AND acquired a new active claim (count 0→1).

THE FIX (minimal, content-free, advisory):
  * handle_event handles `installation.suspend` EXPLICITLY → returns a non-noop `suspended` verdict and releases
    the account's in-flight lanes ACCOUNT-WIDE (the graph-preserving counterpart to the repo `archived` release;
    NOT the uninstall purge — suspend is reversible, unsuspend re-onboards from the retained structure).
  * a SUSPEND-WINDOW QUIESCE at the top of handle_event: any WORK-bearing event (pull_request / push / …)
    delivered while the install is suspended (GitHub stamps installation.suspended_at on it) is a clean no-op —
    no lane, no check. The lifecycle events (installation / installation_repositories) are exempt (they carry
    their own correct handling — suspend RELEASES, unsuspend RE-ONBOARDS, deleted PURGES).
  * a minimal account-scoped release fn core.release_account_claims_with_authority() (35_lifecycle.sql), App-
    delegation only, identity from the connection role (no tenant arg → no cross-tenant release), graph kept.

WHAT THIS GATE PROVES (driven through the REAL make_db_processor / handle_event over the REAL gate, authed as
the REAL least-privilege role veripsa_app, against a scratch Postgres — the exact prod transaction model):

  SUSPEND-1  NON-NOOP VERDICT: installation.suspend returns a clear `suspended` verdict (action=='suspended',
             noop is NOT set). On origin/main this is {"event":"installation","noop":true} → FAILS.
  SUSPEND-2  LANES QUIESCED: an account that held an in-flight lane has it RELEASED by the suspend (active/waiting
             claim count goes to 0 — the board goes quiet by Veripsa's OWN logic). On origin/main the lane stays
             held (the noop releases nothing) → FAILS.
  SUSPEND-3  PR-DURING-SUSPEND IS A NO-OP: a pull_request delivered while the install is suspended (carrying
             installation.suspended_at, as GitHub stamps it) acquires NO new active lane and posts NO check. On
             origin/main it is fully processed (claim 0→1, check posted) → FAILS.
  SUSPEND-4  UNSUSPEND RESTORES OPEN WORK: an All-repositories resume preserves the retained graph and replays
             open PRs using the stable repository id, so released claims do not remain blind after resume.

Run:  python3 tests/test_suspend_handler.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402
import server as S  # noqa: E402  (re-exports make_db_processor / handle_event / _scoped_db)
import ingest  # noqa: E402  (we no-op the co-change clone; its own gate covers that path)
import policy_refresh_queue as PR  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): per-PID, exactly like the other lifecycle gates — a fixed name would let two
# concurrent runs drop each other's DB mid-run.
DB = "veripsa_suspend_handler_" + str(os.getpid())
DSN_APP = f"postgresql://veripsa_app@localhost/{DB}"          # the REAL prod least-privilege role

INSTALL_ID = 5757
ACCOUNT_ID = 717171                  # the stable owning-account id (the tenant key)
TENANT = f"ACCT-GH-{ACCOUNT_ID}"     # what enter_installation_with_authority provisions for ACCOUNT_ID
REPO = "acme/shop"
REPO_ID = 880088
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "sample_app")


class FakeGitHub:
    """Records what the App WOULD post + serves PR files + a repo tarball from the sample_app fixture. The same
    recording-fake shape the other lifecycle gates use — only the GitHub I/O is faked; the brain + gate are real."""

    def __init__(self):
        self.checks, self.comments = [], []
        self.files_by_pr = {}
        self.open_prs = []
        self.current_prs = {}
        self._comment_id = 1000
        self._check_id = 2000

    def for_installation(self, installation_id):
        return self

    def app_installation_identity(self, installation_id):
        return {"installation_id": str(installation_id), "account_id": str(ACCOUNT_ID),
                "created_at": "2026-01-01T00:00:00Z", "suspended": False}

    def app_account_installation_identity(self, account_id):
        return {"installation_id": str(INSTALL_ID), "account_id": str(ACCOUNT_ID),
                "created_at": "2026-01-01T00:00:00Z", "suspended": True}

    def installation_account_id(self):
        return str(ACCOUNT_ID)

    def installation_repos(self, cap=200):
        return [REPO]

    def installation_repo_entries(self, cap=200):
        return [{"full_name": REPO, "id": REPO_ID}]

    def list_pr_files(self, repo, number, pr_changed_files=0):
        return self.files_by_pr.get(number, [])

    def list_pr_file_metadata(self, repo, number, pr_changed_files=0, max_pages=None):
        changed = list(self.files_by_pr.get(number, []))
        return {"changed": changed, "changed_ranges": {}, "added_paths": [],
                "conflict_markers": [], "raw_entry_count": len(changed)}

    def post_check(self, repo, sha, conclusion, title, summary):
        self._check_id += 1
        check = {"id": self._check_id, "sha": sha, "conclusion": conclusion,
                 "title": title, "summary": summary, "name": "Veripsa"}
        self.checks.append(check)
        return check

    def list_check_runs(self, repo, sha):
        return [c for c in self.checks if c["sha"] == sha and c.get("name") == "Veripsa"]

    def patch_check(self, repo, check_run_id, conclusion, title, summary):
        for c in self.checks:
            if c["id"] == check_run_id:
                c.update({"conclusion": conclusion, "title": title, "summary": summary})
                return c
        raise AssertionError(f"check not found: {check_run_id}")

    def upsert_check(self, repo, sha, conclusion, title, summary):
        existing = self.list_check_runs(repo, sha)
        if existing:
            return self.patch_check(repo, existing[0]["id"], conclusion, title, summary)
        return self.post_check(repo, sha, conclusion, title, summary)

    def post_comment(self, repo, number, body):
        self._comment_id += 1
        self.comments.append({"id": self._comment_id, "number": number, "body": body, "user": {"type": "Bot"}})

    def list_issue_comments(self, repo, number):
        return [c for c in self.comments if c["number"] == number]

    def patch_comment(self, repo, comment_id, body):
        for c in self.comments:
            if c["id"] == comment_id:
                c["body"] = body
                return c
        raise AssertionError(f"comment not found: {comment_id}")

    def upsert_comment(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"] or (c["body"].startswith("### Veripsa") and c.get("user", {}).get("type") == "Bot"):
                return self.patch_comment(repo, c["id"], body)
        return self.post_comment(repo, number, body)

    def patch_comment_if_exists(self, repo, number, marker, body):
        for c in self.list_issue_comments(repo, number):
            if marker in c["body"]:
                self.patch_comment(repo, c["id"], body() if callable(body) else body)
                return True
        return False

    def list_open_pull_requests(self, repo, limit=None):
        return self.open_prs[:limit] if limit is not None else list(self.open_prs)

    def get_pull_request(self, repo, number):
        return self.current_prs[number]

    def pull_request_head(self, repo, number):
        return self.current_prs[number]["head"]["sha"]

    def compare_changed_paths(self, repo, base_sha, head_ref):
        return []

    compare_changed_paths_strict = compare_changed_paths

    def repo_default_branch_head(self, repo):
        return "main", "c" * 40

    def repo_branch_head(self, repo, branch):
        assert branch == "main"
        return "c" * 40

    def repo_onboarding_head_info(self, repo):
        return {
            "repository_id": REPO_ID,
            "owner_id": ACCOUNT_ID,
            "full_name": repo,
            "default_branch": "main",
            "head_sha": "c" * 40,
            "empty": False,
        }

    def repo_current_identity(self, repo):
        return {"id": REPO_ID, "full_name": repo, "owner_id": ACCOUNT_ID}

    def download_tarball(self, repo, sha):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            tf.add(FIXTURE, arcname="acme-shop-" + sha[:7])   # GitHub nests under one top dir
        return buf.getvalue()

    def get_file_at(self, repo, path, ref):
        full = os.path.join(FIXTURE, path)
        if not os.path.isfile(full):
            return None
        with open(full, "rb") as fh:
            return fh.read()


# ── payload builders (the real webhook SHAPES, carrying the installation + the stable owner id) ─────────────
def install_created(repos):
    return {"action": "created",
            "installation": {"id": INSTALL_ID, "account": {
                "id": ACCOUNT_ID, "login": "acme", "type": "Organization"}},
            "repositories": [{"id": REPO_ID, "full_name": r} for r in repos]}


def install_suspend():
    # GitHub stamps installation.suspended_at on the suspend event (the install just became suspended).
    return {"action": "suspend",
            "installation": {"id": INSTALL_ID, "account": {
                                 "id": ACCOUNT_ID, "login": "acme", "type": "Organization"},
                             "suspended_at": "2026-06-21T00:00:00Z"}}


def install_unsuspend(repos):
    # On resume GitHub CLEARS suspended_at (null) — the install is live again.
    return {"action": "unsuspend",
            "installation": {"id": INSTALL_ID, "account": {
                                 "id": ACCOUNT_ID, "login": "acme", "type": "Organization"},
                             "suspended_at": None},
            "repositories": [{"id": REPO_ID, "full_name": r} for r in repos]}


def pr_event(action, number, author, files, head_sha=None, suspended_at=None):
    head_sha = head_sha or f"{number:040x}"
    installation = {"id": INSTALL_ID, "account": {"id": ACCOUNT_ID}}
    if suspended_at is not None:
        installation["suspended_at"] = suspended_at      # GitHub stamps this on events delivered while suspended
    return {"action": action, "number": number,
            "installation": installation,
            "repository": {"id": REPO_ID, "full_name": REPO,
                           "default_branch": "main", "owner": {"id": ACCOUNT_ID}},
            "pull_request": {"base": {"ref": "main", "sha": "c" * 40,
                                       "repo": {"id": REPO_ID}},
                             "head": {"sha": head_sha, "repo": {"id": REPO_ID},
                                      "ref": f"feature/{number}"},
                             "user": {"login": author}, "merged": False}}, files


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:])
        return 1

    # the co-change clone has its own dedicated gate — make STEP 1b a content-free no-op in this unit.
    ingest.populate_cochange_async = lambda gh, repo, branch, window=800, repository_id=None: True

    gh = FakeGitHub()
    proc = S.make_db_processor(DSN_APP)
    delivery_seq = 0

    def deliver(event_type, payload):
        """Drive ONE webhook through the exact live per-event path (connection + lock + tenant pin + handle_event)."""
        nonlocal delivery_seq
        if event_type == "installation" and payload.get("action") in {
                "created", "unsuspend", "new_permissions_accepted"}:
            delivery_seq += 1
            key = f"suspend-handler-{payload['action']}-{delivery_seq}"
            stored = dict(payload)
            stored.pop("_veripsa_delivery_key", None)
            conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    cur.execute(
                        "INSERT INTO core.webhook_delivery("
                        "delivery_key,event_type,account_key,payload,status,received_at) "
                        "VALUES (%s,%s,%s,%s::jsonb,'processing',clock_timestamp())",
                        (key, event_type, str(ACCOUNT_ID), json.dumps(stored)),
                    )
            finally:
                conn.close()
            payload["_veripsa_delivery_key"] = key
        proc(event_type, payload, None, gh)

    def admin(sql, args=()):
        """Readback as the migrator with the tenant pinned (App writes via gates; raw SELECT past RLS here)."""
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

    def active_waiting():
        return admin("SELECT count(*)::int FROM core.claim WHERE repo=%s AND claim_state IN ('active','waiting')", (REPO,))

    def app_call(sql, args=()):
        """Call a function AS THE APP (veripsa_app — the real provisioned, App-delegation role), tenant pinned.
        Used to forget the graph via the same account-wide purge the uninstall handler uses (the migrator is not a
        provisioned agent, so it cannot call the *_with_authority delegation fns)."""
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
                cur.execute(sql, args)
                row = cur.fetchone()
                v = row[0] if row else None
                return json.loads(v) if isinstance(v, str) else v
        finally:
            conn.close()

    def graph_committed():
        """COMMITTED-VIEW readback AS THE APP (a fresh connection sees only what events actually committed):
        does the repo's main graph exist? coordinate_graph_sha returns a non-null commit_sha once ingested."""
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
                cur.execute("SELECT (core.coordinate_graph_sha(%s,%s))->>'commit_sha'", (REPO, "main"))
                row = cur.fetchone()
            return bool(row and row[0])
        finally:
            conn.close()

    def suspend_verdict():
        """Drive installation.suspend through handle_event DIRECTLY to capture its verdict dict (the live proc is
        fire-and-forget). Tenant pinned on the connection first, exactly as make_db_processor pins it for an
        account-pinned installation event."""
        payload = install_suspend()
        key = "suspend-handler-current"
        admin_conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
        try:
            with admin_conn, admin_conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "INSERT INTO core.webhook_delivery("
                    "delivery_key,event_type,account_key,payload,status,received_at) "
                    "VALUES (%s,'installation',%s,%s::jsonb,'processing',clock_timestamp())",
                    (key, str(ACCOUNT_ID), json.dumps(payload)),
                )
        finally:
            admin_conn.close()
        payload["_veripsa_delivery_key"] = key
        payload[S._SUSPEND_PROOF_MARKER] = {
            "state": "current",
            "suspended_installation_id": str(INSTALL_ID),
            "account_id": str(ACCOUNT_ID),
            "current": gh.app_account_installation_identity(str(ACCOUNT_ID)),
        }
        conn = psycopg2.connect(DSN_APP)
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT core.enter_installation_with_authority(%s)", (str(ACCOUNT_ID),))
            v = S.handle_event("installation", payload, S._scoped_db(conn), gh)
            return json.loads(v) if isinstance(v, str) else v
        finally:
            conn.close()

    checks = []

    # ── SETUP: install the App, then open a PR that CLAIMS a lane (so there is real in-flight state to quiesce).
    deliver("installation", install_created([REPO]))
    drained = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
        graph_refresh_strict=S.converge_main_graph_strict)
    assert drained.get("graph_drained", 0) >= 1, \
        f"setup: installation graph convergence failed: {drained!r}"
    pa, files = pr_event("opened", 101, "alice", ["backend/auth.py"])
    gh.files_by_pr[101] = files
    repository_metadata = {
        "id": REPO_ID,
        "full_name": REPO,
        "default_branch": "main",
        "owner": {"id": ACCOUNT_ID},
    }
    open_101 = {"number": 101, "state": "open",
                "base": {"ref": "main", "sha": "c" * 40,
                         "repo": dict(repository_metadata)},
                "head": {"sha": pa["pull_request"]["head"]["sha"],
                         "repo": dict(repository_metadata)},
                "user": {"login": "alice"}, "merged": False, "changed_files": len(files)}
    gh.current_prs[101] = open_101
    deliver("pull_request", pa)
    pc, files3 = pr_event("opened", 303, "carol", ["backend/worker.py"])
    gh.files_by_pr[303] = files3
    open_303 = {"number": 303, "state": "open",
                "base": {"ref": "main", "sha": "c" * 40,
                         "repo": dict(repository_metadata)},
                "head": {"sha": pc["pull_request"]["head"]["sha"],
                         "repo": dict(repository_metadata)},
                "user": {"login": "carol"}, "merged": False, "changed_files": len(files3)}
    gh.current_prs[303] = open_303
    deliver("pull_request", pc)
    gh.open_prs = [open_101, open_303]
    gh.current_prs = {101: open_101, 303: open_303}
    claims_before_suspend = active_waiting()
    mismatched_stable_id_allowed = app_call(
        "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
        (REPO, str(REPO_ID + 1)),
    )

    # Simulate a graph written before stable repository IDs were recorded. A reversible suspension must not make
    # this legacy installation permanently blind: the current All-repositories inventory supplies the stable id,
    # onboarding stamps it, and the authoritative open-PR replay restores the released lane.
    admin(
        "SELECT core.mark_governed_write('graph_version'); "
        "UPDATE core.graph_version SET repo_id=NULL WHERE account_id=%s AND repo=%s AND branch='main' RETURNING 1",
        (TENANT, REPO),
    )
    admin(
        "DELETE FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repo=%s RETURNING 1",
        (TENANT, REPO),
    )
    legacy_repo_id_before_suspend = admin(
        "SELECT repo_id FROM core.graph_version WHERE repo=%s AND branch='main'",
        (REPO,),
    )

    # Simulate a rolling migration row whose generation columns have not yet been seeded.  A delayed suspend from
    # old generation A sees current B through App-JWT.  SQL must atomically seed B and return stale WITHOUT
    # quiescing its claims or revoking its live route; DB NULL is never accepted as a green lifecycle no-op.
    admin(
        "UPDATE core.installation_account SET github_installation_id=NULL, "
        "github_installation_created_at=NULL WHERE account_id=%s RETURNING 1",
        (TENANT,),
    )
    stale_suspend_payload = install_suspend()
    stale_suspend_payload["installation"]["id"] = INSTALL_ID - 1
    admin(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,payload,status,received_at) "
        "VALUES ('suspend-handler-stale','installation',%s,%s::jsonb,'processing',clock_timestamp()) "
        "RETURNING 1",
        (str(ACCOUNT_ID), json.dumps(stale_suspend_payload)),
    )
    stale_suspend_result = app_call(
        "SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
        ("suspend-handler-stale", json.dumps({
            "state": "current",
            "suspended_installation_id": str(INSTALL_ID - 1),
            "account_id": str(ACCOUNT_ID),
            "current": {
                "installation_id": str(INSTALL_ID),
                "account_id": str(ACCOUNT_ID),
                "created_at": "2026-01-01T00:00:00Z",
                "suspended": False,
            },
        })),
    )
    claims_after_stale_suspend = active_waiting()
    generation_after_stale_suspend = admin(
        "SELECT github_installation_id FROM core.installation_account WHERE account_id=%s LIMIT 1",
        (TENANT,),
    )
    revoked_after_stale_suspend = admin(
        "SELECT bool_or(revoked_at IS NOT NULL) FROM core.installation_account WHERE account_id=%s",
        (TENANT,),
    )
    checks.append((f"SUSPEND-0 stale generation A is a no-op against current B "
                   f"(result={stale_suspend_result}, claims={claims_before_suspend}->{claims_after_stale_suspend}, "
                   f"seeded={generation_after_stale_suspend}, revoked={revoked_after_stale_suspend})",
                   stale_suspend_result.get("stale_ignored") is True
                   and stale_suspend_result.get("released") == 0
                   and claims_after_stale_suspend == claims_before_suspend
                   and str(generation_after_stale_suspend) == str(INSTALL_ID)
                   and revoked_after_stale_suspend is False))

    # Reset the rollout cache to NULL once more for the real/current target.  This is the dangerous historical
    # shape: it must atomically seed the target and perform the suspend, never green-complete as a stale no-op.
    admin(
        "UPDATE core.installation_account SET github_installation_id=NULL, "
        "github_installation_created_at=NULL WHERE account_id=%s RETURNING 1",
        (TENANT,),
    )

    # ── SUSPEND-1: the verdict is a clear `suspended`, NOT a noop. (handle_event direct → captures the dict; this
    #    ALSO performs the account-wide release, which SUSPEND-2 reads back.)
    verdict = suspend_verdict()
    current_generation_after_suspend = admin(
        "SELECT github_installation_id FROM core.installation_account WHERE account_id=%s LIMIT 1",
        (TENANT,),
    )
    is_non_noop_suspended = (isinstance(verdict, dict)
                             and verdict.get("action") == "suspended"
                             and verdict.get("released", {}).get("suspended") is True
                             and str(current_generation_after_suspend) == str(INSTALL_ID)
                             and not verdict.get("noop"))
    checks.append((f"SUSPEND-1 installation.suspend returns a clear `suspended` verdict, NOT a silent noop "
                   f"(verdict={json.dumps(verdict)})", is_non_noop_suspended))

    # ── SUSPEND-2: the account's in-flight lanes are RELEASED by the suspend — the board goes quiet by Veripsa's
    #    own logic. On origin/main the noop releases nothing → the lane stays held.
    claims_after_suspend = active_waiting()
    checks.append((f"SUSPEND-2 suspend QUIESCES the account's in-flight lanes (active/waiting claims "
                   f"{claims_before_suspend}→{claims_after_suspend}; precondition held>0, after==0)",
                   claims_before_suspend > 0 and claims_after_suspend == 0))

    # ── SUSPEND-3: a pull_request delivered DURING the suspended window (carrying installation.suspended_at, as
    #    GitHub stamps it) acquires NO new active lane and posts NO check — Veripsa records nothing for a suspended
    #    install. On origin/main this PR is fully processed (claim 0→1, check posted).
    pb, files2 = pr_event("opened", 202, "bob", ["backend/api.py"], suspended_at="2026-06-21T00:00:00Z")
    gh.files_by_pr[202] = files2
    deliver("pull_request", pb)
    claims_during_suspend = active_waiting()
    check_for_202 = [c for c in gh.checks if c["sha"] == f"{202:040x}"]
    checks.append((f"SUSPEND-3 a pull_request DURING the suspend window does NOT mutate as if live — no new lane "
                   f"(active/waiting claims stay {claims_during_suspend}; must be 0) and NO check posted for it "
                   f"(checks_for_PR-202={len(check_for_202)}; must be 0)",
                   claims_during_suspend == 0 and len(check_for_202) == 0))

    # ── SUSPEND-4: a later All-repositories unsuspend carries no repo list, enumerates the current stable id, and
    #    replays open PRs against the RETAINED graph. This is the production resume path: suspension keeps graph
    #    state but released the claims above, so a name-only strict gate would skip the repo and leave it blind.
    graph_during_suspend = graph_committed()
    # The list endpoint raced a close: it still contains PR-303, while a point read proves the PR is now closed.
    # Authoritative replay must restore PR-101 but never resurrect PR-303's released lane.
    gh.current_prs[303] = {**open_303, "state": "closed"}
    deliver("installation", install_unsuspend([]))
    resumed = PR._drain_policy_refreshes(
        PR.PolicyRefreshStore(DSN_APP), gh, DSN_APP, limit=20,
        graph_refresh_strict=S.converge_main_graph_strict)
    assert resumed.get("graph_drained", 0) >= 1, \
        f"setup: unsuspend graph convergence failed: {resumed!r}"
    graph_after_unsuspend = graph_committed()
    claims_after_unsuspend = active_waiting()
    claim_states_after_unsuspend = admin(
        "SELECT string_agg(claim_state || ':' || change_id || ':' || target_path, ', ' ORDER BY claim_id) "
        "FROM core.claim WHERE repo=%s",
        (REPO,),
    )
    stored_repo_id = admin("SELECT repo_id FROM core.graph_version WHERE repo=%s AND branch='main'", (REPO,))
    closed_race_claims = admin(
        "SELECT count(*)::int FROM core.claim WHERE repo=%s AND change_id='PR-303' "
        "AND claim_state IN ('active','waiting')",
        (REPO,),
    )
    checks.append((f"SUSPEND-4 All-repositories unsuspend replays open PRs on the retained graph using the stable "
                   f"repository id (graph {graph_during_suspend}->{graph_after_unsuspend}, claims "
                   f"0->{claims_after_unsuspend}, states={claim_states_after_unsuspend}, repo_id={stored_repo_id})",
                   mismatched_stable_id_allowed is False
                   and legacy_repo_id_before_suspend is None
                   and graph_during_suspend is True and graph_after_unsuspend is True
                   and claims_after_unsuspend == 1 and closed_race_claims == 0
                   and str(stored_repo_id) == str(REPO_ID)))

    print("\n── INSTALLATION.SUSPEND HANDLER ─────────────────────────────")
    ok = True
    for label, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {label}")
        ok = ok and bool(passed)
    print("\nSUSPEND HANDLER GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    sys.exit(rc)
