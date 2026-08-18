#!/usr/bin/env python3
"""Durable, exact-generation fan-out plan and per-repository completion proof."""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone

import psycopg2
from psycopg2.extras import Json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = "veripsa_fanout_checkpoint_" + str(os.getpid())
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
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def all_rows(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            return [row[0] for row in cur.fetchall()]
    finally:
        conn.close()


def error_code(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute(sql, args)
            except psycopg2.Error as exc:
                return exc.pgcode
            return None
    finally:
        conn.close()


def payload(action: str, *, all_repositories: bool = False) -> dict:
    value = {
        "action": action,
        "installation": {
            "id": "9001",
            "account": {"id": "fanout-account"},
        },
    }
    if all_repositories:
        value["repository_selection"] = "all"
    return value


def enqueue_and_claim(key: str, body: dict) -> int:
    admitted = one(
        APP_DSN,
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'installation',%s,NULL,%s,1000,2)",
        (key, "fanout-account-" + key, Json(body)),
    )
    assert admitted.get("accepted"), admitted
    claimed = one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8,3,%s,120)",
        (key, "fanout-owner-" + key),
    )
    assert claimed.get("claimed"), claimed
    return int(claimed["lease_generation"])


def seed_processing(
    key: str,
    event_type: str = "installation",
    action: str = "created",
    *,
    generation: int = 1,
) -> None:
    one(
        MIGRATOR_DSN,
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,payload,status,attempts,locked_at,"
        "lease_generation,causal_order_version) "
        "VALUES (%s,%s,%s,%s,'processing',1,clock_timestamp(),%s,1)",
        (key, event_type, "account-" + key, Json(payload(action)), generation),
    )


def prepare(key: str, generation: int, plan):
    return one(
        APP_DSN,
        "SELECT core.prepare_webhook_delivery_fanout_with_authority(%s,%s,%s)",
        (key, generation, Json(plan)),
    )


def complete(key: str, generation: int, repo_key: str):
    return one(
        APP_DSN,
        "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
        (key, generation, repo_key),
    )


def resolve_defer(
    key: str,
    generation: int,
    not_before: datetime,
    reason: str,
):
    return one(
        APP_DSN,
        "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
        (key, generation, not_before, reason),
    )


def delivery_state(key: str):
    return one(
        MIGRATOR_DSN,
        "SELECT jsonb_build_object("
        "'status',status,'attempts',attempts,'generation',lease_generation,"
        "'payload',payload,'locked',locked_at IS NOT NULL,"
        "'not_before',not_before,'last_error',last_error,"
        "'owner_instance',owner_instance) "
        "FROM core.webhook_delivery WHERE delivery_key=%s",
        (key,),
    )


def bootstrap() -> None:
    result = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("bootstrap failed:\n" + (result.stderr or "")[-1600:])


def main() -> int:
    bootstrap()
    try:
        # A test-only "business write" makes the transaction boundary observable without granting the App direct
        # access to any production table. Completion is called on that same transaction and connection.
        one(
            MIGRATOR_DSN,
            "CREATE TABLE public.fanout_business_probe("
            "repo_key text PRIMARY KEY, committed_at timestamptz DEFAULT now() NOT NULL)",
        )
        one(
            MIGRATOR_DSN,
            "GRANT INSERT,SELECT ON public.fanout_business_probe TO veripsa_app",
        )

        # GitHub omits repositories for an all-repositories install. The runtime enumeration is frozen once, with
        # id-first canonical keys, deterministic order, and a bounded name fallback only for genuinely id-less rows.
        main_key = "fanout-allrepos"
        generation_one = enqueue_and_claim(
            main_key,
            payload("created", all_repositories=True),
        )
        proposed = [
            {"key": "name:acme/idless", "full_name": "acme/idless"},
            {"key": "id:202", "full_name": "acme/zeta", "id": 202},
            {"key": "id:101", "full_name": "acme/alpha", "id": "101"},
        ]
        canonical = [
            {"key": "id:101", "full_name": "acme/alpha", "id": "101"},
            {"key": "id:202", "full_name": "acme/zeta", "id": "202"},
            {"key": "name:acme/idless", "full_name": "acme/idless"},
        ]
        first_prepare = prepare(main_key, generation_one, proposed)
        replacement_proposal = [
            {"key": "id:999", "full_name": "other/new", "id": "999"},
        ]
        repeated_prepare = prepare(main_key, generation_one, replacement_proposal)
        prepared_state = delivery_state(main_key)
        check(
            "all-repositories enumeration freezes one canonical immutable plan",
            first_prepare == {"plan": canonical, "completed": {}}
            and repeated_prepare == first_prepare
            and prepared_state["payload"]["_veripsa_fanout_plan"] == canonical
            and prepared_state["payload"]["_veripsa_fanout_completed"] == {},
        )

        premature_finish = one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (main_key, generation_one),
        )
        check(
            "finish refuses a valid but incomplete fanout plan",
            premature_finish is False
            and delivery_state(main_key)["status"] == "processing",
        )

        # Repository A's business write and checkpoint commit atomically. The plan remains incomplete because two
        # siblings are outstanding.
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "INSERT INTO public.fanout_business_probe(repo_key) VALUES (%s)",
                    ("id:101",),
                )
                cur.execute(
                    "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
                    (main_key, generation_one, "id:101"),
                )
                a_all_complete = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        after_a = delivery_state(main_key)
        check(
            "repository A business write and completion checkpoint commit in one transaction",
            a_all_complete == {"updated": True, "all_done": False}
            and after_a["payload"]["_veripsa_fanout_completed"] == {"id:101": True}
            and one(
                APP_DSN,
                "SELECT count(*)::int FROM public.fanout_business_probe WHERE repo_key='id:101'",
            ) == 1,
        )

        # A processor failure releases the delivery. Even a duplicate GitHub admission while queued must preserve
        # the original plan/checkpoint; the next generation can therefore skip A.
        released = one(
            APP_DSN,
            "SELECT core.release_webhook_delivery_with_authority(%s,'sibling failed',8,%s)",
            (main_key, generation_one),
        )
        duplicate = one(
            APP_DSN,
            "SELECT core.enqueue_webhook_delivery_with_authority("
            "%s,'installation',%s,NULL,%s,1000,2)",
            (main_key, "fanout-account-" + main_key, Json(payload("created", all_repositories=True))),
        )
        generation_two_claim = one(
            APP_DSN,
            "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8,3,%s,120)",
            (main_key, "fanout-owner-retry"),
        )
        generation_two = int(generation_two_claim["lease_generation"])
        retry_view = prepare(main_key, generation_two, replacement_proposal)
        check(
            "retry generation retains the immutable plan and can skip already-completed A",
            released == "queued"
            and duplicate.get("accepted") is True
            and generation_two == generation_one + 1
            and retry_view == {"plan": canonical, "completed": {"id:101": True}},
        )

        # A delayed prior owner cannot add a sibling completion, replace the plan, or finish the successor.
        before_aba = delivery_state(main_key)
        stale_complete = complete(main_key, generation_one, "id:202")
        stale_prepare = prepare(main_key, generation_one, proposed)
        stale_defer = resolve_defer(
            main_key,
            generation_one,
            datetime.now(timezone.utc) + timedelta(minutes=1),
            "stale owner must not yield successor",
        )
        stale_finish = one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (main_key, generation_one),
        )
        after_aba = delivery_state(main_key)
        check(
            "old generation is an ABA-safe no-op across prepare, complete, defer, and finish",
            stale_complete == {"updated": False, "all_done": False}
            and stale_prepare is None
            and stale_defer == "ownership_lost"
            and stale_finish is False
            and before_aba == after_aba,
        )

        unknown_before = delivery_state(main_key)
        unknown_code = error_code(
            APP_DSN,
            "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
            (main_key, generation_two, "id:404"),
        )
        check(
            "completion accepts only a key in the immutable plan",
            unknown_code == "22023"
            and delivery_state(main_key) == unknown_before,
        )

        # Checkpoint the second repository, leaving one id-less fallback key for the terminal transaction.
        check(
            "intermediate retry checkpoint remains partial",
            complete(main_key, generation_two, "id:202")
            == {"updated": True, "all_done": False},
        )

        # The DR export is content-minimized: private execution markers are not copied into the backup stream.
        stored_before_export = delivery_state(main_key)
        exported = all_rows(
            MIGRATOR_DSN,
            "SELECT core.export_durable_rows_with_authority('')",
        )
        exported_main = next(
            row for row in exported
            if row.get("_table") == "webhook_delivery"
            and row.get("delivery_key") == main_key
        )
        check(
            "backup export strips internal fanout plan and completion markers",
            "_veripsa_fanout_plan" in stored_before_export["payload"]
            and "_veripsa_fanout_completed" in stored_before_export["payload"]
            and "_veripsa_fanout_plan" not in exported_main["payload"]
            and "_veripsa_fanout_completed" not in exported_main["payload"],
        )

        # Last repository business write, checkpoint, and durable finish share one transaction. A crash before this
        # commit would expose neither the business row nor completion/done; after commit all three are visible.
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "INSERT INTO public.fanout_business_probe(repo_key) VALUES (%s)",
                    ("name:acme/idless",),
                )
                cur.execute(
                    "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
                    (main_key, generation_two, "name:acme/idless"),
                )
                last_complete = cur.fetchone()[0]
                cur.execute(
                    "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
                    (main_key, generation_two),
                )
                finished = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        terminal = delivery_state(main_key)
        business_keys = all_rows(
            APP_DSN,
            "SELECT repo_key FROM public.fanout_business_probe ORDER BY repo_key",
        )
        check(
            "last completion and finish commit atomically; done payload is fully scrubbed",
            last_complete == {"updated": True, "all_done": True}
            and finished is True
            and terminal["status"] == "done"
            and terminal["payload"] == {}
            and business_keys == ["id:101", "name:acme/idless"],
        )

        # A partial slice has one indivisible publication boundary: the repository's business write, its
        # checkpoint, and the exact-generation attempt-neutral queued transition. Rolling the transaction back
        # must expose none of them.
        rollback_key = "fanout-yield-rollback"
        yield_plan = [
            {"key": "id:301", "full_name": "yield/one", "id": "301"},
            {"key": "id:302", "full_name": "yield/two", "id": "302"},
        ]
        seed_processing(rollback_key)
        prepare(rollback_key, 1, yield_plan)
        rollback_before = delivery_state(rollback_key)
        rollback_retry_at = datetime.now(timezone.utc) + timedelta(minutes=2)
        rollback_reason = "durable_fanout_budget_slice"
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "INSERT INTO public.fanout_business_probe(repo_key) VALUES (%s)",
                    ("yield-rollback",),
                )
                cur.execute(
                    "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
                    (rollback_key, 1, "id:301"),
                )
                rollback_complete = cur.fetchone()[0]
                cur.execute(
                    "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
                    (rollback_key, 1, rollback_retry_at, rollback_reason),
                )
                rollback_staged_defer = cur.fetchone()[0]
            conn.rollback()
        finally:
            conn.close()
        rollback_after = delivery_state(rollback_key)
        check(
            "rolling back a partial-slice transaction exposes no business/checkpoint/queue mutation",
            rollback_complete == {"updated": True, "all_done": False}
            and rollback_staged_defer == "deferred"
            and rollback_after == rollback_before
            and one(
                APP_DSN,
                "SELECT count(*)::int FROM public.fanout_business_probe "
                "WHERE repo_key='yield-rollback'",
            ) == 0,
        )

        # If COMMIT acknowledgement was lost and the transaction actually rolled back, the exact row is still
        # processing. The resolver safely applies only the attempt-neutral yield; the uncommitted business work
        # and checkpoint remain absent and will be retried.
        rollback_resolution = resolve_defer(
            rollback_key, 1, rollback_retry_at, rollback_reason)
        rollback_resolution_state = delivery_state(rollback_key)
        rollback_queued_proof = resolve_defer(
            rollback_key, 1, rollback_retry_at, rollback_reason)
        check(
            "exact processing resolves a rolled-back commit to an attempt-neutral retry",
            rollback_resolution == "deferred"
            and rollback_queued_proof == "deferred"
            and rollback_resolution_state["status"] == "queued"
            and rollback_resolution_state["attempts"] == 0
            and rollback_resolution_state["generation"] == 1
            and rollback_resolution_state["locked"] is False
            and rollback_resolution_state["not_before"] is not None
            and rollback_resolution_state["last_error"] == rollback_reason
            and rollback_resolution_state["payload"]["_veripsa_fanout_completed"] == {}
            and one(
                APP_DSN,
                "SELECT count(*)::int FROM public.fanout_business_probe "
                "WHERE repo_key='yield-rollback'",
            ) == 0,
        )

        # The commit case publishes all four facts together. Re-running the resolver with the exact same
        # generation/schedule/reason is the positive ACK-ambiguity proof and does not mutate the queued row.
        commit_key = "fanout-yield-commit"
        seed_processing(commit_key)
        prepare(commit_key, 1, yield_plan)
        commit_retry_at = datetime.now(timezone.utc) + timedelta(minutes=3)
        commit_reason = "durable_fanout_slice"
        conn = psycopg2.connect(APP_DSN)
        try:
            conn.autocommit = False
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "INSERT INTO public.fanout_business_probe(repo_key) VALUES (%s)",
                    ("yield-commit",),
                )
                cur.execute(
                    "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
                    (commit_key, 1, "id:301"),
                )
                commit_complete = cur.fetchone()[0]
                cur.execute(
                    "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
                    (commit_key, 1, commit_retry_at, commit_reason),
                )
                commit_staged_defer = cur.fetchone()[0]
            conn.commit()
        finally:
            conn.close()
        committed_state = delivery_state(commit_key)
        queued_ack = resolve_defer(
            commit_key, 1, commit_retry_at, commit_reason)
        queued_before_mismatch = delivery_state(commit_key)
        mismatched_ack = resolve_defer(
            commit_key, 1, commit_retry_at, "different reason")
        queued_after_mismatch = delivery_state(commit_key)
        check(
            "commit publishes business/checkpoint/attempt-neutral defer and exact queued state proves it",
            commit_complete == {"updated": True, "all_done": False}
            and commit_staged_defer == "deferred"
            and queued_ack == "deferred"
            and mismatched_ack == "ownership_lost"
            and committed_state["status"] == "queued"
            and committed_state["attempts"] == 0
            and committed_state["generation"] == 1
            and committed_state["locked"] is False
            and committed_state["not_before"] is not None
            and committed_state["last_error"] == commit_reason
            and committed_state["payload"]["_veripsa_fanout_completed"]
            == {"id:301": True}
            and one(
                APP_DSN,
                "SELECT count(*)::int FROM public.fanout_business_probe "
                "WHERE repo_key='yield-commit'",
            ) == 1
            and queued_before_mismatch == queued_after_mismatch,
        )
        check(
            "resolver distinguishes missing and invalid authority without mutating a committed yield",
            resolve_defer(
                "fanout-yield-missing", 1, commit_retry_at, commit_reason)
            == "missing"
            and resolve_defer(
                commit_key, 2, commit_retry_at, commit_reason)
            == "ownership_lost"
            and delivery_state(commit_key) == queued_after_mismatch,
        )

        # The final checkpoint is not a yield boundary: it must stay processing so finish can publish in the same
        # terminal transaction. The resolver fails closed and cannot demote the completed row to queued.
        final_defer_key = "fanout-final-must-finish"
        seed_processing(final_defer_key)
        prepare(final_defer_key, 1, [yield_plan[0]])
        final_checkpoint = complete(final_defer_key, 1, "id:301")
        before_final_defer = delivery_state(final_defer_key)
        final_defer_code = error_code(
            APP_DSN,
            "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
            (
                final_defer_key,
                1,
                datetime.now(timezone.utc) + timedelta(minutes=1),
                "must not defer final",
            ),
        )
        after_final_defer = delivery_state(final_defer_key)
        final_finished = one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (final_defer_key, 1),
        )
        check(
            "final checkpoint remains processing and can only take the terminal finish path",
            final_checkpoint == {"updated": True, "all_done": True}
            and final_defer_code == "55000"
            and before_final_defer == after_final_defer
            and before_final_defer["status"] == "processing"
            and before_final_defer["attempts"] == 1
            and before_final_defer["locked"] is True
            and final_finished is True
            and delivery_state(final_defer_key)["status"] == "done",
        )

        # Inclusive upper bound: 500 canonical repositories are accepted and retained without truncation.
        seed_processing("fanout-max")
        max_plan = [
            {
                "key": f"id:{10000 + index}",
                "full_name": f"large/repo-{index:03d}",
                "id": str(10000 + index),
            }
            for index in range(500)
        ]
        max_prepared = prepare("fanout-max", 1, max_plan)
        check(
            "fanout plan accepts the bounded 500-repository ceiling without truncation",
            len(max_prepared["plan"]) == 500 and max_prepared["completed"] == {},
        )

        # Every permitted lifecycle/action shape can prepare; adjacent account-wide actions cannot forge a plan.
        allowed = [
            ("installation", "unsuspend"),
            ("installation", "new_permissions_accepted"),
            ("installation_repositories", "added"),
            ("installation_repositories", "removed"),
        ]
        allowed_results = []
        singleton = [{"key": "id:7", "full_name": "allowed/repo", "id": "7"}]
        for index, (event_type, action) in enumerate(allowed):
            key = f"fanout-allowed-{index}"
            seed_processing(key, event_type, action)
            allowed_results.append(prepare(key, 1, singleton))
        seed_processing("fanout-disallowed-delete", "installation", "deleted")
        disallowed_code = error_code(
            APP_DSN,
            "SELECT core.prepare_webhook_delivery_fanout_with_authority(%s,1,%s)",
            ("fanout-disallowed-delete", Json(singleton)),
        )
        check(
            "only the five explicit installation fanout event/actions can prepare a plan",
            all(result == {"plan": singleton, "completed": {}} for result in allowed_results)
            and disallowed_code == "22023",
        )

        # Reject empty/oversized/noncanonical/duplicate/conflicting/invalid plans before writing any marker.
        seed_processing("fanout-poison")
        invalid_plans = [
            [],
            max_plan + [{"key": "id:999999", "full_name": "large/overflow", "id": "999999"}],
            [{"key": "name:poison/repo", "full_name": "poison/repo", "id": "8"}],
            [
                {"key": "id:8", "full_name": "poison/a", "id": "8"},
                {"key": "id:8", "full_name": "poison/b", "id": "8"},
            ],
            [
                {"key": "id:8", "full_name": "poison/same", "id": "8"},
                {"key": "id:9", "full_name": "poison/same", "id": "9"},
            ],
            [{"key": "id:0", "full_name": "poison/zero", "id": 0}],
            [{"key": "id:8", "full_name": "poison/extra", "id": "8", "secret": "x"}],
            [{"key": "name:not-a-coordinate", "full_name": "not-a-coordinate"}],
        ]
        invalid_codes = [
            error_code(
                APP_DSN,
                "SELECT core.prepare_webhook_delivery_fanout_with_authority(%s,1,%s)",
                ("fanout-poison", Json(candidate)),
            )
            for candidate in invalid_plans
        ]
        poison_after = delivery_state("fanout-poison")
        check(
            "empty, oversized, duplicate, conflicting, noncanonical, and invalid plans fail closed",
            invalid_codes == ["22023"] * len(invalid_plans)
            and "_veripsa_fanout_plan" not in poison_after["payload"]
            and "_veripsa_fanout_completed" not in poison_after["payload"],
        )

        # A malformed marker introduced by a bad restore/manual owner write cannot be laundered into done.
        one(
            MIGRATOR_DSN,
            "UPDATE core.webhook_delivery "
            "SET payload=payload||jsonb_build_object("
            "'_veripsa_fanout_plan','[]'::jsonb,"
            "'_veripsa_fanout_completed','{}'::jsonb) "
            "WHERE delivery_key='fanout-poison'",
        )
        poison_finish = one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority('fanout-poison',1)",
        )
        check(
            "finish refuses a poisoned stored plan and preserves it for recovery",
            poison_finish is False
            and delivery_state("fanout-poison")["status"] == "processing"
            and delivery_state("fanout-poison")["payload"]["_veripsa_fanout_plan"] == [],
        )

        seed_processing("fanout-no-plan")
        no_plan_before = delivery_state("fanout-no-plan")
        no_plan_defer_code = error_code(
            APP_DSN,
            "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
            (
                "fanout-no-plan",
                1,
                datetime.now(timezone.utc) + timedelta(minutes=1),
                "invalid unprepared yield",
            ),
        )
        check(
            "missing/stale/no-plan completion is explicit and an unprepared yield fails closed",
            complete("fanout-no-plan", 1, "id:7")
            == {"updated": False, "all_done": False}
            and complete("fanout-missing", 1, "id:7")
            == {"updated": False, "all_done": False}
            and no_plan_defer_code == "22023"
            and delivery_state("fanout-no-plan") == no_plan_before
            and "_veripsa_fanout_completed" not in no_plan_before["payload"],
        )

        signatures = {
            "prepare": "core.prepare_webhook_delivery_fanout_with_authority(text,bigint,jsonb)",
            "complete": "core.complete_webhook_delivery_fanout_repository_with_authority(text,bigint,text)",
            "defer_resolver": (
                "core.resolve_webhook_delivery_fanout_defer_with_authority("
                "text,bigint,timestamp with time zone,text)"
            ),
        }
        return_types = {
            "prepare": "jsonb",
            "complete": "jsonb",
            "defer_resolver": "text",
        }
        contracts = {}
        for name, signature in signatures.items():
            contracts[name] = one(
                MIGRATOR_DSN,
                "SELECT jsonb_build_object("
                "'security_definer',p.prosecdef,"
                "'owner',r.rolname,"
                "'fixed_search_path',COALESCE(p.proconfig,'{}'::text[]) "
                "  @> ARRAY['search_path=core, pg_catalog'],"
                "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
                "'writer_execute',has_function_privilege('veripsa_writer',p.oid,'EXECUTE'),"
                "'return_type',pg_get_function_result(p.oid)) "
                "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
                "WHERE p.oid=to_regprocedure(%s)",
                (signature,),
            )
        private_acl = one(
            MIGRATOR_DSN,
            "SELECT NOT has_function_privilege("
            "'veripsa_app','core._canonical_webhook_delivery_fanout_plan(jsonb)','EXECUTE') "
            "AND NOT has_function_privilege("
            "'veripsa_app','core._webhook_delivery_fanout_completion_valid(jsonb,jsonb)','EXECUTE') "
            "AND NOT has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE')",
        )
        check(
            "fanout mutation APIs are migrator-owned fixed-search-path SECURITY DEFINER and App-only",
            all(
                contract["security_definer"] is True
                and contract["owner"] == "veripsa_migrator"
                and contract["fixed_search_path"] is True
                and contract["app_execute"] is True
                and contract["writer_execute"] is False
                and contract["return_type"] == return_types[name]
                for name, contract in contracts.items()
            )
            and private_acl is True,
        )
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)

    ok = all(condition for _, condition in checks)
    print("\nDELIVERY FANOUT CHECKPOINT GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
