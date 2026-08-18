#!/usr/bin/env python3
"""Real-Postgres proof of one bounded durable retry epoch.

The database owns one absolute execution window across ordinary
release/reclaim generations, and automatic failed-row rearm is finite.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import subprocess
import sys
import threading
import time

import psycopg2
from psycopg2.extras import Json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import delivery_queue as DQ  # noqa: E402
import event_budget  # noqa: E402
from event_queue import EventQueue  # noqa: E402


DB = f"veripsa_durable_retry_window_{os.getpid()}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
OWNER_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
CHECKS: list[tuple[str, bool]] = []


def check(label: str, condition, *, detail=None) -> None:
    passed = bool(condition)
    CHECKS.append((label, passed))
    print(("  [PASS] " if passed else "  [FAIL] ") + label)
    if not passed and detail is not None:
        print(f"    detail: {detail!r}")


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


def attempted_one(dsn: str, sql: str, args=()) -> dict:
    """Return either one JSON value or the SQL rejection from a legacy shim."""
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute(sql, args)
                row = cur.fetchone()
                return {"result": row[0] if row else None}
            except psycopg2.Error as exc:
                return {
                    "pgcode": exc.pgcode,
                    "message": str(exc),
                    "detail": (exc.diag.message_detail or ""),
                }
    finally:
        conn.close()


def state(key: str) -> dict:
    value = one(
        OWNER_DSN,
        "SELECT jsonb_build_object("
        "'status',status,'attempts',attempts,'generation',lease_generation,"
        "'received_at',received_at,'updated_at',updated_at,"
        "'locked',locked_at IS NOT NULL,'owner',owner_instance,"
        "'not_before',not_before,'error',last_error,'payload',payload,"
        "'retry_expires',retry_window_expires_at,'auto_rearms',auto_rearm_count) "
        "FROM core.webhook_delivery WHERE delivery_key=%s",
        (key,),
    )
    return value if isinstance(value, dict) else {}


def push_payload(repo_id: int, repo: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "after": "a" * 40,
        "commits": [],
        "repository": {
            "id": repo_id,
            "full_name": repo,
            "default_branch": "main",
            "owner": {"id": repo_id + 1000, "login": repo.split("/")[0]},
        },
    }


def installation_payload(account_id: int) -> dict:
    return {
        "action": "created",
        "installation": {
            "id": account_id + 5000,
            "account": {"id": account_id, "login": f"acct-{account_id}"},
        },
        "repositories": [
            {"id": account_id + 7000, "full_name": f"acct-{account_id}/one"},
            {"id": account_id + 7001, "full_name": f"acct-{account_id}/two"},
        ],
    }


def enqueue_sql(key: str, event_type: str, account: str, payload: dict):
    repo = payload.get("repository", {}).get("full_name")
    return one(
        APP_DSN,
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,%s,%s,%s,%s,1000,2)",
        (key, event_type, account, repo, Json(payload)),
    )


def claim_sql(key: str, *, max_attempts: int = 3, window: int = 2,
              owner: str | None = None, stale_seconds: int = 100):
    return one(
        APP_DSN,
        "SELECT core.claim_webhook_delivery_with_authority(%s,%s,%s,3,%s,%s)",
        (key, stale_seconds, max_attempts,
         owner or f"test-owner-{key}"[:64], window),
    )


def release_sql(key: str, generation: int, *, max_attempts: int = 3,
                error: str = "ordinary failure"):
    return one(
        APP_DSN,
        "SELECT core.resolve_webhook_delivery_release_with_authority(%s,%s,%s,%s)",
        (key, error, max_attempts, generation),
    )


class _AliveThread:
    def is_alive(self):
        return True


def claim_while_head_finish_is_uncommitted(
        key: str, head_key: str, head_generation: int, *,
        stale_seconds: int, hold_seconds: float = 2.05) -> dict:
    """Call rolling /5 while an earlier causal head's finish is uncommitted."""
    holder = psycopg2.connect(OWNER_DSN)
    holder.autocommit = False
    with holder.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute(
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (head_key, head_generation),
        )
        finish_returned = cur.fetchone()[0]
    observed: dict = {
        "finish_returned": finish_returned,
        "state_before": state(key),
    }
    query_started = threading.Event()

    def claim() -> None:
        conn = psycopg2.connect(
            APP_DSN, application_name="retry-post-lock-test",
        )
        try:
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute("SET search_path=core")
                query_started.set()
                started_at = time.monotonic()
                cur.execute(
                    "SELECT core.claim_webhook_delivery_with_authority("
                    "%s,%s,3,2,%s)",
                    (key, stale_seconds, f"rolling-{key}"[:64]),
                )
                observed["result"] = cur.fetchone()[0]
                observed["elapsed"] = time.monotonic() - started_at
        except Exception as exc:  # pragma: no cover - asserted by the caller
            observed["error"] = repr(exc)
        finally:
            conn.close()

    waiter = threading.Thread(target=claim, daemon=True)
    waiter.start()
    observed["query_started"] = query_started.wait(1.0)
    waiter.join(1.0)
    observed["returned_while_locked"] = not waiter.is_alive()
    observed["state_while_locked"] = state(key)
    time.sleep(hold_seconds)
    holder.commit()
    holder.close()
    waiter.join(2.0)
    observed["waiter_alive"] = waiter.is_alive()
    return observed


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
        # Ordinary generations retain one immutable database expiry.
        enqueue_sql("window-nonextend", "push", "acct-window",
                    push_payload(1101, "window/nonextend"))
        first = claim_sql("window-nonextend", window=2)
        first_expiry = state("window-nonextend")["retry_expires"]
        first_release = release_sql(
            "window-nonextend", int(first["lease_generation"]))
        time.sleep(0.08)
        second = claim_sql("window-nonextend", window=30)
        second_expiry = state("window-nonextend")["retry_expires"]
        check(
            "ordinary release/reclaim preserves one absolute expiry and never extends it",
            first.get("claimed") and first_release == "queued"
            and second.get("claimed") and first_expiry == second_expiry
            and int(second["lease_generation"]) > int(first["lease_generation"]),
        )
        release_sql("window-nonextend", int(second["lease_generation"]))
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key='window-nonextend'",
        )

        # The target is still within its window while its earlier causal head is
        # committing finish(). NOWAIT must defer rolling /5 immediately without
        # changing the target. After the finish commits and the absolute window
        # crosses, the next claim normalizes that queued target to failed.
        enqueue_sql("window-lock-cross-head", "push", "acct-lock-cross",
                    push_payload(1103, "window/lock-cross"))
        lock_cross_head = claim_sql("window-lock-cross-head", window=30)
        enqueue_sql("window-lock-cross", "push", "acct-lock-cross",
                    push_payload(1103, "window/lock-cross"))
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery "
            "SET retry_window_expires_at=clock_timestamp()+interval '2 seconds' "
            "WHERE delivery_key='window-lock-cross'",
        )
        lock_claim = claim_while_head_finish_is_uncommitted(
            "window-lock-cross", "window-lock-cross-head",
            int(lock_cross_head["lease_generation"]), stale_seconds=100,
        )
        lock_cross_after_expiry = claim_sql(
            "window-lock-cross", window=30,
        )
        lock_cross_state = state("window-lock-cross")
        lock_cross_result = lock_claim.get("result", {})
        check(
            "queued expiry crossing defers during head finish, then fails on the next claim",
            lock_cross_head.get("claimed")
            and lock_claim.get("finish_returned") is True
            and lock_claim.get("query_started")
            and lock_claim.get("returned_while_locked")
            and not lock_claim.get("waiter_alive")
            and not lock_claim.get("error")
            and float(lock_claim.get("elapsed", 99)) < 1.0
            and lock_cross_result.get("claimed") is False
            and lock_cross_result.get("reason") == "blocked_by_earlier"
            and lock_cross_result.get("status") == "queued"
            and lock_claim.get("state_while_locked")
                == lock_claim.get("state_before")
            and lock_cross_after_expiry.get("claimed") is False
            and lock_cross_after_expiry.get("reason") == "failed"
            and lock_cross_after_expiry.get("status") == "failed"
            and lock_cross_state["status"] == "failed"
            and lock_cross_state["attempts"] == 0
            and lock_cross_state["generation"] == 0
            and lock_cross_state["error"] == "retry_window_exhausted"
            and lock_cross_state["owner"] is None
            and not lock_cross_state["locked"],
            detail={
                "first_claim": lock_claim,
                "next_claim": lock_cross_after_expiry,
                "state": lock_cross_state,
            },
        )
        # This row has served its race proof. Keep it out of the later
        # one-row automatic-rearm selection so the checks remain independent.
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key='window-lock-cross'",
        )

        # A later processing owner can be fresh when an earlier head starts
        # committing, then cross both stale and absolute-expiry boundaries.
        # The contended claim is still an immediate, mutation-free deferral;
        # after the head commits, the next claim must fail the stale generation
        # rather than minting another handler lease.
        enqueue_sql("processing-lock-cross-head", "push", "acct-processing-lock",
                    push_payload(1104, "window/processing-lock"))
        processing_cross_head = claim_sql(
            "processing-lock-cross-head", window=30,
        )
        enqueue_sql("processing-lock-cross", "push", "acct-processing-lock",
                    push_payload(1104, "window/processing-lock"))
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET "
            "status='processing',attempts=1,lease_generation=1,"
            "locked_at=clock_timestamp(),owner_instance='old-processing-owner',"
            "retry_window_expires_at=clock_timestamp()+interval '2 seconds' "
            "WHERE delivery_key='processing-lock-cross'",
        )
        processing_lock_claim = claim_while_head_finish_is_uncommitted(
            "processing-lock-cross", "processing-lock-cross-head",
            int(processing_cross_head["lease_generation"]), stale_seconds=1,
        )
        processing_after_expiry = claim_sql(
            "processing-lock-cross", window=30, stale_seconds=1,
        )
        processing_lock_result = processing_lock_claim.get("result", {})
        processing_lock_state = state("processing-lock-cross")
        check(
            "stale-processing expiry crossing defers during head finish, then fails on the next claim",
            processing_cross_head.get("claimed")
            and processing_lock_claim.get("finish_returned") is True
            and processing_lock_claim.get("query_started")
            and processing_lock_claim.get("returned_while_locked")
            and not processing_lock_claim.get("waiter_alive")
            and not processing_lock_claim.get("error")
            and float(processing_lock_claim.get("elapsed", 99)) < 1.0
            and processing_lock_result.get("claimed") is False
            and processing_lock_result.get("reason") == "blocked_by_earlier"
            and processing_lock_result.get("status") == "processing"
            and processing_lock_claim.get("state_while_locked")
                == processing_lock_claim.get("state_before")
            and processing_after_expiry.get("claimed") is False
            and processing_after_expiry.get("reason") == "failed"
            and processing_after_expiry.get("status") == "failed"
            and processing_lock_state["status"] == "failed"
            and processing_lock_state["attempts"] == 1
            and processing_lock_state["generation"] == 1
            and processing_lock_state["error"] == "retry_window_exhausted"
            and processing_lock_state["owner"] is None
            and not processing_lock_state["locked"],
            detail={
                "first_claim": processing_lock_claim,
                "next_claim": processing_after_expiry,
                "state": processing_lock_state,
            },
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key='processing-lock-cross'",
        )

        # The previous live /4 and /3 images cannot prove their configured
        # handler budget (officially 1..900s), so schema-first rollout rejects
        # even a fresh claim and leaves the row for protocol 3. The
        # intermediate /5 ABI carries `budget + ambiguity grace` in its exact
        # owner nonce: a 95s allowance may start one 120s epoch, a 205s
        # allowance may not, and /5 may never reclaim an existing epoch.
        legacy_sql = {
            "5": (
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,100,3,2,%s)"
            ),
            "4": (
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,100,3,2)"
            ),
            "3": (
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,100,3)"
            ),
        }
        safe_owner = (
            "wk-" + "1" * 32 + ".0095." + "2" * 22
        )
        wide_owner = (
            "wk-" + "3" * 32 + ".0205." + "4" * 22
        )

        enqueue_sql(
            "legacy-safe-5", "push", "acct-legacy-safe",
            push_payload(1110, "window/legacy-safe"),
        )
        safe_first = attempted_one(
            APP_DSN, legacy_sql["5"], ("legacy-safe-5", safe_owner),
        )
        safe_claim = safe_first.get("result") or {}
        safe_release = release_sql(
            "legacy-safe-5", int(safe_claim.get("lease_generation", -1)),
        ) if safe_claim.get("claimed") else None
        safe_second = attempted_one(
            APP_DSN, legacy_sql["5"], ("legacy-safe-5", safe_owner),
        )

        legacy_fresh_rejections = {}
        for repo_id, (arity, owner) in enumerate(
            (("5-wide", wide_owner), ("4", None), ("3", None)),
            start=1121,
        ):
            key = f"legacy-fresh-{arity}"
            enqueue_sql(
                key, "push", f"acct-legacy-{arity}",
                push_payload(repo_id, f"window/legacy-{arity}"),
            )
            if arity == "5-wide":
                attempted = attempted_one(
                    APP_DSN, legacy_sql["5"], (key, owner),
                )
            else:
                attempted = attempted_one(
                    APP_DSN, legacy_sql[arity], (key,),
                )
            legacy_fresh_rejections[arity] = attempted

        modern_results = {}
        for key in (
            "legacy-safe-5", "legacy-fresh-5-wide",
            "legacy-fresh-4", "legacy-fresh-3",
        ):
            modern = claim_sql(
                key, max_attempts=3, window=120,
                owner=f"modern-{key}"[:64],
            )
            modern_results[key] = modern
            if modern.get("claimed"):
                release_sql(key, int(modern["lease_generation"]))
            one(
                OWNER_DSN,
                "UPDATE core.webhook_delivery "
                "SET status='done',done_at=clock_timestamp() "
                "WHERE delivery_key=%s",
                (key,),
            )

        safe_existing_rejection = safe_second.get("result", {})
        wide_fresh_rejection = legacy_fresh_rejections[
            "5-wide"].get("result", {})
        four_fresh_rejection = legacy_fresh_rejections[
            "4"].get("result", {})
        three_fresh_rejection = legacy_fresh_rejections["3"]
        check(
            "schema-first rolling ABIs require a bounded /5 proof and protocol 3 owns every replay",
            safe_claim.get("claimed")
            and safe_release == "queued"
            and safe_existing_rejection.get("claimed") is False
            and safe_existing_rejection.get("reason")
                == "legacy_retry_window_unsupported"
            and wide_fresh_rejection.get("claimed") is False
            and wide_fresh_rejection.get("reason")
                == "legacy_budget_unproven"
            and four_fresh_rejection.get("claimed") is False
            and four_fresh_rejection.get("reason")
                == "legacy_budget_unproven"
            and three_fresh_rejection.get("pgcode") == "55000"
            and "legacy_budget_unproven"
                in three_fresh_rejection.get("detail", "")
            and all(
                modern.get("claimed")
                and 0 <= int(modern.get(
                    "retry_window_remaining_ms", -1)) <= 120_000
                for modern in modern_results.values()
            ),
            detail={
                "safe_first": safe_first,
                "safe_release": safe_release,
                "safe_second": safe_second,
                "fresh_rejections": legacy_fresh_rejections,
                "modern": modern_results,
            },
        )

        # A post-COMMIT probe stays exact when wall time crosses the expiry.
        enqueue_sql("window-ack-cross", "push", "acct-ack",
                    push_payload(1102, "window/ack"))
        ack_claim = claim_sql("window-ack-cross", window=1)
        ack_gen = int(ack_claim["lease_generation"])
        ack_release = release_sql(
            "window-ack-cross", ack_gen, error="same release")
        time.sleep(1.1)
        ack_probe = release_sql(
            "window-ack-cross", ack_gen, error="same release")
        before_sweep = state("window-ack-cross")
        one(
            APP_DSN,
            "SELECT core.pending_webhook_deliveries_with_authority(100,100,3)",
        )
        after_sweep = state("window-ack-cross")
        check(
            "exact release proof does not recalculate expected state after expiry crossing",
            ack_release == ack_probe == "queued"
            and before_sweep["status"] == "queued"
            and after_sweep["status"] == "failed"
            and after_sweep["error"] == "retry_window_exhausted",
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key='window-ack-cross'",
        )

        # A final-attempt crash and an expired stale generation both converge
        # to failed, while a fresh owner is untouched.
        for key, attempts, expiry in (
            ("stale-final-attempt", 3, "clock_timestamp()+interval '1 hour'"),
            ("stale-window", 1, "clock_timestamp()-interval '1 second'"),
        ):
            one(
                OWNER_DSN,
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,payload,status,attempts,"
                "locked_at,owner_instance,lease_generation,causal_order_version,"
                "retry_window_expires_at) "
                f"VALUES (%s,'ping',%s,'{{}}'::jsonb,'processing',%s,"
                f"clock_timestamp(),%s,1,1,{expiry})",
                (key, key, attempts, f"owner-{key}"[:64]),
            )
        one(APP_DSN, "SELECT core.pending_webhook_deliveries_with_authority(100,100,3)")
        fresh_window = state("stale-window")
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET locked_at=clock_timestamp()-interval '101 seconds' "
            "WHERE delivery_key IN ('stale-final-attempt','stale-window')",
        )
        one(APP_DSN, "SELECT core.pending_webhook_deliveries_with_authority(100,100,3)")
        stale_final = state("stale-final-attempt")
        stale_window = state("stale-window")
        check(
            "fresh processing is fenced, then stale final-attempt/window rows fail and clear ownership",
            fresh_window["status"] == "processing"
            and stale_final["status"] == stale_window["status"] == "failed"
            and not stale_final["locked"] and not stale_window["locked"]
            and stale_final["owner"] is None and stale_window["owner"] is None,
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key IN ('stale-final-attempt','stale-window')",
        )

        # Intentional waits and proven partial fanout progress start a later
        # execution window instead of consuming the old no-progress allowance.
        enqueue_sql("intentional-defer", "push", "acct-defer",
                    push_payload(1201, "defer/one"))
        defer_claim = claim_sql("intentional-defer", window=5)
        defer_at = datetime.now(timezone.utc) + timedelta(seconds=2)
        deferred = one(
            APP_DSN,
            "SELECT core.resolve_webhook_delivery_defer_with_authority(%s,%s,%s,%s)",
            ("intentional-defer", int(defer_claim["lease_generation"]),
             defer_at, "await consistency"),
        )
        check(
            "generic intentional defer resets the durable retry window",
            deferred == "deferred"
            and state("intentional-defer")["retry_expires"] is None,
        )

        fanout_body = installation_payload(1301)
        enqueue_sql("fanout-progress", "installation", "acct-fanout", fanout_body)
        fanout_claim = claim_sql("fanout-progress", window=5)
        fanout_gen = int(fanout_claim["lease_generation"])
        plan = [
            {"key": "id:8301", "full_name": "acct-1301/one", "id": "8301"},
            {"key": "id:8302", "full_name": "acct-1301/two", "id": "8302"},
        ]
        one(
            APP_DSN,
            "SELECT core.prepare_webhook_delivery_fanout_with_authority(%s,%s,%s)",
            ("fanout-progress", fanout_gen, Json(plan)),
        )
        one(
            APP_DSN,
            "SELECT core.complete_webhook_delivery_fanout_repository_with_authority(%s,%s,%s)",
            ("fanout-progress", fanout_gen, "id:8301"),
        )
        fanout_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        fanout_defer = one(
            APP_DSN,
            "SELECT core.resolve_webhook_delivery_fanout_defer_with_authority(%s,%s,%s,%s)",
            ("fanout-progress", fanout_gen, fanout_at, "fanout progress"),
        )
        check(
            "proven fanout checkpoint defer resets the durable retry window",
            fanout_defer == "deferred"
            and state("fanout-progress")["retry_expires"] is None,
        )

        # Reproduce the reported silence as a complete causal chain, not just
        # a health-probe result: one repo head is abandoned past its absolute
        # retry window, process replacement makes its lease reclaimable, and
        # the stale claim terminalizes it to a freshly-updated failed row. Its
        # already-aged same-repo follower must spend the one finite automatic
        # epoch immediately; waiting another escalation age from failed.updated
        # would recreate a >787-second lane outage. Another account remains
        # claimable throughout.
        recovery_repo = push_payload(1351, "recovery/bounded")
        enqueue_sql(
            "replacement-head", "push", "acct-replacement",
            recovery_repo,
        )
        enqueue_sql(
            "replacement-follower", "push", "acct-replacement",
            recovery_repo,
        )
        enqueue_sql(
            "replacement-other", "push", "acct-independent",
            push_payload(1352, "independent/live"),
        )
        replacement_initial = claim_sql(
            "replacement-head", max_attempts=3, window=5,
        )
        replacement_generation = int(
            replacement_initial["lease_generation"])
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery "
            "SET received_at=clock_timestamp()-make_interval("
            "secs=>%s + CASE WHEN delivery_key='replacement-head' "
            "THEN 2 ELSE 1 END),"
            "locked_at=CASE WHEN delivery_key='replacement-head' "
            "THEN clock_timestamp()-interval '101 seconds' "
            "ELSE locked_at END,"
            "retry_window_expires_at=CASE "
            "WHEN delivery_key='replacement-head' "
            "THEN clock_timestamp()-interval '1 second' "
            "ELSE retry_window_expires_at END "
            "WHERE delivery_key IN "
            "('replacement-head','replacement-follower')",
            (DQ._DEFERRED_ESCALATE_SECONDS,),
        )
        replacement_terminal = claim_sql(
            "replacement-head", max_attempts=3, window=5,
        )
        replacement_failed_state = state("replacement-head")
        blocked_before_rearm = claim_sql(
            "replacement-follower", window=5,
        )
        independent_started = time.monotonic()
        independent = claim_sql("replacement-other", window=5)
        independent_elapsed = time.monotonic() - independent_started
        assert one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (
                "replacement-other",
                int(independent["lease_generation"]),
            ),
        )
        convergence_started = time.monotonic()
        replacement_escalation = one(
            APP_DSN,
            "SELECT core.escalate_blocked_webhook_deliveries_with_authority("
            "%s,100,1)",
            (DQ._DEFERRED_ESCALATE_SECONDS,),
        )
        replacement_retry = claim_sql(
            "replacement-head", max_attempts=3, window=5,
        )
        assert one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (
                "replacement-head",
                int(replacement_retry["lease_generation"]),
            ),
        )
        replacement_follower = claim_sql(
            "replacement-follower", window=5,
        )
        convergence_elapsed = time.monotonic() - convergence_started
        assert one(
            APP_DSN,
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (
                "replacement-follower",
                int(replacement_follower["lease_generation"]),
            ),
        )
        default_recovery_ceiling = (
            # worker hard envelope + fixed continuity grace
            DQ._DEFAULT_DEFERRED_ESCALATE_SECONDS + 60
            # maximum configured shutdown drain + early-lease grace
            + 25 + DQ._SHUTDOWN_GRACE_SECONDS
            # one recovery tick + the sole fresh durable epoch
            + DQ._RECOVER_INTERVAL + DQ._DELIVERY_RETRY_WINDOW_SECONDS
        )
        check(
            "stuck replacement unfreezes its same-repo follower below the reported 787s while another account progresses",
            replacement_initial.get("claimed")
            and replacement_generation >= 1
            and replacement_terminal.get("reason") == "failed"
            and replacement_terminal.get("status") == "failed"
            and replacement_failed_state.get("error")
            == "retry_window_exhausted"
            and state("replacement-head")["status"] == "done"
            and blocked_before_rearm.get("reason") == "blocked_by_earlier"
            and independent.get("claimed")
            and independent_elapsed < 1.0
            and replacement_escalation.get("escalated") == 1
            and replacement_retry.get("claimed")
            and replacement_follower.get("claimed")
            and convergence_elapsed < 1.0
            and DQ._DEFERRED_ESCALATE_SECONDS == 120
            and default_recovery_ceiling < 787,
            detail={
                "replacement_terminal": replacement_terminal,
                "replacement_failed_state": replacement_failed_state,
                "blocked_before_rearm": blocked_before_rearm,
                "independent": independent,
                "replacement_escalation": replacement_escalation,
                "replacement_retry": replacement_retry,
                "replacement_follower": replacement_follower,
                "convergence_elapsed": convergence_elapsed,
                "default_recovery_ceiling": default_recovery_ceiling,
            },
        )

        # One automatic epoch is shared by slow DLQ rearm and aged-blocker
        # escalation. A second poison failure remains a visible causal barrier.
        blocker_body = installation_payload(1401)
        enqueue_sql("finite-blocker", "installation", "acct-finite", blocker_body)
        enqueue_sql("finite-follower", "push", "acct-finite",
                    push_payload(9401, "finite/follower"))
        enqueue_sql("other-account", "push", "acct-other",
                    push_payload(9501, "other/free"))
        initial = claim_sql("finite-blocker", max_attempts=1, window=5)
        release_sql("finite-blocker", int(initial["lease_generation"]),
                    max_attempts=1, error="poison first epoch")
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours',"
            "received_at=clock_timestamp()-interval '3 hours' "
            "WHERE delivery_key='finite-blocker';"
            "UPDATE core.webhook_delivery SET received_at=clock_timestamp()-interval '2 hours' "
            "WHERE delivery_key='finite-follower'",
        )
        first_rearm = one(
            APP_DSN,
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(1,100,1)",
        )
        second_epoch = claim_sql("finite-blocker", max_attempts=1, window=5)
        second_generation = int(second_epoch["lease_generation"])
        release_sql("finite-blocker", second_generation,
                    max_attempts=1, error="poison second epoch")
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours' "
            "WHERE delivery_key='finite-blocker'",
        )
        second_rearm = one(
            APP_DSN,
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(1,100,1)",
        )
        escalation = one(
            APP_DSN,
            "SELECT core.escalate_blocked_webhook_deliveries_with_authority(60,100,1)",
        )
        follower = claim_sql("finite-follower", window=5)
        other_started = time.monotonic()
        other = claim_sql("other-account", window=5)
        other_elapsed = time.monotonic() - other_started
        finite_state = state("finite-blocker")
        check(
            "automatic rearm is exactly one; failed head stays visible while another account claims promptly",
            first_rearm.get("rearmed") == 1
            and finite_state["auto_rearms"] == 1
            and second_rearm.get("rearmed") == 0
            and escalation.get("escalated") == 0
            and follower.get("reason") == "blocked_by_earlier"
            and other.get("claimed") and other_elapsed < 1.0,
            detail={
                "first_rearm": first_rearm,
                "second_epoch": second_epoch,
                "second_rearm": second_rearm,
                "escalation": escalation,
                "follower": follower,
                "other": other,
                "finite_state": finite_state,
            },
        )

        # If automatic rearm wins just before a signed GitHub duplicate, the
        # row is already queued but auto_rearm_count=1. Explicit ingress still
        # owns a fresh epoch and must reset that counter; outcome cannot depend
        # on which of the two serialized transactions arrived first.
        race_body = push_payload(1451, "race/rearm")
        enqueue_sql("rearm-redelivery-race", "push", "acct-race", race_body)
        race_claim = claim_sql(
            "rearm-redelivery-race", max_attempts=1, window=5,
        )
        release_sql(
            "rearm-redelivery-race", int(race_claim["lease_generation"]),
            max_attempts=1, error="race poison",
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery "
            "SET updated_at=clock_timestamp()-interval '2 hours' "
            "WHERE delivery_key='rearm-redelivery-race'",
        )
        race_rearm = one(
            APP_DSN,
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(1,100,1)",
        )
        after_race_rearm = state("rearm-redelivery-race")
        race_redelivery = enqueue_sql(
            "rearm-redelivery-race", "push", "acct-race", race_body,
        )
        after_race_redelivery = state("rearm-redelivery-race")
        check(
            "signed redelivery resets the explicit epoch even when automatic rearm won first",
            race_rearm.get("rearmed") == 1
            and after_race_rearm["status"] == "queued"
            and after_race_rearm["auto_rearms"] == 1
            and race_redelivery.get("accepted")
            and race_redelivery.get("queued")
            and after_race_redelivery["status"] == "queued"
            and after_race_redelivery["attempts"] == 0
            and after_race_redelivery["auto_rearms"] == 0
            and after_race_redelivery["retry_expires"] is None,
        )

        # The same signed redelivery can arrive after the automatically
        # re-armed generation has already claimed. It must not steal that
        # exact lease, but it must reserve a fresh finite epoch if the current
        # handler later fails.
        processing_race_body = push_payload(1452, "race/processing")
        enqueue_sql(
            "processing-redelivery-race", "push", "acct-processing-race",
            processing_race_body,
        )
        processing_initial = claim_sql(
            "processing-redelivery-race", max_attempts=1, window=5,
        )
        release_sql(
            "processing-redelivery-race",
            int(processing_initial["lease_generation"]),
            max_attempts=1, error="processing race first epoch",
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery "
            "SET updated_at=clock_timestamp()-interval '4 hours' "
            "WHERE delivery_key='processing-redelivery-race'",
        )
        processing_first_rearm = one(
            APP_DSN,
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(1,100,1)",
        )
        processing_claim = claim_sql(
            "processing-redelivery-race", max_attempts=1, window=5,
        )
        processing_before_duplicate = state("processing-redelivery-race")
        processing_duplicate = enqueue_sql(
            "processing-redelivery-race", "push", "acct-processing-race",
            processing_race_body,
        )
        processing_after_duplicate = state("processing-redelivery-race")
        processing_release = release_sql(
            "processing-redelivery-race",
            int(processing_claim["lease_generation"]),
            max_attempts=1, error="processing race second epoch",
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery "
            "SET updated_at=clock_timestamp()-interval '4 hours' "
            "WHERE delivery_key='processing-redelivery-race'",
        )
        processing_second_rearm = one(
            APP_DSN,
            "SELECT core.rearm_failed_webhook_deliveries_with_authority(1,100,1)",
        )
        processing_after_rearm = state("processing-redelivery-race")
        check(
            "signed redelivery during a re-armed claim preserves its lease and reserves one fresh epoch",
            processing_first_rearm.get("rearmed") == 1
            and processing_before_duplicate["status"] == "processing"
            and processing_before_duplicate["auto_rearms"] == 1
            and processing_duplicate.get("accepted")
            and processing_duplicate.get("status") == "processing"
            and processing_after_duplicate["status"] == "processing"
            and processing_after_duplicate["attempts"]
                == processing_before_duplicate["attempts"]
            and processing_after_duplicate["generation"]
                == processing_before_duplicate["generation"]
            and processing_after_duplicate["owner"]
                == processing_before_duplicate["owner"]
            and processing_after_duplicate["retry_expires"]
                == processing_before_duplicate["retry_expires"]
            and processing_after_duplicate["auto_rearms"] == 0
            and processing_release == "failed"
            and processing_second_rearm.get("rearmed") == 1
            and processing_after_rearm["status"] == "queued"
            and processing_after_rearm["auto_rearms"] == 1,
            detail={
                "first_rearm": processing_first_rearm,
                "before_duplicate": processing_before_duplicate,
                "duplicate": processing_duplicate,
                "after_duplicate": processing_after_duplicate,
                "release": processing_release,
                "second_rearm": processing_second_rearm,
                "after_rearm": processing_after_rearm,
            },
        )
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET status='done',done_at=clock_timestamp() "
            "WHERE delivery_key='processing-redelivery-race'",
        )

        # A genuine ingress duplicate is explicit redelivery authority. It
        # resets the epoch but preserves immutable order/generation and frozen
        # fanout proof. A stale generation cannot mutate the new epoch.
        frozen_plan = [{"key": "id:8401", "full_name": "acct-1401/one", "id": "8401"}]
        frozen_completed = {"id:8401": True}
        one(
            OWNER_DSN,
            "UPDATE core.webhook_delivery SET payload=payload||jsonb_build_object("
            "'_veripsa_fanout_plan',%s::jsonb,'_veripsa_fanout_completed',%s::jsonb) "
            "WHERE delivery_key='finite-blocker'",
            (Json(frozen_plan), Json(frozen_completed)),
        )
        before_redelivery = state("finite-blocker")
        redelivery_store = DQ.DeliveryStore(
            APP_DSN, max_pending=1000, max_attempts=1,
            retry_window_seconds=5,
        )
        redelivery = redelivery_store.submit(
            "installation", blocker_body, "finite-blocker",
            account_key="acct-finite",
        )
        after_redelivery = state("finite-blocker")
        new_claim = claim_sql("finite-blocker", max_attempts=1, window=5)
        stale_release = release_sql(
            "finite-blocker", second_generation, max_attempts=1,
            error="stale predecessor",
        )
        check(
            "explicit duplicate alone resets epoch while preserving order, generation, and fanout proof",
            redelivery.get("accepted") and redelivery.get("queued")
            and after_redelivery["status"] == "queued"
            and after_redelivery["attempts"] == 0
            and after_redelivery["auto_rearms"] == 0
            and after_redelivery["retry_expires"] is None
            and after_redelivery["received_at"] == before_redelivery["received_at"]
            and after_redelivery["generation"] == before_redelivery["generation"]
            and after_redelivery["payload"]["_veripsa_fanout_plan"] == frozen_plan
            and after_redelivery["payload"]["_veripsa_fanout_completed"] == frozen_completed
            and new_claim.get("claimed")
            and stale_release == "ownership_lost",
        )

        depth = redelivery_store.depth()
        check(
            "content-free depth exposes retry-window and finite-rearm states",
            all(name in depth for name in (
                "retry_window_active", "retry_window_exhausted_failed",
                "auto_rearm_eligible_failed", "auto_rearm_exhausted_failed",
            )),
        )

        # Real DeliveryStore wrapper + EventQueue: the DB's one-second
        # remaining window narrows the fresh 90-second dequeue budget, cancels
        # the handler, and the durable marker prevents any inline rerun.
        runtime_store = DQ.DeliveryStore(
            APP_DSN, max_pending=1000, max_attempts=3,
            retry_window_seconds=2,
        )
        runtime_submit = runtime_store.submit(
            "push", push_payload(1601, "runtime/window"), "runtime-window",
            account_key="acct-runtime", repo="runtime/window",
        )
        handler_calls = 0

        def expiring_handler(_event_type, _payload, _db, _gh, coalesce=None):
            nonlocal handler_calls
            handler_calls += 1
            while True:
                event_budget.raise_if_expired()
                time.sleep(0.01)

        wrapped = runtime_store.wrap_processor(expiring_handler)
        queue = EventQueue(
            None, None, wrapped, retry_attempts=3, retry_base_seconds=5,
        ).start()
        runtime_started = time.monotonic()
        queue.submit("push", runtime_submit["payload"], "runtime-window")
        runtime_idle = queue.wait_idle(4.0)
        runtime_elapsed = time.monotonic() - runtime_started
        runtime_state = state("runtime-window")
        check(
            "real wrapper narrows DB remaining time and one dequeue executes one handler generation",
            getattr(wrapped, "_veripsa_one_durable_attempt_per_dequeue", False)
            and runtime_idle and handler_calls == 1
            and queue.retried() == 0 and queue.failed() == 1
            and runtime_elapsed < 2.5
            and runtime_state["status"] in ("queued", "failed")
            and runtime_state["owner"] is None
            and not runtime_state["locked"],
        )

        # A terminal resolver intent older than the safe dead-owner age must
        # fail health so restart + dead-owner reaping converges a permanent
        # resolver error instead of preserving its heartbeat for 1800 seconds.
        health_store = DQ.DeliveryStore(APP_DSN)
        health_store._liveness_thread = _AliveThread()
        health_store._last_successful_heartbeat_at = time.monotonic()
        identity = ("release", "health-key", 1, ("error", 3))
        with health_store._pending_terminal_lock:
            health_store._pending_terminals[identity] = {
                "kind": "release", "key": "health-key", "generation": 1,
                "args": ("error", 3),
                "created_at": (
                    time.monotonic()
                    - DQ._DEAD_INSTANCE_RECLAIM_SAFE_SECONDS + 0.2
                ),
                "next_attempt_at": time.monotonic(), "attempts": 1,
            }
        just_before = health_store.liveness_snapshot()
        with health_store._pending_terminal_lock:
            health_store._pending_terminals[identity]["created_at"] = (
                time.monotonic() - DQ._DEAD_INSTANCE_RECLAIM_SAFE_SECONDS
            )
        expired_health = health_store.liveness_snapshot()
        check(
            "pending terminal is healthy just before safe age and unhealthy at/after it",
            just_before["healthy"] is True
            and expired_health["healthy"] is False
            and expired_health["pending_terminal_depth"] == 1,
        )

        passed = all(result for _, result in CHECKS)
        print("DURABLE RETRY WINDOW:", "PASS" if passed else "FAIL")
        return 0 if passed else 1
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)


if __name__ == "__main__":
    raise SystemExit(main())
