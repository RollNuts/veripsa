#!/usr/bin/env bash
# Veripsa GitHub App — pre-deploy schema apply (the "schema can never drift behind code" guard).
#
# FAILURE WINDOW CLOSED BY THIS SCRIPT. Application code can adopt a new function signature or column before
# the database schema is applied. That mismatch makes otherwise healthy processes fail each event at runtime,
# while a shallow liveness probe may remain green. Applying and verifying the schema before traffic promotion
# eliminates the entire class of "code shipped without its schema" failures.
#
# WIRING. render.yaml's `preDeployCommand` for the web service runs this script BEFORE traffic is routed to
# the new image. Render fails the deploy on a non-zero exit (so a broken schema apply blocks the bad code
# from serving). The Dockerfile (1) installs `postgresql-client` so `psql` exists in the image, (2) COPYs
# this script + the entire `db/` tree (schema.sql + the schema/*.sql modules \ir'd by schema.sql) into the
# image so the apply is fully self-contained — no checkout / no git / no external assets.
#
# SAFETY. schema_manifest.py computes a deterministic digest over db/schema.sql and its ordered includes, then
# serializes inspect/apply/stamp with a session advisory lock. The content-free COMMENT ON SCHEMA core marker
# lets an unchanged deploy skip full-schema DDL. A new generation applies the idempotent schema exactly once and
# stamps only after psql succeeds; a rollback behind a newer live generation skips. Malformed state and a reused
# generation with different SQL fail closed. ALWAYS pass `-v ON_ERROR_STOP=1` so a mid-file error fails LOUD
# instead of advancing the marker over a half-applied schema.
#
# OWNER_DSN. Schema apply requires OWNER (DDL) privileges, NOT the least-privilege App role (`veripsa_app`
# can't DDL — by design). OWNER_DSN is a Render SECRET, set by the PO in the dashboard alongside VERIPSA_DSN.
# If OWNER_DSN is UNSET, this script PRINTS A CLEAR LINE + EXITS 0 (degrade to the boot-time contract
# assertion in github-app/server.py that catches drift) — so a fresh service that hasn't had OWNER_DSN wired
# yet can still deploy and serve. The actual MECHANISM that prevents future drift is the loud log line +
# the contract assertion that will block startup if the schema is behind. Once OWNER_DSN is wired, the apply
# runs on every deploy and the runbook's "ONE-TIME DB SETUP" becomes "AUTOMATIC ON EVERY DEPLOY".
#
# HOT-DEPLOY DISCIPLINE. This script applies db/schema.sql while the previous image may still be serving.
# The schema modules therefore follow expand/contract and idempotent hot-deploy rules, enforced by
# test_schema_hot_deploy_guards.py. Permanent indexes are concurrent and every
# replayed column/default/not-null and sequence-owner ensure checks pg_catalog
# before DDL, so an already-applied generation delta never queues a heavyweight
# no-op ahead of live table or graph-sequence writers. A short lock_timeout
# remains mandatory for a genuinely new
# relation change: if live traffic prevents its required lock, this pre-deploy
# fails quickly and Render keeps the previous image serving. Destructive changes
# (DROP COLUMN, RENAME, retype, NOT-NULL added) still MUST use the db/migrations/ pair (apply + rollback +
# post-verify) in a maintenance window.

set -uo pipefail

# psql connect / per-statement timeout. An unbounded connect can stall the pre-deploy phase before the new
# image boots, obscuring the actual database reachability failure. PGCONNECT_TIMEOUT bounds the TCP/SSL
# connect handshake (psql 12+ honors this); a
# bounded statement_timeout via PGOPTIONS bounds any statement that wedges. lock_timeout is deliberately much
# shorter: an AccessExclusive DDL waiter can otherwise sit ahead of ordinary app DML in PostgreSQL's lock queue
# and turn a deploy into a live worker outage. Required options are appended after caller-provided PGOPTIONS so
# a dashboard setting cannot accidentally remove these safety bounds.
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-15}"
PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS="${PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS:-120000}"
PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS="${PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS:-5000}"
case "$PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS" in
  ''|*[!0-9]*)
    echo "[predeploy_schema] FATAL: PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS must be a positive integer."
    exit 2
    ;;
esac
case "$PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS" in
  ''|*[!0-9]*)
    echo "[predeploy_schema] FATAL: PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS must be a positive integer."
    exit 2
    ;;
esac
if [ "$PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS" -le 0 ] || [ "$PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS" -le 0 ]; then
  echo "[predeploy_schema] FATAL: schema statement/lock timeouts must be greater than zero."
  exit 2
fi
if [ "$PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS" -gt 120000 ]; then
  echo "[predeploy_schema] FATAL: PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS may not exceed the 120000ms deploy safety ceiling."
  exit 2
fi
if [ "$PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS" -gt 5000 ]; then
  echo "[predeploy_schema] FATAL: PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS may not exceed the 5000ms live-traffic safety ceiling."
  exit 2
fi
PREDEPLOY_REQUIRED_PGOPTIONS="-c statement_timeout=${PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS} -c lock_timeout=${PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS}"
export PGOPTIONS="${PGOPTIONS:+${PGOPTIONS} }${PREDEPLOY_REQUIRED_PGOPTIONS}"

# Find the repo root: prefer the explicit env (set by Dockerfile / CI), else walk up from this script.
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="${VERIPSA_REPO_ROOT:-$(cd "$HERE/../.." && pwd)}"

SCHEMA_FILE="$REPO_ROOT/db/schema.sql"
GENERATION_FILE="$REPO_ROOT/db/schema_generation"
MANIFEST_HELPER="$REPO_ROOT/github-app/schema_manifest.py"

echo "[predeploy_schema] Veripsa pre-deploy schema apply starting (PID=$$ at $(date -u +%FT%TZ))."
echo "[predeploy_schema] PGCONNECT_TIMEOUT=${PGCONNECT_TIMEOUT}s, statement_timeout=${PREDEPLOY_SCHEMA_STATEMENT_TIMEOUT_MS}ms, lock_timeout=${PREDEPLOY_SCHEMA_LOCK_TIMEOUT_MS}ms."

# --- 1. OWNER_DSN missing → degrade-gracefully (don't block deploy on a fresh service) ----------------------
if [ -z "${OWNER_DSN:-}" ]; then
  echo "[predeploy_schema] OWNER_DSN missing — skipping schema apply (relying on boot-time contract assertion to catch drift)."
  echo "[predeploy_schema] To enable automatic schema apply on every deploy, add OWNER_DSN as a Render secret (owner/migrator DSN, NOT veripsa_app)."
  echo "[predeploy_schema] EXIT 0 (degrade — boot-time assertion is the backup defense)."
  exit 0
fi

# --- 2. psql must be in the image (installed by github-app/Dockerfile via postgresql-client) -----------------
if ! command -v psql >/dev/null 2>&1; then
  echo "[predeploy_schema] FATAL: psql not found in image — install postgresql-client in github-app/Dockerfile."
  echo "[predeploy_schema] EXIT 2 (mis-wired image — Render will fail the deploy, which is the correct response)."
  exit 2
fi

# --- 3. schema.sql must be present (Dockerfile COPYs the db/ tree including db/schema/ modules) -------------
if [ ! -f "$SCHEMA_FILE" ]; then
  echo "[predeploy_schema] FATAL: $SCHEMA_FILE not found — Dockerfile must COPY db/ into the image."
  echo "[predeploy_schema] EXIT 2 (mis-wired image — Render will fail the deploy)."
  exit 2
fi
if [ ! -f "$GENERATION_FILE" ] || [ ! -f "$MANIFEST_HELPER" ]; then
  echo "[predeploy_schema] FATAL: schema generation manifest files are missing from the image."
  echo "[predeploy_schema] EXIT 2 (mis-wired image — Render will fail the deploy)."
  exit 2
fi

# --- 4. Decide/apply/stamp under one session lock ----------------------------------------------------------
# The helper invokes psql with -X -v ON_ERROR_STOP=1 only when the live marker proves an apply is required.
# Its process exit is the deploy result: non-zero keeps the previous image serving.
START_TS=$(date -u +%s)
python3 "$MANIFEST_HELPER"
RC=$?
if [ "$RC" -ne 0 ]; then
  echo "[predeploy_schema] FATAL: schema manifest guard exited non-zero ($RC) — schema apply FAILED."
  echo "[predeploy_schema] Render will mark the deploy FAILED + keep the prior image serving (correct: never serve code whose schema didn't land)."
  exit "$RC"
fi
END_TS=$(date -u +%s)
echo "[predeploy_schema] schema manifest guard OK in $((END_TS - START_TS))s. Render will now start the new image."
exit 0
