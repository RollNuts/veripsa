#!/usr/bin/env python3
"""Real-Postgres and offline-HTTP proof for bounded terminal recovery."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import inspect
import io
import json
import os
import subprocess
import sys
import threading

import psycopg2
from psycopg2.extras import Json


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "github-app")
for path in (ROOT, APP):
    if path not in sys.path:
        sys.path.insert(0, path)

import delivery_queue as DQ  # noqa: E402
import event_queue as EQ  # noqa: E402
import server_http as SH  # noqa: E402


DB = f"veripsa_terminal_recovery_runtime_{os.getpid()}"
APP_DSN = f"postgresql://veripsa_app@localhost/{DB}"
OWNER_DSN = f"postgresql://veripsa_migrator@localhost/{DB}"
WRITER_DSN = f"postgresql://veripsa_demo_agent@localhost/{DB}"
SECRET = "terminal-recovery-runtime-secret"
RUNTIME_SHA = "a" * 40
RECOVERY_ID = "deploy:terminal-recovery:001"
CONTINUATION_ID = "deploy:terminal-continuation:001"

ELIGIBLE_A = "11111111-1111-4111-8111-00000000000a"
ELIGIBLE_B = "00000000-0000-4000-8000-000000000032"
AUTO_ZERO = "00000000-0000-4000-8000-000000000024"
LOCKED = "00000000-0000-4000-8000-000000000004"
EMPTY_PAYLOAD = "00000000-0000-4000-8000-000000000009"
MIXED_MISSING_CANDIDATE = "00000000-0000-4000-8000-000000000012"
MISSING = "00000000-0000-4000-8000-000000000014"
MIXED_INELIGIBLE_CANDIDATE = "00000000-0000-4000-8000-000000000016"
MIXED_ALREADY_CANDIDATE = "00000000-0000-4000-8000-000000000019"
VALIDATION_CANDIDATE = "00000000-0000-4000-8000-000000000021"
RACE = "00000000-0000-4000-8000-000000000023"
LEGACY = "00000000-0000-4000-8000-000000000031"
NOT_FAILED = "00000000-0000-4000-8000-000000000036"
STATUS_QUEUED = "00000000-0000-4000-8000-000000000045"
STATUS_PROCESSING = "00000000-0000-4000-8000-000000000046"
STATUS_DONE = "00000000-0000-4000-8000-000000000047"
STATUS_FAILED = "00000000-0000-4000-8000-000000000048"
STATUS_WRONG_ID = "00000000-0000-4000-8000-000000000049"
STATUS_MISSING = "00000000-0000-4000-8000-000000000038"
STATUS_NEVER_A = "00000000-0000-4000-8000-000000000037"
STATUS_NEVER_B = "00000000-0000-4000-8000-000000000039"
STATUS_INVALID_DONE = "00000000-0000-4000-8000-000000000040"
CONTINUATION_FAILED = "00000000-0000-4000-8000-000000000041"
CONTINUATION_DONE = "00000000-0000-4000-8000-000000000042"
CONTINUATION_RACE = "00000000-0000-4000-8000-000000000043"
CONTINUATION_LOCK_WAIT = "00000000-0000-4000-8000-000000000044"

RESULT_KEYS = {
    "status", "requested", "rearmed", "already_recovered",
    "ineligible", "missing", "results",
}
STATUS_RESULT_KEYS = {"status", "requested"}
STATE_COLUMNS = (
    "status", "attempts", "received_at", "updated_at", "locked_at",
    "done_at", "last_error", "not_before", "lease_generation",
    "causal_order_version", "owner_instance", "retry_window_expires_at",
    "auto_rearm_count", "operator_recovery_id", "operator_recovered_at",
    "operator_recovery_batch_size", "operator_recovery_batch_token",
    "operator_recovery_count", "operator_continuation_id",
    "operator_continued_at", "operator_continuation_sha",
    "operator_continuation_count", "event_type", "account_key", "repo", "payload",
)
FAILURES = 0
DB_BATCH_TOKENS: set[str] = set()


def check(condition, label: str, *, detail=None) -> None:
    global FAILURES
    passed = bool(condition)
    print(("  [PASS] " if passed else "  [FAIL] ") + label)
    if not passed:
        FAILURES += 1
        if detail is not None:
            print(f"    detail: {detail!r}")


def run_log_privacy_checks() -> None:
    class FailingRowError(Exception):
        pgcode = "23514"

        def __str__(self) -> str:
            return f"DETAIL: Failing row contains ({ELIGIBLE_A}, private-payload)"

    unsafe = FailingRowError()
    codes = (
        SH._bounded_error_code(unsafe),
        DQ._bounded_error_code(unsafe),
        EQ._bounded_error_code(unsafe),
    )
    handler_source = inspect.getsource(SH.make_handler)
    persist_block = handler_source[
        handler_source.index("durable inbox persist FAILED"):
        handler_source.index("if not res.get(\"accepted\")")
    ]
    check(
        codes == ("exception_failingrowerror_sqlstate_23514",) * 3
        and all(ELIGIBLE_A not in code and "payload" not in code for code in codes)
        and "_bounded_error_code(e)" in persist_block
        and "str(e)" not in persist_block,
        "ingress and worker error logs retain only exception class/SQLSTATE, never failing-row text",
        detail={"codes": codes, "persist_block": persist_block},
    )

    poisoned_state = type("PoisonedState", (Exception,), {"pgcode": "23\n14"})()
    poisoned_code = SH._bounded_error_code(poisoned_state)
    check(
        "sqlstate" not in poisoned_code and "\n" not in poisoned_code,
        "malformed SQLSTATE cannot inject text into the bounded error code",
        detail=poisoned_code,
    )


def scalar(dsn: str, sql: str, args=()):
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


def attempted(dsn: str, sql: str, args=()) -> dict:
    conn = psycopg2.connect(dsn)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            try:
                cur.execute(sql, args)
                row = cur.fetchone()
                return {"value": row[0] if row else None}
            except psycopg2.Error as exc:
                return {"pgcode": exc.pgcode, "message": str(exc)}
    finally:
        conn.close()


def read_only_scalar(dsn: str, sql: str, args=()):
    conn = psycopg2.connect(dsn)
    try:
        conn.set_session(readonly=True, autocommit=False)
        with conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            conn.rollback()
            return row[0] if row else None
    finally:
        conn.close()


def payload_for(guid: str) -> dict:
    return {
        "ref": "refs/heads/main",
        "after": hashlib.sha1(guid.encode("ascii")).hexdigest(),
        "commits": [],
        "repository": {
            "id": 70001,
            "full_name": "acme/recovery-proof",
            "default_branch": "main",
            "owner": {"id": 700, "login": "acme", "type": "Organization"},
        },
        "sender": {"id": 701, "login": "octo", "type": "User"},
    }


def seed(
        guid: str, *, status: str = "failed", auto_rearms: int = 1,
        causal_version: int = 1, payload: dict | None = None,
        locked: bool = False) -> None:
    now = datetime.now(timezone.utc)
    locked_at = now - timedelta(minutes=10) if locked else None
    owner = "wk-" + "c" * 32 if locked else None
    scalar(
        OWNER_DSN,
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,attempts,"
        "received_at,updated_at,locked_at,last_error,not_before,lease_generation,"
        "causal_order_version,owner_instance,retry_window_expires_at,auto_rearm_count) "
        "VALUES(%s,'push','700','acme/recovery-proof',%s,%s,7,%s,%s,%s,"
        "'retry_window_exhausted',%s,19,%s,%s,%s,%s) RETURNING delivery_key",
        (
            guid, Json(payload_for(guid) if payload is None else payload), status,
            now - timedelta(hours=4), now - timedelta(hours=2), locked_at,
            now + timedelta(minutes=5), causal_version, owner,
            now - timedelta(hours=1), auto_rearms,
        ),
    )


def state(guid: str) -> dict:
    conn = psycopg2.connect(OWNER_DSN)
    try:
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT " + ",".join(STATE_COLUMNS)
                + " FROM core.webhook_delivery WHERE delivery_key=%s",
                (guid,),
            )
            row = cur.fetchone()
            return dict(zip(STATE_COLUMNS, row)) if row else {}
    finally:
        conn.close()


def result_for(results: list[str]) -> dict:
    return {
        "status": "ok",
        "requested": len(results),
        "rearmed": results.count("rearmed"),
        "already_recovered": results.count("already_recovered"),
        "ineligible": results.count("ineligible"),
        "missing": results.count("missing"),
        "results": results,
    }


def status_result_for(status: str, requested: int) -> dict:
    return {"status": status, "requested": requested}


def continuation_result_for(results: list[str]) -> dict:
    return {
        "status": "ok",
        "requested": len(results),
        "continued": results.count("continued"),
        "already_completed": results.count("already_completed"),
        "already_continued": results.count("already_continued"),
        "ineligible": results.count("ineligible"),
        "missing": results.count("missing"),
        "results": results,
    }


def bootstrap() -> None:
    completed = subprocess.run(
        ["bash", "db/bootstrap_local.sh", DB],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if completed.returncode:
        raise RuntimeError(
            "bootstrap failed:\n" + (completed.stderr or completed.stdout)[-1800:])


def function_contract() -> dict:
    return scalar(
        OWNER_DSN,
        "SELECT jsonb_build_object("
        "'overloads',(SELECT count(*)::int FROM pg_proc q "
        " JOIN pg_namespace n ON n.oid=q.pronamespace "
        " WHERE n.nspname='core' "
        " AND q.proname='recover_terminal_webhook_deliveries_with_authority'),"
        "'args',pg_catalog.oidvectortypes(p.proargtypes),"
        "'security_definer',p.prosecdef,'owner',r.rolname,"
        "'fixed_path',COALESCE(p.proconfig,'{}'::text[]) "
        " @> ARRAY['search_path=core, pg_catalog'],"
        "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
        "'writer_execute',has_function_privilege('veripsa_demo_agent',p.oid,'EXECUTE'),"
        "'public_execute',EXISTS(SELECT 1 FROM aclexplode("
        " COALESCE(p.proacl,acldefault('f',p.proowner))) a "
        " WHERE a.grantee=0 AND a.privilege_type='EXECUTE'),"
        "'app_select',has_table_privilege('veripsa_app','core.webhook_delivery','SELECT'),"
        "'app_insert',has_table_privilege('veripsa_app','core.webhook_delivery','INSERT'),"
        "'app_update',has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE'),"
        "'app_delete',has_table_privilege('veripsa_app','core.webhook_delivery','DELETE')) "
        "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid='core.recover_terminal_webhook_deliveries_with_authority(text[],text)'::regprocedure",
    )


def status_function_contract() -> dict:
    return scalar(
        OWNER_DSN,
        "SELECT jsonb_build_object("
        "'overloads',(SELECT count(*)::int FROM pg_proc q "
        " JOIN pg_namespace n ON n.oid=q.pronamespace "
        " WHERE n.nspname='core' "
        " AND q.proname='terminal_webhook_delivery_recovery_status_with_authority'),"
        "'args',pg_catalog.oidvectortypes(p.proargtypes),"
        "'security_definer',p.prosecdef,'volatility',p.provolatile,'owner',r.rolname,"
        "'fixed_path',COALESCE(p.proconfig,'{}'::text[]) "
        " @> ARRAY['search_path=core, pg_catalog'],"
        "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
        "'writer_execute',has_function_privilege('veripsa_demo_agent',p.oid,'EXECUTE'),"
        "'public_execute',EXISTS(SELECT 1 FROM aclexplode("
        " COALESCE(p.proacl,acldefault('f',p.proowner))) a "
        " WHERE a.grantee=0 AND a.privilege_type='EXECUTE'),"
        "'app_select',has_table_privilege('veripsa_app','core.webhook_delivery','SELECT'),"
        "'app_insert',has_table_privilege('veripsa_app','core.webhook_delivery','INSERT'),"
        "'app_update',has_table_privilege('veripsa_app','core.webhook_delivery','UPDATE'),"
        "'app_delete',has_table_privilege('veripsa_app','core.webhook_delivery','DELETE')) "
        "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid="
        "'core.terminal_webhook_delivery_recovery_status_with_authority(text[])'::regprocedure",
    )


def continuation_function_contract() -> dict:
    return scalar(
        OWNER_DSN,
        "SELECT jsonb_build_object("
        "'overloads',(SELECT count(*)::int FROM pg_proc q "
        " JOIN pg_namespace n ON n.oid=q.pronamespace "
        " WHERE n.nspname='core' "
        " AND q.proname='continue_terminal_webhook_deliveries_with_authority'),"
        "'args',pg_catalog.oidvectortypes(p.proargtypes),"
        "'security_definer',p.prosecdef,'owner',r.rolname,"
        "'fixed_path',COALESCE(p.proconfig,'{}'::text[]) "
        " @> ARRAY['search_path=core, pg_catalog'],"
        "'app_execute',has_function_privilege('veripsa_app',p.oid,'EXECUTE'),"
        "'writer_execute',has_function_privilege('veripsa_demo_agent',p.oid,'EXECUTE'),"
        "'public_execute',EXISTS(SELECT 1 FROM aclexplode("
        " COALESCE(p.proacl,acldefault('f',p.proowner))) a "
        " WHERE a.grantee=0 AND a.privilege_type='EXECUTE')) "
        "FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid="
        "'core.continue_terminal_webhook_deliveries_with_authority(text[],text,text)'::regprocedure",
    )


def rearmed_safely(
        guid: str, before: dict, recovery_id: str, batch_guids: list[str]) -> bool:
    after = state(guid)
    return bool(
        after.get("status") == "queued"
        and after.get("attempts") == 0
        and after.get("locked_at") is None
        and after.get("owner_instance") is None
        and after.get("not_before") is None
        and after.get("retry_window_expires_at") is None
        and after.get("done_at") is None
        and after.get("last_error") is None
        and after.get("auto_rearm_count") == 2
        and after.get("operator_recovery_id") == recovery_id
        and after.get("operator_recovered_at") is not None
        and after.get("operator_recovery_batch_size") == len(batch_guids)
        and after.get("operator_recovery_batch_token") is not None
        and after.get("operator_recovery_count") == 1
        and after.get("payload") == before.get("payload")
        and after.get("received_at") == before.get("received_at")
        and after.get("lease_generation") == before.get("lease_generation")
        and after.get("causal_order_version") == before.get("causal_order_version")
        and after.get("event_type") == before.get("event_type")
        and after.get("account_key") == before.get("account_key")
        and after.get("repo") == before.get("repo")
        and after.get("updated_at") >= before.get("updated_at")
    )


def run_database_checks() -> None:
    contract = function_contract()
    check(
        contract == {
            "overloads": 1, "args": "text[], text", "security_definer": True,
            "owner": "veripsa_migrator", "fixed_path": True,
            "app_execute": True, "writer_execute": False,
            "public_execute": False, "app_select": False,
            "app_insert": False, "app_update": False, "app_delete": False,
        },
        "only the App can execute one text[]/text SECURITY DEFINER surface; no payload or table privilege is exposed",
        detail=contract,
    )
    status_contract = status_function_contract()
    check(
        status_contract == {
            "overloads": 1, "args": "text[]", "security_definer": True,
            "volatility": "s", "owner": "veripsa_migrator", "fixed_path": True,
            "app_execute": True, "writer_execute": False,
            "public_execute": False, "app_select": False,
            "app_insert": False, "app_update": False, "app_delete": False,
        },
        "durable status is one App-only STABLE SECURITY DEFINER surface without table privileges",
        detail=status_contract,
    )
    continuation_contract = continuation_function_contract()
    check(
        continuation_contract == {
            "overloads": 1, "args": "text[], text, text",
            "security_definer": True, "owner": "veripsa_migrator",
            "fixed_path": True, "app_execute": True,
            "writer_execute": False, "public_execute": False,
        },
        "one App-only exact-SHA continuation surface exists without writer or PUBLIC authority",
        detail=continuation_contract,
    )
    writer_denied = attempted(
        WRITER_DSN,
        "SELECT core.recover_terminal_webhook_deliveries_with_authority(%s,%s)",
        ([ELIGIBLE_A], RECOVERY_ID),
    )
    table_denied = attempted(
        APP_DSN,
        "UPDATE core.webhook_delivery SET attempts=0 WHERE delivery_key=%s RETURNING attempts",
        (ELIGIBLE_A,),
    )
    status_writer_denied = attempted(
        WRITER_DSN,
        "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
        ([ELIGIBLE_A],),
    )
    continuation_writer_denied = attempted(
        WRITER_DSN,
        "SELECT core.continue_terminal_webhook_deliveries_with_authority(%s,%s,%s)",
        ([ELIGIBLE_A], CONTINUATION_ID, RUNTIME_SHA),
    )
    check(
        writer_denied.get("pgcode") == "42501"
        and continuation_writer_denied.get("pgcode") == "42501"
        and status_writer_denied.get("pgcode") == "42501"
        and table_denied.get("pgcode") == "42501",
        "writer recovery/status execution and direct App inbox mutation are denied at runtime",
        detail={
            "writer_recovery": writer_denied,
            "writer_status": status_writer_denied,
            "writer_continuation": continuation_writer_denied,
            "table": table_denied,
        },
    )

    fixtures = (
        (ELIGIBLE_A, {}), (ELIGIBLE_B, {}),
        (AUTO_ZERO, {"auto_rearms": 0}), (LOCKED, {"locked": True}),
        (EMPTY_PAYLOAD, {"payload": {}}), (MIXED_MISSING_CANDIDATE, {}),
        (MIXED_INELIGIBLE_CANDIDATE, {}), (MIXED_ALREADY_CANDIDATE, {}),
        (VALIDATION_CANDIDATE, {}), (RACE, {}),
        (LEGACY, {"causal_version": 0}), (NOT_FAILED, {"status": "queued"}),
        (STATUS_QUEUED, {}), (STATUS_PROCESSING, {}), (STATUS_DONE, {}),
        (STATUS_FAILED, {}), (STATUS_WRONG_ID, {}),
        (STATUS_NEVER_A, {"status": "done"}),
        (STATUS_NEVER_B, {"status": "done"}),
        (STATUS_INVALID_DONE, {}),
        (CONTINUATION_FAILED, {}), (CONTINUATION_DONE, {}),
        (CONTINUATION_RACE, {}), (CONTINUATION_LOCK_WAIT, {}),
    )
    for guid, options in fixtures:
        seed(guid, **options)

    constraint_before = state(VALIDATION_CANDIDATE)
    missing_batch_size = attempted(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET "
        "operator_recovery_id='deploy:constraint:size',operator_recovered_at=now(),"
        "operator_recovery_batch_size=NULL,"
        "operator_recovery_batch_token=gen_random_uuid(),operator_recovery_count=1 "
        "WHERE delivery_key=%s RETURNING operator_recovery_count",
        (VALIDATION_CANDIDATE,),
    )
    missing_batch_token = attempted(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET "
        "operator_recovery_id='deploy:constraint:token',operator_recovered_at=now(),"
        "operator_recovery_batch_size=1,operator_recovery_batch_token=NULL,"
        "operator_recovery_count=1 WHERE delivery_key=%s "
        "RETURNING operator_recovery_count",
        (VALIDATION_CANDIDATE,),
    )
    missing_continuation_sha = attempted(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET "
        "operator_recovery_id='deploy:constraint:continuation',"
        "operator_recovered_at=now(),operator_recovery_batch_size=1,"
        "operator_recovery_batch_token=gen_random_uuid(),operator_recovery_count=1,"
        "operator_continuation_id='deploy:constraint:continuation',"
        "operator_continued_at=now(),operator_continuation_sha=NULL,"
        "operator_continuation_count=1 WHERE delivery_key=%s "
        "RETURNING operator_continuation_count",
        (VALIDATION_CANDIDATE,),
    )
    check(
        missing_batch_size.get("pgcode") == "23514"
        and missing_batch_token.get("pgcode") == "23514"
        and missing_continuation_sha.get("pgcode") == "23514"
        and state(VALIDATION_CANDIDATE) == constraint_before,
        "table CHECK rejects count=1 when recovery token/size or continuation SHA is NULL",
        detail={
            "size": missing_batch_size,
            "token": missing_batch_token,
            "continuation_sha": missing_continuation_sha,
        },
    )

    store = DQ.DeliveryStore(APP_DSN, max_pending=100, max_attempts=3)
    check(
        list(inspect.signature(DQ.DeliveryStore.recover_terminal).parameters)
        == ["self", "delivery_guids", "recovery_id"],
        "DeliveryStore recovery accepts only GUIDs and an audit id",
    )
    check(
        list(inspect.signature(DQ.DeliveryStore.terminal_recovery_status).parameters)
        == ["self", "delivery_guids"],
        "DeliveryStore durable status accepts only exact GUIDs",
    )
    check(
        list(inspect.signature(DQ.DeliveryStore.continue_terminal).parameters)
        == ["self", "delivery_guids", "continuation_id", "exact_sha"],
        "DeliveryStore continuation requires the exact batch, audit id, and runtime SHA",
    )

    before = {guid: state(guid) for guid in (ELIGIBLE_A, ELIGIBLE_B)}
    recovered = store.recover_terminal([ELIGIBLE_B, ELIGIBLE_A], RECOVERY_ID)
    eligible_batch_token = state(ELIGIBLE_A).get("operator_recovery_batch_token")
    if isinstance(eligible_batch_token, str):
        DB_BATCH_TOKENS.add(eligible_batch_token)
    check(
        recovered == result_for(["rearmed", "rearmed"])
        and rearmed_safely(
            ELIGIBLE_A, before[ELIGIBLE_A], RECOVERY_ID,
            [ELIGIBLE_B, ELIGIBLE_A])
        and rearmed_safely(
            ELIGIBLE_B, before[ELIGIBLE_B], RECOVERY_ID,
            [ELIGIBLE_B, ELIGIBLE_A])
        and state(ELIGIBLE_A).get("operator_recovery_batch_token")
        == state(ELIGIBLE_B).get("operator_recovery_batch_token"),
        "all-eligible batch resets retry state, preserves stored payload/lease coordinates, and stamps one audit",
        detail={"result": recovered, "a": state(ELIGIBLE_A), "b": state(ELIGIBLE_B)},
    )
    encoded = json.dumps(recovered, sort_keys=True, separators=(",", ":"))
    check(
        set(recovered) == RESULT_KEYS
        and all(token not in encoded for token in (
            ELIGIBLE_A, ELIGIBLE_B, RECOVERY_ID, "acme/recovery-proof",
            *DB_BATCH_TOKENS,
        )),
        "database result is positional and content-free",
        detail=recovered,
    )

    status_recovery_guids = [STATUS_QUEUED, STATUS_PROCESSING, STATUS_DONE]
    status_recovery_id = "deploy:terminal-recovery:status"
    status_recovered = store.recover_terminal(
        status_recovery_guids, status_recovery_id)
    failed_status_id = "deploy:terminal-recovery:failed"
    failed_status_recovered = store.recover_terminal(
        [STATUS_FAILED], failed_status_id)
    invalid_done_id = "deploy:terminal-recovery:invalid-done"
    invalid_done_recovered = store.recover_terminal(
        [STATUS_INVALID_DONE], invalid_done_id)
    wrong_status_id = "deploy:terminal-recovery:wrong-owner"
    wrong_status_recovered = store.recover_terminal(
        [STATUS_WRONG_ID], wrong_status_id)
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='processing',locked_at=now(),"
        "owner_instance=%s WHERE delivery_key=%s RETURNING status",
        ("wk-" + "d" * 32, STATUS_PROCESSING),
    )
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',done_at=now(),locked_at=NULL,"
        "owner_instance=NULL WHERE delivery_key=%s RETURNING status",
        (STATUS_DONE,),
    )
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='failed',last_error='terminal after recovery',"
        "locked_at=NULL,owner_instance=NULL WHERE delivery_key=%s RETURNING status",
        (STATUS_FAILED,),
    )
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',"
        "done_at=operator_recovered_at-interval '1 second',locked_at=NULL,"
        "owner_instance=NULL WHERE delivery_key=%s RETURNING status",
        (STATUS_INVALID_DONE,),
    )
    never_guids = [STATUS_NEVER_A, STATUS_NEVER_B]
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',done_at=now(),"
        "locked_at=NULL,owner_instance=NULL WHERE delivery_key=ANY(%s) RETURNING status",
        (never_guids,),
    )
    recovering_guids = [STATUS_DONE, STATUS_QUEUED, STATUS_PROCESSING]
    subset_guids = [STATUS_DONE, STATUS_QUEUED]
    status_guids = [
        *recovering_guids, STATUS_FAILED, STATUS_WRONG_ID,
        *never_guids, STATUS_INVALID_DONE,
    ]
    status_before = {
        guid: state(guid) for guid in status_guids
    }
    recovering_status = store.terminal_recovery_status(recovering_guids)
    subset_status = store.terminal_recovery_status(subset_guids)
    failed_status = store.terminal_recovery_status([STATUS_FAILED])
    unspent_status = store.terminal_recovery_status([AUTO_ZERO])
    never_status = store.terminal_recovery_status(never_guids)
    mixed_epoch_status = store.terminal_recovery_status(
        [STATUS_DONE, STATUS_WRONG_ID])
    mixed_audit_status = store.terminal_recovery_status(
        [STATUS_DONE, STATUS_NEVER_A])
    missing_status = store.terminal_recovery_status(
        [STATUS_DONE, STATUS_MISSING])
    invalid_done_status = store.terminal_recovery_status(
        [STATUS_INVALID_DONE])
    read_only_recovering = read_only_scalar(
        APP_DSN,
        "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
        (recovering_guids,),
    )
    status_after = {
        guid: state(guid) for guid in status_guids
    }
    observed_statuses = (
        recovering_status, subset_status, failed_status, unspent_status, never_status,
        mixed_epoch_status, mixed_audit_status, missing_status, invalid_done_status,
        read_only_recovering,
    )
    encoded_status = json.dumps(observed_statuses, separators=(",", ":"))
    check(
        status_recovered == result_for(["rearmed"] * 3)
        and failed_status_recovered == result_for(["rearmed"])
        and invalid_done_recovered == result_for(["rearmed"])
        and wrong_status_recovered == result_for(["rearmed"])
        and recovering_status == status_result_for("recovering", 3)
        and read_only_recovering == status_result_for("recovering", 3)
        and subset_status == status_result_for("unverified", 2)
        and failed_status == status_result_for("continuable", 1)
        and unspent_status == status_result_for("unspent", 1)
        and never_status == status_result_for("never_recovered", 2)
        and mixed_epoch_status == status_result_for("unverified", 2)
        and mixed_audit_status == status_result_for("unverified", 2)
        and missing_status == status_result_for("unverified", 2)
        and invalid_done_status == status_result_for("unverified", 1)
        and status_before == status_after
        and all(set(result) == STATUS_RESULT_KEYS for result in observed_statuses)
        and all(token not in encoded_status for token in (
            *status_guids, STATUS_MISSING, status_recovery_id,
            failed_status_id, invalid_done_id, wrong_status_id,
            "acme/recovery-proof",
        )),
        "durable status binds the complete stored opaque batch while a subset, missing, mixed epoch/audit, and invalid done proof stay generically unverified",
        detail={
            "statuses": observed_statuses,
            "before": status_before,
            "after": status_after,
        },
    )

    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',done_at=now(),"
        "locked_at=NULL,owner_instance=NULL WHERE delivery_key=ANY(%s) RETURNING status",
        ([STATUS_QUEUED, STATUS_PROCESSING],),
    )
    verified_before = {guid: state(guid) for guid in recovering_guids}
    reordered_recovering_guids = [
        STATUS_PROCESSING, STATUS_DONE, STATUS_QUEUED,
    ]
    verified_status = store.terminal_recovery_status(reordered_recovering_guids)
    verified_read_only = read_only_scalar(
        APP_DSN,
        "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
        (reordered_recovering_guids,),
    )
    verified_after = {guid: state(guid) for guid in recovering_guids}
    check(
        verified_status == status_result_for("verified", 3)
        and verified_read_only == status_result_for("verified", 3)
        and verified_before == verified_after
        and set(verified_status) == STATUS_RESULT_KEYS,
        "a new workflow run verifies the same complete reordered batch without knowing the prior run id",
        detail={
            "status": verified_status,
            "read_only": verified_read_only,
            "before": verified_before,
            "after": verified_after,
        },
    )

    after_first = {guid: state(guid) for guid in (ELIGIBLE_A, ELIGIBLE_B)}
    replay = store.recover_terminal([ELIGIBLE_A, ELIGIBLE_B], RECOVERY_ID)
    wrong_id = store.recover_terminal(
        [ELIGIBLE_B, ELIGIBLE_A], "deploy:terminal-recovery:other")
    check(
        replay == result_for(["already_recovered", "already_recovered"])
        and wrong_id == result_for(["ineligible", "ineligible"])
        and {guid: state(guid) for guid in (ELIGIBLE_A, ELIGIBLE_B)} == after_first,
        "reordered exact-metadata replay is idempotent while a different recovery id cannot spend the epoch",
        detail={"replay": replay, "wrong_id": wrong_id},
    )

    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',done_at=now(),"
        "locked_at=NULL,owner_instance=NULL WHERE delivery_key=ANY(%s) RETURNING status",
        ([ELIGIBLE_A, ELIGIBLE_B],),
    )
    exact_pair_status = store.terminal_recovery_status(
        [ELIGIBLE_B, ELIGIBLE_A])

    erased = scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET event_type='erased',payload='{}'::jsonb,"
        "account_key=NULL,repo=NULL,retry_window_expires_at=now()+interval '1 hour' "
        "WHERE delivery_key=%s RETURNING jsonb_build_object("
        "'event_type',event_type,'recovery_id',operator_recovery_id,"
        "'recovered_at',operator_recovered_at,"
        "'batch_size',operator_recovery_batch_size,"
        "'batch_token',operator_recovery_batch_token,"
        "'recovery_count',operator_recovery_count,"
        "'retry_window',retry_window_expires_at)",
        (ELIGIBLE_A,),
    )
    erased_recovery_status = store.terminal_recovery_status([ELIGIBLE_A])
    erased_sibling_subset_status = store.terminal_recovery_status([ELIGIBLE_B])
    check(
        erased == {
            "event_type": "erased", "recovery_id": None,
            "recovered_at": None, "batch_size": None, "batch_token": None,
            "recovery_count": 0, "retry_window": None,
        },
        "hard erasure centrally clears operator correlation, timestamp/count, and retry window",
        detail=erased,
    )
    check(
        exact_pair_status == status_result_for("verified", 2)
        and erased_recovery_status == status_result_for("unverified", 1)
        and erased_sibling_subset_status == status_result_for("unverified", 1),
        "hard-erasing one batch member cannot make its completed sibling subset impersonate the original batch",
        detail={
            "exact": exact_pair_status,
            "erased": erased_recovery_status,
            "sibling_subset": erased_sibling_subset_status,
        },
    )

    missing_before = state(MIXED_MISSING_CANDIDATE)
    mixed_missing = store.recover_terminal(
        [MIXED_MISSING_CANDIDATE, MISSING], "deploy:atomic:missing")
    ineligible_before = {
        MIXED_INELIGIBLE_CANDIDATE: state(MIXED_INELIGIBLE_CANDIDATE),
        AUTO_ZERO: state(AUTO_ZERO),
    }
    mixed_ineligible = store.recover_terminal(
        [MIXED_INELIGIBLE_CANDIDATE, AUTO_ZERO], "deploy:atomic:ineligible")
    check(
        mixed_missing == result_for(["ineligible", "missing"])
        and state(MIXED_MISSING_CANDIDATE) == missing_before
        and mixed_ineligible == result_for(["ineligible", "ineligible"])
        and {
            MIXED_INELIGIBLE_CANDIDATE: state(MIXED_INELIGIBLE_CANDIDATE),
            AUTO_ZERO: state(AUTO_ZERO),
        } == ineligible_before,
        "missing or ineligible inventory makes the entire bounded batch mutation-free",
        detail={"missing": mixed_missing, "ineligible": mixed_ineligible},
    )

    ineligible_keys = (AUTO_ZERO, LOCKED, EMPTY_PAYLOAD, LEGACY, NOT_FAILED)
    untouched = {guid: state(guid) for guid in ineligible_keys}
    rejected = store.recover_terminal(list(ineligible_keys), "deploy:ineligible:proof")
    check(
        rejected == result_for(["ineligible"] * len(ineligible_keys))
        and {guid: state(guid) for guid in ineligible_keys} == untouched,
        "unexhausted, locked, erased-payload, legacy-protocol, and non-failed rows stay untouched",
        detail=rejected,
    )

    first_single = store.recover_terminal(
        [MIXED_INELIGIBLE_CANDIDATE], "deploy:atomic:single")
    already_before = state(MIXED_INELIGIBLE_CANDIDATE)
    fresh_before = state(MIXED_ALREADY_CANDIDATE)
    mixed_already = store.recover_terminal(
        [MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE],
        "deploy:atomic:single",
    )
    check(
        first_single == result_for(["rearmed"])
        and mixed_already == result_for(["ineligible", "ineligible"])
        and state(MIXED_INELIGIBLE_CANDIDATE) == already_before
        and state(MIXED_ALREADY_CANDIDATE) == fresh_before,
        "same-id widened replay is rejected by exact batch metadata without partial mutation",
        detail={"first": first_single, "mixed": mixed_already},
    )

    second_single = store.recover_terminal(
        [MIXED_ALREADY_CANDIDATE], "deploy:atomic:single")
    distinct_single_tokens = {
        state(guid).get("operator_recovery_batch_token")
        for guid in (MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE)
    }
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET operator_recovery_batch_size=2 "
        "WHERE delivery_key=ANY(%s) RETURNING operator_recovery_batch_size",
        ([MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE],),
    )
    mixed_token_before = {
        guid: state(guid)
        for guid in (MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE)
    }
    mixed_token_status = store.terminal_recovery_status(
        [MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE])
    mixed_token_replay = store.recover_terminal(
        [MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE],
        "deploy:atomic:single",
    )
    check(
        second_single == result_for(["rearmed"])
        and len(distinct_single_tokens) == 2
        and None not in distinct_single_tokens
        and mixed_token_status == status_result_for("unverified", 2)
        and mixed_token_replay == result_for(["ineligible", "ineligible"])
        and {
            guid: state(guid)
            for guid in (MIXED_INELIGIBLE_CANDIDATE, MIXED_ALREADY_CANDIDATE)
        } == mixed_token_before,
        "same-id same-size rows from distinct DB-generated batch tokens cannot be mixed into one epoch",
        detail={"status": mixed_token_status, "replay": mixed_token_replay},
    )

    validation_before = state(VALIDATION_CANDIDATE)
    eleven = [f"00000000-0000-4000-8000-{index:012x}" for index in range(1, 12)]
    invalid = (
        ([VALIDATION_CANDIDATE, VALIDATION_CANDIDATE.upper()], "deploy:bad:upper"),
        ([VALIDATION_CANDIDATE, VALIDATION_CANDIDATE], "deploy:bad:duplicate"),
        ([], "deploy:bad:empty"), (eleven, "deploy:bad:many"),
        ([VALIDATION_CANDIDATE], "bad recovery id"),
        ([VALIDATION_CANDIDATE], "x" * 121), (None, "deploy:bad:null"),
    )
    invalid_results = [
        attempted(
            APP_DSN,
            "SELECT core.recover_terminal_webhook_deliveries_with_authority(%s,%s)",
            args,
        )
        for args in invalid
    ]
    invalid_status_guids = (
        [VALIDATION_CANDIDATE, VALIDATION_CANDIDATE.upper()],
        [VALIDATION_CANDIDATE, VALIDATION_CANDIDATE],
        [], eleven, None, [[VALIDATION_CANDIDATE]],
    )
    invalid_status_results = [
        attempted(
            APP_DSN,
            "SELECT core.terminal_webhook_delivery_recovery_status_with_authority(%s)",
            (guids,),
        )
        for guids in invalid_status_guids
    ]
    check(
        all(item.get("pgcode") == "22023" for item in invalid_results)
        and all(item.get("pgcode") == "22023" for item in invalid_status_results)
        and state(VALIDATION_CANDIDATE) == validation_before,
        "database recovery and status reject invalid bounds/tokens/canonicalization without writes",
        detail={"recovery": invalid_results, "status": invalid_status_results},
    )

    continuation_guids = [CONTINUATION_FAILED, CONTINUATION_DONE]
    continuation_recovery_id = "deploy:terminal-continuation:original"
    original_continuation_batch = store.recover_terminal(
        continuation_guids, continuation_recovery_id)
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='failed',attempts=7,"
        "last_error='first operator epoch exhausted',locked_at=NULL,owner_instance=NULL "
        "WHERE delivery_key=%s RETURNING status",
        (CONTINUATION_FAILED,),
    )
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='done',done_at=now(),"
        "locked_at=NULL,owner_instance=NULL WHERE delivery_key=%s RETURNING status",
        (CONTINUATION_DONE,),
    )
    continuation_before = {
        guid: state(guid) for guid in continuation_guids
    }
    continuable = store.terminal_recovery_status(continuation_guids)
    mixed_candidate_missing = store.continue_terminal(
        [CONTINUATION_FAILED, MISSING], CONTINUATION_ID, RUNTIME_SHA)
    after_mixed_candidate = {
        guid: state(guid) for guid in continuation_guids
    }
    continued = store.continue_terminal(
        continuation_guids, CONTINUATION_ID, RUNTIME_SHA)
    continuation_after = {
        guid: state(guid) for guid in continuation_guids
    }
    failed_after = continuation_after[CONTINUATION_FAILED]
    done_after = continuation_after[CONTINUATION_DONE]
    common_audit = all(
        item.get("operator_continuation_count") == 1
        and item.get("operator_continuation_id") == CONTINUATION_ID
        and item.get("operator_continuation_sha") == RUNTIME_SHA
        and item.get("operator_continued_at") is not None
        for item in continuation_after.values()
    )
    check(
        original_continuation_batch == result_for(["rearmed", "rearmed"])
        and continuable == status_result_for("continuable", 2)
        and mixed_candidate_missing
        == continuation_result_for(["ineligible", "missing"])
        and after_mixed_candidate == continuation_before
        and continued == continuation_result_for(
            ["continued", "already_completed"])
        and failed_after.get("status") == "queued"
        and failed_after.get("attempts") == 0
        and failed_after.get("auto_rearm_count") == 3
        and failed_after.get("done_at") is None
        and failed_after.get("last_error") is None
        and done_after.get("status") == "done"
        and done_after.get("done_at")
        == continuation_before[CONTINUATION_DONE].get("done_at")
        and done_after.get("done_at")
        < done_after.get("operator_continued_at")
        and done_after.get("updated_at")
        >= done_after.get("operator_continued_at")
        and done_after.get("auto_rearm_count") == 2
        and common_audit
        and failed_after.get("operator_continued_at")
        == done_after.get("operator_continued_at"),
        "continuation atomically requeues only failed members and stamps one exact-SHA batch audit",
        detail={"result": continued, "status": continuable},
    )

    replay_before = {
        guid: state(guid) for guid in continuation_guids
    }
    continuation_replay = store.continue_terminal(
        list(reversed(continuation_guids)), CONTINUATION_ID, RUNTIME_SHA)
    wrong_continuation_id = store.continue_terminal(
        continuation_guids, "deploy:terminal-continuation:other", RUNTIME_SHA)
    wrong_continuation_sha = store.continue_terminal(
        continuation_guids, CONTINUATION_ID, "b" * 40)
    subset_continuation = store.continue_terminal(
        [CONTINUATION_FAILED], CONTINUATION_ID, RUNTIME_SHA)
    superset_continuation = store.continue_terminal(
        [*continuation_guids, MISSING], CONTINUATION_ID, RUNTIME_SHA)
    check(
        continuation_replay
        == continuation_result_for(["already_continued", "already_continued"])
        and wrong_continuation_id
        == continuation_result_for(["ineligible", "ineligible"])
        and wrong_continuation_sha
        == continuation_result_for(["ineligible", "ineligible"])
        and subset_continuation == continuation_result_for(["ineligible"])
        and superset_continuation
        == continuation_result_for(["ineligible", "ineligible", "missing"])
        and {guid: state(guid) for guid in continuation_guids} == replay_before,
        "exact continuation replay is idempotent while changed authority and batch shape are mutation-free",
        detail={
            "replay": continuation_replay,
            "wrong_id": wrong_continuation_id,
            "wrong_sha": wrong_continuation_sha,
            "subset": subset_continuation,
            "superset": superset_continuation,
        },
    )

    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='failed',attempts=7,"
        "last_error='continuation exhausted',locked_at=NULL,owner_instance=NULL "
        "WHERE delivery_key=%s RETURNING status",
        (CONTINUATION_FAILED,),
    )
    spent_failed = store.terminal_recovery_status(continuation_guids)
    spent_before = {
        guid: state(guid) for guid in continuation_guids
    }
    spent_other_id = store.continue_terminal(
        continuation_guids, "deploy:terminal-continuation:second", RUNTIME_SHA)
    check(
        spent_failed == status_result_for("continuation_failed", 2)
        and spent_other_id
        == continuation_result_for(["ineligible", "ineligible"])
        and {guid: state(guid) for guid in continuation_guids} == spent_before,
        "a failed continuation is durably spent and cannot authorize a second mutation",
        detail={"status": spent_failed, "result": spent_other_id},
    )

    lock_wait_original = store.recover_terminal(
        [CONTINUATION_LOCK_WAIT],
        "deploy:terminal-continuation:lock-wait-original",
    )
    lock_conn = psycopg2.connect(OWNER_DSN)
    lock_wait_results: list[dict] = []
    lock_wait_errors: list[str] = []
    lock_wait_pids: list[int] = []
    lock_wait_ready = threading.Event()

    def continue_after_lock_wait() -> None:
        contender_conn = None
        try:
            contender_conn = psycopg2.connect(APP_DSN)
            contender_conn.autocommit = True
            with contender_conn.cursor() as contender_cur:
                contender_cur.execute("SET search_path=core")
                contender_cur.execute("SELECT pg_backend_pid()")
                lock_wait_pids.append(contender_cur.fetchone()[0])
                lock_wait_ready.set()
                contender_cur.execute(
                    "SELECT core.continue_terminal_webhook_deliveries_with_authority(%s,%s,%s)",
                    ([CONTINUATION_LOCK_WAIT],
                     "deploy:terminal-continuation:lock-wait", RUNTIME_SHA),
                )
                lock_wait_results.append(contender_cur.fetchone()[0])
        except Exception as exc:  # pragma: no cover - asserted diagnostics
            lock_wait_errors.append(repr(exc))
            lock_wait_ready.set()
        finally:
            if contender_conn is not None:
                contender_conn.close()

    lock_wait_thread = threading.Thread(
        target=continue_after_lock_wait, daemon=True)
    lock_wait_observed = False
    lock_owner_pid = None
    transitioned_at = None
    try:
        with lock_conn.cursor() as lock_cur:
            lock_cur.execute("SET search_path=core")
            lock_cur.execute("SELECT pg_backend_pid()")
            lock_owner_pid = lock_cur.fetchone()[0]
            lock_cur.execute(
                "SELECT delivery_key FROM core.webhook_delivery "
                "WHERE delivery_key=%s FOR UPDATE",
                (CONTINUATION_LOCK_WAIT,),
            )
            lock_wait_thread.start()
            lock_wait_ready.wait(timeout=5)
            if lock_wait_pids:
                for _ in range(100):
                    lock_wait_observed = bool(scalar(
                        OWNER_DSN,
                        "SELECT %s=ANY(pg_catalog.pg_blocking_pids(%s))",
                        (lock_owner_pid, lock_wait_pids[0]),
                    ))
                    if lock_wait_observed:
                        break
                    threading.Event().wait(0.02)
            # This transition occurs while the continuation statement is
            # already blocked on the canonical row lock. A function-entry
            # audit timestamp would therefore be older than this state.
            lock_cur.execute(
                "UPDATE core.webhook_delivery SET status='failed',attempts=7,"
                "last_error='lock-wait transition',locked_at=NULL,owner_instance=NULL,"
                "updated_at=clock_timestamp() WHERE delivery_key=%s RETURNING updated_at",
                (CONTINUATION_LOCK_WAIT,),
            )
            transitioned_at = lock_cur.fetchone()[0]
        lock_conn.commit()
        lock_wait_thread.join(timeout=10)
    finally:
        if not lock_conn.closed:
            lock_conn.rollback()
            lock_conn.close()
    lock_wait_after = state(CONTINUATION_LOCK_WAIT)
    check(
        lock_wait_original == result_for(["rearmed"])
        and lock_wait_observed
        and not lock_wait_errors
        and not lock_wait_thread.is_alive()
        and lock_wait_results == [continuation_result_for(["continued"])]
        and transitioned_at is not None
        and lock_wait_after.get("status") == "queued"
        and lock_wait_after.get("operator_continuation_count") == 1
        and lock_wait_after.get("operator_continued_at") is not None
        and lock_wait_after.get("operator_continued_at") > transitioned_at
        and lock_wait_after.get("updated_at")
        == lock_wait_after.get("operator_continued_at"),
        "continuation samples its audit after a real row-lock wait and the state transition observed behind it",
        detail={
            "wait_observed": lock_wait_observed,
            "result": lock_wait_results,
            "errors": lock_wait_errors,
            "transitioned_at": transitioned_at,
            "continued_at": lock_wait_after.get("operator_continued_at"),
            "updated_at": lock_wait_after.get("updated_at"),
        },
    )

    race_original = store.recover_terminal(
        [CONTINUATION_RACE], "deploy:terminal-continuation:race-original")
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='failed',attempts=7,"
        "last_error='race first epoch exhausted' WHERE delivery_key=%s RETURNING status",
        (CONTINUATION_RACE,),
    )
    continuation_race_results: list[dict] = []
    continuation_race_errors: list[str] = []
    continuation_barrier = threading.Barrier(3)

    def contend_continuation() -> None:
        try:
            contender = DQ.DeliveryStore(APP_DSN, max_pending=100, max_attempts=3)
            continuation_barrier.wait(timeout=5)
            continuation_race_results.append(contender.continue_terminal(
                [CONTINUATION_RACE], "deploy:terminal-continuation:race", RUNTIME_SHA))
        except Exception as exc:  # pragma: no cover - asserted diagnostics
            continuation_race_errors.append(repr(exc))

    continuation_threads = [
        threading.Thread(target=contend_continuation, daemon=True)
        for _ in range(2)
    ]
    for thread in continuation_threads:
        thread.start()
    continuation_barrier.wait(timeout=5)
    for thread in continuation_threads:
        thread.join(timeout=10)
    continuation_raced = state(CONTINUATION_RACE)
    check(
        race_original == result_for(["rearmed"])
        and not continuation_race_errors
        and all(not thread.is_alive() for thread in continuation_threads)
        and sorted(
            result["results"][0] for result in continuation_race_results
        ) == ["already_continued", "continued"]
        and continuation_raced.get("status") == "queued"
        and continuation_raced.get("auto_rearm_count") == 3
        and continuation_raced.get("operator_continuation_count") == 1,
        "concurrent identical continuations serialize into one logical mutation",
        detail={
            "results": continuation_race_results,
            "errors": continuation_race_errors,
        },
    )

    race_results: list[dict] = []
    race_errors: list[str] = []
    barrier = threading.Barrier(3)

    def contend(recovery_id: str) -> None:
        try:
            contender = DQ.DeliveryStore(APP_DSN, max_pending=100, max_attempts=3)
            barrier.wait(timeout=5)
            race_results.append(contender.recover_terminal([RACE], recovery_id))
        except Exception as exc:  # pragma: no cover - asserted diagnostics
            race_errors.append(repr(exc))

    threads = [
        threading.Thread(target=contend, args=("deploy:race:a",), daemon=True),
        threading.Thread(target=contend, args=("deploy:race:b",), daemon=True),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
    raced = state(RACE)
    check(
        not race_errors and all(not thread.is_alive() for thread in threads)
        and sorted(result["results"][0] for result in race_results)
        == ["ineligible", "rearmed"]
        and raced.get("status") == "queued"
        and raced.get("operator_recovery_count") == 1
        and raced.get("operator_recovery_id") in {"deploy:race:a", "deploy:race:b"},
        "overlapping operator ids serialize and spend the row epoch once",
        detail={"results": race_results, "errors": race_errors, "state": raced},
    )

    audit_id = raced["operator_recovery_id"]
    audit_size = raced["operator_recovery_batch_size"]
    audit_token = raced["operator_recovery_batch_token"]
    signed_duplicate = scalar(
        APP_DSN,
        "SELECT core.enqueue_webhook_delivery_with_authority(%s,%s,%s,%s,%s,%s,%s)",
        (RACE, "push", "700", "acme/recovery-proof", Json(raced["payload"]), 100, 2),
    )
    after_duplicate = state(RACE)
    scalar(
        OWNER_DSN,
        "UPDATE core.webhook_delivery SET status='failed',attempts=7,auto_rearm_count=1,"
        "last_error='failed after signed duplicate',updated_at=now() "
        "WHERE delivery_key=%s RETURNING attempts",
        (RACE,),
    )
    refailed = state(RACE)
    duplicate_failed_status = store.terminal_recovery_status([RACE])
    second_operator = store.recover_terminal([RACE], "deploy:race:second")
    check(
        isinstance(signed_duplicate, dict) and signed_duplicate.get("accepted") is True
        and after_duplicate.get("auto_rearm_count") == 0
        and after_duplicate.get("operator_recovery_id") == audit_id
        and after_duplicate.get("operator_recovery_batch_size") == audit_size == 1
        and after_duplicate.get("operator_recovery_batch_token") == audit_token
        and after_duplicate.get("operator_recovery_count") == 1
        and duplicate_failed_status == status_result_for("failed", 1)
        and second_operator == result_for(["ineligible"])
        and state(RACE) == refailed,
        "signed duplicate resets only the automatic epoch; the operator audit remains lifetime one-shot",
        detail={"duplicate": signed_duplicate, "second": second_operator},
    )


@contextmanager
def environment(**updates):
    sentinel = object()
    previous = {key: os.environ.get(key, sentinel) for key in updates}
    try:
        for key, value in updates.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def install_fake_server() -> None:
    fake = type(sys)("server")
    fake.read_bounded_body = lambda length, stream: (stream.read(int(length)), None)
    fake.verify_signature = lambda *_args: False
    fake._as_obj = lambda value: value if isinstance(value, dict) else {}
    fake._event_account_key = lambda _payload: None
    fake._event_repo = lambda _payload: None
    fake._FAILING_CONCLUSIONS = frozenset(("failure",))
    sys.modules["server"] = fake


class RecoveryStore:
    def __init__(
            self, result: dict | None = None, error: Exception | None = None,
            *, status_result: dict | None = None,
            status_error: Exception | None = None,
            continuation_result: dict | None = None,
            continuation_error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[tuple[list[str], str]] = []
        self.status_result = status_result
        self.status_error = status_error
        self.status_calls: list[list[str]] = []
        self.continuation_result = continuation_result
        self.continuation_error = continuation_error
        self.continuation_calls: list[tuple[list[str], str, str]] = []

    def recover_terminal(self, guids, recovery_id):
        self.calls.append((list(guids), recovery_id))
        if self.error is not None:
            raise self.error
        return self.result

    def terminal_recovery_status(self, guids):
        self.status_calls.append(list(guids))
        if self.status_error is not None:
            raise self.status_error
        return self.status_result

    def continue_terminal(self, guids, continuation_id, exact_sha):
        self.continuation_calls.append(
            (list(guids), continuation_id, exact_sha))
        if self.continuation_error is not None:
            raise self.continuation_error
        return self.continuation_result


def request_body(
        guids: list[str] | None = None, *, recovery_id: str = RECOVERY_ID,
        expected_sha: str = RUNTIME_SHA, extra: dict | None = None) -> bytes:
    value = {
        "expected_sha": expected_sha,
        "recovery_id": recovery_id,
        "delivery_guids": [ELIGIBLE_A, ELIGIBLE_B] if guids is None else guids,
    }
    if extra:
        value.update(extra)
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), b"recoveryz\0" + body, hashlib.sha256).hexdigest()


def status_signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), b"recovery-statusz\0" + body, hashlib.sha256).hexdigest()


def continuation_signature(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(
        secret.encode(), b"recovery-continuationz\0" + body,
        hashlib.sha256).hexdigest()


def invoke(
        body: bytes, store, *, supplied_signature: str | None = None,
        secret: str = SECRET, role: str = "web", render_sha: str = RUNTIME_SHA,
        build_sha: str = RUNTIME_SHA, status_probe: bool = False,
        continuation: bool = False) -> dict:
    Handler = SH.make_handler(
        secret=secret, store=store,
        worker=type("Worker", (), {"submit": lambda *_args: True})(),
        db=lambda *_args, **_kwargs: None, dsn="", gh=None, persist_all=True,
    )
    handler = Handler.__new__(Handler)
    assert not (status_probe and continuation)
    handler.path = (
        "/recoveryz/durable-deliveries/status" if status_probe
        else "/recoveryz/durable-deliveries/continue" if continuation
        else "/recoveryz/durable-deliveries"
    )
    handler.headers = {
        "Content-Length": str(len(body)),
        "X-Veripsa-Recovery-Signature": (
            (status_signature(body, secret) if status_probe
             else continuation_signature(body, secret) if continuation
             else signature(body, secret))
            if supplied_signature is None else supplied_signature),
    }
    handler.rfile = io.BytesIO(body)
    handler._headers_buffer = []
    handler.request_version = "HTTP/1.1"
    captured = {"status": None, "headers": [], "body": b""}
    handler.send_response = lambda code, message=None: captured.update(status=code)
    handler.send_header = lambda key, value: captured["headers"].append((key, value))
    handler.flush_headers = lambda: None

    class Wfile:
        def write(self, value):
            captured["body"] += value

    handler.wfile = Wfile()
    with environment(
            VERIPSA_RUNTIME_ROLE=role, RENDER_GIT_COMMIT=render_sha,
            VERIPSA_BUILD_SHA=build_sha):
        handler.do_POST()
    try:
        captured["json"] = json.loads(captured["body"].decode())
    except (UnicodeDecodeError, ValueError):
        captured["json"] = None
    return captured


def run_http_checks() -> None:
    install_fake_server()
    body = request_body()
    expected = result_for(["rearmed", "rearmed"])
    store = RecoveryStore(expected)
    valid = invoke(body, store)
    headers = {key.lower(): value for key, value in valid["headers"]}
    response_text = valid["body"].decode("utf-8", "replace")
    check(
        valid["status"] == 200 and valid["json"] == expected
        and store.calls == [([ELIGIBLE_A, ELIGIBLE_B], RECOVERY_ID)]
        and headers.get("content-type") == "application/json"
        and headers.get("cache-control") == "no-store"
        and all(token not in response_text for token in (
            ELIGIBLE_A, ELIGIBLE_B, RECOVERY_ID, *DB_BATCH_TOKENS,
        )),
        "web role accepts exact-SHA domain-HMAC request and emits only content-free classifications",
        detail={"response": valid, "calls": store.calls},
    )

    continuation_expected = continuation_result_for(
        ["continued", "already_completed"])
    continuation_store = RecoveryStore(
        continuation_result=continuation_expected)
    continuation_valid = invoke(
        body, continuation_store, continuation=True)
    continuation_headers = {
        key.lower(): value for key, value in continuation_valid["headers"]
    }
    continuation_text = continuation_valid["body"].decode(
        "utf-8", "replace")
    check(
        continuation_valid["status"] == 200
        and continuation_valid["json"] == continuation_expected
        and continuation_store.continuation_calls == [(
            [ELIGIBLE_A, ELIGIBLE_B], RECOVERY_ID, RUNTIME_SHA)]
        and continuation_store.calls == []
        and continuation_store.status_calls == []
        and continuation_headers.get("content-type") == "application/json"
        and continuation_headers.get("cache-control") == "no-store"
        and all(token not in continuation_text for token in (
            ELIGIBLE_A, ELIGIBLE_B, RECOVERY_ID, *DB_BATCH_TOKENS,
        )),
        "web continuation requires its own HMAC and passes exact runtime SHA to one content-free Store mutation",
        detail={
            "response": continuation_valid,
            "calls": continuation_store.continuation_calls,
        },
    )

    status_probe_guids = [
        STATUS_DONE, STATUS_QUEUED, STATUS_FAILED,
        STATUS_WRONG_ID, STATUS_MISSING,
    ]
    new_workflow_id = "deploy:terminal-recovery:new-workflow-run"
    status_body = request_body(
        status_probe_guids, recovery_id=new_workflow_id)
    status_expected = status_result_for("verified", len(status_probe_guids))
    status_store = RecoveryStore(status_result=status_expected)
    status_valid = invoke(status_body, status_store, status_probe=True)
    status_headers = {
        key.lower(): value for key, value in status_valid["headers"]
    }
    status_response_text = status_valid["body"].decode("utf-8", "replace")
    check(
        status_valid["status"] == 200
        and status_valid["json"] == status_expected
        and status_store.status_calls == [status_probe_guids]
        and status_store.calls == []
        and status_headers.get("content-type") == "application/json"
        and status_headers.get("cache-control") == "no-store"
        and all(token not in status_response_text for token in (
            *status_probe_guids, RECOVERY_ID, new_workflow_id,
            *DB_BATCH_TOKENS,
        )),
        "a new workflow id reaches the GUID-only Store status method and receives one content-free classification",
        detail={
            "response": status_valid,
            "status_calls": status_store.status_calls,
            "mutation_calls": status_store.calls,
        },
    )

    status_with_mutation_key = RecoveryStore(status_result=status_expected)
    mutation_with_status_key = RecoveryStore(expected)
    continuation_with_recovery_key = RecoveryStore(
        continuation_result=continuation_expected)
    recovery_with_continuation_key = RecoveryStore(expected)
    continuation_with_status_key = RecoveryStore(
        continuation_result=continuation_expected)
    status_with_continuation_key = RecoveryStore(status_result=status_expected)
    wrong_status_domain = invoke(
        status_body, status_with_mutation_key,
        supplied_signature=signature(status_body),
        status_probe=True,
    )
    wrong_mutation_domain = invoke(
        status_body, mutation_with_status_key,
        supplied_signature=status_signature(status_body),
    )
    wrong_continuation_recovery_domain = invoke(
        body, continuation_with_recovery_key,
        supplied_signature=signature(body), continuation=True)
    wrong_recovery_continuation_domain = invoke(
        body, recovery_with_continuation_key,
        supplied_signature=continuation_signature(body))
    wrong_continuation_status_domain = invoke(
        body, continuation_with_status_key,
        supplied_signature=status_signature(body), continuation=True)
    wrong_status_continuation_domain = invoke(
        status_body, status_with_continuation_key,
        supplied_signature=continuation_signature(status_body),
        status_probe=True)
    check(
        wrong_status_domain["status"] == 401
        and wrong_status_domain["json"] == {"status": "unauthorized"}
        and wrong_mutation_domain["status"] == 401
        and wrong_mutation_domain["json"] == {"status": "unauthorized"}
        and status_with_mutation_key.status_calls == []
        and status_with_mutation_key.calls == []
        and mutation_with_status_key.status_calls == []
        and mutation_with_status_key.calls == [],
        "status and first-recovery HMAC capabilities are domain-separated in both directions",
        detail={
            "status_with_mutation_key": wrong_status_domain,
            "mutation_with_status_key": wrong_mutation_domain,
        },
    )
    check(
        all(response["status"] == 401
            and response["json"] == {"status": "unauthorized"}
            for response in (
                wrong_continuation_recovery_domain,
                wrong_recovery_continuation_domain,
                wrong_continuation_status_domain,
                wrong_status_continuation_domain,
            ))
        and continuation_with_recovery_key.continuation_calls == []
        and recovery_with_continuation_key.calls == []
        and continuation_with_status_key.continuation_calls == []
        and status_with_continuation_key.status_calls == [],
        "continuation HMAC authority is isolated from both recovery and status domains",
        detail={
            "continuation_recovery": wrong_continuation_recovery_domain,
            "recovery_continuation": wrong_recovery_continuation_domain,
            "continuation_status": wrong_continuation_status_domain,
            "status_continuation": wrong_status_continuation_domain,
        },
    )

    status_role_store = RecoveryStore(status_result=status_expected)
    status_mismatch_store = RecoveryStore(status_result=status_expected)
    status_conflict_store = RecoveryStore(status_result=status_expected)
    status_wrong_role = invoke(
        status_body, status_role_store,
        role="convergence-worker", status_probe=True)
    status_mismatch = invoke(
        request_body(
            status_probe_guids, recovery_id=new_workflow_id,
            expected_sha="b" * 40),
        status_mismatch_store,
        status_probe=True,
    )
    status_conflict = invoke(
        status_body, status_conflict_store, build_sha="c" * 40,
        status_probe=True,
    )
    check(
        status_wrong_role["status"] == 404
        and status_wrong_role["json"] == {"status": "unavailable"}
        and status_mismatch["status"] == 409
        and status_mismatch["json"] == {"status": "sha_mismatch"}
        and status_conflict["status"] == 503
        and status_conflict["json"] == {"status": "unavailable"}
        and status_role_store.status_calls == []
        and status_mismatch_store.status_calls == []
        and status_conflict_store.status_calls == [],
        "status route is web-only and binds its read proof to the exact runtime SHA",
        detail={
            "role": status_wrong_role,
            "mismatch": status_mismatch,
            "conflict": status_conflict,
        },
    )

    status_missing_method = invoke(status_body, object(), status_probe=True)
    status_raising_store = RecoveryStore(
        status_error=RuntimeError("private durable status detail"))
    status_raising = invoke(
        status_body, status_raising_store, status_probe=True)
    status_wide = dict(status_expected)
    status_wide["delivery_guid"] = ELIGIBLE_A
    status_wide_response = invoke(
        status_body, RecoveryStore(status_result=status_wide),
        status_probe=True)
    status_bad_count = dict(status_expected)
    status_bad_count["requested"] -= 1
    status_bad_count_response = invoke(
        status_body, RecoveryStore(status_result=status_bad_count),
        status_probe=True)
    status_bad_enum_response = invoke(
        status_body,
        RecoveryStore(status_result={
            "status": "done", "requested": len(status_probe_guids),
        }),
        status_probe=True,
    )
    status_mutation_shape = invoke(
        status_body, RecoveryStore(status_result=expected),
        status_probe=True)
    status_enum_responses = [
        invoke(
            status_body,
            RecoveryStore(status_result=status_result_for(
                status, len(status_probe_guids))),
            status_probe=True,
        )
        for status in (
            "unspent", "never_recovered", "recovering", "verified", "failed",
            "continuable", "continuation_failed", "unverified",
        )
    ]
    status_unavailable = (
        status_missing_method, status_raising, status_wide_response,
        status_bad_count_response, status_bad_enum_response,
        status_mutation_shape,
    )
    check(
        all(response["status"] == 503
            and response["json"] == {"status": "unavailable"}
            and ELIGIBLE_A not in response["body"].decode("utf-8", "replace")
            and RECOVERY_ID not in response["body"].decode("utf-8", "replace")
            and new_workflow_id not in response["body"].decode("utf-8", "replace")
            for response in status_unavailable),
        "status route rejects missing/raising stores and widened, inconsistent, or mutation-shaped results content-free",
        detail=status_unavailable,
    )
    check(
        all(response["status"] == 200
            and response["json"] == status_result_for(
                expected_status, len(status_probe_guids))
            for response, expected_status in zip(
                status_enum_responses,
                (
                    "unspent", "never_recovered", "recovering", "verified",
                    "failed", "continuable", "continuation_failed", "unverified",
                ),
            )),
        "status response validator accepts only the eight aggregate proof states",
        detail=status_enum_responses,
    )

    continuation_wide = dict(continuation_expected)
    continuation_wide["delivery_guid"] = ELIGIBLE_A
    continuation_bad_count = dict(continuation_expected)
    continuation_bad_count["continued"] = 2
    continuation_mixed = continuation_result_for(
        ["continued", "already_continued"])
    continuation_unavailable = (
        invoke(body, object(), continuation=True),
        invoke(
            body,
            RecoveryStore(continuation_error=RuntimeError("private detail")),
            continuation=True),
        invoke(
            body, RecoveryStore(continuation_result=continuation_wide),
            continuation=True),
        invoke(
            body, RecoveryStore(continuation_result=continuation_bad_count),
            continuation=True),
        invoke(
            body, RecoveryStore(continuation_result=continuation_mixed),
            continuation=True),
    )
    check(
        all(response["status"] == 503
            and response["json"] == {"status": "unavailable"}
            and ELIGIBLE_A not in response["body"].decode("utf-8", "replace")
            and RECOVERY_ID not in response["body"].decode("utf-8", "replace")
            for response in continuation_unavailable),
        "continuation rejects missing/raising stores and inconsistent or widened result shapes content-free",
        detail=continuation_unavailable,
    )

    continuation_role_store = RecoveryStore(
        continuation_result=continuation_expected)
    continuation_mismatch_store = RecoveryStore(
        continuation_result=continuation_expected)
    continuation_wrong_role = invoke(
        body, continuation_role_store, role="convergence-worker",
        continuation=True)
    continuation_mismatch = invoke(
        request_body(expected_sha="b" * 40), continuation_mismatch_store,
        continuation=True)
    check(
        continuation_wrong_role["status"] == 404
        and continuation_wrong_role["json"] == {"status": "unavailable"}
        and continuation_mismatch["status"] == 409
        and continuation_mismatch["json"] == {"status": "sha_mismatch"}
        and continuation_role_store.continuation_calls == []
        and continuation_mismatch_store.continuation_calls == [],
        "continuation is web-only and rejects an expected SHA different from the exact live artifact",
        detail={
            "role": continuation_wrong_role,
            "mismatch": continuation_mismatch,
        },
    )

    ordinary_signature = "sha256=" + hmac.new(
        SECRET.encode(), body, hashlib.sha256).hexdigest()
    denied = []
    for supplied, request_secret in ((ordinary_signature, SECRET), ("sha256=" + "0" * 64, SECRET), (None, "")):
        denied_store = RecoveryStore(expected)
        response = invoke(
            body, denied_store, supplied_signature=supplied, secret=request_secret)
        denied.append((response, denied_store.calls))
    check(
        all(response["status"] == 401
            and response["json"] == {"status": "unauthorized"}
            and calls == [] for response, calls in denied),
        "ordinary GitHub HMAC, forgery, and empty-secret requests fail before storage",
        detail=denied,
    )

    role_store, mismatch_store, conflict_store = (
        RecoveryStore(expected), RecoveryStore(expected), RecoveryStore(expected))
    wrong_role = invoke(body, role_store, role="convergence-worker")
    mismatch = invoke(request_body(expected_sha="b" * 40), mismatch_store)
    conflict = invoke(body, conflict_store, build_sha="c" * 40)
    check(
        wrong_role["status"] == 404 and wrong_role["json"] == {"status": "unavailable"}
        and mismatch["status"] == 409 and mismatch["json"] == {"status": "sha_mismatch"}
        and conflict["status"] == 503 and conflict["json"] == {"status": "unavailable"}
        and role_store.calls == mismatch_store.calls == conflict_store.calls == [],
        "worker role, wrong expected SHA, and conflicting runtime provenance fail closed",
        detail={"role": wrong_role, "mismatch": mismatch, "conflict": conflict},
    )

    eleven = [f"00000000-0000-4000-8000-{index:012x}" for index in range(1, 12)]
    duplicate_keys = (
        '{"expected_sha":"' + RUNTIME_SHA + '","expected_sha":"' + RUNTIME_SHA
        + '","recovery_id":"' + RECOVERY_ID
        + '","delivery_guids":["' + ELIGIBLE_A + '"]}'
    ).encode("ascii")
    invalid_bodies = (
        request_body(extra={"payload": {"forbidden": True}}), request_body([]),
        request_body([ELIGIBLE_A, ELIGIBLE_A]), request_body([ELIGIBLE_A.upper()]),
        request_body(eleven), request_body(recovery_id="bad recovery id"),
        request_body(expected_sha="ABCDEF"), duplicate_keys,
        b'{"expected_sha":NaN,"recovery_id":"x","delivery_guids":[]}',
        b"x" * 4097,
    )
    invalid = []
    for malformed in invalid_bodies:
        malformed_store = RecoveryStore(expected)
        invalid.append((invoke(malformed, malformed_store), malformed_store.calls))
    check(
        all(response["status"] == 400
            and response["json"] == {"status": "invalid"}
            and calls == [] for response, calls in invalid),
        "HTTP rejects payload/extra keys, duplicate keys/GUIDs, bad bounds/tokens, NaN, and oversize bodies",
        detail=invalid,
    )

    missing_method = invoke(body, object())
    raising_store = RecoveryStore(error=RuntimeError("private database detail"))
    raising = invoke(body, raising_store)
    wide = dict(expected)
    wide["delivery_guid"] = ELIGIBLE_A
    wide_store = RecoveryStore(wide)
    wide_response = invoke(body, wide_store)
    bad_count = dict(expected)
    bad_count["rearmed"] = 1
    count_store = RecoveryStore(bad_count)
    count_response = invoke(body, count_store)
    mixed_store = RecoveryStore(result_for(["rearmed", "ineligible"]))
    mixed_response = invoke(body, mixed_store)
    unavailable = (missing_method, raising, wide_response, count_response, mixed_response)
    check(
        all(response["status"] == 503
            and response["json"] == {"status": "unavailable"}
            and ELIGIBLE_A not in response["body"].decode("utf-8", "replace")
            and RECOVERY_ID not in response["body"].decode("utf-8", "replace")
            for response in unavailable),
        "missing/raising store and widened, inconsistent, or non-atomic DB results fail content-free",
        detail=unavailable,
    )


def main() -> int:
    print("=== TERMINAL DELIVERY RECOVERY RUNTIME GATE ===")
    run_log_privacy_checks()
    try:
        bootstrap()
        run_database_checks()
    finally:
        subprocess.run(["dropdb", DB], capture_output=True, text=True)
    run_http_checks()
    print(
        "TERMINAL DELIVERY RECOVERY RUNTIME GATE:",
        "PASS" if FAILURES == 0 else "FAIL",
    )
    return 0 if FAILURES == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
