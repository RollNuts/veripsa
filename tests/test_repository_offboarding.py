#!/usr/bin/env python3
"""Repository offboarding residue gate.

Proves the repository-delete/removal lifecycle against a real Postgres instance:
  * stable repository ids find a renamed coordinate;
  * a stale delete cannot purge a same-name replacement with a different id;
  * add/remove lifecycle events use authenticated durable receive order in both directions;
  * stale adds skip backfill and stale removes discard only pre-reactivation authority;
  * graphless renamed coordinates resolve through stable lifecycle activation;
  * legacy id-less deletes use that order, while a real current delete still purges;
  * graph, claims, co-change, live consent/authority, and attributable settled inbox rows are purged;
  * the in-flight deletion row survives until its owner finalizes it, then loses the repo name;
  * a minimal tombstone blocks stale work and repo-level platform reads;
  * an explicit re-add clears the tombstone;
  * account-level events cannot clear or cold-start behind repo tombstones, and the unordered App overload is denied;
  * sanitization keeps canonical repository ids, rejects malformed identity, and DB failures retry durably.

No source or diff body is seeded or inspected. All fixtures are bounded metadata.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

import psycopg2

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import delivery_queue  # noqa: E402
import cochange  # noqa: E402
import event_processor  # noqa: E402
import ingest  # noqa: E402
import server_dbops  # noqa: E402
import webhook_handlers  # noqa: E402

DB = "veripsa_repooffboard_" + str(os.getpid())
ACCOUNT = "ACCT-DEMO"
INSTALLATION = "inst-repo-offboard"
OLD_REPO = "acme/old-name"
LIVE_REPO = "acme/current-name"
KEEP_REPO = "acme/keep"
REUSED_REPO = "acme/reused"
PENDING_REPLACEMENT_REPO = "acme/pending-replacement"
LEGACY_REPLACEMENT_REPO = "acme/legacy-replacement"
EARLY_WORK_REPLACEMENT_REPO = "acme/early-work-replacement"
GRAPH_CONFLICT_REPO = "acme/graph-conflict"
NAME_ONLY_LIVE_REPO = "acme/name-only-live"
ORDERED_REPO = "acme/ordered-lifecycle"
REVERSE_ORDERED_REPO = "acme/reverse-ordered-lifecycle"
GRAPHLESS_RENAMED_OLD_REPO = "acme/graphless-old-name"
GRAPHLESS_RENAMED_REPO = "acme/graphless-current-name"
QUEUED_REPLACEMENT_REPO = "acme/queued-replacement"
STRICT_RENAMED_OLD_REPO = "acme/strict-old-name"
STRICT_RENAMED_REPO = "acme/strict-current-name"
ID_DOWNGRADE_REPO = "acme/id-downgrade"
LEGACY_WORKER_REPO = "acme/legacy-worker-delete"
NO_AUTHORITY_REPO = "acme/no-delete-authority"
AMBIGUOUS_SHIM_REPO = "acme/ambiguous-legacy-delete"
FANOUT_DELETE_REPO_A = "acme/fanout-delete-a"
FANOUT_DELETE_REPO_B = "acme/fanout-delete-b"
LEGACY_IDLESS_REPLACEMENT_REPO = "acme/legacy-idless-replacement"
FRESH_IDLESS_REPO = "acme/fresh-idless-removal"
LEGACY_IDLESS_GONE_REPO = "acme/legacy-idless-gone"
LEGACY_IDLESS_OBSERVED_GONE_REPO = "acme/legacy-idless-observed-gone"
REPO_ID = "4004"
REUSED_ID = "5005"
PENDING_OLD_ID = "7007"
PENDING_NEW_ID = "8008"
LEGACY_NEW_ID = "9009"
EARLY_OLD_ID = "10010"
EARLY_NEW_ID = "11011"
GRAPH_CONFLICT_OLD_ID = "11111"
GRAPH_CONFLICT_NEW_ID = "11112"
NAME_ONLY_LIVE_ID = "12012"
ORDERED_ID = "13013"
ORDERED_OLD_ID = "13012"
REVERSE_ORDERED_ID = "14014"
GRAPHLESS_RENAMED_ID = "15015"
QUEUED_OLD_ID = "16016"
QUEUED_NEW_ID = "17017"
STRICT_RENAMED_ID = "18018"
ID_DOWNGRADE_OLD_ID = "21001"
ID_DOWNGRADE_NEW_ID = "22002"
LEGACY_WORKER_ID = "23003"
AMBIGUOUS_SHIM_ID_A = "24004"
AMBIGUOUS_SHIM_ID_B = "25005"
FANOUT_DELETE_ID_A = "26006"
FANOUT_DELETE_ID_B = "27007"
LEGACY_IDLESS_REPLACEMENT_ID = "28008"
FRESH_IDLESS_ID = "29009"
LEGACY_IDLESS_GONE_ID = "30010"
LEGACY_IDLESS_OBSERVED_GONE_ID = "31011"


def conn_for(role: str):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def one(role: str, sql: str, args=()):
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def app(sql: str, args=()):
    return one("veripsa_app", sql, args)


def admin(sql: str, args=()):
    return one("veripsa_migrator", sql, args)


def steward(sql: str, args=()):
    return one("veripsa_demo_steward", sql, args)


def account_one(sql: str, args=()):
    """Read FORCE-RLS tenant tables as their owner with the tenant pinned in the same transaction."""
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (ACCOUNT,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def scoped_one(role: str, account: str, sql: str, args=()):
    """Run one focused cross-account assertion with the RLS tenant pinned in the same transaction."""
    conn = conn_for(role)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if role == "veripsa_app":
                cur.execute("SELECT set_config('core.installation_account',%s,true)", (account,))
            else:
                cur.execute("SELECT set_config('core.current_account',%s,true)", (account,))
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def as_json(value):
    return value if isinstance(value, (dict, list)) else json.loads(value)


def count(table: str, repo: str) -> int:
    return int(account_one(
        f"SELECT count(*)::int FROM core.{table} WHERE account_id=%s AND repo=%s",
        (ACCOUNT, repo),
    ))


def seed() -> None:
    sql = """
    SET search_path=core;
    INSERT INTO core.installation_account(installation_id,account_id)
      VALUES (%(installation)s,%(account)s)
      ON CONFLICT (installation_id) DO UPDATE SET account_id=EXCLUDED.account_id,revoked_at=NULL;
    SELECT set_config('core.current_account',%(account)s,true);

    SELECT core.mark_governed_write('graph_version');
    INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at)
      VALUES (%(account)s,%(live)s,'main',repeat('a',40),1,1,%(repo_id)s,now()),
             (%(account)s,%(keep)s,'main',repeat('b',40),1,0,'4999',now()),
             (%(account)s,%(reused)s,'main',repeat('c',40),1,0,%(reused_id)s,now()-interval '1 hour'),
             (%(account)s,%(legacy_replacement)s,'main',repeat('e',40),1,0,NULL,now()-interval '2 hours'),
             (%(account)s,%(name_only_live)s,'main',repeat('f',40),1,0,%(name_only_live_id)s,now()-interval '1 hour'),
             (%(account)s,%(ordered)s,'main',repeat('7',40),1,0,%(ordered_id)s,now()-interval '4 hours'),
             (%(account)s,%(reverse_ordered)s,'main',repeat('8',40),1,0,%(reverse_ordered_id)s,now()-interval '4 hours'),
             (%(account)s,%(queued_replacement)s,'main',repeat('9',40),1,0,%(queued_old_id)s,now()-interval '3 hours');
    SELECT core.mark_governed_write('code_node');
    INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) VALUES
      (%(account)s,%(live)s,'main','LIVE-N','file','src/live.py'),
      (%(account)s,%(keep)s,'main','KEEP-N','file','src/keep.py'),
      (%(account)s,%(reused)s,'main','REUSED-N','file','src/reused.py'),
      (%(account)s,%(legacy_replacement)s,'main','LEGACY-N','file','src/legacy.py'),
      (%(account)s,%(name_only_live)s,'main','NAME-ONLY-N','file','src/current.py'),
      (%(account)s,%(ordered)s,'main','ORDERED-N','file','src/ordered.py'),
      (%(account)s,%(reverse_ordered)s,'main','REVERSE-ORDERED-N','file','src/reverse.py'),
      (%(account)s,%(queued_replacement)s,'main','QUEUED-OLD-N','file','src/queued-old.py');
    SELECT core.mark_governed_write('code_edge');
    INSERT INTO core.code_edge(account_id,repo,branch,src,dst,edge_kind)
      VALUES (%(account)s,%(live)s,'main','LIVE-N','LIVE-N','contains');
    SELECT core.mark_governed_write('claim');
    INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state)
      VALUES ('PR-1:src/live.py',%(account)s,'AG-APP','PR-1',%(live)s,'main','src/live.py','active'),
             ('PR-9:src/legacy.py',%(account)s,'AG-APP','PR-9',%(legacy_replacement)s,'main','src/legacy.py','active'),
             ('PR-15:src/graphless.py',%(account)s,'AG-APP','PR-15',%(graphless_renamed)s,'main',
              'src/graphless.py','active');
    SELECT core.mark_governed_write('co_change');
    INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total)
      VALUES (%(account)s,%(live)s,'src/a.py','src/b.py',5,5,6,0.8,2.1,12),
             (%(account)s,%(reused)s,'src/old-pair.py','src/old.py',7,9,8,0.8,3.0,50),
             (%(account)s,%(legacy_replacement)s,'src/legacy.py','src/pair.py',5,5,6,0.8,2.1,12);
    SELECT core.mark_governed_write('co_change_seen_commit');
    INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha)
      VALUES (%(account)s,%(live)s,repeat('d',40)),
             (%(account)s,%(reused)s,repeat('e',40)),
             (%(account)s,%(legacy_replacement)s,repeat('f',40));

    SELECT core.mark_governed_write('workspace');
    INSERT INTO core.workspace(workspace_id,created_by_account,state) VALUES
      ('WS-REPO-OFFBOARD',%(account)s,'active'),
      ('WS-REUSED-OLD',%(account)s,'active'),
      ('WS-REUSED-NEW',%(account)s,'active'),
      ('WS-GRAPHLESS-RENAMED',%(account)s,'active');
    SELECT core.mark_governed_write('workspace_member');
    INSERT INTO core.workspace_member(
      workspace_id,account_id,repo,branch,consent_state,consented_at,joined_at) VALUES
      ('WS-REPO-OFFBOARD',%(account)s,%(live)s,'main','accepted',now(),now()),
      ('WS-REUSED-OLD',%(account)s,%(reused)s,'main','accepted',now()-interval '2 hours',now()-interval '2 hours'),
      ('WS-REUSED-NEW',%(account)s,%(reused)s,'main','accepted',now()-interval '30 minutes',now()-interval '30 minutes'),
      ('WS-GRAPHLESS-RENAMED',%(account)s,%(graphless_renamed)s,'main','accepted',now(),now());
    SELECT core.mark_governed_write('grant');
    INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,repo,scope,granted_at) VALUES
      ('GR-REPO-OFFBOARD',%(account)s,'AG-APP',%(live)s,ARRAY['read'],now()),
      ('GR-REUSED-OLD',%(account)s,'AG-APP',%(reused)s,ARRAY['read'],now()-interval '2 hours'),
      ('GR-REUSED-NEW',%(account)s,'AG-APP',%(reused)s,ARRAY['read'],now()-interval '30 minutes'),
      ('GR-GRAPHLESS-RENAMED',%(account)s,'AG-APP',%(graphless_renamed)s,ARRAY['read'],now());
    SELECT core.mark_governed_write('store_connection');
    INSERT INTO core.store_connection(connection_id,account_id,provider,target,connected_at) VALUES
      ('CN-REPO-OFFBOARD',%(account)s,'github',%(live)s,now()),
      ('CN-REUSED-OLD',%(account)s,'github',%(reused)s,now()-interval '2 hours'),
      ('CN-REUSED-NEW',%(account)s,'github',%(reused)s,now()-interval '2 hours'),
      ('CN-GRAPHLESS-RENAMED',%(account)s,'github',%(graphless_renamed)s,now());

    INSERT INTO core.repository_lifecycle_activation(account_id,repository_id,repo,activated_at)
      VALUES (%(account)s,%(graphless_renamed_id)s,%(graphless_renamed)s,now()-interval '1 hour'),
             (%(account)s,%(strict_renamed_id)s,%(strict_renamed)s,now()-interval '1 hour');

    SELECT core.mark_governed_write('event');
    INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail,occurred_at) VALUES
      ('EV-REPO-OFFBOARD',%(account)s,'warn_issued','AG-APP',%(live)s,'main','src/live.py','PR-1',now()),
      ('EV-STUCK-REPO-OFFBOARD',%(account)s,'pr_failing','AG-APP',%(live)s,'main','PR-404','ci_failed',now()),
      ('EV-REPO-KEEP',%(account)s,'warn_issued','AG-APP',%(keep)s,'main','src/keep.py','PR-2',now()),
      ('EV-REUSED-OLD',%(account)s,'warn_issued','AG-APP',%(reused)s,'main','src/old.py','PR-OLD',now()-interval '2 hours'),
      ('EV-PENDING-OLD',%(account)s,'warn_issued','AG-APP',%(pending)s,'main','src/old.py','PR-OLD',now()-interval '2 hours'),
      ('EV-LEGACY-OLD',%(account)s,'warn_issued','AG-APP',%(legacy_replacement)s,'main','src/legacy.py','PR-OLD',now()-interval '2 hours');

    INSERT INTO core.webhook_delivery(
      delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) VALUES
      ('D-REPO-DONE','pull_request',%(account)s,%(live)s,'{}'::jsonb,'done',1,now(),NULL),
      ('D-REPO-PROCESSING','repository',%(account)s,%(live)s,
       jsonb_build_object('action','deleted','repository',jsonb_build_object('full_name',%(live)s)),
       'processing',1,now()-interval '10 minutes',now()),
      ('D-KEEP-DONE','pull_request',%(account)s,%(keep)s,'{}'::jsonb,'done',1,now(),NULL),
      ('D-LEGACY-REPLACEMENT-QUEUED','push',%(account)s,%(legacy_replacement)s,
       jsonb_build_object('repository',jsonb_build_object('id',%(legacy_new_id)s,'full_name',%(legacy_replacement)s)),
       'queued',0,now(),NULL),
      ('D-REUSED-STALE-NAME-DELETE','repository',%(account)s,%(reused)s,
       jsonb_build_object('action','deleted','repository',jsonb_build_object('full_name',%(reused)s)),
       'processing',1,now()-interval '2 hours',now()),
      ('D-NAME-ONLY-CURRENT-REMOVE','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','removed','repositories_removed',
                          jsonb_build_array(jsonb_build_object('full_name',%(name_only_live)s))),
       'processing',1,now()-interval '10 minutes',now()),
      ('D-ORDER-ADD-OLD','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(ordered_id)s,'full_name',%(ordered)s))),
       'processing',1,now()-interval '3 hours',now()),
      ('D-ORDER-DIFFERENT-ADD-OLD','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(ordered_old_id)s,'full_name',%(ordered)s))),
       'processing',1,now()-interval '3 hours 30 minutes',now()),
      ('D-ORDER-REMOVE-NEW','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','removed','repositories_removed',
                          jsonb_build_array(jsonb_build_object('id',%(ordered_id)s,'full_name',%(ordered)s))),
       'processing',1,now()-interval '2 hours',now()),
      ('D-ORDER-ADD-NEW','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(ordered_id)s,'full_name',%(ordered)s))),
       'processing',1,now()-interval '1 hour',now()),
      ('D-INSTALL-CREATED','installation',%(account)s,NULL,
       jsonb_build_object('action','created','repositories',
                          jsonb_build_array(jsonb_build_object('id',%(ordered_id)s,'full_name',%(ordered)s))),
       'processing',1,now()-interval '30 minutes',now()),
      ('D-REVERSE-REMOVE-OLD','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','removed','repositories_removed',
                          jsonb_build_array(jsonb_build_object('id',%(reverse_ordered_id)s,'full_name',%(reverse_ordered)s))),
       'processing',1,now()-interval '3 hours',now()),
      ('D-REVERSE-ADD-NEW','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(reverse_ordered_id)s,'full_name',%(reverse_ordered)s))),
       'processing',1,now()-interval '2 hours',now()),
      ('D-GRAPHLESS-DELETE','repository',%(account)s,%(graphless_renamed_old)s,
       jsonb_build_object('action','deleted','repository',
                          jsonb_build_object('id',%(graphless_renamed_id)s,'full_name',%(graphless_renamed_old)s)),
       'processing',1,now(),now()),
      ('D-PENDING-ADD','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(pending_new_id)s,'full_name',%(pending)s))),
       'processing',1,now()-interval '1 hour',now()),
      ('D-LEGACY-ADD','installation_repositories',%(account)s,NULL,
       jsonb_build_object('action','added','repositories_added',
                          jsonb_build_array(jsonb_build_object('id',%(legacy_new_id)s,'full_name',%(legacy_replacement)s))),
       'processing',1,now()-interval '1 hour',now()),
      ('D-QUEUE-DELETE-OLD','repository',%(account)s,%(queued_replacement)s,
       jsonb_build_object('action','deleted','repository',
                          jsonb_build_object('id',%(queued_old_id)s,'full_name',%(queued_replacement)s)),
       'processing',1,now()-interval '1 hour',now()),
      ('D-QUEUE-WORK-OLD','push',%(account)s,%(queued_replacement)s,
       jsonb_build_object('repository',
                          jsonb_build_object('id',%(queued_old_id)s,'full_name',%(queued_replacement)s)),
       'queued',0,now()-interval '30 minutes',NULL),
      ('D-QUEUE-WORK-NEW','push',%(account)s,%(queued_replacement)s,
       jsonb_build_object('repository',
                          jsonb_build_object('id',%(queued_new_id)s,'full_name',%(queued_replacement)s)),
       'queued',0,now()-interval '2 hours',NULL),
      ('D-QUEUE-WORK-LEGACY','push',%(account)s,%(queued_replacement)s,
       jsonb_build_object('repository',jsonb_build_object('full_name',%(queued_replacement)s)),
       'queued',0,now()-interval '2 hours',NULL),
      ('D-QUEUE-DONE-OLD','pull_request',%(account)s,%(queued_replacement)s,
       '{}'::jsonb,'done',1,now()-interval '2 hours',NULL),
      ('D-QUEUE-FOREIGN','push','ACCT-FOREIGN',%(queued_replacement)s,
       jsonb_build_object('repository',
                          jsonb_build_object('id',%(queued_old_id)s,'full_name',%(queued_replacement)s)),
       'queued',0,now()-interval '2 hours',NULL);
    -- These rows model deliveries already owned by a pre-schema worker. The lease migration backfills such
    -- processing rows to generation 1; seeding generation 0 after the migration would be a production-impossible
    -- state and makes the exact generation-1 rolling finish shim correctly refuse them.
    UPDATE core.webhook_delivery SET lease_generation=1 WHERE status='processing';
    """
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, {
                "installation": INSTALLATION,
                "account": ACCOUNT,
                "live": LIVE_REPO,
                "keep": KEEP_REPO,
                "reused": REUSED_REPO,
                "pending": PENDING_REPLACEMENT_REPO,
                "legacy_replacement": LEGACY_REPLACEMENT_REPO,
                "name_only_live": NAME_ONLY_LIVE_REPO,
                "ordered": ORDERED_REPO,
                "reverse_ordered": REVERSE_ORDERED_REPO,
                "graphless_renamed_old": GRAPHLESS_RENAMED_OLD_REPO,
                "graphless_renamed": GRAPHLESS_RENAMED_REPO,
                "queued_replacement": QUEUED_REPLACEMENT_REPO,
                "strict_renamed": STRICT_RENAMED_REPO,
                "repo_id": REPO_ID,
                "reused_id": REUSED_ID,
                "legacy_new_id": LEGACY_NEW_ID,
                "pending_new_id": PENDING_NEW_ID,
                "name_only_live_id": NAME_ONLY_LIVE_ID,
                "ordered_id": ORDERED_ID,
                "ordered_old_id": ORDERED_OLD_ID,
                "reverse_ordered_id": REVERSE_ORDERED_ID,
                "graphless_renamed_id": GRAPHLESS_RENAMED_ID,
                "queued_old_id": QUEUED_OLD_ID,
                "queued_new_id": QUEUED_NEW_ID,
                "strict_renamed_id": STRICT_RENAMED_ID,
            })
    finally:
        conn.close()


def main() -> int:
    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB], cwd=ROOT,
        capture_output=True, text=True,
    )
    if boot.returncode != 0:
        print("bootstrap failed")
        print((boot.stdout + boot.stderr)[-2000:])
        return 2

    checks: list[tuple[str, bool]] = []

    def check(label: str, passed: bool) -> None:
        checks.append((label, bool(passed)))
        print(("[PASS] " if passed else "[FAIL] ") + label)

    # The new worker must never call a proofless cross-tenant purge.  It threads the private durable delivery key,
    # GitHub's stable repository.id, and one scoped live current-identity point read; the DB revalidates both proofs.
    transfer_calls = []

    def capture_transfer(sql, args=()):
        transfer_calls.append((sql, args))
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "activated": True, "stale_lifecycle_event": False}
        if len(args) >= 5 and args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        if len(args) >= 5 and args[4] == "found":
            return {"ok": True, "transferred": True, "ownership_isolated": True,
                    "current_account_owns_repository": True, "reingest_on_next_push": True}
        return {"ok": True, "transferred": False, "ownership_isolated": False,
                "stale_lifecycle_event": True}

    transfer_handler_payload = {
        "action": "transferred",
        "repository": {
            "id": 410010,
            "name": "svc",
            "full_name": "new-transfer/svc",
            "owner": {"id": 410002, "login": "new-transfer"},
        },
        "changes": {"owner": {"from": {"organization": {
            "id": 410001, "login": "old-transfer",
        }}}},
        "_veripsa_delivery_key": "D-XFER-HANDLER",
    }

    class TransferIdentityGH:
        def __init__(self, current=None, error=None):
            self.current = current
            self.error = error
            self.calls = []

        def repo_current_identity(self, repo):
            self.calls.append(repo)
            if self.error is not None:
                raise self.error
            return self.current

    transfer_gh = TransferIdentityGH({
        "id": 410010, "owner_id": 410002, "full_name": "new-transfer/svc",
    })
    transfer_handler_result = webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, capture_transfer, transfer_gh)
    check("cross-account transfer handler passes durable + scoped live identity proof to the /8 boundary",
          transfer_handler_result.get("cross_account") is True
          and len(transfer_calls) == 3
          and transfer_gh.calls == ["new-transfer/svc"]
          and transfer_calls[0][1][-4:] == ("probe", None, None, None)
          and "transfer_repo_coordinate_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)" in transfer_calls[1][0]
          and transfer_calls[1][1] == (
              "ACCT-GH-410001", "old-transfer/svc", "410010", "D-XFER-HANDLER",
              "found", "410010", "410002", "new-transfer/svc")
          and transfer_calls[2][1] == ("new-transfer/svc", "410010", "D-XFER-HANDLER"))

    renamed_transfer_calls = []

    def capture_renamed_transfer(sql, args=()):
        renamed_transfer_calls.append((sql, args))
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "activated": True, "stale_lifecycle_event": False}
        if args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        return {"ok": True, "transferred": True, "ownership_isolated": True,
                "current_account_owns_repository": True, "reingest_on_next_push": True}

    renamed_transfer_payload = json.loads(json.dumps(transfer_handler_payload))
    renamed_transfer_payload["repository"]["name"] = "new-svc"
    renamed_transfer_payload["repository"]["full_name"] = "new-transfer/new-svc"
    renamed_transfer_payload["changes"]["repository"] = {"name": {"from": "old-svc"}}
    renamed_transfer_gh = TransferIdentityGH({
        "id": 410010, "owner_id": 410002, "full_name": "new-transfer/new-svc",
    })
    renamed_transfer_result = webhook_handlers._handle_repository_event(
        "repository", renamed_transfer_payload, capture_renamed_transfer, renamed_transfer_gh)
    check("transfer-with-rename derives the former coordinate from changes.repository.name.from",
          renamed_transfer_result.get("old") == "old-transfer/old-svc"
          and renamed_transfer_calls[0][1][:2] == (
              "ACCT-GH-410001", "old-transfer/old-svc")
          and renamed_transfer_calls[1][1][:2] == (
              "ACCT-GH-410001", "old-transfer/old-svc"))

    redirect_calls = []

    def capture_redirect_transfer(sql, args=()):
        redirect_calls.append((sql, args))
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "activated": True, "stale_lifecycle_event": False}
        if args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        if args[4] == "lock_current":
            return {"ok": True, "completed": False, "current_reproof_required": True}
        return {"ok": True, "transferred": True, "ownership_isolated": True,
                "current_account_owns_repository": True, "reingest_on_next_push": True}

    redirect_gh = TransferIdentityGH({
        "id": 410010, "owner_id": 410002, "full_name": "new-transfer/renamed-svc",
    })
    redirect_handler_result = webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, capture_redirect_transfer, redirect_gh)
    check("same-owner redirect is locked and point-read again before transfer activation",
          redirect_handler_result.get("reactivated", {}).get("activated") is True
          and redirect_gh.calls == ["new-transfer/svc", "new-transfer/renamed-svc"]
          and [call[1][4] for call in redirect_calls[:3]] == ["probe", "lock_current", "found"]
          and redirect_calls[1][1][-4:] == (
              "lock_current", "410010", "410002", "new-transfer/renamed-svc")
          and redirect_calls[3][1] == (
              "new-transfer/renamed-svc", "410010", "D-XFER-HANDLER"))

    onward_calls = []

    def capture_onward_transfer(sql, args=()):
        onward_calls.append((sql, args))
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "activated": True, "stale_lifecycle_event": False}
        if args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        if args[4] == "lock_current":
            return {"ok": True, "completed": False, "current_reproof_required": True}
        return {"ok": True, "transferred": True, "ownership_isolated": True,
                "current_account_owns_repository": False, "reingest_on_next_push": True}

    onward_gh = TransferIdentityGH({
        "id": 410010, "owner_id": 410003, "full_name": "onward-transfer/svc",
    })
    onward_handler_result = webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, capture_onward_transfer, onward_gh)
    check("rapid A-to-B-to-C proof isolates A without activating C inside tenant B",
          onward_handler_result.get("transferred", {}).get("ownership_isolated") is True
          and onward_handler_result.get("reactivated") is None
          and onward_gh.calls == ["new-transfer/svc", "onward-transfer/svc"]
          and [call[1][4] for call in onward_calls] == ["probe", "lock_current", "found"]
          and not any("reactivate_repository_with_authority" in call[0] for call in onward_calls))

    private_onward_calls = []

    def capture_private_onward(sql, args=()):
        private_onward_calls.append((sql, args))
        if "reactivate_repository_with_authority" in sql:
            return {"ok": True, "activated": True, "stale_lifecycle_event": False}
        if args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        if args[4] == "lock_current":
            return {"ok": True, "completed": False, "current_reproof_required": True}
        return {"ok": True, "transferred": True, "ownership_isolated": True,
                "current_account_owns_repository": False, "reingest_on_next_push": True}

    class PrivateOnwardGH:
        def __init__(self):
            self.installation_calls = []
            self.app_calls = []

        def repo_current_identity(self, repo):
            self.installation_calls.append(repo)
            return None

        def repo_current_identity_via_app_installation(self, repo):
            self.app_calls.append(repo)
            return {"id": 410010, "owner_id": 410003, "full_name": "private-third/svc"}

    private_onward_gh = PrivateOnwardGH()
    private_onward_result = webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, capture_private_onward, private_onward_gh)
    check("private A-to-B-to-C transfer resolves C through App installation authority and still avoids B activation",
          private_onward_result.get("transferred", {}).get("ownership_isolated") is True
          and private_onward_result.get("reactivated") is None
          and private_onward_gh.installation_calls == [
              "new-transfer/svc", "private-third/svc"]
          and private_onward_gh.app_calls == ["new-transfer/svc", "private-third/svc"]
          and [call[1][4] for call in private_onward_calls] == ["probe", "lock_current", "found"])

    changing_redirect_calls = []

    def capture_changing_redirect(sql, args=()):
        changing_redirect_calls.append((sql, args))
        if args[4] == "probe":
            return {"ok": True, "completed": False, "proof_required": True}
        return {"ok": True, "completed": False, "current_reproof_required": True}

    class ChangingRedirectGH:
        def __init__(self):
            self.calls = []

        def repo_current_identity(self, repo):
            self.calls.append(repo)
            if len(self.calls) == 1:
                return {"id": 410010, "owner_id": 410002,
                        "full_name": "new-transfer/renamed-svc"}
            return None

    changing_redirect_gh = ChangingRedirectGH()
    changing_redirect_retried = False
    try:
        webhook_handlers._handle_repository_event(
            "repository", transfer_handler_payload, capture_changing_redirect, changing_redirect_gh)
    except RuntimeError as exc:
        changing_redirect_retried = "changed during locked proof" in str(exc)
    check("redirect identity changing after lock retries without finalizing an old proof",
          changing_redirect_retried and changing_redirect_gh.calls == [
              "new-transfer/svc", "new-transfer/renamed-svc"]
          and [call[1][4] for call in changing_redirect_calls] == ["probe", "lock_current"])
    missing_transfer_authority_refused = False
    try:
        webhook_handlers._handle_repository_event(
            "repository",
            {k: v for k, v in transfer_handler_payload.items()
             if k != "_veripsa_delivery_key"},
            capture_transfer,
            transfer_gh,
        )
    except RuntimeError as exc:
        missing_transfer_authority_refused = "durable delivery authority" in str(exc)
    check("cross-account transfer handler fails closed without durable delivery authority",
          missing_transfer_authority_refused and len(transfer_calls) == 3)
    absent_gh = TransferIdentityGH(None)
    webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, capture_transfer, absent_gh)
    check("cross-account transfer 404 is threaded as non-destructive absent proof",
          absent_gh.calls == ["new-transfer/svc"]
          and transfer_calls[-1][1][-4:] == ("absent", None, None, None))
    transient_retried = False
    try:
        webhook_handlers._handle_repository_event(
            "repository", transfer_handler_payload, capture_transfer,
            TransferIdentityGH(error=RuntimeError("transient point read")))
    except RuntimeError as exc:
        transient_retried = "transient point read" in str(exc)
    malformed_retried = False
    try:
        webhook_handlers._handle_repository_event(
            "repository", transfer_handler_payload, capture_transfer,
            TransferIdentityGH({"id": 410010, "full_name": "new-transfer/svc"}))
    except RuntimeError as exc:
        malformed_retried = "current identity is malformed" in str(exc)
    check("transient or malformed current-identity reads retry instead of purging",
          transient_retried and malformed_retried)

    completed_calls = []

    def completed_transfer(sql, args=()):
        completed_calls.append((sql, args))
        return {"ok": True, "transferred": True, "stale_lifecycle_event": False,
                "idempotent": True, "reingest_on_next_push": True}

    crash_gap_gh = TransferIdentityGH(error=RuntimeError("must not point read"))
    crash_gap_result = webhook_handlers._handle_repository_event(
        "repository", transfer_handler_payload, completed_transfer, crash_gap_gh)
    check("committed transfer marker finalizes recovery without a fallible GitHub point read",
          crash_gap_result.get("transferred", {}).get("idempotent") is True
          and len(completed_calls) == 1 and completed_calls[0][1][4] == "probe"
          and crash_gap_gh.calls == [])

    tombstoned_boot_bind_refused = False
    try:
        ingest._bind_onboarded_repo_identity(
            lambda _sql, _args=(): {
                "ok": True, "reconciled": False,
                "reason": "repository stable id is tombstoned",
            },
            "old-owner/late-boot", "410010",
        )
    except ingest.RepositoryIdentityBindingError as exc:
        tombstoned_boot_bind_refused = "tombstoned" in str(exc)
    check("boot identity binding stops before backfill when transfer already tombstoned the stable id",
          tombstoned_boot_bind_refused)

    def seed_add_delivery(key: str, repo: str, repository_id: str | None) -> int:
        repository = {"full_name": repo}
        if repository_id is not None:
            repository["id"] = repository_id
        return int(admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at,lease_generation) "
            "VALUES (%s,'installation_repositories',%s,NULL,%s::jsonb,'processing',1,clock_timestamp(),clock_timestamp(),1) "
            "RETURNING 1",
            (key, ACCOUNT, json.dumps({"action": "added", "repositories_added": [repository]})),
        ))

    def seed_delete_delivery(key: str, repo: str, repository_id: str) -> int:
        return int(admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at,lease_generation) "
            "VALUES (%s,'repository',%s,%s,%s::jsonb,'processing',1,clock_timestamp(),clock_timestamp(),1) "
            "RETURNING 1",
            (key, ACCOUNT, repo, json.dumps({
                "action": "deleted",
                "repository": {"id": repository_id, "full_name": repo},
            })),
        ))

    def seed_remove_delivery(key: str, repo: str, repository_id: str | None) -> int:
        repository = {"full_name": repo}
        if repository_id is not None:
            repository["id"] = repository_id
        return int(admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at,lease_generation) "
            "VALUES (%s,'installation_repositories',%s,NULL,%s::jsonb,'processing',1,"
            "clock_timestamp(),clock_timestamp(),1) RETURNING 1",
            (key, ACCOUNT, json.dumps({"action": "removed", "repositories_removed": [repository]})),
        ))

    def age_delivery(key: str, age: str) -> int:
        return int(admin(
            "UPDATE core.webhook_delivery SET received_at=clock_timestamp()-(%s::interval),updated_at=now() "
            "WHERE delivery_key=%s RETURNING 1",
            (age, key),
        ))

    offboard_sequence = 0

    def offboard_current(repo: str, repository_id: str | None, reason: str) -> dict:
        nonlocal offboard_sequence
        offboard_sequence += 1
        key = f"D-OPERATOR-SAFE-OFFBOARD-{offboard_sequence}"
        if reason == "repository_deleted":
            if repository_id is None:
                raise AssertionError("repository.deleted fixture requires a stable id")
            seed_delete_delivery(key, repo, repository_id)
        else:
            seed_remove_delivery(key, repo, repository_id)
            if repository_id is None:
                # Legacy name-only fixtures exercise the post-consistency decision, not the deliberate fresh-event
                # defer path (which has its own focused assertion below).
                age_delivery(key, "10 minutes")
        return as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (repo, repository_id, reason, key),
        ))

    def seed_graph(repo: str, repository_id: str, node_id: str) -> int:
        return int(account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,%s,'main',repeat('a',40),1,0,%s,clock_timestamp()); "
            "SELECT core.mark_governed_write('code_node'); "
            "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) "
            "VALUES (%s,%s,'main',%s,'file','src/current.py'); SELECT 1",
            (ACCOUNT, repo, repository_id, ACCOUNT, repo, node_id),
        ))

    try:
        # Durable minimization must retain stable ids for both top-level and install fan-out repos.
        top = delivery_queue.sanitize_payload("repository", {
            "action": "deleted",
            "repository": {"id": 4004, "full_name": LIVE_REPO},
        })
        fanout = delivery_queue.sanitize_payload("installation_repositories", {
            "action": "removed",
            "repositories_removed": [{"id": 4004, "full_name": LIVE_REPO}],
        })
        renamed_transfer_payload = delivery_queue.sanitize_payload("repository", {
            "action": "transferred",
            "repository": {"id": 4004, "name": "new-name", "full_name": "new/new-name"},
            "changes": {"repository": {"name": {"from": "old-name"}}},
        })
        check("sanitizer retains top-level repository.id",
              str(top.get("repository", {}).get("id")) == REPO_ID)
        check("sanitizer retains installation fan-out repository.id",
              str(fanout.get("repositories_removed", [{}])[0].get("id")) == REPO_ID)
        check("sanitizer retains the former short name for transfer-with-rename authority",
              renamed_transfer_payload.get("changes", {}).get("repository", {})
              .get("name", {}).get("from") == "old-name")
        background_lock_conn = conn_for("veripsa_app")
        try:
            background_lock_conn.autocommit = True
            with background_lock_conn.cursor() as background_lock_cur:
                server_dbops._take_repository_id_lock(background_lock_cur, "404004")
                background_stable_lock_held = admin(
                    "SELECT NOT pg_try_advisory_xact_lock("
                    "hashtext('github-repository-id'),hashtext(%s))",
                    ("404004",),
                )
        finally:
            background_lock_conn.close()
        background_stable_lock_released = admin(
            "SELECT pg_try_advisory_xact_lock("
            "hashtext('github-repository-id'),hashtext(%s))",
            ("404004",),
        )
        check("autocommit background writer holds the global stable-id lock until its connection closes",
              background_stable_lock_held is True and background_stable_lock_released is True)
        check("multi-coordinate lifecycle actions delegate mutable locks to the DB transaction",
              all(event_processor._lifecycle_owns_coordinate_locks(event_type, {"action": action})
                  for event_type, action in (
                      ("repository", "renamed"),
                      ("repository", "transferred"),
                  ))
              and event_processor._lifecycle_owns_coordinate_locks(
                  "repository", {"action": "deleted", "repository": {"id": "4004"}})
              and event_processor._lifecycle_owns_coordinate_locks(
                  "installation_repositories", {
                      "action": "removed", "repositories_removed": [{"id": "4004"}],
                  })
              and not event_processor._lifecycle_owns_coordinate_locks(
                  "repository", {"action": "deleted", "repository": {}})
              and not event_processor._lifecycle_owns_coordinate_locks(
                  "installation_repositories", {
                      "action": "removed", "repositories_removed": [{"full_name": "acme/legacy"}],
                  })
              and not event_processor._lifecycle_owns_coordinate_locks("push", {})
              and not event_processor._lifecycle_owns_coordinate_locks(
                  "repository", {"action": "created"})
              and not event_processor._lifecycle_owns_coordinate_locks(
                  "installation_repositories", {"action": "added"}))

        # Lock-order regression: each live session already owns its authenticated payload ID before rename. A
        # nested helper that discovers the opposite historical ID creates ID-A→ID-B / ID-B→ID-A inversion. The
        # mover may take its old/new mutable coordinates in sorted order, but never another historical stable-ID
        # key, so both disjoint renames finish while both payload-ID locks remain held.
        inverse_id_a, inverse_id_b = "404005", "404006"
        inverse_old_a, inverse_new_a = "acme/inverse-a-old", "acme/inverse-a-new"
        inverse_old_b, inverse_new_b = "acme/inverse-b-old", "acme/inverse-b-new"
        seed_graph(inverse_old_a, inverse_id_b, "INVERSE-A-N")
        seed_graph(inverse_old_b, inverse_id_a, "INVERSE-B-N")
        # Coordinate the actual DB calls only after BOTH sessions have acquired their independent payload-ID
        # locks. A short Barrier.wait(timeout) made this a host-load race: if the second thread took >2s merely
        # to connect/acquire its uncontended lock, the first raised BrokenBarrierError and the gate failed without
        # exercising rename at all. The condition is a deterministic two-phase handshake; the bounded main-thread
        # wait remains only a fail-safe, and always releases any waiter so a setup failure cannot strand a daemon
        # connection holding a session advisory lock.
        inverse_condition = threading.Condition()
        inverse_ready = set()
        inverse_release = False
        inverse_results = []
        inverse_errors = []

        def run_inverse_rename(held_id, old_repo, new_repo):
            nonlocal inverse_release
            inverse_conn = None
            try:
                inverse_conn = conn_for("veripsa_app")
                inverse_conn.autocommit = True
                with inverse_conn.cursor() as inverse_cur:
                    inverse_cur.execute("SET search_path=core")
                    inverse_cur.execute("SET lock_timeout='3s'")
                    inverse_cur.execute(
                        "SELECT set_config('core.installation_account',%s,false)", (ACCOUNT,))
                    inverse_cur.execute(
                        "SELECT pg_advisory_lock(hashtext('github-repository-id'),hashtext(%s))",
                        (held_id,),
                    )
                    with inverse_condition:
                        inverse_ready.add(held_id)
                        inverse_condition.notify_all()
                        inverse_condition.wait_for(lambda: inverse_release)
                    inverse_cur.execute(
                        "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
                        (old_repo, new_repo),
                    )
                    inverse_results.append(as_json(inverse_cur.fetchone()[0]))
            except Exception as exc:
                with inverse_condition:
                    inverse_errors.append(getattr(exc, "pgcode", None) or type(exc).__name__)
                    inverse_condition.notify_all()
            finally:
                if inverse_conn is not None:
                    inverse_conn.close()

        inverse_threads = (
            threading.Thread(
                target=run_inverse_rename,
                args=(inverse_id_a, inverse_old_a, inverse_new_a), daemon=True),
            threading.Thread(
                target=run_inverse_rename,
                args=(inverse_id_b, inverse_old_b, inverse_new_b), daemon=True),
        )
        for inverse_thread in inverse_threads:
            inverse_thread.start()
        with inverse_condition:
            inverse_synchronized = inverse_condition.wait_for(
                lambda: len(inverse_ready) == 2 or bool(inverse_errors), timeout=15)
            inverse_synchronized = inverse_synchronized and len(inverse_ready) == 2
            inverse_release = True
            inverse_condition.notify_all()
        for inverse_thread in inverse_threads:
            inverse_thread.join(15)
        inverse_alive = [inverse_thread.name for inverse_thread in inverse_threads
                         if inverse_thread.is_alive()]
        inverse_counts = (
            count("graph_version", inverse_old_a),
            count("graph_version", inverse_old_b),
            count("graph_version", inverse_new_a),
            count("graph_version", inverse_new_b),
        )
        inverse_ok = (inverse_synchronized and inverse_alive == []
                      and inverse_errors == [] and len(inverse_results) == 2
                      and inverse_counts == (0, 0, 1, 1))
        inverse_diagnostic = (
            "" if inverse_ok else
            f" (synchronized={inverse_synchronized}, ready={sorted(inverse_ready)}, "
            f"alive={inverse_alive}, errors={inverse_errors}, results={len(inverse_results)}, "
            f"counts={inverse_counts})"
        )
        check("rename never nests a second stable-id lock behind the live payload-id lock"
              + inverse_diagnostic, inverse_ok)

        # Real two-transaction name swap: ID-A's durable delete is signed at coordinate A but its exact graph has
        # moved to B, while ID-B is the mirror image. Both DB calls need {A,B}; sorted candidate locking lets one
        # wait before holding the second key, then both commit without a deadlock or timeout.
        swap_id_a, swap_id_b = "404007", "404008"
        swap_coord_a, swap_coord_b = "acme/swap-a", "acme/swap-b"
        swap_delivery_a, swap_delivery_b = "D-SWAP-DELETE-A", "D-SWAP-DELETE-B"
        seed_graph(swap_coord_a, swap_id_b, "SWAP-B-N")
        seed_graph(swap_coord_b, swap_id_a, "SWAP-A-N")
        seed_delete_delivery(swap_delivery_a, swap_coord_a, swap_id_a)
        seed_delete_delivery(swap_delivery_b, swap_coord_b, swap_id_b)
        swap_condition = threading.Condition()
        swap_ready = set()
        swap_release = False
        swap_results = []
        swap_errors = []

        def run_swap_delete(repository_id, signed_repo, delivery_key):
            nonlocal swap_release
            swap_conn = None
            try:
                swap_conn = conn_for("veripsa_app")
                swap_conn.autocommit = True
                with swap_conn.cursor() as swap_cur:
                    swap_cur.execute("SET search_path=core")
                    swap_cur.execute("SET lock_timeout='5s'")
                    swap_cur.execute(
                        "SELECT set_config('core.installation_account',%s,false)", (ACCOUNT,))
                    server_dbops._take_repository_id_lock(swap_cur, repository_id)
                    with swap_condition:
                        swap_ready.add(repository_id)
                        swap_condition.notify_all()
                        swap_condition.wait_for(lambda: swap_release)
                    swap_cur.execute(
                        "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                        (signed_repo, repository_id, "repository_deleted", delivery_key),
                    )
                    swap_results.append(as_json(swap_cur.fetchone()[0]))
            except Exception as exc:
                with swap_condition:
                    swap_errors.append(getattr(exc, "pgcode", None) or type(exc).__name__)
                    swap_condition.notify_all()
            finally:
                if swap_conn is not None:
                    swap_conn.close()

        swap_threads = (
            threading.Thread(
                target=run_swap_delete,
                args=(swap_id_a, swap_coord_a, swap_delivery_a), daemon=True),
            threading.Thread(
                target=run_swap_delete,
                args=(swap_id_b, swap_coord_b, swap_delivery_b), daemon=True),
        )
        for swap_thread in swap_threads:
            swap_thread.start()
        with swap_condition:
            swap_synchronized = swap_condition.wait_for(
                lambda: len(swap_ready) == 2 or bool(swap_errors), timeout=15)
            swap_synchronized = swap_synchronized and len(swap_ready) == 2
            swap_release = True
            swap_condition.notify_all()
        for swap_thread in swap_threads:
            swap_thread.join(15)
        swap_alive = [swap_thread.name for swap_thread in swap_threads if swap_thread.is_alive()]
        swap_remaining = (
            count("graph_version", swap_coord_a), count("graph_version", swap_coord_b))
        swap_target_sets = [result.get("targets", []) for result in swap_results]
        check("sorted DB candidate locks serialize a two-ID coordinate swap without deadlock "
              f"(synchronized={swap_synchronized}, ready={sorted(swap_ready)}, alive={swap_alive}, "
              f"errors={swap_errors}, targets={swap_target_sets}, remaining={swap_remaining})",
              swap_synchronized and swap_alive == []
              and swap_errors == [] and len(swap_results) == 2
              and all(result.get("ok") is True and result.get("revoked") is True
                      for result in swap_results)
              and any(swap_coord_a in result.get("targets", []) for result in swap_results)
              and any(swap_coord_b in result.get("targets", []) for result in swap_results)
              and swap_remaining == (0, 0))
        malformed_full, malformed_id, malformed_id_present = webhook_handlers._repository_identity(
            {"id": "not-a-github-id", "full_name": LIVE_REPO}
        )
        check("malformed repository id stays distinct from the legacy name-only path",
              malformed_full == LIVE_REPO and malformed_id is None and malformed_id_present is True)
        malformed_repository_ids = (
            "0", "0" + REPO_ID, "\u0664\u0660\u0660\u0664", "9" * 33,
        )
        normalized_ids = [
            webhook_handlers._repository_identity({"id": value, "full_name": LIVE_REPO})[1:]
            for value in malformed_repository_ids
        ]
        check("repository identity accepts only positive canonical ASCII decimals",
              webhook_handlers._repository_identity({"id": int(REPO_ID), "full_name": LIVE_REPO})[1] == REPO_ID
              and webhook_handlers._repository_identity({"id": int(REPO_ID), "full_name": LIVE_REPO})[2] is False
              and webhook_handlers._repository_identity({"id": True, "full_name": LIVE_REPO})[1:] == (None, True)
              and all(value == (None, True) for value in normalized_ids))

        # The DB is the independent authority boundary. Every lifecycle entry point must reject the same malformed
        # ids even if a caller bypasses the Python coercion; otherwise a non-null non-matching id can evade exact-id
        # tombstones and skip the name-only fallback.
        malformed_repo = "acme/noncanonical-id"
        work_rejected = all(
            app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                (malformed_repo, value)) is False
            for value in malformed_repository_ids
        )
        onboarding_rejected = all(
            app("SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
                (malformed_repo, value)) is False
            for value in malformed_repository_ids
        )
        invalid_reactivation = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (malformed_repo, "0" + REPO_ID, "D-NOT-AUTHORITY"),
        ))
        invalid_reconcile = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (malformed_repo, "0" + REPO_ID),
        ))
        invalid_offboard_rejected = False
        try:
            app("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                (malformed_repo, "0" + REPO_ID, "repository_deleted", "D-NOT-AUTHORITY"))
        except psycopg2.Error as exc:
            invalid_offboard_rejected = exc.pgcode == "23514"
        check("DB lifecycle authority rejects noncanonical repository ids",
              work_rejected and onboarding_rejected
              and invalid_reactivation.get("ok") is False
              and invalid_reactivation.get("reason") == "invalid repository id"
              and invalid_reconcile.get("reconciled") is False
              and invalid_offboard_rejected)

        # Reproduce the dangerous end-to-end distinction: a present malformed id is not a legacy id-less event.
        # Both durable payloads name the coordinate and carry a bad id; older coercion collapsed that to None, so
        # the DB's name-only lifecycle path accepted the delivery and cleared the `unknown` revocation marker.
        malformed_add_repo = "acme/malformed-add"
        malformed_created_repo = "acme/malformed-created"
        for repo in (malformed_add_repo, malformed_created_repo):
            offboard_current(repo, None, "installation_removed")
        seed_add_delivery("D-MALFORMED-ADD", malformed_add_repo, "0" + REPO_ID)
        admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) "
            "VALUES (%s,'repository',%s,%s,%s::jsonb,'processing',1,clock_timestamp(),clock_timestamp()) "
            "RETURNING 1",
            ("D-MALFORMED-CREATED", ACCOUNT, malformed_created_repo, json.dumps({
                "action": "created",
                "repository": {"id": "0" + REPO_ID, "full_name": malformed_created_repo},
            })),
        )
        malformed_lifecycle_rejected = []
        for repo, delivery in (
            (malformed_add_repo, "D-MALFORMED-ADD"),
            (malformed_created_repo, "D-MALFORMED-CREATED"),
        ):
            try:
                webhook_handlers._reactivate_repository(
                    app, {"id": "0" + REPO_ID, "full_name": repo}, delivery,
                )
                malformed_lifecycle_rejected.append(False)
            except RuntimeError as exc:
                malformed_lifecycle_rejected.append("invalid repository id" in str(exc))
        malformed_markers = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo IN (%s,%s) AND repository_id='unknown' AND superseded_at IS NULL",
            (ACCOUNT, malformed_add_repo, malformed_created_repo),
        ))
        check("malformed add/create identity cannot enter the legacy reactivation path",
              all(malformed_lifecycle_rejected) and malformed_markers == 2)

        # Simulate the previous production constraints and seed the values they admitted but the canonical contract
        # rejects. Normal schema auto-apply must not rewrite tenant data; runtime reads quarantine each coordinate,
        # while the explicit owner migration refuses to promote constraints until an operator has audited/remediated
        # the aggregate residue. The up/down/verify scripts are transactional and row-preserving.
        legacy_graph_repo = "acme/legacy-graph-id"
        legacy_marker_repo = "acme/legacy-marker-id"
        legacy_activation_repo = "acme/legacy-activation-id"
        admin(
            "ALTER TABLE core.graph_version DROP CONSTRAINT graph_version_repo_id_shape; "
            "ALTER TABLE core.graph_version ADD CONSTRAINT graph_version_repo_id_shape "
            "CHECK (repo_id IS NULL OR (length(repo_id) BETWEEN 1 AND 32 AND repo_id ~ '^[0-9]+$')); "
            "ALTER TABLE core.repository_lifecycle_tombstone "
            "DROP CONSTRAINT repository_tombstone_id_shape, DROP CONSTRAINT repository_tombstone_reason_check; "
            "ALTER TABLE core.repository_lifecycle_tombstone "
            "ADD CONSTRAINT repository_tombstone_id_shape CHECK (repository_id='unknown' OR "
            "(length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[0-9]+$')), "
            "ADD CONSTRAINT repository_tombstone_reason_check CHECK ("
            "reason = ANY (ARRAY['repository_deleted','installation_removed'])); "
            "ALTER TABLE core.repository_lifecycle_activation DROP CONSTRAINT repository_activation_id_shape; "
            "ALTER TABLE core.repository_lifecycle_activation ADD CONSTRAINT repository_activation_id_shape "
            "CHECK (length(repository_id) BETWEEN 1 AND 32 AND repository_id ~ '^[0-9]+$'); "
            "SELECT 1"
        )
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
            "VALUES (%s,%s,'main',repeat('3',40),0,0,%s) RETURNING 1",
            (ACCOUNT, legacy_graph_repo, "0" + REPO_ID),
        )
        account_one(
            "INSERT INTO core.repository_lifecycle_tombstone("
            "account_id,repository_id,repo,reason,lifecycle_received_at) "
            "VALUES (%s,%s,%s,'repository_deleted',clock_timestamp()) RETURNING 1",
            (ACCOUNT, "0" + REPO_ID, legacy_marker_repo),
        )
        account_one(
            "INSERT INTO core.repository_lifecycle_activation(account_id,repository_id,repo,activated_at) "
            "VALUES (%s,'0',%s,clock_timestamp()) RETURNING 1",
            (ACCOUNT, legacy_activation_repo),
        )
        schema_reapply = subprocess.run(
            ["psql", f"postgresql://veripsa_migrator@localhost/{DB}", "-X", "-v", "ON_ERROR_STOP=1",
             "-q", "-f", "db/schema.sql"],
            cwd=ROOT, capture_output=True, text=True,
        )
        check("normal schema auto-apply remains additive over historical repository ids",
              schema_reapply.returncode == 0)
        if schema_reapply.returncode != 0:
            raise RuntimeError((schema_reapply.stdout + schema_reapply.stderr)[-2000:])
        preserved_graph_id = account_one(
            "SELECT repo_id FROM core.graph_version WHERE account_id=%s AND repo=%s AND branch='main'",
            (ACCOUNT, legacy_graph_repo),
        )
        preserved_marker_id = account_one(
            "SELECT repository_id FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s",
            (ACCOUNT, legacy_marker_repo),
        )
        check("stored noncanonical repository identity is quarantined without auto-rewrite",
              preserved_graph_id == "0" + REPO_ID and preserved_marker_id == "0" + REPO_ID
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_graph_repo, REPO_ID)) is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_marker_repo, REPO_ID)) is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_activation_repo, REPO_ID)) is False)

        # An exact-ID delete may purge only coordinates whose every graph/activation row positively carries that
        # exact ID. Historical noncanonical identity is ambiguous replacement residue and stays quarantined until
        # explicit lifecycle authority resets it; cover both fallback and exact-ID candidate paths.
        legacy_delete_graph_repo = "acme/legacy-delete-graph-id"
        legacy_delete_activation_repo = "acme/legacy-delete-activation-id"
        legacy_delete_mixed_repo = "acme/legacy-delete-mixed-id"
        legacy_delete_cases = (
            (legacy_delete_graph_repo, "31001"),
            (legacy_delete_activation_repo, "31002"),
            (legacy_delete_mixed_repo, "31003"),
        )
        for repo, repo_id, sha in (
            (legacy_delete_graph_repo, "031001", "6"),
            (legacy_delete_mixed_repo, "31003", "7"),
        ):
            account_one(
                "SELECT core.mark_governed_write('graph_version'); "
                "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
                "VALUES (%s,%s,'main',repeat(%s,40),0,0,%s) RETURNING 1",
                (ACCOUNT, repo, sha, repo_id),
            )
        for repo, repository_id in (
            (legacy_delete_activation_repo, "031002"),
            (legacy_delete_mixed_repo, "031003"),
        ):
            account_one(
                "INSERT INTO core.repository_lifecycle_activation(account_id,repository_id,repo,activated_at) "
                "VALUES (%s,%s,%s,clock_timestamp()) RETURNING 1",
                (ACCOUNT, repository_id, repo),
            )

        offboard_results = []
        for index, (repo, repository_id) in enumerate(legacy_delete_cases, start=1):
            delivery = f"D-LEGACY-ID-DELETE-{index}"
            seed_delete_delivery(delivery, repo, repository_id)
            offboard_results.append(as_json(app(
                "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                (repo, repository_id, "repository_deleted", delivery),
            )))
        check("canonical delete isolates quarantined noncanonical repository state",
              all(result.get("ok") is True and result.get("targets") == []
                  for result, (repo, _repository_id) in zip(offboard_results, legacy_delete_cases))
              and count("graph_version", legacy_delete_graph_repo) == 1
              and count("graph_version", legacy_delete_activation_repo) == 0
              and count("graph_version", legacy_delete_mixed_repo) == 1
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                  "WHERE account_id=%s AND repo IN (%s,%s)",
                  (ACCOUNT, legacy_delete_activation_repo, legacy_delete_mixed_repo),
              )) == 2
              and all(app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                          (repo, repository_id)) is False
                      for repo, repository_id in legacy_delete_cases))

        # A generic Core graph has no authenticated GitHub stable identity. If a former name is reused after ID1
        # moved away, a delayed ID1 delete may purge the exact-ID current coordinate but must preserve/isolate the
        # NULL-ID replacement. The same rule applies to a mixed exact+NULL branch coordinate: no partial purge.
        null_reuse_old = "acme/null-reuse-old"
        null_reuse_current = "acme/null-reuse-current"
        null_reuse_id = "31004"
        mixed_null_repo = "acme/mixed-exact-null-delete"
        mixed_null_id = "31005"
        seed_graph(null_reuse_current, null_reuse_id, "NULL-REUSE-CURRENT-N")
        seed_graph(mixed_null_repo, mixed_null_id, "MIXED-EXACT-N")
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version("
            "account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) VALUES "
            "(%s,%s,'main',repeat('8',40),1,0,NULL,clock_timestamp()),"
            "(%s,%s,'legacy',repeat('9',40),1,0,NULL,clock_timestamp()); "
            "SELECT core.mark_governed_write('code_node'); "
            "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) VALUES "
            "(%s,%s,'main','NULL-REUSE-OLD-N','file','src/generic.py'),"
            "(%s,%s,'legacy','MIXED-NULL-N','file','src/generic.py'); SELECT 1",
            (ACCOUNT, null_reuse_old, ACCOUNT, mixed_null_repo,
             ACCOUNT, null_reuse_old, ACCOUNT, mixed_null_repo),
        )
        null_reuse_delete = offboard_current(
            null_reuse_old, null_reuse_id, "repository_deleted")
        mixed_null_delete = offboard_current(
            mixed_null_repo, mixed_null_id, "repository_deleted")
        check("exact-ID delete preserves a NULL-ID same-name replacement",
              null_reuse_delete.get("targets") == [null_reuse_current]
              and count("graph_version", null_reuse_current) == 0
              and count("graph_version", null_reuse_old) == 1
              and count("code_node", null_reuse_old) == 1)
        check("exact-ID delete preserves a mixed exact-plus-NULL coordinate atomically",
              mixed_null_delete.get("targets") == []
              and count("graph_version", mixed_null_repo) == 2
              and count("code_node", mixed_null_repo) == 2
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (mixed_null_repo, mixed_null_id)) is False)

        legacy_graph_recover_repo = "acme/legacy-graph-id-recovery"
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
            "VALUES (%s,%s,'main',repeat('4',40),0,0,%s) RETURNING 1",
            (ACCOUNT, legacy_graph_recover_repo, "0" + REPO_ID),
        )
        seed_add_delivery("D-LEGACY-GRAPH-ID-RECOVERY", legacy_graph_recover_repo, REPO_ID)
        legacy_graph_recovered = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (legacy_graph_recover_repo, REPO_ID, "D-LEGACY-GRAPH-ID-RECOVERY"),
        ))
        check("current lifecycle authority resets a graph-only noncanonical identity",
              legacy_graph_recovered.get("ok") is True
              and legacy_graph_recovered.get("lifecycle_reset") is True
              and count("graph_version", legacy_graph_recover_repo) == 0
              and account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id<>'unknown'",
                  (ACCOUNT, legacy_graph_recover_repo),
              ) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_graph_recover_repo, REPO_ID)) is True)

        legacy_recover_repo = "acme/legacy-id-recovery"
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
            "VALUES (%s,%s,'main',repeat('5',40),0,0,%s) RETURNING 1",
            (ACCOUNT, legacy_recover_repo, "0" + REPO_ID),
        )
        account_one(
            "INSERT INTO core.repository_lifecycle_activation(account_id,repository_id,repo,activated_at) "
            "VALUES (%s,'00',%s,clock_timestamp()) RETURNING 1",
            (ACCOUNT, legacy_recover_repo),
        )
        account_one(
            "INSERT INTO core.repository_lifecycle_tombstone("
            "account_id,repository_id,repo,reason,lifecycle_received_at) "
            "VALUES (%s,'000',%s,'repository_deleted',clock_timestamp()) RETURNING 1",
            (ACCOUNT, legacy_recover_repo),
        )
        seed_add_delivery("D-LEGACY-ID-RECOVERY", legacy_recover_repo, REPO_ID)
        legacy_recovered = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (legacy_recover_repo, REPO_ID, "D-LEGACY-ID-RECOVERY"),
        ))
        recovered_id = account_one(
            "SELECT repository_id FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s",
            (ACCOUNT, legacy_recover_repo),
        )
        check("current lifecycle authority resets combined noncanonical identity residue",
              legacy_recovered.get("ok") is True
              and legacy_recovered.get("lifecycle_reset") is True
              and recovered_id == REPO_ID
              and count("graph_version", legacy_recover_repo) == 0
              and account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id<>'unknown'",
                  (ACCOUNT, legacy_recover_repo),
              ) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_recover_repo, REPO_ID)) is True)

        migration_up = os.path.join(ROOT, "db/migrations/001_repository_id_canonical.up.sql")
        migration_down = os.path.join(ROOT, "db/migrations/001_repository_id_canonical.down.sql")
        migration_verify = os.path.join(ROOT, "db/migrations/001_repository_id_canonical.verify.sql")
        migration_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"

        def run_migration(path: str):
            return subprocess.run(
                ["psql", migration_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-f", path],
                cwd=ROOT, capture_output=True, text=True,
            )

        refused_migration = run_migration(migration_up)
        refused_text = refused_migration.stdout + refused_migration.stderr
        force_rls_after_refusal = bool(admin(
            "SELECT bool_and(relforcerowsecurity) FROM pg_class "
            "WHERE oid IN ('core.graph_version'::regclass,'core.repository_lifecycle_activation'::regclass)"
        ))
        check("owner migration refuses residue without mutating rows or RLS",
              refused_migration.returncode != 0
              and "historical noncanonical rows remain" in refused_text
              and account_one(
                  "SELECT repo_id FROM core.graph_version WHERE account_id=%s AND repo=%s AND branch='main'",
                  (ACCOUNT, legacy_graph_repo),
              ) == "0" + REPO_ID
              and force_rls_after_refusal)

        # Test-only operator remediation after the refusal. Production requires an audited owner decision; the
        # migration itself deliberately never guesses whether 00123 meant 123 or was forged.
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "UPDATE core.graph_version SET repo_id=%s WHERE account_id=%s AND repo=%s RETURNING 1",
            (REPO_ID, ACCOUNT, legacy_graph_repo),
        )
        account_one(
            "UPDATE core.repository_lifecycle_tombstone SET repository_id=%s "
            "WHERE account_id=%s AND repo=%s RETURNING 1",
            (REPO_ID, ACCOUNT, legacy_marker_repo),
        )
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "DELETE FROM core.graph_version WHERE account_id=%s AND repo_id IS NOT NULL "
            "AND (length(repo_id)>32 OR repo_id !~ '^[1-9][0-9]*$'); "
            "DELETE FROM core.repository_lifecycle_activation WHERE account_id=%s "
            "AND (length(repository_id)>32 OR repository_id !~ '^[1-9][0-9]*$'); SELECT 1",
            (ACCOUNT, ACCOUNT),
        )
        promoted = run_migration(migration_up)
        verified = run_migration(migration_verify)
        rolled_back = run_migration(migration_down)
        old_constraints = str(admin(
            "SELECT string_agg(pg_get_constraintdef(oid),' ') FROM pg_constraint "
            "WHERE conname IN ('graph_version_repo_id_shape','repository_tombstone_id_shape',"
            "'repository_activation_id_shape')"
        ))
        promoted_again = run_migration(migration_up)
        verified_again = run_migration(migration_verify)
        canonical_constraints = str(admin(
            "SELECT string_agg(pg_get_constraintdef(oid),' ') FROM pg_constraint "
            "WHERE conname IN ('graph_version_repo_id_shape','repository_tombstone_id_shape',"
            "'repository_activation_id_shape')"
        ))
        check("explicit repository-id migration has reversible up/down and post-verify",
              promoted.returncode == 0 and verified.returncode == 0 and rolled_back.returncode == 0
              and old_constraints.count("^[0-9]+$") == 3
              and promoted_again.returncode == 0 and verified_again.returncode == 0
              and canonical_constraints.count("^[1-9][0-9]*$") == 3)

        # A DB exception must reach the durable worker so the delivery is released/retried.
        def db_failure(_sql, _args):
            raise RuntimeError("synthetic offboard failure")

        propagated = False
        try:
            ingest.purge_repo(db_failure, LIVE_REPO, REPO_ID, "repository_deleted", "D-SYNTHETIC-FAIL")
        except RuntimeError as exc:
            propagated = "synthetic offboard failure" in str(exc)
        check("repository purge propagates DB failure for durable retry", propagated)

        no_authority_called = False
        no_authority_rejected = False

        def should_not_purge(_sql, _args):
            nonlocal no_authority_called
            no_authority_called = True
            return {"ok": True}

        try:
            ingest.purge_repo(should_not_purge, LIVE_REPO, REPO_ID, "repository_deleted")
        except RuntimeError as exc:
            no_authority_rejected = "needs durable delivery authority" in str(exc)
        check("repository purge without durable authority fails before DB mutation",
              no_authority_rejected and not no_authority_called)

        # The durable wrapper must thread its content-free key through both lifecycle handlers. The DB uses this
        # key to resolve received_at; handlers must never invent or accept a payload timestamp.
        captured_calls = []

        def db_capture(sql, args):
            captured_calls.append((sql, args))
            return {"ok": True, "targets": []}

        class RouteIdentityGH:
            def repo_current_identity(self, repo):
                return {"full_name": repo, "id": REPO_ID}

        route_gh = RouteIdentityGH()

        webhook_handlers._handle_repository_event("repository", {
            "action": "deleted",
            "repository": {"full_name": LIVE_REPO},
            "_veripsa_delivery_key": "D-ROUTE-REPOSITORY",
        }, db_capture, route_gh)
        webhook_handlers._handle_installation_event("installation_repositories", {
            "action": "removed",
            "repositories_removed": [{"full_name": LIVE_REPO}],
            "_veripsa_delivery_key": "D-ROUTE-INSTALLATION",
        }, db_capture, route_gh)
        webhook_handlers._handle_repository_event("repository", {
            "action": "created",
            "repository": {"id": REPO_ID, "full_name": LIVE_REPO},
            "_veripsa_delivery_key": "D-ROUTE-REACTIVATE",
        }, db_capture, None)
        check("repository lifecycle handlers pass durable ordering evidence and legacy identity authority",
              len(captured_calls) == 5
              and all(sql.count("%s") == len(args) for sql, args in captured_calls)
              and "prepare_legacy_repository_offboard_with_authority" in captured_calls[0][0]
              and "resolve_legacy_repository_offboard_with_authority" in captured_calls[1][0]
              and "prepare_legacy_repository_offboard_with_authority" in captured_calls[2][0]
              and "resolve_legacy_repository_offboard_with_authority" in captured_calls[3][0]
              and captured_calls[0][1][-1] == "D-ROUTE-REPOSITORY"
              and captured_calls[1][1][-1] == "D-ROUTE-REPOSITORY"
              and captured_calls[2][1][-1] == "D-ROUTE-INSTALLATION"
              and captured_calls[3][1][-1] == "D-ROUTE-INSTALLATION"
              and captured_calls[4][1][-1] == "D-ROUTE-REACTIVATE")

        seed()
        before_repos = as_json(admin("SELECT core.repos_for_installation(%s)", (INSTALLATION,)))
        stuck_before = as_json(steward("SELECT core.stuck_prs_surface()"))
        check("repo is visible before offboarding",
              LIVE_REPO in {row.get("repo") for row in before_repos}
              and LIVE_REPO in {row.get("repo") for row in stuck_before.get("items", [])})

        result = offboard_current(OLD_REPO, REPO_ID, "repository_deleted")
        targets = set(result.get("targets", []))
        check("old-name delete resolves the current coordinate by stable id",
              result.get("ok") is True and LIVE_REPO in targets)

        working_tables = (
            "code_node", "code_edge", "graph_version", "claim",
            "co_change", "co_change_seen_commit",
        )
        check("all repo working-set tables are purged",
              all(count(table, LIVE_REPO) == 0 for table in working_tables))
        check("live workspace consent is purged",
              count("workspace_member", LIVE_REPO) == 0)
        check("outbound repo grant is purged",
              int(account_one("SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s",
                              (ACCOUNT, LIVE_REPO))) == 0)
        check("GitHub store attachment is purged",
              int(account_one("SELECT count(*)::int FROM core.store_connection WHERE account_id=%s "
                              "AND provider='github' AND target=%s", (ACCOUNT, LIVE_REPO))) == 0)
        check("settled repo delivery is purged while processing delivery survives",
              int(admin("SELECT count(*)::int FROM core.webhook_delivery WHERE delivery_key='D-REPO-DONE'")) == 0
              and int(admin("SELECT count(*)::int FROM core.webhook_delivery "
                            "WHERE delivery_key='D-REPO-PROCESSING' AND status='processing'")) == 1)
        check("other repository remains intact",
              count("code_node", KEEP_REPO) == 1
              and int(admin("SELECT count(*)::int FROM core.webhook_delivery "
                            "WHERE delivery_key='D-KEEP-DONE'")) == 1)

        # The old object can be deleted while GitHub has already accepted a same-name replacement event. Purge only
        # inbox rows positively tied to the old stable id (plus settled id-less history); never lose the 202'd
        # different-id or ambiguous legacy work GitHub will not resend.
        queue_safe_offboard = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (QUEUED_REPLACEMENT_REPO, QUEUED_OLD_ID,
             "repository_deleted", "D-QUEUE-DELETE-OLD"),
        ))
        queue_rows = as_json(admin(
            "SELECT COALESCE(jsonb_object_agg(delivery_key,status),'{}'::jsonb) "
            "FROM core.webhook_delivery WHERE delivery_key LIKE 'D-QUEUE-%%'"
        ))
        check("offboard purges old-id queue rows without deleting a queued replacement",
              queue_safe_offboard.get("selective_webhook_deliveries_purged") == 2
              and "D-QUEUE-WORK-OLD" not in queue_rows
              and "D-QUEUE-DONE-OLD" not in queue_rows
              and queue_rows.get("D-QUEUE-WORK-NEW") == "queued")
        check("ambiguous queued legacy work is preserved for tombstone-guarded processing",
              queue_rows.get("D-QUEUE-WORK-LEGACY") == "queued"
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (QUEUED_REPLACEMENT_REPO, None)) is False)
        check("processing deletion and another tenant's queue row are never purged",
              queue_rows.get("D-QUEUE-DELETE-OLD") == "processing"
              and queue_rows.get("D-QUEUE-FOREIGN") == "queued")
        check("different-id queued replacement remains eligible to establish current ownership",
              app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (QUEUED_REPLACEMENT_REPO, QUEUED_NEW_ID)) is True
              and count("code_node", QUEUED_REPLACEMENT_REPO) == 0)

        graphless_renamed = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (GRAPHLESS_RENAMED_OLD_REPO, GRAPHLESS_RENAMED_ID,
             "repository_deleted", "D-GRAPHLESS-DELETE"),
        ))
        graphless_state = {
            "targets": sorted(graphless_renamed.get("targets", [])),
            "claims": count("claim", GRAPHLESS_RENAMED_REPO),
            "workspace_members": count("workspace_member", GRAPHLESS_RENAMED_REPO),
            "grants": int(account_one(
                "SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s",
                (ACCOUNT, GRAPHLESS_RENAMED_REPO),
            )),
            "stores": int(account_one(
                "SELECT count(*)::int FROM core.store_connection WHERE account_id=%s "
                "AND provider='github' AND target=%s",
                (ACCOUNT, GRAPHLESS_RENAMED_REPO),
            )),
            "activations": int(account_one(
                "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                "WHERE account_id=%s AND repository_id=%s",
                (ACCOUNT, GRAPHLESS_RENAMED_ID),
            )),
        }
        check(f"stable id resolves and purges a renamed graphless repository ({graphless_state})",
              graphless_state == {
                  "targets": sorted([GRAPHLESS_RENAMED_OLD_REPO, GRAPHLESS_RENAMED_REPO]), "claims": 0,
                  "workspace_members": 0, "grants": 0, "stores": 0, "activations": 0,
              })

        tombstones = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s", (ACCOUNT, REPO_ID),
        ))
        blocked = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (LIVE_REPO, REPO_ID),
        )
        check("minimal stable-id tombstone is recorded", tombstones >= 1)
        check("stale event for removed repository is blocked", blocked is False)
        check("malformed non-empty ids cannot bypass a canonical repository tombstone",
              all(app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LIVE_REPO, value)) is False for value in malformed_repository_ids))

        after_repos = as_json(admin("SELECT core.repos_for_installation(%s)", (INSTALLATION,)))
        repo_insight = admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, LIVE_REPO),
        )
        file_insight = admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, LIVE_REPO, "src/live.py"),
        )
        stuck_after = as_json(steward("SELECT core.stuck_prs_surface()"))
        check("repo-level platform readers fail closed while tombstoned",
              LIVE_REPO not in {row.get("repo") for row in after_repos}
              and repo_insight is None and file_insight is None
              and LIVE_REPO not in {row.get("repo") for row in stuck_after.get("items", [])})

        cochange_resurrection_denied = 0
        for sql, args in (
            ("SELECT core.co_change_filter_unseen_commits_with_authority(%s,%s)",
             (LIVE_REPO, ["f" * 40])),
            ("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)",
             (json.dumps([{
                 "a": "src/a.py", "b": "src/b.py", "co": 5, "n_a": 5,
                 "n_b": 6, "strength": 0.8, "lift": 2.1, "n_total": 12,
             }]), LIVE_REPO)),
        ):
            try:
                app(sql, args)
            except psycopg2.Error as exc:
                cochange_resurrection_denied += int(
                    exc.pgcode == "42501" and "repository" in str(exc)
                    and "tombstoned" in str(exc))
        check("repository removal blocks asynchronous co-change resurrection",
              cochange_resurrection_denied == 2
              and count("co_change", LIVE_REPO) == 0
              and count("co_change_seen_commit", LIVE_REPO) == 0)

        # Durable lifecycle order and processing order use different clocks. Receive the replacement add first,
        # then let predecessor work commit before that add is processed. A generation boundary based on received_at
        # would expose these old-object events as replacement history; the processing-time boundary must exclude them.
        replacement_add_delivery = seed_add_delivery(
            "D-REUSED-ADD-CURRENT", REUSED_REPO, REUSED_ID)
        reused_old_pr_seeded = account_one(
            "SELECT core.mark_governed_write('event'); "
            "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail) VALUES "
            "('EV-REUSED-OLD-FAIL',%s,'pr_failing','AG-APP',%s,'main','PR-1','old generation failed'),"
            "('EV-REUSED-OLD-LANDED',%s,'landed','AG-APP',%s,'main','PR-1','old generation landed') "
            "RETURNING 1",
            (ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO),
        )

        # A late delete for the old object must not touch a same-name replacement with another stable id.
        refreshed_store = app(
            "SELECT core.connect_store_with_authority(%s,%s,%s,%s)",
            ("CN-REUSED-NEW", "github", REUSED_REPO, ""),
        )
        check("replacement store re-attach refreshes its authority timestamp",
              refreshed_store == "CN-REUSED-NEW")
        stale = offboard_current(REUSED_REPO, REPO_ID, "repository_deleted")
        replacement_allowed = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (REUSED_REPO, REUSED_ID),
        )
        old_marker_after_replacement = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (ACCOUNT, REPO_ID, REUSED_REPO),
        ))
        old_event_after_replacement = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (REUSED_REPO, REPO_ID),
        )
        class LifecycleGH:
            def installation_account_id(self):
                return INSTALLATION

        stale_cochange_before = (
            count("co_change", REUSED_REPO),
            count("co_change_seen_commit", REUSED_REPO),
        )
        old_dsn = os.environ.get("VERIPSA_DSN")
        os.environ["VERIPSA_DSN"] = f"postgresql://veripsa_app@localhost/{DB}"
        try:
            stale_cochange_task = cochange._cochange_increment_task(
                LifecycleGH(), REUSED_REPO, "main",
                [("b" * 40, {"src/stale.py", "src/stale-pair.py"})],
                repository_id=REPO_ID,
            )
        finally:
            if old_dsn is None:
                os.environ.pop("VERIPSA_DSN", None)
            else:
                os.environ["VERIPSA_DSN"] = old_dsn
        stale_cochange_after = (
            count("co_change", REUSED_REPO),
            count("co_change_seen_commit", REUSED_REPO),
        )
        replacement_read_before_readd = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, REUSED_REPO),
        ))
        replacement_old_file_before_readd = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, REUSED_REPO, "src/old.py"),
        ))
        check("stale delete cannot purge a same-name replacement",
              stale.get("targets") == [] and count("code_node", REUSED_REPO) == 1)
        stale_authority = stale.get("stale_authority_purged", {})
        replacement_authority = account_one(
            "SELECT jsonb_build_object("
            "'old_ws',(SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s AND workspace_id='WS-REUSED-OLD'),"
            "'new_ws',(SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s AND workspace_id='WS-REUSED-NEW'),"
            "'old_grant',(SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s AND grant_id='GR-REUSED-OLD'),"
            "'new_grant',(SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s AND grant_id='GR-REUSED-NEW'),"
            "'old_store',(SELECT count(*)::int FROM core.store_connection WHERE account_id=%s AND target=%s AND connection_id='CN-REUSED-OLD'),"
            "'new_store',(SELECT count(*)::int FROM core.store_connection WHERE account_id=%s AND target=%s AND connection_id='CN-REUSED-NEW'))",
            (ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO,
             ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO,
             ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO),
        )
        replacement_authority = as_json(replacement_authority)
        check("stale delete drops only pre-replacement coordinate authority",
              stale_authority == {"workspace_members": 1, "grants": 1, "store_connections": 1}
              and replacement_authority == {
                  "old_ws": 0, "new_ws": 1,
                  "old_grant": 0, "new_grant": 1,
                  "old_store": 0, "new_store": 1,
              })
        check("same-name replacement with a different stable id is allowed",
              replacement_allowed is True)
        check("replacement work retains the old exact-id tombstone",
              old_marker_after_replacement == 1 and old_event_after_replacement is False
              and stale_cochange_task.get("skipped") == "repository generation no longer current"
              and stale_cochange_after == stale_cochange_before)
        check("late old-object delete does not hide an established replacement",
              replacement_read_before_readd.get("recent") == []
              and replacement_read_before_readd.get("hotspots") == []
              and replacement_read_before_readd.get("couplings") == []
              and replacement_old_file_before_readd.get("partners") == [])

        replacement_readd = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (REUSED_REPO, REUSED_ID, "D-REUSED-ADD-CURRENT"),
        ))
        old_event_after_readd = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (REUSED_REPO, REPO_ID),
        )
        old_marker_superseded = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s AND superseded_at IS NOT NULL",
            (ACCOUNT, REPO_ID, REUSED_REPO),
        ))
        check("explicit replacement re-add does not clear an old object's marker",
              replacement_add_delivery == 1 and replacement_readd.get("ok") is True
              and replacement_readd.get("cleared", 0) == 0
              and replacement_readd.get("superseded", 0) == 1
              and old_marker_superseded == 1 and old_event_after_readd is False)

        replacement_rebuilt = account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,%s,'main',repeat('6',40),1,0,%s,now()); "
            "SELECT core.mark_governed_write('code_node'); "
            "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) "
            "VALUES (%s,%s,'main','REUSED-REBUILT-N','file','src/rebuilt.py'); SELECT 1",
            (ACCOUNT, REUSED_REPO, REUSED_ID, ACCOUNT, REUSED_REPO),
        )
        check("current replacement is rebuilt after lifecycle reset",
              replacement_readd.get("lifecycle_reset") is True
              and replacement_rebuilt == 1 and count("code_node", REUSED_REPO) == 1)

        # Simulate rows written by an older schema before generation provenance existed. Additive production rollout
        # must hide this residue even if it survives into the replacement generation, then let fresh ingest replace it.
        legacy_generation_residue = account_one(
            "SELECT core.mark_governed_write('co_change'); "
            "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total) "
            "VALUES (%s,%s,'src/old-pair.py','src/old.py',7,9,8,0.8,3.0,50); "
            "SELECT core.mark_governed_write('co_change_seen_commit'); "
            "INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha) "
            "VALUES (%s,%s,repeat('e',40)); SELECT 1",
            (ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO),
        )

        replacement_repos_before_activity = as_json(admin(
            "SELECT core.repos_for_installation(%s)", (INSTALLATION,),
        ))
        replacement_insight_before_activity = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, REUSED_REPO),
        ))
        old_file_after_readd = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, REUSED_REPO, "src/old.py"),
        ))
        check("replacement does not inherit old-coordinate audit history",
              legacy_generation_residue == 1
              and REUSED_REPO not in {row.get("repo") for row in replacement_repos_before_activity}
              and replacement_insight_before_activity.get("recent") == []
              and replacement_insight_before_activity.get("hotspots") == []
              and replacement_insight_before_activity.get("couplings") == []
              and old_file_after_readd.get("total") == 0
              and old_file_after_readd.get("events") == []
              and old_file_after_readd.get("partners") == [])

        inserted = account_one(
            "SELECT core.mark_governed_write('event'); "
            "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail) "
            "VALUES ('EV-REUSED-NEW',%s,'warn_issued','AG-APP',%s,'main','src/new.py','PR-NEW') "
            "RETURNING 1",
            (ACCOUNT, REUSED_REPO),
        )
        refolded_sha = as_json(app(
            "SELECT core.co_change_filter_unseen_commits_with_authority(%s,%s)",
            (REUSED_REPO, ["e" * 40]),
        ))
        current_cochange = as_json(app(
            "SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)",
            (json.dumps([{
                "a": "src/new.py", "b": "src/current-pair.py", "co": 7,
                "n_a": 8, "n_b": 9, "strength": 0.8, "lift": 3.0, "n_total": 50,
            }]), REUSED_REPO),
        ))
        current_cochange_all = as_json(app(
            "SELECT core.co_change_all_with_authority(%s)", (REUSED_REPO,),
        ))
        current_cochange_partners = as_json(app(
            "SELECT core.co_change_partners_with_authority(%s,%s)",
            (REUSED_REPO, ["src/new.py"]),
        ))
        current_generation_stamps = as_json(account_one(
            "SELECT jsonb_build_object("
            "'pair',(SELECT bool_and(generation_observed_at IS NOT NULL) FROM core.co_change "
            "WHERE account_id=%s AND repo=%s),"
            "'seen',(SELECT bool_and(generation_observed_at IS NOT NULL) FROM core.co_change_seen_commit "
            "WHERE account_id=%s AND repo=%s))",
            (ACCOUNT, REUSED_REPO, ACCOUNT, REUSED_REPO),
        ))
        replacement_repos_after_activity = as_json(admin(
            "SELECT core.repos_for_installation(%s)", (INSTALLATION,),
        ))
        replacement_insight_after_activity = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, REUSED_REPO),
        ))
        new_file_after_readd = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, REUSED_REPO, "src/new.py"),
        ))
        check("replacement reads resume with only post-activation activity",
              inserted == 1
              and refolded_sha == ["e" * 40]
              and current_cochange.get("pairs") == 1
              and current_generation_stamps == {"pair": True, "seen": True}
              and len(current_cochange_all) == 1
              and {current_cochange_all[0].get("a"), current_cochange_all[0].get("b")}
                  == {"src/new.py", "src/current-pair.py"}
              and any(row.get("partner") == "src/current-pair.py"
                      for row in current_cochange_partners)
              and REUSED_REPO in {row.get("repo") for row in replacement_repos_after_activity}
              and [row.get("path") for row in replacement_insight_after_activity.get("recent", [])]
                  == ["src/new.py"]
              and len(replacement_insight_after_activity.get("couplings", [])) == 1
              and new_file_after_readd.get("total") == 1
              and len(new_file_after_readd.get("events", [])) == 1
              and any(row.get("partner") == "src/current-pair.py"
                      for row in new_file_after_readd.get("partners", [])))

        # A recreated same-name repository restarts PR numbering. Retained predecessor audit may therefore
        # contain a landed PR-1 with the same coordinate/ref as the replacement's new failing PR-1. The reader
        # boundary must apply to the correlated landed row as well as the failing row.
        replacement_received_boundary = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REUSED_REPO, REUSED_ID),
        )
        replacement_processed_boundary = account_one(
            "SELECT generation_started_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REUSED_REPO, REUSED_ID),
        )
        reused_old_pr_latest = account_one(
            "SELECT max(occurred_at) FROM core.event "
            "WHERE account_id=%s AND repo=%s AND event_id IN "
            "('EV-REUSED-OLD-FAIL','EV-REUSED-OLD-LANDED')",
            (ACCOUNT, REUSED_REPO),
        )
        reused_pr_number_seeded = account_one(
            "SELECT core.mark_governed_write('event'); "
            "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail) VALUES "
            "('EV-REUSED-NEW-FAIL',%s,'pr_failing','AG-APP',%s,'main','PR-1','new generation failed') "
            "RETURNING 1",
            (ACCOUNT, REUSED_REPO),
        )
        replacement_stuck = as_json(app("SELECT core.stuck_prs_surface()"))
        replacement_stuck_items = [
            row for row in replacement_stuck.get("items", [])
            if row.get("repo") == REUSED_REPO and row.get("pr") == "PR-1"
        ]
        check("old-generation landed PR does not hide a replacement's reused PR number",
              reused_old_pr_seeded == 1
              and replacement_received_boundary < reused_old_pr_latest
              and reused_old_pr_latest < replacement_processed_boundary
              and reused_pr_number_seeded == 1
              and len(replacement_stuck_items) == 1
              and replacement_stuck_items[0].get("reason") == "new generation failed")

        # A minimized pre-fix delete may have no repository id. Its authenticated durable receipt predates the
        # replacement boundary, so it belongs to the predecessor and must preserve the replacement.
        name_only_late_delete = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (REUSED_REPO, None, "repository_deleted", "D-REUSED-STALE-NAME-DELETE"),
        ))
        name_only_boundary = account_one(
            "SELECT superseded_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id='unknown'",
            (ACCOUNT, REUSED_REPO),
        )
        check("name-only stale delete preserves a known same-name replacement",
              name_only_late_delete.get("targets") == []
              and name_only_late_delete.get("delivery_order") == "stale_before_replacement"
              and count("code_node", REUSED_REPO) == 1
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s",
                  (ACCOUNT, REUSED_REPO, REUSED_ID),
              )) == 1
              and name_only_boundary is not None
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (REUSED_REPO, REUSED_ID)) is True)

        # The replacement object can itself be deselected and reselected later with the SAME stable id. Its
        # retained audit belongs to the same GitHub object and must remain visible; only the older different-id
        # predecessor stays behind the original generation boundary.
        same_id_generation_before = account_one(
            "SELECT generation_started_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REUSED_REPO, REUSED_ID),
        )
        same_id_remove = offboard_current(
            REUSED_REPO, REUSED_ID, "installation_removed")
        same_id_tombstone_generation = account_one(
            "SELECT generation_started_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s AND superseded_at IS NULL",
            (ACCOUNT, REUSED_REPO, REUSED_ID),
        )
        same_id_add_key = "D-REUSED-SAME-ID-READD"
        seed_add_delivery(same_id_add_key, REUSED_REPO, REUSED_ID)
        same_id_readd = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (REUSED_REPO, REUSED_ID, same_id_add_key),
        ))
        same_id_generation_after = account_one(
            "SELECT generation_started_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REUSED_REPO, REUSED_ID),
        )
        same_id_rebuilt = seed_graph(REUSED_REPO, REUSED_ID, "REUSED-SAME-ID-REBUILT-N")
        same_id_insights = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, REUSED_REPO),
        ))
        same_id_old_file = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, REUSED_REPO, "src/old.py"),
        ))
        check("same-id reselect preserves current-object history and its original generation boundary",
              same_id_generation_before is not None
              and same_id_remove.get("targets") == [REUSED_REPO]
              and same_id_tombstone_generation == same_id_generation_before
              and same_id_readd.get("activated") is True
              and same_id_readd.get("lifecycle_reset") is True
              and same_id_generation_after == same_id_generation_before
              and same_id_rebuilt == 1
              and any(row.get("path") == "src/new.py"
                      for row in same_id_insights.get("recent", []))
              and all(row.get("path") != "src/old.py"
                      for row in same_id_insights.get("recent", []))
              and same_id_old_file.get("total") == 0)

        # The opposite side is equally important: a real current removal received after the known graph boundary
        # must purge even though its legacy payload has no repository id. Stable-id presence alone cannot skip it.
        name_only_current_delete = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (NAME_ONLY_LIVE_REPO, None, "installation_removed", "D-NAME-ONLY-CURRENT-REMOVE"),
        ))
        name_only_current_marker = account_one(
            "SELECT superseded_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id='unknown'",
            (ACCOUNT, NAME_ONLY_LIVE_REPO),
        )
        check("name-only current removal purges the live repository working set",
              name_only_current_delete.get("targets") == [NAME_ONLY_LIVE_REPO]
              and name_only_current_delete.get("delivery_order") == "current_or_later"
              and count("code_node", NAME_ONLY_LIVE_REPO) == 0
              and count("graph_version", NAME_ONLY_LIVE_REPO) == 0
              and name_only_current_marker is None
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (NAME_ONLY_LIVE_REPO, NAME_ONLY_LIVE_ID)) is False)

        invalid_delivery_rejected = False
        try:
            app("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                (REUSED_REPO, None, "repository_deleted", "D-KEEP-DONE"))
        except psycopg2.Error as exc:
            invalid_delivery_rejected = "not authoritative" in str(exc)
        check("foreign or settled delivery keys cannot supply deletion ordering",
              invalid_delivery_rejected and count("code_node", REUSED_REPO) == 1)

        # Processing order is not lifecycle order. A newer remove may finish before an older add that was already
        # queued. The add must compare durable receipt time and leave the newer tombstone/read block intact.
        ordered_remove = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (ORDERED_REPO, ORDERED_ID, "installation_removed", "D-ORDER-REMOVE-NEW"),
        ))
        ordered_remove_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key='D-ORDER-REMOVE-NEW'"
        )
        ordered_marker_received_at = account_one(
            "SELECT lifecycle_received_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, ORDERED_REPO, ORDERED_ID),
        )
        check("newer remove records its durable lifecycle boundary and purges",
              ordered_remove.get("targets") == [ORDERED_REPO]
              and ordered_remove.get("stale_lifecycle_event") is False
              and ordered_marker_received_at == ordered_remove_received_at
              and count("code_node", ORDERED_REPO) == 0)

        onboard_attempts = []
        original_onboard = webhook_handlers._queue_onboard_repos
        original_signal = webhook_handlers._post_watching_signal
        original_reactivate_account = webhook_handlers._reactivate_account
        try:
            def capture_onboard(_db, _gh, repos):
                onboard_attempts.extend(repos)
                return [], 0

            webhook_handlers._queue_onboard_repos = capture_onboard
            webhook_handlers._post_watching_signal = lambda _gh, _repos: 0
            # Account-generation admission is covered by the dedicated lifecycle suite.  This focused repository
            # ordering check still executes every DB call under the production installation-account pin.
            webhook_handlers._reactivate_account = lambda *_args, **_kwargs: True
            stale_handler_result = webhook_handlers._handle_installation_event(
                "installation_repositories",
                {
                    "action": "added",
                    "repositories_added": [{"id": ORDERED_OLD_ID, "full_name": ORDERED_REPO}],
                    "_veripsa_delivery_key": "D-ORDER-DIFFERENT-ADD-OLD",
                },
                lambda sql, args=(): scoped_one("veripsa_app", ACCOUNT, sql, args),
                None,
            )
        finally:
            webhook_handlers._queue_onboard_repos = original_onboard
            webhook_handlers._post_watching_signal = original_signal
            webhook_handlers._reactivate_account = original_reactivate_account
        check("stale different-id add skips cold graph and PR onboarding",
              stale_handler_result.get("stale_repositories_skipped") == 1
              and onboard_attempts == []
              and count("code_node", ORDERED_REPO) == 0)

        account_event_repo_reactivation_rejected = False
        try:
            app("SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
                (ORDERED_REPO, ORDERED_ID, "D-INSTALL-CREATED"))
        except psycopg2.Error as exc:
            account_event_repo_reactivation_rejected = "not authoritative" in str(exc)
        check("account-level installation event cannot clear a repository tombstone",
              account_event_repo_reactivation_rejected
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s AND superseded_at IS NULL",
                  (ACCOUNT, ORDERED_REPO, ORDERED_ID),
              )) == 1)

        account_onboard_attempts = []
        original_queue_backfill = ingest._queue_backfill_repo
        original_signal = webhook_handlers._post_watching_signal
        original_reactivate_account = webhook_handlers._reactivate_account
        try:
            ingest._queue_backfill_repo = lambda _db, _gh, repo, repository_id=None: (
                account_onboard_attempts.append(repo) or {
                    "backfilled": repo,
                    "graph": {"queued": True, "indexing": True},
                }
            )
            webhook_handlers._post_watching_signal = lambda _gh, _repos: 0
            webhook_handlers._reactivate_account = lambda *_args, **_kwargs: True
            account_onboard_result = webhook_handlers._handle_installation_event(
                "installation",
                {
                    "action": "created",
                    "repositories": [
                        {"id": ORDERED_ID, "full_name": ORDERED_REPO},
                        {"id": "4999", "full_name": KEEP_REPO},
                    ],
                    "_veripsa_delivery_key": "D-INSTALL-CREATED",
                },
                lambda sql, args=(): scoped_one("veripsa_app", ACCOUNT, sql, args),
                object(),
            )

            class AllRepos:
                @staticmethod
                def installation_repos(cap):
                    return [ORDERED_REPO]

            allrepos_onboard_result = webhook_handlers._handle_installation_event(
                "installation",
                {"action": "unsuspend", "repositories": []},
                lambda sql, args=(): scoped_one("veripsa_app", ACCOUNT, sql, args),
                AllRepos(),
            )
        finally:
            ingest._queue_backfill_repo = original_queue_backfill
            webhook_handlers._post_watching_signal = original_signal
            webhook_handlers._reactivate_account = original_reactivate_account
        check("account-level onboarding skips a removed repo but keeps a current repo",
              account_onboard_result.get("stale_repositories_skipped") == 1
              and account_onboard_attempts == [KEEP_REPO])
        check("All-repositories fallback applies the same repo lifecycle gate",
              allrepos_onboard_result.get("stale_repositories_skipped") == 1
              and allrepos_onboard_result.get("onboarded") == [])
        strict_renamed_old = app(
            "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
            (STRICT_RENAMED_OLD_REPO, STRICT_RENAMED_ID),
        )
        strict_renamed_current = app(
            "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
            (STRICT_RENAMED_REPO, STRICT_RENAMED_ID),
        )
        strict_name_only_current = app(
            "SELECT core.repository_account_onboarding_allowed_with_authority(%s,%s)",
            (STRICT_RENAMED_REPO, None),
        )
        check("account onboarding cannot use rename reconciliation authority",
              strict_renamed_old is False
              and strict_renamed_current is True
              and strict_name_only_current is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (STRICT_RENAMED_OLD_REPO, STRICT_RENAMED_ID)) is True)

        ordered_stale_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (ORDERED_REPO, ORDERED_ID, "D-ORDER-ADD-OLD"),
        ))
        check("stale add cannot clear a newer remove tombstone",
              ordered_stale_add.get("stale_lifecycle_event") is True
              and ordered_stale_add.get("activated") is False
              and ordered_stale_add.get("cleared") == 0
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s AND superseded_at IS NULL",
                  (ACCOUNT, ORDERED_REPO, ORDERED_ID),
              )) == 1
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s",
                  (ACCOUNT, ORDERED_REPO, ORDERED_ID),
              )) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (ORDERED_REPO, ORDERED_ID)) is False)

        ordered_current_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (ORDERED_REPO, ORDERED_ID, "D-ORDER-ADD-NEW"),
        ))
        ordered_add_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key='D-ORDER-ADD-NEW'"
        )
        ordered_activated_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, ORDERED_REPO, ORDERED_ID),
        )
        check("newer re-add clears the older remove and reopens the same repository",
              ordered_current_add.get("stale_lifecycle_event") is False
              and ordered_current_add.get("activated") is True
              and ordered_current_add.get("cleared", 0) >= 1
              and ordered_activated_at == ordered_add_received_at
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s",
                  (ACCOUNT, ORDERED_REPO, ORDERED_ID),
              )) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (ORDERED_REPO, ORDERED_ID)) is True)

        # The reverse processing order is equally dangerous: a delayed older remove must not purge graph state or
        # write an exact-id tombstone after a newer re-add of that same GitHub repository object.
        reverse_current_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID, "D-REVERSE-ADD-NEW"),
        ))
        reverse_activated_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID),
        )
        reverse_lifecycle_authoritative = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID),
        )
        reverse_rebuilt = account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,%s,'main',repeat('9',40),1,0,%s,now()); "
            "SELECT core.mark_governed_write('code_node'); "
            "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) "
            "VALUES (%s,%s,'main','REVERSE-REBUILT-N','file','src/rebuilt.py'); SELECT 1",
            (ACCOUNT, REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID,
             ACCOUNT, REVERSE_ORDERED_REPO),
        )
        reverse_authority_seeded = account_one(
            "SELECT core.mark_governed_write('workspace'); "
            "INSERT INTO core.workspace(workspace_id,created_by_account,state) VALUES "
            "('WS-REVERSE-OLD',%s,'active'),('WS-REVERSE-NEW',%s,'active'); "
            "SELECT core.mark_governed_write('workspace_member'); "
            "INSERT INTO core.workspace_member(workspace_id,account_id,repo,branch,consent_state,consented_at,joined_at) "
            "VALUES ('WS-REVERSE-OLD',%s,%s,'main','accepted',%s-interval '1 minute',%s-interval '1 minute'),"
            "('WS-REVERSE-NEW',%s,%s,'main','accepted',%s+interval '1 minute',%s+interval '1 minute'); "
            "INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,repo,scope,granted_at) VALUES "
            "('GR-REVERSE-OLD',%s,'AG-APP',%s,ARRAY['read'],%s-interval '1 minute'),"
            "('GR-REVERSE-NEW',%s,'AG-APP',%s,ARRAY['read'],%s+interval '1 minute'); "
            "SELECT core.mark_governed_write('store_connection'); "
            "INSERT INTO core.store_connection(connection_id,account_id,provider,target,connected_at) VALUES "
            "('CN-REVERSE-OLD',%s,'github',%s,%s-interval '1 minute'),"
            "('CN-REVERSE-NEW',%s,'github',%s,%s+interval '1 minute'); SELECT 1",
            (ACCOUNT, ACCOUNT,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at, reverse_activated_at,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at, reverse_activated_at,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at,
             ACCOUNT, REVERSE_ORDERED_REPO, reverse_activated_at),
        )
        reverse_stale_remove = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID,
             "installation_removed", "D-REVERSE-REMOVE-OLD"),
        ))
        reverse_authority = as_json(account_one(
            "SELECT jsonb_build_object("
            "'old_ws',(SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s "
            "AND workspace_id='WS-REVERSE-OLD'),"
            "'new_ws',(SELECT count(*)::int FROM core.workspace_member WHERE account_id=%s AND repo=%s "
            "AND workspace_id='WS-REVERSE-NEW'),"
            "'old_grant',(SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s "
            "AND grant_id='GR-REVERSE-OLD'),"
            "'new_grant',(SELECT count(*)::int FROM core.grant WHERE grantor_account=%s AND repo=%s "
            "AND grant_id='GR-REVERSE-NEW'),"
            "'old_store',(SELECT count(*)::int FROM core.store_connection WHERE account_id=%s AND target=%s "
            "AND connection_id='CN-REVERSE-OLD'),"
            "'new_store',(SELECT count(*)::int FROM core.store_connection WHERE account_id=%s AND target=%s "
            "AND connection_id='CN-REVERSE-NEW'))",
            (ACCOUNT, REVERSE_ORDERED_REPO, ACCOUNT, REVERSE_ORDERED_REPO,
             ACCOUNT, REVERSE_ORDERED_REPO, ACCOUNT, REVERSE_ORDERED_REPO,
             ACCOUNT, REVERSE_ORDERED_REPO, ACCOUNT, REVERSE_ORDERED_REPO),
        ))
        check("stale remove cannot purge a newer same-id reactivation",
              reverse_current_add.get("stale_lifecycle_event") is False
              and reverse_current_add.get("lifecycle_reset") is True
              and reverse_activated_at is not None
              and reverse_lifecycle_authoritative is True
              and reverse_rebuilt == 1 and reverse_authority_seeded == 1
              and reverse_stale_remove.get("stale_lifecycle_event") is True
              and reverse_stale_remove.get("revoked") is False
              and reverse_stale_remove.get("delivery_order") == "stale_before_reactivation"
              and reverse_stale_remove.get("targets") == []
              and count("code_node", REVERSE_ORDERED_REPO) == 1
              and count("graph_version", REVERSE_ORDERED_REPO) == 1
              and reverse_stale_remove.get("stale_authority_purged") == {
                  "workspace_members": 1, "grants": 1, "store_connections": 1,
              }
              and reverse_authority == {
                  "old_ws": 0, "new_ws": 1,
                  "old_grant": 0, "new_grant": 1,
                  "old_store": 0, "new_store": 1,
              }
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s",
                  (ACCOUNT, REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID),
              )) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID)) is True)

        # A stable-id rename must carry authenticated lifecycle provenance to the new coordinate. Otherwise an
        # old removal received before a later add can purge the current object merely because ordinary work detected
        # its rename before the removal processed.
        rename_order_old = "acme/lifecycle-rename-old"
        rename_order_new = "acme/lifecycle-rename-new"
        rename_order_id = "39002"
        rename_remove_key = "D-LIFECYCLE-RENAME-REMOVE-OLD"
        rename_add_key = "D-LIFECYCLE-RENAME-ADD-NEWER"
        seed_remove_delivery(rename_remove_key, rename_order_old, rename_order_id)
        age_delivery(rename_remove_key, "20 minutes")
        seed_add_delivery(rename_add_key, rename_order_old, rename_order_id)
        age_delivery(rename_add_key, "10 minutes")
        rename_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (rename_order_old, rename_order_id, rename_add_key),
        ))
        rename_add_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key=%s",
            (rename_add_key,),
        )
        seed_graph(rename_order_new, rename_order_id, "LIFECYCLE-RENAME-N")
        rename_reconcile = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (rename_order_new, rename_order_id),
        ))
        rename_activation_repo = account_one(
            "SELECT repo FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, rename_order_id),
        )
        rename_activation_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, rename_order_id),
        )
        rename_activation_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, rename_order_id),
        )
        rename_stale_remove = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (rename_order_old, rename_order_id, "installation_removed", rename_remove_key),
        ))
        check("work-detected stable-id rename preserves newer authenticated add authority",
              rename_add.get("stale_lifecycle_event") is False
              and rename_reconcile.get("activation_recorded") is True
              and rename_activation_repo == rename_order_new
              and rename_activation_at == rename_add_received_at
              and rename_activation_authority is True
              and rename_stale_remove.get("stale_lifecycle_event") is True
              and rename_stale_remove.get("revoked") is False
              and rename_stale_remove.get("targets") == []
              and count("graph_version", rename_order_new) == 1
              and count("code_node", rename_order_new) == 1
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repository_id=%s",
                  (ACCOUNT, rename_order_id),
              )) == 0)

        # Normal processing must still let a newer authenticated add move an older work-only activation to
        # the add payload's coordinate. The coordinate-preservation rule below is intentionally time-bounded.
        forward_rename_old = "acme/forward-processing-rename-old"
        forward_rename_new = "acme/forward-processing-rename-new"
        forward_rename_id = "39004"
        forward_rename_add_key = "D-FORWARD-PROCESSING-RENAME-ADD"
        seed_graph(forward_rename_old, forward_rename_id, "FORWARD-PROCESSING-RENAME-N")
        forward_rename_work = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (forward_rename_old, forward_rename_id),
        ))
        forward_rename_work_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, forward_rename_id),
        )
        forward_rename_work_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, forward_rename_id),
        )
        seed_add_delivery(forward_rename_add_key, forward_rename_new, forward_rename_id)
        forward_rename_add_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key=%s",
            (forward_rename_add_key,),
        )
        forward_rename_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (forward_rename_new, forward_rename_id, forward_rename_add_key),
        ))
        forward_rename_activation_repo = account_one(
            "SELECT repo FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, forward_rename_id),
        )
        forward_rename_activation_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, forward_rename_id),
        )
        forward_rename_activation_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, forward_rename_id),
        )
        check("newer authenticated add coordinate supersedes an older work-only coordinate",
              forward_rename_work.get("activation_recorded") is True
              and forward_rename_work_authority is False
              and forward_rename_work_at < forward_rename_add_received_at
              and forward_rename_add.get("stale_lifecycle_event") is False
              and forward_rename_activation_repo == forward_rename_new
              and forward_rename_activation_at == forward_rename_add_received_at
              and forward_rename_activation_authority is True)

        # Receive remove→add for the old name, but process a later work-observed rename first. Stable identity
        # makes that work row the same object, not contradictory lifecycle evidence: the add must promote it at t2
        # while retaining the current t3 coordinate, so the older t1 remove cannot purge the renamed graph.
        reverse_rename_old = "acme/reverse-processing-rename-old"
        reverse_rename_new = "acme/reverse-processing-rename-new"
        reverse_rename_id = "39003"
        reverse_rename_remove_key = "D-REVERSE-PROCESSING-RENAME-REMOVE"
        reverse_rename_add_key = "D-REVERSE-PROCESSING-RENAME-ADD"
        seed_remove_delivery(reverse_rename_remove_key, reverse_rename_old, reverse_rename_id)
        age_delivery(reverse_rename_remove_key, "20 minutes")
        seed_add_delivery(reverse_rename_add_key, reverse_rename_old, reverse_rename_id)
        age_delivery(reverse_rename_add_key, "10 minutes")
        reverse_rename_add_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key=%s",
            (reverse_rename_add_key,),
        )
        seed_graph(reverse_rename_new, reverse_rename_id, "REVERSE-PROCESSING-RENAME-N")
        reverse_rename_work = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (reverse_rename_new, reverse_rename_id),
        ))
        reverse_rename_work_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, reverse_rename_id),
        )
        reverse_rename_work_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, reverse_rename_id),
        )
        reverse_rename_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (reverse_rename_old, reverse_rename_id, reverse_rename_add_key),
        ))
        reverse_rename_activation_repo = account_one(
            "SELECT repo FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, reverse_rename_id),
        )
        reverse_rename_activation_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, reverse_rename_id),
        )
        reverse_rename_activation_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s",
            (ACCOUNT, reverse_rename_id),
        )
        reverse_rename_remove = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (reverse_rename_old, reverse_rename_id,
             "installation_removed", reverse_rename_remove_key),
        ))
        check("later work-observed same-id rename cannot make an older authenticated add stale",
              reverse_rename_work.get("activation_recorded") is True
              and reverse_rename_work_authority is False
              and reverse_rename_work_at > reverse_rename_add_received_at
              and reverse_rename_add.get("stale_lifecycle_event") is False
              and reverse_rename_activation_repo == reverse_rename_new
              and reverse_rename_activation_at == reverse_rename_add_received_at
              and reverse_rename_activation_authority is True
              and reverse_rename_remove.get("stale_lifecycle_event") is True
              and reverse_rename_remove.get("revoked") is False
              and reverse_rename_remove.get("targets") == []
              and count("graph_version", reverse_rename_new) == 1
              and count("code_node", reverse_rename_new) == 1
              and count("graph_version", reverse_rename_old) == 0
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repository_id=%s",
                  (ACCOUNT, reverse_rename_id),
              )) == 0)

        # Work/boot identity observation is useful for rejecting a different object, but it is not
        # repository-selection authority. In particular, processing work after a remove was durably received must
        # not make that remove stale. Also prove an older authenticated add is promoted at its own received_at,
        # rather than inheriting the later work-observation timestamp and outranking the remove.
        work_order_repo = "acme/work-observed-remove-order"
        work_order_id = "39001"
        work_add_key = "D-WORK-ORDER-ADD-OLD"
        work_remove_key = "D-WORK-ORDER-REMOVE-NEW"
        seed_add_delivery(work_add_key, work_order_repo, work_order_id)
        age_delivery(work_add_key, "20 minutes")
        seed_remove_delivery(work_remove_key, work_order_repo, work_order_id)
        age_delivery(work_remove_key, "10 minutes")
        seed_graph(work_order_repo, work_order_id, "WORK-ORDER-BEFORE-ADD-N")
        work_observed = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (work_order_repo, work_order_id),
        ))
        work_observed_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, work_order_repo, work_order_id),
        )
        promoted_old_add = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (work_order_repo, work_order_id, work_add_key),
        ))
        work_add_received_at = admin(
            "SELECT received_at FROM core.webhook_delivery WHERE delivery_key=%s",
            (work_add_key,),
        )
        promoted_activation_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, work_order_repo, work_order_id),
        )
        promoted_activation_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, work_order_repo, work_order_id),
        )
        # Rebuild after the add's intentional reselection reset, then run ordinary identity reconciliation again.
        # It must preserve both the lifecycle provenance and the add's older durable timestamp.
        seed_graph(work_order_repo, work_order_id, "WORK-ORDER-AFTER-ADD-N")
        work_after_add = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (work_order_repo, work_order_id),
        ))
        activation_after_work_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, work_order_repo, work_order_id),
        )
        activation_after_work_authority = account_one(
            "SELECT lifecycle_authoritative FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, work_order_repo, work_order_id),
        )
        work_order_remove = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (work_order_repo, work_order_id, "installation_removed", work_remove_key),
        ))
        check("work-observed activation cannot make a same-id removal stale",
              work_observed.get("activation_recorded") is True
              and work_observed_authority is False
              and promoted_old_add.get("stale_lifecycle_event") is False
              and promoted_activation_at == work_add_received_at
              and promoted_activation_authority is True
              and work_after_add.get("activation_recorded") is True
              and activation_after_work_at == promoted_activation_at
              and activation_after_work_authority is True
              and work_order_remove.get("stale_lifecycle_event") is False
              and work_order_remove.get("revoked") is True
              and work_order_remove.get("targets") == [work_order_repo]
              and count("code_node", work_order_repo) == 0
              and count("graph_version", work_order_repo) == 0
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                  "WHERE account_id=%s AND repo=%s",
                  (ACCOUNT, work_order_repo),
              )) == 0
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s AND superseded_at IS NULL",
                  (ACCOUNT, work_order_repo, work_order_id),
              )) == 1)

        mismatched_lifecycle_identity_rejected = 0
        for sql, args in (
            (
                "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
                (REVERSE_ORDERED_REPO, "99999", "D-REVERSE-ADD-NEW"),
            ),
            (
                "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
                (REVERSE_ORDERED_REPO, "99999", "installation_removed", "D-REVERSE-REMOVE-OLD"),
            ),
        ):
            try:
                app(sql, args)
            except psycopg2.Error as exc:
                mismatched_lifecycle_identity_rejected += int("not authoritative" in str(exc))
        check("durable lifecycle authority binds both repository name and stable id",
              mismatched_lifecycle_identity_rejected == 2
              and count("code_node", REVERSE_ORDERED_REPO) == 1
              and int(account_one(
                  "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                  "WHERE account_id=%s AND repo=%s AND repository_id=%s",
                  (ACCOUNT, REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID),
              )) == 0)

        invalid_activation_rejected = False
        try:
            app("SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
                (REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID, "D-KEEP-DONE"))
        except psycopg2.Error as exc:
            invalid_activation_rejected = "not authoritative" in str(exc)
        check("foreign or settled delivery keys cannot supply activation ordering",
              invalid_activation_rejected
              and count("code_node", REVERSE_ORDERED_REPO) == 1
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (REVERSE_ORDERED_REPO, REVERSE_ORDERED_ID)) is True)

        # The explicit replacement lifecycle event is authoritative before backfill creates graph_version. A late
        # delete for the old object must use that recorded boundary, preserve the new selection, and exclude old
        # retained history. This is the delivery order that graph-only replacement detection could not represent.
        pending_activation = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (PENDING_REPLACEMENT_REPO, PENDING_NEW_ID, "D-PENDING-ADD"),
        ))
        pending_activated_at = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, PENDING_REPLACEMENT_REPO, PENDING_NEW_ID),
        )
        pending_duplicate = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (PENDING_REPLACEMENT_REPO, PENDING_NEW_ID, "D-PENDING-ADD"),
        ))
        pending_activated_at_after_duplicate = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, PENDING_REPLACEMENT_REPO, PENDING_NEW_ID),
        )
        check("explicit replacement activation exists before graph ingest",
              pending_activation.get("activated") is True
              and pending_activated_at is not None
              and count("graph_version", PENDING_REPLACEMENT_REPO) == 0)
        check("duplicate replacement activation preserves the first boundary",
              pending_duplicate.get("activated") is True
              and pending_activated_at_after_duplicate == pending_activated_at)
        check("activation blocks the old id before its delayed delete arrives",
              app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (PENDING_REPLACEMENT_REPO, PENDING_OLD_ID)) is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (PENDING_REPLACEMENT_REPO, PENDING_NEW_ID)) is True)

        pending_late_delete = offboard_current(
            PENDING_REPLACEMENT_REPO, PENDING_OLD_ID, "repository_deleted")
        pending_activation_survives = int(account_one(
            "SELECT count(*)::int FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, PENDING_REPLACEMENT_REPO, PENDING_NEW_ID),
        ))
        pending_superseded_marker = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s AND superseded_at IS NOT NULL",
            (ACCOUNT, PENDING_REPLACEMENT_REPO, PENDING_OLD_ID),
        ))
        pending_insight_before_activity = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, PENDING_REPLACEMENT_REPO),
        ))
        pending_old_file = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, PENDING_REPLACEMENT_REPO, "src/old.py"),
        ))
        check("late old delete preserves a graphless replacement activation",
              pending_late_delete.get("targets") == []
              and pending_activation_survives == 1
              and pending_superseded_marker == 1)
        check("graphless replacement is visible without inheriting old audit history",
              pending_insight_before_activity.get("recent") == []
              and pending_insight_before_activity.get("hotspots") == []
              and pending_old_file.get("total") == 0)

        pending_inserted = account_one(
            "SELECT core.mark_governed_write('event'); "
            "INSERT INTO core.event(event_id,account_id,kind,agent_id,repo,branch,path,detail) "
            "VALUES ('EV-PENDING-NEW',%s,'warn_issued','AG-APP',%s,'main','src/new.py','PR-NEW') "
            "RETURNING 1",
            (ACCOUNT, PENDING_REPLACEMENT_REPO),
        )
        pending_repos_after_activity = as_json(admin(
            "SELECT core.repos_for_installation(%s)", (INSTALLATION,),
        ))
        check("graphless replacement reads resume after post-activation activity",
              pending_inserted == 1
              and PENDING_REPLACEMENT_REPO in {
                  row.get("repo") for row in pending_repos_after_activity
              })

        # A repository ingested before stable repository ids were persisted has repo_id=NULL. Replacement activation
        # must reset that old working set and record a superseded unknown boundary, while preserving a queued webhook
        # that already belongs to the replacement id.
        legacy_replacement_activation = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (LEGACY_REPLACEMENT_REPO, LEGACY_NEW_ID, "D-LEGACY-ADD"),
        ))
        legacy_reset = legacy_replacement_activation.get("replacement_reset") or {}
        legacy_reset_purged = legacy_reset.get("purged") or {}
        legacy_working_tables = (
            "code_node", "graph_version", "claim", "co_change", "co_change_seen_commit",
        )
        queued_replacement_delivery = int(admin(
            "SELECT count(*)::int FROM core.webhook_delivery "
            "WHERE delivery_key='D-LEGACY-REPLACEMENT-QUEUED' AND status='queued'",
        ))
        legacy_unknown_boundary = account_one(
            "SELECT superseded_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id='unknown'",
            (ACCOUNT, LEGACY_REPLACEMENT_REPO),
        )
        check("null-id predecessor working state is reset before replacement backfill",
              legacy_replacement_activation.get("legacy_replacement_reset") is True
              and all(count(table, LEGACY_REPLACEMENT_REPO) == 0
                      for table in legacy_working_tables)
              and legacy_reset_purged.get("webhook_inbox_purged") is False)
        check("replacement activation does not delete its queued webhook",
              queued_replacement_delivery == 1)
        check("null-id predecessor gets a superseded reader boundary",
              legacy_unknown_boundary is not None
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_REPLACEMENT_REPO, None)) is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_REPLACEMENT_REPO, LEGACY_NEW_ID)) is True)
        legacy_replacement_insight = as_json(admin(
            "SELECT core.repo_insights_for_installation(%s,%s,168)",
            (INSTALLATION, LEGACY_REPLACEMENT_REPO),
        ))
        legacy_old_file = as_json(admin(
            "SELECT core.file_insights_for_installation(%s,%s,%s)",
            (INSTALLATION, LEGACY_REPLACEMENT_REPO, "src/legacy.py"),
        ))
        check("null-id predecessor audit is not attributed to the replacement",
              legacy_replacement_insight.get("recent") == []
              and legacy_replacement_insight.get("hotspots") == []
              and legacy_old_file.get("total") == 0)
        legacy_duplicate = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (LEGACY_REPLACEMENT_REPO, LEGACY_NEW_ID, "D-LEGACY-ADD"),
        ))
        legacy_unknown_after_duplicate = account_one(
            "SELECT superseded_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id='unknown'",
            (ACCOUNT, LEGACY_REPLACEMENT_REPO),
        )
        check("duplicate replacement activation preserves the legacy reader boundary",
              legacy_duplicate.get("legacy_replacement_reset") is False
              and legacy_unknown_after_duplicate == legacy_unknown_boundary)

        # A replacement push can be processed before GitHub's lifecycle activation event. After that successful
        # graph ingest, identity reconciliation must pin the new id so a delayed old-id work event is rejected.
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
            "VALUES (%s,%s,'main',repeat('1',40),0,0,%s) RETURNING 1",
            (ACCOUNT, EARLY_WORK_REPLACEMENT_REPO, EARLY_NEW_ID),
        )
        early_reconcile = as_json(app(
            "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
            (EARLY_WORK_REPLACEMENT_REPO, EARLY_NEW_ID),
        ))
        early_activation = account_one(
            "SELECT activated_at FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s AND repository_id=%s",
            (ACCOUNT, EARLY_WORK_REPLACEMENT_REPO, EARLY_NEW_ID),
        )
        check("successful replacement ingest records a stable-id ownership boundary",
              early_reconcile.get("activation_recorded") is True
              and early_activation is not None)
        check("work-observed ownership rejects a delayed old-id event",
              app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (EARLY_WORK_REPLACEMENT_REPO, EARLY_OLD_ID)) is False
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (EARLY_WORK_REPLACEMENT_REPO, EARLY_NEW_ID)) is True)

        # A replacement work event is not lifecycle authority. If the current coordinate is already stamped with a
        # different known object id, fail closed until repository.created/installation_repositories.added performs
        # the bounded predecessor reset. Only a truly legacy NULL identity may converge from signed work metadata.
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id) "
            "VALUES (%s,%s,'main',repeat('2',40),0,0,%s) RETURNING 1",
            (ACCOUNT, GRAPH_CONFLICT_REPO, GRAPH_CONFLICT_OLD_ID),
        )
        known_old_allowed = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (GRAPH_CONFLICT_REPO, GRAPH_CONFLICT_OLD_ID),
        )
        different_new_allowed = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (GRAPH_CONFLICT_REPO, GRAPH_CONFLICT_NEW_ID),
        )
        account_one(
            "SELECT core.mark_governed_write('graph_version'); "
            "UPDATE core.graph_version SET repo_id=NULL WHERE account_id=%s AND repo=%s RETURNING 1",
            (ACCOUNT, GRAPH_CONFLICT_REPO),
        )
        legacy_new_allowed = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (GRAPH_CONFLICT_REPO, GRAPH_CONFLICT_NEW_ID),
        )
        check("work events cannot overwrite a different known graph id before lifecycle activation",
              known_old_allowed is True and different_new_allowed is False)
        check("signed work may converge only a legacy graph id that was never recorded",
              legacy_new_allowed is True)

        # Rows accepted before repository-id persistence may have only a name. They receive an explicit
        # unknown marker and remain fail-closed until a lifecycle re-add, not an ordinary work event.
        legacy_repo = "acme/legacy-no-id"
        legacy_offboard = offboard_current(legacy_repo, None, "installation_removed")
        legacy_without_id = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (legacy_repo, None),
        )
        legacy_with_later_id = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (legacy_repo, "6006"),
        )
        check("pre-id deletion records an unknown marker",
              legacy_offboard.get("marker_repository_id") == "unknown")
        check("pre-id deletion blocks both legacy and later work events",
              legacy_without_id is False and legacy_with_later_id is False)
        legacy_add_delivery = seed_add_delivery("D-LEGACY-NAME-ADD-CURRENT", legacy_repo, None)
        legacy_readd = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (legacy_repo, None, "D-LEGACY-NAME-ADD-CURRENT"),
        ))
        check("explicit name-only re-add clears a pre-id marker",
              legacy_add_delivery == 1 and legacy_readd.get("cleared", 0) == 1
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (legacy_repo, "6006")) is True)

        live_add_delivery = seed_add_delivery("D-LIVE-ADD-CURRENT", LIVE_REPO, REPO_ID)
        reactivated = as_json(app(
            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
            (LIVE_REPO, REPO_ID, "D-LIVE-ADD-CURRENT"),
        ))
        now_allowed = app(
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (LIVE_REPO, REPO_ID),
        )
        restored_repos = as_json(admin("SELECT core.repos_for_installation(%s)", (INSTALLATION,)))
        check("explicit re-add clears the repository tombstone",
              live_add_delivery == 1 and reactivated.get("ok") is True
              and reactivated.get("cleared", 0) >= 1
              and now_allowed is True)
        check("repo-level reader resumes after explicit re-add",
              LIVE_REPO in {row.get("repo") for row in restored_repos})

        # A caller cannot turn an ID-bearing durable deletion into name-only authority by passing NULL. The DB
        # adopts the authenticated payload ID, so an established same-name replacement remains untouched.
        check("stable-id downgrade fixture is seeded",
              seed_graph(ID_DOWNGRADE_REPO, ID_DOWNGRADE_NEW_ID, "ID-DOWNGRADE-NEW") == 1
              and seed_delete_delivery(
                  "D-ID-DOWNGRADE-OLD", ID_DOWNGRADE_REPO, ID_DOWNGRADE_OLD_ID) == 1)
        id_downgrade = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (ID_DOWNGRADE_REPO, None, "repository_deleted", "D-ID-DOWNGRADE-OLD"),
        ))
        check("ID-bearing durable deletion cannot downgrade to name-only replacement purge",
              id_downgrade.get("repository_id") == ID_DOWNGRADE_OLD_ID
              and id_downgrade.get("marker_repository_id") == ID_DOWNGRADE_OLD_ID
              and id_downgrade.get("targets") == []
              and count("graph_version", ID_DOWNGRADE_REPO) == 1
              and count("code_node", ID_DOWNGRADE_REPO) == 1
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (ID_DOWNGRADE_REPO, ID_DOWNGRADE_NEW_ID)) is True)

        class CurrentIdentityGH:
            def __init__(self, identity):
                self.identity = identity
                self.calls = []

            def repo_current_identity(self, repo):
                self.calls.append(repo)
                return self.identity

        class FailingIdentityGH:
            def __init__(self):
                self.calls = []

            def repo_current_identity(self, repo):
                self.calls.append(repo)
                raise RuntimeError("transient GitHub failure must not run inside grace")

        # The production sanitizer before this fix omitted repository.id. During a rolling deploy the old worker
        # still calls the one-argument DB function, while the new worker must recover the same processing row. If a
        # same-name repository is currently visible to the installation, preserve only state already bound to that
        # current id and supersede the legacy unknown marker. The old worker cannot make the name-only decision.
        check("legacy id-less replacement fixture is seeded",
              seed_graph(
                  LEGACY_IDLESS_REPLACEMENT_REPO,
                  LEGACY_IDLESS_REPLACEMENT_ID,
                  "LEGACY-IDLESS-REPLACEMENT-N",
              ) == 1
              and seed_remove_delivery(
                  "D-LEGACY-IDLESS-REPLACEMENT",
                  LEGACY_IDLESS_REPLACEMENT_REPO,
                  None,
              ) == 1
              # This giant gate intentionally leaves many independent processing fixtures in one synthetic
              # account. Make this focused recovery row the durable account head; production reaches it only after
              # earlier rows finish, while the identity-resolution assertion itself needs an honestly claimable row.
              and age_delivery("D-LEGACY-IDLESS-REPLACEMENT", "100 years") == 1)
        legacy_idless_shim_deferred = as_json(app(
            "SELECT core.purge_repo_with_authority(%s)",
            (LEGACY_IDLESS_REPLACEMENT_REPO,),
        ))
        legacy_idless_deferred_state = as_json(admin(
            "SELECT jsonb_build_object('status',status,'attempts',attempts,"
            "'due',not_before<=clock_timestamp()) FROM core.webhook_delivery "
            "WHERE delivery_key='D-LEGACY-IDLESS-REPLACEMENT'"
        ))
        legacy_idless_old_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-LEGACY-IDLESS-REPLACEMENT",),
        )
        legacy_idless_reclaimed = as_json(app(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1,8,3,%s,120)",
            ("D-LEGACY-IDLESS-REPLACEMENT", "offboard-legacy-replacement"),
        ))
        check(f"legacy id-less deferred row is reclaimed under an exact lease before identity resolution "
              f"({legacy_idless_reclaimed})",
              legacy_idless_reclaimed.get("claimed") is True
              and int(legacy_idless_reclaimed.get("lease_generation", 0)) >= 1)
        replacement_gh = CurrentIdentityGH({
            "full_name": LEGACY_IDLESS_REPLACEMENT_REPO,
            "id": int(LEGACY_IDLESS_REPLACEMENT_ID),
        })
        legacy_idless_resolved = ingest.purge_repo(
            app,
            LEGACY_IDLESS_REPLACEMENT_REPO,
            None,
            "installation_removed",
            "D-LEGACY-IDLESS-REPLACEMENT",
            gh=replacement_gh,
        )
        legacy_idless_marker = as_json(account_one(
            "SELECT jsonb_build_object('superseded',superseded_at IS NOT NULL,'reason',reason) "
            "FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repo=%s AND repository_id='unknown'",
            (ACCOUNT, LEGACY_IDLESS_REPLACEMENT_REPO),
        ))
        legacy_idless_completion = as_json(admin(
            "SELECT payload->'_veripsa_offboard_completed' FROM core.webhook_delivery "
            "WHERE delivery_key='D-LEGACY-IDLESS-REPLACEMENT'"
        ))
        legacy_idless_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            ("D-LEGACY-IDLESS-REPLACEMENT", legacy_idless_reclaimed["lease_generation"]),
        )
        check("old id-less deletion preserves the current same-name replacement via GitHub point authority",
              legacy_idless_shim_deferred.get("deferred") is True
              and legacy_idless_shim_deferred.get("defer_reason") == "legacy_identity_requires_new_worker"
              and legacy_idless_deferred_state == {
                  "status": "queued", "attempts": 0, "due": True}
              and legacy_idless_old_finish is False
              and legacy_idless_reclaimed.get("claimed") is True
              and legacy_idless_reclaimed.get("attempts") == 1
              and replacement_gh.calls == [LEGACY_IDLESS_REPLACEMENT_REPO]
              and legacy_idless_resolved.get("preserved_current") is True
              and legacy_idless_resolved.get("deferred") is False
              and legacy_idless_resolved.get("lifecycle_reset") is False
              and count("graph_version", LEGACY_IDLESS_REPLACEMENT_REPO) == 1
              and count("code_node", LEGACY_IDLESS_REPLACEMENT_REPO) == 1
              and legacy_idless_marker == {
                  "superseded": True, "reason": "installation_removed"}
              and legacy_idless_completion == {
                  LEGACY_IDLESS_REPLACEMENT_REPO: LEGACY_IDLESS_REPLACEMENT_ID}
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_IDLESS_REPLACEMENT_REPO,
                       LEGACY_IDLESS_REPLACEMENT_ID)) is True
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_IDLESS_REPLACEMENT_REPO, None)) is False
              and legacy_idless_finish is True)

        # Even with a positive point read, do not resolve a just-received legacy event during GitHub's consistency
        # window. Schedule a due time without consuming an attempt; the completion guard still prevents finish.
        check("fresh id-less grace fixture is seeded",
              seed_graph(FRESH_IDLESS_REPO, FRESH_IDLESS_ID, "FRESH-IDLESS-N") == 1
              and seed_remove_delivery("D-FRESH-IDLESS", FRESH_IDLESS_REPO, None) == 1)
        fresh_gh = FailingIdentityGH()
        fresh_idless = ingest.purge_repo(
            app, FRESH_IDLESS_REPO, None, "installation_removed",
            "D-FRESH-IDLESS", gh=fresh_gh,
        )
        fresh_state = as_json(admin(
            "SELECT jsonb_build_object('status',status,'attempts',attempts,"
            "'future',not_before>clock_timestamp(),"
            "'delay_seconds',extract(epoch FROM (not_before-clock_timestamp()))) "
            "FROM core.webhook_delivery "
            "WHERE delivery_key='D-FRESH-IDLESS'"
        ))
        fresh_completion = admin(
            "SELECT payload->'_veripsa_offboard_completed' FROM core.webhook_delivery "
            "WHERE delivery_key='D-FRESH-IDLESS'"
        )
        fresh_pending = {
            row.get("key") for row in as_json(app(
                "SELECT core.pending_webhook_deliveries_with_authority(100,1,8)"
            ))
        }
        fresh_claim = as_json(app(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1,8,3,%s,120)",
            ("D-FRESH-IDLESS", "offboard-fresh-idless"),
        ))
        fresh_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-FRESH-IDLESS",),
        )
        check("fresh id-less deletion is attempt-neutral and unclaimable throughout the consistency grace",
              fresh_idless.get("deferred") is True
              and fresh_idless.get("defer_reason") == "legacy_identity_consistency_window"
              and fresh_state.get("status") == "queued"
              and fresh_state.get("attempts") == 0
              and fresh_state.get("future") is True
              and 240 < float(fresh_state.get("delay_seconds", 0)) <= 300
              and fresh_gh.calls == []
              and "D-FRESH-IDLESS" not in fresh_pending
              and fresh_claim.get("claimed") is False
              and fresh_completion is None and fresh_finish is False
              and count("graph_version", FRESH_IDLESS_REPO) == 1
              and count("code_node", FRESH_IDLESS_REPO) == 1)

        # A 404 from the installation-token point read is authoritative absence, not an API failure. Once the
        # grace elapsed, a current id-less removal received after the graph snapshot purges by coordinate and marks
        # completion. Permission and transient errors are covered separately by the GitHub resilience gate.
        check("legacy id-less 404 fixture is seeded",
              seed_graph(
                  LEGACY_IDLESS_GONE_REPO,
                  LEGACY_IDLESS_GONE_ID,
                  "LEGACY-IDLESS-GONE-N",
              ) == 1
              and int(account_one(
                  "SELECT core.mark_governed_write('graph_version'); "
                  "UPDATE core.graph_version SET ingested_at=clock_timestamp()-interval '20 minutes' "
                  "WHERE account_id=%s AND repo=%s; SELECT 1",
                  (ACCOUNT, LEGACY_IDLESS_GONE_REPO),
              )) == 1
              and seed_remove_delivery(
                  "D-LEGACY-IDLESS-GONE", LEGACY_IDLESS_GONE_REPO, None) == 1
              and age_delivery("D-LEGACY-IDLESS-GONE", "10 minutes") == 1)
        gone_gh = CurrentIdentityGH(None)
        legacy_idless_gone = ingest.purge_repo(
            app,
            LEGACY_IDLESS_GONE_REPO,
            None,
            "installation_removed",
            "D-LEGACY-IDLESS-GONE",
            gh=gone_gh,
        )
        legacy_idless_gone_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-LEGACY-IDLESS-GONE",),
        )
        check("old id-less deletion purges after GitHub confirms the repository is absent",
              gone_gh.calls == [LEGACY_IDLESS_GONE_REPO]
              and legacy_idless_gone.get("targets") == [LEGACY_IDLESS_GONE_REPO]
              and legacy_idless_gone.get("delivery_order") == "github_confirmed_absent"
              and legacy_idless_gone.get("confirmed_absent") is True
              and count("graph_version", LEGACY_IDLESS_GONE_REPO) == 0
              and count("code_node", LEGACY_IDLESS_GONE_REPO) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_IDLESS_GONE_REPO, LEGACY_IDLESS_GONE_ID)) is False
              and legacy_idless_gone_finish is True)

        # Current 404 authority is newer than an old delivery. Even if a stable replacement was observed after that
        # delivery, it is now inaccessible to the installation and must not be retained/finalized as stale residue.
        check("legacy id-less observed-then-gone fixture is seeded",
              seed_graph(
                  LEGACY_IDLESS_OBSERVED_GONE_REPO,
                  LEGACY_IDLESS_OBSERVED_GONE_ID,
                  "LEGACY-IDLESS-OBSERVED-GONE-N",
              ) == 1
              and seed_remove_delivery(
                  "D-LEGACY-IDLESS-OBSERVED-GONE",
                  LEGACY_IDLESS_OBSERVED_GONE_REPO,
                  None,
              ) == 1
              and age_delivery("D-LEGACY-IDLESS-OBSERVED-GONE", "10 minutes") == 1)
        observed_gone_gh = CurrentIdentityGH(None)
        observed_gone = ingest.purge_repo(
            app,
            LEGACY_IDLESS_OBSERVED_GONE_REPO,
            None,
            "installation_removed",
            "D-LEGACY-IDLESS-OBSERVED-GONE",
            gh=observed_gone_gh,
        )
        observed_gone_marker = as_json(account_one(
            "SELECT jsonb_build_object('id',repository_id,'active',superseded_at IS NULL) "
            "FROM core.repository_lifecycle_tombstone WHERE account_id=%s AND repo=%s "
            "AND repository_id='unknown'",
            (ACCOUNT, LEGACY_IDLESS_OBSERVED_GONE_REPO),
        ))
        observed_gone_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-LEGACY-IDLESS-OBSERVED-GONE",),
        )
        check("current GitHub 404 purges replacement state observed after the old id-less delivery",
              observed_gone_gh.calls == [LEGACY_IDLESS_OBSERVED_GONE_REPO]
              and observed_gone.get("confirmed_absent") is True
              and observed_gone.get("targets") == [LEGACY_IDLESS_OBSERVED_GONE_REPO]
              and count("graph_version", LEGACY_IDLESS_OBSERVED_GONE_REPO) == 0
              and count("code_node", LEGACY_IDLESS_OBSERVED_GONE_REPO) == 0
              and observed_gone_marker == {"id": "unknown", "active": True}
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_IDLESS_OBSERVED_GONE_REPO,
                       LEGACY_IDLESS_OBSERVED_GONE_ID)) is False
              and observed_gone_finish is True)

        # Render applies the schema before routing traffic to the new image. The previous/rollback worker calls the
        # one-argument name, so that compatibility shim must authenticate its unique processing delivery and purge
        # through /4 instead of returning a swallowed DB error with residue.
        check("mixed-version rollback fixture is seeded",
              seed_graph(LEGACY_WORKER_REPO, LEGACY_WORKER_ID, "LEGACY-WORKER-N") == 1
              and seed_remove_delivery(
                  "D-LEGACY-WORKER-REMOVE", LEGACY_WORKER_REPO, LEGACY_WORKER_ID) == 1)
        legacy_worker_offboard = as_json(app(
            "SELECT core.purge_repo_with_authority(%s)", (LEGACY_WORKER_REPO,),
        ))
        check("legacy worker shim re-authenticates the durable deletion and leaves no working residue",
              legacy_worker_offboard.get("repository_id") == LEGACY_WORKER_ID
              and legacy_worker_offboard.get("targets") == [LEGACY_WORKER_REPO]
              and count("graph_version", LEGACY_WORKER_REPO) == 0
              and count("code_node", LEGACY_WORKER_REPO) == 0
              and app("SELECT core.repository_event_allowed_with_authority(%s,%s)",
                      (LEGACY_WORKER_REPO, LEGACY_WORKER_ID)) is False)

        check("ambiguous mixed-version fixture is seeded",
              seed_graph(AMBIGUOUS_SHIM_REPO, AMBIGUOUS_SHIM_ID_A, "AMBIGUOUS-SHIM-N") == 1
              and seed_delete_delivery(
                  "D-AMBIGUOUS-SHIM-A", AMBIGUOUS_SHIM_REPO, AMBIGUOUS_SHIM_ID_A) == 1
              and seed_delete_delivery(
                  "D-AMBIGUOUS-SHIM-B", AMBIGUOUS_SHIM_REPO, AMBIGUOUS_SHIM_ID_B) == 1)
        ambiguous_shim_rejected = False
        try:
            app("SELECT core.purge_repo_with_authority(%s)", (AMBIGUOUS_SHIM_REPO,))
        except psycopg2.Error as exc:
            ambiguous_shim_rejected = "exactly one durable deletion authority" in str(exc)
        swallowed_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-AMBIGUOUS-SHIM-A",),
        )
        swallowed_row = as_json(admin(
            "SELECT jsonb_build_object('status',status,'completed',"
            "payload->'_veripsa_offboard_completed') "
            "FROM core.webhook_delivery WHERE delivery_key='D-AMBIGUOUS-SHIM-A'"
        ))
        check("old-worker swallowed failure cannot finalize an incomplete deletion delivery",
              ambiguous_shim_rejected
              and swallowed_finish is False
              and swallowed_row == {"status": "processing", "completed": None}
              and count("graph_version", AMBIGUOUS_SHIM_REPO) == 1
              and count("code_node", AMBIGUOUS_SHIM_REPO) == 1)

        check("multi-repository removal fixture is seeded",
              seed_graph(FANOUT_DELETE_REPO_A, FANOUT_DELETE_ID_A, "FANOUT-DELETE-A") == 1
              and seed_graph(FANOUT_DELETE_REPO_B, FANOUT_DELETE_ID_B, "FANOUT-DELETE-B") == 1
              and int(admin(
                  "INSERT INTO core.webhook_delivery("
                  "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at,lease_generation) "
                  "VALUES ('D-FANOUT-DELETE','installation_repositories',%s,NULL,%s::jsonb,"
                  "'processing',1,clock_timestamp(),clock_timestamp(),1) RETURNING 1",
                  (ACCOUNT, json.dumps({
                      "action": "removed",
                      "repositories_removed": [
                          {"id": FANOUT_DELETE_ID_A, "full_name": FANOUT_DELETE_REPO_A},
                          {"id": FANOUT_DELETE_ID_B, "full_name": FANOUT_DELETE_REPO_B},
                      ],
                  })),
              )) == 1)
        fanout_a = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (FANOUT_DELETE_REPO_A, FANOUT_DELETE_ID_A,
             "installation_removed", "D-FANOUT-DELETE"),
        ))
        partial_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-FANOUT-DELETE",),
        )
        partial_marker = as_json(admin(
            "SELECT payload->'_veripsa_offboard_completed' "
            "FROM core.webhook_delivery WHERE delivery_key='D-FANOUT-DELETE'"
        ))
        check("multi-repository removal cannot finalize after only one sibling completes",
              fanout_a.get("targets") == [FANOUT_DELETE_REPO_A]
              and partial_finish is False
              and partial_marker == {FANOUT_DELETE_REPO_A: FANOUT_DELETE_ID_A}
              and count("code_node", FANOUT_DELETE_REPO_A) == 0
              and count("code_node", FANOUT_DELETE_REPO_B) == 1)
        fanout_b = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (FANOUT_DELETE_REPO_B, FANOUT_DELETE_ID_B,
             "installation_removed", "D-FANOUT-DELETE"),
        ))
        complete_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-FANOUT-DELETE",),
        )
        completed_fanout = as_json(admin(
            "SELECT jsonb_build_object('status',status,'payload',payload) "
            "FROM core.webhook_delivery WHERE delivery_key='D-FANOUT-DELETE'"
        ))
        check("multi-repository removal finalizes only after every sibling completes",
              fanout_b.get("targets") == [FANOUT_DELETE_REPO_B]
              and complete_finish is True
              and completed_fanout == {"status": "done", "payload": {}}
              and count("code_node", FANOUT_DELETE_REPO_B) == 0)

        check("conflicting duplicate fan-out fixture is seeded",
              seed_graph(FANOUT_DELETE_REPO_A, FANOUT_DELETE_ID_B, "FANOUT-DUPLICATE-CURRENT") == 1
              and int(admin(
                  "INSERT INTO core.webhook_delivery("
                  "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) "
                  "VALUES ('D-FANOUT-CONFLICTING-DUPLICATE','installation_repositories',%s,NULL,%s::jsonb,"
                  "'processing',1,clock_timestamp(),clock_timestamp()) RETURNING 1",
                  (ACCOUNT, json.dumps({
                      "action": "removed",
                      "repositories_removed": [
                          {"id": FANOUT_DELETE_ID_A, "full_name": FANOUT_DELETE_REPO_A},
                          {"id": FANOUT_DELETE_ID_B, "full_name": FANOUT_DELETE_REPO_A},
                      ],
                  })),
              )) == 1)
        conflicting_duplicate_old = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (FANOUT_DELETE_REPO_A, FANOUT_DELETE_ID_A,
             "installation_removed", "D-FANOUT-CONFLICTING-DUPLICATE"),
        ))
        conflicting_duplicate_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-FANOUT-CONFLICTING-DUPLICATE",),
        )
        conflicting_duplicate_row = as_json(admin(
            "SELECT jsonb_build_object('status',status,'completed',"
            "payload->'_veripsa_offboard_completed') FROM core.webhook_delivery "
            "WHERE delivery_key='D-FANOUT-CONFLICTING-DUPLICATE'"
        ))
        check("one completion key cannot finalize conflicting same-name repository identities",
              conflicting_duplicate_old.get("targets") == []
              and conflicting_duplicate_finish is False
              and conflicting_duplicate_row == {
                  "status": "processing",
                  "completed": {FANOUT_DELETE_REPO_A: FANOUT_DELETE_ID_A},
              }
              and count("code_node", FANOUT_DELETE_REPO_A) == 1)

        malformed_deletions_seeded = int(admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) VALUES "
            "('D-MALFORMED-REPOSITORY-DELETE','repository',%s,NULL,%s::jsonb,'processing',1,now(),now()),"
            "('D-MALFORMED-INSTALL-REMOVE','installation_repositories',%s,NULL,%s::jsonb,'processing',1,now(),now()),"
            "('D-MALFORMED-ID-INSTALL-REMOVE','installation_repositories',%s,NULL,%s::jsonb,'processing',1,now(),now()),"
            "('D-WRONG-ID-REPOSITORY-DELETE','repository',%s,%s,%s::jsonb,'processing',1,now(),now()),"
            "('D-EMPTY-INSTALL-REMOVE','installation_repositories',%s,NULL,%s::jsonb,'processing',1,now(),now()) "
            "RETURNING 1",
            (
                ACCOUNT, json.dumps({"action": "deleted", "repository": {}}),
                ACCOUNT, json.dumps({"action": "removed", "repositories_removed": [{}]}),
                ACCOUNT, json.dumps({
                    "action": "removed",
                    "repositories_removed": [{"id": "not-an-id", "full_name": "acme/malformed"}],
                    "_veripsa_offboard_completed": {"acme/malformed": "not-an-id"},
                }),
                ACCOUNT, "acme/wrong-id", json.dumps({
                    "action": "deleted",
                    "repository": {"id": "101", "full_name": "acme/wrong-id"},
                    "_veripsa_offboard_completed": {"acme/wrong-id": "202"},
                }),
                ACCOUNT, json.dumps({"action": "removed", "repositories_removed": []}),
            ),
        ))
        malformed_finishes = [
            app("SELECT core.finish_webhook_delivery_with_authority(%s)", (key,))
            for key in (
                "D-MALFORMED-REPOSITORY-DELETE",
                "D-MALFORMED-INSTALL-REMOVE",
                "D-MALFORMED-ID-INSTALL-REMOVE",
                "D-WRONG-ID-REPOSITORY-DELETE",
                "D-EMPTY-INSTALL-REMOVE",
            )
        ]
        malformed_statuses = set(admin(
            "SELECT array_agg(status ORDER BY delivery_key) FROM core.webhook_delivery "
            "WHERE delivery_key LIKE 'D-MALFORMED-%%' "
            "OR delivery_key IN ('D-WRONG-ID-REPOSITORY-DELETE','D-EMPTY-INSTALL-REMOVE')"
        ) or [])
        check("malformed or empty deletion payloads cannot finalize without an actionable completed target",
              malformed_deletions_seeded == 1
              and malformed_finishes == [False, False, False, False, False]
              and malformed_statuses == {"processing"})

        unordered_lifecycle_denied = 0
        for sql, args in (
            ("SELECT core.reactivate_repository_with_authority(%s,%s)",
             (LIVE_REPO, REPO_ID)),
            ("SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
             (LIVE_REPO, REPO_ID, None)),
            ("SELECT core.offboard_repository_with_authority(%s,%s,%s)",
             (LIVE_REPO, REPO_ID, "repository_deleted")),
            ("SELECT core.purge_repo_with_authority(%s)", (NO_AUTHORITY_REPO,)),
            ("SELECT core._purge_repo_with_authority(%s,%s)", (LIVE_REPO, True)),
            ("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
             (LIVE_REPO, REPO_ID, "repository_deleted", None)),
            ("SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
             (LIVE_REPO, REPO_ID, "repository_deleted", "   ")),
        ):
            try:
                app(sql, args)
            except psycopg2.Error as exc:
                unordered_lifecycle_denied += int(
                    "permission denied" in str(exc)
                    or "durable delivery authority" in str(exc)
                    or "durable deletion authority" in str(exc)
                )
        check("App cannot call unordered lifecycle paths or an unauthenticated compatibility shim",
              unordered_lifecycle_denied == 7)

        disabled_owner_compatibility = 0
        for sql, args in (
            ("SELECT core.purge_repo_with_authority(%s)", (LIVE_REPO,)),
            ("SELECT core.offboard_repository_with_authority(%s,%s,%s)",
             (LIVE_REPO, REPO_ID, "repository_deleted")),
        ):
            try:
                admin(sql, args)
            except psycopg2.Error as exc:
                disabled_owner_compatibility += int(exc.pgcode == "42501")
        check("deprecated unordered offboard and owner raw shim fail closed without session authority",
              disabled_owner_compatibility == 2)

        app_durable_offboard = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.offboard_repository_with_authority(text,text,text,text)','EXECUTE')")
        writer_durable_offboard = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core.offboard_repository_with_authority(text,text,text,text)','EXECUTE')")
        app_raw_purge = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.purge_repo_with_authority(text)','EXECUTE')")
        writer_raw_purge = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core.purge_repo_with_authority(text)','EXECUTE')")
        app_private_defer = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core._defer_webhook_delivery_with_authority(text,timestamptz,text)','EXECUTE')")
        writer_private_defer = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core._defer_webhook_delivery_with_authority(text,timestamptz,text)','EXECUTE')")
        app_exact_defer = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.defer_webhook_delivery_with_authority(text,timestamptz,text,bigint)','EXECUTE')")
        writer_exact_defer = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core.defer_webhook_delivery_with_authority(text,timestamptz,text,bigint)','EXECUTE')")
        app_legacy_preflight = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.prepare_legacy_repository_offboard_with_authority(text,text,text)','EXECUTE')")
        writer_legacy_preflight = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core.prepare_legacy_repository_offboard_with_authority(text,text,text)','EXECUTE')")
        app_confirm_absent = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.confirm_absent_legacy_repository_offboard_with_authority(text,text,text)','EXECUTE')")
        writer_confirm_absent = admin(
            "SELECT has_function_privilege('veripsa_writer',"
            "'core.confirm_absent_legacy_repository_offboard_with_authority(text,text,text)','EXECUTE')")
        check("only the App durable boundary and authenticated rollback shim retain EXECUTE",
              app_durable_offboard is True
              and writer_durable_offboard is False
              and app_raw_purge is True
              and writer_raw_purge is False
              and app_private_defer is False
              and writer_private_defer is False
              and app_exact_defer is True
              and writer_exact_defer is False
              and app_legacy_preflight is True
              and writer_legacy_preflight is False
              and app_confirm_absent is True
              and writer_confirm_absent is False)

        admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,payload,status,attempts,locked_at,lease_generation) "
            "VALUES ('D-EXACT-DEFER','push',%s,'{}'::jsonb,'processing',3,now(),7) RETURNING 1",
            (ACCOUNT,),
        )
        stale_defer = app(
            "SELECT core.defer_webhook_delivery_with_authority(%s,now()+interval '5 minutes',%s,%s)",
            ("D-EXACT-DEFER", "consistency retry", 6),
        )
        after_stale_defer = as_json(admin(
            "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
            "FROM core.webhook_delivery WHERE delivery_key='D-EXACT-DEFER'"))
        exact_defer = app(
            "SELECT core.defer_webhook_delivery_with_authority(%s,now()+interval '5 minutes',%s,%s)",
            ("D-EXACT-DEFER", "consistency retry", 7),
        )
        after_exact_defer = as_json(admin(
            "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation,"
            "'not_before_set',not_before IS NOT NULL) "
            "FROM core.webhook_delivery WHERE delivery_key='D-EXACT-DEFER'"))
        check("App consistency defer is exact-lease: stale owner cannot requeue a successor",
              stale_defer is False and after_stale_defer == {
                  "status": "processing", "attempts": 3, "lease": 7,
              }
              and exact_defer is True and after_exact_defer == {
                  "status": "queued", "attempts": 2, "lease": 7, "not_before_set": True,
              })

        premature_finish = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-REPO-PROCESSING",),
        )
        still_processing = admin(
            "SELECT status FROM core.webhook_delivery WHERE delivery_key='D-REPO-PROCESSING'"
        )
        completed_delete = as_json(app(
            "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
            (LIVE_REPO, None, "repository_deleted", "D-REPO-PROCESSING"),
        ))
        finished = app(
            "SELECT core.finish_webhook_delivery_with_authority(%s)",
            ("D-REPO-PROCESSING",),
        )
        finalized = admin(
            "SELECT jsonb_build_object('status',status,'repo',repo,'payload',payload) "
            "FROM core.webhook_delivery WHERE delivery_key='D-REPO-PROCESSING'",
        )
        finalized = as_json(finalized)
        check("deletion delivery finalizes and drops the deleted repo coordinate",
              premature_finish is False and still_processing == "processing"
              and completed_delete.get("ok") is True
              and finished is True and finalized.get("status") == "done"
              and finalized.get("repo") is None and finalized.get("payload") == {})

        privileges = admin("""
          SELECT count(*)::int FROM (VALUES
            (has_table_privilege('veripsa_writer','core.repository_lifecycle_tombstone','SELECT')),
            (has_table_privilege('veripsa_writer','core.repository_lifecycle_tombstone','INSERT')),
            (has_table_privilege('veripsa_reader','core.repository_lifecycle_tombstone','SELECT')),
            (has_table_privilege('veripsa_app','core.repository_lifecycle_tombstone','SELECT')),
            (has_table_privilege('veripsa_writer','core.repository_lifecycle_activation','SELECT')),
            (has_table_privilege('veripsa_writer','core.repository_lifecycle_activation','INSERT')),
            (has_table_privilege('veripsa_reader','core.repository_lifecycle_activation','SELECT')),
            (has_table_privilege('veripsa_app','core.repository_lifecycle_activation','SELECT'))
          ) AS p(allowed) WHERE allowed
        """)
        check("repository lifecycle tables have no direct buyer or app table access", int(privileges) == 0)

        # CROSS-ACCOUNT TRANSFER REPLAY: the transfer payload is durable authority for the former coordinate, but
        # its retry can lose a race with GitHub reusing old-owner/name for a different stable repository.  The DB
        # must authenticate every coordinate from the exact processing row and apply its immutable receive boundary.
        def provision_transfer_accounts(old_account: str, new_account: str, installation: str) -> None:
            admin(
                "SELECT set_config('core.current_account',%s,true); "
                "SELECT core.mark_governed_write('account'); "
                "INSERT INTO core.account(account_id,display_name) VALUES (%s,'Transfer old') "
                "ON CONFLICT (account_id) DO NOTHING; "
                "SELECT set_config('core.current_account',%s,true); "
                "SELECT core.mark_governed_write('account'); "
                "INSERT INTO core.account(account_id,display_name) VALUES (%s,'Transfer new') "
                "ON CONFLICT (account_id) DO NOTHING; "
                "INSERT INTO core.installation_account(installation_id,account_id) VALUES (%s,%s),(%s,%s) "
                "ON CONFLICT (installation_id) DO UPDATE SET account_id=EXCLUDED.account_id,revoked_at=NULL; "
                "SELECT 1",
                (old_account, old_account, new_account, new_account,
                 installation + "-old", old_account, installation, new_account),
            )

        def transfer_payload(repo_id: str, repo_name: str, old_id: str, old_login: str,
                             new_id: str, new_login: str, old_repo_name: str | None = None) -> dict:
            changes = {"owner": {"from": {"organization": {
                "id": old_id, "login": old_login,
            }}}}
            if old_repo_name is not None:
                changes["repository"] = {"name": {"from": old_repo_name}}
            return {
                "action": "transferred",
                "repository": {
                    "id": repo_id,
                    "name": repo_name,
                    "full_name": f"{new_login}/{repo_name}",
                    "owner": {"id": new_id, "login": new_login},
                },
                "changes": changes,
            }

        def seed_transfer_delivery(key: str, payload: dict, new_id: str, age: str = "2 hours") -> int:
            return int(admin(
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,locked_at) "
                "VALUES (%s,'repository',%s,%s,%s::jsonb,'processing',2,"
                "clock_timestamp()-(%s::interval),clock_timestamp()) RETURNING 1",
                (key, new_id, payload["repository"]["full_name"], json.dumps(payload), age),
            ))

        def seed_transfer_coordinate(account: str, repo: str, repo_id: str,
                                     graph_age: str, activation_age: str) -> int:
            agent_id = "XFER-AG-" + repo_id
            workspace_id = "XFER-WS-" + repo_id
            return int(scoped_one(
                "veripsa_migrator", account,
                "SELECT core.mark_governed_write('graph_version'); "
                "INSERT INTO core.graph_version("
                "account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
                "VALUES (%s,%s,'main',repeat('a',40),1,0,%s,clock_timestamp()-(%s::interval)); "
                "SELECT core.mark_governed_write('code_node'); "
                "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) "
                "VALUES (%s,%s,'main',%s,'file','src/transfer.py'); "
                "INSERT INTO core.repository_lifecycle_activation("
                "account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at) "
                "VALUES (%s,%s,%s,clock_timestamp()-(%s::interval),true,clock_timestamp()-(%s::interval)); "
                "SELECT core.mark_governed_write('agent'); "
                "INSERT INTO core.agent(agent_id,account_id,display_name) VALUES (%s,%s,'Transfer agent'); "
                "SELECT core.mark_governed_write('claim'); "
                "INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state) "
                "VALUES (%s,%s,%s,'PR-XFER',%s,'main','src/claimed.py','active'); "
                "SELECT core.mark_governed_write('co_change'); "
                "INSERT INTO core.co_change(account_id,repo,path_a,path_b,co,n_a,n_b,strength,lift,n_total,generation_observed_at) "
                "VALUES (%s,%s,'src/a.py','src/b.py',1,1,1,1,2,1,clock_timestamp()-interval '3 hours'); "
                "SELECT core.mark_governed_write('co_change_seen_commit'); "
                "INSERT INTO core.co_change_seen_commit(account_id,repo,commit_sha,generation_observed_at) "
                "VALUES (%s,%s,repeat('c',40),clock_timestamp()-interval '3 hours'); "
                "SELECT core.mark_governed_write('workspace'); "
                "INSERT INTO core.workspace(workspace_id,created_by_account,state) VALUES (%s,%s,'active'); "
                "SELECT core.mark_governed_write('workspace_member'); "
                "INSERT INTO core.workspace_member(workspace_id,account_id,repo,branch,consent_state,joined_at) "
                "VALUES (%s,%s,%s,'main','accepted',clock_timestamp()-interval '3 hours'); "
                "SELECT core.mark_governed_write('grant'); "
                "INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,repo,scope,granted_at) "
                "VALUES (%s,%s,%s,%s,ARRAY['read'],clock_timestamp()-interval '3 hours'); "
                "SELECT core.mark_governed_write('store_connection'); "
                "INSERT INTO core.store_connection(connection_id,account_id,provider,target,connected_at) "
                "VALUES (%s,%s,'github',%s,clock_timestamp()-interval '3 hours'); "
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,repo,payload,status,attempts,received_at,done_at) VALUES "
                "(%s,'push',%s,%s,'{}'::jsonb,'done',1,clock_timestamp()-interval '4 hours',clock_timestamp()),"
                "(%s,'push',%s,%s,jsonb_build_object('repository',jsonb_build_object('id',%s,'full_name',%s)),"
                " 'queued',0,clock_timestamp()-interval '3 hours',NULL),"
                "(%s,'push',%s,%s,jsonb_build_object('repository',jsonb_build_object('id',%s,'full_name',%s)),"
                " 'queued',0,clock_timestamp()-interval '3 hours',NULL),"
                "(%s,'push',%s,%s,jsonb_build_object('repository',jsonb_build_object('id',%s,'full_name',%s)),"
                " 'queued',0,clock_timestamp()-interval '1 hour',NULL); "
                "SELECT 1",
                (account, repo, repo_id, graph_age,
                 account, repo, "XFER-N-" + repo_id,
                 account, repo_id, repo, activation_age, activation_age,
                 agent_id, account,
                 "XFER-CL-" + repo_id, account, agent_id, repo,
                 account, repo,
                 account, repo,
                 workspace_id, account,
                 workspace_id, account, repo,
                 "XFER-GR-" + repo_id, account, agent_id, repo,
                 "XFER-CN-" + repo_id, account, repo,
                 "D-XFER-SIDE-DONE-" + repo_id, account, repo,
                 "D-XFER-SIDE-OLD-" + repo_id, account, repo, repo_id, repo,
                 "D-XFER-SIDE-OTHER-" + repo_id, account, repo,
                 str(int(repo_id) + 900000), repo,
                 "D-XFER-SIDE-LATE-" + repo_id, account, repo, repo_id, repo),
            ))

        def call_transfer(new_account: str, old_account: str, old_repo: str, repo_id: str,
                          key: str, proof_state: str, current_repo_id=None,
                          current_owner_id=None, current_full_name=None) -> dict:
            conn = conn_for("veripsa_app")
            try:
                with conn, conn.cursor() as cur:
                    cur.execute("SET search_path=core")
                    cur.execute("SELECT set_config('core.installation_account',%s,true)", (new_account,))
                    cur.execute(
                        "SELECT core.transfer_repo_coordinate_with_authority("
                        "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                        (old_account, old_repo, repo_id, key),
                    )
                    probe = as_json(cur.fetchone()[0])
                    if probe.get("idempotent") is True:
                        return probe
                    expected_new_owner = (new_account[8:]
                                          if new_account.startswith("ACCT-GH-") else new_account)
                    expected_old_owner = (old_account[8:]
                                          if old_account.startswith("ACCT-GH-") else old_account)
                    if (proof_state == "found" and str(current_repo_id) == repo_id
                            and str(current_owner_id) != expected_old_owner):
                        cur.execute(
                            "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,"
                            "'lock_current',%s,%s,%s)",
                            (old_account, old_repo, repo_id, key,
                             current_repo_id, current_owner_id, current_full_name),
                        )
                        locked = as_json(cur.fetchone()[0])
                        if locked.get("current_reproof_required") is not True:
                            raise AssertionError(f"transfer current lock failed: {locked}")
                    cur.execute(
                        "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,%s,%s,%s,%s)",
                        (old_account, old_repo, repo_id, key, proof_state,
                         current_repo_id, current_owner_id, current_full_name),
                    )
                    result = as_json(cur.fetchone()[0])
                    if (result.get("ownership_isolated") is True
                            and str(current_owner_id) == expected_new_owner
                            and result.get("current_account_owns_repository") is True):
                        cur.execute(
                            "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
                            (current_full_name, repo_id, key),
                        )
                        activated = as_json(cur.fetchone()[0])
                        if (activated.get("activated") is not True
                                or activated.get("stale_lifecycle_event") is not False):
                            raise AssertionError(f"transfer activation failed: {activated}")
                    return result
            finally:
                conn.close()

        # Legitimate id1 coordinate is older than its signed transfer boundary and is purged exactly once.
        legit_old_id, legit_new_id, legit_repo_id = "410001", "410002", "410010"
        legit_old_account, legit_new_account = f"ACCT-GH-{legit_old_id}", f"ACCT-GH-{legit_new_id}"
        legit_old_repo = "old-transfer/svc"
        legit_payload = transfer_payload(
            legit_repo_id, "svc", legit_old_id, "old-transfer", legit_new_id, "new-transfer")
        provision_transfer_accounts(legit_old_account, legit_new_account, "inst-xfer-legit")
        check("legitimate transfer fixture has an old id1 coordinate and durable processing boundary",
              seed_transfer_coordinate(
                  legit_old_account, legit_old_repo, legit_repo_id, "3 hours", "3 hours") == 1
              and seed_transfer_delivery("D-XFER-LEGIT", legit_payload, legit_new_id) == 1)
        scoped_one(
            "veripsa_migrator", legit_old_account,
            "SELECT core.mark_governed_write('workspace'); "
            "INSERT INTO core.workspace(workspace_id,created_by_account,state) VALUES (%s,%s,'active'); "
            "SELECT core.mark_governed_write('workspace_member'); "
            "INSERT INTO core.workspace_member(workspace_id,account_id,repo,branch,consent_state,joined_at) "
            "VALUES (%s,%s,%s,'main','accepted',clock_timestamp()); "
            "SELECT core.mark_governed_write('grant'); "
            "INSERT INTO core.grant(grant_id,grantor_account,grantee_agent,repo,scope,granted_at) "
            "VALUES (%s,%s,%s,%s,ARRAY['read'],clock_timestamp()); "
            "SELECT core.mark_governed_write('store_connection'); "
            "INSERT INTO core.store_connection(connection_id,account_id,provider,target,connected_at) "
            "VALUES (%s,%s,'github',%s,clock_timestamp()); SELECT 1",
            ("XFER-WS-NEW-" + legit_repo_id, legit_old_account,
             "XFER-WS-NEW-" + legit_repo_id, legit_old_account, legit_old_repo,
             "XFER-GR-NEW-" + legit_repo_id, legit_old_account,
             "XFER-AG-" + legit_repo_id, legit_old_repo,
             "XFER-CN-NEW-" + legit_repo_id, legit_old_account, legit_old_repo),
        )
        # A generic Core write on the NEW owner intentionally has repo_id=NULL until App reconciliation. It may be
        # fresh current work, so transfer activation must not classify it as a legacy predecessor and purge it.
        scoped_one(
            "veripsa_migrator", legit_new_account,
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,'new-transfer/svc','main',repeat('e',40),1,0,NULL,clock_timestamp()); "
            "SELECT core.mark_governed_write('code_node'); "
            "INSERT INTO core.code_node(account_id,repo,branch,node_id,node_kind,path) "
            "VALUES (%s,'new-transfer/svc','main',%s,'file','src/current-core.py'); "
            "INSERT INTO core.repository_lifecycle_activation("
            "account_id,repository_id,repo,activated_at,lifecycle_authoritative,generation_started_at) "
            "VALUES (%s,%s,'new-transfer/svc',clock_timestamp()-interval '1 hour',false,"
            "NULL); "
            "SELECT core.mark_governed_write('agent'); "
            "INSERT INTO core.agent(agent_id,account_id,display_name) VALUES (%s,%s,'Current Core agent'); "
            "SELECT core.mark_governed_write('claim'); "
            "INSERT INTO core.claim(claim_id,account_id,agent_id,change_id,repo,branch,target_path,claim_state) "
            "VALUES (%s,%s,%s,'PR-CURRENT','new-transfer/svc','main','src/current-core.py','active'); SELECT 1",
            (legit_new_account, legit_new_account, "XFER-NEW-N-" + legit_repo_id,
             legit_new_account, legit_repo_id,
             "XFER-NEW-AG-" + legit_repo_id, legit_new_account,
             "XFER-NEW-CL-" + legit_repo_id, legit_new_account, "XFER-NEW-AG-" + legit_repo_id),
        )
        legit_new_boundary_before = as_json(scoped_one(
            "veripsa_migrator", legit_new_account,
            "SELECT jsonb_build_object('activated_at',activated_at,'generation_started_at',generation_started_at) "
            "FROM core.repository_lifecycle_activation WHERE account_id=%s AND repository_id=%s",
            (legit_new_account, legit_repo_id),
        ))
        legit_transfer = call_transfer(
            legit_new_account, legit_old_account, legit_old_repo, legit_repo_id, "D-XFER-LEGIT",
            "found", legit_repo_id, legit_new_id, "new-transfer/svc")
        legit_rows_after = int(scoped_one(
            "veripsa_migrator", legit_old_account,
            "SELECT ((SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.code_node WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.repository_lifecycle_activation WHERE account_id=%s AND repo=%s))::int",
            (legit_old_account, legit_old_repo, legit_old_account, legit_old_repo,
             legit_old_account, legit_old_repo),
        ))
        legit_new_authority_after = int(scoped_one(
            "veripsa_migrator", legit_old_account,
            "SELECT ((SELECT count(*) FROM core.workspace_member WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.grant WHERE grantor_account=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.store_connection WHERE account_id=%s AND provider='github' AND target=%s))::int",
            (legit_old_account, legit_old_repo, legit_old_account, legit_old_repo,
             legit_old_account, legit_old_repo),
        ))
        legit_unversioned_after = int(scoped_one(
            "veripsa_migrator", legit_old_account,
            "SELECT ((SELECT count(*) FROM core.claim WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.co_change WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.co_change_seen_commit WHERE account_id=%s AND repo=%s))::int",
            (legit_old_account, legit_old_repo, legit_old_account, legit_old_repo,
             legit_old_account, legit_old_repo),
        ))
        legit_transfer_marker = as_json(scoped_one(
            "veripsa_migrator", legit_old_account,
            "SELECT jsonb_build_object('reason',reason,'superseded',superseded_at IS NOT NULL) "
            "FROM core.repository_lifecycle_tombstone WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (legit_old_account, legit_repo_id, legit_old_repo),
        ))
        legit_new_activation = as_json(scoped_one(
            "veripsa_migrator", legit_new_account,
            "SELECT jsonb_build_object('id',repository_id,'repo',repo,'authoritative',lifecycle_authoritative) "
            "FROM core.repository_lifecycle_activation WHERE account_id=%s AND repository_id=%s",
            (legit_new_account, legit_repo_id),
        ))
        legit_new_boundary_after = as_json(scoped_one(
            "veripsa_migrator", legit_new_account,
            "SELECT jsonb_build_object('activated_at',activated_at,'generation_started_at',generation_started_at) "
            "FROM core.repository_lifecycle_activation WHERE account_id=%s AND repository_id=%s",
            (legit_new_account, legit_repo_id),
        ))
        legit_new_core_after = int(scoped_one(
            "veripsa_migrator", legit_new_account,
            "SELECT ((SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo='new-transfer/svc' "
            "AND repo_id IS NULL) + (SELECT count(*) FROM core.code_node WHERE account_id=%s "
            "AND repo='new-transfer/svc') + (SELECT count(*) FROM core.claim WHERE account_id=%s "
            "AND repo='new-transfer/svc'))::int",
            (legit_new_account, legit_new_account, legit_new_account),
        ))
        legit_old_event_allowed = scoped_one(
            "veripsa_app", legit_old_account,
            "SELECT core.repository_event_allowed_with_authority(%s,%s)",
            (legit_old_repo, legit_repo_id),
        )
        legit_replay = call_transfer(
            legit_new_account, legit_old_account, legit_old_repo, legit_repo_id, "D-XFER-LEGIT",
            "found", legit_repo_id, legit_new_id, "new-transfer/svc")
        check("legitimate id1 transfer purges only its old coordinate",
              legit_transfer.get("ok") is True and legit_transfer.get("transferred") is True
              and legit_transfer.get("stale_lifecycle_event") is False and legit_rows_after == 0
              and (legit_transfer.get("purged_old") or {}).get("workspace_members") == 0
              and (legit_transfer.get("purged_old") or {}).get("grants") == 0
              and (legit_transfer.get("purged_old") or {}).get("store_connections") == 0
              and (legit_transfer.get("purged_old") or {}).get("webhook_deliveries") == 2
              and legit_new_authority_after == 6 and legit_unversioned_after == 3
              and legit_transfer_marker == {
                  "reason": "repository_transferred", "superseded": True}
              and legit_new_activation == {
                  "id": legit_repo_id, "repo": "new-transfer/svc", "authoritative": True}
              and legit_new_boundary_before.get("generation_started_at") is None
              and legit_new_boundary_after.get("activated_at") == legit_new_boundary_before.get("activated_at")
              and legit_new_boundary_after.get("generation_started_at") == legit_new_boundary_before.get("activated_at")
              and legit_new_core_after == 3 and legit_old_event_allowed is False)
        check("legitimate transfer replay is idempotent from its exact durable completion marker",
              legit_replay.get("ok") is True and legit_replay.get("transferred") is True
              and legit_replay.get("idempotent") is True and legit_rows_after == 0)

        # GitHub explicitly allows a short-name change as part of transfer. The durable former short-name, not the
        # NEW repository.name, must select the old tenant coordinate or private graph residue strands at A/old.
        renamed_old_id, renamed_new_id, renamed_repo_id = "433001", "433002", "433010"
        renamed_old_account = f"ACCT-GH-{renamed_old_id}"
        renamed_new_account = f"ACCT-GH-{renamed_new_id}"
        renamed_old_repo, renamed_new_repo = (
            "rename-transfer-old/legacy-svc", "rename-transfer-new/current-svc")
        provision_transfer_accounts(
            renamed_old_account, renamed_new_account, "inst-xfer-with-rename")
        seed_transfer_coordinate(
            renamed_old_account, renamed_old_repo, renamed_repo_id, "3 hours", "3 hours")
        seed_transfer_delivery(
            "D-XFER-WITH-RENAME",
            transfer_payload(
                renamed_repo_id, "current-svc", renamed_old_id, "rename-transfer-old",
                renamed_new_id, "rename-transfer-new"),
            renamed_new_id,
        )
        renamed_cross_transfer = call_transfer(
            renamed_new_account, renamed_old_account, "rename-transfer-old/current-svc", renamed_repo_id,
            "D-XFER-WITH-RENAME", "found", renamed_repo_id, renamed_new_id, renamed_new_repo)
        renamed_old_rows = int(scoped_one(
            "veripsa_migrator", renamed_old_account,
            "SELECT ((SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.repository_lifecycle_activation WHERE account_id=%s AND repo=%s))::int",
            (renamed_old_account, renamed_old_repo, renamed_old_account, renamed_old_repo),
        ))
        renamed_old_marker = int(scoped_one(
            "veripsa_migrator", renamed_old_account,
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (renamed_old_account, renamed_repo_id, renamed_old_repo),
        ))
        renamed_new_activation = int(scoped_one(
            "veripsa_migrator", renamed_new_account,
            "SELECT count(*)::int FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (renamed_new_account, renamed_repo_id, renamed_new_repo),
        ))
        check("standard transfer payload without former name reverse-resolves and purges A/old-name",
              renamed_cross_transfer.get("outcome") == "purged"
              and renamed_old_rows == 0 and renamed_old_marker == 1
              and renamed_new_activation == 1)

        # Snapshot-race regression: rename owns the stable-id/source fence while transfer starts. Transfer must
        # wait, then either observe the committed coordinate directly or retry a changed candidate snapshot. A
        # clean retry must forget the moved target; ambiguous side-inbox rows intentionally select isolation.
        race_old_id, race_new_id, race_repo_id = "434001", "434002", "434010"
        race_old_account, race_new_account = f"ACCT-GH-{race_old_id}", f"ACCT-GH-{race_new_id}"
        race_old_repo, race_moved_repo = "race-old/svc", "race-old/moved-svc"
        provision_transfer_accounts(race_old_account, race_new_account, "inst-xfer-snapshot-race")
        seed_transfer_coordinate(race_old_account, race_old_repo, race_repo_id, "3 hours", "3 hours")
        seed_transfer_delivery(
            "D-XFER-SNAPSHOT-RACE",
            transfer_payload(race_repo_id, "svc", race_old_id, "race-old", race_new_id, "race-new"),
            race_new_id,
        )
        rename_race_conn = conn_for("veripsa_app")
        probe_ready = threading.Event()
        probe_pid = []
        probe_errors = []
        probe_results = []

        def run_snapshot_probe():
            probe_conn = conn_for("veripsa_app")
            try:
                with probe_conn.cursor() as probe_cur:
                    probe_cur.execute("SET search_path=core")
                    probe_cur.execute(
                        "SELECT set_config('core.installation_account',%s,true)",
                        (race_new_account,),
                    )
                    probe_pid.append(probe_conn.get_backend_pid())
                    probe_ready.set()
                    probe_cur.execute(
                        "SELECT core.transfer_repo_coordinate_with_authority("
                        "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                        (race_old_account, race_old_repo, race_repo_id, "D-XFER-SNAPSHOT-RACE"),
                    )
                    probe_results.append(as_json(probe_cur.fetchone()[0]))
            except psycopg2.Error as exc:
                probe_errors.append(exc.pgcode)
            finally:
                probe_conn.rollback()
                probe_conn.close()

        try:
            with rename_race_conn.cursor() as rename_cur:
                rename_cur.execute("SET search_path=core")
                rename_cur.execute(
                    "SELECT set_config('core.installation_account',%s,true)", (race_old_account,))
                rename_cur.execute(
                    "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
                    (race_old_repo, race_moved_repo),
                )
                probe_thread = threading.Thread(target=run_snapshot_probe, daemon=True)
                probe_thread.start()
                probe_ready.wait(2)
                snapshot_wait_seen = False
                for _ in range(100):
                    if probe_pid and admin(
                        "SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid=%s AND NOT granted)",
                        (probe_pid[0],),
                    ) is True:
                        snapshot_wait_seen = True
                        break
                    time.sleep(0.02)
                if snapshot_wait_seen:
                    rename_race_conn.commit()
                else:
                    rename_race_conn.rollback()
                probe_thread.join(3)
        finally:
            rename_race_conn.close()
        race_retry = call_transfer(
            race_new_account, race_old_account, race_old_repo, race_repo_id,
            "D-XFER-SNAPSHOT-RACE", "found", race_repo_id, race_new_id, "race-new/svc")
        race_residue = int(scoped_one(
            "veripsa_migrator", race_old_account,
            "SELECT count(*)::int FROM core.graph_version WHERE account_id=%s AND repo IN (%s,%s)",
            (race_old_account, race_old_repo, race_moved_repo),
        ))
        check("transfer serializes with an in-flight rename, then safely isolates the moved target "
              f"(wait={snapshot_wait_seen}, errors={probe_errors}, probes={len(probe_results)}, "
              f"outcome={race_retry.get('outcome')}, "
              f"residue={race_residue})",
              snapshot_wait_seen
              and ((probe_errors == ["40001"] and probe_results == [])
                   or (probe_errors == [] and len(probe_results) == 1
                       and probe_results[0].get("proof_required") is True))
              and race_retry.get("outcome") == "isolated" and race_residue == 0)

        # A failed/replayed id1 transfer arrives after old-owner/name is activated and pushed as id2.  Both the
        # different activation id and the graph's newer processing boundary forbid any coordinate-wide delete.
        stale_old_id, stale_new_id = "420001", "420002"
        stale_transfer_id, stale_replacement_id = "420010", "420011"
        stale_old_account, stale_new_account = f"ACCT-GH-{stale_old_id}", f"ACCT-GH-{stale_new_id}"
        stale_old_repo = "old-reuse/svc"
        stale_payload = transfer_payload(
            stale_transfer_id, "svc", stale_old_id, "old-reuse", stale_new_id, "new-reuse")
        provision_transfer_accounts(stale_old_account, stale_new_account, "inst-xfer-stale")
        check("stale transfer fixture has a newer same-name id2 activation and push",
              seed_transfer_delivery("D-XFER-STALE", stale_payload, stale_new_id) == 1
              and seed_transfer_coordinate(
                  stale_old_account, stale_old_repo, stale_replacement_id,
                  "1 minute", "1 minute") == 1)
        stale_transfer = call_transfer(
            stale_new_account, stale_old_account, stale_old_repo, stale_transfer_id, "D-XFER-STALE",
            "found", stale_transfer_id, stale_new_id, "new-reuse/svc")
        stale_identity_after = scoped_one(
            "veripsa_migrator", stale_old_account,
            "SELECT repository_id FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repo=%s",
            (stale_old_account, stale_old_repo),
        )
        stale_graph_after = scoped_one(
            "veripsa_migrator", stale_old_account,
            "SELECT repo_id FROM core.graph_version WHERE account_id=%s AND repo=%s",
            (stale_old_account, stale_old_repo),
        )
        stale_transfer_marker_generation = scoped_one(
            "veripsa_migrator", stale_old_account,
            "SELECT generation_started_at FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (stale_old_account, stale_transfer_id, stale_old_repo),
        )
        check("replayed old transfer isolates id1 and never purges the same-name id2 repository",
              stale_transfer.get("ok") is True and stale_transfer.get("transferred") is True
              and stale_transfer.get("outcome") == "isolated"
              and stale_transfer.get("stale_lifecycle_event") is False
              and stale_identity_after == stale_replacement_id
              and stale_graph_after == stale_replacement_id
              and stale_transfer_marker_generation is None
              and scoped_one(
                  "veripsa_app", stale_old_account,
                  "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (stale_old_repo, stale_transfer_id)) is False
              and scoped_one(
                  "veripsa_app", stale_old_account,
                  "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (stale_old_repo, stale_replacement_id)) is True)

        # A delayed old-owner push may stamp the transferred stable ID after the transfer's receive boundary. The
        # locked live point read still proves that exact ID is currently at NEW, so this is stale ID1 residue to
        # purge—not replacement evidence. Only NULL/different-id state receives fail-closed preservation.
        late_old_id, late_new_id, late_repo_id = "423001", "423002", "423010"
        late_old_account, late_new_account = f"ACCT-GH-{late_old_id}", f"ACCT-GH-{late_new_id}"
        late_old_repo = "late-old/svc"
        provision_transfer_accounts(late_old_account, late_new_account, "inst-xfer-late-old")
        seed_transfer_delivery(
            "D-XFER-LATE-OLD",
            transfer_payload(late_repo_id, "svc", late_old_id, "late-old", late_new_id, "late-new"),
            late_new_id, "3 hours")
        seed_transfer_coordinate(late_old_account, late_old_repo, late_repo_id, "1 hour", "1 hour")
        late_old_transfer = call_transfer(
            late_new_account, late_old_account, late_old_repo, late_repo_id, "D-XFER-LATE-OLD",
            "found", late_repo_id, late_new_id, "late-new/svc")
        late_old_rows_after = int(scoped_one(
            "veripsa_migrator", late_old_account,
            "SELECT ((SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.code_node WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.repository_lifecycle_activation WHERE account_id=%s AND repo=%s))::int",
            (late_old_account, late_old_repo, late_old_account, late_old_repo,
             late_old_account, late_old_repo),
        ))
        check("same-ID old-owner work written after transfer receipt is still purged by current ownership proof",
              late_old_transfer.get("outcome") == "purged" and late_old_rows_after == 0
              and scoped_one(
                  "veripsa_app", late_old_account,
                  "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (late_old_repo, late_repo_id)) is False)

        # Pre-id graphs are not positive old-object evidence.  A delayed transfer may arrive after this same name
        # was reused while the replacement was still represented by a legacy NULL repo_id; preserve it unless an
        # exact same-id activation/graph proves that the coordinate belongs to the transferred object.
        legacy_old_id, legacy_new_owner_id, legacy_transfer_repo_id = "425001", "425002", "425010"
        legacy_old_account = f"ACCT-GH-{legacy_old_id}"
        legacy_new_account = f"ACCT-GH-{legacy_new_owner_id}"
        legacy_old_repo = "legacy-reuse/svc"
        legacy_transfer_payload = transfer_payload(
            legacy_transfer_repo_id, "svc", legacy_old_id, "legacy-reuse",
            legacy_new_owner_id, "legacy-new")
        provision_transfer_accounts(
            legacy_old_account, legacy_new_account, "inst-xfer-legacy-null")
        seed_transfer_delivery(
            "D-XFER-LEGACY-NULL", legacy_transfer_payload, legacy_new_owner_id)
        seed_transfer_coordinate(
            legacy_old_account, legacy_old_repo, legacy_transfer_repo_id, "3 hours", "3 hours")
        scoped_one(
            "veripsa_migrator", legacy_old_account,
            "SELECT core.mark_governed_write('graph_version'); "
            "UPDATE core.graph_version SET repo_id=NULL "
            "WHERE account_id=%s AND repo=%s; "
            "DELETE FROM core.repository_lifecycle_activation WHERE account_id=%s AND repo=%s; "
            "SELECT 1",
            (legacy_old_account, legacy_old_repo, legacy_old_account, legacy_old_repo),
        )
        legacy_null_transfer = call_transfer(
            legacy_new_account, legacy_old_account, legacy_old_repo, legacy_transfer_repo_id,
            "D-XFER-LEGACY-NULL", "found", legacy_transfer_repo_id,
            legacy_new_owner_id, "legacy-new/svc")
        legacy_null_rows_after = int(scoped_one(
            "veripsa_migrator", legacy_old_account,
            "SELECT ((SELECT count(*) FROM core.graph_version WHERE account_id=%s AND repo=%s) + "
            "(SELECT count(*) FROM core.code_node WHERE account_id=%s AND repo=%s))::int",
            (legacy_old_account, legacy_old_repo, legacy_old_account, legacy_old_repo),
        ))
        check("legacy NULL-id same-name coordinate is preserved without positive same-id transfer proof",
              legacy_null_transfer.get("ok") is True
              and legacy_null_transfer.get("outcome") == "isolated"
              and legacy_null_transfer.get("stale_reason") == "newer_or_replacement_graph"
              and legacy_null_rows_after == 2)

        # An exact activation cannot bless a mixed graph coordinate: one rolling legacy NULL-id branch may already
        # be a same-name replacement.  Any branch without the transfer id makes the whole coordinate non-destructive.
        mixed_old_id, mixed_new_id, mixed_repo_id = "426001", "426002", "426010"
        mixed_old_account, mixed_new_account = f"ACCT-GH-{mixed_old_id}", f"ACCT-GH-{mixed_new_id}"
        mixed_old_repo = "mixed-reuse/svc"
        mixed_payload = transfer_payload(
            mixed_repo_id, "svc", mixed_old_id, "mixed-reuse", mixed_new_id, "mixed-new")
        provision_transfer_accounts(mixed_old_account, mixed_new_account, "inst-xfer-mixed-null")
        seed_transfer_delivery("D-XFER-MIXED-NULL", mixed_payload, mixed_new_id)
        seed_transfer_coordinate(mixed_old_account, mixed_old_repo, mixed_repo_id, "3 hours", "3 hours")
        scoped_one(
            "veripsa_migrator", mixed_old_account,
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,%s,'legacy',repeat('b',40),0,0,NULL,clock_timestamp()-interval '3 hours'); SELECT 1",
            (mixed_old_account, mixed_old_repo),
        )
        mixed_transfer = call_transfer(
            mixed_new_account, mixed_old_account, mixed_old_repo, mixed_repo_id, "D-XFER-MIXED-NULL",
            "found", mixed_repo_id, mixed_new_id, "mixed-new/svc")
        mixed_graphs_after = int(scoped_one(
            "veripsa_migrator", mixed_old_account,
            "SELECT count(*)::int FROM core.graph_version WHERE account_id=%s AND repo=%s",
            (mixed_old_account, mixed_old_repo),
        ))
        check("same-id activation never authorizes coordinate purge across a mixed NULL-id graph branch",
              mixed_transfer.get("outcome") == "isolated"
              and mixed_transfer.get("stale_reason") == "newer_or_replacement_graph"
              and mixed_graphs_after == 2)

        # Current GitHub ownership proof closes stable-id returns and rapid onward transfers. A same-id third owner
        # proves the former owner's residue stale, but is never activated inside the delivery's intermediate tenant.
        # Same-new-owner redirects remain safe; an authoritative private/404 result is non-destructive.
        def current_owner_fixture(prefix: str, old_id: str, new_id: str, repo_id: str,
                                  proof_state: str, current_id, current_owner, current_full):
            old_login, new_login = f"{prefix}-old", f"{prefix}-new"
            old_account, new_account = f"ACCT-GH-{old_id}", f"ACCT-GH-{new_id}"
            old_repo = f"{old_login}/svc"
            payload = transfer_payload(repo_id, "svc", old_id, old_login, new_id, new_login)
            provision_transfer_accounts(old_account, new_account, f"inst-xfer-{prefix}")
            seed_transfer_delivery(f"D-XFER-{prefix.upper()}", payload, new_id)
            seed_transfer_coordinate(old_account, old_repo, repo_id, "3 hours", "3 hours")
            result = call_transfer(
                new_account, old_account, old_repo, repo_id, f"D-XFER-{prefix.upper()}",
                proof_state, current_id, current_owner, current_full)
            remaining = int(scoped_one(
                "veripsa_migrator", old_account,
                "SELECT count(*)::int FROM core.graph_version WHERE account_id=%s AND repo=%s",
                (old_account, old_repo),
            ))
            tombstones = int(scoped_one(
                "veripsa_migrator", old_account,
                "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
                "WHERE account_id=%s AND repository_id=%s AND repo=%s",
                (old_account, repo_id, old_repo),
            ))
            new_activations = int(scoped_one(
                "veripsa_migrator", new_account,
                "SELECT count(*)::int FROM core.repository_lifecycle_activation "
                "WHERE account_id=%s AND repository_id=%s AND repo=%s",
                (new_account, repo_id, current_full),
            ))
            return result, remaining, tombstones, new_activations

        returned_transfer, returned_rows, returned_markers, returned_activations = current_owner_fixture(
            "returned", "427001", "427002", "427010",
            "found", "427010", "427001", "returned-old/svc")
        third_transfer, third_rows, third_markers, third_activations = current_owner_fixture(
            "third", "428001", "428002", "428010",
            "found", "428010", "428003", "third-owner/svc")
        absent_transfer, absent_rows, absent_markers, absent_activations = current_owner_fixture(
            "absent", "429001", "429002", "429010",
            "absent", None, None, None)
        renamed_transfer, renamed_rows, renamed_markers, renamed_activations = current_owner_fixture(
            "redirect", "431001", "431002", "431010",
            "found", "431010", "431002", "redirect-new/renamed-svc")
        third_owner_activation_refused = False
        try:
            scoped_one(
                "veripsa_app", "ACCT-GH-428002",
                "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
                ("third-owner/svc", "428010", "D-XFER-THIRD"),
            )
        except psycopg2.Error as exc:
            third_owner_activation_refused = exc.pgcode == "23514"
        check("same stable repository returned to the old owner is a non-destructive stale transfer",
              returned_transfer.get("stale_reason") == "current_owner_changed" and returned_rows == 1
              and returned_markers == 0 and returned_activations == 0)
        check("rapid A-to-B-to-C transfer purges A without activating C inside tenant B",
              third_transfer.get("outcome") == "purged"
              and third_transfer.get("current_account_owns_repository") is False
              and third_rows == 0 and third_markers == 1 and third_activations == 0
              and third_owner_activation_refused)
        check("private/404 current target is non-destructive without inventing manual resolution",
              absent_transfer.get("stale_reason") == "current_target_absent" and absent_rows == 1
              and absent_markers == 0 and absent_activations == 0)
        check("same-new-owner redirect preserves proof and permits old-generation purge",
              renamed_transfer.get("transferred") is True and renamed_rows == 0
              and renamed_markers == 1 and renamed_activations == 1)

        # Full A→B→A lifecycle: the first leg leaves an exact, superseded A tombstone; the reverse transfer creates
        # B's marker and, under its locked current point proof, reactivates A as a fresh generation atomically.
        round_a_id, round_b_id, round_repo_id = "432001", "432002", "432010"
        round_a_account, round_b_account = f"ACCT-GH-{round_a_id}", f"ACCT-GH-{round_b_id}"
        round_a_repo, round_b_repo = "round-a/svc", "round-b/svc"
        provision_transfer_accounts(round_a_account, round_b_account, "inst-xfer-roundtrip")
        seed_transfer_coordinate(round_a_account, round_a_repo, round_repo_id, "4 hours", "4 hours")
        seed_transfer_delivery(
            "D-XFER-ROUND-A-B",
            transfer_payload(round_repo_id, "svc", round_a_id, "round-a", round_b_id, "round-b"),
            round_b_id, "3 hours")
        round_first = call_transfer(
            round_b_account, round_a_account, round_a_repo, round_repo_id, "D-XFER-ROUND-A-B",
            "found", round_repo_id, round_b_id, round_b_repo)
        scoped_one(
            "veripsa_migrator", round_b_account,
            "SELECT core.mark_governed_write('graph_version'); "
            "INSERT INTO core.graph_version(account_id,repo,branch,commit_sha,node_count,edge_count,repo_id,ingested_at) "
            "VALUES (%s,%s,'main',repeat('d',40),0,0,%s,clock_timestamp()-interval '2 hours'); SELECT 1",
            (round_b_account, round_b_repo, round_repo_id),
        )
        seed_transfer_delivery(
            "D-XFER-ROUND-B-A",
            transfer_payload(round_repo_id, "svc", round_b_id, "round-b", round_a_id, "round-a"),
            round_a_id, "1 hour")
        round_return = call_transfer(
            round_a_account, round_b_account, round_b_repo, round_repo_id, "D-XFER-ROUND-B-A",
            "found", round_repo_id, round_a_id, round_a_repo)
        round_a_marker_after = int(scoped_one(
            "veripsa_migrator", round_a_account,
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s",
            (round_a_account, round_repo_id, round_a_repo),
        ))
        round_a_activation_after = int(scoped_one(
            "veripsa_migrator", round_a_account,
            "SELECT count(*)::int FROM core.repository_lifecycle_activation "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s AND lifecycle_authoritative",
            (round_a_account, round_repo_id, round_a_repo),
        ))
        round_b_marker_after = int(scoped_one(
            "veripsa_migrator", round_b_account,
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone "
            "WHERE account_id=%s AND repository_id=%s AND repo=%s AND reason='repository_transferred'",
            (round_b_account, round_repo_id, round_b_repo),
        ))
        check("A-to-B-to-A transfer clears A's old marker, starts a fresh A generation, and isolates B",
              round_first.get("outcome") == "purged" and round_return.get("outcome") == "purged"
              and round_a_marker_after == 0 and round_a_activation_after == 1
              and round_b_marker_after == 1
              and scoped_one(
                  "veripsa_app", round_a_account,
                  "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (round_a_repo, round_repo_id)) is True
              and scoped_one(
                  "veripsa_app", round_b_account,
                  "SELECT core.repository_event_allowed_with_authority(%s,%s)",
                  (round_b_repo, round_repo_id)) is False)

        # Caller-supplied variants cannot redirect this signed delivery to another account/name/id/key.
        forged_transfer_refusals = []
        for forged_args in (
            ("ACCT-GH-999001", legit_old_repo, legit_repo_id, "D-XFER-LEGIT"),
            (legit_old_account, "victim/svc", legit_repo_id, "D-XFER-LEGIT"),
            (legit_old_account, legit_old_repo, "999010", "D-XFER-LEGIT"),
            (legit_old_account, legit_old_repo, legit_repo_id, "D-XFER-MISSING"),
        ):
            try:
                scoped_one(
                    "veripsa_app", legit_new_account,
                    "SELECT core.transfer_repo_coordinate_with_authority("
                    "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                    forged_args,
                )
                forged_transfer_refusals.append(False)
            except psycopg2.Error as exc:
                forged_transfer_refusals.append(exc.pgcode in ("42501", "23514"))
        check("forged transfer account/name/id/key mismatches are all refused before cross-tenant deletion",
              forged_transfer_refusals == [True, True, True, True])

        # Rolling proofless workers cannot bypass the live point read.  Both historical overloads are denied even
        # when one unique processing row would make the omitted durable fields inferable.
        bridge_old_id, bridge_new_id, bridge_repo_id = "430001", "430002", "430010"
        bridge_old_account, bridge_new_account = f"ACCT-GH-{bridge_old_id}", f"ACCT-GH-{bridge_new_id}"
        bridge_old_repo = "bridge-old/svc"
        bridge_payload = transfer_payload(
            bridge_repo_id, "svc", bridge_old_id, "bridge-old", bridge_new_id, "bridge-new")
        provision_transfer_accounts(bridge_old_account, bridge_new_account, "inst-xfer-bridge")
        seed_transfer_delivery("D-XFER-BRIDGE-1", bridge_payload, bridge_new_id)
        seed_transfer_coordinate(
            bridge_new_account, "bridge-new/svc", bridge_repo_id, "3 hours", "3 hours")
        # A probe committed on its own connection cannot authorize finalize in another transaction.
        scoped_one(
            "veripsa_app", bridge_new_account,
            "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
            (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1"),
        )
        split_transaction_refused = False
        try:
            scoped_one(
                "veripsa_app", bridge_new_account,
                "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s,'found',%s,%s,%s)",
                (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1",
                 bridge_repo_id, bridge_new_id, "bridge-new/svc"),
            )
        except psycopg2.Error as exc:
            split_transaction_refused = exc.pgcode == "42501"

        # The probe lock key must be byte-for-byte the live old-owner raw-id + old-full coordinate key.
        probe_conn = conn_for("veripsa_app")
        rename_blocked_by_probe = False
        try:
            with probe_conn.cursor() as probe_cur:
                probe_cur.execute("SET search_path=core")
                probe_cur.execute(
                    "SELECT set_config('core.installation_account',%s,true)", (bridge_new_account,))
                probe_cur.execute(
                    "SELECT core.transfer_repo_coordinate_with_authority("
                    "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                    (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1"),
                )
                probe_lock_blocks_live_key = admin(
                    "SELECT NOT pg_try_advisory_xact_lock(hashtext(%s),hashtext(%s)) "
                    "AND NOT pg_try_advisory_xact_lock(hashtext(%s),hashtext(%s))",
                    (bridge_old_id, bridge_old_repo, bridge_new_id, "bridge-new/svc"),
                )
                # Explicit rename and identity reconciliation share _migrate_repo_coordinate. Its internal SOURCE
                # lock must wait behind the transfer proof; otherwise B/foo could move after the point read but
                # before transfer finalization and invalidate the proof while the wrong coordinate is activated.
                rename_conn = conn_for("veripsa_app")
                try:
                    with rename_conn.cursor() as rename_cur:
                        rename_cur.execute("SET search_path=core")
                        rename_cur.execute(
                            "SELECT set_config('core.installation_account',%s,true)",
                            (bridge_new_account,),
                        )
                        rename_cur.execute("SET LOCAL lock_timeout='150ms'")
                        try:
                            rename_cur.execute(
                                "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
                                ("bridge-new/svc", "bridge-new/proof-race-svc"),
                            )
                        except psycopg2.Error as exc:
                            rename_blocked_by_probe = exc.pgcode == "55P03"
                finally:
                    rename_conn.rollback()
                    rename_conn.close()
        finally:
            probe_conn.rollback()
            probe_conn.close()
        rename_after_probe = None
        rename_after_conn = conn_for("veripsa_app")
        try:
            with rename_after_conn.cursor() as rename_cur:
                rename_cur.execute("SET search_path=core")
                rename_cur.execute(
                    "SELECT set_config('core.installation_account',%s,true)",
                    (bridge_new_account,),
                )
                rename_cur.execute(
                    "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
                    ("bridge-new/svc", "bridge-new/proof-race-svc"),
                )
                rename_after_probe = as_json(rename_cur.fetchone()[0])
        finally:
            # Verify that the operation can proceed after proof release without changing later bridge fixtures.
            rename_after_conn.rollback()
            rename_after_conn.close()
        check("completion probe locks old plus durable-new coordinates and serializes coordinate migration",
              split_transaction_refused and probe_lock_blocks_live_key is True
              and rename_blocked_by_probe
              and rename_after_probe.get("repointed", {}).get("versions") == 1
              and rename_after_probe.get("repointed", {}).get("repository_activations") == 1)

        redirect_unlocked_refused = False
        redirect_unlocked_conn = conn_for("veripsa_app")
        try:
            with redirect_unlocked_conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.installation_account',%s,true)", (bridge_new_account,))
                cur.execute(
                    "SELECT core.transfer_repo_coordinate_with_authority("
                    "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                    (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1"),
                )
                try:
                    cur.execute(
                        "SELECT core.transfer_repo_coordinate_with_authority("
                        "%s,%s,%s,%s,'found',%s,%s,%s)",
                        (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1",
                         bridge_repo_id, bridge_new_id, "bridge-new/renamed-svc"),
                    )
                except psycopg2.Error as exc:
                    redirect_unlocked_refused = exc.pgcode == "42501"
        finally:
            redirect_unlocked_conn.rollback()
            redirect_unlocked_conn.close()

        redirect_lock_conn = conn_for("veripsa_app")
        try:
            with redirect_lock_conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SELECT set_config('core.installation_account',%s,true)", (bridge_new_account,))
                cur.execute(
                    "SELECT core.transfer_repo_coordinate_with_authority("
                    "%s,%s,%s,%s,'probe',NULL,NULL,NULL)",
                    (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1"),
                )
                cur.execute(
                    "SELECT core.transfer_repo_coordinate_with_authority("
                    "%s,%s,%s,%s,'lock_current',%s,%s,%s)",
                    (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1",
                     bridge_repo_id, bridge_new_id, "bridge-new/renamed-svc"),
                )
                redirect_lock_result = as_json(cur.fetchone()[0])
                redirect_lock_blocks_second_session = admin(
                    "SELECT NOT pg_try_advisory_xact_lock(hashtext(%s),hashtext(%s))",
                    (bridge_new_id, "bridge-new/renamed-svc"),
                )
        finally:
            redirect_lock_conn.rollback()
            redirect_lock_conn.close()
        check("redirect finalization requires a bound lock token and blocks a second-session owner move",
              redirect_unlocked_refused
              and redirect_lock_result.get("current_reproof_required") is True
              and redirect_lock_blocks_second_session is True)
        bridge_two_refused = False
        try:
            scoped_one(
                "veripsa_app", bridge_new_account,
                "SELECT core.transfer_repo_coordinate_with_authority(%s,%s)",
                (bridge_old_account, bridge_old_repo),
            )
        except psycopg2.Error as exc:
            bridge_two_refused = exc.pgcode == "42501"
        bridge_four_refused = False
        try:
            scoped_one(
                "veripsa_app", bridge_new_account,
                "SELECT core.transfer_repo_coordinate_with_authority(%s,%s,%s,%s)",
                (bridge_old_account, bridge_old_repo, bridge_repo_id, "D-XFER-BRIDGE-1"),
            )
        except psycopg2.Error as exc:
            bridge_four_refused = exc.pgcode == "42501"
        check("rolling /2 and proofless /4 workers fail closed without live identity proof",
              bridge_two_refused and bridge_four_refused)

        transfer_acl_safe = admin(
            "SELECT has_function_privilege('veripsa_app',"
            "'core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)','EXECUTE') "
            "AND NOT has_function_privilege('veripsa_writer',"
            "'core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)','EXECUTE') "
            "AND NOT has_function_privilege('veripsa_app',"
            "'core.transfer_repo_coordinate_with_authority(text,text)','EXECUTE') "
            "AND NOT has_function_privilege('veripsa_app',"
            "'core.transfer_repo_coordinate_with_authority(text,text,text,text)','EXECUTE') "
            "AND NOT EXISTS (SELECT 1 FROM pg_proc p, "
            "LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) acl "
            "WHERE p.oid='core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)'::regprocedure "
            "AND acl.grantee=0 AND acl.privilege_type='EXECUTE')"
        )
        check("proof-required transfer /8 is App-only while proofless overloads have no App/PUBLIC ACL",
              transfer_acl_safe is True)

        # Account uninstall supersedes and removes per-repository tombstones.
        offboard_current(LIVE_REPO, REPO_ID, "installation_removed")
        admin(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,payload,status,received_at) "
            "VALUES ('D-ACCOUNT-UNINSTALL','installation',%s,"
            "jsonb_build_object('action','deleted','installation',jsonb_build_object("
            "'id',%s,'account',jsonb_build_object('id',%s))),"
            "'processing',clock_timestamp()) RETURNING 1",
            (ACCOUNT, INSTALLATION, ACCOUNT),
        )
        uninstall_proof = json.dumps({
            "state": "absent",
            "deleted_installation_id": INSTALLATION,
            "account_id": ACCOUNT,
        })
        app(
            "SELECT set_config('core.current_delivery_key',%s,false); "
            "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
            ("D-ACCOUNT-UNINSTALL", uninstall_proof),
        )
        remaining_tombstones = int(admin(
            "SELECT count(*)::int FROM core.repository_lifecycle_tombstone WHERE account_id=%s",
            (ACCOUNT,),
        ))
        remaining_activations = int(account_one(
            "SELECT count(*)::int FROM core.repository_lifecycle_activation WHERE account_id=%s",
            (ACCOUNT,),
        ))
        check("account uninstall removes repository lifecycle state",
              remaining_tombstones == 0 and remaining_activations == 0)
    finally:
        subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)

    print()
    if all(ok for _, ok in checks):
        print("REPOSITORY OFFBOARDING GATE: PASS")
        return 0
    print(f"REPOSITORY OFFBOARDING GATE: FAIL ({sum(1 for _, ok in checks if not ok)} failed)")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
