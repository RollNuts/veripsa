#!/usr/bin/env python3
"""PREDEPLOY SCHEMA-APPLY WIRING gate.

Closes the 2026-06-25 incident class: PR #474 added schema deltas (act_for_claim_with_authority 8→9 args,
declare_claim_with_authority 6→7 args, an is_draft column on core.claim). Render does NOT auto-apply schema
on deploy (render.yaml documented this as ONE-TIME-DB-SETUP, manual + PO-gated). The schema apply step was
forgotten → the deployed Python called the new signatures → prod DB still had the old ones → SQLSTATE 42883
"function does not exist" → every PR webhook aborted silently → no check runs posted for ~1 hour, 11 PRs in
the window got zero Veripsa output.

This gate proves the wires that make the generation-aware auto-apply MECHANICAL on every deploy stay connected:
  1. github-app/scripts/predeploy_schema.sh exists, is executable, invokes schema_manifest.py, and handles
     OWNER_DSN-missing as a clean exit-0 degrade (not a deploy block).
  2. schema_manifest.py computes the digest, holds a session advisory lock, invokes psql with fail-loud flags
     only for fresh/older generations, and stores the content-free marker in COMMENT ON SCHEMA core.
  3. github-app/Dockerfile installs postgresql-client (so psql is in the image) AND copies db/ (so schema.sql
     + the schema/*.sql modules it \ir's are at the expected path).
  4. render.yaml's veripsa-app web service declares `preDeployCommand: bash github-app/scripts/predeploy_schema.sh`
     (the actual wiring that runs it on every deploy).
  5. render.yaml declares OWNER_DSN as a `sync: false` secret on the web service (so it's surfaced in the
     Render dashboard for the PO to set; without it the preDeploy step degrades to a log-only skip).

A future split that breaks ANY of these (renames the script, drops psql from the image, drops the COPY db/,
forgets the preDeployCommand line, drops the OWNER_DSN env stub) FAILS here — before it can ship to prod.
"""
from __future__ import annotations
import os
import stat

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read()


def main() -> int:
    checks = []

    # --- (1) predeploy_schema.sh: present, executable, sound ---------------------------------------------
    script_path = os.path.join(ROOT, "github-app", "scripts", "predeploy_schema.sh")
    script_exists = os.path.isfile(script_path)
    checks.append(("predeploy_schema.sh exists at github-app/scripts/predeploy_schema.sh", script_exists))
    if not script_exists:
        # downstream checks would all FAIL anyway with a missing file — print + return early.
        for name, cond in checks:
            print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        print("PREDEPLOY SCHEMA-APPLY WIRING GATE:", "FAIL")
        return 1

    script_mode = os.stat(script_path).st_mode
    script_executable = bool(script_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
    checks.append(("predeploy_schema.sh is executable (chmod +x — preDeployCommand runs it with `bash` but exec bit is the canonical wire)", script_executable))

    script_txt = _read(script_path)
    checks.append(("predeploy_schema.sh uses `set -uo pipefail` (no silent failures inside the script)", "set -uo pipefail" in script_txt))
    checks.append(("predeploy_schema.sh invokes the schema manifest helper", 'python3 "$MANIFEST_HELPER"' in script_txt))
    checks.append(("predeploy_schema.sh requires db/schema.sql + db/schema_generation + schema_manifest.py in the image", "schema.sql" in script_txt and "schema_generation" in script_txt and "schema_manifest.py" in script_txt))
    checks.append(("predeploy_schema.sh degrades gracefully when OWNER_DSN is unset (logs + exit 0; first-deploy/unwired-secret path)", 'OWNER_DSN:-' in script_txt and "exit 0" in script_txt))
    checks.append(("predeploy_schema.sh exits NON-ZERO on a real psql failure (Render then fails the deploy + keeps the prior image)", 'exit "$RC"' in script_txt or "exit $RC" in script_txt))
    checks.append((
        "predeploy_schema.sh appends mandatory statement_timeout + short lock_timeout after caller PGOPTIONS",
        'PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS="${PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS:-120000}"'
        in script_txt
        and 'PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS="${PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS:-5000}"'
        in script_txt
        and 'PGOPTIONS:+${PGOPTIONS}' in script_txt
        and "-c statement_timeout=${PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS}" in script_txt
        and "-c lock_timeout=${PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS}" in script_txt
        and '"$PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS" -gt 120000' in script_txt
        and '"$PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS" -gt 5000' in script_txt,
    ))

    manifest_path = os.path.join(ROOT, "github-app", "schema_manifest.py")
    manifest_txt = _read(manifest_path)
    generation_txt = _read(os.path.join(ROOT, "db", "schema_generation"))
    checks.append(("schema generation is a positive integer (monotonic; bumped on each schema change)",
                   generation_txt.strip().isdigit() and int(generation_txt.strip()) >= 1))
    checks.append(("manifest digest is SHA-256 over a canonical JSON file inventory", "hashlib.sha256(canonical)" in manifest_txt and "sort_keys=True" in manifest_txt))
    checks.append(("manifest marker is stored only as COMMENT ON SCHEMA core", "COMMENT ON SCHEMA core" in manifest_txt and "CREATE TABLE" not in manifest_txt.upper()))
    checks.append(("inspect/apply/stamp is serialized by a session advisory lock", "pg_advisory_lock" in manifest_txt and "conn.close()" in manifest_txt))
    checks.append(("generation-1 adoption calls the read-only runtime schema contract", "check_schema_contract(app_dsn)" in manifest_txt and 'manifest.generation == 1' in manifest_txt))
    checks.append((
        "full apply uses psql -X -v ON_ERROR_STOP=1 and supplies the exact atomic cutover marker",
        '"-X"' in manifest_txt
        and '"ON_ERROR_STOP=1"' in manifest_txt
        and '"-f"' in manifest_txt
        and '"schema.sql"' in manifest_txt
        and "veripsa_schema_marker=" in manifest_txt
        and "comment_after != expected_marker" in manifest_txt,
    ))
    checks.append(("same-generation mismatch, malformed marker, and future unmarked state are explicit fail-closed decisions", all(token in manifest_txt for token in ("fail_same_generation_digest", "fail_malformed", "fail_unmarked"))))

    schema_txt = _read(os.path.join(ROOT, "db", "schema.sql"))
    bad_ir_lines = []
    for lineno, line in enumerate(schema_txt.splitlines(), start=1):
        stripped = line.strip()
        if not stripped.startswith(r"\ir"):
            continue
        # psql backslash commands do not parse SQL-style inline comments the way
        # SQL statements do. `\ir file.sql -- comment` passes the comment words
        # as extra args; an apostrophe in that "comment" can even produce an
        # unterminated quoted string while the bootstrap still prints "ready".
        parts = stripped.split()
        if len(parts) != 2 or "--" in stripped:
            bad_ir_lines.append(f"{lineno}:{line}")
    checks.append((r"db/schema.sql \ir lines contain only the include command + file path (comments live on preceding lines)",
                   not bad_ir_lines))
    include_lines = [
        line.strip().split()[1]
        for line in schema_txt.splitlines()
        if line.strip().startswith(r"\ir")
    ]
    cutover_path = os.path.join(ROOT, "db", "schema", "100_schema_cutover.sql")
    cutover_txt = _read(cutover_path)
    checks.append((
        "the manifest marker publishes last; legacy fences require the explicit "
        "readiness cutover and share its final transaction",
        include_lines
        and include_lines[-1] == "schema/100_schema_cutover.sql"
        and "BEGIN;" in cutover_txt
        and "COMMIT;" in cutover_txt
        and "veripsa_schema_contract_cutover" in cutover_txt
        and "worker-ready-v1" in cutover_txt
        and "veripsa-legacy-webhook-claim/v1/fenced" in cutover_txt
        and "veripsa-legacy-policy-claim/v1/fenced" in cutover_txt
        and "veripsa-graph-direct/v1/fenced" in cutover_txt
        and "claim_webhook_delivery_with_authority(" in cutover_txt
        and "claim_policy_refresh_with_authority(" in cutover_txt
        and "aclexplode(" in cutover_txt
        and "REVOKE ALL PRIVILEGES ON FUNCTION %s FROM %I CASCADE" in cutover_txt
        and "acl.grantee <> v_target.proowner" in cutover_txt
        and "v_target.owner_name" in cutover_txt
        and "GRANT EXECUTE ON FUNCTION %s TO %I" in cutover_txt
        and "CREATE FUNCTION core._normalize_cutover_acl_v1()" in cutover_txt
        and "SELECT core._normalize_cutover_acl_v1();" in cutover_txt
        and "DROP FUNCTION core._normalize_cutover_acl_v1();" in cutover_txt
        and "DO $veripsa_cutover_acl$" not in cutover_txt
        and "FROM PUBLIC,veripsa_writer,veripsa_app" in cutover_txt
        and "COMMENT ON SCHEMA core IS :'veripsa_schema_marker'" in cutover_txt
        and cutover_txt.index("claim_webhook_delivery_with_authority(")
        < cutover_txt.index("COMMENT ON SCHEMA core IS :'veripsa_schema_marker'")
        and cutover_txt.index("COMMENT ON SCHEMA core IS :'veripsa_schema_marker'")
        < cutover_txt.rindex("COMMIT;"),
    ))
    finalizer_path = os.path.join(
        ROOT, "github-app", "schema_contract_cutover.py")
    finalizer_txt = _read(finalizer_path)
    checks.append((
        "the one-off finalizer proves exact artifact + manifest identity, "
        "shares the schema lock, and exact-verifies the post-state",
        "RENDER_GIT_COMMIT" in finalizer_txt
        and "/app/BUILD_SHA" in finalizer_txt
        and "manifest.marker(\"applied\")" in finalizer_txt
        and "pg_advisory_lock" in finalizer_txt
        and "LOCK_NAMESPACE" in finalizer_txt
        and "LOCK_RESOURCE" in finalizer_txt
        and "before == \"unknown\"" in finalizer_txt
        and "after == \"cut_over\"" in finalizer_txt
        and "_exact_app_execute_acl(cursor, WEBHOOK_V4)" in finalizer_txt
        and "_exact_app_execute_acl(cursor, POLICY_V4)" in finalizer_txt
        and "_exact_app_execute_acl(cursor, POLICY_V5)" in finalizer_txt
        and "_exact_owner_execute_acl(cursor, GRAPH_FULL)" in finalizer_txt
        and "_exact_owner_execute_acl(cursor, GRAPH_PATCH)" in finalizer_txt
        and "aclexplode(" in finalizer_txt
        and "acldefault('f',p.proowner)" in finalizer_txt
        and "veripsa_schema_contract_cutover=" in finalizer_txt,
    ))

    queue_schema_txt = _read(os.path.join(ROOT, "db", "schema", "25_webhook_queue.sql"))
    lifecycle_schema_txt = _read(os.path.join(ROOT, "db", "schema", "35_lifecycle.sql"))
    module_begin = queue_schema_txt.find("BEGIN;")
    lease_column_pos = queue_schema_txt.find(
        "'webhook_delivery','lease_generation'")
    retry_window_column_pos = queue_schema_txt.find(
        "'webhook_delivery','retry_window_expires_at'")
    terminal_trigger_pos = queue_schema_txt.find(
        "CREATE TRIGGER webhook_delivery_clear_terminal_retry_window")
    concurrent_index_pos = queue_schema_txt.find(
        "CREATE INDEX CONCURRENTLY IF NOT EXISTS webhook_delivery_pending")
    claim_atomic_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(\n"
        "    p_key text,\n    p_stale_seconds int,\n    p_max_attempts int,\n"
        "    p_protocol int,\n    p_owner_instance text")
    claim_v2_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(\n"
        "    p_key text,\n    p_stale_seconds int,\n    p_max_attempts int,\n"
        "    p_protocol int\n")
    claim_legacy_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.claim_webhook_delivery_with_authority(\n"
        "    p_key text,\n    p_stale_seconds int DEFAULT 1800")
    switch_start = queue_schema_txt.find("-- BEGIN repository-offboarding rollout switch")
    defer_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core._defer_webhook_delivery_with_authority(", switch_start)
    defer_owner_pos = queue_schema_txt.find(
        "ALTER FUNCTION core._defer_webhook_delivery_with_authority(text,timestamptz,text)", switch_start)
    defer_revoke_pos = queue_schema_txt.find(
        "REVOKE EXECUTE ON FUNCTION core._defer_webhook_delivery_with_authority(text,timestamptz,text)",
        switch_start,
    )
    shim_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.purge_repo_with_authority(p_repo text)", switch_start)
    finish_v2_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.finish_webhook_delivery_with_authority(\n"
        "    p_key text, p_lease_generation bigint)", switch_start)
    finish_legacy_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.finish_webhook_delivery_with_authority(p_key text)", switch_start)
    release_v2_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.release_webhook_delivery_with_authority(\n"
        "    p_key text,\n    p_error text,\n    p_max_attempts int,\n    p_lease_generation bigint", switch_start)
    commit_resolution_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_commit_with_authority(\n"
        "    p_key text,\n    p_error text,\n    p_max_attempts int,\n"
        "    p_lease_generation bigint", switch_start)
    fanout_canonical_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core._canonical_webhook_delivery_fanout_plan(", switch_start)
    fanout_completion_valid_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core._webhook_delivery_fanout_completion_valid(", switch_start)
    fanout_prepare_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.prepare_webhook_delivery_fanout_with_authority(", switch_start)
    fanout_complete_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.complete_webhook_delivery_fanout_repository_with_authority(", switch_start)
    fanout_defer_resolution_pos = queue_schema_txt.find(
        "CREATE OR REPLACE FUNCTION core.resolve_webhook_delivery_fanout_defer_with_authority(", switch_start)
    release_legacy_pos = queue_schema_txt.find(
        "-- Rolling old-worker shim: exactly like finish(/1)", switch_start)
    commit_pos = queue_schema_txt.rfind("COMMIT;")
    switch_end = queue_schema_txt.rfind("-- END atomic queue-protocol + repository-offboarding rollout switch")
    transaction_controls = [
        line.strip() for line in queue_schema_txt.splitlines()
        if line.strip() in ("BEGIN;", "COMMIT;")
    ]
    checks.append((
        "short additive relation changes precede the atomic claims + exact terminal/fanout function publication",
        transaction_controls == ["BEGIN;", "COMMIT;"]
        and -1 < lease_column_pos < retry_window_column_pos
        < terminal_trigger_pos < concurrent_index_pos < module_begin
        < claim_atomic_pos < claim_v2_pos
        < claim_legacy_pos < switch_start
        < defer_pos < defer_owner_pos < defer_revoke_pos < shim_pos
        < fanout_canonical_pos < fanout_completion_valid_pos
        < fanout_prepare_pos < fanout_complete_pos < fanout_defer_resolution_pos < finish_v2_pos
        < finish_legacy_pos < release_v2_pos < commit_resolution_pos
        < release_legacy_pos < commit_pos < switch_end,
    ))
    checks.append((
        "early queue publication preserves only the exact operational /4, rejects unknown ABI drift, and canonicalizes /3",
        "to_regprocedure(" in queue_schema_txt
        and "veripsa_preserve_exact_predecessor_claim_v2" in queue_schema_txt
        and "veripsa_unknown_predecessor_claim_v2" in queue_schema_txt
        and "unsupported existing durable webhook /4 claim ABI" in queue_schema_txt
        and "_claim_v2_preflight_rejection" in manifest_txt
        and queue_schema_txt.index("veripsa_preserve_exact_predecessor_claim_v2")
        < queue_schema_txt.index("veripsa_unknown_predecessor_claim_v2")
        < claim_v2_pos < claim_legacy_pos < queue_schema_txt.rindex("COMMIT;"),
    ))
    checks.append((
        "module 35 does not redefine the one-argument compatibility shim outside the atomic rollout switch",
        "CREATE OR REPLACE FUNCTION core.purge_repo_with_authority(p_repo text)" not in lifecycle_schema_txt,
    ))
    checks.append((
        "cross-account transfer publishes only the proof-required /8 App surface; proofless /2 and /4 are revoked",
        "transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)" in lifecycle_schema_txt
        and "GRANT EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text,text,text,text,text)"
        in lifecycle_schema_txt
        and "REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text)\n"
        "  FROM PUBLIC,veripsa_writer,veripsa_app" in lifecycle_schema_txt
        and "REVOKE EXECUTE ON FUNCTION core.transfer_repo_coordinate_with_authority(text,text,text,text)\n"
        "  FROM PUBLIC,veripsa_writer,veripsa_app" in lifecycle_schema_txt,
    ))

    # --- (2) Dockerfile: postgresql-client installed + db/ COPY'd ----------------------------------------
    dockerfile_path = os.path.join(ROOT, "github-app", "Dockerfile")
    df = _read(dockerfile_path)
    checks.append(("Dockerfile installs postgresql-client (so `psql` exists in the image for the preDeploy step)", "postgresql-client" in df))
    checks.append(("Dockerfile COPYs db/ into the image (so db/schema.sql + db/schema/*.sql modules are at the expected path)", "COPY db/" in df or "COPY ./db/" in df))

    # --- (3) render.yaml: preDeployCommand wires the script ----------------------------------------------
    render_path = os.path.join(ROOT, "render.yaml")
    rt = _read(render_path)
    checks.append(("render.yaml declares preDeployCommand on the veripsa-app web service", "preDeployCommand:" in rt))
    checks.append(("render.yaml's preDeployCommand runs github-app/scripts/predeploy_schema.sh (the actual wiring)", "github-app/scripts/predeploy_schema.sh" in rt))

    # --- (4) render.yaml: OWNER_DSN secret stub surfaces in the dashboard --------------------------------
    # OWNER_DSN should appear as an env key with sync:false somewhere under the web service envVars.
    has_owner_dsn_key = "OWNER_DSN" in rt
    checks.append(("render.yaml lists OWNER_DSN as an env key (surfaces in the dashboard; required by the preDeploy script)", has_owner_dsn_key))

    ok = True
    for name, cond in checks:
        print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
        ok = ok and bool(cond)
    print("PREDEPLOY SCHEMA-APPLY WIRING GATE:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
