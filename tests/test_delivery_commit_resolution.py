#!/usr/bin/env python3
"""Exact-generation durable resolution of an ambiguous server-side COMMIT."""
from __future__ import annotations

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import psycopg2  # noqa: E402
from psycopg2.extras import Json  # noqa: E402


DB = "veripsa_commitresolve_" + str(os.getpid())
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
MIGRATOR_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
checks: list[tuple[str, bool]] = []


def check(label: str, condition) -> None:
    checks.append((label, bool(condition)))
    print(("  [PASS] " if condition else "  [FAIL] ") + label)


def one(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        conn.close()


def enqueue(key: str, account: str):
    payload = {
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "repository": {
            "id": str(abs(hash(key)) % 1_000_000 + 1),
            "full_name": f"acme/{key}",
            "owner": {"id": account},
            "default_branch": "main",
        },
        "sender": {"login": "ann", "type": "User"},
        "commits": [],
    }
    result = one(
        APP_DSN,
        "SELECT core.enqueue_webhook_delivery_with_authority(%s,'push',%s,%s,%s,100,2)",
        (key, account, f"acme/{key}", Json(payload)),
    )
    assert result.get("accepted"), result


def claim(key: str, owner: str, max_attempts: int = 3):
    result = one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,%s,3,%s,120)",
        (key, max_attempts, owner),
    )
    assert result.get("claimed"), result
    return int(result["lease_generation"])


def resolve(key: str, generation: int, error: str = "commit response lost", max_attempts: int = 3):
    return one(
        APP_DSN,
        "SELECT core.resolve_webhook_delivery_commit_with_authority(%s,%s,%s,%s)",
        (key, error, max_attempts, generation),
    )


def state(key: str):
    conn = psycopg2.connect(MIGRATOR_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status,attempts,lease_generation,locked_at,last_error,owner_instance "
                "FROM core.webhook_delivery WHERE delivery_key=%s",
                (key,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def finish_and_drop_response(key: str, generation: int) -> None:
    """Commit finish server-side without observing its SELECT result."""
    conn = psycopg2.connect(APP_DSN)
    try:
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(
                "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
                (key, generation),
            )
            # Deliberately do not fetch: this models a server-side COMMIT whose
            # result/ACK disappears before the caller learns the outcome.
        conn.commit()
    finally:
        conn.close()


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("bootstrap failed:\n" + (result.stderr or "")[-1200:])


def main() -> int:
    bootstrap()
    try:
        # Server committed and the response disappeared. The exact generation
        # is already done, so resolution must authorize NO replay.
        enqueue("commit-done", "acct-done")
        done_generation = claim("commit-done", "commit-owner-done")
        finish_and_drop_response("commit-done", done_generation)
        committed = resolve("commit-done", done_generation)
        done_state = state("commit-done")
        wrong_done_generation = resolve("commit-done", done_generation + 1)
        check(
            "done at the exact generation resolves committed and is never requeued",
            committed == "committed"
            and done_state[0] == "done"
            and done_state[2] == done_generation
            and done_state[3] is None
            and wrong_done_generation == "ownership_lost",
        )

        # No terminal commit landed: exact processing ownership is released
        # through the same bounded attempt policy as ordinary failure.
        enqueue("commit-pre", "acct-pre")
        first_generation = claim("commit-pre", "commit-owner-pre-1")
        released = resolve(
            "commit-pre", first_generation,
            error="connection disappeared before COMMIT",
            max_attempts=3,
        )
        released_state = state("commit-pre")
        repeated_exact_release = resolve(
            "commit-pre", first_generation,
            error="connection disappeared before COMMIT",
            max_attempts=3,
        )
        repeated_after_release = resolve("commit-pre", first_generation)
        check(
            "same-generation processing and a lost resolver ACK converge through exact queued release",
            released == "queued"
            and released_state[0:3] == ("queued", 1, first_generation)
            and released_state[3] is None
            and released_state[4] == "connection disappeared before COMMIT"
            and repeated_exact_release == "queued"
            and repeated_after_release == "ownership_lost",
        )

        # ABA fence: once a new claim advances generation, the old resolver can
        # neither requeue nor mark the successor committed.
        second_generation = claim("commit-pre", "commit-owner-pre-2")
        before_late = state("commit-pre")
        late_old_resolution = resolve("commit-pre", first_generation)
        after_late = state("commit-pre")
        check(
            "late old-generation resolution cannot mutate a newer processing owner",
            second_generation == first_generation + 1
            and late_old_resolution == "ownership_lost"
            and before_late == after_late
            and after_late[0:3] == ("processing", 2, second_generation)
            and after_late[5] == "commit-owner-pre-2",
        )
        assert resolve("commit-pre", second_generation) == "queued"

        # Attempt ceiling follows release's visible DLQ policy, not an invisible
        # processing freeze or a forged committed outcome.
        enqueue("commit-dlq", "acct-dlq")
        dlq_generation = claim("commit-dlq", "commit-owner-dlq", max_attempts=1)
        dlq_result = resolve(
            "commit-dlq", dlq_generation,
            error="precommit failure at durable ceiling",
            max_attempts=1,
        )
        dlq_state = state("commit-dlq")
        dlq_exact_repeat = resolve(
            "commit-dlq", dlq_generation,
            error="precommit failure at durable ceiling",
            max_attempts=1,
        )
        dlq_repeat = resolve("commit-dlq", dlq_generation, max_attempts=1)
        check(
            "processing at the attempt ceiling resolves failed/DLQ and unlocks",
            dlq_result == "failed"
            and dlq_state[0:3] == ("failed", 1, dlq_generation)
            and dlq_state[3] is None
            and dlq_exact_repeat == "failed"
            and dlq_repeat == "ownership_lost",
        )

        check(
            "missing durable key is distinct from lost generation authority",
            resolve("commit-no-such-key", 1) == "missing",
        )

        contract = one(
            MIGRATOR_DSN,
            "SELECT jsonb_build_object("
            "'exists',p.oid IS NOT NULL,"
            "'security_definer',p.prosecdef,"
            "'owner',r.rolname,"
            "'fixed_search_path',COALESCE(p.proconfig,'{}'::text[]) "
            "  @> ARRAY['search_path=core, pg_catalog'],"
            "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
            "'writer_execute',has_function_privilege('veripsa_writer',p.oid,'EXECUTE'),"
            "'app_table_update',has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE')) "
            "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
            "WHERE p.oid='core.resolve_webhook_delivery_commit_with_authority"
            "(text,text,integer,bigint)'::regprocedure",
        )
        check(
            "commit resolver is migrator-owned SECURITY DEFINER with fixed search_path and App-only execute",
            contract.get("exists") is True
            and contract.get("security_definer") is True
            and contract.get("owner") == "veripsa_migrator"
            and contract.get("fixed_search_path") is True
            and contract.get("app_execute") is True
            and contract.get("writer_execute") is False
            and contract.get("app_table_update") is False,
        )
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = all(condition for _, condition in checks)
    print("\nDELIVERY COMMIT RESOLUTION GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
