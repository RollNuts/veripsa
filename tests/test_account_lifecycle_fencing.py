#!/usr/bin/env python3
"""Account lifecycle serialization/order gate.

Proves with independent live PostgreSQL sessions that boot-style multi-statement work cannot cross an uninstall or
GDPR erase, and that durable lifecycle receive order prevents stale activation/deletion from changing generations.
Run: python3 tests/test_account_lifecycle_fencing.py
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
DB = "veripsa_account_fence_" + str(os.getpid())
checks: list[bool] = []
PAIR = json.dumps([
    {"a": "a.py", "b": "b.py", "co": 2, "n_a": 2, "n_b": 2,
     "n_total": 2, "strength": 1.0, "lift": 2.0}
])


def chk(value, label):
    print(("  [PASS] " if value else "  [FAIL] ") + label)
    checks.append(bool(value))


def conn(role="veripsa_migrator", autocommit=True):
    c = psycopg2.connect(f"postgresql://{role}@localhost/{DB}")
    c.autocommit = autocommit
    return c


def app(install, autocommit=True):
    c = conn("veripsa_app", autocommit)
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (install,))
    if not autocommit:
        c.commit()
    return c


def scalar(sql, args=()):
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        c.close()


def account_count(account, table):
    c = conn()
    try:
        c.autocommit = False
        with c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SELECT set_config('core.current_account',%s,true)", (account,))
            cur.execute(f"SELECT count(*) FROM core.{table} WHERE account_id=%s", (account,))
            out = cur.fetchone()[0]
        c.rollback()
        return out
    finally:
        c.close()


def active_tombstone(account):
    return scalar(
        "SELECT count(*) FROM core.account_lifecycle_tombstone WHERE account_id=%s AND active",
        (account,),
    )


def lifecycle_lock(cur, account):
    cur.execute(
        "SELECT pg_advisory_lock_shared(hashtext('core.account_lifecycle'),hashtext(%s))",
        (account,),
    )


def lifecycle_unlock(cur, account):
    cur.execute(
        "SELECT pg_advisory_unlock_shared(hashtext('core.account_lifecycle'),hashtext(%s))",
        (account,),
    )


def run_blocked_lifecycle(install, operation):
    state = {"started": threading.Event(), "done": threading.Event(), "error": None, "result": None}

    def work():
        c = app(install)
        state["started"].set()
        try:
            with c.cursor() as cur:
                cur.execute(operation)
                row = cur.fetchone()
                state["result"] = row[0] if row else None
        except Exception as exc:  # surfaced by the assertions below
            state["error"] = str(exc)
        finally:
            c.close()
            state["done"].set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, state


def run_generation_admission(install, installation_id, created_at):
    state = {"started": threading.Event(), "done": threading.Event(), "error": None, "result": None}

    def work():
        c = app(install)
        state["started"].set()
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT core.admit_event_installation_generation_with_authority(%s,%s::timestamptz)",
                    (installation_id, created_at),
                )
                state["result"] = cur.fetchone()[0]
        except Exception as exc:
            state["error"] = str(exc)
        finally:
            c.close()
            state["done"].set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, state


def generation_proof(account_key, installation_id, created_at="2099-01-01T00:00:00Z"):
    return json.dumps({
        "installation_id": installation_id,
        "account_id": account_key,
        "created_at": created_at,
        "suspended": False,
    })


def current_suspend_proof(account_key, suspended_installation_id, created_at):
    return json.dumps({
        "state": "current",
        "suspended_installation_id": suspended_installation_id,
        "account_id": account_key,
        "current": {
            "installation_id": suspended_installation_id,
            "account_id": account_key,
            "created_at": created_at,
            "suspended": True,
        },
    })


def absent_delete_proof(account_key, installation_id):
    return json.dumps({
        "state": "absent",
        "deleted_installation_id": installation_id,
        "account_id": account_key,
    })


def replacement_delete_proof(account_key, deleted_installation_id, current_installation_id, created_at):
    return json.dumps({
        "state": "replacement",
        "deleted_installation_id": deleted_installation_id,
        "account_id": account_key,
        "current": {
            "installation_id": current_installation_id,
            "account_id": account_key,
            "created_at": created_at,
            "suspended": False,
        },
    })


def run_enqueue(key, account_key, action="created", installation_id=None):
    state = {"started": threading.Event(), "done": threading.Event(), "error": None, "result": None}

    def work():
        c = conn("veripsa_app")
        state["started"].set()
        try:
            with c.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute(
                    "SELECT core.enqueue_webhook_delivery_with_authority(%s,%s,%s,%s,%s::jsonb,%s,%s)",
                    (key, "installation", account_key, None, json.dumps({
                        "action": action,
                        "installation": {
                            "id": installation_id or f"I-{account_key}",
                            "account": {"id": account_key},
                        },
                    }), 1000, 2),
                )
                row = cur.fetchone()
                state["result"] = row[0] if row else None
        except Exception as exc:
            state["error"] = str(exc)
        finally:
            c.close()
            state["done"].set()

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, state


def seed_delivery(key, event_type, account_key, action, received_at, status="processing", installation_id=None):
    c = conn()
    try:
        with c.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute(
                "INSERT INTO core.webhook_delivery("
                "delivery_key,event_type,account_key,payload,status,received_at) "
                "VALUES (%s,%s,%s,%s::jsonb,%s,%s::timestamptz)",
                (key, event_type, account_key, json.dumps({
                    "action": action,
                    "installation": {
                        "id": installation_id or f"I-{account_key}",
                        "account": {"id": account_key},
                    },
                }), status, received_at),
            )
    finally:
        c.close()


def call_with_delivery(c, key, sql, args=()):
    with c.cursor() as cur:
        cur.execute("SELECT set_config('core.current_delivery_key',%s,false)", (key,))
        cur.execute(sql, args)
        row = cur.fetchone()
        return row[0] if row else None


def main():
    boot = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if boot.returncode:
        print("bootstrap failed:\n", boot.stderr[-1000:])
        return 1

    # Production regression: an older rolling instance may still hold the account lifecycle lock exclusively.
    # Exercise the real durable check_suite → EventQueue → make_db_processor chain: the marked admission timeout
    # must restore the claimed attempt and report a deferral, then durable recovery must finish after release.
    sys.path.insert(0, os.path.join(ROOT, "github-app"))
    import delivery_queue as DQ
    import server as S
    import server_boot as SB
    import server_dbops as SDB

    app_dsn = f"postgresql://veripsa_app@localhost/{DB}"
    fleet_blocker = conn()
    with fleet_blocker.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_lock(hashtext('core.boot_reconcile'),hashtext('fleet'))")
    skipped_without_work = False
    try:
        SB._boot_reconcile_throttled(
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("singleflight loser must not read/stamp")),
            object(), 1, app_dsn, 0, True, 0,
        )
        skipped_without_work = True
    finally:
        with fleet_blocker.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext('core.boot_reconcile'),hashtext('fleet'))")
        fleet_blocker.close()
    chk(skipped_without_work,
        "a rolling peer that loses boot singleflight skips the whole read/run/stamp pass")

    original_boot_once = SB._boot_reconcile_throttled_once
    singleton_runs = []
    lock_released_after_error = False
    try:
        SB._boot_reconcile_throttled_once = lambda *_args, **_kwargs: singleton_runs.append("run")
        SB._boot_reconcile_throttled(lambda *_args, **_kwargs: None, object(), 1, app_dsn, 0, True, 0)

        def fail_once(*_args, **_kwargs):
            raise RuntimeError("synthetic reconcile failure")

        SB._boot_reconcile_throttled_once = fail_once
        try:
            SB._boot_reconcile_throttled(
                lambda *_args, **_kwargs: None, object(), 1, app_dsn, 0, True, 0)
        except RuntimeError:
            pass
        probe = conn()
        try:
            with probe.cursor() as cur:
                cur.execute(
                    "SELECT pg_try_advisory_lock(hashtext('core.boot_reconcile'),hashtext('fleet'))")
                lock_released_after_error = cur.fetchone()[0] is True
                if lock_released_after_error:
                    cur.execute(
                        "SELECT pg_advisory_unlock(hashtext('core.boot_reconcile'),hashtext('fleet'))")
        finally:
            probe.close()
    finally:
        SB._boot_reconcile_throttled_once = original_boot_once
    chk(singleton_runs == ["run"] and lock_released_after_error,
        "boot singleflight runs once and session-close releases authority after an exception")

    live_install, live_account = "8099", "ACCT-GH-8099"
    live_route = app(live_install)
    live_route.close()
    scalar(
        "UPDATE core.installation_account SET github_installation_id='A-8099', "
        "github_installation_created_at='2026-01-01 00:00:00+00'::timestamptz "
        "WHERE account_id=%s RETURNING 1",
        (live_account,),
    )
    store = S.DeliveryStore(app_dsn, max_pending=20, max_attempts=3, stale_seconds=1)
    check_payload = {
        "action": "requested",
        "check_suite": {
            "head_sha": "a" * 40,
            "app": {"slug": "other-ci", "name": "Other CI"},
            "pull_requests": [],
        },
        "repository": {
            "id": 98099,
            "full_name": "acme/live-priority",
            "default_branch": "main",
            "owner": {"id": int(live_install), "login": "acme", "type": "Organization"},
        },
        "installation": {
            "id": "A-8099",
            "account": {"id": int(live_install), "login": "acme", "type": "Organization"},
        },
    }
    queued = store.submit(
        "check_suite", check_payload, "live-priority-check-suite",
        account_key=live_install, repo="acme/live-priority")
    old_timeout = os.environ.get("VERIPSA_DB_LOCK_TIMEOUT_MS")
    os.environ["VERIPSA_DB_LOCK_TIMEOUT_MS"] = "150"
    try:
        processor = store.wrap_processor(S.make_db_processor(app_dsn))
    finally:
        if old_timeout is None:
            os.environ.pop("VERIPSA_DB_LOCK_TIMEOUT_MS", None)
        else:
            os.environ["VERIPSA_DB_LOCK_TIMEOUT_MS"] = old_timeout
    worker = S.EventQueue(
        None, object(), process=processor,
        account_of=S._event_account_key, repo_of=S._event_repo,
        branch_from_ref=S._branch_from_ref, retry_attempts=3,
    ).start()
    blocker = conn()
    with blocker.cursor() as cur:
        cur.execute(
            "SELECT pg_advisory_lock(hashtext('core.account_lifecycle'),hashtext(%s))",
            (live_account,),
        )
    first_drained = False
    try:
        worker.submit(queued["event_type"], queued["payload"], queued["key"])
        first_drained = worker.wait_idle(4.0)
        deferred_state = scalar(
            "SELECT jsonb_build_object('status',status,'attempts',attempts,'error',last_error) "
            "FROM core.webhook_delivery WHERE delivery_key='live-priority-check-suite'")
    finally:
        with blocker.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_unlock(hashtext('core.account_lifecycle'),hashtext(%s))",
                (live_account,),
            )
        blocker.close()
    chk(first_drained
        and deferred_state == {
            "status": "queued", "attempts": 0,
            "error": "account lifecycle convergence is busy",
        }
        and worker.failed() == 0 and worker.retried() == 0 and worker.claim_deferred() == 1,
        "durable check_suite contention is attempt-neutral instead of a failed/DLQ attempt")
    scalar(
        "UPDATE core.webhook_delivery SET not_before=now() "
        "WHERE delivery_key='live-priority-check-suite' RETURNING 1")
    recovery = DQ._recover_pending_deliveries(store, worker, limit=20)
    recovered_drained = worker.wait_idle(4.0)
    recovered_state = scalar(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'payload',payload) "
        "FROM core.webhook_delivery WHERE delivery_key='live-priority-check-suite'")
    chk(recovery.get("submitted") == 1 and recovered_drained
        and recovered_state == {"status": "done", "attempts": 1, "payload": {}}
        and worker.processed() == 1 and worker.failed() == 0,
        "durable recovery completes the same check_suite after lifecycle contention clears")

    # Exercise both explicit repository lock layers through the same real durable path. A lock timeout at either
    # boundary is expected rolling-instance contention: it must defer without spending poison budget, release any
    # earlier session lock by closing the event connection, and complete the identical row after recovery.
    original_repo_lock_timeout = SDB._LOCK_TIMEOUT_MS
    SDB._LOCK_TIMEOUT_MS = 150
    try:
        repository_lock_cases = (
            (
                "stable-id",
                "SELECT pg_advisory_lock(hashtext('github-repository-id'),hashtext(%s))",
                "SELECT pg_advisory_unlock(hashtext('github-repository-id'),hashtext(%s))",
                ("98099",),
            ),
            (
                "coordinate",
                "SELECT pg_advisory_lock(hashtext(%s),hashtext(%s))",
                "SELECT pg_advisory_unlock(hashtext(%s),hashtext(%s))",
                (live_install, "acme/live-priority"),
            ),
        )
        for lock_name, take_sql, release_sql, lock_args in repository_lock_cases:
            delivery_key = f"live-priority-{lock_name}-lock"
            repo_queued = store.submit(
                "check_suite", check_payload, delivery_key,
                account_key=live_install, repo="acme/live-priority")
            repo_blocker = conn()
            with repo_blocker.cursor() as cur:
                cur.execute(take_sql, lock_args)
            deferred_before = worker.claim_deferred()
            processed_before = worker.processed()
            failed_before = worker.failed()
            repo_drained = False
            try:
                worker.submit(
                    repo_queued["event_type"], repo_queued["payload"], repo_queued["key"])
                repo_drained = worker.wait_idle(4.0)
                repo_deferred_state = scalar(
                    "SELECT jsonb_build_object('status',status,'attempts',attempts,'error',last_error) "
                    "FROM core.webhook_delivery WHERE delivery_key=%s",
                    (delivery_key,),
                )
            finally:
                with repo_blocker.cursor() as cur:
                    cur.execute(release_sql, lock_args)
                repo_blocker.close()
            chk(repo_drained
                and repo_deferred_state == {
                    "status": "queued", "attempts": 0,
                    "error": "repository convergence is busy",
                }
                and worker.claim_deferred() == deferred_before + 1
                and worker.processed() == processed_before
                and worker.failed() == failed_before,
                f"durable check_suite {lock_name} contention is attempt-neutral")
            scalar(
                "UPDATE core.webhook_delivery SET not_before=now() "
                "WHERE delivery_key=%s RETURNING 1",
                (delivery_key,),
            )
            repo_recovery = DQ._recover_pending_deliveries(store, worker, limit=20)
            repo_recovered_drained = worker.wait_idle(4.0)
            repo_recovered_state = scalar(
                "SELECT jsonb_build_object('status',status,'attempts',attempts,'payload',payload) "
                "FROM core.webhook_delivery WHERE delivery_key=%s",
                (delivery_key,),
            )
            chk(repo_recovery.get("submitted") == 1 and repo_recovered_drained
                and repo_recovered_state == {
                    "status": "done", "attempts": 1, "payload": {},
                }
                and worker.processed() == processed_before + 1
                and worker.failed() == failed_before,
                f"durable recovery completes after {lock_name} contention clears")
    finally:
        SDB._LOCK_TIMEOUT_MS = original_repo_lock_timeout

    # Boot wins first: purge waits for the whole pass, then reaps everything the pass committed.
    install, account = "8101", "ACCT-GH-8101"
    seed_delivery("delete-8101", "installation", install, "deleted",
                  "2026-01-01 00:00:02+00", installation_id="A-8101")
    seeded = app(install)
    seeded.close()
    scalar(
        "UPDATE core.installation_account SET github_installation_id='A-8101', "
        "github_installation_created_at='2026-01-01 00:00:00+00'::timestamptz "
        "WHERE account_id=%s RETURNING 1",
        (account,),
    )
    bg = app(install)
    with bg.cursor() as cur:
        lifecycle_lock(cur, account)
        cur.execute("SELECT core.assert_account_live_with_authority()")

    # The boot pass is read/convergence work. A normal event for the already-current generation must not wait
    # behind it, while a proof-bearing generation mutation remains exclusive.
    current = app(install)
    current_admission = None
    current_error = None
    started_at = time.monotonic()
    try:
        with current.cursor() as cur:
            cur.execute("SET lock_timeout = 250")
            cur.execute(
                "SELECT core.admit_event_installation_generation_with_authority(%s,NULL)",
                ("A-8101",),
            )
            current_admission = cur.fetchone()[0]
    except Exception as exc:
        current_error = str(exc)
    finally:
        current.close()
    chk(current_error is None and current_admission.get("admitted") is True
        and time.monotonic() - started_at < 1.0,
        "current-generation live admission shares boot's account lifecycle lock")

    generation_thread, generation_state = run_generation_admission(
        install, "A-8101", "2026-01-01 00:00:01+00")
    generation_state["started"].wait(5)
    time.sleep(0.35)
    chk(not generation_state["done"].is_set(),
        "proof-bearing generation mutation remains exclusive behind boot")
    with bg.cursor() as cur:
        lifecycle_unlock(cur, account)
    generation_thread.join(10)
    chk(generation_state["done"].is_set() and generation_state["error"] is None
        and generation_state["result"].get("admitted") is True,
        "generation mutation completes after boot releases the shared lifecycle lock")
    with bg.cursor() as cur:
        lifecycle_lock(cur, account)
        cur.execute("SELECT core.assert_account_live_with_authority()")

    thread, state = run_blocked_lifecycle(
        install,
        "SELECT set_config('core.current_delivery_key','delete-8101',false); "
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)" %
        ("'" + absent_delete_proof(install, "A-8101").replace("'", "''") + "'"),
    )
    state["started"].wait(5)
    time.sleep(0.35)
    chk(not state["done"].is_set(), "uninstall waits while boot holds the account lifecycle lock")
    with bg.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/web"))
        lifecycle_unlock(cur, account)
    bg.close()
    thread.join(10)
    chk(state["done"].is_set() and state["error"] is None, "uninstall completes after boot releases the lock")
    chk(active_tombstone(account) == 1 and account_count(account, "co_change") == 0,
        "uninstall tombstones and reaps every write from the earlier boot generation")

    # The same account fence covers GDPR erase, including its hard deletion of the account row.
    install2, account2 = "8102", "ACCT-GH-8102"
    bg2 = app(install2)
    seed_delivery(
        "create-before-erase", "installation", install2, "created",
        "2026-01-01 00:00:00+00", status="done", installation_id="A-8102",
    )
    with bg2.cursor() as cur:
        lifecycle_lock(cur, account2)
        cur.execute("SELECT core.assert_account_live_with_authority()")
    thread2, state2 = run_blocked_lifecycle(install2, "SELECT core.erase_account_with_authority()")
    state2["started"].wait(5)
    time.sleep(0.35)
    chk(not state2["done"].is_set(), "GDPR erase waits while boot holds the account lifecycle lock")
    with bg2.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/web"))
        lifecycle_unlock(cur, account2)
    bg2.close()
    thread2.join(10)
    chk(state2["done"].is_set() and state2["error"] is None, "GDPR erase completes after boot releases the lock")
    chk(active_tombstone(account2) == 0
        and scalar("SELECT count(*) FROM core.account WHERE account_id=%s", (account2,)) == 0,
        "GDPR erase retains no account-keyed lifecycle fence and leaves no resurrected account")

    existing_probe = conn("veripsa_app")
    with existing_probe.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_existing_installation_with_authority(%s)", (install2,))
        existing_after_erase = cur.fetchone()[0]
    existing_probe.close()
    chk(existing_after_erase is None
        and scalar("SELECT count(*) FROM core.account WHERE account_id=%s", (account2,)) == 0,
        "ordinary/background routing cannot lazily recreate an erased account")

    # The same old delivery id must remain a content-free receipt.  If erase deletes the key, enqueue gives the
    # duplicate a fresh receive time and the old activation can be mistaken for a genuine reinstall.
    replay_thread, replay_state = run_enqueue("create-before-erase", install2)
    replay_thread.join(10)
    erased_receipt = scalar(
        "SELECT jsonb_build_object('event_type',event_type,'account_key',account_key,'repo',repo,"
        "'payload',payload,'status',status) FROM core.webhook_delivery WHERE delivery_key=%s",
        ("create-before-erase",),
    )
    chk(replay_state["error"] is None
        and replay_state["result"].get("queued") is False
        and erased_receipt == {
            "event_type": "erased", "account_key": None, "repo": None, "payload": {}, "status": "done"
        }
        and active_tombstone(account2) == 0
        and scalar("SELECT count(*) FROM core.account WHERE account_id=%s", (account2,)) == 0,
        "an erase-before activation delivery id remains a scrubbed receipt and cannot resurrect the account")

    # A genuine activation after erase must be able to authenticate from the durable delivery even though erase
    # removed both the account and installation route. Clean-prod veripsa_app has no credential fallback.
    seed_delivery("create-after-erase", "installation", install2, "created", "2099-01-01 00:00:00+00",
                  installation_id="B-8102")
    reinstalled = app(install2)
    erase_reactivation = call_with_delivery(
        reinstalled,
        "create-after-erase",
        "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-after-erase", generation_proof(install2, "B-8102")),
    )
    reinstalled.close()
    erase_active = active_tombstone(account2)
    erase_accounts = account_count(account2, "account")
    erase_routes = scalar("SELECT count(*) FROM core.installation_account WHERE account_id=%s", (account2,))
    chk(erase_reactivation.get("reactivated") is True
        and erase_active == 0 and erase_accounts == 1 and erase_routes == 1,
        "a durable newer activation re-provisions a GDPR-erased account without an App credential "
        f"(result={erase_reactivation}, active={erase_active}, accounts={erase_accounts}, routes={erase_routes})")

    # Erase and durable admission share the account lock. Hold the account row so erase pauses after acquiring its
    # admission lock, then prove a concurrent activation enqueue waits and is inserted only after erase commits.
    install_race, account_race = "8150", "ACCT-GH-8150"
    seeded_race = app(install_race)
    seeded_race.close()
    blocker = conn(autocommit=False)
    with blocker.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT set_config('core.current_account',%s,true)", (account_race,))
        cur.execute("SELECT 1 FROM core.account WHERE account_id=%s FOR UPDATE", (account_race,))
    erase_thread, erase_state = run_blocked_lifecycle(
        install_race, "SELECT core.erase_account_with_authority()"
    )
    erase_state["started"].wait(5)
    time.sleep(0.35)
    chk(not erase_state["done"].is_set(), "erase is paused after taking the durable account-admission lock")
    enqueue_thread, enqueue_state = run_enqueue(
        "created-during-erase", install_race, installation_id="B-8150")
    enqueue_state["started"].wait(5)
    time.sleep(0.35)
    chk(not enqueue_state["done"].is_set(), "a concurrent activation enqueue waits behind erase admission")
    blocker.commit()
    blocker.close()
    erase_thread.join(10)
    enqueue_thread.join(10)
    queued_after_erase = scalar(
        "SELECT count(*) FROM core.webhook_delivery WHERE delivery_key='created-during-erase' AND status='queued'"
    )
    chk(erase_state["error"] is None and enqueue_state["error"] is None and queued_after_erase == 1,
        "activation accepted during erase is admitted afterward and survives as the next generation")
    c_mark = conn()
    with c_mark.cursor() as cur:
        cur.execute("UPDATE core.webhook_delivery SET status='processing' "
                    "WHERE delivery_key='created-during-erase'")
    c_mark.close()
    race_reinstall = app(install_race)
    race_result = call_with_delivery(
        race_reinstall,
        "created-during-erase",
        "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("created-during-erase", generation_proof(install_race, "B-8150")),
    )
    race_reinstall.close()
    chk(race_result.get("reactivated") is True and active_tombstone(account_race) == 0,
        "the post-erase queued activation reactivates instead of being permanently lost")

    # Durable order: delete(t2), stale create(t1), fresh create(t3), then replay delete(t2).
    install3, account3 = "8201", "ACCT-GH-8201"
    live = app(install3)
    with live.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/web"))
    live.close()
    seed_delivery("delete-t2", "installation", install3, "deleted", "2026-01-01 00:00:02+00",
                  installation_id="A-8201")
    seed_delivery(
        "create-t0", "installation", install3, "created",
        "2026-01-01 00:00:00+00", status="done", installation_id="A-8201",
    )
    seed_delivery("create-t1", "installation", install3, "created", "2026-01-01 00:00:01+00",
                  installation_id="A-8201")
    seed_delivery("create-t3", "installation", install3, "created", "2026-01-01 00:00:03+00",
                  installation_id="B-8201")

    deleted = app(install3)
    purge = call_with_delivery(
        deleted, "delete-t2",
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
        (absent_delete_proof(install3, "A-8201"),),
    )
    with deleted.cursor() as cur:
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,NULL)",
            ("A-8201",),
        )
        tombstoned_preflight = cur.fetchone()[0]
    deleted.close()
    chk(purge.get("ok") and active_tombstone(account3) == 1, "ordered uninstall t2 creates the active lifecycle boundary")
    chk(tombstoned_preflight.get("admitted") is False
        and tombstoned_preflight.get("proof_required") is False,
        "hot-path generation preflight never admits an active tombstone")

    purge_replay_thread, purge_replay_state = run_enqueue("create-t0", install3)
    purge_replay_thread.join(10)
    purge_receipt = scalar(
        "SELECT event_type||':'||status||':'||COALESCE(account_key,'null')||':'||payload::text "
        "FROM core.webhook_delivery WHERE delivery_key='create-t0'"
    )
    chk(purge_replay_state["error"] is None
        and purge_replay_state["result"].get("queued") is False
        and purge_receipt == "erased:done:null:{}" and active_tombstone(account3) == 1,
        "an activation settled before uninstall remains a scrubbed duplicate receipt after working-set purge")

    stale = app(install3)
    old_activation = call_with_delivery(
        stale, "create-t1", "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-t1", generation_proof(install3, "A-8201", "2026-01-01T00:00:01Z")))
    stale.close()
    chk(old_activation.get("reactivated") is False and active_tombstone(account3) == 1,
        "activation t1 cannot clear the newer uninstall t2")

    fresh = app(install3)
    new_activation = call_with_delivery(
        fresh, "create-t3", "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-t3", generation_proof(install3, "B-8201", "2026-01-01T00:00:03Z")))
    with fresh.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/web"))
    fresh.close()
    chk(new_activation.get("reactivated") is True and active_tombstone(account3) == 0,
        "activation t3 clears t2 and opens exactly the newer lifecycle generation")

    replay = app(install3)
    stale_delete = call_with_delivery(
        replay, "delete-t2",
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
        (replacement_delete_proof(
            install3, "A-8201", "B-8201", "2026-01-01T00:00:03Z"),),
    )
    replay.close()
    chk(stale_delete.get("stale_ignored") is True and account_count(account3, "co_change") == 1,
        "replayed uninstall t2 cannot purge data written after activation t3")

    # Schema-first rolling compatibility: an old worker calls the no-arg wrapper and ignores its JSON. Missing
    # delivery authority must abort the transaction so that worker cannot continue onboarding into the tombstone.
    install4, account4 = "8301", "ACCT-GH-8301"
    seed_delivery("delete-legacy", "installation", install4, "deleted", "2026-01-01 00:00:02+00",
                  installation_id="A-8301")
    legacy_delete = app(install4)
    call_with_delivery(
        legacy_delete,
        "delete-legacy",
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
        (absent_delete_proof(install4, "A-8301"),),
    )
    with legacy_delete.cursor() as cur:
        cur.execute("SELECT core.finish_webhook_delivery_with_authority(%s,%s)", ("delete-legacy", 0))
        legacy_finished = cur.fetchone()[0]
    legacy_delete.close()
    legacy_receipt = scalar(
        "SELECT event_type||':'||status||':'||COALESCE(account_key,'null')||':'||payload::text "
        "FROM core.webhook_delivery WHERE delivery_key='delete-legacy'"
    )
    chk(legacy_finished is True and legacy_receipt == "erased:done:null:{}",
        "the current installation.deleted row finalizes as a scrubbed receipt after purge")
    legacy = app(install4, autocommit=False)
    legacy_refused = None
    legacy_write_refused = None
    try:
        with legacy.cursor() as cur:
            cur.execute("SELECT core.reactivate_account_with_authority()")
    except psycopg2.Error as exc:
        legacy_refused = exc.pgcode
    try:
        with legacy.cursor() as cur:
            cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/web"))
    except psycopg2.Error as exc:
        legacy_write_refused = exc.pgcode
    legacy.rollback()
    legacy.close()
    chk(legacy_refused == "55000" and legacy_write_refused == "25P02"
        and active_tombstone(account4) == 1 and account_count(account4, "co_change") == 0,
        "the no-arg rolling bridge aborts an old worker before tombstoned onboarding can write")

    # The proof-less rolling bridge must also abort the very first activation. Otherwise an old worker can make a
    # live account during schema→worker rollout without recording its real installation generation, reopening the
    # point-read→delete race the generation column closes.
    install5, account5 = "8401", "ACCT-GH-8401"
    seed_delivery("create-old-worker", "installation", install5, "created",
                  "2026-01-01 00:00:01+00", installation_id="A-8401")
    old_first = app(install5, autocommit=False)
    old_first_refused = None
    try:
        with old_first.cursor() as cur:
            cur.execute("SELECT core.reactivate_account_with_authority(%s)", ("create-old-worker",))
    except psycopg2.Error as exc:
        old_first_refused = exc.pgcode
    old_first.rollback()
    old_first.close()
    first_generation = scalar(
        "SELECT github_installation_id FROM core.installation_account WHERE account_id=%s", (account5,))
    chk(old_first_refused == "55000" and first_generation is None,
        "the proof-less rolling bridge cannot publish an unfenced first installation generation")

    # Webhook receive order is not lifecycle order. B's activation can be queued before a delayed delete for A,
    # then process afterward. Different live generation proof must reopen B while the local tuple high-water stays
    # monotonic; replaying A must then be rejected by the route generation before it deletes B's data.
    install6, account6 = "8501", "ACCT-GH-8501"
    seed_delivery("create-a-8501", "installation", install6, "created",
                  "2026-01-01 00:00:00+00", installation_id="A-8501")
    first = app(install6)
    first_result = call_with_delivery(
        first, "create-a-8501", "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-a-8501", generation_proof(install6, "A-8501", "2025-12-01T00:00:00Z")))
    first.close()
    seed_delivery("create-b-8501", "installation", install6, "created",
                  "2026-01-01 00:00:01+00", installation_id="B-8501")
    scalar(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,repo,payload,status,received_at) "
        "VALUES ('done-b-private','push',%s,'secret/replacement',"
        "jsonb_build_object('installation',jsonb_build_object('id','B-8501'),"
        "'repository',jsonb_build_object('full_name','secret/replacement')),"
        "'done','2026-01-01 00:00:01.5+00'::timestamptz) RETURNING 1",
        (install6,),
    )
    seed_delivery("delete-a-8501", "installation", install6, "deleted",
                  "2026-01-01 00:00:02+00", installation_id="A-8501")
    delete_a = app(install6)
    delete_a_result = call_with_delivery(
        delete_a, "delete-a-8501",
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
        (replacement_delete_proof(
            install6, "A-8501", "B-8501", "2025-12-15T00:00:00Z"),))
    delete_a.close()
    activate_b = app(install6)
    activate_b_result = call_with_delivery(
        activate_b, "create-b-8501", "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-b-8501", generation_proof(install6, "B-8501", "2025-12-15T00:00:00Z")))
    with activate_b.cursor() as cur:
        cur.execute("SELECT core.ingest_cochange_with_authority(%s::jsonb,%s)", (PAIR, "acme/race"))
    activate_b.close()
    replay_a = app(install6)
    replay_a_result = call_with_delivery(
        replay_a, "delete-a-8501",
        "SELECT core.purge_account_working_set_with_authority(%s::jsonb)",
        (replacement_delete_proof(
            install6, "A-8501", "B-8501", "2025-12-15T00:00:00Z"),))
    replay_a.close()
    lifecycle6 = scalar(
        "SELECT jsonb_build_object('active',active,'at',last_event_received_at,'preserved',"
        "last_event_received_at='2026-01-01 00:00:02+00'::timestamptz,'generation',"
        "(SELECT github_installation_id FROM core.installation_account WHERE account_id=%s LIMIT 1)) "
        "FROM core.account_lifecycle_tombstone WHERE account_id=%s", (account6, account6))
    done_b_residue = scalar(
        "SELECT count(*) FROM core.webhook_delivery WHERE delivery_key='done-b-private'")
    chk(first_result.get("reactivated") is True and delete_a_result.get("ok") is True
        and activate_b_result.get("reactivated") is True
        and replay_a_result.get("stale_ignored") is True
        and delete_a_result.get("stale_ignored") is True
        and lifecycle6 is None
        and account_count(account6, "co_change") == 1 and done_b_residue == 1,
        "replacement B survives delayed delete A even when B's delivery timestamp is older "
        f"(first={first_result}, delete={delete_a_result}, activation={activate_b_result}, "
        f"replay={replay_a_result}, lifecycle={lifecycle6}, cochange={account_count(account6, 'co_change')}, "
        f"terminal_private_residue={done_b_residue})")

    # A delayed App point-read for old A can arrive after B is durable.  Immutable created_at is the generation
    # high-water: the stale proof cannot replace B even when its activation delivery itself arrived later.
    seed_delivery("create-a-stale-8501", "installation", install6, "created",
                  "2026-01-01 00:00:04+00", installation_id="A-8501")
    stale_a = app(install6)
    stale_a_result = call_with_delivery(
        stale_a, "create-a-stale-8501",
        "SELECT core.reactivate_account_with_authority(%s,%s::jsonb)",
        ("create-a-stale-8501", generation_proof(
            install6, "A-8501", "2025-12-01T00:00:00Z")),
    )
    stale_a.close()
    generation_after_stale_a = scalar(
        "SELECT github_installation_id||':'||github_installation_created_at::text "
        "FROM core.installation_account WHERE account_id=%s LIMIT 1", (account6,))
    chk(stale_a_result.get("reactivated") is False
        and generation_after_stale_a.startswith("B-8501:2025-12-15"),
        "an older different activation proof cannot replace the durable newer generation B")

    generation_admission = app(install6)
    with generation_admission.cursor() as cur:
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,NULL)",
            ("B-8501",),
        )
        same_generation_without_proof = cur.fetchone()[0]
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,NULL)",
            ("D-8501",),
        )
        different_generation_without_proof = cur.fetchone()[0]
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,%s::timestamptz)",
            ("B-8501", "2025-12-15T00:00:00Z"),
        )
        same_generation = cur.fetchone()[0]
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,%s::timestamptz)",
            ("C-8501", "2025-12-14T00:00:00Z"),
        )
        older_different_generation = cur.fetchone()[0]
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,%s::timestamptz)",
            ("C-8501", "2026-01-01T00:00:00Z"),
        )
        newer_generation = cur.fetchone()[0]
    generation_admission.close()
    admitted_route = scalar(
        "SELECT github_installation_id FROM core.installation_account WHERE account_id=%s LIMIT 1",
        (account6,),
    )
    chk(same_generation_without_proof.get("admitted") is True
        and same_generation_without_proof.get("proof_required") is False
        and different_generation_without_proof.get("admitted") is False
        and different_generation_without_proof.get("proof_required") is True
        and same_generation.get("admitted") is True
        and older_different_generation.get("admitted") is False
        and newer_generation.get("admitted") is True
        and newer_generation.get("advanced") is True and admitted_route == "C-8501",
        "general event admission fast-allows same/null, requests proof for different/null, and advances only newer proof")

    seed_delivery("suspend-c-8501", "installation", install6, "suspend",
                  "2026-01-02 00:00:00+00", installation_id="C-8501")
    revoked_generation = app(install6)
    with revoked_generation.cursor() as cur:
        cur.execute("SELECT core.release_account_claims_with_authority(%s,%s::jsonb)",
                    ("suspend-c-8501",
                     current_suspend_proof(install6, "C-8501", "2026-01-01T00:00:00Z")))
        current_suspend = cur.fetchone()[0]
        cur.execute(
            "SELECT core.admit_event_installation_generation_with_authority(%s,NULL)",
            ("C-8501",),
        )
        revoked_preflight = cur.fetchone()[0]
    revoked_generation.close()
    chk(current_suspend.get("suspended") is True
        and revoked_preflight.get("admitted") is False
        and revoked_preflight.get("proof_required") is False,
        "hot-path generation preflight denies the exact current generation while it is revoked")

    # A repository removal can arrive immediately after erase commits. The route is gone, so the processor returns
    # without tenant work; finalisation must scrub the newly-arrived identifiers rather than retry forever or lazily
    # recreating the erased account.
    install7, account7, repo7, repo_id7 = "8601", "ACCT-GH-8601", "acme/erased", "98601"
    erasing = app(install7)
    with erasing.cursor() as cur:
        cur.execute("SELECT core.erase_account_with_authority()")
    erasing.close()
    post_erase_payload = {
        "action": "removed",
        "installation": {"id": "A-8601", "account": {"id": install7}},
        "repositories_removed": [{"id": repo_id7, "full_name": repo7}],
    }
    c_seed = conn()
    with c_seed.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute(
            "INSERT INTO core.webhook_delivery("
            "delivery_key,event_type,account_key,repo,payload,status,received_at) "
            "VALUES ('removed-after-erase','installation_repositories',%s,%s,%s::jsonb,'processing',now())",
            (install7, repo7, json.dumps(post_erase_payload)),
        )
    c_seed.close()
    erased_offboard = conn("veripsa_app")
    with erased_offboard.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
                    ("removed-after-erase", 0))
        erased_offboard_finished = cur.fetchone()[0]
    erased_offboard.close()
    erased_offboard_receipt = scalar(
        "SELECT event_type||':'||status||':'||COALESCE(account_key,'null')||':'||"
        "COALESCE(repo,'null')||':'||payload::text FROM core.webhook_delivery "
        "WHERE delivery_key='removed-after-erase'")
    chk(erased_offboard_finished is True
        and erased_offboard_receipt == "erased:done:null:null:{}"
        and scalar("SELECT count(*) FROM core.account WHERE account_id=%s", (account7,)) == 0,
        "a post-erase repository removal becomes a scrubbed receipt without recreating the tenant")

    # A delayed uninstall must not hide or absorb an earlier-received replacement B. B remains the causal head and
    # executes before A can mutate the account; a direct out-of-order claim of A is still refused.
    account8 = "8701"
    seed_delivery("create-b-8701", "installation", account8, "created",
                  "2026-01-01 00:00:01+00", status="queued", installation_id="B-8701")
    seed_delivery("delete-a-8701", "installation", account8, "deleted",
                  "2026-01-01 00:00:02+00", status="queued", installation_id="A-8701")
    q = conn("veripsa_app")
    with q.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,3)")
        pending8 = cur.fetchone()[0]
        cur.execute(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1800,3,3,%s,120)",
            ("delete-a-8701", "lifecycle-delete-a"),
        )
        claim_a8 = cur.fetchone()[0]
        cur.execute(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1800,3,3,%s,120)",
            ("create-b-8701", "lifecycle-create-b"),
        )
        claim_b8 = cur.fetchone()[0]
    q.close()
    pending8_keys = [row.get("key") for row in pending8]
    b8 = scalar("SELECT status||':'||(payload->'installation'->>'id') FROM core.webhook_delivery "
                "WHERE delivery_key='create-b-8701'")
    chk("create-b-8701" in pending8_keys and "delete-a-8701" not in pending8_keys
        and claim_a8.get("claimed") is False and claim_a8.get("reason") == "blocked_by_earlier"
        and claim_b8.get("claimed") is True and b8 == "processing:B-8701",
        "urgent delete A cannot supersede a queued replacement-generation activation B "
        f"(pending={pending8_keys}, claim_a={claim_a8}, claim_b={claim_b8}, B={b8})")

    # Default recovery scans 100 rows. With 101 independent A/B pairs, every slot must expose B; ranking the
    # blocked deletes as urgent would fill all 100 slots with unclaimable A forever and starve every replacement.
    c_reset = conn()
    with c_reset.cursor() as cur:
        cur.execute("DELETE FROM core.webhook_delivery")
        rows = []
        for i in range(101):
            account_key = f"storm-{i:03d}"
            rows.extend([
                (f"storm-b-{i:03d}", account_key, "created", f"B-{i:03d}",
                 "2026-02-01 00:00:01+00"),
                (f"storm-a-{i:03d}", account_key, "deleted", f"A-{i:03d}",
                 "2026-02-01 00:00:02+00"),
            ])
        cur.executemany(
            "INSERT INTO core.webhook_delivery(delivery_key,event_type,account_key,payload,status,received_at) "
            "VALUES (%s,'installation',%s,jsonb_build_object('action',%s,'installation',"
            "jsonb_build_object('id',%s,'account',jsonb_build_object('id',%s))),"
            "'queued',%s::timestamptz)",
            [(key, acct, action, iid, acct, received) for key, acct, action, iid, received in rows],
        )
        cur.execute("SELECT core.pending_webhook_deliveries_with_authority(100,1800,3)")
        storm_pending = cur.fetchone()[0]
    c_reset.close()
    chk(len(storm_pending) == 100
        and all(row.get("payload", {}).get("action") == "created" for row in storm_pending),
        "a default 100-row recovery scan exposes replacement B for 100/101 accounts without delete starvation")

    print("ACCOUNT LIFECYCLE FENCING GATE:", "PASS" if all(checks) else "FAIL")
    subprocess.run(["dropdb", "--if-exists", DB], capture_output=True, text=True)
    return 0 if all(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
