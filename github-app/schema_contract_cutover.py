#!/usr/bin/env python3
"""Finalize the production legacy-worker cutover after exact runtime proof.

This is intentionally separate from the generic schema pre-deploy.  The latter
must leave the serving predecessor able to drain if the new worker never
becomes ready.  The deploy workflow runs this helper in an exact target-artifact
Render one-off job only after worker + web + smoke verification.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from schema_manifest import (
    LOCK_NAMESPACE,
    LOCK_RESOURCE,
    _claim_v2_catalog_state,
    _read_live_state,
    build_manifest,
)


ROOT = Path(__file__).resolve().parents[1]
CUTOVER_SQL = ROOT / "db" / "schema" / "100_schema_cutover.sql"
CUTOVER_CONFIRMATION = "worker-ready-v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")

WEBHOOK_OPERATIONAL = "exact_public_predecessor"
WEBHOOK_SAFE = "exact_current_safe"
POLICY4_OPERATIONAL_MD5 = "e4f049d3a611657dc301b3ed3bb65238"
POLICY5_OPERATIONAL_MD5 = "27f6397f7200b51cafe4134b8ff48fbf"
SAFE_SQL_BODY = "SELECT NULL::jsonb"

WEBHOOK_MARKER = "veripsa-legacy-webhook-claim/v1/fenced"
POLICY_MARKER = "veripsa-legacy-policy-claim/v1/fenced"
GRAPH_MARKER = "veripsa-graph-direct/v1/fenced"

WEBHOOK_V4 = (
    "core.claim_webhook_delivery_with_authority"
    "(text,integer,integer,integer)"
)
POLICY_V4 = (
    "core.claim_policy_refresh_with_authority"
    "(text,integer,integer,integer)"
)
POLICY_V5 = (
    "core.claim_policy_refresh_with_authority"
    "(text,integer,integer,integer,boolean)"
)
POLICY_V6 = (
    "core.claim_policy_refresh_with_authority"
    "(text,integer,integer,integer,boolean,integer)"
)
GRAPH_FULL = (
    "core.ingest_graph_with_authority"
    "(jsonb,text,text,text,timestamptz)"
)
GRAPH_PATCH = (
    "core.patch_graph_with_authority"
    "(jsonb,text,text,text[],text[],text,timestamptz)"
)
LEASE_FULL = (
    "core.ingest_graph_with_authority_for_convergence_lease"
    "(jsonb,text,text,text,timestamptz,text,jsonb,bigint,smallint,bigint)"
)
LEASE_PATCH = (
    "core.patch_graph_with_authority_for_convergence_lease"
    "(jsonb,text,text,text[],text[],text,timestamptz,text,jsonb,bigint,smallint,bigint)"
)

CutoverState = Literal["pre_cutover", "cut_over", "unknown"]


@dataclass(frozen=True)
class _SqlCatalog:
    body_md5: str
    body: str
    language: str
    security_definer: bool
    config: list[str] | None
    owner: str
    returns_jsonb: bool
    returns_set: bool
    default_count: int
    arg_names: list[str] | None
    arg_modes: list[str] | None
    all_arg_types: list[int] | None
    strict: bool
    volatility: str
    parallel: str
    leakproof: bool
    kind: str
    not_variadic: bool
    cost: float
    rows: float
    no_support_function: bool
    binary_is_null: bool
    comment: str | None


def _validate_artifact_identity(
    expected_sha: str,
    token: str,
    render_sha: str | None,
    baked_sha: str | None,
) -> bool:
    """Pure fail-closed identity gate, kept separately unit-testable."""
    return bool(
        SHA_RE.fullmatch(expected_sha)
        and TOKEN_RE.fullmatch(token)
        and render_sha == expected_sha
        and baked_sha == expected_sha
    )


def _baked_build_sha() -> str | None:
    # Production images are rooted at /app.  The repository-root fallback is
    # only for an equivalent locally-built artifact layout.
    for path in (Path("/app/BUILD_SHA"), ROOT / "BUILD_SHA"):
        try:
            value = path.read_text(encoding="ascii").strip()
        except OSError:
            continue
        return value
    return None


def _read_sql_catalog(cursor, signature: str) -> _SqlCatalog | None:
    cursor.execute(
        "SELECT md5(p.prosrc),p.prosrc,l.lanname,p.prosecdef,p.proconfig,"
        "r.rolname,p.prorettype='jsonb'::regtype,p.proretset,"
        "p.pronargdefaults,p.proargnames,p.proargmodes,p.proallargtypes,"
        "p.proisstrict,p.provolatile,p.proparallel,p.proleakproof,p.prokind,"
        "p.provariadic=0,p.procost,p.prorows,p.prosupport=0,p.probin IS NULL,"
        "obj_description(p.oid,'pg_proc') "
        "FROM pg_proc p "
        "JOIN pg_language l ON l.oid=p.prolang "
        "JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid=to_regprocedure(%s)",
        (signature,),
    )
    row = cursor.fetchone()
    return None if row is None else _SqlCatalog(*row)


def _exact_sql_surface(
    catalog: _SqlCatalog | None,
    *,
    arg_names: tuple[str, ...],
    body_md5: str | None = None,
    body: str | None = None,
    marker: str | None = None,
) -> bool:
    if catalog is None:
        return False
    return bool(
        catalog.language == "sql"
        and catalog.security_definer is True
        and catalog.config == ["search_path=core, pg_catalog"]
        and catalog.owner == "veripsa_migrator"
        and catalog.returns_jsonb is True
        and catalog.returns_set is False
        and catalog.default_count == 0
        and tuple(catalog.arg_names or ()) == arg_names
        and catalog.arg_modes is None
        and catalog.all_arg_types is None
        and catalog.strict is False
        and catalog.volatility == "v"
        and catalog.parallel == "u"
        and catalog.leakproof is False
        and catalog.kind == "f"
        and catalog.not_variadic is True
        and catalog.cost == 100.0
        and catalog.rows == 0.0
        and catalog.no_support_function is True
        and catalog.binary_is_null is True
        and (body_md5 is None or catalog.body_md5 == body_md5)
        and (body is None or catalog.body == body)
        and catalog.comment == marker
    )


def _privilege(cursor, role: str, signature: str) -> bool:
    cursor.execute(
        "SELECT has_function_privilege(%s,%s,'EXECUTE')",
        (role, signature),
    )
    row = cursor.fetchone()
    return bool(row and row[0] is True)


def _exact_app_execute_acl(cursor, signature: str) -> bool:
    """Require the complete direct ACL to be owner + App EXECUTE only.

    has_function_privilege() is intentionally insufficient for this fence: it
    answers one effective-role question and cannot prove that an unqueried role
    has no direct grant on a SECURITY DEFINER function.  Expand PostgreSQL's
    default ACL when proacl is NULL and compare the complete grantee set.
    """
    cursor.execute(
        "SELECT count(*)=2 "
        "AND count(*) FILTER ("
        "  WHERE a.grantee=p.proowner "
        "    AND a.privilege_type='EXECUTE' AND NOT a.is_grantable"
        ")=1 "
        "AND count(*) FILTER ("
        "  WHERE grantee.rolname='veripsa_app' "
        "    AND a.privilege_type='EXECUTE' AND NOT a.is_grantable"
        ")=1 "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode("
        "  COALESCE(p.proacl,acldefault('f',p.proowner))"
        ") a "
        "LEFT JOIN pg_roles grantee ON grantee.oid=a.grantee "
        "WHERE p.oid=to_regprocedure(%s) "
        "GROUP BY p.oid,p.proowner",
        (signature,),
    )
    row = cursor.fetchone()
    return bool(row and row[0] is True)


def _exact_owner_execute_acl(cursor, signature: str) -> bool:
    """Require that no non-owner has a direct EXECUTE ACL entry."""
    cursor.execute(
        "SELECT count(*)=1 "
        "AND count(*) FILTER ("
        "  WHERE a.grantee=p.proowner "
        "    AND a.privilege_type='EXECUTE' AND NOT a.is_grantable"
        ")=1 "
        "FROM pg_proc p "
        "CROSS JOIN LATERAL aclexplode("
        "  COALESCE(p.proacl,acldefault('f',p.proowner))"
        ") a "
        "WHERE p.oid=to_regprocedure(%s) "
        "GROUP BY p.oid,p.proowner",
        (signature,),
    )
    row = cursor.fetchone()
    return bool(row and row[0] is True)


def _comment(cursor, signature: str) -> str | None:
    cursor.execute(
        "SELECT obj_description(to_regprocedure(%s),'pg_proc')",
        (signature,),
    )
    row = cursor.fetchone()
    return None if row is None else row[0]


def _lease_wrapper_secure(cursor, signature: str) -> bool:
    cursor.execute(
        "SELECT l.lanname='plpgsql',p.prosecdef,"
        "p.proconfig=ARRAY['search_path=core, pg_catalog']::text[],"
        "r.rolname='veripsa_migrator' "
        "FROM pg_proc p "
        "JOIN pg_language l ON l.oid=p.prolang "
        "JOIN pg_roles r ON r.oid=p.proowner "
        "WHERE p.oid=to_regprocedure(%s)",
        (signature,),
    )
    row = cursor.fetchone()
    return bool(
        row == (True, True, True, True)
        and _privilege(cursor, "veripsa_app", signature)
        and not _privilege(cursor, "veripsa_writer", signature)
        and not _privilege(cursor, "veripsa_reader", signature)
    )


def _catalog_state(cursor) -> CutoverState:
    webhook_state = _claim_v2_catalog_state(cursor)
    policy4 = _read_sql_catalog(cursor, POLICY_V4)
    policy5 = _read_sql_catalog(cursor, POLICY_V5)
    policy4_operational = _exact_sql_surface(
        policy4,
        arg_names=(
            "p_worker",
            "p_max_attempts",
            "p_stale_seconds",
            "p_scan_cap",
        ),
        body_md5=POLICY4_OPERATIONAL_MD5,
        marker=None,
    )
    policy5_operational = _exact_sql_surface(
        policy5,
        arg_names=(
            "p_worker",
            "p_max_attempts",
            "p_stale_seconds",
            "p_scan_cap",
            "p_support_graph",
        ),
        body_md5=POLICY5_OPERATIONAL_MD5,
        marker=None,
    )
    policy4_safe = _exact_sql_surface(
        policy4,
        arg_names=(
            "p_worker",
            "p_max_attempts",
            "p_stale_seconds",
            "p_scan_cap",
        ),
        body=SAFE_SQL_BODY,
        marker=POLICY_MARKER,
    )
    policy5_safe = _exact_sql_surface(
        policy5,
        arg_names=(
            "p_worker",
            "p_max_attempts",
            "p_stale_seconds",
            "p_scan_cap",
            "p_support_graph",
        ),
        body=SAFE_SQL_BODY,
        marker=POLICY_MARKER,
    )

    webhook_comment = _comment(cursor, WEBHOOK_V4)
    full_comment = _comment(cursor, GRAPH_FULL)
    patch_comment = _comment(cursor, GRAPH_PATCH)
    full_writer = _privilege(cursor, "veripsa_writer", GRAPH_FULL)
    full_app = _privilege(cursor, "veripsa_app", GRAPH_FULL)
    full_reader = _privilege(cursor, "veripsa_reader", GRAPH_FULL)
    patch_writer = _privilege(cursor, "veripsa_writer", GRAPH_PATCH)
    patch_app = _privilege(cursor, "veripsa_app", GRAPH_PATCH)
    patch_reader = _privilege(cursor, "veripsa_reader", GRAPH_PATCH)

    pre_cutover = (
        webhook_state in (WEBHOOK_OPERATIONAL, WEBHOOK_SAFE)
        and webhook_comment is None
        and policy4_operational
        and policy5_operational
        and full_comment is None
        and patch_comment is None
        and full_writer
        and full_app
        and not full_reader
        and patch_writer
        and patch_app
        and not patch_reader
    )
    cut_over = (
        webhook_state == WEBHOOK_SAFE
        and webhook_comment == WEBHOOK_MARKER
        and policy4_safe
        and policy5_safe
        and full_comment == GRAPH_MARKER
        and patch_comment == GRAPH_MARKER
        and not full_writer
        and not full_app
        and not full_reader
        and not patch_writer
        and not patch_app
        and not patch_reader
        and _exact_owner_execute_acl(cursor, GRAPH_FULL)
        and _exact_owner_execute_acl(cursor, GRAPH_PATCH)
        and _exact_app_execute_acl(cursor, WEBHOOK_V4)
        and _exact_app_execute_acl(cursor, POLICY_V4)
        and _exact_app_execute_acl(cursor, POLICY_V5)
        and _privilege(cursor, "veripsa_app", POLICY_V6)
        and _lease_wrapper_secure(cursor, LEASE_FULL)
        and _lease_wrapper_secure(cursor, LEASE_PATCH)
    )
    if pre_cutover:
        return "pre_cutover"
    if cut_over:
        return "cut_over"
    return "unknown"


def _run_cutover_psql(owner_dsn: str, marker: str) -> int:
    psql = shutil.which("psql")
    if psql is None:
        return 127
    env = os.environ.copy()
    env["PGCONNECT_TIMEOUT"] = "15"
    required_options = "-c statement_timeout=60000 -c lock_timeout=5000"
    env["PGOPTIONS"] = (
        f"{env.get('PGOPTIONS', '')} {required_options}".strip()
    )
    try:
        completed = subprocess.run(
            [
                psql,
                owner_dsn,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                f"veripsa_schema_marker={marker}",
                "-v",
                f"veripsa_schema_contract_cutover={CUTOVER_CONFIRMATION}",
                "-f",
                str(CUTOVER_SQL),
            ],
            cwd=ROOT / "db",
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            timeout=60,
            check=False,
        )
        return completed.returncode
    except (OSError, subprocess.TimeoutExpired):
        # The post-state read under the same advisory lock resolves whether an
        # apparently failed/lost response committed or rolled back.
        return 124


def run(expected_sha: str, token: str) -> int:
    render_sha = os.environ.get("RENDER_GIT_COMMIT")
    baked_sha = _baked_build_sha()
    if not _validate_artifact_identity(
        expected_sha, token, render_sha, baked_sha
    ):
        print(
            "[schema_contract_cutover] FATAL: exact artifact identity was not "
            "proved; no database action taken.",
            flush=True,
        )
        return 2

    owner_dsn = os.environ.get("OWNER_DSN", "")
    if not owner_dsn:
        print(
            "[schema_contract_cutover] FATAL: OWNER_DSN is required; no "
            "database action taken.",
            flush=True,
        )
        return 2
    try:
        manifest = build_manifest(ROOT)
    except (OSError, ValueError):
        print(
            "[schema_contract_cutover] FATAL: checked-in schema manifest is "
            "invalid; no database action taken.",
            flush=True,
        )
        return 2
    expected_marker = manifest.marker("applied")

    import psycopg2

    conn = None
    try:
        conn = psycopg2.connect(
            owner_dsn,
            connect_timeout=15,
            options="-c statement_timeout=60000 -c lock_timeout=5000",
        )
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_lock(hashtext(%s),hashtext(%s))",
                (LOCK_NAMESPACE, LOCK_RESOURCE),
            )
            core_exists, live_marker = _read_live_state(cur)
            if not core_exists or live_marker != expected_marker:
                print(
                    "[schema_contract_cutover] FATAL: live schema marker is "
                    "not this exact artifact's applied manifest; no cutover.",
                    flush=True,
                )
                return 3
            before = _catalog_state(cur)
            if before == "unknown":
                print(
                    "[schema_contract_cutover] FATAL: legacy claim/graph "
                    "catalog state is unknown; no cutover.",
                    flush=True,
                )
                return 3
            if before == "cut_over":
                print(
                    "[schema_contract_cutover] exact production contract was "
                    "already finalized.",
                    flush=True,
                )
                return 0

            psql_rc = _run_cutover_psql(owner_dsn, expected_marker)
            core_after, marker_after = _read_live_state(cur)
            after = _catalog_state(cur)
            if (
                core_after
                and marker_after == expected_marker
                and after == "cut_over"
            ):
                if psql_rc != 0:
                    print(
                        "[schema_contract_cutover] cutover response was "
                        "ambiguous, but exact committed post-state was proved.",
                        flush=True,
                    )
                else:
                    print(
                        "[schema_contract_cutover] exact production contract "
                        "finalized and verified.",
                        flush=True,
                    )
                return 0
            print(
                "[schema_contract_cutover] FATAL: atomic cutover did not "
                "publish the exact verified post-state.",
                flush=True,
            )
            return 3
    except Exception as exc:
        print(
            "[schema_contract_cutover] FATAL: operation failed "
            f"({type(exc).__name__}); cutover not reported successful.",
            flush=True,
        )
        return 3
    finally:
        if conn is not None:
            conn.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2:
        print(
            "usage: schema_contract_cutover.py "
            "<exact-40hex-target-sha> <unique-32hex-token>",
            file=sys.stderr,
        )
        return 2
    return run(args[0], args[1])


if __name__ == "__main__":
    raise SystemExit(main())
