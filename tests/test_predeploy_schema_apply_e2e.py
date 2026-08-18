#!/usr/bin/env python3
"""PREDEPLOY SCHEMA-APPLY END-TO-END gate — the script ACTUALLY APPLIES the schema (not just that the YAML names it).

The wiring gate (test_predeploy_schema_wiring.py) proves the SHAPE: the script exists, the Dockerfile installs
psql + COPYs db/, render.yaml declares preDeployCommand pointing at the script, OWNER_DSN is a sync:false secret.
But the wiring being correct is not the same as the script DOING ITS JOB. This gate drives the script against a
REAL ephemeral Postgres (the same per-PID scratch DB pattern the rest of the suite uses) and proves:

  Every generation-dependent assertion is RELATIVE to the checked-in db/schema_generation (G), so a routine
  generation bump never restales this gate.

  (1) FRESH — given a fresh owner-owned DB and OWNER_DSN, the script applies db/schema.sql successfully,
      stamps generation G, and the schema actually lands.
  (2) CURRENT — the same generation + digest performs zero full-schema DDL.
  (3) UNMARKED AT GEN>1 — an unmarked existing DB (a DR restore / legacy DB behind current) APPLIES the full
      schema idempotently and stamps an APPLIED marker at generation G. Generation-1 adoption (contract-gated,
      DDL-skipped) is the one-time bootstrap, unit-tested in test_schema_generation_manifest.py.
  (4) STATE MACHINE — malformed markers, same-generation/different-digest fail closed;
      a newer live generation makes a rollback skip; an older generation applies exactly once under the lock.
      A failed apply never advances the marker.
  (5) RETRY SAFETY — a canceled concurrent non-unique graph-index build leaves an invalid catalog shell; the next
      full pre-deploy removes it and completes the index instead of being poisoned forever by IF NOT EXISTS.
      An invalid UNIQUE shell is never auto-dropped (which would open a live uniqueness gap): deploy fails closed
      and preserves it for explicit operator repair. The sole expansion exception is the exact
      policy_refresh_outbox identity index while the legacy primary key still enforces uniqueness; retry removes
      that unattached shell, rebuilds it, and attaches the final primary-key contract.
      A historical partial module-25 commit which left the old marker but published the NULL-owner /4 fence is
      recognized by exact function metadata and rejected without falsely advancing the marker.
  (6) LEGACY FUNCTION ABI — an older generation's published input parameter names remain replace-compatible;
      PostgreSQL rejects an input-name rename even when the argument types and arity are unchanged.
  (7) PROMOTION-SAFE EXPAND + READINESS-GATED CONTRACT — generic pre-deploy and variable-free manual apply
      preserve the serving image's legacy claim body. Only the explicit final one-off transaction replaces it,
      disables legacy policy claims and generic graph writes, and re-stamps the exact already-live marker. An
      injected final-marker failure rolls every fence back; pre-cutover in-flight terminal CAS remains usable.
      Durable function comments keep all fences closed across later full schema replays.
  (8) LIVE-LOCK CONVOY — an old-worker transaction holding the delivery table makes pre-deploy DDL fail on
      the short lock_timeout; a queued app DML statement resumes promptly and the live marker is unchanged.
  (9) LOCK-HOLD CONVOY — after expansion DDL acquires its lock immediately, deliberately pause later function
      publication for modules 25 and 30; live table DML still completes under 750ms because expansion locks were
      committed before the long publication transaction.
  (10) ROLLING UPGRADE — on a cluster with every origin/main role but no new veripsa_backup role, applying only
      30_gate.sql preserves a legacy GDPR-erasure tombstone and the old worker's provisioning route still cannot
      resurrect the erased tenant. The subsequent full predeploy succeeds without creating or substituting the
      missing role; App/readers still cannot reach the export.
  (11) FAIL-LOUD — given an UNREACHABLE OWNER_DSN, the script exits NON-ZERO (so Render fails the deploy + keeps
      the prior image serving). The 2026-06-25 incident class is closed by this exit code propagating, NOT just
      by the script existing.
  (12) UNSET-OWNER-DSN DEGRADE — given no OWNER_DSN, the script exits 0 + logs the "skipping" line (so a fresh
      service whose secret isn't wired yet can still deploy and serve, falling back to boot-time assertion).

Run: python3 tests/test_predeploy_schema_apply_e2e.py   (needs local Postgres with the veripsa roles bootstrapped;
run_gates.sh stands up the ephemeral cluster + bootstraps the roles before this gate runs).
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "github-app", "scripts", "predeploy_schema.sh")
# The image schema generation is the checked-in db/schema_generation. MODULE-LEVEL so both main() and the
# separate --rolling-upgrade-child process see it; every generation-dependent assertion is RELATIVE to it,
# so a routine generation bump never restales this gate.
G = int((Path(ROOT) / "db" / "schema_generation").read_text(encoding="ascii").strip())
SYNTHETIC_PREDECESSOR_CLAIM_V4_MD5 = "8bdf79d0259b27c37f48a9f52c0416b6"

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB.
DB = "veripsa_predeploye2e_" + str(os.getpid())
BAD_CONTRACT_DB = "veripsa_predeploybadcontract_" + str(os.getpid())
ROLLING_DB = "veripsa_predeployrolling_" + str(os.getpid())


def _run_script(env_overlay: dict) -> tuple[int, str]:
    env = os.environ.copy()
    env.update(env_overlay)
    # ensure the script can find the repo root regardless of where psql / db/ live
    env["VERIPSA_REPO_ROOT"] = ROOT
    r = subprocess.run(["bash", SCRIPT], env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout + r.stderr)


def _run_contract_cutover(
    owner_dsn: str, marker: str
) -> tuple[int, str]:
    """Run only the readiness-gated final transaction, as the one-off does."""
    r = subprocess.run(
        [
            "psql",
            owner_dsn,
            "-X",
            "-v",
            "ON_ERROR_STOP=1",
            "-v",
            f"veripsa_schema_marker={marker}",
            "-v",
            "veripsa_schema_contract_cutover=worker-ready-v1",
            "-f",
            "schema/100_schema_cutover.sql",
        ],
        cwd=os.path.join(ROOT, "db"),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return r.returncode, r.stdout + r.stderr


def _psql_query(dsn: str, sql: str) -> tuple[int, str]:
    r = subprocess.run(["psql", dsn, "-X", "-At", "-c", sql], capture_output=True, text=True, timeout=30)
    return r.returncode, (r.stdout or "").strip()


def _isolate_claim_v4_sql(module_sql: str) -> str:
    signature = """CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int
) RETURNS jsonb"""
    end_marker = (
        "ALTER FUNCTION "
        "core.claim_webhook_delivery_with_authority(text,int,int,int) "
        "OWNER TO veripsa_migrator;"
    )
    start = module_sql.find(signature)
    end = module_sql.find(end_marker, start)
    if start < 0 or end < 0:
        raise RuntimeError("claim /4 definition could not be isolated")
    block = module_sql[start:end + len(end_marker)]
    return "\n".join(
        line for line in block.splitlines()
        if not line.startswith("\\")
    )


def _synthetic_predecessor_claim_sql() -> str:
    """Return a public synthetic predecessor with the complete /4 catalog ABI."""
    return """CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
    p_key text,
    p_stale_seconds int,
    p_max_attempts int,
    p_protocol int
) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path TO 'core','pg_catalog' AS $$
DECLARE
  v_result jsonb;
BEGIN
  v_result := core.claim_webhook_delivery_with_authority(
    p_key,p_stale_seconds,p_max_attempts,p_protocol,
    'wk-00000000000000000000000000000000.0001.0000000000000000000000');
  RETURN v_result;
END
$$;
ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int)
  OWNER TO veripsa_migrator;"""


def _exact_current_safe_claim_sql() -> str:
    module = (
        Path(ROOT) / "db" / "schema" / "25_webhook_queue.sql"
    ).read_text(encoding="utf-8")
    return _isolate_claim_v4_sql(module)


def _install_synthetic_predecessor_claim(dsn: str) -> tuple[int, str]:
    return _psql_query(dsn, _synthetic_predecessor_claim_sql())


def _leave_invalid_index(
    owner_dsn: str,
    *,
    index_name: str,
    table_name: str,
    create_sql: str,
    application_tag: str,
) -> tuple[bool, str]:
    """Cancel a real CIC after its catalog shell commits."""
    drop_rc, drop_out = _psql_query(
        owner_dsn, f"DROP INDEX CONCURRENTLY IF EXISTS core.{index_name}")
    if drop_rc != 0:
        return False, f"drop_rc={drop_rc} drop_out={drop_out!r}"

    blocker_name = f"veripsa_invalid_{application_tag}_blocker"
    builder_name = f"veripsa_invalid_{application_tag}_builder"
    blocker_dsn = owner_dsn + f"?application_name={blocker_name}"
    builder_dsn = owner_dsn + f"?application_name={builder_name}"
    blocker = subprocess.Popen(
        [
            "psql", blocker_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q",
            "-c",
            f"BEGIN; LOCK TABLE core.{table_name} IN ROW EXCLUSIVE MODE; "
            "SELECT pg_sleep(30); ROLLBACK",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    builder: subprocess.Popen[str] | None = None
    detail = ""
    try:
        deadline = time.monotonic() + 5
        lock_seen = False
        while time.monotonic() < deadline:
            lock_rc, lock_count = _psql_query(
                owner_dsn,
                "SELECT count(*) FROM pg_locks "
                "WHERE database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
                f"AND relation='core.{table_name}'::regclass "
                "AND mode='RowExclusiveLock' AND granted",
            )
            if lock_rc == 0 and lock_count != "0":
                lock_seen = True
                break
            time.sleep(0.05)
        if not lock_seen:
            return False, "blocker never acquired RowExclusiveLock"

        builder = subprocess.Popen(
            [
                "psql", builder_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q",
                "-c", create_sql,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 8
        shell_seen = False
        while time.monotonic() < deadline:
            shell_rc, shell_state = _psql_query(
                owner_dsn,
                "SELECT (NOT i.indisvalid OR NOT i.indisready)::text "
                "FROM pg_index i "
                f"WHERE i.indexrelid='core.{index_name}'::regclass",
            )
            if shell_rc == 0 and shell_state == "true":
                shell_seen = True
                break
            if builder.poll() is not None:
                break
            time.sleep(0.05)
        if not shell_seen:
            detail = "concurrent build completed before an invalid shell was observable"
            return False, detail

        cancel_rc, cancel_out = _psql_query(
            owner_dsn,
            "SELECT COALESCE(bool_and(pg_cancel_backend(pid)),false) "
            "FROM pg_stat_activity "
            "WHERE datname=current_database() "
            f"AND application_name='{builder_name}'",
        )
        builder_out, _ = builder.communicate(timeout=10)
        state_rc, state = _psql_query(
            owner_dsn,
            "SELECT i.indisvalid::text || '|' || i.indisready::text "
            "FROM pg_index i "
            f"WHERE i.indexrelid='core.{index_name}'::regclass",
        )
        detail = (
            f"cancel_rc={cancel_rc} cancel={cancel_out!r} "
            f"builder_rc={builder.returncode} state={state!r} out={builder_out[-200:]!r}"
        )
        return (
            cancel_rc == 0
            and cancel_out == "t"
            and builder.returncode != 0
            and state_rc == 0
            and state.startswith("false|"),
            detail,
        )
    finally:
        for app_name in (builder_name, blocker_name):
            _psql_query(
                owner_dsn,
                "SELECT COALESCE(bool_and(pg_cancel_backend(pid)),false) "
                "FROM pg_stat_activity "
                "WHERE datname=current_database() "
                f"AND application_name='{app_name}'",
            )
        if builder is not None and builder.poll() is None:
            builder.terminate()
            builder.communicate(timeout=10)
        if blocker.poll() is None:
            blocker.terminate()
        blocker.communicate(timeout=10)


def _leave_invalid_graph_index(owner_dsn: str) -> tuple[bool, str]:
    """Create the interrupted non-unique build that retry may self-heal."""
    return _leave_invalid_index(
        owner_dsn,
        index_name="code_node_coord_uncertain",
        table_name="code_node",
        create_sql=(
            "CREATE INDEX CONCURRENTLY code_node_coord_uncertain "
            "ON core.code_node (account_id,repo,branch) "
            "WHERE analysis_status IS NOT NULL"
        ),
        application_tag="graph",
    )


def _leave_invalid_unique_claim_index(
    owner_dsn: str,
) -> tuple[bool, str]:
    """Create an interrupted unique build that retry must preserve/fail."""
    return _leave_invalid_index(
        owner_dsn,
        index_name="claim_one_active",
        table_name="claim",
        create_sql=(
            "CREATE UNIQUE INDEX CONCURRENTLY claim_one_active "
            "ON core.claim (account_id,repo,branch,target_path) "
            "WHERE claim_state='active'"
        ),
        application_tag="claim_unique",
    )


def _leave_invalid_policy_identity_index(
    owner_dsn: str,
) -> tuple[bool, str]:
    """Create the one UNIQUE shell retry may heal while the legacy PK remains."""
    fixture_rc, fixture_out = _psql_query(
        owner_dsn,
        "TRUNCATE core.policy_refresh_outbox; "
        "ALTER TABLE core.policy_refresh_outbox "
        "DROP CONSTRAINT policy_refresh_outbox_identity_uq, "
        "ADD CONSTRAINT policy_refresh_outbox_pkey PRIMARY KEY (account_id)",
    )
    if fixture_rc != 0:
        return False, f"legacy_pk_rc={fixture_rc} out={fixture_out!r}"
    return _leave_invalid_index(
        owner_dsn,
        index_name="policy_refresh_outbox_identity_uq",
        table_name="policy_refresh_outbox",
        create_sql=(
            "CREATE UNIQUE INDEX CONCURRENTLY "
            "policy_refresh_outbox_identity_uq "
            "ON core.policy_refresh_outbox "
            "(account_id,request_kind,repository_id)"
        ),
        application_tag="policy_identity",
    )


def _leave_invalid_actual_unique_delivery_index(
    owner_dsn: str,
) -> tuple[bool, str]:
    """Create a unique shell under a module-25 non-unique contract."""
    return _leave_invalid_index(
        owner_dsn,
        index_name="webhook_delivery_pending",
        table_name="webhook_delivery",
        create_sql=(
            "CREATE UNIQUE INDEX CONCURRENTLY webhook_delivery_pending "
            "ON core.webhook_delivery (status,received_at) "
            "WHERE status IN ('queued','processing')"
        ),
        application_tag="delivery_unique",
    )


def _marker(dsn: str) -> str:
    rc, value = _psql_query(
        dsn,
        "SELECT COALESCE(obj_description(oid,'pg_namespace'),'') "
        "FROM pg_namespace WHERE nspname='core'",
    )
    return value if rc == 0 else "QUERY-FAILED"


def _set_marker(dsn: str, value: str | None) -> None:
    literal = "NULL" if value is None else "'" + value.replace("'", "''") + "'"
    rc, _ = _psql_query(dsn, f"COMMENT ON SCHEMA core IS {literal}")
    if rc != 0:
        raise RuntimeError("could not set schema marker fixture")


def _legacy_claim_acl_shape(
    dsn: str, unexpected_role: str
) -> tuple[int, str]:
    """Return non-App|extra|App-clean|App-grantable|total ACL rows."""
    return _psql_query(
        dsn,
        "WITH target AS ("
        " SELECT p.oid,p.proowner,p.proacl"
        " FROM pg_proc p"
        " WHERE p.oid IN ("
        "  'core.claim_webhook_delivery_with_authority("
        "text,integer,integer,integer)'::regprocedure,"
        "  'core.claim_policy_refresh_with_authority("
        "text,integer,integer,integer)'::regprocedure,"
        "  'core.claim_policy_refresh_with_authority("
        "text,integer,integer,integer,boolean)'::regprocedure"
        " )"
        "), expanded AS ("
        " SELECT t.proowner,a.grantee,a.privilege_type,a.is_grantable,"
        "        r.rolname"
        " FROM target t"
        " CROSS JOIN LATERAL aclexplode("
        "  COALESCE(t.proacl,acldefault('f',t.proowner))) a"
        " LEFT JOIN pg_roles r ON r.oid=a.grantee"
        ")"
        " SELECT "
        " count(*) FILTER (WHERE grantee<>proowner AND "
        "   COALESCE(rolname,'PUBLIC')<>'veripsa_app')"
        " || '|' ||"
        f" count(*) FILTER (WHERE rolname='{unexpected_role}')"
        " || '|' ||"
        " count(*) FILTER (WHERE rolname='veripsa_app' "
        "   AND privilege_type='EXECUTE' AND NOT is_grantable)"
        " || '|' ||"
        " count(*) FILTER (WHERE rolname='veripsa_app' "
        "   AND privilege_type='EXECUTE' AND is_grantable)"
        " || '|' || count(*)"
        " FROM expanded",
    )


def _direct_graph_acl_shape(
    dsn: str, unexpected_role: str
) -> tuple[int, str]:
    """Return non-owner|named-extra|owner|total ACL rows for direct writers."""
    return _psql_query(
        dsn,
        "WITH target AS ("
        " SELECT p.oid,p.proowner,p.proacl"
        " FROM pg_proc p"
        " WHERE p.oid IN ("
        "  'core.ingest_graph_with_authority("
        "jsonb,text,text,text,timestamptz)'::regprocedure,"
        "  'core.patch_graph_with_authority("
        "jsonb,text,text,text[],text[],text,timestamptz)'::regprocedure"
        " )"
        "), expanded AS ("
        " SELECT t.proowner,a.grantee,a.privilege_type,a.is_grantable,"
        "        r.rolname"
        " FROM target t"
        " CROSS JOIN LATERAL aclexplode("
        "  COALESCE(t.proacl,acldefault('f',t.proowner))) a"
        " LEFT JOIN pg_roles r ON r.oid=a.grantee"
        ")"
        " SELECT "
        " count(*) FILTER (WHERE grantee<>proowner)"
        " || '|' ||"
        f" count(*) FILTER (WHERE rolname='{unexpected_role}')"
        " || '|' ||"
        " count(*) FILTER (WHERE grantee=proowner "
        "   AND privilege_type='EXECUTE' AND NOT is_grantable)"
        " || '|' || count(*)"
        " FROM expanded",
    )


def _fake_psql(*, exit_code: int, delegate: str | None = None, call_log: str | None = None,
               delay_seconds: int = 0) -> tuple[str, str]:
    directory = tempfile.mkdtemp(prefix="veripsa-fake-psql-")
    path = os.path.join(directory, "psql")
    lines = ["#!/usr/bin/env bash", "set -eu"]
    if call_log:
        lines.append(f"printf 'called\\n' >> {shlex.quote(call_log)}")
    if delay_seconds:
        lines.append(f"sleep {delay_seconds}")
    if delegate:
        lines.append(f"exec {shlex.quote(delegate)} \"$@\"")
    else:
        lines.append(f"exit {exit_code}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return directory, directory + os.pathsep + os.environ.get("PATH", "")


def _run_rolling_upgrade_child() -> int:
    """Run where the expected veripsa_backup role name is genuinely absent."""
    admin = os.environ["ADMIN_DSN"]
    roles_path = os.path.join(ROOT, "db", "roles.sql")
    with open(roles_path, encoding="utf-8") as fh:
        roles_sql = fh.read()
    role_line = ("    'veripsa_backup',       -- dedicated cross-tenant DR export principal "
                 "(only the export gate; no table grants)\n")
    if roles_sql.count(role_line) != 1:
        print("rolling fixture FAILED: could not remove exactly one veripsa_backup role declaration")
        return 1
    legacy_roles_sql = roles_sql.replace(role_line, "")
    br = subprocess.run(
        ["psql", admin, "-X", "-v", "ON_ERROR_STOP=1", "-q"],
        input=legacy_roles_sql, capture_output=True, text=True,
    )
    if br.returncode != 0:
        print("rolling fixture roles FAILED:", br.stderr[-800:])
        return 1

    subprocess.run(["dropdb", ROLLING_DB], capture_output=True)
    cr = subprocess.run(["createdb", ROLLING_DB, "-O", "veripsa_migrator"],
                        capture_output=True, text=True)
    if cr.returncode != 0:
        print("rolling fixture createdb FAILED:", cr.stderr[-800:])
        return 1

    owner_dsn = f"postgresql://veripsa_migrator@localhost/{ROLLING_DB}"
    app_dsn = f"postgresql://veripsa_app@localhost/{ROLLING_DB}"
    checks: list[tuple[str, bool]] = []
    try:
        # Exact schema-prefix state before 30_gate.sql, plus the legacy origin/main tombstone shape/data. Applying
        # only the changed module models Render's schema-first window while the old Python worker is still live.
        for module in ("10_substrate.sql", "20_core.sql", "25_webhook_queue.sql"):
            mr = subprocess.run(
                ["psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-f",
                 os.path.join(ROOT, "db", "schema", module)],
                capture_output=True, text=True,
            )
            if mr.returncode != 0:
                print(f"rolling fixture {module} FAILED:", mr.stderr[-800:])
                return 1
        legacy_sql = """
            CREATE TABLE core.account_lifecycle_tombstone (
              account_id text PRIMARY KEY,
              reason text NOT NULL,
              tombstoned_at timestamptz DEFAULT now() NOT NULL,
              CONSTRAINT account_tombstone_reason_check
                CHECK (reason = ANY (ARRAY['uninstall_purge','gdpr_erase']))
            );
            ALTER TABLE core.account_lifecycle_tombstone OWNER TO veripsa_migrator;
            INSERT INTO core.account_lifecycle_tombstone(account_id,reason)
              VALUES ('ACCT-GH-rolling-erased','gdpr_erase');
        """
        lr = subprocess.run(
            ["psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q"],
            input=legacy_sql, capture_output=True, text=True,
        )
        if lr.returncode != 0:
            print("rolling legacy tombstone fixture FAILED:", lr.stderr[-800:])
            return 1

        gate_apply = subprocess.run(
            ["psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-f",
             os.path.join(ROOT, "db", "schema", "30_gate.sql")],
            capture_output=True, text=True,
        )
        checks.append((f"rolling prefix: 30_gate.sql applies over the legacy tombstone (rc={gate_apply.returncode})",
                       gate_apply.returncode == 0))
        route_rc, _ = _psql_query(
            app_dsn, "SELECT core.enter_installation_with_authority('rolling-erased')")
        state_rc, state = _psql_query(
            owner_dsn,
            "SELECT (SELECT count(*) FROM core.account_lifecycle_tombstone "
            "WHERE account_id='ACCT-GH-rolling-erased' AND reason='gdpr_erase') || '|' || "
            "(SELECT count(*) FROM core.account WHERE account_id='ACCT-GH-rolling-erased') || '|' || "
            "(SELECT count(*) FROM core.installation_account WHERE account_id='ACCT-GH-rolling-erased')",
        )
        checks.append((f"rolling prefix: old-worker provisioning call is accepted but cannot resurrect "
                       f"the erased tenant (route_rc={route_rc}, state={state!r})",
                       route_rc == 0 and state_rc == 0 and state == "1|0|0"))

        # Generation 13 models an earlier manifest-aware image. The current generation must run the full
        # additive apply once (this intentionally does not take the generation-1 adoption path). This isolated
        # Prefix is built from the current module rather than a predecessor snapshot, so use an older marker
        # without misrepresenting its fresh fail-closed /4 as an interrupted publication.
        _set_marker(owner_dsn, "veripsa-schema/v1/13/" + "0" * 64 + "/applied")
        rc, log = _run_script({"OWNER_DSN": owner_dsn})
        checks.append((f"missing-role upgrade: full predeploy exits 0 without veripsa_backup (rc={rc})",
                       rc == 0 and "schema apply OK" in log and f"/{G}/" in _marker(owner_dsn)))
        role_rc, role_exists = _psql_query(
            admin, "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='veripsa_backup')")
        privilege_rc, privilege_state = _psql_query(
            owner_dsn,
            "SELECT has_function_privilege('veripsa_app',"
            "'core.export_durable_rows_with_authority(text)','EXECUTE') || '|' || "
            "has_function_privilege('veripsa_reader',"
            "'core.export_durable_rows_with_authority(text)','EXECUTE')",
        )
        final_rc, final_state = _psql_query(
            owner_dsn,
            "SELECT (SELECT count(*) FROM core.account_lifecycle_tombstone "
            "WHERE account_id='ACCT-GH-rolling-erased' AND reason='gdpr_erase') || '|' || "
            "(SELECT count(*) FROM core.account WHERE account_id='ACCT-GH-rolling-erased') || '|' || "
            "(SELECT count(*) FROM core.installation_account WHERE account_id='ACCT-GH-rolling-erased')",
        )
        checks.append((f"missing-role upgrade: role remains operator-provisioned and export is not broadened "
                       f"(role={role_exists!r}, privileges={privilege_state!r})",
                       role_rc == 0 and role_exists == "f" and privilege_rc == 0
                       and privilege_state == "false|false"))
        checks.append((f"full predeploy still preserves the rolling erasure fence (state={final_state!r})",
                       final_rc == 0 and final_state == "1|0|0"))
    finally:
        subprocess.run(["dropdb", ROLLING_DB], capture_output=True)

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("ROLLING PREDEPLOY UPGRADE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def _private_outer_backup_role_oid() -> int | None:
    """Return the backup-role OID only when ADMIN_DSN is the private outer cluster."""
    pg_host = os.environ.get("PGHOST", "")
    admin = os.environ.get("ADMIN_DSN", "")
    if not pg_host or not admin:
        return None
    socket_dir = Path(pg_host)
    if not (
        socket_dir.is_absolute()
        and socket_dir.name == "sock"
        and socket_dir.parent.name.startswith("veripsa-eph-pg.")
        and socket_dir.is_dir()
    ):
        return None

    # PGHOST and ADMIN_DSN are independent libpq inputs. Trust the private
    # fixture only after the server reached through ADMIN_DSN identifies the
    # exact sibling data directory owned by _ephemeral_pg.sh.
    data_rc, server_data_dir = _psql_query(admin, "SHOW data_directory")
    if data_rc != 0 or not server_data_dir or "\n" in server_data_dir:
        return None
    try:
        expected_data_dir = (socket_dir.resolve(strict=True).parent / "data").resolve(strict=True)
        actual_data_dir = Path(server_data_dir).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if actual_data_dir != expected_data_dir:
        return None

    role_rc, role_row = _psql_query(
        admin,
        "SELECT oid::text || '|' || rolcanlogin::text "
        "FROM pg_roles WHERE rolname='veripsa_backup'",
    )
    if role_rc != 0:
        return None
    fields = role_row.split("|")
    if len(fields) != 2 or fields[1] != "false":
        return None
    try:
        return int(fields[0])
    except ValueError:
        return None


def _run_rolling_upgrade_in_private_outer(backup_role_oid: int) -> tuple[int, str]:
    """Model a missing new role without starting a second Postgres cluster.

    run_gates.sh already gives this test a cluster no sibling can reach. Rename
    the existing NOLOGIN backup role for the fixture, exercise the exact
    missing-name rollout, then restore the same role OID and all of its grants.
    This avoids exhausting macOS's finite System V shared-memory IDs by nesting
    another postmaster inside the already-private outer postmaster.
    """
    admin = os.environ["ADMIN_DSN"]
    hidden_role = f"veripsa_backup_rolling_{os.getpid()}"
    hide = subprocess.run(
        [
            "psql", admin, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-c",
            f"ALTER ROLE veripsa_backup RENAME TO {hidden_role}",
        ],
        capture_output=True,
        text=True,
    )
    if hide.returncode != 0:
        return 1, "rolling private-role hide FAILED: " + hide.stderr[-800:]

    child_rc = 1
    child_log = ""
    try:
        child = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "--rolling-upgrade-child"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        child_rc = child.returncode
        child_log = child.stdout + child.stderr
    except subprocess.TimeoutExpired as exc:
        child_rc = 124
        timeout_stdout = exc.stdout or ""
        timeout_stderr = exc.stderr or ""
        if isinstance(timeout_stdout, bytes):
            timeout_stdout = timeout_stdout.decode("utf-8", "replace")
        if isinstance(timeout_stderr, bytes):
            timeout_stderr = timeout_stderr.decode("utf-8", "replace")
        child_log = timeout_stdout + timeout_stderr
        child_log += "\nrolling private-role child timed out"
    finally:
        restore = subprocess.run(
            [
                "psql", admin, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-c",
                f"ALTER ROLE {hidden_role} RENAME TO veripsa_backup",
            ],
            capture_output=True,
            text=True,
        )
        if restore.returncode != 0:
            child_rc = 1
            child_log += (
                "\nrolling private-role restore FAILED: "
                + restore.stderr[-800:]
            )
        else:
            verify_rc, restored = _psql_query(
                admin,
                "SELECT EXISTS ("
                "  SELECT 1 FROM pg_roles "
                f"  WHERE rolname='veripsa_backup' AND oid={backup_role_oid} AND NOT rolcanlogin"
                ") AND NOT EXISTS ("
                "  SELECT 1 FROM pg_roles "
                f"  WHERE rolname='{hidden_role}'"
                ")",
            )
            if verify_rc != 0 or restored != "t":
                child_rc = 1
                child_log += (
                    "\nrolling private-role restore verification FAILED: "
                    f"rc={verify_rc} result={restored!r}"
                )
    return child_rc, child_log


def _run_rolling_upgrade_isolated() -> tuple[int, str]:
    backup_role_oid = _private_outer_backup_role_oid()
    if backup_role_oid is not None:
        return _run_rolling_upgrade_in_private_outer(backup_role_oid)

    # A standalone invocation may target a developer's shared postmaster.
    # Never rename a cluster-global role there; retain the nested isolation.
    helper = os.path.join(ROOT, "db", "_ephemeral_pg.sh")
    command = (
        f"source {shlex.quote(helper)}; "
        "ephemeral_pg_start && "
        f"python3 {shlex.quote(__file__)} --rolling-upgrade-child"
    )
    r = subprocess.run(["bash", "-c", command], cwd=ROOT, capture_output=True, text=True, timeout=180)
    return r.returncode, r.stdout + r.stderr


def main() -> int:
    checks = []
    rolling_failure_log = ""
    extra_claim_role = f"veripsa_cutover_extra_{os.getpid()}"

    # bootstrap roles (idempotent, harmless if already present)
    br = subprocess.run(
        ["psql", "postgresql://localhost/postgres", "-X", "-v", "ON_ERROR_STOP=1", "-q", "-f", "db/roles.sql"],
        cwd=ROOT, capture_output=True, text=True,
    )
    if br.returncode != 0:
        print("roles bootstrap FAILED:", br.stderr[-800:]); return 1

    # fresh DB owned by veripsa_migrator (the OWNER role)
    subprocess.run(["dropdb", DB], capture_output=True)
    cr = subprocess.run(["createdb", DB, "-O", "veripsa_migrator"], capture_output=True, text=True)
    if cr.returncode != 0:
        print("createdb FAILED:", cr.stderr); return 1
    subprocess.run(["dropdb", BAD_CONTRACT_DB], capture_output=True)
    bad_cr = subprocess.run(["createdb", BAD_CONTRACT_DB, "-O", "veripsa_migrator"],
                            capture_output=True, text=True)
    if bad_cr.returncode != 0:
        subprocess.run(["dropdb", DB], capture_output=True)
        print("bad-contract createdb FAILED:", bad_cr.stderr); return 1

    try:
        owner_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
        app_dsn = f"postgresql://veripsa_app@localhost/{DB}"

        # --- (1) FRESH DATABASE -----------------------------------------------------------------------------
        rc1, log1 = _run_script({"OWNER_DSN": owner_dsn})
        checks.append((f"happy path: predeploy_schema.sh exits 0 (rc={rc1})", rc1 == 0))
        checks.append(("happy path: log says 'schema apply OK'", "schema apply OK" in log1))
        # verify schema actually landed
        q_rc, q_out = _psql_query(owner_dsn, "SELECT to_regclass('core.event') IS NOT NULL")
        checks.append((f"happy path: core.event table exists after apply (regclass result: {q_out!r})", q_rc == 0 and q_out == "t"))
        # verify the gate functions are callable (their existence is the real proof that schema.sql ran)
        q_rc, q_out = _psql_query(owner_dsn, "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON p.pronamespace=n.oid WHERE n.nspname='core' AND p.proname='resolve_session_identity'")
        checks.append((f"happy path: core.resolve_session_identity() exists (count={q_out})", q_rc == 0 and q_out.isdigit() and int(q_out) >= 1))
        applied_marker = _marker(owner_dsn)
        checks.append((
            f"fresh apply stamps a content-free generation-{G} applied marker ({applied_marker!r})",
            applied_marker.startswith(f"veripsa-schema/v1/{G}/")
            and applied_marker.endswith("/applied")
            and len(applied_marker.split("/")[3]) == 64,
        ))
        fresh_claim_rc, fresh_claim_is_fenced = _psql_query(
            owner_dsn,
            "SELECT prosrc LIKE '%NULL::text%' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        checks.append((
            "fresh apply creates the fail-closed /4 compatibility overload",
            fresh_claim_rc == 0 and fresh_claim_is_fenced == "t",
        ))

        # --- (2) SAME GENERATION + DIGEST: ZERO FULL-SCHEMA DDL --------------------------------------------
        fake_dir, fake_path = _fake_psql(exit_code=91)
        try:
            rc2, log2 = _run_script({"OWNER_DSN": owner_dsn, "PATH": fake_path})
        finally:
            shutil.rmtree(fake_dir)
        checks.append((
            f"current manifest: re-run exits 0 without invoking the deliberately failing psql (rc={rc2})",
            rc2 == 0 and "DDL skipped" in log2 and _marker(owner_dsn) == applied_marker,
        ))

        # --- (3) UNMARKED EXISTING DB AT GENERATION > 1: FULL IDEMPOTENT APPLY (DR / legacy bring-current) --
        # Generation-1 adoption (contract-gated, DDL-skipped) is the one-time pre-marker bootstrap; its pure
        # decision (unmarked + generation 1 -> adopt_existing) is unit-tested in test_schema_generation_manifest.
        # Past generation 1 an UNMARKED existing core schema is a DR restore or a legacy DB BEHIND current: it must
        # APPLY the current schema idempotently (apply_upgrade) and stamp an APPLIED marker at the current
        # generation -- never fail closed (which would strand DR), never skip DDL.
        assert G > 1, (
            f"this E2E scenario assumes the repo schema generation is > 1 (got {G}); "
            "generation-1 adoption is covered by the manifest unit test")
        _set_marker(owner_dsn, None)
        unmarked_rc, unmarked_log = _run_script({"OWNER_DSN": owner_dsn})
        unmarked_marker = _marker(owner_dsn)
        unmarked_claim_rc, unmarked_claim_is_fenced = _psql_query(
            owner_dsn,
            "SELECT prosrc LIKE '%NULL::text%' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        checks.append((
            f"unmarked existing DB at generation {G} applies the full schema and stamps an applied marker "
            f"(DR/legacy bring-current; rc={unmarked_rc}, marker={unmarked_marker!r})",
            unmarked_rc == 0
            and "schema apply OK" in unmarked_log
            and unmarked_marker.startswith(f"veripsa-schema/v1/{G}/")
            and unmarked_marker.endswith("/applied")
            and unmarked_claim_rc == 0
            and unmarked_claim_is_fenced == "t",
        ))

        # The remaining older-marker state-machine checks use the public synthetic predecessor's complete /4
        # body and catalog ABI.
        operational_fixture_rc, _ = _install_synthetic_predecessor_claim(owner_dsn)
        if operational_fixture_rc != 0:
            raise RuntimeError("could not install synthetic predecessor /4 fixture")

        # --- (4) MARKER STATE MACHINE + FAILURE DOES NOT ADVANCE ------------------------------------------
        wrong_digest = f"veripsa-schema/v1/{G}/" + "f" * 64 + "/applied"
        _set_marker(owner_dsn, wrong_digest)
        wrong_rc, wrong_log = _run_script({"OWNER_DSN": owner_dsn})
        checks.append((
            "same generation with a different digest fails closed and preserves the marker",
            wrong_rc != 0
            and "fail_same_generation_digest" in wrong_log
            and _marker(owner_dsn) == wrong_digest,
        ))

        _set_marker(owner_dsn, "malformed-schema-marker")
        malformed_rc, malformed_log = _run_script({"OWNER_DSN": owner_dsn})
        checks.append((
            "malformed marker fails closed without DDL or marker mutation",
            malformed_rc != 0
            and "fail_malformed" in malformed_log
            and _marker(owner_dsn) == "malformed-schema-marker",
        ))

        newer_marker = f"veripsa-schema/v1/{G + 1}/" + "e" * 64 + "/applied"
        _set_marker(owner_dsn, newer_marker)
        fake_dir, fake_path = _fake_psql(exit_code=93)
        try:
            newer_rc, newer_log = _run_script({"OWNER_DSN": owner_dsn, "PATH": fake_path})
        finally:
            shutil.rmtree(fake_dir)
        checks.append((
            "newer live generation makes an older rollback image skip without rewriting the marker",
            newer_rc == 0
            and "newer than this rollback image" in newer_log
            and _marker(owner_dsn) == newer_marker,
        ))

        older_marker = "veripsa-schema/v1/0/" + "d" * 64 + "/applied"
        _set_marker(owner_dsn, older_marker)
        fake_dir, fake_path = _fake_psql(exit_code=47)
        try:
            failed_apply_rc, failed_apply_log = _run_script({"OWNER_DSN": owner_dsn, "PATH": fake_path})
        finally:
            shutil.rmtree(fake_dir)
        checks.append((
            "failed older-generation apply returns non-zero and never advances the live marker",
            failed_apply_rc == 47
            and "live marker not advanced" in failed_apply_log
            and _marker(owner_dsn) == older_marker,
        ))

        # Two simultaneous deploys both observe generation zero, but the session advisory lock lets only the
        # first invoke psql. The second re-reads generation one after acquiring the lock and skips.
        real_psql = shutil.which("psql")
        call_log = tempfile.mktemp(prefix="veripsa-schema-psql-calls-")
        fake_dir, delegated_path = _fake_psql(
            exit_code=0,
            delegate=real_psql,
            call_log=call_log,
            delay_seconds=1,
        )
        concurrent_env = os.environ.copy()
        concurrent_env.update({
            "OWNER_DSN": owner_dsn,
            "VERIPSA_REPO_ROOT": ROOT,
            "PATH": delegated_path,
        })
        first = subprocess.Popen(["bash", SCRIPT], env=concurrent_env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True)
        second = subprocess.Popen(["bash", SCRIPT], env=concurrent_env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True)
        first_log, _ = first.communicate(timeout=120)
        second_log, _ = second.communicate(timeout=120)
        try:
            calls = Path(call_log).read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            calls = []
        shutil.rmtree(fake_dir)
        try:
            os.unlink(call_log)
        except FileNotFoundError:
            pass
        concurrent_marker = _marker(owner_dsn)
        checks.append((
            "session advisory lock makes an older generation apply exactly once across duplicate deploys",
            first.returncode == 0
            and second.returncode == 0
            and calls == ["called"]
            and concurrent_marker.startswith(f"veripsa-schema/v1/{G}/")
            and concurrent_marker.endswith("/applied")
            and sum("schema apply OK" in log for log in (first_log, second_log)) == 1
            and sum("DDL skipped" in log for log in (first_log, second_log)) == 1,
        ))

        # --- (5) CANCELED CONCURRENT INDEX BUILD MUST BE SELF-HEALING ON RETRY ------------------------------
        invalid_created, invalid_detail = _leave_invalid_graph_index(owner_dsn)
        invalid_marker = "veripsa-schema/v1/12/" + "b" * 64 + "/applied"
        _set_marker(owner_dsn, invalid_marker)
        invalid_retry_rc, invalid_retry_log = _run_script({"OWNER_DSN": owner_dsn})
        invalid_final_rc, invalid_final_state = _psql_query(
            owner_dsn,
            "SELECT i.indisvalid::text || '|' || i.indisready::text "
            "FROM pg_index i "
            "WHERE i.indexrelid='core.code_node_coord_uncertain'::regclass",
        )
        checks.append((
            "concurrent-index retry: canceled real build leaves an invalid shell and the next pre-deploy "
            f"rebuilds it valid/ready ({invalid_detail}; retry_rc={invalid_retry_rc})",
            invalid_created
            and invalid_retry_rc == 0
            and "schema apply OK" in invalid_retry_log
            and invalid_final_rc == 0
            and invalid_final_state == "true|true"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # The policy outbox's target identity index is the deliberately narrow UNIQUE exception. During this
        # expansion, the legacy account-only primary key remains attached, so removing only this exact invalid,
        # unattached shell cannot open a uniqueness gap. Retry must rebuild it and atomically promote it to the
        # final stable-coordinate primary key.
        policy_identity_created, policy_identity_detail = (
            _leave_invalid_policy_identity_index(owner_dsn)
        )
        policy_identity_marker = (
            "veripsa-schema/v1/12/" + "8" * 64 + "/applied"
        )
        _set_marker(owner_dsn, policy_identity_marker)
        policy_identity_retry_rc, policy_identity_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        policy_identity_final_rc, policy_identity_final = _psql_query(
            owner_dsn,
            "SELECT p.conname || '|' "
            "|| string_agg(a.attname,',' ORDER BY u.ord) || '|' "
            "|| i.indisunique::text || '|' || i.indisvalid::text || '|' "
            "|| i.indisready::text "
            "FROM pg_constraint p "
            "JOIN pg_index i ON i.indexrelid=p.conindid "
            "CROSS JOIN LATERAL unnest(p.conkey) "
            "WITH ORDINALITY AS u(attnum,ord) "
            "JOIN pg_attribute a "
            "ON a.attrelid=p.conrelid AND a.attnum=u.attnum "
            "WHERE p.conrelid='core.policy_refresh_outbox'::regclass "
            "AND p.contype='p' "
            "GROUP BY p.conname,i.indisunique,i.indisvalid,i.indisready",
        )
        checks.append((
            "unique-index retry: the exact policy identity shell self-heals "
            "only behind the retained legacy primary key, then becomes the "
            "final stable-coordinate primary key "
            f"({policy_identity_detail}; retry_rc={policy_identity_retry_rc}, "
            f"final={policy_identity_final!r})",
            policy_identity_created
            and policy_identity_retry_rc == 0
            and "schema apply OK" in policy_identity_retry_log
            and policy_identity_final_rc == 0
            and policy_identity_final
            == (
                "policy_refresh_outbox_identity_uq|"
                "account_id,request_kind,repository_id|true|true|true"
            )
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # An invalid UNIQUE shell is materially different from a non-unique
        # performance index. Automatically dropping it before the replacement
        # starts enforcing uniqueness opens a live race in _place_claim. Keep
        # the exact shell, fail before marker publication, and require an
        # explicit operator decision before retry.
        unique_invalid_created, unique_invalid_detail = (
            _leave_invalid_unique_claim_index(owner_dsn)
        )
        unique_shell_before_rc, unique_shell_before = _psql_query(
            owner_dsn,
            "SELECT c.oid::text || '|' || i.indisvalid::text || '|' "
            "|| i.indisready::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_index i ON i.indexrelid=c.oid "
            "WHERE n.nspname='core' AND c.relname='claim_one_active'",
        )
        unique_invalid_marker = (
            "veripsa-schema/v1/12/" + "7" * 64 + "/applied"
        )
        _set_marker(owner_dsn, unique_invalid_marker)
        unique_invalid_retry_rc, unique_invalid_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        unique_shell_after_rc, unique_shell_after = _psql_query(
            owner_dsn,
            "SELECT c.oid::text || '|' || i.indisvalid::text || '|' "
            "|| i.indisready::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_index i ON i.indexrelid=c.oid "
            "WHERE n.nspname='core' AND c.relname='claim_one_active'",
        )
        checks.append((
            "unique-index retry: an interrupted UNIQUE shell fails closed, "
            "is never auto-dropped, and cannot advance the schema marker "
            f"({unique_invalid_detail}; retry_rc={unique_invalid_retry_rc}, "
            f"before={unique_shell_before!r}, after={unique_shell_after!r})",
            unique_invalid_created
            and unique_shell_before_rc == 0
            and unique_shell_after_rc == 0
            and unique_shell_before == unique_shell_after
            and unique_shell_after.endswith("|false|false")
            and unique_invalid_retry_rc != 0
            and "managed online index contract failed: claim_one_active"
            in unique_invalid_retry_log
            and _marker(owner_dsn) == unique_invalid_marker,
        ))
        unique_shell_drop_rc, _ = _psql_query(
            owner_dsn,
            "DROP INDEX CONCURRENTLY core.claim_one_active",
        )
        unique_repair_retry_rc, unique_repair_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        unique_repair_final_rc, unique_repair_final = _psql_query(
            owner_dsn,
            "SELECT i.indisunique::text || '|' || i.indisvalid::text || '|' "
            "|| i.indisready::text "
            "FROM pg_index i "
            "WHERE i.indexrelid='core.claim_one_active'::regclass",
        )
        checks.append((
            "unique-index retry: explicit shell removal lets the exact unique "
            "contract rebuild before marker publication",
            unique_shell_drop_rc == 0
            and unique_repair_retry_rc == 0
            and "schema apply OK" in unique_repair_retry_log
            and unique_repair_final_rc == 0
            and unique_repair_final == "true|true|true"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # Cleanup must inspect both sides of the uniqueness contract. A name
        # whose registry entry is non-unique may still be an actual UNIQUE
        # invalid shell (operator drift or a canceled alternate build).
        # Dropping that shell automatically could remove live enforcement.
        actual_unique_created, actual_unique_detail = (
            _leave_invalid_actual_unique_delivery_index(owner_dsn)
        )
        actual_unique_before_rc, actual_unique_before = _psql_query(
            owner_dsn,
            "SELECT c.oid::text || '|' || i.indisunique::text || '|' "
            "|| i.indisvalid::text || '|' || i.indisready::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_index i ON i.indexrelid=c.oid "
            "WHERE n.nspname='core' "
            "AND c.relname='webhook_delivery_pending'",
        )
        actual_unique_marker = (
            "veripsa-schema/v1/12/" + "6" * 64 + "/applied"
        )
        _set_marker(owner_dsn, actual_unique_marker)
        actual_unique_retry_rc, actual_unique_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        actual_unique_after_rc, actual_unique_after = _psql_query(
            owner_dsn,
            "SELECT c.oid::text || '|' || i.indisunique::text || '|' "
            "|| i.indisvalid::text || '|' || i.indisready::text "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "JOIN pg_index i ON i.indexrelid=c.oid "
            "WHERE n.nspname='core' "
            "AND c.relname='webhook_delivery_pending'",
        )
        checks.append((
            "unique-index retry: actual UNIQUE metadata fails closed even "
            "when the registry expects a non-unique index "
            f"({actual_unique_detail}; retry_rc={actual_unique_retry_rc}, "
            f"before={actual_unique_before!r}, after={actual_unique_after!r})",
            actual_unique_created
            and actual_unique_before_rc == 0
            and actual_unique_after_rc == 0
            and actual_unique_before == actual_unique_after
            and actual_unique_after.endswith("|true|false|false")
            and actual_unique_retry_rc != 0
            and "managed online index contract failed: "
            "webhook_delivery_pending"
            in actual_unique_retry_log
            and _marker(owner_dsn) == actual_unique_marker,
        ))
        actual_unique_drop_rc, _ = _psql_query(
            owner_dsn,
            "DROP INDEX CONCURRENTLY core.webhook_delivery_pending",
        )
        actual_unique_repair_rc, actual_unique_repair_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        actual_unique_final_rc, actual_unique_final = _psql_query(
            owner_dsn,
            "SELECT i.indisunique::text || '|' || i.indisvalid::text || '|' "
            "|| i.indisready::text "
            "FROM pg_index i "
            "WHERE i.indexrelid='core.webhook_delivery_pending'::regclass",
        )
        checks.append((
            "unique-index retry: explicit actual-unique shell removal rebuilds "
            "the exact expected non-unique contract",
            actual_unique_drop_rc == 0
            and actual_unique_repair_rc == 0
            and "schema apply OK" in actual_unique_repair_log
            and actual_unique_final_rc == 0
            and actual_unique_final == "false|true|true"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # A valid same-name index is not necessarily the declared index.
        # IF NOT EXISTS must never let a wrong key/predicate/opclass advance the
        # marker, and repair must not destructively guess that it owns a valid
        # operator object. Fail closed, leave it intact for diagnosis, then
        # prove an explicit operator removal lets the next retry self-heal.
        wrong_index_fixture_rc, _ = _psql_query(
            owner_dsn,
            "DROP INDEX CONCURRENTLY core.agent_by_account",
        )
        wrong_index_create_rc, _ = _psql_query(
            owner_dsn,
            "CREATE INDEX CONCURRENTLY agent_by_account "
            "ON core.agent(created_at)",
        )
        wrong_index_marker = (
            f"veripsa-schema/v1/{max(19,G - 1)}/"
            + "e" * 64
            + "/applied"
        )
        _set_marker(owner_dsn, wrong_index_marker)
        wrong_index_rc, wrong_index_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        wrong_index_after_rc, wrong_index_after = _psql_query(
            owner_dsn,
            "SELECT substring(pg_get_indexdef("
            "'core.agent_by_account'::regclass) FROM 'USING .*$')",
        )
        checks.append((
            "online-index contract: valid same-name wrong definition fails "
            "before marker publication and is not destructively replaced "
            f"(fixture={wrong_index_fixture_rc}/{wrong_index_create_rc}, "
            f"deploy_rc={wrong_index_rc}, definition={wrong_index_after!r})",
            wrong_index_fixture_rc == 0
            and wrong_index_create_rc == 0
            and wrong_index_rc != 0
            and "managed online index contract failed: agent_by_account"
            in wrong_index_log
            and wrong_index_after_rc == 0
            and wrong_index_after == "USING btree (created_at)"
            and _marker(owner_dsn) == wrong_index_marker,
        ))
        wrong_index_drop_rc, _ = _psql_query(
            owner_dsn,
            "DROP INDEX CONCURRENTLY core.agent_by_account",
        )
        wrong_index_retry_rc, wrong_index_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn}
        )
        wrong_index_final_rc, wrong_index_final = _psql_query(
            owner_dsn,
            "SELECT substring(pg_get_indexdef("
            "'core.agent_by_account'::regclass) FROM 'USING .*$')",
        )
        checks.append((
            "online-index contract: explicit wrong-index removal lets retry "
            "rebuild the exact declared definition",
            wrong_index_drop_rc == 0
            and wrong_index_retry_rc == 0
            and "schema apply OK" in wrong_index_retry_log
            and wrong_index_final_rc == 0
            and wrong_index_final == "USING btree (account_id)"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        stranded_claim_sql = _exact_current_safe_claim_sql() + """
          INSERT INTO core.webhook_delivery(delivery_key,event_type,payload)
            VALUES ('partial-fence-recovery','push','{}'::jsonb)
          ON CONFLICT (delivery_key) DO UPDATE
            SET status='queued',attempts=0,lease_generation=0,locked_at=NULL,
                owner_instance=NULL,retry_window_expires_at=NULL;
        """
        stranded_fixture_rc, _ = _psql_query(owner_dsn, stranded_claim_sql)
        stranded_marker = "veripsa-schema/v1/12/" + "c" * 64 + "/applied"
        _set_marker(owner_dsn, stranded_marker)
        stranded_acl_before_rc, stranded_acl_before = _psql_query(
            owner_dsn,
            "SELECT "
            "has_function_privilege('veripsa_app',"
            "'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_writer',"
            "'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)',"
            "'EXECUTE')::text",
        )
        stranded_retry_rc, stranded_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn})
        stranded_body_rc, stranded_body = _psql_query(
            owner_dsn,
            "SELECT (prosrc LIKE '%NULL::text%')::text "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        stranded_row_rc, stranded_row = _psql_query(
            owner_dsn,
            "SELECT status || '|' || attempts::text || '|' "
            "|| lease_generation::text "
            "FROM core.webhook_delivery "
            "WHERE delivery_key='partial-fence-recovery'",
        )
        stranded_acl_after_rc, stranded_acl_after = _psql_query(
            owner_dsn,
            "SELECT "
            "has_function_privilege('veripsa_app',"
            "'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_writer',"
            "'core.claim_webhook_delivery_with_authority(text,integer,integer,integer)',"
            "'EXECUTE')::text",
        )
        _psql_query(
            owner_dsn,
            "DELETE FROM core.webhook_delivery "
            "WHERE delivery_key='partial-fence-recovery'",
        )
        checks.append((
            "partial-publication retry: exact old-marker + NULL-owner /4 fence fails loud without changing "
            f"function, row, or marker (fixture_rc={stranded_fixture_rc}, retry_rc={stranded_retry_rc}, "
            f"body={stranded_body!r}, row={stranded_row!r})",
            stranded_fixture_rc == 0
            and stranded_retry_rc != 0
            and "known partial schema publication detected" in stranded_retry_log
            and stranded_body_rc == 0
            and stranded_body == "true"
            and stranded_row_rc == 0
            and stranded_row == "queued|0|0"
            and stranded_acl_before_rc == 0
            and stranded_acl_after_rc == 0
            and stranded_acl_before == "true|false"
            and stranded_acl_after == stranded_acl_before
            and _marker(owner_dsn) == stranded_marker,
        ))

        # A substring is not a fingerprint. An arbitrary body may mention the
        # text NULL::text; it must not be mislabeled as the exact historical
        # residue. It is still an unknown predecessor ABI, so fail before DDL
        # and preserve both body and marker.
        restore_operational_claim_sql = """
          CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(
              p_key text,
              p_stale_seconds int,
              p_max_attempts int,
              p_protocol int)
          RETURNS jsonb
          LANGUAGE sql
          SECURITY DEFINER
          SET search_path TO 'core','pg_catalog'
          AS $body$
            SELECT jsonb_build_object(
              'claimed',false,
              'reason','operational_predecessor_fixture',
              'note','NULL::text is documentation, not a fence')
          $body$;
          ALTER FUNCTION core.claim_webhook_delivery_with_authority(text,int,int,int)
            OWNER TO veripsa_migrator;
        """
        restored_claim_rc, _ = _psql_query(
            owner_dsn, restore_operational_claim_sql)
        operational_retry_rc, operational_retry_log = _run_script(
            {"OWNER_DSN": owner_dsn})
        operational_body_rc, operational_body_preserved = _psql_query(
            owner_dsn,
            "SELECT prosrc LIKE '%operational_predecessor_fixture%' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        unknown_direct_apply = subprocess.run(
            [
                "psql",
                owner_dsn,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                "schema.sql",
            ],
            cwd=os.path.join(ROOT, "db"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        unknown_direct_body_rc, unknown_direct_body_preserved = _psql_query(
            owner_dsn,
            "SELECT prosrc LIKE '%operational_predecessor_fixture%' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        checks.append((
            "partial-publication preflight distinguishes the exact residue and fails unknown /4 ABIs before DDL",
            restored_claim_rc == 0
            and operational_retry_rc != 0
            and "unknown legacy claim ABI" in operational_retry_log
            and "known partial schema publication detected" not in operational_retry_log
            and operational_body_rc == 0
            and operational_body_preserved == "t"
            and _marker(owner_dsn) == stranded_marker,
        ))
        checks.append((
            "direct schema apply has the same unknown-/4 non-destructive backstop",
            unknown_direct_apply.returncode != 0
            and "unsupported existing durable webhook /4 claim ABI"
            in (unknown_direct_apply.stdout + unknown_direct_apply.stderr)
            and unknown_direct_body_rc == 0
            and unknown_direct_body_preserved == "t"
            and _marker(owner_dsn) == stranded_marker,
        ))

        # --- (6) LEGACY FUNCTION INPUT ABI: OLD GENERATION MUST BE CREATE-OR-REPLACE COMPATIBLE ------------
        # PostgreSQL preserves input parameter names as part of a function's public identity even though
        # regprocedure displays only its argument types. Renaming generation 12's third input from
        # p_grace_seconds to p_safe_seconds made the real 12 -> 18 production pre-deploy fail before the new
        # worker could boot. Rebuild that exact published ABI, then prove the current full schema replaces it.
        legacy_reaper_sql = """
          DROP FUNCTION core.reap_dead_instance_leases_with_authority(int,int,int);
          CREATE FUNCTION core.reap_dead_instance_leases_with_authority(
              p_dead_seconds int DEFAULT 15,
              p_stale_seconds int DEFAULT 1800,
              p_grace_seconds int DEFAULT 60)
          RETURNS int
          LANGUAGE sql
          SECURITY DEFINER
          SET search_path TO 'core','pg_catalog'
          AS 'SELECT 0';
          ALTER FUNCTION core.reap_dead_instance_leases_with_authority(int,int,int)
            OWNER TO veripsa_migrator;
        """
        legacy_reaper_rc, _ = _psql_query(owner_dsn, legacy_reaper_sql)
        legacy_claim_restore_rc, _ = _install_synthetic_predecessor_claim(owner_dsn)
        legacy_reaper_marker = "veripsa-schema/v1/12/" + "a" * 64 + "/applied"
        _set_marker(owner_dsn, legacy_reaper_marker)
        legacy_upgrade_rc, legacy_upgrade_log = _run_script({"OWNER_DSN": owner_dsn})
        legacy_args_rc, legacy_args = _psql_query(
            owner_dsn,
            "SELECT pg_get_function_arguments("
            "'core.reap_dead_instance_leases_with_authority(int,int,int)'::regprocedure)",
        )
        legacy_body_rc, legacy_body_current = _psql_query(
            owner_dsn,
            "SELECT prosrc LIKE '%ambiguity_expired%' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='reap_dead_instance_leases_with_authority' "
            "AND p.pronargs=3",
        )
        checks.append((
            "legacy function ABI: the synthetic predecessor p_grace_seconds is replace-compatible with the current body "
            f"(fixture_rc={legacy_reaper_rc}, deploy_rc={legacy_upgrade_rc}, args={legacy_args!r})",
            legacy_reaper_rc == 0
            and legacy_claim_restore_rc == 0
            and legacy_upgrade_rc == 0
            and "schema apply OK" in legacy_upgrade_log
            and legacy_args_rc == 0
            and "p_grace_seconds integer DEFAULT 100" in legacy_args
            and "p_safe_seconds" not in legacy_args
            and legacy_body_rc == 0
            and legacy_body_current == "t"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # --- (7) EXPAND FIRST; EXPLICIT CONTRACT ONLY AFTER RUNTIME READINESS ------------------------------
        # Generic pre-deploy runs before the target worker exists, so it must
        # preserve the serving /4 body. The explicit one-off replays only module
        # 100 after exact worker+web readiness. Its claim fences, direct graph
        # ACL revocations, durable function comments, and same-value manifest
        # stamp are one transaction.
        legacy_claim_rc, _ = _install_synthetic_predecessor_claim(owner_dsn)
        atomic_marker = "veripsa-schema/v1/12/" + "9" * 64 + "/applied"
        _set_marker(owner_dsn, atomic_marker)
        direct_apply = subprocess.run(
            ["psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-f", "schema.sql"],
            cwd=os.path.join(ROOT, "db"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        direct_claim_rc, direct_claim_is_legacy = _psql_query(
            owner_dsn,
            f"SELECT md5(prosrc)='{SYNTHETIC_PREDECESSOR_CLAIM_V4_MD5}' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        marker_after_direct_apply = _marker(owner_dsn)
        expand_rc, expand_log = _run_script({"OWNER_DSN": owner_dsn})
        expanded_marker = _marker(owner_dsn)
        expanded_claim_rc, expanded_claim_is_legacy = _psql_query(
            owner_dsn,
            f"SELECT md5(prosrc)='{SYNTHETIC_PREDECESSOR_CLAIM_V4_MD5}' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        sys.path.insert(0, os.path.join(ROOT, "github-app"))
        import schema_contract_cutover as schema_cutover
        import psycopg2
        state_conn = psycopg2.connect(owner_dsn)
        try:
            state_conn.autocommit = True
            with state_conn.cursor() as state_cur:
                finalizer_pre_state = schema_cutover._catalog_state(state_cur)
        finally:
            state_conn.close()
        checks.append((
            "promotion-safe expand: variable-free and generic manifest applies "
            "preserve the old /4 drainer until the explicit readiness cutover",
            legacy_claim_rc == 0
            and direct_apply.returncode == 0
            and direct_claim_rc == 0
            and direct_claim_is_legacy == "t"
            and marker_after_direct_apply == atomic_marker
            and expand_rc == 0
            and "schema apply OK" in expand_log
            and expanded_marker.startswith(f"veripsa-schema/v1/{G}/")
            and expanded_claim_rc == 0
            and expanded_claim_is_legacy == "t"
            and finalizer_pre_state == "pre_cutover",
        ))

        # Claim one webhook and one policy sentinel immediately before the
        # contract. Their exact terminal ABIs must remain valid afterwards.
        inflight_delivery_rc, _ = _psql_query(
            owner_dsn,
            "INSERT INTO core.webhook_delivery(delivery_key,event_type,payload) "
            "VALUES ('cutover-inflight','push','{}'::jsonb) "
            "ON CONFLICT (delivery_key) DO UPDATE SET status='queued',"
            "attempts=0,lease_generation=0,locked_at=NULL,done_at=NULL,"
            "owner_instance=NULL,retry_window_expires_at=NULL",
        )
        inflight_claim_rc, inflight_claim = _psql_query(
            app_dsn,
            "SELECT COALESCE(("
            "core.claim_webhook_delivery_with_authority("
            "'cutover-inflight',1800,8,2)->>'claimed')::boolean,false)",
        )
        _psql_query(owner_dsn, "TRUNCATE core.graph_convergence_lease,core.policy_refresh_outbox")
        policy_account_rc, policy_account = _psql_query(
            app_dsn,
            "SELECT core.enter_installation_with_authority('987654321')",
        )
        policy_enqueue_rc, _ = _psql_query(
            owner_dsn,
            "SELECT set_config('core.current_account',"
            f"'{policy_account}',true); "
            f"SELECT core._enqueue_policy_refresh('{policy_account}')",
        )
        policy_claim_rc, policy_claim = _psql_query(
            app_dsn,
            "SELECT COALESCE(c->>'account_id','') || '|' "
            "|| COALESCE(c->>'policy_epoch','') "
            "FROM (SELECT core.claim_policy_refresh_with_authority("
            "'cutover-old-web',5,300,8) AS c) q",
        )

        admin_dsn = f"postgresql://localhost/{DB}"
        extra_role_rc, _ = _psql_query(
            admin_dsn,
            f"CREATE ROLE {extra_claim_role} NOLOGIN",
        )
        poison_acl_rc, _ = _psql_query(
            admin_dsn,
            "GRANT EXECUTE ON FUNCTION "
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer),"
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer,boolean),"
            "core.ingest_graph_with_authority("
            "jsonb,text,text,text,timestamptz),"
            "core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz) "
            f"TO {extra_claim_role}; "
            "GRANT EXECUTE ON FUNCTION "
            "core.claim_webhook_delivery_with_authority("
            "text,integer,integer,integer),"
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer),"
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer,boolean),"
            "core.ingest_graph_with_authority("
            "jsonb,text,text,text,timestamptz),"
            "core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz) "
            "TO veripsa_reader; "
            "GRANT EXECUTE ON FUNCTION "
            "core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz) "
            "TO veripsa_migrator WITH GRANT OPTION; "
            "GRANT EXECUTE ON FUNCTION "
            "core.claim_webhook_delivery_with_authority("
            "text,integer,integer,integer) "
            "TO veripsa_app WITH GRANT OPTION; "
            "SET ROLE veripsa_app; "
            "GRANT EXECUTE ON FUNCTION "
            "core.claim_webhook_delivery_with_authority("
            "text,integer,integer,integer) "
            f"TO {extra_claim_role}; "
            "RESET ROLE",
        )
        poisoned_acl_rc, poisoned_acl = _legacy_claim_acl_shape(
            owner_dsn, extra_claim_role)
        poisoned_graph_acl_rc, poisoned_graph_acl = _direct_graph_acl_shape(
            owner_dsn, extra_claim_role)
        fail_cutover_sql = r"""
          CREATE OR REPLACE FUNCTION public.veripsa_fail_schema_cutover_marker()
          RETURNS event_trigger
          LANGUAGE plpgsql
          SECURITY DEFINER
          SET search_path TO 'pg_catalog','public'
          AS $fail$
          BEGIN
            IF current_setting('application_name',true)='veripsa_atomic_cutover_gate'
               AND current_query() ILIKE '%COMMENT ON SCHEMA core%' THEN
              RAISE EXCEPTION 'injected final schema cutover marker failure';
            END IF;
          END
          $fail$;
          CREATE EVENT TRIGGER veripsa_fail_schema_cutover_marker
            ON ddl_command_start
            WHEN TAG IN ('COMMENT')
            EXECUTE FUNCTION public.veripsa_fail_schema_cutover_marker();
        """
        trigger_rc, _ = _psql_query(admin_dsn, fail_cutover_sql)
        cutover_dsn = owner_dsn + "?application_name=veripsa_atomic_cutover_gate"
        failed_cutover_rc = -1
        failed_cutover_log = ""
        try:
            failed_cutover_rc, failed_cutover_log = _run_contract_cutover(
                cutover_dsn, expanded_marker)
        finally:
            _psql_query(
                admin_dsn,
                "DROP EVENT TRIGGER IF EXISTS veripsa_fail_schema_cutover_marker; "
                "DROP FUNCTION IF EXISTS public.veripsa_fail_schema_cutover_marker()",
            )
        failed_claim_rc, failed_claim_is_legacy = _psql_query(
            owner_dsn,
            f"SELECT md5(prosrc)='{SYNTHETIC_PREDECESSOR_CLAIM_V4_MD5}' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        failed_acl_rc, failed_acl = _legacy_claim_acl_shape(
            owner_dsn, extra_claim_role)
        failed_graph_acl_rc, failed_graph_acl = _direct_graph_acl_shape(
            owner_dsn, extra_claim_role)
        marker_after_failed_cutover = _marker(owner_dsn)
        retry_cutover_rc, retry_cutover_log = _run_contract_cutover(
            cutover_dsn, expanded_marker)
        final_claim_rc, final_claim_is_safe = _psql_query(
            owner_dsn,
            "SELECT md5(prosrc)='877a1f799a016bcd42a7a57b61750c25' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        checks.append((
            "readiness-gated contract: injected final marker failure preserves "
            "the old /4 body, exact already-live generation, and every "
            "pre-transaction claim/graph ACL including a grantable owner row "
            f"(fixture_rc={legacy_claim_rc}, trigger_rc={trigger_rc}, deploy_rc={failed_cutover_rc})",
            extra_role_rc == 0
            and poison_acl_rc == 0
            and poisoned_acl_rc == 0
            and poisoned_acl == "6|3|2|1|12"
            and poisoned_graph_acl_rc == 0
            and poisoned_graph_acl.split("|")[1:] == ["2", "1", "8"]
            and trigger_rc == 0
            and failed_cutover_rc != 0
            and "injected final schema cutover marker failure" in failed_cutover_log
            and marker_after_failed_cutover == expanded_marker
            and failed_claim_rc == 0
            and failed_claim_is_legacy == "t"
            and failed_acl_rc == 0
            and failed_acl == poisoned_acl
            and failed_graph_acl_rc == 0
            and failed_graph_acl == poisoned_graph_acl,
        ))

        post_claim_shape_rc, post_claim_shape = _psql_query(
            owner_dsn,
            "INSERT INTO core.webhook_delivery(delivery_key,event_type,payload) "
            "VALUES ('cutover-blocked','push','{}'::jsonb) "
            "ON CONFLICT (delivery_key) DO UPDATE SET status='queued',attempts=0,"
            "lease_generation=0,locked_at=NULL,done_at=NULL,owner_instance=NULL,"
            "retry_window_expires_at=NULL; "
            "SELECT (r->>'claimed') || '|' || (r->>'reason') || '|' || "
            "(SELECT status || '|' || attempts::text || '|' "
            "|| lease_generation::text FROM core.webhook_delivery "
            "WHERE delivery_key='cutover-blocked') "
            "FROM (SELECT core.claim_webhook_delivery_with_authority("
            "'cutover-blocked',1800,8,2) AS r) q",
        )
        policy_cutoff_rc, policy_cutoff = _psql_query(
            app_dsn,
            "SELECT "
            "(core.claim_policy_refresh_with_authority('old4',5,300,8) IS NULL)"
            "::text || '|' || "
            "(core.claim_policy_refresh_with_authority("
            "'old5',5,300,8,true) IS NULL)::text",
        )
        graph_acl_rc, graph_acl = _psql_query(
            owner_dsn,
            "SELECT "
            "has_function_privilege('veripsa_writer',"
            "'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_app',"
            "'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_writer',"
            "'core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz)','EXECUTE')::text "
            "|| '|' || has_function_privilege('veripsa_app',"
            "'core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz)','EXECUTE')::text",
        )
        final_claim_acl_rc, final_claim_acl = _legacy_claim_acl_shape(
            owner_dsn, extra_claim_role)
        final_direct_graph_acl_rc, final_direct_graph_acl = (
            _direct_graph_acl_shape(owner_dsn, extra_claim_role)
        )
        state_conn = psycopg2.connect(owner_dsn)
        try:
            state_conn.autocommit = True
            with state_conn.cursor() as state_cur:
                finalizer_post_state = schema_cutover._catalog_state(state_cur)
        finally:
            state_conn.close()
        terminal_webhook_rc, terminal_webhook = _psql_query(
            app_dsn,
            "SELECT core.finish_webhook_delivery_with_authority("
            "'cutover-inflight',1)",
        )
        terminal_policy_rc = -1
        terminal_policy = ""
        if "|" in policy_claim:
            claimed_account, claimed_epoch = policy_claim.split("|", 1)
            terminal_policy_rc, terminal_policy = _psql_query(
                app_dsn,
                "SELECT core.finish_policy_refresh_with_authority("
                f"'{claimed_account}',{int(claimed_epoch)})",
            )
        checks.append((
            "readiness-gated contract: successful transaction makes old web "
            "ingress-only, revokes inherited generic graph writes, and preserves "
            "pre-cutover exact terminal CAS "
            f"(cutover={retry_cutover_rc}, safe={final_claim_is_safe!r}, "
            f"inflight={inflight_claim!r}/{terminal_webhook!r}, "
            f"policy={policy_account!r}/{policy_claim!r}/{terminal_policy!r}, "
            f"blocked={post_claim_shape!r}, policy_cutoff={policy_cutoff!r}, "
            f"graph_acl={graph_acl!r}/{final_direct_graph_acl!r}, "
            f"claim_acl={final_claim_acl!r})",
            retry_cutover_rc == 0
            and final_claim_rc == 0
            and final_claim_is_safe == "t"
            and _marker(owner_dsn) == expanded_marker
            and inflight_delivery_rc == 0
            and inflight_claim_rc == 0
            and inflight_claim == "t"
            and policy_account_rc == 0
            and policy_enqueue_rc == 0
            and policy_claim_rc == 0
            and policy_claim.startswith(policy_account + "|")
            and post_claim_shape_rc == 0
            and post_claim_shape.endswith(
                "false|legacy_budget_unproven|queued|0|0")
            and policy_cutoff_rc == 0
            and policy_cutoff == "true|true"
            and graph_acl_rc == 0
            and graph_acl == "false|false|false|false"
            and final_claim_acl_rc == 0
            and final_claim_acl == "0|0|3|0|6"
            and final_direct_graph_acl_rc == 0
            and final_direct_graph_acl == "0|0|2|2"
            and finalizer_post_state == "cut_over"
            and terminal_webhook_rc == 0
            and terminal_webhook == "t"
            and terminal_policy_rc == 0
            and terminal_policy == "t",
        ))

        post_poison_rc, _ = _psql_query(
            admin_dsn,
            "GRANT EXECUTE ON FUNCTION "
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer,boolean),"
            "core.ingest_graph_with_authority("
            "jsonb,text,text,text,timestamptz) "
            f"TO {extra_claim_role}",
        )
        state_conn = psycopg2.connect(owner_dsn)
        try:
            state_conn.autocommit = True
            with state_conn.cursor() as state_cur:
                poisoned_post_state = schema_cutover._catalog_state(state_cur)
        finally:
            state_conn.close()
        post_poison_acl_rc, post_poison_acl = _legacy_claim_acl_shape(
            owner_dsn, extra_claim_role)
        post_poison_graph_acl_rc, post_poison_graph_acl = (
            _direct_graph_acl_shape(owner_dsn, extra_claim_role)
        )
        post_unpoison_rc, _ = _psql_query(
            admin_dsn,
            "REVOKE ALL PRIVILEGES ON FUNCTION "
            "core.claim_policy_refresh_with_authority("
            "text,integer,integer,integer,boolean),"
            "core.ingest_graph_with_authority("
            "jsonb,text,text,text,timestamptz) "
            f"FROM {extra_claim_role}",
        )
        state_conn = psycopg2.connect(owner_dsn)
        try:
            state_conn.autocommit = True
            with state_conn.cursor() as state_cur:
                restored_post_state = schema_cutover._catalog_state(state_cur)
        finally:
            state_conn.close()
        checks.append((
            "cutover classifier expands the complete proacl: a grant to an "
            "otherwise unqueried role is Unknown, never a false green",
            post_poison_rc == 0
            and post_poison_acl_rc == 0
            and post_poison_acl == "1|1|3|0|7"
            and post_poison_graph_acl_rc == 0
            and post_poison_graph_acl == "1|1|2|3"
            and poisoned_post_state == "unknown"
            and post_unpoison_rc == 0
            and restored_post_state == "cut_over",
        ))

        policy_module_replay = subprocess.run(
            [
                "psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1",
                "-f", "schema/97_policy_refresh.sql",
            ],
            cwd=os.path.join(ROOT, "db"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        policy_module_fence_rc, policy_module_fence = _psql_query(
            app_dsn,
            "SELECT "
            "(core.claim_policy_refresh_with_authority('old4',5,300,8) IS NULL)"
            "::text || '|' || "
            "(core.claim_policy_refresh_with_authority("
            "'old5',5,300,8,true) IS NULL)::text",
        )
        checks.append((
            "durable policy markers make module 97 itself preserve /4 and /5 "
            "NULL fences before final module 100 can run",
            policy_module_replay.returncode == 0
            and policy_module_fence_rc == 0
            and policy_module_fence == "true|true",
        ))

        # A later full schema replay republishes compatibility bodies in modules
        # 25/97 and reaches the generic writer grants in module 30. Durable
        # function comments make 30 keep inherited writer privilege closed,
        # make 97 preserve both NULL claim bodies, and let final module 100
        # verify/restore the complete contract.
        replay_after_cutover = subprocess.run(
            ["psql", owner_dsn, "-X", "-v", "ON_ERROR_STOP=1", "-f", "schema.sql"],
            cwd=os.path.join(ROOT, "db"),
            capture_output=True,
            text=True,
            timeout=120,
        )
        replay_claim_rc, replay_claim = _psql_query(
            owner_dsn,
            "SELECT md5(prosrc)='877a1f799a016bcd42a7a57b61750c25' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        replay_policy_rc, replay_policy = _psql_query(
            app_dsn,
            "SELECT "
            "(core.claim_policy_refresh_with_authority('old4',5,300,8) IS NULL)"
            "::text || '|' || "
            "(core.claim_policy_refresh_with_authority("
            "'old5',5,300,8,true) IS NULL)::text",
        )
        replay_graph_acl_rc, replay_graph_acl = _psql_query(
            owner_dsn,
            "SELECT "
            "has_function_privilege('veripsa_writer',"
            "'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_app',"
            "'core.ingest_graph_with_authority(jsonb,text,text,text,timestamptz)',"
            "'EXECUTE')::text || '|' || "
            "has_function_privilege('veripsa_writer',"
            "'core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz)','EXECUTE')::text "
            "|| '|' || has_function_privilege('veripsa_app',"
            "'core.patch_graph_with_authority("
            "jsonb,text,text,text[],text[],text,timestamptz)','EXECUTE')::text",
        )
        checks.append((
            "durable cutover comments keep legacy claims and inherited generic "
            "graph writes fenced across a complete later schema replay",
            replay_after_cutover.returncode == 0
            and replay_claim_rc == 0
            and replay_claim == "t"
            and replay_policy_rc == 0
            and replay_policy == "true|true"
            and replay_graph_acl_rc == 0
            and replay_graph_acl == "false|false|false|false"
            and _marker(owner_dsn) == expanded_marker,
        ))

        # --- (8) ONLINE REPLAY: NO-OP DDL MUST NOT QUEUE AHEAD OF LIVE WRITERS ------------------------------
        # Production gen19 failed at the first plain CREATE INDEX replay while
        # the predecessor held RowExclusive on core.agent. PostgreSQL also takes
        # AccessExclusive for ADD COLUMN IF NOT EXISTS / same-default /
        # already-not-null replays. A lock_timeout merely shortens that convoy;
        # it does not make the deploy online. Hold the exact live lock class on
        # EVERY core table and prove the complete declarative replay finishes
        # while the holder remains open. This closes both shared cuts across
        # the full current schema, not a hand-picked hot-table sample:
        # catalog-first column ensures and concurrent permanent indexes.
        import psycopg2
        from psycopg2 import sql

        replay_marker = (
            f"veripsa-schema/v1/{max(1,G - 1)}/"
            + "c" * 64
            + "/applied"
        )
        _set_marker(owner_dsn, replay_marker)
        replay_blocker = psycopg2.connect(owner_dsn)
        replay_deploy = None
        replay_dml_rc = -1
        replay_dml_elapsed = 99.0
        replay_elapsed = 99.0
        replay_log = ""
        replay_locked_tables = 0
        replay_sequence_held = False
        try:
            replay_blocker.autocommit = False
            with replay_blocker.cursor() as cur:
                cur.execute(
                    "SELECT n.nspname,c.relname "
                    "FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='core' AND c.relkind IN ('r','p') "
                    "ORDER BY c.oid"
                )
                core_tables = cur.fetchall()
                replay_locked_tables = len(core_tables)
                cur.execute(
                    sql.SQL("LOCK TABLE {} IN ROW EXCLUSIVE MODE").format(
                        sql.SQL(",").join(
                            sql.Identifier(schema_name, table_name)
                            for schema_name, table_name in core_tables
                        )
                    )
                )
                cur.execute("SELECT nextval('core.graph_revision_seq')")
                replay_sequence_held = int(cur.fetchone()[0]) >= 1

            replay_dsn = (
                owner_dsn
                + "?application_name=veripsa_predeploy_online_replay"
            )
            replay_env = os.environ.copy()
            replay_env.update({
                "OWNER_DSN": replay_dsn,
                "VERIPSA_REPO_ROOT": ROOT,
                "PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS": "1500",
                "PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS": "30000",
                "PGOPTIONS": "-c application_name=caller_option_preserved",
            })
            replay_started = time.monotonic()
            replay_deploy = subprocess.Popen(
                ["bash", SCRIPT],
                env=replay_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # Let an unsafe ALTER SEQUENCE OWNER reach the lock queue before
            # probing a later graph writer. With the catalog guard the deploy
            # normally finishes inside this interval.
            time.sleep(0.1)
            replay_app = psycopg2.connect(owner_dsn)
            dml_started = time.monotonic()
            try:
                replay_app.autocommit = True
                with replay_app.cursor() as cur:
                    cur.execute("SET statement_timeout='750ms'")
                    dml_started = time.monotonic()
                    cur.execute("SELECT nextval('core.graph_revision_seq')")
                    cur.execute(
                        "UPDATE core.webhook_delivery "
                        "SET updated_at=updated_at WHERE false"
                    )
                    replay_dml_elapsed = time.monotonic() - dml_started
                    replay_dml_rc = 0
            except Exception:
                replay_dml_elapsed = time.monotonic() - dml_started
                replay_dml_rc = 1
            finally:
                replay_app.close()

            replay_log, _ = replay_deploy.communicate(timeout=35)
            replay_elapsed = time.monotonic() - replay_started
        finally:
            if replay_deploy is not None and replay_deploy.poll() is None:
                replay_deploy.terminate()
                replay_deploy.communicate(timeout=5)
            replay_blocker.rollback()
            replay_blocker.close()

        checks.append((
            "online replay: full pre-deploy completes while live RowExclusive "
            f"holders remain open on all {replay_locked_tables} core tables "
            "and a graph sequence user remains open "
            f"(rc={getattr(replay_deploy, 'returncode', None)}, "
            f"elapsed={replay_elapsed:.3f}s)",
            replay_locked_tables >= 30
            and replay_sequence_held
            and replay_deploy is not None
            and replay_deploy.returncode == 0
            and "schema apply OK" in replay_log
            and replay_elapsed < 35.0,
        ))
        checks.append((
            "online replay: later queue and graph writers are not queued "
            "behind the deploy "
            f"(rc={replay_dml_rc}, elapsed={replay_dml_elapsed:.3f}s)",
            replay_dml_rc == 0 and replay_dml_elapsed < 0.75,
        ))
        checks.append((
            "online replay: successful apply advances the marker only after "
            "the complete replay",
            _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # Drop the exact production-failing index and repeat under a live
        # RowExclusive holder. CIC is allowed to wait for the old writer before
        # its build, but it must not sit in the lock queue ahead of later DML.
        drop_agent_index_rc, _ = _psql_query(
            owner_dsn,
            "DROP INDEX CONCURRENTLY IF EXISTS core.agent_by_account",
        )
        build_marker = (
            f"veripsa-schema/v1/{max(1,G - 1)}/"
            + "d" * 64
            + "/applied"
        )
        _set_marker(owner_dsn, build_marker)
        build_blocker = psycopg2.connect(owner_dsn)
        build_deploy = None
        build_wait_seen = False
        build_dml_rc = -1
        build_dml_elapsed = 99.0
        marker_while_waiting = ""
        build_log = ""
        try:
            build_blocker.autocommit = False
            with build_blocker.cursor() as cur:
                cur.execute("LOCK TABLE core.agent IN ROW EXCLUSIVE MODE")

            build_dsn = (
                owner_dsn
                + "?application_name=veripsa_predeploy_online_index_gate"
            )
            build_env = os.environ.copy()
            build_env.update({
                "OWNER_DSN": build_dsn,
                "VERIPSA_REPO_ROOT": ROOT,
                "PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS": "1500",
                "PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS": "30000",
            })
            build_deploy = subprocess.Popen(
                ["bash", SCRIPT],
                env=build_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            progress_sql = (
                "SELECT EXISTS ("
                "SELECT 1 "
                "FROM pg_stat_progress_create_index p "
                "JOIN pg_class c ON c.oid=p.index_relid "
                "JOIN pg_stat_activity a ON a.pid=p.pid "
                "WHERE c.relname='agent_by_account' "
                "AND a.application_name="
                "'veripsa_predeploy_online_index_gate' "
                "AND p.phase LIKE 'waiting for writers%')"
            )
            progress_deadline = time.monotonic() + 10.0
            while (
                time.monotonic() < progress_deadline
                and build_deploy.poll() is None
            ):
                progress_rc, progress_value = _psql_query(
                    owner_dsn,progress_sql
                )
                if progress_rc == 0 and progress_value == "t":
                    build_wait_seen = True
                    break
                time.sleep(0.03)

            if build_wait_seen:
                marker_while_waiting = _marker(owner_dsn)
                build_app = psycopg2.connect(owner_dsn)
                dml_started = time.monotonic()
                try:
                    build_app.autocommit = True
                    with build_app.cursor() as cur:
                        cur.execute("SET statement_timeout='750ms'")
                        dml_started = time.monotonic()
                        cur.execute(
                            "UPDATE core.agent "
                            "SET created_at=created_at WHERE false"
                        )
                        build_dml_elapsed = time.monotonic() - dml_started
                        build_dml_rc = 0
                except Exception:
                    build_dml_elapsed = time.monotonic() - dml_started
                    build_dml_rc = 1
                finally:
                    build_app.close()
        finally:
            build_blocker.rollback()
            build_blocker.close()
            if build_deploy is not None:
                try:
                    build_log, _ = build_deploy.communicate(timeout=45)
                except subprocess.TimeoutExpired:
                    build_deploy.terminate()
                    build_log, _ = build_deploy.communicate(timeout=5)

        index_contract_rc, index_contract = _psql_query(
            owner_dsn,
            "SELECT tn.nspname || '.' || t.relname || '|' "
            "|| i.indisvalid::text || '|' || i.indisready::text "
            "FROM pg_index i "
            "JOIN pg_class x ON x.oid=i.indexrelid "
            "JOIN pg_class t ON t.oid=i.indrelid "
            "JOIN pg_namespace tn ON tn.oid=t.relnamespace "
            "WHERE x.oid=to_regclass('core.agent_by_account')",
        )
        build_claim_rc, build_claim_is_safe = _psql_query(
            owner_dsn,
            "SELECT md5(prosrc)='877a1f799a016bcd42a7a57b61750c25' "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='core' "
            "AND p.proname='claim_webhook_delivery_with_authority' "
            "AND p.pronargs=4",
        )
        checks.append((
            "online index build: exact agent index waits for the predecessor "
            "writer without queuing later DML "
            f"(wait={build_wait_seen}, dml_rc={build_dml_rc}, "
            f"elapsed={build_dml_elapsed:.3f}s)",
            drop_agent_index_rc == 0
            and build_wait_seen
            and build_dml_rc == 0
            and build_dml_elapsed < 0.75
            and marker_while_waiting == build_marker,
        ))
        checks.append((
            "online index build: holder release completes a valid/ready index, "
            "preserves the durable cutover /4 fence, and only then advances the marker "
            f"(deploy_rc={getattr(build_deploy, 'returncode', None)}, "
            f"index={index_contract!r})",
            build_deploy is not None
            and build_deploy.returncode == 0
            and "schema apply OK" in build_log
            and index_contract_rc == 0
            and index_contract == "core.agent|true|true"
            and build_claim_rc == 0
            and build_claim_is_safe == "t"
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # --- (9) ACQUIRED DDL LOCKS ARE RELEASED BEFORE LONG FUNCTION PUBLICATION --------------------------
        # lock_timeout only limits WAITING for a lock. It does nothing once ALTER TABLE acquires
        # AccessExclusive immediately. A historical BEGIN at the top of modules 25/30 retained that lock through
        # hundreds of later CREATE FUNCTION/ACL statements. Pause inside each real function-publication phase and
        # prove a RowExclusive live-worker operation is not held behind the deploy transaction.
        admin_dsn = f"postgresql://localhost/{DB}"
        trigger_sql = r"""
          CREATE TABLE public.veripsa_schema_publish_delay (
            phase text PRIMARY KEY,
            fired boolean DEFAULT false NOT NULL
          );
          INSERT INTO public.veripsa_schema_publish_delay(phase) VALUES ('25'),('30');
          CREATE OR REPLACE FUNCTION public.veripsa_delay_schema_publication()
          RETURNS event_trigger
          LANGUAGE plpgsql
          SECURITY DEFINER
          SET search_path TO 'pg_catalog','public'
          AS $delay$
          DECLARE v_phase text;
          BEGIN
            IF current_setting('application_name',true) <> 'veripsa_predeploy_expand_gate' THEN
              RETURN;
            END IF;
            IF current_query() ILIKE
                 '%CREATE OR REPLACE FUNCTION core.enqueue_webhook_delivery_with_authority(%' THEN
              v_phase := '25';
            ELSIF current_query() ILIKE
                    '%CREATE OR REPLACE FUNCTION core.installation_is_live(%' THEN
              v_phase := '30';
            ELSE
              RETURN;
            END IF;
            UPDATE public.veripsa_schema_publish_delay
               SET fired=true
             WHERE phase=v_phase AND NOT fired;
            IF FOUND THEN
              PERFORM pg_sleep(4);
            END IF;
          END
          $delay$;
          CREATE EVENT TRIGGER veripsa_delay_schema_publication
            ON ddl_command_start
            WHEN TAG IN ('CREATE FUNCTION')
            EXECUTE FUNCTION public.veripsa_delay_schema_publication();
        """
        trigger_rc, _ = _psql_query(admin_dsn, trigger_sql)
        publish_marker = (
            f"veripsa-schema/v1/{G - 1}/" + "b" * 64 + "/applied"
        )
        _set_marker(owner_dsn, publish_marker)
        publish_deploy = None
        phases_seen: set[str] = set()
        phase_dml: dict[str, tuple[int, float]] = {}
        publish_log = ""
        try:
            publish_dsn = owner_dsn + "?application_name=veripsa_predeploy_expand_gate"
            publish_env = os.environ.copy()
            publish_env.update({
                "OWNER_DSN": publish_dsn,
                "VERIPSA_REPO_ROOT": ROOT,
                "PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS": "1500",
                "PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS": "30000",
            })
            publish_deploy = subprocess.Popen(
                ["bash", SCRIPT],
                env=publish_env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            observe_deadline = time.monotonic() + 90.0
            while (time.monotonic() < observe_deadline
                   and publish_deploy.poll() is None
                   and len(phases_seen) < 2):
                phase_rc, phase = _psql_query(
                    admin_dsn,
                    "SELECT COALESCE((SELECT CASE "
                    "WHEN query ILIKE '%enqueue_webhook_delivery_with_authority%' THEN '25' "
                    "WHEN query ILIKE '%installation_is_live%' THEN '30' ELSE '' END "
                    "FROM pg_stat_activity "
                    "WHERE application_name='veripsa_predeploy_expand_gate' "
                    "AND state='active' AND wait_event='PgSleep' LIMIT 1),'')",
                )
                if phase_rc == 0 and phase in {"25", "30"} and phase not in phases_seen:
                    phases_seen.add(phase)
                    table = (
                        "core.webhook_delivery" if phase == "25"
                        else "core.installation_account"
                    )
                    started = time.monotonic()
                    dml_rc, _ = _psql_query(
                        owner_dsn,
                        "SET statement_timeout='750ms'; "
                        f"UPDATE {table} SET "
                        + ("updated_at=updated_at" if phase == "25"
                           else "bound_at=bound_at")
                        + " WHERE false",
                    )
                    phase_dml[phase] = (dml_rc, time.monotonic() - started)
                time.sleep(0.025)
            if publish_deploy is not None:
                publish_log, _ = publish_deploy.communicate(timeout=90)
        finally:
            if publish_deploy is not None and publish_deploy.poll() is None:
                publish_deploy.terminate()
                publish_deploy.communicate(timeout=5)
            _psql_query(
                admin_dsn,
                "DROP EVENT TRIGGER IF EXISTS veripsa_delay_schema_publication; "
                "DROP FUNCTION IF EXISTS public.veripsa_delay_schema_publication(); "
                "DROP TABLE IF EXISTS public.veripsa_schema_publish_delay",
            )

        checks.append((
            "lock-hold convoy: real modules 25 and 30 both reach a deliberately paused "
            f"function-publication phase (trigger_rc={trigger_rc}, seen={sorted(phases_seen)})",
            trigger_rc == 0 and phases_seen == {"25", "30"},
        ))
        for phase in ("25", "30"):
            dml_rc, elapsed = phase_dml.get(phase, (-1, 99.0))
            checks.append((
                f"lock-hold convoy: module {phase} publication retains no live-table DML lock "
                f"(rc={dml_rc}, elapsed={elapsed:.3f}s)",
                dml_rc == 0 and elapsed < 0.75,
            ))
        checks.append((
            "lock-hold convoy: the delayed real pre-deploy completes and advances the marker",
            publish_deploy is not None
            and publish_deploy.returncode == 0
            and "schema apply OK" in publish_log
            and _marker(owner_dsn).startswith(f"veripsa-schema/v1/{G}/"),
        ))

        # --- (10) ROLLING UPGRADE: old worker + missing new cluster-global role ---------------------------
        rolling_rc, rolling_log = _run_rolling_upgrade_isolated()
        checks.append((f"rolling upgrade: partial 30_gate apply preserves erase fence and full predeploy works "
                       f"without the new backup role (rc={rolling_rc})",
                       rolling_rc == 0 and "ROLLING PREDEPLOY UPGRADE: PASS" in rolling_log))
        if rolling_rc != 0:
            rolling_failure_log = rolling_log[-2400:]

        # --- (11) FAIL-LOUD on a real owner connection failure --------------------------------------------
        rc3, log3 = _run_script({"OWNER_DSN": "postgresql://nope:nope@127.0.0.1:1/nonexistent"})
        checks.append((f"fail-loud: an unreachable OWNER_DSN exits NON-ZERO (rc={rc3}; Render then fails the deploy)", rc3 != 0))
        checks.append(("fail-loud: log says 'schema apply FAILED'", "schema apply FAILED" in log3))

        # --- (12) UNSET-OWNER-DSN DEGRADE PATH ------------------------------------------------------------
        # NOTE: bash inherits parent env unless we explicitly clear OWNER_DSN. subprocess env=... replaces the
        # whole env, so just don't include OWNER_DSN in the overlay AND copy the parent env without it.
        env_no_dsn = {k: v for k, v in os.environ.items() if k != "OWNER_DSN"}
        env_no_dsn["VERIPSA_REPO_ROOT"] = ROOT
        r = subprocess.run(["bash", SCRIPT], env=env_no_dsn, capture_output=True, text=True, timeout=30)
        rc4, log4 = r.returncode, (r.stdout + r.stderr)
        checks.append((f"unset-OWNER_DSN degrade: exits 0 (rc={rc4}) so a fresh service can still deploy and serve", rc4 == 0))
        checks.append(("unset-OWNER_DSN degrade: log says 'OWNER_DSN missing'", "OWNER_DSN missing" in log4))
        checks.append(("unset-OWNER_DSN degrade: log says 'skipping schema apply'", "skipping schema apply" in log4))

    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
        subprocess.run(["dropdb", BAD_CONTRACT_DB], capture_output=True)
        _psql_query(
            "postgresql://localhost/postgres",
            f"DROP ROLE IF EXISTS {extra_claim_role}",
        )

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    if rolling_failure_log:
        # Keep the root nested/role-fixture diagnostic at the END of this
        # standard gate's output so run_gates.sh's bounded failure tail retains
        # it instead of showing only the aggregate rc=1.
        print("--- rolling-upgrade fixture diagnostics ---")
        print(rolling_failure_log)
        print("--- end rolling-upgrade fixture diagnostics ---")
    print("PREDEPLOY SCHEMA-APPLY E2E GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--rolling-upgrade-child":
        raise SystemExit(_run_rolling_upgrade_child())
    raise SystemExit(main())
