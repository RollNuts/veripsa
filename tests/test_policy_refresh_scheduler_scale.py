#!/usr/bin/env python3
"""Account-convergence scheduler scale and lease-fence gate.

This is intentionally a real PostgreSQL gate.  It covers failure modes that a
small fixture cannot expose:

* the oldest due account may sit beyond the former 5,000-account scan window;
* global selection must use bounded policy/graph lane probes, never sort every
  installation route;
* a repository-heavy tenant must enqueue linearly rather than rescan its queue;
* an account has exactly one graph slot even when a caller requests more, a
  later tenant can immediately use the other worker, an expired slot has a new
  lease generation, and a late terminal callback is fenced;
* repository offboarding cancels pending and claimed graph work, including the
  slot snapshot and mutable repository authority.
"""
from __future__ import annotations

import json
import os
import subprocess
import time

import psycopg2


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "veripsa_policy_scale_" + str(os.getpid())
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
MIG_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
checks: list[bool] = []


def check(ok: bool, label: str) -> None:
    passed = bool(ok)
    checks.append(passed)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)


def one(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone() if cur.description else None
            return row[0] if row else None
    finally:
        conn.close()


def rows(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            return cur.fetchall()
    finally:
        conn.close()


def scoped_rows(account: str, sql: str, args=()):
    """Read/write FORCE-RLS tenant state with the account pin in the same transaction."""
    conn = psycopg2.connect(MIG_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT set_config('core.current_account',%s,true)",
                (account,),
            )
            cur.execute(sql, args)
            return cur.fetchall() if cur.description else []
    finally:
        conn.close()


def scoped_governed(account: str, table: str, sql: str, args=()) -> None:
    conn = psycopg2.connect(MIG_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT set_config('core.current_account',%s,true)",
                (account,),
            )
            cur.execute("SELECT core.mark_governed_write(%s)", (table,))
            cur.execute(sql, args)
    finally:
        conn.close()


def as_json(value):
    if value is None or isinstance(value, dict):
        return value
    return json.loads(value)


def provision(owner_id: str) -> str:
    return str(one(
        APP_DSN,
        "SELECT core.enter_installation_with_authority(%s)",
        (owner_id,),
    ))


def enqueue_graph(
    owner_id: str,
    repo: str,
    repository_id: str,
    sha: str,
) -> int:
    conn = psycopg2.connect(APP_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (owner_id,),
            )
            cur.execute(
                "SELECT core.enqueue_graph_refresh_with_authority(%s,'main',%s,%s)",
                (repo, sha, repository_id),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def wake_graph(owner_id: str, repo: str, repository_id: str, sha: str) -> int:
    conn = psycopg2.connect(APP_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (owner_id,),
            )
            cur.execute(
                "SELECT core.wake_graph_refresh_candidate_with_authority("
                "%s,'main',%s,%s)",
                (repo, sha, repository_id),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def graph_lifecycle_refused(call) -> bool:
    try:
        call()
    except psycopg2.Error as exc:
        return (
            exc.pgcode == "55000"
            and "graph refresh lifecycle coordinate is not live"
            in str(exc).lower()
        )
    return False


def claim(worker: str, *, slots: int = 1, max_attempts: int = 5, stale: int = 300):
    return as_json(one(
        APP_DSN,
        "SELECT core.claim_policy_refresh_with_authority(%s,%s,%s,8,true,%s)",
        (worker, max_attempts, stale, slots),
    ))


def claim_current(worker: str, *, slots: int = 1, max_attempts: int = 5, stale: int = 300):
    return as_json(one(
        APP_DSN,
        "SELECT core.claim_policy_refresh_with_authority(%s,%s,%s,8,true,%s,true)",
        (worker, max_attempts, stale, slots),
    ))


def graph_terminal_args(turn: dict) -> tuple:
    return (
        turn["account_id"],
        turn["request_kind"],
        turn["repository_id"],
        turn["branch"],
        int(turn["request_epoch"]),
        int(turn["graph_slot"]),
        int(turn["lease_epoch"]),
    )


def reset_queue() -> None:
    """Fast isolated-fixture reset; this database exists only for this gate."""
    one(
        MIG_DSN,
        "TRUNCATE core.graph_convergence_lease,core.policy_refresh_outbox",
    )
    one(
        MIG_DSN,
        "UPDATE core.installation_account SET "
        "policy_refresh_due_at=NULL,graph_refresh_due_at=NULL,legacy_graph_refresh_due_at=NULL,"
        "convergence_claimed_until=NULL,convergence_claimed_by=NULL,"
        "convergence_claim_epoch=NULL,convergence_graph_claim_count=0,"
        "convergence_graph_reclaim_at=NULL,convergence_pending_count=0,"
        "convergence_retry_exhausted_count=0,"
        "convergence_quota_deferred_count=0,"
        "convergence_stall_started_at=NULL",
    )


def pr_wake_and_stall_age_are_monotonic() -> None:
    reset_queue()
    account = provision("92500")
    repo = "wake/current"
    repository_id = "92501"
    current_sha = "b" * 40
    old_pr_base = "a" * 40
    first_epoch = enqueue_graph("92500", repo, repository_id, current_sha)
    turn = claim("wake-active")
    scoped_governed(
        account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox "
        "SET enqueued_at=now()-interval '10 minutes' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account, repository_id),
    )
    scoped_rows(
        account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (account,),
    )
    before = scoped_rows(
        account,
        "SELECT target_sha,policy_epoch,attempts,policy_cursor_repo,"
        "policy_cursor_branch,change_cursor,claimed_at,claimed_by,"
        "(SELECT convergence_stall_started_at FROM core.installation_account "
        " WHERE account_id=%s),surface_dirty "
        "FROM core.policy_refresh_outbox WHERE account_id=%s "
        "AND request_kind='graph' AND repository_id=%s",
        (account, account, repository_id),
    )[0]
    lease_before = scoped_rows(
        account,
        "SELECT request_epoch,lease_epoch,target_sha,claimed_until "
        "FROM core.graph_convergence_lease WHERE account_id=%s",
        (account,),
    )[0]
    wake_epoch = wake_graph("92500", repo, repository_id, old_pr_base)
    after_wake = scoped_rows(
        account,
        "SELECT target_sha,policy_epoch,attempts,policy_cursor_repo,"
        "policy_cursor_branch,change_cursor,claimed_at,claimed_by,"
        "(SELECT convergence_stall_started_at FROM core.installation_account "
        " WHERE account_id=%s),surface_dirty "
        "FROM core.policy_refresh_outbox WHERE account_id=%s "
        "AND request_kind='graph' AND repository_id=%s",
        (account, account, repository_id),
    )[0]
    lease_after = scoped_rows(
        account,
        "SELECT request_epoch,lease_epoch,target_sha,claimed_until "
        "FROM core.graph_convergence_lease WHERE account_id=%s",
        (account,),
    )[0]
    check(
        turn and first_epoch == wake_epoch
        and before[:-1] == after_wake[:-1]
        and before[-1] is False and after_wake[-1] is True
        and lease_before == lease_after
        and after_wake[0] == current_sha,
        "delayed PR wake coalesces only surface dirtiness while target, epoch, lease, attempts, and cursor stay byte-stable",
    )

    # A genuinely authoritative newer push may supersede the desired target,
    # but uninterrupted unfinished work keeps the original incident age.
    next_epoch = enqueue_graph("92500", repo, repository_id, "c" * 40)
    stall_after_push = one(
        MIG_DSN,
        "SELECT extract(epoch FROM now()-convergence_stall_started_at)::int "
        "FROM core.installation_account WHERE account_id=%s",
        (account,),
    )
    depth = as_json(one(
        APP_DSN, "SELECT core.account_convergence_depth_with_authority()"))
    check(
        next_epoch > first_epoch
        and int(stall_after_push) >= 590
        and int(depth.get("oldest_age_seconds") or 0) >= 590
        and int(depth.get("stalled_accounts") or 0) >= 1,
        "latest-target supersede preserves original stall age and the global indexed alert sees it while claimed",
    )


def scheduler_plan_sql() -> str:
    """The exact bounded candidate shape used by the v2 claim function."""
    return """
      EXPLAIN (ANALYZE,BUFFERS,COSTS OFF)
      WITH policy_candidate AS MATERIALIZED (
        SELECT account_id,installation_id,policy_refresh_due_at AS due_at
          FROM core.installation_account
         WHERE revoked_at IS NULL
           AND policy_refresh_due_at<=statement_timestamp()
           AND (convergence_graph_claim_count=0
                OR convergence_graph_reclaim_at<statement_timestamp())
           AND (convergence_claimed_until IS NULL
                OR convergence_claimed_until<statement_timestamp())
         ORDER BY policy_refresh_due_at,account_id
         LIMIT 1 FOR UPDATE SKIP LOCKED
      ), graph_candidate AS MATERIALIZED (
        SELECT account_id,installation_id,graph_refresh_due_at AS due_at
          FROM core.installation_account
         WHERE revoked_at IS NULL
           AND graph_refresh_due_at<=statement_timestamp()
           AND (convergence_graph_claim_count=0
                OR convergence_graph_reclaim_at<statement_timestamp())
           AND (convergence_claimed_until IS NULL
                OR convergence_claimed_until<statement_timestamp())
         ORDER BY graph_refresh_due_at,account_id
         LIMIT 1 FOR UPDATE SKIP LOCKED
      ), candidates AS MATERIALIZED (
        SELECT * FROM policy_candidate
        UNION ALL SELECT * FROM graph_candidate
      )
      SELECT account_id,installation_id
        FROM candidates
       ORDER BY due_at,account_id
       LIMIT 1
    """


def rolling_two_slot_cutover_requeues_second() -> None:
    """A live predecessor's slot 2 loses authority and returns to the queue at schema cutover."""
    reset_queue()
    account = provision("91900")
    enqueue_graph("91900", "rolling/one", "91901", "1" * 40)
    enqueue_graph("91900", "rolling/two", "91902", "2" * 40)
    first = claim("rolling-slot-one")

    # Recreate the exact predecessor shape: the old token-domain CHECK allowed slot 2 and the router counted both
    # leases. The desired outbox row remains unclaimed, as it did under lease protocol v2.
    one(
        MIG_DSN,
        "ALTER TABLE core.graph_convergence_lease "
        "DROP CONSTRAINT graph_convergence_lease_account_cap_one",
    )
    conn = psycopg2.connect(MIG_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT set_config('core.current_account',%s,true)",
                (account,),
            )
            cur.execute(
                "SELECT repo,branch,target_sha,policy_epoch "
                "FROM core.policy_refresh_outbox "
                "WHERE account_id=%s AND request_kind='graph' "
                "AND repository_id='91902'",
                (account,),
            )
            repo, branch, target_sha, request_epoch = cur.fetchone()
            cur.execute(
                "SELECT core._next_account_convergence_epoch(%s)",
                (account,),
            )
            lease_epoch = int(cur.fetchone()[0])
            cur.execute("SELECT core.mark_governed_write('graph_convergence_lease')")
            cur.execute(
                "INSERT INTO core.graph_convergence_lease("
                "account_id,slot,lease_epoch,request_epoch,repository_id,"
                "repo,branch,target_sha,claimed_by,claimed_until) "
                "VALUES (%s,2,%s,%s,'91902',%s,%s,%s,"
                "'rolling-old-worker',clock_timestamp()+interval '5 minutes')",
                (
                    account, lease_epoch, int(request_epoch),
                    repo, branch, target_sha,
                ),
            )
            cur.execute("SELECT core._sync_graph_claim_router(%s,300)", (account,))
            cur.execute(
                "SELECT core._sync_account_convergence_due(%s,5,300,false)",
                (account,),
            )
    finally:
        conn.close()

    legacy_slot_two = {
        "account_id": account,
        "request_kind": "graph",
        "repository_id": "91902",
        "branch": branch,
        "request_epoch": int(request_epoch),
        "graph_slot": 2,
        "lease_epoch": lease_epoch,
    }
    before_count = int(one(
        MIG_DSN,
        "SELECT convergence_graph_claim_count "
        "FROM core.installation_account WHERE account_id=%s",
        (account,),
    ))
    reapplied = subprocess.run(
        [
            "psql", "-v", "ON_ERROR_STOP=1", "-d", DB,
            "-f", os.path.join(ROOT, "db", "schema.sql"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if reapplied.returncode != 0:
        print("    rolling schema reapply stderr:\n" + reapplied.stderr[-3000:])

    after = scoped_rows(
        account,
        "SELECT "
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=%s AND slot=2),"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=%s AND slot=1),"
        "convergence_graph_claim_count,"
        "(SELECT count(*) FROM core.policy_refresh_outbox "
        " WHERE account_id=%s AND request_kind='graph' "
        " AND repository_id='91902' AND claimed_at IS NULL AND done_at IS NULL),"
        "(SELECT convalidated FROM pg_constraint "
        " WHERE conrelid='core.graph_convergence_lease'::regclass "
        " AND conname='graph_convergence_lease_account_cap_one') "
        "FROM core.installation_account WHERE account_id=%s",
        (account, account, account, account),
    )[0]
    late_finish = as_json(one(
        APP_DSN,
        "SELECT core.finish_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(legacy_slot_two),
    ))
    still_blocked = claim("rolling-same-account")
    first_failed = fail_graph(first, "rolling_slot_one_release")
    replacement = claim("rolling-requeued-slot-two")
    check(
        reapplied.returncode == 0
        and before_count == 2
        and after == (0, 1, 1, 1, True)
        and late_finish.get("lease_lost") is True
        and still_blocked is None
        and first_failed == 1
        and replacement
        and replacement["repository_id"] == "91902"
        and int(replacement["graph_slot"]) == 1
        and int(replacement["lease_epoch"]) > lease_epoch,
        "rolling two-slot state: cutover fences slot 2, preserves its pending fact, and reclaims it only as slot 1",
    )
    if replacement:
        fail_graph(replacement, "rolling_fixture_release")


def oldest_beyond_5000_and_indexed() -> None:
    created = int(one(
        APP_DSN,
        "SELECT count(core.enter_installation_with_authority("
        "to_char(g,'FM00000'))) FROM generate_series(1,5001) g",
    ))
    oldest = "ACCT-GH-05001"
    one(MIG_DSN, "SELECT core._enqueue_policy_refresh(%s)", (oldest,))
    enqueued = int(one(
        MIG_DSN,
        "SELECT count(core._enqueue_policy_refresh("
        "'ACCT-GH-'||to_char(g,'FM00000'))) "
        "FROM generate_series(1,5000) g",
    ))
    one(MIG_DSN, "ANALYZE core.installation_account")

    plan = "\n".join(str(r[0]) for r in rows(MIG_DSN, scheduler_plan_sql()))
    started = time.perf_counter()
    turn = claim("scale-oldest")
    claim_seconds = time.perf_counter() - started
    check(
        created == 5001 and enqueued == 5000
        and turn and turn["account_id"] == oldest,
        "5,001 live tenants: the true oldest account beyond the former 5,000-row window is claimed",
    )
    check(
        "policy_refresh_account_policy_due" in plan
        and "policy_refresh_account_graph_due" in plan
        and "Seq Scan on installation_account" not in plan,
        "global claim probes policy and single-slot graph lanes through bounded due indexes (no tenant scan)",
    )
    check(
        "Index Cond: (policy_refresh_due_at <= statement_timestamp())" in plan
        and "Index Cond: (graph_refresh_due_at <= statement_timestamp())" in plan,
        "idle/future-only polls keep both due cutoffs as index range conditions, never volatile full-index filters",
    )
    scoped_governed(
        oldest,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox "
        "SET enqueued_at=now()-interval '20 minutes' "
        "WHERE account_id=%s AND request_kind='policy'",
        (oldest,),
    )
    scoped_rows(
        oldest,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (oldest,),
    )
    depth = as_json(one(
        APP_DSN, "SELECT core.account_convergence_depth_with_authority()"))
    stall_plan = "\n".join(str(r[0]) for r in rows(
        MIG_DSN,
        "EXPLAIN (COSTS OFF) SELECT convergence_stall_started_at "
        "FROM core.installation_account "
        "WHERE revoked_at IS NULL AND convergence_stall_started_at IS NOT NULL "
        "ORDER BY convergence_stall_started_at,account_id LIMIT 1001",
    ))
    check(
        int(depth.get("oldest_age_seconds") or 0) >= 1190
        and depth.get("stalled_truncated") is True
        and "policy_refresh_account_stall_started" in stall_plan,
        "global oldest stall remains exact beyond 5,001 tenants via the ordered partial index",
    )
    print(f"    measured claim wall: {claim_seconds:.4f}s for 5,001 queued tenants")


def enqueue_fat_account_is_linear() -> None:
    reset_queue()
    account = provision("90000")

    def batch(first: int, last: int) -> float:
        conn = psycopg2.connect(APP_DSN)
        try:
            started = time.perf_counter()
            with conn, conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.enter_existing_installation_with_authority('90000')"
                )
                cur.execute(
                    "SELECT count(core.enqueue_graph_refresh_with_authority("
                    "'fat/repo-'||g,'main',lpad(to_hex(g),40,'a'),"
                    "(100000+g)::text)) FROM generate_series(%s,%s) g",
                    (first, last),
                )
                assert int(cur.fetchone()[0]) == last - first + 1
            return time.perf_counter() - started
        finally:
            conn.close()

    first_n, second_n = 300, 600
    first_wall = batch(1, first_n)
    second_wall = batch(first_n + 1, first_n + second_n)
    per_first = first_wall / first_n
    per_second = second_wall / second_n
    state = rows(
        MIG_DSN,
        "SELECT convergence_pending_count,convergence_next_epoch "
        "FROM core.installation_account WHERE account_id=%s",
        (account,),
    )[0]
    source = str(one(
        MIG_DSN,
        "SELECT pg_get_functiondef("
        "'core.enqueue_graph_refresh_with_authority(text,text,text,text)'"
        "::regprocedure)",
    ))
    check(
        state[0] == first_n + second_n
        and state[1] >= first_n + second_n,
        "repository-heavy tenant keeps exact O(1)-delta counters and monotonic epochs for 900 distinct repos",
    )
    check(
        per_second <= per_first * 3.5 + 0.002
        and "max(policy_epoch)" not in source.lower()
        and "for v_account in" not in source.lower(),
        "successive fat-account enqueue batches remain linear and contain no account/outbox aggregate loop",
    )

    # Put 850 older rows onto the poison-isolated slow lane. The runnable head must still be one ordered
    # hot-partial-index lookup, not a sort/scan of this tenant's deferred history on every turn.
    scoped_governed(
        account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox "
        "SET attempts=5,not_before=now()+interval '1 hour' "
        "WHERE account_id=%s AND request_kind='graph' "
        "AND repository_id::bigint<100851",
        (account,),
    )
    one(
        MIG_DSN,
        "UPDATE core.installation_account "
        "SET convergence_pending_count=50,convergence_retry_exhausted_count=850 "
        "WHERE account_id=%s",
        (account,),
    )
    scoped_rows(
        account,
        "SELECT core._sync_account_convergence_due(%s,1,300,false)",
        (account,),
    )
    one(MIG_DSN, "ANALYZE core.policy_refresh_outbox")
    fat_plan = "\n".join(str(r[0]) for r in rows(
        MIG_DSN,
        "EXPLAIN (ANALYZE,BUFFERS,COSTS OFF) "
        "SELECT repository_id FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND done_at IS NULL "
        "AND attempts<5 AND claimed_at IS NULL "
        "AND COALESCE(not_before,enqueued_at)<=statement_timestamp() "
        "ORDER BY COALESCE(not_before,enqueued_at),repository_id "
        "LIMIT 1 FOR UPDATE SKIP LOCKED",
        (account,),
    ))
    claim_started = time.perf_counter()
    runnable = claim("fat-runnable")
    fat_claim_wall = time.perf_counter() - claim_started
    check(
        "policy_refresh_outbox_claimable_due" in fat_plan
        and "Seq Scan on policy_refresh_outbox" not in fat_plan
        and "Sort" not in fat_plan
        and runnable and runnable["repository_id"] == "100851",
        "fat tenant claim skips 850 slow-retry rows through the hot runnable index without a per-turn sort",
    )
    if runnable:
        fail_graph(runnable, "fat_fixture_release")
    print(
        "    measured enqueue: "
        f"{first_n}={first_wall:.4f}s ({per_first * 1000:.3f}ms/repo), "
        f"{second_n}={second_wall:.4f}s ({per_second * 1000:.3f}ms/repo), "
        f"claim={fat_claim_wall:.4f}s"
    )


def graph_slot_expiry_fences_late_worker() -> None:
    reset_queue()
    account = provision("92000")
    enqueue_graph("92000", "slot/one", "92001", "1" * 40)
    enqueue_graph("92000", "slot/two", "92002", "2" * 40)
    first = claim("slot-first", slots=99)
    same_account_second = claim("slot-second", slots=99)
    router_count = int(one(
        MIG_DSN,
        "SELECT convergence_graph_claim_count "
        "FROM core.installation_account WHERE account_id=%s",
        (account,),
    ))
    cap_constraint = str(one(
        MIG_DSN,
        "SELECT regexp_replace(pg_get_constraintdef(oid),'[[:space:]()]','','g') "
        "FROM pg_constraint "
        "WHERE conrelid='core.graph_convergence_lease'::regclass "
        "AND conname='graph_convergence_lease_account_cap_one' "
        "AND contype='c' AND convalidated",
    ))
    check(
        first and same_account_second is None
        and first["account_id"] == account
        and int(first["graph_slot"]) == 1
        and router_count == 1
        and cap_constraint == "CHECKslot=1",
        "caller slots=99 cannot raise the exact-one SQL/table/router account cap",
    )

    scoped_governed(
        account,
        "graph_convergence_lease",
        "UPDATE core.graph_convergence_lease "
        "SET claimed_until=now()-interval '1 second' "
        "WHERE account_id=%s AND slot=%s::smallint AND lease_epoch=%s",
        (account, int(first["graph_slot"]), int(first["lease_epoch"])),
    )
    one(
        MIG_DSN,
        "UPDATE core.installation_account "
        "SET convergence_graph_reclaim_at=now()-interval '1 second',"
        "graph_refresh_due_at=now()-interval '1 second' "
        "WHERE account_id=%s",
        (account,),
    )
    late_current = one(
        APP_DSN,
        "SELECT core.policy_refresh_turn_is_current_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(first),
    )
    late_finish = as_json(one(
        APP_DSN,
        "SELECT core.finish_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(first),
    ))
    late_page = as_json(one(
        APP_DSN,
        "SELECT core.requeue_graph_refresh_page_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,'PR-EXPIRED')",
        (
            first["account_id"], first["repository_id"], first["branch"],
            int(first["request_epoch"]), int(first["graph_slot"]),
            int(first["lease_epoch"]),
        ),
    ))
    late_fail = fail_graph(first, "expired_must_not_fail")
    late_defer = as_json(one(
        APP_DSN,
        "SELECT core.defer_graph_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,'quota_paused',900)",
        (
            first["account_id"], first["repository_id"], first["branch"],
            int(first["request_epoch"]), int(first["graph_slot"]),
            int(first["lease_epoch"]),
        ),
    ))
    untouched = scoped_rows(
        account,
        "SELECT attempts,change_cursor,"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=%s AND slot=%s::smallint AND lease_epoch=%s) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (
            account, int(first["graph_slot"]), int(first["lease_epoch"]),
            account, first["repository_id"],
        ),
    )[0]
    immediate_replacement = claim_current("slot-sibling-during-backoff")
    after_abandon = scoped_rows(
        account,
        "SELECT attempts,last_error,not_before>now(),"
        "(SELECT count(*) FROM core.graph_convergence_lease WHERE account_id=%s) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account, account, first["repository_id"]),
    )[0]
    if immediate_replacement:
        fail_graph(immediate_replacement, "slot_sibling_fixture_release")
    scoped_governed(
        account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s "
        "AND attempts=1 AND last_error='turn_abandoned'",
        (account, first["repository_id"]),
    )
    scoped_rows(
        account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (account,),
    )
    after_advance = scoped_rows(
        account,
        "SELECT q.repository_id,q.attempts,q.not_before<=now(),q.onboarding_pending,"
        "a.graph_refresh_due_at<=now(),a.legacy_graph_refresh_due_at<=now(),"
        "a.convergence_graph_claim_count,a.convergence_graph_reclaim_at "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account a USING (account_id) "
        "WHERE q.account_id=%s AND q.request_kind='graph' AND q.done_at IS NULL "
        "ORDER BY q.repository_id",
        (account,),
    )
    replacement = claim_current("slot-replacement-after-backoff")
    print(
        "    lease reclaim diagnostic: "
        f"old={first.get('lease_epoch')} immediate="
        f"{immediate_replacement.get('lease_epoch') if immediate_replacement else None} replacement="
        f"{replacement.get('lease_epoch') if replacement else None} "
        f"late_current={late_current!r} late_finish={late_finish!r} "
        f"after_abandon={after_abandon!r} after_advance={after_advance!r}"
    )
    check(
        late_current is False
        and late_finish.get("lease_lost") is True
        and late_page.get("lease_lost") is True
        and late_fail == -1
        and late_defer.get("lease_lost") is True
        and untouched == (0, "", 1),
        "an expired worker cannot page, finish, fail, defer, or mutate the request before replacement",
    )
    check(
        immediate_replacement
        and immediate_replacement["repository_id"] == "92002"
        and after_abandon == (1, "turn_abandoned", True, 1)
        and replacement
        and int(replacement["lease_epoch"]) > int(first["lease_epoch"])
        and replacement["repository_id"] == first["repository_id"],
        "expired slot backs off the abandoned repository, lets its sibling progress, then reclaims under a newer lease",
    )


def fail_graph(turn: dict, error: str) -> int:
    return int(one(
        APP_DSN,
        "SELECT core.fail_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s,%s,20)",
        (*graph_terminal_args(turn), error),
    ))


def requeue_graph_page(turn: dict, cursor: str) -> dict:
    return as_json(one(
        APP_DSN,
        "SELECT core.requeue_graph_refresh_page_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,%s)",
        (
            turn["account_id"], turn["repository_id"], turn["branch"],
            int(turn["request_epoch"]), int(turn["graph_slot"]),
            int(turn["lease_epoch"]), cursor,
        ),
    ))


def finish_graph(turn: dict) -> dict:
    return as_json(one(
        APP_DSN,
        "SELECT core.finish_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(turn),
    ))


def defer_graph(turn: dict, delay_seconds: int = 60) -> dict:
    return as_json(one(
        APP_DSN,
        "SELECT core.defer_graph_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s::smallint,%s,'quota_paused',%s)",
        (
            turn["account_id"], turn["repository_id"], turn["branch"],
            int(turn["request_epoch"]), int(turn["graph_slot"]),
            int(turn["lease_epoch"]), delay_seconds,
        ),
    ))


def graph_external_fence(owner_id: str, turn: dict) -> bool:
    conn = psycopg2.connect(APP_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (owner_id,),
            )
            cur.execute(
                "SELECT core.policy_refresh_external_write_fence_with_authority("
                "%s,%s,%s,%s,%s,%s::smallint,%s,1)",
                graph_terminal_args(turn),
            )
            return bool(cur.fetchone()[0])
    finally:
        conn.close()


def persist_claimed_graph(owner_id: str, turn: dict) -> tuple[object, dict]:
    """Exercise the production lifecycle+lease writer and stable-id restamp."""
    graph = {
        "nodes": [{
            "id": "quota/reprobe:fixture.py",
            "kind": "file",
            "path": "fixture.py",
            "name": "fixture.py",
        }],
        "edges": [],
        "metrics": {"schema_contract_version": 2},
    }
    conn = psycopg2.connect(APP_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (owner_id,),
            )
            cur.execute(
                "SELECT core.capture_repository_graph_generation_with_authority(%s,%s)",
                (turn["repo"], turn["repository_id"]),
            )
            generation = as_json(cur.fetchone()[0])
            cur.execute("SELECT core.current_extractor_version()")
            graph["extractor_version"] = cur.fetchone()[0]
            cur.execute(
                "SELECT core.ingest_graph_with_authority_for_convergence_lease("
                "%s::jsonb,%s,%s,%s,NULL,%s,%s::jsonb,%s,%s::smallint,%s)",
                (
                    json.dumps(graph), turn["repo"], turn["branch"], turn["target_sha"],
                    turn["repository_id"], json.dumps(generation),
                    int(turn["request_epoch"]), int(turn["graph_slot"]),
                    int(turn["lease_epoch"]),
                ),
            )
            persisted = cur.fetchone()[0]
            cur.execute(
                "SELECT core.reconcile_repo_identity_with_authority(%s,%s)",
                (turn["repo"], turn["repository_id"]),
            )
            reconciled = as_json(cur.fetchone()[0])
            return persisted, reconciled
    finally:
        conn.close()


def graph_surface_dirty_restarts_only_after_paginated_tail() -> None:
    """A neighbor behind a moved cursor is recovered without starving the tail."""
    reset_queue()
    owner_id = "92600"
    account = provision(owner_id)
    repo = "surface/paginated"
    repository_id = "92601"
    target_sha = "e" * 40
    seed_repo_activation(account, repo, repository_id)
    request_epoch = enqueue_graph(owner_id, repo, repository_id, target_sha)

    first = claim("surface-page-1")
    persisted, reconciled = persist_claimed_graph(owner_id, first)
    first_page = requeue_graph_page(first, "PR-30")
    second = claim("surface-page-2")
    before_wake = scoped_rows(
        account,
        "SELECT q.target_sha,q.policy_epoch,q.change_cursor,q.surface_dirty,"
        "q.attempts,q.terminal_reason,q.not_before,"
        "l.request_epoch,l.lease_epoch,l.target_sha "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.graph_convergence_lease l ON l.account_id=q.account_id "
        "AND l.repository_id=q.repository_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]
    first_wake_epoch = wake_graph(
        owner_id, repo, repository_id, "a" * 40)
    second_wake_epoch = wake_graph(
        owner_id, repo, repository_id, "b" * 40)
    after_wake = scoped_rows(
        account,
        "SELECT q.target_sha,q.policy_epoch,q.change_cursor,q.surface_dirty,"
        "q.attempts,q.terminal_reason,q.not_before,"
        "l.request_epoch,l.lease_epoch,l.target_sha "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.graph_convergence_lease l ON l.account_id=q.account_id "
        "AND l.repository_id=q.repository_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]

    second_page = requeue_graph_page(second, "PR-60")
    third = claim("surface-tail")
    dirty_mid_tail = scoped_rows(
        account,
        "SELECT change_cursor,surface_dirty FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account, repository_id),
    )[0]
    dirty_finish = finish_graph(third)
    rearmed_state = scoped_rows(
        account,
        "SELECT q.done_at IS NULL,q.change_cursor,q.surface_dirty,q.attempts,"
        "q.terminal_reason,q.not_before IS NULL,"
        "r.convergence_pending_count,r.convergence_graph_claim_count,"
        "r.graph_refresh_due_at IS NOT NULL,"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=r.account_id) "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]
    full_pass = claim("surface-full-pass")
    final_finish = finish_graph(full_pass)
    final_state = scoped_rows(
        account,
        "SELECT q.done_at IS NOT NULL,q.change_cursor,q.surface_dirty,"
        "r.convergence_pending_count,r.convergence_graph_claim_count,"
        "r.graph_refresh_due_at IS NULL "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]

    check(
        first and int(first["request_epoch"]) == request_epoch
        and persisted is not None and reconciled.get("ok") is True
        and first_page.get("requeued") is True
        and second and second["change_cursor"] == "PR-30"
        and before_wake[3] is False
        and first_wake_epoch == second_wake_epoch == request_epoch
        and after_wake[:3] == before_wake[:3]
        and after_wake[3] is True
        and after_wake[4:] == before_wake[4:],
        "mid-page PR wakes coalesce without changing target, epoch, lease, cursor, retry, or quota authority",
    )
    check(
        second_page.get("requeued") is True
        and third and third["change_cursor"] == "PR-60"
        and dirty_mid_tail == ("PR-60", True)
        and dirty_finish.get("finished") is True
        and dirty_finish.get("surface_rearmed") is True
        and dirty_finish.get("graph_fulfilled") is True
        and rearmed_state == (True, "", False, 0, None, True, 1, 0, True, 0),
        "dirty pagination completes its current tail then atomically re-arms one full pass from the beginning",
    )
    check(
        full_pass and full_pass["change_cursor"] == ""
        and final_finish.get("finished") is True
        and final_finish.get("surface_rearmed") is False
        and final_state == (True, "", False, 0, 0, True),
        "the coalesced full pass drains exactly once with no cursor or counter residue",
    )


def quota_defer_autonomously_reprobes_without_synthetic_failure() -> None:
    """A due quota row keeps honest quota state while a new exact lease retries it."""
    reset_queue()
    owner_id = "92200"
    repo = "quota/reprobe"
    repository_id = "92201"
    target_sha = "d" * 40
    account = provision(owner_id)
    seed_repo_activation(account, repo, repository_id)
    request_epoch = enqueue_graph(owner_id, repo, repository_id, target_sha)

    first = claim("quota-first")
    first_defer = defer_graph(first)
    first_state = scoped_rows(
        account,
        "SELECT q.attempts,q.terminal_reason,q.claimed_at IS NULL,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count,r.convergence_graph_claim_count,"
        "r.convergence_stall_started_at IS NULL,"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=r.account_id) "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]
    stale_fence = graph_external_fence(owner_id, first)
    quota_before_wake = scoped_rows(
        account,
        "SELECT q.target_sha,q.policy_epoch,q.change_cursor,q.surface_dirty,"
        "q.attempts,q.terminal_reason,q.not_before,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]
    quota_wake_epoch = wake_graph(
        owner_id, repo, repository_id, "a" * 40)
    quota_after_wake = scoped_rows(
        account,
        "SELECT q.target_sha,q.policy_epoch,q.change_cursor,q.surface_dirty,"
        "q.attempts,q.terminal_reason,q.not_before,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]

    # Advance only the durable scheduler clock. No new push, epoch, or
    # operator enqueue is needed for the quota probe to become claimable.
    scoped_governed(
        account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account, repository_id),
    )
    scoped_rows(
        account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (account,),
    )
    second = claim("quota-second")
    second_fence = graph_external_fence(owner_id, second)
    second_state = scoped_rows(
        account,
        "SELECT q.policy_epoch,q.attempts,q.terminal_reason,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count,r.convergence_graph_claim_count,"
        "r.convergence_stall_started_at IS NULL "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]
    second_defer = defer_graph(second)
    repeated_state = scoped_rows(
        account,
        "SELECT q.attempts,q.terminal_reason,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count,r.convergence_graph_claim_count "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (account, repository_id),
    )[0]

    scoped_governed(
        account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id=%s",
        (account, repository_id),
    )
    scoped_rows(
        account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (account,),
    )
    third = claim("quota-third")
    persisted, reconciled = persist_claimed_graph(owner_id, third)
    finished = as_json(one(
        APP_DSN,
        "SELECT core.finish_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(third),
    ))
    final_state = scoped_rows(
        account,
        "SELECT q.done_at IS NOT NULL,q.attempts,q.terminal_reason,"
        "r.convergence_pending_count,r.convergence_retry_exhausted_count,"
        "r.convergence_quota_deferred_count,r.convergence_graph_claim_count,"
        "r.convergence_stall_started_at IS NULL,r.graph_refresh_due_at IS NULL,"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        " WHERE account_id=r.account_id),"
        "(SELECT count(*) FROM core.graph_version "
        " WHERE account_id=r.account_id AND repo=%s AND branch='main' "
        " AND repo_id=%s AND commit_sha=%s) "
        "FROM core.policy_refresh_outbox q "
        "JOIN core.installation_account r ON r.account_id=q.account_id "
        "WHERE q.account_id=%s AND q.request_kind='graph' "
        "AND q.repository_id=%s",
        (repo, repository_id, target_sha, account, repository_id),
    )[0]

    check(
        first and int(first["request_epoch"]) == request_epoch
        and first_defer.get("deferred") is True
        and first_state == (0, "quota_paused", True, 0, 0, 1, 0, True, 0)
        and quota_wake_epoch == request_epoch
        and quota_before_wake[3] is False and quota_after_wake[3] is True
        and quota_before_wake[:3] == quota_after_wake[:3]
        and quota_before_wake[4:] == quota_after_wake[4:]
        and stale_fence is False,
        "quota defer preserves its wall/counters across a dirty wake, stays attempts-neutral, and an old lease cannot write",
    )
    check(
        second
        and int(second["request_epoch"]) == request_epoch
        and int(second["lease_epoch"]) > int(first["lease_epoch"])
        and second_fence is True
        and second_state == (request_epoch, 0, "quota_paused", 0, 0, 1, 1, True)
        and second_defer.get("deferred") is True
        and repeated_state == (0, "quota_paused", 0, 0, 1, 0),
        "time-advanced quota work reclaims under a new exact lease and can re-defer without a synthetic failure or counter drift",
    )
    check(
        third
        and int(third["request_epoch"]) == request_epoch
        and int(third["lease_epoch"]) > int(second["lease_epoch"])
        and persisted is not None
        and reconciled.get("ok") is True
        and finished.get("finished") is True
        and finished.get("graph_fulfilled") is True
        and final_state == (True, 0, None, 0, 0, 0, 0, True, True, 0, 1),
        "a later autonomous quota probe persists through the exact writer and drains quota/lease/alert state with attempts still zero",
    )


def late_account_gets_reserved_worker_capacity() -> None:
    reset_queue()
    account_a = provision("92300")
    enqueue_graph("92300", "fair/a-oldest", "92301", "1" * 40)
    enqueue_graph("92300", "fair/a-second", "92302", "2" * 40)

    first = claim("tier-first")
    same_account_blocked = claim("tier-same-account")

    # B does not exist when A gets its first turn. Arriving afterwards must still use worker two immediately.
    account_b = provision("92310")
    time.sleep(0.01)
    enqueue_graph("92310", "fair/b-late", "92311", "3" * 40)
    late_tenant = claim("tier-late-tenant")

    first_failed = fail_graph(first, "tier_a1_release") if first else -1
    a_tail = claim("tier-a-tail")
    check(
        first and same_account_blocked is None and late_tenant and a_tail
        and first["account_id"] == account_a
        and late_tenant["account_id"] == account_b
        and a_tail["account_id"] == account_a
        and first["repository_id"] == "92301"
        and late_tenant["repository_id"] == "92311"
        and a_tail["repository_id"] == "92302"
        and first_failed == 1,
        "A1→none, late B→B1, finish A1→A2: one tenant never consumes both workers",
    )
    if late_tenant:
        fail_graph(late_tenant, "tier_b_release")
    if a_tail:
        fail_graph(a_tail, "tier_a2_release")


def exhausted_graph_recovers_without_starving_neighbor() -> None:
    """Five transient failures isolate, but never permanently strand, one exact graph fact."""
    reset_queue()
    poison_account = provision("92400")
    healthy_account = provision("92410")
    enqueue_graph("92400", "retry/poison", "92401", "8" * 40)

    failure_counts = []
    for attempt in range(1, 6):
        turn = claim(f"retry-poison-{attempt}")
        if not turn:
            break
        failure_counts.append(fail_graph(turn, "transient_upstream"))
        if attempt < 5:
            scoped_governed(
                poison_account,
                "policy_refresh_outbox",
                "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
                "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
                (poison_account,),
            )
            scoped_rows(
                poison_account,
                "SELECT core._sync_account_convergence_due(%s,5,20,false)",
                (poison_account,),
            )

    isolated = scoped_rows(
        poison_account,
        "SELECT attempts,"
        "extract(epoch FROM not_before-now())::int,"
        "(SELECT convergence_retry_exhausted_count "
        " FROM core.installation_account WHERE account_id=%s) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
        (poison_account, poison_account),
    )[0]

    # A later healthy tenant uses the other/global head while poison is cooling down.
    enqueue_graph("92410", "retry/healthy", "92411", "9" * 40)
    healthy = claim("retry-healthy")
    if healthy:
        fail_graph(healthy, "fixture_release")

    # Advance only the durable clock field. The same exact request becomes claimable automatically; no new push,
    # policy write, operator mutation, or epoch rewrite is needed.
    scoped_governed(
        poison_account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox SET not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
        (poison_account,),
    )
    scoped_rows(
        poison_account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (poison_account,),
    )
    recovered = claim("retry-auto-rearm")
    sixth = fail_graph(recovered, "still_transient") if recovered else -1
    slow_again = scoped_rows(
        poison_account,
        "SELECT attempts,extract(epoch FROM not_before-now())::int,"
        "(SELECT convergence_retry_exhausted_count "
        " FROM core.installation_account WHERE account_id=%s) "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
        (poison_account, poison_account),
    )[0]
    check(
        failure_counts == [1, 2, 3, 4, 5]
        and isolated[0] == 5 and 290 <= isolated[1] <= 305 and isolated[2] == 1
        and healthy and healthy["account_id"] == healthy_account
        and recovered and recovered["account_id"] == poison_account
        and recovered["repository_id"] == "92401"
        and sixth == 6
        and slow_again[0] == 6 and 590 <= slow_again[1] <= 605
        and slow_again[2] == 1,
        "attempt 5 enters bounded slow retry, another tenant proceeds, and the exact fact auto-rearms",
    )

    # The exponential slow lane has a hard one-hour ceiling even after an indefinitely bad coordinate.
    scoped_governed(
        poison_account,
        "policy_refresh_outbox",
        "UPDATE core.policy_refresh_outbox "
        "SET attempts=8,not_before=now()-interval '1 second' "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
        (poison_account,),
    )
    scoped_rows(
        poison_account,
        "SELECT core._sync_account_convergence_due(%s,5,300,false)",
        (poison_account,),
    )
    capped_turn = claim("retry-cap")
    capped_attempt = fail_graph(capped_turn, "deterministic_poison") if capped_turn else -1
    capped_delay = scoped_rows(
        poison_account,
        "SELECT extract(epoch FROM not_before-now())::int "
        "FROM core.policy_refresh_outbox "
        "WHERE account_id=%s AND request_kind='graph' AND repository_id='92401'",
        (poison_account,),
    )[0][0]
    check(
        capped_attempt == 9 and 3590 <= capped_delay <= 3605,
        "poison-row retry backoff is capped at one globally-fair turn per hour",
    )


def continuous_graph_cannot_starve_policy() -> None:
    reset_queue()
    account = provision("92500")
    enqueue_graph("92500", "fair/active-one", "92501", "4" * 40)
    first = claim("fair-active-one")
    same_account_blocked = claim("fair-active-two")
    policy_epoch = int(one(
        MIG_DSN,
        "SELECT core._enqueue_policy_refresh(%s)",
        (account,),
    ))
    enqueue_graph("92500", "fair/active-two", "92502", "5" * 40)
    enqueue_graph("92500", "fair/new-three", "92503", "6" * 40)
    enqueue_graph("92500", "fair/new-four", "92504", "7" * 40)

    while_active = claim("fair-policy-waits")
    first_failed = fail_graph(first, "fair_release_one")
    next_turn = claim("fair-policy-next")
    check(
        same_account_blocked is None
        and while_active is None
        and first_failed == 1
        and next_turn
        and next_turn["account_id"] == account
        and next_turn["request_kind"] == "policy"
        and int(next_turn["request_epoch"]) == policy_epoch,
        "an older policy turn waits for the sole graph lease, then wins before a continuous stream of newer repos",
    )


def seed_repo_activation(account: str, repo: str, repository_id: str) -> None:
    conn = psycopg2.connect(MIG_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT set_config('core.current_account',%s,true)",
                (account,),
            )
            cur.execute(
                "INSERT INTO core.repository_lifecycle_activation("
                "account_id,repository_id,repo,lifecycle_authoritative,"
                "generation_started_at) VALUES (%s,%s,%s,true,clock_timestamp()) "
                "ON CONFLICT (account_id,repository_id) DO UPDATE SET "
                "repo=EXCLUDED.repo,lifecycle_authoritative=true",
                (account, repository_id, repo),
            )
    finally:
        conn.close()


def offboard(
    owner_id: str,
    account: str,
    repo: str,
    repository_id: str,
    delivery_key: str,
) -> dict:
    payload = {
        "action": "deleted",
        "repository": {"id": repository_id, "full_name": repo},
    }
    one(
        MIG_DSN,
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,attempts,"
        "received_at,locked_at) "
        "VALUES (%s,'repository',%s,%s,%s::jsonb,'processing',1,"
        "clock_timestamp(),clock_timestamp())",
        (delivery_key, account, repo, json.dumps(payload)),
    )
    conn = psycopg2.connect(APP_DSN)
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "SELECT core.enter_existing_installation_with_authority(%s)",
                (owner_id,),
            )
            cur.execute(
                "SELECT core.offboard_repository_with_authority("
                "%s,%s,'repository_deleted',%s)",
                (repo, repository_id, delivery_key),
            )
            return as_json(cur.fetchone()[0])
    finally:
        conn.close()


def offboard_cancels_pending_and_claimed() -> None:
    reset_queue()

    pending_account = provision("91000")
    seed_repo_activation(pending_account, "offboard/pending", "91001")
    enqueue_graph("91000", "offboard/pending", "91001", "a" * 40)
    pending_result = offboard(
        "91000", pending_account, "offboard/pending", "91001",
        "D-SCALE-OFFBOARD-PENDING",
    )
    # The queue cleanup and the later enqueue are deliberately separate App
    # transactions.  Before the central lifecycle fence this recreated the
    # just-deleted graph row after offboard committed and then retried it
    # forever.  Both ordinary latest-wins enqueue and the PR wake early-return
    # path must now refuse the exact tombstoned stable object.
    post_offboard_enqueue_refused = graph_lifecycle_refused(
        lambda: enqueue_graph(
            "91000", "offboard/pending", "91001", "c" * 40))
    post_offboard_wake_refused = graph_lifecycle_refused(
        lambda: wake_graph(
            "91000", "offboard/pending", "91001", "d" * 40))
    pending_state = scoped_rows(
        pending_account,
        "SELECT "
        "(SELECT count(*) FROM core.policy_refresh_outbox "
        "  WHERE account_id=%s AND repository_id='91001'),"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        "  WHERE account_id=%s AND repository_id='91001'),"
        "(SELECT count(*) FROM core.repository_lifecycle_activation "
        "  WHERE account_id=%s AND repository_id='91001'),"
        "convergence_pending_count,graph_refresh_due_at IS NULL "
        "FROM core.installation_account WHERE account_id=%s",
        (pending_account, pending_account, pending_account, pending_account),
    )[0]
    check(
        pending_result.get("ok") is True
        and post_offboard_enqueue_refused
        and post_offboard_wake_refused
        and pending_state == (0, 0, 0, 0, True),
        "authoritative offboard atomically prevents post-commit enqueue/wake resurrection and leaves no queue, lease, authority, counter, or due residue",
    )

    claimed_account = provision("91010")
    seed_repo_activation(claimed_account, "offboard/claimed", "91011")
    enqueue_graph("91010", "offboard/claimed", "91011", "b" * 40)
    turn = claim("offboard-claimed")
    claimed_result = offboard(
        "91010", claimed_account, "offboard/claimed", "91011",
        "D-SCALE-OFFBOARD-CLAIMED",
    )
    late_finish = as_json(one(
        APP_DSN,
        "SELECT core.finish_policy_refresh_turn_with_authority("
        "%s,%s,%s,%s,%s,%s::smallint,%s)",
        graph_terminal_args(turn),
    ))
    claimed_state = scoped_rows(
        claimed_account,
        "SELECT "
        "(SELECT count(*) FROM core.policy_refresh_outbox "
        "  WHERE account_id=%s AND repository_id='91011'),"
        "(SELECT count(*) FROM core.graph_convergence_lease "
        "  WHERE account_id=%s AND repository_id='91011'),"
        "(SELECT count(*) FROM core.repository_lifecycle_activation "
        "  WHERE account_id=%s AND repository_id='91011'),"
        "convergence_graph_claim_count,convergence_pending_count,"
        "graph_refresh_due_at IS NULL "
        "FROM core.installation_account WHERE account_id=%s",
        (claimed_account, claimed_account, claimed_account, claimed_account),
    )[0]
    check(
        turn and turn["account_id"] == claimed_account
        and claimed_result.get("ok") is True
        and claimed_state == (0, 0, 0, 0, 0, True)
        and late_finish.get("lease_lost") is True,
        "offboard of claimed graph work removes its lease/private repo metadata and fences the returning worker",
    )


def main() -> int:
    print("POLICY REFRESH SCHEDULER SCALE GATE")
    boot = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if boot.returncode != 0:
        print("bootstrap failed:\n" + boot.stderr[-2000:])
        return 1
    try:
        rolling_two_slot_cutover_requeues_second()
        quota_defer_autonomously_reprobes_without_synthetic_failure()
        oldest_beyond_5000_and_indexed()
        pr_wake_and_stall_age_are_monotonic()
        graph_surface_dirty_restarts_only_after_paginated_tail()
        enqueue_fat_account_is_linear()
        graph_slot_expiry_fences_late_worker()
        late_account_gets_reserved_worker_capacity()
        exhausted_graph_recovers_without_starving_neighbor()
        continuous_graph_cannot_starve_policy()
        offboard_cancels_pending_and_claimed()
    finally:
        subprocess.run(
            ["dropdb", "--if-exists", DB],
            capture_output=True,
            text=True,
        )

    print()
    if all(checks):
        print("POLICY REFRESH SCHEDULER SCALE GATE: PASS")
        return 0
    print(
        "POLICY REFRESH SCHEDULER SCALE GATE: FAIL "
        f"({sum(not result for result in checks)} of {len(checks)} failed)"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
