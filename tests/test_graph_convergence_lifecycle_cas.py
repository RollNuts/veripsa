#!/usr/bin/env python3
"""Strict graph convergence lifecycle-generation CAS gate.

Proves against real PostgreSQL that a connection-free clone/extract cannot
repopulate graph state after repository offboard, same-id re-add, replacement,
or rename.  The existing generic graph-writer ABI remains available; only the
authenticated background convergence path uses the stable-id/generation
wrappers.

Run: python3 tests/test_graph_convergence_lifecycle_cas.py
"""
from __future__ import annotations

import atexit
import io
import json
import os
import subprocess
import sys
import tarfile
import threading
import time

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "github-app"))

import code_graph_extract as X  # noqa: E402
import ingest as I  # noqa: E402


DB = "veripsa_graph_lifecycle_cas_" + str(os.getpid())
INSTALLATION = "991337"
ACCOUNT = "ACCT-GH-" + INSTALLATION
BRANCH = "main"

NORMAL_REPO = "acme/cas-normal"
NORMAL_ID = "71001"
RACE_REPO = "acme/cas-race"
RACE_ID = "71002"
TOMBSTONE_REPO = "acme/cas-tombstone"
TOMBSTONE_ID = "71003"
RENAME_OLD = "acme/cas-rename-old"
RENAME_NEW = "acme/cas-rename-new"
RENAME_ID = "71004"
UNINDEXED_OLD = "acme/cas-unindexed-old"
UNINDEXED_NEW = "acme/cas-unindexed-new"
UNINDEXED_ID = "71005"
LEASE_REPO = "acme/cas-lease"
LEASE_ID = "71006"

GRAPH = {
    "extractor_version": X.EXTRACTOR_VERSION,
    "metrics": {"schema_contract_version": X.SCHEMA_CONTRACT_VERSION},
    "nodes": [{
        "id": "app.py",
        "kind": "file",
        "path": "app.py",
        "name": "app.py",
        "language": "python",
    }],
    "edges": [],
}

checks: list[bool] = []
_delivery_seq = 0


def _drop_test_database() -> None:
    subprocess.run(
        ["dropdb", "--if-exists", DB],
        capture_output=True,
        text=True,
    )


def chk(value, label: str) -> None:
    passed = bool(value)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def _obj(value):
    if isinstance(value, (dict, list)):
        return value
    return json.loads(value) if isinstance(value, str) and value else value


def _connect(role: str, *, autocommit: bool = True):
    conn = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    conn.autocommit = autocommit
    return conn


def _app_session():
    conn = _connect("veripsa_app")
    with conn.cursor() as cur:
        cur.execute("SET search_path=core,pg_catalog")
        cur.execute(
            "SELECT core.enter_existing_installation_with_authority(%s)",
            (INSTALLATION,),
        )
        row = cur.fetchone()
        if not row or row[0] != ACCOUNT:
            conn.close()
            raise RuntimeError("test installation route is not live")
    return conn


def app(sql: str, args=()):
    conn = _app_session()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone() if cur.description else None
            return row[0] if row else None
    finally:
        conn.close()


def admin(sql: str, args=()):
    conn = _connect("veripsa_migrator", autocommit=False)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute(
                "SELECT set_config('core.current_account',%s,true)",
                (ACCOUNT,),
            )
            cur.execute(sql, args)
            row = cur.fetchone() if cur.description else None
            return row[0] if row else None
    finally:
        conn.close()


def graph_count(repo: str) -> int:
    return int(admin(
        "SELECT (SELECT count(*) FROM core.graph_version "
        "WHERE account_id=%s AND repo=%s)"
        "+(SELECT count(*) FROM core.code_node "
        "WHERE account_id=%s AND repo=%s)"
        "+(SELECT count(*) FROM core.code_edge "
        "WHERE account_id=%s AND repo=%s)",
        (ACCOUNT, repo, ACCOUNT, repo, ACCOUNT, repo),
    ))


def coordinate(repo: str):
    return _obj(admin(
        "SELECT jsonb_build_object("
        "'commit_sha',commit_sha,'graph_revision',graph_revision,"
        "'node_count',node_count,'edge_count',edge_count,"
        "'graph_hash',graph_hash,'observability',observability) "
        "FROM core.graph_version "
        "WHERE account_id=%s AND repo=%s AND branch=%s",
        (ACCOUNT, repo, BRANCH),
    ))


def capture(repo: str, repository_id: str) -> dict:
    return I._capture_repository_graph_generation(
        app, repo, repository_id)


def full(repo: str, repository_id: str, token: dict, sha: str):
    return _obj(app(
        "SELECT core.ingest_graph_with_authority_for_repository_generation("
        "%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)",
        (json.dumps(GRAPH), repo, BRANCH, sha, None, repository_id,
         json.dumps(token)),
    ))


def patch(repo: str, repository_id: str, token: dict, sha: str):
    base = coordinate(repo)
    subgraph = {
        **GRAPH,
        "expected_base_sha": base["commit_sha"],
        "expected_base_revision": base["graph_revision"],
    }
    return _obj(app(
        "SELECT core.patch_graph_with_authority_for_repository_generation("
        "%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
        (json.dumps(subgraph), repo, BRANCH, ["app.py"], [], sha, None,
         repository_id, json.dumps(token)),
    ))


def expect_generation_refusal(call) -> bool:
    try:
        call()
    except psycopg2.Error as exc:
        return (
            exc.pgcode == "55000"
            and "repository graph lifecycle generation" in str(exc).lower()
        )
    except I.RepositoryGraphGenerationChanged:
        return True
    return False


def seed_delivery(event_type: str, repo: str, repository_id: str) -> str:
    global _delivery_seq
    _delivery_seq += 1
    key = f"CAS-{_delivery_seq}-{event_type}"
    if event_type == "repository":
        payload = {
            "action": "deleted",
            "repository": {"id": repository_id, "full_name": repo},
        }
        delivery_repo = repo
    elif event_type == "installation_repositories":
        payload = {
            "action": "added",
            "repositories_added": [{
                "id": repository_id,
                "full_name": repo,
            }],
        }
        delivery_repo = None
    else:
        raise AssertionError("unsupported lifecycle fixture")
    admin(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,attempts,"
        "received_at,locked_at,lease_generation) "
        "VALUES (%s,%s,%s,%s,%s::jsonb,'processing',1,"
        "clock_timestamp(),clock_timestamp(),1)",
        (key, event_type, INSTALLATION, delivery_repo, json.dumps(payload)),
    )
    return key


def offboard(repo: str, repository_id: str):
    key = seed_delivery("repository", repo, repository_id)
    result = _obj(app(
        "SELECT core.offboard_repository_with_authority(%s,%s,%s,%s)",
        (repo, repository_id, "repository_deleted", key),
    ))
    if not isinstance(result, dict) or result.get("revoked") is not True:
        raise AssertionError(f"offboard did not revoke fixture: {result!r}")
    return result


def readd(repo: str, repository_id: str):
    # Ensure durable receive ordering is unambiguous even on coarse clocks.
    time.sleep(0.01)
    key = seed_delivery(
        "installation_repositories", repo, repository_id)
    result = _obj(app(
        "SELECT core.reactivate_repository_with_authority(%s,%s,%s)",
        (repo, repository_id, key),
    ))
    if not isinstance(result, dict) or result.get("activated") is not True:
        raise AssertionError(f"re-add did not activate fixture: {result!r}")
    return result


def tarball() -> bytes:
    body = b"def live():\n    return 1\n"
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        member = tarfile.TarInfo("repo/app.py")
        member.size = len(body)
        tf.addfile(member, io.BytesIO(body))
    return out.getvalue()


class BlockingGH:
    def __init__(self):
        self.download_entered = threading.Event()
        self.release_download = threading.Event()

    def download_tarball(self, repo, sha):
        self.download_entered.set()
        if not self.release_download.wait(10):
            raise RuntimeError("test did not release graph download")
        return tarball()


def main() -> int:
    atexit.register(_drop_test_database)
    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0:
        print("bootstrap failed:\n" + (boot.stdout + boot.stderr)[-3000:])
        return 1

    # Provision one real installation tenant. Every later background-like DB
    # call reconnects, enters the existing route, executes one statement, and
    # closes — the production convergence connection discipline.
    provision = _connect("veripsa_app")
    try:
        with provision.cursor() as cur:
            cur.execute("SET search_path=core,pg_catalog")
            cur.execute(
                "SELECT core.enter_installation_with_authority(%s)",
                (INSTALLATION,),
            )
            chk(cur.fetchone()[0] == ACCOUNT, "test tenant is routed through the real installation authority")
    finally:
        provision.close()

    writer_abi_compatible = admin(
        "SELECT "
        "has_function_privilege("
        "'veripsa_writer',"
        "'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',"
        "'EXECUTE') "
        "AND has_function_privilege("
        "'veripsa_writer',"
        "'core.patch_graph_with_authority("
        "jsonb,text,text,text[],text[],text,timestamptz)',"
        "'EXECUTE') "
        "AND NOT has_function_privilege("
        "'veripsa_writer',"
        "'core.ingest_graph_with_authority_for_repository_generation("
        "jsonb,text,text,text,timestamptz,text,jsonb)',"
        "'EXECUTE') "
        "AND NOT has_function_privilege("
        "'veripsa_writer',"
        "'core.patch_graph_with_authority_for_repository_generation("
        "jsonb,text,text,text[],text[],text,timestamptz,text,jsonb)',"
        "'EXECUTE')"
    )
    chk(
        writer_abi_compatible,
        "legacy writer ABI/grants remain intact while lifecycle wrappers stay App-only",
    )

    # Normal full + patch retain the generic writer's result shapes. The
    # wrapper name also preserves _FreshAccountDB's graph-bulk-loaded detector,
    # while the SQL writer preserves the underlying session stats flag.
    normal_token = capture(NORMAL_REPO, NORMAL_ID)
    conn = _app_session()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT core.ingest_graph_with_authority_for_repository_generation("
                "%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb)",
                (json.dumps(GRAPH), NORMAL_REPO, BRANCH, "1" * 40, None,
                 NORMAL_ID, json.dumps(normal_token)),
            )
            normal_full = _obj(cur.fetchone()[0])
            cur.execute(
                "SELECT current_setting('core.graph_bulk_loaded',true)")
            bulk_loaded = cur.fetchone()[0]
    finally:
        conn.close()
    chk(
        normal_full.get("ok") is True
        and normal_full.get("nodes") == 1
        and normal_full.get("edges") == 0
        and len(normal_full.get("graph_hash", "")) == 64
        and isinstance(normal_full.get("observability"), dict),
        "generation-guarded full write preserves graph result/hash/observability shape",
    )
    chk(
        bulk_loaded == "1" and coordinate(NORMAL_REPO)["commit_sha"] == "1" * 40,
        "generation wrapper preserves the full-ingest graph-stats signal and coordinate",
    )
    normal_patch = patch(
        NORMAL_REPO, NORMAL_ID, normal_token, "2" * 40)
    chk(
        normal_patch.get("ok") is True
        and normal_patch.get("mode") == "patch"
        and normal_patch.get("nodes_total") == 1
        and normal_patch.get("edges_total") == 0
        and len(normal_patch.get("graph_hash", "")) == 64
        and isinstance(normal_patch.get("observability"), dict)
        and coordinate(NORMAL_REPO)["commit_sha"] == "2" * 40,
        "generation-guarded patch preserves totals/hash/observability and advances the coordinate",
    )

    # A caller cannot pair a captured token with another stable id.
    before_wrong_id = coordinate(NORMAL_REPO)["commit_sha"]
    wrong_id_refused = expect_generation_refusal(
        lambda: full(NORMAL_REPO, "79999", normal_token, "3" * 40))
    chk(
        wrong_id_refused
        and coordinate(NORMAL_REPO)["commit_sha"] == before_wrong_id,
        "wrong stable repository id is refused before any graph mutation",
    )

    # Quota is still evaluated by the existing generic writer and returned
    # byte-for-shape through the wrapper; the stored graph is untouched.
    old_limit = int(admin(
        "SELECT core._plan_graph_units_limit('free')"))
    app(
        "SELECT core.set_plan_graph_units_limit_with_authority('free',0)")
    quota_result = full(
        NORMAL_REPO, NORMAL_ID, normal_token, "4" * 40)
    app(
        "SELECT core.set_plan_graph_units_limit_with_authority('free',%s)",
        (old_limit,),
    )
    chk(
        quota_result.get("ok") is False
        and quota_result.get("quota_exceeded") is True
        and quota_result.get("dimension") == "graph_units"
        and quota_result.get("limit") == 0
        and isinstance(quota_result.get("usage"), int)
        and coordinate(NORMAL_REPO)["commit_sha"] == before_wrong_id,
        "generation wrapper preserves quota refusal shape and leaves the graph unchanged",
    )

    # An active tombstone wins even when the caller still has the exact old
    # token. No version/node/edge is recreated.
    tombstone_token = capture(TOMBSTONE_REPO, TOMBSTONE_ID)
    offboard(TOMBSTONE_REPO, TOMBSTONE_ID)
    tombstone_refused = expect_generation_refusal(
        lambda: full(
            TOMBSTONE_REPO, TOMBSTONE_ID, tombstone_token, "5" * 40))
    chk(
        tombstone_refused and graph_count(TOMBSTONE_REPO) == 0,
        "active repository tombstone refuses the old generation with zero graph residue",
    )

    # A rename moves the activation token's coordinate. A late patch under the
    # old name cannot recreate the old graph coordinate.
    rename_token = capture(RENAME_OLD, RENAME_ID)
    renamed = _obj(app(
        "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
        (RENAME_OLD, RENAME_NEW),
    ))
    rename_patch_refused = expect_generation_refusal(
        lambda: app(
            "SELECT core.patch_graph_with_authority_for_repository_generation("
            "%s::jsonb,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (json.dumps({
                **GRAPH,
                "expected_base_sha": "0" * 40,
                "expected_base_revision": 1,
            }), RENAME_OLD, BRANCH, ["app.py"], [], "6" * 40, None,
             RENAME_ID, json.dumps(rename_token)),
        ))
    chk(
        renamed.get("ok") is True
        and rename_patch_refused
        and graph_count(RENAME_OLD) == 0,
        "rename invalidates the old generation before patch DELETE/INSERT",
    )

    # _store_unindexed_graph reaches the exact same full-write interception.
    # It succeeds while current, then a rename invalidates the old proxy and no
    # deterministic-empty graph can resurrect the old name.
    unindexed_token = capture(UNINDEXED_OLD, UNINDEXED_ID)
    unindexed_db = I._RepositoryGenerationGraphDB(
        app, UNINDEXED_OLD, UNINDEXED_ID, unindexed_token)
    unindexed = I._store_unindexed_graph(
        unindexed_db,
        UNINDEXED_OLD,
        BRANCH,
        "7" * 40,
        None,
        reason="source_file_count_cap",
        source_files=123,
    )
    app(
        "SELECT core.rename_repo_coordinate_with_authority(%s,%s)",
        (UNINDEXED_OLD, UNINDEXED_NEW),
    )
    try:
        I._store_unindexed_graph(
            unindexed_db,
            UNINDEXED_OLD,
            BRANCH,
            "8" * 40,
            None,
            reason="source_file_count_cap",
            source_files=123,
        )
        stale_unindexed_refused = False
    except I.RepositoryGraphGenerationChanged:
        stale_unindexed_refused = True
    chk(
        unindexed.get("not_indexed") is True
        and unindexed.get("files") == 0
        and stale_unindexed_refused
        and graph_count(UNINDEXED_OLD) == 0
        and coordinate(UNINDEXED_NEW)["commit_sha"] == "7" * 40,
        "_store_unindexed full writer is generation-guarded and cannot recreate a renamed coordinate",
    )

    # The decisive ABA race: capture old generation, enter clone/download, and
    # complete offboard→same repo/id re-add while extraction is still blocked.
    # Lifecycle completes without waiting, proving capture left no DB
    # connection/lock behind. Final full persistence then rejects the old token.
    race_token = capture(RACE_REPO, RACE_ID)
    race_db = I._RepositoryGenerationGraphDB(
        app, RACE_REPO, RACE_ID, race_token)
    gh = BlockingGH()
    race_state = {"error": None, "result": None}
    original_builder = I._GRAPH_EXTRACTOR.build_graph

    def tiny_graph(_root, universe_paths=None):
        return {
            "nodes": [dict(GRAPH["nodes"][0])],
            "edges": [],
        }

    def run_extract():
        try:
            race_state["result"] = I._full_ingest(
                race_db, gh, RACE_REPO, BRANCH, "9" * 40)
        except Exception as exc:  # asserted below
            race_state["error"] = exc

    I._GRAPH_EXTRACTOR.build_graph = tiny_graph
    worker = threading.Thread(target=run_extract, daemon=True)
    worker.start()
    entered = gh.download_entered.wait(5)
    lifecycle_started = time.monotonic()
    offboard(RACE_REPO, RACE_ID)
    readd(RACE_REPO, RACE_ID)
    lifecycle_elapsed = time.monotonic() - lifecycle_started
    app_backends_during_clone = int(admin(
        "SELECT count(*)::int FROM pg_stat_activity "
        "WHERE datname=%s AND usename='veripsa_app'",
        (DB,),
    ))
    generation_reused = admin(
        "SELECT activated_at=%s::timestamptz "
        "FROM core.repository_lifecycle_activation "
        "WHERE account_id=%s AND repository_id=%s AND repo=%s",
        (race_token["activated_at"], ACCOUNT, RACE_ID, RACE_REPO),
    )
    lifecycle_completed_while_blocked = (
        entered
        and worker.is_alive()
        and not gh.release_download.is_set()
        and lifecycle_elapsed < 5
    )
    gh.release_download.set()
    worker.join(10)
    I._GRAPH_EXTRACTOR.build_graph = original_builder
    chk(
        lifecycle_completed_while_blocked
        and app_backends_during_clone == 0,
        "offboard and same-id re-add complete during blocked clone with no App DB backend/lock retained",
    )
    chk(
        not worker.is_alive()
        and isinstance(
            race_state["error"], I.RepositoryGraphGenerationChanged)
        and race_state["result"] is None
        and generation_reused is False
        and graph_count(RACE_REPO) == 0,
        "old extraction loses exact activated_at/generation CAS after offboard→same-id re-add and writes zero graph rows",
    )

    # The repository-generation token is necessary but not sufficient: an
    # extractor that wakes after its 300s slot was reclaimed still names the
    # same lifecycle activation. Bind the final writer to the exact
    # request/slot/lease and reject it in the mutation transaction.
    lease_token = capture(LEASE_REPO, LEASE_ID)
    lease_sha = "c" * 40
    lease_epoch = int(app(
        "SELECT core.enqueue_graph_refresh_with_authority(%s,%s,%s,%s)",
        (LEASE_REPO, BRANCH, lease_sha, LEASE_ID),
    ))
    old_turn = _obj(app(
        "SELECT core.claim_policy_refresh_with_authority(%s,5,300,8,true,1)",
        ("cas-old-worker",),
    ))
    lease_db = I._RepositoryGenerationGraphDB(
        app, LEASE_REPO, LEASE_ID, lease_token,
        convergence_lease={
            "request_epoch": old_turn["request_epoch"],
            "slot": old_turn["graph_slot"],
            "lease_epoch": old_turn["lease_epoch"],
        },
    )
    lease_db(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
        (json.dumps(GRAPH), LEASE_REPO, BRANCH, lease_sha, None),
    )
    before_stale_write = coordinate(LEASE_REPO)
    admin(
        "SELECT core.mark_governed_write('graph_convergence_lease'); "
        "UPDATE core.graph_convergence_lease SET claimed_until=clock_timestamp()-interval '1 second' "
        "WHERE account_id=%s AND repository_id=%s",
        (ACCOUNT, LEASE_ID),
    )
    admin("SELECT core._sync_graph_claim_router(%s,300)", (ACCOUNT,))
    admin("SELECT core._sync_account_convergence_due(%s,5,300,false)", (ACCOUNT,))
    immediate_replacement = _obj(app(
        "SELECT core.claim_policy_refresh_with_authority(%s,5,300,8,true,1)",
        ("cas-new-worker-before-backoff",),
    ))
    abandoned = admin(
        "SELECT attempts=1 AND last_error='turn_abandoned' AND not_before>now() "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (ACCOUNT, LEASE_ID),
    )
    admin(
        "SELECT core.mark_governed_write('policy_refresh_outbox'); "
        "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s "
        "AND attempts=1 AND last_error='turn_abandoned'; "
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (ACCOUNT, LEASE_ID, ACCOUNT),
    )
    replacement_turn = _obj(app(
        "SELECT core.claim_policy_refresh_with_authority(%s,5,300,8,true,1)",
        ("cas-new-worker-after-backoff",),
    ))
    try:
        lease_db(
            "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
            (json.dumps(GRAPH), LEASE_REPO, BRANCH, lease_sha, None),
        )
        stale_lease_refused = False
    except I.RepositoryGraphGenerationChanged:
        stale_lease_refused = True
    chk(
        lease_epoch == int(old_turn["request_epoch"])
        and immediate_replacement is None
        and abandoned is True
        and int(replacement_turn["lease_epoch"]) != int(old_turn["lease_epoch"])
        and stale_lease_refused
        and coordinate(LEASE_REPO) == before_stale_write,
        "expired exact graph token is refused inside the final writer after slot reclaim",
    )

    # Static reachability at the proxy boundary: both direct graph SQL calls
    # are replaced, while _store_unindexed uses the same full call.
    recorded: list[str] = []

    def recording_db(sql, args=()):
        recorded.append(sql)
        return {"ok": False, "quota_exceeded": True}

    proxy = I._RepositoryGenerationGraphDB(
        recording_db, NORMAL_REPO, NORMAL_ID, normal_token)
    proxy(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
        ("{}", NORMAL_REPO, BRANCH, "a" * 40, None),
    )
    proxy(
        "SELECT core.patch_graph_with_authority(%s,%s,%s,%s,%s,%s,%s)",
        ("{}", NORMAL_REPO, BRANCH, ["app.py"], [], "b" * 40, None),
    )
    chk(
        "ingest_graph_with_authority_for_repository_generation" in recorded[0]
        and "patch_graph_with_authority_for_repository_generation" in recorded[1],
        "strict DB proxy redirects every full/unindexed and patch persistence call to lifecycle CAS wrappers",
    )
    recorded.clear()
    exact_proxy = I._RepositoryGenerationGraphDB(
        recording_db, NORMAL_REPO, NORMAL_ID, normal_token,
        convergence_lease={"request_epoch": 7, "slot": 1, "lease_epoch": 9})
    exact_proxy(
        "SELECT core.ingest_graph_with_authority(%s,%s,%s,%s,%s)",
        ("{}", NORMAL_REPO, BRANCH, "a" * 40, None),
    )
    chk(
        "ingest_graph_with_authority_for_convergence_lease" in recorded[0],
        "production strict DB proxy routes final persistence through the exact lease wrapper",
    )

    ok = all(checks)
    print("GRAPH CONVERGENCE LIFECYCLE CAS:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
