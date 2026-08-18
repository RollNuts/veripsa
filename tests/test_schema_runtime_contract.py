#!/usr/bin/env python3
"""BOOT-TIME SCHEMA↔RUNTIME CONTRACT GATE — fail-closed defense against schema drift.

The synthetic regression fixture models application code calling newer function signatures and columns while
the database still exposes the previous shape. Event work fails, but a shallow process probe could stay green.

WHAT THIS GATE PROVES:
  (1) On a healthy (post-schema.sql) DB, check_schema_contract returns healthy=True and the boot path leaves
      schema_contract._SCHEMA_HEALTHY=True → /healthz keeps its today behavior.
  (2) On a DELIBERATELY DRIFTED DB (schema applied except the contention delta, so
      act_for is still at arity=8, declare at arity=6, core.claim has no is_draft column), check_schema_contract
      returns healthy=False with the EXACT violations named (wrong_arity / wrong_arity / missing_column).
  (3) When the boot path runs check_schema_contract → set_boot_result(violation), the /healthz handler returns 503
      (the SAME mechanism Render's deploy gate uses to reject the deploy). Honored even when the worker is
      otherwise healthy — exactly the modeled drift shape.
  (4) The kill switch (VERIPSA_SCHEMA_CONTRACT=0) bypasses the check (returns healthy=True, skipped=True) so an
      operator can force a deploy through during an emergency forward-compat hotfix.
  (5) The runtime contract list (_EXPECTED_FUNCTIONS / _EXPECTED_COLUMNS) AGREES with the live db/schema/*.sql —
      every (name, arity) / (table, column, type) listed in code is reachable in a clean-bootstrapped DB. This
      catches the OTHER drift direction at gate time: a Python change that adds a new SQL function but forgets to
      list it in the contract (which would silently miss the next incident of this shape).

Run:  python3 tests/test_schema_runtime_contract.py   (needs local Postgres with the veripsa roles)
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "github-app"))
import psycopg2  # noqa: E402
import schema_contract as SC  # noqa: E402

# PROCESS-UNIQUE (parallel-safe): the gate bootstraps + drops this DB, so a FIXED name lets concurrent runs
# (parallel CI shards / several agents each running run_gates) drop each other's DB mid-run → "does not exist".
DB = "veripsa_schemactr_" + str(os.getpid())


def _expected_checked() -> int:
    """Mirror the contract categories instead of pinning a stale aggregate magic number."""
    return (
        len(SC._EXPECTED_FUNCTIONS)
        + len(SC._EXPECTED_EXECUTABLE_FUNCTIONS)
        + 2  # governed extractor + semantic producer values
        + len(SC._EXPECTED_COLUMNS)
        + len(SC._EXPECTED_RELATIONS)
        + len(SC._EXPECTED_INDEXES)
        + len(SC._EXPECTED_KIND_CONSTRAINTS)
        + len(SC._EXPECTED_CHECK_CONSTRAINTS)
        + len(SC._EXPECTED_TRIGGERS)
    )


def _psql_file(dsn, path):
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-f", path],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "")[-800:]


def _psql_inline(dsn, sql):
    r = subprocess.run(["psql", dsn, "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
                       cwd=ROOT, capture_output=True, text=True)
    return r.returncode == 0, (r.stderr or "")[-800:]


def _setup_clean_db():
    """Stand up a clean bootstrapped DB (roles + full schema.sql) — the same shape db/bootstrap_local.sh uses."""
    ok, err = _psql_file("postgresql://localhost/postgres", "db/roles.sql")
    if not ok:
        print("roles.sql failed:\n", err)
        return False
    subprocess.run(["dropdb", DB], capture_output=True)
    cr = subprocess.run(["createdb", DB, "-O", "veripsa_migrator"], capture_output=True, text=True)
    if cr.returncode != 0:
        subprocess.run(["createdb", DB], capture_output=True)
    ok, err = _psql_file(f"postgresql://veripsa_migrator@localhost/{DB}", "db/schema.sql")
    if not ok:
        print("schema.sql failed:\n", err)
        return False
    return True


def _setup_drifted_db_fixture():
    """Stand up a synthetic drifted DB: the bulk of the schema is applied, but the contention delta is rolled
    back. core.claim has no is_draft column, act_for is 8-arg, and declare is 6-arg."""
    subprocess.run(["dropdb", DB], capture_output=True)
    cr = subprocess.run(["createdb", DB, "-O", "veripsa_migrator"], capture_output=True, text=True)
    if cr.returncode != 0:
        subprocess.run(["createdb", DB], capture_output=True)
    ok, err = _psql_file(f"postgresql://veripsa_migrator@localhost/{DB}", "db/schema.sql")
    if not ok:
        print("schema.sql failed:\n", err)
        return False
    # Roll back the is_draft delta while leaving the rest of the schema intact.
    mig_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    # Drop the new overloads and re-create the previous 8-arg/6-arg shape.
    rollback_sql = """
        DROP FUNCTION IF EXISTS core.act_for_claim_with_authority(text,text,text,text,text,jsonb,text,boolean,boolean);
        DROP FUNCTION IF EXISTS core.declare_claim_with_authority(text,text,text,text,jsonb,text,boolean);
        DROP FUNCTION IF EXISTS core._set_claim_is_draft(text,text,text,text,text,text,boolean);
        DROP FUNCTION IF EXISTS core.set_change_head_sha_with_authority(text,text,text,text);
        ALTER TABLE core.claim DROP COLUMN IF EXISTS is_draft;
        ALTER TABLE core.claim DROP COLUMN IF EXISTS analyzed_head_sha;
        -- Re-create the previous 8-arg act_for shape (no p_is_draft).
        CREATE OR REPLACE FUNCTION core.act_for_claim_with_authority(
            p_claim_id text, p_target_path text, p_repo text, p_branch text, p_author text,
            p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL, p_author_is_bot boolean DEFAULT false)
        RETURNS jsonb LANGUAGE plpgsql AS $$ BEGIN RETURN '{}'::jsonb; END $$;
        -- Re-create the OLD 6-arg declare the same way.
        CREATE OR REPLACE FUNCTION core.declare_claim_with_authority(
            p_claim_id text, p_target_path text, p_repo text DEFAULT '', p_branch text DEFAULT '',
            p_ranges jsonb DEFAULT NULL, p_base_hash text DEFAULT NULL)
        RETURNS jsonb LANGUAGE plpgsql AS $$ BEGIN RETURN '{}'::jsonb; END $$;
    """
    ok, err = _psql_inline(mig_dsn, rollback_sql)
    if not ok:
        print("drift rollback failed:\n", err)
        return False
    return True


class _DummyWorker:
    """A worker stand-in for the /healthz probe smoke test — health_snapshot reads fields on this; we mirror the
    minimum the real EventQueue exposes. healthy=True so the ONLY thing that can flip /healthz to 503 is the
    schema contract — which is exactly what we're proving."""
    def __init__(self, inflight_age=None, *, worker_count=1, stuck_workers=None):
        self.inflight = 0
        self._lock = None
        self._inflight_age = inflight_age
        self._worker_count = int(worker_count)
        self._stuck_workers = stuck_workers

    def health_snapshot(self):
        return {"healthy": True, "alive": True, "inflight": 0, "queued": 0,
                "queue_depth": 0, "processed": 0, "failed": 0,
                "worker_alive": True, "worker_count": self._worker_count,
                "alive_workers": self._worker_count,
                "inflight_age_seconds": self._inflight_age}

    def stuck_worker_count(self, _threshold):
        if self._stuck_workers is not None:
            return int(
                isinstance(self._inflight_age, (int, float))
                and self._inflight_age >= _threshold
            ) * int(self._stuck_workers)
        return int(isinstance(self._inflight_age, (int, float)))


def _exercise_healthz(
        simulate_violation: bool, *, inflight_age=None,
        worker_count=1, stuck_workers=None,
        graph_liveness=None, health_db=None,
        health_store=None) -> tuple[int, dict]:
    """Drive server_http.make_handler's /healthz route IN-PROCESS (no socket, no Render). The schema_contract
    module is the single source of truth — set_boot_result() simulates the result the boot path would have
    written, then we call the handler's do_GET("/healthz") through a fake socket and read back (status, body)."""
    SC.reset_for_test()
    if simulate_violation:
        # Simulate the drift result the boot path would set: act_for missing at arity=9.
        # (i.e. WRONG ARITY — prod still has the 8-arg version), declare at WRONG arity, is_draft missing.
        SC.set_boot_result(SC.ContractResult(
            healthy=False,
            violations=(
                SC.ContractViolation(kind="wrong_arity",
                                     name="core.act_for_claim_with_authority",
                                     expected="arity=9", actual="arity=[8]"),
                SC.ContractViolation(kind="wrong_arity",
                                     name="core.declare_claim_with_authority",
                                     expected="arity=7", actual="arity=[6]"),
                SC.ContractViolation(kind="missing_column",
                                     name="core.claim.is_draft",
                                     expected="udt_name=bool", actual="MISSING"),
            ),
            checked=4,
            skipped=False,
        ))
    else:
        SC.set_boot_result(SC.ContractResult(healthy=True, violations=(), checked=4, skipped=False))

    # Build the handler the SAME way server.py:serve() does (via make_handler). The worker + db are stubs — the
    # /healthz code path doesn't actually exercise the durable inbox.
    import server_http as SH
    worker = _DummyWorker(
        inflight_age=inflight_age,
        worker_count=worker_count,
        stuck_workers=stuck_workers,
    )
    # The /healthz handler reads watchdog_last_tick_seconds + health_snapshot via `_server()` (the lazy server seam).
    # We patch the seam directly so the test doesn't need to import the full server.py (which would drag in env vars
    # we'd then have to manage). Both functions are tiny + stable enough to stub.
    import sys as _sys
    fake_server = type(_sys)("server")  # ModuleType
    fake_server.health_snapshot = lambda w: dict(w.health_snapshot())
    fake_server.watchdog_last_tick_seconds = lambda: None
    fake_server.graph_freshness_all = lambda db, gh: []
    fake_server.app_identity_ok = lambda dsn: (True, None)
    fake_server._worker_stuck_seconds = lambda: 120.0
    fake_server._worker_restart_seconds = lambda: 180.0
    fake_server.graph_extraction_liveness = lambda _hard: dict(
        graph_liveness
        if isinstance(graph_liveness, dict)
        else {
            "healthy": True,
            "locked": False,
            "stuck": False,
            "active_seconds": None,
            "reaper_owned": False,
            "reaper_seconds": None,
            "hard_seconds": 180.0,
            "waiting_accounts": 0,
        }
    )
    _sys.modules["server"] = fake_server

    def _default_db(sql, args=()):
        # The /healthz freshness summary calls db("SELECT core.owner_graph_freshness_surface()") — return a dict
        # the handler can json-encode. Fail-open is built into the handler, so an exception is also fine; we use
        # a dict here so the body has the freshness block.
        return {"coordinate_count": 0, "max_age_seconds": None}

    HandlerCls = SH.make_handler(
        secret="x",
        store=health_store,
        worker=worker,
        db=health_db or _default_db,
        dsn="postgresql://",
        gh=None,
    )

    class _FakeReq:
        """Minimal request stub: BaseHTTPRequestHandler reads .makefile() for rfile/wfile, then writes the status
        line + headers + body to wfile. Override __init__ so the BaseHTTPRequestHandler constructor doesn't try to
        actually parse a request from a socket."""
        pass

    # Drive the handler manually: instantiate without going through BaseHTTPRequestHandler.__init__ (which expects
    # a socket), then set the fields do_GET reads.
    h = HandlerCls.__new__(HandlerCls)
    h.path = "/healthz"
    h.headers = {}
    # Capture (status, headers, body) into local buffers via these monkeypatch stubs.
    captured = {"status": None, "headers": [], "body": b""}

    def _send_response(code, message=None):
        captured["status"] = code

    def _send_header(k, v):
        captured["headers"].append((k, v))

    def _end_headers():
        pass

    class _Wfile:
        def write(self, b):
            captured["body"] += b

    h.send_response = _send_response
    h.send_header = _send_header
    h.end_headers = _end_headers
    h.wfile = _Wfile()
    h.do_GET()

    try:
        body = json.loads(captured["body"].decode())
    except Exception:
        body = {}
    return captured["status"], body


def _expected_contract_agrees_with_live_schema() -> tuple[bool, str]:
    """Boot the clean DB, then independently inspect pg_catalog to confirm every entry in the
    runtime's _EXPECTED_FUNCTIONS / _EXPECTED_COLUMNS is reachable. This catches the OTHER drift direction at
    gate time: a Python change that adds a new SQL function but forgets to list it (silently next-incident-prone)."""
    conn = psycopg2.connect(f"postgresql://veripsa_migrator@localhost/{DB}")
    conn.autocommit = True
    missing_fn: list[str] = []
    missing_col: list[str] = []
    missing_relation: list[str] = []
    missing_index: list[str] = []
    try:
        with conn.cursor() as cur:
            for name, arity in SC._EXPECTED_FUNCTIONS:
                cur.execute(
                    "SELECT count(*) FROM pg_proc "
                    "WHERE proname = %s AND pronargs = %s "
                    "AND pronamespace = 'core'::regnamespace",
                    (name, arity),
                )
                oid_count = int(cur.fetchone()[0])
                if oid_count != 1:
                    missing_fn.append(
                        f"core.{name}/{arity} (oid_count={oid_count})")
            for schema, table, column, expected_udt in SC._EXPECTED_COLUMNS:
                # Use pg_catalog (the same shape the runtime check uses — see schema_contract.py for why
                # information_schema would silently 0-row for veripsa_app on a hardened DB).
                cur.execute(
                    "SELECT t.typname "
                    "FROM pg_attribute a "
                    "JOIN pg_type t ON a.atttypid=t.oid "
                    "JOIN pg_class c ON a.attrelid=c.oid "
                    "JOIN pg_namespace n ON c.relnamespace=n.oid "
                    "WHERE n.nspname=%s AND c.relname=%s AND a.attname=%s "
                    "AND a.attnum>0 AND NOT a.attisdropped",
                    (schema, table, column),
                )
                row = cur.fetchone()
                if row is None:
                    missing_col.append(f"{schema}.{table}.{column} (MISSING)")
                elif row[0] != expected_udt:
                    missing_col.append(f"{schema}.{table}.{column} (typname={row[0]}, expected {expected_udt})")
            for schema, relation, expected_relkind in SC._EXPECTED_RELATIONS:
                cur.execute(
                    "SELECT c.relkind FROM pg_class c "
                    "JOIN pg_namespace n ON c.relnamespace=n.oid "
                    "WHERE n.nspname=%s AND c.relname=%s",
                    (schema, relation),
                )
                row = cur.fetchone()
                if row is None:
                    missing_relation.append(
                        f"{schema}.{relation} (MISSING)"
                    )
                elif row[0] != expected_relkind:
                    missing_relation.append(
                        f"{schema}.{relation} (relkind={row[0]}, "
                        f"expected {expected_relkind})"
                    )
            for schema, table, index, expected_keys in SC._EXPECTED_INDEXES:
                cur.execute(
                    "SELECT i.indisvalid,i.indisready,pg_get_indexdef(i.indexrelid) "
                    "FROM pg_index i "
                    "JOIN pg_class idx ON idx.oid=i.indexrelid "
                    "JOIN pg_namespace n ON n.oid=idx.relnamespace "
                    "JOIN pg_class tbl ON tbl.oid=i.indrelid "
                    "WHERE n.nspname=%s AND idx.relname=%s "
                    "AND tbl.relname=%s",
                    (schema, index, table),
                )
                row = cur.fetchone()
                if row is None:
                    missing_index.append(f"{schema}.{index} (MISSING)")
                elif (
                    row[0] is not True
                    or row[1] is not True
                    or SC._canonical_index_definition(expected_keys)
                    not in SC._canonical_index_definition(row[2])
                ):
                    missing_index.append(
                        f"{schema}.{index} (invalid/wrong expression)"
                    )
    finally:
        conn.close()
    if missing_fn or missing_col or missing_relation or missing_index:
        return False, (
            f"missing_fn={missing_fn} missing_col={missing_col} "
            f"missing_relation={missing_relation} "
            f"missing_index={missing_index}"
        )
    return True, (
        "all expected function name/arity keys have exactly one OID and all "
        "columns + relations are reachable on the clean-bootstrap DB"
    )


class _BatchedCatalogCursor:
    """Complete synthetic catalog used to pin fixed boot query complexity."""

    def __init__(self, producer_values=None, ambiguous_acl_target=None):
        self.executions: list[str] = []
        self._rows: list[tuple] = []
        self._producer_values = producer_values or (
            SC._EXPECTED_EXTRACTOR_VERSION,
            SC._EXPECTED_SEMANTIC_REF_VERSION,
        )
        self._ambiguous_acl_target = ambiguous_acl_target

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _args=None):
        statement = str(sql)
        self.executions.append(statement)
        if statement.startswith("SET statement_timeout"):
            self._rows = []
        elif "schema-contract:functions" in statement:
            functions = set(SC._EXPECTED_FUNCTIONS)
            functions.update(SC._EXPECTED_EXECUTABLE_FUNCTIONS)
            self._rows = [
                (
                    name,
                    arity,
                    2 if (name, arity) == self._ambiguous_acl_target else 1,
                    False if (name, arity) == self._ambiguous_acl_target
                    else True,
                )
                for name, arity in sorted(functions)
            ]
        elif "schema-contract:producer-values" in statement:
            self._rows = [tuple(self._producer_values)]
        elif "schema-contract:columns" in statement:
            self._rows = [
                (schema, table, column, expected_udt)
                for schema, table, column, expected_udt
                in SC._EXPECTED_COLUMNS
            ]
        elif "schema-contract:relations" in statement:
            self._rows = [
                (schema, relation, expected_relkind)
                for schema, relation, expected_relkind
                in SC._EXPECTED_RELATIONS
            ]
        elif "schema-contract:indexes" in statement:
            self._rows = [
                (schema, table, index, table, True, True, expected_keys)
                for schema, table, index, expected_keys
                in SC._EXPECTED_INDEXES
            ]
        elif "schema-contract:constraints" in statement:
            kind_rows = [
                (
                    schema,
                    table,
                    constraint,
                    "CHECK (kind IN ("
                    + ",".join(f"'{value}'" for value in expected_values)
                    + "))",
                    True,
                )
                for schema, table, constraint, expected_values
                in SC._EXPECTED_KIND_CONSTRAINTS
            ]
            check_rows = [
                (schema, table, constraint, expected_definition, True)
                for schema, table, constraint, expected_definition
                in SC._EXPECTED_CHECK_CONSTRAINTS
            ]
            self._rows = kind_rows + check_rows
        elif "schema-contract:triggers" in statement:
            self._rows = [
                (schema, table, trigger, "O", function, "core")
                for schema, table, trigger, function
                in SC._EXPECTED_TRIGGERS
            ]
        else:
            raise AssertionError(f"unexpected catalog statement: {statement[:80]}")

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _BatchedCatalogConnection:
    autocommit = False

    def __init__(self, producer_values=None, ambiguous_acl_target=None):
        self.catalog = _BatchedCatalogCursor(
            producer_values, ambiguous_acl_target)

    def cursor(self):
        return self.catalog

    def close(self):
        return None


def _exercise_batched_catalog() -> tuple[bool, str]:
    """Healthy cardinality growth must never add database round trips."""
    original_connect = SC._db_connect.connect
    original_functions = SC._EXPECTED_FUNCTIONS
    connections: list[_BatchedCatalogConnection] = []
    ambiguous_acl_target = None
    producer_values = [
        SC._EXPECTED_EXTRACTOR_VERSION,
        SC._EXPECTED_SEMANTIC_REF_VERSION,
    ]

    def _connect(*_args, **_kwargs):
        connection = _BatchedCatalogConnection(
            tuple(producer_values), ambiguous_acl_target)
        connections.append(connection)
        return connection

    try:
        SC._db_connect.connect = _connect
        baseline = SC._check_schema_contract_sync(
            "postgresql://content-free",
            deadline=time.monotonic() + 2.0,
        )
        baseline_executes = len(connections[-1].catalog.executions)
        baseline_setups = sum(
            statement.startswith("SET statement_timeout")
            for statement in connections[-1].catalog.executions
        )

        SC._EXPECTED_FUNCTIONS = original_functions + (
            ("synthetic_contract_cardinality_probe", 0),
        )
        expanded = SC._check_schema_contract_sync(
            "postgresql://content-free",
            deadline=time.monotonic() + 2.0,
        )
        expanded_executes = len(connections[-1].catalog.executions)
        SC._EXPECTED_FUNCTIONS = original_functions
        ambiguous_acl_target = SC._EXPECTED_EXECUTABLE_FUNCTIONS[0]
        ambiguous_acl = SC._check_schema_contract_sync(
            "postgresql://content-free",
            deadline=time.monotonic() + 2.0,
        )
        ambiguous_acl_target = None
        producer_values[:] = ["stale-extractor", 0]
        semantic_drift = SC._check_schema_contract_sync(
            "postgresql://content-free",
            deadline=time.monotonic() + 2.0,
        )
    finally:
        SC._EXPECTED_FUNCTIONS = original_functions
        SC._db_connect.connect = original_connect

    ok = (
        baseline.healthy
        and baseline.checked == _expected_checked()
        and baseline_executes == 8
        and baseline_setups == 1
        and expanded.healthy
        and expanded.checked == _expected_checked() + 1
        and expanded_executes == baseline_executes
        and not ambiguous_acl.healthy
        and {
            (violation.kind, violation.name)
            for violation in ambiguous_acl.violations
        } == {(
            "ambiguous_function_overload",
            "core."
            f"{SC._EXPECTED_EXECUTABLE_FUNCTIONS[0][0]}/"
            f"{SC._EXPECTED_EXECUTABLE_FUNCTIONS[0][1]}",
        )}
        and not semantic_drift.healthy
        and {
            violation.name
            for violation in semantic_drift.violations
            if violation.kind == "wrong_value"
        } == {
            "core.current_extractor_version",
            "core.current_semantic_ref_version",
        }
    )
    return ok, (
        f"baseline_checked={baseline.checked},expanded_checked={expanded.checked},"
        f"baseline_executes={baseline_executes},"
        f"expanded_executes={expanded_executes},setups={baseline_setups}"
    )


def _exercise_late_completion_rejected() -> tuple[bool, str]:
    """A healthy result completed after the absolute wall is never accepted."""
    original_timeout = SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS
    original_sync = SC._check_schema_contract_sync
    original_get_nowait = SC.queue.Queue.get_nowait
    sync_returning = threading.Event()

    def _late_healthy(_dsn, *, deadline=None):
        del deadline
        time.sleep(0.030)
        sync_returning.set()
        return SC.ContractResult(
            healthy=True,
            violations=(),
            checked=_expected_checked(),
            skipped=False,
            phase="complete",
            elapsed_ms=30,
        )

    def _delayed_get_nowait(queue_instance):
        # Deterministically model the caller being descheduled after join()
        # reaches its .020s wall but before it reads the publication queue.
        time.sleep(0.025)
        sync_returning.wait(0.2)
        time.sleep(0.005)
        return original_get_nowait(queue_instance)

    try:
        SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS = 0.020
        SC._check_schema_contract_sync = _late_healthy
        SC.queue.Queue.get_nowait = _delayed_get_nowait
        started_at = time.monotonic()
        result = SC.check_schema_contract("postgresql://content-free")
        elapsed = time.monotonic() - started_at
    finally:
        SC.queue.Queue.get_nowait = original_get_nowait
        SC._check_schema_contract_sync = original_sync
        SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS = original_timeout

    release_deadline = time.monotonic() + 0.5
    while (
        SC._ACTIVE_CHECK_WORKER is not None
        and time.monotonic() < release_deadline
    ):
        time.sleep(0.005)
    ok = (
        sync_returning.is_set()
        and result.healthy is False
        and result.phase == "timeout"
        and any(v.kind == "check_error" for v in result.violations)
        and elapsed < 0.5
        and SC._ACTIVE_CHECK_WORKER is None
    )
    return ok, (
        f"phase={result.phase},healthy={result.healthy},"
        f"elapsed={elapsed:.3f}s"
    )


def _exercise_transient_boot_no_bind() -> tuple[bool, str]:
    """A check_error must leave no live HTTP process or latched result."""
    import http.server as http_server
    import server as S
    import server_boot as SB

    original_load = S.load_config
    original_wire = S.wire_runtime
    original_httpd = http_server.ThreadingHTTPServer
    original_check = SB.check_schema_contract
    original_set = SB.set_boot_result
    original_stdio = SB.install_nonblocking_stdio
    saved_owner_dsn = os.environ.pop("OWNER_DSN", None)
    binds: list[tuple] = []
    boot_results: list[object] = []

    class _ForbiddenHTTPServer:
        def __init__(self, *args, **kwargs):
            binds.append((args, kwargs))
            raise AssertionError("HTTP bind reached after transient contract error")

    try:
        S.load_config = lambda: SB.BootConfig(
            secret="secret",
            dsn="postgresql://content-free",
            gh=object(),
            db=lambda *_args, **_kwargs: None,
        )
        S.wire_runtime = SB.wire_runtime
        http_server.ThreadingHTTPServer = _ForbiddenHTTPServer
        SB.install_nonblocking_stdio = lambda: None
        SB.check_schema_contract = lambda _dsn: SC._operational_check_error(
            phase="timeout",
            actual_type="DatabaseConnectDeadlineExceeded",
            elapsed_ms=50,
        )
        SB.set_boot_result = boot_results.append
        exit_code = None
        try:
            S.serve(0)
        except SystemExit as exc:
            exit_code = exc.code
    finally:
        S.load_config = original_load
        S.wire_runtime = original_wire
        http_server.ThreadingHTTPServer = original_httpd
        SB.check_schema_contract = original_check
        SB.set_boot_result = original_set
        SB.install_nonblocking_stdio = original_stdio
        if saved_owner_dsn is not None:
            os.environ["OWNER_DSN"] = saved_owner_dsn

    ok = exit_code is not None and not binds and not boot_results
    return ok, (
        f"exit={exit_code!r},binds={len(binds)},"
        f"latched_results={len(boot_results)}"
    )


def main() -> int:
    checks: list[tuple[str, bool]] = []

    batch_ok, batch_detail = _exercise_batched_catalog()
    checks.append((
        "schema contract uses one timeout setup plus a fixed seven batched "
        f"queries independent of contract cardinality ({batch_detail})",
        batch_ok,
    ))
    late_ok, late_detail = _exercise_late_completion_rejected()
    checks.append((
        "a healthy worker publication completed after the absolute deadline "
        f"is rejected even when the caller reads it later ({late_detail})",
        late_ok,
    ))
    transient_ok, transient_detail = _exercise_transient_boot_no_bind()
    checks.append((
        "transient schema check failure exits nonzero before HTTP bind and "
        f"is never latched into process health ({transient_detail})",
        transient_ok,
    ))

    # ============================================================================================
    # (1) CLEAN DB — check_schema_contract passes.
    # ============================================================================================
    if not _setup_clean_db():
        print("could not stand up the clean DB")
        return 1

    SC.reset_for_test()
    res = SC.check_schema_contract(f"postgresql://veripsa_app@localhost/{DB}")
    checks.append((f"clean DB → check_schema_contract healthy (checked={res.checked}, "
                   f"violations={[v.kind for v in res.violations]})",
                   res.healthy and res.checked == _expected_checked()
                   and not res.violations and not res.skipped))

    # The runtime contract list AGREES with the live schema (catches drift in the OTHER direction at gate time).
    ok_agree, err_agree = _expected_contract_agrees_with_live_schema()
    checks.append((f"runtime contract list matches the live db/schema/*.sql ({err_agree})", ok_agree))

    # Semantic identity is load-bearing equality, not display metadata. Prove
    # the boot wall rejects three deceptively similar drift shapes: all
    # expected tokens plus OR TRUE, the exact expression left NOT VALID, and
    # a widened generation range. Then prove schema reapply repairs all three.
    mig_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    semantic_drift_sql = """
        ALTER TABLE core.code_node
          DROP CONSTRAINT code_node_semantic_key_shape;
        ALTER TABLE core.code_node
          ADD CONSTRAINT code_node_semantic_key_shape
          CHECK (
            semantic_key IS NULL OR
            (length(semantic_key)=64 AND
             semantic_key ~ '^[0-9a-f]{64}$') OR TRUE
          );
        ALTER TABLE core.code_edge
          DROP CONSTRAINT code_edge_semantic_dst_key_shape;
        ALTER TABLE core.code_edge
          ADD CONSTRAINT code_edge_semantic_dst_key_shape
          CHECK (
            semantic_dst_key IS NULL OR
            (length(semantic_dst_key)=64 AND
             semantic_dst_key ~ '^[0-9a-f]{64}$')
          ) NOT VALID;
        ALTER TABLE core.graph_version
          DROP CONSTRAINT graph_version_semantic_ref_version_shape;
        ALTER TABLE core.graph_version
          ADD CONSTRAINT graph_version_semantic_ref_version_shape
          CHECK (semantic_ref_version IN (0,1,2));
    """
    drifted, drift_err = _psql_inline(mig_dsn, semantic_drift_sql)
    SC.reset_for_test()
    semantic_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    semantic_names = {v.name for v in semantic_res.violations}
    expected_semantic_names = {
        "core.code_node.code_node_semantic_key_shape",
        "core.code_edge.code_edge_semantic_dst_key_shape",
        "core.graph_version.graph_version_semantic_ref_version_shape",
    }
    checks.append((
        f"semantic CHECK weakening/unvalidated/widened drift is unhealthy "
        f"(fixture={drifted}, err={drift_err})",
        drifted and not semantic_res.healthy
        and expected_semantic_names <= semantic_names,
    ))
    repaired, repair_err = _psql_file(mig_dsn, "db/schema/20_core.sql")
    SC.reset_for_test()
    repaired_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    checks.append((
        f"20_core reapply repairs exact validated semantic constraints "
        f"(apply={repaired}, err={repair_err})",
        repaired and repaired_res.healthy,
    ))

    # The semantic-expression and sparse uncertainty indexes are part of the
    # boot contract. A failed concurrent build must not be promoted as a
    # healthy deploy.
    dropped_index, index_drop_err = _psql_inline(
        mig_dsn,
        "DROP INDEX core.code_edge_coord_kind_effective_semantic_dst; "
        "DROP INDEX core.code_edge_coord_uncertain",
    )
    SC.reset_for_test()
    index_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    checks.append((
        f"missing semantic/uncertainty indexes are unhealthy "
        f"(drop={dropped_index}, err={index_drop_err})",
        dropped_index and not index_res.healthy
        and {
            v.name for v in index_res.violations
            if v.kind == "missing_index"
        } >= {
            "core.code_edge_coord_kind_effective_semantic_dst",
            "core.code_edge_coord_uncertain",
        },
    ))
    restored_index, index_restore_err = _psql_file(
        mig_dsn, "db/schema/30_gate.sql"
    )
    SC.reset_for_test()
    restored_index_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    checks.append((
        f"30_gate reapply restores valid semantic/uncertainty indexes "
        f"(apply={restored_index}, err={index_restore_err})",
        restored_index and restored_index_res.healthy,
    ))

    # The retry-window column is not sufficient by itself. Its non-negative
    # finite-rearm wall, terminal-clearing trigger, and direct runtime EXECUTE
    # grant are all boot dependencies: losing any one can make the worker
    # accept traffic and fail or retain stale execution authority later.
    durable_contract_drift = """
        ALTER TABLE core.webhook_delivery
          DROP CONSTRAINT webhook_delivery_auto_rearm_count_ok;
        ALTER TABLE core.webhook_delivery
          DROP CONSTRAINT webhook_delivery_operator_continuation_ok;
        ALTER TABLE core.webhook_delivery
          ADD CONSTRAINT webhook_delivery_operator_continuation_ok
          CHECK (operator_continuation_count >= 0);
        DROP TRIGGER webhook_delivery_clear_terminal_retry_window
          ON core.webhook_delivery;
        DROP INDEX core.webhook_delivery_failed_auto_rearm;
        REVOKE EXECUTE ON FUNCTION
          core.claim_webhook_delivery_with_authority(
            text,int,int,int,text,int)
          FROM veripsa_app;
    """
    durable_drifted, durable_drift_err = _psql_inline(
        mig_dsn, durable_contract_drift,
    )
    SC.reset_for_test()
    durable_drift_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    durable_violations = {
        (violation.kind, violation.name)
        for violation in durable_drift_res.violations
    }
    checks.append((
        f"retry constraint/terminal trigger/runtime ACL/index drift is unhealthy "
        f"(fixture={durable_drifted}, err={durable_drift_err})",
        durable_drifted
        and not durable_drift_res.healthy
        and (
            "missing_constraint",
            "core.webhook_delivery.webhook_delivery_auto_rearm_count_ok",
        ) in durable_violations
        and (
            "wrong_constraint",
            "core.webhook_delivery.webhook_delivery_operator_continuation_ok",
        ) in durable_violations
        and (
            "missing_trigger",
            "core.webhook_delivery.webhook_delivery_clear_terminal_retry_window",
        ) in durable_violations
        and (
            "missing_execute_privilege",
            "core.claim_webhook_delivery_with_authority/6",
        ) in durable_violations
        and (
            "missing_index",
            "core.webhook_delivery_failed_auto_rearm",
        ) in durable_violations,
    ))
    durable_repaired, durable_repair_err = _psql_file(
        mig_dsn, "db/schema/25_webhook_queue.sql",
    )
    SC.reset_for_test()
    durable_repaired_res = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}"
    )
    checks.append((
        f"25_webhook_queue reapply repairs retry constraint/trigger/ACL/index "
        f"(apply={durable_repaired}, err={durable_repair_err})",
        durable_repaired and durable_repaired_res.healthy,
    ))

    # Keep the privilege assertion aligned with every SECURITY DEFINER entry
    # point the durable queue runtime calls directly.  Checking only claim()
    # would still promote a deploy that accepts work and then hits 42501 in
    # enqueue, recovery, fanout, terminal resolution, or liveness.
    durable_runtime_executable_functions = {
        ("enqueue_webhook_delivery_with_authority", 7),
        ("pending_webhook_deliveries_with_authority", 3),
        ("claim_webhook_delivery_with_authority", 6),
        ("recover_ambiguous_webhook_claim_with_authority", 4),
        ("finish_webhook_delivery_with_authority", 2),
        ("resolve_webhook_delivery_release_with_authority", 4),
        ("resolve_webhook_delivery_defer_with_authority", 4),
        ("resolve_webhook_delivery_commit_with_authority", 4),
        ("prepare_webhook_delivery_fanout_with_authority", 3),
        ("complete_webhook_delivery_fanout_repository_with_authority", 3),
        ("resolve_webhook_delivery_fanout_defer_with_authority", 4),
        ("expire_webhook_delivery_lease_with_authority", 4),
        ("stamp_webhook_owner_instance_with_authority", 3),
        ("beat_webhook_worker_instance_with_authority", 1),
        ("reap_dead_instance_leases_with_authority", 3),
        ("webhook_delivery_depth_with_authority", 0),
        ("rearm_failed_webhook_deliveries_with_authority", 3),
        ("escalate_blocked_webhook_deliveries_with_authority", 3),
    }
    missing_durable_acl_contract = (
        durable_runtime_executable_functions
        - set(SC._EXPECTED_EXECUTABLE_FUNCTIONS)
    )
    checks.append((
        "every directly-called durable runtime function has an EXECUTE "
        f"boot contract (missing={sorted(missing_durable_acl_contract)})",
        not missing_durable_acl_contract,
    ))

    # Repository lifecycle delivery handlers call these signatures only after the webhook is accepted. They must
    # therefore be boot-gated explicitly: otherwise /healthz can promote Python ahead of the lifecycle migration
    # and the first add/remove/delete event fails later in the worker with SQLSTATE 42883.
    lifecycle_runtime_functions = {
        ("reactivate_account_with_authority", 1),
        ("reactivate_account_with_authority", 2),
        ("admit_event_installation_generation_with_authority", 2),
        ("repository_event_allowed_with_authority", 2),
        ("repository_account_onboarding_allowed_with_authority", 2),
        ("reactivate_repository_with_authority", 3),
        ("offboard_repository_with_authority", 3),
        ("offboard_repository_with_authority", 4),
        ("_defer_webhook_delivery_with_authority", 3),
        ("defer_webhook_delivery_with_authority", 4),
        ("purge_account_working_set_with_authority", 1),
        ("release_account_claims_with_authority", 1),
        ("release_account_claims_with_authority", 2),
        ("prepare_legacy_repository_offboard_with_authority", 3),
        ("confirm_absent_legacy_repository_offboard_with_authority", 3),
        ("resolve_legacy_repository_offboard_with_authority", 4),
    }
    missing_lifecycle_contract = lifecycle_runtime_functions - set(SC._EXPECTED_FUNCTIONS)
    checks.append((f"repository lifecycle runtime signatures are boot-gated "
                   f"(missing={sorted(missing_lifecycle_contract)})",
                   not missing_lifecycle_contract))

    account_lifecycle_runtime_columns = {
        ("core", "account_lifecycle_tombstone", "active", "bool"),
        ("core", "account_lifecycle_tombstone", "last_event_received_at", "timestamptz"),
        ("core", "account_lifecycle_tombstone", "last_delivery_key", "text"),
        ("core", "account_lifecycle_tombstone", "blocked_installation_id", "text"),
        ("core", "installation_account", "github_installation_id", "text"),
        ("core", "installation_account", "github_installation_created_at", "timestamptz"),
    }
    missing_account_lifecycle_columns = account_lifecycle_runtime_columns - set(SC._EXPECTED_COLUMNS)
    checks.append((f"account lifecycle runtime columns are boot-gated "
                   f"(missing={sorted(missing_account_lifecycle_columns)})",
                   not missing_account_lifecycle_columns))

    # Hot deploy is expand first: the additive generation column must commit
    # before the long function-publication transaction, while every proof
    # consumer still publishes in one catalog transaction.
    gate_sql = open(os.path.join(ROOT, "db", "schema", "30_gate.sql"), encoding="utf-8").read()
    generation_column = gate_sql.index(
        "'installation_account','github_installation_created_at','timestamptz'")
    lifecycle_begin = gate_sql.index(
        "-- Publish every function which consumes the expanded lifecycle shape")
    lifecycle_begin = gate_sql.index("BEGIN;", lifecycle_begin)
    proof_function = gate_sql.index("reactivate_account_with_authority(\n    p_delivery_key text, p_installation_proof jsonb)")
    lifecycle_commit = gate_sql.index("COMMIT;", proof_function)
    checks.append(("generation column expands before atomic proof-function publication",
                   generation_column < lifecycle_begin < proof_function < lifecycle_commit))
    mig_dsn = f"postgresql://veripsa_migrator@localhost/{DB}"
    dropped, drop_err = _psql_inline(
        mig_dsn,
        "ALTER TABLE core.installation_account DROP COLUMN IF EXISTS github_installation_created_at",
    )
    expanded, expand_err = _psql_inline(
        mig_dsn,
        "ALTER TABLE core.installation_account "
        "ADD COLUMN IF NOT EXISTS github_installation_created_at timestamptz",
    ) if dropped else (False, "drop failed")
    survived_interruption = False
    if dropped:
        verify_conn = psycopg2.connect(mig_dsn)
        try:
            with verify_conn, verify_conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM pg_catalog.pg_attribute a "
                    "JOIN pg_catalog.pg_class c ON c.oid=a.attrelid "
                    "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname='core' AND c.relname='installation_account' "
                    "AND a.attname='github_installation_created_at' AND NOT a.attisdropped")
                survived_interruption = cur.fetchone()[0] == 1
        finally:
            verify_conn.close()
    restored, restore_err = _psql_file(mig_dsn, "db/schema/30_gate.sql")
    checks.append((f"interruption after additive expand leaves a usable column and clean reapply converges "
                   f"(drop={drop_err}, expand={expand_err}, restore={restore_err})",
                   dropped and expanded and survived_interruption and restored))

    # ============================================================================================
    # (2) DRIFTED DB fixture — check_schema_contract refuses.
    # ============================================================================================
    if not _setup_drifted_db_fixture():
        print("could not stand up the drifted DB")
        return 1

    SC.reset_for_test()
    res = SC.check_schema_contract(f"postgresql://veripsa_app@localhost/{DB}")
    kinds = {v.kind for v in res.violations}
    names = {v.name for v in res.violations}
    checks.append((f"drifted DB → check_schema_contract UNHEALTHY ({len(res.violations)} violations)",
                   not res.healthy and len(res.violations) >= 3))
    checks.append(("drift detected: core.act_for_claim_with_authority wrong arity (synthetic fixture)",
                   "wrong_arity" in kinds and "core.act_for_claim_with_authority" in names))
    checks.append(("drift detected: core.declare_claim_with_authority wrong arity (synthetic fixture)",
                   "core.declare_claim_with_authority" in names))
    checks.append(("drift detected: core.claim.is_draft missing column (synthetic fixture)",
                   "missing_column" in kinds and "core.claim.is_draft" in names))
    checks.append(("drift detected: claim/head binding function missing (refresh cannot ship ahead of migration)",
                   "missing_function" in kinds and "core.set_change_head_sha_with_authority" in names))
    checks.append(("drift detected: core.claim.analyzed_head_sha missing column",
                   "missing_column" in kinds and "core.claim.analyzed_head_sha" in names))

    # Account-lifecycle drift is the same schema-drift class: Python now calls the one-argument
    # reactivation gate and SQL relies on the durable ordering columns. Prove each missing object flips the
    # contract unhealthy before Render can promote the worker.
    lifecycle_drift_sql = """
        DROP FUNCTION IF EXISTS core.reactivate_account_with_authority(text) CASCADE;
        DROP FUNCTION IF EXISTS core.reactivate_account_with_authority(text,jsonb) CASCADE;
        ALTER TABLE core.account_lifecycle_tombstone DROP COLUMN IF EXISTS active;
        ALTER TABLE core.account_lifecycle_tombstone DROP COLUMN IF EXISTS last_event_received_at;
        ALTER TABLE core.account_lifecycle_tombstone DROP COLUMN IF EXISTS last_delivery_key;
        ALTER TABLE core.account_lifecycle_tombstone DROP COLUMN IF EXISTS blocked_installation_id;
        ALTER TABLE core.installation_account DROP COLUMN IF EXISTS github_installation_id;
        ALTER TABLE core.installation_account DROP COLUMN IF EXISTS github_installation_created_at;
    """
    ok, err = _psql_inline(f"postgresql://veripsa_migrator@localhost/{DB}", lifecycle_drift_sql)
    checks.append((f"account lifecycle drift fixture applied ({err})", ok))
    SC.reset_for_test()
    lifecycle_res = SC.check_schema_contract(f"postgresql://veripsa_app@localhost/{DB}")
    lifecycle_names = {v.name for v in lifecycle_res.violations}
    expected_lifecycle_names = {
        "core.reactivate_account_with_authority",
        "core.account_lifecycle_tombstone.active",
        "core.account_lifecycle_tombstone.last_event_received_at",
        "core.account_lifecycle_tombstone.last_delivery_key",
        "core.account_lifecycle_tombstone.blocked_installation_id",
        "core.installation_account.github_installation_id",
        "core.installation_account.github_installation_created_at",
    }
    checks.append(("account lifecycle schema drift is unhealthy and names every missing dependency",
                   not lifecycle_res.healthy
                   and expected_lifecycle_names <= lifecycle_names))

    # ============================================================================================
    # (3) THE BIG ONE — /healthz returns 503 when the boot path recorded a violation. This is what Render's deploy
    # gate observes; this is what makes "Python ahead of DB schema" deploys mechanically impossible.
    # ============================================================================================
    status, body = _exercise_healthz(simulate_violation=True)
    checks.append((f"/healthz returns 503 when the boot-time contract was VIOLATED (status={status})",
                   status == 503))
    checks.append(("/healthz body NAMES the schema_contract violations (operator sees what is missing)",
                   isinstance(body.get("schema_contract"), dict)
                   and body["schema_contract"].get("healthy") is False
                   and len(body["schema_contract"].get("violations", [])) >= 3))

    # And the inverse: /healthz returns 200 when the boot path saw NO violation.
    status, body = _exercise_healthz(simulate_violation=False)
    checks.append((f"/healthz returns 200 when the boot-time contract PASSED (status={status})",
                   status == 200))
    checks.append(("/healthz body REPORTS schema_contract healthy when the boot path passed",
                   isinstance(body.get("schema_contract"), dict)
                   and body["schema_contract"].get("healthy") is True))
    stuck_status, stuck_body = _exercise_healthz(
        simulate_violation=False, inflight_age=120.0,
    )
    checks.append((
        "single-lane pool exhaustion crosses the hard envelope and /healthz returns 503 for Render restart",
        stuck_status == 503
        and stuck_body.get("worker_stuck") is True
        and stuck_body.get("worker_capacity_exhausted") is True
        and stuck_body.get("healthy") is False,
    ))
    isolated_status, isolated_body = _exercise_healthz(
        simulate_violation=False, inflight_age=120.0,
        worker_count=3, stuck_workers=1,
    )
    checks.append((
        "one pathological keyed lane gets a bounded continuity grace while two workers keep unrelated accounts live",
        isolated_status == 200
        and isolated_body.get("worker_stuck") is True
        and isolated_body.get("stuck_workers") == 1
        and isolated_body.get("available_workers") == 2
        and isolated_body.get("worker_capacity_exhausted") is False
        and isolated_body.get("worker_restart_required") is False
        and isolated_body.get("healthy") is True,
    ))
    restart_status, restart_body = _exercise_healthz(
        simulate_violation=False, inflight_age=180.0,
        worker_count=3, stuck_workers=1,
    )
    checks.append((
        "one stuck lane crossing the absolute grace forces process replacement so its repo cannot remain stranded",
        restart_status == 503
        and restart_body.get("worker_capacity_exhausted") is False
        and restart_body.get("worker_restart_required") is True
        and restart_body.get("restart_required_workers") == 1
        and restart_body.get("healthy") is False,
    ))
    graph_status, graph_body = _exercise_healthz(
        simulate_violation=False,
        graph_liveness={
            "healthy": False,
            "locked": True,
            "stuck": True,
            "active_seconds": 180.0,
            "reaper_owned": True,
            "reaper_seconds": 90.0,
            "hard_seconds": 180.0,
            "waiting_accounts": 2,
        },
    )
    checks.append((
        "a never-returning graph child remains visible after worker inflight clears and forces Render replacement",
        graph_status == 503
        and graph_body.get("healthy") is False
        and graph_body.get(
            "graph_extraction_liveness", {}).get("reaper_owned") is True
        and graph_body.get(
            "graph_extraction_liveness", {}).get("stuck") is True,
    ))

    advisory_release = threading.Event()
    db_started = threading.Event()
    depth_started = threading.Event()

    def _wedged_advisory_db(_sql, _args=()):
        db_started.set()
        advisory_release.wait(3.0)
        return {"coordinate_count": 1, "max_age_seconds": 1.0}

    class _WedgedAdvisoryStore:
        def liveness_snapshot(self):
            return {"healthy": True, "thread_alive": True}

        def depth(self):
            depth_started.set()
            advisory_release.wait(3.0)
            return {"queued": 0, "processing": 0, "failed": 0}

    health_started = time.monotonic()
    nonblocking_status, nonblocking_body = _exercise_healthz(
        simulate_violation=False,
        health_db=_wedged_advisory_db,
        health_store=_WedgedAdvisoryStore(),
    )
    health_elapsed = time.monotonic() - health_started
    refreshes_started = db_started.wait(0.5) and depth_started.wait(0.5)
    advisory_release.set()
    checks.append((
        "Render /healthz never waits for advisory DB freshness or durable-depth I/O",
        nonblocking_status == 200
        and nonblocking_body.get("healthy") is True
        and health_elapsed < 0.5
        and refreshes_started,
    ))

    # The catalog assertion itself runs before HTTP bind. Neither a connector
    # nor an established query may keep the replacement process invisible.
    original_contract_timeout = SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS
    original_contract_connect = SC._db_connect.connect
    SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS = 0.05
    connect_entered = threading.Event()
    release_connect = threading.Event()
    connect_attempts = 0

    def _blocked_contract_connect(*_args, **_kwargs):
        nonlocal connect_attempts
        connect_attempts += 1
        connect_entered.set()
        release_connect.wait(2.0)
        raise RuntimeError("synthetic blocked connector released")

    SC._db_connect.connect = _blocked_contract_connect
    connect_started_at = time.monotonic()
    bounded_connect = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}")
    connect_elapsed = time.monotonic() - connect_started_at
    overlapping_retry = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}")
    release_connect.set()
    active_release_deadline = time.monotonic() + 1.0
    while (
        SC._ACTIVE_CHECK_WORKER is not None
        and time.monotonic() < active_release_deadline
    ):
        time.sleep(0.005)
    SC._db_connect.connect = original_contract_connect
    checks.append((
        "boot schema contract stays bounded and never multiplies a timed-out connector thread",
        connect_entered.is_set()
        and connect_elapsed < 0.5
        and connect_attempts == 1
        and bounded_connect.healthy is False
        and any(v.kind == "check_error"
                for v in bounded_connect.violations)
        and overlapping_retry.healthy is False
        and overlapping_retry.phase == "in_progress"
        and SC._ACTIVE_CHECK_WORKER is None,
    ))

    query_entered = threading.Event()
    release_query = threading.Event()

    class _BlockedCatalogCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs):
            query_entered.set()
            release_query.wait(2.0)

        def fetchall(self):
            return []

        def fetchone(self):
            return None

    class _BlockedCatalogConnection:
        autocommit = False

        def cursor(self):
            return _BlockedCatalogCursor()

        def close(self):
            return None

    SC._db_connect.connect = (
        lambda *_args, **_kwargs: _BlockedCatalogConnection())
    query_started_at = time.monotonic()
    bounded_query = SC.check_schema_contract(
        f"postgresql://veripsa_app@localhost/{DB}")
    query_elapsed = time.monotonic() - query_started_at
    release_query.set()
    SC._db_connect.connect = original_contract_connect
    SC._SCHEMA_CONTRACT_TIMEOUT_SECONDS = original_contract_timeout
    checks.append((
        "boot schema contract returns in time when an established catalog query never returns",
        query_entered.is_set()
        and query_elapsed < 0.5
        and bounded_query.healthy is False
        and any(v.kind == "check_error"
                for v in bounded_query.violations),
    ))

    # ============================================================================================
    # (4) KILL SWITCH — VERIPSA_SCHEMA_CONTRACT=0 bypasses the check entirely (skipped=True, healthy=True).
    # ============================================================================================
    saved = os.environ.get("VERIPSA_SCHEMA_CONTRACT")
    try:
        os.environ["VERIPSA_SCHEMA_CONTRACT"] = "0"
        res = SC.check_schema_contract(f"postgresql://veripsa_app@localhost/{DB}")
        checks.append((f"kill switch (VERIPSA_SCHEMA_CONTRACT=0) → SKIPPED + healthy "
                       f"(skipped={res.skipped}, healthy={res.healthy}, checked={res.checked})",
                       res.skipped is True and res.healthy is True and res.checked == 0))
    finally:
        if saved is None:
            os.environ.pop("VERIPSA_SCHEMA_CONTRACT", None)
        else:
            os.environ["VERIPSA_SCHEMA_CONTRACT"] = saved

    # ============================================================================================
    # (5) get_boot_result + is_healthy round-trip — the surface server_http reads through.
    # ============================================================================================
    SC.reset_for_test()
    checks.append(("default state: no boot check yet → is_healthy() True (probe behaves as before)",
                   SC.is_healthy() is True and SC.get_boot_result() is None))
    SC.set_boot_result(SC.ContractResult(healthy=False, violations=(
        SC.ContractViolation(kind="missing_function", name="core.x", expected="arity=1", actual="MISSING"),
    ), checked=1, skipped=False))
    checks.append(("after a violation: is_healthy() False, get_boot_result() carries the violation",
                   SC.is_healthy() is False and SC.get_boot_result() is not None
                   and SC.get_boot_result().violations[0].kind == "missing_function"))

    # ============================================================================================
    ok_all = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok_all = ok_all and bool(cond)
    print("SCHEMA RUNTIME CONTRACT GATE:", "PASS" if ok_all else "FAIL")
    return 0 if ok_all else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    finally:
        subprocess.run(["dropdb", DB], capture_output=True)
