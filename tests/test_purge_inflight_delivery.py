#!/usr/bin/env python3
"""PURGE IN-FLIGHT DURABLE-DELIVERY ROW gate (small-findings root-fix sweep, finding 1).

THE FOOTGUN (root-fixed here): an uninstall is ITSELF a webhook delivery. On the durable-inbox path the
`installation.deleted` row that DRIVES the account-wide purge is, at the moment the purge runs, status='processing'
(the worker claimed it, is executing the handler that called the purge, and will finish() it AFTER the handler
returns). The purge's `DELETE FROM core.webhook_delivery WHERE account_key = …` used to be UNFILTERED, so it deleted
that very in-flight row mid-run. 实测-benign (the worker's finish() then no-ops on the owned-row guard), but a
self-inflicted delete of a live operational row + a spurious "concurrently reclaimed/finalised" finalize log.

THE ROOT FIX: the purge now excludes in-flight rows and, when driven by a durable uninstall, only removes settled
rows at or before its immutable `(received_at, delivery_key)`. The current row survives for the worker's own finish(),
while a later `installation.created` remains queued for ordered recovery instead of being erased by the older delete.

PROVES, against a REAL local Postgres (not a mock of the gate):
  (1) the in-flight 'processing' row for THIS tenant SURVIVES the account-wide purge.
  (2) older SETTLED rows are cleared, but a later queued installation.created survives.
  (3) another tenant's processing row is untouched either way (the account_key match is the tenant scope).
  (4) STRUCTURAL: the DELETE carries both status and durable tuple guards, and the claim predecessor lookup
      uses its causal index.
  (5) REAL DB claims serialize same-account rows by (received_at, delivery_key), independently across accounts.
  (6) TWO connections cannot overtake an uncommitted same-account enqueue; another account remains non-blocking.
  (7) locked causal heads defer immediately and attempt-neutrally without weakening FIFO.
  (8) lowered budgets become DLQ, while fresh processing and failed installation state remain causal barriers.
  (9) generic failed push/PR work is DLQ-isolated and does not freeze an account.
  (10) urgent installation.deleted atomically minimizes prior queued/failed work, but only when claimable and only
       after prior processing completes.

PROCESS-UNIQUE scratch DB (parallel-safe). Run:  python3 tests/test_purge_inflight_delivery.py
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import psycopg2  # noqa: E402

DB = "veripsa_purgeinflight_" + str(os.getpid())
checks = []


def chk(cond, label):
    print(("  [PASS] " if cond else "  [FAIL] ") + label)
    checks.append(bool(cond))


def conn_for(role):
    return psycopg2.connect(f"postgresql://{role}@localhost/{DB}")


def app_conn(install_id):
    """A held App connection that ENTERED `install_id` → pinned to its tenant for the whole 'event'."""
    c = conn_for("veripsa_app")
    c.autocommit = True
    with c.cursor() as cur:
        cur.execute("SET search_path=core")
        cur.execute("SELECT core.enter_installation_with_authority(%s)", (install_id,))
    return c


def admin(sql, args=()):
    conn = conn_for("veripsa_migrator")
    try:
        with conn, conn.cursor() as cur:
            cur.execute("SET search_path=core")
            if args:
                cur.execute(sql, args)
            else:
                cur.execute(sql)
            try:
                row = cur.fetchone()
            except psycopg2.ProgrammingError:
                return None
            return row[0] if row else None
    finally:
        conn.close()


def admin_error_code(sql, args=()):
    conn = conn_for("veripsa_migrator")
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


def synthetic_safe_claim_sql():
    """Return the public fail-closed /4 compatibility wrapper using only synthetic source."""
    return """CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int
) RETURNS jsonb
    LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
  SELECT core.claim_webhook_delivery_with_authority(
    p_key,p_stale_seconds,p_max_attempts,p_protocol,NULL::text)
$$;
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int)
  OWNER TO veripsa_migrator;"""


def seed_delivery(key, account_key, status, repo="acme/web", *, event_type="push", action=None,
                  received_at=None, repository_id=None, installation_id=None):
    # cross-tenant operational table (no per-account RLS), written via the App authority fn in prod; for the test we
    # seed rows directly as the migrator (owner) with explicit status so we can assert the purge's per-status behavior.
    payload = {"action": action} if action else {}
    if event_type in ("installation", "installation_repositories"):
        payload["installation"] = {
            "id": installation_id or f"I-{account_key}",
            "account": {"id": account_key},
        }
    if repository_id is not None:
        payload["repository"] = {"id": repository_id, "full_name": repo}
    lease_generation = 1 if status == "processing" else 0
    admin("INSERT INTO core.webhook_delivery("
          "delivery_key,event_type,account_key,repo,payload,status,received_at,lease_generation,causal_order_version) "
          "VALUES (%s,%s,%s,%s,%s::jsonb,%s,COALESCE(%s::timestamptz,clock_timestamp()),%s,1) "
          "ON CONFLICT (delivery_key) DO UPDATE SET status=EXCLUDED.status, "
          "account_key=EXCLUDED.account_key,lease_generation=EXCLUDED.lease_generation",  # gitleaks:allow
          (key, event_type, account_key, repo, json.dumps(payload), status, received_at, lease_generation))


def count_delivery(key):
    return admin("SELECT count(*)::int FROM core.webhook_delivery WHERE delivery_key=%s", (key,)) or 0


def main() -> int:
    r = subprocess.run(["bash", "db/bootstrap_local.sh", DB], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        print("bootstrap failed:\n", r.stderr[-800:]); return 1

    INSTALL, ACCT = "881", "ACCT-GH-881"          # enter_installation provisions ACCT-GH-881; account_key is the BARE id
    GH_ID = "881"
    OTHER = "999"                                 # a DIFFERENT tenant's account_key — must be untouched

    # ── HOT-APPLY/INTERRUPTION. Put the scratch DB in a rolling-old state: an
    # already-processing row has pre-column generation 0 and the public fail-closed compatibility wrapper
    # carries a visible old marker. The actual module expands additive shape
    # first, then publishes functions atomically. While publication is paused,
    # readers must keep using the complete old function *without* being
    # convoyed behind a retained table lock. Killing psql rolls back only the
    # unpublished functions; the compatible generation-1 bridge remains, and
    # a clean rerun converges.
    admin(
        "INSERT INTO core.webhook_delivery("
        "delivery_key,event_type,account_key,payload,status,lease_generation,locked_at) "
        "VALUES ('migration-processing-zero','push','migration-account','{}'::jsonb,'processing',0,now())"
    )
    admin(synthetic_safe_claim_sql())
    fixture_body = admin(
        "SELECT prosrc FROM pg_proc WHERE oid="
        "'core.claim_webhook_delivery_with_authority(text,int,int,int)'::regprocedure"
    )
    module_path = os.path.join(ROOT, "db", "schema", "25_webhook_queue.sql")
    with open(module_path, encoding="utf-8") as source:
        module_sql = source.read()
    commit_marker = "\nCOMMIT;\n-- END atomic queue-protocol + repository-offboarding rollout switch."
    lock_key = 250000000 + os.getpid()
    interrupted_sql = module_sql.replace(
        commit_marker,
        f"\nSELECT pg_advisory_xact_lock({lock_key}::bigint);"
        + commit_marker,
        1,
    )
    atomic_shape = (interrupted_sql != module_sql
                    and [line.strip() for line in module_sql.splitlines()
                         if line.strip() in ("BEGIN;", "COMMIT;")] == ["BEGIN;", "COMMIT;"])
    temp_path = None
    locker = conn_for("veripsa_migrator")
    locker.autocommit = True
    with locker.cursor() as lock_cur:
        lock_cur.execute("SELECT pg_advisory_lock(%s::bigint)", (lock_key,))
    migration = None
    migration_waiting = False
    old_function_visible = False
    table_read_available = False
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".sql", encoding="utf-8", delete=False) as temp_sql:
            temp_sql.write(interrupted_sql)
            temp_path = temp_sql.name
        env = dict(os.environ)
        env["PGAPPNAME"] = f"veripsa-lease-hot-apply-{os.getpid()}"
        migration = subprocess.Popen(
            ["psql", "-X", f"postgresql://veripsa_migrator@localhost/{DB}",
             "-v", "ON_ERROR_STOP=1", "-q", "-f", temp_path],
            cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        for _ in range(300):
            waiting = admin(
                "SELECT count(*)::int FROM pg_stat_activity "
                "WHERE application_name=%s AND wait_event_type='Lock'",
                (env["PGAPPNAME"],),
            )
            if waiting:
                migration_waiting = True
                break
            if migration.poll() is not None:
                break
            time.sleep(0.01)
        old_probe = admin(
            "SELECT core.claim_webhook_delivery_with_authority('probe',1,3,2)"
        ) if migration_waiting else {}
        old_function_visible = isinstance(old_probe, dict) and old_probe.get("reason") == "missing"
        table_read_available = admin_error_code(
            "SET statement_timeout='250ms'; "
            "SELECT lease_generation FROM core.webhook_delivery "
            "WHERE delivery_key='migration-processing-zero'"
        ) is None
    finally:
        if migration is not None and migration.poll() is None:
            migration.terminate()
            try:
                migration.wait(3)
            except subprocess.TimeoutExpired:
                migration.kill()
                migration.wait(3)
        with locker.cursor() as lock_cur:
            lock_cur.execute("SELECT pg_advisory_unlock(%s::bigint)", (lock_key,))
        locker.close()
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
    interrupted_lease = admin(
        "SELECT lease_generation FROM core.webhook_delivery "
        "WHERE delivery_key='migration-processing-zero'"
    )
    interrupted_body = admin(
        "SELECT prosrc FROM pg_proc WHERE oid="
        "'core.claim_webhook_delivery_with_authority(text,int,int,int)'::regprocedure"
    )
    chk(atomic_shape and migration_waiting and old_function_visible and table_read_available
        and interrupted_lease == 1 and interrupted_body == fixture_body,
        "hot apply keeps table reads unblocked; interruption leaves compatible expand state and the safe wrapper")

    clean_apply = subprocess.run(
        ["psql", "-X", f"postgresql://veripsa_migrator@localhost/{DB}",
         "-v", "ON_ERROR_STOP=1", "-q", "-f", module_path],
        cwd=ROOT, capture_output=True, text=True,
    )
    migrated_lease = admin(
        "SELECT lease_generation FROM core.webhook_delivery "
        "WHERE delivery_key='migration-processing-zero'"
    )
    migrated_body = admin(
        "SELECT prosrc FROM pg_proc WHERE oid="
        "'core.claim_webhook_delivery_with_authority(text,int,int,int)'::regprocedure"
    )
    rolling_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority('migration-processing-zero')"
    )
    chk(clean_apply.returncode == 0 and migrated_lease == 1
        and migrated_body == fixture_body and rolling_finish is True,
        "clean hot re-apply preserves the public-safe /4 wrapper and keeps its generation-1 lease finishable")

    # The remaining gate exercises the post-contract fail-closed compatibility
    # surface. Publish that exact wrapper explicitly; gen19 itself deliberately
    # leaves the operational predecessor /4 in place until the separate
    # contract deploy.
    admin("""
        CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
            p_key text,
            p_stale_seconds int,
            p_max_attempts int,
            p_protocol int
        ) RETURNS jsonb
            LANGUAGE sql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
          SELECT core.claim_webhook_delivery_with_authority(
            p_key,p_stale_seconds,p_max_attempts,p_protocol,NULL::text)
        $$;
        ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int)
          OWNER TO veripsa_migrator
    """)

    conn = app_conn(INSTALL)                      # pin the tenant + lazily provision its account

    def run(sql, args=()):
        with conn.cursor() as cur:
            cur.execute(sql, args)
            row = cur.fetchone()
            return row[0] if row else None

    # ── SEED the durable inbox: this tenant has one row per status, PLUS another tenant's processing row. The
    #    account_key is the BARE GitHub owner id (the server's _event_account_key returns repository.owner.id
    #    verbatim), NOT the 'ACCT-GH-'||id form — exactly the key-format the purge strips to match.
    seed_delivery("d-processing-881", GH_ID, "processing", received_at="2026-01-01T00:00:00Z")
    seed_delivery("d-done-881",       GH_ID, "done",       received_at="2026-01-01T00:01:00Z")
    seed_delivery("d-failed-881",     GH_ID, "failed",     received_at="2026-01-01T00:02:00Z")
    # A superseded processing delete from the same generation may coexist, but the destructive boundary binds the
    # exact current delivery key; it never guesses the latest processing row.
    seed_delivery("a-old-uninstall-881", GH_ID, "processing", event_type="installation", action="deleted",
                  received_at="2026-01-01T00:02:30Z")
    seed_delivery("d-queued-881",     GH_ID, "queued",     received_at="2026-01-01T00:03:00Z")
    # This processing installation.deleted row is the durable purge boundary. The newer queued created row is a
    # genuine later lifecycle generation and must survive for ordered recovery instead of being lost in the purge.
    seed_delivery("a-uninstall-881", GH_ID, "processing", event_type="installation", action="deleted",
                  received_at="2026-01-01T00:04:00Z")
    seed_delivery("z-reinstall-881", GH_ID, "queued", event_type="installation", action="created",
                  received_at="2026-01-01T00:04:00Z", installation_id="I-881-B")
    seed_delivery("d-processing-999", OTHER, "processing", received_at="2026-01-01T00:00:00Z")

    chk(count_delivery("d-processing-881") == 1 and count_delivery("d-done-881") == 1
        and count_delivery("d-failed-881") == 1 and count_delivery("d-queued-881") == 1
        and count_delivery("a-old-uninstall-881") == 1 and count_delivery("a-uninstall-881") == 1
        and count_delivery("z-reinstall-881") == 1
        and count_delivery("d-processing-999") == 1, "seeded one delivery row per status for this tenant + another tenant's processing row")

    # ── RUN the account-wide uninstall purge (the SAME fn the installation.deleted handler runs), pinned to this tenant.
    run("SELECT set_config('core.current_delivery_key',%s,false)", ("a-uninstall-881",))
    delete_proof = json.dumps({
        "state": "absent",
        "deleted_installation_id": "I-881",
        "account_id": GH_ID,
    })
    res = run("SELECT core.purge_account_working_set_with_authority(%s::jsonb)", (delete_proof,))
    res = res if isinstance(res, dict) else json.loads(res)
    chk(res.get("ok") and res.get("account_wide"), "the account-wide purge ran")

    # ── (1) THE ROOT FIX: the IN-FLIGHT processing row SURVIVES — the purge did not delete the row it is running under.
    chk(count_delivery("d-processing-881") == 1,
        "ROOT FIX: the in-flight 'processing' installation.deleted row SURVIVES the purge (the worker's finish() clears it)")
    chk(count_delivery("a-uninstall-881") == 1,
        "the processing durable uninstall authority survives for its worker's finish")
    old_uninstall_receipt = admin(
        "SELECT event_type||':'||status||':'||COALESCE(account_key,'null')||':'||payload::text "
        "FROM core.webhook_delivery WHERE delivery_key='a-old-uninstall-881'"
    )
    chk(old_uninstall_receipt == "erased:done:null:{}",
        "rolling fallback converts the superseded in-flight lifecycle row to a scrubbed duplicate receipt")

    # ── (2) the SETTLED rows are still cleared (no retention regression — the tenant's repo full_names are forgotten).
    chk(count_delivery("d-done-881") == 0 and count_delivery("d-failed-881") == 0 and count_delivery("d-queued-881") == 0,
        "SETTLED rows (done/failed/queued) for this tenant are still CLEARED (no retention regression)")
    chk(count_delivery("z-reinstall-881") == 1,
        "same-timestamp later-key installation.created survives the uninstall purge for ordered recovery")

    # ── (2b) the manifest counts three deleted rows plus the superseded lifecycle row scrubbed to a receipt.
    chk(res.get("purged", {}).get("webhook_deliveries") == 4,
        f"the purge manifest counts 3 deletes + 1 scrubbed lifecycle receipt, not the current uninstall "
        f"(got {res.get('purged', {}).get('webhook_deliveries')})")

    # ── (3) another tenant's processing row is untouched (the account_key match is the tenant scope).
    chk(count_delivery("d-processing-999") == 1, "another tenant's processing row is untouched (account_key is the tenant scope)")

    # ── (4) STRUCTURAL: the status guard is present in the purge body (a refactor that drops it must FAIL this gate).
    body = admin("SELECT pg_get_functiondef('core.purge_account_working_set_with_authority(jsonb)'::regprocedure)")
    has_guard = (bool(body) and "webhook_delivery" in body and "status <> 'processing'" in body
                 and re.search(
                     r"\(received_at,\s*delivery_key\)\s*<=\s*\(v_delivery_received_at,\s*v_delivery_key\)",
                     body,
                 ))
    chk(has_guard, "STRUCTURAL: purge deletion uses the same durable (received_at,delivery_key) order as claim")
    claim_v6_body = admin(
        "SELECT pg_get_functiondef("
        "'core.claim_webhook_delivery_with_authority(text,int,int,int,text,int)'::regprocedure)"
    )
    chk(claim_v6_body.count("FOR UPDATE NOWAIT") == 2
        and claim_v6_body.count("WHEN lock_not_available") == 2
        and claim_v6_body.count("VP001") == 3
        and "FOR UPDATE SKIP LOCKED" not in claim_v6_body,
        "STRUCTURAL: two local head-lock handlers feed one rollback boundary; every other 55P03 stays loud "
        "and the true FIFO head is never skipped")
    legacy_purge_code = None
    try:
        run("SELECT core.purge_account_working_set_with_authority()")
    except psycopg2.Error as exc:
        legacy_purge_code = exc.pgcode
    chk(legacy_purge_code == "55000",
        "proof-less /0 purge fails closed so an old worker cannot choose absence vs replacement")
    predecessor_plan = admin(
        "SET LOCAL enable_seqscan=off; "
        "EXPLAIN (FORMAT JSON,COSTS OFF) SELECT 1 FROM core.webhook_delivery earlier "
        "WHERE COALESCE(earlier.account_key,'')=COALESCE(%s,'') "
        "AND earlier.status IN ('queued','processing') "
        "AND (earlier.received_at,earlier.delivery_key)<(%s::timestamptz,%s) LIMIT 1",
        (GH_ID, "2026-01-01T00:04:00Z", "z-reinstall-881"),
    )
    chk("webhook_delivery_account_causal" in json.dumps(predecessor_plan),
        "EXPLAIN: same-account predecessor lookup uses the causal partial index")

    # ── (5) REAL DB claim order: memory admission order is not authority. A newer same-account event may reach
    # claim first during boot recovery/rolling overlap, but immutable received_at keeps it queued until the older
    # row finishes. A different account remains independent.
    CLAIM_ACCT, OTHER_CLAIM_ACCT = "claim-42", "claim-99"
    seed_delivery("claim-old-delete", CLAIM_ACCT, "queued", event_type="installation", action="deleted",
                  received_at="2026-02-01T00:00:00Z")
    seed_delivery("claim-new-create", CLAIM_ACCT, "queued", event_type="installation", action="created",
                  received_at="2026-02-01T00:01:00Z")
    seed_delivery("claim-other", OTHER_CLAIM_ACCT, "queued", event_type="pull_request", action="opened",
                  received_at="2026-02-01T00:00:30Z")

    def claim(key):
        value = admin(
            "SELECT core.claim_webhook_delivery_with_authority("
            "%s,1800,8,3,%s,120)",
            (key, f"purge-claim-{key}"[:64]),
        )
        return value if isinstance(value, dict) else json.loads(value)

    newer_first = claim("claim-new-create")
    other_first = claim("claim-other")
    older = claim("claim-old-delete")
    newer_while_old_processing = claim("claim-new-create")
    older_finished = admin("SELECT core.finish_webhook_delivery_with_authority(%s)", ("claim-old-delete",))
    newer_after_old = claim("claim-new-create")
    newer_duplicate = claim("claim-new-create")
    newer_finished = admin("SELECT core.finish_webhook_delivery_with_authority(%s)", ("claim-new-create",))
    newer_done = claim("claim-new-create")
    missing = claim("claim-no-such-row")
    chk(newer_first.get("claimed") is False and newer_first.get("reason") == "blocked_by_earlier",
        f"durable order blocks newer-first claim (got {newer_first})")
    chk(other_first.get("claimed") is True,
        "a different account claims independently while this account waits")
    chk(older.get("claimed") is True
        and newer_while_old_processing.get("claimed") is False
        and newer_while_old_processing.get("reason") == "blocked_by_earlier",
        "the older row owns the account head until it finishes")
    chk(older_finished is True and newer_after_old.get("claimed") is True,
        "after the older row finishes, the newer row becomes claimable")
    chk(newer_duplicate.get("reason") == "already_owned" and newer_finished is True
        and newer_done.get("reason") == "already_finished",
        "processing/done false claims are classified as legitimate duplicate states")
    chk(missing.get("reason") == "missing" and missing.get("status") == "missing",
        "a missing durable row is classified separately from duplicate/deferred work")

    # ── (6) TWO-CONNECTION admission race. The older enqueue has returned but its transaction is deliberately left
    # uncommitted. A newer same-account enqueue+claim must wait on the shared account xact lock, while another account
    # proceeds independently. received_at is fixed after that wait, so the predecessor is visible and ordered first.
    lock_account = "causal-lock-42"
    old_conn = conn_for("veripsa_migrator")
    old_conn.autocommit = False
    old_cur = old_conn.cursor()
    old_cur.execute("SET search_path=core")
    old_cur.execute(
        "SELECT core.enqueue_webhook_delivery_with_authority(%s,'push',%s,%s,'{}'::jsonb,1000)",
        ("lock-old", lock_account, "lock/repo"),
    )
    old_enqueued = old_cur.fetchone()[0]
    same_started = threading.Event()
    same_result, same_pid = {}, []

    def enqueue_and_claim_same_account():
        c = conn_for("veripsa_migrator")
        try:
            c.autocommit = False
            with c.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET LOCAL statement_timeout='8s'")
                same_pid.append(c.get_backend_pid())
                same_started.set()
                cur.execute(
                    "SELECT core.enqueue_webhook_delivery_with_authority(%s,'push',%s,%s,'{}'::jsonb,1000)",
                    ("lock-new", lock_account, "lock/repo"),
                )
                same_result["enqueue"] = cur.fetchone()[0]
                cur.execute(
                    "SELECT core.claim_webhook_delivery_with_authority("
                    "%s,1800,8,3,%s,120)",
                    ("lock-new", "purge-lock-new"),
                )
                same_result["claim"] = cur.fetchone()[0]
            c.commit()
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            same_result["error"] = repr(exc)
            c.rollback()
        finally:
            c.close()

    same_thread = threading.Thread(target=enqueue_and_claim_same_account, daemon=True)
    same_thread.start()
    same_started.wait(2.0)
    same_waiting = False
    for _ in range(200):
        if same_pid and admin("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s", (same_pid[0],)) == "Lock":
            same_waiting = True
            break
        time.sleep(0.01)

    other_conn = conn_for("veripsa_migrator")
    other_conn.autocommit = False
    try:
        with other_conn.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET LOCAL statement_timeout='2s'")
            cur.execute(
                "SELECT core.enqueue_webhook_delivery_with_authority(%s,'push',%s,%s,'{}'::jsonb,1000)",
                ("lock-other", "causal-lock-99", "other/repo"),
            )
            other_enqueued = cur.fetchone()[0]
            cur.execute(
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,1800,8,3,%s,120)",
                ("lock-other", "purge-lock-other"),
            )
            other_claimed = cur.fetchone()[0]
        other_conn.commit()
    finally:
        other_conn.close()
    chk(bool(old_enqueued.get("accepted")) and same_waiting and same_thread.is_alive()
        and other_enqueued.get("accepted") and other_claimed.get("claimed"),
        "two connections: same-account newer admission waits for the uncommitted predecessor; another account claims")
    old_conn.commit()
    old_cur.close()
    old_conn.close()
    same_thread.join(5.0)
    lock_times = admin(
        "SELECT jsonb_build_object('old',(SELECT received_at FROM core.webhook_delivery WHERE delivery_key='lock-old'),"
        "'new',(SELECT received_at FROM core.webhook_delivery WHERE delivery_key='lock-new'))"
    )
    chk(not same_thread.is_alive() and "error" not in same_result
        and same_result.get("enqueue", {}).get("accepted")
        and same_result.get("claim", {}).get("reason") == "blocked_by_earlier"
        and lock_times.get("old") < lock_times.get("new"),
        f"lock release fixes received_at after admission and newer claim sees committed predecessor ({same_result})")
    lock_old_claim = claim("lock-old")
    lock_old_done = admin("SELECT core.finish_webhook_delivery_with_authority(%s)", ("lock-old",))
    lock_new_claim = claim("lock-new")
    chk(lock_old_claim.get("claimed") and lock_old_done is True and lock_new_claim.get("claimed"),
        "same-account lane advances only after the committed predecessor finishes")

    # ── (7) A finishing predecessor is normal causal contention. Account- and repository-head lock conflicts
    # return immediately without consuming the target attempt; the committed order is re-evaluated by a later claim.
    seed_delivery("finish-race-old", "finish-race", "processing", received_at="2026-03-01T00:00:00Z")
    seed_delivery("finish-race-new", "finish-race", "queued", received_at="2026-03-01T00:01:00Z")
    seed_delivery("finish-race-other", "finish-race-other-account", "queued",
                  received_at="2026-03-01T00:00:30Z")
    finisher = conn_for("veripsa_migrator")
    finisher.autocommit = False
    finish_cur = finisher.cursor()
    finish_cur.execute("SET search_path=core")
    finish_cur.execute("SELECT core.finish_webhook_delivery_with_authority(%s)", ("finish-race-old",))
    finish_returned = finish_cur.fetchone()[0]
    race_started = threading.Event()
    race_result = {}

    def claim_while_finish_is_uncommitted():
        c = conn_for("veripsa_migrator")
        try:
            c.autocommit = False
            with c.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET LOCAL statement_timeout='8s'")
                race_started.set()
                started_at = time.monotonic()
                cur.execute(
                    "SELECT core.claim_webhook_delivery_with_authority("
                    "%s,1800,8,3,%s,120)",
                    ("finish-race-new", "purge-finish-race"),
                )
                race_result["claim"] = cur.fetchone()[0]
                race_result["elapsed"] = time.monotonic() - started_at
            c.commit()
        except Exception as exc:  # pragma: no cover
            race_result["error"] = repr(exc)
            c.rollback()
        finally:
            c.close()

    race_thread = threading.Thread(target=claim_while_finish_is_uncommitted, daemon=True)
    race_thread.start()
    race_started.wait(2.0)
    race_thread.join(1.0)
    race_returned_while_locked = not race_thread.is_alive()
    account_locked_state = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
        "FROM core.webhook_delivery WHERE delivery_key='finish-race-new'"
    )
    other_while_locked = claim("finish-race-other")
    finisher.commit()
    finish_cur.close()
    finisher.close()
    race_thread.join(5.0)
    account_after_finish = claim("finish-race-new")
    chk(finish_returned is True and race_returned_while_locked and not race_thread.is_alive()
        and "error" not in race_result
        and race_result.get("claim", {}).get("claimed") is False
        and race_result.get("claim", {}).get("reason") == "blocked_by_earlier"
        and race_result.get("elapsed", 99) < 1.0
        and account_locked_state == {"status": "queued", "attempts": 0, "lease": 0}
        and other_while_locked.get("claimed") is True
        and account_after_finish.get("claimed") is True,
        f"account head lock defers immediately/attempt-neutrally; unrelated account and later retry progress "
        f"({race_result}, state={account_locked_state})")

    # The locked causal head can be the target itself while its current owner commits finish(). The conservative
    # transient classification is still blocked_by_earlier (EventQueue's attempt-neutral deferral), but it must not
    # mint a second lease or mutate any claim-owned field. Once finish commits, the same delivery id is a duplicate.
    seed_delivery("finish-self", "finish-self-account", "processing",
                  received_at="2026-03-01T00:02:00Z")
    admin(
        "UPDATE core.webhook_delivery SET attempts=1,locked_at=clock_timestamp(),"
        "owner_instance='finish-self-owner',retry_window_expires_at=clock_timestamp()+interval '2 minutes' "
        "WHERE delivery_key='finish-self'"
    )
    finish_self_before = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation,"
        "'locked_at',locked_at,'owner',owner_instance,'retry_window',retry_window_expires_at) "
        "FROM core.webhook_delivery WHERE delivery_key='finish-self'"
    )
    self_finisher = conn_for("veripsa_migrator")
    self_finisher.autocommit = False
    self_finish_cur = self_finisher.cursor()
    self_finish_cur.execute("SET search_path=core")
    self_finish_cur.execute(
        "SELECT core.finish_webhook_delivery_with_authority(%s)", ("finish-self",),
    )
    self_finish_returned = self_finish_cur.fetchone()[0]
    self_claim_started = time.monotonic()
    self_while_finishing = claim("finish-self")
    self_claim_elapsed = time.monotonic() - self_claim_started
    finish_self_during = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation,"
        "'locked_at',locked_at,'owner',owner_instance,'retry_window',retry_window_expires_at) "
        "FROM core.webhook_delivery WHERE delivery_key='finish-self'"
    )
    self_finisher.commit()
    self_finish_cur.close()
    self_finisher.close()
    self_after_finish = claim("finish-self")
    chk(self_finish_returned is True
        and self_while_finishing.get("claimed") is False
        and self_while_finishing.get("reason") == "blocked_by_earlier"
        and self_while_finishing.get("status") == "processing"
        and self_claim_elapsed < 1.0
        and finish_self_during == finish_self_before
        and self_after_finish.get("claimed") is False
        and self_after_finish.get("reason") == "already_finished"
        and self_after_finish.get("status") == "done",
        f"target==head finish contention defers without claim mutation, then classifies the committed duplicate "
        f"({self_while_finishing}, elapsed={self_claim_elapsed:.3f}s, state={finish_self_during})")

    seed_delivery("repo-finish-old", "repo-finish-old-account", "processing", repo="acme/transferred",
                  received_at="2026-03-02T00:00:00Z", repository_id="990070")
    seed_delivery("repo-finish-new", "repo-finish-new-account", "queued", repo="acme/transferred",
                  received_at="2026-03-02T00:01:00Z", repository_id="990070")
    repo_finisher = conn_for("veripsa_migrator")
    repo_finisher.autocommit = False
    repo_finish_cur = repo_finisher.cursor()
    repo_finish_cur.execute("SET search_path=core")
    repo_finish_cur.execute(
        "SELECT core.finish_webhook_delivery_with_authority(%s)", ("repo-finish-old",),
    )
    repo_finish_returned = repo_finish_cur.fetchone()[0]
    repo_race_started = threading.Event()
    repo_race_result = {}

    def claim_while_repository_finish_is_uncommitted():
        c = conn_for("veripsa_migrator")
        try:
            c.autocommit = False
            with c.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET LOCAL statement_timeout='8s'")
                repo_race_started.set()
                started_at = time.monotonic()
                cur.execute(
                    "SELECT core.claim_webhook_delivery_with_authority("
                    "%s,1800,8,3,%s,120)",
                    ("repo-finish-new", "purge-repo-finish-race"),
                )
                repo_race_result["claim"] = cur.fetchone()[0]
                repo_race_result["elapsed"] = time.monotonic() - started_at
            c.commit()
        except Exception as exc:  # pragma: no cover
            repo_race_result["error"] = repr(exc)
            c.rollback()
        finally:
            c.close()

    repo_race_thread = threading.Thread(
        target=claim_while_repository_finish_is_uncommitted, daemon=True,
    )
    repo_race_thread.start()
    repo_race_started.wait(2.0)
    repo_race_thread.join(1.0)
    repo_race_returned_while_locked = not repo_race_thread.is_alive()
    repository_locked_state = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
        "FROM core.webhook_delivery WHERE delivery_key='repo-finish-new'"
    )
    repo_finisher.commit()
    repo_finish_cur.close()
    repo_finisher.close()
    repo_race_thread.join(5.0)
    repository_after_finish = claim("repo-finish-new")
    chk(repo_finish_returned is True and repo_race_returned_while_locked
        and not repo_race_thread.is_alive() and "error" not in repo_race_result
        and repo_race_result.get("claim", {}).get("claimed") is False
        and repo_race_result.get("claim", {}).get("reason") == "blocked_by_earlier"
        and repo_race_result.get("elapsed", 99) < 1.0
        and repository_locked_state == {"status": "queued", "attempts": 0, "lease": 0}
        and repository_after_finish.get("claimed") is True,
        f"repository head lock defers immediately/attempt-neutrally and later retry preserves FIFO "
        f"({repo_race_result}, state={repository_locked_state})")

    # The exception boundary is intentionally narrower than the claim function. A lock_timeout on the target-row
    # path for an honest-unknown account must remain SQLSTATE 55P03 rather than being mislabeled causal contention.
    seed_delivery("noncausal-lock", None, "queued", received_at="2026-03-03T00:00:00Z")
    noncausal_locker = conn_for("veripsa_migrator")
    noncausal_locker.autocommit = False
    noncausal_lock_cur = noncausal_locker.cursor()
    noncausal_lock_cur.execute("SET search_path=core")
    noncausal_lock_cur.execute(
        "SELECT 1 FROM core.webhook_delivery WHERE delivery_key=%s FOR UPDATE",
        ("noncausal-lock",),
    )
    noncausal_error_code = None
    noncausal_claim = conn_for("veripsa_migrator")
    noncausal_claim.autocommit = False
    try:
        with noncausal_claim.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET LOCAL lock_timeout='100ms'")
            cur.execute(
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,1800,8,3,%s,120)",
                ("noncausal-lock", "purge-noncausal-lock"),
            )
    except psycopg2.Error as exc:
        noncausal_error_code = exc.pgcode
        noncausal_claim.rollback()
    finally:
        noncausal_claim.close()
        noncausal_locker.rollback()
        noncausal_lock_cur.close()
        noncausal_locker.close()
    noncausal_state = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
        "FROM core.webhook_delivery WHERE delivery_key='noncausal-lock'"
    )
    chk(noncausal_error_code == "55P03"
        and noncausal_state == {"status": "queued", "attempts": 0, "lease": 0},
        "non-causal lock_timeout remains fail-loud and attempt-neutral")

    # The account advisory lock precedes the causal-head rollback boundary. Its 55P03 must remain a real store
    # failure; otherwise a database-wide contention fault could be mislabeled as ordinary predecessor deferral.
    seed_delivery("noncausal-advisory", "noncausal-advisory-account", "queued",
                  received_at="2026-03-03T00:01:00Z")
    advisory_locker = conn_for("veripsa_migrator")
    advisory_locker.autocommit = False
    advisory_lock_cur = advisory_locker.cursor()
    advisory_lock_cur.execute("SET search_path=core")
    advisory_lock_cur.execute(
        "SELECT pg_advisory_xact_lock(hashtext('core.webhook_delivery.account'),hashtext(%s))",
        ("noncausal-advisory-account",),
    )
    advisory_error_code = None
    advisory_claim = conn_for("veripsa_migrator")
    advisory_claim.autocommit = False
    try:
        with advisory_claim.cursor() as cur:
            cur.execute("SET search_path=core")
            cur.execute("SET LOCAL lock_timeout='100ms'")
            cur.execute(
                "SELECT core.claim_webhook_delivery_with_authority("
                "%s,1800,8,3,%s,120)",
                ("noncausal-advisory", "purge-noncausal-advisory"),
            )
    except psycopg2.Error as exc:
        advisory_error_code = exc.pgcode
        advisory_claim.rollback()
    finally:
        advisory_claim.close()
        advisory_locker.rollback()
        advisory_lock_cur.close()
        advisory_locker.close()
    advisory_state = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
        "FROM core.webhook_delivery WHERE delivery_key='noncausal-advisory'"
    )
    chk(advisory_error_code == "55P03"
        and advisory_state == {"status": "queued", "attempts": 0, "lease": 0},
        "non-causal advisory-lock timeout remains fail-loud and attempt-neutral")

    # ── (8) Lowered attempt budgets become visible DLQ heads; fresh final-attempt processing remains a barrier.
    seed_delivery("budget-old", "budget-lane", "queued", received_at="2026-04-01T00:00:00Z")
    seed_delivery("budget-new", "budget-lane", "queued", received_at="2026-04-01T00:01:00Z")
    admin("UPDATE core.webhook_delivery SET attempts=5 WHERE delivery_key='budget-old'")
    seed_delivery("budget-processing", "budget-processing-lane", "processing",
                  received_at="2026-04-01T00:00:00Z")
    seed_delivery("budget-processing-new", "budget-processing-lane", "queued",
                  received_at="2026-04-01T00:01:00Z")
    admin("UPDATE core.webhook_delivery SET attempts=5,locked_at=clock_timestamp() "
          "WHERE delivery_key='budget-processing'")
    pending_budget = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,3)")
    pending_budget_keys = {row.get("key") for row in pending_budget}
    budget_state = admin(
        "SELECT jsonb_object_agg(delivery_key,status) FROM core.webhook_delivery "
        "WHERE delivery_key IN ('budget-old','budget-processing')"
    )
    budget_new_claim = claim("budget-new")
    budget_processing_new_claim = claim("budget-processing-new")
    admin("UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='budget-old'")
    budget_rearm = admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    budget_old_claim = claim("budget-old")
    budget_old_done = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("budget-old", budget_old_claim.get("lease_generation")),
    )
    budget_new_after_old = claim("budget-new")
    chk(budget_state.get("budget-old") == "failed"
        and budget_state.get("budget-processing") == "processing"
        and "budget-new" not in pending_budget_keys
        and "budget-processing-new" not in pending_budget_keys
        and budget_new_claim.get("reason") == "blocked_by_earlier"
        and budget_processing_new_claim.get("reason") == "blocked_by_earlier",
        "lowered protocol-1 work becomes an ordered DLQ head; fresh exhausted processing remains a barrier")
    chk(budget_rearm.get("rearmed", 0) >= 1 and budget_old_claim.get("claimed")
        and budget_old_done is True and budget_new_after_old.get("claimed"),
        "protocol-1 DLQ rearm preserves FIFO and opens the account only after the failed predecessor finishes")

    # A pre-lane protocol-0 failure is quarantined: it neither replays over newer state nor freezes live work.
    seed_delivery("generic-failed-push", "generic-failed-lane", "failed",
                  received_at="2026-04-02T00:00:00Z")
    admin("UPDATE core.webhook_delivery SET causal_order_version=0,updated_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='generic-failed-push'")
    seed_delivery("generic-new-pr", "generic-failed-lane", "queued", event_type="pull_request", action="opened",
                  received_at="2026-04-02T00:01:00Z")
    generic_pending = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    generic_pr_claim = claim("generic-new-pr")
    legacy_quarantine_rearm = admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    legacy_quarantine_state = admin(
        "SELECT status FROM core.webhook_delivery WHERE delivery_key='generic-failed-push'"
    )
    chk("generic-new-pr" in {row.get("key") for row in generic_pending}
        and generic_pr_claim.get("claimed") is True and legacy_quarantine_state == "failed",
        f"protocol-0 failed work is nonblocking and non-replayable quarantine ({legacy_quarantine_rearm})")

    # ── (9) Failed installation is unfinished account-state. Rearm preserves order; only completion opens created.
    seed_delivery("dlq-old", "dlq-lane", "failed", event_type="installation", action="deleted",
                  received_at="2026-05-01T00:00:00Z")
    seed_delivery("dlq-new", "dlq-lane", "queued", event_type="installation", action="created",
                  received_at="2026-05-01T00:01:00Z")
    admin("UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='dlq-old'")
    pending_before_rearm = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    dlq_blocked = claim("dlq-new")
    rearmed = admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    dlq_still_blocked = claim("dlq-new")
    dlq_old_claim = claim("dlq-old")
    dlq_old_done = admin("SELECT core.finish_webhook_delivery_with_authority(%s)", ("dlq-old",))
    dlq_new_claim = claim("dlq-new")
    chk("dlq-new" not in {row.get("key") for row in pending_before_rearm}
        and dlq_blocked.get("reason") == "blocked_by_earlier"
        and rearmed.get("rearmed", 0) >= 1
        and dlq_still_blocked.get("reason") == "blocked_by_earlier"
        and dlq_old_claim.get("claimed") and dlq_old_done is True and dlq_new_claim.get("claimed"),
        "failed installation head suppresses pending/claim; rearm replays old before the newer lifecycle event")

    # ── (10) Account uninstall is terminal supersession: queued/failed predecessors are minimized to done, while
    # an earlier processing attempt remains a barrier. Recovery must surface the urgent uninstall ahead of poison.
    seed_delivery("privacy-failed", "privacy-lane", "failed", event_type="installation", action="created",
                  repo="secret/private",
                  received_at="2026-06-01T00:00:00Z")
    seed_delivery("privacy-push-failed", "privacy-lane", "failed", repo="secret/private",
                  received_at="2026-06-01T00:00:30Z")
    seed_delivery("privacy-queued", "privacy-lane", "queued", repo="secret/private",
                  received_at="2026-06-01T00:01:00Z")
    seed_delivery("privacy-delete", "privacy-lane", "queued", event_type="installation", action="deleted",
                  repo="secret/private", received_at="2026-06-01T00:02:00Z")
    urgent_pending = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    urgent_keys = [row.get("key") for row in urgent_pending]
    privacy_claim = claim("privacy-delete")
    privacy_absorbed = admin(
        "SELECT jsonb_object_agg(delivery_key,jsonb_build_object('status',status,'payload',payload,'repo',repo)) "
        "FROM core.webhook_delivery "
        "WHERE delivery_key IN ('privacy-failed','privacy-push-failed','privacy-queued')"
    )
    privacy_release = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,8)",
        ("privacy-delete", "retry uninstall"),
    )
    urgent_retry = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    privacy_retry_claim = claim("privacy-delete")
    chk("privacy-delete" in urgent_keys
        and urgent_keys.index("privacy-delete") < min(
            [urgent_keys.index(k) for k in ("privacy-failed", "privacy-push-failed", "privacy-queued")
             if k in urgent_keys] or [10**6])
        and privacy_claim.get("claimed")
        and all(item.get("status") == "done" and item.get("payload") == {} and item.get("repo") is None
                for item in privacy_absorbed.values())
        and privacy_release == "queued"
        and "privacy-delete" in {row.get("key") for row in urgent_retry}
        and privacy_retry_claim.get("claimed"),
        "urgent uninstall absorbs prior failed installation + generic poison + queued work and remains retryable")

    seed_delivery("privacy-processing", "privacy-processing-lane", "processing",
                  received_at="2026-06-02T00:00:00Z")
    seed_delivery("privacy-processing-delete", "privacy-processing-lane", "queued",
                  event_type="installation", action="deleted", received_at="2026-06-02T00:01:00Z")
    processing_pending = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    processing_delete_claim = claim("privacy-processing-delete")
    chk("privacy-processing-delete" not in {row.get("key") for row in processing_pending}
        and processing_delete_claim.get("reason") == "blocked_by_earlier",
        "urgent uninstall still waits for an earlier in-flight processing attempt")

    # A real finisher can hold the processing head row while an urgent uninstall tries to absorb settled siblings.
    # The NOWAIT deferral must roll the whole prospective supersession back: privacy cleanup is atomic with actually
    # acquiring the uninstall claim, never a mutation performed by an unclaimed delivery.
    seed_delivery("privacy-locked-processing", "privacy-locked-lane", "processing",
                  received_at="2026-06-02T00:02:00Z")
    seed_delivery("privacy-locked-queued", "privacy-locked-lane", "queued",
                  received_at="2026-06-02T00:03:00Z")
    seed_delivery("privacy-locked-delete", "privacy-locked-lane", "queued",
                  event_type="installation", action="deleted", received_at="2026-06-02T00:04:00Z")
    privacy_locker = conn_for("veripsa_migrator")
    privacy_locker.autocommit = False
    privacy_lock_cur = privacy_locker.cursor()
    privacy_lock_cur.execute("SET search_path=core")
    privacy_lock_cur.execute(
        "SELECT 1 FROM core.webhook_delivery WHERE delivery_key=%s FOR UPDATE",
        ("privacy-locked-processing",),
    )
    privacy_locked_started = time.monotonic()
    privacy_locked_claim = claim("privacy-locked-delete")
    privacy_locked_elapsed = time.monotonic() - privacy_locked_started
    privacy_locked_before = admin(
        "SELECT jsonb_object_agg(delivery_key,jsonb_build_object("
        "'status',status,'attempts',attempts,'lease',lease_generation,'payload',payload)) "
        "FROM core.webhook_delivery WHERE delivery_key IN "
        "('privacy-locked-queued','privacy-locked-delete')"
    )
    privacy_locker.commit()
    privacy_lock_cur.close()
    privacy_locker.close()
    privacy_processing_done = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s)",
        ("privacy-locked-processing",),
    )
    privacy_locked_retry = claim("privacy-locked-delete")
    privacy_locked_after = admin(
        "SELECT jsonb_build_object('status',status,'payload',payload,'repo',repo) "
        "FROM core.webhook_delivery WHERE delivery_key='privacy-locked-queued'"
    )
    chk(privacy_locked_claim.get("claimed") is False
        and privacy_locked_claim.get("reason") == "blocked_by_earlier"
        and privacy_locked_elapsed < 1.0
        and privacy_locked_before.get("privacy-locked-queued", {}).get("status") == "queued"
        and privacy_locked_before.get("privacy-locked-delete") == {
            "status": "queued", "attempts": 0, "lease": 0,
            "payload": {
                "action": "deleted",
                "installation": {
                    "id": "I-privacy-locked-lane",
                    "account": {"id": "privacy-locked-lane"},
                },
            },
        }
        and privacy_processing_done is True
        and privacy_locked_retry.get("claimed") is True
        and privacy_locked_after == {"status": "done", "payload": {}, "repo": None},
        "locked uninstall head defers quickly without partial supersession, then atomically absorbs on retry")

    # A delete that is not yet due, or has already exhausted its budget, is not allowed to supersede old work merely
    # because somebody called claim() directly. Supersession starts only with an honestly claimable uninstall.
    seed_delivery("notdue-old", "notdue-delete-lane", "queued", received_at="2026-06-03T00:00:00Z")
    seed_delivery("notdue-delete", "notdue-delete-lane", "queued", event_type="installation", action="deleted",
                  received_at="2026-06-03T00:01:00Z")
    admin("UPDATE core.webhook_delivery SET not_before=clock_timestamp()+interval '1 hour' "
          "WHERE delivery_key='notdue-delete'")
    notdue_claim = claim("notdue-delete")
    seed_delivery("exhausted-delete-old", "exhausted-delete-lane", "queued",
                  received_at="2026-06-04T00:00:00Z")
    seed_delivery("exhausted-delete", "exhausted-delete-lane", "queued",
                  event_type="installation", action="deleted", received_at="2026-06-04T00:01:00Z")
    admin("UPDATE core.webhook_delivery SET attempts=8 WHERE delivery_key='exhausted-delete'")
    exhausted_delete_claim = claim("exhausted-delete")
    ineligible_old_states = admin(
        "SELECT jsonb_object_agg(delivery_key,status) FROM core.webhook_delivery "
        "WHERE delivery_key IN ('notdue-old','exhausted-delete-old')"
    )
    chk(notdue_claim.get("claimed") is False and exhausted_delete_claim.get("claimed") is False
        and ineligible_old_states == {"notdue-old": "queued", "exhausted-delete-old": "queued"},
        "not-due/exhausted installation.deleted cannot supersede older work before becoming claimable")

    causal_v2_plan = admin(
        "SET LOCAL enable_seqscan=off; EXPLAIN (FORMAT JSON,COSTS OFF) "
        "SELECT * FROM core.webhook_delivery earlier "
        "WHERE COALESCE(earlier.account_key,'')=%s "
        "AND (earlier.status IN ('queued','processing') "
        "OR (earlier.status='failed' AND earlier.event_type='installation')) "
        "ORDER BY earlier.received_at,earlier.delivery_key LIMIT 1 FOR UPDATE",
        ("dlq-lane",),
    )
    chk("webhook_delivery_account_causal_v2" in json.dumps(causal_v2_plan),
        "EXPLAIN: queued/processing + failed-installation head lookup uses causal v2 partial index")
    repository_causal_plan = admin(
        "SET LOCAL enable_seqscan=off; EXPLAIN (FORMAT JSON,COSTS OFF) "
        "SELECT * FROM core.webhook_delivery earlier "
        "WHERE earlier.event_type IN "
        "('repository','pull_request','push','check_suite','check_run','merge_group') "
        "AND earlier.payload->'repository'->>'id'=%s "
        "AND earlier.status IN ('queued','processing','failed') "
        "ORDER BY earlier.received_at,earlier.delivery_key LIMIT 1 FOR UPDATE",
        ("99001",),
    )
    chk("webhook_delivery_repository_causal_v1" in json.dumps(repository_causal_plan),
        "EXPLAIN: stable repository.id causal head uses the repository partial expression index")

    # ── (11) MONOTONIC LEASE ABA. A stale generation must never finish/release the owner that reclaimed the same
    # processing status. DLQ rearm resets attempts to zero, but deliberately preserves the lease generation.
    seed_delivery("lease-aba", "lease-aba-account", "queued")
    lease_a = claim("lease-aba")
    admin("UPDATE core.webhook_delivery SET locked_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='lease-aba'")
    lease_b = claim("lease-aba")
    stale_a_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("lease-aba", lease_a.get("lease_generation")),
    )
    stale_a_release = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,2,%s)",
        ("lease-aba", "late generation A", lease_a.get("lease_generation")),
    )
    b_state = admin(
        "SELECT jsonb_build_object('status',status,'lease',lease_generation,'payload',payload) "
        "FROM core.webhook_delivery WHERE delivery_key='lease-aba'"
    )
    b_failed = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,2,%s)",
        ("lease-aba", "generation B fails at budget", lease_b.get("lease_generation")),
    )
    admin("UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='lease-aba'")
    aba_rearmed = admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    lease_c = claim("lease-aba")
    stale_b_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("lease-aba", lease_b.get("lease_generation")),
    )
    stale_b_release = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,8,%s)",
        ("lease-aba", "late generation B", lease_b.get("lease_generation")),
    )
    legacy_finish_on_c = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s)", ("lease-aba",),
    )
    legacy_release_on_c = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,8)",
        ("lease-aba", "late legacy generation"),
    )
    c_state = admin(
        "SELECT jsonb_build_object('status',status,'lease',lease_generation,'attempts',attempts,'payload',payload) "
        "FROM core.webhook_delivery WHERE delivery_key='lease-aba'"
    )
    c_finished = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("lease-aba", lease_c.get("lease_generation")),
    )
    chk(lease_a.get("lease_generation") == 1 and lease_b.get("lease_generation") == 2
        and stale_a_finish is False and stale_a_release == "missing"
        and b_state == {"status": "processing", "lease": 2, "payload": {}}
        and b_failed == "failed" and aba_rearmed.get("rearmed", 0) >= 1
        and lease_c.get("lease_generation") == 3
        and stale_b_finish is False and stale_b_release == "missing"
        and legacy_finish_on_c is False and legacy_release_on_c == "missing"
        and c_state == {"status": "processing", "lease": 3, "attempts": 1, "payload": {}}
        and c_finished is True,
        "stale gen1/gen2 and legacy finalizers cannot mutate gen2/gen3; exact current lease survives attempts ABA")

    # Schema-first rolling claims fail closed when /3 cannot prove its handler
    # budget, even for a fresh row. Legacy generation-1 finalizers remain
    # usable for work claimed before publication; protocol 3 owns all new
    # claims and every replay.
    seed_delivery("legacy-gen1-finish", "legacy-gen1-finish-account", "queued")
    legacy_fresh_code = admin_error_code(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("legacy-gen1-finish",),
    )
    legacy_fresh_state = admin(
        "SELECT jsonb_build_object('status',status,'lease',lease_generation,'attempts',attempts) "
        "FROM core.webhook_delivery WHERE delivery_key='legacy-gen1-finish'"  # gitleaks:allow
    )
    modern_first = claim("legacy-gen1-finish")
    legacy_owned_code = admin_error_code(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("legacy-gen1-finish",),
    )
    legacy_first_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s)",
        ("legacy-gen1-finish",),
    )
    legacy_done = admin(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("legacy-gen1-finish",),
    )
    seed_delivery("legacy-release-reclaim", "legacy-release-reclaim-account", "queued")
    modern_release_claim = claim("legacy-release-reclaim")
    legacy_release = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,8)",
        ("legacy-release-reclaim", "legacy retry"),
    )
    legacy_second_code = admin_error_code(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("legacy-release-reclaim",),
    )
    post_legacy_reject = admin(
        "SELECT jsonb_build_object('status',status,'lease',lease_generation,'attempts',attempts) "
        "FROM core.webhook_delivery WHERE delivery_key='legacy-release-reclaim'"
    )
    new_reclaim = claim("legacy-release-reclaim")
    old_finish_on_new = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s)",
        ("legacy-release-reclaim",),
    )
    old_release_on_new = admin(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,8)",
        ("legacy-release-reclaim", "old worker late"),
    )
    new_reclaim_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("legacy-release-reclaim", new_reclaim.get("lease_generation")),
    )
    chk(legacy_fresh_code == "55000"
        and legacy_fresh_state == {"status": "queued", "lease": 0, "attempts": 0}
        and modern_first.get("claimed") and modern_first.get("lease_generation") == 1
        and legacy_owned_code == "55000"
        and legacy_first_finish is True and legacy_done.get("reason") == "already_finished"
        and modern_release_claim.get("lease_generation") == 1 and legacy_release == "queued"
        and legacy_second_code == "55000"
        and post_legacy_reject == {"status": "queued", "lease": 1, "attempts": 1}
        and new_reclaim.get("lease_generation") == 2
        and old_finish_on_new is False and old_release_on_new == "missing"
        and new_reclaim_finish is True,
        "legacy claims fail closed; gen1 finalizers remain compatible while protocol 3 owns every claim/replay")

    # Legacy false claims other than genuine duplicate ownership/completion must raise. This is the rolling-deploy
    # fence that stops the old EventQueue from counting blocked/missing/not-due/failed/exhausted work as processed.
    seed_delivery("legacy-block-old", "legacy-block-account", "queued",
                  received_at="2026-07-01T00:00:00Z")
    seed_delivery("legacy-block-new", "legacy-block-account", "queued",
                  received_at="2026-07-01T00:01:00Z")
    seed_delivery("legacy-not-due", "legacy-not-due-account", "queued")
    admin("UPDATE core.webhook_delivery SET not_before=clock_timestamp()+interval '1 hour' "
          "WHERE delivery_key='legacy-not-due'")
    seed_delivery("legacy-failed", "legacy-failed-account", "failed")
    seed_delivery("legacy-exhausted", "legacy-exhausted-account", "processing")
    admin("UPDATE core.webhook_delivery SET attempts=8,locked_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='legacy-exhausted'")
    legacy_unsafe_codes = {
        key: admin_error_code(
            "SELECT core.claim_webhook_delivery_with_authority(%s,1,8)", (delivery,),
        )
        for key, delivery in {
            "blocked": "legacy-block-new",
            "missing": "legacy-missing",
            "not_due": "legacy-not-due",
            "failed": "legacy-failed",
            "exhausted": "legacy-exhausted",
        }.items()
    }
    blocked_new = claim("legacy-block-new")
    missing_new = claim("legacy-missing")
    unsafe_states = admin(
        "SELECT jsonb_object_agg(delivery_key,jsonb_build_object('status',status,'attempts',attempts)) "
        "FROM core.webhook_delivery WHERE delivery_key IN "
        "('legacy-block-new','legacy-not-due','legacy-failed','legacy-exhausted')"
    )
    chk(all(code == "55000" for code in legacy_unsafe_codes.values())
        and blocked_new.get("reason") == "blocked_by_earlier"
        and missing_new.get("reason") == "missing"
        and unsafe_states.get("legacy-block-new") == {"status": "queued", "attempts": 0}
        and unsafe_states.get("legacy-not-due") == {"status": "queued", "attempts": 0}
        and unsafe_states.get("legacy-failed") == {"status": "failed", "attempts": 0}
        and unsafe_states.get("legacy-exhausted") == {"status": "processing", "attempts": 8},
        f"legacy unsafe claim outcomes fail closed while v2 preserves structured classification ({legacy_unsafe_codes})")

    # Old enqueue /6 marks Marketplace payload causal-v0 because that
    # sanitizer omitted effective_date. Schema-first /4 cannot prove its own
    # event budget and refuses every fresh row before execution; /3 raises and
    # rolls back. A v1 row still carries the HWM, but only protocol 3 may claim
    # it under the bounded runtime.
    legacy_marketplace_enqueue = admin(
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000)",
        ("legacy-v0-marketplace", "billing-legacy-unsafe", json.dumps({
            "action": "purchased",
            "marketplace_purchase": {"account": {"id": 701}, "plan": {"name": "pro"}},
        })),
    )
    legacy_marketplace_outcome = admin(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8,1)",
        ("legacy-v0-marketplace",),
    )
    legacy_marketplace_code = admin_error_code(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("legacy-v0-marketplace",),
    )
    legacy_marketplace_state = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'lease',lease_generation) "
        "FROM core.webhook_delivery WHERE delivery_key='legacy-v0-marketplace'"  # gitleaks:allow
    )
    safe_marketplace_enqueue = admin(
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000,2)",
        ("safe-v1-marketplace", "billing-legacy-safe", json.dumps({
            "action": "purchased", "effective_date": "2027-01-01T00:00:00Z",
            "marketplace_purchase": {"account": {"id": 702}, "plan": {"name": "pro"}},
        })),
    )
    safe_marketplace_legacy_code = admin_error_code(
        "SELECT core.claim_webhook_delivery_with_authority(%s,1800,8)",
        ("safe-v1-marketplace",),
    )
    safe_marketplace_claim = claim("safe-v1-marketplace")
    safe_marketplace_finish = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s)",
        ("safe-v1-marketplace",),
    )
    chk(legacy_marketplace_enqueue.get("accepted")
        and legacy_marketplace_outcome.get("reason") == "legacy_budget_unproven"
        and legacy_marketplace_code == "55000"
        and legacy_marketplace_state == {"status": "queued", "attempts": 0, "lease": 0}
        and safe_marketplace_enqueue.get("accepted")
        and safe_marketplace_legacy_code == "55000"
        and safe_marketplace_claim.get("claimed")
        and safe_marketplace_claim.get("lease_generation") == 1
        and safe_marketplace_claim.get("payload", {}).get("effective_date") == "2027-01-01T00:00:00Z"
        and safe_marketplace_finish is True,
        "rolling-old claims fail closed before Marketplace execution; protocol 3 drains the HWM-safe payload")

    # Schema-first rolling: old Python can only call enqueue /6, so its incomplete sanitizer is protocol 0. Once
    # failed, a duplicate/redelivery may not mutate or reopen it, DLQ rearm ignores it, and it does not freeze new
    # protocol work. This is the hot-deploy answer when newer done payloads have already been minimized to {}.
    legacy_enqueue = admin(
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000)",
        ("legacy-v0-failed", "billing-legacy", json.dumps({
            "action": "cancelled",
            "marketplace_purchase": {"account": {"id": 700}, "plan": {"name": "free"}},
        })),
    )
    admin("UPDATE core.webhook_delivery SET status='failed',attempts=8,last_error='legacy marker',"
          "updated_at=clock_timestamp()-interval '2 hours' WHERE delivery_key='legacy-v0-failed'")
    legacy_before = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'payload',payload,"
        "'error',last_error,'version',causal_order_version,'updated',updated_at) "
        "FROM core.webhook_delivery WHERE delivery_key='legacy-v0-failed'"
    )
    legacy_duplicate = admin(
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000)",
        ("legacy-v0-failed", "billing-legacy", json.dumps({
            "action": "purchased", "effective_date": "2027-01-01T00:00:00Z",
            "marketplace_purchase": {"account": {"id": 700}, "plan": {"name": "pro"}},
        })),
    )
    admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    legacy_after = admin(
        "SELECT jsonb_build_object('status',status,'attempts',attempts,'payload',payload,"
        "'error',last_error,'version',causal_order_version,'updated',updated_at) "
        "FROM core.webhook_delivery WHERE delivery_key='legacy-v0-failed'"
    )
    new_enqueue = admin(
        "SELECT core.enqueue_webhook_delivery_with_authority("
        "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000,2)",
        ("new-v1-billing", "billing-legacy", json.dumps({
            "action": "purchased", "effective_date": "2027-01-01T00:00:00Z",
            "marketplace_purchase": {"account": {"id": 700}, "plan": {"name": "pro"}},
        })),
    )
    new_billing_claim = claim("new-v1-billing")
    chk(legacy_enqueue.get("accepted") and legacy_before == legacy_after
        and legacy_duplicate.get("accepted") is False
        and legacy_duplicate.get("reason") == "legacy_failed_quarantined"
        and legacy_after.get("status") == "failed" and legacy_after.get("version") == 0
        and new_enqueue.get("accepted") and new_billing_claim.get("claimed"),
        "legacy protocol-0 failed redelivery is immutable quarantine; protocol-1 work remains available")

    # Deterministic release→ON CONFLICT race. The duplicate sees committed `processing` at both probes, then waits
    # on release's uncommitted row update. Once release publishes causal-v0 `failed`, conflict handling must perform
    # no UPDATE whatsoever: payload/error/timestamps and every other stored column remain byte-identical.
    seed_delivery("legacy-v0-race", "billing-legacy-race", "processing",
                  event_type="marketplace_purchase")
    admin(
        "UPDATE core.webhook_delivery SET causal_order_version=0,attempts=1,"
        "payload=%s::jsonb,locked_at=clock_timestamp() WHERE delivery_key='legacy-v0-race'",
        (json.dumps({"action": "cancelled", "marker": "release-source"}),),
    )
    race_release_conn = conn_for("veripsa_migrator")
    race_release_conn.autocommit = False
    race_release_cur = race_release_conn.cursor()
    race_release_cur.execute("SET search_path=core")
    race_release_cur.execute(
        "SELECT core.release_webhook_delivery_with_authority(%s,%s,1,%s)",
        ("legacy-v0-race", "release-race-marker", 1),
    )
    race_release_status = race_release_cur.fetchone()[0]
    race_release_cur.execute(
        "SELECT to_jsonb(d) FROM core.webhook_delivery d WHERE delivery_key='legacy-v0-race'"
    )
    race_release_state = race_release_cur.fetchone()[0]
    race_enqueue_started = threading.Event()
    race_enqueue_result, race_enqueue_pid = {}, []

    def enqueue_while_v0_release_commits():
        c = conn_for("veripsa_migrator")
        try:
            c.autocommit = False
            with c.cursor() as cur:
                cur.execute("SET search_path=core")
                cur.execute("SET LOCAL statement_timeout='8s'")
                race_enqueue_pid.append(c.get_backend_pid())
                race_enqueue_started.set()
                cur.execute(
                    "SELECT core.enqueue_webhook_delivery_with_authority("
                    "%s,'marketplace_purchase',%s,NULL,%s::jsonb,1000)",
                    ("legacy-v0-race", "billing-legacy-race", json.dumps({
                        "action": "purchased", "effective_date": "2027-02-01T00:00:00Z",
                        "marker": "must-not-land",
                    })),
                )
                race_enqueue_result["enqueue"] = cur.fetchone()[0]
            c.commit()
        except Exception as exc:  # pragma: no cover - surfaced by the assertion below
            race_enqueue_result["error"] = repr(exc)
            c.rollback()
        finally:
            c.close()

    race_enqueue_thread = threading.Thread(target=enqueue_while_v0_release_commits, daemon=True)
    race_enqueue_thread.start()
    race_enqueue_started.wait(2.0)
    race_enqueue_waiting = False
    for _ in range(200):
        if (race_enqueue_pid
                and admin("SELECT wait_event_type FROM pg_stat_activity WHERE pid=%s",
                          (race_enqueue_pid[0],)) == "Lock"):
            race_enqueue_waiting = True
            break
        time.sleep(0.01)
    race_release_conn.commit()
    race_release_cur.close()
    race_release_conn.close()
    race_enqueue_thread.join(5.0)
    race_after = admin(
        "SELECT to_jsonb(d) FROM core.webhook_delivery d WHERE delivery_key='legacy-v0-race'"
    )
    chk(race_release_status == "failed" and race_enqueue_waiting
        and not race_enqueue_thread.is_alive() and "error" not in race_enqueue_result
        and race_enqueue_result.get("enqueue", {}).get("accepted") is False
        and race_enqueue_result.get("enqueue", {}).get("reason") == "legacy_failed_quarantined"
        and race_release_state == race_after,
        "release racing duplicate enqueue leaves the entire causal-v0 failed row byte-invariant")

    # Billing plan feeds quota and processing behavior for every later event in the account. Both entitlement
    # directions are causal predecessors: purchased/cancelled failure hides a later push until rearm + finish.
    billing_barrier_results = {}
    for index, action in enumerate(("purchased", "cancelled"), start=1):
        account = f"billing-barrier-{action}"
        old_key = f"billing-{action}-old"
        push_key = f"billing-{action}-push"
        seed_delivery(old_key, account, "failed", event_type="marketplace_purchase", action=action,
                      received_at=f"2026-10-0{index}T00:00:00Z")
        admin(
            "UPDATE core.webhook_delivery SET payload=%s::jsonb,updated_at=clock_timestamp()-interval '2 hours' "
            "WHERE delivery_key=%s",
            (json.dumps({
                "action": action,
                "effective_date": f"2026-10-0{index}T00:00:00Z",
                "marketplace_purchase": {"account": {"id": 800 + index}, "plan": {"name": "pro"}},
            }), old_key),
        )
        seed_delivery(push_key, account, "queued", event_type="push",
                      received_at=f"2026-10-0{index}T00:01:00Z")
        billing_barrier_results[action] = {"blocked": claim(push_key)}
    billing_pending = admin("SELECT core.pending_webhook_deliveries_with_authority(1000,1800,8)")
    billing_pending_keys = {row.get("key") for row in billing_pending}
    admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    for action in ("purchased", "cancelled"):
        old_key = f"billing-{action}-old"
        push_key = f"billing-{action}-push"
        old_claim = claim(old_key)
        old_done = admin(
            "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
            (old_key, old_claim.get("lease_generation")),
        )
        billing_barrier_results[action].update(
            old=old_claim, old_done=old_done, push=claim(push_key),
        )
    seed_delivery("billing-inverse-repo", "billing-inverse-account", "failed",
                  event_type="push", repository_id="88001", received_at="2026-10-03T00:00:00Z")
    seed_delivery("billing-inverse-marketplace", "billing-inverse-account", "queued",
                  event_type="marketplace_purchase", action="purchased",
                  received_at="2026-10-03T00:01:00Z")
    billing_inverse = claim("billing-inverse-marketplace")
    billing_inverse_done = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("billing-inverse-marketplace", billing_inverse.get("lease_generation")),
    )
    chk(all(
        result["blocked"].get("reason") == "blocked_by_earlier"
        and f"billing-{action}-push" not in billing_pending_keys
        and result["old"].get("claimed") and result["old_done"] is True
        and result["push"].get("claimed")
        for action, result in billing_barrier_results.items()
    ) and billing_inverse.get("claimed") and billing_inverse_done is True,
        "failed billing is directional account-wide; a Marketplace target does not inherit an unrelated repo DLQ")

    # Repository rename/transfer changes account_key, so account FIFO alone is insufficient. Stable repository.id
    # is a global lane: a newer event under the new owner waits for the failed old-owner event; another id proceeds.
    seed_delivery("repo-global-old", "repo-owner-a", "failed", repo="owner-a/repo",
                  event_type="repository", action="renamed", repository_id="99001",
                  received_at="2026-08-01T00:00:00Z")
    seed_delivery("repo-global-new", "repo-owner-b", "queued", repo="owner-b/repo",
                  event_type="repository", action="renamed", repository_id="99001",
                  received_at="2026-08-01T00:01:00Z")
    seed_delivery("repo-global-other", "repo-owner-c", "queued", repo="owner-c/other",
                  event_type="repository", action="renamed", repository_id="99002",
                  received_at="2026-08-01T00:00:30Z")
    repo_global_blocked = claim("repo-global-new")
    repo_global_other = claim("repo-global-other")
    admin("UPDATE core.webhook_delivery SET updated_at=clock_timestamp()-interval '2 hours' "
          "WHERE delivery_key='repo-global-old'")
    admin("SELECT core.rearm_failed_webhook_deliveries_with_authority(1,1000)")
    repo_global_old = claim("repo-global-old")
    repo_global_old_done = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("repo-global-old", repo_global_old.get("lease_generation")),
    )
    repo_global_new = claim("repo-global-new")
    chk(repo_global_blocked.get("reason") == "blocked_by_earlier"
        and repo_global_other.get("claimed") and repo_global_old.get("claimed")
        and repo_global_old_done is True and repo_global_new.get("claimed"),
        "stable repository.id FIFO spans account changes while a different repository remains available")

    # A future event type has no proven narrower lane. It therefore falls back to the account and cannot overtake
    # even a known-lane causal-v1 failure; after that predecessor is retried and finished, the unknown row may run.
    seed_delivery("unknown-lane-old", "unknown-lane-account", "failed",
                  event_type="push", received_at="2026-09-01T00:00:00Z")
    seed_delivery("unknown-lane-new", "unknown-lane-account", "queued",
                  event_type="future_github_event", received_at="2026-09-01T00:01:00Z")
    unknown_blocked = claim("unknown-lane-new")
    admin("UPDATE core.webhook_delivery SET status='queued',attempts=0,last_error=NULL "
          "WHERE delivery_key='unknown-lane-old'")
    unknown_old = claim("unknown-lane-old")
    unknown_old_done = admin(
        "SELECT core.finish_webhook_delivery_with_authority(%s,%s)",
        ("unknown-lane-old", unknown_old.get("lease_generation")),
    )
    unknown_new = claim("unknown-lane-new")
    chk(unknown_blocked.get("reason") == "blocked_by_earlier"
        and unknown_old.get("claimed") and unknown_old_done is True and unknown_new.get("claimed"),
        "unknown event types conservatively fall back to account FIFO against known causal-v1 failures")

    conn.close()

    ok = all(checks)
    print()
    if ok:
        print("PURGE IN-FLIGHT DELIVERY GATE: PASS")
        return 0
    print(f"PURGE IN-FLIGHT DELIVERY GATE: FAIL ({sum(1 for c in checks if not c)} of {len(checks)} failed)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
